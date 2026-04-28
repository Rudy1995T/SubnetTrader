# Spec: Optimize Trend Strategy (B) — Config-Only Quick Wins

## Overview

Apply three parameter improvements to Strategy B (trend, fast=3, slow=18) based on
backtest results across 90d/120d/150d windows. All changes are `.env`-only — no code
modifications required.

**Why now:** The backtest confirmed that D7 (wider stops SL=12/TP=30) is the #1 ranked
configuration by expectancy across every long window: +2.48 at 90d, +2.98 at 120d, +3.28
at 150d — 45% better than the current A1 scalper baseline. Strategy B's trend EMA (3/18)
is already the #2 config, and these three changes push it further toward D7's profile
without touching any code.

---

## Changes

### 1. Widen Stops

**Rationale:** D7's dominance in the backtest is primarily driven by fewer premature
stop-outs on positions that would have recovered and continued trending. Current 8% SL is
too tight for DTAO pool volatility on a 4h timeframe. Wider stops let genuine trend trades
breathe. Trailing stop raised proportionally so it still captures profits rather than
giving back too much on the way down.

| Variable | Current | New |
|---|---|---|
| `EMA_B_STOP_LOSS_PCT` | 8.0 | 12.0 |
| `EMA_B_TAKE_PROFIT_PCT` | 20.0 | 30.0 |
| `EMA_B_TRAILING_STOP_PCT` | 5.0 | 10.0 |

### 2. Reduce Max Holding Time

**Rationale:** TIME_STOP accounted for ~52% of all exits across strategies in the
backtest. At 168h (7 days), positions that have lost momentum are held far too long —
capital is tied up earning nothing while faster-moving opportunities pass. Cutting to 120h
forces earlier exits on stalled positions; the trailing stop catches the actual trend
reversals before the time limit anyway.

| Variable | Current | New |
|---|---|---|
| `EMA_B_MAX_HOLDING_HOURS` | 168 | 120 |

### 3. Enable RSI Filter

**Rationale:** D2 (RSI-only filter) showed the best Sharpe ratio (1.18 at 90d) of all
filter ablation variants, with fewer but higher-quality entries. RSI > 75 at entry means
the subnet is already overbought — entering there produces poor risk/reward. Blocking these
entries improves the quality of the trade set without needing any additional code.
Period 14 and overbought threshold 75 are already the configured defaults.

| Variable | Current | New |
|---|---|---|
| `EMA_B_RSI_FILTER_ENABLED` | False | True |
| `EMA_B_RSI_PERIOD` | 14 | 14 (unchanged) |
| `EMA_B_RSI_OVERBOUGHT` | 75.0 | 75.0 (unchanged) |

---

## .env Lines to Change

```
# Strategy B — wider stops (D7 backtest result)
EMA_B_STOP_LOSS_PCT=12.0
EMA_B_TAKE_PROFIT_PCT=30.0
EMA_B_TRAILING_STOP_PCT=10.0

# Strategy B — shorter max hold (reduce TIME_STOP exits)
EMA_B_MAX_HOLDING_HOURS=120

# Strategy B — RSI filter to reject overbought entries
EMA_B_RSI_FILTER_ENABLED=True
```

Restart required after editing `.env`:
```bash
lsof -ti:8081 | xargs kill -9
source .venv/bin/activate && nohup python -u -m app.main >> data/bot.log 2>&1 &
```

---

## Risk Assessment

| Risk | Likelihood | Impact | Notes |
|---|---|---|---|
| Wider SL lets more losing trades run further into loss | Low | Medium | Offset by higher TP and trailing stop; net expectancy still positive at 120d/150d |
| 120h time-stop cuts winners that needed 5–7 days to mature | Low | Medium | Trailing stop at 10% should capture most of the gain before time-stop fires |
| RSI filter blocks entries that would have been profitable | Medium | Low | Fewer trades is acceptable; D2 shows improved Sharpe justifies the filter rate |
| Max drawdown increases with 12% SL vs 8% SL | Medium | Medium | Per-position max loss is larger; monitor NAV drawdown for first 2 weeks |

---

## Rollback Plan

Revert these five lines in `.env` and restart the bot:

```
EMA_B_STOP_LOSS_PCT=8.0
EMA_B_TAKE_PROFIT_PCT=20.0
EMA_B_TRAILING_STOP_PCT=5.0
EMA_B_MAX_HOLDING_HOURS=168
EMA_B_RSI_FILTER_ENABLED=False
```

Open positions are unaffected by a config change — they continue to run with the
stop/TP values already stored in the database at entry time. New entries after
restart will use the reverted values.

---

## Success Criteria

Monitor over the next 30 days of live trading:

- [ ] **Win rate ≥ 55%** on Strategy B trades (vs estimated ~48% pre-change)
- [ ] **TIME_STOP exits drop below 35%** of all B exits (was ~52% across strategies)
- [ ] **Average winning trade > 15% gain** (higher TP gives winners more room)
- [ ] **RSI filter rejection rate < 25%** of signals — if > 25%, the threshold may be
      too aggressive for current market conditions
- [ ] **No single position loss > 13%** (12% SL + ~1% slippage tolerance)
- [ ] Strategy B expectancy positive at next 30d backtest run

If any criterion is violated for two consecutive weeks, roll back the relevant parameter.
