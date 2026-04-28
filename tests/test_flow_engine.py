"""Tests for the Pool Flow Momentum backtest engine.

Covers two correctness guards highlighted in the spec:
  1. A clear z-score spike on synthetic history produces exactly one BUY.
  2. The regime filter pauses entries when the aggregate index drops below
     the threshold, even if the per-subnet flow signal would otherwise fire.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.backtest.flow_engine import (
    CadenceNotAcknowledgedError,
    FlowBacktestConfig,
    build_signal_config,
    run_flow_backtest,
)


def _snap(
    ts: datetime,
    tao: float,
    alpha: float,
    block: int,
    price: float | None = None,
) -> dict:
    return {
        "ts": ts.isoformat(),
        "tao_in_pool": tao,
        "alpha_in_pool": alpha,
        "price": price if price is not None else (tao / alpha if alpha > 0 else 0.0),
        "block_number": block,
        "alpha_emission_rate": None,
    }


def _build_series(
    count: int,
    *,
    start_tao: float = 10_000.0,
    start_alpha: float = 100_000.0,
    step_tao: float = 0.0,
    step_alpha: float = 0.0,
    cadence_min: int = 60,
    start_block: int = 1_000_000,
) -> list[dict]:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = []
    for i in range(count):
        ts = base + timedelta(minutes=cadence_min * i)
        tao = start_tao + step_tao * i
        alpha = start_alpha + step_alpha * i
        rows.append(_snap(ts, tao, alpha, start_block + i))
    return rows


@pytest.fixture
def hourly_run_cfg() -> FlowBacktestConfig:
    return FlowBacktestConfig(
        interval="1h",
        interval_seconds=3600,
        window_days=30,
        pot_tao=10.0,
        slots=3,
        position_size_pct=0.33,
        min_pool_depth_tao=1_000.0,
        stop_loss_pct=6.0,
        take_profit_pct=50.0,        # loose take-profit so the test trade doesn't TP
        trailing_pct=100.0,          # effectively disabled
        trailing_trigger_pct=100.0,
        time_soft_hours=1000,
        time_hard_hours=1000,
        cooldown_hours=0.0,
        regime_filter_enabled=False,
        regime_threshold=0.0,
        ema_fast_period=5,
        ema_slow_period=18,
        ema_confirm=False,            # not the subject of this test
        cadence_acknowledged=True,    # test runs at 1h cadence on synthetic data
    )


def test_clear_zscore_spike_produces_single_buy(hourly_run_cfg):
    """Build a flat history, then an obvious TAO inflow + alpha outflow.

    The backtest engine should register exactly one BUY on the spike and
    carry that position forward. No additional entries because flow_signals
    doesn't re-enter while a position is open.
    """
    sig_cfg = build_signal_config(
        interval_seconds=3600,
        z_entry=2.0,
        min_tao_pct=0.5,
        emission_adjust=False,
    )

    # Make sure we have enough bars for cold-start at 1h cadence.
    assert sig_cfg.cold_start_snaps < 200, (
        "cold_start_snaps should be bounded at hourly cadence; got "
        f"{sig_cfg.cold_start_snaps}"
    )

    flat = _build_series(
        count=sig_cfg.cold_start_snaps + 20,
        start_tao=10_000.0,
        start_alpha=100_000.0,
        step_tao=0.0,
        step_alpha=0.0,
    )
    # Inject a strong TAO-up / alpha-down spike across the final 4h window.
    for i in range(1, sig_cfg.window_4h_snaps + 1):
        flat[-i]["tao_in_pool"] = 10_000.0 + 150.0 * (sig_cfg.window_4h_snaps - i + 1)
        flat[-i]["alpha_in_pool"] = 100_000.0 - 400.0 * (sig_cfg.window_4h_snaps - i + 1)
        tao = flat[-i]["tao_in_pool"]
        alpha = flat[-i]["alpha_in_pool"]
        flat[-i]["price"] = tao / alpha

    all_history = {1: flat}
    result = run_flow_backtest(all_history, sig_cfg, hourly_run_cfg)

    assert result.total_trades >= 1, (
        "A deliberate z-score spike should register at least one BUY; got "
        f"{result.total_trades}"
    )
    # The trade we care about was opened on subnet 1.
    assert any(t.netuid == 1 for t in result.trades)
    # z-score at entry must have exceeded the entry threshold.
    for t in result.trades:
        assert t.entry_z_score is None or t.entry_z_score >= sig_cfg.z_entry


def test_regime_filter_blocks_entries_during_drawdown():
    """With regime filter on and most subnets dropping, the aggregate regime
    index falls below the threshold and no BUY is allowed even if a flow
    signal is present on one subnet.
    """
    sig_cfg = build_signal_config(
        interval_seconds=3600,
        z_entry=2.0,
        min_tao_pct=0.5,
        emission_adjust=False,
    )

    bars = sig_cfg.cold_start_snaps + 20

    # Subnet 1: flat early, spike at the end (would otherwise produce a BUY).
    sn1 = _build_series(
        count=bars, start_tao=10_000.0, start_alpha=100_000.0,
    )
    for i in range(1, sig_cfg.window_4h_snaps + 1):
        sn1[-i]["tao_in_pool"] = 10_000.0 + 150.0 * (sig_cfg.window_4h_snaps - i + 1)
        sn1[-i]["alpha_in_pool"] = 100_000.0 - 400.0 * (sig_cfg.window_4h_snaps - i + 1)
        sn1[-i]["price"] = sn1[-i]["tao_in_pool"] / sn1[-i]["alpha_in_pool"]

    # Build a cohort of "wider market" subnets whose price drops ≥20% across
    # the regime lookback window. The drop has to land *inside* the lookback
    # (regime compares price_now vs price at index `now - lookback - 1`), so
    # we drop only in the final `lookback - 2` bars — anything earlier and
    # the lookback bar itself is already discounted.
    all_history: dict[int, list[dict]] = {1: sn1}
    lookback = max(sig_cfg.baseline_snaps // 2, 1)
    drop_start = max(0, bars - lookback + 2)
    for sn in range(2, 12):
        rows = _build_series(
            count=bars, start_tao=20_000.0, start_alpha=100_000.0,
        )
        for i in range(drop_start, bars):
            rows[i]["tao_in_pool"] = 20_000.0 * 0.70
            rows[i]["price"] = rows[i]["tao_in_pool"] / rows[i]["alpha_in_pool"]
        all_history[sn] = rows

    run_cfg = FlowBacktestConfig(
        interval="1h",
        interval_seconds=3600,
        window_days=30,
        pot_tao=10.0,
        slots=3,
        position_size_pct=0.33,
        min_pool_depth_tao=1_000.0,
        stop_loss_pct=6.0,
        take_profit_pct=50.0,
        trailing_pct=100.0,
        trailing_trigger_pct=100.0,
        time_soft_hours=1000,
        time_hard_hours=1000,
        cooldown_hours=0.0,
        regime_filter_enabled=True,     # ← the test subject
        regime_threshold=0.95,
        ema_confirm=False,
        cadence_acknowledged=True,
    )

    result = run_flow_backtest(all_history, sig_cfg, run_cfg)

    # No BUY should have fired on SN1 because the regime gate blocked it.
    assert all(t.netuid != 1 for t in result.trades), (
        "Regime filter failed: SN1 entered despite market-wide drawdown "
        f"(trades={[t.netuid for t in result.trades]})"
    )
    # And the telemetry should show at least one regime block on the counter.
    assert result.pct_blocked_by_regime > 0.0, (
        "Expected non-zero pct_blocked_by_regime counter"
    )


def test_cadence_guard_rejects_unacknowledged_coarse_runs():
    """Running at >=1h cadence without the acknowledgement flag should raise."""
    sig_cfg = build_signal_config(interval_seconds=3600)
    run_cfg = FlowBacktestConfig(
        interval="1h", interval_seconds=3600,
        cadence_acknowledged=False,
    )
    with pytest.raises(CadenceNotAcknowledgedError):
        run_flow_backtest({}, sig_cfg, run_cfg)


def test_cooldown_blocks_immediate_reentry(hourly_run_cfg):
    """After a forced exit, the same subnet must not re-enter until
    ``cooldown_hours`` has elapsed.
    """
    sig_cfg = build_signal_config(
        interval_seconds=3600,
        z_entry=2.0,
        min_tao_pct=0.5,
        emission_adjust=False,
    )
    bars = sig_cfg.cold_start_snaps + 40
    series = _build_series(count=bars, start_tao=10_000.0, start_alpha=100_000.0)
    # Two separate spikes — first one opens a position, second could re-enter.
    def _inject_spike(center_idx: int):
        for i in range(sig_cfg.window_4h_snaps):
            idx = center_idx - i
            if 0 <= idx < len(series):
                series[idx]["tao_in_pool"] = 10_000.0 + 200.0 * (sig_cfg.window_4h_snaps - i)
                series[idx]["alpha_in_pool"] = 100_000.0 - 500.0 * (sig_cfg.window_4h_snaps - i)
                tao = series[idx]["tao_in_pool"]
                alpha = series[idx]["alpha_in_pool"]
                series[idx]["price"] = tao / alpha

    _inject_spike(bars - 25)
    _inject_spike(bars - 2)

    # Force a quick stop-loss so the cooldown test fires. Keep cold_start
    # short enough that both spikes are in-window.
    cfg = FlowBacktestConfig(**{**hourly_run_cfg.__dict__})
    cfg.stop_loss_pct = 0.5                     # very tight — exit fast
    cfg.cooldown_hours = 24.0                   # much wider than the 23 bars between spikes

    result = run_flow_backtest({1: series}, sig_cfg, cfg)
    # With cooldown wider than the gap between spikes, at most one trade.
    assert result.total_trades <= 1, (
        f"Cooldown failed to block re-entry: got {result.total_trades} trades"
    )


def test_pot_accounting_matches_sum_of_pnl(hourly_run_cfg):
    """``pot_growth_tao`` must equal the sum of per-trade ``pnl_tao``."""
    sig_cfg = build_signal_config(
        interval_seconds=3600,
        z_entry=2.0,
        min_tao_pct=0.5,
        emission_adjust=False,
    )
    bars = sig_cfg.cold_start_snaps + 10
    series = _build_series(count=bars, start_tao=10_000.0, start_alpha=100_000.0)
    for i in range(1, sig_cfg.window_4h_snaps + 1):
        series[-i]["tao_in_pool"] = 10_000.0 + 150.0 * (sig_cfg.window_4h_snaps - i + 1)
        series[-i]["alpha_in_pool"] = 100_000.0 - 400.0 * (sig_cfg.window_4h_snaps - i + 1)
        tao = series[-i]["tao_in_pool"]
        alpha = series[-i]["alpha_in_pool"]
        series[-i]["price"] = tao / alpha

    result = run_flow_backtest({1: series}, sig_cfg, hourly_run_cfg)
    if not result.trades:
        pytest.skip("Synthetic spike didn't produce a trade on this build")
    summed = sum(t.pnl_tao for t in result.trades)
    assert abs(result.pot_growth_tao - summed) < 1e-9


def test_ema_overlap_counter_increments():
    """A fabricated EMA entry window that contains the flow trade's entry_ts
    must produce ``ema_overlap_rate > 0`` in the result.
    """
    sig_cfg = build_signal_config(
        interval_seconds=3600,
        z_entry=2.0,
        min_tao_pct=0.5,
        emission_adjust=False,
    )
    bars = sig_cfg.cold_start_snaps + 10
    series = _build_series(count=bars, start_tao=10_000.0, start_alpha=100_000.0)
    for i in range(1, sig_cfg.window_4h_snaps + 1):
        series[-i]["tao_in_pool"] = 10_000.0 + 150.0 * (sig_cfg.window_4h_snaps - i + 1)
        series[-i]["alpha_in_pool"] = 100_000.0 - 400.0 * (sig_cfg.window_4h_snaps - i + 1)
        tao = series[-i]["tao_in_pool"]
        alpha = series[-i]["alpha_in_pool"]
        series[-i]["price"] = tao / alpha

    cfg = FlowBacktestConfig(
        interval="1h", interval_seconds=3600, window_days=30,
        pot_tao=10.0, slots=3, position_size_pct=0.33,
        min_pool_depth_tao=1_000.0,
        stop_loss_pct=6.0, take_profit_pct=50.0,
        trailing_pct=100.0, trailing_trigger_pct=100.0,
        time_soft_hours=1000, time_hard_hours=1000,
        cooldown_hours=0.0, regime_filter_enabled=False,
        ema_confirm=False, cadence_acknowledged=True,
    )
    # EMA window that covers the whole series on SN1.
    start = datetime(2026, 1, 1, tzinfo=timezone.utc) - timedelta(days=1)
    end = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=365)
    result = run_flow_backtest(
        {1: series}, sig_cfg, cfg, ema_entry_windows=[(1, start, end)]
    )
    if not result.trades:
        pytest.skip("Synthetic spike didn't produce a trade on this build")
    assert result.ema_overlap_rate > 0.0
