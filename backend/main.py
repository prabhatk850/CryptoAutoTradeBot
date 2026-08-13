import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from routers import bot, trades, market, news
from db import db
from config import settings
from bot.scheduler import start_bot

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create indexes on startup. Don't let a Mongo outage (e.g. IP not allowlisted)
    # block the whole app from booting — Delta endpoints and the bot must still come up.
    try:
        await db.trade_logs.create_index([("timestamp", -1)])
        await db.trade_logs.create_index([("action", 1)])
        # The decision index scans BUY/SELL sorted by time; a compound index serves
        # both the filter and the sort, so Mongo streams straight from the index.
        await db.trade_logs.create_index([("action", 1), ("timestamp", 1)])
    except Exception as e:
        logger.error(f"Mongo index creation skipped (DB unreachable): {e}")
    # Auto-start the bot so a backend restart doesn't silently leave it stopped.
    if settings.auto_start_bot:
        try:
            start_bot()
            logger.info("Auto-started trading bot on boot.")
        except Exception as e:
            logger.error(f"Auto-start failed: {e}")
    yield


app = FastAPI(title="ForexBot API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # tighten this in production
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(bot.router)
app.include_router(trades.router)
app.include_router(market.router)
app.include_router(news.router)


@app.get("/health")
async def health():
    return {"status": "ok"}
