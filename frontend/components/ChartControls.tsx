"use client";
import { Eye, EyeOff, TrendingUp, Trash2 } from "lucide-react";
import clsx from "clsx";

export const TIMEFRAMES = [
  { label: "1m", m: 1 },
  { label: "5m", m: 5 },
  { label: "15m", m: 15 },
  { label: "1h", m: 60 },
  { label: "4h", m: 240 },
  { label: "1D", m: 1440 },
];

export const SYMBOLS = ["BTCUSD", "ETHUSD"];

interface Props {
  timeframe: number;
  setTimeframe: (m: number) => void;
  ema9: boolean;
  setEma9: (b: boolean) => void;
  ema21: boolean;
  setEma21: (b: boolean) => void;
  ema200: boolean;
  setEma200: (b: boolean) => void;
  showTrades: boolean;
  setShowTrades: (b: boolean) => void;
  supertrend: boolean;
  setSupertrend: (b: boolean) => void;
  trendlineNav: boolean;
  setTrendlineNav: (b: boolean) => void;
  fvg: boolean;
  setFvg: (b: boolean) => void;
  ifvg: boolean;
  setIfvg: (b: boolean) => void;
  smc: boolean;
  setSmc: (b: boolean) => void;
  macd: boolean;
  setMacd: (b: boolean) => void;
  volume: boolean;
  setVolume: (b: boolean) => void;
  tool: string;
  setTool: (t: any) => void;
  onClear: () => void;
}

const seg = "px-2.5 py-1 text-xs rounded-md transition";

export default function ChartControls(p: Props) {
  const Pill = ({ active, onClick, children }: any) => (
    <button onClick={onClick} className={clsx(seg, active ? "bg-[#00C896] text-black font-semibold" : "text-gray-400 hover:text-white hover:bg-[#21262d]")}>
      {children}
    </button>
  );

  const Toggle = ({ on, onClick, label, dot }: any) => (
    <button
      onClick={onClick}
      className={clsx("flex items-center gap-1.5 px-2.5 py-1 text-xs rounded-md border transition",
        on ? "border-[#30363d] bg-[#21262d] text-white" : "border-transparent text-gray-500 hover:text-gray-300")}
    >
      {dot && <span className="w-2 h-2 rounded-full" style={{ background: dot }} />}
      {label}
      {on ? <Eye size={13} /> : <EyeOff size={13} />}
    </button>
  );

  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
      {/* Timeframe */}
      <div className="flex items-center gap-1 bg-[#0d1117] border border-[#30363d] rounded-lg p-0.5">
        {TIMEFRAMES.map((tf) => (
          <Pill key={tf.m} active={p.timeframe === tf.m} onClick={() => p.setTimeframe(tf.m)}>
            {tf.label}
          </Pill>
        ))}
      </div>

      {/* Indicators (eye toggles) */}
      <div className="flex items-center gap-1">
        <Toggle on={p.ema9} onClick={() => p.setEma9(!p.ema9)} label="EMA 9" dot="#f59e0b" />
        <Toggle on={p.ema21} onClick={() => p.setEma21(!p.ema21)} label="EMA 21" dot="#a78bfa" />
        <Toggle on={p.ema200} onClick={() => p.setEma200(!p.ema200)} label="EMA 200" dot="#e5e7eb" />
        <Toggle on={p.supertrend} onClick={() => p.setSupertrend(!p.supertrend)} label="SuperTrend AI" dot="#00C896" />
        <Toggle on={p.trendlineNav} onClick={() => p.setTrendlineNav(!p.trendlineNav)} label="Trendline Nav" dot="#22d3ee" />
        <Toggle on={p.fvg} onClick={() => p.setFvg(!p.fvg)} label="FVG" dot="#089981" />
        <Toggle on={p.ifvg} onClick={() => p.setIfvg(!p.ifvg)} label="IFVG" dot="#f23645" />
        <Toggle on={p.smc} onClick={() => p.setSmc(!p.smc)} label="SMC" dot="#eab308" />
        <Toggle on={p.macd} onClick={() => p.setMacd(!p.macd)} label="MACD" dot="#2962ff" />
        <Toggle on={p.volume} onClick={() => p.setVolume(!p.volume)} label="Volume" dot="#26a69a" />
        <Toggle on={p.showTrades} onClick={() => p.setShowTrades(!p.showTrades)} label="Trades" />
      </div>

      {/* Drawing tools */}
      <div className="flex items-center gap-1 bg-[#0d1117] border border-[#30363d] rounded-lg p-0.5">
        <Pill active={p.tool === "none"} onClick={() => p.setTool("none")}>
          Cursor
        </Pill>
        <button
          onClick={() => p.setTool(p.tool === "trendline" ? "none" : "trendline")}
          className={clsx(seg, "flex items-center gap-1", p.tool === "trendline" ? "bg-[#00C896] text-black font-semibold" : "text-gray-400 hover:text-white hover:bg-[#21262d]")}
          title="Trendline — click two points"
        >
          <TrendingUp size={13} /> Trend
        </button>
        <button onClick={p.onClear} className={clsx(seg, "flex items-center gap-1 text-gray-400 hover:text-red-400")} title="Clear drawings">
          <Trash2 size={13} />
        </button>
      </div>
    </div>
  );
}
