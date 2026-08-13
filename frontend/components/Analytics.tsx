"use client";
import {
  BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, ReferenceLine,
  PieChart, Pie, Cell, Legend, CartesianGrid,
} from "recharts";
import type { Pnl } from "./PnlCards";

interface Stats {
  buys: number;
  sells: number;
  holds: number;
}

const card = "bg-[#161b22] border border-[#30363d] rounded-xl p-4";
const heading = "text-sm font-semibold text-gray-300 uppercase tracking-wider mb-3";

function Empty({ msg }: { msg: string }) {
  return <div className="h-[220px] flex items-center justify-center text-gray-600 text-sm">{msg}</div>;
}

// Recharts defaults its tooltip label/item text to near-black, which is invisible on
// this theme — every tooltip sets these explicitly.
const tooltipStyle = {
  background: "#0d1117",
  border: "1px solid #30363d",
  borderRadius: 8,
  fontSize: 12,
  color: "#e6edf3",
} as const;
const tooltipLabelStyle = { color: "#9ca3af", marginBottom: 2, fontSize: 11 } as const;
const tooltipItemStyle = { color: "#e6edf3", fontSize: 12 } as const;
const legendStyle = { fontSize: 12, color: "#9ca3af" } as const;

export default function Analytics({ pnl, stats }: { pnl: Pnl | null; stats: Stats | null }) {
  const daily = pnl?.daily ?? [];

  const actionData = stats
    ? [
        { name: "Buys", value: stats.buys, color: "#00C896" },
        { name: "Sells", value: stats.sells, color: "#ef4444" },
        { name: "Holds", value: stats.holds, color: "#eab308" },
      ].filter((d) => d.value > 0)
    : [];

  const winLossData =
    pnl && pnl.closed_trades > 0
      ? [
          { name: "Wins", value: pnl.wins, color: "#00C896" },
          { name: "Losses", value: pnl.losses, color: "#ef4444" },
        ].filter((d) => d.value > 0)
      : [];

  return (
    <div className="space-y-4">
      {/* Per-day PnL only — the running total is already on the Total PnL card. */}
      <div className={card}>
        <div className="flex items-baseline justify-between mb-3">
          <h3 className={heading.replace("mb-3", "")}>Daily PnL</h3>
          <span className="text-[11px] text-gray-600">profit or loss booked each day</span>
        </div>
        {daily.length === 0 ? (
          <Empty msg="No closed trades yet — PnL chart fills as the bot trades." />
        ) : (
          <ResponsiveContainer width="100%" height={240}>
            <BarChart data={daily} margin={{ top: 8, right: 8, left: -10, bottom: 0 }}>
              <CartesianGrid stroke="#21262d" vertical={false} />
              <XAxis dataKey="date" tick={{ fill: "#6b7280", fontSize: 11 }} tickFormatter={(d) => d.slice(5)} />
              <YAxis tick={{ fill: "#6b7280", fontSize: 11 }} />
              <Tooltip
                contentStyle={tooltipStyle}
                labelStyle={tooltipLabelStyle}
                itemStyle={tooltipItemStyle}
                cursor={{ fill: "rgba(255,255,255,0.04)" }}
                formatter={(v: number) => [`${v >= 0 ? "+" : ""}$${v.toFixed(2)}`, "Daily PnL"]}
              />
              {/* Zero line: bars run both ways, so break-even needs to be readable */}
              <ReferenceLine y={0} stroke="#484f58" />
              <Bar dataKey="pnl" name="Daily PnL" radius={[3, 3, 0, 0]}>
                {daily.map((d, i) => (
                  <Cell key={i} fill={d.pnl >= 0 ? "#00C896" : "#ef4444"} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        )}
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        {/* Action distribution pie */}
        <div className={card}>
          <h3 className={heading}>Decision Mix</h3>
          {actionData.length === 0 ? (
            <Empty msg="No decisions yet." />
          ) : (
            <ResponsiveContainer width="100%" height={220}>
              <PieChart>
                <Pie data={actionData} dataKey="value" nameKey="name" innerRadius={45} outerRadius={75} paddingAngle={3}>
                  {actionData.map((d, i) => (
                    <Cell key={i} fill={d.color} stroke="#161b22" />
                  ))}
                </Pie>
                <Tooltip contentStyle={tooltipStyle} labelStyle={tooltipLabelStyle} itemStyle={tooltipItemStyle} />
                <Legend wrapperStyle={legendStyle} />
              </PieChart>
            </ResponsiveContainer>
          )}
        </div>

        {/* Win/Loss pie */}
        <div className={card}>
          <h3 className={heading}>Win / Loss</h3>
          {winLossData.length === 0 ? (
            <Empty msg="No closed trades yet." />
          ) : (
            <ResponsiveContainer width="100%" height={220}>
              <PieChart>
                <Pie data={winLossData} dataKey="value" nameKey="name" innerRadius={45} outerRadius={75} paddingAngle={3}>
                  {winLossData.map((d, i) => (
                    <Cell key={i} fill={d.color} stroke="#161b22" />
                  ))}
                </Pie>
                <Tooltip contentStyle={tooltipStyle} labelStyle={tooltipLabelStyle} itemStyle={tooltipItemStyle} />
                <Legend wrapperStyle={legendStyle} />
              </PieChart>
            </ResponsiveContainer>
          )}
        </div>

        {/* Performance metrics */}
        <div className={card}>
          <h3 className={heading}>Performance</h3>
          <div className="space-y-2.5 text-sm">
            <Metric label="Win rate" value={pnl ? `${pnl.win_rate}%` : "—"} color={(pnl?.win_rate ?? 0) >= 50 ? "text-green-400" : "text-gray-300"} />
            <Metric label="Closed trades" value={pnl?.closed_trades ?? "—"} />
            <Metric label="Avg win" value={pnl ? `+$${pnl.avg_win}` : "—"} color="text-green-400" />
            <Metric label="Avg loss" value={pnl ? `$${pnl.avg_loss}` : "—"} color="text-red-400" />
            <Metric label="Best trade" value={pnl ? `+$${pnl.best_trade}` : "—"} color="text-green-400" />
            <Metric label="Worst trade" value={pnl ? `$${pnl.worst_trade}` : "—"} color="text-red-400" />
          </div>
        </div>
      </div>
    </div>
  );
}

function Metric({ label, value, color = "text-gray-300" }: { label: string; value: any; color?: string }) {
  return (
    <div className="flex items-center justify-between">
      <span className="text-gray-500">{label}</span>
      <span className={`font-semibold tabular-nums ${color}`}>{value}</span>
    </div>
  );
}
