"use client";
import { useEffect, useState } from "react";
import { getAllPositions, closePosition, updateStopLoss } from "../lib/api";
import { ArrowUpRight, ArrowDownRight, Pencil, Check, X, Loader2, AlertTriangle } from "lucide-react";
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
  // What closing right now would really fill at (walks the live book). Mark price is
  // not tradeable, so this can differ sharply from `unrealized`.
  exit_price: number | null;
  exit_unrealized: number | null;
  slippage_pct: number | null;
  exit_liquidity_ok: boolean | null;
}

const money = (n: number) => `${n > 0 ? "+" : ""}$${n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

export default function OpenPositions({ activeSymbol }: { activeSymbol?: string }) {
  const [positions, setPositions] = useState<Pos[]>([]);
  const [busy, setBusy] = useState<string | null>(null);      // symbol currently acting on
  const [editSl, setEditSl] = useState<string | null>(null);  // symbol whose SL is being edited
  const [slInput, setSlInput] = useState("");
  // Tied to a symbol so a failed close surfaces on its own card — a bare string was
  // only ever rendered inside the SL editor, which silently swallowed close errors.
  const [err, setErr] = useState<{ symbol: string; msg: string } | null>(null);

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

  const doClose = async (p: Pos) => {
    // Quote the real fill up front — mark-based P/L is not what a market close realises.
    const est = p.exit_price != null
      ? `\n\nFills near $${p.exit_price.toLocaleString()} (mark $${p.mark.toLocaleString()})` +
        `\nReal P/L at that price: ${money(p.exit_unrealized ?? 0)}` +
        (p.exit_unrealized != null && p.unrealized > 0 && p.exit_unrealized < 0
          ? `\n\nWARNING: this position shows ${money(p.unrealized)} on mark but closing now LOSES money.`
          : "")
      : "";
    if (!window.confirm(`Close your ${p.symbol} position at market now?${est}`)) return;

    setBusy(p.symbol); setErr(null);
    try {
      let res = await closePosition(p.symbol);
      // Server refused on slippage: show its real numbers and let the user insist.
      if (!res.data?.ok && res.data?.needs_confirm) {
        if (!window.confirm(`${res.data.error}\n\nClose anyway?`)) {
          setBusy(null);
          return;
        }
        res = await closePosition(p.symbol, true);
      }
      if (!res.data?.ok) setErr({ symbol: p.symbol, msg: res.data?.error || "Close failed" });
      await fetchData();
    } catch (e: any) {
      setErr({ symbol: p.symbol, msg: e?.response?.data?.detail || e?.message || "Close failed" });
    } finally {
      setBusy(null);
    }
  };

  const startEditSl = (p: Pos) => { setEditSl(p.symbol); setSlInput(p.sl ? String(p.sl) : ""); setErr(null); };
  const saveSl = async (symbol: string) => {
    const price = parseFloat(slInput);
    if (!Number.isFinite(price) || price <= 0) { setErr({ symbol, msg: "Enter a valid SL price" }); return; }
    setBusy(symbol); setErr(null);
    try {
      const res = await updateStopLoss(symbol, price);
      if (!res.data?.ok) { setErr({ symbol, msg: res.data?.error || "SL update failed" }); }
      else { setEditSl(null); await fetchData(); }
    } catch (e: any) {
      setErr({ symbol, msg: e?.response?.data?.detail || e?.message || "SL update failed" });
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
      {/* max 2 cards per row — every card stretches to the same width/height */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4 items-stretch">
        {positions.map((p) => (
          <div
            key={p.symbol}
            className={clsx(
              "h-full flex flex-col rounded-lg border p-4 bg-[#0d1117]",
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

            {/* Real exit: mark P/L above is theoretical, this is what a close fills at. */}
            {p.exit_price != null && p.exit_unrealized != null && (
              <div
                className={clsx(
                  "mt-2 rounded px-2 py-1.5 text-[11px] border",
                  p.unrealized > 0 && p.exit_unrealized < 0
                    ? "bg-amber-500/10 border-amber-500/40"
                    : "bg-[#161b22] border-[#30363d]"
                )}
              >
                <div className="flex items-center justify-between">
                  <span className="text-gray-400">If closed now</span>
                  <span className={clsx("font-mono font-semibold", p.exit_unrealized >= 0 ? "text-green-400" : "text-red-400")}>
                    {money(p.exit_unrealized)}
                  </span>
                </div>
                <div className="flex items-center justify-between mt-0.5 text-[10px] text-gray-500">
                  <span className="font-mono">@ ${p.exit_price.toLocaleString()}</span>
                  {p.slippage_pct != null && <span>{p.slippage_pct}% off mark</span>}
                </div>
                {p.unrealized > 0 && p.exit_unrealized < 0 && (
                  <div className="mt-1 flex items-start gap-1 text-[10px] text-amber-300">
                    <AlertTriangle size={11} className="mt-px shrink-0" />
                    <span>Mark shows profit, but the book only fills at a loss.</span>
                  </div>
                )}
                {p.exit_liquidity_ok === false && (
                  <div className="mt-1 text-[10px] text-amber-300">
                    Book too thin to fill the whole size — expect worse.
                  </div>
                )}
              </div>
            )}
            {err?.symbol === p.symbol && (
              <div className="mt-1.5 text-[10px] text-red-400 bg-red-500/10 border border-red-500/30 rounded px-2 py-1">
                {err.msg}
              </div>
            )}
            {/* pt-3 spacer with mt-auto keeps the button flush with the bottom of every card */}
            <div className="mt-auto pt-3">
              <button
                onClick={() => doClose(p)}
                disabled={busy === p.symbol}
                className="w-full flex items-center justify-center gap-1.5 py-2 rounded-md text-[11px] font-semibold text-red-300 bg-red-500/10 hover:bg-red-500/20 border border-red-500/30 transition disabled:opacity-50"
              >
                {busy === p.symbol ? <Loader2 size={12} className="animate-spin" /> : <X size={12} />}
                Close position
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
