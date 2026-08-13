from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

# Project root .env (one level above /backend) so it loads no matter the CWD.
_ROOT_ENV = Path(__file__).resolve().parent.parent / ".env"


class Settings(BaseSettings):
    delta_api_key: str = ""
    delta_api_secret: str = ""
    delta_base_url: str = "https://cdn-ind.testnet.deltaex.org"

    mongo_uri: str = "mongodb://localhost:27017/forexbot"

    trading_symbol: str = "BTCUSD"   # default/primary symbol (chart default)
    # Chart + analysis candles use the index-derived MARK price, not the thin
    # last-traded price. On the demo/testnet the traded feed prints fake wicks to
    # stale levels; mark-price candles are smooth and match the real market.
    use_mark_candles: bool = True
    # Symbols the bot actively trades SIMULTANEOUSLY (each opens + manages its own).
    trade_symbols: str = "BTCUSD,ETHUSD"
    max_concurrent_positions: int = 2  # cap total open positions across all coins
    trade_quantity: int = 1
    check_interval_minutes: int = 5
    # Minimum strategies that must agree (on the entry timeframe) to trade.
    min_signals: int = 2
    # Multi-timeframe: 1h sets bias, 15m is the DECISION timeframe (votes + entry),
    # 5m is analyzed for entry timing/confirmation only (never the decision TF).
    trend_timeframe: int = 60    # 1h — sets allowed direction (bias)
    entry_timeframe: int = 15    # 15m — the timeframe trades are decided on
    ltf_timeframe: int = 5       # 5m — lower timeframe analyzed for entry timing
    # Auto-start the trading bot when the backend boots (survives restarts).
    auto_start_bot: bool = True
    # Comma-separated strategy ids to vote on each trade. Add new ones here.
    # Options: EMA_CROSS, RSI, BREAKOUT, SUPERTREND_AI, TRENDLINE_NAV
    strategies: str = "EMA_CROSS,RSI,BREAKOUT,SUPERTREND_AI,TRENDLINE_NAV,FVG,IFVG,SMC,MACD"
    # Trendline Navigator swing term: Long / Medium / Short (Short = most responsive).
    trendline_term: str = "Medium"
    # Candles fetched per tick (needs enough history for long-swing indicators).
    candle_limit: int = 500

    # --- Risk / sizing (CAPITAL-BASED: each trade deploys a fixed % of capital as margin) ---
    leverage: int = 50                 # margin leverage (max; auto-lowered so SL stays inside liquidation)
    position_capital_pct: float = 50.0 # normal trade: use this % of balance as margin
    big_trade_capital_pct: float = 20.0# "big" trade (strong confluence): smaller margin -> fewer lots
    big_trade_min_agree: float = 0.7   # fraction of enabled strategies that must agree for a big trade
    liq_buffer_pct: float = 0.5        # SL must sit at least this % of price INSIDE the liquidation price
    risk_min_pct: float = 0.5          # (legacy risk-based knobs, kept for fallback symbols)
    risk_max_pct: float = 1.5
    margin_cap_pct: float = 0.5        # never use more than 50% of *available* balance as margin on ONE trade
    stop_loss_pct: float = 1.0         # fallback stop distance (%) if no SL candidate
    risk_reward: float = 2.0           # FLOOR reward:risk (never below 1:2)
    # --- Per-symbol POINT limits (ETH). Normal trades are tight; "big" trades widen SL/TP. ---
    eth_sl_min_pts: float = 5.0
    eth_sl_max_pts: float = 20.0       # normal ETH stop: at most 20 points
    eth_tp_min_pts: float = 50.0       # normal ETH target band: 50–60 points
    eth_tp_max_pts: float = 60.0
    eth_big_sl_max_pts: float = 60.0   # big ETH trade: wider stop
    eth_big_tp_min_pts: float = 120.0  # big ETH trade: larger target
    eth_big_tp_max_pts: float = 220.0
    # Circuit breaker: stop opening NEW trades once today's realized loss reaches this
    # % of account (existing positions keep their exchange SL/TP). 0 disables.
    daily_loss_limit_pct: float = 10.0
    # --- Stop-loss placement (combined ATR + SuperTrend + structure) ---
    atr_period: int = 14
    atr_k: float = 1.5                 # ATR-based stop = entry +/- k*ATR
    sl_lookback: int = 20              # bars for structure swing (entry TF)
    min_sl_pct: float = 0.3            # clamp stop distance to >= this % of price
    max_sl_pct: float = 1.5            # clamp stop distance to <= this % of price (no big stops)
    target_lookback: int = 30          # 1h bars used for the 1:2 feasibility check
    # --- Partial take-profits + breakeven ---
    # Never more than 2 TPs. Set max_tps=1 for a single target (tp_splits="1.0").
    max_tps: int = 2
    tp_splits: str = "0.5,0.5"         # close 50% at TP1, 50% at TP2 (2 TPs)
    move_be_after_tp: int = 1          # move SL to breakeven after TP{n} fills (1 = after TP1)
    # Only show FVG / IFVG zones on the chart whose height is >= this % of price.
    fvg_min_pct: float = 0.6
    # --- Auto-tune: weight each strategy's vote by its live performance ---
    autotune_enabled: bool = True
    autotune_min_trades: int = 8     # need this many attributed trades before weighting a strategy
    autotune_gain: float = 0.6       # how strongly expectancy (R) shifts the weight
    weight_min: float = 0.3          # a strategy's vote can shrink to this
    weight_max: float = 2.0          # ...or grow to this

    # --- AI brain: an LLM analyzes the chart each tick and sets entry/SL/TP ---
    # Trading provider priority: Gemini (GEMINI_API_KEY) → Anthropic API
    # (ANTHROPIC_API_KEY) → local Claude Code CLI → mechanical engine.
    # The news brief is Claude-only (Anthropic API → CLI) — see news.get_brief().
    # Groq was removed deliberately; leftover GROQ_* env vars are ignored.
    gemini_api_key: str = ""
    # Two-tier Gemini. FLASH runs the 5-minute trading loop (288 calls/day — Pro there
    # would be slow and expensive). PRO runs the news brief, which is manual and
    # low-frequency, so it's where deeper reasoning actually pays for itself.
    # Pro falls through to Flash automatically if it errors or is out of quota.
    # Versioned names (gemini-2.5-flash) 404 for newer keys — use the `-latest` aliases.
    gemini_model: str = "gemini-flash-latest"
    gemini_pro_model: str = "gemini-pro-latest"
    # Thinking tokens for 2.5-class models: 0 = off, -1 = dynamic, or a fixed budget.
    # ~8k is the middle ground — enough for multi-step reasoning without paying for
    # pro-tier deliberation on every 5-minute tick.
    gemini_thinking_budget: int = 8192
    # Thinking tokens are billed as output and count toward maxOutputTokens, so the
    # cap must leave room for the answer ON TOP of the budget.
    gemini_max_output_tokens: int = 4096
    anthropic_api_key: str = ""
    ai_enabled: bool = True
    ai_model: str = "claude-sonnet-5"   # claude-sonnet-5 / claude-sonnet-5-6 / claude-haiku-5-20251001
    # decide  = AI picks direction + SL + TP (guardrails enforce risk); this is the default
    # refine  = strategy votes decide direction, AI only sets smarter SL/TP
    # advisory = AI analysis is logged/shown but the mechanical engine still trades
    ai_mode: str = "decide"
    ai_min_confidence: float = 0.55     # below this the AI's trade is skipped (HOLD)
    ai_timeout_sec: int = 150           # max seconds to wait for a Claude response
    # Leave blank to auto-discover the Claude Code binary; set to override.
    claude_cli_path: str = ""
    ai_respect_trend_filter: bool = True  # still block trades that fight the 1h trend

    # --- Market news + economic calendar ------------------------------------ #
    # ForexFactory has no public news API, so headlines are aggregated from forex/
    # crypto RSS feeds; the "forecast" table uses FF's official weekly calendar JSON.
    news_enabled: bool = True
    news_calendar_url: str = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
    # Comma-separated `Name|url` RSS feeds (name optional). Fetched concurrently; any
    # that fail are skipped. NOTE: The Block (theblock.co) is deliberately absent — it
    # answers 403 to all server-side requests regardless of user-agent.
    news_feeds: str = (
        "ForexLive|https://www.forexlive.com/feed/news,"
        "FXStreet|https://www.fxstreet.com/rss/news,"
        "Investing|https://www.investing.com/rss/news_1.rss,"
        "Cointelegraph|https://cointelegraph.com/rss,"
        "CoinDesk|https://www.coindesk.com/arc/outboundfeeds/rss/,"
        "The Defiant|https://thedefiant.io/api/feed,"
        "Decrypt|https://decrypt.co/feed,"
        "Yahoo Finance|https://finance.yahoo.com/news/rssindex,"
        "CryptoSlate|https://cryptoslate.com/feed/,"
        "Bitcoin.com|https://news.bitcoin.com/feed/"
    )
    # Currencies the bot *reacts* to: the AI snapshot and the trading blackout only
    # consider events for these (always keep USD — it drives DXY & crypto). The
    # dashboard calendar shows the full ForexFactory week regardless of this list.
    news_currencies: str = "USD,EUR,GBP,JPY,CNY"
    news_refresh_sec: int = 180      # cache TTL for headline fetches
    # Calendar changes weekly, and its host rate-limits frequent polling — cache it long.
    news_calendar_refresh_sec: int = 900   # 15 min
    # Public Telegram channels, comma-separated `Name|handle` (name optional).
    # Read via each channel's public web preview (t.me/s/<handle>) — no bot token and
    # no user session. A Telegram BOT cannot read channels it doesn't administer, so
    # the preview is the only credential-free way to follow third-party channels.
    telegram_channels: str = "LMWM News|lmwmnews"
    telegram_max_posts: int = 60     # cap the merged channel timeline
    news_max_stories: int = 90       # cap the merged headline list (10 feeds now)
    news_brief_ttl_sec: int = 900    # cached AI brief lifetime (manual refresh overrides)
    news_ai_context: bool = True     # inject news + upcoming events into the AI snapshot
    news_lookahead_hours: float = 24.0  # how far ahead a High-impact event counts as "upcoming"
    # Safety blackout: skip opening NEW trades within this many minutes (before OR after)
    # of a High-impact event for a relevant currency. 0 disables. Open positions keep SL/TP.
    news_blackout_min: int = 15

    # Manual close guard: refuse (pending confirmation) a market close whose real fill
    # is further than this from the mark price. The testnet book is often one-sided,
    # so an unguarded market close can turn a mark-profit into a real loss.
    close_max_slippage_pct: float = 0.5
    # Liquidity gates checked before EVERY entry. Never enter a market you cannot exit:
    # a wide book eats the edge on the way in, and an empty one traps the position.
    max_entry_spread_pct: float = 0.15    # quoted bid/ask spread vs mark
    max_exit_slippage_pct: float = 0.40   # cost of closing the intended size
    # Train the strategy tuner on mark→mark P/L (the DECISION) rather than on fills
    # (the VENUE). Set false only on a venue whose fills you trust completely.
    autotune_use_mark_pnl: bool = True

    ema_fast: int = 9
    ema_slow: int = 21
    rsi_period: int = 14
    rsi_oversold: float = 30.0
    rsi_overbought: float = 70.0

    model_config = SettingsConfigDict(env_file=str(_ROOT_ENV), extra="ignore")

settings = Settings()
