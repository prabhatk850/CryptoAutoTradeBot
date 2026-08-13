import asyncio

from fastapi import APIRouter

from bot import news

router = APIRouter(prefix="/news", tags=["news"])


@router.get("/feed")
async def feed():
    """Latest aggregated forex/crypto headlines (newest-first, images stripped)."""
    stories = await news.get_news()
    return {"stories": stories, "updated_at": news.news_updated_at()}


@router.get("/calendar")
async def calendar():
    """This week's economic calendar (ForexFactory feed): currency, impact, forecast, previous."""
    events = await news.get_calendar()
    return {"events": events, "updated_at": news.calendar_updated_at()}


@router.get("/all")
async def all_():
    """News + calendar in one round-trip (what the dashboard polls)."""
    stories, events = await asyncio.gather(news.get_news(), news.get_calendar())
    return {
        "stories": stories,
        "events": events,
        "news_updated_at": news.news_updated_at(),
        "calendar_updated_at": news.calendar_updated_at(),
    }
