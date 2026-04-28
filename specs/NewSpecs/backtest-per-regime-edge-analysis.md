# Spec: Per-Regime Edge Analysis (Backtest)

**Branch:** `2_EMA_Strategies`
**Date:** 2026-04-21
**Status:** Draft
**Depends on:** [strategy-volatility-regime-filter.md](../strategy-volatility-regime-filter.md) (regime definitions), [backtest-pool-flow-momentum.md](../backtest-pool-flow-momentum.md) (Flow backtest wired into the harness — sibling agent), [strategy-mean-reversion.md](../strategy-mean-reversion.md) + [replace-scalper-with-mean-reversion.md](../replace-scalper-with-mean-reversion.md) (Mean-Rev backtest wired into the harness — sibling agent), [app/backtest/engine.py](../../app/backtest/engine.py), [app/backtest/report.py](../../app/backtest/report.py)

## Motivation

[implement-regime-classifier.md](implement-regime-classifier.md) ships a
regime filter with the design spec's default gate mapping
(`REGIME_GATE_EMA=trending,dispersed`, `REGIME_GATE_MR=choppy,dispersed`,
etc.). That mapping is an educated guess. Before we commit to it in
production, we need to measure — for each strategy, under each regime —
whether the strategy actually had edge during that regime in historical data.

The backtest harness ([app/backtest/engine.py](../../app/backtest/engine.py),
[app/backtest/report.py](../../app/backtest/report.py)) already produces
per-strategy aggregate metrics. What's missing is the second axis: bucket
every simulated trade by the regime that was active *at its entry timestamp*,
then report metrics per bucket. The output is a single `{strategy} × {regime}`
matrix that tells us which combinations have real edge and which are losing
money.

This analysis is the empirical input that finalizes the gate mapping in
Spec A (implement-regime-classifier). Without this, we are guessing.

## Goals

1. Label every historical point in `pool_snapshots` with a regime
   (`TRENDING` / `DISPERSED` / `CHOPPY` / `DEAD`) using the same classifier
   logic that Spec A ships — a single source of truth.
2. Re-run the existing EMA, Flow, and Mean-Rev backtests with regime labels
   attached to each trade record.
3. Produce one pivotable CSV per run: one row per `(strategy, regime)` cell
   with win rate, trade count, mean PnL%, mean PnL TAO, Sharpe, max DD,
   expectancy, profit factor.
4. Apply a statistical-significance filter to the matrix so the user isn't
   misled by a 3-trade bucket with 100% win rate.
5. Emit a decision rubric — given the matrix, which
   `REGIME_GATE_{STRATEGY}` allow-list should ship to production?

## Non-goals

- Changing signal math in any strategy. This is a measurement pass.
- Changing the regime classifier's math. We use the exact same code Spec A
  ships; if the classifier changes, rerun this backtest.
- Live shadow-mode comparison. Spec A's Phase 5 (kill-switch + classification
  logs) covers that path after this analysis is done.
- Sub-regime conditioning (e.g., regime × subnet, or regime × time-of-day).
  Possible v2, but we start with the 2-D matrix.
- Tuning regime thresholds via sweep. Threshold sweeps live in a separate
  future spec; here we use whatever thresholds the classifier is currently
  configured with and measure the edge at those settings.

## Implementation Plan

### Phase 1 — Regime labelling pass

Add [app/backtest/regime_labeler.py](../../app/backtest/regime_labeler.py):

- Use the cached flow-history snapshots
  ([data/backtest/history/flow/](../../data/backtest/history/flow/)) as the
  labeller's data source. Those rows already carry the `ts` + `price` shape
  the classifier consumes, and the cadence (hourly by default) is denser
  than the EMA cache — the labeller walks the shared timeline directly
  without touching the live `pool_snapshots` DB.
- For each timestamp in a coarse grid (default: every `REGIME_BUCKET_HOURS`,
  aligned to the hour), run the exact same classification function Spec A
  uses from [app/strategy/regime.py](../../app/strategy/regime.py). To avoid
  divergence, extract the pure aggregation + classification math out of
  `RegimeFilter._compute_metrics` / `_classify_raw` into two module-level
  functions (`compute_regime_metrics`, `classify_regime`) that both the live
  filter and the labeller call. The refactor is behaviour-preserving for
  live.
- Apply the same debounce logic as live (`REGIME_DEBOUNCE_CYCLES`) so the
  labelled series matches what live would have seen.
- Output an in-memory `list[(ts, regime)]` sorted by `ts`. Lookup of
  "regime at arbitrary ts" is a binary search.
- Persist the labelled series to
  [data/backtest/regime_timeline.json](../../data/backtest/regime_timeline.json)
  so downstream analyses can reuse it without rerunning classification.

### Phase 2 — Trade bucketing

Extend the backtest entry points (the EMA/Flow/MR runners that call
`backtest_strategy()` in [app/backtest/engine.py](../../app/backtest/engine.py))
to attach a `regime_at_entry: str` field to every `TradeRecord` (see the
dataclass at [app/backtest/engine.py](../../app/backtest/engine.py) line 42).

Implementation:

- After all trades are collected, iterate and set
  `trade.regime_at_entry = labeler.regime_at(trade.entry_ts)`.
- For Flow's `FlowTradeRecord` (see [app/backtest/flow_engine.py](../../app/backtest/flow_engine.py))
  do the same. The field name is identical across strategies so the
  downstream aggregator doesn't branch.
- If the regime series has no label for a given `entry_ts` (start of the
  window, pre-warmup), bucket the trade as `UNKNOWN` and exclude it from
  the final matrix.

### Phase 3 — Per-regime aggregation

Add [app/backtest/per_regime_report.py](../../app/backtest/per_regime_report.py)
that consumes a `list[BacktestResult]` (reuses the dataclass from
[app/backtest/engine.py](../../app/backtest/engine.py) line 63) and:

- Flattens trades across all results.
- Groups by `(strategy_id, regime_at_entry)`.
- Computes the same aggregate metrics as `_compute_result()` in
  [app/backtest/engine.py](../../app/backtest/engine.py) line 507. The EMA
  and MR engines already emit `TradeRecord` rows; Flow emits
  `FlowTradeRecord`. The aggregator normalises Flow trades into the
  `TradeRecord` shape before calling `_compute_result` so only one metrics
  implementation exists.
- Writes a single CSV
  [data/backtest/results/per_regime_edge_{ts}.csv](../../data/backtest/results/)
  with columns:

```
strategy_id, regime, total_trades, winning_trades, losing_trades,
win_rate, avg_win_pct, avg_loss_pct, expectancy, profit_factor,
total_pnl_pct, total_pnl_tao, max_drawdown_pct, sharpe_ratio,
avg_hold_hours, significant, recommendation
```

- `significant` is a boolean from the significance filter (Phase 4).
- `recommendation` is one of `ENABLE` / `DISABLE` / `NEUTRAL` from the
  decision rubric (Phase 5).

A matching JSON is written alongside so Python consumers don't re-parse the
CSV.

### Phase 4 — Significance filter

Each `(strategy, regime)` cell is tagged `significant = True` iff:

- `total_trades >= MIN_TRADES_PER_CELL` (default 20 — chosen so a
  `win_rate` estimate has a standard error below ~10 percentage points
  assuming a 50% underlying rate).
- Wilson 95% lower bound on `win_rate` is reported next to the point
  estimate for transparency (stored as `win_rate_lcb_95` in the CSV).

Cells below the threshold are still in the CSV but get
`recommendation=NEUTRAL` regardless of their numbers, so the user doesn't
gate production decisions on thin samples.

### Phase 5 — Decision rubric

For each significant cell, compute `edge_score = expectancy` (already
in units of percent per trade, compounded by profit factor). Apply:

- `edge_score >= +0.5%` AND `profit_factor >= 1.3` → `ENABLE`
- `edge_score <= -0.2%` OR `profit_factor < 0.9` → `DISABLE`
- otherwise → `NEUTRAL`

The final production `REGIME_GATE_{STRATEGY}` env value is built from the
cells where `recommendation == ENABLE`, in lowercase, comma-joined. The
run prints this line ready to paste into `.env`:

```
Suggested .env updates:
  REGIME_GATE_EMA=trending,dispersed
  REGIME_GATE_FLOW=trending
  REGIME_GATE_MR=choppy
  REGIME_GATE_YIELD=all   # always; yield is regime-agnostic
```

Thresholds for the rubric (`0.5%`, `1.3`, `-0.2%`, `0.9`) are exposed as
CLI flags on the runner so the user can tighten them.

### Phase 6 — CLI entry point

Extend [app/backtest/__main__.py](../../app/backtest/__main__.py) with a
`per-regime` subcommand (parallel to `flow`, `meanrev`, `compare`) that:

1. Runs the existing EMA backtest (every strategy variant in
   [app/backtest/strategies.py](../../app/backtest/strategies.py) STRATEGY_MAP).
2. Runs the Flow backtest (using the engine from
   [backtest-pool-flow-momentum.md](../backtest-pool-flow-momentum.md)
   once it lands).
3. Runs the Mean-Rev backtest (using the meanrev path already wired in
   [app/backtest/engine.py](../../app/backtest/engine.py)).
4. Labels all trades via Phase 1–2.
5. Aggregates and writes the CSV/JSON via Phase 3.
6. Prints the significance-filtered matrix and the rubric output.

### Phase 7 — Report format

Extend [app/backtest/report.py](../../app/backtest/report.py) with one
function: `print_per_regime_matrix(cells: list[RegimeCell])` that pretty-
prints the matrix as a table (rows = strategies, cols = regimes, values
= `win_rate% / trades`; cells tagged with `*` if insignificant). This is
the at-a-glance view; the CSV is the pivotable authoritative copy.

Example intended output:

```
  PER-REGIME EDGE MATRIX (primary window = 90d)
  ------------------------------------------------------------
  Strategy     TRENDING       DISPERSED     CHOPPY        DEAD
  ------------------------------------------------------------
  EMA A1       62% / 48       55% / 31      41% / 27*     33% / 9*
  EMA A2       58% / 52       52% / 28      39% / 34      30% / 11*
  Flow v1      64% / 41       51% / 22*     44% / 18*     28% / 7*
  MeanRev      38% / 15*      49% / 23      61% / 44      52% / 19*
  ------------------------------------------------------------
  * = below MIN_TRADES_PER_CELL (20), treat as NEUTRAL
```

## Definition of Done

- `app/backtest/regime_labeler.py` labels the full `pool_snapshots` history
  using Spec A's classifier.
- `app/backtest/per_regime_report.py` writes one CSV + JSON per run with
  `(strategy, regime)` cells and significance/recommendation columns.
- Running the backtest with `--per-regime` produces
  [data/backtest/results/per_regime_edge_{ts}.csv](../../data/backtest/results/)
  and prints the matrix + suggested `.env` lines.
- At least one end-to-end run exists over a ≥90-day window covering all
  three live strategies (EMA, Flow, MR) so Spec A's threshold retune
  (Spec A Phase 8) has data to consume.

## Open Questions

- Window choice: use the same 90d window as the Flow backtest, or a longer
  window to get more regime diversity? Longer windows catch more regime
  transitions but older subnets may have different pool dynamics than current.
  Start at 90d, expand if any regime bucket has fewer than 40 trades across
  all strategies.
- Attribution at entry vs average over hold: we bucket by regime *at entry*
  because that's what the live filter can act on, but a TRENDING trade that
  exits during CHOPPY has PnL partially driven by the later regime. Worth
  a secondary report (regime-transition-conditioned PnL) later; not v1.
- How to handle `DISPERSED` if the classifier rarely labels any window as
  dispersed? If the cell has <20 trades for every strategy, we either (a)
  merge DISPERSED into TRENDING for gating purposes, or (b) loosen the
  dispersion threshold in Spec A. Decision deferred to run-time.
- Should we break Sharpe out by regime given that per-regime trade counts
  are small and Sharpe needs samples? Report it anyway with a caveat; the
  significance flag already warns the reader.
- Regime debounce in the labeller vs live: if debounce settings change
  between backtest and live, the labelled history diverges from what live
  sees. Spec A and this spec read the same `settings.REGIME_*` values so
  they stay in lockstep by construction, but a CI check asserting this
  would be cheap insurance.
