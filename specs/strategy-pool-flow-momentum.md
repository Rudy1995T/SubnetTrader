# Strategy Spec: Pool Flow Momentum (v2)

> **v2 revision:** tightened data model, explicit emission normalization,
> dual-sided flow confirmation, cold-start and manipulation guards, reuse of
> existing `_flow_history` plumbing in `EmaManager`, and concrete
> promotion gates between backtest phases.

## Overview

Track changes in DTAO pool reserves (`tao_in_pool`, `alpha_in_pool`) between
snapshots to detect **real** buy-side capital inflows before price has fully
adjusted. A surge in TAO reserves combined with a drop in alpha reserves means
aggressive alpha buying is happening on-chain. This is the DTAO equivalent of
on-chain order-flow analysis.

## Motivation

Current EMA strategies react to price *after* it has moved — last full backtest
over 120 days shows 44-53% win rates with 70-130h average holds (see
[data/backtest/results/backtest_20260409_195034.csv](../data/backtest/results/backtest_20260409_195034.csv)).
Pool reserve changes are a **leading indicator** — capital must enter the pool
before the price chart reflects it. A well-filtered flow signal should produce
(a) shorter holds, (b) a different distribution of entries than EMAs, and (c)
complement existing strategies during chop where EMA trend-following stalls.

## Why v2 differs from v1

| Area | v1 | v2 |
|---|---|---|
| Snapshot cadence | new 15-min poller | reuse existing 5-min `get_alpha_prices()` cycle |
| Emission false-positives | flagged as risk | explicit adjustment formula |
| Flow direction | TAO-side only | dual-sided (TAO up **and** alpha down) |
| Existing `_flow_history` in `EmaManager` | duplicated | extracted to `app/strategy/flow_signals.py` |
| Manager architecture | standalone `FlowManager` | `flow_config()` returning shared `StrategyConfig` |
| Cold start | implicit | explicit 52h warmup gate per subnet |
| Manipulation | one line in Risks | magnitude cap + Gini + persistence |
| Time stop | 48h | 24h with 6h first-review |
| Phase gates | vague | concrete metrics to advance |

---

## Data Requirements

### Persisted Pool-Reserve Snapshots

Every scan cycle already fetches `/api/dtao/pool/latest/v1` and caches the
result. We persist a **thin slice** per subnet per cycle:

| Field | Source | Notes |
|---|---|---|
| `netuid` | Taostats `netuid` | |
| `ts` | Cycle timestamp | ISO 8601, UTC |
| `block_number` | Taostats `block_number` (if present) | for emission-rate math |
| `tao_in_pool` | Taostats `tao_in_pool` / 1e9 | TAO tokens |
| `alpha_in_pool` | Taostats `alpha_in_pool` / 1e9 | alpha tokens |
| `price` | Taostats `price` | TAO per alpha |
| `alpha_emission_rate` | Taostats `alpha_emission_rate` or derived | alpha per block |

### New DB Table

```sql
CREATE TABLE IF NOT EXISTS pool_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    netuid          INTEGER NOT NULL,
    ts              TEXT    NOT NULL,
    block_number    INTEGER,
    tao_in_pool     REAL NOT NULL,
    alpha_in_pool   REAL NOT NULL,
    price           REAL NOT NULL,
    alpha_emission_rate REAL
);
CREATE INDEX IF NOT EXISTS idx_pool_snap_netuid_ts
    ON pool_snapshots(netuid, ts);
```

Retention: **14 days** (doubled from v1 so parameter sweeps can span two full
weekly regimes). Prune older rows once per hour.

### Snapshot Cadence

- **Do not** introduce a new poller. Hook into the existing
  `TaostatsClient.get_alpha_prices()` call site in the main cycle
  (`SCAN_INTERVAL_MIN=5` minutes). That gives us 5-minute resolution natively.
- If `SCAN_INTERVAL_MIN` is increased later, z-score windows are expressed in
  **snapshots**, not wall-clock hours, so the math adapts automatically.
- If a cycle skips (rate limit, API error), record the gap; gaps > 30 minutes
  invalidate the current 48h rolling baseline for that subnet until it refills.

---

## Signal Computation

### Raw Flow Delta

For each subnet, compute the change in `tao_in_pool` over windows expressed in
snapshots (default 5-min cadence):

```
flow_1h  = tao_in_pool[now] - tao_in_pool[-12]   # 12 snapshots
flow_4h  = tao_in_pool[now] - tao_in_pool[-48]
flow_12h = tao_in_pool[now] - tao_in_pool[-144]
```

### Emission-Adjusted Flow *(critical v2 addition)*

Alpha emissions continuously mint alpha that miners/validators tend to unstake
and dump into the pool. This raises `tao_in_pool` and drops `alpha_in_pool`
**without** any real buying pressure. We must subtract the expected
emission-driven TAO inflow before interpreting deltas.

```
# Over a window of N blocks between snapshots:
expected_emission_alpha    = alpha_emission_rate * blocks_elapsed
expected_sold_fraction     = 0.60           # empirical: most emitters unstake & sell
expected_emission_tao_in   = expected_emission_alpha * expected_sold_fraction * avg_price

adj_flow_tao = raw_flow_tao - expected_emission_tao_in
```

`expected_sold_fraction` starts at 0.60 and is **calibrated per subnet** during
Phase 2 (sweep 0.4-0.8, pick value that minimises `|mean(adj_flow)|` over a
7-day non-signal window — i.e. emission-adjusted flow should be mean-zero in
the absence of real buyers).

### Dual-Sided Confirmation

Pure TAO inflow is ambiguous (could be emission, could be arbitrage, could be
buying). True buying pressure requires **both**:

```
tao_delta_pct   = adj_flow_tao / tao_in_pool_prev * 100
alpha_delta_pct = (alpha_in_pool[now] - alpha_in_pool[-48]) / alpha_in_pool[-48] * 100

bought = (tao_delta_pct > +X) AND (alpha_delta_pct < -X/2)
```

The asymmetric threshold (alpha drop only needs to be half of tao rise)
reflects bonding-curve math: AMM `k = x*y` means a 1% TAO increase pairs with
less than 1% alpha decrease.

### Flow Z-Score

Normalise `adj_flow_tao / tao_in_pool_prev` (i.e. *percentage* flow, not
absolute) against its own rolling history to detect outliers:

```
flow_pct_series = last 48h of (adj_flow_4h / tao_in_pool) samples
z = (flow_pct_now - ewma(flow_pct_series, halflife=24h)) /
    ewstd(flow_pct_series, halflife=24h)
```

EWMA (rather than a flat mean) is less laggy when the last 4h contain a real
event, and a single old spike doesn't dominate the baseline.

### Magnitude Cap *(manipulation guard)*

Reject any signal where `|tao_delta_pct| > 10%` in a single 5-minute snapshot.
That's not flow — that's a single whale or a pool migration event. Cap values
are logged but not acted on.

### Signal

```
BUY:
    z(flow_pct_4h)     >= FLOW_Z_ENTRY (default 2.0)
    AND tao_delta_pct(4h)   >= FLOW_MIN_TAO_PCT (default 2.0)
    AND alpha_delta_pct(4h) <= -FLOW_MIN_TAO_PCT/2
    AND tao_delta_pct(1h)   > 0              # short-term still accelerating
    AND |tao_delta_pct(5m)| < FLOW_MAGNITUDE_CAP (default 10.0)
    AND price > EMA(18) on 4h candles        # trend confirmation
    AND snapshots_collected[netuid] >= 624   # cold-start gate (52h @ 5-min)

SELL (exit):
    z(flow_pct_4h) <= FLOW_Z_EXIT (default -1.5)
    OR tao_delta_pct(1h) < -FLOW_EXIT_PCT (default -0.5) for N consecutive cycles
    OR regime filter fails (see below)
```

---

## Regime Filter

Flow works in calm / neutral markets. In a market-wide crash, forced unstaking
creates flow-like noise in many subnets at once. Before signalling:

```
aggregate_index = median(price[t] / price[t-288]) over top-50 pools by depth
                                    # 288 snapshots = 24h
if aggregate_index < 0.95:          # whole DTAO market down >5% in 24h
    pause entries
    tighten exits (treat FLOW_Z_EXIT as -1.0)
```

Implemented as a shared helper so other strategies can opt in later.

---

## Entry Logic

- **Position sizing**: same pool-depth-aware sizer as EMA (`pot_sizer.py`),
  capped at `FLOW_MAX_POOL_IMPACT_PCT = 1.5%` — lower than EMA's 2.5% because
  our own entry contaminates the next snapshot's flow reading on the subnet we
  just bought into.
- **Gini filter**: `max_gini = 0.82` (shared with EMA).
- **Correlation guard** against existing holdings across all strategies.
- **Cold-start gate**: reject signal until at least 624 snapshots (52h @ 5-min)
  exist for that subnet. Below that, we don't have 48h of baseline *plus* a 4h
  window.
- **Cross-strategy exclusion**: if EMA already holds a subnet, Flow is blocked
  (flow adds no new information when we already have trend exposure there).
- **Cooldowns (asymmetric)**:
  - 12h after a `STOP_LOSS` / `FLOW_REVERSAL` exit — the flow quality was bad
  - 4h after a `TAKE_PROFIT` — subnet showed real buying, may repeat
  - 6h after a `TIME_STOP` — neutral
- **Slot pool**: separate from EMA, default 3 slots, 10 TAO pot.

---

## Exit Logic

| Reason | Condition |
|---|---|
| FLOW_REVERSAL | `tao_delta_pct(1h) < -0.5` for 2 consecutive 5-min cycles after entry |
| STOP_LOSS | PnL ≤ -6% (tighter than EMA's -8%: flow should work fast) |
| TAKE_PROFIT | PnL ≥ 12% (tightened from v1's 15%; shorter-hold strategy) |
| TRAILING_STOP | Peak drawdown ≥ 4% after reaching +3% |
| TIME_STOP_SOFT | At 6h, if PnL < +1% → scale out 50%, tighten trailing to 2% |
| TIME_STOP_HARD | 24h max hold (reduced from v1's 48h) |
| REGIME_EXIT | aggregate index falls below 0.95 threshold for >1h |

---

## Configuration (.env)

```
# Master switches
FLOW_ENABLED=false
FLOW_DRY_RUN=true

# Slots & sizing
FLOW_SLOTS=3
FLOW_POT_TAO=10.0
FLOW_POSITION_SIZE_PCT=0.33
FLOW_MAX_POOL_IMPACT_PCT=1.5

# Signal thresholds
FLOW_Z_ENTRY=2.0
FLOW_Z_EXIT=-1.5
FLOW_MIN_TAO_PCT=2.0
FLOW_EXIT_PCT=0.5
FLOW_MAGNITUDE_CAP=10.0

# Windows (expressed in snapshots, assume 5-min cadence)
FLOW_WINDOW_1H_SNAPS=12
FLOW_WINDOW_4H_SNAPS=48
FLOW_BASELINE_SNAPS=576          # 48h @ 5-min
FLOW_COLD_START_SNAPS=624        # baseline + one window
FLOW_MAX_GAP_MIN=30

# Emission normalization
FLOW_EMISSION_ADJUST=true
FLOW_EMISSION_SOLD_FRACTION=0.60

# Risk / exits
FLOW_STOP_LOSS_PCT=6.0
FLOW_TAKE_PROFIT_PCT=12.0
FLOW_TRAILING_PCT=4.0
FLOW_TRAILING_TRIGGER_PCT=3.0
FLOW_TIME_SOFT_HOURS=6
FLOW_TIME_HARD_HOURS=24
FLOW_REQUIRE_EMA_CONFIRM=true

# Cooldowns (asymmetric)
FLOW_COOLDOWN_STOP_HOURS=12
FLOW_COOLDOWN_WIN_HOURS=4
FLOW_COOLDOWN_TIME_HOURS=6

# Regime
FLOW_REGIME_FILTER_ENABLED=true
FLOW_REGIME_INDEX_THRESHOLD=0.95
FLOW_REGIME_LOOKBACK_SNAPS=288
```

---

## Telemetry (logged per signal evaluation)

Persist to JSONL in `data/logs/YYYY-MM-DD.jsonl` under `event="flow_eval"`:

```
netuid, ts, snapshots_collected, tao_in_pool, alpha_in_pool,
raw_flow_4h, adj_flow_4h, tao_delta_pct_1h/4h/12h,
alpha_delta_pct_4h, z_score, ewma_flow, ewstd_flow,
regime_index, gini, pool_depth_tao, magnitude_capped (bool),
signal ("BUY"|"HOLD"|"BLOCKED-<reason>")
```

Per-trade telemetry extends existing `ema_positions` row:
`entry_z_score`, `entry_flow_pct`, `entry_adj_flow`, `entry_regime_index`.

---

## Architecture Alignment

Fit the new strategy into the existing shapes:

1. **Reuse `StrategyConfig`** — add a `flow_config()` factory in
   `app/config.py` returning a `StrategyConfig` with `strategy_type="flow"`.
   Avoids a parallel dataclass.
2. **Extract flow helpers from `EmaManager`** — move
   `_compute_flow_delta()` and the `_flow_history` ring buffer into
   `app/strategy/flow_signals.py`. `EmaManager` imports the same functions;
   nothing behavioural changes for existing strategies.
3. **Manager reuse vs. new manager** — implementation choice:
   - *Option A* (preferred): extend `EmaManager` with a `strategy_type` branch
     (`"ema" | "meanrev" | "flow"`) like `meanrev` already lives there. Entry
     signal is the only real difference; exits / sizing / cooldowns all exist.
   - *Option B*: dedicated `FlowManager`. Only choose this if Option A grows
     the conditional jungle beyond readability.
4. **Frontend surface** — one new tab `frontend/src/app/flow/page.tsx` mirrors
   the EMA page, reading `/api/flow/portfolio`, `/api/flow/positions`,
   `/api/flow/signals`.

---

## Backtesting Plan

> **Revised rollout (2026-04-21):** the 14-day warm-up phase that used to
> precede any backtest is obsolete. The harness in
> [backtest-pool-flow-momentum.md](backtest-pool-flow-momentum.md) +
> [finish-flow-backtest-harness.md](NewSpecs/finish-flow-backtest-harness.md)
> replays cached Taostats pool history immediately — no waiting on a live
> snapshot table to fill. Live `pool_snapshots` persistence still runs so
> Phase 3 (paper) has 5-min data, but Phase 1/2 no longer block on it.

### Phase 1 — Historical backtest (no warm-up required)
- Run historical backtest per
  [backtest-pool-flow-momentum.md](backtest-pool-flow-momentum.md) against
  Taostats pool history. Live `pool_snapshots` persistence still runs in the
  background so Phase 3 has real 5-min data when it arrives.
- **Known data limit:** Taostats `/api/dtao/pool/history/v1` silently collapses
  every `interval` to `1d`. The harness runs at 1d cadence behind an explicit
  `--acknowledge-cadence-degradation` flag; expectancy at this cadence is a
  lower bound on what the 5-min live strategy can achieve, not a forecast of
  it.

### Phase 2 — Offline backtest
Replay snapshots, compute adj flow + z-scores, simulate entries against
recorded `price` track, then against fresh pool math for realistic slippage.

**Metrics to report per parameter set:**
- Hit rate, avg win %, avg loss %, expectancy (R), profit factor
- Avg & median hold hours, max hold, time-to-peak-PnL
- Max drawdown, Sharpe
- Distribution of exit reasons
- Overlap % with EMA entries on the same subnet × 4h window

**Parameter sweep grid:**
- `FLOW_Z_ENTRY`: 1.5, 2.0, 2.5, 3.0
- `FLOW_MIN_TAO_PCT`: 1.0, 2.0, 3.0
- `FLOW_STOP_LOSS_PCT`: 4, 6, 8
- `FLOW_TAKE_PROFIT_PCT`: 8, 12, 15
- `FLOW_TIME_HARD_HOURS`: 12, 24, 36
- `FLOW_EMISSION_SOLD_FRACTION`: 0.4, 0.5, 0.6, 0.7, 0.8
- Regime filter on/off

**Promotion gate → Phase 3:**
- Hit rate ≥ 55%, **or** expectancy ≥ 0.5R with hit rate ≥ 45%
- Profit factor ≥ 1.5
- Max drawdown ≤ 15%
- At least 40 trades in the full replay window (otherwise not statistically meaningful)

### Phase 3 — Paper trade (≥ 14 days)
- `FLOW_ENABLED=true`, `FLOW_DRY_RUN=true`. Log signals & simulated fills.

**Promotion gate → Phase 4:**
- Paper P&L > 0 and > EMA baseline over the same window
- No 7-day rolling loss worse than -8%
- Median slippage (predicted vs. "would-have-traded" price) < 2%
- Regime filter correctly paused during any >5% index drops in the window

### Phase 4 — Live
- Start with `FLOW_POT_TAO=5.0` (half of spec default), `FLOW_SLOTS=2`.
- Widen to full pot/slots after 2 live weeks showing Phase-3-comparable stats.

---

## Files to Create / Modify

| File | Action |
|---|---|
| `app/storage/models.sql` | Add `pool_snapshots` table + index |
| `app/storage/db.py` | `save_pool_snapshot()`, `get_pool_snapshots(netuid, since_ts)`, `prune_pool_snapshots()`, `snapshot_count(netuid)` |
| `app/strategy/flow_signals.py` | **New.** `compute_flow_delta()`, `compute_flow_zscore()`, `emission_adjusted_flow()`, `flow_entry_signal()`, `flow_exit_signal()`, `regime_index()` |
| `app/portfolio/ema_manager.py` | Replace inline `_compute_flow_delta` with `flow_signals.compute_flow_delta`; add `strategy_type="flow"` branch in entry evaluation |
| `app/config.py` | Add `FLOW_*` settings + `flow_config()` factory |
| `app/data/taostats_client.py` | Expose `tao_in_pool` / `alpha_in_pool` / `alpha_emission_rate` fields from pool snapshot (already in `_pool_snapshot` dict — just add helper) |
| `app/main.py` | Hook snapshot persistence + pruning into main cycle; register Flow strategy in manager list |
| `frontend/src/app/flow/page.tsx` | **New.** Portfolio / signals / positions UI |
| `tests/test_flow_signals.py` | **New.** Unit tests: delta math, z-score, emission adj, cold-start, magnitude cap |
| `tests/test_flow_manager.py` | **New.** Integration: dry-run entry/exit, gap handling, regime pause |
| `specs/run_qa.py` | Add flow strategy QA cases |

---

## Risks & Mitigations

- **Manipulation (whale pump → dump).** Magnitude cap + Gini filter + dual-sided
  confirmation + persistence over ≥2 snapshots before signal fires.
- **Emission-driven false positives.** Per-subnet-calibrated emission adjustment.
- **Low-frequency data.** 5-min snapshots miss sub-minute whale fills. Acceptable
  — we're not trying to front-run bots, just slow humans/news.
- **Reflexivity.** Our own entry creates flow on the next snapshot. Mitigation:
  position size cap at 1.5% of pool depth; 5-min cooldown window before the
  entry is counted in future flow baselines for that subnet.
- **Illiquid subnets.** Minimum pool depth: reuse `min_pool_depth_tao = 5000`
  (slightly higher than EMA's 3000) — flow signals on thin pools are noise.
- **Cold restarts.** 52h warmup per subnet makes "just deployed" scenarios
  flow-blind. Acceptable; document clearly in operator runbook.
- **Taostats outage.** Gap > 30 min invalidates baselines; signals pause for
  affected subnets until baseline refills.
