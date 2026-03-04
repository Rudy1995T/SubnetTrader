"use client";

import { useEffect, useState } from "react";
import useSWR from "swr";

const API = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8080";
const fetcher = (url: string) => fetch(url).then((r) => r.json());

interface ControlStatus {
  kill_switch_active: boolean;
  fast_trading_enabled: boolean;
  scheduler_running: boolean;
  next_cycle: string | null;
  dry_run: boolean;
}

function useCountdown(nextCycle: string | null): string {
  const [display, setDisplay] = useState("");

  useEffect(() => {
    if (!nextCycle) { setDisplay("—"); return; }
    const update = () => {
      const diff = new Date(nextCycle).getTime() - Date.now();
      if (diff <= 0) { setDisplay("now"); return; }
      const m = Math.floor(diff / 60000);
      const s = Math.floor((diff % 60000) / 1000);
      setDisplay(`${m}m ${s.toString().padStart(2, "0")}s`);
    };
    update();
    const id = setInterval(update, 1000);
    return () => clearInterval(id);
  }, [nextCycle]);

  return display;
}

export default function Control() {
  const { data, error, mutate } = useSWR<ControlStatus>(
    `${API}/api/control/status`,
    fetcher,
    { refreshInterval: 10000 },
  );

  const [pauseConfirm, setPauseConfirm] = useState(false);
  const [resumeConfirm, setResumeConfirm] = useState(false);
  const [cycleSpinner, setCycleSpinner] = useState(false);
  const [fastSpinner, setFastSpinner] = useState(false);
  const [actionMsg, setActionMsg] = useState("");
  const [actionError, setActionError] = useState("");

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

  async function runCycleNow() {
    setCycleSpinner(true);
    await postAction("/api/control/run-cycle", "Main cycle triggered!");
    setTimeout(() => setCycleSpinner(false), 2000);
  }

  async function runFastCycleNow() {
    setFastSpinner(true);
    await postAction("/api/control/run-fast-cycle", "Fast cycle triggered!");
    setTimeout(() => setFastSpinner(false), 2000);
  }

  if (error) return <p className="text-red-400">Failed to load control status.</p>;

  const paused = data?.kill_switch_active ?? false;

  return (
    <div>
      <h1 className="text-2xl font-bold mb-6">Bot Control Panel</h1>

      {/* Status row */}
      <div className="flex flex-wrap gap-3 mb-8">
        {data?.dry_run && (
          <span className="px-3 py-1.5 rounded-full bg-yellow-900/30 border border-yellow-700 text-yellow-300 text-sm font-semibold">
            DRY RUN
          </span>
        )}
        <span className={`px-3 py-1.5 rounded-full border text-sm font-semibold ${
          paused
            ? "bg-red-900/30 border-red-700 text-red-300"
            : "bg-green-900/30 border-green-700 text-green-300"
        }`}>
          {paused ? "⏸ PAUSED" : "▶ ACTIVE"}
        </span>
        <span className={`px-3 py-1.5 rounded-full border text-sm ${
          data?.scheduler_running
            ? "bg-indigo-900/20 border-indigo-700 text-indigo-300"
            : "bg-gray-800 border-gray-700 text-gray-400"
        }`}>
          Scheduler: {data?.scheduler_running ? "running" : "stopped"}
        </span>
        {data?.fast_trading_enabled && (
          <span className="px-3 py-1.5 rounded-full bg-amber-900/20 border border-amber-700 text-amber-300 text-sm">
            ⚡ Fast trading ON
          </span>
        )}
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-6 mb-6">

        {/* Kill Switch */}
        <div className="bg-gray-900 border border-gray-800 rounded-xl p-6">
          <h2 className="text-lg font-semibold mb-1">Kill Switch</h2>
          <p className="text-xs text-gray-500 mb-5">
            Pausing halts all new trades (both main and fast). Existing positions keep running until the bot exits naturally.
          </p>

          {paused ? (
            <div>
              <div className="bg-red-900/20 border border-red-800 rounded-lg p-3 mb-4 text-center">
                <p className="text-red-300 font-semibold">Trading is PAUSED</p>
                <p className="text-red-500 text-xs mt-1">KILL_SWITCH file is active</p>
              </div>
              {!resumeConfirm ? (
                <button
                  onClick={() => setResumeConfirm(true)}
                  className="w-full py-2.5 text-sm font-semibold rounded bg-green-700 hover:bg-green-600 text-white transition-colors"
                >
                  Resume Trading
                </button>
              ) : (
                <div className="space-y-2">
                  <p className="text-xs text-center text-gray-300">Resume live trading?</p>
                  <div className="flex gap-2">
                    <button
                      onClick={() => { postAction("/api/control/resume", "Trading resumed."); setResumeConfirm(false); }}
                      className="flex-1 py-2 text-sm font-semibold rounded bg-green-700 hover:bg-green-600 text-white transition-colors"
                    >
                      Confirm
                    </button>
                    <button
                      onClick={() => setResumeConfirm(false)}
                      className="flex-1 py-2 text-sm rounded border border-gray-600 text-gray-400 hover:text-white transition-colors"
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
                  className="w-full py-2.5 text-sm font-semibold rounded bg-red-800 hover:bg-red-700 text-white transition-colors"
                >
                  Pause Trading
                </button>
              ) : (
                <div className="space-y-2">
                  <p className="text-xs text-center text-gray-300">Pause all trading cycles?</p>
                  <div className="flex gap-2">
                    <button
                      onClick={() => { postAction("/api/control/pause", "Trading paused."); setPauseConfirm(false); }}
                      className="flex-1 py-2 text-sm font-semibold rounded bg-red-800 hover:bg-red-700 text-white transition-colors"
                    >
                      Confirm
                    </button>
                    <button
                      onClick={() => setPauseConfirm(false)}
                      className="flex-1 py-2 text-sm rounded border border-gray-600 text-gray-400 hover:text-white transition-colors"
                    >
                      Cancel
                    </button>
                  </div>
                </div>
              )}
            </div>
          )}
        </div>

        {/* Manual Triggers */}
        <div className="bg-gray-900 border border-gray-800 rounded-xl p-6">
          <h2 className="text-lg font-semibold mb-1">Manual Triggers</h2>
          <p className="text-xs text-gray-500 mb-5">
            Run a cycle immediately without waiting for the scheduler.
          </p>
          <div className="space-y-3">
            <button
              onClick={runCycleNow}
              disabled={cycleSpinner}
              className="w-full py-2.5 text-sm font-semibold rounded bg-indigo-700 hover:bg-indigo-600 text-white disabled:opacity-50 transition-colors"
            >
              {cycleSpinner ? "Running…" : "Run Cycle Now"}
            </button>
            {data?.fast_trading_enabled && (
              <button
                onClick={runFastCycleNow}
                disabled={fastSpinner}
                className="w-full py-2.5 text-sm font-semibold rounded bg-amber-700 hover:bg-amber-600 text-white disabled:opacity-50 transition-colors"
              >
                {fastSpinner ? "Running…" : "Run Fast Cycle Now"}
              </button>
            )}
          </div>
        </div>

        {/* Next cycle */}
        <div className="bg-gray-900 border border-gray-800 rounded-xl p-6">
          <h2 className="text-lg font-semibold mb-1">Next Scheduled Cycle</h2>
          <p className="text-xs text-gray-500 mb-5">
            Countdown to the next automatic scan. Updates every 10s.
          </p>
          <div className="text-center">
            <p className="text-4xl font-mono font-bold text-indigo-300 mb-2">{countdown}</p>
            {data?.next_cycle && (
              <p className="text-xs text-gray-500">
                {new Date(data.next_cycle).toLocaleTimeString()}
              </p>
            )}
          </div>
        </div>
      </div>

      {/* Feedback */}
      {actionMsg && (
        <div className="bg-green-900/20 border border-green-700 rounded-lg p-3 text-green-300 text-sm text-center">
          {actionMsg}
        </div>
      )}
      {actionError && (
        <div className="bg-red-900/20 border border-red-700 rounded-lg p-3 text-red-300 text-sm text-center">
          {actionError}
        </div>
      )}
    </div>
  );
}
