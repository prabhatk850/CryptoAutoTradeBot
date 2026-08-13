import asyncio
import math
import time as _time
from datetime import datetime, timezone
from fastapi import APIRouter, Query
from db import db
from config import settings
from bot.delta_client import DeltaClient

router = APIRouter(prefix="/trades", tags=["trades"])
_delta = DeltaClient()

# Short-lived cache so /pnl and /orders (polled together) share one fetch.
_VIEW_CACHE: dict[str, tuple[float, dict]] = {}
_VIEW_TTL = 20.0  # seconds (bot acts every 5m, so 20s-stale views are fine + keep UI fast)
_VIEW_LOCKS: dict[str, asyncio.Lock] = {}  # dedupe concurrent builds per symbol
_DECISIONS_CACHE: dict[str, object] = {"ts": 0.0, "data": []}


def _clean(v):
    """Recursively replace NaN/inf (not JSON-serializable) with None."""
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_clean(x) for x in v]
    return v


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


async def _decision_index() -> list[dict]:
    """Bot BUY/SELL decisions (with humanized reasons), cached 60s (they change slowly)."""
    if _time.time() - _DECISIONS_CACHE["ts"] < 60 and _DECISIONS_CACHE["data"]:
        return _DECISIONS_CACHE["data"]
    cursor = db.trade_logs.find({"action": {"$in": ["BUY", "SELL"]}}).sort("timestamp", 1)
    out = []
    async for d in cursor:
        out.append({
            "ts": _parse_dt(d.get("timestamp")),
            "side": 1 if d["action"] == "BUY" else -1,
            "reason": _humanize_reason(d),
        })
    _DECISIONS_CACHE.update(ts=_time.time(), data=out)
    return out


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
        source, source_label = "fallback", "Delta fills unreachable"
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
    total = await db.trade_logs.count_documents({})
    buys = await db.trade_logs.count_documents({"action": "BUY"})
    sells = await db.trade_logs.count_documents({"action": "SELL"})
    holds = await db.trade_logs.count_documents({"action": "HOLD"})
    latest = await db.trade_logs.find_one(
        {"action": {"$in": ["BUY", "SELL"]}}, sort=[("timestamp", -1)]
    )
    return {
        "total_decisions": total,
        "buys": buys,
        "sells": sells,
        "holds": holds,
        "latest_price": latest["price"] if latest else 0,
        "symbol": settings.trading_symbol,
    }


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
        out.append({
            "symbol": sym, "side": "LONG" if size > 0 else "SHORT", "size": abs(size),
            "entry": round(entry, 2), "mark": round(mark, 2), "unrealized": round(unreal, 2),
            "pnl_pct": round((mark - entry) / entry * 100 * (1 if size > 0 else -1), 2) if entry else 0,
            "sl": sl, "tps": sorted(tps, reverse=(size < 0)),
        })
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
async def close_position(symbol: str):
    """Manually close a symbol's open position at market: cancel its protective
    (reduce-only) orders, then send a reduce-only market order to flatten it."""
    sym = symbol.upper()
    try:
        pos = await _delta.get_position_size(sym)
        if abs(pos) < 1e-9:
            return {"ok": False, "error": "No open position for this symbol."}
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
        await db.bot_state.delete_one({"_id": sym})
        _delta.invalidate_cache()
        _VIEW_CACHE.pop(sym, None)
        return {"ok": True, "closed": qty, "side": close_side, "order_id": str(order.get("id", ""))}
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
