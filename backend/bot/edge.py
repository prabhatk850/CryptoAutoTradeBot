"""Per-strategy backtested edge on mark candles — the prior for AI vote weighting and the expectancy gate."""
import asyncio
import logging
import time

from bot.delta_client import DeltaClient
from bot.backtest import run_backtest
from config import settings

logger = logging.getLogger("bot.edge")
_delta = DeltaClient()
_cache: dict[str, tuple[float, dict]] = {}   # symbol -> (ts, edge)
_TTL = 6 * 3600
_BARS = 1500


async def get_edge(symbol: str) -> dict:
    """{strategy: {pf, exp, n}}, cached 6h; last good (or {}) on failure."""
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


async def blended_expectancy(symbol: str, votes: dict, action: str, weights: dict | None = None) -> dict:
    """Weighted backtested expectancy/PF of the strategies voting `action` (only those with enough trades)."""
    weights = weights or {}
    edge = await get_edge(symbol)
    w_sum = exp_sum = pf_sum = 0.0
    qualifying = []
    for sid, vote in (votes or {}).items():
        if vote != action:
            continue
        e = edge.get(sid)
        if not e or e.get("n", 0) < settings.expectancy_gate_min_strategy_n:
            continue
        w = weights.get(sid, 1.0)
        w_sum += w
        exp_sum += w * e.get("exp", 0.0)
        pf_sum += w * e.get("pf", 0.0)
        qualifying.append(sid)
    if w_sum <= 0:
        return {"blended_exp": None, "blended_pf": None, "n_strategies": 0, "qualifying": []}
    return {
        "blended_exp": round(exp_sum / w_sum, 4),
        "blended_pf": round(pf_sum / w_sum, 3),
        "n_strategies": len(qualifying),
        "qualifying": qualifying,
    }
