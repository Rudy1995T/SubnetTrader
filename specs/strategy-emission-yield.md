# Strategy Spec: Emission Yield Harvesting

## Overview

Target high-emission, low-volatility subnets and hold staked positions to
accumulate alpha token emissions over time. This is the DTAO equivalent of
dividend investing — the return comes from yield, not price speculation.

## Motivation

Every Bittensor subnet distributes emissions to staked alpha holders. The
current EMA/scalper strategies hold positions for 8–48h on average, which
captures minimal emission yield. Some subnets have high emission rates and
relatively stable prices — ideal for a passive hold strategy that earns yield
while accepting modest price risk.

This strategy runs alongside the active trading strategies with separate capital
allocation, providing a steady baseline return that is less correlated with
price action.

## Data Requirements

### Emission Data (new)

Emission rate per subnet is available from the Bittensor metagraph:

```python
subtensor = bittensor.Subtensor(network=settings.SUBTENSOR_NETWORK)
# Total emission across all subnets (tempo-adjusted)
# Per-subnet: metagraph emission field or subnet hyperparameters
```

Alternatively, Taostats may expose emission data via API — check for
`emission` or `tempo` fields in the subnet metadata endpoints.

**Key metric:** emission rate expressed as **daily alpha tokens emitted per
alpha staked** (yield rate).

### Volatility Data

Computed from existing `seven_day_prices`:

```
daily_returns = [close(d) / close(d-1) - 1 for d in days]
volatility_7d = stddev(daily_returns)
```

### New DB Table

```sql
CREATE TABLE IF NOT EXISTS yield_positions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    netuid              INTEGER NOT NULL,
    status              TEXT DEFAULT 'OPEN',
    entry_ts            TEXT NOT NULL,
    exit_ts             TEXT DEFAULT NULL,
    entry_price         REAL NOT NULL,
    exit_price          REAL DEFAULT NULL,
    amount_tao          REAL NOT NULL,
    amount_alpha        REAL NOT NULL,        -- alpha at entry
    amount_alpha_exit   REAL DEFAULT NULL,    -- alpha at exit (includes emissions)
    emission_alpha      REAL DEFAULT NULL,    -- exit_alpha - entry_alpha
    amount_tao_out      REAL DEFAULT NULL,
    pnl_tao             REAL DEFAULT NULL,
    pnl_pct             REAL DEFAULT NULL,
    staked_hotkey       TEXT DEFAULT '',
    exit_reason         TEXT DEFAULT NULL
);
CREATE INDEX IF NOT EXISTS idx_yield_status ON yield_positions(status);
```

## Subnet Selection Criteria

Score each subnet and select the top N by composite yield score:

### 1. Emission Yield (weight: 0.5)

```
emission_yield = daily_emission_alpha / total_staked_alpha
```

Higher is better. Normalize across subnets to 0–1 range.

### 2. Price Stability (weight: 0.3)

```
stability = 1 - min(volatility_7d / 0.10, 1.0)
```

Volatility < 3% is ideal (stability ~0.7+). Volatility > 10% scores 0.

### 3. Pool Depth (weight: 0.2)

```
depth_score = min(tao_in_pool / 1000, 1.0)
```

Deep pools = low slippage entry/exit. Pools with < 100 TAO are excluded.

### Composite Score

```
yield_score = 0.5 * emission_yield_norm + 0.3 * stability + 0.2 * depth_score
```

Rescore all subnets once per cycle (every 4h). Only enter top-scoring subnets.

## Entry Logic

- **Rebalance cycle:** Every 4 hours (longer than EMA's 15 min)
- **Slot count:** 5 slots (diversified across subnets)
- **Position size:** Equal weight (20% of yield pot per slot)
- **Entry filter:** `yield_score >= MIN_YIELD_SCORE` (default 0.5)
- **Gini filter:** Same max 0.82 (whale-dominated subnets have unstable emissions)
- **No EMA confirmation required** — this is not a momentum trade
- Respect fee reserve (0.5 TAO)

## Exit Logic

This strategy holds longer than EMA. Exits are defensive, not profit-taking:

| Reason | Condition |
|---|---|
| YIELD_REBALANCE | Subnet drops out of top-N by yield_score — rotate into better opportunity |
| STOP_LOSS | PnL <= -12% (wider stop — tolerating price noise for yield) |
| VOLATILITY_SPIKE | 7d volatility exceeds 15% (thesis broken — no longer "stable") |
| MANUAL_CLOSE | User intervention |

**No take-profit or trailing stop.** The goal is to hold and accumulate. If the
price also appreciates, that's a bonus captured on eventual rebalance.

### Rebalance Mechanics

Every 4h:
1. Rescore all subnets
2. For each open position: if subnet is still in top-N and above min score, hold
3. If a held subnet drops below min score or out of top-N, exit and replace
4. If a slot is empty, fill with next best subnet from ranked list
5. Stagger rebalance exits to avoid simultaneous unstake pressure

## Configuration (.env)

```
YIELD_ENABLED=false
YIELD_DRY_RUN=true
YIELD_SLOTS=5
YIELD_POT_TAO=15.0
YIELD_POSITION_SIZE_PCT=0.20
YIELD_MIN_SCORE=0.5
YIELD_REBALANCE_HOURS=4
YIELD_STOP_LOSS_PCT=12.0
YIELD_MAX_VOLATILITY=0.15
YIELD_MIN_POOL_TAO=100.0
YIELD_COOLDOWN_HOURS=12
YIELD_EMISSION_WEIGHT=0.5
YIELD_STABILITY_WEIGHT=0.3
YIELD_DEPTH_WEIGHT=0.2
```

## Measuring Performance

Standard PnL doesn't capture this strategy well. Track:

- **Total return = price PnL + emission value**
  ```
  emission_tao = emission_alpha * exit_price
  total_pnl = (tao_out - tao_in) + emission_tao
  ```
- **Annualized yield** = total_return / holding_days * 365
- **Sharpe ratio** = mean(daily_returns) / std(daily_returns) * sqrt(365)

Surface these in the frontend as a separate "Yield" portfolio card.

## Backtesting Plan

1. **Phase 1 — Emission data collection (1 week):** Query metagraph for
   emission rates per subnet. Store alongside pool snapshots. Determine which
   subnets have been consistently high-emission over the past month.

2. **Phase 2 — Historical yield estimation:** For existing closed EMA trades,
   compute `exit_alpha - entry_alpha` to measure emission accrual. Correlate
   with holding time and subnet to estimate per-subnet yield rates.

3. **Phase 3 — Simulated hold:** Take the top 5 subnets by yield_score as of
   7 days ago. Simulate entering at that point and holding to today. Compare
   total return (price + estimated emission) vs. EMA strategy returns over
   the same period.

4. **Phase 4 — Paper trade** (`YIELD_DRY_RUN=true`) for 2 weeks. Track
   emission accrual in real staked positions.

## Files to Create/Modify

| File | Action |
|---|---|
| `app/storage/models.sql` | Add `yield_positions` table |
| `app/storage/db.py` | Add yield position CRUD |
| `app/strategy/yield_scoring.py` | New: `compute_emission_yield()`, `compute_stability()`, `score_subnets()` |
| `app/portfolio/yield_manager.py` | New: YieldManager (rebalance logic, position tracking) |
| `app/data/taostats_client.py` | Expose emission rate data (if available via API) |
| `app/chain/executor.py` | Add `get_subnet_emission()` via metagraph query |
| `app/config.py` | Add YIELD_* settings |
| `app/main.py` | Register YieldManager, schedule 4h rebalance cycle |
| `frontend/` | Add Yield portfolio card |

## Risks

- **Emission rate changes:** Bittensor adjusts emission allocation dynamically
  based on subnet performance. A high-emission subnet today may lose emission
  share tomorrow. The 4h rebalance cycle mitigates this.
- **Impermanent loss:** DTAO pools can shift. Holding through large price swings
  means the alpha devalues even as you earn more of it. The 12% stop-loss and
  volatility spike exit are guardrails.
- **Opportunity cost:** Capital locked in yield positions can't be used for
  active trading. Size the yield pot separately and accept this trade-off.
- **Emission data accuracy:** Metagraph emission queries add RPC load. Cache
  aggressively (emission rates don't change faster than every tempo/epoch).
