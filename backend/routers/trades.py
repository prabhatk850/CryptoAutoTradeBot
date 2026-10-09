"""P/L, order history, positions and manual close/SL — built from Delta's real positions and fills."""
import asyncio
import logging
import math
import time
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Query

from bot.delta_client import DeltaClient, vwap_fill
from config import settings
from db import db

logger = logging.getLogger("routers.trades")
router = APIRouter(prefix="/trades", tags=["trades"])
_delta = DeltaClient()

_VIEW_TTL = 20.0                                   # /pnl and /orders share one build per symbol
_VIEW_CACHE: dict[str, tuple[float, dict]] = {}
_VIEW_LOCKS: dict[str, asyncio.Lock] = {}
_MARK_TTL = 120.0                                  # a remembered mark older than this is "unknown", not a price
_LAST_MARK: dict[str, tuple[float, float]] = {}    # symbol -> (seen_at, price)
_DECISIONS_TTL = 300.0
_DECISIONS_CACHE: dict[str, object] = {"ts": 0.0, "data": []}
_decisions_task: asyncio.Task | None = None
# Only what _humanize_reason reads — full log docs are ~2KB each and stalled /pnl for ~9s.
_DECISION_FIELDS = {
    "timestamp": 1, "action": 1, "reason": 1,
    "indicators.ema_signal": 1, "indicators.breakout_signal": 1,
    "indicators.breakout_level": 1, "indicators.rsi_signal": 1,
}


def _clean(v):
    """Replace NaN/inf (not JSON-serializable) with None, recursively."""
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_clean(x) for x in v]
    return v


def _fetch_error(exc) -> str:
    """Why a signed Delta read failed, in words for the dashboard."""
    if isinstance(exc, httpx.HTTPStatusError):
        try:
            err = exc.response.json().get("error") or {}
            code = err.get("code") or ""
            if code == "ip_not_whitelisted_for_api_key":
                ip = (err.get("context") or {}).get("client_ip")
                return (f"Delta API key not authorised for this IP ({ip}) — add it to the "
                        f"key's whitelist. Your history is safe, just unreadable.")
            if code:
                return f"Delta rejected the request ({code})"
        except Exception:  # noqa: BLE001
            pass
        return f"Delta returned HTTP {exc.response.status_code}"
    return f"Delta unreachable ({type(exc).__name__})" if isinstance(exc, Exception) else "Delta fills unreachable"


def _parse_dt(v) -> datetime:
    """ISO string or epoch s/ms/µs -> aware UTC (now on garbage)."""
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v / 1e6 if v > 1e14 else v / 1e3 if v > 1e12 else v, tz=timezone.utc)
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        except Exception:
            pass
    return datetime.now(timezone.utc)


def _protection(orders: list[dict], short: bool) -> tuple[float | None, list[float]]:
    """(stop-loss, take-profits nearest-first) from resting reduce-only stop orders."""
    sl, tps = None, []
    for o in orders or []:
        if not o.get("reduce_only") or o.get("stop_price") is None:
            continue
        sp = round(float(o["stop_price"]), 2)
        if o.get("stop_order_type") == "stop_loss_order":
            sl = sp
        elif o.get("stop_order_type") == "take_profit_order":
            tps.append(sp)
    return sl, sorted(tps, reverse=short)


async def _ticker_mark(symbol: str) -> float:
    """Live ticker mark, or 0.0 if unavailable."""
    try:
        t = await _delta.get_ticker(symbol)
        return float(t.get("mark_price") or t.get("close") or 0)
    except Exception:
        return 0.0


async def _resolve_mark(symbol: str) -> tuple[float | None, str]:
    """Mark from ticker → book mid → mark candle → last seen (≤ _MARK_TTL) → None, with its source."""
    mark = await _ticker_mark(symbol)
    if mark:
        return _remember_mark(symbol, mark), "ticker"
    try:  # /tickers can 500 for hours while the book still serves
        b = await _delta.get_orderbook(symbol)
        bid = float(((b.get("buy") or [{}])[0]).get("price") or 0)
        ask = float(((b.get("sell") or [{}])[0]).get("price") or 0)
        if bid and ask:
            return _remember_mark(symbol, (bid + ask) / 2), "book"
    except Exception:
        pass
    try:
        c = await _delta.get_candles(symbol, 1, 1, mark=True)
        if c:
            return _remember_mark(symbol, float(c[-1]["close"])), "mark_candle"
    except Exception:
        pass
    seen = _LAST_MARK.get(symbol)
    if seen and time.time() - seen[0] <= _MARK_TTL:
        return seen[1], "last"
    return None, "unknown"


def _remember_mark(symbol: str, mark: float) -> float:
    _LAST_MARK[symbol] = (time.time(), mark)
    return mark


# --------------------------------------------------------------------------- #
#  Decision reasons (human text for each fill)
# --------------------------------------------------------------------------- #
def _humanize_reason(doc: dict) -> str:
    ind = doc.get("indicators", {}) or {}
    lvl = ind.get("breakout_level")
    parts = []
    if doc.get("action") == "BUY":
        if ind.get("ema_signal") == "BULLISH_CROSS":
            parts.append("EMA9 crossed above EMA21 (uptrend momentum)")
        if ind.get("breakout_signal") == "BREAKOUT_UP" and lvl:
            parts.append(f"price broke above resistance ${lvl:,.0f} (breakout)")
        if ind.get("rsi_signal") == "OVERSOLD":
            parts.append("RSI oversold — bounce expected")
        prefix = "Long entry"
    else:
        if ind.get("ema_signal") == "BEARISH_CROSS":
            parts.append("EMA9 crossed below EMA21 (downtrend momentum)")
        if ind.get("breakout_signal") == "BREAKOUT_DOWN" and lvl:
            parts.append(f"price broke below support ${lvl:,.0f} (breakdown)")
        if ind.get("rsi_signal") == "OVERBOUGHT":
            parts.append("RSI overbought — pullback expected")
        prefix = "Short entry"
    return f"{prefix}: " + "; ".join(parts) if parts else (doc.get("reason") or "Signal triggered")[:90]


async def _load_decisions() -> list[dict]:
    """BUY/SELL decisions with humanized reasons. Never raises (also runs as a background task)."""
    try:
        cursor = (db.trade_logs.find({"action": {"$in": ["BUY", "SELL"]}}, _DECISION_FIELDS)
                  .sort("timestamp", 1).batch_size(2000))
        out = [{"ts": _parse_dt(d.get("timestamp")), "side": 1 if d["action"] == "BUY" else -1,
                "reason": _humanize_reason(d)} async for d in cursor]
        _DECISIONS_CACHE.update(ts=time.time(), data=out)
        return out
    except Exception as e:  # noqa: BLE001 — keep last good data, retry in ~30s
        _DECISIONS_CACHE["ts"] = time.time() - max(0.0, _DECISIONS_TTL - 30)
        logger.warning(f"decision index refresh failed ({type(e).__name__}); serving cached")
        return _DECISIONS_CACHE["data"]


async def _decision_index() -> list[dict]:
    """Stale-while-revalidate: once warm, never blocks a request on Atlas."""
    global _decisions_task
    fresh = time.time() - _DECISIONS_CACHE["ts"] < _DECISIONS_TTL
    if _DECISIONS_CACHE["data"]:
        if not fresh and (_decisions_task is None or _decisions_task.done()):
            _decisions_task = asyncio.create_task(_load_decisions())
        return _DECISIONS_CACHE["data"]
    if fresh:
        return _DECISIONS_CACHE["data"]   # empty DB or failed load: don't hammer it
    return await _load_decisions()        # cold start


def _match_reason(decisions: list[dict], ts: datetime, side: int, max_gap_s: int = 21600) -> str:
    """Reason of the nearest same-side decision within 6h."""
    best, best_gap = None, max_gap_s + 1
    for d in decisions:
        gap = abs((d["ts"] - ts).total_seconds())
        if d["side"] == side and gap < best_gap:
            best, best_gap = d, gap
    if best:
        return best["reason"]
    return "Long entry (market order)" if side > 0 else "Short entry (market order)"


# --------------------------------------------------------------------------- #
#  P/L view
# --------------------------------------------------------------------------- #
async def _build_view(symbol: str) -> dict:
    """Open position from Delta's live position; closed trades = fills grouped into round-trip episodes."""
    cached = _VIEW_CACHE.get(symbol)
    if cached and time.time() - cached[0] < _VIEW_TTL:
        return cached[1]
    async with _VIEW_LOCKS.setdefault(symbol, asyncio.Lock()):
        cached = _VIEW_CACHE.get(symbol)
        if cached and time.time() - cached[0] < _VIEW_TTL:
            return cached[1]
        view = await _compute_view(symbol)
        _VIEW_CACHE[symbol] = (time.time(), view)
        return view


async def _compute_view(symbol: str) -> dict:
    cv = await _delta.get_contract_value(symbol)
    ticker_r, positions_r, fills_r, decisions_r, orders_r = await asyncio.gather(
        _delta.get_ticker(symbol), _delta.get_positions(), _delta.get_fills(page_size=400),
        _decision_index(), _delta.get_live_orders(symbol), return_exceptions=True)

    current_price = float(ticker_r.get("mark_price") or ticker_r.get("close") or 0) if isinstance(ticker_r, dict) else 0.0
    open_size = open_entry = 0.0
    for p in positions_r if isinstance(positions_r, list) else []:
        if p.get("product_symbol") == symbol and p.get("size"):
            open_size, open_entry = float(p["size"]), float(p.get("entry_price") or 0)
            break

    # Resting SL/TP orders, so the chart shows protection even after downtime.
    protection = {"sl": None, "tps": [], "entry": round(open_entry, 2) if open_size else None}
    if isinstance(orders_r, list) and open_size:
        protection["sl"], protection["tps"] = _protection(orders_r, open_size < 0)
    open_opened_at = None
    if open_size:
        try:
            bs = await db.bot_state.find_one({"_id": symbol})
            if bs:
                open_opened_at = bs.get("opened_at")
                ai = bs.get("ai") or {}
                protection.update(ai_reasoning=ai.get("reasoning"), ai_confidence=ai.get("confidence"),
                                  sl_method=bs.get("sl_method"), tp_source=bs.get("tp_source"))
                if bs.get("entry"):        # mark entry the SL/TP are anchored to
                    protection["mark_entry"] = round(float(bs["entry"]), 2)
                if bs.get("fill_price"):
                    protection["fill_price"] = round(float(bs["fill_price"]), 2)
        except Exception:
            pass

    decisions = decisions_r if isinstance(decisions_r, list) else []
    if isinstance(fills_r, list):
        fills, source, source_label = fills_r, "delta", "Live · Delta account"
    else:  # say why — empty history reads as "wiped" (usually an IP whitelist issue)
        fills, source, source_label = [], "fallback", _fetch_error(fills_r)
    trades = []
    for f in fills:
        if f.get("product_symbol") and f["product_symbol"] != symbol:
            continue
        qty, price = float(f.get("size") or 0), float(f.get("price") or 0)
        if qty > 0 and price > 0:
            trades.append({"ts": _parse_dt(f.get("created_at")), "side": 1 if str(f.get("side", "")).lower() == "buy" else -1,
                           "qty": qty, "price": price, "commission": float(f.get("commission") or 0)})
    trades.sort(key=lambda x: x["ts"])
    if not current_price and trades:
        current_price = trades[-1]["price"]

    episodes, markers, daily, run_cum, open_t0 = _replay_fills(trades, cv, decisions, symbol)

    realized_net = round(run_cum, 2)
    closed_pnls = [e["realized_pnl"] for e in episodes]
    wins = [p for p in closed_pnls if p > 0]
    losses = [p for p in closed_pnls if p < 0]

    rows, open_unreal = [], 0.0
    if open_size and open_entry:
        sign = 1 if open_size > 0 else -1
        open_unreal = (current_price - open_entry) * sign * abs(open_size) * cv
        # Open time comes from bot_state; the fills replay only sees the last 400 fills.
        entry_time = (open_opened_at.isoformat() if hasattr(open_opened_at, "isoformat")
                      else open_t0.isoformat() if open_t0 else None)
        rows.append({
            "side": "LONG" if open_size > 0 else "SHORT", "lots": round(abs(open_size), 4),
            "avg_entry": round(open_entry, 2), "avg_exit": None, "realized_pnl": None,
            "unrealized_pnl": round(open_unreal, 2),
            "pnl_pct": round((current_price - open_entry) / open_entry * 100 * sign, 2),
            "status": "Open", "entry_time": entry_time, "exit_time": None,
            "reason": _match_reason(decisions, open_t0, sign) if open_t0 else "",
            "cumulative_pnl": realized_net,
        })
    rows += list(reversed(episodes))

    now = datetime.now(timezone.utc)
    today_realized = daily.get(now.date().isoformat(), 0.0)
    month_realized = sum(v for d, v in daily.items() if d[:7] == now.strftime("%Y-%m"))
    daily_series, cc = [], 0.0
    for d in sorted(daily):
        cc += daily[d]
        daily_series.append({"date": d, "pnl": round(daily[d], 2), "cumulative": round(cc, 2)})

    return {
        "rows": rows,
        "markers": markers,
        "protection": protection,
        "closed_pnls": [round(p, 2) for p in closed_pnls],
        "open_position": round(open_size, 4),
        "open_side": "LONG" if open_size > 0 else "SHORT" if open_size < 0 else "FLAT",
        "open_avg_entry": round(open_entry, 2) if open_size else 0,
        "open_unrealized": round(open_unreal, 2),
        "current_price": round(current_price, 2),
        "today_realized": round(today_realized, 2),
        "today_pnl": round(today_realized + open_unreal, 2),
        "month_pnl": round(month_realized + open_unreal, 2),
        "total_pnl": round(realized_net + open_unreal, 2),
        "realized_pnl": realized_net,
        "unrealized_pnl": round(open_unreal, 2),
        "avg_entry": round(open_entry, 2) if open_size else 0,
        "closed_trades": len(episodes),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(episodes) * 100, 1) if episodes else 0,
        "avg_win": round(sum(wins) / len(wins), 2) if wins else 0,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0,
        "best_trade": round(max(closed_pnls), 2) if closed_pnls else 0,
        "worst_trade": round(min(closed_pnls), 2) if closed_pnls else 0,
        "daily": daily_series,
        "source": source,
        "source_label": source_label,
    }


def _replay_fills(trades: list[dict], cv: float, decisions: list[dict], symbol: str):
    """Group fills into closed round trips. Returns (episodes, markers, daily net, cumulative net, open start)."""
    episodes, markers = [], []
    daily: dict[str, float] = {}
    pos = e_side = 0.0
    e_qty = e_cost = x_qty = x_proc = e_comm = e_real = 0.0
    # Realized P/L uses the average cost of what is STILL open (p_*), not the display totals (e_*);
    # mixing them once turned a ~+$4 trade into a phantom -$824.
    p_qty = p_avg = 0.0
    e_t0 = open_t0 = None
    run_cum = 0.0

    for tr in trades:
        side, qty, price, t, comm = tr["side"], tr["qty"], tr["price"], tr["ts"], tr["commission"]
        markers.append({"timestamp": t.isoformat(), "symbol": symbol, "side": "LONG" if side > 0 else "SHORT",
                        "price": round(price, 2), "pnl_status": "open", "realized_pnl": None})
        signed = side * qty
        if pos == 0:
            e_side, e_qty, e_cost, e_comm = side, qty, price * qty, comm
            x_qty = x_proc = e_real = 0.0
            e_t0 = open_t0 = t
            pos, p_qty, p_avg = signed, qty, price
        elif (pos > 0) == (side > 0):
            e_qty += qty
            e_cost += price * qty
            e_comm += comm
            pos += signed
            p_avg = (p_avg * p_qty + price * qty) / (p_qty + qty)
            p_qty += qty
        else:
            close_qty = min(abs(pos), qty)
            e_real += (price - p_avg) * (1 if e_side > 0 else -1) * close_qty * cv
            p_qty = max(p_qty - close_qty, 0.0)
            x_qty += close_qty
            x_proc += price * close_qty
            e_comm += comm
            pos += signed
            rem = qty - close_qty
            # rem > 0 = this fill closed the position AND opened the opposite one.
            if abs(pos) < 1e-9 or rem > 1e-9:
                avg_e = e_cost / e_qty if e_qty else 0.0
                net = e_real - e_comm
                run_cum += net
                daily[t.date().isoformat()] = daily.get(t.date().isoformat(), 0.0) + net
                episodes.append({
                    "side": "LONG" if e_side > 0 else "SHORT",
                    "lots": round(e_qty, 4),
                    "avg_entry": round(avg_e, 2),
                    "avg_exit": round(x_proc / x_qty if x_qty else 0.0, 2),
                    "realized_pnl": round(net, 2),
                    "pnl_pct": round(e_real / (avg_e * e_qty * cv) * 100 if (avg_e and e_qty) else 0.0, 2),
                    "status": "Closed",
                    "entry_time": e_t0.isoformat() if e_t0 else None,
                    "exit_time": t.isoformat(),
                    "reason": _match_reason(decisions, e_t0, int(e_side)) if e_t0 else "",
                    "cumulative_pnl": round(run_cum, 2),
                })
                pos = e_side = e_qty = e_cost = x_qty = x_proc = e_comm = e_real = p_qty = p_avg = 0.0
                e_t0 = open_t0 = None
                if rem > 1e-9:
                    e_side, e_qty, e_cost = side, rem, price * rem
                    e_t0 = open_t0 = t
                    pos, p_qty, p_avg = side * rem, rem, price
    return episodes, markers, daily, run_cum, open_t0


# --------------------------------------------------------------------------- #
#  Endpoints
# --------------------------------------------------------------------------- #
@router.get("/logs")
async def get_logs(limit: int = Query(50, le=500), skip: int = 0):
    logs = []
    async for doc in db.trade_logs.find().sort("timestamp", -1).skip(skip).limit(limit):
        doc["id"] = str(doc.pop("_id", ""))
        if "timestamp" in doc:
            doc["timestamp"] = doc["timestamp"].isoformat()
        logs.append(_clean(doc))
    return {"logs": logs, "count": len(logs)}


@router.get("/stats")
async def get_stats():
    total, buys, sells, holds, latest = await asyncio.gather(
        db.trade_logs.count_documents({}),
        db.trade_logs.count_documents({"action": "BUY"}),
        db.trade_logs.count_documents({"action": "SELL"}),
        db.trade_logs.count_documents({"action": "HOLD"}),
        db.trade_logs.find_one({"action": {"$in": ["BUY", "SELL"]}}, {"price": 1}, sort=[("timestamp", -1)]),
    )
    return {"total_decisions": total, "buys": buys, "sells": sells, "holds": holds,
            "latest_price": latest["price"] if latest else 0, "symbol": settings.trading_symbol}


async def _exit_quote(symbol: str, size: float, entry: float, cv: float, mark: float) -> dict:
    """What a market close would really fill at (longs sell into bids, shorts buy asks) vs the mark."""
    out = {"exit_price": None, "exit_unrealized": None, "exit_pnl_pct": None,
           "slippage_pct": None, "exit_liquidity_ok": None}
    if not size or not entry:
        return out
    try:
        book = await _delta.get_orderbook(symbol)
    except Exception:
        return out
    px, got = vwap_fill(book.get("buy") if size > 0 else book.get("sell"), abs(size))
    if not px:
        return out
    sign = 1 if size > 0 else -1
    out.update({
        "exit_price": round(px, 2),
        "exit_unrealized": round((px - entry) * sign * abs(size) * cv, 2),
        "exit_pnl_pct": round((px - entry) / entry * 100 * sign, 2),
        "slippage_pct": round(abs(px - mark) / mark * 100, 2) if mark else None,
        "exit_liquidity_ok": got >= abs(size) - 1e-9,
    })
    return out


@router.get("/positions")
async def get_positions():
    """Every open position (all symbols) with SL/TP, mark source and an executable exit quote."""
    try:
        positions = await _delta.get_positions()
    except Exception:
        positions = []
    out = []
    for p in positions:
        size = float(p.get("size") or 0)
        if not size:
            continue
        sym, entry, sign = p.get("product_symbol"), float(p.get("entry_price") or 0), (1 if size > 0 else -1)
        try:
            cv = await _delta.get_contract_value(sym)
        except Exception:
            cv = None  # unknown contract size: show no USD figure rather than a guessed one
        mark, mark_src = await _resolve_mark(sym)
        known = bool(entry) and mark is not None
        try:
            sl, tps = _protection(await _delta.get_live_orders(sym), size < 0)
        except Exception:
            sl, tps = None, []
        row = {
            "symbol": sym, "side": "LONG" if size > 0 else "SHORT", "size": abs(size),
            "entry": round(entry, 2), "mark": round(mark, 2) if mark is not None else None,
            "mark_stale": mark_src not in ("ticker", "book", "mark_candle"), "mark_source": mark_src,
            "unrealized": round((mark - entry) * sign * abs(size) * cv, 2) if known and cv else None,
            "pnl_pct": round((mark - entry) / entry * 100 * sign, 2) if known else None,
            "sl": sl, "tps": tps,
        }
        row.update(await _exit_quote(sym, size, entry, cv, mark or 0) if cv else
                   {"exit_price": None, "exit_unrealized": None, "exit_pnl_pct": None,
                    "slippage_pct": None, "exit_liquidity_ok": None})
        out.append(row)
    return {"positions": out, "active": settings.trading_symbol}


@router.get("/pnl")
async def get_pnl(symbol: str = None):
    """P/L cards for one symbol (shares the cached view with /orders)."""
    v = await _build_view(symbol or settings.trading_symbol)
    return _clean({k: val for k, val in v.items() if k not in ("rows", "markers")})


@router.get("/orders")
async def get_orders(limit: int = Query(10, le=500), offset: int = Query(0, ge=0), symbol: str = None):
    """Paged round-trip history (newest first); markers always cover the full window for the chart."""
    v = await _build_view(symbol or settings.trading_symbol)
    total = len(v["rows"])
    rows = v["rows"][offset:offset + limit]
    return _clean({
        "orders": rows, "markers": v["markers"], "protection": v["protection"],
        "count": len(rows), "total": total, "offset": offset, "limit": limit, "has_more": offset + limit < total,
        **{k: v[k] for k in ("open_position", "open_side", "open_avg_entry", "open_unrealized", "current_price")},
        "total_realized": v["realized_pnl"],
    })


@router.post("/close")
async def close_position(symbol: str, force: bool = False, max_slippage_pct: float | None = None):
    """Cancel protective orders and market-close; refuses (needs_confirm) beyond max slippage unless force=true."""
    sym = symbol.upper()
    try:
        pos = await _delta.get_position_size(sym)
        if abs(pos) < 1e-9:
            return {"ok": False, "error": "No open position for this symbol."}

        tol = settings.close_max_slippage_pct if max_slippage_pct is None else max_slippage_pct
        quote = {}
        if not force and tol > 0:
            entry = float((await _delta.get_position(sym)).get("entry_price") or 0)
            mark = await _ticker_mark(sym)
            quote = await _exit_quote(sym, pos, entry, await _delta.get_contract_value(sym), mark)
            slip = quote.get("slippage_pct")
            if slip is not None and slip > tol:
                return {
                    "ok": False, "needs_confirm": True, **quote, "mark": round(mark, 2),
                    "error": (f"Market close would fill near ${quote['exit_price']:,.2f} "
                              f"({slip:.2f}% off the ${mark:,.2f} mark) for a real "
                              f"P/L of ${quote['exit_unrealized']:,.2f}. Confirm to close anyway."),
                }

        await _delta.cancel_reduce_only(sym, best_effort=True)
        close_side, qty = ("buy" if pos < 0 else "sell"), abs(int(round(pos)))
        order = await _delta.place_order(sym, close_side, qty, reduce_only=True)
        try:  # already flat — a Mongo hiccup must not report the close as failed
            await db.bot_state.delete_one({"_id": sym})
        except Exception as e:  # noqa: BLE001
            logger.warning(f"close {sym}: bot_state cleanup failed ({type(e).__name__})")
        _VIEW_CACHE.pop(sym, None)
        return {"ok": True, "closed": qty, "side": close_side, "order_id": str(order.get("id", "")),
                "expected_price": quote.get("exit_price"), "expected_pnl": quote.get("exit_unrealized")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/sl")
async def update_stop_loss(symbol: str, price: float):
    """Replace the stop-loss with a reduce-only stop at `price` (must be on the protective side of mark)."""
    sym = symbol.upper()
    try:
        pos = await _delta.get_position_size(sym)
        if abs(pos) < 1e-9:
            return {"ok": False, "error": "No open position for this symbol."}
        long = pos > 0
        mark = await _ticker_mark(sym)
        if mark and ((long and price >= mark) or (not long and price <= mark)):
            return {"ok": False, "error": f"SL must be {'below' if long else 'above'} the mark price ({mark:g})."}
        await _delta.cancel_reduce_only(sym, "stop_loss_order", best_effort=True)
        await _delta.place_stop_order(sym, "sell" if long else "buy", abs(int(round(pos))), price, "stop_loss_order")
        await db.bot_state.update_one({"_id": sym}, {"$set": {"sl": price, "sl_method": "manual"}})
        _VIEW_CACHE.pop(sym, None)
        return {"ok": True, "sl": price}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.delete("/logs")
async def clear_logs():
    result = await db.trade_logs.delete_many({})
    return {"deleted": result.deleted_count}
