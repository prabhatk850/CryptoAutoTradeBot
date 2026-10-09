from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

# Project-root .env, so it loads regardless of CWD.
_ROOT_ENV = Path(__file__).resolve().parent.parent / ".env"


class Settings(BaseSettings):
    # --- Exchange / DB ---
    delta_api_key: str = ""
    delta_api_secret: str = ""
    delta_base_url: str = "https://cdn-ind.testnet.deltaex.org"
    mongo_uri: str = "mongodb://localhost:27017/forexbot"

    # --- Symbols & cadence ---
    trading_symbol: str = "BTCUSD"             # dashboard's default chart symbol
    trade_symbols: str = "BTCUSD,ETHUSD"       # symbols the bot opens trades on
    use_mark_candles: bool = True              # mark-price candles (testnet traded feed prints fake wicks)
    max_concurrent_positions: int = 2
    check_interval_minutes: int = 5            # deep-loop cadence when check_interval_seconds is 0
    check_interval_seconds: int = 120          # deep-loop cadence; wins over minutes when > 0
    auto_start_bot: bool = True                # start trading when the backend boots
    candle_limit: int = 500                    # candles fetched per timeframe per tick

    # --- Fast execution loop (fires armed triggers between deep ticks) ---
    fast_check_enabled: bool = True
    fast_check_seconds: int = 15
    fast_arm_atr_mult: float = 1.2             # arm if trigger is within this × expected move
    fast_arm_ttl_sec: int = 300                # armed watch expires after this

    # --- Timeframes & voting ---
    trend_timeframe: int = 60                  # 1h sets direction bias
    entry_timeframe: int = 15                  # 15m is the decision timeframe
    ltf_timeframe: int = 5                     # 5m refines entry timing only
    min_signals: int = 2                       # strategies that must agree to trade
    strategies: str = "EMA_CROSS,RSI,BREAKOUT,SUPERTREND_AI,TRENDLINE_NAV,FVG,IFVG,SMC,MACD"
    shadow_strategies: str = "FUNDING_BIAS,ORDERBOOK_IMBALANCE"  # tracked, never vote
    fvg_min_pct: float = 0.6                   # hide chart FVG zones smaller than this % of price

    # --- Risk / sizing (margin = fixed % of capital) ---
    leverage: int = 50                         # max; auto-lowered so SL sits inside liquidation
    position_capital_pct: float = 50.0         # margin for a normal trade
    big_trade_capital_pct: float = 20.0        # margin for a high-confluence trade (wider SL/TP)
    big_trade_min_agree: float = 0.7           # fraction of strategies agreeing to count as "big"
    liq_buffer_pct: float = 0.5                # SL must be this % of price inside liquidation
    risk_min_pct: float = 0.5                  # risk band shown to the AI
    risk_max_pct: float = 1.5
    margin_cap_pct: float = 0.5                # max fraction of available balance per trade
    stop_loss_pct: float = 1.0                 # fallback stop distance when no SL candidate exists
    risk_reward: float = 2.0                   # minimum reward:risk floor
    daily_loss_limit_pct: float = 10.0         # stop new entries after this daily realized loss (0 = off)
    # 3-5-7 rule: ≤3% risk per trade, ≤5% risk across open trades, bot stops for the day at +7%.
    rule_357_enabled: bool = True
    max_trade_risk_pct: float = 3.0
    max_open_risk_pct: float = 5.0
    daily_profit_target_pct: float = 7.0       # of the session's starting balance; manual Start overrides it
    session_reset_hour_ist: int = 18           # daily session reset + bot auto-start, IST
    # Trailing stop: breakeven(+fees) at trigger × the way to TP1, lock × TP1 profit once TP1 fills.
    trail_enabled: bool = True
    trail_trigger_frac: float = 0.5
    trail_lock_frac: float = 0.5
    trail_be_buffer_pct: float = 0.02          # extra margin above round-trip fees, % of entry
    trail_fee_fallback_pct: float = 0.05       # taker fee % if Delta's product rate is unavailable
    # Risk engine: replays closed trades to tune trail params + per-trade risk (never above max_trade_risk_pct).
    risk_engine_enabled: bool = True
    risk_engine_min_trades: int = 20
    risk_engine_min_gain_r: float = 0.05       # adopt new params only if this much better (R)
    risk_engine_min_risk_pct: float = 0.5
    # ETH uses point-based SL/TP bands instead of percentages.
    eth_sl_min_pts: float = 5.0
    eth_sl_max_pts: float = 20.0
    eth_tp_min_pts: float = 50.0
    eth_tp_max_pts: float = 60.0
    eth_big_sl_max_pts: float = 60.0
    eth_big_tp_min_pts: float = 120.0
    eth_big_tp_max_pts: float = 220.0

    # --- Stop-loss & take-profit placement ---
    atr_period: int = 14
    atr_k: float = 1.5                         # ATR stop = entry ± k·ATR
    sl_lookback: int = 20                      # bars for the structure stop
    min_sl_pct: float = 0.3                    # stop distance clamp, % of price
    max_sl_pct: float = 1.5
    target_lookback: int = 30                  # 1h bars checked for room to a 1:2 target
    max_tps: int = 2                           # at most this many take-profits
    tp_splits: str = "0.5,0.5"                 # size split across TPs

    # --- Auto-tune (weight votes by live expectancy) ---
    autotune_enabled: bool = True
    autotune_min_trades: int = 8               # trades needed before a strategy is reweighted
    autotune_gain: float = 0.6                 # weight = 1 + gain × expectancy(R)
    autotune_use_mark_pnl: bool = True         # train on mark→mark P/L, not fills
    weight_min: float = 0.3
    weight_max: float = 2.0

    # --- Agents + learning ensemble (primary decision-maker; see .claude/skills/trading-books) ---
    agents_enabled: bool = True
    agents: str = "MOMENTUM,MEAN_REVERSION,SMC_AGENT,CONFLUENCE"
    agents_min_confidence: float = 0.55        # meta-label take threshold; below it the AI fallback decides
    agents_min_trades: int = 12                # pooled closed trades before half-Kelly can cap size
    agents_agreement_bonus: float = 0.03       # + per extra agreeing agent
    agents_opposition_penalty: float = 0.10    # − × opposing/winning pooled mass
    agents_size_min_mult: float = 0.5          # margin multiplier band from win probability
    agents_size_max_mult: float = 1.5
    agent_conf_min: float = 0.55               # raw agent confidence band (before learning)
    agent_conf_max: float = 0.95
    agents_beta_prior: float = 1.0             # Beta prior on each agent's win rate (starts at 0.5)
    agents_r_history: int = 50                 # R-multiples kept per agent for Kelly
    agent_hurst_mr_max: float = 0.5            # mean-reversion only when Hurst < this
    agent_mr_z_entry: float = 1.5              # |z| that counts as stretched
    agent_mr_min_lookback: int = 10            # z-score window clamp (half-life picks within)
    agent_mr_max_lookback: int = 60
    agent_mr_min_bars: int = 40
    agent_hurst_trend_min: float = 0.5         # momentum only when Hurst >= this (or unknown)
    agent_mom_lookback: int = 20               # momentum return + Donchian window
    agent_mom_full_return: float = 0.02        # |return| counted as full momentum strength

    # --- AI brain: providers (chains live in bot/ai_brain.py) ---
    ai_enabled: bool = True
    ai_mode: str = "decide"                    # decide | refine (AI sets SL/TP only) | advisory
    ai_model: str = "claude-sonnet-5"          # model for the `cli` rung
    ai_min_confidence: float = 0.55            # below this an AI trade becomes HOLD
    ai_timeout_sec: int = 150
    ai_min_interval_sec: int = 300             # deep-loop AI fallback at most this often per symbol (0 = every tick)
    ai_respect_trend_filter: bool = True       # block AI trades against the 1h trend
    ai_allow_subscription_cli: bool = False    # never spend the personal Claude subscription
    subscription_cli_daily_call_cap: int = 200
    claude_cli_path: str = ""                  # blank = auto-discover
    # OpenRouter (Ox Alpha: free stealth model, may vanish without notice)
    openrouter_api_key: str = ""
    openrouter_model: str = "stealth/ox-alpha"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_max_output_tokens: int = 4096
    openrouter_referer: str = "https://github.com/local/forex-bot"
    openrouter_title: str = "forex-bot"
    # Groq (fast; free tier 413s on the full deep snapshot)
    groq_api_key: str = ""
    groq_model: str = "openai/gpt-oss-120b"
    groq_base_url: str = "https://api.groq.com/openai/v1"
    groq_max_output_tokens: int = 4096
    # Gemini (flash for trading, pro for the news brief; use -latest aliases)
    gemini_api_key: str = ""
    gemini_model: str = "gemini-flash-latest"
    gemini_pro_model: str = "gemini-pro-latest"
    gemini_thinking_budget: int = 8192         # 0 = off, -1 = dynamic
    gemini_max_output_tokens: int = 4096       # answer budget on top of thinking tokens
    # AgentRouter (Claude reseller, CLI-only, ~$0.28/call)
    agentrouter_api_key: str = ""
    agentrouter_base_url: str = "https://agentrouter.org"
    agentrouter_model: str = "claude-opus-4-8"
    agentrouter_daily_call_cap: int = 120      # spend guard per UTC day (0 = uncapped)

    # --- News & economic calendar ---
    news_enabled: bool = True
    news_ai_context: bool = True               # feed news + events into the AI snapshot
    news_calendar_url: str = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
    news_feeds: str = (                        # `Name|url` RSS feeds (theblock.co 403s, omitted)
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
    telegram_channels: str = "LMWM News|lmwmnews"  # `Name|handle`, read via public t.me/s preview
    news_currencies: str = "USD,EUR,GBP,JPY,CNY"   # currencies the AI and blackout react to
    news_refresh_sec: int = 180
    news_calendar_refresh_sec: int = 900       # calendar host rate-limits; cache long
    news_brief_ttl_sec: int = 900
    news_max_stories: int = 90
    telegram_max_posts: int = 60
    news_lookahead_hours: float = 24.0         # window for "upcoming" high-impact events
    news_blackout_min: int = 15                # no new entries ± this many min of a high-impact event

    # --- Execution guards ---
    close_max_slippage_pct: float = 0.5        # manual close asks to confirm beyond this slippage
    max_entry_spread_pct: float = 0.15
    max_exit_slippage_pct: float = 0.40
    liquidity_gate_enabled: bool = True
    liquidity_gate_mode: str = "shadow"        # off | shadow (log only) | enforce
    expectancy_gate_enabled: bool = True       # require backtested edge for the voting strategies
    expectancy_gate_min_R: float = -0.1
    expectancy_gate_min_strategy_n: int = 15   # backtested trades before a strategy's edge counts
    expectancy_gate_fail_open: bool = True     # allow trades when no strategy qualifies yet

    # --- Indicators ---
    ema_fast: int = 9
    ema_slow: int = 21
    rsi_period: int = 14
    rsi_oversold: float = 30.0
    rsi_overbought: float = 70.0
    bb_period: int = 20
    bb_mult: float = 2.0
    kc_period: int = 20
    kc_atr_mult: float = 1.5
    kc_atr_len: int = 10
    adx_period: int = 14
    vwap_enabled: bool = True
    adx_gate_enabled: bool = False             # block entries when 1h ADX is below adx_min_trend
    adx_min_trend: float = 20.0
    divergence_swing_left: int = 2
    divergence_swing_right: int = 2

    # --- Crypto-native shadow signals ---
    funding_history_window_days: int = 30
    funding_min_history_samples: int = 20
    funding_extreme_percentile: float = 0.90
    ob_imbalance_levels: int = 10
    ob_imbalance_threshold: float = 0.35

    # --- Portfolio risk ---
    correlation_check_enabled: bool = True
    correlation_lookback_bars: int = 200
    correlation_timeframe_min: int = 60
    correlation_high_threshold: float = 0.7
    correlation_dampen_factor: float = 0.5     # margin multiplier for a correlated same-side trade
    vol_sizing_enabled: bool = False           # scale margin inversely with ATR%
    vol_ref_atr_pct: float = 0.5
    vol_scalar_min: float = 0.4
    vol_scalar_max: float = 1.5

    model_config = SettingsConfigDict(env_file=str(_ROOT_ENV), extra="ignore")

    def symbols(self) -> list[str]:
        """trade_symbols as a clean upper-case list."""
        return [s.strip().upper() for s in self.trade_symbols.split(",") if s.strip()]


settings = Settings()
