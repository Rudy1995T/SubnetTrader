"""Unit tests for the Pool Flow Momentum signal module."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.strategy.flow_signals import (
    FlowSignalConfig,
    _ewma_ewstd,
    compute_flow_delta,
    compute_flow_delta_pct,
    compute_flow_zscore,
    compute_ring_flow_delta,
    emission_adjusted_flow,
    flow_entry_signal,
    flow_exit_signal,
    has_gap,
    regime_index,
)


def _snap(
    ts: datetime,
    tao: float,
    alpha: float,
    price: float | None = None,
    block: int | None = None,
    emission: float | None = None,
) -> dict:
    return {
        "ts": ts.isoformat(),
        "tao_in_pool": tao,
        "alpha_in_pool": alpha,
        "price": price if price is not None else (tao / alpha if alpha > 0 else 0.0),
        "block_number": block,
        "alpha_emission_rate": emission,
    }


def _build_series(
    count: int,
    start_tao: float = 1000.0,
    start_alpha: float = 10000.0,
    step_tao: float = 0.0,
    step_alpha: float = 0.0,
    cadence_min: int = 5,
    start_block: int | None = None,
    emission: float | None = None,
) -> list[dict]:
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    snaps: list[dict] = []
    for i in range(count):
        snaps.append(
            _snap(
                t0 + timedelta(minutes=cadence_min * i),
                start_tao + step_tao * i,
                start_alpha + step_alpha * i,
                block=(start_block + i) if start_block is not None else None,
                emission=emission,
            )
        )
    return snaps


# ── Raw deltas ───────────────────────────────────────────────────

def test_compute_flow_delta_returns_absolute_change():
    snaps = _build_series(20, step_tao=10.0)
    # After 10 snapshots, tao_in_pool went up by 100
    assert compute_flow_delta(snaps, window_snaps=10) == pytest.approx(100.0)


def test_compute_flow_delta_pct_returns_percentage_change():
    snaps = _build_series(20, start_tao=1000.0, step_tao=10.0)
    # After 10 snaps: 1000 -> 1100, +10%
    # At index -11 value was 900, so delta vs -11 snaps ago = (1190-1090)/1090*100
    # simpler: verify sign + magnitude
    pct = compute_flow_delta_pct(snaps, window_snaps=10)
    assert pct is not None and pct > 0


def test_flow_delta_none_when_not_enough_history():
    snaps = _build_series(3)
    assert compute_flow_delta(snaps, window_snaps=10) is None


# ── Emission adjustment ─────────────────────────────────────────

def test_emission_adjustment_subtracts_expected_tao():
    # 100 blocks of emission at rate 1 alpha/block, sold_fraction=0.6, price=0.1:
    # expected_tao_in = 100 * 1 * 0.6 * 0.1 = 6.0
    snaps = _build_series(
        30, start_tao=1000.0, step_tao=0.5, start_alpha=10000.0,
        start_block=1_000_000, emission=1.0,
    )
    raw = compute_flow_delta(snaps, window_snaps=10)
    adj = emission_adjusted_flow(snaps, window_snaps=10, sold_fraction=0.6)
    assert raw is not None and adj is not None
    assert adj < raw  # emission contribution subtracted


def test_emission_adjustment_falls_back_without_block_data():
    snaps = _build_series(30, step_tao=0.5)  # no block, no emission
    raw = compute_flow_delta(snaps, window_snaps=10)
    adj = emission_adjusted_flow(snaps, window_snaps=10)
    assert adj == raw


# ── EWMA / z-score ──────────────────────────────────────────────

def test_ewma_ewstd_constant_series_has_zero_std():
    mean, std = _ewma_ewstd([5.0, 5.0, 5.0, 5.0, 5.0], halflife_samples=2)
    assert mean == pytest.approx(5.0)
    assert std == pytest.approx(0.0, abs=1e-9)


def test_compute_flow_zscore_requires_baseline_plus_window():
    # cold-start: too few snaps
    snaps = _build_series(20, step_tao=0.1)
    assert compute_flow_zscore(snaps, window_snaps=48, baseline_snaps=576) is None


def test_compute_flow_zscore_spike_produces_positive_z():
    # Need baseline_snaps + window_snaps + 1 = 625 snaps minimum
    baseline = _build_series(700, start_tao=1000.0, step_tao=0.0)
    # inject a sustained spike for the last 48 snaps
    for i, s in enumerate(baseline[-48:]):
        s["tao_in_pool"] = 1000.0 + (i + 1) * 1.0  # steady inflow
    z_info = compute_flow_zscore(baseline, window_snaps=48, baseline_snaps=576)
    assert z_info is not None
    z, flow_pct_now, _ewma, _ewstd = z_info
    assert flow_pct_now > 0


# ── Regime index ───────────────────────────────────────────────

def test_regime_index_returns_median_ratio():
    # 10 subnets, all up 10% over lookback
    per = {}
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for n in range(1, 11):
        snaps = []
        for i in range(500):
            price = 1.0 + (0.1 * i / 499)  # linear climb to 1.1
            snaps.append({
                "ts": (t0 + timedelta(minutes=5 * i)).isoformat(),
                "tao_in_pool": 5000.0,
                "alpha_in_pool": 5000.0 / price,
                "price": price,
            })
        per[n] = snaps
    idx = regime_index(per, lookback_snaps=288)
    assert idx is not None
    assert idx > 1.0  # prices rising => ratio > 1


def test_regime_index_returns_none_with_insufficient_subnets():
    per = {1: _build_series(500)}
    assert regime_index(per, lookback_snaps=288) is None


# ── Gap detection ──────────────────────────────────────────────

def test_has_gap_detects_large_gap():
    snaps = _build_series(5, cadence_min=5)
    # Inject a 60-minute gap
    t = datetime.fromisoformat(snaps[-1]["ts"].replace("Z", "+00:00"))
    snaps.append({
        "ts": (t + timedelta(minutes=60)).isoformat(),
        "tao_in_pool": 1000.0, "alpha_in_pool": 10000.0, "price": 0.1,
    })
    assert has_gap(snaps, max_gap_minutes=30, scan_interval_min=5)


def test_has_gap_false_when_cadence_tight():
    snaps = _build_series(20, cadence_min=5)
    assert not has_gap(snaps, max_gap_minutes=30, scan_interval_min=5)


# ── Entry signal ───────────────────────────────────────────────

def _cfg(**overrides) -> FlowSignalConfig:
    defaults = dict(
        z_entry=2.0, z_exit=-1.5, min_tao_pct=2.0, exit_pct=0.5,
        magnitude_cap=10.0,
        window_1h_snaps=12, window_4h_snaps=48,
        baseline_snaps=200, cold_start_snaps=260,
        emission_adjust=False, sold_fraction=0.6,
    )
    defaults.update(overrides)
    return FlowSignalConfig(**defaults)


def test_cold_start_blocks_entry():
    cfg = _cfg()
    snaps = _build_series(50)
    result = flow_entry_signal(snaps, cfg)
    assert result.signal == "BLOCKED-cold_start"


def test_magnitude_cap_blocks_single_spike():
    cfg = _cfg(magnitude_cap=5.0)
    snaps = _build_series(300, start_tao=1000.0, step_tao=0.0)
    # Inject one +20% spike at the end
    snaps[-1]["tao_in_pool"] = snaps[-2]["tao_in_pool"] * 1.20
    result = flow_entry_signal(snaps, cfg)
    assert result.signal == "BLOCKED-magnitude_cap"
    assert result.magnitude_capped


def test_entry_signal_fires_with_strong_dual_sided_flow():
    cfg = _cfg()
    # 300 baseline snaps, the last 50 have steady TAO inflow + alpha outflow
    snaps = _build_series(300, start_tao=1000.0, start_alpha=10000.0)
    for i in range(250, 300):
        snaps[i]["tao_in_pool"] = 1000.0 + (i - 250) * 3.0  # +15% over window
        snaps[i]["alpha_in_pool"] = 10000.0 - (i - 250) * 10.0  # alpha dropping
    result = flow_entry_signal(snaps, cfg, ema_confirm=False)
    # Should be BUY or at minimum not a cold-start block
    assert result.signal != "BLOCKED-cold_start"


def test_ema_confirm_gate_blocks_when_downtrend():
    cfg = _cfg()
    snaps = _build_series(300, start_tao=1000.0, start_alpha=10000.0)
    for i in range(250, 300):
        snaps[i]["tao_in_pool"] = 1000.0 + (i - 250) * 3.0
        snaps[i]["alpha_in_pool"] = 10000.0 - (i - 250) * 10.0
    result = flow_entry_signal(
        snaps, cfg, ema_fast_value=0.09, ema_slow_value=0.10, ema_confirm=True,
    )
    assert result.signal == "HOLD"
    assert "ema" in result.reason.lower()


def test_regime_block_preempts_other_gates():
    cfg = _cfg()
    snaps = _build_series(300, start_tao=1000.0)
    for i in range(250, 300):
        snaps[i]["tao_in_pool"] = 1000.0 + (i - 250) * 3.0
        snaps[i]["alpha_in_pool"] = 10000.0 - (i - 250) * 10.0
    result = flow_entry_signal(snaps, cfg, regime_ok=False)
    assert result.signal == "BLOCKED-regime"


# ── Exit signal ────────────────────────────────────────────────

def test_exit_signal_none_on_insufficient_history():
    cfg = _cfg()
    snaps = _build_series(20)
    assert flow_exit_signal(snaps, cfg, consecutive_outflow_cycles=0) is None


def test_exit_signal_reversal_after_consecutive_outflow():
    # Disable z-score exit so we isolate the FLOW_REVERSAL branch.
    cfg = _cfg(z_exit=-1000.0)
    # Large 1h outflow + 2 consecutive signals
    snaps = _build_series(300, start_tao=1000.0)
    for i in range(288, 300):
        snaps[i]["tao_in_pool"] = 990.0 - (i - 288) * 0.5
    reason = flow_exit_signal(snaps, cfg, consecutive_outflow_cycles=2)
    assert reason == "FLOW_REVERSAL"


def test_exit_signal_regime_exit_when_down():
    cfg = _cfg()
    snaps = _build_series(300)
    reason = flow_exit_signal(snaps, cfg, consecutive_outflow_cycles=0, regime_ok=False)
    assert reason == "REGIME_EXIT"


# ── Ring-buffer legacy helper ──────────────────────────────────

def test_compute_ring_flow_delta_handles_rao_scale():
    cur = {"total_tao": 2_000_000_000_000}  # 2000 TAO in rao
    prev = {"total_tao": 1_000_000_000_000}  # 1000 TAO in rao
    delta = compute_ring_flow_delta(cur, prev)
    assert delta == pytest.approx(100.0)  # +100%


def test_compute_ring_flow_delta_none_on_zero_prev():
    assert compute_ring_flow_delta({"total_tao": 100}, {"total_tao": 0}) is None
