"use client";

import { useState } from "react";
import useSWR from "swr";

const API = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8080";
const fetcher = (url: string) => fetch(url).then((r) => r.json());

interface CorrelationData {
  netuids: number[];
  names: Record<string, string>;
  matrix: Record<string, number>;
  threshold: number;
}

function cellBg(r: number, threshold: number): string {
  const abs = Math.abs(r);
  if (r >= threshold)          return "bg-red-800 text-white";
  if (r >= 0.7)                return "bg-orange-800/70 text-orange-100";
  if (r >= 0.5)                return "bg-orange-900/40 text-orange-300";
  if (r >= 0.3)                return "bg-gray-700/40 text-gray-300";
  if (r >= 0)                  return "bg-gray-900 text-gray-500";
  if (r >= -0.3)               return "bg-blue-950/40 text-blue-400";
  if (r >= -0.6)               return "bg-blue-900/50 text-blue-300";
  return "bg-blue-800/70 text-blue-100";
}

export default function Correlation() {
  const [highlightOnly, setHighlightOnly] = useState(false);
  const [maxSubnets, setMaxSubnets] = useState(20);

  const { data, error, isLoading } = useSWR<CorrelationData>(
    `${API}/api/correlations`,
    fetcher,
    { revalidateOnFocus: false },
  );

  if (error) return <p className="text-red-400">Failed to load correlations.</p>;
  if (isLoading || !data) return <p className="text-gray-400">Computing correlations…</p>;

  const { netuids, names, matrix, threshold } = data;
  const display = netuids.slice(0, maxSubnets);

  function getCorr(a: number, b: number): number | null {
    const key1 = `${a},${b}`;
    const key2 = `${b},${a}`;
    if (key1 in matrix) return matrix[key1];
    if (key2 in matrix) return matrix[key2];
    return null;
  }

  // Count high-correlation pairs
  const highPairs = Object.values(matrix).filter((r) => r >= threshold).length;

  return (
    <div>
      <h1 className="text-2xl font-bold mb-2">Correlation Matrix</h1>
      <p className="text-sm text-gray-400 mb-1">
        Pearson correlation of 7-day price series. Top {display.length} subnets by TVL.
      </p>
      <div className="flex items-center gap-2 mb-4 text-xs text-gray-500">
        <span className="bg-red-800 text-white px-2 py-0.5 rounded">r ≥ {threshold}</span>
        <span>= bot automatically skips entry (correlated with open position)</span>
        <span className="ml-2 text-gray-600">· {highPairs} pairs above threshold today</span>
      </div>

      <div className="flex items-center gap-4 mb-4">
        <label className="flex items-center gap-2 text-sm text-gray-400 cursor-pointer">
          <input
            type="checkbox"
            checked={highlightOnly}
            onChange={(e) => setHighlightOnly(e.target.checked)}
            className="accent-indigo-500"
          />
          Highlight cells ≥ {threshold} only
        </label>
        <div className="flex items-center gap-2">
          <label className="text-sm text-gray-400">Show top</label>
          <select
            value={maxSubnets}
            onChange={(e) => setMaxSubnets(Number(e.target.value))}
            className="bg-gray-800 border border-gray-700 rounded px-2 py-1 text-sm"
          >
            {[15, 20, 30, 40].map((n) => (
              <option key={n} value={n}>{n}</option>
            ))}
          </select>
        </div>
      </div>

      {/* Legend */}
      <div className="flex items-center gap-2 mb-4 text-[10px]">
        <span className="bg-blue-800/70 text-blue-100 px-1.5 py-0.5 rounded">-1.0</span>
        <span className="bg-blue-950/40 text-blue-400 px-1.5 py-0.5 rounded">-0.3</span>
        <span className="bg-gray-900 text-gray-500 px-1.5 py-0.5 rounded border border-gray-700">0</span>
        <span className="bg-orange-900/40 text-orange-300 px-1.5 py-0.5 rounded">+0.5</span>
        <span className="bg-orange-800/70 text-orange-100 px-1.5 py-0.5 rounded">+0.7</span>
        <span className="bg-red-800 text-white px-1.5 py-0.5 rounded">≥{threshold}</span>
      </div>

      <div className="overflow-x-auto">
        <table className="border-collapse text-[10px]">
          <thead>
            <tr>
              <th className="w-16 sticky left-0 bg-black z-10"></th>
              {display.map((netuid) => (
                <th
                  key={netuid}
                  className="p-0.5 text-gray-400 text-center font-mono"
                  style={{ minWidth: "38px", maxWidth: "50px" }}
                >
                  <div className="truncate" title={names[String(netuid)] || ""}>
                    {netuid}
                  </div>
                  <div className="text-gray-600 text-[8px] truncate max-w-[38px]">
                    {(names[String(netuid)] || "").slice(0, 6)}
                  </div>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {display.map((rowNetuid) => (
              <tr key={rowNetuid}>
                <td className="sticky left-0 bg-black z-10 pr-2 py-0.5 text-gray-400 font-mono text-right">
                  <div>{rowNetuid}</div>
                  <div className="text-gray-600 text-[8px] truncate max-w-[60px]">
                    {(names[String(rowNetuid)] || "").slice(0, 6)}
                  </div>
                </td>
                {display.map((colNetuid) => {
                  if (rowNetuid === colNetuid) {
                    return (
                      <td key={colNetuid} className="p-0.5 text-center">
                        <div className="w-8 h-6 bg-gray-800 rounded flex items-center justify-center text-gray-600">
                          1.0
                        </div>
                      </td>
                    );
                  }
                  const r = getCorr(rowNetuid, colNetuid);
                  if (r === null) {
                    return (
                      <td key={colNetuid} className="p-0.5 text-center">
                        <div className="w-8 h-6 bg-gray-900 rounded flex items-center justify-center text-gray-700">
                          —
                        </div>
                      </td>
                    );
                  }
                  const isHigh = r >= threshold;
                  const showCell = !highlightOnly || isHigh;
                  return (
                    <td key={colNetuid} className="p-0.5 text-center">
                      <div
                        className={`w-8 h-6 rounded flex items-center justify-center font-mono ${
                          showCell ? cellBg(r, threshold) : "bg-gray-900 text-gray-700"
                        } ${isHigh ? "ring-1 ring-red-500" : ""}`}
                        title={`netuid ${rowNetuid} ↔ ${colNetuid}: r=${r.toFixed(3)}`}
                      >
                        {showCell ? r.toFixed(2) : ""}
                      </div>
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <p className="text-xs text-gray-600 mt-4">
        Data source: 7-day price series from pool snapshot. Refreshed on page load.
      </p>
    </div>
  );
}
