"use client";

import { useState, useMemo } from "react";
import useSWR from "swr";
import {
  ResponsiveContainer,
  AreaChart,
  Area,
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ReferenceLine,
} from "recharts";

const API = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8080";
const fetcher = (url: string) => fetch(url).then((r) => r.json());

interface Subnet {
  netuid: number;
  name: string;
  symbol: string;
  price: number;
  change_1h: number;
  change_24h: number;
  change_7d: number;
  volume_24h: number;
  market_cap: number;
  total_tao: number;
  buys_24h: number;
  sells_24h: number;
  history: Array<{ t: string; p: number }>;
}

type SortKey = "netuid" | "price" | "change_1h" | "change_24h" | "change_7d" | "volume_24h" | "total_tao";

function Sparkline({ history, change }: { history: Array<{ p: number }>; change: number }) {
  if (!history.length) return <span className="text-gray-700 text-xs">—</span>;
  const positive = change >= 0;
  return (
    <ResponsiveContainer width={80} height={32}>
      <AreaChart data={history} margin={{ top: 2, right: 0, left: 0, bottom: 2 }}>
        <defs>
          <linearGradient id={`sg-${Math.random()}`} x1="0" y1="0" x2="0" y2="1">
            <stop offset="5%" stopColor={positive ? "#34D399" : "#F87171"} stopOpacity={0.4} />
            <stop offset="95%" stopColor={positive ? "#34D399" : "#F87171"} stopOpacity={0} />
          </linearGradient>
        </defs>
        <Area type="monotone" dataKey="p" stroke={positive ? "#34D399" : "#F87171"}
          strokeWidth={1.5} fill="none" dot={false} isAnimationActive={false} />
      </AreaChart>
    </ResponsiveContainer>
  );
}

function ChangeCell({ value }: { value: number }) {
  if (value === 0) return <span className="text-gray-500">—</span>;
  const color = value > 0 ? "text-green-400" : "text-red-400";
  return <span className={color}>{value > 0 ? "+" : ""}{value.toFixed(2)}%</span>;
}

function DetailChart({ netuid, name }: { netuid: number; name: string }) {
  const { data, isLoading } = useSWR(`${API}/api/subnets/${netuid}/history`, fetcher, {
    revalidateOnFocus: false,
  });

  if (isLoading) return <p className="text-gray-500 text-sm py-4">Loading chart…</p>;

  const history: Array<{ t: string; p: number }> = data?.history ?? [];
  if (!history.length) return <p className="text-gray-500 text-sm py-4">No price history available.</p>;

  const chartData = history.map((h) => ({
    t: h.t ? new Date(h.t).toLocaleDateString("en-GB", { month: "short", day: "numeric" }) + " " +
      new Date(h.t).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" }) : "",
    p: h.p,
  }));

  const prices = history.map((h) => h.p);
  const minP = Math.min(...prices);
  const maxP = Math.max(...prices);
  const first = prices[0];
  const last = prices[prices.length - 1];
  const change = first > 0 ? ((last - first) / first * 100) : 0;
  const positive = change >= 0;

  return (
    <div className="mt-4 bg-gray-900 rounded-lg border border-gray-700 p-4">
      <div className="flex items-baseline justify-between mb-4">
        <div>
          <span className="font-bold text-white">{name || `Subnet ${netuid}`}</span>
          <span className="text-gray-400 text-sm ml-2">netuid {netuid}</span>
        </div>
        <div className="text-right">
          <div className="font-mono text-white">{last.toFixed(7)} τ</div>
          <div className={`text-sm ${positive ? "text-green-400" : "text-red-400"}`}>
            {positive ? "+" : ""}{change.toFixed(2)}% (7d)
          </div>
        </div>
      </div>
      <ResponsiveContainer width="100%" height={220}>
        <LineChart data={chartData}>
          <CartesianGrid strokeDasharray="3 3" stroke="#1F2937" />
          <XAxis dataKey="t" stroke="#4B5563" tick={{ fontSize: 10 }}
            interval={Math.floor(chartData.length / 6)} />
          <YAxis stroke="#4B5563" tick={{ fontSize: 10 }}
            domain={[minP * 0.998, maxP * 1.002]}
            tickFormatter={(v: number) => v.toFixed(5)} width={72} />
          <Tooltip
            contentStyle={{ backgroundColor: "#111827", border: "1px solid #374151", borderRadius: 6 }}
            labelStyle={{ color: "#9CA3AF", fontSize: 11 }}
            formatter={(v: number) => [v.toFixed(7) + " τ", "Price"]}
          />
          <Line type="monotone" dataKey="p"
            stroke={positive ? "#34D399" : "#F87171"}
            strokeWidth={2} dot={false} isAnimationActive={false} />
        </LineChart>
      </ResponsiveContainer>
      <div className="flex gap-6 mt-3 text-xs text-gray-400">
        <span>Low: <span className="text-white font-mono">{minP.toFixed(7)} τ</span></span>
        <span>High: <span className="text-white font-mono">{maxP.toFixed(7)} τ</span></span>
        <span>Range: <span className="text-white">{((maxP - minP) / minP * 100).toFixed(2)}%</span></span>
      </div>
    </div>
  );
}

export default function Explore() {
  const { data, error, isLoading } = useSWR(`${API}/api/subnets`, fetcher, {
    refreshInterval: 120000,
  });

  const [search, setSearch] = useState("");
  const [sortKey, setSortKey] = useState<SortKey>("total_tao");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");
  const [selected, setSelected] = useState<number | null>(null);

  const subnets: Subnet[] = data?.subnets ?? [];

  const filtered = useMemo(() => {
    const q = search.toLowerCase();
    const list = subnets.filter(
      (s) =>
        String(s.netuid).includes(q) ||
        (s.name || "").toLowerCase().includes(q) ||
        (s.symbol || "").toLowerCase().includes(q)
    );
    list.sort((a, b) => {
      const va = a[sortKey] as number;
      const vb = b[sortKey] as number;
      return sortDir === "desc" ? vb - va : va - vb;
    });
    return list;
  }, [subnets, search, sortKey, sortDir]);

  function toggleSort(key: SortKey) {
    if (sortKey === key) {
      setSortDir((d) => (d === "desc" ? "asc" : "desc"));
    } else {
      setSortKey(key);
      setSortDir("desc");
    }
  }

  function SortHeader({ label, k }: { label: string; k: SortKey }) {
    const active = sortKey === k;
    return (
      <th
        className={`py-2 pr-4 text-right cursor-pointer select-none whitespace-nowrap ${active ? "text-indigo-300" : "text-gray-400"} hover:text-white`}
        onClick={() => toggleSort(k)}
      >
        {label} {active ? (sortDir === "desc" ? "↓" : "↑") : ""}
      </th>
    );
  }

  if (error) return <p className="text-red-400">Failed to load subnets.</p>;

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold">Subnet Explorer</h1>
        {data && (
          <span className="text-xs text-gray-500">{data.count} subnets · refreshes every 2 min</span>
        )}
      </div>

      <input
        type="text"
        placeholder="Search by netuid, name or symbol…"
        value={search}
        onChange={(e) => setSearch(e.target.value)}
        className="w-full max-w-md bg-gray-900 border border-gray-700 rounded px-4 py-2 text-sm mb-6 focus:outline-none focus:border-indigo-500"
      />

      {/* Selected subnet chart */}
      {selected !== null && (() => {
        const s = subnets.find((x) => x.netuid === selected);
        return s ? <DetailChart netuid={selected} name={s.name} /> : null;
      })()}

      {isLoading && <p className="text-gray-400">Loading subnets…</p>}

      {!isLoading && (
        <div className="overflow-x-auto mt-6">
          <table className="w-full text-sm border-collapse">
            <thead>
              <tr className="border-b border-gray-700 text-xs">
                <SortHeader label="netuid" k="netuid" />
                <th className="py-2 pr-4 text-left text-gray-400">Name</th>
                <SortHeader label="Price (τ)" k="price" />
                <SortHeader label="1h %" k="change_1h" />
                <SortHeader label="24h %" k="change_24h" />
                <SortHeader label="7d %" k="change_7d" />
                <SortHeader label="Vol 24h (τ)" k="volume_24h" />
                <SortHeader label="TVL (τ)" k="total_tao" />
                <th className="py-2 pr-4 text-right text-gray-400">7d chart</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((s) => {
                const isSelected = selected === s.netuid;
                return (
                  <tr
                    key={s.netuid}
                    onClick={() => setSelected(isSelected ? null : s.netuid)}
                    className={`border-b border-gray-800 cursor-pointer transition-colors ${
                      isSelected ? "bg-indigo-900/20 border-indigo-700" : "hover:bg-gray-900"
                    }`}
                  >
                    <td className="py-2 pr-4 text-right font-mono font-bold text-indigo-300">
                      {s.netuid}
                    </td>
                    <td className="py-2 pr-4 text-left max-w-[140px]">
                      <div className="truncate text-gray-200">{s.name || "—"}</div>
                      <div className="text-gray-500 text-xs">{s.symbol}</div>
                    </td>
                    <td className="py-2 pr-4 text-right font-mono text-gray-100">
                      {s.price > 0 ? s.price.toFixed(s.price < 0.01 ? 7 : 4) : "—"}
                    </td>
                    <td className="py-2 pr-4 text-right"><ChangeCell value={s.change_1h} /></td>
                    <td className="py-2 pr-4 text-right"><ChangeCell value={s.change_24h} /></td>
                    <td className="py-2 pr-4 text-right"><ChangeCell value={s.change_7d} /></td>
                    <td className="py-2 pr-4 text-right text-gray-300">
                      {s.volume_24h > 0 ? s.volume_24h.toFixed(2) : "—"}
                    </td>
                    <td className="py-2 pr-4 text-right text-gray-300">
                      {s.total_tao > 0 ? (s.total_tao / 1e6).toFixed(2) + "M" : "—"}
                    </td>
                    <td className="py-2 text-right">
                      <Sparkline history={s.history} change={s.change_7d} />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {filtered.length === 0 && (
            <p className="text-gray-500 text-sm text-center py-8">No subnets match your search.</p>
          )}
        </div>
      )}
    </div>
  );
}
