"""Delta Exchange REST client (testnet by default). Docs: https://docs.delta.exchange/

Reads that fail RAISE — callers treat that as "skip this tick", never as a value (see SKILL.md).
"""
import asyncio
import hashlib
import hmac
import json
import logging
import math
import time
from typing import Optional

import httpx

from config import settings

logger = logging.getLogger("bot.delta_client")

_UA = {"User-Agent": "forexbot/1.0"}
_TICKER_MAX_AGE = 120.0      # older tickers are frozen snapshots (venue has served 2-day-old data with 200)
_RESOLUTIONS = {
    1: "1m", 3: "3m", 5: "5m", 15: "15m", 30: "30m",
    60: "1h", 120: "2h", 240: "4h", 360: "6h", 720: "12h", 1440: "1d", 10080: "1w",
}


def vwap_fill(levels: list[dict], size: float) -> tuple[float, float]:
    """Walk book levels to fill `size`; returns (VWAP price, size actually fillable)."""
    need, cost, got = float(size), 0.0, 0.0
    for lv in levels or []:
        try:
            px, sz = float(lv["price"]), float(lv["size"])
        except (KeyError, TypeError, ValueError):
            continue
        take = min(need, sz)
        if take <= 0:
            break
        cost += px * take
        got += take
        need -= take
        if need <= 1e-9:
            break
    return (cost / got if got else 0.0), got


def minutes_to_resolution(minutes: int) -> str:
    """Interval in minutes -> Delta resolution string (unknown values fall back to 5m)."""
    return _RESOLUTIONS.get(minutes, "5m")


def _parse_candle(c) -> Optional[dict]:
    try:
        if isinstance(c, dict):
            t, o, h, l, cl, vol = c.get("time"), c["open"], c["high"], c["low"], c["close"], c.get("volume", 0)
        elif isinstance(c, list) and len(c) >= 5:
            t, o, h, l, cl = c[:5]
            vol = c[5] if len(c) > 5 else 0
        else:
            return None
        if None in (t, o, h, l, cl):
            return None
        out = {"time": int(t), "open": float(o), "high": float(h), "low": float(l),
               "close": float(cl), "volume": float(vol or 0)}
    except (TypeError, ValueError, KeyError, IndexError):
        return None
    return out if all(math.isfinite(out[k]) for k in ("open", "high", "low", "close")) else None


class DeltaClient:
    def __init__(self):
        self.base_url = settings.delta_base_url.rstrip("/")
        self._meta: dict[tuple[str, str], object] = {}        # (symbol, field) -> static product fact
        self._rcache: dict[str, tuple[float, object]] = {}    # short-TTL read cache
        self._rlocks: dict[str, asyncio.Lock] = {}            # one in-flight fetch per key
        self._client: httpx.AsyncClient | None = None

    async def _cached(self, key: str, ttl: float, factory):
        """Shared short-TTL read; concurrent callers wait on one fetch."""
        ent = self._rcache.get(key)
        if ent and time.time() - ent[0] < ttl:
            return ent[1]
        async with self._rlocks.setdefault(key, asyncio.Lock()):
            ent = self._rcache.get(key)
            if ent and time.time() - ent[0] < ttl:
                return ent[1]
            val = await factory()
            self._rcache[key] = (time.time(), val)
            return val

    def invalidate_cache(self):
        """Drop cached reads (after placing/cancelling orders)."""
        self._rcache.clear()

    async def _send(self, method: str, url: str, **kw) -> httpx.Response:
        """Request on one shared keep-alive pool; a GET whose connection fails is retried once (orders never are)."""
        # A fresh TLS handshake per call turned packet loss into ConnectTimeouts.
        # ponytail: the pool binds to the first event loop that uses it; one loop per process here
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(headers=_UA)
        try:
            return await self._client.request(method, url, **kw)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.RemoteProtocolError):
            if method != "GET":
                raise
            return await self._client.request(method, url, **kw)

    async def _public(self, path: str, params: dict | None = None, timeout: float = 10):
        resp = await self._send("GET", f"{self.base_url}{path}", params=params, timeout=timeout)
        resp.raise_for_status()
        return resp.json().get("result")

    async def _signed(self, method: str, path: str, query: str = "", body: dict | None = None,
                      timeout: float = 10):
        """Authenticated call; Delta signs method + timestamp + path + query + body."""
        payload = json.dumps(body, separators=(",", ":")) if body is not None else ""
        ts = str(int(time.time()))
        sig = hmac.new(settings.delta_api_secret.encode(), (method + ts + path + query + payload).encode(),
                       hashlib.sha256).hexdigest()
        headers = {"api-key": settings.delta_api_key, "timestamp": ts, "signature": sig,
                   "Content-Type": "application/json", **_UA}
        resp = await self._send(method, f"{self.base_url}{path}{query}", content=payload or None,
                                headers=headers, timeout=timeout)
        if method != "GET":
            self.invalidate_cache()  # positions/orders changed (even if the call failed)
        resp.raise_for_status()
        try:
            return resp.json()
        except ValueError:
            return {}

    # ---- market data ----

    async def get_candles(self, symbol: str, resolution: int = 5, limit: int = 100,
                          mark: Optional[bool] = None) -> list[dict]:
        """Last `limit` candles; mark=None follows settings.use_mark_candles (mark candles carry no volume)."""
        end = int(time.time())
        use_mark = settings.use_mark_candles if mark is None else mark
        params = {
            "resolution": minutes_to_resolution(resolution),
            "symbol": f"MARK:{symbol}" if use_mark and not symbol.startswith("MARK:") else symbol,
            "start": end - int(resolution * 60 * limit * 1.5),   # 1.5x window covers testnet gaps
            "end": end,
        }
        # The candle endpoint throws spurious 400s under load; retry with backoff.
        for attempt in range(4):
            try:
                raw = await self._public("/v2/history/candles", params, timeout=15) or []
                break
            except httpx.HTTPStatusError as e:
                logger.warning("Candles request failed (attempt %d/4): %s", attempt + 1, e)
                if attempt == 3:
                    raise
                await asyncio.sleep(0.5 * (attempt + 1))
        candles = sorted((c for c in map(_parse_candle, raw) if c), key=lambda c: c["time"])
        return candles[-limit:]

    async def get_ticker(self, symbol: str) -> dict:
        """Live ticker; RAISES on a stale snapshot rather than serving an old price."""
        async def _fetch():
            r = await self._public(f"/v2/tickers/{symbol}") or {}
            ts = r.get("timestamp")  # microseconds
            if ts and time.time() - float(ts) / 1e6 > _TICKER_MAX_AGE:
                raise RuntimeError(f"{symbol}: ticker is stale by "
                                   f"{(time.time() - float(ts) / 1e6) / 60:.0f} min — refusing it")
            return r
        return await self._cached(f"ticker:{symbol}", 3.0, _fetch)

    async def get_funding_and_oi(self, symbol: str) -> dict:
        """Funding rate + open interest, parsed from the cached ticker."""
        t = await self.get_ticker(symbol)

        def _f(key):
            try:
                return float(t[key]) if t.get(key) is not None else None
            except (TypeError, ValueError):
                return None

        return {"funding_rate": _f("funding_rate"), "oi": _f("oi"), "oi_change_6h": _f("oi_change_usd_6h"),
                "spot_price": _f("spot_price"), "mark_price": _f("mark_price")}

    async def get_orderbook(self, symbol: str) -> dict:
        """L2 book {'buy': bids desc, 'sell': asks asc} — what you can actually trade at, unlike mark."""
        async def _fetch():
            return await self._public(f"/v2/l2orderbook/{symbol}") or {}
        return await self._cached(f"book:{symbol}", 3.0, _fetch)

    # ---- static product facts (memory -> venue -> Mongo) ----

    async def get_product(self, symbol: str) -> dict:
        """Product metadata via the filtered list (this venue 500s on /v2/products/{symbol})."""
        async def _fetch():
            rows = await self._public("/v2/products", {"contract_types": "perpetual_futures"}, timeout=15) or []
            return {p.get("symbol"): p for p in rows}
        return (await self._cached("products", 300.0, _fetch)).get(symbol) or {}

    async def _product_fact(self, symbol: str, field: str, venue_key: str, cast):
        """A never-changing product fact; persisted to Mongo so venue outages and reloads can't lose it."""
        from db import db  # local import keeps the client importable without Mongo
        if (symbol, field) in self._meta:
            return self._meta[(symbol, field)]
        try:
            val = (await self.get_product(symbol)).get(venue_key)
        except Exception:
            val = None
        if val:
            val = cast(val)
            try:
                await db.symbol_meta.update_one({"_id": symbol}, {"$set": {field: val}}, upsert=True)
            except Exception:
                pass
        else:
            try:
                doc = await db.symbol_meta.find_one({"_id": symbol}) or {}
            except Exception:
                doc = {}
            if not doc.get(field):
                return None
            val = cast(doc[field])
            logger.warning(f"{symbol}: {field} {val} from persisted cache — venue unreachable")
        self._meta[(symbol, field)] = val
        return val

    async def get_product_id(self, symbol: str) -> Optional[int]:
        """Numeric product id every order call needs; None if never learned."""
        return await self._product_fact(symbol, "product_id", "id", int)

    async def get_taker_fee(self, symbol: str) -> Optional[float]:
        """Taker commission rate (e.g. 0.0005), or None if never learned."""
        return await self._product_fact(symbol, "taker_fee", "taker_commission_rate", float)

    async def get_contract_value(self, symbol: str) -> float:
        """Underlying units per contract (scales P/L to USD). RAISES if unknown — a guess was 10x off once."""
        cv = await self._product_fact(symbol, "contract_value", "contract_value", float)
        if not cv:
            raise RuntimeError(f"{symbol}: contract_value unknown — refusing to guess")
        return cv

    # ---- account ----

    async def get_positions(self) -> list[dict]:
        async def _fetch():
            return (await self._signed("GET", "/v2/positions/margined")).get("result") or []
        return await self._cached("positions", 12.0, _fetch)

    async def get_position_size(self, symbol: str) -> float:
        """Signed contracts held (0 = flat). RAISES on failure: a false "flat" cancels live stops."""
        for p in await self.get_positions():
            if p.get("product_symbol") == symbol and p.get("size"):
                return float(p["size"])
        return 0.0

    async def get_position(self, symbol: str) -> dict:
        """Position dict for the symbol, or {} if flat/unreadable."""
        try:
            return next((p for p in await self.get_positions()
                         if p.get("product_symbol") == symbol and p.get("size")), {})
        except Exception:
            return {}

    async def get_wallet(self) -> dict:
        """USD/USDT settlement balance row."""
        rows = (await self._signed("GET", "/v2/wallet/balances")).get("result") or []
        return next((r for r in rows if r.get("asset_symbol") in ("USD", "USDT")), rows[0] if rows else {})

    async def get_fills(self, page_size: int = 200) -> list[dict]:
        async def _fetch():
            return (await self._signed("GET", "/v2/fills", f"?page_size={page_size}", timeout=15)).get("result") or []
        return await self._cached(f"fills:{page_size}", 15.0, _fetch)

    async def get_live_orders(self, symbol: str) -> list[dict]:
        """Open + pending orders for the symbol (cached 8s)."""
        async def _fetch():
            pid = await self.get_product_id(symbol)
            q = f"?product_ids={pid}&states=open,pending"
            return (await self._signed("GET", "/v2/orders", q)).get("result") or []
        return await self._cached(f"orders:{symbol}", 8.0, _fetch)

    # ---- orders ----

    async def place_order(self, symbol: str, side: str, size: int, reduce_only: bool = False) -> dict:
        """Market order."""
        body = {"product_id": await self.get_product_id(symbol), "product_symbol": symbol,
                "size": int(size), "side": side, "order_type": "market_order"}
        if reduce_only:
            body["reduce_only"] = True
        return (await self._signed("POST", "/v2/orders", body=body, timeout=15)).get("result") or {}

    async def place_stop_order(self, symbol: str, side: str, size: int, stop_price: float, kind: str) -> dict:
        """Reduce-only stop triggered on mark price; kind = take_profit_order | stop_loss_order."""
        body = {"product_id": await self.get_product_id(symbol), "product_symbol": symbol,
                "size": int(size), "side": side, "order_type": "market_order", "stop_order_type": kind,
                "stop_price": f"{stop_price:.1f}", "reduce_only": True, "stop_trigger_method": "mark_price"}
        return (await self._signed("POST", "/v2/orders", body=body, timeout=15)).get("result") or {}

    async def cancel_order(self, order_id, product_id) -> dict:
        """RAISES on failure, so callers never drop state for an order still on the book."""
        return await self._signed("DELETE", "/v2/orders", body={"id": int(order_id), "product_id": int(product_id)})

    async def cancel_reduce_only(self, symbol: str, stop_kind: Optional[str] = None,
                                 best_effort: bool = False) -> None:
        """Cancel the symbol's reduce-only orders (optionally one stop_order_type); best_effort skips failures."""
        pid = await self.get_product_id(symbol)
        for o in await self.get_live_orders(symbol):
            if o.get("reduce_only") and (stop_kind is None or o.get("stop_order_type") == stop_kind):
                try:
                    await self.cancel_order(o["id"], pid)
                except Exception:
                    if not best_effort:
                        raise

    async def set_leverage(self, symbol: str, leverage: int) -> dict:
        pid = await self.get_product_id(symbol)
        return (await self._signed("POST", f"/v2/products/{pid}/orders/leverage",
                                   body={"leverage": str(leverage)})).get("result") or {}
