"""
Market news + economic calendar (real-time).

Two independent, in-process cached sources:

  • Economic calendar — ForexFactory's official weekly JSON (served via FairEconomy):
    each event carries currency, impact (High/Medium/Low), forecast and previous.
    This is the "forecast" table shown on the dashboard.

  • News headlines — ForexFactory has no public API, so we aggregate the same kind of
    real-time forex/macro/crypto headlines from several established RSS feeds
    (ForexLive, FXStreet, Investing.com, Cointelegraph). Merged, de-duplicated,
    newest-first, HTML/images stripped.

It also exposes:
  • ai_context()  — a compact news+events block injected into the AI snapshot each tick.
  • in_blackout() — a guardrail the scheduler uses to skip opening NEW trades in the
    window around a High-impact release.

No extra dependencies: httpx (already used) + stdlib XML/date parsing.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Optional
from xml.etree import ElementTree as ET

import httpx

from config import settings
from db import db

logger = logging.getLogger("bot.news")

_UA = "Mozilla/5.0 (compatible; ForexBot/1.0)"
_ATOM = "{http://www.w3.org/2005/Atom}"
IMPACT_RANK = {"high": 3, "medium": 2, "low": 1, "holiday": 0, "": 0}

# in-process caches: {"data": [...], "ts": epoch_seconds}
# `hydrated` tracks whether we've already tried to reload this cache from Mongo.
_news_cache: dict = {"data": [], "ts": 0.0, "hydrated": False}
_cal_cache: dict = {"data": [], "ts": 0.0, "hydrated": False}
_tg_cache: dict = {"data": [], "ts": 0.0, "hydrated": False}
_news_lock = asyncio.Lock()
_cal_lock = asyncio.Lock()
_tg_lock = asyncio.Lock()

# Both upstreams rate-limit hard (the calendar host answers 429 to rapid refetches),
# so an in-process-only cache means every backend restart can leave the dashboard
# empty for minutes. Mirroring the last good payload into Mongo makes a restart
# serve data immediately and, because the persisted timestamp comes back too, usually
# avoids refetching at all.
_CACHE_KEYS = {"news": _news_cache, "calendar": _cal_cache, "telegram": _tg_cache}

# --- light, risk/crypto-oriented tone lexicon (display accent + a gentle AI hint) --- #
_BULL = {
    "surge", "surges", "soar", "soars", "rally", "rallies", "jump", "jumps", "gains",
    "rise", "rises", "climb", "climbs", "beat", "beats", "bullish", "record high",
    "upgrade", "optimism", "rebound", "rebounds", "recovers", "boost", "boosts", "inflow",
}
_BEAR = {
    "plunge", "plunges", "fall", "falls", "drop", "drops", "slump", "slumps", "sink",
    "sinks", "tumble", "tumbles", "crash", "selloff", "sell-off", "bearish", "miss",
    "misses", "downgrade", "fear", "fears", "recession", "hawkish", "sanction", "sanctions",
    "ban", "bans", "hack", "hacked", "liquidation", "liquidations", "warning", "outflow", "slide",
}


# --------------------------------------------------------------------------- #
#  Parsing helpers
# --------------------------------------------------------------------------- #
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _clean(text: Optional[str], limit: int = 220) -> str:
    """Strip HTML tags (including any <img>), unescape entities, collapse whitespace."""
    if not text:
        return ""
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text).strip()
    return text[:limit].rstrip()


def _parse_date(s: Optional[str]) -> Optional[datetime]:
    """Parse RFC-822 (RSS), ISO-8601 (Atom / FF calendar), or 'YYYY-MM-DD HH:MM:SS'."""
    if not s:
        return None
    s = s.strip()
    try:  # RFC-822: "Fri, 24 Jul 2026 21:51:42 GMT"
        dt = parsedate_to_datetime(s)
        if dt is not None:
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, IndexError):
        pass
    try:  # ISO-8601, incl. "2026-07-24T20:44:25-04:00" and "2026-07-24 20:44:25"
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _sentiment(text: str) -> str:
    t = " " + text.lower() + " "
    b = sum(1 for w in _BULL if w in t)
    r = sum(1 for w in _BEAR if w in t)
    return "bullish" if b > r else "bearish" if r > b else "neutral"


def _domain(url: str) -> str:
    m = re.search(r"https?://(?:www\.)?([^/]+)", url or "")
    return m.group(1) if m else "news"


def _feed_list() -> list[tuple[str, str]]:
    """Parse `news_feeds` — comma-separated `Name|url` (name optional)."""
    out: list[tuple[str, str]] = []
    for tok in (settings.news_feeds or "").split(","):
        tok = tok.strip()
        if not tok:
            continue
        name, url = tok.split("|", 1) if "|" in tok else ("", tok)
        url = url.strip()
        out.append((name.strip() or _domain(url), url))
    return out


def _parse_feed(source: str, raw: bytes) -> list[dict]:
    """One RSS/Atom document -> list of story dicts."""
    if not raw:
        return []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []
    items = root.findall(".//item") or root.findall(f".//{_ATOM}entry")
    stories = []
    for it in items:
        def get(tag: str) -> Optional[str]:
            v = it.findtext(tag)
            return v if v is not None else it.findtext(f"{_ATOM}{tag}")

        title = _clean(get("title"), 200)
        if not title:
            continue
        link = (get("link") or "").strip()
        if not link:  # Atom stores link as an attribute
            le = it.find(f"{_ATOM}link")
            link = le.get("href", "") if le is not None else ""
        dt = _parse_date(get("pubDate") or get("published") or get("updated") or get("date"))
        summary = _clean(get("description") or get("summary"), 200)
        stories.append({
            "source": source,
            "title": title,
            "link": link,
            "summary": summary,
            "published": dt.isoformat() if dt else None,
            "ts": dt.timestamp() if dt else 0.0,
            "sentiment": _sentiment(f"{title} {summary}"),
        })
    return stories


def _merge_stories(per_feed: list[list[dict]]) -> list[dict]:
    """Flatten, de-dup by normalized title, newest-first, capped."""
    flat = [s for feed in per_feed for s in feed]
    flat.sort(key=lambda x: x["ts"], reverse=True)
    seen, out = set(), []
    for s in flat:
        key = re.sub(r"[^a-z0-9]", "", s["title"].lower())[:64]
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out[: max(1, settings.news_max_stories)]


def _relevant_currencies() -> set[str]:
    """Currencies the bot reacts to (AI context + blackout). Empty set = all."""
    return {c.strip().upper() for c in (settings.news_currencies or "").split(",") if c.strip()}


def _is_relevant(e: dict) -> bool:
    cur = _relevant_currencies()
    return not cur or e.get("currency", "") in cur


# --------------------------------------------------------------------------- #
#  Telegram channels (public web preview)
# --------------------------------------------------------------------------- #
_TG_POST_RE = re.compile(r'data-post="([^"]+)"')
_TG_TIME_RE = re.compile(r'<time datetime="([^"]+)"')
_TG_TEXT_RE = re.compile(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.S)
_TG_BR_RE = re.compile(r"<br\s*/?>", re.I)
_TG_SIG_RE = re.compile(r"\s*@\w+\s*$")          # trailing channel signature


def _tg_list() -> list[tuple[str, str]]:
    """Parse `telegram_channels` — comma-separated `Name|handle` (name optional)."""
    out: list[tuple[str, str]] = []
    for tok in (settings.telegram_channels or "").split(","):
        tok = tok.strip()
        if not tok:
            continue
        name, handle = tok.split("|", 1) if "|" in tok else ("", tok)
        handle = handle.strip().lstrip("@").rsplit("/", 1)[-1]   # accept a full t.me URL too
        if handle:
            out.append((name.strip() or handle, handle))
    return out


def _parse_telegram(name: str, handle: str, html: str) -> list[dict]:
    """One t.me/s/<handle> preview page -> post dicts shaped like news stories.

    Each message block is split on the wrapper div so a malformed post can only lose
    itself, not the rest of the page.
    """
    if not html:
        return []
    posts = []
    for chunk in html.split('class="tgme_widget_message_wrap')[1:]:
        m_txt = _TG_TEXT_RE.search(chunk)
        if not m_txt:
            continue                                    # photo-only / service message
        body = _clean(_TG_BR_RE.sub(" ", m_txt.group(1)), 600)
        body = _TG_SIG_RE.sub("", body).strip()
        if not body:
            continue
        m_post = _TG_POST_RE.search(chunk)
        m_time = _TG_TIME_RE.search(chunk)
        dt = _parse_date(m_time.group(1)) if m_time else None
        post_id = m_post.group(1) if m_post else ""
        posts.append({
            "source": name,
            "channel": handle,
            "post_id": post_id,
            "title": body[:200],
            "text": body,
            "link": f"https://t.me/{post_id}" if post_id else f"https://t.me/{handle}",
            "published": dt.isoformat() if dt else None,
            "ts": dt.timestamp() if dt else 0.0,
            "sentiment": _sentiment(body),
        })
    return posts


def _merge_posts(per_channel: list[list[dict]]) -> list[dict]:
    """Flatten, de-dup by post id, newest-first, capped."""
    flat = [p for ch in per_channel for p in ch]
    flat.sort(key=lambda x: x["ts"], reverse=True)
    seen, out = set(), []
    for p in flat:
        key = p["post_id"] or re.sub(r"[^a-z0-9]", "", p["title"].lower())[:64]
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out[: max(1, settings.telegram_max_posts)]


def _parse_calendar(data: list) -> list[dict]:
    """Keep the whole week the feed publishes — the dashboard shows every currency.
    `news_currencies` narrows only what the AI sees and what triggers a blackout."""
    out = []
    for e in data or []:
        if not isinstance(e, dict):
            continue
        cur = str(e.get("country", "")).upper()
        dt = _parse_date(e.get("date"))
        impact = str(e.get("impact", "")).strip()
        out.append({
            "title": str(e.get("title", "")).strip(),
            "currency": cur,
            "impact": impact,
            "impact_rank": IMPACT_RANK.get(impact.lower(), 0),
            "forecast": str(e.get("forecast") or ""),
            "previous": str(e.get("previous") or ""),
            "actual": str(e.get("actual") or ""),  # feed usually omits; kept for forward-compat
            "date": dt.isoformat() if dt else None,
            "ts": dt.timestamp() if dt else 0.0,
        })
    out.sort(key=lambda x: x["ts"])
    return out


# --------------------------------------------------------------------------- #
#  Fetch + cache
# --------------------------------------------------------------------------- #
_RETRY_AFTER_FAIL = 60   # transient failure (network hiccup, empty parse)
# The calendar host answers 429 for a sustained period once you poll it too often, so
# retrying a minute later just renews the ban. Back off properly instead.
_RETRY_AFTER_429 = 1800  # 30 min


def _due(cache: dict, ttl: float) -> bool:
    """Time-based (not data-based) so the failure backoff applies even on a cold start."""
    return (time.time() - cache["ts"]) >= ttl


async def _hydrate(key: str) -> None:
    """Reload a cold in-process cache from Mongo (once per process)."""
    cache = _CACHE_KEYS[key]
    if cache["hydrated"] or cache["data"]:
        return
    try:
        doc = await db.news_cache.find_one({"_id": key})
        cache["hydrated"] = True   # DB answered — no need to ask again this process
        if doc and doc.get("data"):
            cache["data"], cache["ts"] = doc["data"], float(doc.get("ts") or 0.0)
            logger.info(f"{key} cache restored from Mongo ({len(cache['data'])} items)")
    except Exception as e:  # noqa: BLE001 — leave `hydrated` False so a later call retries
        logger.warning(f"{key} cache restore skipped ({type(e).__name__})")


async def _mark_success(key: str, cache: dict, data: list) -> None:
    cache["data"], cache["ts"] = data, time.time()
    try:
        await db.news_cache.update_one(
            {"_id": key}, {"$set": {"data": data, "ts": cache["ts"]}}, upsert=True
        )
    except Exception as e:  # noqa: BLE001 — persistence is best-effort
        logger.warning(f"{key} cache persist failed ({type(e).__name__})")


def _mark_failure(cache: dict, ttl: float, retry_after: float = _RETRY_AFTER_FAIL) -> None:
    """Keep the last good data and allow a retry in `retry_after` seconds — this is what
    stops us from hammering a rate-limited (HTTP 429) endpoint every tick."""
    cache["ts"] = time.time() - max(0.0, ttl - retry_after)


async def _get(client: httpx.AsyncClient, url: str) -> bytes:
    try:
        r = await client.get(url, headers={"User-Agent": _UA}, follow_redirects=True, timeout=15)
        if r.status_code == 200:
            return r.content
        logger.warning(f"news fetch {url}: HTTP {r.status_code}")
    except Exception as e:  # noqa: BLE001 — any network hiccup: skip this source
        logger.warning(f"news fetch failed {url}: {type(e).__name__}")
    return b""


async def get_news(force: bool = False) -> list[dict]:
    if not settings.news_enabled:
        return []
    ttl = settings.news_refresh_sec
    await _hydrate("news")
    if not force and not _due(_news_cache, ttl):
        return _news_cache["data"]
    async with _news_lock:
        if not force and not _due(_news_cache, ttl):
            return _news_cache["data"]
        feeds = _feed_list()
        async with httpx.AsyncClient() as client:
            payloads = await asyncio.gather(*[_get(client, url) for _, url in feeds])
        per_feed = await asyncio.to_thread(
            lambda: [_parse_feed(name, raw) for (name, _), raw in zip(feeds, payloads)]
        )
        merged = _merge_stories(per_feed)
        if merged:
            await _mark_success("news", _news_cache, merged)
        else:
            _mark_failure(_news_cache, ttl)   # keep whatever we already had
        return _news_cache["data"]


async def get_telegram(force: bool = False) -> list[dict]:
    """Latest posts from the configured public Telegram channels (cached like the feeds)."""
    if not settings.news_enabled:
        return []
    ttl = settings.news_refresh_sec
    await _hydrate("telegram")
    if not force and not _due(_tg_cache, ttl):
        return _tg_cache["data"]
    async with _tg_lock:
        if not force and not _due(_tg_cache, ttl):
            return _tg_cache["data"]
        channels = _tg_list()
        if not channels:
            return _tg_cache["data"]
        async with httpx.AsyncClient() as client:
            pages = await asyncio.gather(
                *[_get(client, f"https://t.me/s/{h}") for _, h in channels])
        per_channel = await asyncio.to_thread(
            lambda: [_parse_telegram(name, h, raw.decode("utf-8", "ignore"))
                     for (name, h), raw in zip(channels, pages)])
        merged = _merge_posts(per_channel)
        if merged:
            await _mark_success("telegram", _tg_cache, merged)
        else:
            _mark_failure(_tg_cache, ttl)   # keep the last good timeline
        return _tg_cache["data"]


async def get_calendar(force: bool = False) -> list[dict]:
    if not settings.news_enabled:
        return []
    ttl = settings.news_calendar_refresh_sec
    await _hydrate("calendar")
    if not force and not _due(_cal_cache, ttl):
        return _cal_cache["data"]
    async with _cal_lock:
        if not force and not _due(_cal_cache, ttl):
            return _cal_cache["data"]
        data, status = [], 0
        async with httpx.AsyncClient() as client:
            try:
                r = await client.get(settings.news_calendar_url, headers={"User-Agent": _UA},
                                     follow_redirects=True, timeout=15)
                status = r.status_code
                if status == 200:
                    data = r.json()
                else:  # 429 rate-limit etc. — back off, keep last good data
                    logger.warning(f"calendar fetch: HTTP {status}")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"calendar fetch failed: {type(e).__name__}")
        events = await asyncio.to_thread(_parse_calendar, data) if data else []
        if events:
            await _mark_success("calendar", _cal_cache, events)
        else:  # keep the last good week; wait out a rate-limit rather than renewing it
            _mark_failure(_cal_cache, ttl,
                          _RETRY_AFTER_429 if status == 429 else _RETRY_AFTER_FAIL)
        return _cal_cache["data"]


# --------------------------------------------------------------------------- #
#  AI pre-market brief
# --------------------------------------------------------------------------- #
_brief_cache: dict = {"data": None, "ts": 0.0}
_brief_lock = asyncio.Lock()

async def _market_snapshot(symbols=("BTCUSD", "ETHUSD")) -> list[dict]:
    """Factual overnight numbers, computed in code — never asked of the model.

    The AI writes prose only; every figure a trader might act on comes from the
    exchange, so the brief cannot invent a price, a move, or a funding rate.
    """
    from bot.delta_client import DeltaClient
    from bot.indicators import calc_rsi, calc_ema
    import pandas as pd

    d = DeltaClient()
    out = []
    for sym in symbols:
        row = {"symbol": sym}
        try:
            t = await d.get_ticker(sym)
            row.update({
                "price": round(float(t.get("mark_price") or t.get("close") or 0), 2),
                "change_24h_pct": round(float(t.get("mark_change_24h") or 0), 2),
                "high_24h": round(float(t.get("mark_high_24h") or 0), 2),
                "low_24h": round(float(t.get("mark_low_24h") or 0), 2),
                "funding_rate": float(t.get("funding_rate") or 0),
                "open_interest": round(float(t.get("oi") or 0), 2),
                "oi_change_6h_usd": round(float(t.get("oi_change_usd_6h") or 0), 2),
            })
        except Exception:
            continue
        try:
            candles = await d.get_candles(sym, 60, 120)
            if len(candles) > 25:
                closes = pd.Series([c["close"] for c in candles])
                rsi = calc_rsi(closes, settings.rsi_period)
                val = rsi.iloc[-1]
                row["rsi_1h"] = round(float(val), 1) if val == val else None
                # A factual technical read to sit alongside the news, so the day bias
                # isn't formed from headlines alone.
                ef = calc_ema(closes, settings.ema_fast).iloc[-1]
                es = calc_ema(closes, settings.ema_slow).iloc[-1]
                if ef == ef and es == es:
                    row["ema_1h"] = f"EMA{settings.ema_fast} {'above' if ef > es else 'below'} EMA{settings.ema_slow}"
                    row["trend_1h"] = "up" if ef > es else "down"
        except Exception:
            pass
        out.append(row)
    return out


_BRIEF_SYSTEM = """You are a crypto/macro desk analyst writing a pre-market brief.

You receive THREE inputs:
  1. `rss_headlines`  — crypto + finance outlets (CoinDesk, Cointelegraph, Yahoo Finance…)
  2. `telegram_posts` — fast breaking-news channels, heavy on macro/geopolitics
  3. `market`         — exchange facts: price, 24h move/high/low, RSI, 1h EMA trend

Weigh all three. Geopolitics and macro (wars, rates, sanctions, oil, dollar) move crypto
risk appetite, so treat telegram_posts as real signal, not noise — but a single
unconfirmed post is weaker evidence than the same story across several sources.

Rules:
- Use ONLY the supplied inputs. Never invent a story, number, or source.
- Every catalyst must map to a real supplied item; copy its `source` verbatim.
- Any price/level you cite must come from the `market` block. Never guess one.
- The bias is a CONTEXTUAL LEAN for the session, not a trade instruction.
- Be honest: if news and technicals disagree, say "neutral" with low confidence.
  A forced directional call is worse than admitting the setup is unclear.

Return ONLY this JSON:
{
  "headline": "one sentence, max 110 chars, capturing the day's setup",
  "bias": {
    "direction": "bullish|bearish|neutral",
    "confidence": 0-100,
    "rationale": "one sentence: what drives this lean, news AND technicals",
    "invalidation": "the specific level or event that would flip this view",
    "support": <number from market block or null>,
    "resistance": <number from market block or null>
  },
  "catalysts": [
    {"title": "the story", "source": "source name",
     "impact": "bullish|bearish|neutral",
     "why": "one short sentence on why a trader should care"}
  ],
  "themes": ["2-5 word theme", "..."],
  "watch": ["specific level or event to watch today", "..."],
  "risk": "one sentence on the main risk to positions today"
}
catalysts: max 5, ordered by importance. watch: max 4. themes: max 4.

confidence guide: 70+ only when news and technicals point the same way and the
catalysts are corroborated; 40-69 for a mild lean; below 40 for genuinely unclear.

Each `watch` item must be actionable: cite a concrete level from the `market` block
(e.g. "BTC reclaim of 64,790 24h high", "ETH holds 1,880") or a dated event from the
inputs. Never write a bare "BTC price" or "watch the market"."""


async def get_brief(force: bool = False) -> Optional[dict]:
    """AI summary of the day's news so far (cached; `force` regenerates).

    Deliberately manual/cached rather than per-page-load: each generation costs
    provider tokens, and the underlying headlines only move every few minutes.
    """
    from bot import ai_brain  # local import: avoids a cycle at module load

    if not settings.news_enabled:
        return None
    ttl = settings.news_brief_ttl_sec
    if not force and _brief_cache["data"] and (time.time() - _brief_cache["ts"]) < ttl:
        return _brief_cache["data"]

    async with _brief_lock:
        if not force and _brief_cache["data"] and (time.time() - _brief_cache["ts"]) < ttl:
            return _brief_cache["data"]
        # Both streams feed the brief, kept LABELLED so the model can weigh a fast
        # unconfirmed channel post differently from a published outlet story.
        stories, tg = await asyncio.gather(get_news(), get_telegram())
        if not stories and not tg:
            return _brief_cache["data"]

        # Rolling 24h rather than the UTC calendar day: at 06:00 UTC a calendar-day
        # filter throws away most of the overnight session, which is exactly the news
        # a pre-market brief is about.
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

        def _recent(items, cap):
            fresh = [s for s in items
                     if s.get("published") and _parse_date(s["published"]) >= cutoff]
            return (fresh or items)[:cap]

        use_rss, use_tg = _recent(stories, 40), _recent(tg, 25)
        market = await _market_snapshot()
        payload = {
            "utc_now": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
            "market": market,
            "rss_headlines": [{"source": s["source"], "title": s["title"],
                               "sentiment": s["sentiment"]} for s in use_rss],
            "telegram_posts": [{"source": p["source"], "title": p["title"],
                                "sentiment": p["sentiment"]} for p in use_tg],
        }
        use = [*use_rss, *use_tg]
        # Gemini writes the brief; Claude (API, then local CLI) catches failures.
        if not ai_brain.available():
            logger.warning("brief skipped — no AI provider configured")
            return _brief_cache["data"]

        out = await asyncio.to_thread(
            ai_brain.complete_json, _BRIEF_SYSTEM, payload, "TODAY'S NEWS:\n",
            ai_brain.BRIEF_PROVIDERS)
        if not out or not out.get("headline"):
            logger.warning("brief generation returned nothing usable")
            return _brief_cache["data"]

        def _lvl(v):
            """Accept a level only if it's a plausible number (model may send null/text)."""
            try:
                f = float(v)
                return round(f, 2) if f > 0 else None
            except (TypeError, ValueError):
                return None

        raw_bias = out.get("bias") or {}
        try:
            conf = max(0, min(100, int(float(raw_bias.get("confidence") or 0))))
        except (TypeError, ValueError):
            conf = 0
        bias = {
            "direction": (raw_bias.get("direction")
                          if raw_bias.get("direction") in ("bullish", "bearish", "neutral")
                          else "neutral"),
            "confidence": conf,
            "rationale": str(raw_bias.get("rationale") or "")[:240],
            "invalidation": str(raw_bias.get("invalidation") or "")[:200],
            "support": _lvl(raw_bias.get("support")),
            "resistance": _lvl(raw_bias.get("resistance")),
        }

        brief = {
            "headline": str(out.get("headline") or "")[:200],
            "bias": bias,
            "catalysts": [{
                "title": str(c.get("title") or "")[:180],
                "source": str(c.get("source") or "")[:40],
                "impact": (c.get("impact") if c.get("impact") in ("bullish", "bearish", "neutral") else "neutral"),
                "why": str(c.get("why") or "")[:200],
            } for c in (out.get("catalysts") or [])[:5] if c.get("title")],
            "themes": [str(t)[:40] for t in (out.get("themes") or [])[:4]],
            "watch": [str(w)[:140] for w in (out.get("watch") or [])[:4]],
            "risk": str(out.get("risk") or "")[:220],
            "market": market,          # code-computed, not model output
            "via": out.get("_via"),
            "articles_used": len(use),
            "rss_used": len(use_rss),
            "telegram_used": len(use_tg),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        _brief_cache.update(data=brief, ts=time.time())
        try:
            await db.news_cache.update_one(
                {"_id": "brief"}, {"$set": {"data": brief, "ts": _brief_cache["ts"]}}, upsert=True)
        except Exception:
            pass
        return brief


async def brief_from_cache() -> Optional[dict]:
    """Last generated brief (in-process, else the persisted copy). Never calls the AI."""
    if _brief_cache["data"]:
        return _brief_cache["data"]
    try:
        doc = await db.news_cache.find_one({"_id": "brief"})
        if doc and doc.get("data"):
            _brief_cache.update(data=doc["data"], ts=float(doc.get("ts") or 0.0))
            return _brief_cache["data"]
    except Exception:
        pass
    return None


def news_updated_at() -> Optional[str]:
    return datetime.fromtimestamp(_news_cache["ts"], timezone.utc).isoformat() if _news_cache["ts"] else None


def telegram_updated_at() -> Optional[str]:
    return datetime.fromtimestamp(_tg_cache["ts"], timezone.utc).isoformat() if _tg_cache["ts"] else None


def calendar_updated_at() -> Optional[str]:
    return datetime.fromtimestamp(_cal_cache["ts"], timezone.utc).isoformat() if _cal_cache["ts"] else None


# --------------------------------------------------------------------------- #
#  AI context + trading blackout
# --------------------------------------------------------------------------- #
def _upcoming_high_impact(events: list[dict], within_hours: float) -> list[dict]:
    now = datetime.now(timezone.utc).timestamp()
    horizon = now + within_hours * 3600
    # from ~5 min ago (just-released, still moving) out to the horizon
    return [e for e in events
            if e["impact_rank"] >= 3 and _is_relevant(e) and (now - 300) <= e["ts"] <= horizon]


async def ai_context(symbol: str = "") -> Optional[dict]:
    """Compact news+events block for the AI snapshot, or None if disabled/empty."""
    if not (settings.news_enabled and settings.news_ai_context):
        return None
    news, cal = await asyncio.gather(get_news(), get_calendar())
    now = datetime.now(timezone.utc).timestamp()
    up = _upcoming_high_impact(cal, settings.news_lookahead_hours)
    events = [{
        "event": e["title"], "currency": e["currency"],
        "in_min": round((e["ts"] - now) / 60),
        "forecast": e["forecast"], "previous": e["previous"],
    } for e in up[:6]]
    heads = [{"source": s["source"], "title": s["title"], "sentiment": s["sentiment"]} for s in news[:8]]
    # The day brief already synthesises RSS + Telegram + technicals into one directional
    # read. Passing it through means the trading model sees that conclusion instead of
    # re-deriving it from raw headlines every tick. Stale briefs are dropped.
    day_bias = None
    try:
        b = await brief_from_cache()
        if b and b.get("bias"):
            age_h = (datetime.now(timezone.utc)
                     - _parse_date(b.get("generated_at"))).total_seconds() / 3600
            if age_h <= 12:
                day_bias = {**b["bias"], "headline": b.get("headline"),
                            "age_hours": round(age_h, 1)}
    except Exception:
        pass

    if not events and not heads and not day_bias:
        return None
    tone = sum(1 if s["sentiment"] == "bullish" else -1 if s["sentiment"] == "bearish" else 0
               for s in news[:12])
    return {
        "day_bias": day_bias,
        "upcoming_high_impact": events,
        "latest_headlines": heads,
        "headline_tone": "bullish" if tone > 1 else "bearish" if tone < -1 else "mixed",
        "note": ("High-impact releases cause sharp, whippy volatility — be cautious opening a "
                 "trade in the minutes right before one. Let headline tone gently adjust "
                 "conviction; never override clean structure with a headline. `day_bias` is "
                 "the desk's multi-source read for the session: treat it as supporting or "
                 "opposing evidence for your own analysis, weighted by its confidence — "
                 "it is context, not an instruction."),
    }


async def in_blackout() -> tuple[bool, Optional[dict]]:
    """True if a High-impact (relevant-currency) event is within news_blackout_min minutes
    (before OR after) of now — the scheduler uses this to skip opening NEW entries.
    Existing positions are untouched (they keep their exchange SL/TP)."""
    if not settings.news_enabled or settings.news_blackout_min <= 0:
        return False, None
    cal = await get_calendar()
    now = datetime.now(timezone.utc).timestamp()
    window = settings.news_blackout_min * 60
    for e in cal:
        if e["impact_rank"] >= 3 and _is_relevant(e) and abs(e["ts"] - now) <= window:
            return True, e
    return False, None
