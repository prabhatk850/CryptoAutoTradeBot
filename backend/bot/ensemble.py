"""
Meta-labeling learning ensemble (see .claude/skills/trading-books).

The agents (agents.py) are AFML "primary models": they set the SIDE. This module is
the "secondary model": it weights each agent by its LIVE win-rate (a Beta posterior
that keeps updating as trades close), decides whether the bet is worth taking
(meta-label take/skip), and sizes it from the predicted probability (AFML Ch.10.3)
capped by half-Kelly (Chan Ch.8).

Continuous learning: every closed trade calls record_outcome(), which pushes the
trade's R-multiple into each participating agent's posterior. Agents that make money
gain reliability (vote louder, size bigger); losers shrink toward — but never below —
a floor. Persisted in Mongo `agent_perf`; degrades gracefully if Mongo is down.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from bot import agents as agents_mod, quant
from bot.agents import AgentContext
from config import settings
from db import db

logger = logging.getLogger("bot.ensemble")

# Beta(prior, prior) prior on each agent's win-probability, from config. prior=1 ⇒
# uniform, so a brand-new agent starts at 0.5 and only moves as its own trades resolve.
_perf_cache: dict = {"ts": 0.0, "p": {}}


# --------------------------------------------------------------------------- #
#  Learned performance (Beta posterior) — persisted, cached ~30s
# --------------------------------------------------------------------------- #
async def get_perf(force: bool = False) -> dict:
    now = time.time()
    if not force and now - _perf_cache["ts"] < 30 and _perf_cache["p"]:
        return _perf_cache["p"]
    out: dict = {}
    try:
        async for d in db.agent_perf.find({}):
            c = int(d.get("count", 0))
            wins = int(d.get("wins", 0))
            out[d["_id"]] = {
                "count": c,
                "wins": wins,
                "sum_r": round(float(d.get("sum_r", 0.0)), 2),
                "win_rate": round(wins / c * 100, 1) if c else 0.0,
                "expectancy": round(float(d.get("sum_r", 0.0)) / c, 3) if c else 0.0,
                "reliability": round((settings.agents_beta_prior + wins)
                                     / (2 * settings.agents_beta_prior + c), 3),
                "r_hist": list(d.get("r_hist", [])),
            }
    except Exception as e:
        logger.error(f"agent_perf read failed ({e}) — using neutral reliability.")
        return _perf_cache["p"] or {}
    _perf_cache.update(ts=now, p=out)
    return out


def _reliability(perf: dict, agent_id: str) -> float:
    p = perf.get(agent_id)
    if not p:
        return 0.5  # neutral prior mean for an unseen agent (Beta(prior,prior))
    return float(p["reliability"])


# --------------------------------------------------------------------------- #
#  Decision: pool agent proposals → take/skip + side + size (meta-labeling)
# --------------------------------------------------------------------------- #
async def decide(ctx: AgentContext) -> dict:
    """Return the ensemble's decision for one symbol/tick:
      {action, confidence, size_mult, agents, proposals, sl_hint, reason, learned}
    action == "HOLD" means the ensemble abstains → scheduler falls back to the LLM brain.
    """
    if not settings.agents_enabled:
        return {"action": "HOLD", "confidence": 0.0, "size_mult": 1.0, "agents": [],
                "proposals": {}, "sl_hint": None, "reason": "agents disabled", "learned": False}

    perf = await get_perf()
    enabled = agents_mod.parse_enabled(settings.agents)

    sides = {"BUY": [], "SELL": []}
    proposals: dict = {}
    for agent in enabled:
        try:
            prop = agent.propose(ctx)
        except Exception as e:
            logger.error(f"agent {agent.id} propose error: {e}")
            prop = None
        if prop is None or prop.side not in sides:
            continue
        rel = _reliability(perf, agent.id)
        sides[prop.side].append((agent.id, prop, rel))
        proposals[agent.id] = {"side": prop.side, "confidence": round(prop.confidence, 2),
                               "reliability": round(rel, 3), "reason": prop.reason}

    def pooled(side: str) -> float:
        # confidence-weighted reliability mass backing a side
        return sum(rel * prop.confidence for _, prop, rel in sides[side])

    buy_pool, sell_pool = pooled("BUY"), pooled("SELL")
    if buy_pool <= 0 and sell_pool <= 0:
        return {"action": "HOLD", "confidence": 0.0, "size_mult": 1.0, "agents": [],
                "proposals": proposals, "sl_hint": None,
                "reason": "no agent took a side", "learned": True}

    win_side = "BUY" if buy_pool >= sell_pool else "SELL"
    win, opp = sides[win_side], sides["SELL" if win_side == "BUY" else "BUY"]
    win_pool = buy_pool if win_side == "BUY" else sell_pool
    opp_pool = sell_pool if win_side == "BUY" else buy_pool

    # Meta-probability. Cold-start matters: a brand-new agent (reliability 0.5) must be
    # able to trade on the strength of its OWN book signal, not sit forever below the
    # take-threshold. So start from the winning side's raw conviction and let the LEARNED
    # reliability EDGE (reliability − 0.5) shift it: a proven agent is amplified upward, a
    # proven-bad one is pushed below the threshold (suppressed). Agreement adds, opposition
    # subtracts. This is what makes the local agents actually drive — and keep improving.
    n = len(win)
    mean_conf = sum(prop.confidence for _, prop, _ in win) / n
    mean_rel = sum(rel for _, _, rel in win) / n
    p = mean_conf + (mean_rel - 0.5)
    p += settings.agents_agreement_bonus * max(0, n - 1)
    if win_pool > 0:
        p -= settings.agents_opposition_penalty * (opp_pool / win_pool)
    p = max(0.0, min(0.999, p))

    win_ids = [aid for aid, _, _ in win]
    # structural stop hint = the most protective hint among winning agents
    sl_hints = [prop.sl_hint for _, prop, _ in win if prop.sl_hint is not None]
    sl_hint = None
    if sl_hints:
        sl_hint = min(sl_hints) if win_side == "BUY" else max(sl_hints)

    reason = (f"agents → {win_side} p={p:.0%} "
              f"[{', '.join(f'{aid}×{rel:.2f}' for aid, _, rel in win)}]"
              + (f" vs opp {opp_pool:.2f}" if opp else ""))

    if p < settings.agents_min_confidence:
        return {"action": "HOLD", "confidence": round(p, 3), "size_mult": 1.0,
                "agents": win_ids, "proposals": proposals, "sl_hint": None,
                "reason": f"below take-threshold ({p:.0%} < {settings.agents_min_confidence:.0%}) → {reason}",
                "learned": True}

    size_mult = _size_multiplier(p, win_ids, perf)
    return {"action": win_side, "confidence": round(p, 3), "size_mult": round(size_mult, 3),
            "agents": win_ids, "proposals": proposals, "sl_hint": sl_hint,
            "reason": reason + f" · size×{size_mult:.2f}", "learned": True}


def _size_multiplier(p: float, win_ids: list[str], perf: dict) -> float:
    """AFML bet-size-from-probability mapped into [size_min, size_max], then capped by
    half-Kelly on the winning agents' pooled R history (Chan Ch.8)."""
    lo, hi = settings.agents_size_min_mult, settings.agents_size_max_mult
    base = quant.bet_size_from_prob(p)          # 0..1
    mult = lo + base * (hi - lo)

    pooled_r: list[float] = []
    total_n = 0
    for aid in win_ids:
        pr = perf.get(aid)
        if pr:
            pooled_r.extend(pr.get("r_hist", []))
            total_n += pr.get("count", 0)
    if total_n >= settings.agents_min_trades:
        k = quant.kelly_fraction(pooled_r)
        if k is not None and k > 0:
            half_kelly = 0.5 * k
            mult = min(mult, max(lo, half_kelly))
    return max(lo, min(hi, mult))


# --------------------------------------------------------------------------- #
#  Continuous learning: update posteriors when a trade closes
# --------------------------------------------------------------------------- #
async def record_outcome(agent_ids: list[str], r_multiple: float):
    """Push a closed trade's R-multiple into every agent that backed the winning side.
    Win = R>0 (triple-barrier collapsed to sign(R), AFML Ch.3). Neutral (R==0) counts
    as a resolved-but-not-win sample so a flat scratch doesn't inflate reliability."""
    if not agent_ids:
        return
    r = float(r_multiple)
    win = 1 if r > 0 else 0
    for aid in agent_ids:
        try:
            await db.agent_perf.update_one(
                {"_id": aid},
                {"$inc": {"count": 1, "sum_r": r, "wins": win},
                 "$push": {"r_hist": {"$each": [round(r, 4)],
                                      "$slice": -settings.agents_r_history}}},
                upsert=True,
            )
        except Exception as e:
            logger.error(f"agent_perf update failed for {aid}: {e}")
    _perf_cache["ts"] = 0.0  # invalidate cache so the next decision sees the update


# --------------------------------------------------------------------------- #
#  Status (for the dashboard / /bot/performance)
# --------------------------------------------------------------------------- #
async def status() -> dict:
    perf = await get_perf(force=True)
    enabled = {a.id for a in agents_mod.parse_enabled(settings.agents)}
    rows = []
    for a in agents_mod.all_agents():
        p = perf.get(a.id) or {}
        rows.append({
            "id": a.id, "label": a.label, "enabled": a.id in enabled,
            "count": p.get("count", 0), "wins": p.get("wins", 0),
            "win_rate": p.get("win_rate", 0.0), "expectancy": p.get("expectancy", 0.0),
            "reliability": p.get("reliability", 0.5),
            "sum_r": p.get("sum_r", 0.0),
        })
    return {
        "enabled": settings.agents_enabled,
        "min_confidence": settings.agents_min_confidence,
        "min_trades_for_kelly": settings.agents_min_trades,
        "agents": rows,
    }
