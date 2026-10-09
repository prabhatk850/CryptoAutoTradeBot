"""AI brain: asks a provider chain for a JSON trade plan; the scheduler enforces every risk rule."""
from __future__ import annotations

import glob
import json
import logging
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from shutil import which
from typing import Optional

import httpx

from bot.swings import find_swings
from config import settings

logger = logging.getLogger("bot.ai_brain")

# Provider chains. `cli` (personal subscription) is deliberately in none of them.
# openrouter (Ox Alpha retired, 404) and agentrouter (account banned, 403) are out since 2026-09-20; re-add when fixed.
DEFAULT_PROVIDERS = ("groq", "gemini")       # deep loop + ad-hoc analysis
BRIEF_PROVIDERS = ("groq", "gemini-pro")     # news brief
FAST_PROVIDERS = ("groq", "gemini")          # fast loop: never a slow reasoning model


def _key(value: str) -> str:
    """Configured key, or "" for blanks and the .env.example placeholder."""
    v = (value or "").strip()
    return "" if v == "paste-your-key-here" else v


# --------------------------------------------------------------------------- #
#  Claude CLI (AgentRouter / subscription)
# --------------------------------------------------------------------------- #
_CLI_CACHE: Optional[str] = None


def _ver_key(p: str) -> tuple:
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", p)
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


def find_claude_cli() -> Optional[str]:
    """Locate the newest Claude Code binary (cached)."""
    global _CLI_CACHE
    if _CLI_CACHE and Path(_CLI_CACHE).exists():
        return _CLI_CACHE
    if settings.claude_cli_path and Path(settings.claude_cli_path).exists():
        _CLI_CACHE = settings.claude_cli_path
        return _CLI_CACHE
    home = Path.home()
    patterns = [
        str(home / "AppData/Local/Packages/*/LocalCache/Roaming/Claude/claude-code/*/claude.exe"),  # Desktop app
        str(home / "AppData/Roaming/Claude/claude-code/*/claude.exe"),
        str(home / "AppData/Roaming/npm/claude.cmd"),
        str(home / ".local/bin/claude"),
        "/usr/local/bin/claude",
    ]
    candidates = [c for pat in patterns for c in glob.glob(pat)]
    if which("claude"):
        candidates.append(which("claude"))
    candidates = sorted((c for c in candidates if Path(c).exists()), key=_ver_key)
    if not candidates:
        return None
    _CLI_CACHE = candidates[-1]
    logger.info(f"AI brain using Claude CLI: {_CLI_CACHE}")
    return _CLI_CACHE


_router_spend: list = [None, 0]   # [UTC date, calls today]
_cli_spend: list = [None, 0]


def _budget_left(counter: list, cap: int) -> bool:
    """Per-UTC-day call budget (cap <= 0 = unlimited); rolls over at midnight."""
    if cap <= 0:
        return True
    today = datetime.now(timezone.utc).date()
    if counter[0] != today:
        counter[0], counter[1] = today, 0
    return counter[1] < cap


def _spend(counter: list, cap: int, label: str) -> None:
    counter[1] += 1
    if cap > 0 and counter[1] == cap:
        logger.warning(f"{label} daily cap reached ({cap} calls) — skipping it until UTC midnight.")


def agentrouter_available() -> bool:
    return (bool(_key(settings.agentrouter_api_key)) and find_claude_cli() is not None
            and _budget_left(_router_spend, settings.agentrouter_daily_call_cap))


def _claude_env(via_router: bool) -> dict:
    """Subprocess env: scrub inherited Anthropic creds; inject AgentRouter's only for router calls."""
    env = os.environ.copy()
    for var in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"):
        env.pop(var, None)
    if via_router:
        key = _key(settings.agentrouter_api_key)
        env.update(ANTHROPIC_BASE_URL=settings.agentrouter_base_url, ANTHROPIC_AUTH_TOKEN=key, ANTHROPIC_API_KEY=key)
    return env


def _call_claude(prompt: str, via_router: bool = False) -> Optional[str]:
    """Headless `claude -p`. via_router bills AgentRouter; otherwise the (disabled-by-default) subscription."""
    cli = find_claude_cli()
    if not cli:
        return None
    label = "agentrouter" if via_router else "cli"
    if via_router:
        if not agentrouter_available():
            return None
        _spend(_router_spend, settings.agentrouter_daily_call_cap, "AgentRouter")
    else:
        if not settings.ai_allow_subscription_cli:
            logger.warning("Subscription CLI disabled (AI_ALLOW_SUBSCRIPTION_CLI=false) — not spending it.")
            return None
        if not _budget_left(_cli_spend, settings.subscription_cli_daily_call_cap):
            return None
        _spend(_cli_spend, settings.subscription_cli_daily_call_cap, "Subscription CLI")
    model = settings.agentrouter_model if via_router else settings.ai_model
    try:
        proc = subprocess.run([cli, "-p", "--output-format", "json", "--model", model],
                              input=prompt.encode("utf-8"), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=settings.ai_timeout_sec, env=_claude_env(via_router))
    except subprocess.TimeoutExpired:
        logger.error(f"Claude CLI [{label}] timed out after {settings.ai_timeout_sec}s")
        return None
    except Exception as e:
        logger.error(f"Claude CLI [{label}] invocation error: {e}")
        return None
    try:
        envelope = json.loads(proc.stdout.decode("utf-8", "ignore"))
    except json.JSONDecodeError:
        envelope = None
    if proc.returncode != 0 or not isinstance(envelope, dict) or envelope.get("is_error"):
        # The real reason is usually in the JSON envelope; stderr is often a benign warning.
        detail = (envelope or {}).get("result") or proc.stderr.decode("utf-8", "ignore")[:300]
        logger.error(f"Claude CLI [{label}] failed (exit {proc.returncode}): {detail}")
        return None
    return envelope.get("result")


# --------------------------------------------------------------------------- #
#  HTTP providers
# --------------------------------------------------------------------------- #
def _post(label: str, url: str, body: dict, extract, **kw) -> Optional[str]:
    """POST a provider request; any failure logs and returns None so the chain moves on."""
    try:
        r = httpx.post(url, json=body, timeout=settings.ai_timeout_sec, **kw)
    except Exception as e:
        logger.error(f"{label} request error: {e} — trying next provider.")
        return None
    if r.status_code != 200:
        # 404 usually means a retired model, 429/413 a free-tier limit.
        logger.error(f"{label} {r.status_code}: {r.text[:160]} — trying next provider.")
        return None
    try:
        return extract(r.json()) or None
    except Exception as e:
        logger.error(f"{label} parse error: {e} ({r.text[:200]})")
        return None


def _openai_chat(label: str, base_url: str, key: str, model: str, max_tokens: int,
                 system: str, user: str, json_mode: bool, headers: dict | None = None) -> Optional[str]:
    """OpenAI-compatible chat completion (Groq, OpenRouter)."""
    if not key:
        return None
    body = {"model": model, "temperature": 0.2, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    return _post(f"{label} {model}", base_url.rstrip("/") + "/chat/completions", body,
                 lambda j: j["choices"][0]["message"]["content"],
                 headers={"Authorization": f"Bearer {key}", **(headers or {})})


def _call_gemini(system: str, user: str, model: str) -> Optional[str]:
    key = _key(settings.gemini_api_key)
    if not key:
        return None
    gen = {"temperature": 0.2, "responseMimeType": "application/json",
           # Thinking tokens bill as output, so the cap is answer budget + thinking budget.
           "maxOutputTokens": settings.gemini_max_output_tokens + max(0, settings.gemini_thinking_budget)}
    if settings.gemini_thinking_budget != 0:
        gen["thinkingConfig"] = {"thinkingBudget": settings.gemini_thinking_budget}
    body = {"systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": gen}
    return _post(f"Gemini {model}", f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                 body, lambda j: "".join(p.get("text", "") for p in j["candidates"][0]["content"]["parts"]),
                 params={"key": key})


# name -> fn(system, user) -> raw text | None
_PROVIDERS = {
    # No JSON mode on Ox Alpha: support is undocumented and a 400 would waste the free rung.
    "openrouter": lambda s, u: _openai_chat(
        "OpenRouter", settings.openrouter_base_url, _key(settings.openrouter_api_key), settings.openrouter_model,
        settings.openrouter_max_output_tokens, s, u, json_mode=False,
        headers={"HTTP-Referer": settings.openrouter_referer, "X-Title": settings.openrouter_title}),
    "groq": lambda s, u: _openai_chat(
        "Groq", settings.groq_base_url, _key(settings.groq_api_key), settings.groq_model,
        settings.groq_max_output_tokens, s, u, json_mode=True),
    "gemini": lambda s, u: _call_gemini(s, u, settings.gemini_model),
    "gemini-pro": lambda s, u: _call_gemini(s, u, settings.gemini_pro_model),
    "agentrouter": lambda s, u: _call_claude(s + "\n\n" + u, via_router=True),
    "cli": lambda s, u: _call_claude(s + "\n\n" + u),
}


def _configured(provider: str) -> bool:
    return {
        "openrouter": lambda: bool(_key(settings.openrouter_api_key)),
        "groq": lambda: bool(_key(settings.groq_api_key)),
        "gemini": lambda: bool(_key(settings.gemini_api_key)),
        "gemini-pro": lambda: bool(_key(settings.gemini_api_key)),
        "agentrouter": agentrouter_available,
        "cli": lambda: settings.ai_allow_subscription_cli and find_claude_cli() is not None,
    }.get(provider, lambda: False)()


def available(providers: tuple[str, ...] = DEFAULT_PROVIDERS) -> bool:
    """AI is on and at least one provider in the chain is configured."""
    return settings.ai_enabled and any(_configured(p) for p in providers)


def _extract_json(text: str) -> Optional[dict]:
    """Outermost {...} in a reply, tolerating code fences and prose."""
    if not text:
        return None
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    m = re.search(r"\{.*\}", text, re.S)
    try:
        return json.loads(m.group(0)) if m else None
    except json.JSONDecodeError:
        return None


def complete_json(system: str, payload: dict, user_prefix: str = "INPUT:\n",
                  providers: tuple[str, ...] = DEFAULT_PROVIDERS) -> Optional[dict]:
    """First provider in the chain that returns parseable JSON wins; result carries `_via`."""
    user = user_prefix + json.dumps(payload, separators=(",", ":"))
    for via in providers:
        fn = _PROVIDERS.get(via)
        if not fn:
            logger.warning(f"unknown provider '{via}' — skipping")
            continue
        try:
            raw = fn(system, user)
        except Exception as e:  # noqa: BLE001 — try the next provider
            logger.error(f"{via} call failed: {type(e).__name__}")
            continue
        if not raw:
            continue
        out = _extract_json(raw)
        if out is not None:
            out["_via"] = via
            return out
        logger.error(f"Could not parse JSON from {via}: {raw[:200]}")
    return None


# --------------------------------------------------------------------------- #
#  Trade plan
# --------------------------------------------------------------------------- #
def _r(v, nd=2):
    try:
        return round(float(v), nd)
    except (TypeError, ValueError):
        return None


def _swing_points(candles: list[dict], kind: str) -> list[dict]:
    return [{"time": s["time"], "price": s["price"]} for s in find_swings(candles) if s["kind"] == kind][-6:]


def _near_zones(zones, price, pct=3.0, limit=6):
    """FVG/IFVG zones within `pct`% of price, nearest first."""
    out = []
    for z in zones or []:
        top, bot = z.get("top"), z.get("bottom")
        if top is None or bot is None or price <= 0 or abs((top + bot) / 2 - price) / price * 100 > pct:
            continue
        out.append({"top": round(top, 2), "bottom": round(bot, 2),
                    "bull": bool(z.get("isbull", z.get("dir", 0) == 1))})
    out.sort(key=lambda z: abs((z["top"] + z["bottom"]) / 2 - price))
    return out[:limit]


def _tf_block(tf: dict) -> dict:
    """Compact indicator read for one timeframe (see scheduler.analyze_timeframe)."""
    ind, st, tn = tf["ind"], tf["st"], tf["tn"]
    return {
        "ema_fast": _r(ind.get("ema_fast")),
        "ema_slow": _r(ind.get("ema_slow")),
        "rsi": _r(ind.get("rsi")),
        "macd": {"line": _r(ind.get("macd"), 4), "signal": _r(ind.get("macd_signal_line"), 4),
                 "hist": _r(ind.get("macd_hist"), 4), "state": ind.get("macd_signal")},
        "supertrend": st.get("latest") if st else None,
        "trendline": tn.get("latest") if tn else None,
        "recent_swing_highs": _swing_points(tf["candles"], "high"),
        "recent_swing_lows": _swing_points(tf["candles"], "low"),
        "bb": {"mid": _r(ind.get("bb_mid")), "upper": _r(ind.get("bb_upper")), "lower": _r(ind.get("bb_lower"))},
        "kc": {"mid": _r(ind.get("kc_mid")), "upper": _r(ind.get("kc_upper")), "lower": _r(ind.get("kc_lower"))},
        "squeeze": ind.get("squeeze_signal"),
        "adx": _r(ind.get("adx"), 1),
        "vwap": _r(ind.get("vwap")),
        "smc": tf["smc"],
    }


def build_snapshot(symbol: str, entry: dict, trend: dict, timing: Optional[dict], bias_txt: str,
                   votes: dict, weights: dict | None, historical_edge=None, news=None,
                   divergence=None, funding=None, orderbook=None) -> dict:
    """Everything the model reasons over: 1h bias, 15m decision, 5m timing, votes, edge and context."""
    price = entry["ind"]["close"]
    snap = {
        "symbol": symbol,
        "current_price": round(price, 2),
        "decision_timeframe_min": settings.entry_timeframe,
        "trend_timeframe_min": settings.trend_timeframe,
        "timing_timeframe_min": settings.ltf_timeframe,
        "trend_bias_1h": bias_txt,
        "entry_tf": {
            **_tf_block(entry),
            "last_20_ohlc": [[round(c[k], 2) for k in ("open", "high", "low", "close")] for c in entry["candles"][-20:]],
            "fvg_zones_near": _near_zones((entry["fvg"] or {}).get("unmitigated"), price),
            "ifvg_zones_near": _near_zones((entry["ifvg"] or {}).get("zones"), price),
            "divergence": (divergence or {}).get("latest"),
        },
        "trend_tf": _tf_block(trend),
        "strategy_votes": votes,
        "strategy_weights": {k: round(v, 2) for k, v in (weights or {}).items()},
        "historical_edge": historical_edge or {},   # backtested {strategy: {pf, exp, n}} on this symbol
        "risk_rules": {
            "min_reward_risk": settings.risk_reward,
            "risk_pct_range": [settings.risk_min_pct, settings.risk_max_pct],
            "sl_distance_pct_bounds": [settings.min_sl_pct, settings.max_sl_pct],
            "leverage": settings.leverage,
        },
    }
    if timing:
        snap["timing_tf"] = _tf_block(timing)
    for key, val in (("news", news), ("funding", funding), ("orderbook", orderbook)):
        if val:
            snap[key] = val
    return snap


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
- Each timeframe also carries `bb`/`kc`/`squeeze` (Bollinger/Keltner squeeze state — "SQUEEZE_ON" means volatility \
is compressed and building, "RELEASE_UP"/"RELEASE_DOWN" means it just let go in that direction), `adx` (trend \
strength, 0-100), and `vwap` (session volume-weighted average price, when available). Treat `adx` on trend_tf \
below ~20 as a warning that the 1h market is ranging — trend-following reads (EMA, SuperTrend, trendline) are \
less reliable there, so demand a cleaner SMC setup before trusting a breakout. A squeeze release in your \
direction is supportive confluence, never a standalone reason to trade.
- `entry_tf.divergence`, when present, is the latest RSI/price divergence at a swing point ("regular" = reversal, \
"hidden" = trend continuation). Treat it as one more piece of confluence for or against the setup — not an \
override of the SMC read.
- An optional `funding` block (perpetual funding rate + open interest) may be present: `extreme` is "high" \
(crowded longs paying heavily — mild contrarian lean against more upside), "low" (crowded shorts — mild \
contrarian lean against more downside), or null (not extreme / not enough history). Let it gently tilt \
conviction at most — never let it override a clean SMC setup or invent a trade the structure doesn't support.
- An optional `orderbook` block (top-of-book bid/ask volume imbalance) may be present: it is a SECONDS-scale \
signal, useful only the way `timing_tf` is — to sharpen entry timing — never to override the 15m decision.

Respond with ONLY minified JSON (no markdown, no prose) matching exactly:
{"action":"BUY|SELL|HOLD","confidence":0.0-1.0,"entry":<number>,"stop_loss":<number>,\
"take_profits":[{"price":<number>,"size_pct":<0..1>},...],"reasoning":"<2-3 sentences citing the SMC elements>",\
"invalidation":"<1 sentence>"}"""


def analyze(snapshot: dict, providers: tuple[str, ...] = DEFAULT_PROVIDERS) -> Optional[dict]:
    """Sanitized trade plan (with `via`), or None if no provider answered usefully."""
    plan = complete_json(_SYSTEM, snapshot, "MARKET SNAPSHOT:\n", providers)
    if not plan:
        return None
    via = plan.pop("_via")
    out = sanitize(plan)
    if out is not None:
        out["via"] = via
    return out


def sanitize(plan: dict) -> Optional[dict]:
    """Shape/type validation only; numbers are checked by the scheduler."""
    if not isinstance(plan, dict):
        return None
    action = str(plan.get("action", "HOLD")).upper()
    try:
        conf = max(0.0, min(1.0, float(plan.get("confidence"))))
    except (TypeError, ValueError):
        conf = 0.0
    tps = []
    for t in plan.get("take_profits") or []:
        p, sz = (_r(t.get("price")), t.get("size_pct")) if isinstance(t, dict) else (_r(t), None)
        if p is None:
            continue
        try:
            sz = float(sz)
        except (TypeError, ValueError):
            sz = None
        tps.append({"price": p, "size_pct": sz})
    return {
        "action": action if action in ("BUY", "SELL", "HOLD") else "HOLD",
        "confidence": conf,
        "entry": _r(plan.get("entry")),
        "stop_loss": _r(plan.get("stop_loss")),
        "take_profits": tps,
        "reasoning": str(plan.get("reasoning", ""))[:500],
        "invalidation": str(plan.get("invalidation", ""))[:300],
    }
