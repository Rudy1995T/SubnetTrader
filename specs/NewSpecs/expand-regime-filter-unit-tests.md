# Spec: Expand Unit Test Coverage for the Regime Filter

**Branch:** `2_EMA_Strategies`
**Date:** 2026-04-22
**Status:** Draft
**Depends on:** [implement-regime-classifier.md](implement-regime-classifier.md)
**Related:** [strategy-volatility-regime-filter.md](../strategy-volatility-regime-filter.md)

## Motivation

On 2026-04-22 the live bot logged `REGIME: DEAD (vol=0.0 dir=0.0 disp=0.0)`
on every one of 222 cycles, blocking every trend and mean-reversion entry
for the day. Root cause was a bucket-counting off-by-one in
`_bucket_snapshots` in [app/strategy/regime.py](../../app/strategy/regime.py):
with `REGIME_VOL_WINDOW_HOURS=24` and `REGIME_BUCKET_HOURS=4` the function
produced 5 edges instead of 6 because the edge loop anchored at
`first_ts + bucket_sec` and terminated at `last_ts`, losing a bucket to
the ~10-minute sampling jitter at the window edge. With
`REGIME_MIN_BUCKETS=6` every subnet was filtered out, the universe was
flagged thin, and the classifier locked to `DEAD`.

The existing test suite [tests/test_regime_filter.py](../../tests/test_regime_filter.py)
did not catch this because:

1. It runs with `REGIME_MIN_BUCKETS=4` (relaxed for synthetic fixtures),
   so the prod combination `MIN_BUCKETS=6` / `WINDOW=24` / `BUCKET=4` is
   never exercised.
2. Its synthetic series always span `bucket_hours * n_buckets` starting
   at a clean bucket boundary, so sampling jitter at the window edge is
   never reproduced.
3. There is no assertion on `n_subnets` — a thin-universe fallback that
   should not fire in a healthy fixture can go unnoticed.

The fix landed in [app/strategy/regime.py](../../app/strategy/regime.py)
anchors edges at `last_ts` stepping backwards, but the regression-catching
responsibility belongs in the test suite.

## Goals

1. Cover every branch of `classify_regime` and the thin-universe fallback
   in `compute_regime_metrics` with deterministic fixtures.
2. Pin the bucketing contract: at prod config (24h / 4h / min=6) a 23h 55m
   span must yield 6 buckets, and a 12h span must yield 4 (and be rejected
   as thin).
3. Exercise the full `RegimeFilter` state machine — refresh throttling,
   debounce window, kill-switch, and per-strategy gate matrix.
4. Add a regression test that reproduces the 2026-04-22 DEAD-lock with
   realistic 10-min snapshot cadence and asserts the classifier recovers.
5. Keep the suite fast (< 2s total) and hermetic (no DB, no network).

## Non-goals

- Testing `RegimeFilter._compute_metrics`'s SQL path. The `_FakeDB` stand-in
  in the existing file already shims that; we keep using it.
- Tuning thresholds. Threshold values live in `.env` / backtest output,
  not tests.
- Integration tests that bring up the FastAPI server or the Telegram bot.
- Property-based tests. Deterministic fixtures are clearer here; the math
  is small enough that exhaustive branch coverage suffices.

## Implementation Plan

### Phase 1 — Bucketing contract tests

Add to [tests/test_regime_filter.py](../../tests/test_regime_filter.py):

1. **`test_bucket_snapshots_prod_config_24h_4h`** — 144 rows at 10-min
   cadence spanning ~23h 55m. Assert `len(prices) == 6` with
   `bucket_hours=4, window_hours=24`. This is the regression test for the
   2026-04-22 lock-up.
2. **`test_bucket_snapshots_drops_edges_before_first_observation`** —
   6 rows over only 12h. Assert `len(prices) == 4` (only edges at
   `last_ts - 0,4,8,12h` survive; `-16h` and `-20h` fall before
   `first_ts`).
3. **`test_bucket_snapshots_respects_window_override`** — same rows,
   `window_hours=12, bucket_hours=4`. Assert `len(prices) == 3`
   (`max(1, 12 // 4) = 3`).
4. **`test_bucket_snapshots_rejects_empty_and_invalid`** — empty input,
   `bucket_hours=0`, `window_hours=0`, rows with non-positive or missing
   prices. Assert `[]` in each case.
5. **`test_bucket_snapshots_locf_fills_gaps`** — gap of three buckets
   between two observations. Assert the gap buckets carry the last
   observed price forward.

### Phase 2 — State table coverage

`classify_regime` has five branches (DEAD-by-vol, TRENDING, DISPERSED,
CHOPPY, DEAD-fallthrough). The existing tests cover four. Add the
fallthrough case that the current tests miss:

6. **`test_classify_regime_fallthrough_to_dead`** — `vol=0.06` (clears
   DEAD threshold), `dir_strength=0.01` (below dir threshold),
   `dispersion=0.01` (below dispersion threshold), `vol=0.06` (below
   chop floor 0.10). Assert `classify_regime(...) == DEAD`.
7. **`test_classify_regime_boundary_equals_threshold`** — each threshold
   is `>=`, not `>`. Pin that behavior at the `TREND`, `DISP`, and
   `CHOP_FLOOR` boundaries.

### Phase 3 — Thin-universe explicit coverage

The existing `test_thin_data_classifies_as_dead_without_raising` exercises
one thin case. Split it into two to separate concerns:

8. **`test_thin_when_subnet_count_below_min`** — `REGIME_MIN_SUBNETS=5`,
   provide only 3 healthy subnets. Assert `raw_regime=DEAD` and
   `thin_universe=True`, even if vol/dispersion would otherwise classify
   as TRENDING.
9. **`test_thin_when_every_subnet_below_min_buckets`** — provide 5 subnets,
   each with only 2 buckets of data. Assert `n_subnets=0` and
   `thin_universe=True`.

### Phase 4 — Regression fixture: live sampling cadence

10. **`test_regression_2026_04_22_dead_lock`** — build 10 subnets each
    with 144 rows at 10-min cadence spanning 23h 55m (the exact shape
    logged on 2026-04-22). Run with prod settings
    (`MIN_BUCKETS=6, WINDOW=24, BUCKET=4, MIN_SUBNETS=10`). Assert the
    metrics have `n_subnets == 10` (not 0) and `thin_universe is False`,
    guaranteeing the bucket-count regression can't come back.

### Phase 5 — `RegimeFilter` state machine gaps

Existing tests cover gates, kill-switch, and debounce flips. Add:

11. **`test_refresh_throttles_within_interval`** — `REGIME_REFRESH_SECONDS=60`.
    Patch `_compute_metrics` to count invocations. Call `refresh()` twice
    back-to-back. Assert the second call is a no-op
    (`_last_refresh_ts` unchanged, `_compute_metrics` called once).
12. **`test_refresh_force_bypasses_throttle`** — same setup, second call
    with `force=True`. Assert `_compute_metrics` called twice.
13. **`test_debounce_resets_when_raw_flips_back`** — with
    `REGIME_DEBOUNCE_CYCLES=3`, feed sequence
    `DEAD, TRENDING, DEAD, TRENDING, TRENDING, TRENDING`. Assert the
    debounced `current_regime` never leaves `DEAD` because the pending
    counter resets when the raw classification flips back.
14. **`test_gates_map_exposes_all_strategies`** — call `gates_map()` in
    each of the four regimes. Assert the return dict has keys
    `{ema, flow, mr, yield}` and values match `entry_allowed(strategy)`.
15. **`test_entry_allowed_unknown_strategy_defaults_to_all`** —
    `entry_allowed("nonexistent")` returns `True` in every regime.

### Phase 6 — Gate parser edge cases

16. **`test_parse_gate_whitespace_and_duplicates`** — `"  trending , trending "`
    collapses to `frozenset({TRENDING})`.
17. **`test_parse_gate_all_overrides_other_tokens`** — `"trending,all,dead"`
    returns every regime (the `all` short-circuit must win).

## Fixtures & helpers

Extend the existing `_series(...)` helper in
[tests/test_regime_filter.py](../../tests/test_regime_filter.py):

- Add a `cadence_minutes` arg so Phase 4's fixture can generate 144
  rows at 10-min intervals rather than the current bucket-aligned series.
- Add a `span_hours` convenience wrapper that computes row count from
  span and cadence.

Keep `_FakeSettings` as the canonical knobs injector; add a
`_ProdFakeSettings` variant with `REGIME_MIN_BUCKETS=6` for the
bucketing contract and regression tests so the prod config is
exercised at least once.

`_FakeDB` stays unchanged — the only SQL it needs to shim is
`SELECT DISTINCT netuid FROM pool_snapshots WHERE ts >= ? AND netuid != 0`
plus `Database.get_pool_snapshots`, both already covered.

## Acceptance criteria

- `pytest tests/test_regime_filter.py -q` passes all tests (existing 13 +
  ~17 new) in under 2 seconds on the Pi.
- Reverting the `_bucket_snapshots` fix causes
  `test_regression_2026_04_22_dead_lock` and
  `test_bucket_snapshots_prod_config_24h_4h` to fail (confirmed by
  stashing the fix, running, unstashing).
- `coverage run --source=app.strategy.regime -m pytest tests/test_regime_filter.py`
  reports ≥ 95% line coverage on [app/strategy/regime.py](../../app/strategy/regime.py).

## Risks

- **Flaky tests on the Pi.** Synthetic series use `random.Random(seed)`
  — deterministic. Timestamps are relative to `datetime.now(timezone.utc)`
  but every assertion is on shape/counts, not wall-clock.
- **Test drift from config changes.** Threshold values live in `.env`
  and may be retuned after the per-regime-edge backtest lands. Keep
  thresholds inline in `_FakeSettings` rather than reading `.env`, so
  production retunes don't move the tests.

## Out-of-scope follow-ups

- Integration test that wires `RegimeFilter` into `EmaManager` and
  asserts `skipping entries — regime=DEAD` is logged when it should be
  and *not* logged when the gate opens. Nice-to-have but doubles suite
  runtime; defer until we see a manager-side regression.
- A CLI probe (`python -m app.strategy.regime --probe`) that prints
  current metrics against the live DB — separate spec if operators want
  it.
