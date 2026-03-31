# Strategy Spec: Pool Flow Momentum

## Overview

Track changes in DTAO pool reserves (`tao_in_pool`) between snapshots to detect
capital inflows before the price fully adjusts. A surge in TAO reserves means
aggressive alpha buying is happening on-chain. This is the DTAO equivalent of
on-chain order flow analysis.

## Motivation

Current EMA strategies react to price *after* it has moved. Pool reserve changes
are a **leading indicator** — capital enters the pool before the price chart
reflects it. By measuring flow direction and magnitude, we can front-run slow
price discovery on smaller subnets.

## Data Requirements

### New: Pool Reserve Snapshots

Each scan cycle already fetches `/api/dtao/pool/latest/v1` and caches the result.
We need to **persist** the following per subnet per cycle:

| Field | Source | Notes |
|---|---|---|
| `netuid` | Taostats `netuid` | |
| `ts` | Cycle timestamp | ISO 8601 |
| `tao_in_pool` | Taostats `tao_in_pool` | RAO, divide by 1e9 for tokens |
| `alpha_in_pool` | Taostats `alpha_in_pool` | RAO, divide by 1e9 |
| `price` | Taostats `price` | Spot price at snapshot |

### New DB Table

```sql
CREATE TABLE IF NOT EXISTS pool_snapshots (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    netuid   INTEGER NOT NULL,
    ts       TEXT    NOT NULL,
    tao_in_pool   REAL NOT NULL,   -- in TAO tokens (not RAO)
    alpha_in_pool REAL NOT NULL,   -- in alpha tokens (not RAO)
    price         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pool_snap_netuid_ts ON pool_snapshots(netuid, ts);
```

Retention: keep 7 days of snapshots, prune older rows once per hour.

## Signal Computation

### Flow Delta

For each subnet, compute the change in `tao_in_pool` over configurable windows:

```
flow_1h  = tao_in_pool(now) - tao_in_pool(1h ago)
flow_4h  = tao_in_pool(now) - tao_in_pool(4h ago)
flow_12h = tao_in_pool(now) - tao_in_pool(12h ago)
```

### Flow Z-Score

Normalize the flow against its own rolling history to detect outlier inflows:

```
z_score = (flow_4h - mean(flow_4h, 48h)) / std(flow_4h, 48h)
```

A z-score > 2.0 is a strong inflow signal. A z-score < -2.0 is a strong outflow.

### Signal

```
BUY:  z_score(4h) >= FLOW_Z_ENTRY (default 2.0)
       AND flow_1h > 0 (short-term flow still positive — not reversing)
       AND price > EMA(18) (optional trend confirmation)

SELL: z_score(4h) <= -FLOW_Z_EXIT (default -1.5)
       OR flow_1h reversal after entry (flow_1h < 0 for 2 consecutive cycles)
```

## Entry Logic

- Same position sizing as EMA strategy (pool-depth-aware, max 2.5% price impact)
- Same Gini filter (max 0.82)
- Same correlation guard against existing holdings
- Cooldown: 6h after exit on same subnet (flow signals are slower to reset)
- Separate slot pool from EMA strategies (default 3 slots)

## Exit Logic

| Reason | Condition |
|---|---|
| FLOW_REVERSAL | flow_1h < 0 for 2 consecutive cycles after entry |
| STOP_LOSS | PnL <= -6% (tighter than EMA — flow should work quickly or not at all) |
| TAKE_PROFIT | PnL >= 15% |
| TRAILING_STOP | Peak drawdown >= 5% after reaching +4% |
| TIME_STOP | 48h max hold (flow signals decay fast) |

## Configuration (.env)

```
FLOW_ENABLED=false
FLOW_DRY_RUN=true
FLOW_SLOTS=3
FLOW_POT_TAO=10.0
FLOW_POSITION_SIZE_PCT=0.33
FLOW_Z_ENTRY=2.0
FLOW_Z_EXIT=-1.5
FLOW_STOP_LOSS_PCT=6.0
FLOW_TAKE_PROFIT_PCT=15.0
FLOW_TRAILING_PCT=5.0
FLOW_TRAILING_TRIGGER_PCT=4.0
FLOW_MAX_HOLD_HOURS=48
FLOW_COOLDOWN_HOURS=6
FLOW_REQUIRE_EMA_CONFIRM=true
```

## Backtesting Plan

1. **Phase 1 — Data collection (1 week):** Enable snapshot persistence only.
   No trading. Accumulate 7 days of 15-min reserve snapshots across all subnets.

2. **Phase 2 — Offline backtest:** Replay snapshots, compute flow z-scores,
   simulate entries/exits against recorded prices. Measure:
   - Hit rate (% of flow signals that produce >0% return within 24h)
   - Average return per signal vs. random entry baseline
   - Optimal z-score thresholds (sweep 1.5–3.0)
   - Optimal exit window (12h, 24h, 48h)

3. **Phase 3 — Paper trading:** Enable `FLOW_DRY_RUN=true`, log signals and
   simulated trades alongside real EMA trades for comparison.

4. **Phase 4 — Live:** Enable with small pot after Phase 3 validation.

## Files to Create/Modify

| File | Action |
|---|---|
| `app/storage/models.sql` | Add `pool_snapshots` table |
| `app/storage/db.py` | Add `save_pool_snapshot()`, `get_pool_snapshots()`, `prune_pool_snapshots()` |
| `app/strategy/flow_signals.py` | New: `compute_flow_delta()`, `compute_flow_zscore()`, `flow_signal()` |
| `app/portfolio/flow_manager.py` | New: FlowManager (similar to EmaManager but flow-based entry/exit) |
| `app/config.py` | Add FLOW_* settings |
| `app/main.py` | Register FlowManager, schedule snapshot persistence and flow cycle |
| `app/data/taostats_client.py` | Expose raw `tao_in_pool` / `alpha_in_pool` per subnet in pool snapshot |

## Risks

- **Low-frequency data:** 15-min snapshots may miss fast inflows. Consider
  reducing snapshot interval to 5 min for this strategy only.
- **False positives from emissions:** Subnet emissions continuously add alpha to
  pools. Normalize for emission rate to avoid treating organic emission as "flow."
- **Manipulation:** A whale could pump reserves then dump. The z-score threshold
  and EMA confirmation help, but monitor for adversarial patterns.
