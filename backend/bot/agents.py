"""
In-house trading agents (see .claude/skills/trading-books).

Each agent embodies ONE strategy from a real trading book and, given a market
snapshot, either proposes a side (BUY/SELL) with a raw conviction or abstains
(returns None). Agents are the "primary models" in the AFML meta-labeling scheme:
they set the SIDE. The learning ensemble (ensemble.py) is the secondary model that
weights them by their live track record and decides whether/how big to bet.

Agents are intentionally stateless and I/O-free — all learning lives in the ensemble.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from bot import quant, strategies
from bot.strategies import Action, StrategyContext, StrategyId
from config import settings


@dataclass
class Proposal:
    side: str                      # "BUY" or "SELL" (abstain by returning None)
    confidence: float              # raw signal conviction in [0, 1] (pre-learning)
    reason: str = ""
    sl_hint: Optional[float] = None  # optional structural stop; caller clamps to the band


@dataclass
class AgentContext:
    """Everything the agents need for one symbol on one tick. Assembled in scheduler."""
    symbol: str
    price: float
    c_entry: list                  # decision-timeframe candles (15m)
    c_trend: list                  # bias-timeframe candles (1h)
    ind: dict
    supertrend: Optional[dict] = None
    trendline: Optional[dict] = None
    fvg: Optional[dict] = None
    ifvg: Optional[dict] = None
    smc: Optional[dict] = None
    smc_trend: Optional[dict] = None
    bias: int = 0                  # 1 bullish / -1 bearish / 0 neutral (1h)
    confluence: dict = field(default_factory=dict)  # strategies.evaluate() result


def _clip(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _conf(strength: float) -> float:
    """Map a 0..1 signal strength into the configured raw-confidence band. Keeps every
    agent's confidence scale in one place (config) — no per-agent magic numbers."""
    lo, hi = settings.agent_conf_min, settings.agent_conf_max
    return lo + (hi - lo) * _clip(strength, 0.0, 1.0)


class Agent:
    """Base agent. Subclasses implement propose()."""
    id: str = "BASE"
    label: str = "Base Agent"

    def propose(self, ctx: AgentContext) -> Optional[Proposal]:  # pragma: no cover - abstract
        raise NotImplementedError


# --------------------------------------------------------------------------- #
#  Chan Ch.6–7 — Momentum / trend-following
# --------------------------------------------------------------------------- #
class TrendMomentumAgent(Agent):
    id = "MOMENTUM"
    label = "Trend Momentum (Chan Ch.6–7)"

    def propose(self, ctx: AgentContext) -> Optional[Proposal]:
        p = quant.closes(ctx.c_entry)
        if len(p) < settings.agent_mom_lookback + 5:
            return None
        h = quant.hurst_exponent(p)
        # Only trend-follow in a trending (or at least not clearly mean-reverting) regime.
        if h is not None and h < settings.agent_hurst_trend_min:
            return None
        ret = quant.n_bar_return(ctx.c_entry, settings.agent_mom_lookback)
        don = quant.donchian(ctx.c_entry, settings.agent_mom_lookback)
        if ret is None or don is None:
            return None
        hi, lo = don
        price = ctx.price
        breakout_up = price > hi
        breakout_down = price < lo
        mom_str = _clip(abs(ret) / settings.agent_mom_full_return, 0.0, 1.0)
        hurst_txt = f"H={h:.2f}" if h is not None else "H=?"
        # composite strength: a confirmed breakout weighs more than raw drift magnitude
        def strength(breakout: bool) -> float:
            return 0.6 * (1.0 if breakout else 0.0) + 0.4 * mom_str

        if ret > 0 and (breakout_up or ctx.bias >= 0):
            why = f"{settings.agent_mom_lookback}-bar momentum +{ret*100:.1f}% {hurst_txt}" + (
                " · Donchian breakout up" if breakout_up else "")
            return Proposal("BUY", _conf(strength(breakout_up)), why)
        if ret < 0 and (breakout_down or ctx.bias <= 0):
            why = f"{settings.agent_mom_lookback}-bar momentum {ret*100:.1f}% {hurst_txt}" + (
                " · Donchian breakout down" if breakout_down else "")
            return Proposal("SELL", _conf(strength(breakout_down)), why)
        return None


# --------------------------------------------------------------------------- #
#  Chan Ch.2–5 — Mean reversion (Hurst + O-U half-life gated, z-score entry)
# --------------------------------------------------------------------------- #
class MeanReversionAgent(Agent):
    id = "MEAN_REVERSION"
    label = "Mean Reversion (Chan Ch.2–5)"

    def propose(self, ctx: AgentContext) -> Optional[Proposal]:
        p = quant.closes(ctx.c_entry)
        if len(p) < settings.agent_mr_min_bars:
            return None
        h = quant.hurst_exponent(p)
        # Require a mean-reverting regime (H < threshold) — never fade a trend.
        if h is None or h > settings.agent_hurst_mr_max:
            return None
        hl = quant.half_life(p)
        if hl is None:
            return None  # not an O-U mean-reverting series
        lookback = int(_clip(round(hl), settings.agent_mr_min_lookback, settings.agent_mr_max_lookback))
        z = quant.zscore(p, lookback)
        if z is None:
            return None
        z_entry = settings.agent_mr_z_entry
        stretch = _clip((abs(z) - z_entry) / max(z_entry, 0.5), 0.0, 1.0)
        conf = _conf(stretch)
        window = ctx.c_entry[-lookback:]

        if z <= -z_entry and ctx.bias >= 0:
            # fade the dip; stop below the window low
            sl = min(float(c["low"]) for c in window)
            return Proposal("BUY", conf,
                            f"z={z:.2f} below mean · H={h:.2f} MR regime · half-life {hl:.0f} bars",
                            sl_hint=sl)
        if z >= z_entry and ctx.bias <= 0:
            sl = max(float(c["high"]) for c in window)
            return Proposal("SELL", conf,
                            f"z={z:.2f} above mean · H={h:.2f} MR regime · half-life {hl:.0f} bars",
                            sl_hint=sl)
        return None


# --------------------------------------------------------------------------- #
#  Smart Money Concepts — reuses the deterministic SMC read as a standalone agent
# --------------------------------------------------------------------------- #
class SMCAgent(Agent):
    id = "SMC_AGENT"
    label = "Smart Money Concepts"

    def propose(self, ctx: AgentContext) -> Optional[Proposal]:
        fn = strategies.REGISTRY.get(StrategyId.SMC)
        if not fn:
            return None
        sctx = StrategyContext(candles=ctx.c_entry, ind=ctx.ind, supertrend=ctx.supertrend,
                               trendline=ctx.trendline, fvg=ctx.fvg, ifvg=ctx.ifvg,
                               extra={"smc": ctx.smc, "smc_trend": ctx.smc_trend})
        sig = fn(sctx)
        if sig.action not in (Action.BUY, Action.SELL):
            return None
        # SMC strategy strength runs ~1.0–1.6 (see strategies._smc) → normalize to 0..1
        strength01 = _clip((sig.strength - 1.0) / 0.6, 0.0, 1.0)
        return Proposal(sig.action.value, _conf(strength01), sig.reason)


# --------------------------------------------------------------------------- #
#  Confluence — wraps the existing multi-indicator vote engine as one agent
# --------------------------------------------------------------------------- #
class ConfluenceAgent(Agent):
    id = "CONFLUENCE"
    label = "Indicator Confluence (auto-tuned votes)"

    def propose(self, ctx: AgentContext) -> Optional[Proposal]:
        r = ctx.confluence or {}
        action = r.get("action")
        if action not in ("BUY", "SELL"):
            return None
        buy_w, sell_w = float(r.get("buy_w", 0)), float(r.get("sell_w", 0))
        total = buy_w + sell_w
        margin = abs(buy_w - sell_w) / total if total > 0 else 0.0
        return Proposal(action, _conf(margin), r.get("reason", "")[:180])


_ALL: dict[str, Agent] = {
    a.id: a for a in (TrendMomentumAgent(), MeanReversionAgent(), SMCAgent(), ConfluenceAgent())
}


def parse_enabled(csv: str) -> list[Agent]:
    """Enabled agents from the AGENTS setting (falls back to all)."""
    out = []
    for tok in (csv or "").split(","):
        tok = tok.strip().upper()
        if tok in _ALL:
            out.append(_ALL[tok])
    return out or list(_ALL.values())


def all_agents() -> list[Agent]:
    return list(_ALL.values())


def label_for(agent_id: str) -> str:
    a = _ALL.get(agent_id)
    return a.label if a else agent_id
