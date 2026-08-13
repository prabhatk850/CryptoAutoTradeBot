"use client";
import { TrendingUp, TrendingDown, CalendarDays, Wallet, AlertTriangle } from "lucide-react";
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

function PnlCard({ label, value, icon: Icon, sub, unavailable = false }:
  { label: string; value: number | undefined; icon: any; sub?: string; unavailable?: boolean }) {
  const positive = (value ?? 0) >= 0;
  return (
    <div className="bg-[#161b22] border border-[#30363d] rounded-xl p-4 flex flex-col gap-2">
      <div className="flex items-center justify-between">
        <p className="text-xs text-gray-400 uppercase tracking-wider">{label}</p>
        <Icon size={16} className={unavailable ? "text-gray-600" : positive ? "text-green-400" : "text-red-400"} />
      </div>
      {/* An unreadable account must not render as $0.00 — that looks like a wipe. */}
      <p className={clsx("text-2xl font-bold tabular-nums",
        unavailable ? "text-gray-600" : positive ? "text-green-400" : "text-red-400")}>
        {unavailable ? "—" : money(value)}
      </p>
      <p className="text-xs text-gray-500">{unavailable ? "unavailable" : sub}</p>
    </div>
  );
}

export default function PnlCards({ pnl }: { pnl: Pnl | null }) {
  const isLive = pnl?.source === "delta";
  const broken = !!pnl && !isLive;   // account reachable? not the same as "no trades"
  return (
    <div className="space-y-3">
      {broken ? (
        <div className="flex items-start gap-2 text-xs bg-amber-500/10 border border-amber-500/40 rounded-lg px-3 py-2">
          <AlertTriangle size={14} className="text-amber-400 mt-px shrink-0" />
          <span className="text-amber-200">{pnl?.source_label}</span>
        </div>
      ) : pnl?.source_label ? (
        <div className="flex items-center gap-2 text-xs">
          <span className="w-2 h-2 rounded-full bg-green-400" />
          <span className="text-green-400">{pnl.source_label}</span>
        </div>
      ) : null}
      {/* Live position details live in the Open Positions panel above, not here. */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        <PnlCard label="Today's PnL" value={pnl?.today_pnl} unavailable={broken} icon={(pnl?.today_pnl ?? 0) >= 0 ? TrendingUp : TrendingDown} sub="Realized + open" />
        <PnlCard label="This Month" value={pnl?.month_pnl} unavailable={broken} icon={CalendarDays} sub="Month to date" />
        <PnlCard label="Total PnL" value={pnl?.total_pnl} unavailable={broken} icon={Wallet} sub="Since inception" />
      </div>
    </div>
  );
}
