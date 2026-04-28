# Spec: Implement Volatility Regime Classifier

**Branch:** `2_EMA_Strategies`
**Date:** 2026-04-21
**Status:** Draft
**Depends on:** [strategy-volatility-regime-filter.md](../strategy-volatility-regime-filter.md) (design), [backtest-per-regime-edge-analysis.md](backtest-per-regime-edge-analysis.md) (produces the per-regime edge matrix used to finalize thresholds and per-strategy gates), [strategy-pool-flow-momentum.md](../strategy-pool-flow-momentum.md) and [strategy-mean-reversion.md](../strategy-mean-reversion.md) (managers that must expose a regime check before entry)

## Motivation

The design spec ([strategy-volatility-regime-filter.md](../strategy-volatility-regime-filter.md))
argues that losses and wins cluster in time because the subnet market oscillates
between trending, choppy, and dead regimes. That spec defines the math
(realized vol, directional strength, cross-sectional dispersion) and the state
table, but stops before code. This spec turns that design into a concrete
implementation plan wired into the live bot: module location, data source,
caching, integration points in each strategy manager, config flags, kill-switch,
and tests.

Final threshold numbers are deliberately left open here — they are the output
of [backtest-per-regime-edge-analysis.md](backtest-per-regime-edge-analysis.md).
This spec ships with the design's defaults and is re-tuned once that backtest
lands.

## Goals

1. Deliver a single `RegimeFilter` class that every strategy manager can query
   in O(1) inside its entry loop.
2. Drive the classifier off the existing `pool_snapshots` table (see
   [app/storage/models.sql](../../app/storage/models.sql) and
   [app/storage/db.py](../../app/storage/db.py)) — no new data source, no new
   Taostats calls on the hot path.
3. Gate entries per strategy via an env-driven allow-list (EMA / Flow / MR /
   Yield can each target different regimes).
4. Never gate exits. Stop-loss, trailing stop, take-profit, and signal-based
   exits always run.
5. Support a hard kill-switch (`REGIME_ENABLED=false`) that disables the filter
   without a restart — the classifier still computes and logs, but
   `entry_allowed(strategy)` returns `True` unconditionally.
6. Expose the current regime over HTTP (`GET /api/regime`) for the frontend
   chip and the widget.

## Non-goals

- Per-subnet regime detection. v1 is aggregate-only; the design spec flags
  per-subnet as a v2 idea and we stick to that.
- Tuning thresholds. We ship with the design's defaults; final values come
  from Spec B's per-regime edge matrix.
- Writing the frontend regime chip. Covered separately once the API is live.
- Replacing the circuit breaker in [app/portfolio/ema_manager.py](../../app/portfolio/ema_manager.py).
  The breaker handles 15% drawdown crashes; the regime filter handles slow
  chop. They run in parallel and are independent.
- Forecasting future regimes. Classification is backward-looking over the
  configured window.

## Implementation Plan

### Phase 1 — `RegimeFilter` module

Create [app/strategy/regime.py](../../app/strategy/regime.py) exposing:

```
class RegimeFilter:
    def __init__(self, db, settings): ...
    async def refresh(self) -> None          # recompute from pool_snapshots
    def classify(self) -> str                # "TRENDING" | "DISPERSED" | "CHOPPY" | "DEAD"
    @property
    def current_regime(self) -> str          # debounced state
    @property
    def metrics(self) -> dict                # {vol_24h, directional_strength, dispersion, since, raw_regime}
    def entry_allowed(self, strategy: str) -> bool
```

Internals:

- `refresh()` pulls the last `REGIME_VOL_WINDOW_HOURS` of rows per subnet
  from `pool_snapshots` via `Database.get_pool_snapshots(netuid, since_ts=...)`
  (already exists, see [app/storage/db.py](../../app/storage/db.py) line 362).
- **Bucketing.** The bot persists snapshots every `SCAN_INTERVAL_MIN` (5–15
  min in production), not every 4h. `refresh()` downsamples each subnet's
  rows into 4h buckets by taking the last snapshot at or before each
  bucket edge, then computes log-returns on the bucketed series. This
  keeps the vol math consistent with the design spec's `sqrt(6 * 365)`
  annualization regardless of scan cadence. Subnets with fewer than
  `REGIME_MIN_BUCKETS` (default 6 = 24h) bucketed points are skipped.
- **Universe guard.** If fewer than `REGIME_MIN_SUBNETS` (default 10)
  subnets contribute, classification is `DEAD` with a `thin_universe`
  flag in metrics — prevents one noisy subnet from dragging the
  aggregate.
- Per-subnet log returns → rolling realized vol (annualized with
  `sqrt(6 * 365)` for 4h bars, matching the design spec).
- Directional strength = mean of absolute period returns across the subnet
  universe.
- Dispersion = cross-sectional std of per-subnet window returns.
- Classification follows the table in the design spec; thresholds are injected
  from settings, not hard-coded.
- Debounce: keep a `_pending_regime` and `_pending_count`; only flip
  `_current_regime` when a new raw classification has held for
  `REGIME_DEBOUNCE_CYCLES` consecutive `refresh()` calls.
- **Interaction with `FLOW_REGIME_FILTER_ENABLED`.** The existing
  market-wide index gate in
  [app/portfolio/ema_manager.py](../../app/portfolio/ema_manager.py) line
  840 (`_regime_ok`) is a one-dimensional score and stays in place. For
  Flow entries, both gates must pass (AND). The new `RegimeFilter` is
  not a replacement — it's additive, with richer state and per-strategy
  gating.

### Phase 2 — Config

Extend [app/config.py](../../app/config.py) with the env keys already listed
in the design spec:

```
REGIME_ENABLED=true
REGIME_VOL_WINDOW_HOURS=24
REGIME_VOL_TREND_THRESHOLD=0.30
REGIME_VOL_CHOP_FLOOR=0.10
REGIME_VOL_DEAD_THRESHOLD=0.05
REGIME_DIR_THRESHOLD=0.02
REGIME_DISP_THRESHOLD=0.015
REGIME_DEBOUNCE_CYCLES=2
REGIME_REFRESH_SECONDS=60
REGIME_MIN_SUBNETS=10
REGIME_MIN_BUCKETS=6
REGIME_GATE_EMA=trending,dispersed
REGIME_GATE_FLOW=trending,dispersed
REGIME_GATE_MR=choppy,dispersed
REGIME_GATE_YIELD=all
```

`REGIME_GATE_*` values are comma-separated regime names (case-insensitive) or
the literal `all`. Parsing happens once at startup; `entry_allowed()` is a
set-membership check.

Add `.env.example` entries matching these defaults.

### Phase 3 — Caching

- `RegimeFilter.refresh()` is called at most once every
  `REGIME_REFRESH_SECONDS` (default 60s). Internally the class tracks
  `_last_refresh_ts`; faster callers get the cached result.
- `current_regime`, `metrics`, and `entry_allowed()` never trigger a DB read
  on their own. They read `self._current_regime` / `self._last_metrics`.
- The classifier runs in the same event loop as the managers. A shared
  singleton is instantiated in [app/main.py](../../app/main.py) startup and
  injected into each manager constructor.

### Phase 4 — Manager integration

For each strategy manager's entry pass, add one gate check before the first
size/slot computation. The check is uniform across managers so the pattern
reads the same everywhere.

**[app/portfolio/ema_manager.py](../../app/portfolio/ema_manager.py)** —
inside `_do_cycle` at the start of the "Entry pass" block (around line 298 of
the current file, right after the circuit-breaker block and the existing
`if self.is_breaker_active` short-circuit). Before iterating candidates:

```
if self._regime is not None:
    strategy_key = _map_strategy_key(self._cfg.strategy_type, self._cfg.tag)
    if not self._regime.entry_allowed(strategy_key):
        summary["regime_blocked"] = self._regime.current_regime
        summary["entries_skipped"] = f"regime={self._regime.current_regime}"
        logger.info(f"EMA[{self._cfg.tag}] cycle complete", data=summary)
        return summary
```

`_map_strategy_key` maps `strategy_type` ("ema"/"meanrev"/"flow") to the
env-configured gate name ("ema"/"mr"/"flow"/"yield"). The `RegimeFilter`
is optional on `EmaManager.__init__` so existing tests and callers do
not need to change.

**Flow manager** (new, referenced by
[strategy-pool-flow-momentum.md](../strategy-pool-flow-momentum.md)) — same
pattern with `"flow"`.

**Mean-Rev manager** (new, referenced by
[strategy-mean-reversion.md](../strategy-mean-reversion.md) and
[wire-meanrev-to-api-and-ui.md](../wire-meanrev-to-api-and-ui.md)) — same
pattern with `"mr"`; note that MR's env-configured allow-list is the
*inverse* of EMA/Flow (choppy/dispersed instead of trending/dispersed).

**Yield manager** — `REGIME_GATE_YIELD=all`, so the gate check returns
`True` and is effectively a no-op. Still wired for consistency and future
tightening.

Exits remain entirely unaffected in every manager.

### Phase 5 — Kill-switch

`REGIME_ENABLED=false` short-circuits `entry_allowed()` to `True` and the API
response flags `"enabled": false`. Classification still runs and still logs
so operators can compare "what would have been blocked" against actual P&L —
this doubles as the shadow-mode phase from the design spec.

The env var is re-read on `RegimeFilter.refresh()` so toggling without a
restart works (the bot already re-reads `.env` per cycle in other hot paths;
this one does the same).

### Phase 6 — Observability

- Add `GET /api/regime` to [app/main.py](../../app/main.py). Response shape
  matches the design spec (regime, since, vol_24h, directional_strength,
  dispersion, entries_allowed, entries_blocked, enabled).
- Each classifier cycle logs one line:
  `REGIME: TRENDING (vol=0.42 dir=0.031 disp=0.018) gates=ema,flow`
- Regime transitions log at `warning` level and fire a single `send_alert()`
  Telegram message so operators notice flips.

### Phase 7 — Tests

Add [tests/test_regime_filter.py](../../tests/test_regime_filter.py) covering:

1. Synthetic snapshot fixture with known vol/dispersion → expected raw
   regime for each of the four states.
2. Debounce: a single-cycle flicker does NOT change `current_regime`;
   `REGIME_DEBOUNCE_CYCLES` consecutive flips does.
3. `entry_allowed()` matrix across the four regimes × four strategies
   against the design-spec gate config.
4. `REGIME_ENABLED=false` returns `True` for every strategy regardless of
   regime, while still populating `metrics`.
5. Thin data (one subnet, one sample) classifies as `DEAD` and does not
   raise.

### Phase 8 — Threshold retune

Once [backtest-per-regime-edge-analysis.md](backtest-per-regime-edge-analysis.md)
produces its per-regime edge matrix, re-run this plan's Phase 2 with the
tuned numbers and update `REGIME_GATE_*` to match the matrix's
recommendations. This is a config-only change, no code.

## Definition of Done

- `RegimeFilter` exists in [app/strategy/regime.py](../../app/strategy/regime.py)
  and is instantiated once at bot startup.
- All three momentum managers (EMA, Flow, MR) call `entry_allowed()` before
  the size/slot loop.
- `GET /api/regime` returns the current regime, metrics, and per-strategy
  gate booleans.
- `REGIME_ENABLED=false` disables all gating without a restart; classification
  still logs.
- `tests/test_regime_filter.py` passes.
- At least 24h of shadow-mode logs exist showing regime transitions on live
  data before gating is turned on in production.

## Open Questions

- What happens when `pool_snapshots` has <6h of history at bot startup?
  Current plan: classify as `DEAD` and log a warning until enough data
  accumulates. Acceptable, or do we need a cold-start bypass that allows
  entries during the warm-up window?
- Should the classifier weight subnets by pool depth? v1 uses unweighted
  mean — a small thin subnet moves dispersion as much as a top-10 subnet.
  Depth-weighting is a natural v2 refinement but we wait for the edge matrix
  to tell us if it matters.
- Refresh cadence: 60s is arbitrary. If Spec B shows regimes transition on
  faster-than-60s boundaries (unlikely given a 24h vol window), drop to 30s.
- Per-strategy thresholds: design spec has one global vol threshold. Spec B
  may reveal that, e.g., Mean-Rev needs a higher `VOL_DEAD_THRESHOLD` than
  EMA. Leave the config keys per-strategy-able (`REGIME_VOL_TREND_THRESHOLD_MR`
  etc.) in case we need it later, without building the override path until
  the data says so.
