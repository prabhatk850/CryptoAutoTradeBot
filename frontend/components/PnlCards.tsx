"use client";
import { TrendingUp, TrendingDown, CalendarDays, Wallet } from "lucide-react";
import clsx from "clsx";

export interface Pnl {
  today_pnl: number;
  month_pnl: number;
  total_pnl: number;
  realized_pnl: number;
  unrealized_pnl: number;
  open_position: number;
  avg_entry: number;
  current_price: number;
  closed_trades: number;
  wins: number;
  losses: number;
  win_rate: number;
  avg_win: number;
  avg_loss: number;
  best_trade: number;
  worst_trade: number;
  daily: { date: string; pnl: number; cumulative: number }[];
  source?: "delta" | "simulated";
  source_label?: string;
  fills_count?: number;
  open_count?: number;
}

function money(n: number | undefined) {
  if (n == null) return "—";
  const sign = n > 0 ? "+" : n < 0 ? "" : "";
  return `${sign}$${n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function PnlCard({ label, value, icon: Icon, sub }: { label: string; value: number | undefined; icon: any; sub?: string }) {
  const positive = (value ?? 0) >= 0;
  return (
    <div className="bg-[#161b22] border border-[#30363d] rounded-xl p-4 flex flex-col gap-2">
      <div className="flex items-center justify-between">
        <p className="text-xs text-gray-400 uppercase tracking-wider">{label}</p>
        <Icon size={16} className={positive ? "text-green-400" : "text-red-400"} />
      </div>
      <p className={clsx("text-2xl font-bold tabular-nums", positive ? "text-green-400" : "text-red-400")}>
        {money(value)}
      </p>
      {sub && <p className="text-xs text-gray-500">{sub}</p>}
    </div>
  );
}

export default function PnlCards({ pnl }: { pnl: Pnl | null }) {
  const isLive = pnl?.source === "delta";
  return (
    <div className="space-y-3">
      {pnl?.source_label && (
        <div className="flex items-center gap-2 text-xs">
          <span className={clsx("w-2 h-2 rounded-full", isLive ? "bg-green-400" : "bg-yellow-400")} />
          <span className={isLive ? "text-green-400" : "text-yellow-400"}>{pnl.source_label}</span>
        </div>
      )}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
      <PnlCard label="Today's PnL" value={pnl?.today_pnl} icon={(pnl?.today_pnl ?? 0) >= 0 ? TrendingUp : TrendingDown} sub="Realized + open" />
      <PnlCard label="This Month" value={pnl?.month_pnl} icon={CalendarDays} sub="Month to date" />
      <PnlCard label="Total PnL" value={pnl?.total_pnl} icon={Wallet} sub="Since inception" />
      <div className="bg-[#161b22] border border-[#30363d] rounded-xl p-4 flex flex-col gap-2">
        <p className="text-xs text-gray-400 uppercase tracking-wider">Open Position</p>
        <p className="text-2xl font-bold text-white tabular-nums">
          {pnl?.open_position ? `${pnl.open_position > 0 ? "LONG" : "SHORT"} ${Math.abs(pnl.open_position)}` : "Flat"}
        </p>
        <p className="text-xs text-gray-500">
          {pnl?.open_position ? `@ $${pnl.avg_entry?.toLocaleString()} · unrl ${money(pnl?.unrealized_pnl)}` : "No contracts held"}
        </p>
      </div>
      </div>
    </div>
  );
}
