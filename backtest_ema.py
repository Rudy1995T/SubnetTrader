#!/usr/bin/env python3
"""
EMA Timeframe Backtest (Extended)
==================================
Fetches daily price history (~200 days) from Taostats for all subnets,
then simulates the dual-EMA strategy across multiple time windows
(14d, 30d, 90d, full) and EMA period combinations.

Also runs the original 7-day / 4h-candle backtest for comparison.

Usage:
    source .venv/bin/activate
    python backtest_ema.py
"""
from __future__ import annotations

import asyncio
import sys
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from app.strategy.ema_signals import (
    Candle,
    build_sampled_candles,
    compute_ema,
)
from app.config import settings


# ── Configuration ─────────────────────────────────────────────────

EMA_COMBOS: list[tuple[int, int]] = [
    (3, 9),
    (4, 12),
    (5, 15),
    (6, 18),   # ← current production settings
    (8, 24),
    (4, 18),
    (6, 12),
    (3, 18),
    (5, 20),
    (10, 30),
]

CONFIRM_BARS = 3
STOP_LOSS_PCT = 8.0
TAKE_PROFIT_PCT = 20.0
TRAILING_STOP_PCT = 5.0
BREAKEVEN_TRIGGER_PCT = 3.0
MAX_ENTRY_PRICE = 0.1     # TAO
SLIPPAGE_PCT = 1.0         # simulated slippage on entry/exit

# Time windows to test (in days, None = all data)
WINDOWS: list[tuple[str, int | None]] = [
    ("14d", 14),
    ("30d", 30),
    ("90d", 90),
    ("ALL (~200d)", None),
]


# ── Data structures ───────────────────────────────────────────────

@dataclass
class Trade:
    netuid: int
    entry_bar: int
    entry_price: float
    exit_bar: int = -1
    exit_price: float = 0.0
    exit_reason: str = ""
    pnl_pct: float = 0.0
    peak_price: float = 0.0


@dataclass
class BacktestResult:
    fast: int
    slow: int
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_pnl_pct: float = 0.0
    avg_pnl_pct: float = 0.0
    win_rate: float = 0.0
    max_drawdown_pct: float = 0.0
    avg_holding_bars: float = 0.0
    trades: list[Trade] = field(default_factory=list)
    subnets_with_data: int = 0


# ── Signal logic (mirrors live bot) ──────────────────────────────

def _ema_signal(prices: list[float], period: int, confirm: int) -> str:
    if len(prices) < confirm:
        return "HOLD"
    ema = compute_ema(prices, period)
    if len(ema) < confirm:
        return "HOLD"
    recent_p = prices[-confirm:]
    recent_e = ema[-confirm:]
    if all(p > e for p, e in zip(recent_p, recent_e)):
        return "BUY"
    if all(p < e for p, e in zip(recent_p, recent_e)):
        return "SELL"
    return "HOLD"


def _dual_ema_signal(prices: list[float], fast: int, slow: int, confirm: int) -> str:
    f = _ema_signal(prices, fast, confirm)
    s = _ema_signal(prices, slow, confirm)
    if f == "BUY" and s == "BUY":
        return "BUY"
    if f == "SELL" or s == "SELL":
        return "SELL"
    return "HOLD"


# ── Simulation engine ────────────────────────────────────────────

def simulate_subnet(
    closes: list[float],
    fast: int,
    slow: int,
    confirm: int,
    netuid: int,
    max_holding_bars: int,
) -> list[Trade]:
    """Walk forward through daily closes, simulating entries and exits."""
    if len(closes) < max(slow, fast) + confirm:
        return []

    trades: list[Trade] = []
    position: Trade | None = None

    for i in range(max(slow, confirm), len(closes)):
        price = closes[i]
        price_window = closes[: i + 1]

        if position is not None:
            position.peak_price = max(position.peak_price, price)
            entry = position.entry_price
            current_pnl = (price - entry) / entry * 100.0
            bars_held = i - position.entry_bar
            peak_pnl = (position.peak_price - entry) / entry * 100.0

            exit_reason = ""

            if current_pnl <= -STOP_LOSS_PCT:
                exit_reason = "STOP_LOSS"
            elif current_pnl >= TAKE_PROFIT_PCT:
                exit_reason = "TAKE_PROFIT"
            elif peak_pnl >= TRAILING_STOP_PCT and current_pnl <= peak_pnl - TRAILING_STOP_PCT:
                exit_reason = "TRAILING_STOP"
            elif peak_pnl >= BREAKEVEN_TRIGGER_PCT and current_pnl <= 0:
                exit_reason = "BREAKEVEN_STOP"
            elif bars_held >= max_holding_bars:
                exit_reason = "TIME_STOP"
            elif _dual_ema_signal(price_window, fast, slow, confirm) == "SELL":
                exit_reason = "EMA_CROSS"

            if exit_reason:
                exit_price = price * (1.0 - SLIPPAGE_PCT / 100.0)
                pnl = (exit_price - position.entry_price) / position.entry_price * 100.0
                position.exit_bar = i
                position.exit_price = exit_price
                position.exit_reason = exit_reason
                position.pnl_pct = pnl
                trades.append(position)
                position = None
            continue

        if price > MAX_ENTRY_PRICE:
            continue

        signal = _dual_ema_signal(price_window, fast, slow, confirm)
        if signal == "BUY":
            entry_price = price * (1.0 + SLIPPAGE_PCT / 100.0)
            position = Trade(
                netuid=netuid,
                entry_bar=i,
                entry_price=entry_price,
                peak_price=entry_price,
            )

    if position is not None:
        exit_price = closes[-1] * (1.0 - SLIPPAGE_PCT / 100.0)
        pnl = (exit_price - position.entry_price) / position.entry_price * 100.0
        position.exit_bar = len(closes) - 1
        position.exit_price = exit_price
        position.exit_reason = "END_OF_DATA"
        position.pnl_pct = pnl
        trades.append(position)

    return trades


def run_backtest(
    subnet_data: dict[int, list[float]],
    fast: int,
    slow: int,
    max_holding_bars: int,
) -> BacktestResult:
    result = BacktestResult(fast=fast, slow=slow)
    result.subnets_with_data = len(subnet_data)

    all_trades: list[Trade] = []
    for netuid, closes in subnet_data.items():
        trades = simulate_subnet(closes, fast, slow, CONFIRM_BARS, netuid, max_holding_bars)
        all_trades.extend(trades)

    result.trades = all_trades
    result.total_trades = len(all_trades)
    if not all_trades:
        return result

    result.winning_trades = sum(1 for t in all_trades if t.pnl_pct > 0)
    result.losing_trades = sum(1 for t in all_trades if t.pnl_pct <= 0)
    result.total_pnl_pct = sum(t.pnl_pct for t in all_trades)
    result.avg_pnl_pct = result.total_pnl_pct / result.total_trades
    result.win_rate = result.winning_trades / result.total_trades * 100.0
    result.avg_holding_bars = (
        sum(t.exit_bar - t.entry_bar for t in all_trades) / result.total_trades
    )

    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in sorted(all_trades, key=lambda x: x.entry_bar):
        cumulative += t.pnl_pct
        peak = max(peak, cumulative)
        dd = peak - cumulative
        max_dd = max(max_dd, dd)
    result.max_drawdown_pct = max_dd

    return result


# ── Data fetching ─────────────────────────────────────────────────

async def fetch_7day_subnets(client: httpx.AsyncClient) -> dict[int, list[dict]]:
    """Fetch 7-day intraday prices from pool/latest endpoint."""
    resp = await client.get("/api/dtao/pool/latest/v1", params={"limit": 200})
    resp.raise_for_status()
    data = resp.json()
    items = data.get("data", []) if isinstance(data, dict) else []
    result: dict[int, list[dict]] = {}
    for subnet in items:
        try:
            netuid = int(subnet.get("netuid", -1))
            prices = subnet.get("seven_day_prices", [])
            if netuid >= 1 and prices:
                result[netuid] = prices
        except (ValueError, TypeError):
            continue
    return result


async def fetch_daily_history(client: httpx.AsyncClient) -> dict[int, list[float]]:
    """
    Fetch daily price history for all subnets via pool/history endpoint.
    Returns {netuid: [oldest_price, ..., newest_price]}.
    Rate-limited to stay under 30 req/min.
    """
    # First get list of netuids from latest
    resp = await client.get("/api/dtao/pool/latest/v1", params={"limit": 200})
    resp.raise_for_status()
    data = resp.json()
    items = data.get("data", []) if isinstance(data, dict) else []
    netuids = []
    for s in items:
        try:
            netuid = int(s.get("netuid", -1))
            if netuid >= 1:
                netuids.append(netuid)
        except (ValueError, TypeError):
            continue

    print(f"Fetching daily history for {len(netuids)} subnets (rate limited, ~{len(netuids)*2}s)...")

    result: dict[int, list[float]] = {}
    sem = asyncio.Semaphore(5)  # max concurrent
    call_times: list[float] = []
    lock = asyncio.Lock()

    async def fetch_one(netuid: int) -> None:
        async with sem:
            # Rate limit: max 25/min to stay safe
            async with lock:
                now = _time.time()
                call_times[:] = [t for t in call_times if t > now - 60]
                if len(call_times) >= 25:
                    wait = 60.0 - (now - call_times[0]) + 0.2
                    await asyncio.sleep(wait)
                call_times.append(_time.time())

            try:
                resp = await client.get(
                    "/api/dtao/pool/history/v1",
                    params={"netuid": netuid, "limit": 200},
                )
                resp.raise_for_status()
                data = resp.json()
                records = data.get("data", []) if isinstance(data, dict) else data
                if not isinstance(records, list) or not records:
                    return

                # Records come newest-first; reverse to oldest-first
                prices: list[float] = []
                for rec in reversed(records):
                    try:
                        p = float(rec.get("price", 0))
                        if p > 0:
                            prices.append(p)
                    except (ValueError, TypeError):
                        continue

                if len(prices) >= 10:
                    result[netuid] = prices
            except Exception as e:
                pass  # skip failed subnets silently

    tasks = [asyncio.create_task(fetch_one(n)) for n in netuids]

    # Progress reporting
    done_count = 0
    for coro in asyncio.as_completed(tasks):
        await coro
        done_count += 1
        if done_count % 20 == 0 or done_count == len(tasks):
            print(f"  ... {done_count}/{len(tasks)} subnets fetched")

    return result


# ── Display helpers ───────────────────────────────────────────────

def print_table(results: list[BacktestResult], label: str, candle_label: str) -> None:
    current = (6, 18)
    print(f"\n{'='*74}")
    print(f"  {label}  ({candle_label})")
    print(f"{'='*74}")
    print(f"{'Fast':>4} {'Slow':>4} │ {'Trades':>6} {'Win%':>6} {'Avg PnL':>8} "
          f"{'Tot PnL':>9} {'MaxDD':>7} {'AvgBars':>7} │")
    print("─" * 4 + " " + "─" * 4 + " ┼ " + "─" * 6 + " " + "─" * 6 + " "
          + "─" * 8 + " " + "─" * 9 + " " + "─" * 7 + " " + "─" * 7 + " ┼")

    for r in results:
        marker = " ◄ CURRENT" if (r.fast, r.slow) == current else ""
        print(
            f"{r.fast:>4} {r.slow:>4} │ "
            f"{r.total_trades:>6} "
            f"{r.win_rate:>5.1f}% "
            f"{r.avg_pnl_pct:>+7.2f}% "
            f"{r.total_pnl_pct:>+8.1f}% "
            f"{r.max_drawdown_pct:>6.1f}% "
            f"{r.avg_holding_bars:>6.1f} │{marker}"
        )


def print_exit_breakdown(results: list[BacktestResult]) -> None:
    current = (6, 18)
    shown: set[tuple[int, int]] = set()
    to_show: list[BacktestResult] = []
    for r in results[:3]:
        to_show.append(r)
        shown.add((r.fast, r.slow))
    for r in results:
        if (r.fast, r.slow) == current and (r.fast, r.slow) not in shown:
            to_show.append(r)

    for r in to_show:
        marker = " (CURRENT)" if (r.fast, r.slow) == current else ""
        print(f"\n  Fast={r.fast}, Slow={r.slow}{marker}")
        if not r.trades:
            print("    No trades")
            continue
        reasons: dict[str, list[float]] = {}
        for t in r.trades:
            reasons.setdefault(t.exit_reason, []).append(t.pnl_pct)
        for reason in sorted(reasons, key=lambda x: len(reasons[x]), reverse=True):
            pnls = reasons[reason]
            avg = sum(pnls) / len(pnls)
            print(f"    {reason:<16} {len(pnls):>4} trades  avg PnL: {avg:>+7.2f}%")


# ── Main ──────────────────────────────────────────────────────────

async def main() -> None:
    print("=" * 74)
    print("  EMA Backtest — Multi-Timeframe (7d intraday + daily up to ~200 days)")
    print("=" * 74)
    print()

    base = settings.TAOSTATS_BASE_URL.rstrip("/")
    headers: dict[str, str] = {"Accept": "application/json"}
    if settings.TAOSTATS_API_KEY:
        headers["Authorization"] = settings.TAOSTATS_API_KEY

    async with httpx.AsyncClient(
        base_url=base, headers=headers, timeout=30.0
    ) as client:

        # ── Part 1: 7-day intraday (4h candles) ──────────────────
        print("── Part 1: 7-day data (4h candles) ──")
        raw_7d = await fetch_7day_subnets(client)
        print(f"Got 7-day prices for {len(raw_7d)} subnets")

        candles_7d: dict[int, list[float]] = {}
        for netuid, points in raw_7d.items():
            candles = build_sampled_candles(points, 4)
            closes = [c.close for c in candles]
            if len(closes) >= 10:
                candles_7d[netuid] = closes
        print(f"Built 4h candles for {len(candles_7d)} subnets")

        max_hold_4h = 168 // 4  # 42 bars
        results_7d: list[BacktestResult] = []
        for fast, slow in EMA_COMBOS:
            r = run_backtest(candles_7d, fast, slow, max_hold_4h)
            results_7d.append(r)
        results_7d.sort(key=lambda r: r.total_pnl_pct, reverse=True)
        print_table(results_7d, "7-Day Backtest", "4h candles, bars = 4h periods")

        # ── Part 2: Daily history for multiple windows ────────────
        print("\n\n── Part 2: Fetching daily history ──")
        daily_data = await fetch_daily_history(client)
        print(f"Got daily history for {len(daily_data)} subnets")
        if daily_data:
            lengths = [len(v) for v in daily_data.values()]
            print(f"Daily record counts: min={min(lengths)}, max={max(lengths)}, avg={sum(lengths)/len(lengths):.0f}")

        max_hold_daily = 7  # 7 days max holding (same as 168h)

        for window_label, window_days in WINDOWS:
            # Trim data to window
            windowed: dict[int, list[float]] = {}
            for netuid, prices in daily_data.items():
                if window_days is not None:
                    trimmed = prices[-window_days:]
                else:
                    trimmed = prices
                if len(trimmed) >= 10:
                    windowed[netuid] = trimmed

            results: list[BacktestResult] = []
            for fast, slow in EMA_COMBOS:
                r = run_backtest(windowed, fast, slow, max_hold_daily)
                results.append(r)
            results.sort(key=lambda r: r.total_pnl_pct, reverse=True)

            n_subnets = len(windowed)
            print_table(
                results,
                f"{window_label} Backtest ({n_subnets} subnets)",
                f"daily candles, bars = days",
            )

        # ── Exit breakdown for the longest window ─────────────────
        print(f"\n{'='*74}")
        print(f"  Exit Reason Breakdown — ALL (~200d) window")
        print(f"{'='*74}")

        # Rerun ALL window for breakdown
        all_windowed: dict[int, list[float]] = {}
        for netuid, prices in daily_data.items():
            if len(prices) >= 10:
                all_windowed[netuid] = prices
        all_results: list[BacktestResult] = []
        for fast, slow in EMA_COMBOS:
            r = run_backtest(all_windowed, fast, slow, max_hold_daily)
            all_results.append(r)
        all_results.sort(key=lambda r: r.total_pnl_pct, reverse=True)
        print_exit_breakdown(all_results)

    # ── Final summary ─────────────────────────────────────────────
    print(f"\n{'='*74}")
    print("  SUMMARY — Best combo per window")
    print(f"{'='*74}")
    print(f"  {'Window':<16} {'Best':>10} {'Tot PnL':>10} {'Trades':>7} {'Win%':>6}  │  "
          f"{'Current 6/18':>10} {'Tot PnL':>10}")
    print("  " + "─" * 72)

    # Collect from all windows
    for label, best_results in [
        ("7d (4h candles)", results_7d),
    ]:
        best = best_results[0]
        cur = next((r for r in best_results if (r.fast, r.slow) == (6, 18)), None)
        cur_pnl = f"{cur.total_pnl_pct:>+9.1f}%" if cur else "N/A"
        print(f"  {label:<16} {best.fast}/{best.slow:>2}      "
              f"{best.total_pnl_pct:>+9.1f}% {best.total_trades:>7} {best.win_rate:>5.1f}%  │  "
              f"{'6/18':<10} {cur_pnl}")

    for window_label, window_days in WINDOWS:
        windowed = {}
        for netuid, prices in daily_data.items():
            trimmed = prices[-window_days:] if window_days else prices
            if len(trimmed) >= 10:
                windowed[netuid] = trimmed
        wr: list[BacktestResult] = []
        for fast, slow in EMA_COMBOS:
            r = run_backtest(windowed, fast, slow, max_hold_daily)
            wr.append(r)
        wr.sort(key=lambda r: r.total_pnl_pct, reverse=True)
        best = wr[0]
        cur = next((r for r in wr if (r.fast, r.slow) == (6, 18)), None)
        cur_pnl = f"{cur.total_pnl_pct:>+9.1f}%" if cur else "N/A"
        print(f"  {window_label + ' (daily)':<16} {best.fast}/{best.slow:>2}      "
              f"{best.total_pnl_pct:>+9.1f}% {best.total_trades:>7} {best.win_rate:>5.1f}%  │  "
              f"{'6/18':<10} {cur_pnl}")

    print()


if __name__ == "__main__":
    asyncio.run(main())
