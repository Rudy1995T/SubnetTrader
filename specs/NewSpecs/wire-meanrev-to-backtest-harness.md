# Spec: Wire Mean-Reversion Strategy into the Backtest Harness

**Branch:** `2_EMA_Strategies`
**Date:** 2026-04-21
**Status:** Draft
**Depends on:** [strategy-mean-reversion.md](../strategy-mean-reversion.md), [replace-scalper-with-mean-reversion.md](../replace-scalper-with-mean-reversion.md) (signal math lives in [app/strategy/mean_reversion.py](../../app/strategy/mean_reversion.py)), [app/backtest/engine.py](../../app/backtest/engine.py) (existing dispatch on `strategy_type`)

## Motivation

Three strategies now co-exist — dual-EMA (Scalper/Trend), Pool Flow Momentum,
and Mean-Reversion — but only EMA has a fully instrumented backtest path.
Flow has its own engine via [backtest-pool-flow-momentum.md](../backtest-pool-flow-momentum.md).
Mean-reversion has a `strategy_type == "meanrev"` branch wired into
[app/backtest/engine.py](../../app/backtest/engine.py) and a single config row
(`F1`) in [app/backtest/strategies.py](../../app/backtest/strategies.py), but
there is no dedicated CLI surface, no parameter sweep grid, no
meanrev-specific metric reporting, and no side-by-side comparison artifact.

Before we can answer "which strategy does what regime" we need an
apples-to-apples backtest output for meanrev with the same window set, same
history cache, same slippage model, and a shared results format. The
plumbing is 80% there; this spec is the remaining 20%.

## Goals

1. Run meanrev through [app/backtest/engine.py](../../app/backtest/engine.py)
   across the same `LOOKBACK_WINDOWS` the EMA sweep uses (7 / 14 / 30 / 90 /
   120 / 150 days) without duplicating engine logic.
2. Produce a parameter sweep (RSI entry threshold, BB std, take-profit,
   stop-loss, holding period) with one CSV/JSON row per config so we can pick
   a production setting before cutting `F1` over to live.
3. Emit the same `BacktestResult` metric set as EMA plus meanrev-specific
   diagnostics (exit-reason breakdown including `BB_MID_CROSS` /
   `RSI_OVERBOUGHT`, avg hold time, trade count per subnet) so the report
   writer in [app/backtest/report.py](../../app/backtest/report.py) needs no
   new columns — only a richer `strategies` list.
4. Let the operator run `python -m app.backtest --strategy F1 --window 30` and
   `python -m app.backtest meanrev --sweep` and get comparable numbers next
   to the existing EMA and flow results in `data/backtest/results/`.

## Non-goals

- API or UI wiring for live meanrev output. That is covered in
  [wire-meanrev-to-api-and-ui.md](../wire-meanrev-to-api-and-ui.md) and is
  explicitly out of scope here — this spec touches only the offline
  backtest path.
- Changing signal math in
  [app/strategy/mean_reversion.py](../../app/strategy/mean_reversion.py).
  Entry/exit rules stay exactly as implemented; the backtest reads the same
  functions the live manager will.
- Extending the historical data fetcher. Meanrev uses the same 1h candles
  from [app/backtest/data_loader.py](../../app/backtest/data_loader.py) that
  EMA already consumes — no new endpoint, no new cache directory.
- Tuning EMA or Flow parameters. Those have their own backtest artifacts.
- Any database, chain executor, or Telegram changes.

## Implementation Plan

### Phase 1 — Config surface

1. **[app/backtest/strategies.py](../../app/backtest/strategies.py)** —
   expand the `F*` block. `F1` stays as the current-spec baseline (RSI 30 /
   65, BB 20/2.0, TP 8%, SL 5%, hold 24h, cooldown 2h, pot 5 τ, 25% size,
   4 slots, 1h candles). Add:

   | ID | Tag | Delta from F1 |
   |---|---|---|
   | `F2` | `meanrev_loose` | `rsi_entry=35`, `bb_std=2.0` |
   | `F3` | `meanrev_tight` | `rsi_entry=25`, `bb_std=2.5` |
   | `F4` | `meanrev_4h` | `candle_timeframe_hours=4` (matches EMA TF) |
   | `F5` | `meanrev_longhold` | `max_holding_hours=72`, `take_profit_pct=12` |
   | `F6` | `meanrev_tight_stop` | `stop_loss_pct=3.0` |
   | `F7` | `meanrev_wide_stop` | `stop_loss_pct=8.0`, `take_profit_pct=12` |
   | `F8` | `meanrev_no_bbmid` | `bb_mid_exit=False` (RSI-only exit) |

   Add `MEAN_REVERSION = [F1, F2, F3, F4, F5, F6, F7, F8]` to
   `ALL_STRATEGIES` (already grouped — just widen the list).

2. No new fields on `BacktestStrategyConfig` — every meanrev knob
   (`rsi_entry`, `rsi_exit`, `bb_period`, `bb_std`, `bb_mid_exit`,
   `rsi_period`) already exists on the dataclass.

### Phase 2 — CLI subcommand

3. **[app/backtest/__main__.py](../../app/backtest/__main__.py)** — mirror
   the `flow` / `probe` subcommand pattern in `_dispatch_subcommand()`. Add
   a `meanrev` sub-verb that runs the `F*` group, defaulting to all windows:

   ```
   python -m app.backtest meanrev                      # F1..F8 × all windows
   python -m app.backtest meanrev --strategy F1        # single config, all windows
   python -m app.backtest meanrev --sweep              # all F* × all windows, CSV export
   python -m app.backtest meanrev --window 30 --strategy F1
   ```

   Internally reuses `run_backtest()` and the existing report writers. No new
   data fetch — if cache is cold, emit the same hint the EMA path does
   (`run with --fetch-only first`).

   The existing `--strategy F1` path under the default CLI already works; the
   subcommand is a convenience wrapper that (a) auto-selects `MEAN_REVERSION`
   as the strategy list and (b) names the output file
   `meanrev_<timestamp>.{csv,json}` so it does not collide with EMA
   artifacts in `data/backtest/results/`.

### Phase 3 — Report plumbing

4. **[app/backtest/report.py](../../app/backtest/report.py)** — no schema
   change. `print_ranking_table()` already sorts by expectancy regardless of
   strategy family, `print_exit_breakdown()` walks `result.exit_reasons`
   dynamically (so `BB_MID_CROSS` and `RSI_OVERBOUGHT` render automatically),
   `save_results_json` already dumps `exit_reasons` and
   `trade_count_by_subnet`. One small addition:

   - `save_results_json()` / `save_results_csv()` accept an optional
     `filename_prefix` argument (default `"backtest"`) so the meanrev sub-
     command can write `meanrev_<timestamp>.json` instead of mingling into
     the EMA files. Same for `flow_<timestamp>` when it uses this path
     (already covered in its own writers — leave those alone).

5. Production-marker logic in `print_ranking_table()` currently hard-codes
   `{"A1", "A2"}`. Add a `production_ids` override when the caller is the
   meanrev subcommand — it should mark whichever `F*` gets promoted (initially
   none, so pass an empty set). No behavioural change for the EMA path.

### Phase 4 — Data requirements sanity check

6. Meanrev needs `bb_period + 1 = 21` bars of warmup at the target
   timeframe. At `candle_timeframe_hours=1` that is 21 hourly bars; the EMA
   fetcher (`fetch_all()` in
   [app/backtest/data_loader.py](../../app/backtest/data_loader.py), MAX_LIMIT
   = 3600 hourly points) requests 1h data — in practice Taostats currently
   returns ~200 daily rows per netuid (the bug tracked in
   [fix-deep-history-resolution-mismatch.md](../fix-deep-history-resolution-mismatch.md)),
   so `_detect_data_resolution_hours()` upscales every `F*` run to 24h
   until that fetch is fixed. That still clears the 21-bar warmup for the
   cached 200-row window and is sufficient for wiring validation; signal
   density will improve once hourly resolution lands. No new data
   acquisition work in this spec; this phase is only a guard:

   - `backtest_subnet()` already computes `min_bars = cfg.bb_period + 1`
     when `strategy_type == "meanrev"` and returns `[]` below that
     threshold. Confirm (via `tests/test_mean_reversion_backtest.py`, new)
     that subnets with < 21 resampled bars yield 0 trades rather than
     crashing.
   - `_detect_data_resolution_hours()` must keep 1h meanrev configs at 1h
     when the underlying cache is hourly. If the cache degrades to 4h (the
     bug path fixed in [fix-deep-history-resolution-mismatch.md](../fix-deep-history-resolution-mismatch.md)),
     `F4` (4h meanrev) still works; `F1`..`F3` auto-upscale to 4h with a
     logged warning rather than producing spurious sub-bar signals.

### Phase 5 — Metrics & comparison

7. The existing `BacktestResult` already reports the five metrics the user
   called out:

   - `win_rate` (win rate)
   - `sharpe_ratio` (Sharpe, annualised by `trades_per_year` heuristic)
   - `avg_hold_hours` (avg hold time)
   - `max_drawdown_pct` (max DD on the equity curve, in % of pot)
   - `trade_count_by_subnet` (emitted by
     [_trade_count_by_subnet()](../../app/backtest/report.py#L308))

   All render through `print_ranking_table()` + `print_subnet_performance()`
   unchanged. No metric work required beyond the sweep CSV.

8. **Side-by-side comparison artifact** — add a small helper in
   [app/backtest/report.py](../../app/backtest/report.py) (or new sibling
   module `compare.py`) that loads the latest `backtest_*.json` (EMA),
   `meanrev_*.json`, and `flow_*.json` and prints a single ranking across
   all strategies for a given window, sorted by expectancy. One-shot, no
   persistent state. Invocation:

   ```
   python -m app.backtest compare --window 30
   ```

   Output columns match `print_ranking_table` plus a `family` column
   (`ema` / `meanrev` / `flow`). This is the "apples-to-apples" deliverable
   — everything else in this spec is prep work to make that comparison
   mean something.

### Phase 6 — Tests

9. **`tests/test_mean_reversion_backtest.py`** (new) — three deterministic
   cases:

   - **Insufficient history:** 15 bars of synthetic price data returns `[]`
     from `backtest_subnet` under `F1`, no exception.
   - **Clean RSI dip:** a 40-bar series with a controlled drop to RSI ≈ 25
     and a recovery back through the lower BB produces exactly one trade
     with `exit_reason == "BB_MID_CROSS"` or `"TAKE_PROFIT"`.
   - **Time-stop path:** a flat series after entry yields `exit_reason ==
     "TIME_STOP"` after `max_holding_hours`.

10. Existing `tests/test_flow_engine.py` / `tests/test_flow_signals.py`
    stay untouched. EMA tests stay untouched.

## Definition of Done

- `python -m app.backtest --strategy F1 --window 30` runs to completion
  against cached history and writes
  `data/backtest/results/backtest_<ts>.{json,csv}` with meanrev rows
  populated (`BB_MID_CROSS` / `RSI_OVERBOUGHT` visible in `exit_reasons`).
- `python -m app.backtest meanrev --sweep` runs the full `F1`..`F8` × 6-
  window matrix in under 5 minutes on the Pi using the EMA candle cache,
  writing `data/backtest/results/meanrev_<ts>.{json,csv}`.
- `python -m app.backtest compare --window 30` prints one ranking table
  containing EMA (`A1`, `A2`, …), meanrev (`F1`..`F8`), and the latest
  flow sweep row(s), sorted by expectancy.
- `BacktestResult.exit_reasons` for meanrev runs includes at least
  `BB_MID_CROSS`, `RSI_OVERBOUGHT`, `TAKE_PROFIT`, `STOP_LOSS`, `TIME_STOP`
  wherever they fire (zeros allowed; keys must exist when triggered).
- `tests/test_mean_reversion_backtest.py` passes on the Pi venv.
- No edits to
  [app/strategy/mean_reversion.py](../../app/strategy/mean_reversion.py),
  [app/portfolio/ema_manager.py](../../app/portfolio/ema_manager.py),
  [app/main.py](../../app/main.py), the frontend, or the widget.
- No new Taostats fetches; cache hits only. If cache is cold the CLI prints
  the same `--fetch-only` hint the EMA path uses and exits cleanly.

## Open Questions

- **Pot sizing for comparison.** `F1` uses `pot_tao=5.0`, EMA configs use
  `10.0`. `total_pnl_pct` normalises by pot in
  [_compute_result()](../../app/backtest/engine.py#L507) so the percent
  metric is comparable, but absolute TAO PnL is not. Do we add a
  `pnl_tao_normalised_to_10tao` column for like-for-like absolute
  comparison, or document the 5-τ-vs-10-τ convention in the compare
  output and let the reader scale mentally?
- **Should `F4` (4h meanrev) exist given that the strategy spec mandates
  1h candles?** It is useful as a sensitivity check ("does meanrev survive
  the EMA timeframe?") but will likely underperform. Keep it for
  diagnostic value, or drop to save sweep runtime?
- **Do we include cross-strategy exclusion in the backtest?** Live EMA and
  meanrev coordinate via `_companion_netuids_cb`; the offline engine is
  per-strategy and will double-count subnets where both signals fire. The
  `ema_overlap_rate` metric exists for flow; a symmetric
  `meanrev_vs_ema_overlap` would be cheap and answer "is meanrev actually
  complementary to Trend?". Worth adding in this spec or defer to a
  follow-up?
- **Sweep grid density.** Eight configs × six windows = 48 runs. A full
  grid-search over `{rsi_entry, bb_std, take_profit, stop_loss,
  max_holding_hours}` at 3 values each is 3⁵ = 243 × 6 = 1458 runs —
  probably still sub-hour on cached data but crowds the CSV. Is the
  hand-picked `F1..F8` table sufficient, or do we wire a generic
  `--grid` flag?
