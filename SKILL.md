---
name: forexbot-no-fabricated-data
description: Rules for this trading bot's failure paths. Use when touching anything that reads from Delta Exchange, computes P/L, places or cancels orders, or renders numbers on the dashboard. Triggers on "wrong P/L", "inconsistent data", "position shows 0", "SL disappeared", "stale", "out of sync".
---

# Never fabricate a number you don't have

Every data bug in this bot between 2026-09-19 and 2026-09-24 had one root cause:
**a failure path substituted a plausible-looking value instead of admitting the call failed.**
The venue (Delta testnet) returns HTTP 500 on `/v2/products` and `/v2/tickers` for hours,
and has served 2-day-old tickers with HTTP 200. Code that treats "couldn't ask" as an answer
turns that outage into silent corruption.

## The incidents

| Call failed | Fabricated | Damage | Now |
|---|---|---|---|
| `get_ticker` | `mark = entry` | live position shown flat at `$-0.00 (0%)` | `_resolve_mark` → `None` + `mark_source` |
| `get_ticker` (stale 200) | 2-day-old mark | phantom P/L; fast loop could fire on it | `get_ticker` raises past `_TICKER_MAX_AGE` |
| `get_product` | `contract_value = 0.001`, cached | ETH P/L 10× low for the process lifetime | `get_contract_value` raises if unknown |
| `get_positions` | `size = 0.0` | read "flat", cancelled live SLs, deleted state | `get_position_size` raises |
| `cancel_order` | `{}`, status unchecked | orphan stop shown as the current SL | `cancel_order` raises |
| brief providers | cached brief as fresh, HTTP 200 | week-old brief looked current | `/news/brief` returns `stale: true` |
| `get_position` after entry | `fill_price = mark` | fake "zero slippage" fill | `fill_price = None` |
| `docker push \| tail` | `tail`'s exit code | "ALL PUSHED" while two tags failed | check the real status |

## Rules

1. **A read that fails must raise, not return a default.** `0`, `0.0`, `entry`, `0.001`, `{}`
   are answers. Every caller sits in a try/except, so raising degrades to *skip this tick*.
2. **Never cache a fallback.** Cache only what the venue returned (`DeltaClient._cached`, `_meta`).
3. **Static facts persist to Mongo.** `product_id` and `contract_value` go through
   `DeltaClient._product_fact`: memory → venue → `symbol_meta`. Without `product_id` the bot can't
   place, cancel or close anything, even while the orders API is up.
4. **Prefer a live second source, and expire memory.** Mark resolves ticker → book mid →
   mark candle → last seen within `_MARK_TTL` → `None` (`routers/trades.py::_resolve_mark`).
5. **A destructive branch needs positive confirmation.** Cancelling stops or deleting state on
   "flat" requires *knowing* it's flat. `cancel_reduce_only(..., best_effort=True)` is only for
   user-initiated closes/SL moves; the bot's own paths must stop on a failed cancel.
6. **Label uncertainty in the payload.** `mark_stale`, `mark_source`, `stale`, `verified`.
   The UI renders "—" rather than a confident wrong number.
7. **Don't pipe a command whose exit code matters.** `cmd | tail` returns `tail`'s status.

## Before you ship a change here

- Does any new `except` return a number that reaches money math or an order call?
- Would this behave differently during a 500 storm than in a green test?
- If it guesses, does the response say so?
- Money-path refactors: diff old vs new outputs on the same inputs (pure functions, and
  `_process_symbol` against a fake `DeltaClient` + DB) before saving into the live bot.

One runnable check per non-trivial money path (see `backend/test_real_rr.py`).
