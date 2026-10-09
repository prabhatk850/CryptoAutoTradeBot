"""Realized correlation between symbols, used to shrink a second same-direction position."""
from __future__ import annotations
import asyncio
import time
from typing import Optional

import pandas as pd

from bot.delta_client import DeltaClient

_cache: dict[tuple, tuple[float, Optional[float]]] = {}  # (sym_a, sym_b, tf) -> (ts, corr)
_TTL = 300.0  # correlation drifts slowly


async def realized_correlation(delta: DeltaClient, sym_a: str, sym_b: str,
                               timeframe_min: int, lookback: int = 200) -> Optional[float]:
    """Pearson correlation of bar returns (cached per pair), or None without enough data."""
    a, b = sorted((sym_a.upper(), sym_b.upper()))
    key = (a, b, timeframe_min)
    now = time.time()
    cached = _cache.get(key)
    if cached and now - cached[0] < _TTL:
        return cached[1]
    try:
        ca, cb = await asyncio.gather(
            delta.get_candles(a, timeframe_min, lookback),
            delta.get_candles(b, timeframe_min, lookback),
        )
    except Exception:
        return cached[1] if cached else None
    if len(ca) < 20 or len(cb) < 20:
        return cached[1] if cached else None

    da = {int(c["time"]): c["close"] for c in ca}
    db_ = {int(c["time"]): c["close"] for c in cb}
    common = sorted(set(da) & set(db_))
    if len(common) < 20:
        return cached[1] if cached else None

    sa = pd.Series([da[t] for t in common]).pct_change().dropna()
    sb = pd.Series([db_[t] for t in common]).pct_change().dropna()
    n = min(len(sa), len(sb))
    if n < 15:
        return cached[1] if cached else None
    corr = sa.tail(n).reset_index(drop=True).corr(sb.tail(n).reset_index(drop=True))
    result = float(corr) if pd.notna(corr) else None
    _cache[key] = (now, result)
    return result
