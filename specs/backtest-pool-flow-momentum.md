# Spec: Historical Backtest for Pool Flow Momentum (v2)

**Branch:** `2_EMA_Strategies`
**Date:** 2026-04-21
**Status:** Draft
**Depends on:** [strategy-pool-flow-momentum.md](strategy-pool-flow-momentum.md) (signal math is implemented in [app/strategy/flow_signals.py](../app/strategy/flow_signals.py)), [app/backtest/engine.py](../app/backtest/engine.py) (reuse for EMA/mean-rev already proven)

## Motivation

Pool Flow Momentum is currently in Phase 1 of the original strategy spec — 14
days of live snapshot collection before any offline replay. That plan burns
two weeks of calendar time, locks up a trading pot in dry-run, and produces a
sample of ~40 trades that is statistically thin by design. Worse: the moment
anything is tuned (z-score threshold, stop-loss, time-stop, emission
sold-fraction), the clock resets.

Taostats already holds the data we need. The
[/api/dtao/pool/history/v1](https://docs.taostats.io) endpoint returns per-pool
snapshots with every field flow_signals consumes (`total_tao`, `alpha_in_pool`,
`price`, `block_number`, `timestamp`, and where available,
`alpha_emission_rate`). That means we can **replay three to six months of real
pool history through the existing signal functions**, get hundreds of trades
instead of dozens, and see concrete TAO P&L for what the strategy *would have*
done if it had been live in Q4 2025 / Q1 2026.

The existing backtest framework ([app/backtest/engine.py](../app/backtest/engine.py),
[strategies.py](../app/backtest/strategies.py), [slippage.py](../app/backtest/slippage.py),
[data_loader.py](../app/backtest/data_loader.py)) already replays EMA and
mean-reversion against cached candle history with pool-depth-aware slippage.
Flow needs the same treatment, not another dry-run.

## Goals

1. Quantify Flow's expected performance across the **longest window Taostats
   will serve at the finest interval it will serve** (target: ≥ 90 days at
   hourly resolution, per subnet, top 50 by depth).
2. Produce a comparable metrics set to the existing EMA backtest
   ([data/backtest/results/backtest_20260409_195034.csv](../data/backtest/results/backtest_20260409_195034.csv))
   so we can answer "would Flow have complemented EMA in the same window?"
3. Establish the **data-availability ceiling** up front — how far back history
   goes per interval, how long one full fetch takes under the 60 req/min
   Taostats limit, and whether `alpha_emission_rate` is present historically
   or must be reconstructed.
4. Drop flow's current warm-up-then-paper plan in favour of
   **backtest → paper → live**, cutting the pre-live window from ~6 weeks to
   ~2 weeks.

## Non-goals

- Changing any signal math in [flow_signals.py](../app/strategy/flow_signals.py).
  Thresholds, EWMA halflife, magnitude cap, dual-sided confirmation — all
  stay as implemented. The backtest reads the same functions the live manager
  reads.
- Replacing the live snapshot persistence (`pool_snapshots` table). That
  continues to run because (a) live regime-index math needs real-time data
  anyway, and (b) 5-min cadence flow in production is still the target; this
  spec only covers *historical* replay.
- Tuning any EMA / mean-rev parameters. Those already have a working
  backtest.
- Adding a UI for backtest results. CSV + JSON into
  `data/backtest/results/` like the existing runs; operator reads them via
  `cat` or the existing report generator.

## What we need to confirm before implementation (data-availability probe)

Before writing any engine code, run a **small probe script** (~20 lines,
one-off) against Taostats to answer three questions. The answers shape the
rest of the spec.

| Question | Probe | Decision it drives |
|---|---|---|
| What `interval` values does `/api/dtao/pool/history/v1` accept? Specifically: does it serve `5m`, `15m`, `1h`, `4h`, `1d`? | Issue one request per candidate interval for a single known-good netuid (e.g. SN1), `limit=10`, inspect returned timestamp spacing. | If `5m` or `15m` is available, we can match live cadence exactly. If only `1h`+, we run flow with cadence-scaled windows (see below). |
| How far back does history go at the finest supported interval? | At the finest interval, page backwards (`limit=200` with `timestamp_end`) until the endpoint stops returning rows. | Sets the backtest window length. Target is ≥ 90 days. |
| Is `alpha_emission_rate` present in historical rows? | Inspect keys on the oldest returned record. Per-row emission rate is part of the latest-pool schema but may be absent historically. | If absent: derive `alpha_emission_rate` from consecutive `alpha_staked` deltas per block (the protocol mints ≈ 1 alpha per block per subnet subject to root prop), or fall back to `FLOW_EMISSION_ADJUST=false` for the backtest and document that as a known gap. |

Probe script lives at `app/backtest/probe_flow_history.py`, writes
`data/backtest/history/flow_probe.json` with:

```json
{
  "intervals_supported": ["1h", "4h", "1d"],
  "finest_interval": "1h",
  "max_history_days_per_netuid": 180,
  "emission_rate_present": false,
  "sample_record": { ... },
  "probed_at": "2026-04-21T..."
}
```

The probe is idempotent and completes in < 30 s at 60 req/min. Its output is
the first artifact committed under `data/backtest/history/` for the flow
run, and every downstream script reads it.

## Cadence scaling rule

flow_signals is already cadence-agnostic — the strategy spec calls this out
explicitly ("windows are expressed in snapshots, not wall-clock hours").
Based on the probe, pick `N = snapshots_per_hour` and scale:

| Parameter | 5-min cadence (live target) | 1-hour cadence (if probe confirms 1h is finest) |
|---|---:|---:|
| `window_1h_snaps` | 12 | 1 |
| `window_4h_snaps` | 48 | 4 |
| `baseline_snaps` | 576 | 48 |
| `cold_start_snaps` | 624 | 52 |
| EWMA halflife | 288 (24h) | 24 (24h) |

The backtest uses a separate `FlowSignalConfig` instance per run, derived
from the probe result — never hardcode the 5-min numbers.

**Expected behaviour shift at hourly cadence:** signal is smoother (one
aggregate per hour instead of 12 per hour), so z-scores move slower and the
strategy registers *fewer but cleaner* entries. We report this as a caveat
on every metric — the live 5-min version will be noisier, with more signals
and more whipsaws, so hourly-backtest expectancy is likely an **upper bound**.

## Data acquisition

### Endpoint + fetch plan

- **Endpoint**: `GET /api/dtao/pool/history/v1?netuid=<n>&interval=<i>&limit=200`
  (max page = 200; paginate with `timestamp_end` to go further back).
- **Auth, rate limit, retry**: reuse the existing `_rate_limited_get` helper
  in [data_loader.py](../app/backtest/data_loader.py) — no new rate limiter.
- **Subnets**: use `fetch_qualifying_netuids()` with
  `min_pool_depth_tao=5000` (matches flow strategy spec §Risks, tighter than
  EMA's 3000).
- **Window**: target 120 days at the finest supported interval. At hourly
  cadence that's ~2880 rows/subnet; at 5-min that's ~34 560 rows/subnet.
- **Cache**: new directory `data/backtest/history/flow/sn<N>.json`,
  separate from the EMA candle cache (schema differs — raw pool snapshots,
  not candles). Same 24 h TTL rule as EMA cache.

### Time estimate (before writing code)

```
50 subnets × (120 days / 200 rows-per-page) = 50 × (2880 rows @ 1h / 200)
                                             = 50 × 15 pages
                                             = 750 requests
                                            @ 60 req/min
                                             = ~13 minutes
```

At 5-min cadence (if supported): 50 × (34 560 / 200) = 8 640 requests ≈ 2.4 h.
The engine should support both; operator picks via CLI flag.

### Gap handling

Taostats occasionally returns missing rows when the upstream indexer lags.
The existing [flow_signals.has_gap()](../app/strategy/flow_signals.py#L206)
already rejects stale baselines — reuse it in the backtest. A subnet with
> 10% missing rows over the backtest window is excluded with a logged
warning rather than silently zero-filled (zero-fill would create fake
deltas and pollute z-scores).

## Engine integration

### New module: `app/backtest/flow_engine.py`

Mirrors [app/backtest/engine.py](../app/backtest/engine.py) but keyed on pool
snapshots instead of candles. Reuses:

- [flow_signals.flow_entry_signal()](../app/strategy/flow_signals.py#L272) — unchanged.
- [flow_signals.flow_exit_signal()](../app/strategy/flow_signals.py) — unchanged.
- [flow_signals.regime_index()](../app/strategy/flow_signals.py#L166) — computed once per
  timestep across *all* loaded subnets (this is the one place backtest and
  live diverge: live has 5-min top-50 regime from `_pool_snapshot`,
  backtest reconstructs it from the same history corpus).
- [slippage.apply_entry_slippage / apply_exit_slippage](../app/backtest/slippage.py) —
  existing pool-depth-aware AMM math.

New: `TradeRecord` carries flow-specific telemetry
(`entry_z_score`, `entry_adj_flow`, `entry_regime_index`, `exit_reason`
∈ {`FLOW_REVERSAL`, `STOP_LOSS`, `TAKE_PROFIT`, `TRAILING_STOP`,
`TIME_STOP_SOFT`, `TIME_STOP_HARD`, `REGIME_EXIT`}).

### Simulation loop (one sentence per step)

```
For each timestep t in the union of all subnets' timestamps (sorted):
  1. Pop any open positions whose exit condition triggers at t.
  2. Compute regime_index across all subnets with enough lookback at t.
  3. For each subnet with enough lookback, evaluate flow_entry_signal(t).
  4. If BUY and slot available and pool depth OK: open position using
     apply_entry_slippage against that subnet's snapshot at t.
  5. Record PnL marks for open positions for the equity curve.
```

The key correctness discipline: **at timestep t, signals only see snapshots
≤ t** — same `snapshots[:end_idx]` pattern already used inside
[compute_flow_zscore](../app/strategy/flow_signals.py#L124). Future
leakage is a common backtest bug; we have a defence already.

### CLI

```
python -m app.backtest.flow_engine \
    --window-days 120 \
    --interval 1h \
    --pot-tao 10 \
    --slots 3 \
    --z-entry 2.0 \
    --output data/backtest/results/flow_<timestamp>.csv
```

Defaults come from `flow_config()` in [app/config.py](../app/config.py); CLI
flags override for sweeps.

## Metrics

Same shape as the existing EMA result CSV, plus flow-specific columns:

**Standard (reuse [BacktestResult](../app/backtest/engine.py#L62)):**
`total_trades`, `win_rate`, `avg_win_pct`, `avg_loss_pct`, `expectancy`,
`profit_factor`, `total_pnl_pct`, `total_pnl_tao`, `max_drawdown_pct`,
`sharpe_ratio`, `avg_hold_hours`, `max_concurrent`, `exit_reasons`,
`subnets_traded`.

**Flow-specific:**
- `avg_entry_z_score`, `avg_entry_flow_pct`
- `pct_blocked_by_regime`, `pct_blocked_by_magnitude_cap`,
  `pct_blocked_by_cold_start`
- `mean_snapshots_to_first_signal_per_subnet`
- `ema_overlap_rate` — fraction of flow trades whose entry ts falls inside
  an EMA position window on the same subnet (answers "does flow add real
  diversification or just shadow EMA?")

**"How much would we have made" headline:**
- Start pot = `FLOW_POT_TAO` (default 10 τ).
- End pot = start + sum of realized `pnl_tao` − simulated fees (2 × 0.0003
  per trade, matching the live fee reserve).
- Reported as `pot_growth_tao` and `pot_growth_pct`.

## Parameter sweeps

Same grid as the original strategy spec §Backtesting Phase 2 —
`FLOW_Z_ENTRY` ∈ {1.5, 2.0, 2.5, 3.0}, `FLOW_MIN_TAO_PCT` ∈ {1.0, 2.0, 3.0},
stops, take-profit, time-stop, emission fraction, regime on/off. Runner is
a thin wrapper that iterates parameter tuples and writes one CSV row per
combination. Because backtest runs on cached data, a full sweep (5⁶ = ~15K
combinations) completes in minutes once history is fetched.

Promotion gates to advance from backtest → paper stay identical to the
strategy spec (hit rate ≥ 55% or expectancy ≥ 0.5R, profit factor ≥ 1.5,
max drawdown ≤ 15%, ≥ 40 trades). With 120 days of history, we expect 100–
400 trades depending on `FLOW_Z_ENTRY`, comfortably above the 40-trade
minimum.

## Revised rollout plan

| Phase | Original spec | This spec |
|---|---|---|
| 1 | 14 days snapshot collection, no trading | **Deleted** — replaced by backtest |
| 2 | Offline backtest after 14 days | **Immediate** — runs against Taostats history |
| 3 | 14 days paper trading | Unchanged (with stricter promotion gates informed by Phase 2 metrics) |
| 4 | Live, half pot | Unchanged |

Time-to-live shrinks from ~42 days to ~14 days. The live
`pool_snapshots` persistence still runs continuously so that Phase 3
paper-trading has real 5-min data to validate the cadence scaling
assumption.

## Files to create / modify

| File | Action |
|---|---|
| `app/backtest/probe_flow_history.py` | **New.** Small one-shot probe answering the three availability questions; writes `flow_probe.json`. |
| `app/backtest/flow_data_loader.py` | **New.** Paginated pool-history fetcher per subnet, cache under `data/backtest/history/flow/`. Reuses `_rate_limited_get` from `data_loader.py`. |
| `app/backtest/flow_engine.py` | **New.** Stateless replay of `flow_signals` over cached history with slippage. Mirrors [engine.py](../app/backtest/engine.py). |
| `app/backtest/__main__.py` | Add `flow` subcommand next to existing `ema` / `meanrev` invocations. |
| `app/backtest/report.py` | Extend CSV/JSON writer with flow-specific columns (§Metrics). |
| `data/backtest/history/flow/` | **New cache dir** — pool snapshots per subnet. |
| `data/backtest/results/flow_<ts>.csv` | Output of each run. |
| `specs/strategy-pool-flow-momentum.md` | One-line edit replacing Phase 1 "14-day snapshot collection" with "run historical backtest per [backtest-pool-flow-momentum.md](backtest-pool-flow-momentum.md)". |
| `tests/test_flow_engine.py` | **New.** Two small cases: (a) synthetic history with a clear z-score spike produces one BUY; (b) regime filter pauses entries when aggregate index drops below 0.95. |

**No changes** to [app/strategy/flow_signals.py](../app/strategy/flow_signals.py),
[app/portfolio/ema_manager.py](../app/portfolio/ema_manager.py), API routes,
frontend, or widget. The live path is untouched.

## Risks & mitigations

- **Hourly-only data overstates performance.** If the probe comes back with
  1h as the finest interval, the smoothed signal will register fewer
  whipsaws than the live 5-min version would. Report all metrics with an
  explicit "cadence: 1h" tag and treat expectancy as an upper bound; the
  Phase-3 paper trade is the corrective.
- **Missing `alpha_emission_rate` in history.** Emission adjustment silently
  falls back to raw flow (already handled at
  [flow_signals.py:82](../app/strategy/flow_signals.py#L82)). We run the
  sweep with `FLOW_EMISSION_ADJUST=true/false` as one of the sweep axes so
  the cost of dropping the adjustment is explicit.
- **Slippage over/under estimation.** The existing slippage model uses the
  *latest* `pool_snapshots.json` depth, which drifts over a 120-day
  window. Fix: look up the per-subnet pool depth *at the timestep t* from
  the same history row already loaded, not from the latest cache.
- **Look-ahead leakage via regime index.** Regime uses top-50 subnets by
  depth; "top-50" must itself be reconstructed at time t, not taken from
  the latest snapshot. Test case in `test_flow_engine.py` guards this.
- **Correlation with EMA entries in historical regime.** If flow ends up
  picking the same subnets at the same moments EMA already does, the
  backtest looks good but adds nothing to the live portfolio. The
  `ema_overlap_rate` metric surfaces this directly.

## Success criteria

1. Probe script runs in < 60 s and writes a `flow_probe.json` answering the
   three availability questions.
2. Data loader fetches ≥ 90 days of history for all qualifying subnets
   (depth ≥ 5000 τ) within the probed rate-limit budget, and caches to
   `data/backtest/history/flow/`.
3. `python -m app.backtest flow --window-days 120` produces a results CSV
   and JSON under `data/backtest/results/` with all standard and
   flow-specific metrics populated.
4. Parameter sweep across the §Parameter-sweeps grid completes in < 30 min
   on the Pi and writes one CSV row per combination.
5. The winning parameter set clears the existing promotion gates (hit rate,
   profit factor, max drawdown, trade count) — or, if it does not, the
   report clearly shows which gate fails and by how much, so we can decide
   whether to retune, redesign, or shelve the strategy before spending live
   TAO.
