import asyncio
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from bot.scheduler import start_bot, stop_bot, bot_status, set_symbol
from bot.delta_client import DeltaClient, minutes_to_resolution
from bot.backtest import run_backtest
from bot import strategies, autotune
from config import settings

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
