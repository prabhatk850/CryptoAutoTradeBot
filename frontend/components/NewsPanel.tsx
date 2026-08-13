"use client";
import { useEffect, useState } from "react";
import { Newspaper, CalendarClock, ExternalLink, Coffee, Send } from "lucide-react";
import clsx from "clsx";
import { getNewsAll } from "../lib/api";
import NewsBrief from "./NewsBrief";

interface Story {
  source: string;
  title: string;
  link: string;
  summary: string;
  published: string | null;
  sentiment: "bullish" | "bearish" | "neutral";
}
interface TgPost {
  source: string;
  channel: string;
  post_id: string;
  title: string;
  text: string;
  link: string;
  published: string | null;
  sentiment: "bullish" | "bearish" | "neutral";
}
interface Event {
  title: string;
  currency: string;
  impact: string;
  impact_rank: number;
  forecast: string;
  previous: string;
  actual: string;
  date: string | null;
}

const SENTIMENT_DOT: Record<string, string> = {
  bullish: "bg-green-400",
  bearish: "bg-red-400",
  neutral: "bg-gray-500",
};

// Per-outlet chip colour, so you can tell at a glance where a headline came from.
// Anything unrecognised falls back to neutral grey rather than going uncoloured.
const SOURCE_CHIP: Record<string, string> = {
  cointelegraph: "text-amber-300 bg-amber-500/10 border-amber-500/25",
  coindesk: "text-sky-300 bg-sky-500/10 border-sky-500/25",
  decrypt: "text-violet-300 bg-violet-500/10 border-violet-500/25",
  "the defiant": "text-pink-300 bg-pink-500/10 border-pink-500/25",
  cryptoslate: "text-teal-300 bg-teal-500/10 border-teal-500/25",
  "bitcoin.com": "text-orange-300 bg-orange-500/10 border-orange-500/25",
  "yahoo finance": "text-indigo-300 bg-indigo-500/10 border-indigo-500/25",
  fxstreet: "text-emerald-300 bg-emerald-500/10 border-emerald-500/25",
  forexlive: "text-lime-300 bg-lime-500/10 border-lime-500/25",
  investing: "text-rose-300 bg-rose-500/10 border-rose-500/25",
};

const sourceChip = (source: string) =>
  SOURCE_CHIP[(source || "").toLowerCase()] ?? "text-gray-400 bg-gray-500/10 border-gray-600/30";

// Impact drives both the left rail and the currency chip, so severity is scannable
// down the column. Low is sky rather than yellow — amber/yellow read as the same
// colour at this size.
function impactColor(rank: number) {
  if (rank >= 3) return { bar: "bg-red-500", chip: "text-red-300 bg-red-500/15" };        // High
  if (rank === 2) return { bar: "bg-amber-500", chip: "text-amber-300 bg-amber-500/15" }; // Medium
  if (rank === 1) return { bar: "bg-sky-500", chip: "text-sky-300 bg-sky-500/15" };       // Low
  return { bar: "bg-gray-600", chip: "text-gray-400 bg-gray-500/15" };                    // Holiday/other
}

function timeAgo(iso: string | null) {
  if (!iso) return "";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

function eventTime(iso: string | null) {
  if (!iso) return { label: "—", soon: false, past: false };
  const d = new Date(iso);
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  const time = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const diffMin = (d.getTime() - now.getTime()) / 60000;
  return {
    label: sameDay ? time : `${d.toLocaleDateString([], { weekday: "short" })} ${time}`,
    soon: diffMin >= 0 && diffMin <= 60,   // within the next hour
    past: diffMin < -5,
  };
}

export default function NewsPanel() {
  const [stories, setStories] = useState<Story[]>([]);
  const [events, setEvents] = useState<Event[]>([]);
  const [posts, setPosts] = useState<TgPost[]>([]);
  const [updated, setUpdated] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [briefOpen, setBriefOpen] = useState(false);
  const [tab, setTab] = useState<"news" | "telegram">("news");

  const fetchData = async () => {
    try {
      const res = await getNewsAll();
      setStories(res.data.stories ?? []);
      setEvents(res.data.events ?? []);
      setPosts(res.data.telegram ?? []);
      setUpdated(res.data.news_updated_at ?? null);
    } catch {
      /* keep last data on a transient error */
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchData();
    const iv = setInterval(fetchData, 60_000);
    return () => clearInterval(iv);
  }, []);

  // Upcoming-first: keep events from ~1h ago onward; fall back to the whole week if none.
  const now = Date.now();
  const upcoming = events.filter((e) => e.date && new Date(e.date).getTime() >= now - 3600_000);
  // The panel scrolls, so keep the cap well above a full ForexFactory week (~70 events).
  const calRows = (upcoming.length ? upcoming : events).slice(0, 100);

  // On lg the panel is absolutely pinned to its (relative) column so it spans exactly
  // from the top of the page to the bottom of the chart card without stretching the
  // row; the two lists below split that height and scroll inside it.
  return (
    <div className="bg-[#161b22] border border-[#30363d] rounded-xl p-3 flex flex-col gap-3 lg:absolute lg:inset-0">
      <div className="flex items-center justify-between px-1">
        <h2 className="text-sm font-semibold text-gray-300 uppercase tracking-wider">Market News</h2>
        <span className="flex items-center gap-1.5 text-[11px] text-gray-500">
          <span className="w-1.5 h-1.5 rounded-full bg-green-400 animate-pulse" />
          {updated ? `updated ${timeAgo(updated)}` : "live"}
        </span>
      </div>

      {/* AI pre-market brief — opened from the Brief button below */}
      <NewsBrief open={briefOpen} onToggle={() => setBriefOpen(false)} />

      {/* Inner container 1 — Latest news headlines */}
      <div className="bg-[#0d1117] border border-[#30363d] rounded-lg overflow-hidden flex flex-col lg:flex-1 lg:min-h-0">
        {/* Tabs: RSS outlets vs Telegram channels — two separate streams */}
        <div className="flex items-center gap-1 px-2 py-1.5 border-b border-[#30363d] shrink-0">
          {([
            { id: "news", icon: Newspaper, label: "Latest News", n: stories.length },
            { id: "telegram", icon: Send, label: "Telegram", n: posts.length },
          ] as const).map((t) => (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              className={clsx(
                "flex items-center gap-1 px-2 py-1 rounded text-[10px] font-semibold uppercase tracking-wider transition",
                tab === t.id
                  ? "bg-[#161b22] text-gray-200 border border-[#30363d]"
                  : "text-gray-500 border border-transparent hover:text-gray-300"
              )}
            >
              <t.icon size={11} /> {t.label}
              <span className="text-[9px] font-normal text-gray-600">{t.n}</span>
            </button>
          ))}
          {!briefOpen && (
            <button
              onClick={() => setBriefOpen(true)}
              title="AI summary of the last 24h of headlines and channel posts"
              className="ml-auto flex items-center gap-1 px-2 py-0.5 rounded border border-amber-500/40 bg-amber-500/10
                         text-amber-400 text-[10px] font-semibold hover:bg-amber-500/20 transition"
            >
              <Coffee size={10} /> Brief
            </button>
          )}
        </div>
        <div className="max-h-[300px] lg:max-h-none lg:flex-1 lg:min-h-0 overflow-y-auto divide-y divide-[#30363d]/60">
          {tab === "telegram" ? (
            loading && posts.length === 0 ? (
              <p className="px-3 py-4 text-xs text-gray-600">Loading channel posts…</p>
            ) : posts.length === 0 ? (
              <p className="px-3 py-4 text-xs text-gray-600">No channel posts right now.</p>
            ) : (
              posts.map((p) => (
                <a
                  key={p.post_id || p.link}
                  href={p.link || undefined}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="group block px-3 py-2 hover:bg-[#161b22] transition"
                >
                  <div className="flex items-center gap-2 mb-0.5">
                    <span className={clsx("w-1.5 h-1.5 rounded-full shrink-0", SENTIMENT_DOT[p.sentiment])} />
                    {/* channel chip — which Telegram channel this post came from */}
                    <span className="px-1.5 py-px rounded text-[9px] font-semibold shrink-0 border truncate max-w-[130px]
                                     text-[#29b6f6] bg-[#29b6f6]/10 border-[#29b6f6]/25 flex items-center gap-0.5">
                      <Send size={8} /> {p.source}
                    </span>
                    <span className="text-[10px] text-gray-600 ml-auto shrink-0">{timeAgo(p.published)}</span>
                  </div>
                  <p className="text-xs text-gray-300 leading-snug group-hover:text-white flex items-start gap-1">
                    <span className="line-clamp-3">{p.text}</span>
                    <ExternalLink size={10} className="mt-0.5 shrink-0 text-gray-600 opacity-0 group-hover:opacity-100 transition" />
                  </p>
                </a>
              ))
            )
          ) : loading && stories.length === 0 ? (
            <p className="px-3 py-4 text-xs text-gray-600">Loading headlines…</p>
          ) : stories.length === 0 ? (
            <p className="px-3 py-4 text-xs text-gray-600">No headlines right now.</p>
          ) : (
            stories.map((s, i) => (
              <a
                key={i}
                href={s.link || undefined}
                target="_blank"
                rel="noopener noreferrer"
                className="group block px-3 py-2 hover:bg-[#161b22] transition"
              >
                <div className="flex items-center gap-2 mb-0.5">
                  <span className={clsx("w-1.5 h-1.5 rounded-full shrink-0", SENTIMENT_DOT[s.sentiment])} />
                  {/* source chip — which outlet this headline came from */}
                  <span className={clsx(
                    "px-1.5 py-px rounded text-[9px] font-semibold shrink-0 border truncate max-w-[110px]",
                    sourceChip(s.source),
                  )}>
                    {s.source}
                  </span>
                  <span className="text-[10px] text-gray-600 ml-auto shrink-0">{timeAgo(s.published)}</span>
                </div>
                <p className="text-xs text-gray-300 leading-snug group-hover:text-white flex items-start gap-1">
                  <span className="line-clamp-2">{s.title}</span>
                  <ExternalLink size={10} className="mt-0.5 shrink-0 text-gray-600 opacity-0 group-hover:opacity-100 transition" />
                </p>
              </a>
            ))
          )}
        </div>
      </div>

      {/* Inner container 2 — Economic calendar / forecast */}
      <div className="bg-[#0d1117] border border-[#30363d] rounded-lg overflow-hidden flex flex-col lg:flex-1 lg:min-h-0">
        <div className="flex items-center gap-1.5 px-3 py-2 border-b border-[#30363d] text-[11px] font-semibold text-gray-400 uppercase tracking-wider shrink-0">
          <CalendarClock size={12} /> Economic Calendar
          {/* legend for the impact rail */}
          <span className="ml-auto flex items-center gap-1.5 normal-case tracking-normal font-normal text-[10px] text-gray-600">
            <span className="w-1.5 h-1.5 rounded-full bg-red-500" />High
            <span className="w-1.5 h-1.5 rounded-full bg-amber-500 ml-1" />Med
            <span className="w-1.5 h-1.5 rounded-full bg-sky-500 ml-1" />Low
          </span>
        </div>
        <div className="max-h-[240px] lg:max-h-none lg:flex-1 lg:min-h-0 overflow-y-auto divide-y divide-[#30363d]/60">
          {loading && calRows.length === 0 ? (
            <p className="px-3 py-4 text-xs text-gray-600">Loading events…</p>
          ) : calRows.length === 0 ? (
            <p className="px-3 py-4 text-xs text-gray-600">No events available.</p>
          ) : (
            calRows.map((e, i) => {
              const c = impactColor(e.impact_rank);
              const t = eventTime(e.date);
              return (
                <div
                  key={i}
                  className={clsx("relative pl-4 pr-3 py-2 hover:bg-[#161b22] transition", t.past && "opacity-50")}
                >
                  {/* impact rail */}
                  <span className={clsx("absolute left-0 top-0 bottom-0 w-[3px]", c.bar)} title={e.impact} />
                  <div className="flex items-center gap-2">
                    <span className={clsx("shrink-0 w-9 text-center rounded px-1 py-0.5 text-[10px] font-bold tracking-wide", c.chip)}>
                      {e.currency}
                    </span>
                    <span className="text-xs text-gray-200 leading-snug flex-1 line-clamp-1">{e.title}</span>
                    <span
                      className={clsx(
                        "text-[10px] tabular-nums shrink-0",
                        t.soon ? "text-[#00C896] font-semibold" : "text-gray-500"
                      )}
                    >
                      {t.label}
                    </span>
                  </div>
                  {(e.forecast || e.previous || e.actual) && (
                    <div className="flex items-center gap-3 mt-1 pl-11 text-[10px] tabular-nums">
                      {e.actual && (
                        <span className="text-gray-600">A <span className="text-white font-semibold">{e.actual}</span></span>
                      )}
                      {e.forecast && (
                        <span className="text-gray-600">F <span className="text-sky-300">{e.forecast}</span></span>
                      )}
                      {e.previous && (
                        <span className="text-gray-600">P <span className="text-gray-400">{e.previous}</span></span>
                      )}
                    </div>
                  )}
                </div>
              );
            })
          )}
        </div>
      </div>
    </div>
  );
}
