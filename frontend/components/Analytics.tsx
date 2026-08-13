"use client";
import {
  ComposedChart, Bar, Line, XAxis, YAxis, Tooltip, ResponsiveContainer,
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

const tooltipStyle = {
  background: "#0d1117",
  border: "1px solid #30363d",
  borderRadius: 8,
  fontSize: 12,
} as const;

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
      {/* Daily PnL bar + cumulative line */}
      <div className={card}>
        <h3 className={heading}>Daily PnL &amp; Equity Curve</h3>
        {daily.length === 0 ? (
          <Empty msg="No closed trades yet — PnL chart fills as the bot trades." />
        ) : (
          <ResponsiveContainer width="100%" height={240}>
            <ComposedChart data={daily} margin={{ top: 8, right: 8, left: -10, bottom: 0 }}>
              <CartesianGrid stroke="#21262d" vertical={false} />
              <XAxis dataKey="date" tick={{ fill: "#6b7280", fontSize: 11 }} tickFormatter={(d) => d.slice(5)} />
              <YAxis tick={{ fill: "#6b7280", fontSize: 11 }} />
              <Tooltip contentStyle={tooltipStyle} formatter={(v: number) => `$${v}`} />
              <Bar dataKey="pnl" name="Daily PnL" radius={[3, 3, 0, 0]}>
                {daily.map((d, i) => (
                  <Cell key={i} fill={d.pnl >= 0 ? "#00C896" : "#ef4444"} />
                ))}
              </Bar>
              <Line type="monotone" dataKey="cumulative" name="Cumulative" stroke="#60a5fa" strokeWidth={2} dot={false} />
            </ComposedChart>
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
                    <Cell key={i} fill={d.color} />
                  ))}
                </Pie>
                <Tooltip contentStyle={tooltipStyle} />
                <Legend wrapperStyle={{ fontSize: 12 }} />
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
                    <Cell key={i} fill={d.color} />
                  ))}
                </Pie>
                <Tooltip contentStyle={tooltipStyle} />
                <Legend wrapperStyle={{ fontSize: 12 }} />
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
