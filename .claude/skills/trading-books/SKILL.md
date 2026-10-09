---
name: trading-books
description: Book-derived trading strategies and continuous-learning framework that power the bot's in-house trading agents (backend/bot/agents.py + ensemble.py). Extracted from the PDFs in C:/Users/varun/Desktop/kitaabe. Read this when working on the agents, the learning ensemble, or adding a new book strategy.
---

# Trading Books → In-House Agents

The bot no longer depends on an LLM to decide trades. A set of **self-contained trading
agents** — each embodying a strategy from a real trading book — make the decisions, and a
**continuous-learning ensemble** scores them on the outcomes of their own trades so the mix
keeps improving. The LLM brain (Claude/Gemini) is kept only as a **fallback** for when no
agent has a confident read.

Source PDFs: `C:/Users/varun/Desktop/kitaabe/kitaabe/`.

## The books and what we take from each

### Ernest Chan — *Algorithmic Trading: Winning Strategies and Their Rationale*
The concrete entry logic for two of the agents.
- **Ch. 2–5 Mean Reversion** → `MeanReversionAgent`
  - **Hurst exponent** `H`: `H < 0.5` mean-reverting, `H ≈ 0.5` random walk, `H > 0.5`
    trending. Estimated from the structure function of log-prices (generalized Hurst, q=2 —
    same idea as Chan's `genhurst`). Only fade when the regime is actually mean-reverting.
  - **Ornstein–Uhlenbeck half-life**: regress `Δy(t)` on `y(t−1)`; slope `λ` gives
    `half_life = −ln(2)/λ`. A short half-life ⇒ a good mean-reversion candidate; it also sets
    the natural lookback for the z-score.
  - **Linear / Bollinger entry**: trade units proportional to the **negative z-score** — buy
    when price is stretched below the mean, sell when stretched above.
- **Ch. 6–7 Momentum** → `TrendMomentumAgent`
  - **Time-series momentum**: sign of the N-bar return; **Donchian breakout** of the
    N-bar high/low. Only trend-follow when `H > 0.5` (trending regime) and aligned with the
    1h bias.
- **Ch. 8 Risk Management** → position sizing
  - **Kelly optimal leverage** `f = μ/σ²` from the realized per-trade R distribution. We use
    **half-Kelly** (Chan's own practice) and treat it as an *upper bound* size multiplier.

### Marcos López de Prado — *Advances in Financial Machine Learning*
The continuous-learning framework (the ensemble).
- **Ch. 3.4 Triple-barrier labeling**: a trade is labeled by which barrier it hits first —
  take-profit (+1 win), stop-loss (−1 loss), or the time/vertical barrier (0 neutral). We
  collapse this to `sign(R)` of the closed trade's realized R-multiple.
- **Ch. 3.6 Meta-labeling**: the *primary* models set the **side** (long/short); a *secondary*
  model decides **whether to take the bet** and, via its confidence, **how big**. Our agents
  are the primary models; the ensemble is the secondary meta-model.
- **Ch. 10.3 Bet sizing from predicted probability**: with predicted probability `p` of the
  side being right, `z = (p−0.5)/√(p(1−p))`, size `m = 2·Φ(z) − 1 ∈ [0,1]`.

### Supporting books (context, not yet distinct agents)
- **Halls-Moore — *Successful Algorithmic Trading***: Sharpe, walk-forward, risk of ruin —
  the discipline behind the metrics we persist per agent.
- **Larry Harris — *Trading and Exchanges***: microstructure — spreads/liquidity. Reflected
  in the existing pre-entry liquidity gates (`max_entry_spread_pct`, `max_exit_slippage_pct`).
- **Hull / Natenberg / Bennett — options & volatility**: volatility-regime intuition; only
  used indirectly (ATR-based stops, regime gating). No options agent yet.

## How the pieces fit in the codebase
- `backend/bot/quant.py` — pure math: Hurst, O-U half-life, z-score, Kelly, bet-size-from-p.
- `backend/bot/agents.py` — the `Agent` base class + book agents; each returns a `Proposal`
  (side, confidence, reason, optional SL hint).
- `backend/bot/ensemble.py` — the meta-labeling learning layer: pools proposals by each
  agent's learned win-probability (Beta posterior), decides take/skip/size, and records
  outcomes on trade close (persisted in Mongo `agent_perf`).
- `backend/bot/scheduler.py` — calls the ensemble as the **primary** decision-maker; the LLM
  brain runs only when the ensemble abstains.

## Adding a new book strategy (2 steps)
1. Add an `Agent` subclass in `agents.py` implementing `propose(ctx) -> Proposal | None`, and
   register it in `agents.build_default()`.
2. Add its id to the `AGENTS` setting. It starts at neutral weight and **learns its own
   reliability** from live trades — no other wiring needed.
