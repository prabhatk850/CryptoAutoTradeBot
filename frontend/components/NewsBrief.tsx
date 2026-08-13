"use client";
import { useEffect, useState } from "react";
import { getBrief } from "../lib/api";
import { Coffee, RefreshCw, ChevronUp, TrendingUp, TrendingDown, Eye, AlertTriangle, Minus, Crosshair } from "lucide-react";
import clsx from "clsx";

interface Catalyst { title: string; source: string; impact: "bullish" | "bearish" | "neutral"; why: string }
interface MarketRow {
  symbol: string; price: number; change_24h_pct: number; rsi_1h?: number | null;
  funding_rate?: number; high_24h?: number; low_24h?: number; trend_1h?: string;
}
interface Bias {
  direction: "bullish" | "bearish" | "neutral";
  confidence: number;
  rationale: string;
  invalidation: string;
  support: number | null;
  resistance: number | null;
}
interface Brief {
  headline: string; bias?: Bias; catalysts: Catalyst[]; themes: string[]; watch: string[];
  risk: string; market: MarketRow[]; via?: string; articles_used?: number;
  rss_used?: number; telegram_used?: number; generated_at?: string;
}

const BIAS_STYLE: Record<string, { box: string; text: string; bar: string; Icon: any }> = {
  bullish: { box: "bg-green-500/10 border-green-500/40", text: "text-green-400", bar: "bg-green-400", Icon: TrendingUp },
  bearish: { box: "bg-red-500/10 border-red-500/40", text: "text-red-400", bar: "bg-red-400", Icon: TrendingDown },
  neutral: { box: "bg-gray-500/10 border-gray-500/40", text: "text-gray-300", bar: "bg-gray-400", Icon: Minus },
};

const IMPACT_DOT: Record<string, string> = {
  bullish: "bg-green-400",
  bearish: "bg-red-400",
  neutral: "bg-gray-500",
};

function hhmm(iso?: string) {
  if (!iso) return "";
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export default function NewsBrief({ open, onToggle }: { open: boolean; onToggle: () => void }) {
  const [brief, setBrief] = useState<Brief | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [loadedOnce, setLoadedOnce] = useState(false);

  // Read the cached brief when first opened — never auto-generates (that costs tokens).
  useEffect(() => {
    if (!open || loadedOnce) return;
    setLoadedOnce(true);
    (async () => {
      try {
        const res = await getBrief(false);
        setBrief(res.data?.brief ?? null);
      } catch { /* leave empty; the button can generate one */ }
    })();
  }, [open, loadedOnce]);

  const generate = async () => {
    setLoading(true); setErr(null);
    try {
      const res = await getBrief(true);
      // A configuration problem comes back as a 200 with `error`: show it, but still
      // render whatever cached brief accompanied it.
      if (res.data?.brief) setBrief(res.data.brief);
      if (res.data?.error) setErr(res.data.error);
      else if (!res.data?.brief) setErr("Could not generate a brief — the AI returned nothing.");
    } catch (e: any) {
      setErr(e?.response?.data?.detail || e?.message || "Brief generation failed");
    } finally {
      setLoading(false);
    }
  };

  if (!open) return null;

  return (
    <div className="bg-[#0d1117] border border-amber-500/30 rounded-lg overflow-hidden flex flex-col shrink-0">
      <button
        onClick={onToggle}
        className="w-full flex items-center gap-2 px-3 py-2 border-b border-[#30363d] hover:bg-[#161b22] transition shrink-0"
      >
        <Coffee size={12} className="text-amber-400 shrink-0" />
        <span className="text-[11px] font-semibold text-amber-400 uppercase tracking-wider">24hr-market brief</span>
        <span className="text-[10px] text-gray-600 ml-auto">
          {brief?.generated_at ? `${hhmm(brief.generated_at)} · ${brief.articles_used} articles` : "not generated"}
        </span>
        <ChevronUp size={12} className="text-gray-600 shrink-0" />
      </button>

      {/* Body scrolls on its own so the brief can be long without burying the
          refresh control or pushing the news list off-screen. */}
      <div className="p-3 space-y-3 overflow-y-auto max-h-[320px]">
        {!brief && !loading && (
          <p className="text-[11px] text-gray-500">
            No brief yet. Generate one to summarise the last 24h of headlines.
          </p>
        )}

        {brief && (
          <>
            <p className="text-xs text-gray-200 font-medium leading-snug">{brief.headline}</p>

            {/* Day bias — the session lean drawn from news + Telegram + technicals */}
            {brief.bias && (() => {
              const st = BIAS_STYLE[brief.bias.direction] ?? BIAS_STYLE.neutral;
              return (
                <div className={clsx("rounded-lg border p-2.5", st.box)}>
                  <div className="flex items-center gap-2">
                    <st.Icon size={15} className={st.text} />
                    <span className={clsx("text-sm font-bold uppercase tracking-wide", st.text)}>
                      {brief.bias.direction}
                    </span>
                    <span className="text-[10px] text-gray-500 uppercase tracking-wider">day bias</span>
                    <span className="ml-auto text-[10px] text-gray-400 tabular-nums">
                      {brief.bias.confidence}% confidence
                    </span>
                  </div>
                  <div className="mt-1.5 h-1 rounded-full bg-[#0d1117] overflow-hidden">
                    <div className={clsx("h-full rounded-full", st.bar)}
                      style={{ width: `${Math.max(0, Math.min(100, brief.bias.confidence))}%` }} />
                  </div>
                  {brief.bias.rationale && (
                    <p className="text-[11px] text-gray-300 leading-snug mt-1.5">{brief.bias.rationale}</p>
                  )}
                  {(brief.bias.support || brief.bias.resistance) && (
                    <div className="flex items-center gap-3 mt-1.5 text-[10px] font-mono">
                      {brief.bias.support && (
                        <span className="text-gray-500">S <span className="text-sky-300">{brief.bias.support.toLocaleString()}</span></span>
                      )}
                      {brief.bias.resistance && (
                        <span className="text-gray-500">R <span className="text-amber-300">{brief.bias.resistance.toLocaleString()}</span></span>
                      )}
                    </div>
                  )}
                  {brief.bias.invalidation && (
                    <p className="flex items-start gap-1 text-[10px] text-gray-500 mt-1.5">
                      <Crosshair size={10} className="mt-0.5 shrink-0" />
                      <span>Invalidated if: {brief.bias.invalidation}</span>
                    </p>
                  )}
                </div>
              );
            })()}

            {/* Facts, computed from the exchange — not model output */}
            {brief.market?.length > 0 && (
              <div>
                <p className="text-[10px] text-gray-600 uppercase tracking-wider mb-1">Overnight moves</p>
                <div className="space-y-1">
                  {brief.market.map((m) => (
                    <div key={m.symbol} className="flex items-center gap-2 bg-[#161b22] rounded px-2 py-1.5">
                      <span className="text-[11px] font-semibold text-gray-300 w-14 shrink-0">
                        {m.symbol.replace("USD", "")}
                      </span>
                      <span className={clsx("flex items-center gap-0.5 text-[11px] font-mono font-semibold w-16 shrink-0",
                        m.change_24h_pct >= 0 ? "text-green-400" : "text-red-400")}>
                        {m.change_24h_pct >= 0 ? <TrendingUp size={10} /> : <TrendingDown size={10} />}
                        {m.change_24h_pct >= 0 ? "+" : ""}{m.change_24h_pct}%
                      </span>
                      <span className="text-[10px] text-gray-500 truncate">
                        ${m.price?.toLocaleString()}
                        {m.rsi_1h != null && (
                          <> · RSI <span className={clsx(
                            m.rsi_1h >= 70 ? "text-amber-400" : m.rsi_1h <= 30 ? "text-sky-400" : "text-gray-400"
                          )}>{m.rsi_1h}</span></>
                        )}
                        {m.trend_1h && (
                          <> · 1h <span className={m.trend_1h === "up" ? "text-green-400" : "text-red-400"}>{m.trend_1h}</span></>
                        )}
                        {!!m.funding_rate && <> · funding {m.funding_rate}</>}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {brief.catalysts?.length > 0 && (
              <div>
                <p className="text-[10px] text-gray-600 uppercase tracking-wider mb-1">Top catalysts</p>
                <div className="space-y-1.5">
                  {brief.catalysts.map((c, i) => (
                    <div key={i} className="bg-[#161b22] rounded px-2 py-1.5">
                      <div className="flex items-start gap-1.5">
                        <span className={clsx("w-1.5 h-1.5 rounded-full mt-1 shrink-0", IMPACT_DOT[c.impact])} />
                        <p className="text-[11px] text-gray-300 leading-snug">{c.title}</p>
                      </div>
                      <p className="text-[10px] text-gray-500 mt-0.5 pl-3">{c.why}</p>
                      {c.source && (
                        <span className="inline-block mt-1 ml-3 px-1.5 py-px rounded bg-[#0d1117] border border-[#30363d] text-[9px] text-gray-500">
                          {c.source}
                        </span>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            )}

            {brief.themes?.length > 0 && (
              <div className="flex flex-wrap gap-1">
                {brief.themes.map((t, i) => (
                  <span key={i} className="px-1.5 py-0.5 rounded bg-[#00C896]/10 text-[#00C896] border border-[#00C896]/25 text-[10px]">
                    {t}
                  </span>
                ))}
              </div>
            )}

            {brief.watch?.length > 0 && (
              <div>
                <p className="text-[10px] text-gray-600 uppercase tracking-wider mb-1 flex items-center gap-1">
                  <Eye size={10} /> Today&apos;s watch
                </p>
                <ul className="space-y-0.5">
                  {brief.watch.map((w, i) => (
                    <li key={i} className="text-[11px] text-gray-400 flex gap-1.5">
                      <span className="text-gray-600">·</span>{w}
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {brief.risk && (
              <div className="flex items-start gap-1.5 text-[10px] text-amber-300/90 bg-amber-500/5 border border-amber-500/20 rounded px-2 py-1.5">
                <AlertTriangle size={11} className="mt-px shrink-0" />
                <span>{brief.risk}</span>
              </div>
            )}
          </>
        )}

        {err && <p className="text-[10px] text-red-400">{err}</p>}

        {brief?.via && (
          <p className="text-[9px] text-gray-700 text-center leading-relaxed">
            {brief.via} · {brief.rss_used ?? 0} articles + {brief.telegram_used ?? 0} channel posts + live market data
            <br />prices and levels read from the exchange · context, not a trade instruction
          </p>
        )}
      </div>

      {/* Pinned outside the scroll area — always reachable however long the brief is */}
      <div className="p-2 border-t border-[#30363d] bg-[#0d1117] shrink-0">
        <button
          onClick={generate}
          disabled={loading}
          title="Re-reads the last 24h of headlines, channel posts and live prices"
          className="w-full flex items-center justify-center gap-1.5 py-1.5 rounded-md text-[11px] font-semibold
                     text-amber-300 bg-amber-500/10 hover:bg-amber-500/20 border border-amber-500/30
                     transition disabled:opacity-50"
        >
          <RefreshCw size={11} className={clsx(loading && "animate-spin")} />
          {loading ? "Analysing latest news…" : brief ? "Refresh brief" : "Generate brief"}
        </button>
      </div>
    </div>
  );
}
