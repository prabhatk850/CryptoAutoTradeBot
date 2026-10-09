"""Bot control, backtest, strategy performance and the training monitor."""
import asyncio
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from bot.scheduler import start_bot, stop_bot, bot_status, override_daily_target
from bot.delta_client import DeltaClient
from bot.backtest import run_backtest
from bot import strategies, autotune, ensemble, risk_engine
from config import settings
from db import db

router = APIRouter(prefix="/bot", tags=["bot"])
_delta = DeltaClient()


@router.post("/start")
async def start():
    """Manual start — also overrides today's 3-5-7 profit stop."""
    await override_daily_target()
    return start_bot()


@router.post("/stop")
async def stop():
    return stop_bot()


@router.get("/status")
async def status():
    return bot_status()


@router.get("/backtest")
async def backtest(symbol: str = None, bars: int = 1500, adx_gate_min: float = None):
    """Backtest every strategy + COMBINED; `adx_gate_min` adds a COMBINED_ADX_GATED comparison row."""
    sym = symbol or settings.trading_symbol
    try:
        c_entry = await _delta.get_candles(sym, settings.entry_timeframe, bars)
        c_trend = await _delta.get_candles(sym, settings.trend_timeframe, max(bars // 4, 300))
        res = await asyncio.to_thread(run_backtest, c_entry, c_trend, None, None, adx_gate_min)
        res["symbol"] = sym
        res["entry_tf"] = settings.entry_timeframe
        res["trend_tf"] = settings.trend_timeframe
        return res
    except Exception as e:
        return JSONResponse(status_code=502, content={"error": str(e)})


@router.get("/performance")
async def performance():
    """Live performance: per-strategy auto-tuned vote weights + the learning agents'
    win-rates/reliability (the book-derived agents that now drive the decisions)."""
    strat, agents = await asyncio.gather(autotune.status(), ensemble.status())
    return {**strat, "agents": agents}


@router.get("/performance/shadow")
async def performance_shadow():
    """Live track record of shadow-only strategies (no history to backtest them on)."""
    return await autotune.shadow_status()


def _skip_bucket(status: str) -> str:
    """Human category for a skip reason."""
    s = (status or "").lower()
    if any(k in s for k in ("spread", "illiquid", "unexitable", "thin", "order book")):
        return "Illiquid market"
    if "news blackout" in s:
        return "News blackout"
    if "daily loss" in s:
        return "Daily loss limit"
    if "concurrent" in s:
        return "Position cap reached"
    if "1:2" in s or "reachable" in s:
        return "No room for 1:2 target"
    if "already in position" in s or "held" in s:
        return "Already in a position"
    if "expectancy gate" in s:
        return "Low historical edge"
    return "Other"


@router.get("/training")
async def training(days: int = 30, limit: int = 60):
    """Is the bot learning? Strategy (mark) vs execution (fills) P/L, skip reasons, liquidity-gate stats.

    recent_trades/summary = last `limit` verified outcomes (not windowed; `stale_days` shows their age);
    skipped = last `days` days only.
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)

    async def _outcomes():
        # Verified only — unverified rows once produced 129 phantom "wins".
        try:
            return [d async for d in db.trade_outcomes.find(
                {"verified": True}, {"votes": 0}).sort("closed_at", -1).limit(limit)]
        except Exception:
            return []

    async def _unverified():
        try:
            return await db.trade_outcomes.count_documents({"verified": {"$ne": True}})
        except Exception:
            return 0

    async def _liquidity_shadow():
        """How often the shadow liquidity gate would have blocked a trade."""
        try:
            total = await db.trade_logs.count_documents(
                {"timestamp": {"$gte": since}, "liquidity.mode": "shadow"})
            would_block = await db.trade_logs.count_documents(
                {"timestamp": {"$gte": since}, "liquidity.mode": "shadow", "liquidity.ok": False})
            return {"mode": settings.liquidity_gate_mode, "checked": total, "would_have_blocked": would_block,
                    "would_block_pct": round(would_block / total * 100, 1) if total else 0}
        except Exception:
            return {"mode": settings.liquidity_gate_mode, "checked": 0, "would_have_blocked": 0, "would_block_pct": 0}

    async def _skips():
        try:
            rows = {}
            async for d in db.trade_logs.find(
                {"timestamp": {"$gte": since}, "order_status": {"$regex": "^skipped", "$options": "i"}},
                {"order_status": 1},
            ):
                b = _skip_bucket(d.get("order_status", ""))
                rows[b] = rows.get(b, 0) + 1
            return rows
        except Exception:
            return {}

    perf, outcomes, skips, unverified, liq_shadow, engine = await asyncio.gather(
        autotune.status(), _outcomes(), _skips(), _unverified(), _liquidity_shadow(), risk_engine.status())

    for o in outcomes:
        o["_id"] = str(o.get("_id", ""))
        for k in ("opened_at", "closed_at"):
            if hasattr(o.get(k), "isoformat"):
                o[k] = o[k].isoformat()

    n = len(outcomes)
    latest_closed = outcomes[0].get("closed_at") if outcomes else None
    stale_days = None
    if latest_closed:
        try:
            ts = datetime.fromisoformat(str(latest_closed))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            stale_days = round((datetime.now(timezone.utc) - ts).total_seconds() / 86400, 1)
        except Exception:
            stale_days = None
    strat_total = round(sum(float(o.get("strategy_pnl") or 0) for o in outcomes), 2)
    exec_total = round(sum(float(o.get("execution_pnl") or 0) for o in outcomes), 2)
    strat_wins = sum(1 for o in outcomes if float(o.get("strategy_pnl") or 0) > 0)

    return {
        "config": {
            "trains_on": "mark" if settings.autotune_use_mark_pnl else "fills",
            "symbols": settings.symbols(),
            "max_entry_spread_pct": settings.max_entry_spread_pct,
            "max_exit_slippage_pct": settings.max_exit_slippage_pct,
            "autotune_enabled": settings.autotune_enabled,
            "min_trades_before_weighting": settings.autotune_min_trades,
        },
        "summary": {
            "trades": n,
            "strategy_pnl": strat_total,
            "execution_pnl": exec_total,
            "slippage_cost": round(exec_total - strat_total, 2),
            "strategy_win_rate": round(strat_wins / n * 100, 1) if n else 0,
            "unverified_excluded": unverified,
            "latest_closed_at": latest_closed,
            "stale_days": stale_days,
        },
        "strategies": perf["strategies"],
        "liquidity_gate": liq_shadow,
        "risk_engine": engine,
        "recent_trades": outcomes,
        "skipped": [{"reason": k, "count": v} for k, v in
                    sorted(skips.items(), key=lambda kv: -kv[1])],
        "skipped_total": sum(skips.values()),
        "window_days": days,
    }


@router.post("/risk-engine/train")
async def train_risk_engine():
    """Retrain trail/risk params now (also runs at every 6PM IST reset)."""
    doc = await risk_engine.retrain()
    if hasattr(doc.get("trained_at"), "isoformat"):
        doc["trained_at"] = doc["trained_at"].isoformat()
    return doc


@router.get("/strategies")
async def list_strategies():
    """All registered strategies + which are currently enabled (voting)."""
    return {
        "available": strategies.available(),
        "enabled": [s.value for s in strategies.parse_enabled(settings.strategies)],
        "min_signals": settings.min_signals,
    }


@router.post("/strategies")
async def set_strategies(ids: str):
    """Set which strategies vote (comma-separated ids). e.g. EMA_CROSS,SUPERTREND_AI"""
    enabled = strategies.parse_enabled(ids)
    settings.strategies = ",".join(s.value for s in enabled)
    return {"enabled": [s.value for s in enabled]}
