"""EMA strategy manager for the live-trading runtime."""
from __future__ import annotations

import asyncio
import typing
from dataclasses import dataclass, field
from datetime import datetime, timezone

import math as _math
from datetime import timedelta

from app.config import StrategyConfig
from app.chain.executor import SwapExecutor
from app.data.taostats_client import TaostatsClient
from app.logging.logger import logger
from app.notifications.telegram import send_alert
from app.storage.db import Database
from app.strategy.ema_signals import (
    Candle,
    bars_above_below_ema,
    build_sampled_candles,
    bullish_ema_bounce,
    candle_close_prices,
    dual_ema_signal,
    ema_signal,
)
from app.utils.math import gini_coefficient, pearson_r
from app.utils.time import utc_now, utc_iso, parse_iso


@dataclass
class EmaPosition:
    position_id: int
    netuid: int
    entry_price: float
    amount_tao: float
    amount_alpha: float
    peak_price: float
    entry_ts: str
    staked_hotkey: str = ""  # validator hotkey used at entry (needed for unstake)


class EmaManager:
    """
    Manages the EMA portfolio, positions, entries, and exits.
    """

    def __init__(
        self,
        db: Database,
        executor: SwapExecutor,
        taostats: TaostatsClient,
        config: StrategyConfig,
    ) -> None:
        self._db = db
        self._executor = executor
        self._taostats = taostats
        self._cfg = config
        self._open: list[EmaPosition] = []
        self._realized_pnl: float = 0.0  # cumulative PnL from closed positions
        self._cooldowns: dict[int, datetime] = {}  # netuid → earliest re-entry time
        # Circuit breaker: track pot value over rolling 24h window
        self._pot_history: list[tuple[datetime, float]] = []
        self._breaker_tripped_at: datetime | None = None
        self._state_lock = asyncio.Lock()
        self._closing_positions: set[int] = set()
        self._exit_fail_count: int = 0  # consecutive swap failures across watcher cycles
        # Gini cache: netuid → (gini_value, timestamp)
        self._gini_cache: dict[int, tuple[float, float]] = {}
        # Companion exit callback: set by main.py to notify the other strategy
        self._companion_exit_cb: typing.Callable[..., typing.Awaitable[float]] | None = None

    # ── Init ───────────────────────────────────────────────────────

    async def initialize(self) -> None:
        rows = await self._db.get_open_ema_positions(strategy=self._cfg.tag)
        self._open = [
            EmaPosition(
                position_id=r["id"],
                netuid=r["netuid"],
                entry_price=r["entry_price"],
                amount_tao=r["amount_tao"],
                amount_alpha=r["amount_alpha"],
                peak_price=r.get("peak_price") or r["entry_price"],
                entry_ts=r["entry_ts"],
                staked_hotkey=r.get("staked_hotkey") or "",
            )
            for r in rows
        ]
        self._cooldowns = await self._db.get_cooldowns(self._cfg.tag)
        self._pot_history = []
        self._breaker_tripped_at = None
        self._closing_positions = set()
        # Load realized PnL from all closed positions so it survives restarts
        try:
            closed_rows = await self._db.get_ema_positions(limit=10000, strategy=self._cfg.tag)
            self._realized_pnl = sum(
                r.get("pnl_tao") or 0.0
                for r in closed_rows
                if r.get("status") == "CLOSED" and r.get("pnl_tao") is not None
            )
        except Exception:
            self._realized_pnl = 0.0
        logger.info(
            f"EmaManager[{self._cfg.tag}] initialized",
            data={
                "strategy": self._cfg.tag,
                "open_positions": len(self._open),
                "pot_tao": self._cfg.pot_tao,
                "fast_period": self._cfg.fast_period,
                "slow_period": self._cfg.slow_period,
                "confirm_bars": self._cfg.confirm_bars,
            },
        )

    # ── Main cycle ─────────────────────────────────────────────────

    async def run_cycle(self, globally_occupied: set[int] | None = None) -> dict:
        scan_ts = utc_iso()
        summary: dict = {"scan_ts": scan_ts, "mode": "EMA", "strategy": self._cfg.tag, "exits": [], "entries": []}

        alpha_prices = await self._taostats.get_alpha_prices()
        if not alpha_prices:
            summary["skipped"] = True
            return summary

        snapshot = self._taostats._pool_snapshot

        # 1. Exit pass — use on-chain price when available (more current than Taostats)
        for pos in await self._open_positions_snapshot():
            taostats_price = alpha_prices.get(pos.netuid, 0.0)
            if taostats_price <= 0:
                continue

            # Query live on-chain price from substrate pool reserves
            onchain_price = await self._executor.get_onchain_alpha_price(pos.netuid)

            # Use LOWER price for exit checks (catch dips even if Taostats lags)
            # Use HIGHER price for peak tracking (avoid false trailing stops)
            if onchain_price > 0:
                cur = min(taostats_price, onchain_price)
                peak_candidate = max(taostats_price, onchain_price)
                if abs(taostats_price - onchain_price) / taostats_price > 0.02:
                    logger.info(
                        f"EMA SN{pos.netuid} price divergence: taostats={taostats_price:.6f}, "
                        f"onchain={onchain_price:.6f}, exit_check={cur:.6f}"
                    )
            else:
                cur = taostats_price
                peak_candidate = taostats_price

            if peak_candidate > pos.peak_price:
                pos.peak_price = peak_candidate
                await self._db.update_ema_peak_price(pos.position_id, peak_candidate)

            prices = _get_completed_prices(
                pos.netuid,
                snapshot,
                cur,
                timeframe_hours=self._cfg.candle_timeframe_hours,
            )
            reason = self._check_exit(pos, cur, prices)
            if reason:
                if not await self._reserve_exit(pos.position_id):
                    continue
                try:
                    result = await self._exit(pos, cur, reason)
                    if result:
                        summary["exits"].append(result)
                finally:
                    await self._release_exit(pos.position_id)

        # 2. Circuit breaker — track pot and check for drawdown
        current_pot = self._cfg.pot_tao + self._realized_pnl
        now = utc_now()
        self._pot_history.append((now, current_pot))
        # Prune entries older than 24h
        cutoff = now - timedelta(hours=24)
        self._pot_history = [(t, v) for t, v in self._pot_history if t >= cutoff]
        summary["breaker_active"] = self.is_breaker_active

        # Check if breaker should trip
        if len(self._pot_history) >= 2:
            max_pot_24h = max(v for _, v in self._pot_history)
            if max_pot_24h > 0:
                dd_pct = (max_pot_24h - current_pot) / max_pot_24h * 100
                if dd_pct >= self._cfg.drawdown_breaker_pct and not self.is_breaker_active:
                    self._breaker_tripped_at = now
                    logger.warning(f"EMA[{self._cfg.tag}] CIRCUIT BREAKER TRIPPED: {dd_pct:.1f}% drawdown in 24h")
                    await send_alert(
                        f"🛑 <b>[{self._cfg.tag.upper()}] Circuit Breaker</b>: {dd_pct:.1f}% drawdown in 24h — "
                        f"entries paused for {self._cfg.drawdown_pause_hours}h"
                    )

        # 3. Entry pass
        open_positions = await self._open_positions_snapshot()
        slots_free = self._cfg.max_positions - len(open_positions)
        if slots_free <= 0:
            logger.debug(f"EMA[{self._cfg.tag}]: all slots occupied")
            logger.info("EMA cycle complete", data=summary)
            return summary
        if self.is_breaker_active:
            logger.info(f"EMA[{self._cfg.tag}]: skipping entries (circuit breaker)")
            summary["entries_skipped"] = "circuit breaker"
            logger.info(f"EMA[{self._cfg.tag}] cycle complete", data=summary)
            return summary

        occupied = {p.netuid for p in open_positions}
        if globally_occupied:
            occupied |= globally_occupied

        # Score all BUY candidates combining freshness and liquidity.
        # freshness = confirm_bars / bars_above (1.0 = just crossed, decays as bars grows)
        # score = freshness * log1p(tao_in_pool) — liquidity as tiebreaker
        scored: list[tuple[float, int, dict]] = []  # (score, netuid, snap_data)
        for netuid, snap_data in snapshot.items():
            if netuid == 0 or netuid in occupied:
                continue
            cooldown_until = self._cooldowns.get(netuid)
            if cooldown_until and utc_now() < cooldown_until:
                logger.debug(f"EMA: SN{netuid} on cooldown until {cooldown_until.isoformat()}")
                continue
            cur = alpha_prices.get(netuid, 0.0)
            if cur <= 0:
                continue
            if cur > self._cfg.max_entry_price_tao:
                continue
            candles = _get_completed_candles(
                netuid,
                snapshot,
                cur,
                timeframe_hours=self._cfg.candle_timeframe_hours,
            )
            prices = candle_close_prices(candles)
            if len(prices) < self._cfg.confirm_bars:
                continue
            # Dual EMA confirmation: both fast and slow must agree
            if dual_ema_signal(prices, self._cfg.fast_period, self._cfg.slow_period, self._cfg.confirm_bars) != "BUY":
                continue
            if self._cfg.bounce_enabled and not bullish_ema_bounce(
                candles,
                period=self._cfg.slow_period,
                touch_tolerance_pct=self._cfg.bounce_touch_tolerance_pct,
                require_green=self._cfg.bounce_require_green,
            ):
                logger.debug(f"EMA[{self._cfg.tag}]: SN{netuid} skipped — no bullish bounce off EMA")
                continue
            # Correlation guard: skip if highly correlated with an existing position
            if self._is_correlated_with_holdings(netuid, snapshot):
                logger.debug(f"EMA[{self._cfg.tag}]: SN{netuid} skipped — correlated with existing position")
                continue
            # Gini guard: skip whale-dominated subnets
            gini = await self._get_gini(netuid)
            if gini is not None and gini > self._cfg.max_gini:
                logger.debug(f"EMA[{self._cfg.tag}]: SN{netuid} skipped — Gini {gini:.4f} > {self._cfg.max_gini}")
                continue
            bars = bars_above_below_ema(prices, self._cfg.slow_period)
            bars = max(bars, self._cfg.confirm_bars)  # clamp floor
            freshness = self._cfg.confirm_bars / bars  # 1.0 at confirm_bars, decays
            tao_in_pool = float(snap_data.get("tao_in_pool", 0) or 0)
            score = freshness * _math.log1p(tao_in_pool)
            scored.append((score, netuid, snap_data))

        # Sort descending by score — freshest + most liquid first
        scored.sort(key=lambda x: x[0], reverse=True)

        for score, netuid, snap_data in scored:
            if slots_free <= 0:
                break
            cur = alpha_prices.get(netuid, 0.0)
            bars = bars_above_below_ema(
                _get_completed_prices(
                    netuid,
                    snapshot,
                    cur,
                    timeframe_hours=self._cfg.candle_timeframe_hours,
                ),
                self._cfg.slow_period,
            )
            logger.debug(
                f"EMA CANDIDATE SN{netuid}: score={score:.3f}, bars={bars}, "
                f"tao_in_pool={float(snap_data.get('tao_in_pool', 0) or 0):.0f}"
            )
            result = await self._enter(netuid, cur, snap_data)
            if result:
                summary["entries"].append(result)
                occupied.add(netuid)
                slots_free -= 1

        logger.info(f"EMA[{self._cfg.tag}] cycle complete", data=summary)

        # Send balance summary to Telegram after any trades
        if summary["exits"] or summary["entries"]:
            open_positions = await self._open_positions_snapshot()
            deployed = sum(p.amount_tao for p in open_positions)
            unstaked = max(0.0, self._cfg.pot_tao + self._realized_pnl - deployed)
            unrealized = 0.0
            for p in open_positions:
                cur_p = alpha_prices.get(p.netuid, p.entry_price)
                if p.entry_price:
                    unrealized += p.amount_tao * ((cur_p - p.entry_price) / p.entry_price)
            total_pnl = self._realized_pnl + unrealized
            arrow = "🟢" if total_pnl >= 0 else "🔴"
            await send_alert(
                f"💰 <b>[{self._cfg.tag.upper()}] Balance</b>\n"
                f"Deployed: {deployed:.4f} τ ({len(self._open)}/{self._cfg.max_positions} slots)\n"
                f"Unstaked: {unstaked:.4f} τ\n"
                f"Realized: {self._realized_pnl:+.4f} τ\n"
                f"Unrealized: {unrealized:+.4f} τ\n"
                f"{arrow} Total: {total_pnl:+.4f} τ"
            )

        return summary

    # ── Exit logic ─────────────────────────────────────────────────

    async def run_price_exit_watch(self, dual_held_netuids: set[int] | None = None) -> dict:
        """Watch open EMA positions with on-chain prices for fast risk exits.

        Args:
            dual_held_netuids: Netuids held by *both* strategies. If a position's
                netuid is in this set, skip the exit this cycle to avoid
                double-dumping liquidity on the same subnet.
        """
        summary: dict = {"scan_ts": utc_iso(), "mode": "EMA_EXIT_WATCH", "exits": []}

        # Back off if swaps keep failing (e.g. wallet can't pay fees)
        if self._exit_fail_count >= 3:
            summary["backoff"] = True
            self._exit_fail_count = max(0, self._exit_fail_count - 1)  # slowly recover
            return summary

        for pos in await self._open_positions_snapshot():
            onchain_price = await self._executor.get_onchain_alpha_price(pos.netuid)
            if onchain_price <= 0:
                continue

            if onchain_price > pos.peak_price:
                pos.peak_price = onchain_price
                await self._db.update_ema_peak_price(pos.position_id, onchain_price)

            reason = self._check_price_exit(pos, onchain_price)
            if not reason:
                continue
            # Stagger dual-held exits: skip this cycle, let the other strategy exit first
            if dual_held_netuids and pos.netuid in dual_held_netuids:
                logger.warning(
                    f"EMA[{self._cfg.tag}]: deferring exit SN{pos.netuid} — dual-held, "
                    f"staggering to avoid double-dump (reason={reason})"
                )
                summary.setdefault("deferred", []).append(
                    {"netuid": pos.netuid, "reason": reason}
                )
                continue
            if not await self._reserve_exit(pos.position_id):
                continue

            try:
                result = await self._exit(pos, onchain_price, reason)
                if result:
                    summary["exits"].append(result)
                    self._exit_fail_count = 0
                else:
                    self._exit_fail_count += 1
            finally:
                await self._release_exit(pos.position_id)

        return summary

    def _check_price_exit(self, pos: EmaPosition, cur: float) -> str | None:
        """Price-only exits used by the high-frequency watcher."""
        pnl_pct = (cur - pos.entry_price) / pos.entry_price * 100.0

        if pnl_pct <= -self._cfg.stop_loss_pct:
            return "STOP_LOSS"

        if pnl_pct >= self._cfg.take_profit_pct:
            return "TAKE_PROFIT"

        # Breakeven stop: once we've been up >= BREAKEVEN_TRIGGER_PCT,
        # never let the trade go negative (exit if PnL drops back to 0%)
        peak_pnl = (pos.peak_price - pos.entry_price) / pos.entry_price * 100.0
        if peak_pnl >= self._cfg.breakeven_trigger_pct and pnl_pct <= 0:
            return "BREAKEVEN_STOP"

        # Trailing stop: once in profit, exit if price drops TRAILING_STOP_PCT from peak
        if pnl_pct > 0 and pos.peak_price > pos.entry_price:
            drawdown = (pos.peak_price - cur) / pos.peak_price * 100.0
            if drawdown >= self._cfg.trailing_stop_pct:
                return "TRAILING_STOP"

        return None

    def _check_exit(self, pos: EmaPosition, cur: float, prices: list[float]) -> str | None:
        price_reason = self._check_price_exit(pos, cur)
        if price_reason in {"STOP_LOSS", "TAKE_PROFIT"}:
            return price_reason

        entry_dt = parse_iso(pos.entry_ts)
        hours_held = (utc_now() - entry_dt).total_seconds() / 3600.0
        if hours_held >= self._cfg.max_holding_hours:
            return "TIME_STOP"

        if price_reason == "TRAILING_STOP":
            return price_reason

        # EMA cross exit: consecutive closes below EMA
        sig = ema_signal(prices, self._cfg.slow_period, self._cfg.confirm_bars)
        if sig == "SELL":
            return "EMA_CROSS"

        return None

    async def _exit(self, pos: EmaPosition, cur: float, reason: str) -> dict | None:
        pnl_pct = (cur - pos.entry_price) / pos.entry_price * 100.0

        # Ghost detection: if on-chain alpha is effectively zero, the position
        # was already exited (e.g. companion strategy's unstake_all consumed it).
        if not self._cfg.dry_run and pos.staked_hotkey:
            onchain_alpha = await self._executor.get_onchain_stake(pos.staked_hotkey, pos.netuid)
            if onchain_alpha < 0.001:
                logger.warning(
                    f"EMA[{self._cfg.tag}] GHOST detected SN{pos.netuid}: "
                    f"on-chain alpha={onchain_alpha:.6f}, closing as full loss"
                )
                ghost_pnl = -pos.amount_tao
                ghost_pnl_pct = -100.0
                await self._db.close_ema_position(
                    position_id=pos.position_id,
                    exit_price=cur,
                    amount_tao_out=0.0,
                    pnl_tao=ghost_pnl,
                    pnl_pct=ghost_pnl_pct,
                    exit_reason="GHOST_CLOSE",
                )
                self._realized_pnl += ghost_pnl
                expires = utc_now() + timedelta(hours=self._cfg.cooldown_hours)
                async with self._state_lock:
                    self._open = [p for p in self._open if p.position_id != pos.position_id]
                    self._cooldowns[pos.netuid] = expires
                await self._db.set_cooldown(self._cfg.tag, pos.netuid, expires.isoformat())
                await send_alert(
                    f"👻 <b>[{self._cfg.tag.upper()}] GHOST CLOSE</b>: SN{pos.netuid} | "
                    f"No alpha on-chain — closed as {ghost_pnl:+.4f} τ loss"
                )
                return {
                    "netuid": pos.netuid,
                    "reason": "GHOST_CLOSE",
                    "pnl_pct": round(ghost_pnl_pct, 2),
                    "pnl_tao": round(ghost_pnl, 4),
                    "tao_out": 0.0,
                }

        logger.info(
            f"EMA[{self._cfg.tag}] EXIT: netuid={pos.netuid}, reason={reason}",
            data={
                "netuid": pos.netuid,
                "entry_price": pos.entry_price,
                "exit_price": cur,
                "pnl_pct": pnl_pct,
                "amount_tao": pos.amount_tao,
            },
        )

        try:
            swap = await self._executor.execute_swap(
                origin_netuid=pos.netuid,
                destination_netuid=0,
                amount_tao=pos.amount_tao,
                max_slippage_pct=self._cfg.max_slippage_pct,
                dry_run=self._cfg.dry_run,
                hotkey_ss58=pos.staked_hotkey or None,
            )
        except Exception as e:
            logger.error(f"EMA[{self._cfg.tag}] EXIT FAILED SN{pos.netuid}: {e}")
            return None

        if swap.success:
            total_received = swap.received_tao
            my_received = total_received  # default: all mine

            # Notify companion strategy: unstake_all consumed ALL alpha for
            # this coldkey+hotkey+netuid, so the other strategy's position is
            # now a ghost.  Split the received TAO proportionally and close both.
            companion_amount_tao: float = 0.0
            if self._companion_exit_cb is not None:
                try:
                    companion_amount_tao = await self._companion_exit_cb(
                        pos.netuid, total_received, cur,
                        exiter_amount_tao=pos.amount_tao,
                    ) or 0.0
                except Exception as cb_err:
                    logger.warning(f"Companion exit callback failed SN{pos.netuid}: {cb_err}")

            # If a companion position existed, adjust our share proportionally
            if companion_amount_tao > 0:
                total_deployed = pos.amount_tao + companion_amount_tao
                my_received = pos.amount_tao / total_deployed * total_received

            # PnL based on actual TAO returned vs TAO deployed
            actual_pnl_tao = my_received - pos.amount_tao

            # Use price-based PnL% (excludes emission yield) so it matches
            # what Taostats shows and what stop-loss/take-profit conditions check.
            actual_pnl_pct = (cur - pos.entry_price) / pos.entry_price * 100.0 if pos.entry_price > 0 else 0.0

            # Exit slippage: use on-chain alpha (includes emissions) for accurate calc
            exit_alpha = swap.received_alpha if swap.received_alpha > 0 else pos.amount_alpha
            expected_tao_out = exit_alpha * cur
            exit_slippage_pct = (
                (expected_tao_out - my_received) / expected_tao_out * 100
                if expected_tao_out > 0 else None
            )
            await self._db.close_ema_position(
                position_id=pos.position_id,
                exit_price=cur,
                amount_tao_out=my_received,
                pnl_tao=actual_pnl_tao,
                pnl_pct=actual_pnl_pct,
                exit_reason=reason,
                exit_slippage_pct=exit_slippage_pct,
            )
            self._realized_pnl += actual_pnl_tao
            expires = utc_now() + timedelta(hours=self._cfg.cooldown_hours)
            async with self._state_lock:
                self._open = [p for p in self._open if p.position_id != pos.position_id]
                self._cooldowns[pos.netuid] = expires
            await self._db.set_cooldown(self._cfg.tag, pos.netuid, expires.isoformat())
            # Emission yield: extra alpha accumulated beyond entry alpha
            emission_tao = (exit_alpha - pos.amount_alpha) * cur if exit_alpha > pos.amount_alpha else 0.0
            yield_note = f" (incl ~{emission_tao:.4f} τ emissions)" if emission_tao > 0.01 else ""
            await send_alert(
                f"📉 <b>[{self._cfg.tag.upper()}] EXIT {reason}</b>: SN{pos.netuid} | "
                f"Price {actual_pnl_pct:+.2f}% | {actual_pnl_tao:+.4f} τ{yield_note} | "
                f"{my_received:.4f} τ out"
            )
            return {
                "netuid": pos.netuid,
                "reason": reason,
                "pnl_pct": round(actual_pnl_pct, 2),
                "pnl_tao": round(actual_pnl_tao, 4),
                "tao_out": my_received,
            }
        return None

    # ── Entry logic ────────────────────────────────────────────────

    async def _enter(self, netuid: int, cur: float, snap_data: dict) -> dict | None:
        full_amount = round(self._cfg.pot_tao * self._cfg.position_size_pct, 6)

        # Adapt position size to pool depth to limit slippage.
        # For constant-product AMM: price_impact ≈ tao_in / tao_reserve.
        # Target max 2.5% impact (leaves headroom within 5% rate_tolerance).
        tao_in_pool = float(snap_data.get("total_tao", 0) or 0) / 1e9  # rao → τ
        max_impact = 0.025  # 2.5%
        if tao_in_pool > 0:
            safe_tao = tao_in_pool * max_impact
            if full_amount > safe_tao:
                amount_tao = round(max(safe_tao, full_amount * 0.25), 6)  # min 25% of full size
                logger.info(
                    f"EMA SIZE ADJUSTED SN{netuid}: {full_amount:.4f} → {amount_tao:.4f} τ "
                    f"(pool={tao_in_pool:.2f} τ, est impact={amount_tao/tao_in_pool*100:.1f}%)"
                )
            else:
                amount_tao = full_amount
        else:
            amount_tao = full_amount

        # Check actual coldkey balance before live entry
        # Always keep FEE_RESERVE_TAO in wallet for transaction fees (entries + exits)
        if not self._cfg.dry_run:
            try:
                ck_balance = await self._executor.get_tao_balance()
                available = ck_balance - self._cfg.fee_reserve_tao
                if available < amount_tao:
                    logger.warning(
                        f"EMA SKIP ENTRY SN{netuid}: insufficient balance "
                        f"{ck_balance:.4f} τ (reserve {self._cfg.fee_reserve_tao} τ) "
                        f"< {amount_tao:.4f} τ needed"
                    )
                    return None
            except Exception as e:
                logger.warning(f"EMA balance check failed: {e}")
                return None

        # Resolve which validator hotkey to stake against on this subnet
        validator_hk: str = ""
        if not self._cfg.dry_run:
            try:
                validator_hk = await self._executor.get_validator_hotkey(netuid)
                logger.info(f"EMA[{self._cfg.tag}] ENTER SN{netuid}: using validator hotkey {validator_hk}")
            except Exception as e:
                logger.error(f"EMA[{self._cfg.tag}] SKIP ENTRY SN{netuid}: cannot resolve validator hotkey — {e}")
                return None

        logger.info(
            f"EMA[{self._cfg.tag}] ENTER: netuid={netuid}",
            data={
                "netuid": netuid,
                "alpha_price": cur,
                "amount_tao": amount_tao,
                "validator_hk": validator_hk,
            },
        )

        try:
            swap = await self._executor.execute_swap(
                origin_netuid=0,
                destination_netuid=netuid,
                amount_tao=amount_tao,
                max_slippage_pct=self._cfg.max_slippage_pct,
                dry_run=self._cfg.dry_run,
                hotkey_ss58=validator_hk or None,
            )
        except Exception as e:
            logger.error(f"EMA[{self._cfg.tag}] ENTER FAILED SN{netuid}: {e}")
            return None

        if swap.success:
            # Use actual alpha from ExtrinsicResponse when available,
            # fall back to estimate from price quote.
            if swap.received_alpha > 0:
                amount_alpha = swap.received_alpha
            else:
                amount_alpha = swap.received_tao / cur if cur > 0 else 0.0

            # Store effective entry price (cost basis) = TAO spent / alpha received.
            # This accounts for slippage so stop-loss/take-profit use real PnL.
            effective_price = amount_tao / amount_alpha if amount_alpha > 0 else cur

            entry_slippage_pct = (effective_price - cur) / cur * 100 if cur > 0 else None
            position_id = await self._db.open_ema_position(
                netuid=netuid,
                entry_price=effective_price,
                amount_tao=amount_tao,
                amount_alpha=amount_alpha,
                staked_hotkey=validator_hk,
                entry_spot_price=cur,
                entry_slippage_pct=entry_slippage_pct,
                strategy=self._cfg.tag,
            )
            async with self._state_lock:
                self._open.append(
                    EmaPosition(
                        position_id=position_id,
                        netuid=netuid,
                        entry_price=effective_price,
                        amount_tao=amount_tao,
                        amount_alpha=amount_alpha,
                        peak_price=effective_price,
                        entry_ts=utc_iso(),
                        staked_hotkey=validator_hk,
                    )
                )
            name = snap_data.get("name", "") or f"SN{netuid}"
            await send_alert(
                f"📈 <b>[{self._cfg.tag.upper()}] ENTRY</b>: {name} (SN{netuid}) | "
                f"{amount_tao:.4f} τ | price {effective_price:.6f} "
                f"(spot {cur:.6f}, slip {entry_slippage_pct:+.1f}%)"
            )
            return {"netuid": netuid, "amount_tao": amount_tao, "price": effective_price}
        return None

    # ── Manual close ──────────────────────────────────────────────

    async def manual_close(self, position_id: int) -> dict:
        """Manually close an EMA position by ID. Returns result dict or raises."""
        pos = await self._find_open_position(position_id)
        if pos is None:
            raise ValueError("Position not found in open positions")
        alpha_prices = await self._taostats.get_alpha_prices()
        cur = alpha_prices.get(pos.netuid, pos.entry_price)
        # Retry reserving the exit lock — the exit watcher may be holding it
        for attempt in range(5):
            if await self._reserve_exit(pos.position_id):
                break
            await asyncio.sleep(0.5)
        else:
            raise RuntimeError("Position is currently being processed by the exit watcher, try again")
        try:
            result = await self._exit(pos, cur, "MANUAL_CLOSE")
            if result is None:
                raise RuntimeError("Swap failed — check wallet balance and chain connectivity")
            return result
        finally:
            await self._release_exit(pos.position_id)

    # ── Companion exit (cross-strategy ghost resolution) ────────

    async def on_companion_exit(
        self, netuid: int, total_received_tao: float, exit_price: float,
        exiter_amount_tao: float = 0.0,
    ) -> float:
        """Called by the other strategy after it exits a dual-held subnet.

        Closes this strategy's position for the same netuid using a
        proportional share of the total TAO received on-chain.

        Returns this position's amount_tao if a companion was found (so the
        caller can adjust its own PnL), or 0.0 if no companion position existed.
        """
        pos = None
        async with self._state_lock:
            pos = next((p for p in self._open if p.netuid == netuid), None)
        if pos is None:
            return 0.0

        # Proportional share: my_tao / (my_tao + exiter_tao) * total
        total_deployed = pos.amount_tao + exiter_amount_tao
        if total_deployed > 0:
            my_share = pos.amount_tao / total_deployed * total_received_tao
        else:
            my_share = 0.0
        pnl_tao = my_share - pos.amount_tao
        pnl_pct = pnl_tao / pos.amount_tao * 100 if pos.amount_tao > 0 else 0.0

        await self._db.close_ema_position(
            position_id=pos.position_id,
            exit_price=exit_price,
            amount_tao_out=my_share,
            pnl_tao=pnl_tao,
            pnl_pct=pnl_pct,
            exit_reason="COMPANION_EXIT",
        )
        self._realized_pnl += pnl_tao
        expires = utc_now() + timedelta(hours=self._cfg.cooldown_hours)
        async with self._state_lock:
            self._open = [p for p in self._open if p.position_id != pos.position_id]
            self._cooldowns[pos.netuid] = expires
        await self._db.set_cooldown(self._cfg.tag, pos.netuid, expires.isoformat())

        logger.warning(
            f"EMA[{self._cfg.tag}] COMPANION_EXIT SN{netuid}: "
            f"pnl={pnl_tao:+.4f} τ ({pnl_pct:+.1f}%)"
        )
        await send_alert(
            f"🤝 <b>[{self._cfg.tag.upper()}] COMPANION EXIT</b>: SN{netuid} | "
            f"{pnl_tao:+.4f} τ ({pnl_pct:+.1f}%) | "
            f"Closed because other strategy exited same subnet"
        )
        return pos.amount_tao

    # ── Circuit breaker ─────────────────────────────────────────

    @property
    def is_breaker_active(self) -> bool:
        if self._breaker_tripped_at is None:
            return False
        elapsed = (utc_now() - self._breaker_tripped_at).total_seconds() / 3600
        return elapsed < self._cfg.drawdown_pause_hours

    # ── Correlation guard ────────────────────────────────────────

    def _is_correlated_with_holdings(self, candidate_netuid: int, snapshot: dict) -> bool:
        """Return True if candidate is too correlated with any open position."""
        threshold = self._cfg.correlation_threshold
        cand_data = snapshot.get(candidate_netuid, {})
        cand_prices = [
            float(e["price"]) for e in cand_data.get("seven_day_prices", []) if e.get("price")
        ]
        if len(cand_prices) < 10:
            return False
        for pos in self._open:
            held_data = snapshot.get(pos.netuid, {})
            held_prices = [
                float(e["price"]) for e in held_data.get("seven_day_prices", []) if e.get("price")
            ]
            if len(held_prices) < 10:
                continue
            r = pearson_r(cand_prices, held_prices)
            if r > threshold:
                logger.debug(
                    f"EMA CORRELATION SKIP: SN{candidate_netuid} ↔ SN{pos.netuid} r={r:.3f} > {threshold}"
                )
                return True
        return False

    # ── Gini filter ────────────────────────────────────────────────

    async def _get_gini(self, netuid: int) -> float | None:
        """Return cached Gini coefficient for a subnet, fetching from chain if stale."""
        import time as _time

        now = _time.time()
        cached = self._gini_cache.get(netuid)
        if cached and (now - cached[1]) < self._cfg.gini_cache_ttl_sec:
            return cached[0]

        try:
            await self._executor._ensure_substrate()
            sub = self._executor._substrate
            if sub is None:
                return None
            loop = asyncio.get_running_loop()
            mg = await loop.run_in_executor(None, sub.metagraph, netuid)
            stakes = [float(s) for s in mg.S if float(s) > 0]
            gini = gini_coefficient(stakes)
            self._gini_cache[netuid] = (gini, now)
            return gini
        except Exception as e:
            logger.warning(f"EMA Gini lookup failed SN{netuid}: {e}")
            return None

    # ── Portfolio summary ──────────────────────────────────────────

    def get_portfolio_summary(self, alpha_prices: dict[int, float]) -> dict:
        deployed = sum(p.amount_tao for p in self._open)
        raw_pot = self._cfg.pot_tao + self._realized_pnl
        unstaked = max(0.0, raw_pot - deployed)
        open_positions = []
        for p in self._open:
            cur = alpha_prices.get(p.netuid, p.entry_price)
            pnl_pct = (cur - p.entry_price) / p.entry_price * 100.0 if p.entry_price else 0.0
            hours = (utc_now() - parse_iso(p.entry_ts)).total_seconds() / 3600.0
            open_positions.append({
                "position_id": p.position_id,
                "netuid": p.netuid,
                "entry_price": p.entry_price,
                "current_price": cur,
                "pnl_pct": round(pnl_pct, 4),
                "amount_tao": p.amount_tao,
                "amount_alpha": round(p.amount_alpha, 4),
                "peak_price": p.peak_price,
                "entry_ts": p.entry_ts,
                "hours_held": round(hours, 1),
            })
        actual_pot = deployed + unstaked  # always satisfies pot = deployed + unstaked
        return {
            "tag": self._cfg.tag,
            "fast_period": self._cfg.fast_period,
            "slow_period": self._cfg.slow_period,
            "pot_tao": round(actual_pot, 6),
            "deployed_tao": round(deployed, 6),
            "unstaked_tao": round(unstaked, 6),
            "open_count": len(self._open),
            "max_positions": self._cfg.max_positions,
            "open_positions": open_positions,
            "breaker_active": self.is_breaker_active,
        }

    async def _open_positions_snapshot(self) -> list[EmaPosition]:
        async with self._state_lock:
            return list(self._open)

    async def _find_open_position(self, position_id: int) -> EmaPosition | None:
        async with self._state_lock:
            return next((p for p in self._open if p.position_id == position_id), None)

    async def _reserve_exit(self, position_id: int) -> bool:
        async with self._state_lock:
            if position_id in self._closing_positions:
                return False
            if not any(p.position_id == position_id for p in self._open):
                return False
            self._closing_positions.add(position_id)
            return True

    async def _release_exit(self, position_id: int) -> None:
        async with self._state_lock:
            self._closing_positions.discard(position_id)


# ── Helpers ────────────────────────────────────────────────────────

def _get_prices(
    netuid: int,
    snapshot: dict,
    fallback_price: float,
) -> list[float]:
    """Extract raw close samples from snapshot seven_day_prices."""
    data = snapshot.get(netuid, {})
    seven_day = data.get("seven_day_prices", [])
    prices = [float(e["price"]) for e in seven_day if e.get("price")]
    if not prices:
        prices = [fallback_price]
    return prices


def _get_price_points(
    netuid: int,
    snapshot: dict,
    fallback_price: float,
) -> list[dict]:
    """Extract timestamped price samples from snapshot seven_day_prices."""
    data = snapshot.get(netuid, {})
    seven_day = data.get("seven_day_prices", [])
    points = [
        {"timestamp": e.get("timestamp"), "price": e.get("price")}
        for e in seven_day
        if isinstance(e, dict) and e.get("timestamp") and e.get("price") is not None
    ]
    if not points:
        points = [{"timestamp": utc_iso(), "price": fallback_price}]
    return points


def _get_completed_candles(
    netuid: int,
    snapshot: dict,
    fallback_price: float,
    timeframe_hours: int,
):
    """
    Build completed 4h candles from the mixed Taostats sample stream.

    The feed includes partial in-progress updates, so candle reconstruction is
    used for entry/EMA-cross logic and the unfinished last bucket is dropped.
    """
    points = _get_price_points(netuid, snapshot, fallback_price)
    candles = build_sampled_candles(points, timeframe_hours=timeframe_hours)
    if candles:
        return candles

    # Fallback keeps the strategy running even if timestamp parsing fails.
    prices = _get_prices(netuid, snapshot, fallback_price)
    synthetic = []
    for idx, price in enumerate(prices):
        ts = utc_now() - timedelta(hours=(len(prices) - idx) * timeframe_hours)
        synthetic.append(
            Candle(
                start_ts=ts.isoformat(),
                end_ts=(ts + timedelta(hours=timeframe_hours)).isoformat(),
                open=price,
                high=price,
                low=price,
                close=price,
                sample_count=1,
            )
        )
    return synthetic


def _get_completed_prices(
    netuid: int,
    snapshot: dict,
    fallback_price: float,
    timeframe_hours: int,
) -> list[float]:
    candles = _get_completed_candles(netuid, snapshot, fallback_price, timeframe_hours)
    return candle_close_prices(candles)
