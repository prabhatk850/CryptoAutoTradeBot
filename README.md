# ForexBot — Automated Trading Dashboard

Paper trading bot using Delta Exchange Testnet, with a Next.js dashboard, Python/FastAPI backend, and MongoDB.

## Stack
| Layer | Tech |
|---|---|
| Frontend | Next.js 14 + Tailwind + Lightweight Charts |
| Backend | Python FastAPI + APScheduler |
| Database | MongoDB (Motor async) |
| Exchange | Delta Exchange (Testnet) |

---

## Quick Start (Local)

### 1. Get Delta Exchange Testnet API keys
1. Go to https://testnet.delta.exchange
2. Sign up for a free account
3. Go to **Profile → API Keys → Create Key**
4. Copy the API key and secret

### 2. Configure environment
```bash
cp .env.example .env
# Edit .env and fill in your DELTA_API_KEY and DELTA_API_SECRET
```

### 3. Run with Docker (easiest)
```bash
docker compose up --build
```
- Dashboard: http://localhost:3000
- API docs:  http://localhost:8000/docs

### 4. Run without Docker

**Backend:**
```bash
cd backend
python -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --reload
```

**Frontend:**
```bash
cd frontend
npm install
npm run dev
```

**MongoDB:** Install locally or use MongoDB Atlas free tier (update MONGO_URI in .env).

> **MongoDB Atlas / Delta note:** both lock access to an IP allowlist. If your public IP
> changes and data stops loading, add the new IP to Atlas → Network Access **and** to the
> Delta API key's whitelist (or set them unrestricted for testnet).

---

## 🤖 AI Brain (Claude-powered SL/TP + analysis)

Each tick, the bot sends a compact **multi-timeframe snapshot** (15m entry + 1h trend
candles, EMA/RSI/SuperTrend AI/trendline/FVG/IFVG, swing structure, ATR, strategy votes)
to **Claude**, which returns a structured trade plan: direction, a **structure-based
stop-loss** (below the invalidating swing / SuperTrend / gap edge — not a fixed distance),
**TP1/TP2/TP3**, confidence, and written reasoning.

**No Anthropic API key required** — it uses the local **Claude Code CLI** authenticated by
your Claude subscription (billed against your plan, not per-call). The bot auto-discovers
the Claude Code binary; set `CLAUDE_CLI_PATH` in `.env` to override.

Guardrails always hold regardless of what the AI says (in code, in `scheduler.py`):
- Risk-based sizing (0.5–1.5% of account per trade; AI confidence scales it)
- Stop clamped to `MIN/MAX_SL_PCT`; must be on the correct side of entry
- First TP must clear the **2R** floor; TPs must be progressive — else the trade is skipped
- Never trade against the 1h trend; concurrency cap; margin cap

Configure in `.env`:
```bash
AI_ENABLED=true
AI_MODEL=claude-opus-4-8      # or claude-sonnet-4-6 (cheaper/faster), claude-haiku-4-5-...
AI_MODE=decide                # decide = AI picks entry+SL+TP · refine = AI sets SL/TP only · advisory = log only
AI_MIN_CONFIDENCE=0.55        # below this the AI trade becomes HOLD
CLAUDE_CLI_PATH=              # blank = auto-discover
```
If the CLI is missing/times out, the bot cleanly **falls back to the mechanical engine**.
AI-driven trades show a 🤖 badge in Order History; per-trade reasoning is stored in the log.

### MCP server (chat with the bot from Claude Desktop / Claude Code)
`mcp_server/server.py` exposes the running backend as MCP tools — `analyze_symbol`,
`get_indicators`, `get_pnl`, `get_positions`, `recent_trades`, `bot_status`, `start_bot`,
`stop_bot`. Register it (backend must be running):

```bash
claude mcp add forexbot -- <backend/venv python> <repo>/mcp_server/server.py
# or paste mcp_server/claude_desktop_config.example.json into your Claude Desktop config
```
Then ask: *"analyze BTCUSD right now"*, *"show my open positions and P&L"*.
The MCP tools are **read-only for the market** (they never place orders directly).

---

## How the Bot Works

Every 5 minutes the bot:
1. Fetches the latest 100 candles from Delta Exchange
2. Calculates EMA(9), EMA(21), RSI(14), and trendline breakout
3. Runs the strategy (needs 2/3 signals to agree before trading)
4. Places a paper trade order via the Testnet API if BUY or SELL
5. Logs every decision to MongoDB — visible in the dashboard

### Strategy Rules
| Signal | BUY | SELL |
|---|---|---|
| EMA | Fast(9) crosses above Slow(21) | Fast(9) crosses below Slow(21) |
| RSI | RSI < 30 (oversold) | RSI > 70 (overbought) |
| Breakout | Price closes above resistance | Price closes below support |

**2 out of 3 signals must agree** to place a trade. Tune this in `backend/bot/strategy.py`.

---

## Deploying to Railway (free tier)

1. Push this repo to GitHub
2. Go to https://railway.app → New Project → Deploy from GitHub
3. Add 3 services: **MongoDB** (plugin), **backend**, **frontend**
4. Set environment variables in Railway's dashboard (same as .env)
5. Done — Railway gives you a public URL

## Deploying to DigitalOcean ($5/month droplet)

```bash
# On your droplet:
git clone <your-repo>
cd forex-bot
cp .env.example .env && nano .env   # fill in keys
docker compose up -d --build
```

---

## Going Live (Real Money — Read Carefully)

When you're ready to trade real money:
1. Get a **live** Delta Exchange account and API keys
2. Change `DELTA_BASE_URL` in .env to `https://api.delta.exchange`
3. Change `paper_trade: True` to `False` in `backend/bot/scheduler.py`
4. Start with the **smallest position size** allowed
5. Set a daily loss limit — stop the bot if your account drops 5%

⚠️ **Paper trade for at least 30 days before going live.**

---

## File Structure
```
forex-bot/
├── backend/
│   ├── main.py              ← FastAPI app entry point
│   ├── config.py            ← All settings from .env
│   ├── db.py                ← MongoDB connection
│   ├── bot/
│   │   ├── delta_client.py  ← Delta Exchange API wrapper
│   │   ├── indicators.py    ← EMA, RSI, Breakout calculations
│   │   ├── strategy.py      ← BUY/SELL/HOLD decision logic
│   │   └── scheduler.py     ← APScheduler bot loop (every 5m)
│   └── routers/
│       ├── bot.py           ← /bot/start, /bot/stop, /bot/status
│       ├── trades.py        ← /trades/logs, /trades/stats
│       └── market.py        ← /market/ticker, /market/candles
├── frontend/
│   ├── app/page.tsx         ← Main dashboard page
│   ├── components/
│   │   ├── TradingViewChart.tsx  ← Candlestick chart
│   │   ├── BotControl.tsx        ← Start/stop bot UI
│   │   ├── TradeLog.tsx          ← Decision history table
│   │   ├── StatCard.tsx          ← Metric cards
│   │   └── IndicatorBar.tsx      ← Live RSI/EMA badges
│   └── lib/api.ts           ← All API calls
├── docker-compose.yml
└── .env.example
```
