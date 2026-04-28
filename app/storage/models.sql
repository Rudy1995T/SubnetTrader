-- EMA-only storage schema

CREATE TABLE IF NOT EXISTS ema_positions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    netuid              INTEGER NOT NULL,
    status              TEXT    NOT NULL DEFAULT 'OPEN',
    entry_ts            TEXT    NOT NULL,
    exit_ts             TEXT    DEFAULT NULL,
    entry_price         REAL    NOT NULL,
    entry_spot_price    REAL    DEFAULT NULL,
    entry_slippage_pct  REAL    DEFAULT NULL,
    exit_price          REAL    DEFAULT NULL,
    exit_slippage_pct   REAL    DEFAULT NULL,
    amount_tao          REAL    NOT NULL,
    amount_alpha        REAL    NOT NULL,
    amount_tao_out      REAL    DEFAULT NULL,
    pnl_tao             REAL    DEFAULT NULL,
    pnl_pct             REAL    DEFAULT NULL,
    exit_reason         TEXT    DEFAULT NULL,
    peak_price          REAL    DEFAULT 0.0,
    staked_hotkey       TEXT    DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_ema_status ON ema_positions(status);
CREATE INDEX IF NOT EXISTS idx_ema_netuid ON ema_positions(netuid);

CREATE TABLE IF NOT EXISTS ema_cooldowns (
    strategy   TEXT    NOT NULL,
    netuid     INTEGER NOT NULL,
    expires_at TEXT    NOT NULL,
    PRIMARY KEY (strategy, netuid)
);

-- Pool reserve snapshots for the Pool Flow Momentum strategy.
-- One row per subnet per scan cycle; pruned after retention window.
CREATE TABLE IF NOT EXISTS pool_snapshots (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    netuid              INTEGER NOT NULL,
    ts                  TEXT    NOT NULL,
    block_number        INTEGER,
    tao_in_pool         REAL    NOT NULL,
    alpha_in_pool       REAL    NOT NULL,
    price               REAL    NOT NULL,
    alpha_emission_rate REAL
);

CREATE INDEX IF NOT EXISTS idx_pool_snap_netuid_ts
    ON pool_snapshots(netuid, ts);
