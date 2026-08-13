"use client";
import { useState } from "react";
import { runBacktest } from "../lib/api";
import { Play, Loader2 } from "lucide-react";
import clsx from "clsx";

interface Metric {
  trades: number; win_rate: number; avg_r: number; total_r: number;
  profit_factor: number; max_dd_r: number;
}

export default function Backtest({ symbol }: { symbol: string }) {
  const [data, setData] = useState<any>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const run = async () => {
    setLoading(true); setErr(null);
    try {
      const res = await runBacktest(symbol, 1500);
      if (res.data?.error) setErr(res.data.error);
      else setData(res.data);
    } catch (e: any) {
      setErr("Backtest failed (try again)");
    } finally {
      setLoading(false);
    }
  };

  const rows: [string, Metric][] = data ? Object.entries(data.results) : [];

  return (
    <div className="bg-[#161b22] border border-[#30363d] rounded-xl p-4">
      <div className="flex items-center justify-between mb-3">
        <div>
          <h3 className="text-sm font-semibold text-gray-300 uppercase tracking-wider">Strategy Backtest</h3>
          <p className="text-xs text-gray-500 mt-0.5">
            {data ? `${data.bars} bars · ${data.entry_tf}m entry / ${data.trend_tf}m trend · RR 1:${data.rr} · need ${data.min_signals} votes` : `Replays history for ${symbol} through each strategy`}
          </p>
        </div>
        <button onClick={run} disabled={loading}
          className="flex items-center gap-1.5 px-3 py-2 bg-[#00C896] hover:bg-[#00b386] disabled:opacity-50 rounded-lg text-sm font-semibold text-black transition">
          {loading ? <Loader2 size={14} className="animate-spin" /> : <Play size={13} />}
          {loading ? "Running…" : "Run Backtest"}
        </button>
      </div>
      {err && <p className="text-xs text-red-400">{err}</p>}
      {rows.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="text-gray-500 border-b border-[#30363d]">
                <th className="px-3 py-2 text-left">Strategy</th>
                <th className="px-3 py-2 text-right">Trades</th>
                <th className="px-3 py-2 text-right">Win %</th>
                <th className="px-3 py-2 text-right">Avg R</th>
                <th className="px-3 py-2 text-right">Total R</th>
                <th className="px-3 py-2 text-right">Profit Factor</th>
                <th className="px-3 py-2 text-right">Max DD (R)</th>
              </tr>
            </thead>
            <tbody>
              {rows.map(([name, m]) => {
                const good = m.total_r > 0;
                const combined = name === "COMBINED";
                return (
                  <tr key={name} className={clsx("border-b border-[#21262d]", combined && "bg-[#1c2128]")}>
                    <td className={clsx("px-3 py-2 font-medium", combined ? "text-white" : "text-gray-300")}>{name}</td>
                    <td className="px-3 py-2 text-right text-gray-400">{m.trades}</td>
                    <td className="px-3 py-2 text-right text-gray-400">{m.win_rate}%</td>
                    <td className={clsx("px-3 py-2 text-right font-mono", m.avg_r >= 0 ? "text-green-400" : "text-red-400")}>{m.avg_r}</td>
                    <td className={clsx("px-3 py-2 text-right font-mono font-semibold", good ? "text-green-400" : "text-red-400")}>{m.total_r > 0 ? "+" : ""}{m.total_r}R</td>
                    <td className={clsx("px-3 py-2 text-right font-mono", m.profit_factor >= 1 ? "text-green-400" : "text-red-400")}>{m.profit_factor}</td>
                    <td className="px-3 py-2 text-right text-gray-400 font-mono">{m.max_dd_r}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <p className="text-[11px] text-gray-600 mt-2">
            R = reward:risk multiple. Profit Factor &gt; 1 and positive Total R = an edge. Simplified sim (ATR stop, fixed 1:{data.rr}); live trading uses structure stops + partial TPs.
          </p>
        </div>
      )}
    </div>
  );
}
