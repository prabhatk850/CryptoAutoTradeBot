"use client";
interface Props {
  rsi: number | null;
  emaSignal: string | null;
  breakoutSignal: string | null;
}

function Badge({ label, color }: { label: string; color: string }) {
  return (
    <span className={`px-2 py-1 rounded text-xs font-semibold ${color}`}>{label}</span>
  );
}

export default function IndicatorBar({ rsi, emaSignal, breakoutSignal }: Props) {
  const rsiColor =
    rsi == null ? "bg-gray-700 text-gray-400"
    : rsi < 30  ? "bg-green-500/20 text-green-400"
    : rsi > 70  ? "bg-red-500/20 text-red-400"
    : "bg-gray-700 text-gray-300";

  const emaColor =
    emaSignal === "BULLISH_CROSS" ? "bg-green-500/20 text-green-400"
    : emaSignal === "BEARISH_CROSS" ? "bg-red-500/20 text-red-400"
    : "bg-gray-700 text-gray-400";

  const brkColor =
    breakoutSignal === "BREAKOUT_UP" ? "bg-green-500/20 text-green-400"
    : breakoutSignal === "BREAKOUT_DOWN" ? "bg-red-500/20 text-red-400"
    : "bg-gray-700 text-gray-400";

  return (
    <div className="flex flex-wrap gap-2 items-center">
      <span className="text-xs text-gray-500">Live indicators:</span>
      <Badge label={`RSI ${rsi?.toFixed(1) ?? "—"}`} color={rsiColor} />
      <Badge label={emaSignal ?? "EMA —"} color={emaColor} />
      <Badge label={breakoutSignal ?? "Breakout —"} color={brkColor} />
    </div>
  );
}
