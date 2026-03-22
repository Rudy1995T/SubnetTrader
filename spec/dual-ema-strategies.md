# Dual EMA Strategies — Specification

## Overview

Run two independent EMA strategies simultaneously, each with its own pot, positions,
and EMA parameters. Based on the 200-day backtest:

| Strategy | Fast | Slow | 200d PnL | 90d PnL | Win Rate | Character |
|----------|------|------|----------|---------|----------|-----------|
| **Scalper** | 3 | 9 | +437% | +127% | 41.6% | Aggressive, more trades |
| **Trend** | 3 | 18 | +359% | +165% | 43.6% | Selective, higher conviction |

Both use fast=3 for quick entries but differ in trend confirmation — Scalper reacts
faster (9-bar EMA), Trend requires a stronger trend (18-bar EMA). They naturally
diversify: Scalper catches short moves, Trend holds through noise.

Current 6/18 ranked 9th/10 over 200 days (+165%). Both new strategies outperform it.

---

## Architecture

```
                ┌─────────────┐
                │  main.py    │
                │  scheduler  │
                └──────┬──────┘
            ┌──────────┼──────────┐
            ▼                     ▼
   EmaManager("scalper")   EmaManager("trend")
   fast=3, slow=9          fast=3, slow=18
   pot=5τ, 5 slots         pot=5τ, 5 slots
            │                     │
            └──────────┬──────────┘
                       ▼
              ema_positions table
              (strategy column)
```

Both managers share the same Database, SwapExecutor, and TaostatsClient instances.
They run in the same scheduler cycle but maintain completely separate state.

---

## Changes Required

### 1. Config — `app/config.py`

Add a second set of strategy params. The existing `EMA_*` settings become **Strategy A
(Scalper)**. New `EMA_B_*` settings control **Strategy B (Trend)**.

```python
# Strategy A — "Scalper" (existing EMA_ vars, new defaults)
EMA_ENABLED: bool = True
EMA_DRY_RUN: bool = True
EMA_STRATEGY_TAG: str = "scalper"
EMA_PERIOD: int = 9              # was 18
EMA_FAST_PERIOD: int = 3         # was 6
EMA_POT_TAO: float = 5.0         # was 10 — split the pot
EMA_MAX_POSITIONS: int = 5

# Strategy B — "Trend" (new vars)
EMA_B_ENABLED: bool = True
EMA_B_DRY_RUN: bool = True
EMA_B_STRATEGY_TAG: str = "trend"
EMA_B_PERIOD: int = 18
EMA_B_FAST_PERIOD: int = 3
EMA_B_POT_TAO: float = 5.0
EMA_B_POSITION_SIZE_PCT: float = 0.20
EMA_B_MAX_POSITIONS: int = 5
EMA_B_STOP_LOSS_PCT: float = 8.0
EMA_B_TAKE_PROFIT_PCT: float = 20.0
EMA_B_TRAILING_STOP_PCT: float = 5.0
EMA_B_MAX_HOLDING_HOURS: int = 168
EMA_B_COOLDOWN_HOURS: float = 4.0
EMA_B_BOUNCE_ENABLED: bool = True
```

Strategy B inherits all shared settings it doesn't override (MAX_SLIPPAGE_PCT,
CORRELATION_THRESHOLD, MAX_ENTRY_PRICE_TAO, CANDLE_TIMEFRAME_HOURS, etc.).

### 2. Database — `app/storage/models.sql` + `app/storage/db.py`

Add a `strategy` column to distinguish positions.

**Schema migration (in `_apply_schema`):**
```python
"ALTER TABLE ema_positions ADD COLUMN strategy TEXT DEFAULT 'scalper'"
```

**Query changes — all position queries gain a `strategy` filter:**

```python
async def get_open_ema_positions(self, strategy: str = "scalper") -> list[dict]:
    return await self.fetchall(
        "SELECT * FROM ema_positions WHERE status = 'OPEN' AND strategy = ? ORDER BY entry_ts",
        (strategy,),
    )

async def open_ema_position(self, ..., strategy: str = "scalper") -> int:
    # INSERT includes strategy column

async def get_ema_positions(self, limit: int = 200, strategy: str | None = None) -> list[dict]:
    # If strategy is None, return all (for /history, /export)
    # If strategy is set, filter to that strategy

async def get_closed_ema_positions(self, limit: int = 10, strategy: str | None = None) -> list[dict]:
    # Same pattern
```

**Index:**
```sql
CREATE INDEX IF NOT EXISTS idx_ema_strategy ON ema_positions(strategy);
```

### 3. EmaManager — `app/portfolio/ema_manager.py`

Make the manager parameterised by a strategy config dataclass instead of reading
`settings.*` directly.

**New dataclass:**
```python
@dataclass(frozen=True)
class StrategyConfig:
    tag: str                     # "scalper" or "trend"
    fast_period: int
    slow_period: int
    confirm_bars: int
    pot_tao: float
    position_size_pct: float
    max_positions: int
    stop_loss_pct: float
    take_profit_pct: float
    trailing_stop_pct: float
    breakeven_trigger_pct: float
    max_holding_hours: int
    cooldown_hours: float
    bounce_enabled: bool
    bounce_touch_tolerance_pct: float
    bounce_require_green: bool
    max_gini: float
    correlation_threshold: float
    candle_timeframe_hours: int
    dry_run: bool
    max_slippage_pct: float
    max_entry_price_tao: float
    drawdown_breaker_pct: float
    drawdown_pause_hours: float
```

**Factory functions in config.py:**
```python
def strategy_a_config() -> StrategyConfig:
    """Build StrategyConfig for Strategy A (Scalper) from settings."""
    return StrategyConfig(
        tag=settings.EMA_STRATEGY_TAG,
        fast_period=settings.EMA_FAST_PERIOD,
        slow_period=settings.EMA_PERIOD,
        ...
    )

def strategy_b_config() -> StrategyConfig:
    """Build StrategyConfig for Strategy B (Trend) from settings."""
    return StrategyConfig(
        tag=settings.EMA_B_STRATEGY_TAG,
        fast_period=settings.EMA_B_FAST_PERIOD,
        slow_period=settings.EMA_B_PERIOD,
        ...
    )
```

**EmaManager changes:**
```python
class EmaManager:
    def __init__(
        self,
        db: Database,
        executor: SwapExecutor,
        taostats: TaostatsClient,
        config: StrategyConfig,      # ← NEW: replaces direct settings access
    ) -> None:
        self._cfg = config
        ...
```

All `settings.EMA_*` references inside EmaManager become `self._cfg.*`. This is the
bulk of the change — every method that reads settings needs updating, but it's
mechanical: find-replace `settings.EMA_PERIOD` → `self._cfg.slow_period`, etc.

The DB calls gain `strategy=self._cfg.tag`.

**Cross-strategy subnet exclusion (optional but recommended):**
A subnet occupied by Strategy A should not be entered by Strategy B. Both managers
share a set of occupied netuids during the entry pass. This can be implemented by
passing the occupied set as a parameter to `run_cycle()`:

```python
async def run_cycle(self, globally_occupied: set[int] | None = None) -> dict:
    ...
    occupied = {p.netuid for p in open_positions}
    if globally_occupied:
        occupied |= globally_occupied
```

### 4. Main — `app/main.py`

**Globals:**
```python
ema_scalper: EmaManager | None = None
ema_trend: EmaManager | None = None
```

**init_services:**
```python
if settings.EMA_ENABLED:
    ema_scalper = EmaManager(db, executor, taostats, strategy_a_config())
    await ema_scalper.initialize()

if settings.EMA_B_ENABLED:
    ema_trend = EmaManager(db, executor, taostats, strategy_b_config())
    await ema_trend.initialize()
```

**run_ema_cycle:**
```python
async def run_ema_cycle() -> None:
    # Collect occupied netuids from both strategies
    scalper_netuids = set()
    trend_netuids = set()

    if ema_scalper:
        scalper_netuids = {p.netuid for p in await ema_scalper._open_positions_snapshot()}
    if ema_trend:
        trend_netuids = {p.netuid for p in await ema_trend._open_positions_snapshot()}

    if ema_scalper:
        await ema_scalper.run_cycle(globally_occupied=trend_netuids)
    if ema_trend:
        await ema_trend.run_cycle(globally_occupied=scalper_netuids)
```

**Exit watchers — separate jobs for each strategy:**
```python
sched.add_job(run_scalper_exit_watch, trigger="interval", seconds=15, ...)
sched.add_job(run_trend_exit_watch, trigger="interval", seconds=15, ...)
```

### 5. API Endpoints — `app/main.py`

**New routes:**

| Endpoint | Description |
|----------|-------------|
| `GET /api/ema/portfolio` | Returns both strategies' summaries |
| `GET /api/ema/portfolio/scalper` | Scalper-only summary |
| `GET /api/ema/portfolio/trend` | Trend-only summary |
| `GET /api/ema/signals` | Signals for both strategies |
| `POST /api/ema/close/{position_id}` | Works for either (looked up by ID) |

**Portfolio response shape:**
```json
{
  "scalper": {
    "tag": "scalper",
    "fast_period": 3,
    "slow_period": 9,
    "pot_tao": 5.0,
    "deployed_tao": 2.0,
    "unstaked_tao": 3.0,
    "open_count": 1,
    "max_positions": 5,
    "open_positions": [...]
  },
  "trend": {
    "tag": "trend",
    "fast_period": 3,
    "slow_period": 18,
    "pot_tao": 5.0,
    "deployed_tao": 4.0,
    "unstaked_tao": 1.0,
    "open_count": 2,
    "max_positions": 5,
    "open_positions": [...]
  },
  "combined": {
    "total_pot": 10.0,
    "total_deployed": 6.0,
    "total_open": 3,
    "wallet_balance": 42.5
  }
}
```

### 6. Frontend — `frontend/src/app/ema/page.tsx`

Show two side-by-side strategy cards, each displaying:
- Strategy name and EMA params (e.g., "Scalper 3/9", "Trend 3/18")
- Pot, deployed, unstaked
- Open positions list
- Win rate and PnL from closed trades

Combined summary at the top with total pot and overall PnL.

### 7. Telegram — notifications

Prefix alerts with strategy tag:
```
📈 [SCALPER] EMA ENTRY: SN42 | 1.0 τ | price 0.0045
📉 [TREND] EMA EXIT STOP_LOSS: SN18 | PnL -7.2% (-0.14 τ)
```

---

## .env.example additions

```env
# Strategy A — Scalper (fast reactor)
EMA_STRATEGY_TAG=scalper
EMA_PERIOD=9
EMA_FAST_PERIOD=3
EMA_POT_TAO=5.0

# Strategy B — Trend (selective)
EMA_B_ENABLED=true
EMA_B_DRY_RUN=true
EMA_B_STRATEGY_TAG=trend
EMA_B_PERIOD=18
EMA_B_FAST_PERIOD=3
EMA_B_POT_TAO=5.0
EMA_B_POSITION_SIZE_PCT=0.20
EMA_B_MAX_POSITIONS=5
EMA_B_STOP_LOSS_PCT=8.0
EMA_B_TAKE_PROFIT_PCT=20.0
EMA_B_TRAILING_STOP_PCT=5.0
EMA_B_MAX_HOLDING_HOURS=168
EMA_B_COOLDOWN_HOURS=4.0
EMA_B_BOUNCE_ENABLED=true
EMA_B_BOUNCE_TOUCH_TOLERANCE_PCT=1.0
EMA_B_BOUNCE_REQUIRE_GREEN=true
EMA_B_MAX_GINI=0.82
```

---

## Migration & Rollback

**Forward migration:**
- Add `strategy` column with default `'scalper'` — all existing positions become
  Scalper positions, preserving history
- No data loss, no position disruption

**Rollback:**
- Set `EMA_B_ENABLED=false` in `.env` — Trend strategy stops, Scalper continues solo
- Existing Trend positions stay in DB as OPEN until manually closed or the strategy
  is re-enabled
- To fully revert: restore original EMA_PERIOD/EMA_FAST_PERIOD, ignore `strategy` column

---

## What Does NOT Change

- SwapExecutor, FlameWire RPC, Taostats client — shared, stateless
- EMA signal functions (`compute_ema`, `dual_ema_signal`, `build_sampled_candles`) — pure
  functions, already parameterised
- Exit logic (stop-loss, take-profit, trailing, breakeven, time stop, EMA cross) —
  same rules, just read from StrategyConfig instead of settings
- Wallet — both strategies share the same coldkey/hotkey

---

## Implementation Order

1. Add `StrategyConfig` dataclass and factory functions to `config.py`
2. Add `strategy` column migration to `db.py` + update all DB methods
3. Refactor `EmaManager.__init__` to accept `StrategyConfig`
4. Mechanical replace: `settings.EMA_*` → `self._cfg.*` throughout `ema_manager.py`
5. Update `main.py`: two manager instances, two exit watchers, updated API routes
6. Update Telegram alerts with strategy tag prefix
7. Update frontend to show dual strategy cards
8. Update `.env.example` and `.env` with new defaults
