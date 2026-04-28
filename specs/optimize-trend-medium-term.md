# Spec: Optimize Trend Strategy (B) — Medium-Term Code Changes

## Overview

Three code-level improvements to Strategy B (trend, fast=3, slow=18) that require
changes to the EMA manager, signal functions, and/or database schema. These build
on the quick-win config changes (wider stops, shorter time stop, RSI filter) in
`specs/optimize-trend-quick-wins.md`.

**Why these three:** The backtest showed TIME_STOP dominates exits (52%), confirm=2
outperforms confirm=3, and fixed trailing stops are suboptimal across varied subnet
volatilities. These changes address the root causes.

---

## Change 1: ATR-Based Dynamic Trailing Stop

### Problem

The current trailing stop is a fixed percentage (10% after quick-win change). This
is too loose for calm subnets (giving back profit unnecessarily) and too tight for
volatile ones (getting shaken out of good trends). The existing
`EMA_B_TRAILING_STOP_DYNAMIC` flag uses rolling close-to-close volatility, which
underestimates true range because it ignores intra-candle wicks.

### Design

Compute true ATR from OHLC candle data and use it to set a per-position trailing
distance that adapts to each subnet's current volatility.

**ATR calculation** (`app/strategy/indicators.py`):

```python
def compute_atr(candles: list[Candle], period: int = 14) -> list[float]:
    """Compute Average True Range from OHLC candles.

    True Range = max(high-low, abs(high-prev_close), abs(low-prev_close))
    ATR = EMA of True Range over `period`.
    """
```

**Trailing distance formula:**
```
trailing_pct = (atr / current_price) * multiplier * 100
trailing_pct = clamp(trailing_pct, floor_pct, cap_pct)
```

**Exit watcher change** (`app/portfolio/ema_manager.py`):

In the exit check loop, when `trailing_stop_dynamic` is True:
1. Fetch cached candles for the subnet (already available from `_warm_history`)
2. Compute ATR from the last N candles
3. Derive trailing_pct from ATR instead of the fixed config value
4. Apply floor/cap to prevent extreme values

### New Config Variables

| Variable | Default | Description |
|---|---|---|
| `EMA_B_ATR_PERIOD` | 14 | Number of candles for ATR calculation |
| `EMA_B_ATR_MULTIPLIER` | 2.0 | ATR × multiplier = trailing distance |
| `EMA_B_TRAILING_MIN_PCT` | 3.0 | Floor — never trail tighter than 3% |
| `EMA_B_TRAILING_MAX_PCT` | 15.0 | Cap — never trail wider than 15% |

### Files to Modify

| File | Change |
|---|---|
| `app/strategy/indicators.py` | Add `compute_atr(candles, period)` function |
| `app/portfolio/ema_manager.py` | Replace fixed trailing % with ATR-derived value in exit watcher |
| `app/config.py` | Add `EMA_B_ATR_PERIOD`, `EMA_B_ATR_MULTIPLIER`, `EMA_B_TRAILING_MIN_PCT`, `EMA_B_TRAILING_MAX_PCT` |

---

## Change 2: Reduce Confirm Bars from 3 to 2

### Problem

E2 (confirm=2) outperforms E3 (confirm=4) and current production (confirm=3) in
the backtest: +2.64 expectancy at 150d with 967 trades vs A1's +2.81 with 763
trades. The extra confirmation bar delays entries, missing early momentum on
legitimate EMA crossovers.

### Design

This is primarily a config change but included here because it benefits from a
**stale signal guard** — with fewer confirm bars, false signals increase slightly.
Adding a price-velocity check compensates.

**Config change:**
```
EMA_B_CONFIRM_BARS=2
EMA_B_MTF_CONFIRM_BARS=2
```

**Stale signal guard** (optional hardening in `ema_manager.py`):

When confirm=2, add a check that the most recent candle's close is within
`EMA_B_ENTRY_PRICE_DRIFT_PCT` (currently 5%) of the signal price. This prevents
acting on a signal generated at bar N when the price has already moved significantly
by the time the entry watcher fires.

This guard already exists in the entry logic (`EMA_B_ENTRY_PRICE_DRIFT_PCT`), so
this change is really just the config update plus documentation that the drift guard
covers the confirm=2 risk.

### Files to Modify

| File | Change |
|---|---|
| `app/config.py` | Change defaults: `EMA_B_CONFIRM_BARS=2`, `EMA_B_MTF_CONFIRM_BARS=2` |
| `.env` | Update values |

---

## Change 3: Hybrid Time Exit (Partial Scale-Out)

### Problem

Even with the quick-win reduction to 120h, TIME_STOP will still be a significant
exit reason. Many time-stopped positions are still profitable — they just haven't
hit TP or trailing stop yet. A hard 100% exit throws away potential upside.

### Design

Replace the hard time stop with a two-stage scale-out:

1. **Stage 1 — Partial exit at 120h:** Close 50% of the position using `unstake()`
   (partial), tighten trailing stop to 60% of current trailing distance for the
   remainder
2. **Stage 2 — Full exit at 168h:** Close whatever remains (safety net)

**Position state tracking:**

Add a `scaled_out` flag and `scaled_out_ts` to the position record so the exit
watcher knows which stage the position is in.

**Partial unstake flow:**
```python
# Stage 1: 50% exit
half_alpha = position.current_alpha * partial_exit_pct
result = await executor.unstake(wallet, netuid, hotkey, half_alpha)
# Update position: reduce amount_alpha, record partial PnL
# Tighten trailing: trailing_pct *= partial_trailing_tighten (0.6)
```

**Exit watcher changes:**
```python
hold_hours = (now - entry_ts).total_seconds() / 3600

if hold_hours >= partial_exit_hours and not position.scaled_out:
    # Stage 1: partial exit
    await self._partial_exit(position, partial_exit_pct)
    position.scaled_out = True
    position.trailing_pct *= partial_trailing_tighten

elif hold_hours >= final_time_stop_hours:
    # Stage 2: full exit of remainder
    await self._close_position(position, reason="TIME_STOP")
```

### New Config Variables

| Variable | Default | Description |
|---|---|---|
| `EMA_B_PARTIAL_EXIT_HOURS` | 120 | Hours before first partial exit |
| `EMA_B_PARTIAL_EXIT_PCT` | 0.50 | Fraction to exit at stage 1 (50%) |
| `EMA_B_FINAL_TIME_STOP_HOURS` | 168 | Hard stop for remainder |
| `EMA_B_PARTIAL_TRAILING_TIGHTEN` | 0.60 | Multiply trailing % by this after partial exit |

### DB Migration

Add columns to `ema_positions` table:

```sql
ALTER TABLE ema_positions ADD COLUMN scaled_out INTEGER DEFAULT 0;
ALTER TABLE ema_positions ADD COLUMN scaled_out_ts TEXT DEFAULT NULL;
ALTER TABLE ema_positions ADD COLUMN partial_pnl_tao REAL DEFAULT 0.0;
```

The migration should follow the existing pattern in `app/storage/db.py` — add to
the `_run_migrations()` method.

### Files to Modify

| File | Change |
|---|---|
| `app/config.py` | Add 4 new `EMA_B_PARTIAL_*` config vars |
| `app/storage/db.py` | Add migration for `scaled_out`, `scaled_out_ts`, `partial_pnl_tao` columns |
| `app/portfolio/ema_manager.py` | Add `_partial_exit()` method; modify time-stop logic in exit watcher; update `EmaPosition` dataclass |
| `app/chain/executor.py` | Verify `unstake()` (partial) works correctly (it calls `remove_stake` not `remove_stake_full_limit`) |

---

## Implementation Order

1. **ATR trailing stop** (Change 1) — Lowest risk, most independent. Can be tested
   immediately with existing positions.
2. **Confirm bars reduction** (Change 2) — Config-only in practice. Deploy after
   ATR trailing is live so the wider trailing catches any false signals.
3. **Hybrid time exit** (Change 3) — Most complex, requires DB migration and new
   exit flow. Deploy last, after the other two are validated.

---

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| ATR too volatile → trailing distance oscillates wildly | Low | Medium | Floor/cap (3–15%) prevent extremes; ATR is inherently smoothed |
| Confirm=2 increases false entries | Medium | Low | Price drift guard + RSI filter (quick-win) compensate; monitor win rate |
| Partial unstake fails mid-exit → position in unknown state | Low | High | Post-exit verification loop already exists; add `scaled_out` state to prevent double-partial |
| Partial exit leaves dust alpha that can't be unstaked | Low | Low | If remaining alpha < min threshold, exit fully instead of partial |
| DB migration on live DB | Low | Medium | SQLite ALTER TABLE ADD COLUMN is safe with WAL mode; no downtime |

---

## Testing Plan

### ATR Trailing Stop
- [ ] Unit test: `compute_atr()` against known OHLC data (compare to TradingView ATR)
- [ ] Verify ATR-derived trailing % stays within floor/cap bounds
- [ ] Dry-run: log ATR trailing % per subnet for 24h, compare to fixed 10%
- [ ] Confirm exits fire at correct levels on volatile vs calm subnets

### Confirm Bars
- [ ] Verify `dual_ema_signal()` returns correct signals with confirm=2
- [ ] Monitor entry rate for first week — should see ~25% more entries than confirm=3
- [ ] Check win rate hasn't degraded vs confirm=3 period

### Hybrid Time Exit
- [ ] Unit test: partial exit flow with mock executor
- [ ] Verify DB migration adds columns without data loss
- [ ] Test with DRY_RUN=True: confirm staged exit logic triggers at correct hours
- [ ] Verify position tracking: `scaled_out` flag persists across bot restarts
- [ ] Edge case: position hits trailing stop between stage 1 and stage 2
- [ ] Edge case: partial alpha < min unstake threshold → falls through to full exit

---

## Success Criteria

Monitor over 30 days post-deployment:

- [ ] ATR trailing: positions in volatile subnets trail wider, calm subnets trail tighter
      (log trailing % per exit, expect std dev across subnets > 2%)
- [ ] TIME_STOP exits drop below 25% of all Strategy B exits (was ~52%)
- [ ] Partial exits capture profit on 60%+ of stage-1 exits (measured by partial_pnl_tao > 0)
- [ ] Overall Strategy B expectancy ≥ 2.5 on next 90d backtest
- [ ] No stuck positions from partial exit failures
