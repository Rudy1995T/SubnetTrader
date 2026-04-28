# Spec: Historical Backtest — EMA Strategy Comparison

## Overview

Build an offline backtest engine that replays historical Taostats price data
through the current strategy logic and compares it against alternative EMA
timeframe configurations. The goal is to measure win rate, risk-adjusted
returns, and signal quality across 7, 14, 30, 90, 120, and 150-day lookback
windows — giving us a picture of where the bot has been, where it is now, and
where performance is trending.

## Motivation

The bot is live-trading the EMA strategy with two parameter sets:

| Tag      | Fast | Slow | Confirm | TF  | SL   | TP   | Trailing |
|----------|------|------|---------|-----|------|------|----------|
| scalper  | 3    | 9    | 3       | 4h  | 8%   | 20%  | 5%       |
| trend    | 3    | 18   | 3       | 4h  | 8%   | 20%  | 5%       |

These were chosen from intuition + limited observation. With 60 req/min on the
Taostats API we can now pull deep history for every subnet and run proper
backtests to answer:

1. **Are our current EMA periods optimal?** Would 5/15, 8/21, 12/26, etc. win more?
2. **Does the 4h candle timeframe beat 1h or 8h?**
3. **How much do the filters (RSI, MACD, BB, Gini, momentum) actually help?**
4. **What is the historical win rate / expectancy by lookback window?**
5. **Are there regimes (trending vs ranging) where one config dominates?**

## Data Acquisition

### Source: Taostats `/api/dtao/pool/history/v1`

```
GET /api/dtao/pool/history/v1?netuid={N}&interval=1h&limit=3600
```

- `interval=1h` gives 1 data point per hour
- `limit=3600` = 150 days of hourly data (the deepest lookback we need)
- Fields: `timestamp`, `price`, `alpha_in_pool`, `tao_in_pool`

### Fetch Strategy

With 60 req/min and ~45 active subnets:

1. **Phase 1 — Bulk download**: Fetch 150-day history for all subnets with
   `pool_depth > MIN_POOL_DEPTH_TAO` (currently 3000 TAO). Estimated ~45
   requests, under 1 minute.
2. **Cache to disk**: Save raw JSON per subnet to `data/backtest/history/sn{N}.json`
   with fetch timestamp. Reuse if < 24h old.
3. **Phase 2 — Incremental**: On subsequent runs, only fetch data newer than
   the cached end timestamp, then append.

Rate-limit: respect `TAOSTATS_RATE_LIMIT_PER_MIN` setting. Add 1s sleep
between requests to stay well under ceiling.

### Pool Snapshot Data

Also fetch current pool snapshots for:
- `alpha_in_pool` (for chunked exit simulation / slippage modeling)
- `total_tao` (for pool depth filtering)

## Backtest Engine

### Location

```
app/backtest/
    __init__.py
    engine.py         # Core backtest loop
    data_loader.py    # Fetch + cache historical data
    strategies.py     # Strategy parameter sets to test
    report.py         # Output formatting (terminal + JSON)
    slippage.py       # Simple slippage model
```

### Core Loop (`engine.py`)

For each (subnet, strategy_config, lookback_window):

```python
def backtest_subnet(
    candles: list[Candle],
    config: StrategyConfig,
    pot_tao: float = 10.0,
) -> BacktestResult:
    """Simulate entry/exit decisions on historical candle data."""
```

1. Walk candles left-to-right, maintaining simulated portfolio state
2. At each bar, compute signals using the **existing** functions:
   - `dual_ema_signal(prices, fast, slow, confirm)` for entry
   - `bullish_ema_bounce()` for bounce entries
   - `compute_rsi()`, `compute_macd()`, `compute_bollinger_bands()` as filters
   - `compute_mtf_signal()` for multi-timeframe confirmation
3. Simulate entries: deduct `position_size_pct * pot_tao` from available capital
4. Simulate exits using the same logic as `EmaManager`:
   - Stop-loss at `-stop_loss_pct%` from entry price
   - Take-profit at `+take_profit_pct%`
   - Trailing stop: activate at `breakeven_trigger_pct`, trail at `trailing_stop_pct`
   - Time stop at `max_holding_hours`
   - EMA cross (SELL signal) exit
5. Apply cooldown between same-subnet re-entries
6. Track per-trade PnL, hold duration, exit reason

### Slippage Model (`slippage.py`)

Since we can't know exact slippage historically, use a simple model:

- **Entry slippage**: `trade_amount / total_pool_tao * slippage_factor`
  where `slippage_factor = 2.0` (conservative constant-product estimate)
- **Exit slippage**: same formula using `alpha_value / alpha_in_pool` ratio
- Cap at `MAX_SLIPPAGE_PCT`
- Use the pool depth from the nearest historical data point

### Position Sizing

Mirror the live bot:
- Fixed pot: `pot_tao` (default 10 TAO)
- Position size: `pot_tao * position_size_pct`
- Max concurrent positions: `max_positions`
- Respect `max_entry_price_tao` filter

## Strategy Configurations to Test

### A. Current Production Configs

| ID  | Tag      | Fast | Slow | Confirm | TF  | Bounce | MTF | Filters |
|-----|----------|------|------|---------|-----|--------|-----|---------|
| A1  | scalper  | 3    | 9    | 3       | 4h  | yes    | yes | current |
| A2  | trend    | 3    | 18   | 3       | 4h  | yes    | yes | current |

### B. Alternative EMA Periods

| ID  | Tag        | Fast | Slow | Confirm | TF  | Notes               |
|-----|------------|------|------|---------|-----|---------------------|
| B1  | fast_cross | 5    | 13   | 2       | 4h  | Faster confirmation  |
| B2  | classic    | 8    | 21   | 3       | 4h  | Traditional swing    |
| B3  | macd_ema   | 12   | 26   | 3       | 4h  | MACD-aligned periods |
| B4  | slow_trend | 9    | 50   | 4       | 4h  | Longer-term trend    |
| B5  | micro      | 2    | 5    | 2       | 4h  | Ultra-fast scalp     |

### C. Alternative Timeframes

| ID  | Tag     | Fast | Slow | Confirm | TF  | Notes                   |
|-----|---------|------|------|---------|-----|-------------------------|
| C1  | hourly  | 3    | 9    | 3       | 1h  | More signals, more noise |
| C2  | 2h      | 3    | 9    | 3       | 2h  | Middle ground            |
| C3  | 8h      | 3    | 9    | 3       | 8h  | Fewer, higher conviction |
| C4  | daily   | 3    | 9    | 2       | 24h | Swing only               |

### D. Filter Ablation (using scalper 3/9 as base)

| ID  | Variation                      | Change from A1              |
|-----|-------------------------------|-----------------------------|
| D1  | No filters                    | RSI/MACD/BB/momentum off    |
| D2  | RSI only                      | RSI on, rest off            |
| D3  | MACD only                     | MACD on, rest off           |
| D4  | Momentum only                 | Momentum filters on only    |
| D5  | All filters on                | RSI + MACD + BB + momentum  |
| D6  | Tighter stops (SL=5, TP=15)   | More aggressive risk mgmt   |
| D7  | Wider stops (SL=12, TP=30)    | Let winners run longer      |
| D8  | No bounce entry               | Bounce disabled             |

### E. Confirm Bar Sensitivity

| ID  | Confirm | Notes                             |
|-----|---------|-----------------------------------|
| E1  | 1       | Fastest entry, most false signals  |
| E2  | 2       | Moderate                           |
| E3  | 4       | Conservative, fewer entries        |
| E4  | 5       | Very selective                     |

## Lookback Windows

Run every configuration across these windows to see regime-dependent behavior:

| Window  | Period               | Bars (4h) | What it tells us               |
|---------|----------------------|-----------|--------------------------------|
| 7 days  | 2026-04-02 → now     | ~42       | Current micro-regime           |
| 14 days | 2026-03-26 → now     | ~84       | Recent trend                   |
| 30 days | 2026-03-10 → now     | ~180      | Monthly cycle                  |
| 90 days | 2026-01-09 → now     | ~540      | Quarterly performance          |
| 120 days| 2025-12-10 → now     | ~720      | Extended trend + drawdown      |
| 150 days| 2025-11-10 → now     | ~900      | Full dataset, seasonal effects |

## Output Metrics

### Per-Strategy Per-Window

```python
@dataclass
class BacktestResult:
    strategy_id: str
    window_days: int
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float              # winning / total
    avg_win_pct: float           # average % gain on winners
    avg_loss_pct: float          # average % loss on losers
    expectancy: float            # (win_rate * avg_win) - ((1-win_rate) * avg_loss)
    profit_factor: float         # gross_wins / gross_losses
    total_pnl_pct: float         # total return on pot
    max_drawdown_pct: float      # worst peak-to-trough
    sharpe_ratio: float          # annualized risk-adjusted return
    avg_hold_hours: float        # mean hold duration
    max_concurrent: int          # peak simultaneous positions
    exit_reasons: dict[str, int] # {stop_loss: N, take_profit: N, trailing: N, ...}
    subnets_traded: list[int]    # which netuids were entered
    trades: list[TradeRecord]    # individual trade log
```

### Aggregate Report

```
=== BACKTEST REPORT: 2026-04-09 ===

Window: 30 days (2026-03-10 → 2026-04-09)
Subnets tested: 42 (pool depth > 3000 TAO)

STRATEGY RANKING (by expectancy):
┌─────┬──────────┬───────┬─────────┬────────┬───────────┬───────────┬──────────┐
│ Rank│ Strategy │ Trades│ Win Rate│ Expect.│ Total PnL │ Max DD    │ Sharpe   │
├─────┼──────────┼───────┼─────────┼────────┼───────────┼───────────┼──────────┤
│  1  │ B2 8/21  │   47  │  62.3%  │ +1.8%  │  +18.4%   │  -6.2%    │  1.42    │
│  2  │ A1 3/9*  │   83  │  55.4%  │ +1.2%  │  +14.1%   │  -8.7%    │  1.15    │
│  3  │ A2 3/18* │   61  │  57.4%  │ +1.1%  │  +12.8%   │  -7.3%    │  1.08    │
│ ... │          │       │         │        │           │           │          │
└─────┴──────────┴───────┴─────────┴────────┴───────────┴───────────┴──────────┘
* = current production config

EXIT REASON BREAKDOWN (A1 scalper):
  stop_loss: 23 (27.7%)  |  take_profit: 18 (21.7%)
  trailing:  31 (37.3%)  |  time_stop:    6 ( 7.2%)
  ema_cross:  5 ( 6.0%)

TOP PERFORMING SUBNETS (A1, 30d):
  SN8:  +4.2% avg  |  SN19: +3.8% avg  |  SN13: +2.9% avg

WORST PERFORMING SUBNETS (A1, 30d):
  SN32: -3.1% avg  |  SN41: -2.7% avg  |  SN5:  -1.9% avg
```

### Trend Analysis (across windows)

For each strategy, show how metrics evolve across time windows:

```
A1 (scalper 3/9) — Win Rate Trend:
  150d: 48.2%  →  120d: 51.0%  →  90d: 53.7%  →  30d: 55.4%  →  14d: 58.1%  →  7d: 61.5%
  Trend: IMPROVING ↑

A1 (scalper 3/9) — Expectancy Trend:
  150d: +0.4%  →  120d: +0.6%  →  90d: +0.8%  →  30d: +1.2%  →  14d: +1.5%  →  7d: +1.9%
  Trend: IMPROVING ↑
```

This tells us if the current market regime favors our strategy or if
performance is degrading.

## Implementation Plan

### Phase 1: Data Layer (data_loader.py)

1. Create `app/backtest/data_loader.py`
2. Fetch 150-day hourly history for all qualifying subnets
3. Cache raw data to `data/backtest/history/sn{N}.json`
4. Build candles at multiple timeframes (1h, 2h, 4h, 8h, 24h) using
   existing `build_candles_from_history()`
5. Add a CLI entry point: `python -m app.backtest.data_loader` to pre-fetch

### Phase 2: Engine (engine.py)

1. Port entry/exit logic from `EmaManager` into a stateless simulation
2. Reuse existing signal functions from `ema_signals.py` and `indicators.py`
3. Implement the slippage model
4. Add position tracking, PnL computation, drawdown tracking

### Phase 3: Strategy Matrix (strategies.py)

1. Define all strategy configs (A, B, C, D, E groups) as `StrategyConfig` instances
2. Matrix: `strategies x subnets x windows` = full parameter sweep

### Phase 4: Reporting (report.py)

1. Compute aggregate metrics per strategy per window
2. Rank strategies by expectancy, Sharpe, win rate
3. Generate trend analysis across windows
4. Output to terminal (formatted tables) + JSON (`data/backtest/results/`)
5. Optional: CSV export for spreadsheet analysis

### Phase 5: CLI Runner

```bash
# Full backtest (all strategies, all windows)
python -m app.backtest --full

# Quick test (production configs only, 30d)
python -m app.backtest --quick

# Specific strategy + window
python -m app.backtest --strategy A1 --window 30

# Just fetch/refresh data
python -m app.backtest --fetch-only

# Export results
python -m app.backtest --full --export csv
```

## Key Insights We're Looking For

1. **Optimal EMA pair**: Which fast/slow combination has the best expectancy
   across all windows? Is it stable or regime-dependent?

2. **Timeframe sensitivity**: Does 4h consistently beat 1h/8h, or are there
   subnets where shorter/longer timeframes win?

3. **Filter value**: Do RSI/MACD/BB filters improve win rate enough to justify
   the missed entries? What's the marginal improvement per filter?

4. **Stop-loss tuning**: Is 8% SL optimal or are we getting stopped out of
   winners? Compare exit reason distributions.

5. **Regime detection**: Do certain EMA configs dominate in trending periods
   vs ranging periods? Can we build an adaptive selector?

6. **Subnet clustering**: Are there subnet groups that consistently respond
   well to specific strategies? (e.g., high-volume subnets prefer slower EMA)

7. **Deterioration warning**: If win rate is declining across windows, that's
   an early signal to adapt parameters before losses mount.

## Constraints

- **No live trading**: Backtest is purely offline simulation
- **No Subtensor calls**: All data comes from Taostats API
- **Slippage is approximate**: Real DTAO slippage depends on exact pool state
  at trade time; the model gives a reasonable estimate
- **Emissions not modeled**: Backtest measures price PnL only; emission yield
  is additive upside not captured here
- **Single subnet per trade**: No portfolio correlation modeling in v1 (the
  Gini/correlation filters are tested as entry gates, not portfolio-level)

## Success Criteria

- [ ] All 45+ subnets fetched with 150d hourly history
- [ ] Backtest runs in < 5 minutes on RPi 5
- [ ] At least 20 strategy configs compared
- [ ] Clear ranking table showing which configs beat current production
- [ ] Trend analysis shows performance direction for each config
- [ ] Results saved to JSON for future comparison
- [ ] Actionable recommendation: keep current params, or switch to {X}
