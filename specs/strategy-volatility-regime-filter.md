# Strategy Spec: Volatility Regime Filter (Meta-Strategy)

## Overview

A market regime detection layer that gates entry for all active strategies.
Measures realized volatility across the subnet universe to classify the current
environment as **trending**, **choppy**, or **dead**. Strategies are only allowed
to enter during trending regimes. During chop, the bot sits on its hands.

This is not a standalone strategy — it's a filter that wraps existing strategies
(EMA, Flow, Mean Reversion) to reduce losing trades in unfavorable conditions.

## Motivation

Trade history shows losses cluster in time:

- **Mar 11:** 8 stop-losses in one day (-8.09% to -9.28%), all scalper
- **Mar 24-25:** 6 losses in 24h across both strategies
- **Mar 28-29:** 5 losses in 36h

Meanwhile, winning streaks also cluster (Mar 9-10, Mar 19-20, Mar 22-23).
This clustering indicates the market oscillates between regimes where
trend-following works and regimes where it doesn't. A regime filter could
have prevented ~30-40% of the losing trades by simply pausing entries during
the choppy periods.

## Regime Classification

### Input: Realized Volatility

Compute per-subnet and aggregate:

```python
def realized_volatility(prices, window_hours=24):
    """Annualized realized vol from 4h candle returns."""
    returns = [close[i] / close[i-1] - 1 for i in range(1, len(closes))]
    # Use last `window` candles
    recent = returns[-window_candles:]
    return std(recent) * sqrt(6 * 365)  # 6 candles/day at 4h
```

### Input: Directional Strength

Average absolute return across subnets — are things moving or flat?

```python
def directional_strength(all_subnet_returns, window_hours=24):
    """Mean absolute return across all tracked subnets."""
    abs_returns = [abs(r) for subnet in subnets for r in subnet.recent_returns]
    return mean(abs_returns)
```

### Input: Cross-Subnet Dispersion

Are subnets moving together (correlated sell-off/rally) or independently?

```python
def dispersion(all_subnet_returns):
    """Cross-sectional standard deviation of returns."""
    period_returns = [mean(s.recent_returns) for s in subnets]
    return std(period_returns)
```

High dispersion = subnets diverging = good for stock-picking (selective entry).
Low dispersion + low vol = everything is flat = chop.

### Regime States

| Regime | Condition | Action |
|---|---|---|
| **TRENDING** | `vol > VOL_TREND_THRESHOLD` AND `directional_strength > DIR_THRESHOLD` | All entries allowed |
| **DISPERSED** | `vol moderate` AND `dispersion > DISP_THRESHOLD` | Entries allowed with tighter filters |
| **CHOPPY** | `vol > VOL_CHOP_FLOOR` AND `directional_strength < DIR_THRESHOLD` | **Block new entries** |
| **DEAD** | `vol < VOL_DEAD_THRESHOLD` | Block entries (nothing is moving) |

### State Transitions

To avoid flipping rapidly, require N consecutive cycles (default 2 = 30 min)
in a new state before transitioning. This is a simple debounce.

```
current_regime = TRENDING

# Each cycle:
raw_regime = classify(vol, dir_strength, dispersion)
if raw_regime != current_regime:
    transition_count += 1
    if transition_count >= REGIME_DEBOUNCE:
        current_regime = raw_regime
        transition_count = 0
else:
    transition_count = 0
```

## Integration With Existing Strategies

### Gating Logic

Each strategy manager's entry path gets a regime check:

```python
# In EmaManager.run_cycle(), FlowManager.run_cycle(), etc.
regime = regime_filter.current_regime

if regime == "CHOPPY" or regime == "DEAD":
    log.info(f"Regime={regime}, skipping entries")
    return  # skip entry scan, exits still run

if regime == "DISPERSED":
    # Tighten entry filters
    min_signal_strength *= 1.5
```

**Exits are never gated.** Stop-losses, trailing stops, and signal-based exits
always execute regardless of regime. We only gate new entries.

### Per-Strategy Override

Some strategies may want different regime sensitivity:

- **Mean Reversion:** should be *enabled* during CHOPPY (that's when it works).
  MR gets an inverted regime gate — blocked during TRENDING, allowed during CHOPPY.
- **Emission Yield:** ignores regime entirely (it's not a momentum strategy).
- **EMA / Flow:** blocked during CHOPPY and DEAD.

```
REGIME_GATE_EMA=trending,dispersed
REGIME_GATE_FLOW=trending,dispersed
REGIME_GATE_MR=choppy,dispersed
REGIME_GATE_YIELD=all
```

## Observability

### API Endpoint

```
GET /api/regime
```

Response:
```json
{
  "regime": "TRENDING",
  "since": "2026-03-30T14:00:00Z",
  "vol_24h": 0.42,
  "directional_strength": 0.031,
  "dispersion": 0.018,
  "entries_allowed": ["ema", "flow"],
  "entries_blocked": ["mr"]
}
```

### Logging

Each cycle logs:
```
REGIME: TRENDING (vol=0.42, dir=0.031, disp=0.018) — entries: ON
```

On state transition:
```
REGIME CHANGE: CHOPPY → TRENDING (after 2 confirming cycles)
```

### Frontend

Add a regime indicator to the dashboard header:
- Green chip: TRENDING
- Yellow chip: DISPERSED
- Red chip: CHOPPY
- Gray chip: DEAD

## Configuration (.env)

```
REGIME_ENABLED=true
REGIME_VOL_WINDOW_HOURS=24
REGIME_VOL_TREND_THRESHOLD=0.30
REGIME_VOL_CHOP_FLOOR=0.10
REGIME_VOL_DEAD_THRESHOLD=0.05
REGIME_DIR_THRESHOLD=0.02
REGIME_DISP_THRESHOLD=0.015
REGIME_DEBOUNCE_CYCLES=2
REGIME_GATE_EMA=trending,dispersed
REGIME_GATE_FLOW=trending,dispersed
REGIME_GATE_MR=choppy,dispersed
REGIME_GATE_YIELD=all
```

## Backtesting Plan

1. **Phase 1 — Historical regime reconstruction:** Using the 7 days of
   `seven_day_prices` available from Taostats, compute vol/direction/dispersion
   at each 4h point. Classify regimes retroactively.

2. **Phase 2 — Overlay on trade history:** Map each of your 136 closed trades
   to the regime that was active at entry time. Compute:
   - Win rate per regime (expect TRENDING >> CHOPPY)
   - Average PnL per regime
   - How many losing trades would have been avoided by the filter

3. **Phase 3 — Threshold tuning:** Sweep threshold values and measure the
   trade-off between trades filtered (lower risk) and winners missed
   (opportunity cost). Find the knee of the curve.

4. **Phase 4 — Live shadow mode:** Enable regime classification and logging
   but don't gate entries. Compare "would have been filtered" trades vs
   actual outcomes for 1-2 weeks before enforcing.

## Files to Create/Modify

| File | Action |
|---|---|
| `app/strategy/regime_filter.py` | New: `RegimeFilter` class with `classify()`, `current_regime`, debounce logic |
| `app/config.py` | Add REGIME_* settings |
| `app/portfolio/ema_manager.py` | Add regime gate check before entry scan |
| `app/portfolio/flow_manager.py` | Add regime gate check (once flow strategy exists) |
| `app/portfolio/mr_manager.py` | Add inverted regime gate (once MR strategy exists) |
| `app/main.py` | Instantiate RegimeFilter, pass to managers, add /api/regime endpoint |
| `frontend/` | Add regime indicator chip to dashboard |

## Risks

- **Over-filtering:** If thresholds are too aggressive, the bot sits idle
  during periods that would have been profitable. The shadow mode phase
  measures this before enforcing.
- **Lag:** Regime detection is inherently backward-looking. A regime change
  is confirmed 30 min after it starts (2 debounce cycles). Fast reversals
  may be missed.
- **Correlation with circuit breaker:** The existing circuit breaker (15%
  drawdown) already pauses entries during crashes. The regime filter catches
  a different case: slow chop that doesn't trigger the breaker but still
  produces losing trades. They're complementary, not redundant.
- **Per-subnet vs aggregate:** The current design uses aggregate market regime.
  A subnet-level regime filter could be more precise but adds complexity.
  Start with aggregate, consider per-subnet in v2.
