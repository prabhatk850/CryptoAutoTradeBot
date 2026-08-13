"""
Strategy engine: combines indicator signals into a single BUY / SELL / HOLD decision.

Rules (all 3 must agree for a trade to fire):
  BUY  when: EMA bullish cross  AND  RSI oversold or neutral  AND  (breakout up OR neutral)
  SELL when: EMA bearish cross  AND  RSI overbought or neutral  AND  (breakout down OR neutral)
  HOLD otherwise

You can tune these rules without touching anything else.
"""
from bot.indicators import IndicatorResult


def decide(ind: IndicatorResult, min_signals: int = 2) -> tuple[str, str]:
    """
    Returns (action, reason) where action is 'BUY' | 'SELL' | 'HOLD'.
    reason is a human-readable string logged to MongoDB.

    min_signals controls risk: how many of the 3 signals must agree.
      2 = balanced (default), 1 = riskier/more trades, 3 = conservative.
    """
    reasons = []

    # --- BUY conditions ---
    buy_score = 0
    if ind["ema_signal"] == "BULLISH_CROSS":
        buy_score += 1
        reasons.append(f"EMA{9} crossed above EMA{21}")
    if ind["rsi_signal"] == "OVERSOLD":
        buy_score += 1
        reasons.append(f"RSI={ind['rsi']:.1f} oversold (<30)")
    if ind["breakout_signal"] == "BREAKOUT_UP":
        buy_score += 1
        reasons.append(f"Price broke above resistance {ind['breakout_level']:.2f}")

    # --- SELL conditions ---
    sell_score = 0
    if ind["ema_signal"] == "BEARISH_CROSS":
        sell_score += 1
        reasons.append(f"EMA{9} crossed below EMA{21}")
    if ind["rsi_signal"] == "OVERBOUGHT":
        sell_score += 1
        reasons.append(f"RSI={ind['rsi']:.1f} overbought (>70)")
    if ind["breakout_signal"] == "BREAKOUT_DOWN":
        sell_score += 1
        reasons.append(f"Price broke below support {ind['breakout_level']:.2f}")

    # Need at least `min_signals` out of 3 signals to agree
    threshold = max(1, min(min_signals, 3))
    if buy_score >= threshold and buy_score > sell_score:
        return "BUY", " | ".join(reasons)
    if sell_score >= threshold and sell_score > buy_score:
        return "SELL", " | ".join(reasons)

    return "HOLD", f"No strong signal (buy_score={buy_score}, sell_score={sell_score}, need {threshold})"
