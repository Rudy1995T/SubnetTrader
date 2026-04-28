# Fix: Deep-History Confirmation Uses Mismatched Timeframe

## Problem

The EMA trend and mean-reversion strategies run a "deep history" confirmation
before opening a position: fetch ~14 days of 1h-resolution price data from
Taostats and re-verify the signal on that longer window. In practice this
gate is silently **rejecting valid candidates** and the cause is a
timeframe mismatch, not a trading filter.

### Evidence

- 2026-04-18 logs (trend strategy) show exactly one live candidate per cycle
  (SN48), and it is rejected every time with:
  `EMA[trend]: SN48 deep history signal=SELL (199 candles) — crossover not confirmed`
- Immediately preceding that line:
  `History candles: 92% missing (200/2385 present, 2h tf)`
- Probing the upstream endpoint directly for SN48 with
  `interval=1h, limit=336`:

  ```
  Returned 200 entries
  First ts: 2026-04-18T15:35:24Z
  Index 10:  2026-04-08T23:59:48Z   (~10 days back)
  Index 100: 2026-01-08T23:59:48Z   (~100 days back)
  Last  ts:  2025-10-01T23:59:48Z   (~200 days back)
  ```

### Root cause

Taostats `/api/dtao/pool/history/v1` **ignores the `interval` parameter**.
Regardless of what we request, it returns up to 200 samples spanning the
subnet's lifetime at ~daily cadence (one point per day, with the most recent
entry being current time).

Two downstream failures follow from that:

1. `build_candles_from_history(history, candle_hours=2)` buckets 200 daily
   samples into 2385 two-hour buckets spanning ~200 days, correctly reporting
   92% missing. It then produces ~200 candles where each "2h candle" is built
   from a single daily sample — open/high/low/close are all identical and
   `prior_close` chaining stitches unrelated daily prices as if they were
   2h-adjacent.
2. `_confirm_with_deep_history()` runs `dual_ema_signal(prices, fast=3, slow=18,
   confirm_bars=2)` over that degenerate series. The EMAs now span
   approximately *3 days* and *18 days* of real time — not 3 and 18 bars of
   the strategy's 2h timeframe. The "confirmation" is effectively a random
   higher-timeframe signal that has **no relationship** to the short-term
   setup the live code just validated, so it rejects correct buys.

The mean-reversion manager's `_get_meanrev_prices()` has the same bug
(calls `build_candles_from_history(..., candle_hours=self._cfg.candle_timeframe_hours)`
on daily-resolution history), and `EmaManager` startup warmup
(`_warm_history` in `__init__`) too.

## Affected code

- [app/strategy/ema_signals.py](app/strategy/ema_signals.py) —
  `build_candles_from_history()` (buckets by requested TF, warns on gaps,
  stitches across gaps with `prior_close`)
- [app/portfolio/ema_manager.py](app/portfolio/ema_manager.py) —
  `_confirm_with_deep_history()` (trend gate)
- [app/portfolio/ema_manager.py](app/portfolio/ema_manager.py) —
  `_get_meanrev_prices()` (mean-reversion signal source)
- [app/portfolio/ema_manager.py](app/portfolio/ema_manager.py) —
  startup warmup loop in `__init__` that populates `_warm_history`
- [app/config.py](app/config.py) — `SUBNET_HISTORY_INTERVAL`,
  `SUBNET_HISTORY_LIMIT` (currently `"1h"` / `336`, both effectively ignored)

## Fix

Treat the deep-history response as **higher-timeframe (HTF) data at its
natural resolution** and run an HTF-appropriate confirmation, instead of
pretending it's the strategy's live timeframe.

### 1. Detect the natural resolution

Add a helper that infers candle size from the history payload:

```python
# app/strategy/ema_signals.py

def detect_history_resolution_hours(history: list[dict]) -> float | None:
    """Estimate the native sample interval of a history payload, in hours.

    Uses the median of consecutive timestamp deltas (robust to occasional
    gaps). Returns None if fewer than 2 parseable timestamps are present.
    """
```

For the current Taostats payload this returns ~24.0. If upstream ever honours
the `interval=1h` parameter, it will return ~1.0 and the downstream code
degrades gracefully.

### 2. Build candles at the natural resolution

Change the confirmation path to stop forcing `candle_hours =
self._cfg.candle_timeframe_hours`. Instead:

```python
native_tf = detect_history_resolution_hours(history) or self._cfg.candle_timeframe_hours
htf_hours = max(native_tf, self._cfg.candle_timeframe_hours)
deep_candles = build_candles_from_history(history, candle_hours=htf_hours)
```

This eliminates the "92% missing" warning for real — the warning only fired
because we asked for a timeframe finer than the data. It also eliminates the
degenerate-candle problem (each bucket now holds at least one real sample).

### 3. Run HTF confirmation with HTF-appropriate periods

The trend strategy uses `fast=3, slow=18, confirm=2` on 2h candles —
well-tuned for short-term setups. Reusing those periods on a 24h series
asks "has the subnet been trending up for 1.5 days and 36 days?", which is
stricter and noisier than intended.

Add new config knobs with sensible daily-timeframe defaults, and fall back
to the live periods if the HTF resolution happens to equal the live TF:

```python
# app/config.py  (new fields)

# Trend strategy (Strategy B) — HTF confirmation
EMA_B_HTF_FAST_PERIOD: int = 5     # ~5 days at daily resolution
EMA_B_HTF_SLOW_PERIOD: int = 20    # ~4 weeks at daily resolution
EMA_B_HTF_CONFIRM_BARS: int = 2

# Mean-reversion (Strategy MR) — HTF trend guard
MR_HTF_FAST_PERIOD: int = 5
MR_HTF_SLOW_PERIOD: int = 20
MR_HTF_CONFIRM_BARS: int = 2
```

In `_confirm_with_deep_history`:

```python
if htf_hours > self._cfg.candle_timeframe_hours:
    fast, slow, confirm = (
        self._cfg.htf_fast_period,
        self._cfg.htf_slow_period,
        self._cfg.htf_confirm_bars,
    )
else:
    fast, slow, confirm = (
        self._cfg.fast_period,
        self._cfg.slow_period,
        self._cfg.confirm_bars,
    )

if len(deep_prices) < slow + confirm:
    # Graceful pass — we already log and return True for this case today.
    return True

signal = dual_ema_signal(deep_prices, fast, slow, confirm)
```

Change the semantics of this gate from "confirm the exact same crossover"
to "the HTF trend is not explicitly bearish". Concretely:

- Pass when `signal in {"BUY", "HOLD"}` (don't block because the HTF isn't
  mid-crossover right now).
- Fail only on `signal == "SELL"` — a clear counter-trend.

This matches the original intent of the gate (avoid buying into a broken
long-term trend) without requiring the HTF to fire a fresh crossover.

### 4. Fix mean-reversion's deep-history path

`_get_meanrev_prices()` today builds candles at the strategy's 1h timeframe
from the same broken payload, which means the MR signal itself is computed
on degenerate candles. Two options:

- **Preferred:** build HTF candles as above, and use them as a **trend guard**
  (don't take mean-reversion longs when the HTF is in a clear downtrend)
  while keeping the primary MR signal on the pool-snapshot sampled candles
  (`_get_completed_candles`).
- Alternative: leave the MR signal as-is (pool-snapshot sampled), add an
  HTF guard check, drop the broken `build_candles_from_history(..., 1h)`
  path entirely.

Either way, `_warm_history` stops doubling as "dense 1h history we can EMA
on" and becomes "daily-ish background series for HTF guards."

### 5. Update the gap warning

The 92%-missing warning in `build_candles_from_history` is now misleading —
a daily series bucketed at daily resolution is not "missing" anything. Gate
the warning on the ratio between requested `candle_hours` and the detected
native resolution: only warn when `candle_hours <= native_tf` (i.e. we
expected a denser sampling than we got).

### 6. Startup warmup

`EmaManager.__init__` warms `_warm_history` for open positions from the same
endpoint. Keep the warmup but stop treating it as 2h/1h data downstream —
it's only useful for the HTF guard now. No functional change, just a
comment/rename to prevent future confusion.

## Config migration

Add to [.env.example](.env.example):

```
# HTF (higher-timeframe) confirmation — uses the deepest history the
# Taostats subnet-history endpoint returns (currently daily). Fast/slow
# are EMA periods in HTF bars.
EMA_B_HTF_FAST_PERIOD=5
EMA_B_HTF_SLOW_PERIOD=20
EMA_B_HTF_CONFIRM_BARS=2
MR_HTF_FAST_PERIOD=5
MR_HTF_SLOW_PERIOD=20
MR_HTF_CONFIRM_BARS=2
```

Leave `SUBNET_HISTORY_INTERVAL` and `SUBNET_HISTORY_LIMIT` as-is — they're
still sent to Taostats in case the endpoint ever starts honouring them, and
the code now adapts to whatever resolution is returned.

## Testing

### Unit

- `detect_history_resolution_hours` — verify on fixture with regular daily
  samples (→ 24.0), regular hourly samples (→ 1.0), and a mostly-daily
  series with two 2-day gaps (→ still 24.0).
- `build_candles_from_history` with `candle_hours=24` on the 200-point
  SN48 fixture — expect ~200 candles, no "missing" warning, each candle
  has `sample_count == 1` but distinct OHLC.
- `_confirm_with_deep_history`:
  - HTF signal `SELL` → returns False (blocks entry).
  - HTF signal `HOLD` → returns True.
  - HTF signal `BUY` → returns True.
  - History empty / too short / endpoint raises → returns True (unchanged).

### Integration (live, DRY_RUN off)

1. Before rolling out, capture one full scan's log (expect today's SN48
   rejection to still appear).
2. Deploy; restart bot.
3. On the next scan, SN48 should either pass (enter) or be rejected by a
   *different* gate. The "deep history signal=SELL" line on SN48 should
   stop appearing with the current configuration.
4. Monitor for 24h: confirm the trend strategy opens ≥1 position and that
   no HTF-rejected subnet retroactively showed obvious downtrend behaviour.

### Regression

- Backtest engine already calls `build_candles_from_history` with explicit
  timeframes from cached JSON (`data/backtest/history/sn{N}.json`). That
  path passes real hourly data and must keep producing hourly candles
  without regression — add a backtest smoke run after the change.

## Rollout

1. Land config + `detect_history_resolution_hours` + warning gate (no
   behaviour change for backtest).
2. Land HTF confirmation logic behind a feature flag
   (`EMA_HTF_CONFIRM_ENABLED`, default `true`); keep the old code path
   reachable via flag for one release in case HTF rejections are too loose
   or tight in practice.
3. After a week of clean live data, delete the flag and the old path.

## Non-goals

- Fixing Taostats to honour `interval=1h`. Out of our control; file a
  ticket separately. The fix here is robust either way.
- Changing the strategy timeframes themselves (2h for trend, 1h for MR).
- Persisting HTF candles to disk — the endpoint is cheap enough with the
  60 req/min tier to refetch on entry.
