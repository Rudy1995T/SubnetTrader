"use client";

import { useEffect, useRef, useState } from "react";
import useSWR from "swr";

const API = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8080";
const fetcher = (url: string) => fetch(url).then((r) => r.json());

interface LogEntry {
  level?: string;
  message?: string;
  timestamp?: string;
  data?: unknown;
}

function levelColor(level: string | undefined): string {
  switch ((level || "").toUpperCase()) {
    case "DEBUG":    return "text-gray-600";
    case "INFO":     return "text-gray-300";
    case "WARNING":  return "text-amber-400";
    case "ERROR":    return "text-red-400";
    case "CRITICAL": return "text-red-400 font-bold";
    default:         return "text-gray-500";
  }
}

function levelBadge(level: string | undefined): string {
  switch ((level || "").toUpperCase()) {
    case "DEBUG":    return "bg-gray-800 text-gray-500";
    case "INFO":     return "bg-blue-900/40 text-blue-300";
    case "WARNING":  return "bg-amber-900/40 text-amber-300";
    case "ERROR":    return "bg-red-900/40 text-red-300";
    case "CRITICAL": return "bg-red-700 text-white";
    default:         return "bg-gray-800 text-gray-400";
  }
}

function formatTs(ts: string | undefined): string {
  if (!ts) return "";
  try {
    return new Date(ts).toLocaleTimeString("en-GB", {
      hour: "2-digit", minute: "2-digit", second: "2-digit",
    });
  } catch {
    return ts.slice(11, 19) || "";
  }
}

function LogRow({ entry }: { entry: LogEntry }) {
  const [open, setOpen] = useState(false);
  const hasData = entry.data != null && typeof entry.data === "object" &&
    Object.keys(entry.data as object).length > 0;

  return (
    <div className={`py-0.5 px-2 hover:bg-gray-900 rounded ${levelColor(entry.level)}`}>
      <div className="flex items-start gap-2 font-mono text-xs">
        <span className="text-gray-600 shrink-0 w-16">{formatTs(entry.timestamp)}</span>
        <span className={`shrink-0 px-1 rounded text-[10px] font-bold ${levelBadge(entry.level)}`}>
          {(entry.level || "RAW").slice(0, 4)}
        </span>
        <span className="break-all">{entry.message || JSON.stringify(entry)}</span>
        {hasData && (
          <button
            onClick={() => setOpen(!open)}
            className="shrink-0 text-gray-600 hover:text-gray-400 ml-auto"
          >
            {open ? "▲" : "▼"}
          </button>
        )}
      </div>
      {open && hasData && (
        <pre className="ml-20 mt-1 text-[10px] text-gray-500 bg-gray-950 rounded p-2 overflow-x-auto">
          {JSON.stringify(entry.data, null, 2)}
        </pre>
      )}
    </div>
  );
}

export default function Terminal() {
  const [autoScroll, setAutoScroll] = useState(true);
  const [filter, setFilter] = useState<string>("ALL");
  const bottomRef = useRef<HTMLDivElement>(null);

  const { data, error } = useSWR(`${API}/api/logs?lines=200`, fetcher, {
    refreshInterval: 3000,
  });

  const logs: LogEntry[] = data?.logs ?? [];
  const filtered = filter === "ALL"
    ? logs
    : logs.filter((e) => (e.level || "").toUpperCase() === filter);

  useEffect(() => {
    if (autoScroll && bottomRef.current) {
      bottomRef.current.scrollIntoView({ behavior: "smooth" });
    }
  }, [logs, autoScroll]);

  const LEVELS = ["ALL", "INFO", "WARNING", "ERROR", "CRITICAL", "DEBUG"];

  return (
    <div className="h-full">
      <div className="flex items-center justify-between mb-4">
        <h1 className="text-2xl font-bold font-mono">Terminal</h1>
        <div className="flex items-center gap-3">
          {/* Level filter */}
          <div className="flex gap-1">
            {LEVELS.map((l) => (
              <button
                key={l}
                onClick={() => setFilter(l)}
                className={`text-xs px-2 py-0.5 rounded font-mono transition-colors ${
                  filter === l
                    ? "bg-indigo-600 text-white"
                    : "bg-gray-800 text-gray-400 hover:bg-gray-700"
                }`}
              >
                {l}
              </button>
            ))}
          </div>
          {/* Auto-scroll toggle */}
          <button
            onClick={() => setAutoScroll(!autoScroll)}
            className={`text-xs px-3 py-1 rounded border transition-colors ${
              autoScroll
                ? "border-green-600 text-green-400"
                : "border-gray-600 text-gray-400"
            }`}
          >
            {autoScroll ? "↓ Auto-scroll ON" : "Auto-scroll OFF"}
          </button>
        </div>
      </div>

      {error && <p className="text-red-400 text-sm mb-2">Failed to load logs.</p>}

      <div className="bg-black border border-gray-800 rounded-lg overflow-y-auto"
           style={{ height: "calc(100vh - 160px)" }}>
        <div className="py-2 space-y-0.5">
          {filtered.length === 0 && (
            <p className="text-gray-600 text-xs font-mono text-center py-8">
              {data ? "No log entries match filter." : "Loading logs…"}
            </p>
          )}
          {filtered.map((entry, i) => (
            <LogRow key={i} entry={entry} />
          ))}
          <div ref={bottomRef} />
        </div>
      </div>

      <p className="text-xs text-gray-600 mt-2 font-mono">
        {filtered.length} entries · refreshes every 3s
      </p>
    </div>
  );
}
