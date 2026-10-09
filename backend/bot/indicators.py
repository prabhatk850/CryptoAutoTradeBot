"""Classic indicators (EMA, RSI, MACD, breakout, BB/KC squeeze, ADX, VWAP, ATR) over candle dicts."""
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd

from config import settings


def candles_to_df(candles: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(candles).rename(columns={"time": "timestamp"})
    for col in ("open", "high", "low", "close", "volume"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.sort_values("timestamp").reset_index(drop=True)


def calc_atr(candles: list[dict], length: int) -> list[float]:
    """Wilder ATR per bar; bars before the first full window reuse the first value."""
    n = len(candles)
    tr = [0.0] * n
    for i, c in enumerate(candles):
        if i == 0:
            tr[i] = c["high"] - c["low"]
        else:
            pc = candles[i - 1]["close"]
            tr[i] = max(c["high"] - c["low"], abs(c["high"] - pc), abs(c["low"] - pc))
    atr: list[Optional[float]] = [None] * n
    prev, s = None, 0.0
    for i in range(n):
        if i < length:
            s += tr[i]
            if i == length - 1:
                prev = atr[i] = s / length
        else:
            prev = atr[i] = (prev * (length - 1) + tr[i]) / length
    first = next((a for a in atr if a is not None), tr[0] if tr else 0.0)
    return [a if a is not None else first for a in atr]


def calc_ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    avg_gain = delta.clip(lower=0).ewm(com=period - 1, adjust=False).mean()
    avg_loss = (-delta.clip(upper=0)).ewm(com=period - 1, adjust=False).mean()
    rsi = 100 - (100 / (1 + avg_gain / avg_loss.replace(0, np.nan)))
    # All-gain bars -> 100, flat bars -> 50; never emit NaN/inf.
    rsi = rsi.where(~((avg_loss == 0) & (avg_gain > 0)), 100.0)
    return rsi.replace([np.inf, -np.inf], np.nan).fillna(50.0).clip(0, 100)


def calc_trendline_breakout(df: pd.DataFrame, lookback: int = 20) -> tuple[str, Optional[float]]:
    """First close beyond the prior `lookback`-bar high/low."""
    if len(df) < lookback + 2:
        return "NEUTRAL", None
    window = df.tail(lookback + 1)
    resistance = window["high"].iloc[:-1].max()
    support = window["low"].iloc[:-1].min()
    last, prev = df["close"].iloc[-1], df["close"].iloc[-2]
    if last > resistance and prev <= resistance:
        return "BREAKOUT_UP", float(resistance)
    if last < support and prev >= support:
        return "BREAKOUT_DOWN", float(support)
    return "NEUTRAL", None


def calc_macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    """Returns (macd_line, signal_line, histogram)."""
    line = calc_ema(series, fast) - calc_ema(series, slow)
    sig = calc_ema(line, signal)
    return line, sig, line - sig


def calc_bollinger(series: pd.Series, period: int = 20, mult: float = 2.0):
    """Returns (mid, upper, lower)."""
    mid = series.rolling(period).mean()
    std = series.rolling(period).std(ddof=0)
    return mid, mid + mult * std, mid - mult * std


def calc_keltner(df: pd.DataFrame, candles: list[dict], period: int = 20,
                 atr_mult: float = 1.5, atr_len: int = 10):
    """Returns (mid, upper, lower)."""
    mid = calc_ema(df["close"], period)
    atr = pd.Series(calc_atr(candles, atr_len), index=df.index).astype(float)
    return mid, mid + atr_mult * atr, mid - atr_mult * atr


def calc_squeeze(bb_upper, bb_lower, kc_upper, kc_lower) -> pd.Series:
    """True while Bollinger Bands sit inside the Keltner Channel."""
    return (bb_upper < kc_upper) & (bb_lower > kc_lower)


def calc_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder ADX (0-100)."""
    high, low, close = df["high"], df["low"], df["close"]
    up, down = high.diff(), -low.diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    atr = tr.ewm(com=period - 1, adjust=False).mean().replace(0, np.nan)
    plus_di = 100 * plus_dm.ewm(com=period - 1, adjust=False).mean() / atr
    minus_di = 100 * minus_dm.ewm(com=period - 1, adjust=False).mean() / atr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(com=period - 1, adjust=False).mean().fillna(0).clip(0, 100)


def calc_vwap(candles: list[dict]) -> Optional[float]:
    """UTC-day VWAP from traded-price candles; None when volume is missing (mark candles have none)."""
    if not candles:
        return None
    days = [datetime.fromtimestamp(int(c["time"]), tz=timezone.utc).date() for c in candles]
    start = len(candles) - 1
    while start > 0 and days[start - 1] == days[-1]:
        start -= 1
    seg = candles[start:]
    vols = [float(c.get("volume") or 0) for c in seg]
    total = sum(vols)
    if total <= 0:
        return None
    typical = [(c["high"] + c["low"] + c["close"]) / 3 for c in seg]
    return round(sum(tp * v for tp, v in zip(typical, vols)) / total, 2)


def _last(series: pd.Series) -> Optional[float]:
    v = series.iloc[-1]
    return float(v) if pd.notna(v) else None


def compute_indicators(candles: list[dict], vwap_candles: Optional[list[dict]] = None) -> dict:
    """Latest-bar indicator snapshot for one timeframe."""
    s = settings
    df = candles_to_df(candles)
    close = df["close"]
    ema_f, ema_s = calc_ema(close, s.ema_fast), calc_ema(close, s.ema_slow)

    prev_diff = ema_f.iloc[-2] - ema_s.iloc[-2]
    curr_diff = ema_f.iloc[-1] - ema_s.iloc[-1]
    ema_signal = ("BULLISH_CROSS" if prev_diff < 0 < curr_diff
                  else "BEARISH_CROSS" if prev_diff > 0 > curr_diff else "NEUTRAL")

    rsi = float(calc_rsi(close, s.rsi_period).iloc[-1])
    rsi_signal = "OVERSOLD" if rsi < s.rsi_oversold else "OVERBOUGHT" if rsi > s.rsi_overbought else "NEUTRAL"

    breakout_signal, breakout_level = calc_trendline_breakout(df)

    macd_line, macd_sig, macd_hist = calc_macd(close)
    hist = float(macd_hist.iloc[-1])
    prev_hist = float(macd_hist.iloc[-2]) if len(macd_hist) > 1 else hist
    if prev_hist <= 0 < hist:
        macd_signal = "BULLISH_CROSS"
    elif prev_hist >= 0 > hist:
        macd_signal = "BEARISH_CROSS"
    else:
        macd_signal = "BULLISH" if hist > 0 else "BEARISH" if hist < 0 else "NEUTRAL"

    bb_mid, bb_upper, bb_lower = calc_bollinger(close, s.bb_period, s.bb_mult)
    kc_mid, kc_upper, kc_lower = calc_keltner(df, candles, s.kc_period, s.kc_atr_mult, s.kc_atr_len)
    squeeze = calc_squeeze(bb_upper, bb_lower, kc_upper, kc_lower)
    sq_now = bool(squeeze.iloc[-1]) if pd.notna(squeeze.iloc[-1]) else False
    sq_prev = bool(squeeze.iloc[-2]) if len(squeeze) > 1 and pd.notna(squeeze.iloc[-2]) else sq_now
    if sq_prev and not sq_now:
        up = pd.notna(bb_mid.iloc[-1]) and close.iloc[-1] > bb_mid.iloc[-1]
        squeeze_signal = "RELEASE_UP" if up else "RELEASE_DOWN"
    else:
        squeeze_signal = "SQUEEZE_ON" if sq_now else "NEUTRAL"

    return {
        "close": float(close.iloc[-1]),
        "ema_fast": float(ema_f.iloc[-1]),
        "ema_slow": float(ema_s.iloc[-1]),
        "ema_signal": ema_signal,
        "rsi": rsi,
        "rsi_signal": rsi_signal,
        "breakout_signal": breakout_signal,
        "breakout_level": breakout_level,
        "macd": round(float(macd_line.iloc[-1]), 4),
        "macd_signal_line": round(float(macd_sig.iloc[-1]), 4),
        "macd_hist": round(hist, 4),
        "macd_signal": macd_signal,
        "bb_mid": _last(bb_mid), "bb_upper": _last(bb_upper), "bb_lower": _last(bb_lower),
        "kc_mid": _last(kc_mid), "kc_upper": _last(kc_upper), "kc_lower": _last(kc_lower),
        "squeeze_signal": squeeze_signal,
        "adx": _last(calc_adx(df, s.adx_period)),
        "vwap": calc_vwap(vwap_candles if vwap_candles is not None else candles) if s.vwap_enabled else None,
    }
