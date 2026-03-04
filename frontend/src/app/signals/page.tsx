"use client";

import useSWR from "swr";

const API = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8080";
const fetcher = (url: string) => fetch(url).then((r) => r.json());

interface Signal {
  id: number;
  scan_ts: string;
  netuid: number;
  trend: number;
  support_resist: number;
  fibonacci: number;
  volatility: number;
  mean_reversion: number;
  value_band: number;
  composite: number;
  rank: number;
}

function SignalBar({ value }: { value: number }) {
  const pct = Math.round(value * 100);
  const color = pct >= 70 ? "bg-green-500" : pct >= 50 ? "bg-yellow-500" : "bg-gray-600";
  return (
    <div className="flex items-center gap-1">
      <div className="w-16 bg-gray-800 rounded h-1.5 overflow-hidden">
        <div className={`h-full ${color}`} style={{ width: `${pct}%` }} />
      </div>
      <span className="text-xs text-gray-400 w-7">{pct}%</span>
    </div>
  );
}

export default function Signals() {
  const { data, error } = useSWR(`${API}/api/signals/latest`, fetcher, { refreshInterval: 30000 });
  const { data: subnetData } = useSWR(`${API}/api/subnets`, fetcher, {
    refreshInterval: 120000,
    revalidateOnFocus: false,
  });

  if (error) return <p className="text-red-400">Failed to load signals.</p>;
  if (!data) return <p className="text-gray-400">Loading …</p>;

  const signals: Signal[] = data.signals ?? [];
  const scanTs: string | null = data.scan_ts;

  const subnetNames: Record<number, string> = {};
  for (const s of (subnetData?.subnets ?? [])) {
    subnetNames[s.netuid] = s.name || "";
  }

  return (
    <div>
      <h1 className="text-2xl font-bold mb-2">Latest Signals</h1>
      {scanTs && (
        <p className="text-xs text-gray-500 mb-6">
          Scan: {new Date(scanTs).toLocaleString()} · {signals.length} subnets
        </p>
      )}

      <div className="overflow-x-auto">
        <table className="w-full text-sm text-left border-collapse">
          <thead>
            <tr className="border-b border-gray-700 text-gray-400 text-xs">
              <th className="py-2 pr-3">Rank</th>
              <th className="py-2 pr-3">netuid</th>
              <th className="py-2 pr-3">Name</th>
              <th className="py-2 pr-3">Composite</th>
              <th className="py-2 pr-3">Trend</th>
              <th className="py-2 pr-3">Support/Res</th>
              <th className="py-2 pr-3">Fibonacci</th>
              <th className="py-2 pr-3">Volatility</th>
              <th className="py-2 pr-3">Mean Rev</th>
              <th className="py-2">Value Band</th>
            </tr>
          </thead>
          <tbody>
            {signals.map((s) => (
              <tr key={s.id} className="border-b border-gray-800 hover:bg-gray-900">
                <td className="py-2 pr-3 text-gray-500">#{s.rank}</td>
                <td className="py-2 pr-3 font-mono font-bold text-indigo-300">{s.netuid}</td>
                <td className="py-2 pr-3 text-gray-300 text-xs max-w-[140px] truncate">
                  {subnetNames[s.netuid] || "—"}
                </td>
                <td className="py-2 pr-3">
                  <span className={`font-bold ${s.composite >= 0.55 ? "text-green-400" : "text-gray-300"}`}>
                    {(s.composite * 100).toFixed(1)}%
                  </span>
                </td>
                <td className="py-2 pr-3"><SignalBar value={s.trend} /></td>
                <td className="py-2 pr-3"><SignalBar value={s.support_resist} /></td>
                <td className="py-2 pr-3"><SignalBar value={s.fibonacci} /></td>
                <td className="py-2 pr-3"><SignalBar value={s.volatility} /></td>
                <td className="py-2 pr-3"><SignalBar value={s.mean_reversion} /></td>
                <td className="py-2"><SignalBar value={s.value_band} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
