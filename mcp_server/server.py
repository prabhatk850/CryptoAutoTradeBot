"""
ForexBot MCP server.

Exposes the running ForexBot FastAPI backend as MCP tools so an AI agent
(Claude Desktop, Claude Code, Cursor, ...) can control and inspect the bot
in natural language.

This server is a thin client over the HTTP backend — the backend
(uvicorn main:app) must be running. It does NOT open its own DB connection
or scheduler, so there is exactly one source of truth.

Run (stdio transport, for Claude Desktop):
    python mcp_server/server.py

Configure the backend URL with FOREXBOT_API_URL (default http://localhost:8000).
"""
import os
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

API_URL = os.environ.get("FOREXBOT_API_URL", "http://localhost:8000").rstrip("/")
TIMEOUT = 20

mcp = FastMCP("forexbot")


async def _request(method: str, path: str, **kwargs) -> Any:
    """Call the backend and return parsed JSON (or a structured error dict)."""
    url = f"{API_URL}{path}"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.request(method, url, **kwargs)
            try:
                body = resp.json()
            except Exception:
                body = {"raw": resp.text}
            if resp.status_code >= 400:
                return {"ok": False, "status": resp.status_code, "error": body}
            return body
    except httpx.ConnectError:
        return {
            "ok": False,
            "error": f"Cannot reach ForexBot backend at {API_URL}. "
                     "Start it with: uvicorn main:app (from the backend/ folder).",
        }
    except Exception as e:  # pragma: no cover - defensive
        return {"ok": False, "error": str(e)}


@mcp.tool()
async def start_bot() -> dict:
    """Start the trading bot. It begins checking the market every few minutes
    and places paper trades on Delta Exchange testnet when signals agree."""
    return await _request("POST", "/bot/start")


@mcp.tool()
async def stop_bot() -> dict:
    """Stop the trading bot. No further market checks or orders will be made."""
    return await _request("POST", "/bot/stop")


@mcp.tool()
async def bot_status() -> dict:
    """Get the bot's current status: whether it is running, the trading symbol,
    the check interval, and the next scheduled run time."""
    return await _request("GET", "/bot/status")


@mcp.tool()
async def get_stats() -> dict:
    """Get aggregate trading statistics: total decisions, number of BUY / SELL /
    HOLD decisions, the latest traded price, and the trading symbol."""
    return await _request("GET", "/trades/stats")


@mcp.tool()
async def recent_trades(limit: int = 20) -> dict:
    """Get the most recent bot decisions (BUY / SELL / HOLD) with their reasons,
    prices, indicator snapshots, and order status.

    Args:
        limit: How many recent decisions to return (1-500). Default 20.
    """
    limit = max(1, min(int(limit), 500))
    return await _request("GET", f"/trades/logs?limit={limit}")


@mcp.tool()
async def get_ticker(symbol: str | None = None) -> dict:
    """Get the live market ticker (price, mark price, volume) for a symbol.

    Args:
        symbol: Delta Exchange symbol, e.g. 'BTCUSDT'. Defaults to the bot's
                configured trading symbol if omitted.
    """
    path = "/market/ticker" + (f"?symbol={symbol}" if symbol else "")
    return await _request("GET", path)


@mcp.tool()
async def get_wallet() -> dict:
    """Get the testnet wallet balance. Requires valid Delta API keys in the
    backend's .env — returns an error object if the keys are missing/invalid."""
    return await _request("GET", "/market/wallet")


@mcp.tool()
async def get_positions() -> dict:
    """Get currently open positions on the Delta testnet account. Requires valid
    Delta API keys — returns an error object if the keys are missing/invalid."""
    return await _request("GET", "/market/positions")


@mcp.tool()
async def analyze_symbol(symbol: str | None = None) -> dict:
    """Run the AI (Claude) trade analysis for a symbol RIGHT NOW and return a plan:
    action (BUY/SELL/HOLD), structure-based stop-loss, TP1/TP2/TP3, confidence, and
    written reasoning — plus the multi-timeframe snapshot it reasoned over. Read-only,
    places no order. Use this to sanity-check or explain the bot's view of the market.

    Args:
        symbol: e.g. 'BTCUSD' or 'ETHUSD'. Defaults to the bot's configured symbol.
    """
    path = "/market/analyze" + (f"?symbol={symbol}" if symbol else "")
    return await _request("GET", path)


@mcp.tool()
async def get_indicators(symbol: str | None = None, resolution: int = 15) -> dict:
    """Get computed indicators + LuxAlgo overlays (EMA, RSI, SuperTrend AI, trendline,
    FVG/IFVG) for a symbol/timeframe.

    Args:
        symbol: e.g. 'BTCUSD'. Defaults to the bot's configured symbol.
        resolution: timeframe in minutes (1, 5, 15, 60, 240, 1440). Default 15.
    """
    q = f"?resolution={int(resolution)}" + (f"&symbol={symbol}" if symbol else "")
    return await _request("GET", f"/market/indicators{q}")


@mcp.tool()
async def get_pnl(symbol: str | None = None) -> dict:
    """Get realized/unrealized P&L, win rate, and open-position protection (live SL/TP
    and current AI reasoning) for a symbol from the Delta account.

    Args:
        symbol: e.g. 'BTCUSD'. Defaults to the bot's configured symbol.
    """
    path = "/trades/pnl" + (f"?symbol={symbol}" if symbol else "")
    return await _request("GET", path)


if __name__ == "__main__":
    # stdio transport — what Claude Desktop / Claude Code launch.
    mcp.run()
