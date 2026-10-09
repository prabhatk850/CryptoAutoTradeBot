"""Strategy registry: each strategy casts BUY/SELL/NEUTRAL; `evaluate` aggregates weighted votes.

To add one: add a `StrategyId`, write a `@register` function, add its id to the STRATEGIES setting.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional


class Action(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"
    NEUTRAL = "NEUTRAL"


class StrategyId(str, Enum):
    EMA_CROSS = "EMA_CROSS"
    RSI = "RSI"
    BREAKOUT = "BREAKOUT"
    SUPERTREND_AI = "SUPERTREND_AI"
    TRENDLINE_NAV = "TRENDLINE_NAV"
    FVG = "FVG"
    IFVG = "IFVG"
    SMC = "SMC"
    MACD = "MACD"
    VOL_SQUEEZE_BREAKOUT = "VOL_SQUEEZE_BREAKOUT"
    DIVERGENCE = "DIVERGENCE"
    FUNDING_BIAS = "FUNDING_BIAS"                  # shadow-only until promoted
    ORDERBOOK_IMBALANCE = "ORDERBOOK_IMBALANCE"    # shadow-only until promoted


@dataclass
class Signal:
    action: Action
    reason: str = ""
    strength: float = 1.0                          # conviction; only SMC sets it (read by agents.SMCAgent)


@dataclass
class StrategyContext:
    """Everything a strategy may read, computed once per tick."""
    candles: list
    ind: dict
    supertrend: Optional[dict] = None
    trendline: Optional[dict] = None
    fvg: Optional[dict] = None
    ifvg: Optional[dict] = None
    extra: dict = field(default_factory=dict)      # smc, smc_trend, divergence, funding, orderbook_imbalance


StrategyFn = Callable[[StrategyContext], Signal]
REGISTRY: dict[StrategyId, StrategyFn] = {}
LABELS: dict[StrategyId, str] = {
    StrategyId.EMA_CROSS: "EMA 9/21 Crossover",
    StrategyId.RSI: "RSI Reversal",
    StrategyId.BREAKOUT: "Trendline Breakout (basic)",
    StrategyId.SUPERTREND_AI: "SuperTrend AI (LuxAlgo)",
    StrategyId.TRENDLINE_NAV: "Trendline Breakout Navigator (LuxAlgo)",
    StrategyId.FVG: "Fair Value Gap (LuxAlgo)",
    StrategyId.IFVG: "Inversion Fair Value Gap (LuxAlgo)",
    StrategyId.SMC: "Smart Money Concepts (structure/liquidity/OB)",
    StrategyId.MACD: "MACD (12/26/9)",
    StrategyId.VOL_SQUEEZE_BREAKOUT: "Volatility Squeeze Breakout (BB/KC)",
    StrategyId.DIVERGENCE: "RSI/Price Divergence",
    StrategyId.FUNDING_BIAS: "Funding Rate Bias (contrarian, shadow)",
    StrategyId.ORDERBOOK_IMBALANCE: "Order-Book Imbalance (shadow)",
}
NEUTRAL = Signal(Action.NEUTRAL)


def register(sid: StrategyId):
    def deco(fn: StrategyFn) -> StrategyFn:
        REGISTRY[sid] = fn
        return fn
    return deco


def _side(direction, buy_reason: str, sell_reason: str, buy="long", sell="short") -> Signal:
    """Map a direction value to a BUY/SELL signal (NEUTRAL otherwise)."""
    if direction == buy:
        return Signal(Action.BUY, buy_reason)
    if direction == sell:
        return Signal(Action.SELL, sell_reason)
    return NEUTRAL


@register(StrategyId.EMA_CROSS)
def _ema_cross(ctx: StrategyContext) -> Signal:
    fast, slow = ctx.ind.get("ema_fast"), ctx.ind.get("ema_slow")
    if fast is None or slow is None or fast == slow:
        return NEUTRAL
    crossed = ctx.ind.get("ema_signal")
    if fast > slow:
        return Signal(Action.BUY, "EMA9 above EMA21 (uptrend)" + (" — fresh cross" if crossed == "BULLISH_CROSS" else ""))
    return Signal(Action.SELL, "EMA9 below EMA21 (downtrend)" + (" — fresh cross" if crossed == "BEARISH_CROSS" else ""))


@register(StrategyId.RSI)
def _rsi(ctx: StrategyContext) -> Signal:
    rsi = ctx.ind.get("rsi", 0)
    return _side(ctx.ind.get("rsi_signal"), f"RSI {rsi:.1f} oversold — bounce expected",
                 f"RSI {rsi:.1f} overbought — pullback expected", "OVERSOLD", "OVERBOUGHT")


@register(StrategyId.BREAKOUT)
def _breakout(ctx: StrategyContext) -> Signal:
    s, lvl = ctx.ind.get("breakout_signal"), ctx.ind.get("breakout_level")
    if s not in ("BREAKOUT_UP", "BREAKOUT_DOWN"):
        return NEUTRAL
    return _side(s, f"price broke above resistance {lvl:.0f}", f"price broke below support {lvl:.0f}",
                 "BREAKOUT_UP", "BREAKOUT_DOWN")


@register(StrategyId.SUPERTREND_AI)
def _supertrend_ai(ctx: StrategyContext) -> Signal:
    latest = (ctx.supertrend or {}).get("latest", {})
    strength = latest.get("strength", 0)
    fresh = " — fresh flip" if latest.get("flip") else ""
    return _side(latest.get("dir"), f"SuperTrend AI bullish (strength {strength}/10){fresh}",
                 f"SuperTrend AI bearish (strength {strength}/10){fresh}")


@register(StrategyId.TRENDLINE_NAV)
def _trendline_nav(ctx: StrategyContext) -> Signal:
    latest = (ctx.trendline or {}).get("latest", {})
    fresh = " — fresh break" if latest.get("flip") else ""
    return _side(latest.get("trend"), f"Trendline Navigator bullish{fresh}",
                 f"Trendline Navigator bearish{fresh}", 1, -1)


@register(StrategyId.FVG)
def _fvg(ctx: StrategyContext) -> Signal:
    latest = (ctx.fvg or {}).get("latest", {})
    if not latest.get("new"):
        return NEUTRAL
    return _side(latest.get("dir"), "Bullish Fair Value Gap formed (imbalance up)",
                 "Bearish Fair Value Gap formed (imbalance down)")


@register(StrategyId.IFVG)
def _ifvg(ctx: StrategyContext) -> Signal:
    latest = (ctx.ifvg or {}).get("latest", {})
    if not latest.get("new"):
        return NEUTRAL
    return _side(latest.get("dir"), "Inversion FVG reclaimed as support (bullish)",
                 "Inversion FVG rejected as resistance (bearish)")


@register(StrategyId.MACD)
def _macd(ctx: StrategyContext) -> Signal:
    """Histogram sign votes; reason notes a fresh cross and the zero-line side."""
    sig, macd = ctx.ind.get("macd_signal"), ctx.ind.get("macd", 0.0)
    if sig in ("BULLISH_CROSS", "BULLISH"):
        return Signal(Action.BUY, f"MACD {'bullish cross' if sig == 'BULLISH_CROSS' else 'histogram positive'}"
                      + (" above zero" if macd > 0 else ""))
    if sig in ("BEARISH_CROSS", "BEARISH"):
        return Signal(Action.SELL, f"MACD {'bearish cross' if sig == 'BEARISH_CROSS' else 'histogram negative'}"
                      + (" below zero" if macd < 0 else ""))
    return NEUTRAL


@register(StrategyId.SMC)
def _smc(ctx: StrategyContext) -> Signal:
    """Score structure + sweep + premium/discount + unmitigated OB; needs 2+ points and no opposing 1h trend."""
    s = ctx.extra.get("smc")
    if not s:
        return NEUTRAL
    htf = (ctx.extra.get("smc_trend") or {}).get("trend")
    sb = s.get("structure_break") or {}
    sweep = s.get("recent_sweep") or {}
    zone = (s.get("premium_discount") or {}).get("zone")
    obs = s.get("order_blocks") or {}

    bull = bear = 0
    reasons = []
    if s.get("trend") == "bullish": bull += 1
    if s.get("trend") == "bearish": bear += 1
    if sb.get("dir") == "bullish": bull += 1; reasons.append(f"{sb.get('type')}↑")
    if sb.get("dir") == "bearish": bear += 1; reasons.append(f"{sb.get('type')}↓")
    if sweep.get("dir") == "bullish": bull += 1; reasons.append("sell-side sweep")
    if sweep.get("dir") == "bearish": bear += 1; reasons.append("buy-side sweep")
    if zone == "discount": bull += 1
    if zone == "premium": bear += 1
    if any(not o.get("mitigated") for o in obs.get("bullish", [])): bull += 1; reasons.append("bull OB")
    if any(not o.get("mitigated") for o in obs.get("bearish", [])): bear += 1; reasons.append("bear OB")

    if bull >= 2 and bull > bear and htf != "bearish":
        return Signal(Action.BUY, "SMC bullish: " + ", ".join(reasons), min(1.6, 1.0 + 0.15 * bull))
    if bear >= 2 and bear > bull and htf != "bullish":
        return Signal(Action.SELL, "SMC bearish: " + ", ".join(reasons), min(1.6, 1.0 + 0.15 * bear))
    return NEUTRAL


@register(StrategyId.VOL_SQUEEZE_BREAKOUT)
def _vol_squeeze_breakout(ctx: StrategyContext) -> Signal:
    adx = ctx.ind.get("adx") or 0
    return _side(ctx.ind.get("squeeze_signal"), f"Volatility squeeze released up (ADX {adx:.0f})",
                 f"Volatility squeeze released down (ADX {adx:.0f})", "RELEASE_UP", "RELEASE_DOWN")


@register(StrategyId.DIVERGENCE)
def _divergence(ctx: StrategyContext) -> Signal:
    """Only a fresh divergence votes (regular = reversal, hidden = continuation)."""
    latest = (ctx.extra.get("divergence") or {}).get("latest")
    if not latest or not latest.get("fresh"):
        return NEUTRAL
    label = f"{latest.get('kind')} {latest.get('dir')} RSI divergence"
    return _side(latest.get("dir"), label, label, "bullish", "bearish")


@register(StrategyId.FUNDING_BIAS)
def _funding_bias(ctx: StrategyContext) -> Signal:
    """Contrarian fade of extreme funding."""
    f = ctx.extra.get("funding") or {}
    rate = f.get("funding_rate")
    return _side(f.get("extreme"), f"Funding extremely low ({rate}) — crowded shorts, fade",
                 f"Funding extremely high ({rate}) — crowded longs, fade", "low", "high")


@register(StrategyId.ORDERBOOK_IMBALANCE)
def _orderbook_imbalance(ctx: StrategyContext) -> Signal:
    ob = ctx.extra.get("orderbook_imbalance") or {}
    imb = ob.get("imb") or 0.0
    return _side(ob.get("signal"), f"Order book bid-heavy (imbalance {imb:+.2f})",
                 f"Order book ask-heavy (imbalance {imb:+.2f})", "BUY", "SELL")


def parse_enabled(csv: str) -> list[StrategyId]:
    out = []
    for tok in (csv or "").split(","):
        try:
            out.append(StrategyId(tok.strip().upper()))
        except ValueError:
            pass
    return out or [StrategyId.EMA_CROSS, StrategyId.RSI, StrategyId.BREAKOUT]


def _run(sid: StrategyId, ctx: StrategyContext) -> Signal:
    try:
        return REGISTRY[sid](ctx)
    except Exception as e:
        return Signal(Action.NEUTRAL, f"error: {e}")


def evaluate(ctx: StrategyContext, enabled: list[StrategyId], min_signals: int,
             weights: dict | None = None, shadow: list[StrategyId] | None = None) -> dict:
    """Needs `min_signals` agreeing strategies; the weighted score picks the side. Shadow votes never count."""
    weights = weights or {}
    shadow_votes = {sid.value: _run(sid, ctx).action.value
                    for sid in (shadow or []) if sid not in enabled and sid in REGISTRY}
    votes: dict[str, str] = {}
    reasons: list[str] = []
    buy = sell = 0
    buy_w = sell_w = 0.0
    for sid in enabled:
        if sid not in REGISTRY:
            continue
        sig = _run(sid, ctx)
        votes[sid.value] = sig.action.value
        wt = weights.get(sid.value, 1.0)
        if sig.action == Action.BUY:
            buy, buy_w = buy + 1, buy_w + wt
        elif sig.action == Action.SELL:
            sell, sell_w = sell + 1, sell_w + wt
        else:
            continue
        reasons.append(f"[{sid.value}×{wt:g}] {sig.reason}")

    threshold = max(1, min(min_signals, len(enabled)))
    if buy >= threshold and buy_w > sell_w:
        action = Action.BUY
    elif sell >= threshold and sell_w > buy_w:
        action = Action.SELL
    else:
        action = Action.HOLD
    return {
        "action": action.value,
        "reason": " | ".join(reasons) or f"No strong signal (buy={buy}, sell={sell}, need {threshold})",
        "votes": votes,
        "shadow_votes": shadow_votes,
        "buy_score": buy,
        "sell_score": sell,
        "buy_w": round(buy_w, 2),
        "sell_w": round(sell_w, 2),
        "threshold": threshold,
    }


def available() -> list[dict]:
    return [{"id": sid.value, "label": LABELS.get(sid, sid.value)} for sid in StrategyId if sid in REGISTRY]
