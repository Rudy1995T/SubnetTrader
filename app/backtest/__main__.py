"""
CLI runner for the backtest engine.

Usage:
    python -m app.backtest --full                     # EMA: all strategies, all windows
    python -m app.backtest --quick                    # EMA: production configs, 30d only
    python -m app.backtest --strategy A1 --window 30  # EMA: specific strategy + window
    python -m app.backtest --fetch-only               # EMA: download data only
    python -m app.backtest flow --window-days 120     # Flow Momentum single run
    python -m app.backtest flow --sweep               # Flow: full parameter grid
    python -m app.backtest flow --quick --sweep       # Flow: small sanity grid
    python -m app.backtest flow --fetch-only          # Flow: fetch cached history only
    python -m app.backtest meanrev                    # Mean-reversion: F1..F8 x all windows
    python -m app.backtest meanrev --sweep            # Alias for the full F* grid
    python -m app.backtest meanrev --strategy F1      # Single meanrev config, all windows
    python -m app.backtest meanrev --window 30        # All F* on 30d only
    python -m app.backtest compare --window 30        # Cross-family ranking (EMA/meanrev/flow)
    python -m app.backtest per-regime                 # {strategy × regime} edge matrix
    python -m app.backtest per-regime --window 90     # Single window override
    python -m app.backtest probe                      # Run Taostats availability probe
"""
from __future__ import annotations

import argparse
import asyncio
import sys
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
    MEAN_REVERSION,
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


def _dispatch_subcommand() -> bool:
    """Handle subcommands (flow, probe, meanrev, compare) before falling
    through to the EMA CLI.

    Returns True when a subcommand ran (and the main EMA path should be
    skipped). Subcommand flags are consumed here so the EMA parser doesn't
    see them.
    """
    if len(sys.argv) < 2:
        return False
    sub = sys.argv[1]
    if sub == "flow":
        # Replace argv so the flow_engine's own parser sees a clean argv.
        sys.argv = [sys.argv[0]] + sys.argv[2:]
        from .flow_engine import main as flow_main
        flow_main()
        return True
    if sub == "probe":
        sys.argv = [sys.argv[0]] + sys.argv[2:]
        from .probe_flow_history import main as probe_main
        probe_main()
        return True
    if sub == "meanrev":
        sys.argv = [sys.argv[0]] + sys.argv[2:]
        _run_meanrev_cli()
        return True
    if sub == "compare":
        sys.argv = [sys.argv[0]] + sys.argv[2:]
        from .compare import main as compare_main
        compare_main()
        return True
    if sub in ("per-regime", "per_regime"):
        sys.argv = [sys.argv[0]] + sys.argv[2:]
        _run_per_regime_cli()
        return True
    return False


def _parse_meanrev_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Mean-Reversion backtest harness",
        prog="python -m app.backtest meanrev",
    )
    parser.add_argument(
        "--sweep", action="store_true",
        help="Run the full F* grid x all windows (default behaviour; kept for parity with flow CLI)",
    )
    parser.add_argument(
        "--strategy", type=str, default=None,
        help="Run a single F* config (e.g. F1, F4). Defaults to F1..F8.",
    )
    parser.add_argument(
        "--window", type=int, default=None,
        help="Run a single lookback window (days). Defaults to all LOOKBACK_WINDOWS.",
    )
    parser.add_argument(
        "--export", type=str, choices=["json", "csv", "both"], default="both",
        help="Export format (default: both — JSON + CSV).",
    )
    parser.add_argument(
        "--force-refresh", action="store_true",
        help="Force re-download of cached history (same semantics as EMA CLI).",
    )
    return parser.parse_args()


def _run_meanrev_cli() -> None:
    args = _parse_meanrev_args()

    print("Loading historical data...")
    all_history = load_cached_history()
    pool_snapshots = load_pool_snapshots()

    if not all_history:
        print(
            "ERROR: No cached history found. "
            "Run `python -m app.backtest --fetch-only` first."
        )
        return
    print(f"Loaded {len(all_history)} subnets")

    if args.strategy:
        cfg = STRATEGY_MAP.get(args.strategy)
        if not cfg:
            print(f"ERROR: Unknown strategy '{args.strategy}'")
            print(f"Available meanrev: {', '.join(s.strategy_id for s in MEAN_REVERSION)}")
            return
        if cfg.strategy_type != "meanrev":
            print(
                f"ERROR: {args.strategy} is a {cfg.strategy_type} strategy. "
                "Use the default CLI (without the meanrev subcommand) for non-meanrev configs."
            )
            return
        strategies = [cfg]
    else:
        strategies = list(MEAN_REVERSION)

    windows = [args.window] if args.window else list(LOOKBACK_WINDOWS)

    print(f"\nRunning meanrev backtest: {len(strategies)} strategies x {len(windows)} windows")
    print(f"Strategies: {', '.join(s.strategy_id for s in strategies)}")
    print(f"Windows: {', '.join(f'{w}d' for w in windows)}\n")

    t0 = time.time()
    results_by_window = run_backtest(strategies, windows, all_history, pool_snapshots)
    elapsed = time.time() - t0

    for w in sorted(results_by_window.keys()):
        results = results_by_window[w]
        if not results:
            continue
        print_ranking_table(results, w, production_ids=set())
        if len(strategies) == 1 and results:
            print_exit_breakdown(results[0])
            print_subnet_performance(results[0])
        else:
            best = max(results, key=lambda r: r.expectancy)
            print_exit_breakdown(best)

    print(f"\n  Total runtime: {elapsed:.1f}s")

    if args.export in ("json", "both"):
        save_results_json(results_by_window, filename_prefix="meanrev")
    if args.export in ("csv", "both"):
        save_results_csv(results_by_window, filename_prefix="meanrev")


def _parse_per_regime_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Per-regime edge analysis — {strategy × regime} matrix",
        prog="python -m app.backtest per-regime",
    )
    p.add_argument("--window", type=int, default=90,
                   help="Lookback window in days for all three strategies (default: 90)")
    p.add_argument("--flow-interval", type=str, default=None,
                   help="Flow cadence override (e.g. 1h, 4h, 1d)")
    p.add_argument("--acknowledge-cadence-degradation", action="store_true",
                   help="Required when flow cadence >= 1h (see flow backtest spec)")
    p.add_argument("--min-trades", type=int, default=20,
                   help="Significance threshold per cell (default: 20)")
    p.add_argument("--enable-edge", type=float, default=0.5,
                   help="Edge percent required for ENABLE (default: 0.5)")
    p.add_argument("--enable-pf", type=float, default=1.3,
                   help="Profit factor required for ENABLE (default: 1.3)")
    p.add_argument("--disable-edge", type=float, default=-0.2,
                   help="Edge percent at or below which we DISABLE (default: -0.2)")
    p.add_argument("--disable-pf", type=float, default=0.9,
                   help="Profit factor below which we DISABLE (default: 0.9)")
    p.add_argument("--skip-ema", action="store_true", help="Skip EMA backtest")
    p.add_argument("--skip-flow", action="store_true", help="Skip Flow backtest")
    p.add_argument("--skip-meanrev", action="store_true", help="Skip Mean-Rev backtest")
    p.add_argument("--save-timeline", action="store_true", default=True,
                   help="Persist the regime timeline under data/backtest/regime_timeline.json")
    p.add_argument("--output", type=str, default=None,
                   help="Override CSV output path")
    return p.parse_args()


def _run_per_regime_cli() -> None:
    from datetime import datetime, timezone
    from pathlib import Path

    from .flow_data_loader import load_cached_flow_history
    from .regime_labeler import (
        build_regime_timeline,
        save_timeline,
        TIMELINE_PATH,
    )
    from .per_regime_report import (
        DEFAULT_MIN_TRADES_PER_CELL,
        build_cells,
        label_trades,
        print_per_regime_matrix,
        print_decision_rubric,
        save_per_regime_csv,
        save_per_regime_json,
        suggested_env_lines,
    )

    args = _parse_per_regime_args()

    print("=== Per-Regime Edge Analysis ===")
    print("Loading cached histories...")
    ema_history = load_cached_history()
    pool_snapshots = load_pool_snapshots()
    flow_history = load_cached_flow_history()

    if not flow_history:
        print("ERROR: no flow history cached — run `python -m app.backtest flow --fetch-only` first.")
        return

    print("\nBuilding regime timeline from flow cache...")
    timeline, resolved = build_regime_timeline(flow_history)
    print(
        f"  labels={len(timeline.epochs)} bucket={resolved.bucket_hours}h "
        f"window={resolved.window_hours}h source={resolved.source}"
    )
    if args.save_timeline and timeline.epochs:
        path = save_timeline(timeline, TIMELINE_PATH, resolved=resolved)
        print(f"  wrote {path}")

    if not timeline.epochs:
        print("ERROR: timeline is empty — no regime labels could be produced.")
        return

    trades_by_strategy: dict[str, list] = {}

    # ── EMA ──
    if not args.skip_ema and ema_history:
        print("\nRunning EMA backtest (PRODUCTION configs)...")
        for cfg in PRODUCTION:
            print(f"  {cfg.strategy_id} ({cfg.tag}) @ {args.window}d ...", end=" ", flush=True)
            result = backtest_strategy(ema_history, cfg, pool_snapshots, args.window)
            label_trades(result.trades, timeline)
            key = f"ema_{cfg.strategy_id}"
            trades_by_strategy[key] = result.trades
            print(f"{len(result.trades)} trades")

    # ── Mean-Reversion ──
    if not args.skip_meanrev and ema_history:
        print("\nRunning Mean-Reversion backtest (F* configs)...")
        for cfg in MEAN_REVERSION:
            print(f"  {cfg.strategy_id} ({cfg.tag}) @ {args.window}d ...", end=" ", flush=True)
            result = backtest_strategy(ema_history, cfg, pool_snapshots, args.window)
            label_trades(result.trades, timeline)
            key = f"meanrev_{cfg.strategy_id}"
            trades_by_strategy[key] = result.trades
            print(f"{len(result.trades)} trades")

    # ── Flow ──
    if not args.skip_flow:
        print("\nRunning Flow backtest...")
        from app.config import settings as _settings
        from .flow_engine import (
            FlowBacktestConfig,
            build_signal_config,
            run_flow_backtest,
        )
        from .probe_flow_history import INTERVAL_SECONDS, load_probe

        probe = load_probe() or {}
        interval = args.flow_interval or probe.get("finest_interval") or "1h"
        interval_seconds = INTERVAL_SECONDS.get(interval, 3600)
        ack = bool(args.acknowledge_cadence_degradation) or interval_seconds < 3600
        run_cfg = FlowBacktestConfig(
            interval=interval,
            interval_seconds=interval_seconds,
            window_days=args.window,
            pot_tao=_settings.FLOW_POT_TAO,
            slots=_settings.FLOW_SLOTS,
            position_size_pct=_settings.FLOW_POSITION_SIZE_PCT,
            min_pool_depth_tao=_settings.FLOW_MIN_POOL_DEPTH_TAO,
            stop_loss_pct=_settings.FLOW_STOP_LOSS_PCT,
            take_profit_pct=_settings.FLOW_TAKE_PROFIT_PCT,
            trailing_pct=_settings.FLOW_TRAILING_PCT,
            trailing_trigger_pct=_settings.FLOW_TRAILING_TRIGGER_PCT,
            time_soft_hours=_settings.FLOW_TIME_SOFT_HOURS,
            time_hard_hours=_settings.FLOW_TIME_HARD_HOURS,
            cooldown_hours=max(_settings.FLOW_COOLDOWN_TIME_HOURS, interval_seconds / 3600),
            regime_filter_enabled=False,  # analysis wants unfiltered trades
            regime_threshold=_settings.FLOW_REGIME_INDEX_THRESHOLD,
            ema_fast_period=_settings.FLOW_EMA_FAST_PERIOD,
            ema_slow_period=_settings.FLOW_EMA_SLOW_PERIOD,
            ema_confirm=_settings.FLOW_REQUIRE_EMA_CONFIRM,
            cadence_acknowledged=ack,
        )
        sig_cfg = build_signal_config(interval_seconds)
        try:
            flow_result = run_flow_backtest(flow_history, sig_cfg, run_cfg)
            label_trades(flow_result.trades, timeline)
            trades_by_strategy["flow"] = flow_result.trades
            print(f"  flow: {len(flow_result.trades)} trades")
        except Exception as exc:
            print(f"  flow: failed — {exc}")

    if not trades_by_strategy:
        print("ERROR: no trades produced by any strategy — nothing to aggregate.")
        return

    print("\nAggregating per-regime cells...")
    cells = build_cells(
        trades_by_strategy,
        min_trades=args.min_trades,
        enable_edge=args.enable_edge,
        enable_pf=args.enable_pf,
        disable_edge=args.disable_edge,
        disable_pf=args.disable_pf,
    )

    print_per_regime_matrix(cells, window_days=args.window, min_trades=args.min_trades)
    suggestions = suggested_env_lines(cells)
    print_decision_rubric(cells, suggestions)

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    default_csv = Path(f"data/backtest/results/per_regime_edge_{ts}.csv")
    csv_path = Path(args.output) if args.output else default_csv
    save_per_regime_csv(cells, csv_path)
    json_path = csv_path.with_suffix(".json")
    save_per_regime_json(
        cells, json_path,
        window_days=args.window,
        resolved_label_cfg={
            "bucket_hours": resolved.bucket_hours,
            "window_hours": resolved.window_hours,
            "source": resolved.source,
        },
        gate_suggestions=suggestions,
    )
    print(f"\n  wrote {csv_path}")
    print(f"  wrote {json_path}")


def main() -> None:
    if _dispatch_subcommand():
        return

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
