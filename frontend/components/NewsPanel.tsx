"use client";
import { useEffect, useState } from "react";
import { Newspaper, CalendarClock, ExternalLink } from "lucide-react";
import clsx from "clsx";
import { getNewsAll } from "../lib/api";

interface Story {
  source: string;
  title: string;
  link: string;
  summary: string;
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

function impactColor(rank: number) {
  if (rank >= 3) return { dot: "bg-red-500", text: "text-red-400" };      // High
  if (rank === 2) return { dot: "bg-amber-500", text: "text-amber-400" }; // Medium
  if (rank === 1) return { dot: "bg-gray-500", text: "text-gray-400" };   // Low
  return { dot: "bg-blue-500/60", text: "text-blue-300" };                // Holiday/other
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
  const [updated, setUpdated] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const fetchData = async () => {
    try {
      const res = await getNewsAll();
      setStories(res.data.stories ?? []);
      setEvents(res.data.events ?? []);
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
  const calRows = (upcoming.length ? upcoming : events).slice(0, 25);

  return (
    <div className="bg-[#161b22] border border-[#30363d] rounded-xl p-3 flex flex-col gap-3 lg:self-start">
      <div className="flex items-center justify-between px-1">
        <h2 className="text-sm font-semibold text-gray-300 uppercase tracking-wider">Market News</h2>
        <span className="flex items-center gap-1.5 text-[11px] text-gray-500">
          <span className="w-1.5 h-1.5 rounded-full bg-green-400 animate-pulse" />
          {updated ? `updated ${timeAgo(updated)}` : "live"}
        </span>
      </div>

      {/* Inner container 1 — Latest news headlines */}
      <div className="bg-[#0d1117] border border-[#30363d] rounded-lg overflow-hidden">
        <div className="flex items-center gap-1.5 px-3 py-2 border-b border-[#30363d] text-[11px] font-semibold text-gray-400 uppercase tracking-wider">
          <Newspaper size={12} /> Latest News
        </div>
        <div className="max-h-[300px] overflow-y-auto divide-y divide-[#30363d]/60">
          {loading && stories.length === 0 ? (
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
                  <span className="text-[10px] text-gray-500 uppercase tracking-wide truncate">{s.source}</span>
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
      <div className="bg-[#0d1117] border border-[#30363d] rounded-lg overflow-hidden">
        <div className="flex items-center gap-1.5 px-3 py-2 border-b border-[#30363d] text-[11px] font-semibold text-gray-400 uppercase tracking-wider">
          <CalendarClock size={12} /> Economic Calendar
        </div>
        <div className="max-h-[240px] overflow-y-auto divide-y divide-[#30363d]/60">
          {calRows.length === 0 ? (
            <p className="px-3 py-4 text-xs text-gray-600">No events available.</p>
          ) : (
            calRows.map((e, i) => {
              const c = impactColor(e.impact_rank);
              const t = eventTime(e.date);
              return (
                <div key={i} className={clsx("px-3 py-2", t.past && "opacity-45")}>
                  <div className="flex items-center gap-2">
                    <span className={clsx("w-1.5 h-1.5 rounded-full shrink-0", c.dot)} title={e.impact} />
                    <span className="text-[10px] font-bold text-gray-400 w-8 shrink-0">{e.currency}</span>
                    <span className="text-xs text-gray-300 leading-snug flex-1 line-clamp-1">{e.title}</span>
                    <span className={clsx("text-[10px] tabular-nums shrink-0", t.soon ? "text-[#00C896] font-semibold" : "text-gray-500")}>
                      {t.label}
                    </span>
                  </div>
                  {(e.forecast || e.previous || e.actual) && (
                    <div className="flex items-center gap-3 mt-1 pl-4 text-[10px] tabular-nums">
                      {e.actual && <span className="text-gray-300">A <span className="font-semibold">{e.actual}</span></span>}
                      {e.forecast && <span className="text-gray-500">F <span className="text-gray-300">{e.forecast}</span></span>}
                      {e.previous && <span className="text-gray-600">P <span className="text-gray-400">{e.previous}</span></span>}
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
