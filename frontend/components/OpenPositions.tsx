"use client";
import { useEffect, useState } from "react";
import { getAllPositions, closePosition, updateStopLoss } from "../lib/api";
import { ArrowUpRight, ArrowDownRight, Pencil, Check, X, Loader2 } from "lucide-react";
import clsx from "clsx";

interface Pos {
  symbol: string;
  side: "LONG" | "SHORT";
  size: number;
  entry: number;
  mark: number;
  unrealized: number;
  pnl_pct: number;
  sl: number | null;
  tps: number[];
}

const money = (n: number) => `${n > 0 ? "+" : ""}$${n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

export default function OpenPositions({ activeSymbol }: { activeSymbol?: string }) {
  const [positions, setPositions] = useState<Pos[]>([]);
  const [busy, setBusy] = useState<string | null>(null);      // symbol currently acting on
  const [editSl, setEditSl] = useState<string | null>(null);  // symbol whose SL is being edited
  const [slInput, setSlInput] = useState("");
  const [err, setErr] = useState<string | null>(null);

  const fetchData = async () => {
    try {
      const res = await getAllPositions();
      setPositions(res.data.positions ?? []);
    } catch {
      /* keep last */
    }
  };

  useEffect(() => {
    fetchData();
    const iv = setInterval(fetchData, 15_000);
    return () => clearInterval(iv);
  }, []);

  const doClose = async (symbol: string) => {
    if (!window.confirm(`Close your ${symbol} position at market now?`)) return;
    setBusy(symbol); setErr(null);
    try {
      const res = await closePosition(symbol);
      if (!res.data?.ok) setErr(res.data?.error || "Close failed");
      await fetchData();
    } catch (e: any) {
      setErr(e?.message || "Close failed");
    } finally {
      setBusy(null);
    }
  };

  const startEditSl = (p: Pos) => { setEditSl(p.symbol); setSlInput(p.sl ? String(p.sl) : ""); setErr(null); };
  const saveSl = async (symbol: string) => {
    const price = parseFloat(slInput);
    if (!Number.isFinite(price) || price <= 0) { setErr("Enter a valid SL price"); return; }
    setBusy(symbol); setErr(null);
    try {
      const res = await updateStopLoss(symbol, price);
      if (!res.data?.ok) { setErr(res.data?.error || "SL update failed"); }
      else { setEditSl(null); await fetchData(); }
    } catch (e: any) {
      setErr(e?.message || "SL update failed");
    } finally {
      setBusy(null);
    }
  };

  if (positions.length === 0) return null;
  const totalUnreal = positions.reduce((a, p) => a + (p.unrealized || 0), 0);

  return (
    <div className="bg-[#161b22] border border-[#30363d] rounded-xl p-4">
      <div className="flex items-center justify-between mb-3">
        <h2 className="text-sm font-semibold text-gray-300 uppercase tracking-wider">Open Positions (all coins)</h2>
        <span className="text-xs text-gray-500">
          {positions.length} live · managed by bot · total unrl{" "}
          <span className={totalUnreal >= 0 ? "text-green-400" : "text-red-400"}>{money(totalUnreal)}</span>
        </span>
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
        {positions.map((p) => (
          <div
            key={p.symbol}
            className={clsx(
              "rounded-lg border p-3 bg-[#0d1117]",
              p.symbol === activeSymbol ? "border-[#00C896]/50" : "border-[#30363d]"
            )}
          >
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <span className="font-semibold text-gray-200">{p.symbol}</span>
                <span className={clsx("inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded text-[11px] font-semibold",
                  p.side === "LONG" ? "text-green-400 bg-green-400/10" : "text-red-400 bg-red-400/10")}>
                  {p.side === "LONG" ? <ArrowUpRight size={11} /> : <ArrowDownRight size={11} />}{p.side} {p.size}
                </span>
              </div>
              <span className={clsx("text-sm font-bold tabular-nums", p.unrealized >= 0 ? "text-green-400" : "text-red-400")}>
                {money(p.unrealized)}
                <span className="text-[11px] text-gray-500 ml-1">({p.pnl_pct > 0 ? "+" : ""}{p.pnl_pct}%)</span>
              </span>
            </div>
            <div className="mt-2 grid grid-cols-3 gap-2 text-[11px]">
              <div><div className="text-gray-500">Entry</div><div className="text-gray-300 font-mono">${p.entry.toLocaleString()}</div></div>
              <div><div className="text-gray-500">Mark</div><div className="text-gray-300 font-mono">${p.mark.toLocaleString()}</div></div>
              <div>
                <div className="text-gray-500 flex items-center gap-1">
                  SL
                  {editSl !== p.symbol && (
                    <button onClick={() => startEditSl(p)} title="Edit stop-loss"
                      className="text-gray-500 hover:text-gray-200"><Pencil size={10} /></button>
                  )}
                </div>
                {editSl === p.symbol ? (
                  <div className="flex items-center gap-1 mt-0.5">
                    <input
                      autoFocus type="number" value={slInput}
                      onChange={(e) => setSlInput(e.target.value)}
                      onKeyDown={(e) => { if (e.key === "Enter") saveSl(p.symbol); if (e.key === "Escape") setEditSl(null); }}
                      className="w-20 bg-[#161b22] border border-[#30363d] rounded px-1 py-0.5 text-[11px] text-gray-200 font-mono focus:outline-none focus:border-[#00C896]"
                    />
                    <button onClick={() => saveSl(p.symbol)} disabled={busy === p.symbol} title="Save"
                      className="text-green-400 hover:text-green-300 disabled:opacity-50">
                      {busy === p.symbol ? <Loader2 size={12} className="animate-spin" /> : <Check size={12} />}
                    </button>
                    <button onClick={() => setEditSl(null)} title="Cancel" className="text-gray-500 hover:text-gray-300"><X size={12} /></button>
                  </div>
                ) : (
                  <div className="text-red-400 font-mono">{p.sl ? `$${p.sl.toLocaleString()}` : "—"}</div>
                )}
              </div>
            </div>
            {p.tps.length > 0 && (
              <div className="mt-1.5 text-[11px]">
                <span className="text-gray-500">TPs: </span>
                <span className="text-green-400 font-mono">{p.tps.map((t) => `$${t.toLocaleString()}`).join(" · ")}</span>
              </div>
            )}
            {err && editSl === p.symbol && <div className="mt-1 text-[10px] text-red-400">{err}</div>}
            <button
              onClick={() => doClose(p.symbol)}
              disabled={busy === p.symbol}
              className="mt-2.5 w-full flex items-center justify-center gap-1.5 py-1.5 rounded-md text-[11px] font-semibold text-red-300 bg-red-500/10 hover:bg-red-500/20 border border-red-500/30 transition disabled:opacity-50"
            >
              {busy === p.symbol ? <Loader2 size={12} className="animate-spin" /> : <X size={12} />}
              Close position
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}
