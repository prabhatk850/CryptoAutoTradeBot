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
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional
from xml.etree import ElementTree as ET

import httpx

from config import settings

logger = logging.getLogger("bot.news")

_UA = "Mozilla/5.0 (compatible; ForexBot/1.0)"
_ATOM = "{http://www.w3.org/2005/Atom}"
IMPACT_RANK = {"high": 3, "medium": 2, "low": 1, "holiday": 0, "": 0}

# in-process caches: {"data": [...], "ts": epoch_seconds}
_news_cache: dict = {"data": [], "ts": 0.0}
_cal_cache: dict = {"data": [], "ts": 0.0}
_news_lock = asyncio.Lock()
_cal_lock = asyncio.Lock()

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


def _parse_calendar(data: list) -> list[dict]:
    currencies = {c.strip().upper() for c in (settings.news_currencies or "").split(",") if c.strip()}
    out = []
    for e in data or []:
        if not isinstance(e, dict):
            continue
        cur = str(e.get("country", "")).upper()
        if currencies and cur not in currencies:
            continue
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
_RETRY_AFTER_FAIL = 60  # seconds to wait before re-fetching after a failed/empty cycle


def _due(cache: dict, ttl: float) -> bool:
    """Time-based (not data-based) so the failure backoff applies even on a cold start."""
    return (time.time() - cache["ts"]) >= ttl


def _mark_success(cache: dict, data: list) -> None:
    cache["data"], cache["ts"] = data, time.time()


def _mark_failure(cache: dict, ttl: float) -> None:
    """Keep the last good data; only allow a retry in _RETRY_AFTER_FAIL seconds — this
    is what stops us from hammering a rate-limited (HTTP 429) endpoint every tick."""
    cache["ts"] = time.time() - max(0.0, ttl - _RETRY_AFTER_FAIL)


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
        _mark_success(_news_cache, merged) if merged else _mark_failure(_news_cache, ttl)
        return _news_cache["data"]


async def get_calendar(force: bool = False) -> list[dict]:
    if not settings.news_enabled:
        return []
    ttl = settings.news_calendar_refresh_sec
    if not force and not _due(_cal_cache, ttl):
        return _cal_cache["data"]
    async with _cal_lock:
        if not force and not _due(_cal_cache, ttl):
            return _cal_cache["data"]
        data = []
        async with httpx.AsyncClient() as client:
            try:
                r = await client.get(settings.news_calendar_url, headers={"User-Agent": _UA},
                                     follow_redirects=True, timeout=15)
                if r.status_code == 200:
                    data = r.json()
                else:  # 429 rate-limit etc. — back off, keep last good data
                    logger.warning(f"calendar fetch: HTTP {r.status_code}")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"calendar fetch failed: {type(e).__name__}")
        events = await asyncio.to_thread(_parse_calendar, data) if data else []
        _mark_success(_cal_cache, events) if events else _mark_failure(_cal_cache, ttl)
        return _cal_cache["data"]


def news_updated_at() -> Optional[str]:
    return datetime.fromtimestamp(_news_cache["ts"], timezone.utc).isoformat() if _news_cache["ts"] else None


def calendar_updated_at() -> Optional[str]:
    return datetime.fromtimestamp(_cal_cache["ts"], timezone.utc).isoformat() if _cal_cache["ts"] else None


# --------------------------------------------------------------------------- #
#  AI context + trading blackout
# --------------------------------------------------------------------------- #
def _upcoming_high_impact(events: list[dict], within_hours: float) -> list[dict]:
    now = datetime.now(timezone.utc).timestamp()
    horizon = now + within_hours * 3600
    # from ~5 min ago (just-released, still moving) out to the horizon
    return [e for e in events if e["impact_rank"] >= 3 and (now - 300) <= e["ts"] <= horizon]


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
    if not events and not heads:
        return None
    tone = sum(1 if s["sentiment"] == "bullish" else -1 if s["sentiment"] == "bearish" else 0
               for s in news[:12])
    return {
        "upcoming_high_impact": events,
        "latest_headlines": heads,
        "headline_tone": "bullish" if tone > 1 else "bearish" if tone < -1 else "mixed",
        "note": ("High-impact releases cause sharp, whippy volatility — be cautious opening a "
                 "trade in the minutes right before one. Let headline tone gently adjust "
                 "conviction; never override clean structure with a headline."),
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
        if e["impact_rank"] >= 3 and abs(e["ts"] - now) <= window:
            return True, e
    return False, None
