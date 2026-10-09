"""Trailing-stop rules + a self-tuning engine that learns trail/risk parameters by replaying closed trades."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from bot import quant
from config import settings
from db import db

logger = logging.getLogger("bot.risk_engine")

GRID_TRIGGER = (0.3, 0.4, 0.5, 0.6, 0.7, 0.8)   # fraction of the way to TP1 that arms breakeven
GRID_LOCK = (0.25, 0.5, 0.75)                   # fraction of TP1 profit locked once TP1 fills
_cache: dict = {"ts": 0.0, "doc": None}


# --------------------------------------------------------------------------- #
#  Trailing rule (shared by the live bot and the simulator)
# --------------------------------------------------------------------------- #
def next_sl(long: bool, basis: float, tp1: float, sl: float | None, stage: int, mark: float,
            tp1_filled: bool, trigger: float, lock: float, fee_buf: float):
    """(new_sl, new_stage, why) if the stop should tighten now, else None.

    Stage 1: price ≥ `trigger` of the way to TP1 → stop to basis ± fee_buf (fee-covered breakeven).
    Stage 2: TP1 filled → stop locks `lock` of the TP1 profit. Never loosens; never crosses the mark.
    """
    sgn = 1 if long else -1
    dist = (tp1 - basis) * sgn
    if dist <= 0:
        return None
    cands = []
    if tp1_filled and stage < 2:
        cands.append((basis + sgn * lock * dist, 2, f"TP1 filled → lock {lock:.0%} of TP1 profit"))
    if stage < 1 and (tp1_filled or (mark - basis) * sgn >= trigger * dist):
        cands.append((basis + sgn * fee_buf, 1, f"{trigger:.0%} of the way to TP1 → fee-covered breakeven"))
    for new, new_stage, why in cands:
        new = round(new, 1)
        if sl is not None and (new - sl) * sgn <= 0:
            continue  # would loosen or not move
        if (mark - new) * sgn <= 0:
            continue  # wrong side of the mark: the exchange would fill it instantly
        return new, new_stage, why
    return None


def fee_buffer(basis: float, taker_rate: float) -> float:
    """Price distance that covers round-trip taker fees plus a small safety margin."""
    return basis * (2 * taker_rate + settings.trail_be_buffer_pct / 100)


# --------------------------------------------------------------------------- #
#  Replay
# --------------------------------------------------------------------------- #
def simulate(t: dict, trigger: float, lock: float) -> float | None:
    """R-multiple of a recorded trade replayed on its 1m mark path under these trail params.

    Pessimistic within a bar: the stop is checked before targets. Unclosed size exits at the last close.
    """
    long = t["side"] == "buy"
    sgn = 1 if long else -1
    basis, sl, size = t["basis"], t["sl0"], t["size"]
    tps = t["tps"]
    risk = abs(basis - sl)
    if risk <= 0 or size <= 0 or not tps or not t["path"]:
        return None
    fee_buf = fee_buffer(basis, t["fee_rate"])
    remaining, pnl, stage, tp_i = size, 0.0, 0, 0
    for _, _o, h, l, c in t["path"]:
        adverse, favorable = (l, h) if long else (h, l)
        if (adverse - sl) * sgn <= 0:
            pnl += (sl - basis) * sgn * remaining
            remaining = 0
            break
        while tp_i < len(tps) and remaining > 0 and (favorable - tps[tp_i]["price"]) * sgn >= 0:
            q = remaining if tp_i == len(tps) - 1 else min(tps[tp_i]["size"], remaining)
            pnl += (tps[tp_i]["price"] - basis) * sgn * q
            remaining -= q
            tp_i += 1
        if remaining <= 0:
            break
        move = next_sl(long, basis, tps[0]["price"], sl, stage, c, tp_i >= 1, trigger, lock, fee_buf)
        if move:
            sl, stage, _ = move
    if remaining > 0:
        pnl += (t["path"][-1][4] - basis) * sgn * remaining
    pnl -= 2 * t["fee_rate"] * basis * size
    return pnl / (risk * size)


def train_from(trades: list[dict], current: dict) -> dict:
    """Grid-search trail params on recorded trades; adopt only a clear, well-sampled improvement."""
    def score(trigger, lock):
        rs = [r for r in (simulate(t, trigger, lock) for t in trades) if r is not None]
        return {"trigger": trigger, "lock": lock, "n": len(rs),
                "expectancy": round(sum(rs) / len(rs), 4) if rs else None, "rs": rs}

    grid = [score(tr, lk) for tr in GRID_TRIGGER for lk in GRID_LOCK]
    grid = [g for g in grid if g["n"]]
    out = {**current, "n": 0, "adopted": False, "top": [], "trained_at": datetime.now(timezone.utc)}
    if not grid:
        return out
    cur = score(current["trigger"], current["lock"])
    best = max(grid, key=lambda g: g["expectancy"])
    enough = best["n"] >= settings.risk_engine_min_trades
    if enough and (cur["expectancy"] is None or best["expectancy"] >= cur["expectancy"] + settings.risk_engine_min_gain_r):
        out.update(trigger=best["trigger"], lock=best["lock"], adopted=True)
        chosen = best
    else:
        chosen = cur if cur["n"] else best
    if enough:
        k = quant.kelly_fraction(chosen["rs"])
        pct = 100 * 0.5 * k if k and k > 0 else settings.risk_engine_min_risk_pct  # half-Kelly, as % of balance
        out["trade_risk_pct"] = round(min(max(pct, settings.risk_engine_min_risk_pct), settings.max_trade_risk_pct), 2)
    out.update(n=chosen["n"], expectancy=chosen["expectancy"],
               top=[{k: g[k] for k in ("trigger", "lock", "n", "expectancy")}
                    for g in sorted(grid, key=lambda g: -g["expectancy"])[:5]])
    return out


# --------------------------------------------------------------------------- #
#  Persistence
# --------------------------------------------------------------------------- #
def _defaults() -> dict:
    return {"trigger": settings.trail_trigger_frac, "lock": settings.trail_lock_frac,
            "trade_risk_pct": settings.max_trade_risk_pct}


async def params(force: bool = False) -> dict:
    """Live trail/risk params: learned values (if any), never riskier than the configured 3% cap."""
    if force or time.time() - _cache["ts"] > 60:
        try:
            _cache["doc"] = await db.risk_params.find_one({"_id": "current"})
        except Exception as e:
            logger.warning(f"risk_params read failed ({type(e).__name__}) — using last/defaults")
        _cache["ts"] = time.time()
    p = _defaults()
    if settings.risk_engine_enabled and _cache["doc"]:
        p.update({k: _cache["doc"][k] for k in p if _cache["doc"].get(k) is not None})
    p["trade_risk_pct"] = min(p["trade_risk_pct"], settings.max_trade_risk_pct)
    return p


async def record_path(delta, symbol: str, state: dict, opened_at: datetime, closed_at: datetime,
                      fee_rate: float) -> None:
    """Store a closed trade with its 1m mark path for replay."""
    sl0 = state.get("sl0", None if state.get("be_moved") else state.get("sl"))
    if sl0 is None or not state.get("tps"):
        return
    minutes = (closed_at - opened_at).total_seconds() / 60
    candles = await delta.get_candles(symbol, 1, int(min(max(minutes + 10, 30), 2000)), mark=True)
    lo, hi = opened_at.timestamp(), closed_at.timestamp()
    await db.trade_paths.insert_one({
        "symbol": symbol, "side": state["side"], "basis": float(state.get("fill_price") or state["entry"]),
        "sl0": float(sl0), "tps": state["tps"], "size": float(state["size"]), "fee_rate": fee_rate,
        "opened_at": opened_at, "closed_at": closed_at, "complete": minutes + 10 <= 2000,
        "path": [[c["time"], c["open"], c["high"], c["low"], c["close"]] for c in candles if lo <= c["time"] <= hi],
    })


async def retrain() -> dict:
    """Retrain on the last 200 complete trades and persist the result."""
    if not settings.risk_engine_enabled:
        return {"enabled": False}
    trades = [d async for d in db.trade_paths.find({"complete": True}).sort("closed_at", -1).limit(200)]
    current = await params(force=True)
    doc = await asyncio.to_thread(train_from, trades, current)
    await db.risk_params.replace_one({"_id": "current"}, {"_id": "current", **doc}, upsert=True)
    _cache.update(ts=0.0)
    logger.info(f"risk engine trained on {doc['n']} trades: trigger {doc['trigger']} lock {doc['lock']} "
                f"risk {doc['trade_risk_pct']}% (adopted={doc['adopted']}, expectancy {doc.get('expectancy')}R)")
    return doc


async def status() -> dict:
    p = await params(force=True)
    doc = {k: v for k, v in (_cache["doc"] or {}).items() if k != "_id"}
    if hasattr(doc.get("trained_at"), "isoformat"):
        doc["trained_at"] = doc["trained_at"].isoformat()
    try:
        recorded = await db.trade_paths.count_documents({"complete": True})
    except Exception:
        recorded = None
    return {"enabled": settings.risk_engine_enabled, "active": p, "recorded_trades": recorded,
            "min_trades": settings.risk_engine_min_trades, "last_training": doc or None}
