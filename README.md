# SubnetTrader

Automated Bittensor subnet alpha trading bot with dual EMA crossover strategies, multi-filter entry logic, risk management, a web dashboard, and Telegram alerts.

## Quick Start

```bash
git clone <repo-url> SubnetTrader && cd SubnetTrader
bash install.sh
bash start.sh          # opens http://localhost:3000/setup
```

The setup wizard walks you through wallet, API keys, and trading configuration. No `.env` editing required.

## Architecture

```
+-------------------------------------------------------------+
|  Browser - http://localhost:3000                             |
|  +------+  +---------+  +----------+  +------+              |
|  | EMA  |  | Control |  | Settings |  |Setup |              |
|  +--+---+  +----+----+  +----+-----+  +--+---+              |
|     +-----------+------------+-----------+                   |
|                      | fetch                                 |
+----------------------|---------------------------------------+
|  FastAPI - :8081     v                                       |
|  +---------+  +---------+  +---------+  +--------+          |
|  | Scalper |  |  Trend  |  |Executor |  |Config  |          |
|  | Manager |  | Manager |  |(staking)|  |  API   |          |
|  +----+----+  +----+----+  +----+----+  +--------+          |
|       |            |            |                            |
|  +----+----+  +----+------+  +--+------+  +---------+       |
|  |Taostats |  | Subtensor |  |Telegram |  |Indicators|      |
|  |(prices) |  |(chain ops)|  |(alerts) |  |(RSI,BB,.)|      |
|  +---------+  +-----------+  +---------+  +----------+      |
|                                                              |
|  SQLite (data/ledger.db) - positions, trades, signals        |
+--------------------------------------------------------------+
```

### Key Files

| Path | Purpose |
|------|---------|
| `app/main.py` | FastAPI server, scheduler, API endpoints |
| `app/portfolio/ema_manager.py` | EMA strategy logic (entries, exits, risk) |
| `app/portfolio/pot_sizer.py` | Dynamic pot sizing from wallet balance |
| `app/chain/executor.py` | On-chain staking: `add_stake`, `unstake_all`, slippage quotes |
| `app/strategy/ema_signals.py` | EMA crossover signal computation, candle building |
| `app/strategy/indicators.py` | RSI, Bollinger Bands, MACD, volatility indicators |
| `app/data/taostats_client.py` | Taostats API — pool data, prices, history |
| `app/config.py` | All settings via pydantic-settings |
| `app/config_api.py` | POST/GET `/api/config`, wallet ops, go-live |
| `app/storage/db.py` | SQLite with WAL mode |
| `app/notifications/telegram.py` | Alerts + bot commands |
| `frontend/src/app/ema/page.tsx` | EMA portfolio dashboard |
| `frontend/src/app/control/page.tsx` | Kill switch, manual triggers, health |
| `frontend/src/app/settings/page.tsx` | Edit config post-setup |
| `frontend/src/app/setup/page.tsx` | First-run setup wizard |

## Dual Strategy Architecture

The bot runs **two parallel EMA strategies** with independent pools, positions, and parameters:

| | Scalper (Strategy A) | Trend (Strategy B) |
|---|---|---|
| **Purpose** | Fast-moving, tight targets | Slower, longer holds |
| **Default EMA** | Fast=3, Slow=9 | Fast=3, Slow=18 |
| **Config prefix** | `EMA_*` | `EMA_B_*` |
| **Enabled by** | `EMA_ENABLED` | `EMA_B_ENABLED` |

Both strategies use identical logic with different parameter sets. They have separate position pools, cooldowns, and pots. **Cross-strategy exclusion** prevents both from entering the same subnet simultaneously.

## Entry Logic

### Signal Generation

Entry signals are computed from dual EMA crossovers on 4-hour candles (configurable):

- **BUY**: Last `confirm_bars` closes all above both the slow and fast EMA
- **SELL**: Either EMA signals sell (conservative)
- **Bounce entry**: Detects bullish pullbacks where the low touches the slow EMA with a green close

Deep history (~14 days of 1h candles) is fetched on startup and before each entry for accurate EMA warmup.

### Candidate Ranking

Entry candidates are scored on **freshness and liquidity**:

```
freshness = confirm_bars / bars_above_ema    (decays as signal ages)
score     = freshness * log(tao_in_pool)     (liquidity-weighted)
```

### Entry Filters

Candidates must pass all enabled filters before entry:

| Filter | Default | Description |
|--------|---------|-------------|
| **Momentum** | Enabled | Rejects if daily/weekly returns < -5% or structural decline > -10% |
| **Multi-Timeframe (MTF)** | Enabled | Lower timeframe (1h) must show bullish EMA alignment |
| **Volatility Sizing** | Enabled | Scales position size by 24h rolling volatility |
| **Gini Coefficient** | Max 0.82 | Rejects subnets with high validator stake concentration (with hysteresis) |
| **Correlation Guard** | Max 0.80 | Blocks entry if price correlation with existing positions is too high |
| **Pool Depth** | Min 3000 TAO | Requires minimum pool reserves for liquidity |
| **Max Entry Price** | 0.1 TAO | Skips subnets priced above this per alpha token |
| **Max Slippage** | 5% | Enforced via `safe_staking=True` with pre-trade pool refresh |
| **Bollinger Bands** | Disabled | Rejects if closing in upper band |
| **RSI** | Disabled | Rejects overbought (>75) or oversold (<25) |
| **MACD** | Disabled | Validates histogram direction |

## Exit Logic

Positions are monitored continuously (every 15s by default) and exited on any of these triggers:

| Trigger | Default | Description |
|---------|---------|-------------|
| **Stop Loss** | -8% | Hard floor exit |
| **Take Profit** | +20% | Hard ceiling exit |
| **Trailing Stop** | 5% | Activates at +3% profit (breakeven trigger); dynamic mode adjusts trail % based on distance above breakeven |
| **Time Stop** | 168h (7 days) | Force close after max holding period |
| **Flow Reversal** | 3 consecutive | Exits on sustained outflows detected from pool reserve changes |
| **EMA Cross** | — | Exits when EMA signals flip to SELL |
| **Structural Decline** | — | Exits on daily/weekly momentum collapse |
| **Circuit Breaker** | -15% portfolio | Pauses all entries for 6h if portfolio drawdown exceeds threshold |

### Post-Exit Verification

After exiting, the bot verifies on-chain that alpha was actually unstaked. If verification fails, it retries with exponential backoff (up to 3 attempts) and flags the position as "stuck".

### PnL Calculation

- **Entry price** = cost basis (TAO spent / alpha received), not spot price — slippage-aware
- **Exit PnL** = actual TAO returned (wallet balance delta) vs TAO deployed

## Position Sizing & Pot Management

### Fixed Mode (default: `EMA_POT_MODE=fixed`)

Each strategy has a fixed TAO pot:

- `EMA_POT_TAO` / `EMA_B_POT_TAO` (default: 5.0 TAO each)
- Position size = `pot * EMA_POSITION_SIZE_PCT` (default: 33% = 1.65 TAO per slot)
- Max positions: `EMA_MAX_POSITIONS` (default: 3)

### Wallet-Split Mode (`EMA_POT_MODE=wallet_split`)

Dynamically scales pots from live wallet balance each cycle:

```
spendable   = wallet_balance - EMA_FEE_RESERVE_TAO
pot_scalper  = spendable * EMA_POT_WEIGHT
pot_trend    = spendable * (1 - EMA_POT_WEIGHT)
```

### Volatility-Aware Sizing (when enabled)

Scales position size by recent volatility with configurable min/max bounds (10%-40% of pot), targeting a risk budget per position (default: 2% of pot).

## Emission Tracking

The bot periodically snapshots on-chain alpha balances to track staking emissions:

```
emission_alpha = current_alpha_balance - alpha_at_entry
emission_tao   = emission_alpha * spot_price
```

Emission data is shown on the dashboard and included in portfolio summaries.

## Configuration Reference

All settings live in `.env` (created from `.env.example` by `install.sh`). They can also be edited from the web UI at `/settings`.

### Wallet

| Variable | Default | Description |
|----------|---------|-------------|
| `BT_WALLET_NAME` | `default` | Bittensor wallet name |
| `BT_WALLET_HOTKEY` | `default` | Hotkey name within the wallet |
| `BT_WALLET_PATH` | `~/.bittensor/wallets` | Path to wallets directory |
| `BT_WALLET_PASSWORD` | _(empty)_ | Coldkey password (leave empty if unencrypted) |

### RPC & Data

| Variable | Default | Description |
|----------|---------|-------------|
| `SUBTENSOR_NETWORK` | `wss://entrypoint-finney.opentensor.ai:443` | Subtensor WebSocket endpoint |
| `TAOSTATS_API_KEY` | _(empty)_ | Taostats API key for pool data and prices |
| `TAOSTATS_CACHE_TTL_SEC` | `300` | How long to cache pool data (seconds) |
| `TAOSTATS_RATE_LIMIT_PER_MIN` | `30` | API rate limit cap |
| `PREFERRED_VALIDATORS` | _(hardcoded)_ | Validator hotkeys to prefer for staking |

### Strategy (A = Scalper, B = Trend)

All `EMA_*` variables have `EMA_B_*` counterparts for the Trend strategy.

| Variable | Default | Description |
|----------|---------|-------------|
| `EMA_ENABLED` / `EMA_B_ENABLED` | `true` | Enable the strategy |
| `EMA_DRY_RUN` | `true` | Paper trading mode — no real trades |
| `EMA_DRY_RUN_STARTING_TAO` | `2.0` | Simulated starting balance for paper mode |
| `EMA_PERIOD` | `9` (A), `18` (B) | Slow EMA period (candles) |
| `EMA_FAST_PERIOD` | `3` | Fast EMA period (candles) |
| `EMA_CONFIRM_BARS` | `3` | Bars to confirm a crossover signal |
| `EMA_CANDLE_TIMEFRAME_HOURS` | `4` | Candle size for EMA calculation |

### Pot & Sizing

| Variable | Default | Description |
|----------|---------|-------------|
| `EMA_POT_MODE` | `fixed` | `fixed` or `wallet_split` |
| `EMA_POT_TAO` / `EMA_B_POT_TAO` | `5.0` | Fixed TAO trading pot per strategy |
| `EMA_POSITION_SIZE_PCT` | `0.33` | Fraction of pot per position |
| `EMA_MAX_POSITIONS` | `3` | Maximum concurrent open positions |
| `EMA_POT_WEIGHT` | `0.5` | Strategy A's share in wallet-split mode |
| `EMA_FEE_RESERVE_TAO` | `1.0` | TAO held back for fees (wallet-split mode) |

### Risk Management

| Variable | Default | Description |
|----------|---------|-------------|
| `EMA_STOP_LOSS_PCT` | `8.0` | Stop-loss threshold (%) |
| `EMA_TAKE_PROFIT_PCT` | `20.0` | Take-profit threshold (%) |
| `EMA_TRAILING_STOP_PCT` | `5.0` | Trailing stop distance (%) |
| `EMA_TRAILING_STOP_DYNAMIC` | `true` | Dynamically adjust trail % |
| `EMA_BREAKEVEN_TRIGGER_PCT` | `3.0` | Profit % to activate trailing stop |
| `EMA_MAX_HOLDING_HOURS` | `168` | Time-stop: auto-exit after 7 days |
| `EMA_COOLDOWN_HOURS` | `4.0` | Minimum wait before re-entering a subnet |
| `EMA_DRAWDOWN_BREAKER_PCT` | `15.0` | Pause entries if portfolio drawdown exceeds this |
| `EMA_DRAWDOWN_PAUSE_HOURS` | `6.0` | How long to pause after circuit breaker trips |
| `MAX_SLIPPAGE_PCT` | `5.0` | Max allowed slippage on entry/exit |
| `EMA_PRE_TRADE_MAX_SLIPPAGE_PCT` | `4.0` | Stricter pre-entry slippage check |
| `MAX_ENTRY_PRICE_TAO` | `0.1` | Skip subnets priced above this |
| `FEE_RESERVE_TAO` | `0.5` | Global fee buffer |

### Entry Filters

| Variable | Default | Description |
|----------|---------|-------------|
| `EMA_MOMENTUM_FILTERS_ENABLED` | `true` | Day/week momentum pre-filter |
| `EMA_MTF_ENABLED` | `true` | Multi-timeframe EMA confirmation |
| `EMA_MTF_LOWER_TF_HOURS` | `1` | Lower timeframe for MTF check |
| `EMA_VOL_SIZING_ENABLED` | `true` | Volatility-aware position sizing |
| `EMA_VOL_WINDOW` | `24` | Volatility lookback (hours) |
| `EMA_CORRELATION_THRESHOLD` | `0.80` | Max correlation with existing positions |
| `EMA_BOUNCE_ENABLED` | `true` | EMA bounce entry signals |
| `EMA_MAX_GINI` | `0.82` | Max stake concentration (Gini) |
| `EMA_MIN_POOL_DEPTH_TAO` | `3000` | Minimum pool depth for entry |
| `EMA_RSI_FILTER_ENABLED` | `false` | RSI overbought/oversold filter |
| `EMA_MACD_FILTER_ENABLED` | `false` | MACD histogram filter |
| `EMA_BB_FILTER_ENABLED` | `false` | Bollinger Bands filter |

### Exit Features

| Variable | Default | Description |
|----------|---------|-------------|
| `EMA_FLOW_REVERSAL_EXIT_ENABLED` | `true` | Exit on sustained outflows |
| `EMA_POST_EXIT_VERIFY` | `true` | Verify unstake on-chain after exit |
| `EMA_FRESH_POOL_ON_TRADE` | `true` | Refresh pool state before trades |

### Subnet History (EMA Warmup)

| Variable | Default | Description |
|----------|---------|-------------|
| `SUBNET_HISTORY_ENABLED` | `true` | Deep history loading |
| `SUBNET_HISTORY_INTERVAL` | `1h` | Fetch 1h candles for warmup |
| `SUBNET_HISTORY_LIMIT` | `336` | ~14 days of history |
| `SUBNET_HISTORY_ON_STARTUP` | `true` | Warm up existing positions on restart |
| `SUBNET_HISTORY_ON_ENTRY` | `true` | Fetch deep history before entering |

### Execution

| Variable | Default | Description |
|----------|---------|-------------|
| `SCAN_INTERVAL_MIN` | `5` | Minutes between full strategy scan cycles |
| `EMA_EXIT_WATCHER_ENABLED` | `true` | Continuous exit-condition monitor between scans |
| `EMA_EXIT_WATCHER_SEC` | `15` | Exit watcher check interval (seconds) |
| `EMA_ENTRY_WATCHER_SEC` | `90` | Entry crossover poll interval (seconds) |

### Telegram (Optional)

| Variable | Default | Description |
|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | _(empty)_ | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | _(empty)_ | Your chat ID (get from @userinfobot) |

Bot commands: `/help`, `/status`, `/positions`, `/close SN{netuid}`, `/history`, `/export`, `/pause`, `/resume`

### Observability

| Variable | Default | Description |
|----------|---------|-------------|
| `LOG_LEVEL` | `INFO` | Python log level |
| `LOG_RETENTION_DAYS` | `3` | Delete JSONL logs older than this |
| `DB_PATH` | `data/ledger.db` | SQLite database path |
| `JSONL_DIR` | `data/logs` | Structured JSON log directory |
| `HEALTH_PORT` | `8081` | FastAPI server port |
| `KILL_SWITCH_PATH` | `./KILL_SWITCH` | Touch this file to pause all trading |

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Basic health check |
| GET | `/api/health/services` | Taostats / Telegram / DB status |
| GET | `/api/ema/portfolio` | Current holdings, PnL, emissions |
| GET | `/api/ema/positions` | Open positions detail |
| GET | `/api/ema/recent-trades` | Last N closed trades |
| GET | `/api/ema/signals` | Real-time EMA signals (top 120 subnets) |
| POST | `/api/ema/positions/{id}/close` | Manual position close |
| GET | `/api/ema/stuck-positions` | Failed post-exit verifications |
| GET | `/api/ema/slippage-stats` | Entry/exit slippage analytics |
| GET | `/api/subnets/{netuid}/history` | Price history for charts |
| GET | `/api/subnets/{netuid}/spot` | Live price (on-chain or Taostats) |
| GET | `/api/price/tao-usd` | TAO/USD rate (cached 2 min) |
| GET | `/api/export/trades.csv` | CSV export of all trades |
| POST | `/api/control/pause` | Pause trading |
| POST | `/api/control/resume` | Resume trading |
| POST | `/api/control/run-cycle` | Trigger manual scan cycle |
| GET/POST | `/api/config/get`, `/api/config/set` | Read/write settings |

## Troubleshooting

### Bot won't start

```bash
# Check if port 8081 is already in use
lsof -ti:8081

# Check Python venv is activated
source .venv/bin/activate && python -m app.main

# Check logs
tail -50 data/bot.log
```

### Frontend shows "Cannot connect to backend"

The backend must be running on port 8081 before the frontend can fetch data.

```bash
# Verify backend is up
curl -s http://localhost:8081/health

# Restart everything
bash start.sh
```

### Wallet verification fails

- **Coldkey not found**: Check `BT_WALLET_PATH` and `BT_WALLET_NAME`.
- **Could not unlock coldkey**: Wrong password. Set `BT_WALLET_PASSWORD` in `.env`.
- **Balance unavailable**: The chain endpoint may be down. Check your internet connection.

### Trades not executing (live mode)

1. Confirm `EMA_DRY_RUN=false` in `.env` or via `/settings`.
2. Check wallet balance covers at least one position size.
3. Look for slippage rejections in logs: `grep -i slippage data/bot.log`.
4. Ensure `KILL_SWITCH` file does not exist: `ls ./KILL_SWITCH`.
5. Check the Control page health dashboard for service status.

### High slippage / rejected entries

The bot enforces `MAX_SLIPPAGE_PCT` via `safe_staking=True` and runs a pre-trade pool refresh. If a subnet's pool is thin, the entry will be rejected. Lower pot size or increase `MAX_SLIPPAGE_PCT` (with caution).

### Taostats API errors

- **HTTP 401**: Invalid or expired API key. Update in `/settings` or `.env`.
- **Rate limited**: Respect `TAOSTATS_RATE_LIMIT_PER_MIN`. If you hit limits, increase `TAOSTATS_CACHE_TTL_SEC`.

### Telegram not sending alerts

1. Verify both `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set.
2. Use the "Send Test Message" button in `/settings` or `/setup`.
3. Make sure you've sent at least one message to the bot first (Telegram requires this).

### Raspberry Pi specific

- **Slow npm install**: Normal on Pi — first install may take 5-10 minutes.
- **Frontend compilation slow**: First page load after `start.sh` takes ~10s to compile.
- **Memory**: The bot + frontend use ~300MB RAM. A Pi 5 with 4GB+ is recommended.

### Common Commands

```bash
# View today's structured logs
tail -30 data/logs/$(date -u +%Y-%m-%d).jsonl

# Check if bot process is running
pgrep -f "python.*app.main"

# Force-kill backend
lsof -ti:8081 | xargs kill -9

# Query EMA portfolio via API
curl -s http://localhost:8081/api/ema/portfolio | python3 -m json.tool

# Export trade history
curl -s http://localhost:8081/api/export/trades.csv -o trades.csv
```

## License

Private — not for redistribution.
