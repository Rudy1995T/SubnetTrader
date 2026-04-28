"""
Per-regime aggregation of backtest trades.

Consumes trade records labelled with ``regime_at_entry`` and emits one row
per ``(strategy_id, regime)`` cell with significance tags and an
ENABLE/DISABLE/NEUTRAL recommendation. Reuses the EMA engine's
``_compute_result`` for the metric math so per-regime numbers stay in
lockstep with the aggregate report.
"""
from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .engine import TradeRecord, _compute_result
from .regime_labeler import RegimeTimeline, UNKNOWN

RESULTS_DIR = Path("data/backtest/results")
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

# Significance filter: below this count, recommendation is always NEUTRAL
# regardless of the point estimate — stops the user from chasing a
# 3-trade bucket with a 100% win rate.
DEFAULT_MIN_TRADES_PER_CELL = 20

# Decision rubric thresholds (see spec Phase 5).
DEFAULT_ENABLE_EDGE = 0.5
DEFAULT_ENABLE_PF = 1.3
DEFAULT_DISABLE_EDGE = -0.2
DEFAULT_DISABLE_PF = 0.9

REGIME_ORDER = ("TRENDING", "DISPERSED", "CHOPPY", "DEAD")
# Strategy → env variable for the gate allow-list. Yield is regime-
# agnostic by design, so its row always prints `all`.
STRATEGY_GATE_ENV = {
    "ema": "REGIME_GATE_EMA",
    "flow": "REGIME_GATE_FLOW",
    "mr": "REGIME_GATE_MR",
    "meanrev": "REGIME_GATE_MR",
    "yield": "REGIME_GATE_YIELD",
}


@dataclass
class RegimeCell:
    strategy_id: str
    regime: str
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    win_rate_lcb_95: float = 0.0
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    expectancy: float = 0.0
    profit_factor: float = 0.0
    total_pnl_pct: float = 0.0
    total_pnl_tao: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe_ratio: float = 0.0
    avg_hold_hours: float = 0.0
    significant: bool = False
    recommendation: str = "NEUTRAL"

    def as_csv_row(self) -> dict:
        pf = self.profit_factor if self.profit_factor < 1e6 else None
        return {
            "strategy_id": self.strategy_id,
            "regime": self.regime,
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "win_rate": round(self.win_rate, 2),
            "win_rate_lcb_95": round(self.win_rate_lcb_95, 2),
            "avg_win_pct": round(self.avg_win_pct, 2),
            "avg_loss_pct": round(self.avg_loss_pct, 2),
            "expectancy": round(self.expectancy, 4),
            "profit_factor": round(pf, 4) if pf is not None else None,
            "total_pnl_pct": round(self.total_pnl_pct, 2),
            "total_pnl_tao": round(self.total_pnl_tao, 4),
            "max_drawdown_pct": round(self.max_drawdown_pct, 2),
            "sharpe_ratio": round(self.sharpe_ratio, 4),
            "avg_hold_hours": round(self.avg_hold_hours, 1),
            "significant": self.significant,
            "recommendation": self.recommendation,
        }


# ── Normalisation ────────────────────────────────────────────────────

def _to_trade_record(t: Any) -> TradeRecord:
    """Duck-type a Flow trade record into the shape ``_compute_result``
    expects. TradeRecord is already fine; FlowTradeRecord lacks bar-index
    fields we fill with zeros.
    """
    if isinstance(t, TradeRecord):
        return t
    return TradeRecord(
        netuid=getattr(t, "netuid", 0),
        entry_bar=0,
        exit_bar=0,
        entry_price=float(getattr(t, "entry_price", 0.0) or 0.0),
        exit_price=float(getattr(t, "exit_price", 0.0) or 0.0),
        entry_ts=str(getattr(t, "entry_ts", "")),
        exit_ts=str(getattr(t, "exit_ts", "")),
        amount_tao=float(getattr(t, "amount_tao", 0.0) or 0.0),
        pnl_pct=float(getattr(t, "pnl_pct", 0.0) or 0.0),
        pnl_tao=float(getattr(t, "pnl_tao", 0.0) or 0.0),
        hold_bars=0,
        hold_hours=float(getattr(t, "hold_hours", 0.0) or 0.0),
        exit_reason=str(getattr(t, "exit_reason", "")),
        peak_price=float(getattr(t, "peak_price", 0.0) or 0.0),
        entry_slippage_pct=float(getattr(t, "entry_slippage_pct", 0.0) or 0.0),
        exit_slippage_pct=float(getattr(t, "exit_slippage_pct", 0.0) or 0.0),
        regime_at_entry=str(getattr(t, "regime_at_entry", UNKNOWN)),
    )


def label_trades(trades: Iterable[Any], timeline: RegimeTimeline) -> list[Any]:
    """Attach ``regime_at_entry`` to each trade from the labelled timeline.

    Mutates in place for convenience and returns the same list.
    """
    out: list[Any] = []
    for t in trades:
        t.regime_at_entry = timeline.regime_at(t.entry_ts)
        out.append(t)
    return out


# ── Stats helpers ────────────────────────────────────────────────────

def wilson_lower_bound(wins: int, total: int, z: float = 1.96) -> float:
    """Wilson 95% lower confidence bound on a proportion, returned as %.

    Reference: https://en.wikipedia.org/wiki/Binomial_proportion_confidence_interval#Wilson_score_interval
    """
    if total <= 0:
        return 0.0
    p = wins / total
    denom = 1 + z * z / total
    centre = p + z * z / (2 * total)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total)
    lcb = (centre - margin) / denom
    return max(0.0, min(100.0, lcb * 100.0))


# ── Aggregation ──────────────────────────────────────────────────────

def build_cells(
    trades_by_strategy: dict[str, list[Any]],
    *,
    min_trades: int = DEFAULT_MIN_TRADES_PER_CELL,
    enable_edge: float = DEFAULT_ENABLE_EDGE,
    enable_pf: float = DEFAULT_ENABLE_PF,
    disable_edge: float = DEFAULT_DISABLE_EDGE,
    disable_pf: float = DEFAULT_DISABLE_PF,
) -> list[RegimeCell]:
    """Flatten trades into ``(strategy, regime)`` cells with metrics and tags.

    Trades tagged ``UNKNOWN`` (pre-warmup of the regime timeline) are
    skipped — consistent with spec Phase 2 point 3.
    """
    cells: list[RegimeCell] = []

    for strategy_id, trades in trades_by_strategy.items():
        by_regime: dict[str, list[TradeRecord]] = {}
        for raw in trades:
            tr = _to_trade_record(raw)
            reg = tr.regime_at_entry or UNKNOWN
            if reg == UNKNOWN:
                continue
            by_regime.setdefault(reg, []).append(tr)

        for regime, bucket in by_regime.items():
            agg = _compute_result(
                strategy_id=strategy_id,
                window_days=0,
                trades=bucket,
                subnets=sorted({t.netuid for t in bucket}),
            )
            cell = RegimeCell(
                strategy_id=strategy_id,
                regime=regime,
                total_trades=agg.total_trades,
                winning_trades=agg.winning_trades,
                losing_trades=agg.losing_trades,
                win_rate=agg.win_rate,
                win_rate_lcb_95=wilson_lower_bound(
                    agg.winning_trades, agg.total_trades
                ),
                avg_win_pct=agg.avg_win_pct,
                avg_loss_pct=agg.avg_loss_pct,
                expectancy=agg.expectancy,
                profit_factor=agg.profit_factor,
                total_pnl_pct=agg.total_pnl_pct,
                total_pnl_tao=sum(t.pnl_tao for t in bucket),
                max_drawdown_pct=agg.max_drawdown_pct,
                sharpe_ratio=agg.sharpe_ratio,
                avg_hold_hours=agg.avg_hold_hours,
            )
            cell.significant = cell.total_trades >= min_trades
            cell.recommendation = _decide(
                cell,
                enable_edge=enable_edge,
                enable_pf=enable_pf,
                disable_edge=disable_edge,
                disable_pf=disable_pf,
            )
            cells.append(cell)

    return cells


def _decide(
    cell: RegimeCell,
    *,
    enable_edge: float,
    enable_pf: float,
    disable_edge: float,
    disable_pf: float,
) -> str:
    if not cell.significant:
        return "NEUTRAL"
    pf = cell.profit_factor if cell.profit_factor < 1e6 else float("inf")
    if cell.expectancy <= disable_edge or pf < disable_pf:
        return "DISABLE"
    if cell.expectancy >= enable_edge and pf >= enable_pf:
        return "ENABLE"
    return "NEUTRAL"


def _strategy_family(strategy_id: str) -> str:
    """Collapse a per-config id (``ema_A1``, ``meanrev_F2``, ``flow``…) to
    the gate family (``ema``/``flow``/``mr``/``yield``). Unknown ids map
    to an empty string so they're excluded from suggestions.
    """
    sid = strategy_id.lower()
    for prefix, family in (
        ("ema", "ema"),
        ("flow", "flow"),
        ("meanrev", "mr"),
        ("mr", "mr"),
        ("yield", "yield"),
    ):
        if sid == prefix or sid.startswith(prefix + "_"):
            return family
    return ""


def suggested_env_lines(cells: list[RegimeCell]) -> dict[str, str]:
    """Build the ``REGIME_GATE_*`` allow-lists from cells with
    ``recommendation == ENABLE``. A regime is included in a family's gate
    if *any* config in that family shows an ENABLE cell for it (and no
    config in the same family shows a DISABLE for the same regime — a
    DISABLE anywhere vetoes). Strategies with zero resulting regimes
    fall back to the comment ``# disabled — no regime had edge``.
    """
    enables: dict[str, set[str]] = {}
    disables: dict[str, set[str]] = {}
    for c in cells:
        fam = _strategy_family(c.strategy_id)
        if not fam:
            continue
        reg = c.regime.lower()
        if c.recommendation == "ENABLE":
            enables.setdefault(fam, set()).add(reg)
        elif c.recommendation == "DISABLE":
            disables.setdefault(fam, set()).add(reg)

    out: dict[str, str] = {}
    for strategy in ("ema", "flow", "mr", "yield"):
        env_key = STRATEGY_GATE_ENV.get(strategy)
        if env_key is None:
            continue
        if strategy == "yield":
            out[env_key] = "all"
            continue
        hits = enables.get(strategy, set()) - disables.get(strategy, set())
        out[env_key] = ",".join(sorted(hits)) if hits else "# disabled — no regime had edge"
    return out


# ── Persistence ──────────────────────────────────────────────────────

def save_per_regime_csv(cells: list[RegimeCell], path: Path) -> Path:
    fields = list(RegimeCell(strategy_id="", regime="").as_csv_row().keys())
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for c in sorted(cells, key=lambda x: (x.strategy_id, x.regime)):
            writer.writerow(c.as_csv_row())
    return path


def save_per_regime_json(
    cells: list[RegimeCell],
    path: Path,
    *,
    window_days: int,
    resolved_label_cfg: dict | None = None,
    gate_suggestions: dict[str, str] | None = None,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_days": window_days,
        "resolved_label_cfg": resolved_label_cfg,
        "gate_suggestions": gate_suggestions or {},
        "cells": [c.as_csv_row() for c in sorted(cells, key=lambda x: (x.strategy_id, x.regime))],
    }
    with open(path, "w") as f:
        json.dump(blob, f, indent=2)
    return path


# ── Console rendering (spec Phase 7) ─────────────────────────────────

def print_per_regime_matrix(
    cells: list[RegimeCell],
    *,
    window_days: int | None = None,
    min_trades: int = DEFAULT_MIN_TRADES_PER_CELL,
) -> None:
    """Pretty-print the `{strategy} × {regime}` win-rate matrix.

    Insignificant cells (fewer than ``min_trades``) are tagged with a
    trailing asterisk, as per the spec's example output.
    """
    by_sid: dict[str, dict[str, RegimeCell]] = {}
    for c in cells:
        by_sid.setdefault(c.strategy_id, {})[c.regime] = c
    if not by_sid:
        print("  No per-regime cells to display.")
        return

    strategies = sorted(by_sid.keys())
    title = "PER-REGIME EDGE MATRIX"
    if window_days:
        title += f" (primary window = {window_days}d)"
    sep = "-" * 60

    print(f"\n  {title}")
    print(f"  {sep}")
    header = f"  {'Strategy':<14}" + "".join(f"{r:>14}" for r in REGIME_ORDER)
    print(header)
    print(f"  {sep}")

    for sid in strategies:
        row_cells = by_sid[sid]
        row = f"  {sid:<14}"
        for reg in REGIME_ORDER:
            c = row_cells.get(reg)
            if c is None or c.total_trades == 0:
                row += f"{'--':>14}"
                continue
            marker = "" if c.significant else "*"
            row += f"{c.win_rate:>6.0f}% / {c.total_trades:<3}{marker:<2}".rjust(14)
        print(row)

    print(f"  {sep}")
    print(f"  * = below MIN_TRADES_PER_CELL ({min_trades}), treated as NEUTRAL")


def print_decision_rubric(
    cells: list[RegimeCell],
    suggestions: dict[str, str],
) -> None:
    enable_cells = [c for c in cells if c.recommendation == "ENABLE"]
    disable_cells = [c for c in cells if c.recommendation == "DISABLE"]
    print("\n  DECISION RUBRIC")
    print(f"  ENABLE cells:  {len(enable_cells)}")
    for c in sorted(enable_cells, key=lambda x: (x.strategy_id, x.regime)):
        print(
            f"    {c.strategy_id:<12} {c.regime:<10} "
            f"E={c.expectancy:+.2f}% PF={c.profit_factor:.2f} "
            f"n={c.total_trades}"
        )
    print(f"  DISABLE cells: {len(disable_cells)}")
    for c in sorted(disable_cells, key=lambda x: (x.strategy_id, x.regime)):
        print(
            f"    {c.strategy_id:<12} {c.regime:<10} "
            f"E={c.expectancy:+.2f}% PF={c.profit_factor:.2f} "
            f"n={c.total_trades}"
        )
    print("\n  Suggested .env updates:")
    for k, v in suggestions.items():
        print(f"    {k}={v}")
