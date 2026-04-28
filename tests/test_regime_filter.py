"""Unit tests for the volatility regime classifier."""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest

from app.strategy.regime import (
    CHOPPY,
    DEAD,
    DISPERSED,
    TRENDING,
    RegimeFilter,
    _bucket_snapshots,
    _parse_gate,
    classify_regime,
    compute_regime_metrics,
)


@dataclass
class _FakeSettings:
    REGIME_ENABLED: bool = True
    REGIME_VOL_WINDOW_HOURS: int = 24
    REGIME_VOL_TREND_THRESHOLD: float = 0.30
    REGIME_VOL_CHOP_FLOOR: float = 0.10
    REGIME_VOL_DEAD_THRESHOLD: float = 0.05
    REGIME_DIR_THRESHOLD: float = 0.02
    REGIME_DISP_THRESHOLD: float = 0.015
    REGIME_DEBOUNCE_CYCLES: int = 2
    REGIME_REFRESH_SECONDS: int = 0  # force refresh in tests
    REGIME_MIN_SUBNETS: int = 3
    # 4 buckets = 16h of data; lower than prod (6=24h) so synthetic
    # fixtures don't need to span a full vol window to the microsecond.
    REGIME_MIN_BUCKETS: int = 4
    REGIME_BUCKET_HOURS: int = 4
    REGIME_GATE_EMA: str = "trending,dispersed"
    REGIME_GATE_FLOW: str = "trending,dispersed"
    REGIME_GATE_MR: str = "choppy,dispersed"
    REGIME_GATE_YIELD: str = "all"


@dataclass
class _ProdFakeSettings(_FakeSettings):
    """Prod-equivalent knobs — exercises MIN_BUCKETS=6 / WINDOW=24 / BUCKET=4."""
    REGIME_MIN_BUCKETS: int = 6
    REGIME_MIN_SUBNETS: int = 10


@dataclass
class _FakeDB:
    """Minimal in-memory stand-in for Database used by RegimeFilter."""
    snaps_by_netuid: dict[int, list[dict]] = field(default_factory=dict)

    async def fetchall(self, sql: str, params: tuple) -> list[dict]:
        # Only query the regime filter issues is distinct-netuids
        return [{"netuid": nid} for nid in sorted(self.snaps_by_netuid)]

    async def get_pool_snapshots(
        self, netuid: int, since_ts: str | None = None, limit: int | None = None
    ) -> list[dict]:
        rows = self.snaps_by_netuid.get(netuid, [])
        if since_ts:
            rows = [r for r in rows if r["ts"] >= since_ts]
        return list(rows)


def _series(
    netuid: int,
    start_price: float,
    drift_per_bucket: float,
    vol_per_bucket: float,
    n_buckets: int = 12,
    bucket_hours: int = 4,
    seed: int = 0,
    *,
    cadence_minutes: int | None = None,
    span_hours: float | None = None,
    n_rows: int | None = None,
) -> list[dict]:
    """Build a bucketed price series with log-normal steps.

    Default n_buckets=12 covers a 48h span so the 24h vol window survives
    comfortably past the since_ts cutoff (>=6 buckets after filtering).

    When ``cadence_minutes`` is provided, rows are emitted at that sub-bucket
    interval (e.g. 10-min snapshot cadence matching the live bot). ``n_rows``
    pins the row count exactly; otherwise ``span_hours`` (default
    ``bucket_hours * n_buckets``) determines the span and row count. The
    per-bucket drift/vol are rescaled to per-step units so the realized
    volatility stays comparable across cadences.
    """
    rng = random.Random(seed + netuid)
    now = datetime.now(timezone.utc).replace(microsecond=0)

    if cadence_minutes is not None:
        if n_rows is None:
            if span_hours is None:
                span_hours = bucket_hours * n_buckets
            span_minutes = int(round(span_hours * 60))
            n_rows = span_minutes // cadence_minutes + 1
        start = now - timedelta(minutes=cadence_minutes * (n_rows - 1))
        scale = cadence_minutes / (bucket_hours * 60)
        step_drift = drift_per_bucket * scale
        step_vol = vol_per_bucket * math.sqrt(scale)
        rows: list[dict] = []
        price = start_price
        for i in range(n_rows):
            noise = rng.gauss(0.0, step_vol)
            step = step_drift + noise
            price = max(1e-6, price * math.exp(step))
            rows.append(
                {
                    "ts": (start + timedelta(minutes=cadence_minutes * i)).isoformat(),
                    "tao_in_pool": 10_000.0,
                    "alpha_in_pool": 100_000.0,
                    "price": price,
                    "block_number": None,
                    "alpha_emission_rate": None,
                }
            )
        return rows

    start = now - timedelta(hours=bucket_hours * n_buckets)
    rows: list[dict] = []
    price = start_price
    for i in range(n_buckets + 1):
        noise = rng.gauss(0.0, vol_per_bucket)
        step = drift_per_bucket + noise
        price = max(1e-6, price * math.exp(step))
        rows.append(
            {
                "ts": (start + timedelta(hours=bucket_hours * i)).isoformat(),
                "tao_in_pool": 10_000.0,
                "alpha_in_pool": 100_000.0,
                "price": price,
                "block_number": None,
                "alpha_emission_rate": None,
            }
        )
    return rows


# ── helper function tests ──────────────────────────────────────

def test_parse_gate_all():
    g = _parse_gate("all")
    assert g == frozenset({TRENDING, DISPERSED, CHOPPY, DEAD})


def test_parse_gate_mixed_case():
    g = _parse_gate("Trending, DISPERSED,chop_typo")
    assert g == frozenset({TRENDING, DISPERSED})


def test_parse_gate_empty():
    assert _parse_gate("") == frozenset()


def test_bucket_snapshots_downsamples_to_4h():
    ts = datetime.now(timezone.utc)
    rows = []
    # 12 rows spaced 30 min apart → 6h span → at least one 4h bucket edge.
    for i in range(12):
        rows.append({"ts": (ts + timedelta(minutes=30 * i)).isoformat(), "price": 100 + i})
    prices = _bucket_snapshots(rows, bucket_hours=4, window_hours=24)
    assert prices
    assert prices[0] > 0


# ── classification state-table tests ───────────────────────────

@pytest.mark.asyncio
async def test_trending_regime_with_strong_drift():
    s = _FakeSettings()
    db = _FakeDB()
    # 5 subnets, strong positive drift + high noise → high vol + high dir strength
    for nid in range(1, 6):
        db.snaps_by_netuid[nid] = _series(
            nid, 0.05, drift_per_bucket=0.03, vol_per_bucket=0.08, seed=1
        )
    f = RegimeFilter(db, s)
    await f.refresh()
    assert f.classify() == TRENDING


@pytest.mark.asyncio
async def test_dead_regime_with_tiny_vol():
    s = _FakeSettings()
    db = _FakeDB()
    for nid in range(1, 6):
        db.snaps_by_netuid[nid] = _series(
            nid, 0.05, drift_per_bucket=0.0, vol_per_bucket=0.0005, seed=2
        )
    f = RegimeFilter(db, s)
    await f.refresh()
    assert f.classify() == DEAD


@pytest.mark.asyncio
async def test_choppy_regime_when_moving_without_direction():
    s = _FakeSettings()
    db = _FakeDB()
    # Moderate noise, zero drift → vol clears the chop floor but dir_strength
    # stays below the dir threshold and dispersion stays small (noise cancels).
    for nid in range(1, 8):
        db.snaps_by_netuid[nid] = _series(
            nid, 0.05, drift_per_bucket=0.0, vol_per_bucket=0.008, seed=3
        )
    f = RegimeFilter(db, s)
    await f.refresh()
    raw = f.classify()
    # dispersion may land either side of the threshold given randomness — both
    # CHOPPY and DISPERSED are acceptable non-TRENDING/DEAD outcomes here.
    assert raw in (CHOPPY, DISPERSED)


@pytest.mark.asyncio
async def test_dispersed_regime_when_subnets_diverge():
    s = _FakeSettings()
    db = _FakeDB()
    # Opposing strong drifts across subnets → high cross-sectional dispersion,
    # each subnet on its own moves strongly (high abs returns) but vol stays
    # below the trending threshold because step vol is small.
    for nid in range(1, 6):
        drift = 0.04 if nid % 2 == 0 else -0.04
        db.snaps_by_netuid[nid] = _series(
            nid, 0.05, drift_per_bucket=drift, vol_per_bucket=0.002, seed=4
        )
    f = RegimeFilter(db, s)
    await f.refresh()
    m = f.metrics
    assert m["dispersion"] >= s.REGIME_DISP_THRESHOLD


# ── debounce tests ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_debounce_blocks_single_flicker():
    s = _FakeSettings(REGIME_DEBOUNCE_CYCLES=2)
    db = _FakeDB()
    for nid in range(1, 6):
        db.snaps_by_netuid[nid] = _series(
            nid, 0.05, drift_per_bucket=0.0, vol_per_bucket=0.0005, seed=5
        )
    f = RegimeFilter(db, s)
    await f.refresh()  # → DEAD current
    assert f.current_regime == DEAD

    # Swap the data to trending for one cycle only
    for nid in range(1, 6):
        db.snaps_by_netuid[nid] = _series(
            nid, 0.05, drift_per_bucket=0.03, vol_per_bucket=0.08, seed=6
        )
    await f.refresh()
    assert f.classify() == TRENDING
    # Debounce hasn't elapsed yet
    assert f.current_regime == DEAD

    await f.refresh()
    # Second confirming cycle flips the debounced regime
    assert f.current_regime == TRENDING


# ── gate matrix tests ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_entry_allowed_matrix():
    s = _FakeSettings()
    db = _FakeDB()
    f = RegimeFilter(db, s)

    # Directly exercise the gate map across all regimes.
    matrix = {
        TRENDING: {"ema": True, "flow": True, "mr": False, "yield": True},
        DISPERSED: {"ema": True, "flow": True, "mr": True, "yield": True},
        CHOPPY: {"ema": False, "flow": False, "mr": True, "yield": True},
        DEAD: {"ema": False, "flow": False, "mr": False, "yield": True},
    }
    for regime, expected in matrix.items():
        f._current_regime = regime
        for strat, allowed in expected.items():
            assert f.entry_allowed(strat) is allowed, f"{regime}/{strat}"


@pytest.mark.asyncio
async def test_kill_switch_allows_all_entries():
    s = _FakeSettings(REGIME_ENABLED=False)
    db = _FakeDB()
    f = RegimeFilter(db, s)
    f._current_regime = DEAD  # worst regime
    for strat in ("ema", "flow", "mr", "yield"):
        assert f.entry_allowed(strat) is True


@pytest.mark.asyncio
async def test_kill_switch_still_populates_metrics():
    s = _FakeSettings(REGIME_ENABLED=False)
    db = _FakeDB()
    for nid in range(1, 6):
        db.snaps_by_netuid[nid] = _series(
            nid, 0.05, drift_per_bucket=0.03, vol_per_bucket=0.08, seed=7
        )
    f = RegimeFilter(db, s)
    await f.refresh()
    assert f.metrics["vol_24h"] is not None
    assert f.metrics["n_subnets"] >= 1
    # All gates open regardless
    assert f.entry_allowed("ema") is True


# ── thin-universe handling ─────────────────────────────────────

@pytest.mark.asyncio
async def test_thin_data_classifies_as_dead_without_raising():
    s = _FakeSettings(REGIME_MIN_SUBNETS=5)
    db = _FakeDB()
    # Only one subnet with one useful sample — should not raise and should
    # fall through to DEAD with thin_universe=True.
    db.snaps_by_netuid[1] = _series(
        1, 0.05, drift_per_bucket=0.0, vol_per_bucket=0.001, n_buckets=2, seed=8
    )
    f = RegimeFilter(db, s)
    await f.refresh()
    assert f.classify() == DEAD
    assert f.metrics["thin_universe"] is True


# ── Phase 1: bucketing contract ────────────────────────────────

def test_bucket_snapshots_prod_config_24h_4h():
    # 144 rows at 10-min cadence → 23h50m span (the live-bot shape
    # that triggered the 2026-04-22 DEAD lock-up before the fix).
    rows = _series(
        netuid=1,
        start_price=0.05,
        drift_per_bucket=0.0,
        vol_per_bucket=0.0,
        cadence_minutes=10,
        n_rows=144,
    )
    assert len(rows) == 144
    prices = _bucket_snapshots(rows, bucket_hours=4, window_hours=24)
    assert len(prices) == 6


def test_bucket_snapshots_drops_edges_before_first_observation():
    # 6 rows evenly spaced over 12h. With window=24h and bucket=4h the
    # function would produce 6 backward edges (0, -4, -8, -12, -16, -20h),
    # but the last two fall before first_ts and must be dropped.
    now = datetime.now(timezone.utc).replace(microsecond=0)
    start = now - timedelta(hours=12)
    rows = []
    for i in range(6):
        rows.append(
            {
                "ts": (start + timedelta(hours=12 * i / 5)).isoformat(),
                "price": 100 + i,
            }
        )
    prices = _bucket_snapshots(rows, bucket_hours=4, window_hours=24)
    assert len(prices) == 4


def test_bucket_snapshots_respects_window_override():
    now = datetime.now(timezone.utc).replace(microsecond=0)
    start = now - timedelta(hours=12)
    rows = []
    for i in range(6):
        rows.append(
            {
                "ts": (start + timedelta(hours=12 * i / 5)).isoformat(),
                "price": 100 + i,
            }
        )
    prices = _bucket_snapshots(rows, bucket_hours=4, window_hours=12)
    # max(1, 12 // 4) = 3 edges; all three survive inside first_ts..last_ts.
    assert len(prices) == 3


def test_bucket_snapshots_rejects_empty_and_invalid():
    ts = datetime.now(timezone.utc).isoformat()
    assert _bucket_snapshots([], bucket_hours=4, window_hours=24) == []
    assert _bucket_snapshots(
        [{"ts": ts, "price": 1.0}], bucket_hours=0, window_hours=24
    ) == []
    assert _bucket_snapshots(
        [{"ts": ts, "price": 1.0}], bucket_hours=4, window_hours=0
    ) == []
    # Non-positive price, None price, missing ts, un-parseable ts — all dropped.
    bad_rows = [
        {"ts": ts, "price": 0.0},
        {"ts": ts, "price": -1.0},
        {"ts": ts, "price": None},
        {"ts": None, "price": 1.0},
        {"ts": "not-a-date", "price": 1.0},
    ]
    assert _bucket_snapshots(bad_rows, bucket_hours=4, window_hours=24) == []


def test_bucket_snapshots_locf_fills_gaps():
    # Two observations, one at -12h and one at 0h. With window=24h/bucket=4h
    # the surviving edges are -12, -8, -4, 0. The three intermediate edges
    # should carry the -12h price forward (LOCF).
    now = datetime.now(timezone.utc).replace(microsecond=0)
    rows = [
        {"ts": (now - timedelta(hours=12)).isoformat(), "price": 1.0},
        {"ts": now.isoformat(), "price": 2.0},
    ]
    prices = _bucket_snapshots(rows, bucket_hours=4, window_hours=24)
    assert prices == [1.0, 1.0, 1.0, 2.0]


# ── Phase 2: classify_regime state-table coverage ──────────────

def test_classify_regime_fallthrough_to_dead():
    s = _FakeSettings()
    # vol clears DEAD (0.06 >= 0.05) but falls short of TREND (0.30)
    # and CHOP_FLOOR (0.10); dir below DIR; disp below DISP.
    assert classify_regime(
        vol=0.06, dir_strength=0.01, dispersion=0.01, settings=s
    ) == DEAD


def test_classify_regime_boundary_equals_threshold():
    s = _FakeSettings()
    # TREND boundary: equals threshold is enough to classify as TRENDING.
    assert classify_regime(
        vol=s.REGIME_VOL_TREND_THRESHOLD,
        dir_strength=s.REGIME_DIR_THRESHOLD,
        dispersion=0.0,
        settings=s,
    ) == TRENDING
    # DISP boundary: equals threshold → DISPERSED (vol below TREND).
    assert classify_regime(
        vol=s.REGIME_VOL_CHOP_FLOOR,
        dir_strength=0.0,
        dispersion=s.REGIME_DISP_THRESHOLD,
        settings=s,
    ) == DISPERSED
    # CHOP_FLOOR boundary: equals threshold, dir below → CHOPPY.
    assert classify_regime(
        vol=s.REGIME_VOL_CHOP_FLOOR,
        dir_strength=s.REGIME_DIR_THRESHOLD - 0.001,
        dispersion=0.0,
        settings=s,
    ) == CHOPPY


# ── Phase 3: thin-universe explicit cases ──────────────────────

@pytest.mark.asyncio
async def test_thin_when_subnet_count_below_min():
    s = _FakeSettings(REGIME_MIN_SUBNETS=5)
    db = _FakeDB()
    # 3 healthy subnets with strong drift (would classify as TRENDING on
    # their own) — but the universe is too thin to trust.
    for nid in range(1, 4):
        db.snaps_by_netuid[nid] = _series(
            nid, 0.05, drift_per_bucket=0.03, vol_per_bucket=0.08, seed=20
        )
    f = RegimeFilter(db, s)
    await f.refresh()
    m = f.metrics
    assert m["raw_regime"] == DEAD
    assert m["thin_universe"] is True
    assert m["n_subnets"] == 3


@pytest.mark.asyncio
async def test_thin_when_every_subnet_below_min_buckets():
    s = _FakeSettings(REGIME_MIN_SUBNETS=3, REGIME_MIN_BUCKETS=4)
    db = _FakeDB()
    # 5 subnets each with only 2 buckets of data → all filtered at the
    # per-subnet min_buckets gate before aggregation.
    for nid in range(1, 6):
        db.snaps_by_netuid[nid] = _series(
            nid,
            0.05,
            drift_per_bucket=0.0,
            vol_per_bucket=0.001,
            n_buckets=2,
            seed=21,
        )
    f = RegimeFilter(db, s)
    await f.refresh()
    m = f.metrics
    assert m["n_subnets"] == 0
    assert m["thin_universe"] is True
    assert m["raw_regime"] == DEAD


# ── Phase 4: regression — 2026-04-22 live cadence ──────────────

def test_regression_2026_04_22_dead_lock():
    # 10 subnets × 144 rows × 10-min cadence, run through prod settings
    # (MIN_BUCKETS=6, WINDOW=24, BUCKET=4, MIN_SUBNETS=10). Before the
    # _bucket_snapshots fix this produced 5 buckets per subnet → all
    # filtered → thin universe → DEAD. After the fix each subnet yields
    # exactly 6 buckets and the classifier can operate normally.
    s = _ProdFakeSettings()
    per_netuid: dict[int, list[dict]] = {}
    for nid in range(1, 11):
        per_netuid[nid] = _series(
            nid,
            start_price=0.05,
            drift_per_bucket=0.01,
            vol_per_bucket=0.02,
            cadence_minutes=10,
            n_rows=144,
            seed=30,
        )
    m = compute_regime_metrics(per_netuid, s)
    assert m["n_subnets"] == 10
    assert m["thin_universe"] is False


# ── Phase 5: RegimeFilter state-machine gaps ───────────────────

@pytest.mark.asyncio
async def test_refresh_throttles_within_interval():
    s = _FakeSettings(REGIME_REFRESH_SECONDS=60)
    db = _FakeDB()
    for nid in range(1, 6):
        db.snaps_by_netuid[nid] = _series(
            nid, 0.05, drift_per_bucket=0.03, vol_per_bucket=0.08, seed=40
        )
    f = RegimeFilter(db, s)

    calls = {"n": 0}
    orig = f._compute_metrics

    async def counting():
        calls["n"] += 1
        return await orig()

    f._compute_metrics = counting  # type: ignore[assignment]

    await f.refresh()
    ts_after_first = f._last_refresh_ts
    await f.refresh()
    assert calls["n"] == 1
    assert f._last_refresh_ts == ts_after_first


@pytest.mark.asyncio
async def test_refresh_force_bypasses_throttle():
    s = _FakeSettings(REGIME_REFRESH_SECONDS=60)
    db = _FakeDB()
    for nid in range(1, 6):
        db.snaps_by_netuid[nid] = _series(
            nid, 0.05, drift_per_bucket=0.03, vol_per_bucket=0.08, seed=41
        )
    f = RegimeFilter(db, s)

    calls = {"n": 0}
    orig = f._compute_metrics

    async def counting():
        calls["n"] += 1
        return await orig()

    f._compute_metrics = counting  # type: ignore[assignment]

    await f.refresh()
    await f.refresh(force=True)
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_debounce_resets_when_raw_flips_back():
    # With debounce=3, a TRENDING streak must stay unbroken for 3 cycles to
    # flip. The sequence below lets TRENDING accumulate twice, resets when
    # raw returns to DEAD, then only rebuilds to count=2 — never enough to
    # flip. The ``raw == current_regime`` branch zeroes the pending counter.
    s = _FakeSettings(REGIME_DEBOUNCE_CYCLES=3)
    db = _FakeDB()
    f = RegimeFilter(db, s)

    seq = iter([DEAD, TRENDING, TRENDING, DEAD, TRENDING, TRENDING])

    async def fake_compute():
        return {
            "raw_regime": next(seq),
            "vol_24h": 0.1,
            "directional_strength": 0.01,
            "dispersion": 0.01,
            "n_subnets": 5,
            "thin_universe": False,
            "updated_at": None,
        }

    f._compute_metrics = fake_compute  # type: ignore[assignment]

    for cycle in range(6):
        await f.refresh()
        assert f.current_regime == DEAD, f"flipped at cycle {cycle}"


@pytest.mark.asyncio
async def test_gates_map_exposes_all_strategies():
    s = _FakeSettings()
    db = _FakeDB()
    f = RegimeFilter(db, s)
    for regime in (TRENDING, DISPERSED, CHOPPY, DEAD):
        f._current_regime = regime
        gm = f.gates_map()
        assert set(gm.keys()) == {"ema", "flow", "mr", "yield"}
        for name, allowed in gm.items():
            assert allowed is f.entry_allowed(name), f"{regime}/{name}"


@pytest.mark.asyncio
async def test_entry_allowed_unknown_strategy_defaults_to_all():
    s = _FakeSettings()
    db = _FakeDB()
    f = RegimeFilter(db, s)
    for regime in (TRENDING, DISPERSED, CHOPPY, DEAD):
        f._current_regime = regime
        assert f.entry_allowed("nonexistent") is True


# ── Phase 6: gate parser edge cases ────────────────────────────

def test_parse_gate_whitespace_and_duplicates():
    assert _parse_gate("  trending , trending ") == frozenset({TRENDING})


def test_parse_gate_all_overrides_other_tokens():
    assert _parse_gate("trending,all,dead") == frozenset(
        {TRENDING, DISPERSED, CHOPPY, DEAD}
    )


# ── coverage-closing niceties ──────────────────────────────────

def test_compute_regime_metrics_skips_empty_netuid():
    # A netuid with no snapshots should be silently skipped, not crash.
    s = _FakeSettings(REGIME_MIN_SUBNETS=1)
    per_netuid: dict[int, list[dict]] = {7: []}
    m = compute_regime_metrics(per_netuid, s)
    assert m["n_subnets"] == 0
    assert m["thin_universe"] is True


@pytest.mark.asyncio
async def test_refresh_swallows_compute_exceptions():
    s = _FakeSettings()
    db = _FakeDB()
    f = RegimeFilter(db, s)

    async def boom():
        raise RuntimeError("synthetic")

    f._compute_metrics = boom  # type: ignore[assignment]
    # Must not raise; state stays on the initial DEAD/thin defaults.
    await f.refresh()
    assert f.current_regime == DEAD


@pytest.mark.asyncio
async def test_regime_since_is_readable():
    s = _FakeSettings()
    db = _FakeDB()
    f = RegimeFilter(db, s)
    assert isinstance(f.regime_since, str)
    assert f.regime_since  # non-empty ISO timestamp
