import asyncio
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from bot.scheduler import start_bot, stop_bot, bot_status, set_symbol
from bot.delta_client import DeltaClient, minutes_to_resolution
from bot.backtest import run_backtest
from bot import strategies, autotune
from config import settings
from db import db

router = APIRouter(prefix="/bot", tags=["bot"])
_delta = DeltaClient()


@router.post("/start")
async def start():
    return start_bot()


@router.post("/stop")
async def stop():
    return stop_bot()


@router.get("/status")
async def status():
    return bot_status()


@router.get("/backtest")
async def backtest(symbol: str = None, bars: int = 1500):
    """Backtest every strategy + the combined engine on recent history."""
    sym = symbol or settings.trading_symbol
    try:
        c_entry = await _delta.get_candles(sym, settings.entry_timeframe, bars)
        c_trend = await _delta.get_candles(sym, settings.trend_timeframe, max(bars // 4, 300))
        res = await asyncio.to_thread(run_backtest, c_entry, c_trend)
        res["symbol"] = sym
        res["entry_tf"] = settings.entry_timeframe
        res["trend_tf"] = settings.trend_timeframe
        return res
    except Exception as e:
        return JSONResponse(status_code=502, content={"error": str(e)})


@router.get("/performance")
async def performance():
    """Live per-strategy performance + the auto-tuned vote weights."""
    return await autotune.status()


def _skip_bucket(status: str) -> str:
    """Group a skip reason into a human category for the training view."""
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
    return "Other"


@router.get("/training")
async def training(days: int = 30, limit: int = 25):
    """Everything needed to judge whether the bot is LEARNING correctly.

    Separates the two things that used to be conflated:
      • strategy P/L — mark→mark, the quality of the DECISION (this trains the tuner)
      • execution P/L — real fills, the quality of the VENUE (never trains anything)
    Plus why entries were skipped, so the guard rails are visible rather than silent.
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)

    async def _outcomes():
        # Only trades whose fills we actually matched. Unverified rows can't be scored
        # (a stuck position once produced 129 phantom "wins" that made this panel lie).
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

    perf, outcomes, skips, unverified = await asyncio.gather(
        autotune.status(), _outcomes(), _skips(), _unverified())

    for o in outcomes:
        o["_id"] = str(o.get("_id", ""))
        for k in ("opened_at", "closed_at"):
            if hasattr(o.get(k), "isoformat"):
                o[k] = o[k].isoformat()

    n = len(outcomes)
    strat_total = round(sum(float(o.get("strategy_pnl") or 0) for o in outcomes), 2)
    exec_total = round(sum(float(o.get("execution_pnl") or 0) for o in outcomes), 2)
    strat_wins = sum(1 for o in outcomes if float(o.get("strategy_pnl") or 0) > 0)

    return {
        "config": {
            "trains_on": "mark" if settings.autotune_use_mark_pnl else "fills",
            "symbols": [s.strip().upper() for s in settings.trade_symbols.split(",") if s.strip()],
            "max_entry_spread_pct": settings.max_entry_spread_pct,
            "max_exit_slippage_pct": settings.max_exit_slippage_pct,
            "autotune_enabled": settings.autotune_enabled,
            "min_trades_before_weighting": settings.autotune_min_trades,
        },
        "summary": {
            "trades": n,
            "strategy_pnl": strat_total,      # what the decisions were worth
            "execution_pnl": exec_total,      # what the venue actually paid
            "slippage_cost": round(exec_total - strat_total, 2),
            "strategy_win_rate": round(strat_wins / n * 100, 1) if n else 0,
            "unverified_excluded": unverified,
        },
        "strategies": perf["strategies"],
        "recent_trades": outcomes,
        "skipped": [{"reason": k, "count": v} for k, v in
                    sorted(skips.items(), key=lambda kv: -kv[1])],
        "skipped_total": sum(skips.values()),
        "window_days": days,
    }


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


@router.post("/symbol")
async def change_symbol(symbol: str):
    """Switch the symbol the bot trades (e.g. BTCUSD / ETHUSD)."""
    symbol = symbol.upper().strip()
    try:
        pid = await _delta.get_product_id(symbol)
    except Exception:
        pid = None
    if not pid:
        return JSONResponse(status_code=400, content={"error": f"Unknown symbol '{symbol}'"})
    return set_symbol(symbol)
