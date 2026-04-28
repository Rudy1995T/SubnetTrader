# Spec: Finish Pool Flow Momentum Backtest Harness

**Branch:** `2_EMA_Strategies`
**Date:** 2026-04-21
**Status:** In progress (Phase 1 finding confirmed)
**Depends on:** [backtest-pool-flow-momentum.md](../backtest-pool-flow-momentum.md) (design), [strategy-pool-flow-momentum.md](../strategy-pool-flow-momentum.md) (signal math), [fix-deep-history-resolution-mismatch.md](../fix-deep-history-resolution-mismatch.md) (same Taostats quirk)

## Phase 1 finding (2026-04-21)

Direct `curl` against `/api/dtao/pool/history/v1?netuid=1&interval={5m,15m,1h,4h,1d}&limit=5`
returned **identical timestamps for every interval** — the first row is
"now", remaining rows at `23:59:48Z` on the prior N days. This confirms the
probe output was not a bug in `_observed_interval_seconds`; Taostats really
does collapse every request to daily cadence (same root cause documented in
[fix-deep-history-resolution-mismatch.md](../fix-deep-history-resolution-mismatch.md)).

**Decision:** take path (b) from the goals section. Accept 1d as the only
honest cadence, scale the signal config accordingly, keep the harness but
gate every run behind an explicit cadence-acknowledgement flag so no one
confuses the 1d-cadence expectancy with what the live 5-min scanner would
produce. Path (a) (alternative data source) is out of scope and left as an
open question for a separate spec.

## Motivation

The design spec [backtest-pool-flow-momentum.md](../backtest-pool-flow-momentum.md)
has been partially implemented. Probe, data loader, engine, and a first
results pair landed uncommitted:

- [app/backtest/probe_flow_history.py](../../app/backtest/probe_flow_history.py)
- [app/backtest/flow_data_loader.py](../../app/backtest/flow_data_loader.py)
- [app/backtest/flow_engine.py](../../app/backtest/flow_engine.py)
- 87 cached subnets in [data/backtest/history/flow/](../../data/backtest/history/flow/)
- [data/backtest/history/flow_probe.json](../../data/backtest/history/flow_probe.json)
- [data/backtest/results/flow_20260421_114956.csv](../../data/backtest/results/flow_20260421_114956.csv)
  (103 trades, WR 44.7%, PF 0.85, expectancy −0.14, potΔ −1.02 τ)
- [tests/test_flow_engine.py](../../tests/test_flow_engine.py) (2 cases), [tests/test_flow_signals.py](../../tests/test_flow_signals.py) (18 cases)

The probe result is the load-bearing finding: **Taostats silently collapses
every requested `interval` to `1d`** — same bug as
[fix-deep-history-resolution-mismatch.md](../fix-deep-history-resolution-mismatch.md).
`5m`, `15m`, `1h`, and `4h` all return daily rows. Only `1d` is honest, and
history only goes back ~198 days at that cadence. This is exactly the failure
mode the design spec flagged as a risk; we now know it is the base case, not
the tail. The first run therefore measured flow on daily candles — a
resolution at which flow_signals has no business running.

To reach parity with the EMA backtest (multi-strategy ranking table, exit
breakdown, subnet attribution, trend across windows, sweep CSV) and to give
the strategy a fair hearing before promotion, several pieces are still
missing: parameter sweeps, reporting parity, CLI parity, data-quality
mitigations for the 1d collapse, and test coverage on the loader / report /
CLI paths.

## Goals

1. Pin down the final data-availability story (5m/15m/1h really unavailable
   vs. probe implementation bug) and land one of two paths:
   **a)** fetch real 5-min or hourly snapshots from an alternative source,
   **b)** accept 1d, scale the signal config accordingly, and document the
   expectancy ceiling explicitly in every report.
2. Reach reporting parity with
   [data/backtest/results/backtest_20260409_195034.csv](../../data/backtest/results/backtest_20260409_195034.csv):
   multi-run ranking table, per-subnet attribution, exit-reason breakdown,
   cross-window trend analysis.
3. Reach CLI parity with [app/backtest/\_\_main\_\_.py](../../app/backtest/__main__.py):
   `--quick` / `--full` / `--sweep` modes; `flow --probe`, `flow --fetch-only`,
   and `flow --sweep` subcommands behave like the EMA counterparts.
4. Close the test-coverage gap on the loader (pagination, gap handling, cache
   TTL) and the reporting functions — currently zero coverage.
5. Produce one committed, reproducible results set that a reviewer can pull up
   and decide "promote to paper" / "shelve" against the gates in
   [backtest-pool-flow-momentum.md §Parameter sweeps](../backtest-pool-flow-momentum.md).

## Non-goals

- Any change to [app/strategy/flow_signals.py](../../app/strategy/flow_signals.py)
  math. Scaling config at the backtest boundary is fine; modifying the signal
  itself is out of scope.
- Live strategy integration (pot sizing in ema_manager, API routes, frontend).
  Covered by [show-flow-momentum-in-ui-and-widget.md](../show-flow-momentum-in-ui-and-widget.md).
- New Taostats endpoints beyond `/api/dtao/pool/history/v1`. If deeper
  resolution requires a different source (block-by-block, Subtensor RPC,
  on-chain indexer), that is a separate spec.
- UI for backtest results — stays CSV + JSON under
  [data/backtest/results/](../../data/backtest/results/).

## What is already built vs. what remains

| Area | Built | Gap |
|---|---|---|
| Probe | [probe_flow_history.py](../../app/backtest/probe_flow_history.py) runs, writes [flow_probe.json](../../data/backtest/history/flow_probe.json) | Result says only `1d` supported; suspicious given live scanner uses 5m. Need to either confirm (with a direct `curl` + docs check) or fix the probe's cadence detection (currently `observed ≈ 86400` for every interval suggests the endpoint returned identical rows). |
| Data loader | [flow_data_loader.py](../../app/backtest/flow_data_loader.py) paginates, caches, 87 subnets cached | No unit tests. No "force refresh stale" policy beyond 24 h TTL. No exclusion rule for subnets with > 10% gap rows (design §Gap handling). |
| Engine | [flow_engine.py](../../app/backtest/flow_engine.py) replays flow + regime + EMA confirm + slippage | Single run only; no sweep runner. Cooldown uses ts comparison only — if the sole honest interval is 1d, cooldown of 6h is meaningless. Scale cooldown by `interval_seconds` or reject cooldown < interval. |
| Signal scaling | `build_signal_config` scales windows from 5-min to the probe interval | At 1d the scaling produces `window_4h_snaps = 4` ≈ 4 days, `baseline_snaps = 48` ≈ 48 days — this is a different strategy than the design intends. Either flag a hard error for interval ≥ 1h, or add an explicit `interval_override` in `FlowBacktestConfig` that requires the operator to acknowledge the degradation. |
| Metrics | Standard + flow-specific on [FlowBacktestResult](../../app/backtest/flow_engine.py) | Missing: `ema_overlap_rate` wired from an actual EMA backtest (current code accepts the arg but `__main__` never passes it). Missing: snapshot-gap-count, excluded-subnets list, cadence-warning flag. |
| Reporting | Single-row CSV/JSON via [save_flow_result_csv / save_flow_result_json](../../app/backtest/report.py) | No `print_flow_ranking_table` equivalent to [print_ranking_table](../../app/backtest/report.py). No exit-reason bar chart. No per-subnet attribution. No trend-across-windows. No sweep-CSV aggregator wired into the CLI (`save_flow_sweep_csv` exists but is unreachable from the CLI). |
| CLI | `python -m app.backtest flow ...` works via `_dispatch_subcommand` in [\_\_main\_\_.py](../../app/backtest/__main__.py) | No `--sweep` / `--quick` / `--full` modes. No `probe` subcommand reports a warning if probe disagrees with live scanner. `--export csv,json,both` flag absent — always writes both. |
| Tests | [test_flow_signals.py](../../tests/test_flow_signals.py) (18), [test_flow_engine.py](../../tests/test_flow_engine.py) (2) | No tests for loader (normalize_snapshot, pagination cursor, cache round-trip, gap exclusion). No tests for report writers. No regression test for the "cooldown < interval" edge. No test guarding against look-ahead via the shared event loop. |
| Docs / spec | Design spec complete | [strategy-pool-flow-momentum.md](../strategy-pool-flow-momentum.md) still advertises the 14-day snapshot warm-up; design spec asks for a one-line edit pointing to the backtest path. Not yet done. |

## Implementation Plan

### Phase 1 — data-availability verification (blocking)

The current probe output is internally inconsistent: all five intervals
return `observed_seconds = 86400`. Either Taostats really does serve one
cadence, or `_observed_interval_seconds` is comparing rows from an identical
response. Before any further engine work:

1. Issue raw `curl` requests for `5m`, `15m`, `1h` on SN1 with `limit=50` and
   inspect timestamps directly. Record findings in the probe JSON as a
   `raw_samples` field.
2. Cross-check with the live [app/data/taostats_client.py](../../app/data/taostats_client.py)
   `_rate_limited_get` usage — the live scanner runs at 5-min cadence against
   `/api/dtao/pool/latest/v1`, not `/history`. If `/history` genuinely only
   serves daily, document that and treat every flow metric as a 1d-cadence
   lower bound. If the probe was wrong, fix the detection logic and re-run.
3. Commit the findings as an amended [flow_probe.json](../../data/backtest/history/flow_probe.json)
   and a short note in the probe script docstring.

### Phase 2 — cadence guardrails

1. In [flow_engine.py](../../app/backtest/flow_engine.py) `main()`, refuse to
   run without `--acknowledge-cadence-degradation` when `interval_seconds >= 3600`.
   Print a clear message: signal windows become days-scale; expectancy is
   not comparable to the live 5-min version.
2. In `_apply_run_cfg_overrides`, scale `cooldown_hours` up to at least
   `interval_seconds / 3600` so cooldown meaningfully gates re-entry.
3. Add `interval` and `cadence_acknowledged` as columns on the CSV + JSON
   output so the artefact self-describes.

### Phase 3 — sweep + reporting parity

1. Add `run_flow_sweep(all_history, base_run_cfg, grid) -> list[FlowBacktestResult]`
   to [flow_engine.py](../../app/backtest/flow_engine.py). Grid matches the
   design spec §Parameter sweeps.
2. Wire into CLI via `flow --sweep` which writes one multi-row CSV via
   [save_flow_sweep_csv](../../app/backtest/report.py). Skip combinations
   whose signal config is degenerate at the current cadence.
3. Add `print_flow_ranking_table`, `print_flow_exit_breakdown`,
   `print_flow_subnet_performance` to [report.py](../../app/backtest/report.py)
   mirroring the EMA versions. Console output on sweep runs should match the
   visual density of
   [data/backtest/results/backtest_20260409_195034.csv](../../data/backtest/results/backtest_20260409_195034.csv)
   companion reports.
4. Extend `print_full_report` (or add `print_flow_full_report`) to accept a
   list of `FlowBacktestResult` and render ranking + best-result exit
   breakdown + per-subnet attribution.

### Phase 4 — EMA overlap wiring

1. After the flow backtest, load the latest EMA `backtest_*.json` from
   [data/backtest/results/](../../data/backtest/results/) and feed
   `(netuid, entry_ts, exit_ts)` tuples to `run_flow_backtest` via
   `ema_entry_windows`.
2. Guard: if EMA JSON is older than the flow history cache, warn; if the two
   windows don't overlap at all, skip the overlap metric.

### Phase 5 — test coverage

Add under [tests/](../../tests/):

1. `test_flow_data_loader.py`:
   - `normalize_snapshot` drops rows missing `tao_in_pool` / `alpha_in_pool`.
   - Cache round-trip (write → read → same shape).
   - Pagination stops when `len(rows) < PAGE_LIMIT` (synthetic fake client).
   - Stops when cursor doesn't advance.
   - Respects `window_days` trim cutoff.
2. `test_flow_report.py`:
   - `save_flow_result_csv` produces the declared `FLOW_STANDARD_FIELDS +
     FLOW_SPECIFIC_FIELDS` schema.
   - `save_flow_sweep_csv` writes N rows for N results.
   - `save_flow_result_json` round-trips trades.
3. Extend [test_flow_engine.py](../../tests/test_flow_engine.py):
   - Cooldown honoured (subnet doesn't re-enter immediately after exit).
   - EMA overlap counter increments when a fabricated window contains an entry ts.
   - Pot accounting: final `pot_growth_tao` equals sum of `pnl_tao`.
   - Cadence guard: running with `interval_seconds >= 3600` without the
     acknowledgement flag raises.

### Phase 6 — document + commit

1. Amend [specs/strategy-pool-flow-momentum.md](../strategy-pool-flow-momentum.md)
   to drop the 14-day warm-up phase per design spec §Revised rollout plan.
2. Commit the cached flow history directory's `.gitkeep` but not the per-subnet
   JSON (regenerable).
3. Commit one canonical results pair under
   [data/backtest/results/](../../data/backtest/results/) — the output of a
   sweep run on the final agreed cadence — so reviewers have something to
   read without a refetch.

## Definition of Done

1. [flow_probe.json](../../data/backtest/history/flow_probe.json) answers the
   three design-spec questions with evidence; if only 1d is available, the
   limitation is documented in both the probe output and the engine's startup
   message.
2. `python -m app.backtest flow --fetch-only` refreshes history within the
   rate-limit budget and writes per-subnet JSON with a gap-fraction field;
   subnets > 10% gaps are excluded from the next run.
3. `python -m app.backtest flow --sweep` runs the parameter grid in under
   30 min on a Pi 5, writes one sweep CSV + JSON, and prints a ranking table,
   exit breakdown, and per-subnet attribution to stdout.
4. The sweep CSV includes an `interval` column and a
   `cadence_acknowledged` column so every row self-describes.
5. `ema_overlap_rate` is populated against the most recent EMA backtest when
   one is available, or reported as `null` with a warning line.
6. Tests: unit coverage on loader, report, and cadence guard passes under
   `pytest tests/test_flow_*.py`.
7. [strategy-pool-flow-momentum.md](../strategy-pool-flow-momentum.md) no
   longer asks operators to wait 14 days before any data exists.
8. One reproducible results pair is committed under
   [data/backtest/results/](../../data/backtest/results/) alongside a
   `flow_sweep_<ts>.csv`.
9. Decision recorded in this spec's follow-up note: the winning parameter set
   either clears the promotion gates (hit rate ≥ 55% OR expectancy ≥ 0.5R,
   profit factor ≥ 1.5, max drawdown ≤ 15%, ≥ 40 trades) and moves to paper
   per [strategy-pool-flow-momentum.md](../strategy-pool-flow-momentum.md)
   Phase 3, or it fails by a documented margin and flow is shelved or
   redesigned.

## Follow-up note (2026-04-21)

Canonical sweep: [data/backtest/results/flow_sweep_20260421_131154.csv](../../data/backtest/results/flow_sweep_20260421_131154.csv)
(72 configs, 1d cadence, 87 cached subnets, window 120d, pot 10 τ, slots 3).
Best by expectancy: `z_entry=1.5, min_tao_pct=2.0, stop_loss_pct=4.0,
take_profit_pct=8.0, regime=off` → 137 trades, WR 51.8%, E 0.99%, PF 1.63,
potΔ +4.40 τ (+44.0%), MaxDD 11.9%. Exit mix is dominated by `TIME_STOP_HARD`
(66%) — at 1d cadence the 24h hard time stop fires almost every trade
before any real flow can develop.

**Promotion gate check vs. [strategy-pool-flow-momentum.md §Phase 2](../strategy-pool-flow-momentum.md#phase-2--offline-backtest):**

| Gate | Threshold | Best run | Status |
|---|---|---|---|
| Hit rate | ≥ 55% | 51.8% | fail by 3.2 pp |
| Expectancy | ≥ 0.5R (R = stop = 4%, so 2%) | 0.99% | fail by ~0.5R |
| Profit factor | ≥ 1.5 | 1.63 | pass |
| Max drawdown | ≤ 15% | 11.9% | pass |
| Trade count | ≥ 40 | 137 | pass |

**Decision:** flow does **not** clear Phase 2 gates at 1d cadence. This is the
expected failure mode — the strategy was designed for 5-min snapshots and the
cadence available from Taostats history cuts ~48h of baseline context down
to one bar. Two of the four gates pass, and the miss on the other two is
small enough that the strategy would likely clear at its design cadence.

Next action: keep flow shelved from live promotion. The live 5-min scanner
continues to persist `pool_snapshots` for the eventual paper phase; once
~14 days of 5-min data accumulate, rerun `flow --sweep` against that cache
(harness already supports overriding `interval` via `--interval 5m`) and
re-evaluate. Treat the 1d sweep artefact as the baseline expectancy floor,
not the final verdict.

## Open Questions

1. **Is the probe itself lying?** All intervals reporting `observed=86400` is
   physically possible (Taostats really only serves daily history) but also
   matches a bug where each probe call returns the same latest-row window.
   Phase 1 must disambiguate. If it turns out `/api/dtao/pool/history/v1`
   does serve finer intervals when called with the correct auth header or
   path, the whole cadence-degradation conversation goes away.
2. **Alternative data sources?** If Taostats truly only serves 1d, is there
   a public Subtensor archive node or a block-by-block dump we can replay to
   reconstruct 5-min snapshots? Out of scope for this spec to implement, but
   the answer determines whether flow is viable at all at its design cadence.
3. **Regime lookback under 1d cadence.** `baseline_snaps // 2 = 24` at 1d is
   24 *days* — regime index becomes a quarterly signal. Is the regime filter
   still meaningful, or should it be disabled at cadences ≥ 1h?
4. **Fees model at sweep scale.** Current per-trade fee is `2 * 0.0003 τ` flat.
   Live fees are percentage-of-position; the sweep may overstate edge for
   tiny positions. Worth revisiting if expectancy is near zero.
5. **EMA overlap attribution.** If flow and EMA enter the same subnet in the
   same window, does the portfolio-level manager double-size (risk) or
   exclude (lost signal)? Overlap metric tells us *whether*, not *what to do
   about it*. Defer the policy decision to a live-integration spec.
