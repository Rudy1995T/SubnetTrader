"""Tests for app.backtest.flow_data_loader."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.backtest import flow_data_loader as loader


def _raw_row(ts: str, tao: float | None = 1_000_000_000, alpha: float = 1_000_000_000):
    return {
        "timestamp": ts,
        "tao_in_pool": tao,
        "alpha_in_pool": alpha,
        "price": None if (tao is None or alpha == 0) else (tao / alpha),
        "block_number": 1,
    }


def test_normalize_snapshot_drops_rows_missing_core_fields():
    assert loader.normalize_snapshot(_raw_row("2026-01-01T00:00:00Z")) is not None
    # Missing tao_in_pool → drop
    row = _raw_row("2026-01-01T00:00:00Z", tao=None)
    assert loader.normalize_snapshot(row) is None
    # Missing alpha → drop
    row = {"timestamp": "2026-01-01T00:00:00Z", "tao_in_pool": 1, "alpha_in_pool": None}
    assert loader.normalize_snapshot(row) is None
    # Missing ts → drop
    row = _raw_row("2026-01-01T00:00:00Z")
    row.pop("timestamp")
    assert loader.normalize_snapshot(row) is None


def test_cache_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(loader, "FLOW_HISTORY_DIR", tmp_path)
    snaps = [
        {
            "ts": "2026-01-01T00:00:00+00:00",
            "tao_in_pool": 1.0,
            "alpha_in_pool": 2.0,
            "price": 0.5,
            "block_number": 1,
            "alpha_emission_rate": None,
        }
    ]
    loader._save_cache(42, snaps, "1d", gap_fraction=0.05)
    path = tmp_path / "sn42.json"
    assert path.exists()
    blob = json.loads(path.read_text())
    assert blob["netuid"] == 42
    assert blob["interval"] == "1d"
    assert blob["gap_fraction"] == 0.05
    assert blob["snapshots"] == snaps


def test_compute_gap_fraction_respects_interval():
    # Perfectly regular daily series → 0 missing
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    snaps = [
        {"ts": (base + timedelta(days=i)).isoformat(), "tao_in_pool": 1.0}
        for i in range(10)
    ]
    assert loader.compute_gap_fraction(snaps, 86400) == pytest.approx(0.0, abs=0.01)
    # Same span but half the rows → ~50% missing
    sparse = snaps[::2]
    gap = loader.compute_gap_fraction(sparse, 86400)
    assert 0.4 <= gap <= 0.6
    # Tiny series → 0
    assert loader.compute_gap_fraction([snaps[0]], 86400) == 0.0


def test_load_cached_excludes_gappy_series(tmp_path, monkeypatch):
    monkeypatch.setattr(loader, "FLOW_HISTORY_DIR", tmp_path)
    (tmp_path / "sn1.json").write_text(
        json.dumps(
            {
                "netuid": 1,
                "interval": "1d",
                "gap_fraction": 0.02,
                "snapshots": [{"ts": "2026-01-01T00:00:00+00:00", "tao_in_pool": 1, "alpha_in_pool": 1}],
            }
        )
    )
    (tmp_path / "sn2.json").write_text(
        json.dumps(
            {
                "netuid": 2,
                "interval": "1d",
                "gap_fraction": 0.50,
                "snapshots": [{"ts": "2026-01-01T00:00:00+00:00", "tao_in_pool": 1, "alpha_in_pool": 1}],
            }
        )
    )
    result = loader.load_cached_flow_history(max_gap_fraction=0.10)
    assert 1 in result
    assert 2 not in result


def test_load_cached_recomputes_gap_when_missing(tmp_path, monkeypatch):
    """Caches written by older code without ``gap_fraction`` still get
    filtered when ``interval_seconds`` is passed in."""
    monkeypatch.setattr(loader, "FLOW_HISTORY_DIR", tmp_path)
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    snaps = [
        {"ts": (base + timedelta(days=i * 5)).isoformat(), "tao_in_pool": 1}
        for i in range(4)
    ]
    (tmp_path / "sn9.json").write_text(
        json.dumps({"netuid": 9, "interval": "1d", "snapshots": snaps})
    )
    # Sparse series (~80% gap) with interval_seconds provided → excluded.
    result = loader.load_cached_flow_history(
        max_gap_fraction=0.10, interval_seconds=86400
    )
    assert 9 not in result
    # Without interval_seconds the loader can't compute gap and still loads it.
    result_all = loader.load_cached_flow_history(max_gap_fraction=0.10)
    assert 9 in result_all


class _FakeClient:
    def __init__(self, pages: list[list[dict]]):
        self._pages = pages
        self._calls = 0

    async def get(self, *_a, **_kw):  # pragma: no cover - unused
        raise NotImplementedError


@pytest.mark.asyncio
async def test_fetch_subnet_history_stops_on_short_page(monkeypatch):
    """Pagination must stop when a page comes back smaller than PAGE_LIMIT."""
    calls = {"n": 0}

    async def fake_rate_limited_get(_client, _path, _params, _call_times, _rate_limit):
        calls["n"] += 1
        if calls["n"] == 1:
            # Full-page response — paginator should ask for more.
            return {
                "data": [
                    {
                        "timestamp": (
                            datetime(2026, 1, 2, tzinfo=timezone.utc)
                            - timedelta(days=i)
                        ).isoformat(),
                        "tao_in_pool": 1e9,
                        "alpha_in_pool": 1e9,
                        "price": 1.0,
                    }
                    for i in range(loader.PAGE_LIMIT)
                ]
            }
        # Short page — must terminate.
        return {
            "data": [
                {
                    "timestamp": datetime(2025, 1, 1, tzinfo=timezone.utc).isoformat(),
                    "tao_in_pool": 1e9,
                    "alpha_in_pool": 1e9,
                    "price": 1.0,
                }
            ]
        }

    monkeypatch.setattr(loader, "_rate_limited_get", fake_rate_limited_get)
    snaps = await loader.fetch_subnet_history(
        client=None, netuid=1, interval="1d", window_days=400,
        call_times=[], rate_limit=60,
    )
    assert calls["n"] == 2
    # Oldest → newest; no duplicates by ts.
    ts_set = {s["ts"] for s in snaps}
    assert len(ts_set) == len(snaps)
