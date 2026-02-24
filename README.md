# 🧠 Bittensor Subnet Alpha Trading Bot

An automated trading bot that scans Bittensor subnets, scores "pump candidates" using an ensemble of technical signals, and manages positions in subnet alpha tokens — all while maintaining strict risk controls and comprehensive logging.

## ✨ Features

- **Ensemble Scoring**: 6 independent signals (trend, S/R, Fibonacci, volatility, mean-reversion, value-band) combined into a weighted composite score
- **4-Slot Portfolio**: Manages up to 4 concurrent alpha positions, each representing 25% of deployable TAO
- **Risk Management**: Daily drawdown limits, trade caps, per-subnet cooldowns, stop-loss, trailing stop, and time stops
- **Kill Switch**: Touch `./KILL_SWITCH` to halt all trading gracefully
- **DRY_RUN Mode**: Full simulation without on-chain transactions
- **Tax-Style Logging**: SQLite ledger + JSONL logs for every decision, order, fill, and position
- **CSV Export**: Generate fills/positions exports for tax reporting
- **Health Endpoint**: Optional FastAPI `/health` for monitoring

## 📋 Prerequisites

- **Python 3.11+**
- **Bittensor wallet** with a funded hotkey (coldkey not needed online)
- **Taostats API key** (from [taostats.io](https://taostats.io))
- **FlameWire RPC** access (optional API key; public endpoint available)
- Docker & Docker Compose (optional, for containerized deployment)

## 🚀 Quick Start

### 1. Clone & Configure

```bash
git clone https://github.com/Rudy1995T/SubnetTrader.git
cd SubnetTrader
cp .env.example .env
# Edit .env with your API keys and wallet config
```

### 2. Install Dependencies

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Optional: install Bittensor SDK for live trading
pip install bittensor
```

### 3. Run (Dry-Run Mode)

```bash
# Ensure DRY_RUN=true in .env (default)
python -m app.main
```

### 4. Run (Docker)

```bash
docker-compose up -d
docker-compose logs -f subnet-trader
```

## ⚙️ Configuration

All settings are configured via environment variables (or `.env` file). See `.env.example` for the complete list.

### Key Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `DRY_RUN` | `true` | Simulate trades without broadcasting |
| `SCAN_INTERVAL_MIN` | `15` | Minutes between scan cycles |
| `NUM_SLOTS` | `4` | Number of portfolio slots |
| `MAX_HOLDING_HOURS` | `72` | Hard time-stop for positions |
| `ENTER_THRESHOLD` | `0.55` | Minimum composite score to enter |
| `HIGH_CONVICTION_THRESHOLD` | `0.80` | Score for double-slot entry |
| `STOP_LOSS_PCT` | `8.0` | Stop-loss percentage |
| `TAKE_PROFIT_PCT` | `15.0` | Take-profit percentage |
| `TRAILING_STOP_PCT` | `5.0` | Trailing stop from peak |
| `DAILY_DRAWDOWN_LIMIT_PCT` | `10.0` | Daily drawdown halt threshold |
| `MAX_TRADES_PER_DAY` | `20` | Maximum trades per day |
| `ALLOW_DOUBLE_SLOT` | `false` | Allow 2 slots for high-conviction |

### Signal Weights

| Weight | Default | Signal |
|--------|---------|--------|
| `W_TREND` | `0.20` | EMA cross + slope momentum |
| `W_SUPPORT_RESISTANCE` | `0.15` | Pivot-based S/R proximity |
| `W_FIBONACCI` | `0.15` | Fib 0.5-0.618 retracement reaction |
| `W_VOLATILITY` | `0.20` | Bollinger squeeze → expansion |
| `W_MEAN_REVERSION` | `0.15` | Bollinger + RSI oversold/overbought |
| `W_VALUE_BAND` | `0.15` | Custom alpha price sweet-spot |

### Value Band Heuristic

The bot applies a Gaussian boost for subnets with alpha prices in the "sweet spot" range `[0.0035, 0.0050]` TAO. This is configurable:

- `VALUE_BAND_LOW` / `VALUE_BAND_HIGH`: The target price band
- `VALUE_BAND_DECAY`: Width of Gaussian decay outside the band

## 🏗️ Architecture

```
app/
├── main.py              # Entrypoint, scheduler, health server
├── config.py            # Pydantic-settings configuration
├── data/
│   └── taostats_client.py   # Taostats API with cache & rate-limiting
├── chain/
│   ├── flamewire_rpc.py     # FlameWire JSON-RPC + WebSocket client
│   └── executor.py          # Swap quoting & execution
├── strategy/
│   ├── signals.py           # 6 signal generators (0..1 each)
│   └── scoring.py           # Ensemble scoring & ranking
├── portfolio/
│   └── manager.py           # Slot management, entry/exit logic, risk
├── storage/
│   ├── db.py                # SQLite async CRUD + CSV export
│   └── models.sql           # Schema definition
├── logging/
│   └── logger.py            # Structured logging (console + JSONL)
└── utils/
    └── time.py              # UTC time utilities
```

## 📊 How It Works

### Each 15-Minute Cycle

1. **Fetch Data**: Pull subnet universe + alpha prices from Taostats API
2. **Compute Signals**: For each subnet, compute 6 independent signals
3. **Score & Rank**: Weighted ensemble → composite score → ranking
4. **Process Exits**: Check open positions for stop-loss, time-stop, trailing stop, take-profit
5. **Process Entries**: Fill available slots with top-ranked subnets above threshold
6. **Log Everything**: Snapshot, signals, decisions, orders, fills → SQLite + JSONL

### Entry Criteria

A subnet becomes eligible when its composite score ≥ `ENTER_THRESHOLD` (default 0.55). With `ALLOW_DOUBLE_SLOT=true`, scores ≥ `HIGH_CONVICTION_THRESHOLD` (0.80) can use 2 slots.

### Exit Criteria (checked in priority order)

1. **Stop-Loss**: Price drops ≥ `STOP_LOSS_PCT`% from entry
2. **Time Stop**: Position held ≥ `MAX_HOLDING_HOURS` (72h)
3. **Trailing Stop**: Price drops ≥ `TRAILING_STOP_PCT`% from peak (only when in profit)
4. **Take-Profit**: Price rises ≥ `TAKE_PROFIT_PCT`% from entry

## 🛡️ Safety Features

- **Kill Switch**: Create `./KILL_SWITCH` file → bot halts gracefully
- **Daily Drawdown Limit**: Trading stops if NAV drops by configured %
- **Max Trades/Day**: Hard cap on daily transaction count
- **Per-Subnet Cooldown**: 6-12h lockout after exiting a position
- **Slippage Guard**: Rejects swaps exceeding `MAX_SLIPPAGE_PCT`
- **DRY_RUN Mode**: Full simulation with no on-chain impact

## 📦 CLI Commands

```bash
# Normal operation
python -m app.main

# Export fills/positions to CSV
python -m app.main export

# RPC health check
python -m app.main health
```

## 🧪 Testing

```bash
# Run all tests
pytest tests/ -v

# Run specific test file
pytest tests/test_scoring.py -v
pytest tests/test_portfolio.py -v
```

### Test Coverage

- **Scoring normalization**: All signals return [0, 1]
- **Value band boost**: Gaussian decay, symmetry, edge cases
- **Portfolio slot sizing**: Allocation math, slot availability
- **Time-stop exits**: 72h hard stop, priority ordering
- **Stop-loss / trailing stop**: Trigger conditions
- **Kill switch**: File detection

## 📈 Observability

### SQLite Ledger Tables

| Table | Purpose |
|-------|---------|
| `subnets_snapshot` | Subnet state at each scan |
| `signals` | Computed signal values per subnet |
| `decisions` | Bot decisions (ENTER, EXIT, HOLD, SKIP) |
| `orders` | Submitted swap orders |
| `fills` | Confirmed execution results |
| `positions` | Open and closed position history |
| `daily_nav` | Daily NAV tracking |
| `cooldowns` | Per-subnet exit cooldowns |

### JSONL Logs

Date-stamped structured logs in `data/logs/YYYY-MM-DD.jsonl`:
```json
{"ts": "2025-01-15T12:30:00+00:00", "level": "INFO", "logger": "subnet_trader", "message": "Cycle complete", "data": {...}}
```

### CSV Export

```bash
python -m app.main export
# → data/exports/fills.csv
# → data/exports/positions.csv
```

## 🐳 Docker

```bash
# Build and run
docker-compose up -d

# View logs
docker-compose logs -f

# Stop
docker-compose down

# Emergency stop
touch KILL_SWITCH
```

## ⚠️ Disclaimer

This is a learning project. Trading cryptocurrency involves significant risk. Use at your own discretion. Always start with `DRY_RUN=true` and small amounts.

## 📄 License

MIT
