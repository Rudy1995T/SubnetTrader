"""Unit tests for the per-regime labeller and aggregator."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.backtest.engine import TradeRecord
from app.backtest.per_regime_report import (
    RegimeCell,
    _decide,
    build_cells,
    label_trades,
    suggested_env_lines,
    wilson_lower_bound,
)
from app.backtest.regime_labeler import RegimeTimeline, UNKNOWN


def _ts(delta_hours: float) -> str:
    return (
        datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(hours=delta_hours)
    ).isoformat()


def _mk_trade(pnl_pct: float, hours_offset: float = 24.0) -> TradeRecord:
    return TradeRecord(
        netuid=1,
        entry_bar=0,
        exit_bar=6,
        entry_price=0.01,
        exit_price=0.01 * (1 + pnl_pct / 100.0),
        entry_ts=_ts(hours_offset),
        exit_ts=_ts(hours_offset + 6),
        amount_tao=2.0,
        pnl_pct=pnl_pct,
        pnl_tao=2.0 * pnl_pct / 100.0,
        hold_bars=6,
        hold_hours=6.0,
        exit_reason="TAKE_PROFIT" if pnl_pct > 0 else "STOP_LOSS",
        peak_price=0.01 * (1 + max(pnl_pct, 0.0) / 100.0),
    )


def test_timeline_regime_at_basic_lookup():
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
    tl = RegimeTimeline(
        epochs=[t0, t0 + 3600, t0 + 7200],
        regimes=["DEAD", "TRENDING", "CHOPPY"],
    )
    before = datetime(2025, 12, 30, tzinfo=timezone.utc).isoformat()
    assert tl.regime_at(before) == UNKNOWN
    assert tl.regime_at(_ts(0)) == "DEAD"
    assert tl.regime_at(_ts(1.5)) == "TRENDING"
    assert tl.regime_at(_ts(3)) == "CHOPPY"


def test_label_trades_sets_regime_at_entry():
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
    tl = RegimeTimeline(epochs=[t0], regimes=["TRENDING"])
    trades = [_mk_trade(5.0, hours_offset=24.0)]
    label_trades(trades, tl)
    assert trades[0].regime_at_entry == "TRENDING"


def test_label_trades_pre_warmup_is_unknown():
    t0 = datetime(2026, 2, 1, tzinfo=timezone.utc).timestamp()
    tl = RegimeTimeline(epochs=[t0], regimes=["TRENDING"])
    # Trade entered before the timeline's first label
    trades = [_mk_trade(5.0, hours_offset=-24.0)]
    label_trades(trades, tl)
    assert trades[0].regime_at_entry == UNKNOWN


def test_build_cells_skips_unknown_and_groups_correctly():
    tl = RegimeTimeline(
        epochs=[datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()],
        regimes=["TRENDING"],
    )
    trades: list[TradeRecord] = []
    for _ in range(10):
        trades.append(_mk_trade(6.0, hours_offset=12.0))  # TRENDING
    for _ in range(5):
        trades.append(_mk_trade(-4.0, hours_offset=12.0))  # TRENDING
    # One pre-warmup trade — should be dropped
    trades.append(_mk_trade(100.0, hours_offset=-12.0))
    label_trades(trades, tl)

    cells = build_cells({"ema_A1": trades}, min_trades=10)
    assert len(cells) == 1
    cell = cells[0]
    assert cell.strategy_id == "ema_A1"
    assert cell.regime == "TRENDING"
    assert cell.total_trades == 15
    assert cell.winning_trades == 10
    assert cell.losing_trades == 5
    assert cell.win_rate == pytest.approx(10 / 15 * 100, rel=1e-3)
    assert cell.significant is True


def test_decide_rubric_enable_disable_neutral():
    base = RegimeCell(
        strategy_id="x", regime="TRENDING", total_trades=30,
        expectancy=0.8, profit_factor=1.5, significant=True,
    )
    assert _decide(base, enable_edge=0.5, enable_pf=1.3,
                   disable_edge=-0.2, disable_pf=0.9) == "ENABLE"
    neg = RegimeCell(
        strategy_id="x", regime="CHOPPY", total_trades=30,
        expectancy=-0.4, profit_factor=1.1, significant=True,
    )
    assert _decide(neg, enable_edge=0.5, enable_pf=1.3,
                   disable_edge=-0.2, disable_pf=0.9) == "DISABLE"
    mid = RegimeCell(
        strategy_id="x", regime="DISPERSED", total_trades=30,
        expectancy=0.2, profit_factor=1.1, significant=True,
    )
    assert _decide(mid, enable_edge=0.5, enable_pf=1.3,
                   disable_edge=-0.2, disable_pf=0.9) == "NEUTRAL"
    thin = RegimeCell(
        strategy_id="x", regime="TRENDING", total_trades=3,
        expectancy=2.0, profit_factor=3.0, significant=False,
    )
    assert _decide(thin, enable_edge=0.5, enable_pf=1.3,
                   disable_edge=-0.2, disable_pf=0.9) == "NEUTRAL"


def test_suggested_env_lines_picks_enable_cells():
    cells = [
        RegimeCell(strategy_id="ema_A1", regime="TRENDING", recommendation="ENABLE"),
        RegimeCell(strategy_id="ema_A2", regime="DISPERSED", recommendation="NEUTRAL"),
        RegimeCell(strategy_id="meanrev_F1", regime="CHOPPY", recommendation="ENABLE"),
    ]
    out = suggested_env_lines(cells)
    assert out["REGIME_GATE_EMA"] == "trending"
    assert out["REGIME_GATE_MR"] == "choppy"
    assert out["REGIME_GATE_YIELD"] == "all"
    assert "disabled" in out["REGIME_GATE_FLOW"]


def test_suggested_env_lines_disable_vetoes_same_family():
    cells = [
        # One config enables TRENDING, another in the same family disables it
        RegimeCell(strategy_id="flow", regime="TRENDING", recommendation="DISABLE"),
        RegimeCell(strategy_id="flow", regime="DISPERSED", recommendation="ENABLE"),
    ]
    out = suggested_env_lines(cells)
    assert out["REGIME_GATE_FLOW"] == "dispersed"


def test_wilson_lower_bound_sanity():
    # 100% win rate on 3 samples still gives a wide interval
    lcb = wilson_lower_bound(3, 3)
    assert 30.0 < lcb < 60.0
    # Large sample with 50/50 → close to 50
    lcb2 = wilson_lower_bound(500, 1000)
    assert 45.0 < lcb2 < 50.0
    # Zero wins → 0.0
    assert wilson_lower_bound(0, 10) == 0.0
