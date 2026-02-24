-- ============================================================
-- Bittensor Subnet Alpha Trading Bot – SQLite Schema
-- ============================================================

-- Subnet snapshots: captured each scan cycle
CREATE TABLE IF NOT EXISTS subnets_snapshot (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_ts         TEXT    NOT NULL,              -- ISO-8601 UTC
    netuid          INTEGER NOT NULL,
    name            TEXT    DEFAULT '',
    alpha_price     REAL    DEFAULT 0.0,
    tao_reserve     REAL    DEFAULT 0.0,
    alpha_reserve   REAL    DEFAULT 0.0,
    emission_pct    REAL    DEFAULT 0.0,
    n_validators    INTEGER DEFAULT 0,
    n_miners        INTEGER DEFAULT 0,
    raw_json        TEXT    DEFAULT '{}'           -- full API blob
);
CREATE INDEX IF NOT EXISTS idx_snap_ts ON subnets_snapshot(scan_ts);
CREATE INDEX IF NOT EXISTS idx_snap_net ON subnets_snapshot(netuid);

-- Computed signals per subnet per scan
CREATE TABLE IF NOT EXISTS signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_ts         TEXT    NOT NULL,
    netuid          INTEGER NOT NULL,
    trend           REAL    DEFAULT 0.0,
    support_resist  REAL    DEFAULT 0.0,
    fibonacci       REAL    DEFAULT 0.0,
    volatility      REAL    DEFAULT 0.0,
    mean_reversion  REAL    DEFAULT 0.0,
    value_band      REAL    DEFAULT 0.0,
    composite       REAL    DEFAULT 0.0,
    rank            INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sig_ts ON signals(scan_ts);
CREATE INDEX IF NOT EXISTS idx_sig_net ON signals(netuid);

-- Trade decisions made by the bot
CREATE TABLE IF NOT EXISTS decisions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_ts         TEXT    NOT NULL,
    action          TEXT    NOT NULL,              -- ENTER, EXIT, ROTATE, HOLD, SKIP
    netuid          INTEGER NOT NULL,
    reason          TEXT    DEFAULT '',
    score           REAL    DEFAULT 0.0,
    slot_id         INTEGER DEFAULT -1,
    amount_tao      REAL    DEFAULT 0.0
);
CREATE INDEX IF NOT EXISTS idx_dec_ts ON decisions(scan_ts);

-- Orders submitted (or simulated in dry-run)
CREATE TABLE IF NOT EXISTS orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    order_ts        TEXT    NOT NULL,
    order_type      TEXT    NOT NULL,              -- BUY_ALPHA, SELL_ALPHA
    netuid          INTEGER NOT NULL,
    amount_tao      REAL    NOT NULL,
    expected_out    REAL    DEFAULT 0.0,
    max_slippage    REAL    DEFAULT 0.0,
    dry_run         INTEGER DEFAULT 1,             -- 1=dry, 0=live
    status          TEXT    DEFAULT 'PENDING'       -- PENDING, FILLED, FAILED
);

-- Fills: confirmed execution results
CREATE TABLE IF NOT EXISTS fills (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    fill_ts         TEXT    NOT NULL,
    order_id        INTEGER REFERENCES orders(id),
    tx_hash         TEXT    DEFAULT '',
    netuid          INTEGER NOT NULL,
    side            TEXT    NOT NULL,              -- BUY, SELL
    amount_in       REAL    NOT NULL,
    amount_out      REAL    NOT NULL,
    fee             REAL    DEFAULT 0.0,
    slippage_pct    REAL    DEFAULT 0.0,
    dry_run         INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_fill_ts ON fills(fill_ts);

-- Open and historical positions
CREATE TABLE IF NOT EXISTS positions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    slot_id         INTEGER NOT NULL,              -- 0..NUM_SLOTS-1
    netuid          INTEGER NOT NULL,
    status          TEXT    DEFAULT 'OPEN',         -- OPEN, CLOSED
    entry_ts        TEXT    NOT NULL,
    exit_ts         TEXT    DEFAULT NULL,
    entry_price     REAL    NOT NULL,              -- alpha price at entry
    exit_price      REAL    DEFAULT NULL,
    amount_tao_in   REAL    NOT NULL,
    amount_alpha    REAL    DEFAULT 0.0,
    amount_tao_out  REAL    DEFAULT NULL,
    pnl_tao         REAL    DEFAULT NULL,
    pnl_pct         REAL    DEFAULT NULL,
    exit_reason     TEXT    DEFAULT NULL,           -- TAKE_PROFIT, STOP_LOSS, TIME_STOP, ROTATION, MANUAL
    peak_price      REAL    DEFAULT 0.0,           -- for trailing stop
    entry_score     REAL    DEFAULT 0.0
);
CREATE INDEX IF NOT EXISTS idx_pos_status ON positions(status);
CREATE INDEX IF NOT EXISTS idx_pos_slot ON positions(slot_id);

-- Daily portfolio value tracking
CREATE TABLE IF NOT EXISTS daily_nav (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    date            TEXT    NOT NULL UNIQUE,        -- YYYY-MM-DD
    nav_tao         REAL    NOT NULL,              -- net asset value in TAO
    tao_cash        REAL    NOT NULL,
    positions_value REAL    NOT NULL,
    drawdown_pct    REAL    DEFAULT 0.0,
    trades_today    INTEGER DEFAULT 0
);

-- Cooldowns: per-subnet lockout after exit
CREATE TABLE IF NOT EXISTS cooldowns (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    netuid          INTEGER NOT NULL,
    exit_ts         TEXT    NOT NULL,
    cooldown_until  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cool_net ON cooldowns(netuid);
