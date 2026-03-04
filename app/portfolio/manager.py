"""
Portfolio manager – slot management, entry/exit logic, risk controls.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

from app.config import settings
from app.chain.executor import SwapExecutor, SwapResult
from app.data.taostats_client import TaostatsClient
from app.logging.logger import logger
from app.notifications.telegram import send_alert
from app.storage.db import Database
from app.strategy.scoring import ScoredSubnet, rank_subnets, select_entries
from app.utils.time import utc_now, utc_iso, hours_since, parse_iso, today_midnight_utc


@dataclass
class Slot:
    """Represents a portfolio slot."""
    slot_id: int
    position_id: int | None = None  # DB position row id
    netuid: int | None = None       # 0 or None = cash
    status: str = "CASH"            # CASH or ALPHA
    amount_tao: float = 0.0         # TAO allocated to this slot


@dataclass
class RiskState:
    """Tracks daily risk metrics."""
    start_of_day_nav: float = 0.0
    current_nav: float = 0.0
    trades_today: int = 0
    halted: bool = False
    halt_reason: str = ""


class PortfolioManager:
    """
    Manages the 4-slot portfolio:
      - Decides entries/exits each scan cycle
      - Enforces risk limits (drawdown, trade cap, cooldowns)
      - Executes swaps via SwapExecutor
      - Logs everything to DB
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
        self._slots: list[Slot] = []
        self._risk = RiskState()

    async def initialize(self) -> None:
        """Load existing open positions from DB into slots."""
        self._slots = [
            Slot(slot_id=i) for i in range(settings.NUM_SLOTS)
        ]

        open_positions = await self._db.get_open_positions()
        for pos in open_positions:
            sid = pos["slot_id"]
            if 0 <= sid < len(self._slots):
                self._slots[sid].position_id = pos["id"]
                self._slots[sid].netuid = pos["netuid"]
                self._slots[sid].status = "ALPHA"
                self._slots[sid].amount_tao = pos["amount_tao_in"]

        # Initialize daily risk state using corrected NAV estimate
        today = today_midnight_utc().strftime("%Y-%m-%d")
        nav_row = await self._db.get_daily_nav(today)
        if nav_row:
            self._risk.trades_today = nav_row["trades_today"]
        # Always re-estimate start_nav using the correct cash calculation
        # (avoids stale/incorrect values stored during previous runs)
        start_nav, _ = await self._estimate_nav()
        self._risk.start_of_day_nav = start_nav

        logger.info(
            "Portfolio initialized",
            data={
                "slots": [
                    {"id": s.slot_id, "status": s.status, "netuid": s.netuid}
                    for s in self._slots
                ],
                "start_nav": self._risk.start_of_day_nav,
            },
        )

    # ── Kill switch ────────────────────────────────────────────────

    def check_kill_switch(self) -> bool:
        """Check if KILL_SWITCH file exists."""
        if os.path.exists(settings.KILL_SWITCH_PATH):
            logger.critical("KILL SWITCH activated – halting all trading")
            self._risk.halted = True
            self._risk.halt_reason = "KILL_SWITCH file detected"
            return True
        return False

    # ── Risk checks ────────────────────────────────────────────────

    async def check_risk_limits(self) -> bool:
        """
        Returns True if trading is allowed, False if halted.
        Checks daily drawdown and trade count.
        """
        if self._risk.halted:
            return False

        if self.check_kill_switch():
            return False

        # Trade count
        today = today_midnight_utc().strftime("%Y-%m-%d")
        self._risk.trades_today = await self._db.count_trades_today(today)
        if self._risk.trades_today >= settings.MAX_TRADES_PER_DAY:
            logger.warning(
                f"Daily trade cap reached: {self._risk.trades_today}/{settings.MAX_TRADES_PER_DAY}"
            )
            return False

        # Drawdown check
        if self._risk.start_of_day_nav > 0:
            current_nav, _ = await self._estimate_nav()
            self._risk.current_nav = current_nav
            drawdown_pct = (
                (self._risk.start_of_day_nav - current_nav)
                / self._risk.start_of_day_nav
                * 100
            )
            if drawdown_pct >= settings.DAILY_DRAWDOWN_LIMIT_PCT:
                logger.critical(
                    f"Daily drawdown limit hit: {drawdown_pct:.2f}% >= {settings.DAILY_DRAWDOWN_LIMIT_PCT}%",
                    data={"start_nav": self._risk.start_of_day_nav, "current_nav": current_nav},
                )
                self._risk.halted = True
                self._risk.halt_reason = f"Drawdown {drawdown_pct:.2f}%"
                return False

        return True

    async def _estimate_nav(self) -> tuple[float, float]:
        """
        Estimate current NAV and cash in TAO.
        Returns (nav_tao, cash_tao).
        In DRY_RUN mode the on-chain balance never changes, so cash is
        DRY_RUN_STARTING_TAO minus the capital deployed in open positions.
        """
        positions_value = 0.0
        deployed_capital = 0.0

        prices = await self._taostats.get_alpha_prices()
        for slot in self._slots:
            if slot.status == "ALPHA" and slot.netuid is not None:
                alpha_price = prices.get(slot.netuid, 0.0)
                if slot.position_id is not None:
                    pos = await self._db.get_position(slot.position_id)
                    if pos and pos["entry_price"] > 0 and alpha_price > 0:
                        ratio = alpha_price / pos["entry_price"]
                        positions_value += pos["amount_tao_in"] * ratio
                        deployed_capital += pos["amount_tao_in"]
                    elif pos:
                        positions_value += pos["amount_tao_in"]
                        deployed_capital += pos["amount_tao_in"]

        if settings.DRY_RUN:
            tao_cash = max(0.0, settings.DRY_RUN_STARTING_TAO - deployed_capital)
        else:
            tao_cash = await self._executor.get_tao_balance()

        return tao_cash + positions_value, tao_cash

    # ── Available slots ────────────────────────────────────────────

    def available_slots(self) -> list[Slot]:
        """Return slots in CASH status."""
        return [s for s in self._slots if s.status == "CASH"]

    def occupied_netuids(self) -> set[int]:
        """Return set of netuids currently in portfolio."""
        return {s.netuid for s in self._slots if s.status == "ALPHA" and s.netuid is not None}

    def slot_allocation_tao(self, total_tao: float) -> float:
        """TAO per slot (25% of deployable)."""
        return total_tao / settings.NUM_SLOTS

    # ── Main cycle ─────────────────────────────────────────────────

    async def run_cycle(self) -> dict:
        """
        Execute one full scan-decide-trade cycle.
        Returns summary dict.
        """
        scan_ts = utc_iso()
        summary: dict = {
            "scan_ts": scan_ts,
            "exits": [],
            "entries": [],
            "skipped": False,
        }

        # Risk check
        can_trade = await self.check_risk_limits()
        if not can_trade:
            summary["skipped"] = True
            summary["reason"] = self._risk.halt_reason or "Risk limits"
            logger.warning("Cycle skipped due to risk limits", data=summary)
            return summary

        # 1. Fetch data
        alpha_prices = await self._taostats.get_alpha_prices()
        if not alpha_prices:
            logger.warning("No alpha prices available, skipping cycle")
            summary["skipped"] = True
            summary["reason"] = "No price data"
            return summary

        # 2. Build subnet data with price history
        subnet_data = []
        for netuid, price in alpha_prices.items():
            history = await self._taostats.get_price_history(netuid, limit=100)
            prices_list = self._extract_prices_from_history(history, price)

            # Store snapshot
            await self._db.insert_subnet_snapshot(
                scan_ts=scan_ts,
                netuid=netuid,
                alpha_price=price,
            )

            subnet_data.append({
                "netuid": netuid,
                "prices": prices_list,
                "alpha_price": price,
            })

        # 3. Score and rank
        ranked = rank_subnets(subnet_data)

        logger.debug(
            f"Ranked {len(ranked)} subnets · top 5: "
            + ", ".join(f"SN{s.netuid}({s.signals.composite:.3f})" for s in ranked[:5]),
        )

        # Store signals
        for scored in ranked:
            await self._db.insert_signal(
                scan_ts=scan_ts,
                netuid=scored.netuid,
                trend=scored.signals.trend,
                support_resist=scored.signals.support_resistance,
                fibonacci=scored.signals.fibonacci,
                volatility=scored.signals.volatility,
                mean_reversion=scored.signals.mean_reversion,
                value_band=scored.signals.value_band,
                dereg=scored.signals.dereg,
                composite=scored.signals.composite,
                rank=scored.rank,
            )

        # 4. Process exits first (frees slots)
        exits = await self._process_exits(scan_ts, alpha_prices, ranked)
        summary["exits"] = exits

        # 5. Process entries
        cooldowns = await self._db.get_active_cooldowns(utc_iso())
        free_slots = self.available_slots()

        # Compute pairwise correlations and skip entries correlated with open positions
        corr = self._compute_correlations(subnet_data)
        occupied = self.occupied_netuids()
        correlated: set[int] = set()
        for (a, b), r in corr.items():
            if abs(r) >= settings.CORRELATION_THRESHOLD:
                if a in occupied:
                    correlated.add(b)
                if b in occupied:
                    correlated.add(a)
        if correlated:
            logger.info(
                "Correlation filter active",
                data={"occupied": list(occupied), "correlated_skipped": list(correlated)},
            )

        entries_to_make = select_entries(
            ranked=ranked,
            available_slots=len(free_slots),
            current_positions=occupied,
            cooldown_netuids=cooldowns,
            correlated_netuids=correlated,
        )

        if free_slots and entries_to_make:
            logger.debug(
                f"Entry plan: {[f'SN{e.netuid}' for e in entries_to_make]} "
                f"into {len(free_slots)} free slot(s)",
            )
        elif free_slots and not entries_to_make:
            logger.debug(
                f"{len(free_slots)} free slot(s) but no qualifying entries "
                f"(occupied={list(occupied)}, correlated={list(correlated)}, cooldowns={list(cooldowns)})",
            )

        for entry in entries_to_make:
            result = await self._enter_position(scan_ts, entry, alpha_prices)
            if result:
                summary["entries"].append(result)

        # 6. Update daily NAV
        await self._update_daily_nav()

        logger.info("Cycle complete", data=summary)
        return summary

    def _compute_correlations(
        self, subnet_data: list[dict], min_len: int = 10
    ) -> dict[tuple[int, int], float]:
        """Compute Pearson correlation for each pair of subnets using their price series."""
        import math
        series: dict[int, list[float]] = {
            d["netuid"]: d["prices"]
            for d in subnet_data
            if len(d["prices"]) >= min_len
        }
        netuids = list(series.keys())
        corr: dict[tuple[int, int], float] = {}
        for i, a in enumerate(netuids):
            for b in netuids[i + 1:]:
                xs, ys = series[a], series[b]
                n = min(len(xs), len(ys))
                xs, ys = xs[-n:], ys[-n:]
                mx = sum(xs) / n
                my = sum(ys) / n
                num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
                da = math.sqrt(sum((x - mx) ** 2 for x in xs))
                db = math.sqrt(sum((y - my) ** 2 for y in ys))
                if da > 0 and db > 0:
                    corr[(a, b)] = round(num / (da * db), 3)
        return corr

    def _extract_prices_from_history(
        self, history: list[dict], current_price: float
    ) -> list[float]:
        """Extract a clean price series from history data."""
        prices = []
        for entry in history:
            p = entry.get("price", entry.get("alpha_price", entry.get("close", None)))
            if p is not None:
                try:
                    prices.append(float(p))
                except (ValueError, TypeError):
                    continue

        # Ensure we have at least the current price
        if not prices:
            prices = [current_price]
        elif prices[-1] != current_price:
            prices.append(current_price)

        return prices

    # ── Exit logic ─────────────────────────────────────────────────

    async def _process_exits(
        self,
        scan_ts: str,
        alpha_prices: dict[int, float],
        ranked: list[ScoredSubnet],
    ) -> list[dict]:
        """Check all open positions for exit conditions."""
        exits = []

        for slot in self._slots:
            if slot.status != "ALPHA" or slot.position_id is None:
                continue

            pos = await self._db.get_position(slot.position_id)
            if pos is None:
                continue

            netuid = pos["netuid"]
            current_price = alpha_prices.get(netuid, 0.0)
            entry_price = pos["entry_price"]

            if entry_price <= 0 or current_price <= 0:
                continue

            # Update peak price for trailing stop
            await self._db.update_peak_price(slot.position_id, current_price)
            pos = await self._db.get_position(slot.position_id)

            exit_reason = self._check_exit_conditions(pos, current_price)
            pnl_pct = (current_price / entry_price - 1) * 100

            if exit_reason:
                logger.debug(f"EXIT SN{netuid} · reason={exit_reason} · PnL {pnl_pct:+.2f}%")
                result = await self._exit_position(scan_ts, slot, pos, current_price, exit_reason)
                if result:
                    exits.append(result)
            else:
                peak_price = pos.get("peak_price", entry_price)
                drawdown_from_peak = ((peak_price - current_price) / peak_price * 100) if peak_price > 0 else 0
                logger.debug(
                    f"HOLD SN{netuid} · PnL {pnl_pct:+.2f}% · peak={peak_price:.7f} · dd_peak={drawdown_from_peak:.2f}%"
                )

        return exits

    def _check_exit_conditions(self, pos: dict, current_price: float) -> str | None:
        """
        Evaluate exit conditions. Returns reason string or None.
        Priority: stop-loss > time-stop > trailing-stop > take-profit.
        """
        entry_price = pos["entry_price"]
        peak_price = pos.get("peak_price", entry_price)
        entry_ts = parse_iso(pos["entry_ts"])
        pnl_pct = ((current_price - entry_price) / entry_price) * 100

        # 1. Stop-loss
        if pnl_pct <= -settings.STOP_LOSS_PCT:
            return "STOP_LOSS"

        # 2. Hard time stop (72h max)
        if hours_since(entry_ts) >= settings.MAX_HOLDING_HOURS:
            return "TIME_STOP"

        # 3. Trailing stop (only after we're in profit)
        if pnl_pct > 0 and peak_price > entry_price:
            drawdown_from_peak = ((peak_price - current_price) / peak_price) * 100
            if drawdown_from_peak >= settings.TRAILING_STOP_PCT:
                return "TRAILING_STOP"

        # 4. Take-profit
        if pnl_pct >= settings.TAKE_PROFIT_PCT:
            return "TAKE_PROFIT"

        return None

    async def _exit_position(
        self,
        scan_ts: str,
        slot: Slot,
        pos: dict,
        current_price: float,
        reason: str,
    ) -> dict | None:
        """Execute an exit swap (alpha → TAO)."""
        netuid = pos["netuid"]
        estimated_tao_out = pos["amount_tao_in"] * (current_price / pos["entry_price"])

        logger.info(
            f"Exiting position: netuid={netuid}, reason={reason}",
            data={"position": pos, "current_price": current_price},
        )

        # Log decision
        await self._db.insert_decision(
            scan_ts=scan_ts,
            action="EXIT",
            netuid=netuid,
            reason=reason,
            score=0.0,
            slot_id=slot.slot_id,
            amount_tao=estimated_tao_out,
        )

        # Execute swap: alpha -> TAO (destination_netuid=0)
        swap_result = await self._executor.execute_swap(
            origin_netuid=netuid,
            destination_netuid=0,
            amount_tao=pos["amount_tao_in"],  # approximate input value
            max_slippage_pct=settings.MAX_SLIPPAGE_PCT,
        )

        if swap_result.success:
            # Create order & fill records
            order_id = await self._db.insert_order(
                order_type="SELL_ALPHA",
                netuid=netuid,
                amount_tao=pos["amount_tao_in"],
                expected_out=estimated_tao_out,
                max_slippage=settings.MAX_SLIPPAGE_PCT,
                dry_run=settings.DRY_RUN,
                status="FILLED",
            )
            await self._db.insert_fill(
                order_id=order_id,
                tx_hash=swap_result.tx_hash,
                netuid=netuid,
                side="SELL",
                amount_in=pos["amount_tao_in"],
                amount_out=swap_result.received_tao,
                fee=swap_result.fee_tao,
                slippage_pct=swap_result.slippage_pct,
                dry_run=settings.DRY_RUN,
            )

            # Close position in DB
            await self._db.close_position(
                position_id=pos["id"],
                exit_price=current_price,
                amount_tao_out=swap_result.received_tao,
                exit_reason=reason,
            )

            # Add cooldown
            cooldown_until = (
                utc_now() + timedelta(hours=settings.COOLDOWN_HOURS)
            ).isoformat()
            await self._db.add_cooldown(netuid, cooldown_until)

            # Free the slot
            slot.status = "CASH"
            slot.position_id = None
            slot.netuid = None
            slot.amount_tao = 0.0

            pnl_pct = (current_price / pos["entry_price"] - 1) * 100
            await send_alert(
                f"📤 <b>EXIT {reason}</b>: netuid {netuid} | "
                f"PnL {pnl_pct:+.2f}% | {swap_result.received_tao:.4f} τ out"
            )
            return {
                "netuid": netuid,
                "reason": reason,
                "tao_out": swap_result.received_tao,
                "tx_hash": swap_result.tx_hash,
            }
        else:
            logger.error(
                f"Exit swap failed for netuid {netuid}: {swap_result.error}"
            )
            await self._db.insert_decision(
                scan_ts=scan_ts,
                action="EXIT_FAILED",
                netuid=netuid,
                reason=swap_result.error,
                slot_id=slot.slot_id,
            )
            return None

    # ── Entry logic ────────────────────────────────────────────────

    async def _enter_position(
        self,
        scan_ts: str,
        scored: ScoredSubnet,
        alpha_prices: dict[int, float],
    ) -> dict | None:
        """Execute an entry swap (TAO → alpha)."""
        free_slots = self.available_slots()
        if not free_slots:
            return None

        slot = free_slots[0]
        netuid = scored.netuid
        alpha_price = alpha_prices.get(netuid, 0.0)

        if alpha_price <= 0:
            return None

        # Calculate amount: 25% of current balance / remaining slots
        tao_balance = await self._executor.get_tao_balance()
        if tao_balance <= 0:
            logger.warning("No TAO balance available for entry")
            return None

        amount_tao = self.slot_allocation_tao(tao_balance)
        amount_tao = min(amount_tao, tao_balance * 0.95)  # keep 5% reserve

        if amount_tao <= 0:
            return None

        # Determine slot count (double if high conviction)
        slots_to_use = 1
        if (
            settings.ALLOW_DOUBLE_SLOT
            and scored.high_conviction
            and len(free_slots) >= 2
        ):
            slots_to_use = 2
            amount_tao *= 2

        logger.info(
            f"Entering position: netuid={netuid}, score={scored.score:.4f}",
            data={
                "netuid": netuid,
                "alpha_price": alpha_price,
                "amount_tao": amount_tao,
                "slots": slots_to_use,
                "high_conviction": scored.high_conviction,
            },
        )

        # Log decision
        await self._db.insert_decision(
            scan_ts=scan_ts,
            action="ENTER",
            netuid=netuid,
            reason=f"score={scored.score:.4f}",
            score=scored.score,
            slot_id=slot.slot_id,
            amount_tao=amount_tao,
        )

        # Execute swap: TAO -> alpha (origin_netuid=0)
        swap_result = await self._executor.execute_swap(
            origin_netuid=0,
            destination_netuid=netuid,
            amount_tao=amount_tao,
            max_slippage_pct=settings.MAX_SLIPPAGE_PCT,
        )

        if swap_result.success:
            # Create order & fill
            order_id = await self._db.insert_order(
                order_type="BUY_ALPHA",
                netuid=netuid,
                amount_tao=amount_tao,
                expected_out=swap_result.received_tao,
                max_slippage=settings.MAX_SLIPPAGE_PCT,
                dry_run=settings.DRY_RUN,
                status="FILLED",
            )
            await self._db.insert_fill(
                order_id=order_id,
                tx_hash=swap_result.tx_hash,
                netuid=netuid,
                side="BUY",
                amount_in=amount_tao,
                amount_out=swap_result.received_tao,
                fee=swap_result.fee_tao,
                slippage_pct=swap_result.slippage_pct,
                dry_run=settings.DRY_RUN,
            )

            # Open position in DB
            position_id = await self._db.open_position(
                slot_id=slot.slot_id,
                netuid=netuid,
                entry_price=alpha_price,
                amount_tao_in=amount_tao,
                amount_alpha=swap_result.received_tao,
                entry_score=scored.score,
            )

            # Update slot(s)
            slot.status = "ALPHA"
            slot.position_id = position_id
            slot.netuid = netuid
            slot.amount_tao = amount_tao

            # If double slot, mark second slot too
            if slots_to_use == 2 and len(free_slots) >= 2:
                slot2 = free_slots[1]
                slot2.status = "ALPHA"
                slot2.position_id = position_id
                slot2.netuid = netuid
                slot2.amount_tao = amount_tao / 2

            await send_alert(
                f"📥 <b>ENTRY</b>: netuid {netuid} | "
                f"score {scored.score:.3f} | {amount_tao:.4f} τ in"
            )
            return {
                "netuid": netuid,
                "score": scored.score,
                "amount_tao": amount_tao,
                "tx_hash": swap_result.tx_hash,
                "high_conviction": scored.high_conviction,
            }
        else:
            logger.error(
                f"Entry swap failed for netuid {netuid}: {swap_result.error}"
            )
            await self._db.insert_decision(
                scan_ts=scan_ts,
                action="ENTER_FAILED",
                netuid=netuid,
                reason=swap_result.error,
                score=scored.score,
                slot_id=slot.slot_id,
            )
            return None

    # ── Daily NAV update ───────────────────────────────────────────

    async def _update_daily_nav(self) -> None:
        """Update the daily NAV tracking record."""
        today = today_midnight_utc().strftime("%Y-%m-%d")
        nav, tao_cash = await self._estimate_nav()
        pos_value = nav - tao_cash
        trades = await self._db.count_trades_today(today)

        drawdown = 0.0
        if self._risk.start_of_day_nav > 0:
            drawdown = max(
                0.0,
                (self._risk.start_of_day_nav - nav) / self._risk.start_of_day_nav * 100,
            )

        await self._db.upsert_daily_nav(
            date_str=today,
            nav_tao=nav,
            tao_cash=tao_cash,
            positions_value=pos_value,
            drawdown_pct=drawdown,
            trades_today=trades,
        )

    # ── Manual close ───────────────────────────────────────────────

    async def manual_close(self, position_id: int) -> dict | None:
        """Manually close a position by its ID (e.g. user-initiated take-profit)."""
        slot = next((s for s in self._slots if s.position_id == position_id), None)
        if slot is None:
            return None

        pos = await self._db.get_position(position_id)
        if pos is None:
            return None

        prices = await self._taostats.get_alpha_prices()
        current_price = prices.get(pos["netuid"], 0.0)
        if current_price <= 0:
            current_price = pos["entry_price"]

        return await self._exit_position(utc_iso(), slot, pos, current_price, "MANUAL")

    # ── Status ─────────────────────────────────────────────────────

    def status(self) -> dict:
        """Return current portfolio status."""
        return {
            "slots": [
                {
                    "id": s.slot_id,
                    "status": s.status,
                    "netuid": s.netuid,
                    "position_id": s.position_id,
                    "amount_tao": s.amount_tao,
                }
                for s in self._slots
            ],
            "risk": {
                "start_nav": self._risk.start_of_day_nav,
                "current_nav": self._risk.current_nav,
                "trades_today": self._risk.trades_today,
                "halted": self._risk.halted,
                "halt_reason": self._risk.halt_reason,
            },
        }
