"""Auto-tune: credit each closed trade's R to the strategies that voted for it; expectancy sets vote weights."""
import time

from db import db
from config import settings
from bot import strategies

_weights_cache = {"ts": 0.0, "w": {}}
_EMPTY = {"count": 0, "sum_r": 0, "wins": 0, "win_rate": 0, "expectancy": 0}


async def _credit(collection, votes: dict, action: str, r_multiple: float):
    for sid, vote in (votes or {}).items():
        if vote == action:
            await collection.update_one(
                {"_id": sid},
                {"$inc": {"count": 1, "sum_r": float(r_multiple), "wins": 1 if r_multiple > 0 else 0}},
                upsert=True,
            )


async def record_outcome(votes: dict, action: str, r_multiple: float):
    """Credit live voters of `action` and refresh weights on next read."""
    await _credit(db.strategy_perf, votes, action, r_multiple)
    _weights_cache["ts"] = 0.0


async def record_shadow_outcome(shadow_votes: dict, action: str, r_multiple: float):
    """Same ledger shape, but shadow results never feed weights."""
    await _credit(db.shadow_strategy_perf, shadow_votes, action, r_multiple)


def _stats(d: dict) -> dict:
    c = d.get("count", 0)
    return {
        "count": c,
        "sum_r": round(d.get("sum_r", 0.0), 2),
        "wins": d.get("wins", 0),
        "win_rate": round(d.get("wins", 0) / c * 100, 1) if c else 0,
        "expectancy": round(d.get("sum_r", 0.0) / c, 3) if c else 0,
    }


async def get_perf() -> dict:
    return {d["_id"]: _stats(d) async for d in db.strategy_perf.find({})}


async def get_weights(force: bool = False) -> dict:
    """1.0 until a strategy has enough trades, then 1 + gain × expectancy (clamped). Cached 30s."""
    now = time.time()
    if not force and now - _weights_cache["ts"] < 30 and _weights_cache["w"]:
        return _weights_cache["w"]
    try:
        perf = await get_perf()
    except Exception:
        # Mongo down: keep trading on last/neutral weights.
        return _weights_cache["w"] or {sid.value: 1.0 for sid in strategies.StrategyId}
    w = {}
    for sid in strategies.StrategyId:
        p = perf.get(sid.value)
        if not settings.autotune_enabled or not p or p["count"] < settings.autotune_min_trades:
            w[sid.value] = 1.0
        else:
            raw = round(1.0 + settings.autotune_gain * p["expectancy"], 3)
            w[sid.value] = max(settings.weight_min, min(settings.weight_max, raw))
    _weights_cache.update(ts=now, w=w)
    return w


async def status() -> dict:
    perf = await get_perf()
    weights = await get_weights(force=True)
    return {
        "enabled": settings.autotune_enabled,
        "min_trades": settings.autotune_min_trades,
        "strategies": [
            {"id": sid.value, "label": strategies.LABELS.get(sid, sid.value),
             "weight": weights.get(sid.value, 1.0), **(perf.get(sid.value) or _EMPTY)}
            for sid in strategies.StrategyId
        ],
    }


async def shadow_status() -> dict:
    """Shadow-strategy track record — evidence for promoting one into STRATEGIES."""
    labels = {sid.value: label for sid, label in strategies.LABELS.items()}
    out = [{"id": d["_id"], "label": labels.get(d["_id"], d["_id"]), **_stats(d)}
           async for d in db.shadow_strategy_perf.find({})]
    return {"shadow_strategies": settings.shadow_strategies, "strategies": out}
