"use client";
import { useEffect, useState } from "react";
import { getPerformance } from "../lib/api";
import clsx from "clsx";

interface Row {
  id: string; label: string; weight: number; count: number;
  win_rate: number; expectancy: number; sum_r: number;
}

export default function StrategyPerformance() {
  const [data, setData] = useState<any>(null);

  const fetchData = async () => {
    try {
      const res = await getPerformance();
      setData(res.data);
    } catch { /* keep last */ }
  };

  useEffect(() => {
    fetchData();
    const iv = setInterval(fetchData, 30_000);
    return () => clearInterval(iv);
  }, []);

  const rows: Row[] = data?.strategies ?? [];

  return (
    <div className="bg-[#161b22] border border-[#30363d] rounded-xl p-4">
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-sm font-semibold text-gray-300 uppercase tracking-wider">Auto-Tune · Live Strategy Weights</h3>
        <span className="text-xs text-gray-500">
          {data?.enabled ? `learning · weights apply after ${data.min_trades} trades` : "auto-tune off"}
        </span>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="text-gray-500 border-b border-[#30363d]">
              <th className="px-3 py-2 text-left">Strategy</th>
              <th className="px-3 py-2 text-right">Weight</th>
              <th className="px-3 py-2 text-right">Trades</th>
              <th className="px-3 py-2 text-right">Win %</th>
              <th className="px-3 py-2 text-right">Expectancy (R)</th>
              <th className="px-3 py-2 text-right">Total R</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const w = r.weight;
              const wColor = w > 1.05 ? "text-green-400" : w < 0.95 ? "text-red-400" : "text-gray-300";
              return (
                <tr key={r.id} className="border-b border-[#21262d]">
                  <td className="px-3 py-2 text-gray-300">{r.label || r.id}</td>
                  <td className="px-3 py-2 text-right">
                    <span className={clsx("font-mono font-semibold", wColor)}>×{w.toFixed(2)}</span>
                    <span className="inline-block ml-2 align-middle h-1.5 rounded-full bg-[#00C896]/60"
                      style={{ width: `${Math.min(w / 2, 1) * 36}px` }} />
                  </td>
                  <td className="px-3 py-2 text-right text-gray-400">{r.count || "—"}</td>
                  <td className="px-3 py-2 text-right text-gray-400">{r.count ? `${r.win_rate}%` : "—"}</td>
                  <td className={clsx("px-3 py-2 text-right font-mono", r.expectancy > 0 ? "text-green-400" : r.expectancy < 0 ? "text-red-400" : "text-gray-500")}>
                    {r.count ? r.expectancy : "—"}
                  </td>
                  <td className={clsx("px-3 py-2 text-right font-mono", r.sum_r > 0 ? "text-green-400" : r.sum_r < 0 ? "text-red-400" : "text-gray-500")}>
                    {r.count ? `${r.sum_r > 0 ? "+" : ""}${r.sum_r}R` : "—"}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        <p className="text-[11px] text-gray-600 mt-2">
          Each closed trade credits the strategies that voted for it. Winners vote louder (weight ↑), losers quieter (↓ to {data ? "0.3" : "min"}) — never silenced, since they're still useful context.
        </p>
      </div>
    </div>
  );
}
