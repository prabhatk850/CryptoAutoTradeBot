"""
Quant primitives from the trading books (see .claude/skills/trading-books).

Pure functions, numpy only — no I/O, no bot state — so they're trivially testable and
safe to import anywhere. These implement the specific mechanics the agents rely on:

  • hurst_exponent   — regime detector (Chan Ch.2): H<0.5 mean-reverting, >0.5 trending
  • half_life        — Ornstein-Uhlenbeck mean-reversion speed (Chan Ch.2)
  • zscore           — how stretched price is vs its rolling mean (Chan linear/Bollinger MR)
  • kelly_fraction   — optimal leverage f=μ/σ² from a trade's R distribution (Chan Ch.8)
  • bet_size_from_prob — AFML Ch.10.3 sizing: probability → size in [0,1]
"""
from __future__ import annotations

import math
from typing import Sequence

import numpy as np


def _closes(candles: Sequence[dict]) -> np.ndarray:
    return np.asarray([float(c["close"]) for c in candles], dtype=float)


# --------------------------------------------------------------------------- #
#  Regime: Hurst exponent (Chan, "Algorithmic Trading" Ch.2)
# --------------------------------------------------------------------------- #
def hurst_exponent(prices: Sequence[float], min_lag: int = 2, max_lag: int = 40) -> float | None:
    """Generalized Hurst exponent (q=2) of a price series via the structure function.

    E[|log P(t+τ) − log P(t)|] ∝ τ^H, so H is the slope of log(mean|Δ|) vs log(τ).
    H < 0.5 ⇒ mean-reverting, H ≈ 0.5 ⇒ random walk, H > 0.5 ⇒ trending. Returns None
    when there isn't enough clean data.
    """
    p = np.asarray(prices, dtype=float)
    p = p[np.isfinite(p) & (p > 0)]
    n = len(p)
    if n < min_lag * 4 + 4:
        return None
    y = np.log(p)
    max_lag = max(min_lag + 2, min(max_lag, n // 2))
    lags = np.arange(min_lag, max_lag)
    tau = []
    good_lags = []
    for lag in lags:
        d = y[lag:] - y[:-lag]
        m = np.mean(np.abs(d))
        if m > 0:
            tau.append(m)
            good_lags.append(lag)
    if len(good_lags) < 3:
        return None
    slope = np.polyfit(np.log(good_lags), np.log(tau), 1)[0]
    return float(slope)


# --------------------------------------------------------------------------- #
#  Mean-reversion speed: Ornstein-Uhlenbeck half-life (Chan Ch.2)
# --------------------------------------------------------------------------- #
def half_life(prices: Sequence[float]) -> float | None:
    """Half-life of mean reversion. Regress Δy(t) on y(t−1); slope λ gives
    half_life = −ln(2)/λ. Returns None if the series isn't mean-reverting (λ ≥ 0)."""
    y = np.asarray(prices, dtype=float)
    y = y[np.isfinite(y)]
    if len(y) < 20:
        return None
    y_lag = y[:-1]
    dy = np.diff(y)
    # slope of dy = λ·y_lag + c
    lam = np.polyfit(y_lag, dy, 1)[0]
    if lam >= 0 or not math.isfinite(lam):
        return None
    hl = -math.log(2) / lam
    if not math.isfinite(hl) or hl <= 0:
        return None
    return float(hl)


# --------------------------------------------------------------------------- #
#  Stretch: rolling z-score (Chan linear / Bollinger mean reversion)
# --------------------------------------------------------------------------- #
def zscore(prices: Sequence[float], lookback: int) -> float | None:
    """z = (price − rolling_mean) / rolling_std over the last `lookback` closes.
    Positive ⇒ stretched above the mean (fade = SELL); negative ⇒ below (fade = BUY)."""
    p = np.asarray(prices, dtype=float)
    p = p[np.isfinite(p)]
    lookback = max(5, int(lookback))
    if len(p) < lookback:
        return None
    window = p[-lookback:]
    mu = float(np.mean(window))
    sd = float(np.std(window, ddof=1))
    if sd <= 0:
        return None
    return (float(p[-1]) - mu) / sd


def closes(candles: Sequence[dict]) -> np.ndarray:
    """Public helper: extract the close array from candle dicts."""
    return _closes(candles)


def n_bar_return(candles: Sequence[dict], lookback: int) -> float | None:
    """Simple N-bar return (Chan time-series momentum). None if too short."""
    p = _closes(candles)
    if len(p) < lookback + 1 or p[-lookback - 1] <= 0:
        return None
    return float(p[-1] / p[-lookback - 1] - 1.0)


def donchian(candles: Sequence[dict], lookback: int) -> tuple[float, float] | None:
    """(highest high, lowest low) over the last `lookback` *closed* bars (excludes the
    current forming bar). Used for breakout momentum."""
    if len(candles) < lookback + 1:
        return None
    window = candles[-lookback - 1:-1]
    hi = max(float(c["high"]) for c in window)
    lo = min(float(c["low"]) for c in window)
    return hi, lo


# --------------------------------------------------------------------------- #
#  Sizing: Kelly (Chan Ch.8) + AFML bet-sizing from probability (Ch.10.3)
# --------------------------------------------------------------------------- #
def kelly_fraction(r_multiples: Sequence[float]) -> float | None:
    """Kelly optimal-leverage f = μ/σ² measured on the realized per-trade R distribution
    (Chan Ch.8, Gaussian form). Returns None with too few samples. Caller applies the
    half-Kelly haircut and clamps — this is the raw, un-haircut number."""
    r = np.asarray([x for x in r_multiples if math.isfinite(x)], dtype=float)
    if len(r) < 10:
        return None
    mu = float(np.mean(r))
    var = float(np.var(r, ddof=1))
    if var <= 0:
        return None
    return mu / var


def _norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def bet_size_from_prob(p: float) -> float:
    """AFML Ch.10.3: turn a predicted win-probability into a bet size in [0, 1].
    z = (p − 0.5)/√(p(1−p)); size m = 2·Φ(z) − 1. p ≤ 0.5 ⇒ 0 (no edge, don't bet)."""
    p = min(0.999, max(0.001, float(p)))
    if p <= 0.5:
        return 0.0
    z = (p - 0.5) / math.sqrt(p * (1.0 - p))
    return max(0.0, min(1.0, 2.0 * _norm_cdf(z) - 1.0))
