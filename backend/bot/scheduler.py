"""Trading loops: `bot_tick` (deep analysis + entries) and `fast_tick` (position upkeep + armed triggers)."""
import asyncio
import logging
import time
from datetime import datetime, timezone, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from bot import strategies, autotune, ai_brain, smc, edge, news, funding, orderbook, portfolio_risk, ensemble
from bot import risk_engine
from bot.agents import AgentContext
from bot.delta_client import DeltaClient, vwap_fill
from bot.divergence import detect_divergence
from bot.indicators import calc_atr, compute_indicators
from bot.lux_indicators import supertrend_ai, trendline_breakout_navigator, fair_value_gaps, inverse_fvg
from bot.swings import find_swings
from config import settings
from db import db

logger = logging.getLogger("bot.scheduler")

scheduler = AsyncIOScheduler()
_bot_running = False
_delta = DeltaClient()
_tick_lock = asyncio.Lock()       # held by bot_tick for its whole pass; fast_tick never interleaves orders
_watch: dict[str, dict] = {}      # symbol -> armed fast-loop trigger {side, trigger, confidence, armed_at, expires}
_last_ai_call: dict[str, float] = {}  # symbol -> monotonic time of the last deep-pass AI call
BIAS_TXT = {1: "bullish", -1: "bearish", 0: "neutral"}
IST = timezone(timedelta(hours=5, minutes=30))  # fixed offset: India has no DST
_stop_reason: str | None = None
_rule = {"session": None, "realized": 0.0, "target": 0.0, "hit": False}  # last 3-5-7 daily check
_override: dict = {"session": None, "loaded": False}  # session the user manually restarted in


class _SkipEntry(Exception):
    """Clean skip of a new entry; the message becomes the logged order_status."""


# --------------------------------------------------------------------------- #
#  Analysis
# --------------------------------------------------------------------------- #
def analyze_timeframe(candles: list[dict], vwap_candles: list[dict] | None = None) -> dict:
    """Every indicator for one timeframe (CPU-heavy: run in a thread)."""
    return {
        "candles": candles,
        "ind": compute_indicators(candles, vwap_candles),
        "st": supertrend_ai(candles),
        "tn": trendline_breakout_navigator(candles),
        "fvg": fair_value_gaps(candles),
        "ifvg": inverse_fvg(candles),
        "smc": smc.analyze(candles),
    }


def trend_bias(tf: dict) -> int:
    """+1 / -1 / 0 from EMA side + SuperTrend direction + trendline trend."""
    ind, st, tn = tf["ind"], tf["st"], tf["tn"]
    score = 0
    if ind.get("ema_fast") is not None and ind.get("ema_slow") is not None:
        score += 1 if ind["ema_fast"] > ind["ema_slow"] else -1
    if st:
        d = st["latest"].get("dir")
        score += 1 if d == "long" else -1 if d == "short" else 0
    if tn:
        score += int(tn["latest"].get("trend") or 0)
    return (score > 0) - (score < 0)


# --------------------------------------------------------------------------- #
#  Trade geometry (pure)
# --------------------------------------------------------------------------- #
def _combined_sl(long: bool, entry: float, candles: list, st) -> tuple[float, str]:
    """Most protective of ATR / SuperTrend / structure stops, clamped to [min_sl_pct, max_sl_pct]."""
    cands: dict[str, float] = {}
    atr_list = calc_atr(candles, settings.atr_period)
    atr = atr_list[-1] if atr_list else 0.0
    if atr > 0:
        cands["atr"] = entry - settings.atr_k * atr if long else entry + settings.atr_k * atr
    if st and st.get("latest", {}).get("ts"):
        ts = float(st["latest"]["ts"])
        if (long and ts < entry) or (not long and ts > entry):
            cands["supertrend"] = ts
    lb = candles[-settings.sl_lookback:]
    if lb:
        cands["structure"] = min(c["low"] for c in lb) if long else max(c["high"] for c in lb)
    if not cands:
        d = settings.stop_loss_pct / 100
        return (entry * (1 - d) if long else entry * (1 + d)), "fixed"

    pick = min if long else max  # furthest from entry
    sl, method = pick(cands.values()), pick(cands, key=cands.get)
    min_d, max_d = entry * settings.min_sl_pct / 100, entry * settings.max_sl_pct / 100
    risk = abs(entry - sl)
    if risk < min_d:
        sl = entry - min_d if long else entry + min_d
    elif risk > max_d:
        sl = entry - max_d if long else entry + max_d
        method += "+capped"
    return sl, method


def _clamp_sl(long: bool, entry: float, sl: float | None) -> float | None:
    """AI stop forced into the allowed distance band; None if on the wrong side of entry."""
    if sl is None or (long and sl >= entry) or (not long and sl <= entry):
        return None
    d = min(max(abs(entry - sl), entry * settings.min_sl_pct / 100), entry * settings.max_sl_pct / 100)
    return round(entry - d if long else entry + d, 2)


def _room_for_1to2(long: bool, entry: float, risk: float, trend_candles: list) -> tuple[bool, str]:
    """Is a 2R target reachable before the nearest 1h high/low?"""
    if risk <= 0:
        return False, "no risk"
    lb = trend_candles[-settings.target_lookback:] if trend_candles else []
    if not lb:
        return True, "open (no structure)"
    need = 2 * risk
    if long:
        res = max(c["high"] for c in lb)
        if res <= entry:
            return True, "open upside"
        return res - entry >= need, f"{res - entry:.0f} to 1h resistance vs need {need:.0f}"
    sup = min(c["low"] for c in lb)
    if sup >= entry:
        return True, "open downside"
    return entry - sup >= need, f"{entry - sup:.0f} to 1h support vs need {need:.0f}"


def _real_rr(long: bool, fill: float, sl: float, tp: float) -> float:
    """Reward:risk measured from the actual fill, not the mark."""
    risk = abs(fill - sl)
    reward = (tp - fill) if long else (fill - tp)
    return reward / risk if risk > 0 else 0.0


def _rr_target(agree: int, strength: int, aligned: bool) -> float:
    """Bigger reward:risk ceiling for more agreement and a strong aligned 1h SuperTrend."""
    base = {2: 2.0, 3: 3.0, 4: 5.0}.get(agree, 10.0 if agree >= 5 else 2.0)
    if aligned:
        if strength >= 8:
            base = max(base, 5.0)
        if strength >= 9 and agree >= 4:
            base = 10.0
    return base


def _swing_prices(candles: list[dict], kind: str) -> list[float]:
    """Raw (unrounded) swing highs or lows."""
    key = "high" if kind == "high" else "low"
    return [candles[s["i"]][key] for s in find_swings(candles) if s["kind"] == kind]


def _target_levels(long: bool, entry: float, risk: float, c_entry, c_trend, fvg, rr_top: float) -> list[float]:
    """TPs snapped to the nearest swing/FVG level beyond each R floor (2R, 3R, max(5, rr_top)); ratio price otherwise."""
    kind = "high" if long else "low"
    levels = _swing_prices(c_entry[-120:], kind) + _swing_prices(c_trend[-60:], kind)
    for z in (fvg or {}).get("unmitigated", []):
        levels.append(z["bottom"] if long else z["top"])  # far edge of a gap is a magnet
    levels = sorted({round(l, 1) for l in levels if (l > entry if long else l < entry)}, reverse=not long)

    tps, last = [], entry
    for fl in [2.0, 3.0, max(5.0, rr_top)][:max(1, settings.max_tps)]:
        floor_price = entry + fl * risk if long else entry - fl * risk
        min_price = max(floor_price, last + risk) if long else min(floor_price, last - risk)
        chosen = next((l for l in levels if (l >= min_price if long else l <= min_price)), min_price)
        tps.append(round(chosen, 1))
        last = chosen
    return tps


def _valid_ai_tps(long: bool, entry: float, risk: float, ai_tps: list) -> list[tuple]:
    """AI TPs that are progressive, on the right side, and whose first clears the R:R floor."""
    if not ai_tps or risk <= 0:
        return []
    out, last = [], entry
    for t in ai_tps:
        p = t.get("price")
        if p is None or not ((p > last) if long else (p < last)):
            continue
        if not out and ((p - entry) if long else (entry - p)) < settings.risk_reward * risk:
            continue
        out.append((round(p, 1), t.get("size_pct")))
        last = p
        if len(out) >= settings.max_tps:
            break
    return out


def _tp_sizes(lots: int, accepted: list[tuple]) -> list[int]:
    """Split lots across TPs by AI size_pct when sane, else by tp_splits; remainder goes to the first TPs."""
    pcts = [s for _, s in accepted]
    if accepted and all(isinstance(s, (int, float)) and s > 0 for s in pcts) and 0.5 <= sum(pcts) <= 1.5:
        fracs = [s / sum(pcts) for s in pcts]
    else:
        cfg = [float(x) for x in settings.tp_splits.split(",")]
        fracs = (cfg + [0] * len(accepted))[:len(accepted)] or [1.0]
    sizes = [int(lots * f) for f in fracs]
    for j in range(min(lots - sum(sizes), len(sizes))):
        sizes[j] += 1
    return sizes


def _symbol_profile(symbol: str, big: bool) -> dict | None:
    """Point-based SL/TP bands for ETH; None means percent-based (BTC)."""
    if not symbol.upper().startswith("ETH"):
        return None
    s = settings
    if big:
        return {"sl_min": s.eth_sl_min_pts, "sl_max": s.eth_big_sl_max_pts,
                "tp_min": s.eth_big_tp_min_pts, "tp_max": s.eth_big_tp_max_pts}
    return {"sl_min": s.eth_sl_min_pts, "sl_max": s.eth_sl_max_pts, "tp_min": s.eth_tp_min_pts, "tp_max": s.eth_tp_max_pts}


def _safe_leverage(entry: float, sl_dist: float) -> int:
    """Highest leverage (≤ configured) whose liquidation sits beyond the stop plus liq_buffer_pct."""
    if entry <= 0 or sl_dist <= 0:
        return settings.leverage
    sl_frac = sl_dist / entry + settings.liq_buffer_pct / 100.0
    return max(1, min(settings.leverage, int(1.0 / sl_frac)))


# --------------------------------------------------------------------------- #
#  Entry guards
# --------------------------------------------------------------------------- #
async def _daily_loss_exceeded(balance: float) -> tuple[bool, float, float]:
    """(breached, today's realized, limit). Fails open — every trade still has its own stop."""
    if settings.daily_loss_limit_pct <= 0 or balance <= 0:
        return False, 0.0, 0.0
    from routers.trades import _build_view  # local import avoids a cycle
    total = 0.0
    for sym in set(settings.symbols()):
        try:
            total += float((await _build_view(sym)).get("today_realized") or 0.0)
        except Exception:
            pass
    limit = -abs(settings.daily_loss_limit_pct) / 100 * balance
    return total <= limit, round(total, 2), round(limit, 2)


async def _liquidity_check(symbol: str, want_long: bool, lots: int = 0) -> tuple[bool, str]:
    """Sane spread, and (with lots) a book deep enough to exit at acceptable slippage."""
    try:
        book = await _delta.get_orderbook(symbol)
        t = await _delta.get_ticker(symbol)
        mark = float(t.get("mark_price") or t.get("close") or 0)
    except Exception as e:
        return False, f"order book unavailable ({type(e).__name__})"
    bids, asks = book.get("buy") or [], book.get("sell") or []
    if not bids or not asks or not mark:
        return False, "empty order book"
    spread_pct = (float(asks[0]["price"]) - float(bids[0]["price"])) / mark * 100
    if spread_pct > settings.max_entry_spread_pct:
        return False, f"spread {spread_pct:.2f}% > {settings.max_entry_spread_pct:g}% (illiquid)"
    if lots > 0:
        px, got = vwap_fill(bids if want_long else asks, lots)  # a long exits into bids
        if got < lots - 1e-9 or not px:
            return False, f"book too thin to exit {lots} lots"
        exit_slip = abs(px - mark) / mark * 100
        if exit_slip > settings.max_exit_slippage_pct:
            return False, f"exit would cost {exit_slip:.2f}% (> {settings.max_exit_slippage_pct:g}%) — unexitable"
    return True, f"spread {spread_pct:.2f}%"


async def _liquidity_gate(symbol: str, want_long: bool, lots: int = 0) -> tuple[bool, str, dict]:
    """Liquidity check under the off/shadow/enforce switch; only "enforce" ever blocks."""
    if not settings.liquidity_gate_enabled or settings.liquidity_gate_mode == "off":
        return True, "", {"mode": "off"}
    ok, txt = await _liquidity_check(symbol, want_long, lots)
    meta = {"mode": settings.liquidity_gate_mode, "ok": ok, "reason": txt, "lots": lots}
    return ok or settings.liquidity_gate_mode != "enforce", txt, meta


# --------------------------------------------------------------------------- #
#  3-5-7 rule
# --------------------------------------------------------------------------- #
def session_start(now: datetime | None = None) -> datetime:
    """Latest daily reset (session_reset_hour_ist, IST) at or before `now`."""
    now = (now or datetime.now(IST)).astimezone(IST)
    start = now.replace(hour=settings.session_reset_hour_ist, minute=0, second=0, microsecond=0)
    return start if now >= start else start - timedelta(days=1)


def _risk_capped_lots(lots: int, risk_per_lot: float, balance: float, open_risk: float,
                      trade_risk_pct: float | None = None) -> int:
    """Largest size ≤ lots keeping this trade ≤ trade_risk_pct (default 3%) and all open risk ≤ max_open_risk_pct."""
    if risk_per_lot <= 0:
        return lots
    trade_pct = settings.max_trade_risk_pct if trade_risk_pct is None else trade_risk_pct
    budget = min(balance * trade_pct / 100, balance * settings.max_open_risk_pct / 100 - open_risk)
    return max(0, min(lots, int(budget // risk_per_lot)))


async def _open_risk() -> float:
    """$ at risk on open bot trades whose stop isn't at breakeven yet."""
    # ponytail: uses the risk recorded at entry; positions opened outside the bot count as 0
    held = {p["product_symbol"] for p in await _delta.get_positions() if p.get("size")}
    return sum([float(s.get("risk_dollars") or 0)
                async for s in db.bot_state.find({"be_moved": {"$ne": True}}, {"risk_dollars": 1})
                if s["_id"] in held])


async def _override_session() -> str | None:
    if not _override["loaded"]:
        try:
            _override["session"] = ((await db.bot_meta.find_one({"_id": "rule_357"})) or {}).get("override_session")
            _override["loaded"] = True
        except Exception:
            pass
    return _override["session"]


async def override_daily_target() -> None:
    """Manual start: ignore this session's +7% stop until the next reset."""
    _override.update(session=session_start().isoformat(), loaded=True)
    try:
        await db.bot_meta.update_one({"_id": "rule_357"}, {"$set": {"override_session": _override["session"]}},
                                     upsert=True)
    except Exception as e:
        logger.warning(f"3-5-7 override not persisted ({type(e).__name__}) — holds until the next reload")


async def _daily_target_hit() -> bool:
    """Realized P/L since the session reset ≥ daily_profit_target_pct of the session's starting balance."""
    if not settings.rule_357_enabled or settings.daily_profit_target_pct <= 0:
        return False
    from routers.trades import _build_view  # local import avoids a cycle
    start = session_start()
    realized = 0.0
    for sym in set(settings.symbols()):
        realized += sum(r["realized_pnl"] or 0 for r in (await _build_view(sym))["rows"]
                        if r["status"] == "Closed" and r["exit_time"]
                        and datetime.fromisoformat(r["exit_time"]) >= start)
    balance = float((await _delta.get_wallet()).get("balance") or 0)
    target = (balance - realized) * settings.daily_profit_target_pct / 100
    _rule.update(session=start.isoformat(), realized=round(realized, 2), target=round(target, 2),
                 hit=target > 0 and realized >= target)
    return _rule["hit"] and await _override_session() != start.isoformat()


async def daily_reset():
    """Session reset (6PM IST): retrain the risk engine, clear the daily stop and start the bot."""
    _rule["hit"] = False
    try:
        await risk_engine.retrain()
    except Exception as e:
        logger.error(f"risk engine retrain failed: {e}")
    logger.info(f"3-5-7: new session at {session_start().isoformat()} — starting bot.")
    start_bot()


def schedule_daily_reset():
    """Register the daily reset job (independent of the trading jobs, so it survives stop_bot)."""
    scheduler.add_job(daily_reset, CronTrigger(hour=settings.session_reset_hour_ist, minute=0, timezone=IST),
                      id="daily_reset", replace_existing=True)
    if not scheduler.running:
        scheduler.start()


# --------------------------------------------------------------------------- #
#  Closed-trade attribution
# --------------------------------------------------------------------------- #
def _parse_iso(v) -> datetime:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except Exception:
        return datetime.now(timezone.utc)


async def _closed_trade_realized(symbol: str, opened_at) -> tuple[float, datetime | None]:
    """Realized USD P/L from fills since `opened_at`, plus the last fill time (None = no fills: unverified)."""
    cv = await _delta.get_contract_value(symbol)
    try:
        fills = await _delta.get_fills(page_size=500)
    except Exception:
        return 0.0, None
    start = _parse_iso(opened_at) - timedelta(seconds=2)
    rows = []
    for f in fills:
        ts = _parse_iso(f.get("created_at"))
        qty, price = float(f.get("size") or 0), float(f.get("price") or 0)
        if f.get("product_symbol") == symbol and ts >= start and qty > 0 and price > 0:
            side = 1 if str(f.get("side", "")).lower() == "buy" else -1
            rows.append((ts, side, qty, price, float(f.get("commission") or 0)))
    rows.sort(key=lambda x: x[0])
    pos = avg = realized = 0.0
    for _, side, qty, price, comm in rows:
        signed = side * qty
        if pos == 0:
            pos, avg = signed, price
        elif (pos > 0) == (side > 0):
            avg = (avg * abs(pos) + price * qty) / (abs(pos) + qty)
            pos += signed
        else:
            realized += (price - avg) * (1 if pos > 0 else -1) * min(abs(pos), qty) * cv
            new = pos + signed
            if new != 0 and (new > 0) != (pos > 0):
                avg = price
            pos = new
        realized -= comm
    return realized, (rows[-1][0] if rows else None)


async def _mark_at(symbol: str, when: datetime | None) -> tuple[float, bool]:
    """(mark price at `when`, whether it truly came from `when`); a live-mark stand-in is flagged False."""
    if when is not None:
        try:
            age_min = (datetime.now(timezone.utc) - when).total_seconds() / 60
            candles = await _delta.get_candles(symbol, 1, int(min(max(age_min + 10, 60), 2000)), mark=True)
            prior = [c for c in candles if c["time"] <= int(when.timestamp())]
            if prior:
                return float(prior[-1]["close"]), True
        except Exception:
            pass
    try:
        t = await _delta.get_ticker(symbol)
        return float(t.get("mark_price") or t.get("close") or 0), when is None
    except Exception:
        return 0.0, False


async def _record_trade_outcome(symbol: str, state: dict) -> dict:
    """Persist a closed trade scored two ways: strategy R (mark→mark, trains the tuner) and execution R (fills)."""
    cv = await _delta.get_contract_value(symbol)
    realized_exec, exit_ts = await _closed_trade_realized(symbol, state.get("opened_at"))
    entry_mark = float(state.get("entry") or 0)
    size = abs(float(state.get("size") or 0))
    sign = 1 if state.get("side") == "buy" else -1
    exit_mark, exit_mark_exact = await _mark_at(symbol, exit_ts)
    strategy_pnl = (exit_mark - entry_mark) * sign * size * cv if (entry_mark and exit_mark) else 0.0
    risk_d = float(state.get("risk_dollars") or 0)

    def _r(pnl: float) -> float:
        return round(pnl / risk_d, 3) if risk_d > 0 else float((pnl > 0) - (pnl < 0))

    doc = {
        "symbol": symbol,
        "verified": exit_ts is not None and exit_mark_exact,   # unverified rows never train or count
        "fills_matched": exit_ts is not None,
        "exit_mark_exact": exit_mark_exact,
        "side": "BUY" if sign > 0 else "SELL",
        "size": size,
        "opened_at": state.get("opened_at"),
        "closed_at": exit_ts or datetime.now(timezone.utc),
        "entry_mark": round(entry_mark, 2),
        "exit_mark": round(exit_mark, 2),
        "fill_entry": state.get("fill_price"),
        "risk_dollars": round(risk_d, 2),
        "strategy_pnl": round(strategy_pnl, 2),
        "execution_pnl": round(realized_exec, 2),
        "slippage_cost": round(realized_exec - strategy_pnl, 2),
        "strategy_r": _r(strategy_pnl),
        "execution_r": _r(realized_exec),
        "votes": state.get("votes") or {},
        "sl_method": state.get("sl_method"),
        "tp_source": state.get("tp_source"),
        "ai_confidence": (state.get("ai") or {}).get("confidence"),
    }
    try:
        await db.trade_outcomes.insert_one(dict(doc))   # sole writer; /bot/training reads it
    except Exception as e:
        logger.error(f"outcome persist failed: {e}")
    return doc


async def _attribute_closed_trade(symbol: str, state: dict) -> None:
    """Score a just-closed trade and train the tuner on it if verified."""
    try:
        o = await _record_trade_outcome(symbol, state)
    except Exception as e:
        logger.error(f"attribution error: {e}")
        return
    if not o["verified"]:
        why = "no fills matched" if not o["fills_matched"] else "no mark price for the exit moment"
        logger.warning(f"{symbol} closed but {why} — outcome recorded unverified and excluded from training.")
        return
    train_r = o["strategy_r"] if settings.autotune_use_mark_pnl else o["execution_r"]
    try:
        await autotune.record_outcome(state.get("votes") or {}, o["side"], train_r)
        await autotune.record_shadow_outcome(state.get("shadow_votes") or {}, o["side"], train_r)
        await ensemble.record_outcome(state.get("agents") or [], train_r)
    except Exception as e:
        logger.error(f"attribution error: {e}")
        return
    try:
        await risk_engine.record_path(_delta, symbol, state, _parse_iso(state.get("opened_at")), o["closed_at"],
                                      await _fee_rate(symbol))
    except Exception as e:
        logger.error(f"{symbol} trade path not recorded: {e}")
    logger.info(f"{symbol} closed — strategy {o['strategy_pnl']:+.2f} ({o['strategy_r']:+.2f}R) "
                f"| execution {o['execution_pnl']:+.2f} ({o['execution_r']:+.2f}R) "
                f"| slippage {o['slippage_cost']:+.2f} → attributed to {o['side']} voters"
                + (f" + agents {state['agents']}" if state.get("agents") else ""))


async def _fee_rate(symbol: str) -> float:
    """Delta taker fee rate; the configured fallback (logged) if the venue never told us."""
    try:
        rate = await _delta.get_taker_fee(symbol)
    except Exception:
        rate = None
    if rate is None:
        logger.warning(f"{symbol}: taker fee unknown — using fallback {settings.trail_fee_fallback_pct}%")
        return settings.trail_fee_fallback_pct / 100
    return rate


async def _move_sl(symbol: str, pos: float, new_sl: float) -> bool:
    """Place the new reduce-only stop first, then cancel the old ones, so the position is never unprotected."""
    old = [o["id"] for o in await _delta.get_live_orders(symbol)
           if o.get("reduce_only") and o.get("stop_order_type") == "stop_loss_order"]
    try:
        await _delta.place_stop_order(symbol, "buy" if pos < 0 else "sell", abs(int(round(pos))), new_sl,
                                      "stop_loss_order")
    except Exception as e:
        logger.error(f"{symbol} SL move to {new_sl} failed: {e} — previous stop kept")
        return False
    pid = await _delta.get_product_id(symbol)
    for oid in old:
        try:
            await _delta.cancel_order(oid, pid)
        except Exception as e:  # both are reduce-only, so a leftover can't open a position
            logger.error(f"{symbol} old SL {oid} not cancelled ({e}) — two stops resting until flat cleanup")
    return True


async def _trail_sl(symbol: str, state: dict, pos: float, tp1_filled: bool) -> None:
    """Breakeven(+fees) at `trigger` of the way to TP1, then lock `lock` of TP1 profit once TP1 fills."""
    stage = state.get("sl_stage", 1 if state.get("be_moved") else 0)
    if stage >= 2:
        return
    mark = float((await _delta.get_ticker(symbol)).get("mark_price") or 0)  # raises on stale → skip this pass
    if not mark:
        return
    long = state["side"] == "buy"
    basis = float(state.get("fill_price") or state["entry"])
    p = await risk_engine.params()
    move = risk_engine.next_sl(long, basis, float(state["tps"][0]["price"]), state.get("sl"), stage, mark,
                               tp1_filled, p["trigger"], p["lock"],
                               risk_engine.fee_buffer(basis, await _fee_rate(symbol)))
    if not move:
        return
    new_sl, new_stage, why = move
    if await _move_sl(symbol, pos, new_sl):
        await db.bot_state.update_one({"_id": symbol}, {"$set": {"sl": new_sl, "sl_stage": new_stage, "be_moved": True}})
        prev = state.get("sl")
        logger.info(f"{symbol} trail SL [{why}] {f'{prev:.1f}' if prev is not None else '—'} → {new_sl:.1f} "
                    f"({'LONG' if long else 'SHORT'}, mark {mark:.1f}, basis {basis:.1f}, stage {stage}→{new_stage})")


async def _manage_open_position(symbol: str):
    """Attribute + clean up when flat; otherwise trail the stop (or plain breakeven after TP1 if trailing is off)."""
    try:
        state = await db.bot_state.find_one({"_id": symbol})
        pos = await _delta.get_position_size(symbol)   # raises rather than report a false flat
        if abs(pos) < 1e-9:
            if state:
                await _attribute_closed_trade(symbol, state)
                await _delta.cancel_reduce_only(symbol)
                await db.bot_state.delete_one({"_id": symbol})
                logger.info(f"{symbol} flat — cleared trade state and leftover orders.")
            return
        if not state:
            return
        tp1_filled = abs(pos) < state["size"] - 1e-9
        if settings.trail_enabled and state.get("tps"):
            await _trail_sl(symbol, state, pos, tp1_filled)
        elif tp1_filled and not state.get("be_moved"):
            entry = state.get("fill_price") or state["entry"]   # true breakeven is the fill, not the mark
            if await _move_sl(symbol, pos, entry):
                await db.bot_state.update_one({"_id": symbol}, {"$set": {"be_moved": True}})
                logger.info(f"{symbol} TP1 hit — moved stop-loss to breakeven ({entry:.1f}).")
    except Exception as e:
        logger.error(f"manage_position error: {e}")


# --------------------------------------------------------------------------- #
#  Fast-loop arming
# --------------------------------------------------------------------------- #
def _expected_move(c_ltf: list[dict], horizon_sec: int) -> float | None:
    """Expected price travel over `horizon_sec`, scaled from 5m ATR."""
    try:
        series = calc_atr(c_ltf, settings.atr_period)
    except Exception:
        return None
    if not series or not series[-1] or series[-1] <= 0:
        return None
    return float(series[-1]) * horizon_sec / (max(settings.ltf_timeframe, 1) * 60)


def _update_watch(symbol: str, ai_plan: dict | None, price: float, c_ltf: list[dict],
                  allow_entry: bool, in_position: bool) -> None:
    """Arm only for a flat, allowed symbol whose AI entry level is ahead of price and within reach; else disarm."""
    _watch.pop(symbol, None)
    if not settings.fast_check_enabled or not allow_entry or in_position or not ai_plan:
        return
    side, trigger = ai_plan.get("action"), ai_plan.get("entry")
    if side not in ("BUY", "SELL") or not trigger:
        return
    if (side == "BUY" and price >= trigger) or (side == "SELL" and price <= trigger):
        return  # already through the level — this analysis has been acted on
    horizon = settings.check_interval_seconds or settings.check_interval_minutes * 60
    reach = _expected_move(c_ltf, horizon)
    distance = abs(price - trigger)
    if reach is None or distance > settings.fast_arm_atr_mult * reach:
        return
    now = datetime.now(timezone.utc)
    _watch[symbol] = {"side": side, "trigger": float(trigger), "confidence": ai_plan.get("confidence"),
                      "armed_at": now, "expires": now + timedelta(seconds=settings.fast_arm_ttl_sec)}
    logger.info(f"[{symbol}] fast-watch ARMED {side} @ {trigger} "
                f"(price {price}, {distance:.2f} away, ~{reach:.2f} expected in {horizon}s)")


def _trigger_hit(watch: dict, price: float) -> bool:
    return price >= watch["trigger"] if watch["side"] == "BUY" else price <= watch["trigger"]


# --------------------------------------------------------------------------- #
#  Per-symbol pipeline: analyze -> decide -> execute -> log
# --------------------------------------------------------------------------- #
async def _analyze_symbol(symbol: str, fast: bool) -> dict | None:
    """All three timeframes + context signals + strategy votes; None if history is too short."""
    c_entry = await _delta.get_candles(symbol, settings.entry_timeframe, settings.candle_limit)
    c_trend = await _delta.get_candles(symbol, settings.trend_timeframe, settings.candle_limit)
    c_ltf = await _delta.get_candles(symbol, settings.ltf_timeframe, settings.candle_limit)
    if len(c_entry) < settings.ema_slow + 5:
        logger.warning("Not enough entry-timeframe candles yet, skipping tick.")
        return None

    # VWAP needs traded volume (mark candles have none); ~2 days covers the current UTC day. Deep pass only.
    c_vol = None
    if settings.vwap_enabled and not fast:
        try:
            bars = max(200, int(2 * 1440 / max(settings.entry_timeframe, 1)))
            c_vol = await _delta.get_candles(symbol, settings.entry_timeframe, bars, mark=False)
        except Exception as e:
            logger.warning(f"[{symbol}] traded-volume candles for VWAP failed: {e}")

    entry, trend, timing, div = await asyncio.gather(
        asyncio.to_thread(analyze_timeframe, c_entry, c_vol),
        asyncio.to_thread(analyze_timeframe, c_trend),
        asyncio.to_thread(analyze_timeframe, c_ltf),
        asyncio.to_thread(detect_divergence, c_entry, settings.divergence_swing_left, settings.divergence_swing_right),
    )

    funding_read = orderbook_read = None
    if not fast:  # slow-moving; no need for 15s-cadence history writes
        try:
            funding_read = await funding.record_and_bias(symbol, _delta)
        except Exception as e:
            logger.warning(f"[{symbol}] funding read failed: {e}")
    try:
        orderbook_read = orderbook.imbalance(await _delta.get_orderbook(symbol))
    except Exception as e:
        logger.warning(f"[{symbol}] orderbook read failed: {e}")

    return {"entry": entry, "trend": trend, "timing": timing, "divergence": div,
            "funding": funding_read, "orderbook": orderbook_read, "bias": trend_bias(trend)}


async def _ask_agents(symbol: str, a: dict, result: dict) -> dict | None:
    """Learning-ensemble decision over the book-derived agents (see bot/ensemble.py)."""
    e = a["entry"]
    try:
        return await ensemble.decide(AgentContext(
            symbol=symbol, price=e["ind"]["close"], c_entry=e["candles"], c_trend=a["trend"]["candles"],
            ind=e["ind"], supertrend=e["st"], trendline=e["tn"], fvg=e["fvg"], ifvg=e["ifvg"],
            smc=e["smc"], smc_trend=a["trend"]["smc"], bias=a["bias"], confluence=result))
    except Exception as ex:
        logger.error(f"[{symbol}] agent ensemble failed: {ex}")
        return None


def _ai_due(symbol: str, fast: bool) -> bool:
    """Fast passes may always ask; the deep pass at most once per ai_min_interval_sec per symbol."""
    if fast:
        return True
    now = time.monotonic()
    if now - _last_ai_call.get(symbol, float("-inf")) < settings.ai_min_interval_sec:
        return False
    _last_ai_call[symbol] = now
    return True


async def _ask_ai(symbol: str, a: dict, votes: dict, weights: dict | None, fast: bool) -> dict | None:
    """AI plan; the fast pass sends a lean snapshot (no edge/news) to fast providers."""
    chain = ai_brain.FAST_PROVIDERS if fast else ai_brain.DEFAULT_PROVIDERS
    if not ai_brain.available(chain) or settings.ai_mode not in ("decide", "refine", "advisory"):
        return None
    try:
        snapshot = ai_brain.build_snapshot(
            symbol, a["entry"], a["trend"], a["timing"], BIAS_TXT[a["bias"]], votes, weights,
            historical_edge=None if fast else await edge.get_edge(symbol),
            news=None if fast else await news.ai_context(symbol),
            divergence=a["divergence"], funding=a["funding"], orderbook=a["orderbook"])
        return await asyncio.to_thread(ai_brain.analyze, snapshot, chain)
    except Exception as e:
        logger.error(f"[{symbol}] AI analysis failed: {e}")
        return None


async def _decide(symbol: str, a: dict, result: dict, weights: dict | None, fast: bool) -> dict:
    """Votes filtered by 1h trend + ADX, then the agent ensemble; the AI decides only if the agents abstain."""
    bias, bias_txt = a["bias"], BIAS_TXT[a["bias"]]
    adx_now = a["trend"]["ind"].get("adx")
    adx_blocks = bool(settings.adx_gate_enabled and adx_now is not None and adx_now < settings.adx_min_trend)
    adx_txt = f"1h ADX {adx_now:.1f} < {settings.adx_min_trend:g} (ranging)" if adx_blocks else ""

    entry_action = action = result["action"]
    if entry_action == "BUY" and bias < 0:
        action, reason = "HOLD", f"Blocked — 15m wanted BUY but 1h trend is bearish | {result['reason']}"
    elif entry_action == "SELL" and bias > 0:
        action, reason = "HOLD", f"Blocked — 15m wanted SELL but 1h trend is bullish | {result['reason']}"
    elif entry_action in ("BUY", "SELL") and adx_blocks:
        action, reason = "HOLD", f"Blocked — {adx_txt} | {result['reason']}"
    else:
        reason = f"1h trend {bias_txt} | {result['reason']}"

    ens = await _ask_agents(symbol, a, result)
    agent_drove = False
    if ens and ens["action"] in ("BUY", "SELL"):
        cand = ens["action"]
        if settings.ai_respect_trend_filter and ((cand == "BUY" and bias < 0) or (cand == "SELL" and bias > 0)):
            action, reason = "HOLD", f"Agents wanted {cand} but blocked by 1h {bias_txt} trend | {ens['reason']}"
        elif adx_blocks:
            action, reason = "HOLD", f"Agents wanted {cand} but blocked by {adx_txt} | {ens['reason']}"
        else:
            action, agent_drove, reason = cand, True, f"{ens['reason']} · 1h {bias_txt}"

    ai_asked = not agent_drove and _ai_due(symbol, fast)
    ai_plan = await _ask_ai(symbol, a, result["votes"], weights, fast) if ai_asked else None
    ai_drove = False
    if ai_plan and settings.ai_mode == "decide":
        cand = ai_plan["action"]
        if cand in ("BUY", "SELL") and ai_plan["confidence"] < settings.ai_min_confidence:
            cand = "HOLD"
        if settings.ai_respect_trend_filter and ((cand == "BUY" and bias < 0) or (cand == "SELL" and bias > 0)):
            action, reason = "HOLD", f"AI wanted {cand} but blocked by 1h {bias_txt} trend | {ai_plan['reasoning']}"
        elif cand in ("BUY", "SELL") and adx_blocks:
            action, reason = "HOLD", f"AI wanted {cand} but blocked by {adx_txt} | {ai_plan['reasoning']}"
        else:
            action, ai_drove = cand, cand in ("BUY", "SELL")
            reason = f"AI {ai_plan['confidence']:.0%} → {cand} · 1h {bias_txt} | {ai_plan['reasoning']}"
    elif ai_plan and settings.ai_mode == "refine":
        ai_drove = action in ("BUY", "SELL")  # votes decide; AI supplies SL/TP
        if ai_drove:
            reason = f"{reason} | AI SL/TP · {ai_plan['reasoning']}"

    ai_meta = None
    if ai_plan:
        ai_meta = {"via": ai_plan.get("via"), "mode": settings.ai_mode, "drove": ai_drove,
                   "proposed_action": ai_plan["action"],
                   **{k: ai_plan[k] for k in ("confidence", "reasoning", "invalidation", "stop_loss", "take_profits")}}
    agent_meta = None
    if ens:
        agent_meta = {"decision": ens["action"], "drove": agent_drove,
                      **{k: ens[k] for k in ("confidence", "size_mult", "agents", "proposals", "reason")}}
    logger.info(f"[{symbol}] {action} (1h bias={bias_txt}, votes={result['votes']}, "
                f"agents={'drove' if agent_drove else (ens['action'] if ens else 'off')}, "
                f"ai={'on ' + format(ai_plan['confidence'], '.0%') if ai_plan else 'off'})")
    return {"action": action, "reason": reason, "entry_action": entry_action,
            "ai_plan": ai_plan, "ai_drove": ai_drove, "ai_meta": ai_meta, "ai_asked": ai_asked,
            "agent_drove": agent_drove, "agent_meta": agent_meta,
            "agent_ids": ens["agents"] if agent_drove else [],
            "agent_size_mult": ens["size_mult"] if agent_drove else 1.0,
            "agent_sl_hint": ens["sl_hint"] if agent_drove else None}


async def _entry_gates(symbol: str, action: str, want_long: bool, votes: dict, weights: dict | None,
                       t: dict) -> tuple[float, float]:
    """Position cap, spread, news blackout, daily loss, expectancy. Returns (balance, available); raises _SkipEntry."""
    n_open = sum(1 for p in await _delta.get_positions() if p.get("size"))
    if n_open >= settings.max_concurrent_positions:
        logger.info(f"{symbol} flat + {action} signal but {n_open} positions already open — skipping")
        raise _SkipEntry(f"skipped: max {settings.max_concurrent_positions} concurrent positions open")

    ok, txt, t["liq_meta"] = await _liquidity_gate(symbol, want_long)
    if not ok:
        logger.info(f"{symbol} entry blocked — liquidity: {txt}")
        raise _SkipEntry(f"skipped: {txt}")

    blocked, ev = await news.in_blackout()
    if blocked:
        logger.info(f"{symbol} entry blocked — news blackout: {ev['currency']} {ev['title']}")
        raise _SkipEntry(f"skipped: news blackout — {ev['impact']}-impact {ev['currency']} "
                         f"'{ev['title']}' within {settings.news_blackout_min}m")

    wallet = await _delta.get_wallet()
    balance = float(wallet.get("balance") or 0)
    avail = float(wallet.get("available_balance") or balance)
    breached, day_pnl, day_limit = await _daily_loss_exceeded(balance)
    if breached:
        logger.warning(f"{symbol} entry blocked — daily loss limit reached (today ${day_pnl} <= ${day_limit})")
        raise _SkipEntry(f"skipped: daily loss limit hit (today {day_pnl} <= {day_limit})")

    exp = t["expectancy_meta"] = await edge.blended_expectancy(symbol, votes, action, weights)
    if settings.expectancy_gate_enabled:
        if exp["blended_exp"] is None:
            if not settings.expectancy_gate_fail_open:
                logger.info(f"{symbol} entry blocked — expectancy gate: no qualifying evidence")
                raise _SkipEntry("skipped: expectancy gate — no qualifying backtested evidence for this vote")
        elif exp["blended_exp"] < settings.expectancy_gate_min_R:
            logger.info(f"{symbol} entry blocked — expectancy gate: "
                        f"{exp['blended_exp']:+.3f}R < {settings.expectancy_gate_min_R:+.3f}R")
            raise _SkipEntry(f"skipped: expectancy gate — blended {exp['blended_exp']:+.3f}R "
                             f"< min {settings.expectancy_gate_min_R:+.3f}R")
    return balance, avail


async def _margin_pct(symbol: str, want_long: bool, big: bool, price: float, c_entry: list,
                      size_mult: float = 1.0) -> float:
    """% of balance used as margin: base (normal/big) × agent size, damped if correlated, scaled by volatility."""
    cap_pct = (settings.big_trade_capital_pct if big else settings.position_capital_pct) * size_mult
    if settings.correlation_check_enabled:
        try:
            for p in await _delta.get_positions():
                other, size = p.get("product_symbol"), float(p.get("size") or 0)
                if not other or other == symbol or not size or (size > 0) != want_long:
                    continue
                corr = await portfolio_risk.realized_correlation(
                    _delta, symbol, other, settings.correlation_timeframe_min, settings.correlation_lookback_bars)
                if corr is not None and corr >= settings.correlation_high_threshold:
                    cap_pct *= settings.correlation_dampen_factor
                    logger.info(f"{symbol} sizing dampened {settings.correlation_dampen_factor:g}x "
                                f"— {corr:.2f} correlated with open {other} {'buy' if want_long else 'sell'}")
                    break
        except Exception as e:
            logger.warning(f"{symbol} correlation check failed ({type(e).__name__}) — using full size")
    if settings.vol_sizing_enabled and price > 0:
        atr_list = calc_atr(c_entry, settings.atr_period)
        if atr_list and atr_list[-1] > 0:
            atr_pct = atr_list[-1] / price * 100
            cap_pct *= min(max(settings.vol_ref_atr_pct / atr_pct, settings.vol_scalar_min), settings.vol_scalar_max)
    return cap_pct


async def _open_trade(symbol: str, a: dict, d: dict, votes_result: dict, weights: dict | None, t: dict) -> None:
    """Size, validate and place a bracketed entry from flat. Fills `t`; raises _SkipEntry to skip."""
    action, ai_plan, ai_drove = d["action"], d["ai_plan"], d["ai_drove"]
    side, want_long = action.lower(), action == "BUY"
    entry_tf, price = a["entry"], a["entry"]["ind"]["close"]
    c_entry, c_trend = entry_tf["candles"], a["trend"]["candles"]

    balance, avail = await _entry_gates(symbol, action, want_long, votes_result["votes"], weights, t)

    # "Big" = strong agreement: wider stop/target, smaller margin.
    agree = votes_result["buy_score"] if want_long else votes_result["sell_score"]
    big = agree / max(len(strategies.parse_enabled(settings.strategies)), 1) >= settings.big_trade_min_agree

    # Stop: the driver's structure stop (AI or agents, clamped), else ATR/SuperTrend/structure.
    sl = sl_method = None
    if ai_drove and ai_plan:
        sl = _clamp_sl(want_long, price, ai_plan.get("stop_loss"))
        sl_method = "ai" if sl is not None else None
    elif d["agent_drove"] and d["agent_sl_hint"] is not None:
        sl = _clamp_sl(want_long, price, d["agent_sl_hint"])
        sl_method = "agent" if sl is not None else None
    if sl is None:
        sl, sl_method = _combined_sl(want_long, price, c_entry, entry_tf["st"])
        if ai_drove:
            sl_method += "+ai-fallback"
        elif d["agent_drove"]:
            sl_method += "+agent-fallback"
    prof = _symbol_profile(symbol, big)
    if prof:  # ETH: clamp stop distance to the point band
        dist = min(max(abs(price - sl), prof["sl_min"]), prof["sl_max"])
        sl = price - dist if want_long else price + dist
        sl_method = f"{sl_method}|pts<= {prof['sl_max']:g}" + ("|BIG" if big else "")
    t["sl_price"] = sl
    risk = abs(price - sl)   # always mark-based

    ok, room_info = (True, "point-based target") if prof else _room_for_1to2(want_long, price, risk, c_trend)
    if not ok:
        logger.info(f"Skip {side}: {room_info}")
        raise _SkipEntry(f"skipped: 1:2 not reachable ({room_info})")

    cv = await _delta.get_contract_value(symbol)
    lev = _safe_leverage(price, risk)
    await _delta.set_leverage(symbol, lev)
    cap_pct = await _margin_pct(symbol, want_long, big, price, c_entry, d["agent_size_mult"])
    margin = min(balance * cap_pct / 100.0, avail * settings.margin_cap_pct)
    lots = t["lots"] = max(int(margin * lev / (price * cv)) if price > 0 and cv > 0 else 0, 1)
    t["fraction"] = round(cap_pct, 2)
    if settings.rule_357_enabled:
        trade_pct = (await risk_engine.params())["trade_risk_pct"]
        capped = _risk_capped_lots(lots, risk * cv, balance, await _open_risk(), trade_pct)
        if capped < 1:
            status = (f"skipped: 3-5-7 risk cap — 1 lot risks ${risk * cv:.2f} (max {trade_pct:g}% "
                      f"per trade, {settings.max_open_risk_pct:g}% open)")
            logger.info(f"{symbol} entry blocked — {status}")
            raise _SkipEntry(status)
        lots = t["lots"] = capped
    risk_dollars = risk * lots * cv

    strength = a["trend"]["st"]["latest"].get("strength", 0) if a["trend"]["st"] else 0
    aligned = (a["bias"] > 0 and want_long) or (a["bias"] < 0 and not want_long)
    rr = t["rr"] = max(_rr_target(agree, strength, aligned), settings.risk_reward)

    ok, txt, t["liq_meta"] = await _liquidity_gate(symbol, want_long, lots)  # can we exit this size?
    if not ok:
        logger.info(f"{symbol} entry blocked — {txt}")
        raise _SkipEntry(f"skipped: {txt}")

    # Targets are anchored to the mark entry.
    if prof:  # ETH: fixed ladder inside the point band (1 TP = top, 2 TPs = band edges)
        dists = [prof["tp_max"]] if settings.max_tps <= 1 else [prof["tp_min"], prof["tp_max"]]
        tps = [round(price + x if want_long else price - x, 1) for x in dists]
        sizes, tp_source = _tp_sizes(lots, [(p, None) for p in tps]), "eth-points" + ("-BIG" if big else "")
    else:
        accepted = _valid_ai_tps(want_long, price, risk, ai_plan["take_profits"]) if (ai_drove and ai_plan) else []
        if accepted:
            tps, sizes, tp_source = [p for p, _ in accepted], _tp_sizes(lots, accepted), "ai"
        else:
            tps = _target_levels(want_long, price, risk, c_entry, c_trend, entry_tf["fvg"], rr)
            sizes, tp_source = _tp_sizes(lots, [(p, None) for p in tps]), "ai-fallback" if ai_drove else "structure"
    tp1 = t["tp_price"] = tps[0] if tps else None

    # R:R from the price the book would actually fill this size at (slippage gutted a 2.5R plan once).
    book = await _delta.get_orderbook(symbol)
    est_fill, got = vwap_fill(book.get("sell" if want_long else "buy") or [], lots)
    est_fill = est_fill if got else price  # ponytail: a book shallower than `lots` understates slip; fine as a floor check
    real_rr = _real_rr(want_long, est_fill, sl, tp1)
    if real_rr < settings.risk_reward:
        status = (f"skipped: 1:{settings.risk_reward:g} not reachable from "
                  f"est. fill {est_fill:.1f} (real R:R {real_rr:.2f})")
        logger.info(f"{symbol} entry blocked — {status}")
        raise _SkipEntry(status)

    await _delta.cancel_reduce_only(symbol)  # flat, so any reduce-only order is an orphan
    order = await _delta.place_order(symbol, side, lots)
    t["order_id"], t["order_status"] = str(order.get("id", "")), order.get("state", "unknown")
    # Fill is recorded for P/L and breakeven only (None if unreadable — breakeven then uses mark).
    fill_price = float((await _delta.get_position(symbol)).get("entry_price") or 0) or None

    close_side = "sell" if want_long else "buy"
    placed_tps = []
    for lvl, sz in zip(tps, sizes):
        if sz <= 0:
            continue
        try:
            await _delta.place_stop_order(symbol, close_side, sz, lvl, "take_profit_order")
            placed_tps.append({"price": lvl, "size": sz})
        except Exception as e:
            logger.error(f"TP order failed @ {lvl}: {e}")
    try:
        await _delta.place_stop_order(symbol, close_side, lots, sl, "stop_loss_order")
    except Exception as e:
        logger.error(f"SL order failed @ {sl}: {e} — position is UNPROTECTED")

    await db.bot_state.replace_one({"_id": symbol}, {
        "_id": symbol, "side": side, "size": lots, "entry": price, "fill_price": fill_price,
        "sl": sl, "sl0": sl, "sl_stage": 0, "tps": placed_tps, "rr": rr, "be_moved": False,
        "votes": votes_result["votes"], "agents": d["agent_ids"],
        "shadow_votes": votes_result.get("shadow_votes") or {},
        "risk_dollars": round(risk_dollars, 4), "sl_method": sl_method, "tp_source": tp_source, "ai": d["ai_meta"],
        "leverage": lev, "big_trade": big, "capital_pct": cap_pct, "opened_at": datetime.now(timezone.utc),
    }, upsert=True)
    logger.info(f"Opened {side} {lots} lots @ {lev}x {'[BIG] ' if big else ''}| margin {t['fraction']:.0f}% cap "
                f"(risk ${risk_dollars:.2f}) | mark-entry {price:.1f} "
                + (f"fill {fill_price:.1f} (slip {fill_price - price:+.1f})" if fill_price else "fill unknown")
                + f" SL {sl:.1f} ({sl_method}) "
                f"TPs {[x['price'] for x in placed_tps]} ({tp_source})")


async def _execute(symbol: str, a: dict, d: dict, votes_result: dict, allow_entry: bool,
                   weights: dict | None) -> dict:
    """Position-aware execution: close on an opposite signal, hold if already in, else try to open."""
    t = {"order_id": None, "order_status": None, "lots": 0, "sl_price": None, "tp_price": None,
         "fraction": 0.0, "rr": float(settings.risk_reward), "liq_meta": None, "expectancy_meta": None}
    action = d["action"]
    if action not in ("BUY", "SELL"):
        return t
    want_long = action == "BUY"
    try:
        pos = await _delta.get_position_size(symbol)
        if pos and (pos > 0) != want_long:
            await _delta.cancel_reduce_only(symbol)
            close_side, qty = ("buy" if pos < 0 else "sell"), abs(int(round(pos)))
            order = await _delta.place_order(symbol, close_side, qty, reduce_only=True)
            await db.bot_state.delete_one({"_id": symbol})
            t["order_id"], t["order_status"] = str(order.get("id", "")), "closed_position"
            logger.info(f"Closed {pos} {symbol} via {close_side} {qty} (cancelled brackets)")
        elif pos:
            t["order_status"] = "held (already in position)"
            logger.info(f"Signal {action} but already {'LONG' if pos > 0 else 'SHORT'} {abs(pos)} — holding (no pyramiding)")
        elif not allow_entry:
            t["order_status"] = "flat (managed only — not the active chart symbol)"
        else:
            await _open_trade(symbol, a, d, votes_result, weights, t)
    except _SkipEntry as skip:
        t["order_status"] = str(skip)
    except Exception as e:
        logger.error(f"Order failed: {e}")
        t["order_status"] = f"error: {e}"
    return t


def _log_doc(symbol: str, a: dict, d: dict, result: dict, t: dict) -> dict:
    entry, trend = a["entry"], a["trend"]
    ind, st, tn, fvg, ifvg, smc_e = (entry[k] for k in ("ind", "st", "tn", "fvg", "ifvg", "smc"))
    return {
        "timestamp": datetime.now(timezone.utc),
        "symbol": symbol,
        "action": d["action"],
        "reason": d["reason"],
        "price": ind["close"],
        "quantity": t["lots"] if d["action"] != "HOLD" else 0,
        "indicators": {k: ind.get(k) for k in (
            "ema_fast", "ema_slow", "ema_signal", "rsi", "rsi_signal", "breakout_signal", "breakout_level",
            "macd", "macd_signal_line", "macd_hist", "macd_signal")},
        "votes": result["votes"],
        "supertrend": st["latest"] if st else None,
        "trendline": tn["latest"] if tn else None,
        "fvg": fvg["latest"] if fvg else None,
        "ifvg": ifvg["latest"] if ifvg else None,
        "smc": {
            "entry_trend": smc_e.get("trend"),
            "trend_trend": (trend["smc"] or {}).get("trend"),
            "structure_break": smc_e.get("structure_break"),
            "recent_sweep": smc_e.get("recent_sweep"),
            "zone": (smc_e.get("premium_discount") or {}).get("zone"),
        } if smc_e else None,
        "timeframes": {
            "entry_tf": settings.entry_timeframe,
            "trend_tf": settings.trend_timeframe,
            "trend_bias": BIAS_TXT[a["bias"]],
            "entry_action": d["entry_action"],
            "trend_supertrend": trend["st"]["latest"] if trend["st"] else None,
        },
        "sizing": {"risk_pct": round(t["fraction"], 2), "leverage": settings.leverage,
                   "reward_risk": round(t["rr"], 1),
                   "stop_loss": round(t["sl_price"], 2) if t["sl_price"] else None,
                   "take_profit": round(t["tp_price"], 2) if t["tp_price"] else None},
        "ai": d["ai_meta"],
        "agents": d["agent_meta"],
        "liquidity": t["liq_meta"],
        "expectancy_gate": t["expectancy_meta"],
        "order_id": t["order_id"],
        "order_status": t["order_status"],
        "paper_trade": True,
    }


async def _process_symbol(symbol: str, allow_entry: bool, weights: dict | None = None, fast: bool = False):
    """Manage, analyze, decide, execute and log one symbol. `fast` = armed-trigger pass (same guardrails)."""
    try:
        await _manage_open_position(symbol)
        a = await _analyze_symbol(symbol, fast)
        if a is None:
            return
        entry = a["entry"]
        ctx = strategies.StrategyContext(
            candles=entry["candles"], ind=entry["ind"], supertrend=entry["st"], trendline=entry["tn"],
            fvg=entry["fvg"], ifvg=entry["ifvg"],
            extra={"smc": entry["smc"], "smc_trend": a["trend"]["smc"], "divergence": a["divergence"],
                   "funding": a["funding"], "orderbook_imbalance": a["orderbook"]})
        result = strategies.evaluate(ctx, strategies.parse_enabled(settings.strategies), settings.min_signals,
                                     weights, shadow=strategies.parse_enabled(settings.shadow_strategies))
        d = await _decide(symbol, a, result, weights, fast)
        t = await _execute(symbol, a, d, result, allow_entry, weights)
        await db.trade_logs.insert_one(_log_doc(symbol, a, d, result, t))

        if not fast:  # only the deep pass arms, so one analysis can't fire twice
            try:
                held = await _delta.get_position_size(symbol)
            except Exception:
                held = 0
            if held or d["ai_asked"]:  # AI on cooldown: keep the current watch (it has its own TTL)
                _update_watch(symbol, d["ai_plan"], entry["ind"]["close"], a["timing"]["candles"],
                              allow_entry=allow_entry, in_position=bool(held))
    except Exception as e:
        logger.exception(f"[{symbol}] process error: {e}")


# --------------------------------------------------------------------------- #
#  Loops
# --------------------------------------------------------------------------- #
async def bot_tick():
    """Deep pass over trade symbols (may enter) plus any symbol with a position or saved state (managed only)."""
    try:
        if await _daily_target_hit():
            logger.warning(f"3-5-7: daily target hit (+${_rule['realized']} ≥ ${_rule['target']}) — "
                           f"bot stopped until {settings.session_reset_hour_ist}:00 IST or a manual Start.")
            stop_bot("daily +7% target hit")
            return
    except Exception as e:  # fail open, like the daily loss limit
        logger.error(f"3-5-7 target check failed: {e}")
    trade_syms = settings.symbols()
    tracked = set(trade_syms)
    try:
        tracked |= {p["product_symbol"] for p in await _delta.get_positions()
                    if p.get("size") and p.get("product_symbol")}
    except Exception as e:
        logger.error(f"tracked-positions fetch failed: {e}")
    try:
        tracked |= {s["_id"] async for s in db.bot_state.find({}, {"_id": 1})}
    except Exception:
        pass
    weights = await autotune.get_weights()
    async with _tick_lock:
        for sym in tracked:
            await _process_symbol(sym, allow_entry=sym in trade_syms, weights=weights)


async def fast_tick():
    """Between deep ticks: manage open positions (no AI) and fire armed triggers through the normal entry path."""
    if not settings.fast_check_enabled or not _bot_running or _tick_lock.locked():
        return
    async with _tick_lock:
        try:
            open_syms = {p["product_symbol"] for p in await _delta.get_positions()
                         if p.get("size") and p.get("product_symbol")}
        except Exception as e:
            logger.error(f"fast_tick position fetch failed: {e}")
            open_syms = set()
        for sym in open_syms:
            await _manage_open_position(sym)

        now = datetime.now(timezone.utc)
        weights = None
        for sym, w in list(_watch.items()):
            if now >= w["expires"]:
                _watch.pop(sym, None)
                logger.info(f"[{sym}] fast-watch expired without triggering")
                continue
            if sym in open_syms:
                _watch.pop(sym, None)
                continue
            try:
                px = float((await _delta.get_ticker(sym)).get("mark_price") or 0)
            except Exception as e:
                logger.error(f"[{sym}] fast ticker fetch failed: {e}")
                continue
            if not px or not _trigger_hit(w, px):
                continue
            _watch.pop(sym, None)  # consume before acting so a slow entry can't double-fire
            logger.info(f"[{sym}] fast-watch TRIGGERED {w['side']} @ {w['trigger']} "
                        f"(mark {px}) — confirming on fast providers")
            weights = weights or await autotune.get_weights()
            await _process_symbol(sym, allow_entry=True, weights=weights, fast=True)


def start_bot():
    global _bot_running, _stop_reason
    if _bot_running:
        return {"status": "already_running"}
    secs = settings.check_interval_seconds
    trigger = IntervalTrigger(seconds=secs) if secs > 0 else IntervalTrigger(minutes=settings.check_interval_minutes)
    # max_instances=1 + coalesce: a long tick never overlaps the next (that could double-enter).
    common = {"replace_existing": True, "max_instances": 1, "coalesce": True, "misfire_grace_time": None}
    scheduler.add_job(bot_tick, trigger=trigger, id="bot_tick", next_run_time=datetime.now(timezone.utc), **common)
    if settings.fast_check_enabled:
        scheduler.add_job(fast_tick, trigger=IntervalTrigger(seconds=settings.fast_check_seconds),
                          id="fast_tick", **common)
    if not scheduler.running:
        scheduler.start()
    _bot_running, _stop_reason = True, None
    cadence = f"{secs} seconds" if secs > 0 else f"{settings.check_interval_minutes} minutes"
    logger.info(f"Bot started — checking every {cadence}.")
    return {"status": "started"}


def stop_bot(reason: str = "manual"):
    global _bot_running, _stop_reason
    if not _bot_running:
        return {"status": "not_running"}
    for job_id in ("bot_tick", "fast_tick"):
        try:
            scheduler.remove_job(job_id)
        except Exception:
            pass
    _watch.clear()  # an armed watch must never survive a stop
    _bot_running, _stop_reason = False, reason
    logger.info(f"Bot stopped ({reason}).")
    return {"status": "stopped"}


def bot_status() -> dict:
    job = scheduler.get_job("bot_tick") if _bot_running else None
    return {
        "running": _bot_running,
        "interval_minutes": settings.check_interval_minutes,
        "interval_seconds": settings.check_interval_seconds or None,
        "fast_check_seconds": settings.fast_check_seconds if settings.fast_check_enabled else None,
        "armed": {s: {"side": w["side"], "trigger": w["trigger"]} for s, w in _watch.items()},
        "symbol": settings.trading_symbol,
        "symbols": settings.symbols(),
        "next_run": str(job.next_run_time) if job else None,
        "stop_reason": _stop_reason,
        "rule_357": {**_rule, "overridden": _override["session"] == _rule["session"],
                     "next_reset": str(scheduler.get_job("daily_reset").next_run_time)
                     if scheduler.get_job("daily_reset") else None},
    }
