"""Fresh start for stats & learning; open positions keep running. Dry run by default: python reset_stats.py [--yes]"""
import asyncio
import sys
from datetime import datetime, timezone

from bot.delta_client import DeltaClient
from db import db

WIPE = ("trade_logs", "trade_outcomes", "strategy_perf", "shadow_strategy_perf", "agent_perf",
        "bot_meta", "trade_paths", "risk_params")
# Kept: bot_state (open trades stay managed), symbol_meta (product ids), news_cache, funding_history.


async def main(apply: bool):
    now = datetime.now(timezone.utc)
    held = {p["product_symbol"] for p in await DeltaClient().get_positions() if p.get("size")}
    opened = [s["opened_at"] async for s in db.bot_state.find({}, {"opened_at": 1})
              if s["_id"] in held and s.get("opened_at")]
    # Start the new window before any still-open trade, so its entry fills stay in the stats.
    cutoff = min([now] + [o if o.tzinfo else o.replace(tzinfo=timezone.utc) for o in opened])
    for c in WIPE:
        print(f"{c:22} {await db[c].count_documents({}):>7} docs to delete")
    print(f"open positions kept: {sorted(held) or 'none'}; stats cutoff: {cutoff.isoformat()}")
    if not apply:
        print("dry run — re-run with --yes to delete")
        return
    for c in WIPE:
        await db[c].delete_many({})
    await db.bot_meta.replace_one({"_id": "account"}, {"_id": "account", "reset_at": cutoff}, upsert=True)
    print("reset done")


asyncio.run(main("--yes" in sys.argv))
