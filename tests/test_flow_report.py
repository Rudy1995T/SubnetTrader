"""Tests for flow-specific reporting writers in app.backtest.report."""
from __future__ import annotations

import csv
import json
from pathlib import Path

from app.backtest.flow_engine import FlowBacktestResult, FlowTradeRecord
from app.backtest.report import (
    FLOW_SPECIFIC_FIELDS,
    FLOW_STANDARD_FIELDS,
    save_flow_result_csv,
    save_flow_result_json,
    save_flow_sweep_csv,
)


def _sample_trade(netuid: int = 1, pnl_tao: float = 0.5) -> FlowTradeRecord:
    return FlowTradeRecord(
        netuid=netuid,
        entry_ts="2026-01-01T00:00:00+00:00",
        exit_ts="2026-01-02T00:00:00+00:00",
        entry_price=1.0,
        exit_price=1.1,
        spot_entry_price=1.0,
        spot_exit_price=1.1,
        amount_tao=2.0,
        pnl_pct=10.0,
        pnl_tao=pnl_tao,
        hold_hours=24.0,
        exit_reason="TAKE_PROFIT",
        peak_price=1.15,
        entry_slippage_pct=0.1,
        exit_slippage_pct=0.2,
        entry_z_score=2.1,
        entry_adj_flow=3.0,
        entry_regime_index=1.0,
    )


def _sample_result(expectancy: float = 0.5) -> FlowBacktestResult:
    r = FlowBacktestResult(
        window_days=120,
        interval="1d",
        cadence_acknowledged=True,
        total_trades=2,
        winning_trades=1,
        losing_trades=1,
        win_rate=50.0,
        expectancy=expectancy,
        profit_factor=1.5,
        total_pnl_tao=0.5,
        stop_loss_pct=6.0,
        take_profit_pct=12.0,
        regime_filter_enabled=True,
        z_entry=2.0,
        min_tao_pct=1.0,
        pot_growth_tao=0.5,
        total_fees_tao=0.0012,
    )
    r.trades = [_sample_trade(pnl_tao=0.75), _sample_trade(netuid=2, pnl_tao=-0.25)]
    r.subnets_traded = [1, 2]
    r.exit_reasons = {"TAKE_PROFIT": 1, "STOP_LOSS": 1}
    return r


def test_save_flow_result_csv_schema(tmp_path):
    out = tmp_path / "flow.csv"
    save_flow_result_csv(_sample_result(), out)
    with open(out) as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames or []
        rows = list(reader)
    assert header == FLOW_STANDARD_FIELDS + FLOW_SPECIFIC_FIELDS
    assert len(rows) == 1
    # Cadence + identity columns self-describe the row.
    assert rows[0]["interval"] == "1d"
    assert rows[0]["cadence_acknowledged"] in {"True", "true"}
    assert rows[0]["stop_loss_pct"] == "6.0"
    assert rows[0]["regime_filter_enabled"] in {"True", "true"}


def test_save_flow_sweep_csv_writes_one_row_per_result(tmp_path):
    results = [_sample_result(expectancy=e) for e in (0.1, 0.3, 0.7)]
    out = tmp_path / "sweep.csv"
    save_flow_sweep_csv(results, out)
    with open(out) as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    assert len(rows) == 3
    expectancies = sorted(float(r["expectancy"]) for r in rows)
    assert expectancies == [0.1, 0.3, 0.7]


def test_save_flow_result_json_round_trips_trades(tmp_path):
    r = _sample_result()
    out = tmp_path / "flow.json"
    save_flow_result_json(r, out)
    blob = json.loads(out.read_text())
    assert blob["total_trades"] == 2
    assert blob["trades"][0]["netuid"] == 1
    assert blob["trades"][0]["pnl_tao"] == 0.75
    assert blob["exit_reasons"] == {"TAKE_PROFIT": 1, "STOP_LOSS": 1}
