# EMA Strategy Optimisation — Specification

## Overview

Backtest results across 7-day (4h candles) and 200-day (daily candles) windows show the
current EMA parameters (fast=6, slow=18) consistently rank bottom-third. The best
long-term performer is **fast=3, slow=9**, which produced +437% total PnL over 200 days
versus +165% for the current settings (2.6x improvement, 250 trades, 41.6% win rate).

**Goal:** update default EMA periods from 6/18 to 3/9 across config, .env, and all code
that references these values, without changing any other strategy logic.

---

## Backtest Evidence

| Window         | Best combo | Best PnL | Current 6/18 PnL | 6/18 Rank |
|----------------|------------|----------|-------------------|-----------|
| 7d (4h)        | 10/30      | -167%    | -398%             | 10/10     |
| 30d (daily)    | 6/12       | +73%     | -7%               | 9/10      |
| 90d (daily)    | 3/18       | +165%    | +90%              | 7/10      |
| 200d (daily)   | **3/9**    | **+437%**| +165%             | 9/10      |

Parameters held constant during backtest: confirm_bars=3, stop_loss=8%,
take_profit=20%, trailing_stop=5%, breakeven_trigger=3%, max_holding=168h,
slippage=1%, max_entry_price=0.1 TAO.

---

## Changes Required

### 1. `app/config.py` — Default values

```python
# Before
EMA_PERIOD: int = 18
EMA_FAST_PERIOD: int = 6

# After
EMA_PERIOD: int = 9
EMA_FAST_PERIOD: int = 3
```

### 2. `.env.example` — Example config

```env
# Before
EMA_PERIOD=18
EMA_FAST_PERIOD=6

# After
EMA_PERIOD=9
EMA_FAST_PERIOD=3
```

### 3. `.env` — Live config (if present)

Update the live `.env` file to match:

```env
EMA_PERIOD=9
EMA_FAST_PERIOD=3
```

### 4. Verify — no other hardcoded references

Grep for any hardcoded `period=18` or `fast_period=6` in strategy/signal code that
bypasses config. All EMA period values should flow from `settings.EMA_PERIOD` and
`settings.EMA_FAST_PERIOD`. The following files read from config and need no changes:

- `app/strategy/ema_signals.py` — receives periods as function arguments
- `app/portfolio/ema_manager.py` — reads `settings.EMA_PERIOD` / `settings.EMA_FAST_PERIOD`

---

## What Does NOT Change

- Confirm bars (3)
- Stop-loss (8%), take-profit (20%), trailing stop (5%), breakeven (3%)
- Max holding hours (168h), cooldown (4h)
- Candle timeframe (4h)
- Bounce filter, correlation guard, Gini filter
- Position sizing, pot size, max positions
- Entry/exit execution logic (staking, unstaking, chunked exits)

---

## Rollback

If live performance degrades, revert to previous values:

```env
EMA_PERIOD=18
EMA_FAST_PERIOD=6
```

No database migration or position cleanup is needed — existing open positions will
simply be evaluated against the new EMA curves on the next cycle. Positions opened
under the old parameters will naturally exit via stop-loss, take-profit, trailing
stop, time stop, or the new EMA cross signal.

---

## Testing

1. After updating, restart the bot and verify logs show the new periods:
   ```
   tail -5 data/logs/$(date -u +%Y-%m-%d).jsonl | grep -i ema
   ```
2. Check signals endpoint reflects new EMA values:
   ```
   curl -s http://localhost:8081/api/ema/signals | python3 -m json.tool
   ```
3. Re-run the backtest to confirm identical results:
   ```
   source .venv/bin/activate && python backtest_ema.py
   ```
