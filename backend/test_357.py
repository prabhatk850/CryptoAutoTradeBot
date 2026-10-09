"""3-5-7 rule checks: python test_357.py"""
import asyncio
from datetime import datetime, timedelta

from bot import scheduler as sch
from bot.scheduler import IST, _risk_capped_lots, session_start

# Session flips at 18:00 IST.
assert session_start(datetime(2026, 10, 9, 17, 59, tzinfo=IST)) == datetime(2026, 10, 8, 18, 0, tzinfo=IST)
assert session_start(datetime(2026, 10, 9, 18, 0, tzinfo=IST)) == datetime(2026, 10, 9, 18, 0, tzinfo=IST)

# $1000 balance: 3% = $30 per trade, 5% = $50 across open trades.
assert _risk_capped_lots(100, 10.0, 1000, 0) == 3      # per-trade cap
assert _risk_capped_lots(100, 10.0, 1000, 40) == 1     # only $10 of open-risk room left
assert _risk_capped_lots(100, 10.0, 1000, 45) == 0     # no room -> skip
assert _risk_capped_lots(2, 10.0, 1000, 0) == 2        # never sizes up


class Delta:
    async def get_wallet(self): return {"balance": "1080"}   # started the session at $1000


class Meta:
    async def find_one(self, q): return None
    async def update_one(self, *a, **k): pass


class DB:
    bot_meta = Meta()


async def view(sym):
    start = session_start()
    return {"rows": [{"status": "Closed", "exit_time": start.replace(minute=30).isoformat(), "realized_pnl": 40.0},
                     {"status": "Closed", "exit_time": (start - timedelta(days=1)).isoformat(), "realized_pnl": 500.0},
                     {"status": "Open", "exit_time": None, "realized_pnl": None}]}


async def main():
    import routers.trades
    routers.trades._build_view = view
    sch._delta, sch.db = Delta(), DB()
    sch.settings.trade_symbols = "BTCUSD,ETHUSD"
    # +$80 today (2 symbols × $40) on a $1000 start = 8% ≥ 7% -> stop; yesterday's $500 ignored
    assert await sch._daily_target_hit() and sch._rule["realized"] == 80.0 and sch._rule["target"] == 70.0
    await sch.override_daily_target()                  # manual Start
    assert not await sch._daily_target_hit()
    sch.settings.daily_profit_target_pct = 9.0         # +8% is below a 9% target
    assert not await sch._daily_target_hit()

asyncio.run(main())
print("ok")
