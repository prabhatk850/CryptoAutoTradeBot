"""
Technical indicators: EMA Crossover, RSI, Trendline Breakout.
All functions accept a list of OHLCV dicts from Delta Exchange.
"""
import numpy as np
import pandas as pd
from typing import TypedDict


class IndicatorResult(TypedDict):
    ema_fast: float
    ema_slow: float
    ema_signal: str        # "BULLISH_CROSS" | "BEARISH_CROSS" | "NEUTRAL"
    rsi: float
    rsi_signal: str        # "OVERSOLD" | "OVERBOUGHT" | "NEUTRAL"
    breakout_signal: str   # "BREAKOUT_UP" | "BREAKOUT_DOWN" | "NEUTRAL"
    breakout_level: float | None
    macd: float
    macd_signal_line: float
    macd_hist: float
    macd_signal: str       # "BULLISH_CROSS" | "BEARISH_CROSS" | "BULLISH" | "BEARISH" | "NEUTRAL"
    close: float


def candles_to_df(candles: list[dict]) -> pd.DataFrame:
    """Convert Delta Exchange candle list to a clean DataFrame."""
    df = pd.DataFrame(candles)
    # Delta returns: time, open, high, low, close, volume
    df = df.rename(columns={"time": "timestamp"})
    for col in ["open", "high", "low", "close", "volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.sort_values("timestamp").reset_index(drop=True)
    return df


def calc_ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, adjust=False).mean()
    avg_loss = loss.ewm(com=period - 1, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    # Flat candles (no gains AND no losses) make RSI undefined -> neutral 50.
    # All-gains (avg_loss==0, avg_gain>0) -> 100. Never leave NaN/inf in output.
    rsi = rsi.where(~((avg_loss == 0) & (avg_gain > 0)), 100.0)
    return rsi.replace([np.inf, -np.inf], np.nan).fillna(50.0).clip(0, 100)


def calc_trendline_breakout(df: pd.DataFrame, lookback: int = 20) -> tuple[str, float | None]:
    """
    Simple swing-high / swing-low trendline breakout.
    Looks at the last `lookback` candles.
    Returns (signal, level).
    """
    if len(df) < lookback + 2:
        return "NEUTRAL", None

    window = df.tail(lookback + 1)
    resistance = window["high"].iloc[:-1].max()   # highest high in lookback (excluding latest)
    support = window["low"].iloc[:-1].min()       # lowest low in lookback

    latest_close = df["close"].iloc[-1]
    prev_close = df["close"].iloc[-2]

    # Breakout UP: price closes above resistance for the first time
    if latest_close > resistance and prev_close <= resistance:
        return "BREAKOUT_UP", float(resistance)

    # Breakout DOWN: price closes below support
    if latest_close < support and prev_close >= support:
        return "BREAKOUT_DOWN", float(support)

    return "NEUTRAL", None


def calc_macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    """Classic MACD: EMA(fast) - EMA(slow), signal = EMA(macd), hist = macd - signal.
    Returns (macd_line, signal_line, hist) as pandas Series."""
    macd_line = calc_ema(series, fast) - calc_ema(series, slow)
    signal_line = calc_ema(macd_line, signal)
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def compute_indicators(
    candles: list[dict],
    ema_fast: int = 9,
    ema_slow: int = 21,
    rsi_period: int = 14,
    rsi_oversold: float = 30.0,
    rsi_overbought: float = 70.0,
    macd_fast: int = 12,
    macd_slow: int = 26,
    macd_signal_len: int = 9,
) -> IndicatorResult:
    df = candles_to_df(candles)

    close = df["close"]
    ema_f = calc_ema(close, ema_fast)
    ema_s = calc_ema(close, ema_slow)
    rsi = calc_rsi(close, rsi_period)

    # EMA crossover signal (compare last two candles)
    prev_diff = ema_f.iloc[-2] - ema_s.iloc[-2]
    curr_diff = ema_f.iloc[-1] - ema_s.iloc[-1]
    if prev_diff < 0 and curr_diff > 0:
        ema_signal = "BULLISH_CROSS"
    elif prev_diff > 0 and curr_diff < 0:
        ema_signal = "BEARISH_CROSS"
    else:
        ema_signal = "NEUTRAL"

    # RSI signal
    rsi_val = float(rsi.iloc[-1])
    if rsi_val < rsi_oversold:
        rsi_signal = "OVERSOLD"
    elif rsi_val > rsi_overbought:
        rsi_signal = "OVERBOUGHT"
    else:
        rsi_signal = "NEUTRAL"

    breakout_signal, breakout_level = calc_trendline_breakout(df)

    # MACD (12/26/9): line vs signal, with a fresh-cross flag
    macd_line, macd_sig, macd_hist = calc_macd(close, macd_fast, macd_slow, macd_signal_len)
    macd_v = float(macd_line.iloc[-1])
    macd_sig_v = float(macd_sig.iloc[-1])
    hist_v = float(macd_hist.iloc[-1])
    prev_hist = float(macd_hist.iloc[-2]) if len(macd_hist) > 1 else hist_v
    if prev_hist <= 0 and hist_v > 0:
        macd_signal = "BULLISH_CROSS"
    elif prev_hist >= 0 and hist_v < 0:
        macd_signal = "BEARISH_CROSS"
    elif hist_v > 0:
        macd_signal = "BULLISH"
    elif hist_v < 0:
        macd_signal = "BEARISH"
    else:
        macd_signal = "NEUTRAL"

    return IndicatorResult(
        ema_fast=float(ema_f.iloc[-1]),
        ema_slow=float(ema_s.iloc[-1]),
        ema_signal=ema_signal,
        rsi=rsi_val,
        rsi_signal=rsi_signal,
        breakout_signal=breakout_signal,
        breakout_level=breakout_level,
        macd=round(macd_v, 4),
        macd_signal_line=round(macd_sig_v, 4),
        macd_hist=round(hist_v, 4),
        macd_signal=macd_signal,
        close=float(close.iloc[-1]),
    )
