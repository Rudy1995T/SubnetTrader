# Strategy Spec: Mean Reversion (Bollinger / RSI)

## Overview

Fade extreme price moves on DTAO subnets using RSI and Bollinger Band signals.
Where the existing EMA strategies chase trends, this strategy bets on reversion
to the mean — buying oversold dips and selling into overbought rallies.

## Motivation

The current EMA strategies have a 38% win rate. Most entries happen during
sideways chop where there is no real trend to capture. DTAO pools are bounded
AMMs — large price moves invite arbitrage and pool rebalancing, creating a
natural mean-reverting force. A reversion strategy would profit in exactly the
regime where trend-following fails, making it a natural complement.

## Indicators

### RSI (Relative Strength Index)

Standard RSI over N periods (default 14 candles at 4h = 56h lookback):

```
RS = avg_gain(N) / avg_loss(N)
RSI = 100 - (100 / (1 + RS))
```

Use Wilder smoothing (exponential with alpha = 1/N) for consistency.

Input: 4h OHLC candles from `build_sampled_candles()` (already exists in
`app/strategy/ema_signals.py`).

### Bollinger Bands

```
middle = SMA(close, 20)
upper  = middle + 2 * stddev(close, 20)
lower  = middle - 2 * stddev(close, 20)
%B     = (close - lower) / (upper - lower)
```

`%B < 0` means price is below the lower band (oversold).
`%B > 1` means price is above the upper band (overbought).

### Bandwidth (optional regime filter)

```
bandwidth = (upper - lower) / middle
```

Narrow bandwidth = low volatility squeeze, likely about to expand.
Wide bandwidth = already in a move, mean reversion is riskier.

## Signal

### Entry (BUY)

All of the following must be true:

1. **RSI <= 30** (oversold)
2. **Bollinger %B < 0.1** (price near or below lower band)
3. **Bandwidth > min threshold** (default 0.03 — skip dead/illiquid subnets)
4. **Price has not breached a 7-day low** — prevents catching falling knives
   on structural downtrends. Specifically: `low(current candle) > min(low, 42 candles)`

### Exit (SELL)

| Reason | Condition |
|---|---|
| MEAN_REACHED | Price crosses above middle Bollinger band (SMA20) |
| RSI_OVERBOUGHT | RSI >= 65 (don't wait for 70 — capture the move, not the top) |
| STOP_LOSS | PnL <= -6% |
| TRAILING_STOP | Peak drawdown >= 4% after reaching +3% |
| TIME_STOP | 72h max hold (if it hasn't reverted by then, thesis is broken) |

The **primary profit target is the middle band** — we're not trying to ride a
trend, just capture the reversion. RSI_OVERBOUGHT is a secondary exit for cases
where momentum overshoots the mean.

## Position Sizing

Same approach as EMA: pool-depth-aware sizing, max 2.5% price impact.

Since mean reversion trades have a defined target (middle band), we can
calculate expected R:R at entry:

```
expected_gain = (middle_band - entry_price) / entry_price
risk = STOP_LOSS_PCT
R:R  = expected_gain / risk
```

**Skip entries where R:R < 1.5** — not enough room to justify the risk.

## Configuration (.env)

```
MR_ENABLED=false
MR_DRY_RUN=true
MR_SLOTS=3
MR_POT_TAO=10.0
MR_POSITION_SIZE_PCT=0.33
MR_RSI_PERIOD=14
MR_RSI_ENTRY=30
MR_RSI_EXIT=65
MR_BB_PERIOD=20
MR_BB_STDDEV=2.0
MR_BB_ENTRY_PCTB=0.10
MR_MIN_BANDWIDTH=0.03
MR_MIN_RR=1.5
MR_STOP_LOSS_PCT=6.0
MR_TRAILING_PCT=4.0
MR_TRAILING_TRIGGER_PCT=3.0
MR_MAX_HOLD_HOURS=72
MR_COOLDOWN_HOURS=4
MR_CANDLE_TF_HOURS=4
```

## Data Requirements

No new external data needed. Uses the same `seven_day_prices` from Taostats
that the EMA strategy already fetches. The existing `build_sampled_candles()`
function provides 4h OHLC candles.

**Constraint:** 7 days of price history limits us to ~42 candles at 4h.
That's enough for RSI(14) and BB(20) but with minimal warmup margin.
If Taostats adds longer history, increase lookback.

## Interaction With EMA Strategies

Mean reversion and trend-following are complementary:

- **Same subnet, opposite signals:** EMA says SELL (below EMA), MR says BUY
  (oversold). This is expected and fine — they have different theses. Both can
  hold positions in the same subnet via separate slot pools.

- **Cross-strategy exclusion:** Reuse the existing `_cross_strategy_netuids()`
  mechanism. If EMA already holds a subnet, MR should still be allowed to enter
  (the signals are uncorrelated). But apply the dual-hold stagger exit logic.

- **Circuit breaker:** Share the same circuit breaker (15% drawdown over 24h
  across all strategies pauses all entries).

## Backtesting Plan

1. **Phase 1 — Indicator validation:** Compute RSI and Bollinger Bands across
   all subnets for the last 7 days. Plot signals against actual price action
   to visually confirm the indicators work on DTAO data (irregular samples,
   thin pools, emission noise).

2. **Phase 2 — Signal replay:** For each historical BUY signal, simulate the
   trade forward:
   - Did price reach the middle band within 72h? (win)
   - Did price hit -6% first? (loss)
   - Compute hit rate, avg gain, avg loss, expectancy

3. **Phase 3 — Parameter sweep:**
   - RSI entry: 25, 30, 35
   - BB %B entry: 0.0, 0.05, 0.10, 0.15
   - Exit at middle band vs RSI 60/65/70
   - Stop loss: 5%, 6%, 8%

4. **Phase 4 — Paper trade** (`MR_DRY_RUN=true`) alongside live EMA for 2 weeks.

## Files to Create/Modify

| File | Action |
|---|---|
| `app/strategy/mr_signals.py` | New: `compute_rsi()`, `compute_bollinger()`, `mr_signal()` |
| `app/portfolio/mr_manager.py` | New: MrManager (entry/exit logic, slot management) |
| `app/config.py` | Add MR_* settings |
| `app/main.py` | Register MrManager, schedule MR cycle |

## Risks

- **Catching falling knives:** The biggest risk. The 7-day-low filter and R:R
  check mitigate this, but structural collapses (subnet deregistration, whale
  exit) can blow through the stop.
- **Limited lookback:** 7 days of price data means RSI and BB have thin warmup.
  Signals in the first ~24h after bot restart may be unreliable — add a warmup
  grace period.
- **Illiquid subnets:** A subnet can look "oversold" simply because nobody is
  trading it. The bandwidth filter and existing pool-depth checks help, but
  be cautious with very thin pools where slippage eats the reversion profit.
