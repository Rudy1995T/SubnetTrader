"use client";

import { useState } from "react";
import useSWR, { mutate } from "swr";
import {
  ResponsiveContainer,
  AreaChart,
  Area,
  YAxis,
  Tooltip,
  BarChart,
  Bar,
  XAxis,
  Cell,
  ReferenceLine,
} from "recharts";

const API = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8080";
const fetcher = (url: string) => fetch(url).then((r) => r.json());

const MAX_HOLD_HOURS = 72;
const STOP_LOSS_PCT = 8;
const TAKE_PROFIT_PCT = 15;

interface Position {
  id: number;
  slot_id: number;
  netuid: number;
  status: string;
  entry_ts: string;
  exit_ts: string | null;
  entry_price: number;
  exit_price: number | null;
  amount_tao_in: number;
  amount_tao_out: number | null;
  pnl_tao: number | null;
  pnl_pct: number | null;
  exit_reason: string | null;
  entry_score: number;
  peak_price: number;
}

interface SubnetInfo {
  netuid: number;
  name: string;
  price: number;
  history: Array<{ t: string; p: number }>;
}

// ── Sparkline for open position cards ────────────────────────────────
function PositionChart({ netuid, entryPrice }: { netuid: number; entryPrice: number }) {
  const { data } = useSWR(`${API}/api/subnets/${netuid}/history`, fetcher, {
    refreshInterval: 60000,
    revalidateOnFocus: false,
  });
  const history: Array<{ p: number }> = data?.history ?? [];
  if (!history.length) return <div className="h-24 bg-gray-800 rounded animate-pulse" />;

  const prices = history.map((h) => h.p);
  const minP = Math.min(...prices, entryPrice) * 0.998;
  const maxP = Math.max(...prices, entryPrice) * 1.002;
  const last = prices[prices.length - 1];
  const positive = last >= entryPrice;

  return (
    <ResponsiveContainer width="100%" height={96}>
      <AreaChart data={history} margin={{ top: 4, right: 0, left: 0, bottom: 0 }}>
        <defs>
          <linearGradient id={`pg-${netuid}`} x1="0" y1="0" x2="0" y2="1">
            <stop offset="5%" stopColor={positive ? "#34D399" : "#F87171"} stopOpacity={0.35} />
            <stop offset="95%" stopColor={positive ? "#34D399" : "#F87171"} stopOpacity={0} />
          </linearGradient>
        </defs>
        <YAxis domain={[minP, maxP]} hide />
        <ReferenceLine y={entryPrice} stroke="#818CF8" strokeDasharray="3 3" strokeWidth={1.5} />
        <Tooltip
          contentStyle={{ backgroundColor: "#111827", border: "1px solid #374151", borderRadius: 4, padding: "4px 8px" }}
          formatter={(v: number) => [v.toFixed(7) + " τ", "price"]}
          labelFormatter={() => ""}
        />
        <Area
          type="monotone"
          dataKey="p"
          stroke={positive ? "#34D399" : "#F87171"}
          strokeWidth={2}
          fill={`url(#pg-${netuid})`}
          dot={false}
          isAnimationActive={false}
        />
      </AreaChart>
    </ResponsiveContainer>
  );
}

// ── SL / TP meter ────────────────────────────────────────────────────
function SLTPMeter({ pnlPct }: { pnlPct: number }) {
  const TOTAL = STOP_LOSS_PCT + TAKE_PROFIT_PCT; // 23
  const clamped = Math.max(-STOP_LOSS_PCT - 2, Math.min(TAKE_PROFIT_PCT + 2, pnlPct));
  const thumbPct = ((clamped + STOP_LOSS_PCT) / TOTAL) * 100;
  const inDanger = pnlPct <= -STOP_LOSS_PCT * 0.7;
  const nearTP = pnlPct >= TAKE_PROFIT_PCT * 0.7;

  return (
    <div className="mt-3">
      <div className="flex justify-between text-xs text-gray-500 mb-1">
        <span className="text-red-400">SL −{STOP_LOSS_PCT}%</span>
        <span className="text-gray-400">entry</span>
        <span className="text-green-400">TP +{TAKE_PROFIT_PCT}%</span>
      </div>
      <div className="relative h-2 rounded-full overflow-hidden bg-gradient-to-r from-red-900 via-gray-700 to-green-900">
        {/* Zero line */}
        <div
          className="absolute top-0 h-full w-px bg-gray-400 opacity-50"
          style={{ left: `${(STOP_LOSS_PCT / TOTAL) * 100}%` }}
        />
        {/* Thumb */}
        <div
          className={`absolute top-1/2 -translate-y-1/2 w-3 h-3 rounded-full border-2 border-gray-900 shadow-lg transition-all ${
            inDanger ? "bg-red-400" : nearTP ? "bg-green-400" : "bg-white"
          }`}
          style={{ left: `calc(${Math.min(96, Math.max(2, thumbPct))}% - 6px)` }}
        />
      </div>
      <div className="text-center mt-1">
        <span className={`text-xs font-bold ${pnlPct >= 0 ? "text-green-400" : "text-red-400"}`}>
          {pnlPct >= 0 ? "+" : ""}{pnlPct.toFixed(2)}% current
        </span>
      </div>
    </div>
  );
}

// ── Time held progress bar ───────────────────────────────────────────
function TimeBar({ entryTs }: { entryTs: string }) {
  const hoursHeld = (Date.now() - new Date(entryTs).getTime()) / 3_600_000;
  const pct = Math.min(100, (hoursHeld / MAX_HOLD_HOURS) * 100);
  const hoursLeft = Math.max(0, MAX_HOLD_HOURS - hoursHeld);
  const color =
    pct > 80 ? "bg-red-500" : pct > 55 ? "bg-yellow-500" : "bg-emerald-500";

  return (
    <div className="mt-3">
      <div className="flex justify-between text-xs text-gray-500 mb-1">
        <span>{hoursHeld.toFixed(1)}h held</span>
        <span className={pct > 80 ? "text-red-400" : "text-gray-500"}>
          {hoursLeft.toFixed(1)}h left of {MAX_HOLD_HOURS}h
        </span>
      </div>
      <div className="h-1.5 bg-gray-800 rounded-full overflow-hidden">
        <div
          className={`h-full rounded-full transition-all ${color}`}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

// ── Open position card ───────────────────────────────────────────────
function OpenCard({ pos, subnets }: { pos: Position; subnets: SubnetInfo[] }) {
  const [confirming, setConfirming] = useState(false);
  const [closing, setClosing] = useState(false);
  const [closeError, setCloseError] = useState<string | null>(null);

  const info = subnets.find((s) => s.netuid === pos.netuid);
  const currentPrice = info?.price ?? pos.entry_price;
  const name = info?.name ?? `Subnet ${pos.netuid}`;
  const pnlPct = ((currentPrice - pos.entry_price) / pos.entry_price) * 100;
  const pnlTao = pos.amount_tao_in * (pnlPct / 100);
  const peakPct = pos.peak_price > pos.entry_price
    ? ((pos.peak_price - pos.entry_price) / pos.entry_price) * 100
    : 0;

  async function handleClose() {
    setClosing(true);
    setCloseError(null);
    try {
      const res = await fetch(`${API}/api/positions/${pos.id}/close`, { method: "POST" });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail ?? `HTTP ${res.status}`);
      }
      // Refresh positions and portfolio data
      mutate(`${API}/api/positions`);
      mutate(`${API}/api/portfolio`);
      mutate(`${API}/api/fast-portfolio`);
    } catch (err: unknown) {
      setCloseError(err instanceof Error ? err.message : "Unknown error");
      setClosing(false);
      setConfirming(false);
    }
  }

  return (
    <div className={`rounded-xl border p-5 flex flex-col gap-1 ${
      pnlPct >= 0 ? "border-emerald-700 bg-emerald-950/20" : "border-red-800 bg-red-950/20"
    }`}>
      {/* Header */}
      <div className="flex items-start justify-between">
        <div>
          <div className="flex items-center gap-2">
            <span className="font-bold text-white text-base">{name}</span>
            <span className="text-xs text-gray-400 bg-gray-800 px-2 py-0.5 rounded font-mono">
              #{pos.netuid}
            </span>
          </div>
          <span className="text-xs text-gray-500">Slot {pos.slot_id} · Score {pos.entry_score.toFixed(3)}</span>
        </div>
        <div className="text-right">
          <div className={`text-2xl font-bold ${pnlPct >= 0 ? "text-emerald-400" : "text-red-400"}`}>
            {pnlPct >= 0 ? "+" : ""}{pnlPct.toFixed(2)}%
          </div>
          <div className={`text-xs ${pnlTao >= 0 ? "text-emerald-500" : "text-red-500"}`}>
            {pnlTao >= 0 ? "+" : ""}{pnlTao.toFixed(5)} τ
          </div>
        </div>
      </div>

      {/* Price chart */}
      <div className="mt-2">
        <PositionChart netuid={pos.netuid} entryPrice={pos.entry_price} />
        <div className="flex justify-between text-xs text-gray-500 mt-0.5">
          <span>Entry <span className="font-mono text-indigo-300">{pos.entry_price.toFixed(7)}</span></span>
          <span>Now <span className="font-mono text-white">{currentPrice.toFixed(7)}</span></span>
        </div>
      </div>

      {/* SL/TP meter */}
      <SLTPMeter pnlPct={pnlPct} />

      {/* Time bar */}
      <TimeBar entryTs={pos.entry_ts} />

      {/* Footer stats */}
      <div className="flex justify-between text-xs text-gray-500 mt-2 pt-2 border-t border-gray-800">
        <span>In: <span className="text-gray-300">{pos.amount_tao_in.toFixed(4)} τ</span></span>
        {peakPct > 0 && (
          <span>Peak: <span className="text-emerald-400">+{peakPct.toFixed(2)}%</span></span>
        )}
        <span>Entered: <span className="text-gray-300">{new Date(pos.entry_ts).toLocaleString()}</span></span>
      </div>

      {/* Manual close */}
      <div className="mt-3 pt-3 border-t border-gray-800">
        {!confirming ? (
          <button
            onClick={() => setConfirming(true)}
            className="w-full py-1.5 text-xs font-semibold rounded border border-gray-600 text-gray-300 hover:border-amber-500 hover:text-amber-300 transition-colors"
          >
            Close Position
          </button>
        ) : (
          <div className="space-y-2">
            <p className="text-xs text-center text-gray-300">
              Close now at{" "}
              <span className={pnlPct >= 0 ? "text-emerald-400 font-bold" : "text-red-400 font-bold"}>
                {pnlPct >= 0 ? "+" : ""}{pnlPct.toFixed(2)}%
              </span>{" "}
              (~{currentPrice.toFixed(7)} τ)?
            </p>
            <div className="flex gap-2">
              <button
                onClick={handleClose}
                disabled={closing}
                className="flex-1 py-1.5 text-xs font-semibold rounded bg-amber-600 hover:bg-amber-500 text-white disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
              >
                {closing ? "Closing…" : "Confirm"}
              </button>
              <button
                onClick={() => { setConfirming(false); setCloseError(null); }}
                disabled={closing}
                className="flex-1 py-1.5 text-xs font-semibold rounded border border-gray-600 text-gray-400 hover:text-white transition-colors"
              >
                Cancel
              </button>
            </div>
            {closeError && (
              <p className="text-xs text-red-400 text-center">{closeError}</p>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

// ── PnL bar chart for closed trades ─────────────────────────────────
function PnLBarChart({ positions }: { positions: Position[] }) {
  if (!positions.length) return null;
  const data = [...positions]
    .sort((a, b) => new Date(a.entry_ts).getTime() - new Date(b.entry_ts).getTime())
    .map((p) => ({
      label: `#${p.netuid}`,
      pnl: p.pnl_pct ?? 0,
      reason: p.exit_reason ?? "",
    }));

  return (
    <div className="bg-gray-900 rounded-xl border border-gray-800 p-4 mb-6">
      <h3 className="text-sm text-gray-400 mb-3">PnL per Trade (%)</h3>
      <ResponsiveContainer width="100%" height={Math.max(120, data.length * 28)}>
        <BarChart data={data} layout="vertical" margin={{ top: 0, right: 40, left: 40, bottom: 0 }}>
          <XAxis type="number" stroke="#4B5563" tick={{ fontSize: 10 }}
            tickFormatter={(v: number) => `${v > 0 ? "+" : ""}${v.toFixed(1)}%`} />
          <YAxis type="category" dataKey="label" stroke="#4B5563" tick={{ fontSize: 11 }} width={36} />
          <ReferenceLine x={0} stroke="#6B7280" strokeWidth={1} />
          <Tooltip
            contentStyle={{ backgroundColor: "#111827", border: "1px solid #374151", borderRadius: 6 }}
            formatter={(v: number) => [`${v >= 0 ? "+" : ""}${v.toFixed(2)}%`, "PnL"]}
          />
          <Bar dataKey="pnl" radius={[0, 3, 3, 0]}>
            {data.map((entry, i) => (
              <Cell key={i} fill={entry.pnl >= 0 ? "#34D399" : "#F87171"} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

// ── Closed trade row ────────────────────────────────────────────────
function ClosedRow({ pos, name }: { pos: Position; name: string }) {
  const pnl = pos.pnl_pct ?? 0;
  const positive = pnl >= 0;
  const reasonColor: Record<string, string> = {
    TAKE_PROFIT: "text-emerald-400 bg-emerald-900/40",
    STOP_LOSS: "text-red-400 bg-red-900/40",
    TRAILING_STOP: "text-yellow-400 bg-yellow-900/40",
    TIME_STOP: "text-blue-400 bg-blue-900/40",
  };
  const rColor = reasonColor[pos.exit_reason ?? ""] ?? "text-gray-400 bg-gray-800";

  return (
    <tr className="border-b border-gray-800 hover:bg-gray-900/60">
      <td className="py-2 pr-3 font-mono text-indigo-300">{pos.netuid}</td>
      <td className="py-2 pr-3 text-gray-300 text-xs max-w-[120px] truncate">{name}</td>
      <td className="py-2 pr-3 font-mono text-xs">{pos.entry_price.toFixed(7)}</td>
      <td className="py-2 pr-3 font-mono text-xs">{pos.exit_price?.toFixed(7) ?? "—"}</td>
      <td className={`py-2 pr-3 font-bold ${positive ? "text-emerald-400" : "text-red-400"}`}>
        {positive ? "+" : ""}{pnl.toFixed(2)}%
      </td>
      <td className={`py-2 pr-3 text-xs ${positive ? "text-emerald-500" : "text-red-500"}`}>
        {pos.pnl_tao != null ? `${pos.pnl_tao >= 0 ? "+" : ""}${pos.pnl_tao.toFixed(5)} τ` : "—"}
      </td>
      <td className="py-2 pr-3">
        {pos.exit_reason && (
          <span className={`text-xs px-1.5 py-0.5 rounded font-medium ${rColor}`}>
            {pos.exit_reason.replace("_", " ")}
          </span>
        )}
      </td>
      <td className="py-2 text-xs text-gray-500">
        {pos.entry_ts && pos.exit_ts
          ? `${((new Date(pos.exit_ts).getTime() - new Date(pos.entry_ts).getTime()) / 3_600_000).toFixed(1)}h`
          : "—"}
      </td>
    </tr>
  );
}

// ── Page ─────────────────────────────────────────────────────────────
export default function Positions() {
  const { data: posData, error } = useSWR(`${API}/api/positions`, fetcher, { refreshInterval: 30000 });
  const { data: subData } = useSWR(`${API}/api/subnets`, fetcher, { refreshInterval: 60000 });

  if (error) return <p className="text-red-400">Failed to load positions.</p>;
  if (!posData) return <p className="text-gray-400">Loading …</p>;

  const positions: Position[] = posData.positions ?? [];
  const subnets: SubnetInfo[] = subData?.subnets ?? [];
  const open = positions.filter((p) => p.status === "OPEN");
  const closed = positions.filter((p) => p.status === "CLOSED");

  // Summary stats
  const realizedPnl = closed.reduce((s, p) => s + (p.pnl_tao ?? 0), 0);
  const wins = closed.filter((p) => (p.pnl_tao ?? 0) > 0).length;
  const winRate = closed.length > 0 ? (wins / closed.length) * 100 : 0;
  const unrealizedPnl = open.reduce((s, p) => {
    const info = subnets.find((x) => x.netuid === p.netuid);
    const current = info?.price ?? p.entry_price;
    return s + p.amount_tao_in * ((current - p.entry_price) / p.entry_price);
  }, 0);

  return (
    <div>
      <h1 className="text-2xl font-bold mb-6">Positions</h1>

      {/* Summary */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-8">
        <Stat label="Open Positions" value={String(open.length)} />
        <Stat
          label="Unrealized PnL"
          value={`${unrealizedPnl >= 0 ? "+" : ""}${unrealizedPnl.toFixed(5)} τ`}
          color={unrealizedPnl >= 0 ? "text-emerald-400" : "text-red-400"}
        />
        <Stat
          label="Realized PnL"
          value={`${realizedPnl >= 0 ? "+" : ""}${realizedPnl.toFixed(5)} τ`}
          color={realizedPnl >= 0 ? "text-emerald-400" : "text-red-400"}
        />
        <Stat
          label="Win Rate"
          value={closed.length > 0 ? `${winRate.toFixed(0)}% (${wins}/${closed.length})` : "—"}
          color={winRate >= 50 ? "text-emerald-400" : winRate > 0 ? "text-red-400" : "text-gray-400"}
        />
      </div>

      {/* Open positions */}
      <h2 className="text-lg font-semibold mb-4 text-indigo-300">
        Open Positions ({open.length})
      </h2>
      {open.length === 0 ? (
        <p className="text-gray-500 text-sm mb-8">No open positions.</p>
      ) : (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 mb-10">
          {open.map((p) => (
            <OpenCard key={p.id} pos={p} subnets={subnets} />
          ))}
        </div>
      )}

      {/* Closed positions */}
      <h2 className="text-lg font-semibold mb-4 text-gray-300">
        Closed Trades ({closed.length})
      </h2>
      {closed.length === 0 ? (
        <p className="text-gray-500 text-sm">No closed trades yet.</p>
      ) : (
        <>
          <PnLBarChart positions={closed} />
          <div className="overflow-x-auto">
            <table className="w-full text-sm border-collapse">
              <thead>
                <tr className="border-b border-gray-700 text-xs text-gray-400">
                  <th className="py-2 pr-3 text-left">netuid</th>
                  <th className="py-2 pr-3 text-left">Name</th>
                  <th className="py-2 pr-3 text-left">Entry τ</th>
                  <th className="py-2 pr-3 text-left">Exit τ</th>
                  <th className="py-2 pr-3 text-left">PnL %</th>
                  <th className="py-2 pr-3 text-left">PnL τ</th>
                  <th className="py-2 pr-3 text-left">Reason</th>
                  <th className="py-2 text-left">Hold</th>
                </tr>
              </thead>
              <tbody>
                {[...closed]
                  .sort((a, b) => new Date(b.entry_ts).getTime() - new Date(a.entry_ts).getTime())
                  .map((p) => (
                    <ClosedRow
                      key={p.id}
                      pos={p}
                      name={subnets.find((s) => s.netuid === p.netuid)?.name ?? `#${p.netuid}`}
                    />
                  ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}

function Stat({ label, value, color = "text-white" }: { label: string; value: string; color?: string }) {
  return (
    <div className="bg-gray-900 rounded-lg border border-gray-800 p-4">
      <p className="text-xs text-gray-400 mb-1">{label}</p>
      <p className={`text-xl font-bold ${color}`}>{value}</p>
    </div>
  );
}
