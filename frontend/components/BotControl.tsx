"use client";
import { useState, useEffect } from "react";
import { Play, Square, RefreshCw } from "lucide-react";
import { startBot, stopBot, getBotStatus } from "../lib/api";

export default function BotControl({ openPosition = 0 }: { openPosition?: number }) {
  const [status, setStatus] = useState<any>(null);
  const [loading, setLoading] = useState(false);
  const hasOpenTrade = openPosition !== 0;

  const fetchStatus = async () => {
    try {
      const res = await getBotStatus();
      setStatus(res.data);
    } catch {
      /* backend momentarily unreachable — keep last status, retry next poll */
    }
  };

  useEffect(() => {
    fetchStatus();
    const interval = setInterval(fetchStatus, 10_000);
    return () => clearInterval(interval);
  }, []);

  const handleStart = async () => {
    setLoading(true);
    try {
      await startBot();
      await fetchStatus();
    } catch {
      /* ignore — UI updates on next poll */
    } finally {
      setLoading(false);
    }
  };

  const handleStop = async () => {
    setLoading(true);
    try {
      await stopBot();
      await fetchStatus();
    } catch {
      /* ignore */
    } finally {
      setLoading(false);
    }
  };

  const isRunning = status?.running;

  return (
    <div className="bg-[#161b22] border border-[#30363d] rounded-xl p-5">
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-sm font-semibold text-gray-300 uppercase tracking-wider">Bot Control</h2>
        <button onClick={fetchStatus} className="text-gray-500 hover:text-gray-300 transition">
          <RefreshCw size={14} />
        </button>
      </div>

      <div className="flex items-center gap-3 mb-4">
        <span
          className={`w-2.5 h-2.5 rounded-full ${isRunning ? "bg-green-400 animate-pulse" : "bg-gray-600"}`}
        />
        <span className={`text-sm font-medium ${isRunning ? "text-green-400" : "text-gray-500"}`}>
          {isRunning ? "RUNNING" : "STOPPED"}
        </span>
      </div>

      {status && (
        <div className="text-xs text-gray-500 space-y-1 mb-4">
          <p>Trading: <span className="text-gray-300">{(status.symbols ?? [status.symbol]).join(", ")}</span></p>
          <p>Interval: <span className="text-gray-300">{status.interval_minutes}m</span></p>
          {status.next_run && (
            <p>Next run: <span className="text-gray-300">{new Date(status.next_run).toLocaleTimeString()}</span></p>
          )}
        </div>
      )}

      <div className="flex gap-2">
        <button
          onClick={handleStart}
          disabled={isRunning || loading}
          className="flex items-center gap-1.5 px-3 py-2 bg-green-600 hover:bg-green-500 disabled:opacity-40 disabled:cursor-not-allowed rounded-lg text-sm font-medium transition"
        >
          <Play size={13} /> Start
        </button>
        <button
          onClick={handleStop}
          disabled={!isRunning || loading || hasOpenTrade}
          title={hasOpenTrade ? "Can't stop while a position is open — wait for the bot to close it" : "Stop the bot"}
          className="flex items-center gap-1.5 px-3 py-2 bg-red-700 hover:bg-red-600 disabled:opacity-40 disabled:cursor-not-allowed rounded-lg text-sm font-medium transition"
        >
          <Square size={13} /> Stop
        </button>
      </div>
      {hasOpenTrade && isRunning && (
        <p className="text-[11px] text-yellow-400/80 mt-2">
          Open position — Stop is locked until the trade closes.
        </p>
      )}
    </div>
  );
}

