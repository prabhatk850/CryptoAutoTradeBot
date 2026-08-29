"""
APScheduler job that fires every CHECK_INTERVAL_MINUTES.
On each tick:
  1. Fetch latest candles from Delta Exchange
  2. Compute indicators
  3. Run strategy → BUY / SELL / HOLD
  4. If BUY/SELL: place order via Delta Exchange API
  5. Log everything to MongoDB
"""
import asyncio
import logging
import time
from datetime import datetime, timezone, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from bot.delta_client import DeltaClient
from bot.indicators import compute_indicators
from bot.lux_indicators import supertrend_ai, trendline_breakout_navigator, fair_value_gaps, inverse_fvg, _atr
from bot import strategies, autotune, ai_brain, smc, edge as edge_mod, news, agents as agents_mod, ensemble
from config import settings
from db import db

logger = logging.getLogger("bot.scheduler")

scheduler = AsyncIOScheduler()
_bot_running = False
_delta = DeltaClient()
_last_ai_call: dict[str, float] = {}  # symbol -> monotonic ts of last LLM fallback (cooldown)


def _analyze(candles):
    """Compute every indicator for one timeframe."""
    ind = compute_indicators(
        candles,
        ema_fast=settings.ema_fast,
        ema_slow=settings.ema_slow,
        rsi_period=settings.rsi_period,
        rsi_oversold=settings.rsi_oversold,
        rsi_overbought=settings.rsi_overbought,
    )
    st = supertrend_ai(candles)
    tn = trendline_breakout_navigator(candles, term=settings.trendline_term)
    fvg = fair_value_gaps(candles)
    ifvg = inverse_fvg(candles)
    return ind, st, tn, fvg, ifvg


def _trend_bias(ind, st, tn) -> int:
    """Higher-TF continuous trend: +1 bullish / -1 bearish / 0 neutral."""
    score = 0
    if ind.get("ema_fast") is not None and ind.get("ema_slow") is not None:
        score += 1 if ind["ema_fast"] > ind["ema_slow"] else -1
    if st:
        d = st["latest"].get("dir")
        score += 1 if d == "long" else -1 if d == "short" else 0
    if tn:
        score += int(tn["latest"].get("trend") or 0)
    return 1 if score > 0 else -1 if score < 0 else 0


def _combined_sl(long: bool, entry: float, candles: list, st) -> tuple[float, dict, str]:
    """
    Stop-loss = the MOST PROTECTIVE of three methods, clamped to a sane range:
      • ATR:        entry -/+ k*ATR (volatility)
      • SuperTrend: the SuperTrend AI trailing-stop line (trend flip level)
      • Structure:  most recent swing low/high (market structure)
    Returns (sl_price, candidate_prices, chosen_method).
    """
    cands: dict[str, float] = {}
    atr_list = _atr(candles, settings.atr_period)
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
        sld = settings.stop_loss_pct / 100
        sl = entry * (1 - sld) if long else entry * (1 + sld)
        return sl, {"fixed": round(sl, 2)}, "fixed"

    # most protective = furthest from entry
    sl = min(cands.values()) if long else max(cands.values())
    method = min(cands, key=cands.get) if long else max(cands, key=cands.get)

    # clamp risk to [min, max] % of price so sizing & targets stay sane
    min_d = entry * settings.min_sl_pct / 100
    max_d = entry * settings.max_sl_pct / 100
    risk = abs(entry - sl)
    if risk < min_d:
        sl = entry - min_d if long else entry + min_d
    elif risk > max_d:
        sl = entry - max_d if long else entry + max_d
        method += "+capped"
    return sl, {k: round(v, 2) for k, v in cands.items()}, method


def _room_for_1to2(long: bool, entry: float, risk: float, trend_candles: list) -> tuple[bool, str]:
    """Only trade if a 1:2 is reachable before the next 1h structural level."""
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
        room = res - entry
        return room >= need, f"{room:.0f} to 1h resistance vs need {need:.0f}"
    sup = min(c["low"] for c in lb)
    if sup >= entry:
        return True, "open downside"
    room = entry - sup
    return room >= need, f"{room:.0f} to 1h support vs need {need:.0f}"


def _rr_target(agree: int, strength: int, aligned: bool) -> float:
    """Dynamic reward:risk. More agreement + stronger aligned trend = bigger target."""
    base = {2: 2.0, 3: 3.0, 4: 5.0}.get(agree, 10.0 if agree >= 5 else 2.0)
    if aligned:
        if strength >= 8:
            base = max(base, 5.0)
        if strength >= 9 and agree >= 4:
            base = 10.0
    return base


def _swings(candles, highs: bool) -> list[float]:
    """Local swing highs (highs=True) or lows over a 2-bar window each side."""
    out = []
    n = len(candles)
    for i in range(2, n - 2):
        c = candles[i]
        if highs:
            h = c["high"]
            if h > candles[i - 1]["high"] and h > candles[i - 2]["high"] and h >= candles[i + 1]["high"] and h >= candles[i + 2]["high"]:
                out.append(h)
        else:
            l = c["low"]
            if l < candles[i - 1]["low"] and l < candles[i - 2]["low"] and l <= candles[i + 1]["low"] and l <= candles[i + 2]["low"]:
                out.append(l)
    return out


def _target_levels(long: bool, entry: float, risk: float, c_entry, c_trend, fvg, rr_top: float) -> list[float]:
    """
    TP1/TP2/TP3 snapped to logical structure (swing highs/lows + FVG edges),
    each at least 1:2 / 1:3 / (5 or rr_top) reward:risk. Falls back to the
    ratio price when no structure level is available.
    """
    levels = _swings(c_entry[-120:], long) + _swings(c_trend[-60:], long)
    if fvg:
        for z in fvg.get("unmitigated", []):
            levels.append(z["bottom"] if long else z["top"])  # opposite gap edge = magnet/target
    levels = [l for l in levels if (l > entry if long else l < entry)]
    levels = sorted(set(round(l, 1) for l in levels), reverse=not long)  # nearest-first

    floors = [2.0, 3.0, max(5.0, rr_top)][:max(1, settings.max_tps)]
    tps = []
    last = entry
    for fl in floors:
        floor_price = entry + fl * risk if long else entry - fl * risk
        # target must clear both the RR floor AND the previous TP (monotonic)
        min_price = max(floor_price, last + risk) if long else min(floor_price, last - risk)
        chosen = None
        for l in levels:  # nearest-first
            if (long and l >= min_price) or (not long and l <= min_price):
                chosen = l
                break
        if chosen is None:
            chosen = min_price  # ratio fallback (already beyond last)
        tps.append(round(chosen, 1))
        last = chosen
    return tps


def _clamp_sl(long: bool, entry: float, sl: float) -> float | None:
    """Force an AI-proposed stop onto the correct side of entry and into the
    allowed distance band. Returns None if it's on the wrong side (unusable)."""
    if sl is None:
        return None
    min_d = entry * settings.min_sl_pct / 100
    max_d = entry * settings.max_sl_pct / 100
    if long:
        if sl >= entry:
            return None
        d = min(max(entry - sl, min_d), max_d)
        return round(entry - d, 2)
    if sl <= entry:
        return None
    d = min(max(sl - entry, min_d), max_d)
    return round(entry + d, 2)


def _valid_ai_tps(long: bool, entry: float, risk: float, ai_tps: list) -> list[tuple]:
    """Keep only AI take-profits that are on the right side, strictly progressive,
    and whose first level clears the 2R floor. Returns [(price, size_pct|None), ...]."""
    if not ai_tps or risk <= 0:
        return []
    need1 = settings.risk_reward * risk
    out, last = [], entry
    for t in ai_tps:
        p = t.get("price")
        if p is None:
            continue
        beyond = (p > last) if long else (p < last)
        if not beyond:
            continue
        if not out:  # first accepted TP must clear the R:R floor
            r1 = (p - entry) if long else (entry - p)
            if r1 < need1:
                continue
        out.append((round(p, 1), t.get("size_pct")))
        last = p
        if len(out) >= settings.max_tps:  # never more than max_tps take-profits
            break
    return out


def _tp_sizes(lots: int, accepted: list[tuple]) -> list[int]:
    """Split `lots` across TPs using AI size_pct when all present & sane, else config splits."""
    pcts = [s for _, s in accepted]
    if accepted and all(isinstance(s, (int, float)) and s > 0 for s in pcts) and 0.5 <= sum(pcts) <= 1.5:
        fracs = [s / sum(pcts) for s in pcts]
    else:
        cfg = [float(x) for x in settings.tp_splits.split(",")]
        fracs = (cfg + [0] * len(accepted))[:len(accepted)] or [1.0]
    sizes = [int(lots * f) for f in fracs]
    rem = lots - sum(sizes)
    for j in range(len(sizes)):
        if rem <= 0:
            break
        sizes[j] += 1
        rem -= 1
    return sizes


async def _daily_loss_exceeded(balance: float) -> tuple[bool, float, float]:
    """Circuit breaker: True once today's REALIZED loss across all traded symbols
    reaches daily_loss_limit_pct of capital. Returns (exceeded, today_realized, limit).
    Fails OPEN (allows trading) if it can't be computed — every trade still carries its
    own exchange-side stop."""
    limit_pct = settings.daily_loss_limit_pct
    if limit_pct <= 0 or balance <= 0:
        return False, 0.0, 0.0
    from routers.trades import _build_view  # local import avoids a circular import
    syms = {s.strip().upper() for s in settings.trade_symbols.split(",") if s.strip()}
    total = 0.0
    for sym in syms:
        try:
            v = await _build_view(sym)
            total += float(v.get("today_realized") or 0.0)
        except Exception:
            pass
    limit = -abs(limit_pct) / 100 * balance
    return (total <= limit), round(total, 2), round(limit, 2)


def _symbol_profile(symbol: str, big: bool) -> dict | None:
    """POINT-based SL/TP limits for symbols that use them (ETH). Returns None for
    symbols that keep the percent-based logic (e.g. BTC)."""
    s = symbol.upper()
    if s.startswith("ETH"):
        if big:
            return {"sl_min": settings.eth_sl_min_pts, "sl_max": settings.eth_big_sl_max_pts,
                    "tp_min": settings.eth_big_tp_min_pts, "tp_max": settings.eth_big_tp_max_pts}
        return {"sl_min": settings.eth_sl_min_pts, "sl_max": settings.eth_sl_max_pts,
                "tp_min": settings.eth_tp_min_pts, "tp_max": settings.eth_tp_max_pts}
    return None


def _safe_leverage(entry: float, sl_dist: float) -> int:
    """Largest leverage (<= configured max) whose liquidation price stays BEYOND the
    stop-loss by liq_buffer_pct — so the stop always triggers before liquidation."""
    if entry <= 0 or sl_dist <= 0:
        return settings.leverage
    sl_frac = sl_dist / entry + settings.liq_buffer_pct / 100.0
    lev = int(1.0 / sl_frac) if sl_frac > 0 else settings.leverage
    return max(1, min(settings.leverage, lev))


class _SkipEntry(Exception):
    """Raised to cleanly skip opening a new entry (not an error)."""


def _parse_iso(v):
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except Exception:
        return datetime.now(timezone.utc)


async def _closed_trade_realized(symbol: str, opened_at) -> float:
    """Realized USD P/L of the just-closed trade (fills since it opened)."""
    cv = await _delta.get_contract_value(symbol)
    try:
        fills = await _delta.get_fills(page_size=500)
    except Exception:
        return 0.0
    rows = []
    start = _parse_iso(opened_at)
    for f in fills:
        if f.get("product_symbol") != symbol:
            continue
        ts = _parse_iso(f.get("created_at"))
        if ts < start - timedelta(seconds=2):
            continue
        side = 1 if str(f.get("side", "")).lower() == "buy" else -1
        qty = float(f.get("size") or 0)
        price = float(f.get("price") or 0)
        if qty > 0 and price > 0:
            rows.append((ts, side, qty, price, float(f.get("commission") or 0)))
    rows.sort(key=lambda x: x[0])
    pos = avg = realized = 0.0
    for _, side, qty, price, comm in rows:
        signed = side * qty
        if pos == 0:
            pos, avg = signed, price
        elif (pos > 0) == (side > 0):
            avg = (avg * abs(pos) + price * abs(signed)) / (abs(pos) + abs(signed))
            pos += signed
        else:
            cq = min(abs(pos), qty)
            realized += (price - avg) * (1 if pos > 0 else -1) * cq * cv
            new = pos + signed
            if new != 0 and (new > 0) != (pos > 0):
                avg = price
            pos = new
        realized -= comm
    return realized


async def _manage_open_position(symbol: str):
    """Move SL to breakeven after the first partial TP fills; clean up when flat."""
    try:
        state = await db.bot_state.find_one({"_id": symbol})
        pos = await _delta.get_position_size(symbol)
        pid = await _delta.get_product_id(symbol)

        if abs(pos) < 1e-9:
            # flat -> attribute the trade's result to its strategies, then clean up
            if state:
                try:
                    realized = await _closed_trade_realized(symbol, state.get("opened_at"))
                    risk_d = float(state.get("risk_dollars") or 0)
                    R = (realized / risk_d) if risk_d > 0 else (1.0 if realized > 0 else -1.0 if realized < 0 else 0.0)
                    action = "BUY" if state.get("side") == "buy" else "SELL"
                    await autotune.record_outcome(state.get("votes") or {}, action, R)
                    # continuous learning: credit/debit the agents that drove this trade
                    agent_ids = state.get("agents") or []
                    if agent_ids:
                        await ensemble.record_outcome(agent_ids, R)
                    logger.info(f"{symbol} closed — realized ${realized:.2f} ({R:+.2f}R) → "
                                f"attributed to {action} voters" + (f" + agents {agent_ids}" if agent_ids else ""))
                except Exception as e:
                    logger.error(f"attribution error: {e}")
                for o in await _delta.get_live_orders(symbol):
                    if o.get("reduce_only"):
                        await _delta.cancel_order(o["id"], pid)
                await db.bot_state.delete_one({"_id": symbol})
                logger.info(f"{symbol} flat — cleared trade state and leftover orders.")
            return

        if not state or state.get("be_moved"):
            return

        # a partial TP filled if current size < original size
        if abs(pos) < state["size"] - 1e-9:
            # true breakeven = the actual FILL price (zero PnL), not the mark entry
            entry = state.get("fill_price") or state["entry"]
            close_side = "buy" if pos < 0 else "sell"
            for o in await _delta.get_live_orders(symbol):
                if o.get("stop_order_type") == "stop_loss_order" and o.get("reduce_only"):
                    await _delta.cancel_order(o["id"], pid)
            await _delta.place_stop_order(symbol, close_side, abs(int(round(pos))), entry, "stop_loss_order")
            await db.bot_state.update_one({"_id": symbol}, {"$set": {"be_moved": True}})
            logger.info(f"{symbol} TP1 hit — moved stop-loss to breakeven ({entry:.1f}).")
    except Exception as e:
        logger.error(f"manage_position error: {e}")


async def _process_symbol(symbol: str, allow_entry: bool, weights: dict | None = None):
    """Manage + (optionally) trade ONE symbol. `allow_entry` gates new positions
    so non-active symbols are still managed/closed but don't get fresh entries."""
    try:
        # 0. Manage any open position (breakeven after TP1, cleanup when flat)
        await _manage_open_position(symbol)

        # 1. Fetch all THREE timeframes (1h bias, 15m decision, 5m timing)
        c_entry = await _delta.get_candles(symbol, settings.entry_timeframe, settings.candle_limit)
        c_trend = await _delta.get_candles(symbol, settings.trend_timeframe, settings.candle_limit)
        c_ltf = await _delta.get_candles(symbol, settings.ltf_timeframe, settings.candle_limit)
        if len(c_entry) < settings.ema_slow + 5:
            logger.warning("Not enough entry-timeframe candles yet, skipping tick.")
            return

        # 2. Analyze each timeframe (off the event loop — CPU-heavy)
        (ind, st, tn, fvg, ifvg), (ind_t, st_t, tn_t, _, _), (ind_l, st_l, tn_l, _, _), smc_e, smc_t, smc_l = await asyncio.gather(
            asyncio.to_thread(_analyze, c_entry),         # entry / decision (15m)
            asyncio.to_thread(_analyze, c_trend),         # trend / bias (1h)
            asyncio.to_thread(_analyze, c_ltf),           # timing (5m)
            asyncio.to_thread(smc.analyze, c_entry),      # SMC read (15m)
            asyncio.to_thread(smc.analyze, c_trend),      # SMC read (1h)
            asyncio.to_thread(smc.analyze, c_ltf),        # SMC read (5m)
        )
        bias = _trend_bias(ind_t, st_t, tn_t)
        bias_txt = {1: "bullish", -1: "bearish", 0: "neutral"}[bias]

        # 3. Entry votes on the lower timeframe (need >= min_signals)
        enabled = strategies.parse_enabled(settings.strategies)
        ctx = strategies.StrategyContext(candles=c_entry, ind=ind, supertrend=st, trendline=tn, fvg=fvg, ifvg=ifvg,
                                         extra={"smc": smc_e, "smc_trend": smc_t})
        result = strategies.evaluate(ctx, enabled, settings.min_signals, weights)
        entry_action = result["action"]

        # 3b. Higher-TF filter: don't fight the 1h trend
        action = entry_action
        if entry_action == "BUY" and bias < 0:
            action = "HOLD"
            reason = f"Blocked — 15m wanted BUY but 1h trend is bearish | {result['reason']}"
        elif entry_action == "SELL" and bias > 0:
            action = "HOLD"
            reason = f"Blocked — 15m wanted SELL but 1h trend is bullish | {result['reason']}"
        else:
            reason = f"1h trend {bias_txt} | {result['reason']}"

        # 3c. AGENTS — the PRIMARY decision-maker. Book-derived trading agents each
        # propose a side; the learning ensemble weights them by their LIVE win-rate,
        # decides take/skip (meta-labeling) and the position-size multiplier. This is
        # what makes the bot improve from its own trades. The LLM brain (3d) runs ONLY
        # as a fallback when the agents abstain.
        agent_drove = False
        agent_size_mult = 1.0
        agent_sl_hint = None
        agent_ids: list[str] = []
        ens = None
        try:
            agent_ctx = agents_mod.AgentContext(
                symbol=symbol, price=ind["close"], c_entry=c_entry, c_trend=c_trend,
                ind=ind, supertrend=st, trendline=tn, fvg=fvg, ifvg=ifvg,
                smc=smc_e, smc_trend=smc_t, bias=bias, confluence=result)
            ens = await ensemble.decide(agent_ctx)
        except Exception as e:
            logger.error(f"[{symbol}] agent ensemble failed: {e}")
        if ens and ens["action"] in ("BUY", "SELL"):
            cand = ens["action"]
            if settings.ai_respect_trend_filter and (
                (cand == "BUY" and bias < 0) or (cand == "SELL" and bias > 0)
            ):
                action = "HOLD"
                reason = f"Agents wanted {cand} but blocked by 1h {bias_txt} trend | {ens['reason']}"
            else:
                action = cand
                agent_drove = True
                agent_size_mult = ens["size_mult"]
                agent_sl_hint = ens["sl_hint"]
                agent_ids = ens["agents"]
                reason = f"{ens['reason']} · 1h {bias_txt}"

        # 3d. AI brain (FALLBACK ONLY) — runs when the agents abstained. In 'decide'
        # mode it makes the call + structure SL/TP; otherwise it refines SL/TP. Falls
        # back to the mechanical result above if unavailable/timed out.
        # COOLDOWN: an LLM call can take 15–150s, so at a fast tick cadence we must NOT
        # call it every tick (that overlaps ticks and triggers provider rate-limit storms).
        # Consult it at most once per ai_min_interval_sec PER SYMBOL; agents cover the rest.
        ai_plan = None
        ai_drove = False
        _now = time.monotonic()
        _ai_cooldown_ok = (settings.ai_min_interval_sec <= 0 or
                           _now - _last_ai_call.get(symbol, 0.0) >= settings.ai_min_interval_sec)
        if not agent_drove and _ai_cooldown_ok and ai_brain.available() \
                and settings.ai_mode in ("decide", "refine", "advisory"):
            _last_ai_call[symbol] = _now
            try:
                hist_edge = await edge_mod.get_edge(symbol)  # backtested per-strategy edge (cached ~6h)
                news_ctx = await news.ai_context(symbol)     # cached: upcoming events + latest headlines
                snapshot = ai_brain.build_snapshot(
                    symbol, ind["close"], c_entry, c_trend, ind, st, tn, fvg, ifvg,
                    ind_t, st_t, tn_t, bias_txt, result["votes"], weights, {},
                    smc_entry=smc_e, smc_trend=smc_t,
                    c_ltf=c_ltf, ind_l=ind_l, st_l=st_l, tn_l=tn_l, smc_ltf=smc_l,
                    historical_edge=hist_edge, news=news_ctx)
                ai_plan = await asyncio.to_thread(ai_brain.analyze, snapshot)
            except Exception as e:
                logger.error(f"[{symbol}] AI analysis failed: {e}")

        if ai_plan and settings.ai_mode == "decide":
            cand = ai_plan["action"]
            if cand in ("BUY", "SELL") and ai_plan["confidence"] < settings.ai_min_confidence:
                cand = "HOLD"
            if settings.ai_respect_trend_filter and (
                (cand == "BUY" and bias < 0) or (cand == "SELL" and bias > 0)
            ):
                action = "HOLD"
                reason = f"AI wanted {cand} but blocked by 1h {bias_txt} trend | {ai_plan['reasoning']}"
            else:
                action = cand
                ai_drove = cand in ("BUY", "SELL")
                reason = f"AI {ai_plan['confidence']:.0%} → {cand} · 1h {bias_txt} | {ai_plan['reasoning']}"
        elif ai_plan and settings.ai_mode == "refine":
            ai_drove = action in ("BUY", "SELL")  # mechanical decides; AI supplies SL/TP
            if ai_drove:
                reason = f"{reason} | AI SL/TP · {ai_plan['reasoning']}"

        ai_meta = ({"model": settings.ai_model, "mode": settings.ai_mode,
                    "confidence": ai_plan["confidence"], "reasoning": ai_plan["reasoning"],
                    "invalidation": ai_plan["invalidation"], "proposed_action": ai_plan["action"],
                    "stop_loss": ai_plan["stop_loss"], "take_profits": ai_plan["take_profits"],
                    "drove": ai_drove} if ai_plan else None)
        agent_meta = ({"decision": ens["action"], "confidence": ens["confidence"],
                       "size_mult": ens["size_mult"], "agents": ens["agents"],
                       "proposals": ens["proposals"], "reason": ens["reason"],
                       "drove": agent_drove} if ens else None)
        logger.info(f"[{symbol}] {action} (1h bias={bias_txt}, votes={result['votes']}, "
                    f"agents={'drove' if agent_drove else (ens['action'] if ens else 'off')}, "
                    f"ai={'on' if ai_plan else 'off'}{f' {ai_plan['confidence']:.0%}' if ai_plan else ''})")

        # 4. Execute (position-aware: no pyramiding; bracketed entry from flat)
        order_id = None
        order_status = None
        lots = 0
        sl_price = tp_price = None
        fraction = 0.0
        rr = float(settings.risk_reward)
        price = ind["close"]
        if action in ("BUY", "SELL"):
            side = action.lower()
            want_long = action == "BUY"
            try:
                pos = await _delta.get_position_size(symbol)
                if pos != 0 and ((pos > 0) != want_long):
                    # opposite signal -> cancel protective orders, then close
                    pid = await _delta.get_product_id(symbol)
                    for o in await _delta.get_live_orders(symbol):
                        if o.get("reduce_only"):
                            await _delta.cancel_order(o["id"], pid)
                    close_side = "buy" if pos < 0 else "sell"
                    order = await _delta.place_order(symbol, close_side, abs(int(round(pos))), reduce_only=True)
                    await db.bot_state.delete_one({"_id": symbol})
                    order_id = str(order.get("id", ""))
                    order_status = "closed_position"
                    logger.info(f"Closed {pos} {symbol} via {close_side} {abs(int(round(pos)))} (cancelled brackets)")
                elif pos != 0:
                    order_status = "held (already in position)"
                    logger.info(f"Signal {action} but already {('LONG' if pos>0 else 'SHORT')} {abs(pos)} — holding (no pyramiding)")
                elif not allow_entry:
                    order_status = "flat (managed only — not the active chart symbol)"
                else:
                    # concurrency cap across all coins
                    n_open = sum(1 for p in await _delta.get_positions() if p.get("size"))
                    if n_open >= settings.max_concurrent_positions:
                        order_status = f"skipped: max {settings.max_concurrent_positions} concurrent positions open"
                        logger.info(f"{symbol} flat + {action} signal but {n_open} positions already open — skipping")
                        raise _SkipEntry()
                    # news blackout: don't open a NEW trade into a high-impact release (whipsaw risk)
                    blocked, ev = await news.in_blackout()
                    if blocked:
                        order_status = (f"skipped: news blackout — {ev['impact']}-impact {ev['currency']} "
                                        f"'{ev['title']}' within {settings.news_blackout_min}m")
                        logger.info(f"{symbol} entry blocked — news blackout: {ev['currency']} {ev['title']}")
                        raise _SkipEntry()
                    # daily loss circuit breaker: stop opening new trades once down the day's limit
                    wallet = await _delta.get_wallet()
                    total_bal = float(wallet.get("balance") or 0)
                    avail = float(wallet.get("available_balance") or total_bal)
                    breached, day_pnl, day_limit = await _daily_loss_exceeded(total_bal)
                    if breached:
                        order_status = f"skipped: daily loss limit hit (today {day_pnl} <= {day_limit})"
                        logger.warning(f"{symbol} entry blocked — daily loss limit reached (today ${day_pnl} <= ${day_limit})")
                        raise _SkipEntry()
                    # "big" trade: most indicators agree -> allow a wider stop + larger
                    # target with a smaller position (special case).
                    agree = result["buy_score"] if want_long else result["sell_score"]
                    total = max(len(enabled), 1)
                    big = (agree / total) >= settings.big_trade_min_agree

                    # flat -> place SL where the idea is invalidated. Use the AI's
                    # structure stop (clamped to a sane band) when it drove the call,
                    # otherwise the mechanical ATR/SuperTrend/structure combination.
                    sl_price = sl_method = None
                    if ai_drove and ai_plan:
                        clamped = _clamp_sl(want_long, price, ai_plan.get("stop_loss"))
                        if clamped is not None:
                            sl_price, sl_method = clamped, "ai"
                    elif agent_drove and agent_sl_hint is not None:
                        clamped = _clamp_sl(want_long, price, agent_sl_hint)
                        if clamped is not None:
                            sl_price, sl_method = clamped, "agent"
                    if sl_price is None:
                        sl_price, _sl_cands, sl_method = _combined_sl(want_long, price, c_entry, st)
                        if ai_drove:
                            sl_method += "+ai-fallback"
                        elif agent_drove:
                            sl_method += "+agent-fallback"

                    # Per-symbol POINT limits (ETH): cap the stop distance (tight for normal,
                    # wider for big trades). Keeps the structure stop if it's already tighter.
                    prof = _symbol_profile(symbol, big)
                    if prof:
                        sl_dist = abs(price - sl_price)
                        sl_dist = min(max(sl_dist, prof["sl_min"]), prof["sl_max"])
                        sl_price = price - sl_dist if want_long else price + sl_dist
                        sl_method = f"{sl_method}|pts<= {prof['sl_max']:g}" + ("|BIG" if big else "")
                    risk = abs(price - sl_price)
                    if prof:
                        # ETH uses a fixed point target ladder (R:R already >= 2.5 by config),
                        # so the structure-reachability gate is skipped — take the trade.
                        ok, room_info = True, "point-based target"
                    else:
                        ok, room_info = _room_for_1to2(want_long, price, risk, c_trend)
                    if not ok:
                        order_status = f"skipped: 1:2 not reachable ({room_info})"
                        logger.info(f"Skip {side}: {room_info}")
                    else:
                        cv = await _delta.get_contract_value(symbol)
                        # Leverage: as high as configured (50x), but auto-lowered so the
                        # liquidation price sits beyond the stop (SL always triggers first).
                        lev = _safe_leverage(price, risk)
                        await _delta.set_leverage(symbol, lev)

                        # CAPITAL-BASED sizing: deploy a fixed % of balance as margin
                        # (50% normal, less for big trades -> smaller lots). When agents
                        # drove the call, scale by the ensemble's Kelly/probability size
                        # multiplier (AFML bet sizing) — conviction trades get more capital.
                        cap_pct = settings.big_trade_capital_pct if big else settings.position_capital_pct
                        if agent_drove:
                            cap_pct *= agent_size_mult
                        margin_usd = total_bal * cap_pct / 100.0
                        # respect the available-balance ceiling
                        margin_usd = min(margin_usd, avail * settings.margin_cap_pct)
                        notional = margin_usd * lev
                        lots = int(notional / (price * cv)) if price > 0 and cv > 0 else 0
                        lots = max(lots, 1)
                        fraction = round(cap_pct, 2)  # for logging: % of capital used as margin
                        risk_dollars = risk * lots * cv  # $ at risk if the stop is hit

                        # dynamic reward:risk ceiling (floor 1:2) from conviction + 1h strength
                        strength = st_t["latest"].get("strength", 0) if st_t else 0
                        aligned = (bias > 0 and want_long) or (bias < 0 and not want_long)
                        rr = max(_rr_target(agree, strength, aligned), settings.risk_reward)

                        # 1) ENTRY (market, no bracket — we manage TP/SL ourselves)
                        order = await _delta.place_order(symbol, side, lots)
                        order_id = str(order.get("id", ""))
                        order_status = order.get("state", "unknown")

                        # The traded fill price (recorded for PnL/breakeven only). The trade
                        # is MANAGED on the MARK price so SL/TP match the chart and trigger on
                        # mark — on the thin testnet the fill can diverge from mark, but the
                        # geometry (20pt stop, 50-60pt target) stays anchored to mark.
                        posdoc = await _delta.get_position(symbol)
                        fill_price = float(posdoc.get("entry_price") or price)
                        entry_ref = price            # mark decision price = management basis
                        # risk stays mark-based (abs(price - sl_price)); do NOT re-anchor to the fill

                        # 2) TP levels (anchored to the mark entry reference).
                        if prof:
                            # ETH: fixed target ladder inside the point band (50–60 pts
                            # normal, wider for big). 1 TP -> just the top; 2 TPs -> band edges.
                            lo, hi = prof["tp_min"], prof["tp_max"]
                            dists = [hi] if settings.max_tps <= 1 else [lo, hi]
                            tps = [round(entry_ref + d, 1) if want_long else round(entry_ref - d, 1) for d in dists]
                            sizes = _tp_sizes(lots, [(p, None) for p in tps])
                            tp_source = "eth-points" + ("-BIG" if big else "")
                        else:
                            # other symbols: AI structure targets (validated), else structure snap
                            accepted = _valid_ai_tps(want_long, entry_ref, risk, ai_plan["take_profits"]) if (ai_drove and ai_plan) else []
                            if accepted:
                                tps = [p for p, _ in accepted]
                                sizes = _tp_sizes(lots, accepted)
                                tp_source = "ai"
                            else:
                                tps = _target_levels(want_long, entry_ref, risk, c_entry, c_trend, fvg, rr)
                                sizes = _tp_sizes(lots, [(p, None) for p in tps])
                                tp_source = "ai-fallback" if ai_drove else "structure"
                        tp_price = tps[0] if tps else None
                        close_side = "sell" if want_long else "buy"

                        # 3) place partial take-profits + full stop-loss (reduce-only stops)
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
                            await _delta.place_stop_order(symbol, close_side, lots, sl_price, "stop_loss_order")
                        except Exception as e:
                            logger.error(f"SL order failed @ {sl_price}: {e} — position is UNPROTECTED")

                        # 4) persist trade state for breakeven management
                        await db.bot_state.replace_one(
                            {"_id": symbol},
                            {"_id": symbol, "side": side, "size": lots, "entry": entry_ref,
                             "fill_price": fill_price,
                             "sl": sl_price, "tps": placed_tps, "rr": rr, "be_moved": False,
                             "votes": result["votes"], "agents": agent_ids, "risk_dollars": round(risk_dollars, 4),
                             "sl_method": sl_method, "tp_source": tp_source, "ai": ai_meta,
                             "leverage": lev, "big_trade": big, "capital_pct": cap_pct,
                             "opened_at": datetime.now(timezone.utc)},
                            upsert=True,
                        )
                        slip = fill_price - entry_ref
                        logger.info(f"Opened {side} {lots} lots @ {lev}x {'[BIG] ' if big else ''}| margin {fraction:.0f}% cap "
                                    f"(risk ${risk_dollars:.2f}) | mark-entry {entry_ref:.1f} fill {fill_price:.1f} "
                                    f"(slip {slip:+.1f}) SL {sl_price:.1f} ({sl_method}) "
                                    f"TPs {[t['price'] for t in placed_tps]} ({tp_source})")
            except _SkipEntry:
                pass  # order_status already set (e.g. concurrency cap)
            except Exception as e:
                logger.error(f"Order failed: {e}")
                order_status = f"error: {e}"

        # 5. Log to MongoDB
        log_doc = {
            "timestamp": datetime.now(timezone.utc),
            "symbol": symbol,
            "action": action,
            "reason": reason,
            "price": price,
            "quantity": lots if action != "HOLD" else 0,
            "indicators": {
                "ema_fast": ind["ema_fast"],
                "ema_slow": ind["ema_slow"],
                "ema_signal": ind["ema_signal"],
                "rsi": ind["rsi"],
                "rsi_signal": ind["rsi_signal"],
                "breakout_signal": ind["breakout_signal"],
                "breakout_level": ind["breakout_level"],
                "macd": ind.get("macd"),
                "macd_signal_line": ind.get("macd_signal_line"),
                "macd_hist": ind.get("macd_hist"),
                "macd_signal": ind.get("macd_signal"),
            },
            "votes": result["votes"],
            "supertrend": st["latest"] if st else None,
            "trendline": tn["latest"] if tn else None,
            "fvg": fvg["latest"] if fvg else None,
            "ifvg": ifvg["latest"] if ifvg else None,
            "smc": {
                "entry_trend": (smc_e or {}).get("trend"),
                "trend_trend": (smc_t or {}).get("trend"),
                "structure_break": (smc_e or {}).get("structure_break"),
                "recent_sweep": (smc_e or {}).get("recent_sweep"),
                "zone": ((smc_e or {}).get("premium_discount") or {}).get("zone"),
            } if smc_e else None,
            "timeframes": {
                "entry_tf": settings.entry_timeframe,
                "trend_tf": settings.trend_timeframe,
                "trend_bias": bias_txt,
                "entry_action": entry_action,
                "trend_supertrend": st_t["latest"] if st_t else None,
            },
            "sizing": {"risk_pct": round(fraction, 2), "leverage": settings.leverage,
                       "reward_risk": round(rr, 1),
                       "stop_loss": round(sl_price, 2) if sl_price else None,
                       "take_profit": round(tp_price, 2) if tp_price else None},
            "ai": ai_meta,
            "agents": agent_meta,
            "order_id": order_id,
            "order_status": order_status,
            "paper_trade": True,
        }
        await db.trade_logs.insert_one(log_doc)

    except Exception as e:
        logger.exception(f"[{symbol}] process error: {e}")


async def bot_tick():
    """
    One scheduler iteration. Processes the ACTIVE chart symbol (new entries allowed)
    plus EVERY symbol that still has an open position or lingering trade state, so
    switching coins never orphans a live trade — old positions keep being managed
    (breakeven, opposite-close, cleanup) while the active symbol trades fresh.
    """
    trade_syms = [s.strip().upper() for s in settings.trade_symbols.split(",") if s.strip()]
    tracked = set(trade_syms)
    try:
        for p in await _delta.get_positions():
            if p.get("size") and p.get("product_symbol"):
                tracked.add(p["product_symbol"])
    except Exception as e:
        logger.error(f"tracked-positions fetch failed: {e}")
    try:
        async for s in db.bot_state.find({}, {"_id": 1}):
            tracked.add(s["_id"])
    except Exception:
        pass

    weights = await autotune.get_weights()  # live-tuned vote weights (cached ~30s)
    # All configured trade symbols can open NEW trades simultaneously; any other
    # symbol with a stray open position is managed (closed) but not re-entered.
    for sym in tracked:
        await _process_symbol(sym, allow_entry=(sym in trade_syms), weights=weights)


def start_bot():
    global _bot_running
    if _bot_running:
        return {"status": "already_running"}
    secs = settings.check_interval_seconds if settings.check_interval_seconds > 0 \
        else settings.check_interval_minutes * 60
    scheduler.add_job(
        bot_tick,
        trigger=IntervalTrigger(seconds=secs),
        id="bot_tick",
        replace_existing=True,
        next_run_time=datetime.now(timezone.utc),  # run immediately on start
        # A tick makes several exchange calls and can outlast a short interval. Never let
        # ticks overlap or pile up: run at most one at a time, and if several are due, run
        # just the latest. This keeps a 30s cadence safe even when a tick runs long.
        max_instances=1,
        coalesce=True,
        misfire_grace_time=max(10, secs),
    )
    # Start the scheduler only once; subsequent start/stop just add/remove the job.
    if not scheduler.running:
        scheduler.start()
    _bot_running = True
    logger.info(f"Bot started — checking every {secs}s.")
    return {"status": "started"}


def stop_bot():
    global _bot_running
    if not _bot_running:
        return {"status": "not_running"}
    try:
        scheduler.remove_job("bot_tick")
    except Exception:
        pass
    # Leave the scheduler running (just without the job) so it can be restarted.
    _bot_running = False
    logger.info("Bot stopped.")
    return {"status": "stopped"}


def set_symbol(symbol: str) -> dict:
    """Change the symbol the bot trades, at runtime. Takes effect on the next tick."""
    settings.trading_symbol = symbol
    logger.info(f"Bot trading symbol changed to {symbol}.")
    return {"symbol": symbol, "running": _bot_running}


def bot_status() -> dict:
    syms = [s.strip().upper() for s in settings.trade_symbols.split(",") if s.strip()]
    return {
        "running": _bot_running,
        "interval_minutes": settings.check_interval_minutes,
        "interval_seconds": (settings.check_interval_seconds if settings.check_interval_seconds > 0
                             else settings.check_interval_minutes * 60),
        "symbol": settings.trading_symbol,
        "symbols": syms,
        "next_run": (
            str(scheduler.get_job("bot_tick").next_run_time)
            if _bot_running and scheduler.get_job("bot_tick")
            else None
        ),
    }
