"use client";

import { useState } from "react";
import useSWR from "swr";
import {
  ResponsiveContainer,
  AreaChart,
  Area,
  ReferenceLine,
  Tooltip,
  YAxis,
  PieChart,
  Pie,
  Cell,
} from "recharts";

const API = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8080";
const fetcher = (url: string) => fetch(url).then((r) => r.json());

interface Slot {
  id: number;
  status: string;
  netuid: number | null;
  amount_tao: number;
  position_id: number | null;
}

interface FastSlot {
  id: number;
  status: string;
  netuid: number | null;
  amount_tao: number;
  entry_price: number;
  peak_price: number;
  entry_ts: string;
  position_id: number | null;
}

interface FastParams {
  stop_loss_pct: number;
  take_profit_pct: number;
  trailing_stop_pct: number;
  max_hold_hours: number;
  enter_threshold: number;
}

interface Risk {
  start_nav: number;
  current_nav: number;
  trades_today: number;
  halted: boolean;
  halt_reason: string;
}

interface Anomaly {
  netuid: number;
  name: string;
  price: number;
  chg_4h: number;
  chg_24h: number;
  magnitude: number;
}

interface HistoryPoint { t: string; p: number }

// ── Anomaly Banner ────────────────────────────────────────────────

function AnomalyBanner() {
  const { data } = useSWR(`${API}/api/anomalies`, fetcher, { refreshInterval: 60000 });
  const [dismissed, setDismissed] = useState<number[]>([]);
  const anomalies: Anomaly[] = (data?.anomalies ?? []).filter(
    (a: Anomaly) => !dismissed.includes(a.netuid)
  );

  if (!anomalies.length) return null;

  return (
    <div className="mb-6 bg-amber-950/30 border border-amber-700 rounded-xl p-4">
      <div className="flex items-center justify-between mb-2">
        <span className="text-amber-300 font-semibold text-sm">
          ⚠ Subnet Anomalies Detected ({anomalies.length})
        </span>
        <button
          onClick={() => setDismissed(anomalies.map((a) => a.netuid))}
          className="text-xs text-gray-500 hover:text-gray-300"
        >
          Dismiss all
        </button>
      </div>
      <div className="space-y-1.5">
        {anomalies.map((a) => (
          <div key={a.netuid} className="flex items-center gap-3 text-sm">
            <span className="font-mono text-indigo-300 w-8 shrink-0">{a.netuid}</span>
            <span className="text-gray-300 truncate max-w-[120px]">{a.name || `Subnet ${a.netuid}`}</span>
            {a.chg_4h !== 0 && (
              <span className={`text-xs font-semibold ${a.chg_4h >= 0 ? "text-green-400" : "text-red-400"}`}>
                {a.chg_4h >= 0 ? "+" : ""}{a.chg_4h.toFixed(1)}% (4h)
              </span>
            )}
            {a.chg_24h !== 0 && (
              <span className={`text-xs ${a.chg_24h >= 0 ? "text-green-300" : "text-red-300"}`}>
                {a.chg_24h >= 0 ? "+" : ""}{a.chg_24h.toFixed(1)}% (24h)
              </span>
            )}
            <button
              onClick={() => setDismissed((d) => [...d, a.netuid])}
              className="ml-auto text-xs text-gray-600 hover:text-gray-400 shrink-0"
            >
              ×
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}

// ── Allocation Donut ──────────────────────────────────────────────

interface DonutEntry { name: string; value: number; color: string }

function AllocationDonut({
  slots, fastSlots, nav, fastBudget, subnetNames,
}: {
  slots: Slot[];
  fastSlots: FastSlot[];
  nav: number;
  fastBudget: number;
  subnetNames: Record<number, string>;
}) {
  const entries: DonutEntry[] = [];

  // Main slots — scale entry amounts so they sum to nav_tao exactly
  const mainAlpha = slots.filter(s => s.status === "ALPHA" && s.netuid !== null);
  const mainDeployed = mainAlpha.reduce((sum, s) => sum + s.amount_tao, 0);
  const mainCash = Math.max(0, nav - mainDeployed);
  const mainScale = mainDeployed > 0 ? Math.min(1, nav / mainDeployed) : 1;

  for (const s of mainAlpha) {
    entries.push({
      name: subnetNames[s.netuid!] || `SN${s.netuid}`,
      value: parseFloat((s.amount_tao * mainScale).toFixed(6)),
      color: "#818CF8",
    });
  }
  if (mainCash > 0.0001) {
    entries.push({ name: "Cash", value: mainCash, color: "#374151" });
  }

  // Fast slots — fastBudget is the exact allocation; use entry amounts directly
  const fastAlpha = fastSlots.filter(s => s.status === "ALPHA" && s.netuid !== null);
  const fastDeployed = fastAlpha.reduce((sum, s) => sum + s.amount_tao, 0);
  const fastCash = Math.max(0, fastBudget - fastDeployed);

  for (const s of fastAlpha) {
    entries.push({
      name: `⚡ ${subnetNames[s.netuid!] || `SN${s.netuid}`}`,
      value: s.amount_tao,
      color: "#F59E0B",
    });
  }
  if (fastCash > 0.0001) {
    entries.push({ name: "⚡ Cash", value: fastCash, color: "#78350F" });
  }

  if (!entries.length) return null;
  const total = entries.reduce((s, e) => s + e.value, 0);

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl p-4">
      <h3 className="text-xs text-gray-400 font-semibold mb-1">Allocation</h3>
      <p className="text-[10px] text-gray-600 mb-2">{total.toFixed(4)} τ total</p>
      <PieChart width={180} height={180}>
        <Pie
          data={entries}
          cx={85}
          cy={85}
          innerRadius={52}
          outerRadius={80}
          dataKey="value"
          strokeWidth={0}
        >
          {entries.map((e, i) => (
            <Cell key={i} fill={e.color} />
          ))}
        </Pie>
        <Tooltip
          contentStyle={{ backgroundColor: "#111827", border: "1px solid #374151", borderRadius: 6, fontSize: 11 }}
          formatter={(v: number, name: string) => [
            `${v.toFixed(4)} τ (${total > 0 ? ((v / total) * 100).toFixed(0) : 0}%)`,
            name,
          ]}
        />
      </PieChart>
      <div className="mt-2 space-y-1">
        {entries.map((e) => (
          <div key={e.name} className="flex items-center gap-2 text-xs">
            <span className="w-2.5 h-2.5 rounded-full shrink-0" style={{ backgroundColor: e.color }} />
            <span className="text-gray-400 truncate flex-1">{e.name}</span>
            <span className="font-mono text-gray-300">{e.value.toFixed(3)} τ</span>
          </div>
        ))}
      </div>
    </div>
  );
}

// ── Slot chart ────────────────────────────────────────────────────

function SlotChart({ netuid, entryPrice, accent = "indigo" }: { netuid: number; entryPrice?: number; accent?: string }) {
  const { data } = useSWR(
    `${API}/api/subnets/${netuid}/history`,
    fetcher,
    { refreshInterval: 60000, revalidateOnFocus: false }
  );

  const history: HistoryPoint[] = data?.history ?? [];
  if (!history.length) {
    return <div className="h-20 flex items-center justify-center text-gray-600 text-xs">no data</div>;
  }

  const prices = history.map((h) => h.p);
  const minP = Math.min(...prices);
  const maxP = Math.max(...prices);
  const first = prices[0];
  const last = prices[prices.length - 1];
  const change = first > 0 ? ((last - first) / first) * 100 : 0;
  const positive = change >= 0;
  const chartData = history.map((h) => ({ p: h.p }));
  const gradId = `grad-${netuid}-${accent}`;

  return (
    <div>
      <div className="flex items-baseline justify-between mb-1">
        <span className="text-xs text-gray-400">7d price</span>
        <span className={`text-xs font-semibold ${positive ? "text-green-400" : "text-red-400"}`}>
          {positive ? "+" : ""}{change.toFixed(2)}% <span className="font-normal opacity-60">7d mkt</span>
        </span>
      </div>
      <ResponsiveContainer width="100%" height={70}>
        <AreaChart data={chartData} margin={{ top: 2, right: 0, left: 0, bottom: 2 }}>
          <defs>
            <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
              <stop offset="5%" stopColor={positive ? "#34D399" : "#F87171"} stopOpacity={0.3} />
              <stop offset="95%" stopColor={positive ? "#34D399" : "#F87171"} stopOpacity={0} />
            </linearGradient>
          </defs>
          <YAxis domain={[minP * 0.99, maxP * 1.01]} hide />
          {entryPrice && entryPrice >= minP && entryPrice <= maxP * 1.02 && (
            <ReferenceLine
              y={entryPrice}
              stroke={accent === "amber" ? "#F59E0B" : "#818CF8"}
              strokeDasharray="3 3"
              strokeWidth={1}
            />
          )}
          <Tooltip
            contentStyle={{ backgroundColor: "#111827", border: "1px solid #374151", borderRadius: 4, padding: "4px 8px" }}
            formatter={(v: number) => [v.toFixed(7) + " τ", "price"]}
            labelFormatter={() => ""}
          />
          <Area
            type="monotone"
            dataKey="p"
            stroke={positive ? "#34D399" : "#F87171"}
            strokeWidth={1.5}
            fill={`url(#${gradId})`}
            dot={false}
            isAnimationActive={false}
          />
        </AreaChart>
      </ResponsiveContainer>
      <div className="flex justify-between text-xs text-gray-600 mt-0.5">
        <span>{minP.toFixed(6)}</span>
        <span>{maxP.toFixed(6)}</span>
      </div>
    </div>
  );
}

function PositionDetail({ positionId }: { positionId: number }) {
  const { data } = useSWR(`${API}/api/positions`, fetcher, { refreshInterval: 30000 });
  const positions = data?.positions ?? [];
  const pos = positions.find((p: { id: number }) => p.id === positionId);
  if (!pos) return null;

  const entryTs = new Date(pos.entry_ts).toLocaleString();
  return (
    <div className="mt-2 pt-2 border-t border-gray-700 text-xs space-y-0.5">
      <div className="flex justify-between text-gray-400">
        <span>Entry price</span>
        <span className="font-mono text-gray-200">{pos.entry_price.toFixed(7)} τ</span>
      </div>
      <div className="flex justify-between text-gray-400">
        <span>Score</span>
        <span className="text-gray-200">{pos.entry_score.toFixed(3)}</span>
      </div>
      <div className="flex justify-between text-gray-400">
        <span>Entered</span>
        <span className="text-gray-200">{entryTs}</span>
      </div>
    </div>
  );
}

function FastSlotLivePnL({ netuid, entryPrice, amountTao }: { netuid: number; entryPrice: number; amountTao: number }) {
  const { data } = useSWR(`${API}/api/subnets/${netuid}/history`, fetcher, {
    refreshInterval: 30000,
    revalidateOnFocus: false,
  });
  const history: HistoryPoint[] = data?.history ?? [];
  if (!history.length || entryPrice <= 0) return null;

  const currentPrice = data?.price ?? history[history.length - 1]?.p ?? 0;
  if (currentPrice <= 0) return null;

  const pnlPct = ((currentPrice - entryPrice) / entryPrice) * 100;
  const pnlTao = amountTao * (pnlPct / 100);
  const positive = pnlPct >= 0;

  return (
    <div className={`mt-1 text-center text-sm font-bold ${positive ? "text-green-400" : "text-red-400"}`}>
      {positive ? "+" : ""}{pnlPct.toFixed(2)}%
      <span className="text-xs font-normal ml-1 opacity-70">
        ({positive ? "+" : ""}{pnlTao.toFixed(4)} τ)
      </span>
    </div>
  );
}

function FastTimeBar({ entryTs, maxHours }: { entryTs: string; maxHours: number }) {
  const entryTime = new Date(entryTs).getTime();
  const now = Date.now();
  const elapsedHours = (now - entryTime) / 3_600_000;
  const pct = Math.min(100, (elapsedHours / maxHours) * 100);
  const color = pct > 75 ? "bg-red-500" : pct > 50 ? "bg-amber-500" : "bg-green-500";

  return (
    <div className="mt-2">
      <div className="flex justify-between text-xs text-gray-500 mb-0.5">
        <span>Hold time</span>
        <span>{elapsedHours.toFixed(1)}h / {maxHours}h</span>
      </div>
      <div className="h-1 bg-gray-700 rounded-full overflow-hidden">
        <div className={`h-full ${color} rounded-full transition-all`} style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}

// ── Main Dashboard ────────────────────────────────────────────────

export default function Dashboard() {
  const { data, error } = useSWR(`${API}/api/portfolio`, fetcher, {
    refreshInterval: 30000,
  });
  const { data: fastData } = useSWR(`${API}/api/fast-portfolio`, fetcher, {
    refreshInterval: 30000,
  });
  const { data: subnetData } = useSWR(`${API}/api/subnets`, fetcher, {
    refreshInterval: 120000,
    revalidateOnFocus: false,
  });

  const subnetNames: Record<number, string> = {};
  for (const s of (subnetData?.subnets ?? [])) {
    subnetNames[s.netuid] = s.name || `Subnet ${s.netuid}`;
  }

  if (error) return <p className="text-red-400">Failed to load portfolio.</p>;
  if (!data) return <p className="text-gray-400">Loading …</p>;

  const slots: Slot[] = data.portfolio?.slots ?? [];
  const risk: Risk = data.portfolio?.risk ?? {};
  const nav: number = data.nav_tao ?? 0;
  const ts: string = data.timestamp ?? "";

  const startNav = risk.start_nav ?? 0;
  const returnPct = startNav > 0 ? ((nav - startNav) / startNav * 100).toFixed(2) : "0.00";
  const drawdownPct = risk.current_nav > 0 && startNav > 0
    ? Math.max(0, (startNav - risk.current_nav) / startNav * 100).toFixed(2)
    : "0.00";

  const fastEnabled: boolean = fastData?.fast_portfolio?.enabled ?? false;
  const fastSlots: FastSlot[] = fastData?.fast_portfolio?.slots ?? [];
  const fastParams: FastParams = fastData?.fast_portfolio?.params ?? {};
  const fastBudget: number = fastData?.fast_portfolio?.budget_tao ?? 0;
  const fastScanMin: number = fastData?.fast_portfolio?.scan_min ?? 30;

  // Derived portfolio stats
  const totalTao = nav + fastBudget;
  const mainOpenCount = slots.filter(s => s.status === "ALPHA").length;
  const fastOpenCount = fastSlots.filter(s => s.status === "ALPHA").length;
  const openPositions = mainOpenCount + fastOpenCount;
  const totalSlots = slots.length + fastSlots.length;

  return (
    <div>
      <h1 className="text-2xl font-bold mb-6">Dashboard</h1>

      {/* Anomaly banner */}
      <AnomalyBanner />

      {/* Stats + donut */}
      <div className="flex flex-col md:flex-row gap-6 mb-8">
        <div className="flex-1">
          <div className="grid grid-cols-2 md:grid-cols-3 gap-4 mb-4">
            <Stat label="Total Portfolio" value={`${totalTao.toFixed(4)} τ`} sub={`${openPositions}/${totalSlots} slots active`} />
            <Stat label="Main Portfolio" value={`${nav.toFixed(4)} τ`} sub={`${mainOpenCount}/${slots.length} slots · mark-to-mkt`} color="text-indigo-300" />
            <Stat label="Fast Portfolio" value={`${fastBudget.toFixed(4)} τ`} sub={`${fastOpenCount}/${fastSlots.length} slots active`} color="text-amber-300" />
          </div>
          <div className="grid grid-cols-2 md:grid-cols-3 gap-4">
            <Stat
              label="Return Today"
              value={`${parseFloat(returnPct) >= 0 ? "+" : ""}${returnPct}%`}
              color={parseFloat(returnPct) >= 0 ? "text-green-400" : "text-red-400"}
            />
            <Stat label="Drawdown" value={`${drawdownPct}%`} color={parseFloat(drawdownPct) > 5 ? "text-red-400" : "text-gray-200"} />
            <Stat
              label="Status"
              value={risk.halted ? "⛔ HALTED" : "✅ ACTIVE"}
              color={risk.halted ? "text-red-400" : "text-green-400"}
              sub={`${risk.trades_today ?? 0} trades today`}
            />
          </div>
        </div>
        <AllocationDonut
          slots={slots}
          fastSlots={fastSlots}
          nav={nav}
          fastBudget={fastBudget}
          subnetNames={subnetNames}
        />
      </div>

      {/* Main portfolio slots */}
      <h2 className="text-lg font-semibold mb-3">Portfolio Slots</h2>
      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-4 mb-8">
        {slots.map((slot) => (
          <div
            key={slot.id}
            className={`rounded-lg border p-4 ${
              slot.status === "ALPHA"
                ? "border-indigo-600 bg-indigo-900/20"
                : "border-gray-700 bg-gray-900"
            }`}
          >
            <div className="flex justify-between items-center mb-3">
              <span className="text-xs text-gray-400">Slot {slot.id}</span>
              <span className={`text-xs font-bold px-2 py-0.5 rounded ${
                slot.status === "ALPHA" ? "bg-indigo-700 text-white" : "bg-gray-700 text-gray-300"
              }`}>
                {slot.status}
              </span>
            </div>

            {slot.status === "ALPHA" && slot.netuid !== null ? (
              <>
                <div className="flex items-baseline justify-between mb-2">
                  <div>
                    <p className="text-base font-bold leading-tight">
                      {subnetNames[slot.netuid] || `Subnet ${slot.netuid}`}
                    </p>
                    <span className="text-xs text-gray-500 font-mono">netuid {slot.netuid}</span>
                  </div>
                  <p className="text-sm text-indigo-300">{slot.amount_tao.toFixed(4)} τ</p>
                </div>
                <SlotChart netuid={slot.netuid} />
                {slot.position_id && <PositionDetail positionId={slot.position_id} />}
              </>
            ) : (
              <p className="text-gray-500 mt-4 text-center text-sm">— empty —</p>
            )}
          </div>
        ))}
      </div>

      {/* Fast trading section */}
      {fastEnabled && (
        <div>
          <div className="flex items-center gap-3 mb-3">
            <h2 className="text-lg font-semibold">⚡ Fast Scalp Trades</h2>
            <span className="text-xs text-amber-400 bg-amber-900/30 border border-amber-700 px-2 py-0.5 rounded">
              {fastBudget} τ budget · scans every {fastScanMin}min
            </span>
          </div>

          {/* Params pill row */}
          <div className="flex flex-wrap gap-2 mb-4 text-xs text-gray-400">
            <span className="bg-gray-800 px-2 py-1 rounded border border-gray-700">
              SL <span className="text-red-400 font-semibold">{fastParams.stop_loss_pct}%</span>
            </span>
            <span className="bg-gray-800 px-2 py-1 rounded border border-gray-700">
              TP <span className="text-green-400 font-semibold">{fastParams.take_profit_pct}%</span>
            </span>
            <span className="bg-gray-800 px-2 py-1 rounded border border-gray-700">
              Trailing <span className="text-amber-400 font-semibold">{fastParams.trailing_stop_pct}%</span>
            </span>
            <span className="bg-gray-800 px-2 py-1 rounded border border-gray-700">
              Max hold <span className="text-blue-400 font-semibold">{fastParams.max_hold_hours}h</span>
            </span>
            <span className="bg-gray-800 px-2 py-1 rounded border border-gray-700">
              Min score <span className="text-purple-400 font-semibold">{fastParams.enter_threshold}</span>
            </span>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mb-6">
            {fastSlots.map((slot) => (
              <div
                key={slot.id}
                className={`rounded-lg border p-4 ${
                  slot.status === "ALPHA"
                    ? "border-amber-600 bg-amber-900/10"
                    : "border-gray-700 bg-gray-900"
                }`}
              >
                <div className="flex justify-between items-center mb-3">
                  <span className="text-xs text-gray-400">Scalp Slot {slot.id}</span>
                  <span className={`text-xs font-bold px-2 py-0.5 rounded ${
                    slot.status === "ALPHA" ? "bg-amber-700 text-white" : "bg-gray-700 text-gray-300"
                  }`}>
                    {slot.status}
                  </span>
                </div>

                {slot.status === "ALPHA" && slot.netuid !== null ? (
                  <>
                    <div className="flex items-baseline justify-between mb-2">
                      <div>
                        <p className="text-base font-bold leading-tight">
                          {subnetNames[slot.netuid] || `Subnet ${slot.netuid}`}
                        </p>
                        <span className="text-xs text-gray-500 font-mono">netuid {slot.netuid}</span>
                      </div>
                      <p className="text-sm text-amber-300">{slot.amount_tao.toFixed(4)} τ</p>
                    </div>
                    <FastSlotLivePnL
                      netuid={slot.netuid}
                      entryPrice={slot.entry_price}
                      amountTao={slot.amount_tao}
                    />
                    <div className="mt-3">
                      <SlotChart netuid={slot.netuid} entryPrice={slot.entry_price} accent="amber" />
                    </div>
                    {slot.entry_ts && (
                      <FastTimeBar entryTs={slot.entry_ts} maxHours={fastParams.max_hold_hours ?? 4} />
                    )}
                    <div className="mt-2 pt-2 border-t border-gray-700 text-xs space-y-0.5">
                      <div className="flex justify-between text-gray-400">
                        <span>Entry price</span>
                        <span className="font-mono text-gray-200">{slot.entry_price.toFixed(7)} τ</span>
                      </div>
                      <div className="flex justify-between text-gray-400">
                        <span>Peak price</span>
                        <span className="font-mono text-gray-200">{slot.peak_price.toFixed(7)} τ</span>
                      </div>
                    </div>
                  </>
                ) : (
                  <p className="text-gray-500 mt-4 text-center text-sm">— watching for signal —</p>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      <p className="text-xs text-gray-600 mt-2">
        Last updated: {new Date(ts).toLocaleString()} · auto-refreshes every 30s
      </p>
    </div>
  );
}

function Stat({ label, value, color = "text-white", sub }: { label: string; value: string; color?: string; sub?: string }) {
  return (
    <div className="bg-gray-900 rounded-lg border border-gray-800 p-4">
      <p className="text-xs text-gray-400 mb-1">{label}</p>
      <p className={`text-xl font-bold ${color}`}>{value}</p>
      {sub && <p className="text-[10px] text-gray-600 mt-0.5">{sub}</p>}
    </div>
  );
}
