"""
Fast (scalp) trading manager.

Runs on a separate 30-minute scheduler, uses a dedicated budget, and applies
much tighter risk parameters so positions are typically held 30 min – 4 h.

Slots are stored in the same `positions` DB table but use slot_id values
starting at FAST_SLOT_OFFSET (default 10) to avoid clashing with the main
portfolio's slots 0–3.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional

from app.config import settings
from app.chain.executor import SwapExecutor
from app.data.taostats_client import TaostatsClient
from app.logging.logger import logger
from app.notifications.telegram import send_alert
from app.storage.db import Database
from app.strategy.scoring import rank_subnets, select_entries
from app.utils.time import utc_now, utc_iso, parse_iso


@dataclass
class FastSlot:
    slot_id: int
    position_id: int | None = None
    netuid: int | None = None
    status: str = "CASH"       # CASH | ALPHA
    amount_tao: float = 0.0
    entry_price: float = 0.0
    peak_price: float = 0.0
    entry_ts: str = ""


class FastPortfolioManager:
    """
    Lightweight scalp trading manager.
    Separate budget, separate slots, separate risk limits.
    Shares the same DB, executor, and taostats client as the main manager.
    """

    def __init__(
        self,
        db: Database,
        executor: SwapExecutor,
        taostats: TaostatsClient,
    ) -> None:
        self._db = db
        self._executor = executor
        self._taostats = taostats
        self._slots: list[FastSlot] = []

    # ── Init ───────────────────────────────────────────────────────

    async def initialize(self) -> None:
        offset = settings.FAST_SLOT_OFFSET
        self._slots = [
            FastSlot(slot_id=offset + i)
            for i in range(settings.FAST_TRADING_SLOTS)
        ]

        # Restore any open positions from DB
        rows = await self._db.fetchall(
            "SELECT * FROM positions WHERE status='OPEN' AND slot_id >= ?",
            (offset,),
        )
        for row in rows:
            sid = row["slot_id"]
            for slot in self._slots:
                if slot.slot_id == sid:
                    slot.position_id = row["id"]
                    slot.netuid = row["netuid"]
                    slot.status = "ALPHA"
                    slot.amount_tao = row["amount_tao_in"]
                    slot.entry_price = row["entry_price"]
                    slot.peak_price = row.get("peak_price", row["entry_price"])
                    slot.entry_ts = row["entry_ts"]

        logger.info(
            "FastPortfolioManager initialized",
            data={
                "slots": [
                    {"id": s.slot_id, "status": s.status, "netuid": s.netuid}
                    for s in self._slots
                ],
                "budget_tao": settings.FAST_TRADING_NAV_TAO,
                "scan_min": settings.FAST_TRADING_SCAN_MIN,
            },
        )

    # ── Main cycle ─────────────────────────────────────────────────

    async def run_cycle(self) -> dict:
        scan_ts = utc_iso()
        summary: dict = {
            "scan_ts": scan_ts,
            "mode": "FAST",
            "exits": [],
            "entries": [],
        }

        if os_kill_switch_active():
            summary["skipped"] = True
            return summary

        alpha_prices = await self._taostats.get_alpha_prices()
        if not alpha_prices:
            summary["skipped"] = True
            return summary

        # 1. Exits
        for slot in self._slots:
            if slot.status != "ALPHA" or slot.position_id is None:
                continue

            current_price = alpha_prices.get(slot.netuid or 0, 0.0)
            if current_price <= 0 or slot.entry_price <= 0:
                continue

            # Update peak
            if current_price > slot.peak_price:
                slot.peak_price = current_price
                await self._db.update_peak_price(slot.position_id, current_price)

            reason = self._check_exit(slot, current_price)
            if reason:
                result = await self._exit(scan_ts, slot, current_price, reason)
                if result:
                    summary["exits"].append(result)

        # 2. Entries
        free_slots = [s for s in self._slots if s.status == "CASH"]
        if not free_slots:
            logger.debug("Fast: all slots occupied, skipping entries")
            logger.info("Fast cycle complete", data=summary)
            return summary

        # Build short-window price data (last 15 bars ≈ 60h) for responsiveness
        subnet_data = []
        occupied = {s.netuid for s in self._slots if s.status == "ALPHA"}
        for netuid, price in alpha_prices.items():
            history = await self._taostats.get_price_history(netuid, limit=20)
            prices = _extract_prices(history, price)
            subnet_data.append({"netuid": netuid, "prices": prices, "alpha_price": price})

        ranked = rank_subnets(
            subnet_data,
            enter_threshold=settings.FAST_TRADING_ENTER_THRESHOLD,
            high_conviction_threshold=1.0,  # no double-slots in fast mode
        )
        to_enter = select_entries(
            ranked=ranked,
            available_slots=len(free_slots),
            current_positions=occupied,
            cooldown_netuids=set(),
        )

        for scored in to_enter:
            result = await self._enter(scan_ts, scored, alpha_prices, free_slots)
            if result:
                summary["entries"].append(result)
                # Remove used slot from free list
                free_slots = [s for s in self._slots if s.status == "CASH"]

        logger.info(f"Fast cycle complete", data=summary)
        return summary

    # ── Exit logic ─────────────────────────────────────────────────

    def _check_exit(self, slot: FastSlot, current_price: float) -> str | None:
        pnl_pct = (current_price - slot.entry_price) / slot.entry_price * 100.0

        if pnl_pct <= -settings.FAST_TRADING_STOP_LOSS_PCT:
            return "STOP_LOSS"

        entry_dt = parse_iso(slot.entry_ts)
        hours_held = (utc_now() - entry_dt).total_seconds() / 3600.0
        if hours_held >= settings.FAST_TRADING_MAX_HOLD_HOURS:
            return "TIME_STOP"

        if pnl_pct > 0 and slot.peak_price > slot.entry_price:
            drawdown = (slot.peak_price - current_price) / slot.peak_price * 100.0
            if drawdown >= settings.FAST_TRADING_TRAILING_STOP_PCT:
                return "TRAILING_STOP"

        if pnl_pct >= settings.FAST_TRADING_TAKE_PROFIT_PCT:
            return "TAKE_PROFIT"

        return None

    async def _exit(
        self, scan_ts: str, slot: FastSlot, current_price: float, reason: str
    ) -> dict | None:
        netuid = slot.netuid
        if netuid is None:
            return None

        pnl_ratio = current_price / slot.entry_price
        estimated_out = slot.amount_tao * pnl_ratio

        logger.info(
            f"FAST EXIT: netuid={netuid}, reason={reason}",
            data={
                "netuid": netuid,
                "entry_price": slot.entry_price,
                "current_price": current_price,
                "pnl_pct": (pnl_ratio - 1) * 100,
                "amount_tao": slot.amount_tao,
            },
        )

        swap = await self._executor.execute_swap(
            origin_netuid=netuid,
            destination_netuid=0,
            amount_tao=slot.amount_tao,
            max_slippage_pct=settings.MAX_SLIPPAGE_PCT,
        )

        if swap.success and slot.position_id is not None:
            await self._db.close_position(
                position_id=slot.position_id,
                exit_price=current_price,
                amount_tao_out=swap.received_tao,
                exit_reason=reason,
            )
            slot.status = "CASH"
            slot.position_id = None
            slot.netuid = None
            slot.amount_tao = 0.0
            slot.entry_price = 0.0
            slot.peak_price = 0.0
            slot.entry_ts = ""
            pnl_pct = (current_price / slot.entry_price - 1) * 100
            await send_alert(
                f"⚡ <b>FAST EXIT {reason}</b>: netuid {netuid} | "
                f"PnL {pnl_pct:+.2f}% | {swap.received_tao:.4f} τ out"
            )
            return {
                "netuid": netuid,
                "reason": reason,
                "tao_out": swap.received_tao,
                "tx_hash": swap.tx_hash,
            }
        return None

    # ── Entry logic ────────────────────────────────────────────────

    async def _enter(
        self,
        scan_ts: str,
        scored,
        alpha_prices: dict[int, float],
        free_slots: list[FastSlot],
    ) -> dict | None:
        if not free_slots:
            return None

        slot = free_slots[0]
        netuid = scored.netuid
        alpha_price = alpha_prices.get(netuid, 0.0)
        if alpha_price <= 0:
            return None

        amount_tao = settings.FAST_TRADING_NAV_TAO / settings.FAST_TRADING_SLOTS
        amount_tao = round(amount_tao, 6)

        logger.info(
            f"FAST ENTER: netuid={netuid}, score={scored.score:.4f}",
            data={
                "netuid": netuid,
                "alpha_price": alpha_price,
                "amount_tao": amount_tao,
                "score": scored.score,
            },
        )

        swap = await self._executor.execute_swap(
            origin_netuid=0,
            destination_netuid=netuid,
            amount_tao=amount_tao,
            max_slippage_pct=settings.MAX_SLIPPAGE_PCT,
        )

        if swap.success:
            order_id = await self._db.insert_order(
                order_type="BUY_ALPHA",
                netuid=netuid,
                amount_tao=amount_tao,
                expected_out=swap.received_tao,
                max_slippage=settings.MAX_SLIPPAGE_PCT,
                dry_run=settings.DRY_RUN,
                status="FILLED",
            )
            await self._db.insert_fill(
                order_id=order_id,
                tx_hash=swap.tx_hash,
                netuid=netuid,
                side="BUY",
                amount_in=amount_tao,
                amount_out=swap.received_tao,
                fee=swap.fee_tao,
                slippage_pct=swap.slippage_pct,
                dry_run=settings.DRY_RUN,
            )
            position_id = await self._db.open_position(
                slot_id=slot.slot_id,
                netuid=netuid,
                entry_price=alpha_price,
                amount_tao_in=amount_tao,
                amount_alpha=swap.received_tao,
                entry_score=scored.score,
            )
            slot.status = "ALPHA"
            slot.position_id = position_id
            slot.netuid = netuid
            slot.amount_tao = amount_tao
            slot.entry_price = alpha_price
            slot.peak_price = alpha_price
            slot.entry_ts = utc_iso()
            await send_alert(
                f"⚡ <b>FAST ENTRY</b>: netuid {netuid} | "
                f"score {scored.score:.3f} | {amount_tao:.4f} τ in"
            )
            return {
                "netuid": netuid,
                "score": scored.score,
                "amount_tao": amount_tao,
                "tx_hash": swap.tx_hash,
            }
        return None

    # ── Manual close ───────────────────────────────────────────────

    async def manual_close(self, position_id: int) -> dict | None:
        """Manually close a fast-trade position by its ID."""
        slot = next((s for s in self._slots if s.position_id == position_id), None)
        if slot is None:
            return None
        if slot.netuid is None or slot.entry_price <= 0:
            return None

        prices = await self._taostats.get_alpha_prices()
        current_price = prices.get(slot.netuid, 0.0)
        if current_price <= 0:
            current_price = slot.entry_price

        return await self._exit(utc_iso(), slot, current_price, "MANUAL")

    # ── Status ─────────────────────────────────────────────────────

    def status(self) -> dict:
        return {
            "enabled": settings.FAST_TRADING_ENABLED,
            "budget_tao": settings.FAST_TRADING_NAV_TAO,
            "scan_min": settings.FAST_TRADING_SCAN_MIN,
            "slots": [
                {
                    "id": s.slot_id,
                    "status": s.status,
                    "netuid": s.netuid,
                    "position_id": s.position_id,
                    "amount_tao": s.amount_tao,
                    "entry_price": s.entry_price,
                    "peak_price": s.peak_price,
                    "entry_ts": s.entry_ts,
                }
                for s in self._slots
            ],
            "params": {
                "stop_loss_pct": settings.FAST_TRADING_STOP_LOSS_PCT,
                "take_profit_pct": settings.FAST_TRADING_TAKE_PROFIT_PCT,
                "trailing_stop_pct": settings.FAST_TRADING_TRAILING_STOP_PCT,
                "max_hold_hours": settings.FAST_TRADING_MAX_HOLD_HOURS,
                "enter_threshold": settings.FAST_TRADING_ENTER_THRESHOLD,
            },
        }


# ── Helpers ────────────────────────────────────────────────────────

def os_kill_switch_active() -> bool:
    import os
    return os.path.exists(settings.KILL_SWITCH_PATH)


def _extract_prices(history: list[dict], current_price: float) -> list[float]:
    prices = []
    for entry in history:
        p = entry.get("price", entry.get("alpha_price", entry.get("close")))
        if p is not None:
            try:
                prices.append(float(p))
            except (ValueError, TypeError):
                continue
    if not prices:
        prices = [current_price]
    elif prices[-1] != current_price:
        prices.append(current_price)
    return prices
