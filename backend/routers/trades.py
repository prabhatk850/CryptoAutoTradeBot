import asyncio
import logging
import math
import time as _time
from datetime import datetime, timezone
from fastapi import APIRouter, Query
from db import db
from config import settings
from bot.delta_client import DeltaClient, vwap_fill

logger = logging.getLogger("routers.trades")
router = APIRouter(prefix="/trades", tags=["trades"])
_delta = DeltaClient()

# Short-lived cache so /pnl and /orders (polled together) share one fetch.
_VIEW_CACHE: dict[str, tuple[float, dict]] = {}
_VIEW_TTL = 20.0  # seconds (bot acts every 5m, so 20s-stale views are fine + keep UI fast)
_VIEW_LOCKS: dict[str, asyncio.Lock] = {}  # dedupe concurrent builds per symbol
_DECISIONS_CACHE: dict[str, object] = {"ts": 0.0, "data": []}
_DECISIONS_TTL = 300.0            # decisions change at most once per bot tick
_decisions_task: asyncio.Task | None = None   # in-flight background refresh

# Only the fields _humanize_reason() actually reads. The full documents average ~2 KB
# (they carry the order payload + the whole indicator snapshot), and pulling thousands
# of them from Atlas on a user request is what used to stall /trades/pnl for ~9s.
_DECISION_FIELDS = {
    "timestamp": 1, "action": 1, "reason": 1,
    "indicators.ema_signal": 1, "indicators.breakout_signal": 1,
    "indicators.breakout_level": 1, "indicators.rsi_signal": 1,
}


def _clean(v):
    """Recursively replace NaN/inf (not JSON-serializable) with None."""
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_clean(x) for x in v]
    return v


def _fetch_error(exc) -> str:
    """Human-readable reason a signed Delta read failed, for the dashboard."""
    import httpx
    if isinstance(exc, httpx.HTTPStatusError):
        try:
            err = exc.response.json().get("error") or {}
            code = err.get("code") or ""
            ip = (err.get("context") or {}).get("client_ip")
            if code == "ip_not_whitelisted_for_api_key":
                return (f"Delta API key not authorised for this IP ({ip}) — add it to the "
                        f"key's whitelist. Your history is safe, just unreadable.")
            if code:
                return f"Delta rejected the request ({code})"
        except Exception:  # noqa: BLE001 — body wasn't the shape we expected
            pass
        return f"Delta returned HTTP {exc.response.status_code}"
    if isinstance(exc, Exception):
        return f"Delta unreachable ({type(exc).__name__})"
    return "Delta fills unreachable"


def _serialize(doc: dict) -> dict:
    doc["id"] = str(doc.pop("_id", ""))
    if "timestamp" in doc:
        doc["timestamp"] = doc["timestamp"].isoformat()
    return _clean(doc)


def _parse_dt(v) -> datetime:
    """Parse Delta timestamps (ISO string or epoch sec/ms/us) to aware UTC."""
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, (int, float)):
        ts = v / 1e6 if v > 1e14 else v / 1e3 if v > 1e12 else v
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        except Exception:
            pass
    return datetime.now(timezone.utc)


def _humanize_reason(doc: dict) -> str:
    """Plain-English explanation of why the bot entered this trade."""
    ind = doc.get("indicators", {}) or {}
    action = doc.get("action")
    lvl = ind.get("breakout_level")
    parts = []
    if action == "BUY":
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
    if not parts:
        return (doc.get("reason") or "Signal triggered")[:90]
    return f"{prefix}: " + "; ".join(parts)


async def _load_decisions() -> list[dict]:
    """Pull BUY/SELL decisions from Mongo (projected + batched) and humanize them.

    Never raises: this also runs as a detached background task, and a Mongo outage
    must not take the PnL view down with it — reasons just fall back to generic text.
    """
    try:
        cursor = (db.trade_logs
                  .find({"action": {"$in": ["BUY", "SELL"]}}, _DECISION_FIELDS)
                  .sort("timestamp", 1)
                  .batch_size(2000))
        out = []
        async for d in cursor:
            out.append({
                "ts": _parse_dt(d.get("timestamp")),
                "side": 1 if d["action"] == "BUY" else -1,
                "reason": _humanize_reason(d),
            })
        _DECISIONS_CACHE.update(ts=_time.time(), data=out)
        return out
    except Exception as e:  # noqa: BLE001 — keep last good data, retry in ~30s
        _DECISIONS_CACHE["ts"] = _time.time() - max(0.0, _DECISIONS_TTL - 30)
        logger.warning(f"decision index refresh failed ({type(e).__name__}); serving cached")
        return _DECISIONS_CACHE["data"]


async def _decision_index() -> list[dict]:
    """Bot BUY/SELL decisions (with humanized reasons).

    Stale-while-revalidate: once warm, this *never* blocks a request — a stale cache is
    returned immediately and refreshed in the background. Decisions only feed the
    human-readable "reason" text, so serving one a few minutes old costs nothing,
    whereas blocking on a slow Atlas round-trip stalls the whole dashboard.
    """
    global _decisions_task
    fresh = _time.time() - _DECISIONS_CACHE["ts"] < _DECISIONS_TTL
    if _DECISIONS_CACHE["data"]:
        if not fresh and (_decisions_task is None or _decisions_task.done()):
            _decisions_task = asyncio.create_task(_load_decisions())
        return _DECISIONS_CACHE["data"]
    if fresh:
        return _DECISIONS_CACHE["data"]      # empty DB (or failed load): don't hammer it
    return await _load_decisions()           # cold start only


def _match_reason(decisions: list[dict], ts: datetime, side: int, max_gap_s: int = 21600) -> str:
    """Find the bot decision that most likely produced this fill (same side, nearest time)."""
    best, best_gap = None, max_gap_s + 1
    for d in decisions:
        if d["side"] != side:
            continue
        gap = abs((d["ts"] - ts).total_seconds())
        if gap < best_gap:
            best, best_gap = d, gap
    if best:
        return best["reason"]
    return "Long entry (market order)" if side > 0 else "Short entry (market order)"


async def _build_view(symbol: str) -> dict:
    """
    Source of truth for the PnL cards + Order History.

    - The OPEN position comes straight from Delta's real position endpoint, so
      the dashboard always matches your Delta account (no drift).
    - CLOSED trades are grouped into round-trip "episodes" (one row per trade:
      avg entry, avg exit, realized P/L), built from real fills.
    - Cached for a few seconds so /pnl and /orders share one fetch.
    """
    now_mono = _time.time()
    cached = _VIEW_CACHE.get(symbol)
    if cached and now_mono - cached[0] < _VIEW_TTL:
        return cached[1]

    # Serialize concurrent builds for the same symbol (e.g. /pnl + /orders fired
    # together) so they share ONE fetch instead of racing.
    lock = _VIEW_LOCKS.setdefault(symbol, asyncio.Lock())
    async with lock:
        cached = _VIEW_CACHE.get(symbol)
        if cached and _time.time() - cached[0] < _VIEW_TTL:
            return cached[1]

        cv = await _delta.get_contract_value(symbol)  # cached after first call

        # Fetch ticker, positions, fills, decisions, live orders concurrently.
        ticker_r, positions_r, fills_r, decisions_r, orders_r = await asyncio.gather(
            _delta.get_ticker(symbol),
            _delta.get_positions(),
            _delta.get_fills(page_size=400),
            _decision_index(),
            _delta.get_live_orders(symbol),
            return_exceptions=True,
        )

    current_price = 0.0
    if isinstance(ticker_r, dict):
        current_price = float(ticker_r.get("mark_price") or ticker_r.get("close") or 0)

    open_size = 0.0
    open_entry = 0.0
    if isinstance(positions_r, list):
        for p in positions_r:
            if p.get("product_symbol") == symbol and p.get("size"):
                open_size = float(p.get("size") or 0)
                open_entry = float(p.get("entry_price") or 0)
                break

    # Protective orders resting on Delta (so SL/TP show on the chart + survive downtime)
    protection = {"sl": None, "tps": [], "entry": round(open_entry, 2) if open_size else None}
    if isinstance(orders_r, list) and open_size:
        for o in orders_r:
            if not o.get("reduce_only") or o.get("stop_price") is None:
                continue
            sp = round(float(o["stop_price"]), 2)
            if o.get("stop_order_type") == "stop_loss_order":
                protection["sl"] = sp
            elif o.get("stop_order_type") == "take_profit_order":
                protection["tps"].append(sp)
        protection["tps"].sort(reverse=(open_size < 0))  # nearest-to-entry first
    if open_size:
        try:
            bs = await db.bot_state.find_one({"_id": symbol})
            if bs:
                ai = bs.get("ai") or {}
                protection["ai_reasoning"] = ai.get("reasoning")
                protection["ai_confidence"] = ai.get("confidence")
                protection["sl_method"] = bs.get("sl_method")
                protection["tp_source"] = bs.get("tp_source")
                # mark entry the bot used (SL/TP are anchored to this; aligns the chart's
                # entry line with the mark candles even when the fill slipped on the testnet)
                if bs.get("entry"):
                    protection["mark_entry"] = round(float(bs["entry"]), 2)
                if bs.get("fill_price"):
                    protection["fill_price"] = round(float(bs["fill_price"]), 2)
        except Exception:
            pass

    decisions = decisions_r if isinstance(decisions_r, list) else []
    source, source_label = "delta", "Live · Delta account"
    fills = fills_r if isinstance(fills_r, list) else []
    if not isinstance(fills_r, list):
        # Say WHY. Zeros with a vague label read as "my history was wiped" — the most
        # common cause is a dynamic IP falling off the API key's whitelist.
        source, source_label = "fallback", _fetch_error(fills_r)
    trades = []
    for f in fills:
        if f.get("product_symbol") and f["product_symbol"] != symbol:
            continue
        side = 1 if str(f.get("side", "")).lower() == "buy" else -1
        qty = float(f.get("size") or 0)
        price = float(f.get("price") or 0)
        if qty <= 0 or price <= 0:
            continue
        trades.append({"ts": _parse_dt(f.get("created_at")), "side": side, "qty": qty,
                       "price": price, "commission": float(f.get("commission") or 0)})
    trades.sort(key=lambda x: x["ts"])
    if not current_price and trades:
        current_price = trades[-1]["price"]

    # ---- group fills into round-trip episodes ----
    episodes = []          # closed round trips
    markers = []           # per-fill markers for the chart
    pos = e_side = 0.0
    e_qty = e_cost = x_qty = x_proc = e_comm = e_real = 0.0
    e_t0 = None
    open_t0 = None
    run_cum = 0.0
    daily: dict[str, float] = {}

    def _finish_episode(exit_t):
        nonlocal run_cum
        avg_e = e_cost / e_qty if e_qty else 0.0
        avg_x = x_proc / x_qty if x_qty else 0.0
        net = e_real - e_comm
        run_cum += net
        day = exit_t.date().isoformat() if exit_t else None
        if day:
            daily[day] = daily.get(day, 0.0) + net
        episodes.append({
            "side": "LONG" if e_side > 0 else "SHORT",
            "lots": round(e_qty, 4),
            "avg_entry": round(avg_e, 2),
            "avg_exit": round(avg_x, 2),
            "realized_pnl": round(net, 2),
            "pnl_pct": round((e_real / (avg_e * e_qty * cv) * 100) if (avg_e and e_qty) else 0.0, 2),
            "status": "Closed",
            "entry_time": e_t0.isoformat() if e_t0 else None,
            "exit_time": exit_t.isoformat() if exit_t else None,
            "reason": _match_reason(decisions, e_t0, int(e_side)) if e_t0 else "",
            "cumulative_pnl": round(run_cum, 2),
        })

    for tr in trades:
        side, qty, price, t, comm = tr["side"], tr["qty"], tr["price"], tr["ts"], tr["commission"]
        markers.append({"timestamp": t.isoformat(), "symbol": symbol,
                        "side": "LONG" if side > 0 else "SHORT", "price": round(price, 2),
                        "pnl_status": "open", "realized_pnl": None})
        signed = side * qty
        if pos == 0:
            e_side, e_qty, e_cost = side, qty, price * qty
            x_qty = x_proc = e_real = 0.0
            e_comm = comm
            e_t0 = open_t0 = t
            pos = signed
        elif (pos > 0) == (side > 0):
            e_qty += qty
            e_cost += price * qty
            e_comm += comm
            pos += signed
        else:
            close_qty = min(abs(pos), qty)
            avg_e = e_cost / e_qty if e_qty else 0.0
            e_real += (price - avg_e) * (1 if e_side > 0 else -1) * close_qty * cv
            x_qty += close_qty
            x_proc += price * close_qty
            e_comm += comm
            pos += signed
            rem = qty - close_qty
            if abs(pos) < 1e-9:
                _finish_episode(t)
                pos = e_side = e_qty = e_cost = x_qty = x_proc = e_comm = e_real = 0.0
                e_t0 = open_t0 = None
                if rem > 1e-9:
                    e_side, e_qty, e_cost = side, rem, price * rem
                    x_qty = x_proc = e_comm = e_real = 0.0
                    e_t0 = open_t0 = t
                    pos = side * rem

    realized_net = round(run_cum, 2)
    closed_pnls = [e["realized_pnl"] for e in episodes]
    wins = [p for p in closed_pnls if p > 0]
    losses = [p for p in closed_pnls if p < 0]

    # OPEN row from Delta's real position (overrides any fills reconstruction)
    rows = []
    open_unreal = 0.0
    if open_size != 0 and open_entry:
        sign = 1 if open_size > 0 else -1
        open_unreal = (current_price - open_entry) * sign * abs(open_size) * cv
        rows.append({
            "side": "LONG" if open_size > 0 else "SHORT",
            "lots": round(abs(open_size), 4),
            "avg_entry": round(open_entry, 2),
            "avg_exit": None,
            "realized_pnl": None,
            "unrealized_pnl": round(open_unreal, 2),
            "pnl_pct": round((current_price - open_entry) / open_entry * 100 * sign, 2) if open_entry else 0.0,
            "status": "Open",
            "entry_time": open_t0.isoformat() if open_t0 else None,
            "exit_time": None,
            "reason": _match_reason(decisions, open_t0, sign) if open_t0 else "",
            "cumulative_pnl": realized_net,
        })
    rows += list(reversed(episodes))  # closed, newest first

    now = datetime.now(timezone.utc)
    today_key = now.date().isoformat()
    month_key = now.strftime("%Y-%m")
    today_realized = daily.get(today_key, 0.0)
    month_realized = sum(v for d, v in daily.items() if d[:7] == month_key)

    cc = 0.0
    daily_series = []
    for d in sorted(daily.keys()):
        cc += daily[d]
        daily_series.append({"date": d, "pnl": round(daily[d], 2), "cumulative": round(cc, 2)})

    view = {
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
    _VIEW_CACHE[symbol] = (now_mono, view)
    return view


@router.get("/logs")
async def get_logs(limit: int = Query(50, le=500), skip: int = 0):
    cursor = db.trade_logs.find().sort("timestamp", -1).skip(skip).limit(limit)
    logs = [_serialize(doc) async for doc in cursor]
    return {"logs": logs, "count": len(logs)}


@router.get("/stats")
async def get_stats():
    # One round-trip's worth of latency instead of five: Atlas is remote, so issuing
    # these sequentially cost ~5x more than running them concurrently.
    total, buys, sells, holds, latest = await asyncio.gather(
        db.trade_logs.count_documents({}),
        db.trade_logs.count_documents({"action": "BUY"}),
        db.trade_logs.count_documents({"action": "SELL"}),
        db.trade_logs.count_documents({"action": "HOLD"}),
        db.trade_logs.find_one({"action": {"$in": ["BUY", "SELL"]}},
                               {"price": 1}, sort=[("timestamp", -1)]),
    )
    return {
        "total_decisions": total,
        "buys": buys,
        "sells": sells,
        "holds": holds,
        "latest_price": latest["price"] if latest else 0,
        "symbol": settings.trading_symbol,
    }


async def _exit_quote(symbol: str, size: float, entry: float, cv: float, mark: float) -> dict:
    """What closing this position RIGHT NOW would actually fill at.

    Closing a long sells into the bids; closing a short buys from the asks. Mark price
    sits between the two and is not tradeable, so a position can show a mark profit
    while the executable exit is a loss — that gap is what this exposes.
    """
    out = {"exit_price": None, "exit_unrealized": None, "exit_pnl_pct": None,
           "slippage_pct": None, "exit_liquidity_ok": None}
    if not size or not entry:
        return out
    try:
        book = await _delta.get_orderbook(symbol)
    except Exception:
        return out
    levels = book.get("buy") if size > 0 else book.get("sell")
    px, got = vwap_fill(levels, abs(size))
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
    """Every open position across all symbols (so the dashboard can show BTC + ETH
    together regardless of which chart is active), with SL/TP and unrealized P/L."""
    out = []
    try:
        positions = await _delta.get_positions()
    except Exception:
        positions = []
    for p in positions:
        size = float(p.get("size") or 0)
        if not size:
            continue
        sym = p.get("product_symbol")
        entry = float(p.get("entry_price") or 0)
        cv = await _delta.get_contract_value(sym)
        mark = entry
        try:
            t = await _delta.get_ticker(sym)
            mark = float(t.get("mark_price") or t.get("close") or entry)
        except Exception:
            pass
        unreal = (mark - entry) * (1 if size > 0 else -1) * abs(size) * cv if entry else 0.0
        sl, tps = None, []
        try:
            for o in await _delta.get_live_orders(sym):
                if not o.get("reduce_only") or o.get("stop_price") is None:
                    continue
                sp = round(float(o["stop_price"]), 2)
                if o.get("stop_order_type") == "stop_loss_order":
                    sl = sp
                elif o.get("stop_order_type") == "take_profit_order":
                    tps.append(sp)
        except Exception:
            pass
        row = {
            "symbol": sym, "side": "LONG" if size > 0 else "SHORT", "size": abs(size),
            "entry": round(entry, 2), "mark": round(mark, 2), "unrealized": round(unreal, 2),
            "pnl_pct": round((mark - entry) / entry * 100 * (1 if size > 0 else -1), 2) if entry else 0,
            "sl": sl, "tps": sorted(tps, reverse=(size < 0)),
        }
        row.update(await _exit_quote(sym, size, entry, cv, mark))
        out.append(row)
    return {"positions": out, "active": settings.trading_symbol}


async def _traded_symbols() -> list[str]:
    """All symbols the bot trades + any with an open position (for aggregate PnL)."""
    syms = {s.strip().upper() for s in settings.trade_symbols.split(",") if s.strip()}
    try:
        for p in await _delta.get_positions():
            if p.get("size") and p.get("product_symbol"):
                syms.add(p["product_symbol"])
    except Exception:
        pass
    return sorted(syms)


@router.get("/pnl")
async def get_pnl(symbol: str = None):
    """PnL cards for ONE coin (the chart symbol). Shares the cached view with
    /trades/orders for the same symbol, so it's fast. Cross-coin view is the
    Open Positions strip + /trades/positions."""
    v = await _build_view(symbol or settings.trading_symbol)
    return _clean({k: val for k, val in v.items() if k not in ("rows", "markers")})


@router.get("/orders")
async def get_orders(limit: int = Query(10, le=500), offset: int = Query(0, ge=0), symbol: str = None):
    """Order History for one symbol (chart) — round-trip trades + markers.
    Rows are newest-first (open position, then closed). `limit`+`offset` power the
    dashboard's page-by-page navigation; `markers` always covers the full window
    so the chart isn't affected by paging."""
    v = await _build_view(symbol or settings.trading_symbol)
    all_rows = v["rows"]
    total = len(all_rows)
    rows = all_rows[offset:offset + limit]
    return _clean({
        "orders": rows,
        "markers": v["markers"],
        "protection": v["protection"],
        "count": len(rows),
        "total": total,
        "offset": offset,
        "limit": limit,
        "has_more": offset + limit < total,
        "open_position": v["open_position"],
        "open_side": v["open_side"],
        "open_avg_entry": v["open_avg_entry"],
        "open_unrealized": v["open_unrealized"],
        "current_price": v["current_price"],
        "total_realized": v["realized_pnl"],
    })


@router.post("/close")
async def close_position(symbol: str, force: bool = False,
                         max_slippage_pct: float | None = None):
    """Manually close a symbol's open position at market: cancel its protective
    (reduce-only) orders, then send a reduce-only market order to flatten it.

    A market close fills against the book, which on this venue can sit percent(s)
    away from mark. Rather than silently turning a mark-profit into a real loss, an
    exit priced worse than `max_slippage_pct` from mark is refused and the true
    numbers are returned so the caller can confirm; `force=true` sends it anyway.
    """
    sym = symbol.upper()
    try:
        pos = await _delta.get_position_size(sym)
        if abs(pos) < 1e-9:
            return {"ok": False, "error": "No open position for this symbol."}

        tol = settings.close_max_slippage_pct if max_slippage_pct is None else max_slippage_pct
        quote = {}
        if not force and tol > 0:
            p = await _delta.get_position(sym)
            entry = float(p.get("entry_price") or 0)
            cv = await _delta.get_contract_value(sym)
            mark = 0.0
            try:
                t = await _delta.get_ticker(sym)
                mark = float(t.get("mark_price") or t.get("close") or 0)
            except Exception:
                pass
            quote = await _exit_quote(sym, pos, entry, cv, mark)
            slip = quote.get("slippage_pct")
            if slip is not None and slip > tol:
                return {
                    "ok": False, "needs_confirm": True, **quote,
                    "mark": round(mark, 2),
                    "error": (f"Market close would fill near ${quote['exit_price']:,.2f} "
                              f"({slip:.2f}% off the ${mark:,.2f} mark) for a real "
                              f"P/L of ${quote['exit_unrealized']:,.2f}. "
                              "Confirm to close anyway."),
                }

        pid = await _delta.get_product_id(sym)
        for o in await _delta.get_live_orders(sym):
            if o.get("reduce_only"):
                try:
                    await _delta.cancel_order(o["id"], pid)
                except Exception:
                    pass
        close_side = "buy" if pos < 0 else "sell"
        qty = abs(int(round(pos)))
        order = await _delta.place_order(sym, close_side, qty, reduce_only=True)
        # The position is already flat at this point — a Mongo hiccup while clearing
        # local state must not report a successful close as a failure.
        try:
            await db.bot_state.delete_one({"_id": sym})
        except Exception as e:  # noqa: BLE001
            logger.warning(f"close {sym}: bot_state cleanup failed ({type(e).__name__})")
        _delta.invalidate_cache()
        _VIEW_CACHE.pop(sym, None)
        return {"ok": True, "closed": qty, "side": close_side,
                "order_id": str(order.get("id", "")),
                "expected_price": quote.get("exit_price"),
                "expected_pnl": quote.get("exit_unrealized")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/sl")
async def update_stop_loss(symbol: str, price: float):
    """Manually move the stop-loss: cancel the existing SL stop order and place a new
    reduce-only stop at `price` (validated to be on the correct side of the mark)."""
    sym = symbol.upper()
    try:
        pos = await _delta.get_position_size(sym)
        if abs(pos) < 1e-9:
            return {"ok": False, "error": "No open position for this symbol."}
        long = pos > 0
        try:
            t = await _delta.get_ticker(sym)
            mark = float(t.get("mark_price") or t.get("close") or 0)
        except Exception:
            mark = 0.0
        if mark and ((long and price >= mark) or (not long and price <= mark)):
            return {"ok": False, "error": f"SL must be {'below' if long else 'above'} the mark price ({mark:g})."}
        pid = await _delta.get_product_id(sym)
        close_side = "sell" if long else "buy"
        for o in await _delta.get_live_orders(sym):
            if o.get("reduce_only") and o.get("stop_order_type") == "stop_loss_order":
                try:
                    await _delta.cancel_order(o["id"], pid)
                except Exception:
                    pass
        await _delta.place_stop_order(sym, close_side, abs(int(round(pos))), price, "stop_loss_order")
        await db.bot_state.update_one({"_id": sym}, {"$set": {"sl": price, "sl_method": "manual"}})
        _delta.invalidate_cache()
        _VIEW_CACHE.pop(sym, None)
        return {"ok": True, "sl": price}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.delete("/logs")
async def clear_logs():
    result = await db.trade_logs.delete_many({})
    return {"deleted": result.deleted_count}
