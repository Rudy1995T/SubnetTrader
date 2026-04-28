"""SQLite storage layer for EMA positions."""
from __future__ import annotations

import csv
import os
from pathlib import Path

import aiosqlite

from app.config import settings
from app.logging.logger import logger
from app.utils.time import utc_iso

_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "models.sql")


class Database:
    """Async SQLite wrapper for the EMA trading ledger."""

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
        with open(_SCHEMA_PATH) as handle:
            schema_sql = handle.read()
        await self._conn.executescript(schema_sql)

        for statement in (
            "ALTER TABLE ema_positions ADD COLUMN staked_hotkey TEXT DEFAULT ''",
            "ALTER TABLE ema_positions ADD COLUMN entry_spot_price REAL DEFAULT NULL",
            "ALTER TABLE ema_positions ADD COLUMN entry_slippage_pct REAL DEFAULT NULL",
            "ALTER TABLE ema_positions ADD COLUMN exit_slippage_pct REAL DEFAULT NULL",
            "ALTER TABLE ema_positions ADD COLUMN strategy TEXT DEFAULT 'scalper'",
            "ALTER TABLE ema_positions ADD COLUMN exit_verified BOOLEAN DEFAULT NULL",
            "ALTER TABLE ema_positions ADD COLUMN exit_verified_at TEXT DEFAULT NULL",
            "ALTER TABLE ema_positions ADD COLUMN tao_recovered REAL DEFAULT 0",
            "ALTER TABLE ema_positions ADD COLUMN emission_alpha REAL DEFAULT 0",
            "ALTER TABLE ema_positions ADD COLUMN emission_tao REAL DEFAULT 0",
            "ALTER TABLE ema_positions ADD COLUMN current_alpha REAL DEFAULT NULL",
            "ALTER TABLE ema_positions ADD COLUMN emission_updated_at TEXT DEFAULT NULL",
            "ALTER TABLE ema_positions ADD COLUMN scaled_out INTEGER DEFAULT 0",
            "ALTER TABLE ema_positions ADD COLUMN scaled_out_ts TEXT DEFAULT NULL",
            "ALTER TABLE ema_positions ADD COLUMN partial_pnl_tao REAL DEFAULT 0.0",
        ):
            try:
                await self._conn.execute(statement)
                await self._conn.commit()
            except Exception:
                pass

        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ema_strategy ON ema_positions(strategy)"
        )
        await self._conn.commit()

    async def execute(self, sql: str, params: tuple = ()) -> aiosqlite.Cursor:
        cursor = await self._conn.execute(sql, params)
        await self._conn.commit()
        return cursor

    async def fetchone(self, sql: str, params: tuple = ()) -> dict | None:
        cursor = await self._conn.execute(sql, params)
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def fetchall(self, sql: str, params: tuple = ()) -> list[dict]:
        cursor = await self._conn.execute(sql, params)
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def open_ema_position(
        self,
        netuid: int,
        entry_price: float,
        amount_tao: float,
        amount_alpha: float,
        strategy: str,
        staked_hotkey: str = "",
        entry_spot_price: float | None = None,
        entry_slippage_pct: float | None = None,
    ) -> int:
        cursor = await self.execute(
            """
            INSERT INTO ema_positions (
                netuid,
                status,
                entry_ts,
                entry_price,
                entry_spot_price,
                entry_slippage_pct,
                amount_tao,
                amount_alpha,
                peak_price,
                staked_hotkey,
                strategy
            )
            VALUES (?, 'OPEN', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                netuid,
                utc_iso(),
                entry_price,
                entry_spot_price,
                entry_slippage_pct,
                amount_tao,
                amount_alpha,
                entry_price,
                staked_hotkey,
                strategy,
            ),
        )
        return cursor.lastrowid

    async def close_ema_position(
        self,
        position_id: int,
        exit_price: float,
        amount_tao_out: float,
        pnl_tao: float,
        pnl_pct: float,
        exit_reason: str,
        exit_slippage_pct: float | None = None,
    ) -> None:
        await self.execute(
            """
            UPDATE ema_positions
            SET status = 'CLOSED',
                exit_ts = ?,
                exit_price = ?,
                exit_slippage_pct = ?,
                amount_tao_out = ?,
                pnl_tao = ?,
                pnl_pct = ?,
                exit_reason = ?
            WHERE id = ?
            """,
            (
                utc_iso(),
                exit_price,
                exit_slippage_pct,
                amount_tao_out,
                pnl_tao,
                pnl_pct,
                exit_reason,
                position_id,
            ),
        )

    async def update_ema_peak_price(self, position_id: int, price: float) -> None:
        await self.execute(
            "UPDATE ema_positions SET peak_price = MAX(peak_price, ?) WHERE id = ?",
            (price, position_id),
        )

    async def get_open_ema_positions(self, strategy: str | None = None) -> list[dict]:
        if strategy:
            return await self.fetchall(
                "SELECT * FROM ema_positions WHERE status = 'OPEN' AND strategy = ? ORDER BY entry_ts",
                (strategy,),
            )
        return await self.fetchall(
            "SELECT * FROM ema_positions WHERE status = 'OPEN' ORDER BY entry_ts"
        )

    async def get_ema_positions(self, limit: int = 200, strategy: str | None = None) -> list[dict]:
        if strategy:
            return await self.fetchall(
                "SELECT * FROM ema_positions WHERE strategy = ? ORDER BY entry_ts DESC LIMIT ?",
                (strategy, limit),
            )
        return await self.fetchall(
            "SELECT * FROM ema_positions ORDER BY entry_ts DESC LIMIT ?",
            (limit,),
        )

    async def get_closed_ema_positions(self, limit: int = 10, strategy: str | None = None) -> list[dict]:
        if strategy:
            return await self.fetchall(
                "SELECT * FROM ema_positions WHERE status = 'CLOSED' AND strategy = ? ORDER BY exit_ts DESC LIMIT ?",
                (strategy, limit),
            )
        return await self.fetchall(
            "SELECT * FROM ema_positions WHERE status = 'CLOSED' ORDER BY exit_ts DESC LIMIT ?",
            (limit,),
        )

    async def set_cooldown(self, strategy: str, netuid: int, expires_at: str) -> None:
        await self.execute(
            "INSERT OR REPLACE INTO ema_cooldowns (strategy, netuid, expires_at) VALUES (?, ?, ?)",
            (strategy, netuid, expires_at),
        )

    async def get_cooldowns(self, strategy: str) -> dict:
        rows = await self.fetchall(
            "SELECT netuid, expires_at FROM ema_cooldowns WHERE strategy = ? AND expires_at > ?",
            (strategy, utc_iso()),
        )
        from app.utils.time import parse_iso as _parse_iso
        return {r["netuid"]: _parse_iso(r["expires_at"]) for r in rows}

    async def update_emission_snapshot(
        self,
        position_id: int,
        current_alpha: float,
        emission_alpha: float,
        emission_tao: float,
    ) -> None:
        await self.execute(
            """
            UPDATE ema_positions
            SET current_alpha = ?,
                emission_alpha = ?,
                emission_tao = ?,
                emission_updated_at = ?
            WHERE id = ?
            """,
            (current_alpha, emission_alpha, emission_tao, utc_iso(), position_id),
        )

    async def update_exit_emission(
        self,
        position_id: int,
        emission_alpha: float,
        emission_tao: float,
    ) -> None:
        await self.execute(
            """
            UPDATE ema_positions
            SET emission_alpha = ?,
                emission_tao = ?
            WHERE id = ?
            """,
            (emission_alpha, emission_tao, position_id),
        )

    async def update_exit_verified(self, position_id: int, verified: bool) -> None:
        await self.execute(
            "UPDATE ema_positions SET exit_verified = ?, exit_verified_at = ? WHERE id = ?",
            (verified, utc_iso(), position_id),
        )

    async def update_exit_tao_recovered(self, position_id: int, tao_recovered: float) -> None:
        await self.execute(
            """
            UPDATE ema_positions
            SET tao_recovered = COALESCE(tao_recovered, 0) + ?,
                amount_tao_out = COALESCE(amount_tao_out, 0) + ?
            WHERE id = ?
            """,
            (tao_recovered, tao_recovered, position_id),
        )

    async def update_partial_exit(
        self,
        position_id: int,
        new_amount_alpha: float,
        partial_pnl_tao: float,
        scaled_out_ts: str,
    ) -> None:
        await self.execute(
            """
            UPDATE ema_positions
            SET scaled_out = 1,
                scaled_out_ts = ?,
                partial_pnl_tao = ?,
                amount_alpha = ?
            WHERE id = ?
            """,
            (scaled_out_ts, partial_pnl_tao, new_amount_alpha, position_id),
        )

    async def update_position_status(self, position_id: int, status: str) -> None:
        await self.execute(
            "UPDATE ema_positions SET status = ? WHERE id = ?",
            (status, position_id),
        )

    async def get_unverified_exits(self) -> list[dict]:
        return await self.fetchall(
            "SELECT * FROM ema_positions WHERE exit_verified IS NULL AND status = 'CLOSED'"
        )

    async def clear_ema_history(self) -> None:
        await self.execute("DELETE FROM ema_positions")

    # ── Pool snapshot helpers (Pool Flow Momentum) ──────────────

    async def save_pool_snapshot(
        self,
        netuid: int,
        ts: str,
        tao_in_pool: float,
        alpha_in_pool: float,
        price: float,
        block_number: int | None = None,
        alpha_emission_rate: float | None = None,
    ) -> None:
        await self.execute(
            """
            INSERT INTO pool_snapshots (
                netuid, ts, block_number,
                tao_in_pool, alpha_in_pool, price, alpha_emission_rate
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                netuid,
                ts,
                block_number,
                tao_in_pool,
                alpha_in_pool,
                price,
                alpha_emission_rate,
            ),
        )

    async def save_pool_snapshots_bulk(self, rows: list[dict]) -> None:
        """Insert many snapshots in one transaction. Each row must include the
        same keys as save_pool_snapshot's parameters (block_number + emission
        rate may be None)."""
        if not rows:
            return
        payload = [
            (
                r["netuid"],
                r["ts"],
                r.get("block_number"),
                r["tao_in_pool"],
                r["alpha_in_pool"],
                r["price"],
                r.get("alpha_emission_rate"),
            )
            for r in rows
        ]
        await self._conn.executemany(
            """
            INSERT INTO pool_snapshots (
                netuid, ts, block_number,
                tao_in_pool, alpha_in_pool, price, alpha_emission_rate
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            payload,
        )
        await self._conn.commit()

    async def get_pool_snapshots(
        self,
        netuid: int,
        since_ts: str | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        if since_ts is not None:
            sql = (
                "SELECT * FROM pool_snapshots "
                "WHERE netuid = ? AND ts >= ? ORDER BY ts ASC"
            )
            params: tuple = (netuid, since_ts)
        else:
            sql = "SELECT * FROM pool_snapshots WHERE netuid = ? ORDER BY ts ASC"
            params = (netuid,)
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        return await self.fetchall(sql, params)

    async def snapshot_count(self, netuid: int) -> int:
        row = await self.fetchone(
            "SELECT COUNT(*) AS n FROM pool_snapshots WHERE netuid = ?",
            (netuid,),
        )
        return int(row["n"]) if row else 0

    async def prune_pool_snapshots(self, older_than_ts: str) -> int:
        """Delete snapshots with ts < older_than_ts. Returns number of rows removed."""
        cursor = await self._conn.execute(
            "DELETE FROM pool_snapshots WHERE ts < ?",
            (older_than_ts,),
        )
        await self._conn.commit()
        return cursor.rowcount or 0

    async def export_ema_positions_csv(self, output_path: str) -> str:
        rows = await self.fetchall("SELECT * FROM ema_positions ORDER BY entry_ts")
        return self._write_csv(output_path, rows)

    def _write_csv(self, path: str, rows: list[dict]) -> str:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        if not rows:
            with open(path, "w") as handle:
                handle.write("")
            return path

        with open(path, "w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)

        logger.info(f"Exported {len(rows)} rows to {path}")
        return path
