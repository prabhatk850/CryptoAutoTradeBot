# ForexBot MCP Server

Control and inspect the ForexBot trading bot in natural language from any
MCP client (Claude Desktop, Claude Code, Cursor, ...).

It is a **thin HTTP client** over the FastAPI backend — the backend must be
running (`uvicorn main:app` in `../backend`). The MCP server holds no state,
no DB connection, and no second scheduler.

## Tools exposed

| Tool | Description |
|------|-------------|
| `start_bot` | Start the trading bot |
| `stop_bot` | Stop the trading bot |
| `bot_status` | Running state, symbol, interval, next run |
| `get_stats` | Totals: decisions, buys, sells, holds, latest price |
| `recent_trades(limit=20)` | Recent BUY/SELL/HOLD decisions + indicators |
| `get_ticker(symbol?)` | Live price for a symbol (default: bot's symbol) |
| `get_wallet` | Testnet wallet balance (needs valid Delta keys) |
| `get_positions` | Open positions (needs valid Delta keys) |

## Setup

The dependencies are already installed in the backend venv. If you ever need a
fresh install:

```powershell
c:\Users\cnd44\Desktop\bot\forex-bot\backend\venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Connect to Claude Desktop

1. Make sure the backend is running:
   ```powershell
   cd c:\Users\cnd44\Desktop\bot\forex-bot\backend
   .\venv\Scripts\Activate.ps1
   uvicorn main:app --reload
   ```
2. Open Claude Desktop config:
   `%APPDATA%\Claude\claude_desktop_config.json`
   (create it if it doesn't exist)
3. Merge in the contents of `claude_desktop_config.example.json` (this folder).
4. Fully quit and reopen Claude Desktop. The `forexbot` tools appear in the
   tools (plug) menu.

## Connect to Claude Code (CLI)

```powershell
claude mcp add forexbot -- c:\Users\cnd44\Desktop\bot\forex-bot\backend\venv\Scripts\python.exe c:\Users\cnd44\Desktop\bot\forex-bot\mcp_server\server.py
```

## Try it

> "Start the forexbot and show me its status."
> "Summarize the bot's last 20 decisions — were the RSI signals reasonable?"
> "What's BTCUSDT trading at right now?"

## Config

| Env var | Default | Purpose |
|---------|---------|---------|
| `FOREXBOT_API_URL` | `http://localhost:8000` | Where the backend is reachable |
