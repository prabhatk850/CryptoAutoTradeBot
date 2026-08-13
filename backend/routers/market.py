import asyncio
import logging
import math
import time as _time
import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from bot.delta_client import DeltaClient
from bot.lux_indicators import supertrend_ai, trendline_breakout_navigator, fair_value_gaps, inverse_fvg
from bot.indicators import compute_indicators, calc_macd, candles_to_df, calc_ema
from bot import ai_brain
from bot import smc as _smc
from config import settings
import pandas as _pd

router = APIRouter(prefix="/market", tags=["market"])
logger = logging.getLogger("bot.market")
_delta = DeltaClient()

# Cache for the heavy indicators endpoint (per symbol+resolution+limit).
_IND_CACHE: dict[str, tuple[float, dict]] = {}
_IND_TTL = 12.0


def _finite(v):
    return v if isinstance(v, (int, float)) and math.isfinite(v) else None


def _error(e: Exception, what: str):
    """Return a clean JSON error instead of a 500 stack trace."""
    status = 502
    detail = str(e)
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
    sym = symbol or settings.trading_symbol
    try:
        return await _delta.get_ticker(sym)
    except Exception as e:
        return _error(e, "get_ticker")


@router.get("/candles")
async def candles(symbol: str = None, resolution: int = 5, limit: int = 100):
    sym = symbol or settings.trading_symbol
    try:
        return await _delta.get_candles(sym, resolution, limit)
    except Exception as e:
        return _error(e, "get_candles")


@router.get("/indicators")
async def indicators(symbol: str = None, resolution: int = 5, limit: int = 300):
    """SuperTrend AI + Trendline Navigator series aligned to candle times, for chart overlay."""
    sym = symbol or settings.trading_symbol
    key = f"{sym}:{resolution}:{limit}"
    now = _time.time()
    cached = _IND_CACHE.get(key)
    if cached and now - cached[0] < _IND_TTL:
        return cached[1]
    try:
        # fetch enough history for long-swing indicators, then expose the last `limit`
        fetch = max(limit, settings.candle_limit)
        candles = await _delta.get_candles(sym, resolution, fetch)
        keep = {int(c["time"]) for c in candles[-limit:]}

        async def _safe_traded():
            # volume lives on the traded feed (mark candles have none)
            try:
                return await _delta.get_candles(sym, resolution, fetch, mark=False)
            except Exception:
                return []

        # Run the CPU-heavy indicators OFF the event loop so other requests aren't blocked.
        st, tn, fvg, ifvg, smc_r, macd_r, traded = await asyncio.gather(
            asyncio.to_thread(supertrend_ai, candles),
            asyncio.to_thread(trendline_breakout_navigator, candles),
            asyncio.to_thread(fair_value_gaps, candles),
            asyncio.to_thread(inverse_fvg, candles),
            asyncio.to_thread(_smc.analyze, candles),
            asyncio.to_thread(_macd_series, candles, keep),
            _safe_traded(),
        )

        out: dict = {"symbol": sym, "supertrend": None, "trendline": None, "fvg": None,
                     "ifvg": None, "smc": smc_r, "macd": macd_r,
                     "volume": _volume_series(candles, traded, keep),
                     "ema200": _ema_series(candles, 200, keep)}
        if st:
            pts = [
                {"time": int(t), "ts": _finite(ts), "os": o, "ama": _finite(ama)}
                for t, ts, o, ama in zip(st["time"], st["ts"], st["os"], st["ama"])
                if int(t) in keep
            ]
            out["supertrend"] = {"points": pts, "signals": st["signals"], "latest": st["latest"]}
        if tn:
            out["trendline"] = {"points": [p for p in tn["points"] if p["time"] in keep],
                                "active": [p for p in tn["active"] if p["time"] in keep],
                                "signals": tn["signals"], "latest": tn["latest"]}
        min_h = settings.fvg_min_pct / 100  # only show zones whose height >= this % of price

        def _big_enough(top, bottom, mid):
            base = mid or ((top + bottom) / 2)
            return base > 0 and abs(top - bottom) / base >= min_h

        if fvg:
            zones = []
            for z in fvg["unmitigated"]:
                mid = round((z["top"] + z["bottom"]) / 2, 2)
                if _big_enough(z["top"], z["bottom"], mid):
                    zones.append({**z, "mid": mid})
            out["fvg"] = {"zones": zones[-20:], "end": fvg["end"], "latest": fvg["latest"]}
        if ifvg:
            zones = [z for z in ifvg["zones"] if _big_enough(z["top"], z["bottom"], z.get("mid"))]
            out["ifvg"] = {"zones": zones, "end": int(candles[-1]["time"]), "latest": ifvg["latest"]}

        _IND_CACHE[key] = (now, out)
        return out
    except Exception as e:
        return _error(e, "indicators")


def _macd_series(candles, keep):
    """Per-bar MACD (12/26/9) for the chart sub-pane, filtered to the visible window."""
    df = candles_to_df(candles)
    if df.empty or "close" not in df:
        return None
    line, sig, hist = calc_macd(df["close"])
    pts = []
    for t, m, s, h in zip(df["timestamp"], line, sig, hist):
        if _pd.isna(m) or _pd.isna(s):
            continue
        ti = int(t)
        if ti not in keep:
            continue
        pts.append({"time": ti, "macd": round(float(m), 4),
                    "signal": round(float(s), 4), "hist": round(float(h), 4)})
    if not pts:
        return None
    last = pts[-1]
    state = ("BULLISH" if last["hist"] > 0 else "BEARISH" if last["hist"] < 0 else "NEUTRAL")
    return {"points": pts, "latest": {**last, "state": state}}


def _ema_series(candles, period, keep):
    """EMA over the full fetched history (so long MAs like 200 are warmed up),
    returned only for the visible window."""
    df = candles_to_df(candles)
    if df.empty or "close" not in df:
        return None
    e = calc_ema(df["close"], period)
    pts = [{"time": int(t), "value": round(float(v), 2)}
           for t, v in zip(df["timestamp"], e)
           if int(t) in keep and _pd.notna(v)]
    return {"points": pts} if pts else None


def _volume_series(mark_candles, traded, keep):
    """Volume bars for the chart sub-pane: magnitude from the traded feed, colored by
    the (mark) candle direction, plus a 20-period volume moving average."""
    volmap = {int(c["time"]): float(c.get("volume") or 0) for c in (traded or [])}
    pts = []
    for c in mark_candles:
        t = int(c["time"])
        if t not in keep:
            continue
        pts.append({"time": t, "value": round(volmap.get(t, 0.0), 2), "up": c["close"] >= c["open"]})
    if not pts:
        return None
    vals = [p["value"] for p in pts]
    win = 20
    ma = [{"time": pts[i]["time"],
           "value": round(sum(vals[max(0, i - win + 1):i + 1]) / len(vals[max(0, i - win + 1):i + 1]), 2)}
          for i in range(len(pts))]
    avg20 = round(sum(vals[-win:]) / min(len(vals), win), 2)
    latest = vals[-1]
    return {"points": pts, "ma": ma,
            "latest": {"volume": latest, "avg20": avg20,
                       "rel": round(latest / avg20, 2) if avg20 else None}}


def _analyze_tf(candles):
    ind = compute_indicators(
        candles, ema_fast=settings.ema_fast, ema_slow=settings.ema_slow,
        rsi_period=settings.rsi_period, rsi_oversold=settings.rsi_oversold,
        rsi_overbought=settings.rsi_overbought,
    )
    return (ind, supertrend_ai(candles), trendline_breakout_navigator(candles),
            fair_value_gaps(candles), inverse_fvg(candles))


@router.get("/analyze")
async def analyze(symbol: str = None):
    """Read-only AI analysis of the current chart: builds the same multi-timeframe
    snapshot the live bot uses, asks Claude for a plan (direction / structure SL /
    TP1-3 / confidence / reasoning), and returns both. Places NO order."""
    sym = symbol or settings.trading_symbol
    try:
        c_entry = await _delta.get_candles(sym, settings.entry_timeframe, settings.candle_limit)
        c_trend = await _delta.get_candles(sym, settings.trend_timeframe, settings.candle_limit)
        c_ltf = await _delta.get_candles(sym, settings.ltf_timeframe, settings.candle_limit)
        (ind, st, tn, fvg, ifvg), (ind_t, st_t, tn_t, _, _), (ind_l, st_l, tn_l, _, _) = await asyncio.gather(
            asyncio.to_thread(_analyze_tf, c_entry),
            asyncio.to_thread(_analyze_tf, c_trend),
            asyncio.to_thread(_analyze_tf, c_ltf),
        )
        from bot.scheduler import _trend_bias
        from bot import smc
        bias = _trend_bias(ind_t, st_t, tn_t)
        bias_txt = {1: "bullish", -1: "bearish", 0: "neutral"}[bias]
        smc_e, smc_t, smc_l = await asyncio.gather(
            asyncio.to_thread(smc.analyze, c_entry),
            asyncio.to_thread(smc.analyze, c_trend),
            asyncio.to_thread(smc.analyze, c_ltf),
        )
        from bot import edge as edge_mod
        hist_edge = await edge_mod.get_edge(sym)
        snapshot = ai_brain.build_snapshot(sym, ind["close"], c_entry, c_trend, ind, st, tn,
                                           fvg, ifvg, ind_t, st_t, tn_t, bias_txt, {}, {}, {},
                                           smc_entry=smc_e, smc_trend=smc_t,
                                           c_ltf=c_ltf, ind_l=ind_l, st_l=st_l, tn_l=tn_l, smc_ltf=smc_l,
                                           historical_edge=hist_edge)
        if not ai_brain.available():
            return {"symbol": sym, "snapshot": snapshot, "ai_plan": None,
                    "note": "AI brain unavailable (Claude CLI not found or AI_ENABLED=false)."}
        plan = await asyncio.to_thread(ai_brain.analyze, snapshot)
        return {"symbol": sym, "snapshot": snapshot, "ai_plan": plan,
                "note": "Advisory only — no order placed."}
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
