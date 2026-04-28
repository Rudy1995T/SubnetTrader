"""
Cross-family backtest comparison.

Loads the most recent backtest_*.json (EMA+meanrev), meanrev_*.json, and
flow_sweep_*.csv artefacts from ``data/backtest/results/`` and prints a
single ranking table for a chosen window, sorted by expectancy.

Usage:
    python -m app.backtest compare --window 30
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from .report import RESULTS_DIR


def _latest(glob: str) -> Path | None:
    candidates = sorted(RESULTS_DIR.glob(glob), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def _load_json_results(path: Path, window: int) -> list[dict]:
    try:
        with open(path) as f:
            blob = json.load(f)
    except Exception:
        return []
    rows = blob.get("windows", {}).get(str(window), []) or []
    out: list[dict] = []
    for r in rows:
        if not r.get("total_trades"):
            continue
        family = "meanrev" if r.get("strategy_type") == "meanrev" else "ema"
        out.append({
            "family": family,
            "strategy_id": r.get("strategy_id", "?"),
            "tag": r.get("tag", ""),
            "total_trades": int(r.get("total_trades") or 0),
            "win_rate": float(r.get("win_rate") or 0.0),
            "expectancy": float(r.get("expectancy") or 0.0),
            "total_pnl_pct": float(r.get("total_pnl_pct") or 0.0),
            "max_drawdown_pct": float(r.get("max_drawdown_pct") or 0.0),
            "sharpe_ratio": float(r.get("sharpe_ratio") or 0.0),
            "profit_factor": r.get("profit_factor"),
            "avg_hold_hours": float(r.get("avg_hold_hours") or 0.0),
            "source": path.name,
        })
    return out


def _load_flow_csv(path: Path, window: int) -> list[dict]:
    rows: list[dict] = []
    try:
        with open(path, newline="") as f:
            reader = csv.DictReader(f)
            for r in reader:
                try:
                    if int(r.get("window_days") or 0) != window:
                        continue
                    if int(r.get("total_trades") or 0) == 0:
                        continue
                except ValueError:
                    continue
                # Derive a compact label from the parameter columns.
                label = (
                    f"z{float(r.get('z_entry') or 0):.1f}"
                    f"/s{float(r.get('stop_loss_pct') or 0):.0f}"
                    f"/t{float(r.get('take_profit_pct') or 0):.0f}"
                    f"/{'R' if r.get('regime_filter_enabled') == 'True' else 'r'}"
                )
                rows.append({
                    "family": "flow",
                    "strategy_id": r.get("strategy_id", "flow"),
                    "tag": label,
                    "total_trades": int(r.get("total_trades") or 0),
                    "win_rate": float(r.get("win_rate") or 0.0),
                    "expectancy": float(r.get("expectancy") or 0.0),
                    "total_pnl_pct": float(r.get("total_pnl_pct") or 0.0),
                    "max_drawdown_pct": float(r.get("max_drawdown_pct") or 0.0),
                    "sharpe_ratio": float(r.get("sharpe_ratio") or 0.0),
                    "profit_factor": r.get("profit_factor") or None,
                    "avg_hold_hours": float(r.get("avg_hold_hours") or 0.0),
                    "source": path.name,
                })
    except Exception:
        return []
    return rows


def _fmt_pct(v: float, width: int = 7) -> str:
    sign = "+" if v > 0 else ""
    return f"{sign}{v:.1f}%".rjust(width)


def _render(rows: list[dict], window: int) -> None:
    if not rows:
        print(f"  No backtest results found for window={window}d.")
        return
    ranked = sorted(rows, key=lambda r: r["expectancy"], reverse=True)
    header = (
        f"{'Rank':>4} {'Family':<7} {'ID':<5} {'Tag':<20} "
        f"{'Trades':>6} {'Win%':>7} {'Expect':>7} {'PnL':>8} "
        f"{'MaxDD':>8} {'Sharpe':>7} {'PF':>6}"
    )
    sep = "-" * len(header)
    print(f"\n  CROSS-FAMILY RANKING (by expectancy) — {window}d window:")
    print(f"  {sep}")
    print(f"  {header}")
    print(f"  {sep}")
    for rank, r in enumerate(ranked, 1):
        pf = r.get("profit_factor")
        pf_s = "-"
        if pf is not None:
            try:
                pfv = float(pf)
                pf_s = f"{pfv:.2f}" if pfv < 100 else "inf"
            except (TypeError, ValueError):
                pf_s = "-"
        tag = (r.get("tag") or "")[:20]
        print(
            f"  {rank:>4} {r['family']:<7} {r['strategy_id']:<5} {tag:<20} "
            f"{r['total_trades']:>6} {_fmt_pct(r['win_rate'])} "
            f"{_fmt_pct(r['expectancy'])} "
            f"{_fmt_pct(r['total_pnl_pct'], 8)} "
            f"{_fmt_pct(-r['max_drawdown_pct'], 8)} "
            f"{r['sharpe_ratio']:>7.2f} {pf_s:>6}"
        )
    print(f"  {sep}")

    sources = sorted({r["source"] for r in rows})
    print(f"  sources: {', '.join(sources)}")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m app.backtest compare",
        description="Cross-family backtest ranking (EMA / meanrev / flow).",
    )
    parser.add_argument(
        "--window", type=int, default=30,
        help="Lookback window in days (default: 30).",
    )
    args = parser.parse_args()
    window = args.window

    rows: list[dict] = []

    ema_json = _latest("backtest_*.json")
    if ema_json:
        rows.extend(_load_json_results(ema_json, window))

    meanrev_json = _latest("meanrev_*.json")
    if meanrev_json:
        rows.extend(_load_json_results(meanrev_json, window))

    flow_csv = _latest("flow_sweep_*.csv")
    if flow_csv:
        rows.extend(_load_flow_csv(flow_csv, window))

    if not rows:
        print(
            "  No backtest artefacts found in "
            f"{RESULTS_DIR}. Run a backtest first."
        )
        sys.exit(0)

    _render(rows, window)


if __name__ == "__main__":
    main()
