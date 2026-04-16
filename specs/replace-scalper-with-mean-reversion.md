# Spec: Replace Scalper Strategy with Mean-Reversion

**Branch:** `2_EMA_Strategies`
**Date:** 2026-04-16
**Status:** Draft

## Motivation

The **Scalper** strategy (tag `"scalper"`, EMA fast=3/slow=9) is underperforming:

| Metric            | Scalper       | Trend         |
|-------------------|---------------|---------------|
| Closed trades     | 157           | 37            |
| Total PnL         | +0.53 TAO     | +2.24 TAO     |
| Win rate          | 36.9%         | 43.2%         |
| Avg PnL/trade     | +0.003 TAO    | +0.06 TAO     |
| Avg win           | +0.16 TAO     | +0.21 TAO     |
| Avg loss          | -0.09 TAO     | -0.06 TAO     |

Scalper generates high churn (157 trades) with razor-thin edge. Most exits are
EMA_CROSS (55) and STOP_LOSS (31), eating into 19 take-profits. It is
essentially break-even after fees and slippage.

Meanwhile, swing analysis of subnets ranked #5–#45 shows:
- 911 swings >=5% across 41 subnets in ~200 hours of data
- Upswings outnumber downswings at 10%+ (157 up vs 105 down)
- Median swing duration: 2 hours
- Mean-reversion dip-buying has a statistical edge in this market

**Replacing Scalper with a mean-reversion strategy diversifies the portfolio
(trend + counter-trend), reuses existing infrastructure, and targets the
frequent 5–15% swings that Scalper was failing to capture.**

## Design

### Strategy identity

- **Tag:** `"meanrev"` (replaces `"scalper"`)
- **Reuse `EmaManager`** — same position lifecycle, exit watcher, circuit breaker,
  Gini guard, cooldowns, chunked exits, post-exit verify. Only the entry signal
  and exit logic change.
- **Cross-exclusion with Trend** — maintained via existing `_companion_netuids_cb`.

### Entry signal

Enter when a subnet has dipped sharply and shows signs of reversal. All three
conditions must be true:

1. **RSI oversold:** RSI(14) on 1h candles < `MR_RSI_ENTRY` (default 30)
2. **Bollinger breach:** Price is at or below the lower Bollinger Band (20-period,
   2 std) on 1h candles
3. **Bounce confirmation:** Current 1h candle closes above the lower BB (don't
   catch a falling knife — require the first green close back inside the band)

Additional filters (inherited from existing infra, same as Trend):
- Pool depth >= `MR_MIN_POOL_DEPTH_TAO` (default 3000 TAO)
- Gini < `MR_MAX_GINI` (default 0.82) with hysteresis
- Not in cooldown for this subnet
- Not correlated with existing holdings (Pearson r > threshold)
- Not held by companion Trend strategy (cross-exclusion)
- Max entry price filter (skip SN0-type outliers)

### Exit logic

Mean-reversion exits are faster and tighter than trend-following:

| Exit type        | Condition                                          | Default    |
|------------------|----------------------------------------------------|------------|
| **Take-profit**  | PnL >= `MR_TAKE_PROFIT_PCT`                        | 8%         |
| **Stop-loss**    | PnL <= -`MR_STOP_LOSS_PCT`                         | 5%         |
| **Time-stop**    | Holding time > `MR_MAX_HOLDING_HOURS`              | 24h        |
| **BB mid cross** | Price crosses above BB middle band (20-SMA)        | enabled    |
| **RSI overbought** | RSI(14) > `MR_RSI_EXIT` while in profit          | 65         |

No trailing stop — mean-reversion targets are fixed. The BB-mid cross is the
primary "mission accomplished" exit (price reverted to the mean). Take-profit is
a hard cap for outsized moves. Time-stop is aggressive (24h) because if the dip
hasn't reversed in a day, the thesis is broken.

### Position sizing

- **Pot:** `MR_POT_TAO` (default 5.0), shared pool with `EMA_POT_MODE`
- **Size per trade:** `MR_POSITION_SIZE_PCT` (default 0.25 = 25% of pot)
- **Max positions:** `MR_MAX_POSITIONS` (default 4)
- **Vol-sizing:** Enabled by default — scale position smaller when volatility is
  elevated (same ATR-based sizing as Trend)

### Candle timeframe

- **Primary:** 1h candles (not 4h). Mean-reversion signals are faster. The 1h
  timeframe matches the 2–3h median swing duration from the swing analysis.
- Source: `build_sampled_candles()` or `build_candles_from_history()` with
  `candle_hours=1`, using seven_day_prices from the pool snapshot.

## Implementation plan

### Phase 1: Config and signal layer

**Files changed:**

1. **`app/config.py`** — Replace all `EMA_*` (Strategy A / Scalper) settings with
   `MR_*` settings:

   ```
   MR_ENABLED: bool = True
   MR_DRY_RUN: bool = True
   MR_STRATEGY_TAG: str = "meanrev"
   MR_POT_TAO: float = 5.0
   MR_POSITION_SIZE_PCT: float = 0.25
   MR_MAX_POSITIONS: int = 4
   MR_STOP_LOSS_PCT: float = 5.0
   MR_TAKE_PROFIT_PCT: float = 8.0
   MR_MAX_HOLDING_HOURS: int = 24
   MR_COOLDOWN_HOURS: float = 2.0
   MR_CANDLE_TIMEFRAME_HOURS: int = 1
   MR_RSI_ENTRY: float = 30.0
   MR_RSI_EXIT: float = 65.0
   MR_RSI_PERIOD: int = 14
   MR_BB_PERIOD: int = 20
   MR_BB_STD: float = 2.0
   MR_BB_MID_EXIT: bool = True
   MR_MIN_POOL_DEPTH_TAO: float = 3000.0
   MR_MAX_GINI: float = 0.82
   MR_CORRELATION_THRESHOLD: float = 0.80
   MR_DRAWDOWN_BREAKER_PCT: float = 15.0
   MR_DRAWDOWN_PAUSE_HOURS: float = 6.0
   MR_VOL_SIZING_ENABLED: bool = True
   MR_VOL_TARGET_RISK: float = 0.02
   MR_VOL_FLOOR: float = 0.10
   MR_VOL_CAP: float = 1.50
   MR_VOL_MIN_SIZE_PCT: float = 0.15
   MR_VOL_MAX_SIZE_PCT: float = 0.40
   MR_VOL_WINDOW: int = 24
   ```

   Replace `strategy_a_config()` with `meanrev_config()` → returns
   `StrategyConfig` with new fields. Add `StrategyConfig.strategy_type: str`
   field (`"ema"` or `"meanrev"`) so the manager knows which entry/exit logic
   to run.

2. **`app/strategy/mean_reversion.py`** (new file) — Pure signal functions:

   ```python
   def meanrev_entry_signal(
       prices: list[float],
       rsi_threshold: float = 30.0,
       rsi_period: int = 14,
       bb_period: int = 20,
       bb_std: float = 2.0,
   ) -> bool:
       """True when RSI < threshold AND price at/below lower BB AND
       current close is back above lower BB (bounce confirmation)."""

   def meanrev_exit_signal(
       prices: list[float],
       entry_price: float,
       take_profit_pct: float,
       rsi_exit: float = 65.0,
       rsi_period: int = 14,
       bb_period: int = 20,
       bb_std: float = 2.0,
       bb_mid_exit: bool = True,
   ) -> str | None:
       """Returns exit reason string or None.
       Checks: BB_MID_CROSS, RSI_OVERBOUGHT, TAKE_PROFIT."""
   ```

   These are pure functions — no state, no async. They consume price arrays and
   return signals. The existing `compute_rsi()` and `compute_bollinger_bands()`
   from `app/strategy/indicators.py` are used internally.

### Phase 2: Manager integration

3. **`app/portfolio/ema_manager.py`** — Branch entry/exit logic based on
   `self._cfg.strategy_type`:

   - **`_do_cycle()` entry pass:** When `strategy_type == "meanrev"`, replace the
     `dual_ema_signal() != "BUY"` check with `meanrev_entry_signal()`. Remove
     EMA-specific filters (bounce, MTF, parabolic guard). Keep: pool depth, Gini,
     cooldown, correlation, cross-exclusion.

   - **Exit pass:** When `strategy_type == "meanrev"`, add `BB_MID_CROSS` and
     `RSI_OVERBOUGHT` exit reasons alongside the existing stop-loss, take-profit,
     and time-stop. Remove trailing stop logic (not used in mean-reversion).

   - **Entry watcher:** When `strategy_type == "meanrev"`, poll for RSI/BB
     conditions instead of EMA crossover. Keep the same watcher interval
     (`EMA_ENTRY_WATCHER_SEC`).

   The manager class name stays `EmaManager` for now (renaming is cosmetic churn).
   Internally it dispatches on `strategy_type`.

### Phase 3: Startup wiring

4. **`app/main.py`** — Replace `strategy_a_config()` call with `meanrev_config()`.
   The manager instantiation, cross-exclusion wiring, and FastAPI endpoints stay
   the same. Update strategy tag references in logging/API from `"scalper"` to
   `"meanrev"`.

5. **`.env` / `.env.example`** — Add `MR_*` variables, remove `EMA_*` Scalper
   variables. Keep all `EMA_B_*` (Trend) variables unchanged.

### Phase 4: Cleanup

6. **Remove dead Scalper config** — Delete all `EMA_STRATEGY_TAG`, `EMA_PERIOD`,
   `EMA_FAST_PERIOD`, `EMA_CONFIRM_BARS`, and other Strategy-A-only settings from
   `config.py`. Keep shared settings (`EMA_EXIT_WATCHER_SEC`, etc.) under their
   current names.

7. **Database** — No schema changes needed. The `ema_positions` table's `strategy`
   column stores the tag string. Old `"scalper"` rows remain as historical data.
   New positions use `"meanrev"`.

8. **Frontend** — Update the EMA page to show `"meanrev"` alongside `"trend"` in
   the strategy filter. The position table and portfolio API already use the
   strategy tag dynamically.

### Phase 5: Backtest validation

9. **`app/backtest/strategies.py`** — Add a `MeanReversionStrategy` backtest class
   that mirrors the live signal logic. Run against the existing 200-point history
   cache for subnets #5–#45. Compare Sharpe ratio, win rate, and max drawdown
   against the historical Scalper results.

## Risk mitigation

- **Launch in DRY_RUN** (`MR_DRY_RUN=true`) and monitor for 48h before going live.
- **Cross-exclusion** prevents both strategies piling into the same subnet.
- **Circuit breaker** (drawdown 15% in 24h) pauses entries — inherited from
  EmaManager.
- **Tight time-stop (24h)** prevents capital lock-up if the thesis fails.
- **BB bounce confirmation** avoids catching falling knives — we require the close
  to be back above the lower band before entering.

## What NOT to change

- Trend strategy (Strategy B) — no changes whatsoever
- `EmaManager` class name — cosmetic rename is not worth the churn
- Database schema — tag-based filtering handles multiple strategies
- Chunked exit logic — reused as-is for mean-reversion
- Telegram alerts, post-exit verification, Gini guard — all inherited

## Success criteria

- Mean-reversion in DRY_RUN for 1 week shows:
  - Win rate > 50% (target: 55–65%)
  - Average holding time < 12h
  - More trades than Trend (targeting the frequent 5–10% swings)
  - Positive PnL after simulated slippage
- Backtest on historical data shows positive expectancy across #5–#45 subnets