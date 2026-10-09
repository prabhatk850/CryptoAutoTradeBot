from bot.scheduler import _real_rr

# ETH 2026-09-21: planned 2.5R from mark 2774.72 (SL 2754.72, TP1 2824.7) but filled
# at 2782.3 — slippage leaves ~1.54R, below the 2.0 floor, so the gate must reject it.
assert round(_real_rr(True, 2774.72, 2754.72, 2824.7), 2) == 2.50
assert round(_real_rr(True, 2782.3, 2754.72, 2824.7), 2) == 1.54
# short mirrors
assert round(_real_rr(False, 100.0, 110.0, 80.0), 2) == 2.0
# zero risk never passes
assert _real_rr(True, 100.0, 100.0, 120.0) == 0.0
print("ok")
