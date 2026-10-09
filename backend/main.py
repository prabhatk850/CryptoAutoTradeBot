import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from bot.scheduler import start_bot
from config import settings
from db import db
from routers import bot, trades, market, news

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # A Mongo outage must not stop the API or the bot from booting.
    try:
        await db.trade_logs.create_index([("timestamp", -1)])
        await db.trade_logs.create_index([("action", 1)])
        await db.trade_logs.create_index([("action", 1), ("timestamp", 1)])  # decision index: filter + sort
        await db.funding_history.create_index([("symbol", 1), ("ts", -1)])  # queried every deep tick
    except Exception as e:
        logger.error(f"Mongo index creation skipped (DB unreachable): {e}")
    if settings.auto_start_bot:
        try:
            start_bot()
            logger.info("Auto-started trading bot on boot.")
        except Exception as e:
            logger.error(f"Auto-start failed: {e}")
    yield


app = FastAPI(title="ForexBot API", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
for r in (bot, trades, market, news):
    app.include_router(r.router)


@app.get("/health")
async def health():
    return {"status": "ok"}
