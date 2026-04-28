"use client";

import { useState } from "react";
import useSWR from "swr";

const API =
  process.env.NEXT_PUBLIC_API_URL ||
  (typeof window !== "undefined"
    ? `http://${window.location.hostname}:8081`
    : "http://localhost:8081");

const fetcher = (url: string) => fetch(url).then((r) => r.json());

interface OpenPosition {
  position_id: number;
  netuid: number;
  name: string;
  entry_price: number;
  current_price: number;
  pnl_pct: number;
  amount_tao: number;
  amount_alpha?: number;
  peak_price: number;
  entry_ts: string;
  hours_held: number;
}

interface SnapshotStatus {
  last_run: string | null;
  last_error: string | null;
  last_row_count: number;
}

interface ExitWatcherStatus {
  enabled: boolean;
  interval_sec: number;
  last_run: string | null;
  last_error: string | null;
  last_exit_count: number;
}

interface PortfolioData {
  enabled: boolean;
  tag?: string;
  pot_tao: number;
  deployed_tao: number;
  unstaked_tao: number;
  open_count: number;
  max_positions: number;
  open_positions: OpenPosition[];
  dry_run: boolean;
  stop_loss_pct?: number;
  take_profit_pct?: number;
  trailing_stop_pct?: number;
  z_entry: number;
  z_exit: number;
  min_tao_pct: number;
  magnitude_cap: number;
  regime_threshold: number;
  breaker_active?: boolean;
  snapshot_status?: SnapshotStatus;
  exit_watcher?: ExitWatcherStatus;
  wallet_balance: number | null;
}

interface FlowSignal {
  netuid: number;
  name: string;
  price: number;
  tao_in_pool: number;
  snapshots: number;
  tao_delta_pct_1h: number | null;
  tao_delta_pct_4h: number | null;
  alpha_delta_pct_4h: number | null;
  adj_flow_4h: number | null;
  signal: string;
  reason: string;
  z_score: number | null;
  magnitude_capped: boolean;
}

interface ClosedPosition {
  id: number;
  netuid: number;
  name: string;
  entry_price: number;
  exit_price: number | null;
  amount_tao: number;
  pnl_tao: number | null;
  pnl_pct: number | null;
  exit_reason: string | null;
  entry_ts: string;
  exit_ts: string | null;
}

function fmt(n: number | null | undefined, digits = 2): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return n.toFixed(digits);
}

function fmtPct(n: number | null | undefined, digits = 2): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return `${n >= 0 ? "+" : ""}${n.toFixed(digits)}%`;
}

export default function FlowPage() {
  const { data: portfolio, mutate: refetchPortfolio } = useSWR<PortfolioData>(
    `${API}/api/flow/portfolio`,
    fetcher,
    { refreshInterval: 10_000 },
  );
  const { data: signalsData } = useSWR<{
    signals: FlowSignal[];
    cold_start_snaps?: number;
  }>(
    `${API}/api/flow/signals`,
    fetcher,
    { refreshInterval: 30_000 },
  );
  const { data: positionsData } = useSWR<{ positions: ClosedPosition[] }>(
    `${API}/api/flow/positions?limit=50`,
    fetcher,
    { refreshInterval: 30_000 },
  );

  const [closingId, setClosingId] = useState<number | null>(null);

  async function handleClose(positionId: number) {
    setClosingId(positionId);
    try {
      const res = await fetch(
        `${API}/api/flow/positions/${positionId}/close`,
        { method: "POST" },
      );
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: "Close failed" }));
        alert(`Close failed: ${err.detail || res.statusText}`);
      } else {
        await refetchPortfolio();
      }
    } catch (e) {
      alert(`Close error: ${e}`);
    } finally {
      setClosingId(null);
    }
  }

  if (!portfolio) {
    return (
      <div className="p-6 text-slate-300">
        <p>Loading Pool Flow Momentum…</p>
      </div>
    );
  }

  if (!portfolio.enabled) {
    return (
      <div className="p-6 text-slate-300">
        <h1 className="text-2xl font-semibold mb-2">Pool Flow Momentum</h1>
        <p className="text-slate-400">
          Flow strategy is disabled. Set{" "}
          <code className="text-sky-400">FLOW_ENABLED=true</code> in the .env file
          to enable.
        </p>
      </div>
    );
  }

  const signals = signalsData?.signals || [];
  const coldStart = signalsData?.cold_start_snaps ?? 624;
  const warmReady = signals.filter((s) => s.snapshots >= coldStart).length;
  const warmTotal = signals.length;
  const closed = (positionsData?.positions || []).filter(
    (p) => p.exit_ts !== null,
  );
  const lastSnap = portfolio.snapshot_status?.last_run
    ? `${portfolio.snapshot_status.last_run.slice(11, 19)} (${portfolio.snapshot_status.last_row_count} rows)`
    : "waiting";

  return (
    <div className="p-6 space-y-6 text-slate-200">
      <header>
        <h1 className="text-2xl font-semibold">Pool Flow Momentum</h1>
        <p className="text-sm text-slate-400">
          Detects buy-side capital inflows by tracking pool reserve changes.{" "}
          <span
            className={
              portfolio.dry_run ? "text-amber-400" : "text-emerald-400"
            }
          >
            {portfolio.dry_run ? "DRY RUN" : "LIVE"}
          </span>
          {portfolio.breaker_active && (
            <span className="ml-2 text-rose-400">Breaker active</span>
          )}
        </p>
      </header>

      {warmTotal > 0 && warmReady < warmTotal && (
        <div className="text-amber-300 text-xs">
          Warming up: {warmReady}/{warmTotal} subnets have ≥52 h of snapshots
          (target {coldStart} snaps @ 5 min)
        </div>
      )}

      <section className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <Stat label="Wallet balance" value={`${fmt(portfolio.wallet_balance, 4)} τ`} />
        <Stat label="Trading pot" value={`${fmt(portfolio.pot_tao, 4)} τ`} />
        <Stat label="Deployed" value={`${fmt(portfolio.deployed_tao, 4)} τ`} />
        <Stat
          label="Slots"
          value={`${portfolio.open_count} / ${portfolio.max_positions}`}
        />
        <Stat label="Last snapshot" value={lastSnap} />
      </section>

      <section className="grid grid-cols-2 md:grid-cols-5 gap-4 text-sm">
        <Stat label="z-entry" value={fmt(portfolio.z_entry, 2)} />
        <Stat label="z-exit" value={fmt(portfolio.z_exit, 2)} />
        <Stat label="Min TAO Δ%" value={`${fmt(portfolio.min_tao_pct, 1)}%`} />
        <Stat label="Magnitude cap" value={`${fmt(portfolio.magnitude_cap, 1)}%`} />
        <Stat
          label="Regime ≥"
          value={fmt(portfolio.regime_threshold, 2)}
        />
      </section>

      <section>
        <h2 className="text-lg font-semibold mb-2">Open positions</h2>
        {portfolio.open_positions.length === 0 ? (
          <p className="text-slate-400 text-sm">No open positions.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-left text-slate-400 border-b border-slate-700">
                <tr>
                  <th className="py-2 pr-3">Subnet</th>
                  <th className="py-2 pr-3">Entry</th>
                  <th className="py-2 pr-3">Current</th>
                  <th className="py-2 pr-3">PnL %</th>
                  <th className="py-2 pr-3">Size (τ)</th>
                  <th className="py-2 pr-3">Held</th>
                  <th className="py-2 pr-3"></th>
                </tr>
              </thead>
              <tbody>
                {portfolio.open_positions.map((p) => (
                  <tr key={p.position_id} className="border-b border-slate-800">
                    <td className="py-2 pr-3">
                      {p.name} <span className="text-slate-500">SN{p.netuid}</span>
                    </td>
                    <td className="py-2 pr-3">{fmt(p.entry_price, 6)}</td>
                    <td className="py-2 pr-3">{fmt(p.current_price, 6)}</td>
                    <td
                      className={`py-2 pr-3 ${p.pnl_pct >= 0 ? "text-emerald-400" : "text-rose-400"}`}
                    >
                      {fmtPct(p.pnl_pct)}
                    </td>
                    <td className="py-2 pr-3">{fmt(p.amount_tao, 4)}</td>
                    <td className="py-2 pr-3">{fmt(p.hours_held, 1)}h</td>
                    <td className="py-2 pr-3">
                      <button
                        disabled={closingId === p.position_id}
                        onClick={() => handleClose(p.position_id)}
                        className="px-2 py-1 bg-rose-700/30 hover:bg-rose-700/60 rounded text-xs"
                      >
                        {closingId === p.position_id ? "Closing…" : "Close"}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section>
        <h2 className="text-lg font-semibold mb-2">
          Live flow signals
          <span className="text-xs text-slate-500 ml-2">
            ({signals.length} subnets scored)
          </span>
        </h2>
        <div className="overflow-x-auto max-h-[480px]">
          <table className="w-full text-xs">
            <thead className="text-left text-slate-400 border-b border-slate-700 sticky top-0 bg-slate-900">
              <tr>
                <th className="py-2 pr-3">Subnet</th>
                <th className="py-2 pr-3">Pool (τ)</th>
                <th className="py-2 pr-3">Snaps</th>
                <th className="py-2 pr-3">z</th>
                <th className="py-2 pr-3">TAO Δ 1h</th>
                <th className="py-2 pr-3">TAO Δ 4h</th>
                <th className="py-2 pr-3">α Δ 4h</th>
                <th className="py-2 pr-3">Signal</th>
                <th className="py-2 pr-3">Reason</th>
              </tr>
            </thead>
            <tbody>
              {signals.map((s) => (
                <tr key={s.netuid} className="border-b border-slate-800">
                  <td className="py-2 pr-3">
                    {s.name}{" "}
                    <span className="text-slate-500">SN{s.netuid}</span>
                  </td>
                  <td className="py-2 pr-3">{fmt(s.tao_in_pool, 0)}</td>
                  <td className="py-2 pr-3 text-slate-500">{s.snapshots}</td>
                  <td
                    className={`py-2 pr-3 ${
                      s.z_score !== null && s.z_score >= portfolio.z_entry
                        ? "text-emerald-400"
                        : ""
                    }`}
                  >
                    {s.z_score === null ? "—" : s.z_score.toFixed(2)}
                  </td>
                  <td className="py-2 pr-3">{fmtPct(s.tao_delta_pct_1h)}</td>
                  <td className="py-2 pr-3">{fmtPct(s.tao_delta_pct_4h)}</td>
                  <td className="py-2 pr-3">{fmtPct(s.alpha_delta_pct_4h)}</td>
                  <td
                    className={`py-2 pr-3 ${
                      s.signal === "BUY"
                        ? "text-emerald-400"
                        : s.signal.startsWith("BLOCKED")
                          ? "text-slate-500"
                          : "text-amber-300"
                    }`}
                  >
                    {s.signal}
                  </td>
                  <td className="py-2 pr-3 text-slate-400">{s.reason}</td>
                </tr>
              ))}
              {signals.length === 0 && (
                <tr>
                  <td colSpan={9} className="py-4 text-slate-500">
                    No signal data yet — pool snapshots are still warming up.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>

      <section>
        <h2 className="text-lg font-semibold mb-2">Recent closed trades</h2>
        {closed.length === 0 ? (
          <p className="text-slate-500 text-sm">No closed trades yet.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead className="text-left text-slate-400 border-b border-slate-700">
                <tr>
                  <th className="py-2 pr-3">Subnet</th>
                  <th className="py-2 pr-3">Entry</th>
                  <th className="py-2 pr-3">Exit</th>
                  <th className="py-2 pr-3">PnL</th>
                  <th className="py-2 pr-3">Reason</th>
                  <th className="py-2 pr-3">Closed</th>
                </tr>
              </thead>
              <tbody>
                {closed.slice(0, 25).map((p) => (
                  <tr key={p.id} className="border-b border-slate-800">
                    <td className="py-2 pr-3">
                      {p.name}{" "}
                      <span className="text-slate-500">SN{p.netuid}</span>
                    </td>
                    <td className="py-2 pr-3">{fmt(p.entry_price, 6)}</td>
                    <td className="py-2 pr-3">{fmt(p.exit_price, 6)}</td>
                    <td
                      className={`py-2 pr-3 ${(p.pnl_tao || 0) >= 0 ? "text-emerald-400" : "text-rose-400"}`}
                    >
                      {fmtPct(p.pnl_pct)} ({fmt(p.pnl_tao, 4)} τ)
                    </td>
                    <td className="py-2 pr-3">{p.exit_reason}</td>
                    <td className="py-2 pr-3 text-slate-500">
                      {p.exit_ts?.slice(0, 16)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="text-xs text-slate-500 grid grid-cols-2 gap-2">
        <div>
          <strong className="text-slate-400">Snapshot persistence:</strong>{" "}
          {portfolio.snapshot_status?.last_run
            ? `${portfolio.snapshot_status.last_row_count} rows @ ${portfolio.snapshot_status.last_run.slice(11, 19)}`
            : "waiting"}
          {portfolio.snapshot_status?.last_error && (
            <span className="text-rose-400">
              {" "}
              — error: {portfolio.snapshot_status.last_error}
            </span>
          )}
        </div>
        <div>
          <strong className="text-slate-400">Exit watcher:</strong>{" "}
          {portfolio.exit_watcher?.enabled
            ? `every ${portfolio.exit_watcher.interval_sec}s`
            : "disabled"}
        </div>
      </section>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="bg-slate-800/60 rounded p-3">
      <div className="text-xs uppercase tracking-wide text-slate-400">
        {label}
      </div>
      <div className="text-lg font-mono">{value}</div>
    </div>
  );
}
