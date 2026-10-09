# ForexBot

Crypto-perpetuals trading bot on the Delta Exchange testnet: FastAPI backend, Next.js dashboard, MongoDB (Atlas).

## Run

```bash
cp .env.example .env            # fill in Delta keys, MONGO_URI, an AI key
docker compose up -d --build    # local dev: base + override (source mounted, hot reload)
```

- Dashboard: http://localhost:4000 (calls `/api`, proxied server-side to the backend)
- API docs: http://localhost:8000/docs

| File | Purpose |
|---|---|
| `docker-compose.yml` | Base stack from published images — what a server runs |
| `docker-compose.override.yml` | Local dev, merged automatically: builds from source, hot reload |

**Server deploy** (only port 4000 needs to be public):

```bash
docker compose build && docker compose push                                        # on the dev machine
docker compose -f docker-compose.yml pull && docker compose -f docker-compose.yml up -d   # on the server
```

**Gotchas**
- `.env` is read when a container is *created*: `docker compose up -d --force-recreate backend` after editing it.
- In dev every saved backend file reloads the **live** bot; add a new module before the line that imports it.
- Want a local Mongo instead of Atlas? `docker compose --profile localdb up -d` and `MONGO_URI=mongodb://mongodb:27017/forexbot`.
- Without Docker: `scripts/run_backend.ps1` and `scripts/run_frontend.ps1` (see `scripts/README.md`).

## How it trades

Two loops in `backend/bot/scheduler.py`:

- **Deep tick** (`CHECK_INTERVAL_SECONDS`, 120s): for each symbol, analyze 1h (bias) / 15m (decision) / 5m (timing),
  collect strategy votes (`bot/strategies.py`), let the learning agent ensemble decide (`bot/ensemble.py`;
  the AI in `bot/ai_brain.py` is a fallback, at most every `AI_MIN_INTERVAL_SEC`), then run entry guards
  (position cap, liquidity, news blackout, daily loss, backtested expectancy, real fill R:R) and place a market entry
  with reduce-only TP/SL stops. Every decision is logged to `trade_logs`.
- **Fast tick** (`FAST_CHECK_SECONDS`, 15s): manages open positions (breakeven after TP1, cleanup when flat) and fires
  entries the deep tick *armed* once price crosses the AI's trigger — through the same guarded entry path.

**3-5-7 rule** (`RULE_357_ENABLED`): each trade risks ≤ 3% of balance, all open trades ≤ 5% (size is cut to fit,
or the entry is skipped), and once realized P/L since 6PM IST reaches +7% the bot stops for the day. Pressing Start
overrides that for the session; every day at 6PM IST the session resets and the bot auto-starts.

**Trailing stop** (`TRAIL_*`): at 50% of the way to TP1 the stop moves to entry + round-trip fees; once TP1
fills it locks 50% of TP1's profit. It only ever tightens, the new stop is placed before the old one is cancelled,
and each move is logged as `trail SL [why] prev → new`.

**Risk engine** (`bot/risk_engine.py`): every verified closed trade is stored with its 1m mark path (`trade_paths`).
At each 6PM IST reset (or `POST /bot/risk-engine/train`) it replays them under different trail settings and
adopts the best once 20+ trades back it. It also sets per-trade risk by half-Kelly, never above the 3% cap.
Results appear under `risk_engine` in `/bot/training`.

**Fresh stats**: `docker exec forex_backend python reset_stats.py` (dry run) then `--yes`. This wipes logs,
outcomes and learning, keeps open positions, and makes the dashboard ignore Delta fills before the cutoff.

Closed trades are scored mark→mark (trains the strategy weights in `bot/autotune.py`) and on real fills
(execution quality only). See `/bot/training`.

AI provider chains are constants at the top of `bot/ai_brain.py`. The personal Claude subscription is never used.

## Layout

```
backend/
  main.py, config.py (every setting + default), db.py
  bot/       scheduler (loops + execution), ai_brain, strategies, indicators, lux_indicators, smc, swings,
             divergence, backtest, edge, autotune, funding, orderbook, portfolio_risk, news, delta_client
  routers/   bot, trades (P/L from real fills), market, news
  test_real_rr.py
frontend/    app/page.tsx, components/, lib/api.ts, next.config.mjs (/api proxy)
mcp_server/  MCP tools over the HTTP API (see its README)
SKILL.md     rules for failure paths: never fabricate a number
```

## Going live

Testnet only today. Going live means a live Delta account and keys, `DELTA_BASE_URL=https://api.delta.exchange`,
small size, and the daily loss limit on. Paper trade for at least 30 days first.
