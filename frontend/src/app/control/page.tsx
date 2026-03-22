"use client";

import { useEffect, useRef, useState } from "react";
import useSWR from "swr";

const API =
  process.env.NEXT_PUBLIC_API_URL ||
  (typeof window !== "undefined"
    ? `http://${window.location.hostname}:8081`
    : "http://localhost:8081");
const fetcher = (url: string) => fetch(url).then((r) => r.json());

interface ControlStatus {
  kill_switch_active: boolean;
  scheduler_running: boolean;
  next_cycle: string | null;
  ema_enabled: boolean;
  ema_dry_run: boolean;
  exit_watcher_enabled: boolean;
  breaker_active: boolean;
}

interface ServiceHealth {
  ok: boolean;
  name: string;
  detail: string;
  last_check: string;
  latency_ms?: number;
  can_trade?: boolean;
  balance_tao?: number;
  pot_tao?: number;
  optional?: boolean;
}

interface HealthResponse {
  timestamp: string;
  services: Record<string, ServiceHealth>;
}

function useCountdown(nextCycle: string | null): string {
  const [display, setDisplay] = useState("");

  useEffect(() => {
    if (!nextCycle) {
      setDisplay("—");
      return;
    }

    const update = () => {
      const diff = new Date(nextCycle).getTime() - Date.now();
      if (diff <= 0) {
        setDisplay("now");
        return;
      }
      const minutes = Math.floor(diff / 60000);
      const seconds = Math.floor((diff % 60000) / 1000);
      setDisplay(`${minutes}m ${seconds.toString().padStart(2, "0")}s`);
    };

    update();
    const id = setInterval(update, 1000);
    return () => clearInterval(id);
  }, [nextCycle]);

  return display;
}

// ── Error help messages ──────────────────────────────────────────────────────

const ERROR_HELP: Record<string, Record<string, string>> = {
  rpc: {
    Unreachable:
      "Check your internet connection and FLAMEWIRE_API_KEY. The bot will fall back to the public subtensor endpoint.",
    default:
      "FlameWire is slow to respond. Trading will continue but may be delayed.",
  },
  taostats: {
    "HTTP 401": "Your Taostats API key is invalid or expired. Update it in Settings.",
    default: "Taostats API is down. The bot cannot fetch subnet data until it recovers.",
  },
  telegram: {
    "invalid token": "Your Telegram bot token is invalid. Create a new bot via @BotFather.",
    "Not configured":
      "Telegram alerts are optional. Set up in Settings to receive trade notifications.",
    default: "Could not reach Telegram. Check your bot token.",
  },
  database: {
    "Not initialized":
      "The database has not been created yet. This usually resolves on first bot startup.",
    default: "Database error detected.",
  },
  wallet: {
    "Executor not initialized": "The bot is still starting up. Wait a moment and refresh.",
    "Balance check failed":
      "Could not query wallet balance. The chain may be temporarily unreachable.",
    default: "Wallet issue detected.",
  },
};

function getErrorHelp(serviceKey: string, detail: string): string {
  const map = ERROR_HELP[serviceKey] || {};
  for (const [pattern, msg] of Object.entries(map)) {
    if (pattern !== "default" && detail.includes(pattern)) return msg;
  }
  return map.default || "";
}

// ── HealthCard component ─────────────────────────────────────────────────────

function HealthCard({
  health,
  onRefresh,
  isDryRun,
}: {
  health: HealthResponse | undefined;
  onRefresh: () => void;
  isDryRun: boolean;
}) {
  const [refreshing, setRefreshing] = useState(false);
  const [expandedRow, setExpandedRow] = useState<string | null>(null);
  const [collapsed, setCollapsed] = useState(false);

  const handleRefresh = async () => {
    setRefreshing(true);
    onRefresh();
    setTimeout(() => setRefreshing(false), 1000);
  };

  const services = health?.services;
  const serviceKeys = ["rpc", "taostats", "telegram", "database", "wallet"];
  const healthyCount = services
    ? serviceKeys.filter((k) => services[k]?.ok).length
    : 0;
  const totalCount = services ? serviceKeys.filter((k) => services[k]).length : 0;

  const lastChecked = health?.timestamp
    ? new Date(health.timestamp).toLocaleTimeString()
    : "—";

  function latencyColor(ms: number) {
    if (ms < 500) return "text-emerald-400";
    if (ms <= 2000) return "text-amber-400";
    return "text-red-400";
  }

  function dotColor(key: string, svc: ServiceHealth) {
    if (svc.ok) return "bg-emerald-400";
    if (key === "telegram" && svc.detail.includes("Not configured")) return "bg-amber-400";
    return "bg-red-400";
  }

  function walletExtra(svc: ServiceHealth) {
    if (!svc.ok) return null;
    if (isDryRun) return <span className="text-xs text-gray-500">(dry run — no trades)</span>;
    if (svc.can_trade) return <span className="text-xs text-emerald-400">Can trade</span>;
    if (svc.pot_tao)
      return (
        <span className="text-xs text-amber-400">
          Pot requires {svc.pot_tao} TAO
        </span>
      );
    return null;
  }

  return (
    <div className="rounded-xl border border-gray-800 bg-gray-900 p-6 mb-6">
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-lg font-semibold">System Health</h2>
        {/* Mobile collapse toggle */}
        <button
          type="button"
          onClick={() => setCollapsed(!collapsed)}
          className="md:hidden text-xs text-gray-400 border border-gray-700 rounded px-2 py-1"
        >
          {collapsed
            ? `${healthyCount}/${totalCount} healthy ▼`
            : "▲"}
        </button>
      </div>

      {/* Collapsed mobile summary */}
      {collapsed && (
        <div className="md:hidden text-sm text-gray-400">
          System Health: {healthyCount}/{totalCount} services healthy
        </div>
      )}

      {/* Full view — always on desktop, toggleable on mobile */}
      <div className={collapsed ? "hidden md:block" : ""}>
        {!services ? (
          <div className="flex items-center gap-3 py-4 justify-center text-gray-400">
            <span className="h-5 w-5 border-2 border-indigo-500/30 border-t-indigo-500 rounded-full animate-spin" />
            <span className="text-sm">Loading health status...</span>
          </div>
        ) : (
          <div>
            {serviceKeys.map((key) => {
              const svc = services[key];
              if (!svc) return null;
              const isExpanded = expandedRow === key;
              const truncDetail =
                svc.detail.length > 40 ? svc.detail.slice(0, 40) + "..." : svc.detail;
              const helpMsg = !svc.ok ? getErrorHelp(key, svc.detail) : "";

              return (
                <div key={key}>
                  <div
                    className={`flex items-center justify-between py-2 border-b border-gray-800 last:border-0 ${
                      !svc.ok ? "cursor-pointer" : ""
                    }`}
                    onClick={() => !svc.ok && setExpandedRow(isExpanded ? null : key)}
                  >
                    <div className="flex items-center gap-3 min-w-0">
                      <span
                        className={`inline-block w-2.5 h-2.5 rounded-full shrink-0 ${dotColor(key, svc)}`}
                      />
                      <span className="text-sm font-medium text-gray-200 w-36 shrink-0">
                        {svc.name}
                      </span>
                      <span className="text-sm text-gray-400 truncate">
                        {truncDetail}
                      </span>
                      {key === "wallet" && walletExtra(svc)}
                    </div>
                    <div className="flex items-center gap-2 shrink-0 ml-2">
                      {svc.latency_ms !== undefined && (
                        <span
                          className={`text-xs font-mono px-2 py-0.5 rounded ${latencyColor(svc.latency_ms)}`}
                        >
                          {svc.latency_ms}ms
                        </span>
                      )}
                      {!svc.ok && (
                        <span className="text-red-400 text-xs">{isExpanded ? "▲" : "▼"}</span>
                      )}
                    </div>
                  </div>
                  {isExpanded && !svc.ok && (
                    <div className="text-xs text-gray-500 mt-1 ml-8 pb-2">
                      <p>{svc.detail}</p>
                      {helpMsg && <p className="mt-1">{helpMsg}</p>}
                      <a
                        href="/settings"
                        className="text-indigo-400 hover:text-indigo-300 underline text-xs"
                      >
                        Open Settings
                      </a>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}

        <div className="flex items-center justify-between mt-4 pt-2 border-t border-gray-800">
          <span className="text-xs text-gray-500">Last checked: {lastChecked}</span>
          <button
            type="button"
            onClick={handleRefresh}
            disabled={refreshing}
            className="text-xs text-gray-400 hover:text-gray-200 border border-gray-700 rounded px-3 py-1 disabled:opacity-50 transition-colors"
          >
            {refreshing ? (
              <span className="flex items-center gap-1">
                <span className="h-3 w-3 border border-gray-500 border-t-gray-300 rounded-full animate-spin" />
                Refreshing
              </span>
            ) : (
              "Refresh"
            )}
          </button>
        </div>
      </div>
    </div>
  );
}

// ── Main Control page ────────────────────────────────────────────────────────

export default function Control() {
  const { data, error, mutate } = useSWR<ControlStatus>(
    `${API}/api/control/status`,
    fetcher,
    { refreshInterval: 10000 },
  );

  const { data: health, mutate: refreshHealth } = useSWR<HealthResponse>(
    `${API}/api/health/services`,
    fetcher,
    { refreshInterval: 30000 },
  );

  const [pauseConfirm, setPauseConfirm] = useState(false);
  const [resumeConfirm, setResumeConfirm] = useState(false);
  const [cycleSpinner, setCycleSpinner] = useState(false);
  const [resetConfirm, setResetConfirm] = useState(false);
  const [resetSpinner, setResetSpinner] = useState(false);
  const [actionMsg, setActionMsg] = useState("");
  const [actionError, setActionError] = useState("");

  const healthRef = useRef<HTMLDivElement>(null);

  const countdown = useCountdown(data?.next_cycle ?? null);

  async function postAction(path: string, label: string) {
    setActionError("");
    setActionMsg("");
    try {
      const res = await fetch(`${API}${path}`, { method: "POST" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setActionMsg(label);
      setTimeout(() => setActionMsg(""), 4000);
      mutate();
    } catch (e: unknown) {
      setActionError(e instanceof Error ? e.message : "Action failed");
    }
  }

  async function runEmaCycleNow() {
    setCycleSpinner(true);
    await postAction("/api/control/run-ema-cycle", "EMA cycle triggered.");
    setTimeout(() => setCycleSpinner(false), 2000);
  }

  async function resetDryRun() {
    setResetSpinner(true);
    setResetConfirm(false);
    setActionError("");
    setActionMsg("");
    try {
      const res = await fetch(`${API}/api/control/reset-dry-run`, { method: "POST" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setActionMsg("EMA dry-run history cleared.");
      setTimeout(() => setActionMsg(""), 6000);
      mutate();
    } catch (e: unknown) {
      setActionError(e instanceof Error ? e.message : "Reset failed");
    } finally {
      setResetSpinner(false);
    }
  }

  function downloadCSV() {
    window.open(`${API}/api/export/trades.csv`, "_blank");
  }

  if (error) return <p className="text-red-400">Failed to load control status.</p>;

  const paused = data?.kill_switch_active ?? false;
  const emaIsLive = data ? !data.ema_dry_run : false;

  return (
    <div>
      <h1 className="mb-6 text-2xl font-bold">EMA Control</h1>

      <div className="mb-8 flex flex-wrap gap-3">
        <span
          className={`rounded-full border px-3 py-1.5 text-sm font-semibold ${
            emaIsLive
              ? "border-emerald-600 bg-emerald-900/30 text-emerald-300"
              : "border-yellow-700 bg-yellow-900/30 text-yellow-300"
          }`}
        >
          {emaIsLive ? "EMA LIVE" : "EMA DRY RUN"}
        </span>
        <span
          className={`rounded-full border px-3 py-1.5 text-sm font-semibold ${
            paused
              ? "border-red-700 bg-red-900/30 text-red-300"
              : "border-green-700 bg-green-900/30 text-green-300"
          }`}
        >
          {paused ? "PAUSED" : "ACTIVE"}
        </span>
        <span
          className={`rounded-full border px-3 py-1.5 text-sm ${
            data?.scheduler_running
              ? "border-indigo-700 bg-indigo-900/20 text-indigo-300"
              : "border-gray-700 bg-gray-800 text-gray-400"
          }`}
        >
          Scheduler: {data?.scheduler_running ? "running" : "stopped"}
        </span>
        {data?.exit_watcher_enabled && (
          <span className="rounded-full border border-sky-700 bg-sky-900/20 px-3 py-1.5 text-sm text-sky-300">
            Exit watcher ON
          </span>
        )}
        {data?.breaker_active && (
          <span className="rounded-full border border-red-800 bg-red-950/40 px-3 py-1.5 text-sm text-red-300">
            Breaker active
          </span>
        )}
        {/* Health summary badge */}
        {health && (() => {
          const svcKeys = ["rpc", "taostats", "telegram", "database", "wallet"];
          const total = svcKeys.filter((k) => health.services[k]).length;
          const ok = svcKeys.filter((k) => health.services[k]?.ok).length;
          const majorityBad = ok < total / 2;
          const allGood = ok === total;
          const badgeClass = allGood
            ? "border-emerald-700 bg-emerald-900/20 text-emerald-300"
            : majorityBad
            ? "border-red-700 bg-red-900/20 text-red-300"
            : "border-amber-700 bg-amber-900/20 text-amber-300";
          return (
            <button
              type="button"
              onClick={() => healthRef.current?.scrollIntoView({ behavior: "smooth" })}
              className={`rounded-full border px-3 py-1.5 text-sm ${badgeClass}`}
            >
              Health: {ok}/{total} {allGood ? "\u2713" : "\u26A0"}
            </button>
          );
        })()}
      </div>

      <div ref={healthRef}>
        <HealthCard
          health={health}
          onRefresh={() => refreshHealth()}
          isDryRun={data?.ema_dry_run ?? true}
        />
      </div>

      <div className="mb-6 grid grid-cols-1 gap-6 md:grid-cols-3">
        <div className="rounded-xl border border-gray-800 bg-gray-900 p-6">
          <h2 className="mb-1 text-lg font-semibold">Kill Switch</h2>
          <p className="mb-5 text-xs text-gray-500">
            Pausing stops new EMA actions until the switch is cleared.
          </p>

          {paused ? (
            <div>
              <div className="mb-4 rounded-lg border border-red-800 bg-red-900/20 p-3 text-center">
                <p className="font-semibold text-red-300">EMA trading is paused</p>
                <p className="mt-1 text-xs text-red-500">KILL_SWITCH file is active</p>
              </div>
              {!resumeConfirm ? (
                <button
                  onClick={() => setResumeConfirm(true)}
                  className="w-full rounded bg-green-700 py-2.5 text-sm font-semibold text-white transition-colors hover:bg-green-600"
                >
                  Resume Trading
                </button>
              ) : (
                <div className="space-y-2">
                  <p className="text-center text-xs text-gray-300">Resume EMA trading?</p>
                  <div className="flex gap-2">
                    <button
                      onClick={() => {
                        postAction("/api/control/resume", "Trading resumed.");
                        setResumeConfirm(false);
                      }}
                      className="flex-1 rounded bg-green-700 py-2 text-sm font-semibold text-white transition-colors hover:bg-green-600"
                    >
                      Confirm
                    </button>
                    <button
                      onClick={() => setResumeConfirm(false)}
                      className="flex-1 rounded border border-gray-600 py-2 text-sm text-gray-400 transition-colors hover:text-white"
                    >
                      Cancel
                    </button>
                  </div>
                </div>
              )}
            </div>
          ) : (
            <div>
              {!pauseConfirm ? (
                <button
                  onClick={() => setPauseConfirm(true)}
                  className="w-full rounded bg-red-800 py-2.5 text-sm font-semibold text-white transition-colors hover:bg-red-700"
                >
                  Pause Trading
                </button>
              ) : (
                <div className="space-y-2">
                  <p className="text-center text-xs text-gray-300">Pause EMA trading?</p>
                  <div className="flex gap-2">
                    <button
                      onClick={() => {
                        postAction("/api/control/pause", "Trading paused.");
                        setPauseConfirm(false);
                      }}
                      className="flex-1 rounded bg-red-800 py-2 text-sm font-semibold text-white transition-colors hover:bg-red-700"
                    >
                      Confirm
                    </button>
                    <button
                      onClick={() => setPauseConfirm(false)}
                      className="flex-1 rounded border border-gray-600 py-2 text-sm text-gray-400 transition-colors hover:text-white"
                    >
                      Cancel
                    </button>
                  </div>
                </div>
              )}
            </div>
          )}
        </div>

        <div className="rounded-xl border border-gray-800 bg-gray-900 p-6">
          <h2 className="mb-1 text-lg font-semibold">Manual Trigger</h2>
          <p className="mb-5 text-xs text-gray-500">
            Run the EMA cycle immediately without waiting for the scheduler.
          </p>
          <button
            onClick={runEmaCycleNow}
            disabled={cycleSpinner}
            className={`w-full rounded py-2.5 text-sm font-semibold text-white transition-colors disabled:opacity-50 ${
              emaIsLive ? "bg-emerald-700 hover:bg-emerald-600" : "bg-indigo-700 hover:bg-indigo-600"
            }`}
          >
            {cycleSpinner ? "Running..." : `Run EMA Cycle${emaIsLive ? " (LIVE)" : ""}`}
          </button>
        </div>

        <div className="rounded-xl border border-gray-800 bg-gray-900 p-6">
          <h2 className="mb-1 text-lg font-semibold">Next Scheduled Cycle</h2>
          <p className="mb-5 text-xs text-gray-500">
            Countdown to the next automatic EMA scan.
          </p>
          <div className="text-center">
            <p className="mb-2 text-4xl font-mono font-bold text-indigo-300">{countdown}</p>
            {data?.next_cycle && (
              <p className="text-xs text-gray-500">
                {new Date(data.next_cycle).toLocaleTimeString()}
              </p>
            )}
          </div>
        </div>
      </div>

      <div className="mb-6 grid grid-cols-1 gap-6 md:grid-cols-2">
        <div className="rounded-xl border border-gray-800 bg-gray-900 p-6">
          <div className="flex items-start justify-between gap-4">
            <div>
              <h2 className="mb-1 text-lg font-semibold">Export EMA Trades</h2>
              <p className="text-xs text-gray-500">
                Download EMA trade history as CSV for review or reporting.
              </p>
            </div>
            <button
              onClick={downloadCSV}
              className="whitespace-nowrap rounded bg-indigo-700 px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-indigo-600"
            >
              Download CSV
            </button>
          </div>
        </div>

        {data?.ema_dry_run && (
          <div className="rounded-xl border border-gray-800 bg-gray-900 p-6">
            <div className="flex items-start justify-between gap-4">
              <div>
                <h2 className="mb-1 text-lg font-semibold">Reset Dry-Run Data</h2>
                <p className="text-xs text-gray-500">
                  Wipes EMA paper trades and starts the strategy from a clean state.
                </p>
              </div>
              <div className="shrink-0">
                {!resetConfirm ? (
                  <button
                    onClick={() => setResetConfirm(true)}
                    className="whitespace-nowrap rounded border border-red-700 px-4 py-2 text-sm font-semibold text-red-400 transition-colors hover:bg-red-900/20"
                  >
                    Reset Data
                  </button>
                ) : (
                  <div className="space-y-2 text-right">
                    <p className="text-xs text-gray-300">Delete all EMA paper history?</p>
                    <div className="flex justify-end gap-2">
                      <button
                        onClick={resetDryRun}
                        disabled={resetSpinner}
                        className="rounded bg-red-700 px-4 py-1.5 text-sm font-semibold text-white transition-colors hover:bg-red-600 disabled:opacity-50"
                      >
                        {resetSpinner ? "Resetting..." : "Yes, reset"}
                      </button>
                      <button
                        onClick={() => setResetConfirm(false)}
                        className="rounded border border-gray-600 px-4 py-1.5 text-sm text-gray-400 transition-colors hover:text-white"
                      >
                        Cancel
                      </button>
                    </div>
                  </div>
                )}
              </div>
            </div>
          </div>
        )}
      </div>

      {actionMsg && (
        <div className="rounded-lg border border-green-700 bg-green-900/20 p-3 text-center text-sm text-green-300">
          {actionMsg}
        </div>
      )}
      {actionError && (
        <div className="rounded-lg border border-red-700 bg-red-900/20 p-3 text-center text-sm text-red-300">
          {actionError}
        </div>
      )}
    </div>
  );
}
