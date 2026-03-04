"""
SQLite storage layer – schema management, CRUD, CSV export.
Uses aiosqlite for async access.
"""
from __future__ import annotations

import csv
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import aiosqlite

from app.config import settings
from app.logging.logger import logger
from app.utils.time import utc_iso

_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "models.sql")


class Database:
    """Async SQLite wrapper for the trading ledger."""

    def __init__(self, db_path: str | None = None) -> None:
        self._path = db_path or settings.DB_PATH
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._apply_schema()
        logger.info(f"Database connected: {self._path}")

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def _apply_schema(self) -> None:
        with open(_SCHEMA_PATH) as f:
            schema_sql = f.read()
        await self._conn.executescript(schema_sql)
        await self._conn.commit()

    # ── Generic helpers ────────────────────────────────────────────

    async def execute(self, sql: str, params: tuple = ()) -> aiosqlite.Cursor:
        cursor = await self._conn.execute(sql, params)
        await self._conn.commit()
        return cursor

    async def fetchone(self, sql: str, params: tuple = ()) -> dict | None:
        cursor = await self._conn.execute(sql, params)
        row = await cursor.fetchone()
        if row is None:
            return None
        return dict(row)

    async def fetchall(self, sql: str, params: tuple = ()) -> list[dict]:
        cursor = await self._conn.execute(sql, params)
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    # ── Subnets snapshot ───────────────────────────────────────────

    async def insert_subnet_snapshot(
        self,
        scan_ts: str,
        netuid: int,
        name: str = "",
        alpha_price: float = 0.0,
        tao_reserve: float = 0.0,
        alpha_reserve: float = 0.0,
        emission_pct: float = 0.0,
        n_validators: int = 0,
        n_miners: int = 0,
        raw_json: dict | None = None,
    ) -> int:
        cursor = await self.execute(
            """INSERT INTO subnets_snapshot
               (scan_ts, netuid, name, alpha_price, tao_reserve, alpha_reserve,
                emission_pct, n_validators, n_miners, raw_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                scan_ts, netuid, name, alpha_price, tao_reserve, alpha_reserve,
                emission_pct, n_validators, n_miners,
                json.dumps(raw_json or {}, default=str),
            ),
        )
        return cursor.lastrowid

    # ── Signals ────────────────────────────────────────────────────

    async def insert_signal(
        self,
        scan_ts: str,
        netuid: int,
        trend: float,
        support_resist: float,
        fibonacci: float,
        volatility: float,
        mean_reversion: float,
        value_band: float,
        composite: float,
        rank: int,
        dereg: float = 0.0,
    ) -> int:
        cursor = await self.execute(
            """INSERT INTO signals
               (scan_ts, netuid, trend, support_resist, fibonacci,
                volatility, mean_reversion, value_band, dereg, composite, rank)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (scan_ts, netuid, trend, support_resist, fibonacci,
             volatility, mean_reversion, value_band, dereg, composite, rank),
        )
        return cursor.lastrowid

    # ── Decisions ──────────────────────────────────────────────────

    async def insert_decision(
        self,
        scan_ts: str,
        action: str,
        netuid: int,
        reason: str = "",
        score: float = 0.0,
        slot_id: int = -1,
        amount_tao: float = 0.0,
    ) -> int:
        cursor = await self.execute(
            """INSERT INTO decisions
               (scan_ts, action, netuid, reason, score, slot_id, amount_tao)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (scan_ts, action, netuid, reason, score, slot_id, amount_tao),
        )
        return cursor.lastrowid

    # ── Orders ─────────────────────────────────────────────────────

    async def insert_order(
        self,
        order_type: str,
        netuid: int,
        amount_tao: float,
        expected_out: float = 0.0,
        max_slippage: float = 0.0,
        dry_run: bool = True,
        status: str = "PENDING",
    ) -> int:
        cursor = await self.execute(
            """INSERT INTO orders
               (order_ts, order_type, netuid, amount_tao, expected_out,
                max_slippage, dry_run, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (utc_iso(), order_type, netuid, amount_tao, expected_out,
             max_slippage, 1 if dry_run else 0, status),
        )
        return cursor.lastrowid

    async def update_order_status(self, order_id: int, status: str) -> None:
        await self.execute(
            "UPDATE orders SET status = ? WHERE id = ?",
            (status, order_id),
        )

    # ── Fills ──────────────────────────────────────────────────────

    async def insert_fill(
        self,
        order_id: int,
        tx_hash: str,
        netuid: int,
        side: str,
        amount_in: float,
        amount_out: float,
        fee: float = 0.0,
        slippage_pct: float = 0.0,
        dry_run: bool = True,
    ) -> int:
        cursor = await self.execute(
            """INSERT INTO fills
               (fill_ts, order_id, tx_hash, netuid, side,
                amount_in, amount_out, fee, slippage_pct, dry_run)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (utc_iso(), order_id, tx_hash, netuid, side,
             amount_in, amount_out, fee, slippage_pct, 1 if dry_run else 0),
        )
        return cursor.lastrowid

    # ── Positions ──────────────────────────────────────────────────

    async def open_position(
        self,
        slot_id: int,
        netuid: int,
        entry_price: float,
        amount_tao_in: float,
        amount_alpha: float = 0.0,
        entry_score: float = 0.0,
    ) -> int:
        cursor = await self.execute(
            """INSERT INTO positions
               (slot_id, netuid, status, entry_ts, entry_price,
                amount_tao_in, amount_alpha, peak_price, entry_score)
               VALUES (?, ?, 'OPEN', ?, ?, ?, ?, ?, ?)""",
            (slot_id, netuid, utc_iso(), entry_price,
             amount_tao_in, amount_alpha, entry_price, entry_score),
        )
        return cursor.lastrowid

    async def close_position(
        self,
        position_id: int,
        exit_price: float,
        amount_tao_out: float,
        exit_reason: str,
    ) -> None:
        entry = await self.fetchone(
            "SELECT amount_tao_in, entry_price FROM positions WHERE id = ?",
            (position_id,),
        )
        # PnL based on price change — meaningful in both DRY_RUN and LIVE mode.
        # DRY_RUN swap amounts use a fixed fee estimate and don't reflect actual price moves.
        entry_price = entry["entry_price"] if entry else 0.0
        amount_tao_in = entry["amount_tao_in"] if entry else 0.0
        if entry_price > 0 and amount_tao_in > 0:
            pnl_pct = (exit_price - entry_price) / entry_price * 100
            pnl_tao = amount_tao_in * pnl_pct / 100
        else:
            pnl_pct = 0.0
            pnl_tao = 0.0

        await self.execute(
            """UPDATE positions
               SET status = 'CLOSED', exit_ts = ?, exit_price = ?,
                   amount_tao_out = ?, pnl_tao = ?, pnl_pct = ?,
                   exit_reason = ?
               WHERE id = ?""",
            (utc_iso(), exit_price, amount_tao_out, pnl_tao, pnl_pct,
             exit_reason, position_id),
        )

    async def update_peak_price(self, position_id: int, price: float) -> None:
        await self.execute(
            "UPDATE positions SET peak_price = MAX(peak_price, ?) WHERE id = ?",
            (price, position_id),
        )

    async def get_open_positions(self) -> list[dict]:
        return await self.fetchall(
            "SELECT * FROM positions WHERE status = 'OPEN' ORDER BY slot_id"
        )

    async def get_position(self, position_id: int) -> dict | None:
        return await self.fetchone(
            "SELECT * FROM positions WHERE id = ?", (position_id,)
        )

    # ── Daily NAV ──────────────────────────────────────────────────

    async def upsert_daily_nav(
        self,
        date_str: str,
        nav_tao: float,
        tao_cash: float,
        positions_value: float,
        drawdown_pct: float = 0.0,
        trades_today: int = 0,
    ) -> None:
        await self.execute(
            """INSERT INTO daily_nav (date, nav_tao, tao_cash, positions_value, drawdown_pct, trades_today)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(date) DO UPDATE SET
                 nav_tao = excluded.nav_tao,
                 tao_cash = excluded.tao_cash,
                 positions_value = excluded.positions_value,
                 drawdown_pct = excluded.drawdown_pct,
                 trades_today = excluded.trades_today""",
            (date_str, nav_tao, tao_cash, positions_value, drawdown_pct, trades_today),
        )

    async def get_daily_nav(self, date_str: str) -> dict | None:
        return await self.fetchone(
            "SELECT * FROM daily_nav WHERE date = ?", (date_str,)
        )

    # ── Cooldowns ──────────────────────────────────────────────────

    async def add_cooldown(self, netuid: int, cooldown_until: str) -> None:
        await self.execute(
            "INSERT INTO cooldowns (netuid, exit_ts, cooldown_until) VALUES (?, ?, ?)",
            (netuid, utc_iso(), cooldown_until),
        )

    async def get_active_cooldowns(self, now_iso: str) -> set[int]:
        rows = await self.fetchall(
            "SELECT DISTINCT netuid FROM cooldowns WHERE cooldown_until > ?",
            (now_iso,),
        )
        return {r["netuid"] for r in rows}

    async def cleanup_cooldowns(self, now_iso: str) -> None:
        await self.execute(
            "DELETE FROM cooldowns WHERE cooldown_until <= ?", (now_iso,)
        )

    # ── Trade counting ─────────────────────────────────────────────

    async def count_trades_today(self, date_str: str) -> int:
        row = await self.fetchone(
            "SELECT COUNT(*) as cnt FROM fills WHERE fill_ts LIKE ?",
            (f"{date_str}%",),
        )
        return row["cnt"] if row else 0

    # ── CSV Export ─────────────────────────────────────────────────

    async def export_fills_csv(self, output_path: str) -> str:
        rows = await self.fetchall("SELECT * FROM fills ORDER BY fill_ts")
        return self._write_csv(output_path, rows)

    async def export_positions_csv(self, output_path: str) -> str:
        rows = await self.fetchall("SELECT * FROM positions ORDER BY entry_ts")
        return self._write_csv(output_path, rows)

    def _write_csv(self, path: str, rows: list[dict]) -> str:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        if not rows:
            with open(path, "w") as f:
                f.write("")
            return path

        fieldnames = list(rows[0].keys())
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

        logger.info(f"Exported {len(rows)} rows to {path}")
        return path
