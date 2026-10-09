"use client";
import { useEffect, useState } from "react";
import TradingViewChart, { ChartOrder, SuperTrendData, TrendlineData, FvgData, IfvgData } from "../components/TradingViewChart";
import BotToggle from "../components/BotToggle";
import NewsPanel from "../components/NewsPanel";
import StatCard from "../components/StatCard";
import TradeLog from "../components/TradeLog";
import IndicatorBar from "../components/IndicatorBar";
import Sidebar from "../components/Sidebar";
import PnlCards, { Pnl } from "../components/PnlCards";
import Analytics from "../components/Analytics";
import Backtest from "../components/Backtest";
import StrategyPerformance from "../components/StrategyPerformance";
import TrainingMonitor from "../components/TrainingMonitor";
import ChartControls, { SYMBOLS } from "../components/ChartControls";
import OpenPositions from "../components/OpenPositions";
import clsx from "clsx";
import { getCandles, getTradeStats, getTicker, getTradeLogs, getPnl, getOrders, getIndicators } from "../lib/api";

export default function Dashboard() {
  // bot/account data (always the bot's configured symbol)
  const [candles, setCandles] = useState<any[]>([]);
  const [stats, setStats] = useState<any>(null);
  const [ticker, setTicker] = useState<any>(null);
  const [pnl, setPnl] = useState<Pnl | null>(null);
  const [orders, setOrders] = useState<ChartOrder[]>([]);
  const [openEntry, setOpenEntry] = useState<{ side: string; avg: number; unrealized: number; sl?: number | null; tps?: number[] } | null>(null);
  const [latestIndicators, setLatestIndicators] = useState<any>(null);

  // chart view controls
  const [chartSymbol, setChartSymbol] = useState("BTCUSD");
  const [timeframe, setTimeframe] = useState(5);
  const [ema9, setEma9] = useState(false);
  const [ema21, setEma21] = useState(false);
  const [ema200Data, setEma200Data] = useState<any | null>(null);
  const [showEma200, setShowEma200] = useState(false);
  const [supertrend, setSupertrend] = useState<SuperTrendData | null>(null);
  const [showSupertrend, setShowSupertrend] = useState(false);
  const [trendline, setTrendline] = useState<TrendlineData | null>(null);
  const [showTrendline, setShowTrendline] = useState(false);
  const [fvg, setFvg] = useState<FvgData | null>(null);
  const [showFvg, setShowFvg] = useState(false);
  const [ifvg, setIfvg] = useState<IfvgData | null>(null);
  const [showIfvg, setShowIfvg] = useState(false);
  const [smc, setSmc] = useState<any | null>(null);
  const [showSmc, setShowSmc] = useState(false);
  const [macd, setMacd] = useState<any | null>(null);
  const [showMacd, setShowMacd] = useState(false);
  const [volume, setVolume] = useState<any | null>(null);
  const [showVolume, setShowVolume] = useState(false);
  const [showTrades, setShowTrades] = useState(true);
  const [tool, setTool] = useState<"none" | "trendline" | "long" | "short">("none");
  const [clearSignal, setClearSignal] = useState(0);

  const botSymbol = stats?.symbol ?? "BTCUSD";

  // market data follows the chart's selected symbol + timeframe
  useEffect(() => {
    let alive = true;
    const wantInd = showSupertrend || showTrendline || showFvg || showIfvg || showSmc || showMacd || showVolume || showEma200;
    const fetchMarket = async () => {
      const [candleRes, tickerRes, indRes] = await Promise.allSettled([
        getCandles(chartSymbol, timeframe),
        getTicker(chartSymbol),
        wantInd ? getIndicators(chartSymbol, timeframe) : Promise.resolve(null as any),
      ]);
      if (!alive) return;
      if (candleRes.status === "fulfilled" && Array.isArray(candleRes.value.data)) setCandles(candleRes.value.data);
      if (tickerRes.status === "fulfilled") setTicker(tickerRes.value.data);
      const ind = wantInd && indRes.status === "fulfilled" ? indRes.value?.data : null;
      setSupertrend(showSupertrend && ind?.supertrend ? ind.supertrend : null);
      setTrendline(showTrendline && ind?.trendline ? ind.trendline : null);
      setFvg(showFvg && ind?.fvg ? ind.fvg : null);
      setIfvg(showIfvg && ind?.ifvg ? ind.ifvg : null);
      setSmc(showSmc && ind?.smc ? ind.smc : null);
      setMacd(showMacd && ind?.macd ? ind.macd : null);
      setVolume(showVolume && ind?.volume ? ind.volume : null);
      setEma200Data(showEma200 && ind?.ema200 ? ind.ema200 : null);
    };
    fetchMarket();
    const iv = setInterval(fetchMarket, 30_000);
    return () => { alive = false; clearInterval(iv); };
  }, [chartSymbol, timeframe, showSupertrend, showTrendline, showFvg, showIfvg, showSmc, showMacd, showVolume, showEma200]);

  // Live price: poll the mark price every second and feed it to the chart so the
  // forming candle updates in real time, like the Delta exchange chart. This is cheap
  // (one lightweight ticker call) and stays out of the heavy 30s candle refresh above.
  useEffect(() => {
    let alive = true;
    const tickLive = async () => {
      try {
        const { data } = await getTicker(chartSymbol);
        if (alive && data) setTicker(data);
      } catch { /* ignore transient ticker errors */ }
    };
    const iv = setInterval(tickLive, 1_000);
    return () => { alive = false; clearInterval(iv); };
  }, [chartSymbol]);

  // bot trades the chart's symbol: switching the chart re-points the bot,
  // then we refetch the account view for that symbol.
  useEffect(() => {
    let alive = true;
    const fetchBot = async () => {
      const [statsRes, pnlRes, ordersRes, logsRes] = await Promise.allSettled([
        getTradeStats(), getPnl(chartSymbol), getOrders(200, chartSymbol), getTradeLogs(1),
      ]);
      if (!alive) return;
      if (statsRes.status === "fulfilled") setStats(statsRes.value.data);
      if (pnlRes.status === "fulfilled") setPnl(pnlRes.value.data);  // this coin's page
      if (ordersRes.status === "fulfilled") {
        const d = ordersRes.value.data;  // per chart symbol
        setOrders(d.markers ?? []);  // per-fill markers for the chart
        setOpenEntry(d.open_position ? {
          // chart entry line uses the bot's MARK entry so it aligns with the mark candles
          // (the fill can differ on the thin testnet); PnL cards still use the real fill.
          side: d.open_side, avg: d.protection?.mark_entry ?? d.open_avg_entry, unrealized: d.open_unrealized,
          sl: d.protection?.sl ?? null, tps: d.protection?.tps ?? [],
        } : null);
      }
      if (logsRes.status === "fulfilled") {
        const logs = logsRes.value.data.logs;
        if (logs.length > 0) setLatestIndicators(logs[0].indicators);
      }
    };
    fetchBot();
    const iv = setInterval(fetchBot, 30_000);
    return () => { alive = false; clearInterval(iv); };
  }, [chartSymbol]);

  // MARK price, not `close`. `close` is the last TRADED price, which on this thin
  // venue prints at stale levels (ETH close 1925 vs mark 1872; BTC 65096 vs 63080).
  // The chart draws MARK candles and every PnL figure is mark-based, so anything
  // else here reads as the dashboard disagreeing with itself.
  const price = Number(ticker?.mark_price ?? ticker?.close ?? 0);

  return (
    <div className="min-h-screen bg-[#0d1117] text-white">
      <Sidebar />

      <div className="pl-14 sm:pl-16">
        <header className="border-b border-[#30363d] px-4 sm:px-6 lg:px-8 py-4 flex flex-wrap items-center justify-between gap-y-2 sticky top-0 bg-[#0d1117]/90 backdrop-blur z-20">
          <div className="flex items-center gap-3 flex-wrap">
            <h1 className="font-semibold text-lg">Trading Dashboard</h1>
            <span className="text-xs bg-yellow-500/20 text-yellow-400 px-2 py-0.5 rounded-full">PAPER TRADING</span>
            {/* Symbol switch — top, above everything */}
            <div className="flex items-center gap-1 bg-[#161b22] border border-[#30363d] rounded-lg p-0.5 ml-2">
              {SYMBOLS.map((s) => (
                <button key={s} onClick={() => setChartSymbol(s)}
                  className={clsx("px-3 py-1 text-xs rounded-md font-semibold transition",
                    chartSymbol === s ? "bg-[#00C896] text-black" : "text-gray-400 hover:text-white hover:bg-[#21262d]")}>
                  {s.replace("USD", "")}
                </button>
              ))}
            </div>
            {/* Single Start/Stop toggle, right next to the coin switch */}
            <BotToggle openPosition={pnl?.open_position ?? 0} />
          </div>
          <div className="flex items-center gap-3 sm:gap-4 text-sm">
            <span className="font-semibold tabular-nums">{price ? `$${price.toLocaleString()}` : "—"}</span>
            <span className="text-xs text-gray-500 hidden sm:inline">{chartSymbol} · Delta Exchange Testnet</span>
          </div>
        </header>

        <main className="max-w-[1500px] mx-auto px-3 sm:px-6 lg:px-8 py-6 space-y-8">

          {/* Positions + stats + chart share the left 3/4; Market News is the full-height right quarter */}
          <div className="grid grid-cols-1 lg:grid-cols-4 gap-6">
            <div className="lg:col-span-3 space-y-6">

          <section id="overview" className="space-y-4 scroll-mt-20">
            <OpenPositions activeSymbol={chartSymbol} />
            <PnlCards pnl={pnl} />
            <div className="grid grid-cols-2 md:grid-cols-3 gap-4">
              <StatCard label="Current Price" value={price ? `$${price.toLocaleString()}` : "—"} sub={chartSymbol} />
              <StatCard label="Total Decisions" value={stats?.total_decisions ?? "—"}
                sub={`${stats?.buys ?? 0} buy · ${stats?.sells ?? 0} sell · ${stats?.holds ?? 0} hold`} />
              <StatCard label="Win Rate" value={pnl ? `${pnl.win_rate}%` : "—"} sub={`${pnl?.closed_trades ?? 0} closed trades`} color="text-green-400" />
            </div>
          </section>

          <section>
            <div className="bg-[#161b22] border border-[#30363d] rounded-xl p-4 space-y-3">
              <ChartControls
                timeframe={timeframe} setTimeframe={setTimeframe}
                ema9={ema9} setEma9={setEma9}
                ema21={ema21} setEma21={setEma21}
                ema200={showEma200} setEma200={setShowEma200}
                supertrend={showSupertrend} setSupertrend={setShowSupertrend}
                trendlineNav={showTrendline} setTrendlineNav={setShowTrendline}
                fvg={showFvg} setFvg={setShowFvg}
                ifvg={showIfvg} setIfvg={setShowIfvg}
                smc={showSmc} setSmc={setShowSmc}
                macd={showMacd} setMacd={setShowMacd}
                volume={showVolume} setVolume={setShowVolume}
                showTrades={showTrades} setShowTrades={setShowTrades}
                tool={tool} setTool={setTool}
                onClear={() => setClearSignal((n) => n + 1)}
              />
              <IndicatorBar
                rsi={latestIndicators?.rsi ?? null}
                emaSignal={latestIndicators?.ema_signal ?? null}
                breakoutSignal={latestIndicators?.breakout_signal ?? null}
              />
              <TradingViewChart
                candles={candles}
                symbol={chartSymbol}
                livePrice={price}
                orders={orders}
                openEntry={chartSymbol === botSymbol ? openEntry : null}
                supertrend={supertrend}
                showSupertrend={showSupertrend}
                trendline={trendline}
                showTrendline={showTrendline}
                fvg={fvg}
                showFvg={showFvg}
                ifvg={ifvg}
                showIfvg={showIfvg}
                smc={smc}
                showSmc={showSmc}
                macd={macd}
                showMacd={showMacd}
                volume={volume}
                showVolume={showVolume}
                ema200={ema200Data}
                showEma200={showEma200}
                showEma9={ema9}
                showEma21={ema21}
                showTrades={showTrades}
                tool={tool}
                clearSignal={clearSignal}
                fitKey={`${chartSymbol}-${timeframe}`}
                height={460}
              />
            </div>
          </section>

            </div>
            {/* relative + an absolutely-placed panel: the news column contributes no
                height of its own, so the row is sized purely by the stack on the left
                and the panel ends exactly where the chart card ends. */}
            <div className="lg:col-span-1 lg:relative" id="settings">
              <NewsPanel />
            </div>
          </div>

          <section id="analytics" className="scroll-mt-20 space-y-4">
            <h2 className="text-sm font-semibold text-gray-400 uppercase tracking-wider">Analytics &amp; Progress</h2>
            <Analytics pnl={pnl} stats={stats} />
            <TrainingMonitor />
            <StrategyPerformance />
            <Backtest symbol={chartSymbol} />
          </section>

          <section id="decision-log" className="scroll-mt-20">
            <TradeLog symbol={chartSymbol} />
          </section>
        </main>
      </div>
    </div>
  );
}
