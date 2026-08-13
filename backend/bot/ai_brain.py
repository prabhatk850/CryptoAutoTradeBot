"""
AI trading brain.

Turns a multi-timeframe market snapshot into a validated trade plan by asking
Claude (via the local Claude Code CLI, authenticated with the user's subscription
— NO Anthropic API key required). The CLI is invoked in headless JSON mode:

    claude -p --output-format json --model <model>   (prompt piped on stdin)

The model returns a structured plan (direction, structure-based stop-loss, TP1/2/3,
confidence, reasoning). All numeric guardrails (risk sizing, min 1:2 R:R, SL clamps)
are enforced by the caller in scheduler.py — the AI proposes, code disposes.
"""
from __future__ import annotations

import glob
import json
import logging
import re
import subprocess
from pathlib import Path
from shutil import which
from typing import Optional

import httpx

from config import settings

logger = logging.getLogger("bot.ai_brain")

_CLI_CACHE: Optional[str] = None


def _ver_key(p: str) -> tuple:
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", p)
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


def find_claude_cli() -> Optional[str]:
    """Locate the Claude Code binary (subscription auth). Cached after first hit."""
    global _CLI_CACHE
    if _CLI_CACHE and Path(_CLI_CACHE).exists():
        return _CLI_CACHE
    if settings.claude_cli_path and Path(settings.claude_cli_path).exists():
        _CLI_CACHE = settings.claude_cli_path
        return _CLI_CACHE

    home = Path.home()
    patterns = [
        # Claude Desktop app (Windows Store package) bundles claude-code/<ver>/claude.exe
        str(home / "AppData/Local/Packages/*/LocalCache/Roaming/Claude/claude-code/*/claude.exe"),
        str(home / "AppData/Roaming/Claude/claude-code/*/claude.exe"),
        # npm global install
        str(home / "AppData/Roaming/npm/claude.cmd"),
        # posix installs
        str(home / ".local/bin/claude"),
        "/usr/local/bin/claude",
    ]
    candidates: list[str] = []
    for pat in patterns:
        candidates += glob.glob(pat)
    w = which("claude")
    if w:
        candidates.append(w)
    candidates = [c for c in candidates if Path(c).exists()]
    if not candidates:
        return None
    candidates.sort(key=_ver_key)  # newest version last
    _CLI_CACHE = candidates[-1]
    logger.info(f"AI brain using Claude CLI: {_CLI_CACHE}")
    return _CLI_CACHE


def _has_api_key() -> bool:
    k = (settings.anthropic_api_key or "").strip()
    return bool(k) and k != "paste-your-key-here"


def _has_gemini_key() -> bool:
    k = (settings.gemini_api_key or "").strip()
    return bool(k) and k != "paste-your-key-here"


def claude_available() -> bool:
    """True if Claude can be reached — direct API key, or the local Claude Code CLI."""
    return _has_api_key() or find_claude_cli() is not None


def available() -> bool:
    return settings.ai_enabled and (_has_gemini_key() or claude_available())


# --------------------------------------------------------------------------- #
#  Snapshot: compress the raw market state into a compact, model-friendly dict
# --------------------------------------------------------------------------- #
def _swings(candles: list[dict], highs: bool, span: int = 2) -> list[dict]:
    """Local swing highs/lows with a `span`-bar window each side."""
    out = []
    n = len(candles)
    for i in range(span, n - span):
        c = candles[i]
        if highs:
            v = c["high"]
            if all(v >= candles[j]["high"] for j in range(i - span, i + span + 1) if j != i) and \
               v > candles[i - 1]["high"]:
                out.append({"time": int(c["time"]), "price": round(v, 2)})
        else:
            v = c["low"]
            if all(v <= candles[j]["low"] for j in range(i - span, i + span + 1) if j != i) and \
               v < candles[i - 1]["low"]:
                out.append({"time": int(c["time"]), "price": round(v, 2)})
    return out


def _fmt_candles(candles: list[dict], n: int) -> list[list]:
    """Last n candles as compact [o,h,l,c] rows (2dp)."""
    return [[round(c["open"], 2), round(c["high"], 2), round(c["low"], 2), round(c["close"], 2)]
            for c in candles[-n:]]


def _near_zones(zones, price, pct=3.0, limit=6):
    """FVG/IFVG zones within `pct`% of price, nearest first."""
    if not zones:
        return []
    out = []
    for z in zones:
        top, bot = z.get("top"), z.get("bottom")
        if top is None or bot is None:
            continue
        mid = (top + bot) / 2
        if price > 0 and abs(mid - price) / price * 100 <= pct:
            out.append({"top": round(top, 2), "bottom": round(bot, 2),
                        "bull": bool(z.get("isbull", z.get("dir", 0) == 1))})
    out.sort(key=lambda z: abs((z["top"] + z["bottom"]) / 2 - price))
    return out[:limit]


def build_snapshot(symbol, price, c_entry, c_trend, ind, st, tn, fvg, ifvg,
                   ind_t, st_t, tn_t, bias_txt, votes, weights, account,
                   smc_entry=None, smc_trend=None,
                   c_ltf=None, ind_l=None, st_l=None, tn_l=None, smc_ltf=None,
                   historical_edge=None, news=None) -> dict:
    """Assemble everything the model needs to reason about the trade, including the
    deterministic Smart Money Concepts read for each timeframe (1h bias, 15m decision,
    5m timing)."""
    et = settings.entry_timeframe
    tt = settings.trend_timeframe
    lt = settings.ltf_timeframe

    def tf_block(candles, ind_, st_, tn_):
        return {
            "ema_fast": _r(ind_.get("ema_fast")),
            "ema_slow": _r(ind_.get("ema_slow")),
            "rsi": _r(ind_.get("rsi")),
            "macd": {"line": _r(ind_.get("macd"), 4), "signal": _r(ind_.get("macd_signal_line"), 4),
                     "hist": _r(ind_.get("macd_hist"), 4), "state": ind_.get("macd_signal")},
            "supertrend": (st_.get("latest") if st_ else None),
            "trendline": (tn_.get("latest") if tn_ else None),
            "recent_swing_highs": _swings(candles, True)[-6:],
            "recent_swing_lows": _swings(candles, False)[-6:],
        }

    snap = {
        "symbol": symbol,
        "current_price": round(price, 2),
        "decision_timeframe_min": et,
        "trend_timeframe_min": tt,
        "timing_timeframe_min": lt,
        "trend_bias_1h": bias_txt,
        "entry_tf": {
            **tf_block(c_entry, ind, st, tn),
            "last_20_ohlc": _fmt_candles(c_entry, 20),
            "fvg_zones_near": _near_zones((fvg or {}).get("unmitigated"), price),
            "ifvg_zones_near": _near_zones((ifvg or {}).get("zones"), price),
            "smc": smc_entry,
        },
        "trend_tf": {**tf_block(c_trend, ind_t, st_t, tn_t), "smc": smc_trend},
        "strategy_votes": votes,
        "strategy_weights": {k: round(v, 2) for k, v in (weights or {}).items()},
        # backtested edge on THIS symbol (clean mark data): {strat: {pf, exp, n}} — use it
        # to weight the votes; trust high-PF signals, discount PF~1.0 as noise.
        "historical_edge": historical_edge or {},
        "account": account,
        "risk_rules": {
            "min_reward_risk": settings.risk_reward,
            "risk_pct_range": [settings.risk_min_pct, settings.risk_max_pct],
            "sl_distance_pct_bounds": [settings.min_sl_pct, settings.max_sl_pct],
            "leverage": settings.leverage,
        },
    }
    # 5m timing timeframe (analyzed for entry timing/confirmation; not the decision TF)
    if ind_l is not None:
        snap["timing_tf"] = {**tf_block(c_ltf or [], ind_l, st_l, tn_l), "smc": smc_ltf}
    # Real-time news + upcoming high-impact economic events (see `news` guidance in system prompt).
    if news:
        snap["news"] = news
    return snap


def _r(v, nd=2):
    try:
        return round(float(v), nd)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
#  Prompt + call
# --------------------------------------------------------------------------- #
_SYSTEM = """You are an elite crypto-futures trader who trades Smart Money Concepts (SMC / ICT) on a live Delta \
Exchange account. You get a compact multi-timeframe snapshot with THREE timeframes: `trend_tf` (1h, sets bias), `entry_tf` \
(15m — the DECISION timeframe you trade on), and `timing_tf` (5m — used ONLY to refine entry timing/confirmation, \
never to override the 15m decision). Each carries EMAs, RSI, MACD (12/26/9 line/signal/histogram + state, for \
momentum confirmation), SuperTrend AI, trendline-break state, fair-value gaps, and — most importantly — a \
precomputed SMC read under `smc`: market structure (swings labelled HH/HL/LH/LL + trend), the latest BOS/CHoCH, liquidity \
(equal highs/lows, prev-day high/low, buy/sell-side pools), any recent liquidity sweep, order blocks (with a \
`mitigated` flag), and premium/discount (equilibrium + discount OTE zone). These SMC levels are computed from real \
candles — trust them over eyeballing the OHLC.

Trade the SMC playbook. Only take A+ setups:

BUY (mirror for SELL):
- 1h (trend_tf.smc) structure is bullish OR just printed a bullish CHoCH (reversal).
- Price has swept sell-side liquidity (recent_sweep dir bullish / took equal-lows or prev-day-low) then reclaimed.
- 15m confirms with a bullish BOS or CHoCH in your direction.
- Entry is into a DISCOUNT array: an unmitigated bullish order block and/or a fair-value gap, ideally at/below \
equilibrium (premium_discount.zone == "discount", inside discount_ote is best).
- Target the opposing liquidity: buy-side pools / equal-highs / prev-day-high / the range high.

Hard rules you MUST follow:
- Decide on the 15m (entry_tf). Use the 5m (timing_tf) only to CONFIRM the trigger (e.g. a 5m CHoCH/BOS or \
momentum turn in your direction) and sharpen entry — never take a trade the 15m doesn't support, and don't let \
5m noise flip a clean 15m read.
- `historical_edge` is each strategy's BACKTESTED profit-factor (pf) and expectancy on THIS symbol \
(clean data, no execution noise). Weight the `strategy_votes` by it: strongly trust a signal with pf >= 1.3, \
treat pf around 1.0 as noise, and be skeptical of pf < 1.0. Favor the setups that actually have edge here.
- Never fight the 1h trend bias. If bias is bullish, only BUY or HOLD; if bearish, only SELL or HOLD.
- Prefer discount entries for longs and premium entries for shorts. Do NOT buy into premium or sell into discount \
unless a fresh CHoCH + sweep justifies a reversal.
- Put the stop where structure is INVALIDATED — beyond the order block / swept swing that gave the entry (not a \
fixed distance). Stop distance must stay within the given sl_distance_pct_bounds (% of price).
- Provide AT MOST TWO take-profits (one is fine). The FIRST must be at least 2R at the next real liquidity pool; \
an optional TP2 sits further at the next pool / range extreme. size_pct must sum to ~1.0 (e.g. 0.5, 0.5 or a \
single 1.0), nearest-first. Never return three take-profits.
- Avoid chasing: if price already made a large impulsive move into premium/discount with no fresh sweep+OB, HOLD.
- A `news` block may be present: `upcoming_high_impact` lists economic releases with `in_min` (minutes \
until — negative means just released), currency, forecast vs previous; `latest_headlines` carry a rough \
tone; `headline_tone` is the net read. If a High-impact release for a relevant currency is imminent \
(small positive `in_min`, roughly < 30), treat it as elevated whipsaw risk — prefer HOLD or demand a \
cleaner setup and a tighter structural stop. Let `headline_tone` GENTLY tilt conviction, but never let a \
headline override a clean SMC read or invent a trade the structure doesn't support.
- If there is no clean SMC setup with a reachable 2R to real liquidity, return HOLD. Be selective — no forced trades.

Respond with ONLY minified JSON (no markdown, no prose) matching exactly:
{"action":"BUY|SELL|HOLD","confidence":0.0-1.0,"entry":<number>,"stop_loss":<number>,\
"take_profits":[{"price":<number>,"size_pct":<0..1>},...],"reasoning":"<2-3 sentences citing the SMC elements>",\
"invalidation":"<1 sentence>"}"""


def _extract_json(text: str) -> Optional[dict]:
    if not text:
        return None
    # strip code fences if present, then grab the outermost {...}
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def _call_gemini(snapshot_json: str, system: str = None, user_prefix: str = "MARKET SNAPSHOT:\n",
                 model: str = None) -> Optional[str]:
    """Google Gemini generateContent call (preferred provider). Returns raw text, or None.
    Forces JSON output via responseMimeType so the plan parses cleanly.
    `model` overrides the default tier (used to run the brief on Pro)."""
    if not _has_gemini_key():
        return None
    key = settings.gemini_api_key.strip()
    model = (model or settings.gemini_model or "gemini-flash-latest").strip()
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    gen: dict = {
        "temperature": 0.2,
        # Thinking tokens are charged as output and consume this same budget, so the
        # cap is the answer allowance PLUS whatever reasoning we've allowed.
        "maxOutputTokens": settings.gemini_max_output_tokens + max(0, settings.gemini_thinking_budget),
        "responseMimeType": "application/json",
    }
    if settings.gemini_thinking_budget != 0:
        gen["thinkingConfig"] = {"thinkingBudget": settings.gemini_thinking_budget}
    body = {
        "systemInstruction": {"parts": [{"text": system or _SYSTEM}]},
        "contents": [{"role": "user", "parts": [{"text": user_prefix + snapshot_json}]}],
        "generationConfig": gen,
    }
    try:
        r = httpx.post(url, params={"key": key}, json=body, timeout=settings.ai_timeout_sec)
    except Exception as e:
        logger.error(f"Gemini request error: {e} — trying next provider.")
        return None
    if r.status_code != 200:
        # 429 on a Pro model usually means the key has no paid quota for that tier —
        # name the model so the log says which tier fell through.
        logger.error(f"Gemini {model} {r.status_code}: {r.text[:160]} — trying next provider.")
        return None
    try:
        cands = r.json().get("candidates") or []
        if not cands:
            logger.error(f"Gemini returned no candidates: {r.text[:200]}")
            return None
        parts = cands[0].get("content", {}).get("parts", [])
        return "".join(p.get("text", "") for p in parts)
    except Exception as e:
        logger.error(f"Gemini parse error: {e}")
        return None


_api_client = None
_api_client_key = None


def _get_api_client():
    """Lazily build (and cache) the Anthropic client for the current key."""
    global _api_client, _api_client_key
    if not _has_api_key():
        return None
    key = settings.anthropic_api_key.strip()
    if _api_client is None or _api_client_key != key:
        try:
            import anthropic
        except ImportError:
            logger.warning("anthropic SDK not installed — falling back to Claude CLI.")
            return None
        _api_client = anthropic.Anthropic(api_key=key)
        _api_client_key = key
    return _api_client


def _call_api(snapshot_json: str, system: str = None, user_prefix: str = "MARKET SNAPSHOT:\n") -> Optional[str]:
    """Direct Anthropic Messages API call (preferred). Returns the raw text, or None."""
    client = _get_api_client()
    if client is None:
        return None
    try:
        msg = client.messages.create(
            model=settings.ai_model,
            max_tokens=1500,
            system=system or _SYSTEM,
            messages=[{"role": "user", "content": user_prefix + snapshot_json}],
        )
        return "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
    except Exception as e:
        logger.error(f"Anthropic API error ({type(e).__name__}): {str(e)[:200]} — falling back to CLI.")
        return None


def _call_claude(prompt: str) -> Optional[str]:
    cli = find_claude_cli()
    if not cli:
        logger.warning("Claude CLI not found — AI brain unavailable, falling back to mechanical engine.")
        return None
    try:
        proc = subprocess.run(
            [cli, "-p", "--output-format", "json", "--model", settings.ai_model],
            input=prompt.encode("utf-8"),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=settings.ai_timeout_sec,
        )
    except subprocess.TimeoutExpired:
        logger.error(f"Claude CLI timed out after {settings.ai_timeout_sec}s")
        return None
    except Exception as e:
        logger.error(f"Claude CLI invocation error: {e}")
        return None
    if proc.returncode != 0:
        logger.error(f"Claude CLI exit {proc.returncode}: {proc.stderr.decode('utf-8', 'ignore')[:300]}")
        return None
    try:
        raw = json.loads(proc.stdout.decode("utf-8", "ignore"))
    except json.JSONDecodeError:
        logger.error("Claude CLI returned non-JSON envelope")
        return None
    if raw.get("is_error"):
        logger.error(f"Claude CLI reported error: {raw.get('result')}")
        return None
    return raw.get("result")


#: Trading loop: Flash (fast + cheap enough to run every 5 min), then Claude.
DEFAULT_PROVIDERS = ("gemini", "anthropic", "cli")
#: News brief: try Pro first for a deeper read, drop to Flash, then Claude.
BRIEF_PROVIDERS = ("gemini-pro", "gemini", "anthropic", "cli")
#: Claude only — no Gemini at all.
CLAUDE_PROVIDERS = ("anthropic", "cli")


def complete_json(system: str, payload: dict, user_prefix: str = "INPUT:\n",
                  providers: tuple[str, ...] = DEFAULT_PROVIDERS) -> Optional[dict]:
    """Run an arbitrary JSON-out prompt through a chosen provider chain.

    `providers` lets a caller pin which models may answer — the brief passes
    CLAUDE_PROVIDERS so it never silently degrades to a different model.
    """
    body = json.dumps(payload, separators=(",", ":"))
    for via in providers:
        try:
            if via == "gemini":
                raw = _call_gemini(body, system=system, user_prefix=user_prefix)
            elif via == "gemini-pro":
                raw = _call_gemini(body, system=system, user_prefix=user_prefix,
                                   model=settings.gemini_pro_model)
            elif via == "anthropic":
                raw = _call_api(body, system=system, user_prefix=user_prefix)
            elif via == "cli":
                raw = _call_claude(system + "\n\n" + user_prefix + body)
            else:
                logger.warning(f"unknown provider '{via}' — skipping")
                continue
        except Exception as e:  # noqa: BLE001 — try the next provider
            logger.error(f"{via} call failed: {type(e).__name__}")
            raw = None
        if raw:
            out = _extract_json(raw)
            if out is not None:
                out["_via"] = via
                return out
    return None


def analyze(snapshot: dict) -> Optional[dict]:
    """
    Ask Claude for a trade plan. Returns a sanitized dict, or None if unavailable.
    Guardrail math (R:R, sizing, clamps) is enforced by the caller.
    """
    snap_json = json.dumps(snapshot, separators=(",", ":"))
    # Gemini first; Claude (API, then local CLI) is the fallback when Gemini fails,
    # errors, or is rate-limited.
    result = _call_gemini(snap_json)
    via = "gemini"
    if result is None:
        result = _call_api(snap_json)
        via = "anthropic"
    if result is None:
        result = _call_claude(_SYSTEM + "\n\nMARKET SNAPSHOT:\n" + snap_json)
        via = "cli"
    if result is None:
        return None
    plan = _extract_json(result)
    if not plan:
        logger.error(f"Could not parse AI plan ({via}) from: {result[:200]}")
        return None
    out = sanitize(plan)
    if out is not None:
        out["via"] = via
    return out


def sanitize(plan: dict) -> Optional[dict]:
    """Shape/type validation only. Returns None if the plan is structurally unusable."""
    if not isinstance(plan, dict):
        return None
    action = str(plan.get("action", "HOLD")).upper()
    if action not in ("BUY", "SELL", "HOLD"):
        action = "HOLD"
    try:
        conf = float(plan.get("confidence"))
    except (TypeError, ValueError):
        conf = 0.0
    conf = max(0.0, min(1.0, conf))

    out = {
        "action": action,
        "confidence": conf,
        "entry": _r(plan.get("entry")),
        "stop_loss": _r(plan.get("stop_loss")),
        "take_profits": [],
        "reasoning": str(plan.get("reasoning", ""))[:500],
        "invalidation": str(plan.get("invalidation", ""))[:300],
    }
    tps = plan.get("take_profits") or []
    for t in tps:
        if isinstance(t, dict):
            p, sz = _r(t.get("price")), t.get("size_pct")
        else:
            p, sz = _r(t), None
        if p is not None:
            try:
                sz = float(sz)
            except (TypeError, ValueError):
                sz = None
            out["take_profits"].append({"price": p, "size_pct": sz})
    return out
