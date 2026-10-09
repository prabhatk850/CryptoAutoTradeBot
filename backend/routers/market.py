"""Market data, chart overlays, and a read-only AI analysis (never places orders)."""
import asyncio
import logging
import math
import time

import httpx
import pandas as pd
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from bot import ai_brain, edge, smc
from bot.delta_client import DeltaClient
from bot.indicators import calc_ema, calc_macd, candles_to_df
from bot.lux_indicators import supertrend_ai, trendline_breakout_navigator, fair_value_gaps, inverse_fvg
from bot.scheduler import BIAS_TXT, analyze_timeframe, trend_bias
from config import settings

router = APIRouter(prefix="/market", tags=["market"])
logger = logging.getLogger("bot.market")
_delta = DeltaClient()

_IND_TTL = 12.0
_IND_CACHE: dict[str, tuple[float, dict]] = {}   # "sym:res:limit" -> (ts, payload)


def _finite(v):
    return v if isinstance(v, (int, float)) and math.isfinite(v) else None


def _error(e: Exception, what: str):
    """Clean JSON error (upstream status when known) instead of a 500 trace."""
    status, detail = 502, str(e)
    if isinstance(e, httpx.HTTPStatusError):
        status = e.response.status_code
        try:
            detail = e.response.json()
        except Exception:
            detail = e.response.text
    logger.warning("%s failed: %s", what, detail)
    return JSONResponse(status_code=status, content={"error": what, "detail": detail})


@router.get("/ticker")
async def ticker(symbol: str = None):
    try:
        return await _delta.get_ticker(symbol or settings.trading_symbol)
    except Exception as e:
        return _error(e, "get_ticker")


@router.get("/candles")
async def candles(symbol: str = None, resolution: int = 5, limit: int = 100):
    try:
        return await _delta.get_candles(symbol or settings.trading_symbol, resolution, limit)
    except Exception as e:
        return _error(e, "get_candles")


@router.get("/indicators")
async def indicators(symbol: str = None, resolution: int = 5, limit: int = 300):
    """Chart overlays (SuperTrend, trendlines, FVG/IFVG, SMC, MACD, volume, EMA200) for the last `limit` bars."""
    sym = symbol or settings.trading_symbol
    key = f"{sym}:{resolution}:{limit}"
    now = time.time()
    cached = _IND_CACHE.get(key)
    if cached and now - cached[0] < _IND_TTL:
        return cached[1]
    try:
        fetch = max(limit, settings.candle_limit)  # extra history warms up long indicators
        bars = await _delta.get_candles(sym, resolution, fetch)
        keep = {int(c["time"]) for c in bars[-limit:]}

        async def _traded():  # volume only exists on the traded feed
            try:
                return await _delta.get_candles(sym, resolution, fetch, mark=False)
            except Exception:
                return []

        st, tn, fvg, ifvg, smc_r, macd_r, traded = await asyncio.gather(
            asyncio.to_thread(supertrend_ai, bars),
            asyncio.to_thread(trendline_breakout_navigator, bars),
            asyncio.to_thread(fair_value_gaps, bars),
            asyncio.to_thread(inverse_fvg, bars),
            asyncio.to_thread(smc.analyze, bars),
            asyncio.to_thread(_macd_series, bars, keep),
            _traded(),
        )
        out: dict = {"symbol": sym, "supertrend": None, "trendline": None, "fvg": None, "ifvg": None,
                     "smc": smc_r, "macd": macd_r, "volume": _volume_series(bars, traded, keep),
                     "ema200": _ema_series(bars, 200, keep)}
        if st:
            pts = [{"time": int(t), "ts": _finite(ts), "os": o, "ama": _finite(ama)}
                   for t, ts, o, ama in zip(st["time"], st["ts"], st["os"], st["ama"]) if int(t) in keep]
            out["supertrend"] = {"points": pts, "signals": st["signals"], "latest": st["latest"]}
        if tn:
            out["trendline"] = {"points": [p for p in tn["points"] if p["time"] in keep],
                                "active": [p for p in tn["active"] if p["time"] in keep],
                                "signals": tn["signals"], "latest": tn["latest"]}

        min_h = settings.fvg_min_pct / 100

        def _big_enough(top, bottom):
            mid = (top + bottom) / 2
            return mid > 0 and abs(top - bottom) / mid >= min_h

        if fvg:
            zones = [{**z, "mid": round((z["top"] + z["bottom"]) / 2, 2)}
                     for z in fvg["unmitigated"] if _big_enough(z["top"], z["bottom"])]
            out["fvg"] = {"zones": zones[-20:], "end": fvg["end"], "latest": fvg["latest"]}
        if ifvg:
            out["ifvg"] = {"zones": [z for z in ifvg["zones"] if _big_enough(z["top"], z["bottom"])],
                           "end": int(bars[-1]["time"]), "latest": ifvg["latest"]}
        _IND_CACHE[key] = (now, out)
        return out
    except Exception as e:
        return _error(e, "indicators")


def _macd_series(bars, keep):
    """Per-bar MACD for the visible window."""
    df = candles_to_df(bars)
    if df.empty or "close" not in df:
        return None
    line, sig, hist = calc_macd(df["close"])
    pts = [{"time": int(t), "macd": round(float(m), 4), "signal": round(float(s), 4), "hist": round(float(h), 4)}
           for t, m, s, h in zip(df["timestamp"], line, sig, hist)
           if not (pd.isna(m) or pd.isna(s)) and int(t) in keep]
    if not pts:
        return None
    last = pts[-1]
    state = "BULLISH" if last["hist"] > 0 else "BEARISH" if last["hist"] < 0 else "NEUTRAL"
    return {"points": pts, "latest": {**last, "state": state}}


def _ema_series(bars, period, keep):
    """EMA over full history (warmed up), returned for the visible window."""
    df = candles_to_df(bars)
    if df.empty or "close" not in df:
        return None
    pts = [{"time": int(t), "value": round(float(v), 2)}
           for t, v in zip(df["timestamp"], calc_ema(df["close"], period)) if int(t) in keep and pd.notna(v)]
    return {"points": pts} if pts else None


def _volume_series(mark_bars, traded, keep, win: int = 20):
    """Traded volume coloured by mark-candle direction, plus a 20-bar average."""
    volmap = {int(c["time"]): float(c.get("volume") or 0) for c in traded or []}
    pts = [{"time": int(c["time"]), "value": round(volmap.get(int(c["time"]), 0.0), 2), "up": c["close"] >= c["open"]}
           for c in mark_bars if int(c["time"]) in keep]
    if not pts:
        return None
    vals = [p["value"] for p in pts]
    ma = [{"time": p["time"], "value": round(sum(vals[max(0, i - win + 1):i + 1]) / len(vals[max(0, i - win + 1):i + 1]), 2)}
          for i, p in enumerate(pts)]
    avg20 = round(sum(vals[-win:]) / min(len(vals), win), 2)
    return {"points": pts, "ma": ma,
            "latest": {"volume": vals[-1], "avg20": avg20, "rel": round(vals[-1] / avg20, 2) if avg20 else None}}


@router.get("/analyze")
async def analyze(symbol: str = None):
    """The live bot's multi-timeframe snapshot plus an AI plan. Advisory only — places no order."""
    sym = symbol or settings.trading_symbol
    try:
        c_entry = await _delta.get_candles(sym, settings.entry_timeframe, settings.candle_limit)
        c_trend = await _delta.get_candles(sym, settings.trend_timeframe, settings.candle_limit)
        c_ltf = await _delta.get_candles(sym, settings.ltf_timeframe, settings.candle_limit)
        entry, trend, timing = await asyncio.gather(*(asyncio.to_thread(analyze_timeframe, c)
                                                      for c in (c_entry, c_trend, c_ltf)))
        snapshot = ai_brain.build_snapshot(sym, entry, trend, timing, BIAS_TXT[trend_bias(trend)], {}, {},
                                           historical_edge=await edge.get_edge(sym))
        if not ai_brain.available():
            return {"symbol": sym, "snapshot": snapshot, "ai_plan": None,
                    "note": "AI brain unavailable (no configured provider or AI_ENABLED=false)."}
        plan = await asyncio.to_thread(ai_brain.analyze, snapshot)
        return {"symbol": sym, "snapshot": snapshot, "ai_plan": plan, "note": "Advisory only — no order placed."}
    except Exception as e:
        return _error(e, "analyze")


@router.get("/positions")
async def positions():
    try:
        return await _delta.get_positions()
    except Exception as e:
        return _error(e, "get_positions")


@router.get("/wallet")
async def wallet():
    try:
        return await _delta.get_wallet()
    except Exception as e:
        return _error(e, "get_wallet")
