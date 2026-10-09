"""Trailing stop + risk engine checks: python test_trailing.py"""
from bot.risk_engine import next_sl, simulate, train_from
from config import settings

FEE = 2.0  # fee buffer in price units

# LONG: basis 100, TP1 110, SL 95
assert next_sl(True, 100, 110, 95, 0, 104.9, False, 0.5, 0.5, FEE) is None                 # < 50% of the way
assert next_sl(True, 100, 110, 95, 0, 105, False, 0.5, 0.5, FEE)[:2] == (102.0, 1)         # 50% → BE + fees
assert next_sl(True, 100, 110, 102, 1, 108, False, 0.5, 0.5, FEE) is None                  # already at stage 1
assert next_sl(True, 100, 110, 102, 1, 108, True, 0.5, 0.5, FEE)[:2] == (105.0, 2)         # TP1 filled → lock 50%
assert next_sl(True, 100, 110, 106, 1, 108, True, 0.5, 0.5, FEE) is None                   # never backward (manual 106)
assert next_sl(True, 100, 110, 95, 0, 104, True, 0.5, 0.5, FEE)[:2] == (102.0, 1)          # lock would cross mark → BE
assert next_sl(True, 100, 110, 95, 0, 101.5, True, 0.5, 0.5, FEE) is None                  # BE would cross mark too
# SHORT mirror: basis 100, TP1 90, SL 105
assert next_sl(False, 100, 90, 105, 0, 95, False, 0.5, 0.5, FEE)[:2] == (98.0, 1)
assert next_sl(False, 100, 90, 98, 1, 92, True, 0.5, 0.5, FEE)[:2] == (95.0, 2)
assert next_sl(False, 100, 90, 94, 1, 92, True, 0.5, 0.5, FEE) is None

settings.trail_be_buffer_pct = 0.0
base = {"side": "buy", "basis": 100.0, "sl0": 95.0, "size": 2.0, "fee_rate": 0.0,
        "tps": [{"price": 110.0, "size": 1}, {"price": 120.0, "size": 1}]}
bar = lambda h, l, c: [0, c, h, l, c]  # noqa: E731
# Runs to 106 (trail to BE), fills TP1, retraces: half closes at 110 (+10), half stopped at the 105 lock (+5)
path = [bar(103, 99, 102), bar(106, 102, 106), bar(111, 105.5, 109), bar(109, 102, 103)]
assert simulate({**base, "path": path}, 0.5, 0.5) == (10 + 5) / (5 * 2)
assert simulate({**base, "path": path}, 0.9, 0.25) == (10 + 2.5) / (5 * 2)
# Stop checked before target inside one bar
assert simulate({**base, "path": [bar(111, 94, 100)]}, 0.5, 0.5) == -1.0
# SHORT that never trails: stopped at the original stop
short = {**base, "side": "sell", "sl0": 105.0, "tps": [{"price": 90.0, "size": 1}, {"price": 80.0, "size": 1}]}
assert simulate({**short, "path": [bar(101, 98, 99), bar(106, 99, 105)]}, 0.5, 0.5) == -1.0
# Fees reduce R
assert simulate({**base, "fee_rate": 0.001, "path": [bar(121, 100, 120)]}, 0.5, 0.5) < (10 + 20) / 10

# Training: an early trail wins on these paths, but nothing is adopted below min_trades
current = {"trigger": 0.8, "lock": 0.25, "trade_risk_pct": 3.0}
trades = [{**base, "path": path}] * 5
assert not train_from(trades, current)["adopted"]
settings.risk_engine_min_trades = 5
out = train_from(trades, current)
assert out["adopted"] and out["lock"] == 0.75 and 0.5 <= out["trade_risk_pct"] <= 3.0, out
print("ok")
