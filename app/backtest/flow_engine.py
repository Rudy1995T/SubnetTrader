"""
Pool Flow Momentum backtest engine.

Stateless replay of ``app.strategy.flow_signals`` against cached pool
snapshots. Mirrors the EMA ``engine.py`` shape — signals only ever see
``snapshots[:end_idx]`` so there is no look-ahead leakage. Regime is
recomputed per timestep from the same corpus (not lifted from the latest
live pool_snapshots cache, which would be look-ahead).

Usage:
    python -m app.backtest.flow_engine --window-days 120
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import settings
from app.strategy.ema_signals import compute_ema
from app.strategy.flow_signals import (
    FlowSignalConfig,
    compute_flow_delta_pct,
    flow_entry_signal,
    flow_exit_signal,
    regime_index,
)

from .flow_data_loader import (
    fetch_all_flow_history,
    load_cached_flow_history,
)
from .probe_flow_history import INTERVAL_SECONDS, load_probe
from .regime_labeler import RegimeTimeline
from .slippage import (
    apply_entry_slippage,
    apply_exit_slippage,
    estimate_entry_slippage,
    estimate_exit_slippage,
)

RESULTS_DIR = Path("data/backtest/results")
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

# Per-trade fees (two extrinsics per trade, matching live FEE_RESERVE budget).
FEE_PER_EXTRINSIC_TAO = 0.0003
FEES_PER_TRADE_TAO = 2.0 * FEE_PER_EXTRINSIC_TAO


@dataclass
class FlowBacktestConfig:
    interval: str = "1h"
    interval_seconds: int = 3600
    window_days: int = 120
    pot_tao: float = 10.0
    slots: int = 3
    position_size_pct: float = 0.33
    min_pool_depth_tao: float = 5000.0
    stop_loss_pct: float = 6.0
    take_profit_pct: float = 12.0
    trailing_pct: float = 4.0
    trailing_trigger_pct: float = 3.0
    time_soft_hours: int = 6
    time_hard_hours: int = 24
    cooldown_hours: float = 6.0
    regime_filter_enabled: bool = True
    regime_threshold: float = 0.95
    # Categorical-label gate: only enter when timeline.regime_at(ts) is in this
    # set. None disables the gate. Requires ``regime_timeline`` passed to
    # ``run_flow_backtest``.
    regime_label_gate: frozenset[str] | None = None
    ema_fast_period: int = 5
    ema_slow_period: int = 18
    ema_confirm: bool = True
    cadence_acknowledged: bool = False


@dataclass
class FlowTradeRecord:
    netuid: int
    entry_ts: str
    exit_ts: str
    entry_price: float           # effective (post-slippage)
    exit_price: float            # effective (post-slippage)
    spot_entry_price: float
    spot_exit_price: float
    amount_tao: float
    pnl_pct: float
    pnl_tao: float
    hold_hours: float
    exit_reason: str
    peak_price: float
    entry_slippage_pct: float
    exit_slippage_pct: float
    entry_z_score: float | None
    entry_adj_flow: float | None
    entry_regime_index: float | None
    regime_at_entry: str = "UNKNOWN"


class CadenceNotAcknowledgedError(RuntimeError):
    """Raised when the engine is invoked at >=1h cadence without the operator
    explicitly acknowledging the signal-window degradation."""


@dataclass
class FlowBacktestResult:
    strategy_id: str = "flow"
    window_days: int = 0
    interval: str = ""
    cadence_acknowledged: bool = False
    # Standard metrics
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    expectancy: float = 0.0
    profit_factor: float = 0.0
    total_pnl_pct: float = 0.0
    total_pnl_tao: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe_ratio: float = 0.0
    avg_hold_hours: float = 0.0
    max_concurrent: int = 0
    exit_reasons: dict[str, int] = field(default_factory=dict)
    subnets_traded: list[int] = field(default_factory=list)
    # Flow-specific
    avg_entry_z_score: float = 0.0
    avg_entry_flow_pct: float = 0.0
    pct_blocked_by_regime: float = 0.0
    pct_blocked_by_regime_label: float = 0.0
    pct_blocked_by_magnitude_cap: float = 0.0
    pct_blocked_by_cold_start: float = 0.0
    mean_snapshots_to_first_signal_per_subnet: float = 0.0
    ema_overlap_rate: float = 0.0
    pot_growth_tao: float = 0.0
    pot_growth_pct: float = 0.0
    total_fees_tao: float = 0.0
    # Sweep-identity: which overrides produced this row.
    stop_loss_pct: float = 0.0
    take_profit_pct: float = 0.0
    regime_filter_enabled: bool = False
    z_entry: float = 0.0
    min_tao_pct: float = 0.0
    trades: list[FlowTradeRecord] = field(default_factory=list)


@dataclass
class _OpenPos:
    netuid: int
    entry_idx: int               # index in the subnet's timeline
    entry_ts: datetime
    entry_price: float           # effective
    spot_entry_price: float
    amount_tao: float
    peak_price: float
    entry_slippage_pct: float
    entry_z_score: float | None
    entry_adj_flow: float | None
    entry_regime_index: float | None
    consecutive_outflow: int = 0


# ── Cadence scaling ────────────────────────────────────────────────

def build_signal_config(
    interval_seconds: int,
    *,
    z_entry: float | None = None,
    z_exit: float | None = None,
    min_tao_pct: float | None = None,
    exit_pct: float | None = None,
    magnitude_cap: float | None = None,
    emission_adjust: bool | None = None,
    sold_fraction: float | None = None,
) -> FlowSignalConfig:
    """Scale FlowSignalConfig windows to match the backtest interval.

    The live cadence is ``settings.SCAN_INTERVAL_MIN`` minutes. Historical
    candles are almost always coarser. Convert every window in snapshots by
    the ratio of live cadence / backtest cadence — rounded up, but never
    below 1 snap.
    """
    snaps_per_hour = max(1, int(round(3600 / interval_seconds)))

    # Pull live defaults from settings so the scaling rule is visible here.
    base = FlowSignalConfig()
    cfg = FlowSignalConfig(
        z_entry=z_entry if z_entry is not None else settings.FLOW_Z_ENTRY,
        z_exit=z_exit if z_exit is not None else settings.FLOW_Z_EXIT,
        min_tao_pct=(
            min_tao_pct if min_tao_pct is not None else settings.FLOW_MIN_TAO_PCT
        ),
        exit_pct=exit_pct if exit_pct is not None else settings.FLOW_EXIT_PCT,
        magnitude_cap=(
            magnitude_cap
            if magnitude_cap is not None
            else settings.FLOW_MAGNITUDE_CAP
        ),
        window_1h_snaps=max(1, snaps_per_hour),
        window_4h_snaps=max(1, snaps_per_hour * 4),
        baseline_snaps=max(base.window_4h_snaps, snaps_per_hour * 48),
        cold_start_snaps=max(base.cold_start_snaps // 12, snaps_per_hour * 52),
        emission_adjust=(
            emission_adjust
            if emission_adjust is not None
            else settings.FLOW_EMISSION_ADJUST
        ),
        sold_fraction=(
            sold_fraction
            if sold_fraction is not None
            else settings.FLOW_EMISSION_SOLD_FRACTION
        ),
    )
    # Keep cold_start ≥ baseline + window so flow_entry_signal's cold-start
    # gate always yields after the z-score has its first real sample.
    cfg.cold_start_snaps = max(
        cfg.cold_start_snaps,
        cfg.baseline_snaps + cfg.window_4h_snaps + 2,
    )
    return cfg


# ── Helpers ────────────────────────────────────────────────────────

def _parse_iso(ts: str) -> datetime:
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _subnet_depth_tao(snap: dict) -> float:
    """TAO depth at a given snapshot (already in TAO tokens, not RAO)."""
    return float(snap.get("tao_in_pool", 0) or 0)


# ── Main loop ──────────────────────────────────────────────────────

def run_flow_backtest(
    all_history: dict[int, list[dict]],
    sig_cfg: FlowSignalConfig,
    run_cfg: FlowBacktestConfig,
    ema_entry_windows: list[tuple[int, datetime, datetime]] | None = None,
    regime_timeline: RegimeTimeline | None = None,
) -> FlowBacktestResult:
    """Replay flow signals across all subnets over a shared timeline.

    ``ema_entry_windows`` is an optional list of (netuid, entry_ts, exit_ts)
    from a prior EMA backtest; if provided, we compute ``ema_overlap_rate``
    against it.
    """
    # ── Cadence gate ────────────────────────────────────────────────
    if (
        run_cfg.interval_seconds >= 3600
        and not run_cfg.cadence_acknowledged
    ):
        raise CadenceNotAcknowledgedError(
            f"Backtest cadence {run_cfg.interval} (={run_cfg.interval_seconds}s) "
            "is coarser than the live 5-min scanner; signal windows expand "
            "to days-scale and expectancy is not comparable to live. "
            "Pass run_cfg.cadence_acknowledged=True (CLI: "
            "--acknowledge-cadence-degradation) to proceed."
        )

    # ── Prep per-subnet timestamp arrays ────────────────────────────
    timelines: dict[int, list[datetime]] = {}
    for netuid, snaps in all_history.items():
        timelines[netuid] = [_parse_iso(s["ts"]) for s in snaps]

    # Shared chronological timeline — each entry is (dt, netuid, local_idx)
    events: list[tuple[datetime, int, int]] = []
    for netuid, stamps in timelines.items():
        for idx, dt in enumerate(stamps):
            events.append((dt, netuid, idx))
    events.sort(key=lambda e: (e[0], e[1]))

    open_positions: dict[int, _OpenPos] = {}  # keyed by netuid
    cooldowns: dict[int, datetime] = {}
    trades: list[FlowTradeRecord] = []

    # Counters for block-reason telemetry.
    evals_total = 0
    blocked_regime = 0
    blocked_regime_label = 0
    blocked_magnitude = 0
    blocked_cold_start = 0
    first_signal_snaps: dict[int, int] = {}

    label_gate = run_cfg.regime_label_gate
    use_label_gate = bool(label_gate) and regime_timeline is not None

    # Pot accounting (simple serial — flow only opens one position per
    # subnet at a time).
    available_tao = run_cfg.pot_tao
    total_fees_tao = 0.0

    # Group events by timestamp so regime is computed once per wall-clock
    # step.
    def _same_ts_group(i: int) -> tuple[int, datetime]:
        start_ts = events[i][0]
        j = i
        while j < len(events) and events[j][0] == start_ts:
            j += 1
        return j, start_ts

    i = 0
    while i < len(events):
        j, cur_ts = _same_ts_group(i)
        batch = events[i:j]

        # ── Regime check ────────────────────────────────────────────
        per_netuid_slice: dict[int, list[dict]] = {}
        for netuid, snaps in all_history.items():
            stamps = timelines[netuid]
            # Only include subnets that have data at or before cur_ts.
            pos = _bisect_right(stamps, cur_ts)
            if pos > 0:
                per_netuid_slice[netuid] = snaps[:pos]
        ri = regime_index(
            per_netuid_slice,
            lookback_snaps=sig_cfg.baseline_snaps // 2,
            top_n=50,
        )
        regime_ok = (
            (not run_cfg.regime_filter_enabled)
            or ri is None
            or ri >= run_cfg.regime_threshold
        )

        # ── Exit scan over all open positions ───────────────────────
        to_close: list[tuple[int, str, float, float, float]] = []
        for netuid, pos in list(open_positions.items()):
            snaps_slice = per_netuid_slice.get(netuid, [])
            if not snaps_slice:
                continue
            cur_snap = snaps_slice[-1]
            cur_price = float(cur_snap.get("price", 0) or 0)
            if cur_price <= 0:
                continue
            if cur_price > pos.peak_price:
                pos.peak_price = cur_price

            pnl_pct = (cur_price - pos.entry_price) / pos.entry_price * 100.0
            reason: str | None = None

            # Hard risk stops
            if pnl_pct <= -run_cfg.stop_loss_pct:
                reason = "STOP_LOSS"
            elif pnl_pct >= run_cfg.take_profit_pct:
                reason = "TAKE_PROFIT"
            else:
                hours_held = (
                    (cur_ts - pos.entry_ts).total_seconds() / 3600.0
                )
                # Trailing stop (once triggered)
                peak_pnl = (
                    (pos.peak_price - pos.entry_price)
                    / pos.entry_price * 100.0
                )
                if peak_pnl >= run_cfg.trailing_trigger_pct:
                    drawdown = (
                        (pos.peak_price - cur_price) / pos.peak_price * 100.0
                    )
                    if drawdown >= run_cfg.trailing_pct:
                        reason = "TRAILING_STOP"
                if reason is None and hours_held >= run_cfg.time_hard_hours:
                    reason = "TIME_STOP_HARD"
                elif reason is None and hours_held >= run_cfg.time_soft_hours and pnl_pct < 0:
                    reason = "TIME_STOP_SOFT"
                if reason is None:
                    # Flow-based exit
                    flow_reason = flow_exit_signal(
                        snaps_slice,
                        sig_cfg,
                        pos.consecutive_outflow,
                        regime_ok=regime_ok,
                    )
                    if flow_reason:
                        reason = flow_reason

            # Maintain consecutive outflow counter (1h window)
            tao_pct_1h = compute_flow_delta_pct(
                snaps_slice, sig_cfg.window_1h_snaps
            )
            if tao_pct_1h is not None and tao_pct_1h < -sig_cfg.exit_pct:
                pos.consecutive_outflow += 1
            else:
                pos.consecutive_outflow = 0

            if reason is not None:
                # Compute effective exit w/ slippage from current pool depth.
                depth = _subnet_depth_tao(cur_snap)
                alpha_value_tao = (
                    pos.amount_tao * (cur_price / pos.entry_price)
                )
                exit_slip = (
                    estimate_exit_slippage(alpha_value_tao, depth)
                    if depth > 0
                    else 0.0
                )
                effective_exit = apply_exit_slippage(cur_price, exit_slip)
                to_close.append(
                    (netuid, reason, effective_exit, cur_price, exit_slip)
                )

        for netuid, reason, eff_exit, spot_exit, slip in to_close:
            pos = open_positions.pop(netuid)
            pnl_pct = (eff_exit - pos.entry_price) / pos.entry_price * 100.0
            pnl_tao = pos.amount_tao * pnl_pct / 100.0
            pnl_tao -= FEES_PER_TRADE_TAO
            total_fees_tao += FEES_PER_TRADE_TAO
            hold_hours = (
                (cur_ts - pos.entry_ts).total_seconds() / 3600.0
            )
            trades.append(
                FlowTradeRecord(
                    netuid=netuid,
                    entry_ts=pos.entry_ts.isoformat(),
                    exit_ts=cur_ts.isoformat(),
                    entry_price=pos.entry_price,
                    exit_price=eff_exit,
                    spot_entry_price=pos.spot_entry_price,
                    spot_exit_price=spot_exit,
                    amount_tao=pos.amount_tao,
                    pnl_pct=pnl_pct,
                    pnl_tao=pnl_tao,
                    hold_hours=hold_hours,
                    exit_reason=reason,
                    peak_price=pos.peak_price,
                    entry_slippage_pct=pos.entry_slippage_pct,
                    exit_slippage_pct=slip,
                    entry_z_score=pos.entry_z_score,
                    entry_adj_flow=pos.entry_adj_flow,
                    entry_regime_index=pos.entry_regime_index,
                )
            )
            available_tao += pos.amount_tao + pnl_tao
            cooldowns[netuid] = cur_ts + timedelta(hours=run_cfg.cooldown_hours)

        # ── Entry scan ──────────────────────────────────────────────
        if len(open_positions) >= run_cfg.slots:
            i = j
            continue

        # Deterministic order by netuid for reproducibility.
        for _, netuid, local_idx in sorted(batch, key=lambda e: e[1]):
            if len(open_positions) >= run_cfg.slots:
                break
            if netuid in open_positions:
                continue
            if netuid in cooldowns and cur_ts < cooldowns[netuid]:
                continue

            snaps_slice = per_netuid_slice.get(netuid, [])
            if not snaps_slice:
                continue
            depth = _subnet_depth_tao(snaps_slice[-1])
            if depth < run_cfg.min_pool_depth_tao:
                continue
            spot_price = float(snaps_slice[-1].get("price", 0) or 0)
            if spot_price <= 0:
                continue

            # Optional EMA trend confirmation (skip if we don't have enough
            # history yet — flow cold-start already guards the rest).
            ema_fast_val = ema_slow_val = None
            if run_cfg.ema_confirm:
                prices = [
                    float(s.get("price", 0) or 0)
                    for s in snaps_slice
                    if (s.get("price") or 0) > 0
                ]
                if len(prices) >= run_cfg.ema_slow_period:
                    ef = compute_ema(prices, run_cfg.ema_fast_period)
                    es = compute_ema(prices, run_cfg.ema_slow_period)
                    ema_fast_val = ef[-1]
                    ema_slow_val = es[-1]

            evals_total += 1
            ev = flow_entry_signal(
                snaps_slice,
                sig_cfg,
                ema_fast_value=ema_fast_val,
                ema_slow_value=ema_slow_val,
                ema_confirm=run_cfg.ema_confirm,
                regime_ok=regime_ok,
            )

            if ev.signal.startswith("BLOCKED-"):
                tag = ev.signal.removeprefix("BLOCKED-")
                if tag == "regime":
                    blocked_regime += 1
                elif tag == "magnitude_cap":
                    blocked_magnitude += 1
                elif tag == "cold_start":
                    blocked_cold_start += 1
                continue

            if ev.signal != "BUY":
                continue

            if use_label_gate:
                cur_regime = regime_timeline.regime_at(cur_ts.isoformat())
                if cur_regime not in label_gate:
                    blocked_regime_label += 1
                    continue

            if netuid not in first_signal_snaps:
                first_signal_snaps[netuid] = len(snaps_slice)

            # Size the position
            size_tao = run_cfg.pot_tao * run_cfg.position_size_pct
            if size_tao > available_tao:
                size_tao = available_tao
            if size_tao < 0.1:
                continue

            entry_slip = estimate_entry_slippage(size_tao, depth)
            effective_entry = apply_entry_slippage(spot_price, entry_slip)

            open_positions[netuid] = _OpenPos(
                netuid=netuid,
                entry_idx=local_idx,
                entry_ts=cur_ts,
                entry_price=effective_entry,
                spot_entry_price=spot_price,
                amount_tao=size_tao,
                peak_price=effective_entry,
                entry_slippage_pct=entry_slip,
                entry_z_score=ev.z_score,
                entry_adj_flow=ev.adj_flow_4h,
                entry_regime_index=ri,
                consecutive_outflow=0,
            )
            available_tao -= size_tao

        i = j

    # Mark-to-market close for anything still open.
    if open_positions and events:
        final_ts = events[-1][0]
        for netuid, pos in open_positions.items():
            snaps = all_history.get(netuid, [])
            if not snaps:
                continue
            cur_price = float(snaps[-1].get("price", 0) or 0)
            pnl_pct = (cur_price - pos.entry_price) / pos.entry_price * 100.0
            pnl_tao = pos.amount_tao * pnl_pct / 100.0
            pnl_tao -= FEES_PER_TRADE_TAO
            total_fees_tao += FEES_PER_TRADE_TAO
            hold_hours = (
                (final_ts - pos.entry_ts).total_seconds() / 3600.0
            )
            trades.append(
                FlowTradeRecord(
                    netuid=netuid,
                    entry_ts=pos.entry_ts.isoformat(),
                    exit_ts=final_ts.isoformat(),
                    entry_price=pos.entry_price,
                    exit_price=cur_price,
                    spot_entry_price=pos.spot_entry_price,
                    spot_exit_price=cur_price,
                    amount_tao=pos.amount_tao,
                    pnl_pct=pnl_pct,
                    pnl_tao=pnl_tao,
                    hold_hours=hold_hours,
                    exit_reason="END_OF_DATA",
                    peak_price=pos.peak_price,
                    entry_slippage_pct=pos.entry_slippage_pct,
                    exit_slippage_pct=0.0,
                    entry_z_score=pos.entry_z_score,
                    entry_adj_flow=pos.entry_adj_flow,
                    entry_regime_index=pos.entry_regime_index,
                )
            )

    return _compute_result(
        trades,
        sig_cfg,
        run_cfg,
        evals_total,
        blocked_regime,
        blocked_regime_label,
        blocked_magnitude,
        blocked_cold_start,
        first_signal_snaps,
        ema_entry_windows,
        total_fees_tao,
    )


def _bisect_right(stamps: list[datetime], target: datetime) -> int:
    """Standard bisect_right on a pre-sorted list of datetimes."""
    lo, hi = 0, len(stamps)
    while lo < hi:
        mid = (lo + hi) // 2
        if stamps[mid] <= target:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _compute_result(
    trades: list[FlowTradeRecord],
    sig_cfg: FlowSignalConfig,
    run_cfg: FlowBacktestConfig,
    evals_total: int,
    blocked_regime: int,
    blocked_regime_label: int,
    blocked_magnitude: int,
    blocked_cold_start: int,
    first_signal_snaps: dict[int, int],
    ema_entry_windows: list[tuple[int, datetime, datetime]] | None,
    total_fees_tao: float,
) -> FlowBacktestResult:
    result = FlowBacktestResult(
        window_days=run_cfg.window_days,
        interval=run_cfg.interval,
        cadence_acknowledged=run_cfg.cadence_acknowledged,
        stop_loss_pct=run_cfg.stop_loss_pct,
        take_profit_pct=run_cfg.take_profit_pct,
        regime_filter_enabled=run_cfg.regime_filter_enabled,
        z_entry=sig_cfg.z_entry,
        min_tao_pct=sig_cfg.min_tao_pct,
        trades=trades,
        total_fees_tao=total_fees_tao,
    )

    subnets = sorted({t.netuid for t in trades})
    result.subnets_traded = subnets

    if evals_total > 0:
        result.pct_blocked_by_regime = (
            blocked_regime / evals_total * 100.0
        )
        result.pct_blocked_by_regime_label = (
            blocked_regime_label / evals_total * 100.0
        )
        result.pct_blocked_by_magnitude_cap = (
            blocked_magnitude / evals_total * 100.0
        )
        result.pct_blocked_by_cold_start = (
            blocked_cold_start / evals_total * 100.0
        )
    if first_signal_snaps:
        result.mean_snapshots_to_first_signal_per_subnet = (
            sum(first_signal_snaps.values()) / len(first_signal_snaps)
        )

    if not trades:
        return result

    result.total_trades = len(trades)
    winners = [t for t in trades if t.pnl_tao > 0]
    losers = [t for t in trades if t.pnl_tao <= 0]
    result.winning_trades = len(winners)
    result.losing_trades = len(losers)
    result.win_rate = len(winners) / len(trades) * 100.0
    result.avg_win_pct = (
        sum(t.pnl_pct for t in winners) / len(winners) if winners else 0.0
    )
    result.avg_loss_pct = (
        sum(t.pnl_pct for t in losers) / len(losers) if losers else 0.0
    )
    wr = result.win_rate / 100.0
    result.expectancy = (
        wr * result.avg_win_pct - (1 - wr) * abs(result.avg_loss_pct)
    )

    gross_wins = sum(t.pnl_tao for t in winners)
    gross_losses = abs(sum(t.pnl_tao for t in losers))
    result.profit_factor = (
        gross_wins / gross_losses if gross_losses > 0 else float("inf")
    )

    total_pnl_tao = sum(t.pnl_tao for t in trades)
    result.total_pnl_tao = total_pnl_tao
    result.total_pnl_pct = (
        total_pnl_tao / run_cfg.pot_tao * 100.0 if run_cfg.pot_tao > 0 else 0.0
    )
    result.pot_growth_tao = total_pnl_tao
    result.pot_growth_pct = result.total_pnl_pct

    # Max drawdown on equity curve (sorted by exit ts)
    sorted_trades = sorted(trades, key=lambda t: t.exit_ts)
    equity = 0.0
    peak = 0.0
    dd = 0.0
    for t in sorted_trades:
        equity += t.pnl_tao
        peak = max(peak, equity)
        dd = max(dd, peak - equity)
    result.max_drawdown_pct = (
        dd / run_cfg.pot_tao * 100.0 if run_cfg.pot_tao > 0 else 0.0
    )

    # Sharpe (trade-level, annualized)
    returns = [t.pnl_pct for t in trades]
    if len(returns) >= 2:
        mean_r = sum(returns) / len(returns)
        var_r = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
        std_r = math.sqrt(var_r) if var_r > 0 else 1.0
        avg_hold_h = (
            sum(t.hold_hours for t in trades) / len(trades)
        )
        trades_per_year = 8760 / avg_hold_h if avg_hold_h > 0 else 1
        result.sharpe_ratio = (
            (mean_r / std_r) * math.sqrt(trades_per_year)
        )

    result.avg_hold_hours = sum(t.hold_hours for t in trades) / len(trades)

    # Max concurrent
    events: list[tuple[str, int]] = []
    for t in trades:
        events.append((t.entry_ts, 1))
        events.append((t.exit_ts, -1))
    events.sort()
    cur = 0
    maxc = 0
    for _, delta in events:
        cur += delta
        maxc = max(maxc, cur)
    result.max_concurrent = maxc

    # Exit reasons
    reasons: dict[str, int] = {}
    for t in trades:
        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
    result.exit_reasons = reasons

    # Flow-specific entry stats
    zs = [t.entry_z_score for t in trades if t.entry_z_score is not None]
    result.avg_entry_z_score = sum(zs) / len(zs) if zs else 0.0
    flows = [
        t.entry_adj_flow
        for t in trades
        if t.entry_adj_flow is not None
    ]
    result.avg_entry_flow_pct = (
        sum(flows) / len(flows) if flows else 0.0
    )

    # EMA overlap (if prior EMA backtest windows supplied)
    if ema_entry_windows:
        overlap = 0
        # Index EMA windows by netuid for O(n·k) where k is small per netuid.
        by_sn: dict[int, list[tuple[datetime, datetime]]] = {}
        for sn, e_in, e_out in ema_entry_windows:
            by_sn.setdefault(sn, []).append((e_in, e_out))
        for t in trades:
            entry_dt = _parse_iso(t.entry_ts)
            for a, b in by_sn.get(t.netuid, []):
                if a <= entry_dt <= b:
                    overlap += 1
                    break
        result.ema_overlap_rate = overlap / len(trades) * 100.0

    return result


# ── CLI ────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m app.backtest.flow_engine",
        description="Pool Flow Momentum historical backtest",
    )
    p.add_argument("--window-days", type=int, default=120)
    p.add_argument("--interval", type=str, default=None,
                   help="Override probe finest interval (e.g. 1h, 4h)")
    p.add_argument("--pot-tao", type=float, default=None)
    p.add_argument("--slots", type=int, default=None)
    p.add_argument("--z-entry", type=float, default=None)
    p.add_argument("--z-exit", type=float, default=None)
    p.add_argument("--min-tao-pct", type=float, default=None)
    p.add_argument("--emission-adjust", type=str, default=None,
                   choices=["true", "false"])
    p.add_argument("--regime-filter", type=str, default=None,
                   choices=["true", "false"])
    p.add_argument("--stop-loss-pct", type=float, default=None)
    p.add_argument("--take-profit-pct", type=float, default=None)
    p.add_argument("--time-hard-hours", type=int, default=None)
    p.add_argument("--fetch-only", action="store_true")
    p.add_argument("--force-refresh", action="store_true")
    p.add_argument("--output", type=str, default=None,
                   help="Override default output CSV path")
    p.add_argument(
        "--acknowledge-cadence-degradation",
        action="store_true",
        help=(
            "Required when interval >= 1h. Confirms operator understands that "
            "signal windows expand to days-scale and expectancy is not "
            "comparable to the live 5-min scanner."
        ),
    )
    p.add_argument(
        "--sweep",
        action="store_true",
        help="Run a parameter sweep (grid from strategy-pool-flow-momentum.md).",
    )
    p.add_argument(
        "--quick",
        action="store_true",
        help="Sweep shorthand: one expectancy-maximising config only.",
    )
    p.add_argument(
        "--full",
        action="store_true",
        help="Sweep shorthand: full grid (z x min_tao x stop x tp).",
    )
    p.add_argument(
        "--export", type=str, choices=["csv", "json", "both"], default="both",
        help="Export format (default: both).",
    )
    p.add_argument(
        "--no-ema-overlap",
        action="store_true",
        help="Skip the EMA-overlap wiring even if an EMA result file exists.",
    )
    return p.parse_args()


def _apply_run_cfg_overrides(
    args: argparse.Namespace,
    interval: str,
) -> FlowBacktestConfig:
    interval_seconds = INTERVAL_SECONDS.get(interval, 3600)
    # Cooldown is meaningless when shorter than one interval — scale up so a
    # 6h cooldown at 1d cadence becomes 24h (one interval) at minimum.
    base_cooldown = settings.FLOW_COOLDOWN_TIME_HOURS
    interval_hours = interval_seconds / 3600.0
    cooldown_hours = max(base_cooldown, interval_hours)
    cfg = FlowBacktestConfig(
        interval=interval,
        interval_seconds=interval_seconds,
        window_days=args.window_days,
        pot_tao=args.pot_tao if args.pot_tao is not None else settings.FLOW_POT_TAO,
        slots=args.slots if args.slots is not None else settings.FLOW_SLOTS,
        position_size_pct=settings.FLOW_POSITION_SIZE_PCT,
        min_pool_depth_tao=settings.FLOW_MIN_POOL_DEPTH_TAO,
        stop_loss_pct=(
            args.stop_loss_pct
            if args.stop_loss_pct is not None
            else settings.FLOW_STOP_LOSS_PCT
        ),
        take_profit_pct=(
            args.take_profit_pct
            if args.take_profit_pct is not None
            else settings.FLOW_TAKE_PROFIT_PCT
        ),
        trailing_pct=settings.FLOW_TRAILING_PCT,
        trailing_trigger_pct=settings.FLOW_TRAILING_TRIGGER_PCT,
        time_soft_hours=settings.FLOW_TIME_SOFT_HOURS,
        time_hard_hours=(
            args.time_hard_hours
            if args.time_hard_hours is not None
            else settings.FLOW_TIME_HARD_HOURS
        ),
        cooldown_hours=cooldown_hours,
        regime_filter_enabled=(
            settings.FLOW_REGIME_FILTER_ENABLED
            if args.regime_filter is None
            else args.regime_filter == "true"
        ),
        regime_threshold=settings.FLOW_REGIME_INDEX_THRESHOLD,
        ema_fast_period=settings.FLOW_EMA_FAST_PERIOD,
        ema_slow_period=settings.FLOW_EMA_SLOW_PERIOD,
        ema_confirm=settings.FLOW_REQUIRE_EMA_CONFIRM,
        cadence_acknowledged=bool(
            getattr(args, "acknowledge_cadence_degradation", False)
        ),
    )
    return cfg


def _save_results(
    result: FlowBacktestResult,
    out_path: Path | None = None,
    export: str = "both",
) -> tuple[Path | None, Path | None]:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    if out_path is None:
        csv_path = RESULTS_DIR / f"flow_{ts}.csv"
    else:
        csv_path = Path(out_path)
    json_path = csv_path.with_suffix(".json")

    from .report import save_flow_result_csv, save_flow_result_json
    csv_written = json_written = None
    if export in ("csv", "both"):
        save_flow_result_csv(result, csv_path)
        csv_written = csv_path
    if export in ("json", "both"):
        save_flow_result_json(result, json_path)
        json_written = json_path
    return csv_written, json_written


# ── Sweep grid + runner ───────────────────────────────────────────

def _build_sweep_grid(quick: bool, full: bool) -> list[dict]:
    """Return a list of override dicts for ``run_flow_sweep``.

    Matches the §Parameter sweeps grid in
    ``specs/strategy-pool-flow-momentum.md`` but is trimmed down at 1d
    cadence to keep runtime sane on a Pi 5.
    """
    if quick and not full:
        # A single modest-sensitivity config so `--quick --sweep` is
        # meaningful but doesn't blow up runtime.
        return [
            {
                "z_entry": 2.0,
                "min_tao_pct": 1.0,
                "stop_loss_pct": 6.0,
                "take_profit_pct": 12.0,
                "regime_filter": True,
            },
            {
                "z_entry": 2.5,
                "min_tao_pct": 2.0,
                "stop_loss_pct": 6.0,
                "take_profit_pct": 12.0,
                "regime_filter": True,
            },
            {
                "z_entry": 2.0,
                "min_tao_pct": 1.0,
                "stop_loss_pct": 6.0,
                "take_profit_pct": 12.0,
                "regime_filter": False,
            },
        ]
    grid: list[dict] = []
    for z in (1.5, 2.0, 2.5):
        for min_tao in (1.0, 2.0):
            for stop in (4.0, 6.0, 8.0):
                for tp in (8.0, 12.0):
                    for regime in (True, False):
                        grid.append(
                            {
                                "z_entry": z,
                                "min_tao_pct": min_tao,
                                "stop_loss_pct": stop,
                                "take_profit_pct": tp,
                                "regime_filter": regime,
                            }
                        )
    return grid


def run_flow_sweep(
    all_history: dict[int, list[dict]],
    base_run_cfg: FlowBacktestConfig,
    grid: list[dict],
    ema_entry_windows: list[tuple[int, datetime, datetime]] | None = None,
    base_z_exit: float | None = None,
    progress: bool = True,
) -> list[FlowBacktestResult]:
    """Run ``run_flow_backtest`` once per grid combination.

    Each grid entry is a dict of per-run overrides applied to both
    ``FlowBacktestConfig`` (stop_loss, take_profit, regime_filter) and the
    signal config (z_entry, min_tao_pct). Combinations whose resulting
    FlowSignalConfig is degenerate for the current cadence (baseline bigger
    than available history, etc.) are skipped and return None, which is
    filtered out of the output list.
    """
    results: list[FlowBacktestResult] = []
    total = len(grid)
    for i, overrides in enumerate(grid, 1):
        run_cfg = FlowBacktestConfig(**{**base_run_cfg.__dict__})
        run_cfg.stop_loss_pct = float(overrides.get("stop_loss_pct", run_cfg.stop_loss_pct))
        run_cfg.take_profit_pct = float(overrides.get("take_profit_pct", run_cfg.take_profit_pct))
        run_cfg.regime_filter_enabled = bool(
            overrides.get("regime_filter", run_cfg.regime_filter_enabled)
        )
        sig_cfg = build_signal_config(
            interval_seconds=run_cfg.interval_seconds,
            z_entry=float(overrides.get("z_entry", 2.0)),
            z_exit=base_z_exit,
            min_tao_pct=float(overrides.get("min_tao_pct", 1.0)),
            emission_adjust=False,  # emission_rate not present at 1d
        )
        max_hist = max((len(h) for h in all_history.values()), default=0)
        if max_hist <= sig_cfg.cold_start_snaps:
            if progress:
                print(
                    f"  [{i}/{total}] skip "
                    f"z={overrides['z_entry']} min_tao={overrides['min_tao_pct']} "
                    f"stop={overrides['stop_loss_pct']} tp={overrides['take_profit_pct']} "
                    f"regime={overrides['regime_filter']} "
                    f"(cold_start={sig_cfg.cold_start_snaps} > max_hist={max_hist})"
                )
            continue
        t0 = time.time()
        try:
            r = run_flow_backtest(
                all_history, sig_cfg, run_cfg, ema_entry_windows=ema_entry_windows
            )
        except CadenceNotAcknowledgedError:
            raise
        except Exception as exc:
            if progress:
                print(f"  [{i}/{total}] error: {exc}")
            continue
        elapsed = time.time() - t0
        if progress:
            print(
                f"  [{i}/{total}] z={overrides['z_entry']} "
                f"min_tao={overrides['min_tao_pct']} "
                f"stop={overrides['stop_loss_pct']} "
                f"tp={overrides['take_profit_pct']} "
                f"regime={str(overrides['regime_filter'])[:5]} "
                f"→ trades={r.total_trades} "
                f"WR={r.win_rate:.1f}% E={r.expectancy:+.2f}% "
                f"PF={r.profit_factor:.2f} "
                f"potΔ={r.pot_growth_tao:+.3f} τ "
                f"({elapsed:.1f}s)"
            )
        results.append(r)
    return results


# ── EMA overlap helper ────────────────────────────────────────────

def _load_latest_ema_windows() -> (
    list[tuple[int, datetime, datetime]] | None
):
    """Read the most recent ``backtest_*.json`` from data/backtest/results/
    and extract (netuid, entry_ts, exit_ts) tuples from its embedded trade
    list. Returns None if no file is found or the file has no trades.

    The existing EMA report writer emits trade counts by subnet but not
    raw entry/exit timestamps. For now, when no timestamp list is present
    we fall back to None and the overlap metric stays at 0.0.
    """
    candidates = sorted(
        RESULTS_DIR.glob("backtest_*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        return None
    try:
        with open(candidates[0]) as f:
            blob = json.load(f)
    except Exception:
        return None
    tuples: list[tuple[int, datetime, datetime]] = []
    windows = blob.get("windows", {}) if isinstance(blob, dict) else {}
    for _w, entries in windows.items():
        for entry in entries or []:
            for t in entry.get("trades", []) or []:
                try:
                    sn = int(t["netuid"])
                    e_in = _parse_iso(str(t["entry_ts"]))
                    e_out = _parse_iso(str(t["exit_ts"]))
                    tuples.append((sn, e_in, e_out))
                except (KeyError, ValueError, TypeError):
                    continue
    return tuples or None


def main() -> None:
    args = parse_args()
    probe = load_probe()
    if probe is None:
        print(
            "  [warn] No flow_probe.json found. "
            "Run `python -m app.backtest.probe_flow_history` first "
            "or pass --interval explicitly."
        )
    elif probe.get("cadence_collapsed_to_1d"):
        print(
            "  [warn] flow_probe.json records that Taostats collapses every "
            "interval to 1d — running at finer cadences will still return "
            "1d rows."
        )
    interval = args.interval or (probe or {}).get("finest_interval") or "1h"
    interval_seconds = INTERVAL_SECONDS.get(interval, 3600)

    if args.fetch_only:
        asyncio.run(
            fetch_all_flow_history(
                window_days=args.window_days,
                interval=interval,
                force_refresh=args.force_refresh,
            )
        )
        return

    print("Loading cached flow history...")
    all_history = load_cached_flow_history(interval_seconds=interval_seconds)
    if not all_history or args.force_refresh:
        print("Fetching from Taostats...")
        all_history = asyncio.run(
            fetch_all_flow_history(
                window_days=args.window_days,
                interval=interval,
                force_refresh=args.force_refresh,
            )
        )

    if not all_history:
        print("ERROR: no history available. Run --fetch-only first.")
        return

    base_run_cfg = _apply_run_cfg_overrides(args, interval)
    if base_run_cfg.interval_seconds >= 3600 and not base_run_cfg.cadence_acknowledged:
        print(
            "\n  ERROR: cadence gate — interval is "
            f"{base_run_cfg.interval} (>=1h); signal windows become days-scale "
            "and results are NOT comparable to the live 5-min scanner. "
            "Re-run with --acknowledge-cadence-degradation to proceed."
        )
        return

    ema_windows = None if args.no_ema_overlap else _load_latest_ema_windows()
    if ema_windows:
        print(f"  EMA overlap: loaded {len(ema_windows)} EMA entry windows")
    elif not args.no_ema_overlap:
        print("  EMA overlap: no prior EMA backtest JSON found — skipping")

    if args.sweep or args.quick or args.full:
        # --quick always wins: small grid, even if --sweep is present.
        # --full or bare --sweep → full grid.
        grid = _build_sweep_grid(
            quick=args.quick, full=not args.quick
        )
        print(
            f"\nSweep: {len(grid)} configs, interval={interval} "
            f"window={base_run_cfg.window_days}d pot={base_run_cfg.pot_tao}"
        )
        t0 = time.time()
        results = run_flow_sweep(
            all_history, base_run_cfg, grid, ema_entry_windows=ema_windows
        )
        elapsed = time.time() - t0
        print(f"\n  sweep complete: {len(results)} runs, {elapsed:.1f}s")
        if not results:
            print("  no usable results (all combinations skipped)")
            return

        from .report import (
            print_flow_ranking_table,
            print_flow_exit_breakdown,
            print_flow_subnet_performance,
            save_flow_sweep_csv,
            save_flow_result_json,
        )
        print_flow_ranking_table(results, interval=interval)
        best = max(results, key=lambda r: r.expectancy)
        print_flow_exit_breakdown(best)
        print_flow_subnet_performance(best)

        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        out_base = Path(args.output) if args.output else RESULTS_DIR / f"flow_sweep_{ts}.csv"
        save_flow_sweep_csv(results, out_base)
        print(f"  wrote {out_base}")
        # Also write the best run's trade detail so reviewers have something
        # to crack open without re-running.
        best_json = out_base.with_name(out_base.stem + "_best.json")
        save_flow_result_json(best, best_json)
        print(f"  wrote {best_json}")
        return

    sig_cfg = build_signal_config(
        base_run_cfg.interval_seconds,
        z_entry=args.z_entry,
        z_exit=args.z_exit,
        min_tao_pct=args.min_tao_pct,
        emission_adjust=(
            None if args.emission_adjust is None
            else args.emission_adjust == "true"
        ),
    )

    print(
        f"\nBacktest: interval={interval} window={base_run_cfg.window_days}d "
        f"pot={base_run_cfg.pot_tao} slots={base_run_cfg.slots} "
        f"z_entry={sig_cfg.z_entry} cadence_ack={base_run_cfg.cadence_acknowledged}"
    )
    print(
        f"  signal cfg: window_1h={sig_cfg.window_1h_snaps} "
        f"window_4h={sig_cfg.window_4h_snaps} "
        f"baseline={sig_cfg.baseline_snaps} "
        f"cold_start={sig_cfg.cold_start_snaps} "
        f"emission_adjust={sig_cfg.emission_adjust} "
        f"cooldown={base_run_cfg.cooldown_hours}h"
    )

    t0 = time.time()
    result = run_flow_backtest(
        all_history, sig_cfg, base_run_cfg, ema_entry_windows=ema_windows
    )
    elapsed = time.time() - t0

    print(
        f"\n  trades={result.total_trades} "
        f"WR={result.win_rate:.1f}% "
        f"E={result.expectancy:+.2f}% "
        f"PF={result.profit_factor:.2f} "
        f"maxDD={result.max_drawdown_pct:.1f}% "
        f"potΔ={result.pot_growth_tao:+.3f} τ ({result.pot_growth_pct:+.1f}%)"
    )
    print(
        f"  blocked: regime={result.pct_blocked_by_regime:.1f}% "
        f"mag_cap={result.pct_blocked_by_magnitude_cap:.1f}% "
        f"cold={result.pct_blocked_by_cold_start:.1f}%"
    )
    if result.ema_overlap_rate:
        print(f"  ema_overlap: {result.ema_overlap_rate:.1f}%")
    print(f"  elapsed: {elapsed:.1f}s")

    csv_path, json_path = _save_results(
        result,
        Path(args.output) if args.output else None,
        export=args.export,
    )
    if csv_path is not None:
        print(f"  wrote {csv_path}")
    if json_path is not None:
        print(f"  wrote {json_path}")

    # Console parity with EMA single-run mode.
    from .report import (
        print_flow_exit_breakdown,
        print_flow_subnet_performance,
    )
    print_flow_exit_breakdown(result)
    print_flow_subnet_performance(result)


if __name__ == "__main__":
    main()
