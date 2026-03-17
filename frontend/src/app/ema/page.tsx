"use client";

import { useState, useEffect, useRef } from "react";
import useSWR from "swr";

const API = process.env.NEXT_PUBLIC_API_URL || (typeof window !== "undefined" ? `http://${window.location.hostname}:8080` : "http://localhost:8080");
const fetcher = (url: string) => fetch(url).then((r) => r.json());

interface EmaSignal {
  netuid: number;
  name: string;
  price: number;
  ema: number;
  fast_ema: number;
  signal: "BUY" | "SELL" | "HOLD";
  bars: number;
}

interface SlippageStats {
  trade_count: number;
  avg_entry_slippage_pct?: number;
  avg_exit_slippage_pct?: number;
  avg_round_trip_pct?: number;
  total_slippage_tao?: number;
  best_trade?: { slippage_pct: number; netuid: number };
  worst_trade?: { slippage_pct: number; netuid: number };
}

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

interface PortfolioData {
  enabled: boolean;
  pot_tao: number;
  deployed_tao: number;
  unstaked_tao: number;
  wallet_balance: number | null;
  open_count: number;
  max_positions: number;
  open_positions: OpenPosition[];
  ema_period: number;
  confirm_bars: number;
  dry_run: boolean;
  signal_timeframe_hours?: number;
  stop_loss_pct?: number;
  take_profit_pct?: number;
  trailing_stop_pct?: number;
  exit_watcher?: {
    enabled: boolean;
    interval_sec: number;
    last_run: string | null;
    last_error: string | null;
    last_exit_count: number;
  };
}

interface EmaPosition {
  id: number;
  netuid: number;
  name: string;
  status: string;
  entry_ts: string;
  exit_ts: string | null;
  entry_price: number;
  exit_price: number | null;
  amount_tao: number;
  amount_tao_out: number | null;
  pnl_tao: number | null;
  pnl_pct: number | null;
  exit_reason: string | null;
}

interface SpotData {
  netuid: number;
  price: number | null;
  available: boolean;
  source: string;
  timestamp: string;
}

type TradeMood = "profit" | "loss";

interface ExitEvent {
  id: string;
  positionId: number;
  netuid: number;
  name: string;
  reason: string | null;
  pnlPct: number | null;
  pnlTao: number | null;
  exitTs: string | null;
  mood: TradeMood;
}

interface ExitAnimation {
  id: string;
  position: OpenPosition;
  event: ExitEvent;
}

function fmt(n: number | null | undefined, decimals = 4) {
  if (n == null) return "—";
  return n.toFixed(decimals);
}

function fmtUsd(n: number | null | undefined, decimals = 2) {
  if (n == null) return "—";
  if (n < 0.01) return `$${n.toFixed(4)}`;
  return `$${n.toFixed(decimals)}`;
}

function fmtPrice(
  n: number | null | undefined,
  showUsd: boolean,
  taoUsd: number | null,
  decimals = 6
): string {
  if (n == null) return "—";
  if (showUsd && taoUsd) {
    const usd = n * taoUsd;
    if (usd < 0.0001) return `$${usd.toFixed(8)}`;
    if (usd < 0.01) return `$${usd.toFixed(6)}`;
    if (usd < 1) return `$${usd.toFixed(4)}`;
    return `$${usd.toFixed(2)}`;
  }
  return `${n.toFixed(decimals)} τ`;
}

function fmtTao(
  n: number | null | undefined,
  showUsd: boolean,
  taoUsd: number | null,
  decimals = 4
): string {
  if (n == null) return "—";
  if (showUsd && taoUsd) return fmtUsd(n * taoUsd);
  return `${n.toFixed(decimals)} τ`;
}

function fmtPnl(
  n: number,
  showUsd: boolean,
  taoUsd: number | null,
  decimals = 4
): string {
  const sign = n >= 0 ? "+" : "-";
  if (showUsd && taoUsd) return `${sign}${fmtUsd(Math.abs(n) * taoUsd)}`;
  return `${sign}${Math.abs(n).toFixed(decimals)} τ`;
}

function fmtDate(iso: string | null) {
  if (!iso) return "—";
  return new Date(iso).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function fmtUtcDate(d: Date) {
  return d.toLocaleString(undefined, {
    timeZone: "UTC",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }) + " UTC";
}

function computeEma(prices: number[], period: number) {
  if (!prices.length) return [];
  const k = 2 / (period + 1);
  const ema = [prices[0]];
  for (let i = 1; i < prices.length; i++) {
    ema.push(prices[i] * k + ema[ema.length - 1] * (1 - k));
  }
  return ema;
}

function getNextBarClose(nowMs: number, timeframeHours: number) {
  const now = new Date(nowMs);
  const close = new Date(nowMs);
  close.setUTCMinutes(0, 0, 0);
  const currentHour = close.getUTCHours();
  const nextHour = Math.ceil((currentHour + 1) / timeframeHours) * timeframeHours;
  if (nextHour >= 24) {
    close.setUTCDate(close.getUTCDate() + 1);
    close.setUTCHours(nextHour % 24, 0, 0, 0);
  } else {
    close.setUTCHours(nextHour, 0, 0, 0);
  }
  return close;
}

function fmtCountdown(target: Date, nowMs: number) {
  const diff = Math.max(0, target.getTime() - nowMs);
  const totalSeconds = Math.floor(diff / 1000);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  return `${hours.toString().padStart(2, "0")}h ${minutes
    .toString()
    .padStart(2, "0")}m ${seconds.toString().padStart(2, "0")}s`;
}

function getTradeMood(pnlPct: number | null | undefined): TradeMood {
  return (pnlPct ?? 0) >= 0 ? "profit" : "loss";
}

function reasonLabel(reason: string | null | undefined) {
  switch (reason) {
    case "TAKE_PROFIT":
      return "Take profit";
    case "STOP_LOSS":
      return "Stop loss";
    case "TRAILING_STOP":
      return "Trailing stop";
    case "EMA_CROSS":
      return "EMA cross";
    case "MANUAL_CLOSE":
      return "Manual close";
    case "TIME_STOP":
      return "Time stop";
    default:
      return reason || "Exit";
  }
}

function SignalBadge({ signal }: { signal: string }) {
  const cls =
    signal === "BUY"
      ? "bg-emerald-900 text-emerald-300 border border-emerald-700"
      : signal === "SELL"
      ? "bg-red-900 text-red-300 border border-red-700"
      : "bg-gray-800 text-gray-400 border border-gray-700";
  return (
    <span className={`px-2 py-0.5 rounded text-xs font-bold ${cls}`}>
      {signal}
    </span>
  );
}

function StatCard({
  label,
  value,
  color = "text-white",
  sub,
}: {
  label: string;
  value: string;
  color?: string;
  sub?: string;
}) {
  return (
    <div className="bg-gray-900 rounded-lg border border-gray-800 p-4">
      <p className="text-xs text-gray-400 mb-1">{label}</p>
      <p className={`text-xl font-bold ${color}`}>{value}</p>
      {sub && <p className="text-xs text-gray-600 mt-0.5">{sub}</p>}
    </div>
  );
}

// ── Candlestick + EMA chart for position cards ───────────────

function CandleChart({
  netuid,
  entryPrice,
  emaPeriod,
}: {
  netuid: number;
  entryPrice: number;
  emaPeriod: number;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<ReturnType<typeof import("lightweight-charts").createChart> | null>(null);
  const { data: historyData } = useSWR(
    `${API}/api/subnets/${netuid}/history`,
    fetcher,
    { refreshInterval: 60000, revalidateOnFocus: false }
  );
  const { data: spotData } = useSWR<SpotData>(
    `${API}/api/subnets/${netuid}/spot`,
    fetcher,
    { refreshInterval: 60000, revalidateOnFocus: false }
  );

  useEffect(() => {
    if (!containerRef.current || !historyData?.history?.length) return;

    let chart = chartRef.current;
    let resizeObserver: ResizeObserver | null = null;
    let disposed = false;

    // Lazy import lightweight-charts (client-side only)
    import("lightweight-charts").then(({ createChart, ColorType, CrosshairMode, LineStyle, CandlestickSeries, LineSeries }) => {
      if (!containerRef.current || disposed) return;

      // Clean up old chart
      if (chart) {
        chart.remove();
        chartRef.current = null;
      }

      const raw: { t: string; p: number }[] = historyData.history;
      if (raw.length < 3) return;

      // Build OHLC candles from close prices (4h bars) — use UTC epoch seconds
      const candles: { time: number; open: number; high: number; low: number; close: number }[] = [];
      for (let i = 0; i < raw.length; i++) {
        const close = raw[i].p;
        const open = i > 0 ? raw[i - 1].p : close;
        const spread = Math.abs(close - open) * 0.3 + close * 0.001;
        const high = Math.max(open, close) + spread * 0.5;
        const low = Math.min(open, close) - spread * 0.5;
        const epoch = Math.floor(new Date(raw[i].t).getTime() / 1000);
        candles.push({ time: epoch, open, high, low, close });
      }

      // Compute EMA line
      const emaLine: { time: number; value: number }[] = [];
      const k = 2 / (emaPeriod + 1);
      let ema = candles[0].close;
      for (let i = 0; i < candles.length; i++) {
        ema = candles[i].close * k + ema * (1 - k);
        emaLine.push({ time: candles[i].time, value: ema });
      }

      chart = createChart(containerRef.current, {
        width: containerRef.current.clientWidth,
        height: 180,
        layout: {
          background: { type: ColorType.Solid, color: "transparent" },
          textColor: "#9CA3AF",
          fontSize: 10,
        },
        grid: {
          vertLines: { color: "rgba(55, 65, 81, 0.3)" },
          horzLines: { color: "rgba(55, 65, 81, 0.3)" },
        },
        crosshair: { mode: CrosshairMode.Magnet },
        rightPriceScale: {
          borderColor: "#374151",
          scaleMargins: { top: 0.1, bottom: 0.1 },
        },
        timeScale: {
          borderColor: "#374151",
          timeVisible: true,
          secondsVisible: false,
        },
      });
      chartRef.current = chart;

      // Candlestick series (v5 API)
      const candleSeries = chart.addSeries(CandlestickSeries, {
        upColor: "#34D399",
        downColor: "#F87171",
        borderUpColor: "#34D399",
        borderDownColor: "#F87171",
        wickUpColor: "#34D399",
        wickDownColor: "#F87171",
      });
      candleSeries.setData(candles as never[]);

      // EMA line overlay
      const emaSeries = chart.addSeries(LineSeries, {
        color: "#FBBF24",
        lineWidth: 2,
        crosshairMarkerVisible: false,
        priceLineVisible: false,
      });
      emaSeries.setData(emaLine as never[]);

      // Entry price line
      candleSeries.createPriceLine({
        price: entryPrice,
        color: "#818CF8",
        lineWidth: 1,
        lineStyle: LineStyle.Dashed,
        axisLabelVisible: true,
        title: "entry",
      });

      if (spotData?.available && spotData.price && spotData.price > 0) {
        const lastCandle = candles[candles.length - 1];
        let liveTime = Math.floor(new Date(spotData.timestamp).getTime() / 1000);
        if (!Number.isFinite(liveTime) || liveTime <= lastCandle.time) {
          liveTime = lastCandle.time + 60;
        }

        const liveSeries = chart.addSeries(LineSeries, {
          color: "#38BDF8",
          lineWidth: 2,
          lineStyle: LineStyle.Dashed,
          crosshairMarkerVisible: true,
          priceLineVisible: false,
          lastValueVisible: false,
        });
        liveSeries.setData([
          { time: lastCandle.time, value: lastCandle.close },
          { time: liveTime, value: spotData.price },
        ] as never[]);

        candleSeries.createPriceLine({
          price: spotData.price,
          color: "#38BDF8",
          lineWidth: 1,
          lineStyle: LineStyle.Dashed,
          axisLabelVisible: true,
          title: "live",
        });
      }

      chart.timeScale().fitContent();

      // Resize observer
      resizeObserver = new ResizeObserver(() => {
        if (containerRef.current && chart) {
          chart.applyOptions({ width: containerRef.current.clientWidth });
        }
      });
      resizeObserver.observe(containerRef.current);
    });

    return () => {
      disposed = true;
      if (resizeObserver) {
        resizeObserver.disconnect();
        resizeObserver = null;
      }
      if (chartRef.current) {
        chartRef.current.remove();
        chartRef.current = null;
      }
    };
  }, [historyData, entryPrice, emaPeriod, spotData]);

  if (!historyData?.history?.length) {
    return (
      <div className="h-[180px] flex items-center justify-center text-gray-700 text-xs">
        no chart data
      </div>
    );
  }

  return (
    <div>
      <div ref={containerRef} className="h-[180px] w-full" />
      <div className="mt-1 px-1 text-[11px] text-gray-600">
        {spotData?.available
          ? `4h candles + EMA · 1m live ${spotData.source} spot overlay`
          : "4h candles + EMA"}
      </div>
    </div>
  );
}

function PositionInsight({
  netuid,
  entryPrice,
  peakPrice,
  fallbackCurrentPrice,
  emaPeriod,
  confirmBars,
  stopLossPct,
  takeProfitPct,
  trailingStopPct,
  showUsd,
  taoUsd,
}: {
  netuid: number;
  entryPrice: number;
  peakPrice: number;
  fallbackCurrentPrice: number;
  emaPeriod: number;
  confirmBars: number;
  stopLossPct: number;
  takeProfitPct: number;
  trailingStopPct: number;
  showUsd: boolean;
  taoUsd: number | null;
}) {
  type ConfirmationBar = {
    time: string;
    price: number;
    ema: number;
    above: boolean;
  };

  const { data: historyData } = useSWR(
    `${API}/api/subnets/${netuid}/history`,
    fetcher,
    { refreshInterval: 60000, revalidateOnFocus: false }
  );
  const { data: spotData } = useSWR<SpotData>(
    `${API}/api/subnets/${netuid}/spot`,
    fetcher,
    { refreshInterval: 60000, revalidateOnFocus: false }
  );

  const prices: number[] = Array.isArray(historyData?.history)
    ? historyData.history.map((point: { t: string; p: number }) => point.p)
    : [];
  const emaValues = computeEma(prices, emaPeriod);
  const livePrice =
    spotData?.available && typeof spotData.price === "number"
      ? spotData.price
      : fallbackCurrentPrice;
  const currentEma = emaValues.length ? emaValues[emaValues.length - 1] : null;
  const recentBars: ConfirmationBar[] =
    prices.length >= confirmBars && emaValues.length >= confirmBars && historyData?.history?.length >= confirmBars
      ? historyData.history.slice(-confirmBars).map((point: { t: string; p: number }, index: number) => {
          const ema = emaValues[emaValues.length - confirmBars + index];
          return {
            time: point.t,
            price: point.p,
            ema,
            above: point.p > ema,
          };
        })
      : [];

  const stopLossPrice = entryPrice * (1 - stopLossPct / 100);
  const takeProfitPrice = entryPrice * (1 + takeProfitPct / 100);
  const trailingStopPrice = peakPrice > entryPrice ? peakPrice * (1 - trailingStopPct / 100) : null;
  const levels = [
    { label: spotData?.available ? "Live" : "Current", value: livePrice, color: "text-sky-400" },
    { label: `EMA(${emaPeriod})`, value: currentEma, color: "text-amber-400" },
    { label: "Entry", value: entryPrice, color: "text-indigo-400" },
    { label: `SL -${stopLossPct}%`, value: stopLossPrice, color: "text-red-400" },
    { label: `TP +${takeProfitPct}%`, value: takeProfitPrice, color: "text-emerald-400" },
  ];
  if (trailingStopPrice) {
    levels.push({
      label: `Trail ${trailingStopPct}%`,
      value: trailingStopPrice,
      color: "text-orange-400",
    });
  }

  return (
    <div className="mt-3 space-y-3">
      <div>
        <div className="flex items-center justify-between mb-1">
          <span className="text-[11px] font-semibold uppercase tracking-wide text-gray-500">
            {confirmBars}-Bar Confirmation
          </span>
          <span className="text-[11px] text-gray-600">
            {recentBars.length
              ? `${recentBars.filter((bar) => bar.above).length}/${recentBars.length} above EMA`
              : "waiting for history"}
          </span>
        </div>
        <div className="grid grid-cols-3 gap-2">
          {recentBars.length ? (
            recentBars.map((bar, index) => (
              <div
                key={`${bar.time}-${index}`}
                className={`rounded-md border px-2 py-1.5 text-[11px] ${
                  bar.above
                    ? "border-emerald-800 bg-emerald-950/40 text-emerald-300"
                    : "border-red-800 bg-red-950/40 text-red-300"
                }`}
              >
                <div className="font-semibold">{bar.above ? "Above" : "Below"}</div>
                <div className="text-[10px] opacity-75">
                  {new Date(bar.time).toLocaleString(undefined, {
                    month: "short",
                    day: "numeric",
                    hour: "2-digit",
                    minute: "2-digit",
                  })}
                </div>
              </div>
            ))
          ) : (
            <div className="col-span-3 rounded-md border border-gray-800 bg-gray-950/40 px-2 py-2 text-[11px] text-gray-600">
              Not enough completed 4h bars yet to show the confirmation strip.
            </div>
          )}
        </div>
      </div>

      <div>
        <div className="flex items-center justify-between mb-1">
          <span className="text-[11px] font-semibold uppercase tracking-wide text-gray-500">
            Risk Ladder
          </span>
          <span className="text-[11px] text-gray-600">
            spot source: {spotData?.available ? spotData.source : "portfolio snapshot"}
          </span>
        </div>
        <div className="grid grid-cols-2 md:grid-cols-3 gap-2">
          {levels.map((level) => {
            const distancePct =
              livePrice > 0 && level.value != null
                ? ((level.value - livePrice) / livePrice) * 100
                : null;
            return (
              <div
                key={level.label}
                className="rounded-md border border-gray-800 bg-gray-950/40 px-2 py-2"
              >
                <div className="text-[11px] text-gray-500">{level.label}</div>
                <div className={`text-sm font-semibold ${level.color}`}>
                  {fmtPrice(level.value, showUsd, taoUsd)}
                </div>
                <div
                  className={`text-[11px] ${
                    distancePct == null
                      ? "text-gray-600"
                      : distancePct >= 0
                      ? "text-emerald-400"
                      : "text-red-400"
                  }`}
                >
                  {distancePct == null
                    ? "—"
                    : `${distancePct >= 0 ? "+" : ""}${distancePct.toFixed(2)}% vs live`}
                </div>
              </div>
            );
          })}
        </div>
        <div className="mt-1 text-[11px] text-gray-600">
          {trailingStopPrice
            ? `Trailing stop is armed from the current peak ${fmtPrice(peakPrice, showUsd, taoUsd)}.`
            : "Trailing stop arms after price moves above entry and sets a higher peak."}
        </div>
      </div>
    </div>
  );
}

// ── Signal summary bar ────────────────────────────────────────

function SignalBar({ buy, sell, hold }: { buy: number; sell: number; hold: number }) {
  const total = buy + sell + hold;
  if (total === 0) return null;
  const buyPct = (buy / total) * 100;
  const sellPct = (sell / total) * 100;
  const holdPct = (hold / total) * 100;

  return (
    <div className="mb-6">
      <div className="flex items-center gap-4 mb-2">
        <h2 className="text-lg font-semibold text-indigo-300">EMA Signals</h2>
        <span className="text-xs text-gray-500">{total} subnets scanned</span>
      </div>
      <div className="flex gap-4 mb-2">
        <span className="flex items-center gap-1.5 text-sm">
          <span className="w-3 h-3 rounded-sm bg-emerald-500" />
          <span className="text-emerald-400 font-semibold">{buy}</span>
          <span className="text-gray-500">BUY</span>
        </span>
        <span className="flex items-center gap-1.5 text-sm">
          <span className="w-3 h-3 rounded-sm bg-red-500" />
          <span className="text-red-400 font-semibold">{sell}</span>
          <span className="text-gray-500">SELL</span>
        </span>
        <span className="flex items-center gap-1.5 text-sm">
          <span className="w-3 h-3 rounded-sm bg-gray-600" />
          <span className="text-gray-400 font-semibold">{hold}</span>
          <span className="text-gray-500">HOLD</span>
        </span>
      </div>
      <div className="flex h-3 rounded-full overflow-hidden bg-gray-800">
        {buyPct > 0 && (
          <div
            className="bg-emerald-500 transition-all duration-500"
            style={{ width: `${buyPct}%` }}
            title={`${buy} BUY (${buyPct.toFixed(0)}%)`}
          />
        )}
        {holdPct > 0 && (
          <div
            className="bg-gray-600 transition-all duration-500"
            style={{ width: `${holdPct}%` }}
            title={`${hold} HOLD (${holdPct.toFixed(0)}%)`}
          />
        )}
        {sellPct > 0 && (
          <div
            className="bg-red-500 transition-all duration-500"
            style={{ width: `${sellPct}%` }}
            title={`${sell} SELL (${sellPct.toFixed(0)}%)`}
          />
        )}
      </div>
    </div>
  );
}

function ExitAnimationTile({
  animation,
  showUsd,
  taoUsd,
}: {
  animation: ExitAnimation;
  showUsd: boolean;
  taoUsd: number | null;
}) {
  const { position, event } = animation;
  const moodClass =
    event.mood === "profit"
      ? "ema-exit-card-profit border-emerald-700/60 bg-emerald-950/40"
      : "ema-exit-card-loss border-red-700/60 bg-red-950/30";
  const accentClass = event.mood === "profit" ? "text-emerald-300" : "text-red-300";
  const badgeClass =
    event.mood === "profit"
      ? "bg-emerald-400/10 text-emerald-300 border border-emerald-500/20"
      : "bg-red-400/10 text-red-300 border border-red-500/20";

  return (
    <div className={`ema-exit-card relative overflow-hidden rounded-lg border p-4 ${moodClass}`}>
      <div
        className={`pointer-events-none absolute inset-0 ${
          event.mood === "profit"
            ? "bg-[radial-gradient(circle_at_top,_rgba(52,211,153,0.18),_transparent_55%)]"
            : "bg-[radial-gradient(circle_at_top,_rgba(248,113,113,0.18),_transparent_55%)]"
        }`}
      />
      <div className="relative">
        <div className="flex items-start justify-between gap-3">
          <div>
            <div className="font-semibold text-white">
              {event.name || `SN${event.netuid}`}
              <span className="ml-2 text-sm text-gray-500">#{event.netuid}</span>
            </div>
            <div className="mt-1 text-xs text-gray-400">
              {reasonLabel(event.reason)} · {fmtDate(event.exitTs)}
            </div>
          </div>
          <span className={`rounded-full px-2 py-1 text-[11px] font-semibold ${badgeClass}`}>
            {event.mood === "profit" ? "Banked" : "Cut"}
          </span>
        </div>
        <div className="mt-4 grid grid-cols-2 gap-3 text-sm">
          <div>
            <div className="text-xs uppercase tracking-wide text-gray-500">Realized</div>
            <div className={`text-lg font-bold ${accentClass}`}>
              {event.pnlPct != null ? `${event.pnlPct >= 0 ? "+" : ""}${event.pnlPct.toFixed(2)}%` : "—"}
            </div>
            {event.pnlTao != null && (
              <div className={`text-xs ${accentClass}`}>{fmtPnl(event.pnlTao, showUsd, taoUsd, 3)}</div>
            )}
          </div>
          <div>
            <div className="text-xs uppercase tracking-wide text-gray-500">Entry Snapshot</div>
            <div className="text-sm text-white">{fmtPrice(position.entry_price, showUsd, taoUsd)}</div>
            <div className="text-xs text-gray-500">
              peak {fmtPrice(position.peak_price, showUsd, taoUsd)}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

function EventFeed({
  events,
  showUsd,
  taoUsd,
}: {
  events: ExitEvent[];
  showUsd: boolean;
  taoUsd: number | null;
}) {
  return (
    <div className="bg-gray-900 rounded-lg border border-gray-800 p-4">
      <div className="flex items-center justify-between mb-3">
        <h2 className="text-sm font-semibold text-gray-300">Recent Events</h2>
        <span className="text-[11px] text-gray-600">latest exits</span>
      </div>
      {events.length ? (
        <div className="space-y-2">
          {events.map((event) => {
            const badgeClass =
              event.mood === "profit"
                ? "bg-emerald-400/10 text-emerald-300"
                : "bg-red-400/10 text-red-300";
            return (
              <div
                key={event.id}
                className="flex items-center justify-between gap-3 rounded-md border border-gray-800 bg-gray-950/50 px-3 py-2"
              >
                <div>
                  <div className="text-sm text-white">
                    {event.name || `SN${event.netuid}`}
                    <span className="ml-2 text-xs text-gray-500">#{event.netuid}</span>
                  </div>
                  <div className="text-xs text-gray-500">
                    {reasonLabel(event.reason)} · {fmtDate(event.exitTs)}
                  </div>
                </div>
                <div className="text-right">
                  <div className={`rounded-full px-2 py-1 text-[11px] font-semibold ${badgeClass}`}>
                    {event.mood === "profit" ? "Profit" : "Loss"}
                  </div>
                  <div className={`mt-1 text-xs ${event.mood === "profit" ? "text-emerald-400" : "text-red-400"}`}>
                    {event.pnlTao != null ? fmtPnl(event.pnlTao, showUsd, taoUsd, 3) : "—"}
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      ) : (
        <div className="rounded-md border border-gray-800 bg-gray-950/40 px-3 py-4 text-sm text-gray-600">
          No exit events captured in this session yet.
        </div>
      )}
    </div>
  );
}

function ToastStack({
  events,
  showUsd,
  taoUsd,
}: {
  events: ExitEvent[];
  showUsd: boolean;
  taoUsd: number | null;
}) {
  if (!events.length) return null;
  return (
    <div className="pointer-events-none fixed right-4 top-20 z-50 flex w-[min(92vw,22rem)] flex-col gap-3">
      {events.map((event) => (
        <div
          key={event.id}
          className={`ema-toast rounded-xl border px-4 py-3 shadow-2xl backdrop-blur ${
            event.mood === "profit"
              ? "border-emerald-500/30 bg-emerald-950/80"
              : "border-red-500/30 bg-red-950/80"
          }`}
        >
          <div className="flex items-center justify-between gap-3">
            <div>
              <div className="text-sm font-semibold text-white">
                {event.name || `SN${event.netuid}`}
              </div>
              <div className="text-[11px] text-gray-300">
                {reasonLabel(event.reason)} · {fmtDate(event.exitTs)}
              </div>
            </div>
            <div className={`text-right ${event.mood === "profit" ? "text-emerald-300" : "text-red-300"}`}>
              <div className="text-sm font-bold">
                {event.pnlPct != null ? `${event.pnlPct >= 0 ? "+" : ""}${event.pnlPct.toFixed(2)}%` : "—"}
              </div>
              {event.pnlTao != null && (
                <div className="text-[11px]">{fmtPnl(event.pnlTao, showUsd, taoUsd, 3)}</div>
              )}
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

// ── Main page ─────────────────────────────────────────────────

export default function EmaPage() {
  const { data: portData, mutate: mutatePort } = useSWR<PortfolioData>(
    `${API}/api/ema/portfolio`,
    fetcher,
    { refreshInterval: 15000 }
  );
  const { data: posData, mutate: mutatePos } = useSWR<{ positions: EmaPosition[] }>(
    `${API}/api/ema/positions`,
    fetcher,
    { refreshInterval: 15000 }
  );
  const { data: sigData } = useSWR<{ signals: EmaSignal[]; ema_period?: number; fast_ema_period?: number }>(
    `${API}/api/ema/signals`,
    fetcher,
    { refreshInterval: 15000 }
  );
  const { data: slipStats } = useSWR<SlippageStats>(
    `${API}/api/ema/slippage-stats`,
    fetcher,
    { refreshInterval: 30000 }
  );
  const [closingId, setClosingId] = useState<number | null>(null);
  const [showUsd, setShowUsd] = useState(true);
  const [clockMs, setClockMs] = useState(() => Date.now());
  const [recentEvents, setRecentEvents] = useState<ExitEvent[]>([]);
  const [toastEvents, setToastEvents] = useState<ExitEvent[]>([]);
  const [exitAnimations, setExitAnimations] = useState<ExitAnimation[]>([]);
  const seenClosedIdsRef = useRef<Set<number>>(new Set());
  const prevOpenMapRef = useRef<Map<number, OpenPosition>>(new Map());
  const exitEventsHydratedRef = useRef(false);
  const timeoutIdsRef = useRef<number[]>([]);
  const { data: taoUsdData } = useSWR<{ usd: number }>(
    showUsd ? `${API}/api/price/tao-usd` : null,
    fetcher,
    { refreshInterval: 120000 }
  );
  const taoUsd = taoUsdData?.usd ?? null;

  useEffect(() => {
    const timer = window.setInterval(() => setClockMs(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    return () => {
      timeoutIdsRef.current.forEach((id) => window.clearTimeout(id));
      timeoutIdsRef.current = [];
    };
  }, []);

  useEffect(() => {
    const currentOpenMap = new Map<number, OpenPosition>(
      (portData?.open_positions ?? []).map((pos) => [pos.position_id, pos])
    );
    const closedPositions = (posData?.positions ?? []).filter((pos) => pos.status === "CLOSED");

    if (!exitEventsHydratedRef.current) {
      seenClosedIdsRef.current = new Set(closedPositions.map((pos) => pos.id));
      prevOpenMapRef.current = currentOpenMap;
      if (posData) {
        exitEventsHydratedRef.current = true;
      }
      return;
    }

    const newEvents = closedPositions
      .filter((pos) => !seenClosedIdsRef.current.has(pos.id))
      .map((pos) => {
        const mood = getTradeMood(pos.pnl_pct);
        const priorOpen = prevOpenMapRef.current.get(pos.id);
        const event: ExitEvent = {
          id: `${pos.id}-${pos.exit_ts ?? Date.now()}`,
          positionId: pos.id,
          netuid: pos.netuid,
          name: pos.name || priorOpen?.name || `SN${pos.netuid}`,
          reason: pos.exit_reason,
          pnlPct: pos.pnl_pct,
          pnlTao: pos.pnl_tao,
          exitTs: pos.exit_ts,
          mood,
        };
        return { event, priorOpen };
      });

    if (newEvents.length) {
      setRecentEvents((prev) => [...newEvents.map((item) => item.event), ...prev].slice(0, 8));
      setToastEvents((prev) => [...newEvents.map((item) => item.event), ...prev].slice(0, 4));

      for (const { event, priorOpen } of newEvents) {
        const toastTimer = window.setTimeout(() => {
          setToastEvents((prev) => prev.filter((item) => item.id !== event.id));
        }, 5200);
        timeoutIdsRef.current.push(toastTimer);

        if (priorOpen) {
          const animation: ExitAnimation = {
            id: `anim-${event.id}`,
            position: priorOpen,
            event,
          };
          setExitAnimations((prev) => [animation, ...prev].slice(0, 6));
          const animationTimer = window.setTimeout(() => {
            setExitAnimations((prev) => prev.filter((item) => item.id !== animation.id));
          }, 1900);
          timeoutIdsRef.current.push(animationTimer);
        }
      }
    }

    seenClosedIdsRef.current = new Set(closedPositions.map((pos) => pos.id));
    prevOpenMapRef.current = currentOpenMap;
  }, [portData?.open_positions, posData]);

  async function closePosition(positionId: number) {
    if (!confirm("Close this EMA position at market price?")) return;
    setClosingId(positionId);
    try {
      const res = await fetch(`${API}/api/ema/positions/${positionId}/close`, { method: "POST" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      mutatePort();
      mutatePos();
    } catch {
      alert("Failed to close position");
    } finally {
      setClosingId(null);
    }
  }

  const closed = (posData?.positions ?? []).filter((p) => p.status === "CLOSED");
  const realizedPnl = closed.reduce((s, p) => s + (p.pnl_tao ?? 0), 0);
  const wins = closed.filter((p) => (p.pnl_pct ?? 0) > 0).length;
  const unrealizedPnl = (portData?.open_positions ?? []).reduce((s, p) => {
    return s + p.amount_tao * (p.pnl_pct / 100);
  }, 0);
  const alphaMarkedValueTao = (portData?.open_positions ?? []).reduce((s, p) => {
    return s + (p.amount_alpha ?? 0) * p.current_price;
  }, 0);
  const totalPnl = realizedPnl + unrealizedPnl;

  if (!portData?.enabled && portData !== undefined) {
    return (
      <div className="text-gray-400 text-sm">
        EMA strategy is disabled. Set <code>EMA_ENABLED=true</code> in .env to enable it.
      </div>
    );
  }

  const port = portData;
  const signals = sigData?.signals ?? [];
  const buySignals = signals.filter((s) => s.signal === "BUY").length;
  const sellSignals = signals.filter((s) => s.signal === "SELL").length;
  const holdSignals = signals.length - buySignals - sellSignals;

  const isLive = port ? !port.dry_run : false;
  const signalTimeframeHours = port?.signal_timeframe_hours ?? 4;
  const exitWatcher = port?.exit_watcher ?? {
    enabled: false,
    interval_sec: 15,
    last_run: null,
    last_error: null,
    last_exit_count: 0,
  };
  const nextBarClose = getNextBarClose(clockMs, signalTimeframeHours);
  const nextBarCountdown = fmtCountdown(nextBarClose, clockMs);
  const positionCards = [
    ...(port?.open_positions ?? []).map((pos) => ({ kind: "active" as const, pos })),
    ...exitAnimations.map((animation) => ({ kind: "exit" as const, animation })),
  ].sort((a, b) => {
    const aTs = a.kind === "active" ? a.pos.entry_ts : a.animation.position.entry_ts;
    const bTs = b.kind === "active" ? b.pos.entry_ts : b.animation.position.entry_ts;
    return new Date(bTs).getTime() - new Date(aTs).getTime();
  });

  return (
    <div>
      <ToastStack events={toastEvents} showUsd={showUsd} taoUsd={taoUsd} />
      <div className="flex items-center gap-4 mb-2">
        <h1 className="text-2xl font-bold">EMA Trend Strategy</h1>
        <span className="text-sm text-gray-400">
          {port ? `EMA(${port.ema_period}) · ${port.confirm_bars}-bar confirm` : ""}
        </span>
        <div className="ml-auto flex items-center gap-2">
          <span className={`text-xs font-semibold ${!showUsd ? "text-white" : "text-gray-500"}`}>τ TAO</span>
          <button
            onClick={() => setShowUsd((v) => !v)}
            className={`relative inline-flex h-5 w-10 items-center rounded-full transition-colors ${showUsd ? "bg-emerald-600" : "bg-gray-700"}`}
            aria-label="Toggle USD/TAO"
          >
            <span
              className={`inline-block h-3.5 w-3.5 transform rounded-full bg-white shadow transition-transform ${showUsd ? "translate-x-5" : "translate-x-1"}`}
            />
          </button>
          <span className={`text-xs font-semibold ${showUsd ? "text-emerald-400" : "text-gray-500"}`}>
            $ USD{showUsd && taoUsd ? ` (${fmtUsd(taoUsd)}/τ)` : ""}
          </span>
        </div>
        {port !== undefined && (
          isLive ? (
            <span className="flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-emerald-950 border border-emerald-600 text-emerald-400 text-xs font-bold">
              <span className="relative flex h-2 w-2">
                <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75" />
                <span className="relative inline-flex rounded-full h-2 w-2 bg-emerald-400" />
              </span>
              LIVE TRADING
            </span>
          ) : (
            <span className="px-2.5 py-1 rounded-full bg-gray-800 border border-gray-600 text-gray-400 text-xs font-bold">
              PAPER
            </span>
          )
        )}
      </div>
      <p className="text-xs text-gray-500 mb-6">
        Enters when price closes above EMA for{" "}
        {port?.confirm_bars ?? 3} consecutive bars on the EMA trend filter
      </p>

      {/* Pot summary */}
      <div className="grid grid-cols-2 md:grid-cols-5 gap-4 mb-4">
        <StatCard
          label="Wallet Balance"
          value={
            port?.wallet_balance != null
              ? fmtTao(port.wallet_balance + alphaMarkedValueTao, showUsd, taoUsd)
              : "—"
          }
          color="text-sky-400"
          sub={
            port?.wallet_balance != null
              ? showUsd
                ? `${fmt(port.wallet_balance + alphaMarkedValueTao)} τ — ${fmt(port.wallet_balance)} cash · ${fmt(alphaMarkedValueTao)} alpha`
                : `${fmtTao(port.wallet_balance, false, null)} cash · ${fmtTao(alphaMarkedValueTao, false, null)} alpha`
              : undefined
          }
        />
        <StatCard
          label="Trading Pot"
          value={fmtTao(port?.pot_tao, showUsd, taoUsd)}
          sub={showUsd && port?.pot_tao != null ? `${fmt(port.pot_tao)} τ` : undefined}
        />
        <StatCard
          label="Deployed"
          value={fmtTao(port?.deployed_tao, showUsd, taoUsd)}
          color={port && port.deployed_tao > 0 ? "text-amber-400" : "text-white"}
          sub={showUsd && port?.deployed_tao != null ? `${fmt(port.deployed_tao)} τ` : undefined}
        />
        <StatCard
          label="Unstaked"
          value={fmtTao(port?.unstaked_tao, showUsd, taoUsd)}
          color="text-emerald-400"
          sub={showUsd && port?.unstaked_tao != null ? `${fmt(port.unstaked_tao)} τ` : undefined}
        />
        <StatCard
          label="Open Positions"
          value={`${port?.open_count ?? 0} / ${port?.max_positions ?? 5}`}
        />
      </div>
      <div className="grid grid-cols-3 gap-4 mb-8">
        <StatCard
          label="Realized PnL"
          value={closed.length > 0 ? fmtPnl(realizedPnl, showUsd, taoUsd) : "—"}
          color={realizedPnl >= 0 ? "text-emerald-400" : "text-red-400"}
          sub={closed.length > 0
            ? showUsd
              ? `${fmtPnl(realizedPnl, false, null)} · ${wins}/${closed.length} wins`
              : `${wins}/${closed.length} wins`
            : undefined}
        />
        <StatCard
          label="Unrealized PnL"
          value={(port?.open_count ?? 0) > 0 ? fmtPnl(unrealizedPnl, showUsd, taoUsd) : "—"}
          color={unrealizedPnl >= 0 ? "text-emerald-400" : "text-red-400"}
          sub={(port?.open_count ?? 0) > 0
            ? showUsd
              ? `${fmtPnl(unrealizedPnl, false, null)} · across ${port?.open_count} positions`
              : `across ${port?.open_count} positions`
            : undefined}
        />
        <StatCard
          label="Total PnL"
          value={
            (closed.length > 0 || (port?.open_count ?? 0) > 0)
              ? fmtPnl(totalPnl, showUsd, taoUsd)
              : "—"
          }
          color={totalPnl >= 0 ? "text-emerald-400" : "text-red-400"}
          sub={
            (closed.length > 0 || (port?.open_count ?? 0) > 0)
              ? showUsd
                ? `${fmtPnl(totalPnl, false, null)} · ${totalPnl >= 0 ? "▲" : "▼"} ${((totalPnl / (port?.pot_tao ?? 10)) * 100).toFixed(2)}% of pot`
                : `${totalPnl >= 0 ? "▲" : "▼"} ${((totalPnl / (port?.pot_tao ?? 10)) * 100).toFixed(2)}% of pot`
              : undefined
          }
        />
      </div>

      {port && (
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-8">
          <div className="bg-gray-900 rounded-lg border border-gray-800 p-4">
            <p className="text-xs text-gray-400 mb-1">Signal Clock</p>
            <p className="text-xl font-bold text-white">{nextBarCountdown}</p>
            <p className="text-xs text-gray-500 mt-1">
              Next 4h bar close: {fmtUtcDate(nextBarClose)}
            </p>
            <p className="text-xs text-gray-600 mt-2">
              Signals only change when a full {signalTimeframeHours}h bar completes.
            </p>
          </div>
          <div className="bg-gray-900 rounded-lg border border-gray-800 p-4">
            <p className="text-xs text-gray-400 mb-1">Exit Watcher</p>
            <p className={`text-xl font-bold ${exitWatcher.enabled ? "text-sky-400" : "text-gray-500"}`}>
              {exitWatcher.enabled ? `LIVE · ${exitWatcher.interval_sec}s` : "OFF"}
            </p>
            <p className="text-xs text-gray-500 mt-1">
              Last run: {fmtDate(exitWatcher.last_run)}
            </p>
            <p className="text-xs text-gray-600 mt-2">
              Last cycle exits: {exitWatcher.last_exit_count}
            </p>
            {exitWatcher.last_error && (
              <p className="text-xs text-red-400 mt-2">
                Last error: {exitWatcher.last_error}
              </p>
            )}
          </div>
          <div className="bg-gray-900 rounded-lg border border-gray-800 p-4">
            <p className="text-xs text-gray-400 mb-1">Rule Summary</p>
            <p className="text-sm font-semibold text-emerald-400">
              Buy: {port.confirm_bars} bars above EMA
            </p>
            <p className="text-sm font-semibold text-red-400 mt-1">
              Sell: {port.confirm_bars} bars below EMA
            </p>
            <p className="text-xs text-gray-600 mt-2">
              Price exits can still close positions between bars via live watcher checks.
            </p>
          </div>
        </div>
      )}

      {/* Status alerts */}
      {(portData as any)?.breaker_active && (
        <div className="mb-4 px-4 py-2 rounded-lg bg-red-950 border border-red-700 text-red-300 text-sm font-semibold">
          🛑 Circuit breaker active — entries paused due to drawdown
        </div>
      )}

      <div className="mb-8">
        <EventFeed events={recentEvents} showUsd={showUsd} taoUsd={taoUsd} />
      </div>

      {/* Fee analytics */}
      {slipStats && slipStats.trade_count > 0 && (
        <div className="mb-8">
          <h2 className="text-sm font-semibold text-gray-400 mb-3">Fee Analytics ({slipStats.trade_count} trades with data)</h2>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            <StatCard
              label="Avg Entry Slippage"
              value={`${(slipStats.avg_entry_slippage_pct ?? 0).toFixed(2)}%`}
              color="text-amber-400"
            />
            <StatCard
              label="Avg Exit Slippage"
              value={`${(slipStats.avg_exit_slippage_pct ?? 0).toFixed(2)}%`}
              color="text-amber-400"
            />
            <StatCard
              label="Avg Round-Trip"
              value={`${(slipStats.avg_round_trip_pct ?? 0).toFixed(2)}%`}
              color="text-red-400"
            />
            <StatCard
              label="Total Slippage Cost"
              value={fmtTao(slipStats.total_slippage_tao ?? 0, showUsd, taoUsd)}
              color="text-red-400"
            />
          </div>
        </div>
      )}

      {/* Open positions with charts */}
      {port && positionCards.length > 0 && (
        <div className="mb-10">
          <h2 className="text-lg font-semibold mb-4 text-indigo-300">
            Open Positions ({port.open_positions.length})
            {exitAnimations.length > 0 && (
              <span className="ml-2 text-sm text-gray-500">
                · {exitAnimations.length} closing
              </span>
            )}
          </h2>
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            {positionCards.map((card) => {
              if (card.kind === "exit") {
                return (
                  <ExitAnimationTile
                    key={card.animation.id}
                    animation={card.animation}
                    showUsd={showUsd}
                    taoUsd={taoUsd}
                  />
                );
              }

              const pos = card.pos;
              const pnlColor =
                pos.pnl_pct >= 0 ? "text-emerald-400" : "text-red-400";
              const pnlTao = pos.amount_tao * (pos.pnl_pct / 100);
              const closeBtnColor = pos.pnl_pct >= 0
                ? "border-emerald-800 text-emerald-400 hover:bg-emerald-900/30"
                : "border-red-800 text-red-400 hover:bg-red-900/30";
              return (
                <div
                  key={pos.position_id}
                  className="bg-gray-900 border border-gray-800 rounded-lg p-4"
                >
                  <div className="flex justify-between items-start mb-2">
                    <div>
                      <span className="font-semibold text-white">
                        {pos.name || `SN${pos.netuid}`}
                      </span>
                      <span className="text-gray-500 text-sm ml-2">
                        #{pos.netuid}
                      </span>
                    </div>
                    <div className="text-right">
                      <span className={`text-lg font-bold ${pnlColor}`}>
                        {pos.pnl_pct >= 0 ? "+" : ""}
                        {pos.pnl_pct.toFixed(2)}%
                      </span>
                      <p className={`text-xs ${pnlColor} opacity-75`}>
                        {fmtPnl(pnlTao, showUsd, taoUsd)}
                      </p>
                    </div>
                  </div>

                  {/* Candlestick + EMA chart */}
                  <div className="my-2 -mx-1">
                    <CandleChart netuid={pos.netuid} entryPrice={pos.entry_price} emaPeriod={port?.ema_period ?? 18} />
                  </div>
                  <PositionInsight
                    netuid={pos.netuid}
                    entryPrice={pos.entry_price}
                    peakPrice={pos.peak_price}
                    fallbackCurrentPrice={pos.current_price}
                    emaPeriod={port?.ema_period ?? 18}
                    confirmBars={port?.confirm_bars ?? 3}
                    stopLossPct={port?.stop_loss_pct ?? 8}
                    takeProfitPct={port?.take_profit_pct ?? 10}
                    trailingStopPct={port?.trailing_stop_pct ?? 10}
                    showUsd={showUsd}
                    taoUsd={taoUsd}
                  />

                  <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm">
                    <div className="text-gray-400">
                      Entry{" "}
                      <span className="text-white">{fmtPrice(pos.entry_price, showUsd, taoUsd)}</span>
                    </div>
                    <div className="text-gray-400">
                      Current{" "}
                      <span className="text-white">{fmtPrice(pos.current_price, showUsd, taoUsd)}</span>
                    </div>
                    <div className="text-gray-400">
                      Invested{" "}
                      <span className="text-white">{fmtTao(pos.amount_tao, showUsd, taoUsd)}</span>
                    </div>
                    <div className="text-gray-400">
                      Held{" "}
                      <span className="text-white">{pos.hours_held.toFixed(1)}h</span>
                    </div>
                  </div>
                  <div className="mt-3 flex items-center justify-between">
                    <span className="text-xs text-gray-600">
                      Peak: {fmtPrice(pos.peak_price, showUsd, taoUsd)} · {fmtDate(pos.entry_ts)}
                    </span>
                    <button
                      onClick={() => closePosition(pos.position_id)}
                      disabled={closingId === pos.position_id}
                      className={`px-3 py-1.5 text-xs font-semibold rounded border disabled:opacity-50 transition-colors ${closeBtnColor}`}
                    >
                      {closingId === pos.position_id
                        ? "Closing..."
                        : `Close ${fmtPnl(pnlTao, showUsd, taoUsd, 3)}`}
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* Signal summary bar */}
      <SignalBar buy={buySignals} sell={sellSignals} hold={holdSignals} />

      {/* EMA Signal table */}
      <div className="mb-10">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-gray-400 text-left border-b border-gray-800">
                <th className="pb-2 pr-4">Subnet</th>
                <th className="pb-2 pr-4">Price {showUsd ? "(USD)" : "(τ)"}</th>
                <th className="pb-2 pr-4">EMA({port?.ema_period ?? 18}) {showUsd ? "(USD)" : "(τ)"}</th>
                <th className="pb-2 pr-4">Fast({sigData?.fast_ema_period ?? 6})</th>
                <th className="pb-2 pr-4">% vs EMA</th>
                <th className="pb-2 pr-4">Signal</th>
                <th className="pb-2">Bars</th>
              </tr>
            </thead>
            <tbody>
              {signals.map((s) => {
                const pctVsEma =
                  s.ema > 0 ? ((s.price - s.ema) / s.ema) * 100 : 0;
                const barColor =
                  s.bars > 0
                    ? "text-emerald-400"
                    : s.bars < 0
                    ? "text-red-400"
                    : "text-gray-500";
                return (
                  <tr
                    key={s.netuid}
                    className="border-b border-gray-800/50 hover:bg-gray-800/30"
                  >
                    <td className="py-2 pr-4">
                      <span className="font-medium text-white">
                        {s.name || `SN${s.netuid}`}
                      </span>
                      <span className="text-gray-600 text-xs ml-1.5">
                        #{s.netuid}
                      </span>
                    </td>
                    <td className="py-2 pr-4 text-gray-300 font-mono">
                      {fmtPrice(s.price, showUsd, taoUsd)}
                    </td>
                    <td className="py-2 pr-4 text-gray-400 font-mono">
                      {fmtPrice(s.ema, showUsd, taoUsd)}
                    </td>
                    <td className={`py-2 pr-4 font-mono ${s.price > s.fast_ema ? "text-emerald-400" : "text-red-400"}`}>
                      {fmtPrice(s.fast_ema, showUsd, taoUsd)}
                    </td>
                    <td
                      className={`py-2 pr-4 font-mono ${
                        pctVsEma >= 0 ? "text-emerald-400" : "text-red-400"
                      }`}
                    >
                      {pctVsEma >= 0 ? "+" : ""}
                      {pctVsEma.toFixed(2)}%
                    </td>
                    <td className="py-2 pr-4">
                      <SignalBadge signal={s.signal} />
                    </td>
                    <td className={`py-2 font-mono ${barColor}`}>
                      {s.bars > 0 ? `+${s.bars}` : s.bars}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

      {/* Trade history */}
      {closed.length > 0 && (
        <div>
          <h2 className="text-lg font-semibold mb-4 text-indigo-300">
            Closed Trades ({closed.length})
          </h2>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-gray-400 text-left border-b border-gray-800">
                  <th className="pb-2 pr-4">Subnet</th>
                  <th className="pb-2 pr-4">Entry</th>
                  <th className="pb-2 pr-4">Exit</th>
                  <th className="pb-2 pr-4">Entry Price {showUsd ? "(USD)" : "(τ)"}</th>
                  <th className="pb-2 pr-4">Exit Price {showUsd ? "(USD)" : "(τ)"}</th>
                  <th className="pb-2 pr-4">PnL</th>
                  <th className="pb-2">Reason</th>
                </tr>
              </thead>
              <tbody>
                {closed.map((p) => {
                  const pnlColor =
                    (p.pnl_pct ?? 0) >= 0 ? "text-emerald-400" : "text-red-400";
                  const reasonColor =
                    p.exit_reason === "TAKE_PROFIT"
                      ? "text-emerald-400"
                      : p.exit_reason === "STOP_LOSS"
                      ? "text-red-400"
                      : p.exit_reason === "EMA_CROSS"
                      ? "text-blue-400"
                      : p.exit_reason === "MANUAL_CLOSE"
                      ? "text-amber-400"
                      : "text-gray-400";
                  return (
                    <tr
                      key={p.id}
                      className="border-b border-gray-800/50 hover:bg-gray-800/30"
                    >
                      <td className="py-2 pr-4">
                        <span className="font-medium text-white">
                          {p.name || `SN${p.netuid}`}
                        </span>
                      </td>
                      <td className="py-2 pr-4 text-gray-400 text-xs">
                        {fmtDate(p.entry_ts)}
                      </td>
                      <td className="py-2 pr-4 text-gray-400 text-xs">
                        {fmtDate(p.exit_ts)}
                      </td>
                      <td className="py-2 pr-4 text-gray-300 font-mono">
                        {fmtPrice(p.entry_price, showUsd, taoUsd)}
                      </td>
                      <td className="py-2 pr-4 text-gray-300 font-mono">
                        {fmtPrice(p.exit_price, showUsd, taoUsd)}
                      </td>
                      <td className={`py-2 pr-4 font-bold ${pnlColor}`}>
                        {p.pnl_pct != null
                          ? `${p.pnl_pct >= 0 ? "+" : ""}${p.pnl_pct.toFixed(2)}%`
                          : "—"}
                        {p.pnl_tao != null && (
                          <span className="font-normal text-xs opacity-60 ml-1">
                            ({fmtPnl(p.pnl_tao, showUsd, taoUsd)})
                          </span>
                        )}
                      </td>
                      <td className={`py-2 text-xs ${reasonColor}`}>
                        {p.exit_reason ?? "—"}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {closed.length === 0 && port?.open_count === 0 && (
        <p className="text-gray-500 text-sm mt-4">
          No EMA trades yet. Signals will trigger entries on the next scan cycle.
        </p>
      )}
    </div>
  );
}
