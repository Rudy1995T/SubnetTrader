"""
Output formatting for backtest results — terminal tables + JSON export.
"""
from __future__ import annotations

import csv
import json
import io
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .engine import BacktestResult, TradeRecord
from .strategies import BacktestStrategyConfig, STRATEGY_MAP, LOOKBACK_WINDOWS

RESULTS_DIR = Path("data/backtest/results")
RESULTS_DIR.mkdir(parents=True, exist_ok=True)


def _fmt_pct(v: float, width: int = 7) -> str:
    sign = "+" if v > 0 else ""
    return f"{sign}{v:.1f}%".rjust(width)


def _fmt_float(v: float, width: int = 7, decimals: int = 2) -> str:
    return f"{v:.{decimals}f}".rjust(width)


def print_ranking_table(
    results: list[BacktestResult],
    window_days: int,
    production_ids: set[str] | None = None,
) -> None:
    """Print strategy ranking table sorted by expectancy."""
    if not results:
        print("  No results to display.")
        return

    if production_ids is None:
        production_ids = {"A1", "A2"}

    # Sort by expectancy descending
    ranked = sorted(results, key=lambda r: r.expectancy, reverse=True)

    header = (
        f"{'Rank':>4} {'Strategy':<14} {'Trades':>6} "
        f"{'Win%':>7} {'Expect':>7} {'PnL':>8} "
        f"{'MaxDD':>8} {'Sharpe':>7} {'PF':>6}"
    )
    sep = "-" * len(header)

    print(f"\n  STRATEGY RANKING (by expectancy) — {window_days}d window:")
    print(f"  {sep}")
    print(f"  {header}")
    print(f"  {sep}")

    for rank, r in enumerate(ranked, 1):
        tag = STRATEGY_MAP.get(r.strategy_id)
        label = f"{r.strategy_id} {tag.tag}" if tag else r.strategy_id
        if r.strategy_id in production_ids:
            label += "*"
        pf = f"{r.profit_factor:.2f}" if r.profit_factor < 100 else "inf"
        row = (
            f"{rank:>4} {label:<14} {r.total_trades:>6} "
            f"{_fmt_pct(r.win_rate)} {_fmt_pct(r.expectancy)} "
            f"{_fmt_pct(r.total_pnl_pct, 8)} "
            f"{_fmt_pct(-r.max_drawdown_pct, 8)} "
            f"{_fmt_float(r.sharpe_ratio)} {pf:>6}"
        )
        print(f"  {row}")

    print(f"  {sep}")
    if production_ids:
        print("  * = current production config")


def print_exit_breakdown(result: BacktestResult) -> None:
    """Print exit reason breakdown for a single strategy result."""
    if not result.exit_reasons:
        return
    tag = STRATEGY_MAP.get(result.strategy_id)
    label = f"{result.strategy_id} {tag.tag}" if tag else result.strategy_id
    print(f"\n  EXIT REASONS ({label}, {result.window_days}d):")
    total = sum(result.exit_reasons.values())
    for reason, count in sorted(result.exit_reasons.items(), key=lambda x: -x[1]):
        pct = count / total * 100 if total > 0 else 0
        bar = "#" * int(pct / 3)
        print(f"    {reason:<16} {count:>4} ({pct:>5.1f}%)  {bar}")


def print_subnet_performance(result: BacktestResult, top_n: int = 5) -> None:
    """Print top/worst performing subnets."""
    if not result.trades:
        return

    # Group by subnet
    by_sn: dict[int, list[TradeRecord]] = {}
    for t in result.trades:
        by_sn.setdefault(t.netuid, []).append(t)

    sn_avg = {
        sn: sum(t.pnl_pct for t in trades) / len(trades)
        for sn, trades in by_sn.items()
        if trades
    }

    tag = STRATEGY_MAP.get(result.strategy_id)
    label = f"{result.strategy_id} {tag.tag}" if tag else result.strategy_id

    best = sorted(sn_avg.items(), key=lambda x: -x[1])[:top_n]
    worst = sorted(sn_avg.items(), key=lambda x: x[1])[:top_n]

    if best:
        print(f"\n  TOP SUBNETS ({label}, {result.window_days}d):")
        for sn, avg in best:
            n = len(by_sn[sn])
            print(f"    SN{sn:<3}  avg {avg:+.1f}%  ({n} trades)")

    if worst:
        print(f"\n  WORST SUBNETS ({label}, {result.window_days}d):")
        for sn, avg in worst:
            n = len(by_sn[sn])
            print(f"    SN{sn:<3}  avg {avg:+.1f}%  ({n} trades)")


def print_trend_analysis(
    results_by_window: dict[int, list[BacktestResult]],
    strategy_ids: list[str] | None = None,
) -> None:
    """Show how metrics evolve across time windows for selected strategies."""
    if strategy_ids is None:
        strategy_ids = ["A1", "A2"]

    windows = sorted(results_by_window.keys(), reverse=True)
    if not windows:
        return

    print("\n  TREND ANALYSIS (across lookback windows):")
    print("  " + "-" * 70)

    for sid in strategy_ids:
        tag = STRATEGY_MAP.get(sid)
        label = f"{sid} ({tag.tag})" if tag else sid

        # Gather metrics per window
        win_rates = []
        expectancies = []
        for w in windows:
            for r in results_by_window.get(w, []):
                if r.strategy_id == sid:
                    win_rates.append((w, r.win_rate))
                    expectancies.append((w, r.expectancy))
                    break

        if not win_rates:
            continue

        # Win rate trend
        wr_parts = [f"{w}d: {wr:.1f}%" for w, wr in win_rates]
        wr_trend = "IMPROVING ^" if len(win_rates) >= 2 and win_rates[-1][1] > win_rates[0][1] else (
            "DECLINING v" if len(win_rates) >= 2 and win_rates[-1][1] < win_rates[0][1] else "STABLE ="
        )
        print(f"\n  {label} Win Rate:")
        print(f"    {' -> '.join(wr_parts)}")
        print(f"    Trend: {wr_trend}")

        # Expectancy trend
        exp_parts = [f"{w}d: {exp:+.2f}%" for w, exp in expectancies]
        exp_trend = "IMPROVING ^" if len(expectancies) >= 2 and expectancies[-1][1] > expectancies[0][1] else (
            "DECLINING v" if len(expectancies) >= 2 and expectancies[-1][1] < expectancies[0][1] else "STABLE ="
        )
        print(f"  {label} Expectancy:")
        print(f"    {' -> '.join(exp_parts)}")
        print(f"    Trend: {exp_trend}")


def print_full_report(
    results_by_window: dict[int, list[BacktestResult]],
    primary_window: int = 30,
) -> None:
    """Print the complete backtest report."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n{'='*72}")
    print(f"  BACKTEST REPORT — {now}")
    print(f"{'='*72}")

    # Main ranking for primary window
    primary_results = results_by_window.get(primary_window, [])
    if primary_results:
        total_subnets = set()
        for r in primary_results:
            total_subnets.update(r.subnets_traded)
        print(f"\n  Window: {primary_window} days")
        print(f"  Subnets tested: {len(total_subnets)}")
        print_ranking_table(primary_results, primary_window)

        # Find best and production configs
        ranked = sorted(primary_results, key=lambda r: r.expectancy, reverse=True)
        if ranked:
            best = ranked[0]
            print_exit_breakdown(best)
            print_subnet_performance(best)

            # Also show production config details
            for r in ranked:
                if r.strategy_id in ("A1", "A2") and r.strategy_id != best.strategy_id:
                    print_exit_breakdown(r)

    # Ranking tables for other windows
    for w in sorted(results_by_window.keys()):
        if w != primary_window:
            results = results_by_window[w]
            if results:
                print_ranking_table(results, w)

    # Trend analysis
    print_trend_analysis(results_by_window)

    print(f"\n{'='*72}")


def save_results_json(
    results_by_window: dict[int, list[BacktestResult]],
    filename_prefix: str = "backtest",
) -> Path:
    """Save all results to JSON."""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = RESULTS_DIR / f"{filename_prefix}_{timestamp}.json"

    output: dict = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "windows": {},
    }

    for window, results in sorted(results_by_window.items()):
        window_data = []
        for r in results:
            cfg = STRATEGY_MAP.get(r.strategy_id)
            d = {
                "strategy_id": r.strategy_id,
                "tag": cfg.tag if cfg else "",
                "strategy_type": cfg.strategy_type if cfg else "ema",
                "window_days": r.window_days,
                "total_trades": r.total_trades,
                "winning_trades": r.winning_trades,
                "losing_trades": r.losing_trades,
                "win_rate": round(r.win_rate, 2),
                "avg_win_pct": round(r.avg_win_pct, 2),
                "avg_loss_pct": round(r.avg_loss_pct, 2),
                "expectancy": round(r.expectancy, 4),
                "profit_factor": round(r.profit_factor, 4) if r.profit_factor < 1e6 else None,
                "total_pnl_pct": round(r.total_pnl_pct, 2),
                "max_drawdown_pct": round(r.max_drawdown_pct, 2),
                "sharpe_ratio": round(r.sharpe_ratio, 4),
                "avg_hold_hours": round(r.avg_hold_hours, 1),
                "max_concurrent": r.max_concurrent,
                "exit_reasons": r.exit_reasons,
                "subnets_traded": r.subnets_traded,
                "trade_count_by_subnet": _trade_count_by_subnet(r.trades),
            }
            window_data.append(d)
        output["windows"][str(window)] = window_data

    with open(path, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\n  Results saved to: {path}")
    return path


def save_results_csv(
    results_by_window: dict[int, list[BacktestResult]],
    filename_prefix: str = "backtest",
) -> Path:
    """Export results as CSV."""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = RESULTS_DIR / f"{filename_prefix}_{timestamp}.csv"

    fields = [
        "window_days", "strategy_id", "total_trades", "winning_trades",
        "losing_trades", "win_rate", "avg_win_pct", "avg_loss_pct",
        "expectancy", "profit_factor", "total_pnl_pct", "max_drawdown_pct",
        "sharpe_ratio", "avg_hold_hours", "max_concurrent",
    ]

    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for window in sorted(results_by_window):
            for r in results_by_window[window]:
                row = {
                    "window_days": r.window_days,
                    "strategy_id": r.strategy_id,
                    "total_trades": r.total_trades,
                    "winning_trades": r.winning_trades,
                    "losing_trades": r.losing_trades,
                    "win_rate": round(r.win_rate, 2),
                    "avg_win_pct": round(r.avg_win_pct, 2),
                    "avg_loss_pct": round(r.avg_loss_pct, 2),
                    "expectancy": round(r.expectancy, 4),
                    "profit_factor": round(r.profit_factor, 4) if r.profit_factor < 1e6 else None,
                    "total_pnl_pct": round(r.total_pnl_pct, 2),
                    "max_drawdown_pct": round(r.max_drawdown_pct, 2),
                    "sharpe_ratio": round(r.sharpe_ratio, 4),
                    "avg_hold_hours": round(r.avg_hold_hours, 1),
                    "max_concurrent": r.max_concurrent,
                }
                writer.writerow(row)

    print(f"  CSV exported to: {path}")
    return path


def _trade_count_by_subnet(trades: list[TradeRecord]) -> dict[int, int]:
    counts: dict[int, int] = {}
    for t in trades:
        counts[t.netuid] = counts.get(t.netuid, 0) + 1
    return counts


# ── Flow Momentum result writers ───────────────────────────────────

FLOW_STANDARD_FIELDS = [
    "strategy_id", "interval", "cadence_acknowledged", "window_days",
    "total_trades",
    "winning_trades", "losing_trades", "win_rate", "avg_win_pct",
    "avg_loss_pct", "expectancy", "profit_factor", "total_pnl_pct",
    "total_pnl_tao", "max_drawdown_pct", "sharpe_ratio", "avg_hold_hours",
    "max_concurrent",
]
FLOW_SPECIFIC_FIELDS = [
    "avg_entry_z_score", "avg_entry_flow_pct", "pct_blocked_by_regime",
    "pct_blocked_by_magnitude_cap", "pct_blocked_by_cold_start",
    "mean_snapshots_to_first_signal_per_subnet", "ema_overlap_rate",
    "pot_growth_tao", "pot_growth_pct", "total_fees_tao",
    "z_entry", "min_tao_pct",
    "stop_loss_pct", "take_profit_pct", "regime_filter_enabled",
]


def _flow_row(result) -> dict:
    """Flatten a FlowBacktestResult into one CSV/JSON row."""
    pf = result.profit_factor if result.profit_factor < 1e6 else None
    return {
        "strategy_id": result.strategy_id,
        "interval": result.interval,
        "cadence_acknowledged": getattr(result, "cadence_acknowledged", False),
        "window_days": result.window_days,
        "total_trades": result.total_trades,
        "winning_trades": result.winning_trades,
        "losing_trades": result.losing_trades,
        "win_rate": round(result.win_rate, 2),
        "avg_win_pct": round(result.avg_win_pct, 2),
        "avg_loss_pct": round(result.avg_loss_pct, 2),
        "expectancy": round(result.expectancy, 4),
        "profit_factor": round(pf, 4) if pf is not None else None,
        "total_pnl_pct": round(result.total_pnl_pct, 2),
        "total_pnl_tao": round(result.total_pnl_tao, 4),
        "max_drawdown_pct": round(result.max_drawdown_pct, 2),
        "sharpe_ratio": round(result.sharpe_ratio, 4),
        "avg_hold_hours": round(result.avg_hold_hours, 1),
        "max_concurrent": result.max_concurrent,
        "avg_entry_z_score": round(result.avg_entry_z_score, 3),
        "avg_entry_flow_pct": round(result.avg_entry_flow_pct, 3),
        "pct_blocked_by_regime": round(result.pct_blocked_by_regime, 2),
        "pct_blocked_by_magnitude_cap": round(
            result.pct_blocked_by_magnitude_cap, 2
        ),
        "pct_blocked_by_cold_start": round(
            result.pct_blocked_by_cold_start, 2
        ),
        "mean_snapshots_to_first_signal_per_subnet": round(
            result.mean_snapshots_to_first_signal_per_subnet, 1
        ),
        "ema_overlap_rate": round(result.ema_overlap_rate, 2),
        "pot_growth_tao": round(result.pot_growth_tao, 4),
        "pot_growth_pct": round(result.pot_growth_pct, 2),
        "total_fees_tao": round(result.total_fees_tao, 4),
        "z_entry": round(getattr(result, "z_entry", 0.0) or 0.0, 2),
        "min_tao_pct": round(getattr(result, "min_tao_pct", 0.0) or 0.0, 2),
        "stop_loss_pct": round(getattr(result, "stop_loss_pct", 0.0) or 0.0, 2),
        "take_profit_pct": round(getattr(result, "take_profit_pct", 0.0) or 0.0, 2),
        "regime_filter_enabled": bool(
            getattr(result, "regime_filter_enabled", False)
        ),
    }


def save_flow_result_csv(result, path: Path) -> Path:
    """Write one flow backtest result as a single-row CSV."""
    fields = FLOW_STANDARD_FIELDS + FLOW_SPECIFIC_FIELDS
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerow(_flow_row(result))
    return path


def save_flow_result_json(result, path: Path) -> Path:
    """Write a flow backtest result as JSON (aggregate + trades + exit mix)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    row = _flow_row(result)
    row["timestamp"] = datetime.now(timezone.utc).isoformat()
    row["exit_reasons"] = result.exit_reasons
    row["subnets_traded"] = result.subnets_traded
    row["trades"] = [
        {
            "netuid": t.netuid,
            "entry_ts": t.entry_ts,
            "exit_ts": t.exit_ts,
            "entry_price": round(t.entry_price, 8),
            "exit_price": round(t.exit_price, 8),
            "amount_tao": round(t.amount_tao, 4),
            "pnl_pct": round(t.pnl_pct, 2),
            "pnl_tao": round(t.pnl_tao, 4),
            "hold_hours": round(t.hold_hours, 1),
            "exit_reason": t.exit_reason,
            "entry_z_score": (
                round(t.entry_z_score, 3) if t.entry_z_score is not None else None
            ),
            "entry_adj_flow": (
                round(t.entry_adj_flow, 3) if t.entry_adj_flow is not None else None
            ),
            "entry_regime_index": (
                round(t.entry_regime_index, 4)
                if t.entry_regime_index is not None else None
            ),
            "entry_slippage_pct": round(t.entry_slippage_pct, 3),
            "exit_slippage_pct": round(t.exit_slippage_pct, 3),
        }
        for t in result.trades
    ]
    with open(path, "w") as f:
        json.dump(row, f, indent=2)
    return path


def save_flow_sweep_csv(results: list, path: Path) -> Path:
    """Write many flow backtest results as a sweep CSV (one row per result)."""
    fields = FLOW_STANDARD_FIELDS + FLOW_SPECIFIC_FIELDS
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for r in results:
            writer.writerow(_flow_row(r))
    return path


# ── Flow console reports ───────────────────────────────────────────

def print_flow_ranking_table(results: list, interval: str = "") -> None:
    """Mirror of ``print_ranking_table`` for FlowBacktestResult rows.

    Sorted by expectancy descending. Identity columns (z_entry, min_tao,
    stop, tp, regime) make the table readable standalone without cross-
    referencing the sweep CSV.
    """
    if not results:
        print("  No flow results to display.")
        return

    ranked = sorted(results, key=lambda r: r.expectancy, reverse=True)

    header = (
        f"{'Rank':>4} {'z':>4} {'minTao':>7} {'stop':>5} {'tp':>5} "
        f"{'reg':>4} {'Trd':>4} {'WR%':>6} {'E%':>7} {'PF':>6} "
        f"{'potΔτ':>8} {'MaxDD%':>7}"
    )
    sep = "-" * len(header)
    window = getattr(ranked[0], "window_days", None)
    title_parts = ["FLOW STRATEGY RANKING (by expectancy)"]
    if interval:
        title_parts.append(f"interval={interval}")
    if window:
        title_parts.append(f"window={window}d")
    print(f"\n  {' — '.join(title_parts)}:")
    print(f"  {sep}")
    print(f"  {header}")
    print(f"  {sep}")

    for rank, r in enumerate(ranked, 1):
        pf = f"{r.profit_factor:.2f}" if r.profit_factor < 100 else "inf"
        row = (
            f"{rank:>4} {getattr(r,'z_entry',0.0):>4.1f} "
            f"{getattr(r,'min_tao_pct',0.0):>7.1f} "
            f"{getattr(r,'stop_loss_pct',0.0):>5.1f} "
            f"{getattr(r,'take_profit_pct',0.0):>5.1f} "
            f"{'Y' if getattr(r,'regime_filter_enabled',False) else 'N':>4} "
            f"{r.total_trades:>4} "
            f"{r.win_rate:>6.1f} {r.expectancy:>+7.2f} "
            f"{pf:>6} {r.pot_growth_tao:>+8.2f} "
            f"{-r.max_drawdown_pct:>+7.1f}"
        )
        print(f"  {row}")

    print(f"  {sep}")


def print_flow_exit_breakdown(result) -> None:
    if not getattr(result, "exit_reasons", None):
        return
    print(
        f"\n  FLOW EXIT REASONS (z={getattr(result,'z_entry',0.0):.1f} "
        f"stop={getattr(result,'stop_loss_pct',0.0):.1f} "
        f"tp={getattr(result,'take_profit_pct',0.0):.1f}):"
    )
    total = sum(result.exit_reasons.values())
    for reason, count in sorted(result.exit_reasons.items(), key=lambda x: -x[1]):
        pct = count / total * 100 if total > 0 else 0
        bar = "#" * int(pct / 3)
        print(f"    {reason:<18} {count:>4} ({pct:>5.1f}%)  {bar}")


def print_flow_subnet_performance(result, top_n: int = 5) -> None:
    trades = getattr(result, "trades", [])
    if not trades:
        return
    by_sn: dict[int, list] = {}
    for t in trades:
        by_sn.setdefault(t.netuid, []).append(t)
    sn_avg = {
        sn: sum(t.pnl_pct for t in v) / len(v) for sn, v in by_sn.items()
    }
    best = sorted(sn_avg.items(), key=lambda x: -x[1])[:top_n]
    worst = sorted(sn_avg.items(), key=lambda x: x[1])[:top_n]

    if best:
        print("\n  TOP FLOW SUBNETS:")
        for sn, avg in best:
            n = len(by_sn[sn])
            pnl_tao = sum(t.pnl_tao for t in by_sn[sn])
            print(
                f"    SN{sn:<3}  avg {avg:+.1f}%  "
                f"pnl {pnl_tao:+.3f} τ  ({n} trades)"
            )
    if worst:
        print("\n  WORST FLOW SUBNETS:")
        for sn, avg in worst:
            n = len(by_sn[sn])
            pnl_tao = sum(t.pnl_tao for t in by_sn[sn])
            print(
                f"    SN{sn:<3}  avg {avg:+.1f}%  "
                f"pnl {pnl_tao:+.3f} τ  ({n} trades)"
            )
