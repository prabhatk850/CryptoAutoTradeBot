"""Headlines, Telegram, economic calendar and the cached AI day brief."""
import asyncio

from fastapi import APIRouter

from bot import ai_brain, news

router = APIRouter(prefix="/news", tags=["news"])


@router.get("/feed")
async def feed():
    return {"stories": await news.get_news(), "updated_at": news.news_updated_at()}


@router.get("/telegram")
async def telegram():
    return {"posts": await news.get_telegram(), "updated_at": news.telegram_updated_at()}


@router.get("/calendar")
async def calendar():
    return {"events": await news.get_calendar(), "updated_at": news.calendar_updated_at()}


@router.get("/brief")
async def brief(refresh: bool = False):
    """Cached brief; `refresh=true` regenerates it (spends provider tokens) and says if that failed."""
    if not refresh:
        data = await news.brief_from_cache()
        return {"brief": data} if data else {"brief": None, "never_generated": True}
    if not ai_brain.available(ai_brain.BRIEF_PROVIDERS):
        return {"brief": await news.brief_from_cache(),
                "error": "No AI provider is configured. Set GROQ_API_KEY or GEMINI_API_KEY in .env "
                         "and recreate the backend container."}
    # get_brief falls back to the cached brief on failure, so an unchanged timestamp means it failed.
    before = (await news.brief_from_cache() or {}).get("generated_at")
    data = await news.get_brief(force=True)
    if data and data.get("generated_at") == before:
        return {"brief": data, "stale": True,
                "error": "Refresh failed — every AI provider rejected the request "
                         "(rate limit / quota exhausted). Showing the last cached brief."}
    return {"brief": data}


@router.get("/all")
async def all_():
    """News + calendar + Telegram in one round-trip (what the dashboard polls)."""
    stories, events, posts = await asyncio.gather(news.get_news(), news.get_calendar(), news.get_telegram())
    return {
        "stories": stories,
        "events": events,
        "telegram": posts,
        "news_updated_at": news.news_updated_at(),
        "calendar_updated_at": news.calendar_updated_at(),
        "telegram_updated_at": news.telegram_updated_at(),
    }
