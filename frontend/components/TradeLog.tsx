"use client";
import { useEffect, useState } from "react";
import { getOrders } from "../lib/api";
import { RefreshCw, ArrowUpRight, ArrowDownRight, ChevronLeft, ChevronRight } from "lucide-react";
import clsx from "clsx";

interface Trade {
  side: "LONG" | "SHORT";
  lots: number;
  avg_entry: number;
  avg_exit: number | null;
  realized_pnl: number | null;
  unrealized_pnl?: number | null;
  pnl_pct: number | null;
  status: "Open" | "Closed";
  entry_time: string | null;
  exit_time: string | null;
  reason: string;
  cumulative_pnl: number;
}

interface OrdersResponse {
  orders: Trade[];
  open_side: string;
  open_position: number;
  open_avg_entry: number;
  open_unrealized: number;
  total_realized: number;
  total: number;
  offset: number;
  limit: number;
}

const PAGE_SIZE = 10;

const ist = (iso: string | null) =>
  !iso ? "—" : new Date(iso).toLocaleString("en-IN", {
    timeZone: "Asia/Kolkata", day: "2-digit", month: "short",
    hour: "2-digit", minute: "2-digit", hour12: true,
  });

const money = (n: number | null | undefined) =>
  n == null ? "—" : `${n > 0 ? "+" : ""}$${n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

const px = (n: number | null | undefined) =>
  n == null ? "—" : `$${n.toLocaleString(undefined, { minimumFractionDigits: 1, maximumFractionDigits: 1 })}`;

export default function TradeLog({ symbol }: { symbol?: string }) {
  const [data, setData] = useState<OrdersResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [page, setPage] = useState(0);   // 0-based

  const fetchData = async () => {
    setLoading(true);
    try {
      const res = await getOrders(PAGE_SIZE, symbol, page * PAGE_SIZE);
      setData(res.data);
    } catch {
      /* backend momentarily unreachable — keep last data, retry next poll */
    } finally {
      setLoading(false);
    }
  };

  // reset to the first page when the coin changes
  useEffect(() => { setPage(0); }, [symbol]);

  useEffect(() => {
    fetchData();
    const iv = setInterval(fetchData, 30_000);
    return () => clearInterval(iv);
  }, [symbol, page]);  // eslint-disable-line react-hooks/exhaustive-deps

  const trades = data?.orders ?? [];
  const total = data?.total ?? 0;
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const from = total === 0 ? 0 : page * PAGE_SIZE + 1;
  const to = Math.min(page * PAGE_SIZE + trades.length, total);
  const canPrev = page > 0;
  const canNext = (page + 1) * PAGE_SIZE < total;

  return (
    <div className="bg-[#161b22] border border-[#30363d] rounded-xl">
      <div className="flex items-center justify-between px-5 py-4 border-b border-[#30363d]">
        <div>
          <h2 className="text-sm font-semibold text-gray-300 uppercase tracking-wider">Order History</h2>
          <p className="text-xs text-gray-500 mt-0.5">One row per trade — entry → exit. 10 per page; use Prev/Next for older trades.</p>
        </div>
        <button onClick={fetchData} className={clsx("text-gray-500 hover:text-gray-300 transition", loading && "animate-spin")}>
          <RefreshCw size={14} />
        </button>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="text-gray-500 border-b border-[#30363d]">
              <th className="px-4 py-2.5 text-left">Opened (IST)</th>
              <th className="px-4 py-2.5 text-left">Closed (IST)</th>
              <th className="px-4 py-2.5 text-left">Side</th>
              <th className="px-4 py-2.5 text-right">Entry</th>
              <th className="px-4 py-2.5 text-right">Exit</th>
              <th className="px-4 py-2.5 text-right">Lots</th>
              <th className="px-4 py-2.5 text-left">Reason</th>
              <th className="px-4 py-2.5 text-center">Status</th>
              <th className="px-4 py-2.5 text-right">P / L</th>
              <th className="px-4 py-2.5 text-right">Cumulative</th>
            </tr>
          </thead>
          <tbody>
            {trades.map((t, i) => {
              const open = t.status === "Open";
              const pl = open ? t.unrealized_pnl : t.realized_pnl;
              const plColor = pl == null ? "text-gray-500" : pl >= 0 ? "text-green-400" : "text-red-400";
              return (
                <tr key={i} className={clsx("border-b border-[#21262d] hover:bg-[#1c2128] transition", open && "bg-[#1c2128]/40")}>
                  <td className="px-4 py-2.5 text-gray-400 whitespace-nowrap">{ist(t.entry_time)}</td>
                  <td className="px-4 py-2.5 text-gray-400 whitespace-nowrap">{ist(t.exit_time)}</td>
                  <td className="px-4 py-2.5">
                    <span className={clsx("inline-flex items-center gap-1 px-2 py-0.5 rounded font-semibold",
                      t.side === "LONG" ? "text-green-400 bg-green-400/10" : "text-red-400 bg-red-400/10")}>
                      {t.side === "LONG" ? <ArrowUpRight size={12} /> : <ArrowDownRight size={12} />}{t.side}
                    </span>
                  </td>
                  <td className="px-4 py-2.5 text-right text-gray-300 font-mono">{px(t.avg_entry)}</td>
                  <td className="px-4 py-2.5 text-right text-gray-300 font-mono">{px(t.avg_exit)}</td>
                  <td className="px-4 py-2.5 text-right text-gray-300 font-mono">{t.lots}</td>
                  <td className="px-4 py-2.5 text-gray-400 max-w-md truncate" title={t.reason}>
                    {/^AI\b/.test(t.reason) && (
                      <span className="inline-block mr-1.5 px-1.5 py-0.5 rounded text-[10px] font-semibold text-purple-300 bg-purple-400/10 align-middle">🤖 AI</span>
                    )}
                    {t.reason}
                  </td>
                  <td className="px-4 py-2.5 text-center">
                    <span className={clsx("px-2 py-0.5 rounded text-[11px] font-medium",
                      open ? "text-yellow-400 bg-yellow-400/10" : "text-blue-300 bg-blue-400/10")}>
                      {t.status}
                    </span>
                  </td>
                  <td className={clsx("px-4 py-2.5 text-right font-mono font-semibold", plColor)}>
                    {money(pl)}
                    {!open && t.pnl_pct != null && <span className="text-gray-600 ml-1">({t.pnl_pct > 0 ? "+" : ""}{t.pnl_pct}%)</span>}
                  </td>
                  <td className="px-4 py-2.5 text-right font-mono text-gray-400">{money(t.cumulative_pnl)}</td>
                </tr>
              );
            })}
            {trades.length === 0 && (
              <tr>
                <td colSpan={10} className="px-4 py-10 text-center text-gray-600">
                  No trades yet — each completed trade shows here as one row (entry → exit).
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {/* pagination footer */}
      <div className="flex items-center justify-between px-5 py-3 border-t border-[#30363d] text-xs text-gray-500">
        <span>{total === 0 ? "No trades" : `Showing ${from}–${to} of ${total}`}</span>
        <div className="flex items-center gap-2">
          <button
            onClick={() => canPrev && setPage((p) => p - 1)}
            disabled={!canPrev}
            className="flex items-center gap-1 px-2.5 py-1 rounded-md border border-[#30363d] text-gray-300 hover:bg-[#21262d] disabled:opacity-40 disabled:cursor-not-allowed transition"
          >
            <ChevronLeft size={13} /> Prev
          </button>
          <span className="tabular-nums text-gray-400">Page {page + 1} / {pageCount}</span>
          <button
            onClick={() => canNext && setPage((p) => p + 1)}
            disabled={!canNext}
            className="flex items-center gap-1 px-2.5 py-1 rounded-md border border-[#30363d] text-gray-300 hover:bg-[#21262d] disabled:opacity-40 disabled:cursor-not-allowed transition"
          >
            Next <ChevronRight size={13} />
          </button>
        </div>
      </div>
    </div>
  );
}
