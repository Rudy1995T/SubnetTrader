# SubnetTrader

Automated Bittensor subnet alpha trading bot with an EMA crossover strategy, web dashboard, and Telegram alerts.

## Quick Start

```bash
git clone <repo-url> SubnetTrader && cd SubnetTrader
bash install.sh
bash start.sh          # opens http://localhost:3000/setup
```

The setup wizard walks you through wallet, API keys, and trading configuration. No `.env` editing required.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Browser — http://localhost:3000                            │
│  ┌──────┐  ┌─────────┐  ┌──────────┐  ┌──────┐            │
│  │ EMA  │  │ Control │  │ Settings │  │Setup │            │
│  └──┬───┘  └────┬────┘  └────┬─────┘  └──┬───┘            │
│     └───────────┴────────────┴────────────┘                │
│                      │ fetch                               │
├──────────────────────┼─────────────────────────────────────┤
│  FastAPI — :8081     ▼                                     │
│  ┌─────────────┐  ┌──────────┐  ┌──────────────┐          │
│  │ EMA Manager │  │ Executor │  │ Config API   │          │
│  │ (strategy)  │  │ (staking)│  │ (setup/save) │          │
│  └──────┬──────┘  └────┬─────┘  └──────────────┘          │
│         │              │                                   │
│  ┌──────┴──────┐  ┌────┴──────────┐  ┌──────────┐         │
│  │ Taostats    │  │ FlameWire RPC │  │ Telegram │         │
│  │ (prices)    │  │ (chain ops)   │  │ (alerts) │         │
│  └─────────────┘  └───────────────┘  └──────────┘         │
│                                                            │
│  SQLite (data/ledger.db) — positions, trades, signals      │
└─────────────────────────────────────────────────────────────┘
```

### Key Files

| Path | Purpose |
|------|---------|
| `app/main.py` | FastAPI server, scheduler, API endpoints |
| `app/portfolio/ema_manager.py` | EMA crossover strategy (entries, exits, risk) |
| `app/chain/executor.py` | On-chain staking: `add_stake`, `unstake_all` |
| `app/chain/flamewire_rpc.py` | FlameWire WebSocket/HTTP RPC client |
| `app/data/taostats_client.py` | Taostats API — pool data, prices |
| `app/config.py` | All settings via pydantic-settings |
| `app/config_api.py` | POST/GET `/api/config`, wallet ops, go-live |
| `app/storage/db.py` | SQLite with WAL mode |
| `app/notifications/telegram.py` | Alerts + bot commands |
| `frontend/src/app/ema/page.tsx` | EMA portfolio dashboard |
| `frontend/src/app/control/page.tsx` | Kill switch, manual triggers, health |
| `frontend/src/app/settings/page.tsx` | Edit config post-setup |
| `frontend/src/app/setup/page.tsx` | First-run setup wizard |

## Configuration Reference

All settings live in `.env` (created from `.env.example` by `install.sh`). They can also be edited from the web UI at `/settings`.

### Wallet

| Variable | Default | Description |
|----------|---------|-------------|
| `BT_WALLET_NAME` | `default` | Bittensor wallet name (directory under wallet path) |
| `BT_WALLET_HOTKEY` | `default` | Hotkey name within the wallet |
| `BT_WALLET_PATH` | `~/.bittensor/wallets` | Path to wallets directory |
| `BT_WALLET_PASSWORD` | _(empty)_ | Coldkey password (leave empty if unencrypted) |

### RPC & Data

| Variable | Default | Description |
|----------|---------|-------------|
| `FLAMEWIRE_API_KEY` | _(empty)_ | FlameWire RPC key for fast chain access. Without it, falls back to public subtensor |
| `FLAMEWIRE_TIMEOUT` | `30.0` | RPC request timeout (seconds) |
| `FLAMEWIRE_RETRIES` | `3` | Retry count on RPC failure |
| `SUBTENSOR_FALLBACK_NETWORK` | `wss://entrypoint-finney.opentensor.ai:443` | Public subtensor endpoint (fallback) |
| `TAOSTATS_API_KEY` | _(empty)_ | Taostats API key. Free tier works (30 req/min) |
| `TAOSTATS_CACHE_TTL_SEC` | `300` | How long to cache pool data (seconds) |

### EMA Strategy

| Variable | Default | Description |
|----------|---------|-------------|
| `EMA_ENABLED` | `true` | Enable the EMA strategy |
| `EMA_DRY_RUN` | `true` | Paper trading mode — no real trades |
| `EMA_DRY_RUN_STARTING_TAO` | `2.0` | Simulated starting balance for paper mode |
| `EMA_POT_TAO` | `10.0` | Fixed TAO trading pot (independent of wallet balance) |
| `EMA_POSITION_SIZE_PCT` | `0.20` | Fraction of pot per position (0.20 = 20%) |
| `EMA_MAX_POSITIONS` | `5` | Maximum concurrent open positions |
| `EMA_PERIOD` | `18` | Slow EMA period (candles) |
| `EMA_FAST_PERIOD` | `6` | Fast EMA period (candles) |
| `EMA_CONFIRM_BARS` | `3` | Bars to confirm a crossover signal |
| `EMA_CANDLE_TIMEFRAME_HOURS` | `4` | Candle size for EMA calculation |
| `EMA_STOP_LOSS_PCT` | `8.0` | Stop-loss threshold (%) |
| `EMA_TAKE_PROFIT_PCT` | `20.0` | Take-profit threshold (%) |
| `EMA_TRAILING_STOP_PCT` | `5.0` | Trailing stop distance (%) |
| `EMA_MAX_HOLDING_HOURS` | `168` | Time-stop: auto-exit after this many hours |
| `EMA_COOLDOWN_HOURS` | `4.0` | Minimum wait before re-entering a subnet after exit |
| `EMA_DRAWDOWN_BREAKER_PCT` | `15.0` | Pause all entries if portfolio drawdown exceeds this |
| `EMA_DRAWDOWN_PAUSE_HOURS` | `6.0` | How long to pause after drawdown breaker trips |
| `EMA_CORRELATION_THRESHOLD` | `0.80` | Skip subnets too correlated with existing positions |
| `EMA_BOUNCE_ENABLED` | `true` | Enable EMA bounce entry signals |
| `EMA_MAX_GINI` | `0.82` | Skip subnets with stake concentration above this |
| `MAX_SLIPPAGE_PCT` | `5.0` | Max allowed slippage on entry (enforced via `safe_staking`) |
| `MAX_ENTRY_PRICE_TAO` | `0.1` | Skip subnets priced above this per alpha token |

### Execution

| Variable | Default | Description |
|----------|---------|-------------|
| `SCAN_INTERVAL_MIN` | `15` | Minutes between strategy scan cycles |
| `EMA_EXIT_WATCHER_ENABLED` | `true` | Continuous exit-condition monitor between scans |
| `EMA_EXIT_WATCHER_SEC` | `15` | Exit watcher check interval (seconds) |

### Telegram (Optional)

| Variable | Default | Description |
|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | _(empty)_ | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | _(empty)_ | Your chat ID (get from @userinfobot) |

Bot commands: `/status`, `/positions`, `/close <id>`, `/pause`, `/resume`, `/run`, `/export`

### Observability

| Variable | Default | Description |
|----------|---------|-------------|
| `LOG_LEVEL` | `INFO` | Python log level (DEBUG, INFO, WARNING, ERROR) |
| `DB_PATH` | `data/ledger.db` | SQLite database path |
| `JSONL_DIR` | `data/logs` | Structured JSON log directory |
| `HEALTH_PORT` | `8081` | FastAPI server port |
| `KILL_SWITCH_PATH` | `./KILL_SWITCH` | Touch this file to pause all trading |

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

- **Coldkey not found**: Check `BT_WALLET_PATH` and `BT_WALLET_NAME`. The wallet directory should exist at `<path>/<name>/`.
- **Could not unlock coldkey**: Wrong password. If the coldkey is encrypted, set `BT_WALLET_PASSWORD` in `.env` or the setup wizard.
- **Balance unavailable**: The chain endpoint may be down. Check your internet connection and `FLAMEWIRE_API_KEY`.

### Trades not executing (live mode)

1. Confirm `EMA_DRY_RUN=false` in `.env` or via `/settings`.
2. Check wallet balance covers `EMA_POT_TAO` (at minimum one position size).
3. Look for slippage rejections in logs: `grep -i slippage data/bot.log`.
4. Ensure `KILL_SWITCH` file does not exist: `ls ./KILL_SWITCH`.
5. Check the Control page health dashboard for service status.

### High slippage / rejected entries

The bot enforces `MAX_SLIPPAGE_PCT` via `safe_staking=True`. If a subnet's pool is thin, the entry will be rejected. Lower `EMA_POT_TAO` or increase `MAX_SLIPPAGE_PCT` (with caution).

### Taostats API errors

- **HTTP 401**: Invalid or expired API key. Update in `/settings` or `.env`.
- **Rate limited**: Free tier allows 30 req/min. The bot respects this automatically. If you hit limits, increase `TAOSTATS_CACHE_TTL_SEC`.

### Telegram not sending alerts

1. Verify both `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set.
2. Use the "Send Test Message" button in `/settings` or `/setup`.
3. Make sure you've sent at least one message to the bot first (Telegram requires this).

### Raspberry Pi specific

- **Slow npm install**: Normal on Pi — first install may take 5-10 minutes.
- **Frontend compilation slow**: First page load after `start.sh` takes ~10s to compile. Subsequent loads are fast.
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
curl -s http://localhost:8081/api/control/export-csv -o trades.csv
```

## License

Private — not for redistribution.
