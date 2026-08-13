"""
Backtested "edge" per strategy, per symbol.

Runs the backtest on CLEAN mark-price candles (free of testnet fill slippage) to
measure each strategy's real profit-factor / expectancy, and caches it. This becomes
an evidence-based PRIOR the AI brain uses to weight the live strategy votes — so the
LLM trusts signals that have actually made money on THIS symbol and discounts the ones
that haven't. Refreshes on a slow cadence (edge changes slowly).
"""
import asyncio
import logging
import time

from bot.delta_client import DeltaClient
from bot.backtest import run_backtest
from config import settings

logger = logging.getLogger("bot.edge")
_delta = DeltaClient()
_cache: dict[str, tuple[float, dict]] = {}   # symbol -> (ts, edge)
_TTL = 6 * 3600      # recompute at most every 6h
_BARS = 1500


async def get_edge(symbol: str) -> dict:
    """{strategy_id: {"pf": profit_factor, "exp": expectancy_R, "n": trades}} on mark data.
    Cached per symbol; returns {} (or last good) if it can't compute."""
    sym = symbol.upper()
    now = time.time()
    cached = _cache.get(sym)
    if cached and now - cached[0] < _TTL:
        return cached[1]
    try:
        ce = await _delta.get_candles(sym, settings.entry_timeframe, _BARS)
        ct = await _delta.get_candles(sym, settings.trend_timeframe, max(_BARS // 4, 400))
        res = await asyncio.to_thread(run_backtest, ce, ct)
        edge = {
            k: {"pf": m.get("profit_factor", 0), "exp": m.get("expectancy", 0), "n": m.get("trades", 0)}
            for k, m in (res.get("results") or {}).items()
            if k != "COMBINED" and m.get("trades", 0) > 0
        }
        if edge:
            _cache[sym] = (now, edge)
            logger.info(f"[{sym}] strategy edge refreshed: "
                        + ", ".join(f"{k} PF{v['pf']}" for k, v in sorted(edge.items(), key=lambda kv: -kv[1]['pf'])[:4]))
        return edge or (cached[1] if cached else {})
    except Exception as e:
        logger.error(f"[{sym}] edge compute failed: {e}")
        return cached[1] if cached else {}
