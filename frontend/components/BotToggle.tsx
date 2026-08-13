"use client";
import { useEffect, useState } from "react";
import { Play, Square, Loader2 } from "lucide-react";
import clsx from "clsx";
import { startBot, stopBot, getBotStatus } from "../lib/api";

/**
 * Single Start/Stop toggle for the header (sits next to the coin switch).
 * Green "Start" when stopped, red "Stop" when running. Stop is locked while a
 * position is open (the bot must close it first) — same guard as before.
 */
export default function BotToggle({ openPosition = 0 }: { openPosition?: number }) {
  const [status, setStatus] = useState<any>(null);
  const [loading, setLoading] = useState(false);
  const hasOpenTrade = openPosition !== 0;
  const isRunning = status?.running;
  const stopLocked = isRunning && hasOpenTrade;

  const fetchStatus = async () => {
    try {
      const res = await getBotStatus();
      setStatus(res.data);
    } catch {
      /* backend momentarily unreachable — keep last status */
    }
  };

  useEffect(() => {
    fetchStatus();
    const iv = setInterval(fetchStatus, 10_000);
    return () => clearInterval(iv);
  }, []);

  const toggle = async () => {
    if (loading || stopLocked) return;
    setLoading(true);
    try {
      if (isRunning) await stopBot();
      else await startBot();
      await fetchStatus();
    } catch {
      /* UI updates on next poll */
    } finally {
      setLoading(false);
    }
  };

  const nextRun = status?.next_run
    ? new Date(status.next_run).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })
    : null;
  const title = stopLocked
    ? "Can't stop while a position is open — wait for the bot to close it"
    : `${isRunning ? "Stop the bot" : "Start the bot"}${nextRun && isRunning ? ` · next run ${nextRun}` : ""}`;

  return (
    <div className="flex items-center gap-2">
      <span className="flex items-center gap-1.5" title={status ? `${(status.symbols ?? []).join(", ")} · every ${status.interval_minutes}m` : ""}>
        <span className={clsx("w-2 h-2 rounded-full", isRunning ? "bg-green-400 animate-pulse" : "bg-gray-600")} />
        <span className={clsx("text-[11px] font-medium hidden sm:inline", isRunning ? "text-green-400" : "text-gray-500")}>
          {isRunning ? "RUNNING" : "STOPPED"}
        </span>
      </span>
      <button
        onClick={toggle}
        disabled={loading || stopLocked}
        title={title}
        className={clsx(
          "flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold transition disabled:opacity-40 disabled:cursor-not-allowed",
          isRunning ? "bg-red-600/90 hover:bg-red-500 text-white" : "bg-[#00C896] hover:bg-[#00b085] text-black"
        )}
      >
        {loading ? <Loader2 size={13} className="animate-spin" /> : isRunning ? <Square size={12} /> : <Play size={13} />}
        {isRunning ? "Stop" : "Start"}
      </button>
    </div>
  );
}
