"use client";

import { useState } from "react";
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
} from "recharts";

const API = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8080";

interface BacktestResult {
  config: {
    days: number;
    nav_tao: number;
    num_slots: number;
    stop_loss_pct: number;
    take_profit_pct: number;
  };
  total_return_pct: number;
  final_nav_tao: number;
  start_nav_tao: number;
  num_trades: number;
  win_rate_pct: number;
  max_drawdown_pct: number;
  avg_hold_bars: number;
  avg_pnl_pct: number;
  equity_curve: number[];
  trades: Array<{
    netuid: number;
    entry_bar: number;
    exit_bar: number;
    pnl_pct: number;
    pnl_tao: number;
    exit_reason: string;
    hold_bars: number;
  }>;
  stats_by_subnet: Array<{
    netuid: number;
    trades: number;
    win_rate_pct: number;
    total_pnl_tao: number;
    avg_pnl_pct: number;
    avg_hold_bars: number;
  }>;
}

export default function Backtest() {
  const [days, setDays] = useState(7);
  const [nav, setNav] = useState(2.0);
  const [topN, setTopN] = useState(20);
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<BacktestResult | null>(null);
  const [error, setError] = useState("");

  async function runBacktest() {
    setLoading(true);
    setError("");
    setResult(null);
    try {
      const res = await fetch(`${API}/api/backtest`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ days, nav, top_n: topN }),
      });
      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail ?? "Backtest failed");
      }
      const data = await res.json();
      setResult(data);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  const equityData = result?.equity_curve.map((nav, i) => ({ bar: i, nav })) ?? [];
  const barsPerDay = 6;

  return (
    <div>
      <h1 className="text-2xl font-bold mb-6">Backtest</h1>

      {/* Form */}
      <div className="bg-gray-900 rounded-lg border border-gray-800 p-6 mb-8 max-w-lg">
        <div className="grid grid-cols-3 gap-4 mb-4">
          <div>
            <label className="text-xs text-gray-400 block mb-1">Window</label>
            <select
              value={days}
              onChange={(e) => setDays(Number(e.target.value))}
              className="w-full bg-gray-800 border border-gray-700 rounded px-3 py-2 text-sm"
            >
              <option value={7}>7 days</option>
              <option value={14}>14 days</option>
              <option value={30}>30 days</option>
            </select>
          </div>
          <div>
            <label className="text-xs text-gray-400 block mb-1">Starting NAV (τ)</label>
            <input
              type="number"
              value={nav}
              step={0.1}
              min={0.1}
              onChange={(e) => setNav(Number(e.target.value))}
              className="w-full bg-gray-800 border border-gray-700 rounded px-3 py-2 text-sm"
            />
          </div>
          <div>
            <label className="text-xs text-gray-400 block mb-1">Top-N subnets</label>
            <input
              type="number"
              value={topN}
              min={5}
              max={50}
              onChange={(e) => setTopN(Number(e.target.value))}
              className="w-full bg-gray-800 border border-gray-700 rounded px-3 py-2 text-sm"
              disabled={days === 7}
            />
          </div>
        </div>
        {days !== 7 && (
          <p className="text-xs text-yellow-400 mb-4">
            ⚠ 14d/30d mode fetches {topN} extra API calls (~{Math.ceil(topN / 30 * 60)}s). Rate-limit aware.
          </p>
        )}
        <button
          onClick={runBacktest}
          disabled={loading}
          className="bg-indigo-600 hover:bg-indigo-500 disabled:bg-gray-700 text-white px-6 py-2 rounded font-medium text-sm transition-colors"
        >
          {loading ? "Running …" : "Run Backtest"}
        </button>
      </div>

      {error && <p className="text-red-400 mb-4">{error}</p>}

      {result && (
        <div>
          {/* Summary stats */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
            <Stat label="Return" value={`${result.total_return_pct >= 0 ? "+" : ""}${result.total_return_pct.toFixed(2)}%`}
              color={result.total_return_pct >= 0 ? "text-green-400" : "text-red-400"} />
            <Stat label="Max Drawdown" value={`${result.max_drawdown_pct.toFixed(2)}%`} color="text-orange-400" />
            <Stat label="Trades" value={String(result.num_trades)} />
            <Stat label="Win Rate" value={`${result.win_rate_pct.toFixed(1)}%`} />
            <Stat label="Final NAV" value={`${result.final_nav_tao.toFixed(4)} τ`} />
            <Stat label="Avg PnL/Trade" value={`${result.avg_pnl_pct >= 0 ? "+" : ""}${result.avg_pnl_pct.toFixed(2)}%`} />
            <Stat label="Avg Hold" value={`${(result.avg_hold_bars / barsPerDay * 24).toFixed(0)}h`} />
            <Stat label="Window" value={`${result.config.days}d`} />
          </div>

          {/* Equity curve */}
          <div className="bg-gray-900 rounded-lg border border-gray-800 p-4 mb-6">
            <h2 className="text-sm text-gray-400 mb-4">Equity Curve (τ)</h2>
            <ResponsiveContainer width="100%" height={250}>
              <LineChart data={equityData}>
                <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
                <XAxis dataKey="bar" stroke="#6B7280" tick={{ fontSize: 10 }} label={{ value: "bar (~4h each)", position: "insideBottom", offset: -2, fill: "#6B7280", fontSize: 10 }} />
                <YAxis stroke="#6B7280" tick={{ fontSize: 10 }} domain={["auto", "auto"]} />
                <Tooltip
                  contentStyle={{ backgroundColor: "#111827", border: "1px solid #374151", borderRadius: 6 }}
                  formatter={(v: number) => [`${v.toFixed(5)} τ`, "NAV"]}
                />
                <Line type="monotone" dataKey="nav" stroke="#34D399" strokeWidth={2} dot={false} />
              </LineChart>
            </ResponsiveContainer>
          </div>

          {/* Trades table */}
          <h2 className="text-lg font-semibold mb-3">Trades ({result.trades.length})</h2>
          <div className="overflow-x-auto mb-6">
            <table className="w-full text-sm border-collapse">
              <thead>
                <tr className="border-b border-gray-700 text-gray-400 text-xs">
                  <th className="py-2 pr-4 text-left">netuid</th>
                  <th className="py-2 pr-4 text-left">Exit Reason</th>
                  <th className="py-2 pr-4 text-right">PnL %</th>
                  <th className="py-2 pr-4 text-right">PnL τ</th>
                  <th className="py-2 text-right">Hold</th>
                </tr>
              </thead>
              <tbody>
                {[...result.trades].sort((a, b) => b.pnl_tao - a.pnl_tao).map((t, i) => (
                  <tr key={i} className="border-b border-gray-800 hover:bg-gray-900">
                    <td className="py-1.5 pr-4 font-mono">{t.netuid}</td>
                    <td className="py-1.5 pr-4 text-xs text-gray-400">{t.exit_reason}</td>
                    <td className={`py-1.5 pr-4 text-right ${t.pnl_pct >= 0 ? "text-green-400" : "text-red-400"}`}>
                      {t.pnl_pct >= 0 ? "+" : ""}{t.pnl_pct.toFixed(2)}%
                    </td>
                    <td className={`py-1.5 pr-4 text-right ${t.pnl_tao >= 0 ? "text-green-400" : "text-red-400"}`}>
                      {t.pnl_tao >= 0 ? "+" : ""}{t.pnl_tao.toFixed(5)} τ
                    </td>
                    <td className="py-1.5 text-right text-gray-400">{(t.hold_bars / barsPerDay * 24).toFixed(0)}h</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* Per-subnet stats */}
          {result.stats_by_subnet.length > 0 && (
            <>
              <h2 className="text-lg font-semibold mb-3">By Subnet</h2>
              <div className="overflow-x-auto">
                <table className="w-full text-sm border-collapse">
                  <thead>
                    <tr className="border-b border-gray-700 text-gray-400 text-xs">
                      <th className="py-2 pr-4 text-left">netuid</th>
                      <th className="py-2 pr-4 text-right">Trades</th>
                      <th className="py-2 pr-4 text-right">Win%</th>
                      <th className="py-2 pr-4 text-right">Total PnL τ</th>
                      <th className="py-2 text-right">Avg PnL%</th>
                    </tr>
                  </thead>
                  <tbody>
                    {result.stats_by_subnet.map((s) => (
                      <tr key={s.netuid} className="border-b border-gray-800 hover:bg-gray-900">
                        <td className="py-1.5 pr-4 font-mono">{s.netuid}</td>
                        <td className="py-1.5 pr-4 text-right">{s.trades}</td>
                        <td className="py-1.5 pr-4 text-right">{s.win_rate_pct.toFixed(1)}%</td>
                        <td className={`py-1.5 pr-4 text-right ${s.total_pnl_tao >= 0 ? "text-green-400" : "text-red-400"}`}>
                          {s.total_pnl_tao >= 0 ? "+" : ""}{s.total_pnl_tao.toFixed(5)}
                        </td>
                        <td className={`py-1.5 text-right ${s.avg_pnl_pct >= 0 ? "text-green-400" : "text-red-400"}`}>
                          {s.avg_pnl_pct >= 0 ? "+" : ""}{s.avg_pnl_pct.toFixed(2)}%
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </div>
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
