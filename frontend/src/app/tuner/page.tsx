"use client";

import { useEffect, useState } from "react";

const API = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8080";

interface Weights {
  w_trend: number;
  w_support_resistance: number;
  w_fibonacci: number;
  w_volatility: number;
  w_mean_reversion: number;
  w_value_band: number;
  w_dereg: number;
}

interface PreviewRow {
  netuid: number;
  composite: number;
  old_rank: number;
  new_rank: number;
  rank_delta: number;
}

const WEIGHT_LABELS: { key: keyof Weights; label: string }[] = [
  { key: "w_trend",              label: "Trend Momentum"    },
  { key: "w_support_resistance", label: "Support/Resistance" },
  { key: "w_fibonacci",          label: "Fibonacci"          },
  { key: "w_volatility",         label: "Volatility"         },
  { key: "w_mean_reversion",     label: "Mean Reversion"     },
  { key: "w_value_band",         label: "Value Band"         },
  { key: "w_dereg",              label: "Dereg Proximity"    },
];

const DEFAULT_WEIGHTS: Weights = {
  w_trend: 0.20,
  w_support_resistance: 0.15,
  w_fibonacci: 0.10,
  w_volatility: 0.20,
  w_mean_reversion: 0.15,
  w_value_band: 0.10,
  w_dereg: 0.10,
};

export default function Tuner() {
  const [weights, setWeights] = useState<Weights>(DEFAULT_WEIGHTS);
  const [liveWeights, setLiveWeights] = useState<Weights | null>(null);
  const [preview, setPreview] = useState<PreviewRow[]>([]);
  const [previewTs, setPreviewTs] = useState<string | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [applying, setApplying] = useState(false);
  const [applyConfirm, setApplyConfirm] = useState(false);
  const [appliedMsg, setAppliedMsg] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    fetch(`${API}/api/settings/weights`)
      .then((r) => r.json())
      .then((d) => {
        setWeights(d);
        setLiveWeights(d);
      })
      .catch(() => {});
  }, []);

  const total = Object.values(weights).reduce((a, b) => a + b, 0);
  const sumOk = Math.abs(total - 1.0) < 0.005;

  async function runPreview() {
    setPreviewing(true);
    setError("");
    try {
      const params = new URLSearchParams();
      for (const [k, v] of Object.entries(weights)) {
        params.set(k, String(v));
      }
      const res = await fetch(`${API}/api/settings/weights/preview?${params}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const d = await res.json();
      setPreview(d.signals ?? []);
      setPreviewTs(d.scan_ts ?? null);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Preview failed");
    } finally {
      setPreviewing(false);
    }
  }

  async function applyWeights() {
    setApplying(true);
    setApplyConfirm(false);
    setError("");
    try {
      const res = await fetch(`${API}/api/settings/weights`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(weights),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setLiveWeights({ ...weights });
      setAppliedMsg("Weights applied — next cycle will use these values.");
      setTimeout(() => setAppliedMsg(""), 4000);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Apply failed");
    } finally {
      setApplying(false);
    }
  }

  function resetDefaults() {
    if (liveWeights) setWeights({ ...liveWeights });
  }

  return (
    <div>
      <h1 className="text-2xl font-bold mb-2">Signal Weight Tuner</h1>
      <p className="text-sm text-gray-400 mb-6">
        Adjust signal weights and preview how subnet rankings would change before applying to the live bot.
      </p>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-8">
        {/* Sliders */}
        <div className="bg-gray-900 border border-gray-800 rounded-xl p-6">
          <h2 className="text-sm font-semibold text-gray-300 mb-4">Weights</h2>

          <div className="space-y-4">
            {WEIGHT_LABELS.map(({ key, label }) => (
              <div key={key}>
                <div className="flex justify-between text-sm mb-1">
                  <span className="text-gray-300">{label}</span>
                  <span className="font-mono text-indigo-300">{weights[key].toFixed(2)}</span>
                </div>
                <input
                  type="range"
                  min={0}
                  max={0.5}
                  step={0.01}
                  value={weights[key]}
                  onChange={(e) =>
                    setWeights((w) => ({ ...w, [key]: parseFloat(e.target.value) }))
                  }
                  className="w-full accent-indigo-500"
                />
              </div>
            ))}
          </div>

          {/* Sum indicator */}
          <div className={`mt-4 text-sm font-mono ${sumOk ? "text-green-400" : "text-red-400"}`}>
            Sum: {total.toFixed(2)}
            {!sumOk && " ⚠ Should be 1.00"}
          </div>

          {/* Buttons */}
          <div className="flex gap-2 mt-5 flex-wrap">
            <button
              onClick={runPreview}
              disabled={previewing}
              className="flex-1 py-2 text-sm font-semibold rounded bg-indigo-600 hover:bg-indigo-500 text-white disabled:opacity-50 transition-colors"
            >
              {previewing ? "Loading…" : "Preview Rankings"}
            </button>
            <button
              onClick={resetDefaults}
              className="py-2 px-3 text-sm rounded border border-gray-600 text-gray-400 hover:text-white transition-colors"
            >
              Reset
            </button>
          </div>

          {/* Apply */}
          <div className="mt-3">
            {!applyConfirm ? (
              <button
                onClick={() => setApplyConfirm(true)}
                className="w-full py-2 text-sm font-semibold rounded border border-amber-600 text-amber-400 hover:bg-amber-900/20 transition-colors"
              >
                Apply to Bot
              </button>
            ) : (
              <div className="space-y-2">
                <p className="text-xs text-center text-gray-300">
                  This changes <b>live</b> trading weights immediately. Confirm?
                </p>
                <div className="flex gap-2">
                  <button
                    onClick={applyWeights}
                    disabled={applying}
                    className="flex-1 py-1.5 text-sm font-semibold rounded bg-amber-600 hover:bg-amber-500 text-white disabled:opacity-50 transition-colors"
                  >
                    {applying ? "Applying…" : "Confirm"}
                  </button>
                  <button
                    onClick={() => setApplyConfirm(false)}
                    className="flex-1 py-1.5 text-sm rounded border border-gray-600 text-gray-400 hover:text-white transition-colors"
                  >
                    Cancel
                  </button>
                </div>
              </div>
            )}
            {appliedMsg && <p className="text-green-400 text-xs text-center mt-2">{appliedMsg}</p>}
          </div>

          {error && <p className="text-red-400 text-xs mt-2 text-center">{error}</p>}
        </div>

        {/* Preview table */}
        <div>
          {liveWeights && (
            <div className="bg-gray-900 border border-gray-800 rounded-xl p-4 mb-4">
              <h3 className="text-xs font-semibold text-gray-400 mb-2">Live weights (bot is using)</h3>
              <div className="grid grid-cols-2 gap-x-4 gap-y-1">
                {WEIGHT_LABELS.map(({ key, label }) => (
                  <div key={key} className="flex justify-between text-xs">
                    <span className="text-gray-500">{label}</span>
                    <span className={`font-mono ${
                      Math.abs(liveWeights[key] - weights[key]) > 0.005 ? "text-amber-400" : "text-gray-300"
                    }`}>{liveWeights[key].toFixed(2)}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {preview.length > 0 && (
            <div className="bg-gray-900 border border-gray-800 rounded-xl p-4">
              <h3 className="text-sm font-semibold text-gray-300 mb-1">
                Ranking Preview
              </h3>
              {previewTs && (
                <p className="text-xs text-gray-500 mb-3">
                  Based on scan: {new Date(previewTs).toLocaleString()}
                </p>
              )}
              <div className="overflow-y-auto max-h-80">
                <table className="text-xs border-collapse w-full table-fixed">
                  <colgroup>
                    <col className="w-12" />
                    <col className="w-16" />
                    <col className="w-16" />
                    <col className="w-16" />
                  </colgroup>
                  <thead>
                    <tr className="border-b border-gray-700 text-gray-400">
                      <th className="py-1.5 text-left">Rank</th>
                      <th className="py-1.5 text-left">netuid</th>
                      <th className="py-1.5 text-right">Score</th>
                      <th className="py-1.5 text-right">Δ</th>
                    </tr>
                  </thead>
                  <tbody>
                    {preview.slice(0, 30).map((row) => (
                      <tr key={row.netuid} className="border-b border-gray-800 hover:bg-gray-800">
                        <td className="py-1 text-gray-400">#{row.new_rank}</td>
                        <td className="py-1 font-mono text-indigo-300">{row.netuid}</td>
                        <td className="py-1 text-right font-mono">{(row.composite * 100).toFixed(1)}%</td>
                        <td className={`py-1 text-right font-mono ${
                          row.rank_delta > 0 ? "text-green-400" : row.rank_delta < 0 ? "text-red-400" : "text-gray-500"
                        }`}>
                          {row.rank_delta > 0 ? `+${row.rank_delta}` : row.rank_delta === 0 ? "—" : row.rank_delta}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
          {preview.length === 0 && !previewing && (
            <div className="bg-gray-900 border border-gray-800 rounded-xl p-6 text-center text-gray-500 text-sm">
              Adjust weights and click "Preview Rankings" to see how rankings would change.
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
