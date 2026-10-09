"""Funding-rate tracking: "extreme" is judged by percentile against the symbol's own Mongo history."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from typing import Optional

from bot.delta_client import DeltaClient
from config import settings
from db import db


async def record_and_bias(symbol: str, delta: DeltaClient) -> Optional[dict]:
    """Record the current funding/OI and classify it; `extreme` stays None until enough history exists."""
    try:
        cur = await delta.get_funding_and_oi(symbol)
    except Exception:
        return None
    if cur.get("funding_rate") is None:
        return None

    now = datetime.now(timezone.utc)
    try:
        await db.funding_history.insert_one({
            "symbol": symbol, "ts": now,
            "funding_rate": cur["funding_rate"], "oi": cur.get("oi"),
        })
    except Exception:
        pass  # a missed point only weakens the percentile

    since = now - timedelta(days=settings.funding_history_window_days)
    try:
        rates = [d["funding_rate"] async for d in db.funding_history.find(
            {"symbol": symbol, "ts": {"$gte": since}}, {"funding_rate": 1, "_id": 0})
            if isinstance(d.get("funding_rate"), (int, float))]
    except Exception:
        rates = []

    extreme = percentile = None
    if len(rates) >= settings.funding_min_history_samples:
        rank = sum(1 for r in rates if r <= cur["funding_rate"]) / len(rates)
        percentile = round(rank, 3)
        if rank >= settings.funding_extreme_percentile:
            extreme = "high"
        elif rank <= (1 - settings.funding_extreme_percentile):
            extreme = "low"

    return {
        "funding_rate": cur["funding_rate"],
        "oi": cur.get("oi"),
        "oi_change_6h": cur.get("oi_change_6h"),
        "percentile": percentile,
        "extreme": extreme,
        "samples": len(rates),
    }
