"use client";

import useSWR from "swr";
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ResponsiveContainer,
} from "recharts";

const API = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8080";
const fetcher = (url: string) => fetch(url).then((r) => r.json());

interface NavRow {
  date: string;
  nav_tao: number;
  tao_cash: number;
  positions_value: number;
  drawdown_pct: number;
  trades_today: number;
}

export default function Equity() {
  const { data, error } = useSWR(`${API}/api/nav`, fetcher, { refreshInterval: 60000 });

  if (error) return <p className="text-red-400">Failed to load NAV data.</p>;
  if (!data) return <p className="text-gray-400">Loading …</p>;

  const rows: NavRow[] = data.nav ?? [];

  if (!rows.length) {
    return (
      <div>
        <h1 className="text-2xl font-bold mb-4">Equity Curve</h1>
        <p className="text-gray-500">No NAV data yet. Data populates after each scan cycle.</p>
      </div>
    );
  }

  const startNav = rows[0]?.nav_tao ?? 0;
  const latest = rows[rows.length - 1];
  const totalReturn = startNav > 0 ? ((latest.nav_tao - startNav) / startNav * 100).toFixed(2) : "0.00";
  const maxDD = Math.max(...rows.map((r) => r.drawdown_pct)).toFixed(2);

  // Add flat benchmark (hold TAO) to each row
  const chartData = rows.map((r) => ({ ...r, hold_tao: startNav }));

  return (
    <div>
      <h1 className="text-2xl font-bold mb-6">Equity Curve</h1>

      <div className="grid grid-cols-3 gap-4 mb-8">
        <Stat label="Current NAV" value={`${latest.nav_tao.toFixed(4)} τ`} />
        <Stat label="Total Return" value={`${totalReturn}%`} color={parseFloat(totalReturn) >= 0 ? "text-green-400" : "text-red-400"} />
        <Stat label="Max Drawdown" value={`${maxDD}%`} color="text-orange-400" />
      </div>

      <div className="bg-gray-900 rounded-lg border border-gray-800 p-4 mb-6">
        <h2 className="text-sm text-gray-400 mb-1">NAV (τ) over time</h2>
        <p className="text-xs text-gray-600 mb-4">Dashed line = Hold TAO flat (break-even benchmark)</p>
        <ResponsiveContainer width="100%" height={300}>
          <LineChart data={chartData}>
            <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
            <XAxis dataKey="date" stroke="#6B7280" tick={{ fontSize: 11 }} />
            <YAxis stroke="#6B7280" tick={{ fontSize: 11 }} domain={["auto", "auto"]} />
            <Tooltip
              contentStyle={{ backgroundColor: "#111827", border: "1px solid #374151", borderRadius: 6 }}
              labelStyle={{ color: "#9CA3AF" }}
            />
            <Legend
              formatter={(value) => (
                <span style={{ color: value === "NAV (τ)" ? "#818CF8" : "#6B7280", fontSize: 12 }}>
                  {value}
                </span>
              )}
            />
            <Line
              type="monotone"
              dataKey="nav_tao"
              stroke="#818CF8"
              strokeWidth={2}
              dot={rows.length < 30}
              name="NAV (τ)"
            />
            <Line
              type="monotone"
              dataKey="hold_tao"
              stroke="#4B5563"
              strokeWidth={1}
              strokeDasharray="5 3"
              dot={false}
              name="Hold TAO"
            />
          </LineChart>
        </ResponsiveContainer>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full text-sm text-left border-collapse">
          <thead>
            <tr className="border-b border-gray-700 text-gray-400 text-xs">
              <th className="py-2 pr-4">Date</th>
              <th className="py-2 pr-4">NAV (τ)</th>
              <th className="py-2 pr-4">Cash (τ)</th>
              <th className="py-2 pr-4">Positions (τ)</th>
              <th className="py-2 pr-4">Drawdown</th>
              <th className="py-2">Trades</th>
            </tr>
          </thead>
          <tbody>
            {[...rows].reverse().map((r) => (
              <tr key={r.date} className="border-b border-gray-800 hover:bg-gray-900">
                <td className="py-2 pr-4">{r.date}</td>
                <td className={`py-2 pr-4 font-mono ${r.nav_tao >= startNav ? "text-green-400" : "text-red-400"}`}>
                  {r.nav_tao.toFixed(4)}
                </td>
                <td className="py-2 pr-4 font-mono">{r.tao_cash.toFixed(4)}</td>
                <td className="py-2 pr-4 font-mono">{r.positions_value.toFixed(4)}</td>
                <td className={`py-2 pr-4 ${r.drawdown_pct > 5 ? "text-red-400" : "text-gray-300"}`}>
                  {r.drawdown_pct.toFixed(2)}%
                </td>
                <td className="py-2">{r.trades_today}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
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
