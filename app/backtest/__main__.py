"""
CLI runner for the backtest engine.

Usage:
    python -m app.backtest --full                     # All strategies, all windows
    python -m app.backtest --quick                    # Production configs, 30d only
    python -m app.backtest --strategy A1 --window 30  # Specific strategy + window
    python -m app.backtest --fetch-only               # Just download data
    python -m app.backtest --full --export csv         # Export to CSV
"""
from __future__ import annotations

import argparse
import asyncio
import time

from .data_loader import fetch_all, load_cached_history, load_pool_snapshots
from .engine import BacktestResult, backtest_strategy
from .report import (
    print_full_report,
    print_ranking_table,
    print_exit_breakdown,
    print_subnet_performance,
    print_trend_analysis,
    save_results_csv,
    save_results_json,
)
from .strategies import (
    ALL_STRATEGIES,
    LOOKBACK_WINDOWS,
    PRODUCTION,
    STRATEGY_MAP,
    BacktestStrategyConfig,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="EMA Strategy Backtest Engine",
        prog="python -m app.backtest",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--full", action="store_true",
        help="Run all strategies across all lookback windows",
    )
    mode.add_argument(
        "--quick", action="store_true",
        help="Run production configs (A1, A2) on 30d window only",
    )
    mode.add_argument(
        "--fetch-only", action="store_true",
        help="Download/refresh historical data without running backtest",
    )
    parser.add_argument(
        "--strategy", type=str, default=None,
        help="Strategy ID to test (e.g., A1, B2, D5)",
    )
    parser.add_argument(
        "--window", type=int, default=None,
        help="Lookback window in days (e.g., 7, 14, 30, 90, 120, 150)",
    )
    parser.add_argument(
        "--export", type=str, choices=["json", "csv", "both"], default="json",
        help="Export format (default: json)",
    )
    parser.add_argument(
        "--force-refresh", action="store_true",
        help="Force re-download of all historical data",
    )
    return parser.parse_args()


def run_backtest(
    strategies: list[BacktestStrategyConfig],
    windows: list[int],
    all_history: dict[int, list[dict]],
    pool_snapshots: dict[int, dict],
) -> dict[int, list[BacktestResult]]:
    """Run backtest matrix: strategies x windows."""
    total = len(strategies) * len(windows)
    results_by_window: dict[int, list[BacktestResult]] = {}
    done = 0

    for window in sorted(windows):
        window_results: list[BacktestResult] = []
        for cfg in strategies:
            done += 1
            label = f"{cfg.strategy_id} ({cfg.tag})"
            print(
                f"  [{done}/{total}] {label} @ {window}d ... ",
                end="", flush=True,
            )
            t0 = time.time()
            result = backtest_strategy(all_history, cfg, pool_snapshots, window)
            elapsed = time.time() - t0
            print(
                f"{result.total_trades} trades, "
                f"WR {result.win_rate:.1f}%, "
                f"E[{result.expectancy:+.2f}%] "
                f"({elapsed:.1f}s)"
            )
            window_results.append(result)
        results_by_window[window] = window_results

    return results_by_window


def main() -> None:
    args = parse_args()

    # ── Fetch data ────────────────────────────────────────────────
    if args.fetch_only:
        print("=== Backtest Data Fetcher ===")
        history = asyncio.run(fetch_all(force_refresh=args.force_refresh))
        total_points = sum(len(v) for v in history.values())
        print(f"\nDone: {len(history)} subnets, {total_points:,} total data points")
        return

    # Load data (fetch if not cached)
    print("Loading historical data...")
    all_history = load_cached_history()
    pool_snapshots = load_pool_snapshots()

    if not all_history:
        print("No cached data found. Fetching from Taostats API...")
        all_history = asyncio.run(fetch_all(force_refresh=args.force_refresh))
        pool_snapshots = load_pool_snapshots()

    if not all_history:
        print("ERROR: No historical data available. Run with --fetch-only first.")
        return

    print(f"Loaded {len(all_history)} subnets")

    # ── Determine strategies and windows ──────────────────────────
    if args.strategy:
        cfg = STRATEGY_MAP.get(args.strategy)
        if not cfg:
            print(f"ERROR: Unknown strategy '{args.strategy}'")
            print(f"Available: {', '.join(sorted(STRATEGY_MAP.keys()))}")
            return
        strategies = [cfg]
    elif args.quick:
        strategies = list(PRODUCTION)
    else:
        strategies = list(ALL_STRATEGIES)

    if args.window:
        windows = [args.window]
    elif args.quick:
        windows = [30]
    else:
        windows = list(LOOKBACK_WINDOWS)

    # ── Run backtest ──────────────────────────────────────────────
    print(f"\nRunning backtest: {len(strategies)} strategies x {len(windows)} windows")
    print(f"Strategies: {', '.join(s.strategy_id for s in strategies)}")
    print(f"Windows: {', '.join(f'{w}d' for w in windows)}")
    print()

    t0 = time.time()
    results_by_window = run_backtest(
        strategies, windows, all_history, pool_snapshots
    )
    elapsed = time.time() - t0

    # ── Report ────────────────────────────────────────────────────
    if len(windows) == 1 and len(strategies) <= 3:
        # Simple output for single window / few strategies
        w = windows[0]
        results = results_by_window.get(w, [])
        print_ranking_table(results, w)
        for r in results:
            print_exit_breakdown(r)
            print_subnet_performance(r)
    else:
        primary = 30 if 30 in windows else windows[0]
        print_full_report(results_by_window, primary_window=primary)

    print(f"\n  Total runtime: {elapsed:.1f}s")

    # ── Export ─────────────────────────────────────────────────────
    if args.export in ("json", "both"):
        save_results_json(results_by_window)
    if args.export in ("csv", "both"):
        save_results_csv(results_by_window)


if __name__ == "__main__":
    main()
