"""EMA strategy manager for the live-trading runtime."""
from __future__ import annotations

import asyncio
import typing
from dataclasses import dataclass, field
from datetime import datetime, timezone

import math as _math
from datetime import timedelta

from app.config import StrategyConfig, settings
from app.chain.executor import SwapExecutor
from app.data.taostats_client import TaostatsClient
from app.logging.logger import logger
from app.notifications.telegram import send_alert
from app.storage.db import Database
from app.strategy.ema_signals import (
    Candle,
    bars_above_below_ema,
    build_candles_from_history,
    build_sampled_candles,
    bullish_ema_bounce,
    candle_close_prices,
    compute_ema,
    compute_mtf_signal,
    dual_ema_signal,
    ema_signal,
)
from app.strategy.indicators import compute_atr, compute_bollinger_bands, compute_macd, compute_rsi
from app.utils.math import compute_price_changes, gini_coefficient, pearson_r, rolling_volatility
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
    current_alpha: float = 0.0  # latest on-chain alpha (includes emissions)
    emission_alpha: float = 0.0  # accumulated emission alpha (current - entry)
    emission_tao: float = 0.0  # emission alpha valued in TAO at last snapshot
    scaled_out: bool = False  # True after partial time-stop exit (stage 1)
    scaled_out_ts: str | None = None
    partial_pnl_tao: float = 0.0  # TAO realized from partial exit
    trailing_override_pct: float | None = None  # tightened trailing after partial exit


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
        # Gini hysteresis: subnets rejected for high Gini stay blocked
        # until Gini drops below (max_gini - 0.04) to prevent flip-flop.
        self._gini_blocked: set[int] = set()
        # Companion exit callback: set by main.py to notify the other strategy
        self._companion_exit_cb: typing.Callable[..., typing.Awaitable[float]] | None = None
        # Companion netuids callback: returns netuids held by companion strategy
        # (used by entry watcher for cross-exclusion)
        self._companion_netuids_cb: typing.Callable[[], typing.Awaitable[set[int]]] | None = None
        # Entry watcher: track last-known EMA crossover state per subnet
        self._last_crossover_state: dict[int, str] = {}
        # Serialize full cycles (scheduled + entry-watcher-triggered)
        self._cycle_lock = asyncio.Lock()
        # Per-subnet deep history for EMA warmup (populated on startup / entry)
        self._warm_history: dict[int, list[dict]] = {}
        # Post-exit verification: positions flagged as stuck after failed retries
        self._stuck_positions: dict[int, dict] = {}
        # Flow reversal tracking: netuid → list of (timestamp, flow_pct) readings
        self._flow_history: dict[int, list[tuple[datetime, float]]] = {}

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
                current_alpha=r.get("current_alpha") or 0.0,
                emission_alpha=r.get("emission_alpha") or 0.0,
                emission_tao=r.get("emission_tao") or 0.0,
                scaled_out=bool(r.get("scaled_out")),
                scaled_out_ts=r.get("scaled_out_ts"),
                partial_pnl_tao=r.get("partial_pnl_tao") or 0.0,
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
        # Warm up deep history for open positions on startup
        if self._open and settings.SUBNET_HISTORY_ENABLED and settings.SUBNET_HISTORY_ON_STARTUP:
            for pos in self._open:
                try:
                    history = await self._taostats.get_subnet_history(
                        netuid=pos.netuid,
                        interval=settings.SUBNET_HISTORY_INTERVAL,
                        limit=settings.SUBNET_HISTORY_LIMIT,
                    )
                    if history:
                        self._warm_history[pos.netuid] = history
                        logger.info(
                            f"EmaManager[{self._cfg.tag}] warmed up history",
                            data={"netuid": pos.netuid, "data_points": len(history)},
                        )
                except Exception as exc:
                    logger.warning(f"EmaManager[{self._cfg.tag}] startup warmup failed SN{pos.netuid}: {exc}")

        # Re-verify any exits that didn't complete verification before last shutdown
        await self._verify_unverified_exits()

        logger.info(
            f"EmaManager[{self._cfg.tag}] initialized",
            data={
                "strategy": self._cfg.tag,
                "open_positions": len(self._open),
                "pot_tao": self._cfg.pot_tao,
                "fast_period": self._cfg.fast_period,
                "slow_period": self._cfg.slow_period,
                "confirm_bars": self._cfg.confirm_bars,
                "warm_history_subnets": list(self._warm_history.keys()),
            },
        )

    # ── Main cycle ─────────────────────────────────────────────────

    async def run_cycle(
        self,
        globally_occupied: set[int] | None = None,
        target_netuids: list[int] | None = None,
    ) -> dict:
        """Acquire cycle lock then delegate to _do_cycle.

        target_netuids: if set, restricts the entry pass to only those subnets.
        The exit pass always runs for all open positions regardless.
        """
        async with self._cycle_lock:
            return await self._do_cycle(globally_occupied, target_netuids)

    async def _do_cycle(
        self,
        globally_occupied: set[int] | None = None,
        target_netuids: list[int] | None = None,
    ) -> dict:
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

            # Record flow delta for flow reversal tracking
            if self._cfg.flow_reversal_exit_enabled:
                flow_pct = self._compute_flow_delta(pos.netuid)
                if flow_pct is not None:
                    now = utc_now()
                    history = self._flow_history.setdefault(pos.netuid, [])
                    history.append((now, flow_pct))
                    # Keep only last 10 readings
                    self._flow_history[pos.netuid] = history[-10:]

            reason = self._check_exit(pos, cur, prices)
            if reason:
                if not await self._reserve_exit(pos.position_id):
                    continue
                try:
                    if reason == "PARTIAL_TIME_EXIT":
                        result = await self._partial_exit(pos, cur)
                    else:
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

        # Pre-fetch Gini for candidate subnets (warms cache before scoring loop)
        prefetch_candidates = [
            netuid
            for netuid, snap_data in snapshot.items()
            if netuid != 0
            and netuid not in occupied
            and alpha_prices.get(netuid, 0.0) > 0
            and alpha_prices.get(netuid, 0.0) <= self._cfg.max_entry_price_tao
            and float(snap_data.get("total_tao", 0) or 0) / 1e9 >= self._cfg.min_pool_depth_tao
            and not (self._cooldowns.get(netuid) and utc_now() < self._cooldowns[netuid])
        ]
        await self._prefetch_gini(prefetch_candidates)

        # Score all BUY candidates combining freshness and liquidity.
        # freshness = confirm_bars / bars_above (1.0 = just crossed, decays as bars grows)
        # score = freshness * log1p(tao_in_pool) — liquidity as tiebreaker
        scored: list[tuple[float, int, dict]] = []  # (score, netuid, snap_data)
        for netuid, snap_data in snapshot.items():
            if netuid == 0 or netuid in occupied:
                continue
            if target_netuids is not None and netuid not in target_netuids:
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
            # --- Momentum pre-filters (cheap, run before EMA/candle work) ---
            if self._cfg.momentum_filters_enabled:
                changes = compute_price_changes(snap_data.get("seven_day_prices", []), cur)
                day_chg = changes.get("day_change_pct")
                week_chg = changes.get("week_change_pct")

                # Accelerating sell-off: both day and week negative beyond threshold
                if (day_chg is not None and week_chg is not None
                        and day_chg < -self._cfg.reject_day_and_week_negative_pct
                        and week_chg < -self._cfg.reject_day_and_week_negative_pct):
                    logger.debug(
                        f"EMA[{self._cfg.tag}]: SN{netuid} rejected — "
                        f"accelerating sell-off day={day_chg:.1f}% week={week_chg:.1f}%"
                    )
                    continue

                # Structural decline: week change very negative
                if week_chg is not None and week_chg < -self._cfg.reject_structural_decline_pct:
                    logger.debug(
                        f"EMA[{self._cfg.tag}]: SN{netuid} rejected — "
                        f"structural decline week={week_chg:.1f}%"
                    )
                    continue
            tao_in_pool = float(snap_data.get("total_tao", 0) or 0) / 1e9
            if tao_in_pool < self._cfg.min_pool_depth_tao:
                logger.debug(
                    f"EMA[{self._cfg.tag}]: SN{netuid} skipped — "
                    f"pool depth {tao_in_pool:.0f} TAO < min {self._cfg.min_pool_depth_tao:.0f}"
                )
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
            # Multi-timeframe confirmation: lower TF must also be bullish
            if self._cfg.mtf_enabled:
                lower_candles = _get_completed_candles(
                    netuid,
                    snapshot,
                    cur,
                    timeframe_hours=self._cfg.mtf_lower_tf_hours,
                )
                mtf = compute_mtf_signal(
                    lower_candles,
                    fast_period=self._cfg.fast_period,
                    slow_period=self._cfg.slow_period,
                    confirm_bars=self._cfg.mtf_confirm_bars,
                )
                if not mtf["lower_tf_bullish"]:
                    logger.info(
                        f"EMA[{self._cfg.tag}]: SN{netuid} MTF filter — "
                        f"{self._cfg.mtf_lower_tf_hours}h not confirmed "
                        f"(bars_above={mtf['lower_tf_bars_above']}, need={self._cfg.mtf_confirm_bars})"
                    )
                    continue
            # Parabolic guard: reject if price is too extended above slow EMA
            slow_ema_values = compute_ema(prices, self._cfg.slow_period)
            if slow_ema_values and slow_ema_values[-1] > 0:
                extension = cur / slow_ema_values[-1]
                if extension > self._cfg.parabolic_guard_mult:
                    logger.info(
                        f"EMA[{self._cfg.tag}]: SN{netuid} parabolic guard — "
                        f"price/EMA={extension:.2f}x > {self._cfg.parabolic_guard_mult}x"
                    )
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
            # Gini guard with hysteresis: reject at max_gini, re-allow at max_gini-0.04
            gini = await self._get_gini(netuid)
            if gini is not None:
                reallow = self._cfg.max_gini - 0.04
                if gini >= self._cfg.max_gini:
                    self._gini_blocked.add(netuid)
                    logger.debug(
                        f"EMA[{self._cfg.tag}]: SN{netuid} skipped — "
                        f"Gini {gini:.4f} >= {self._cfg.max_gini}"
                    )
                    continue
                elif gini < reallow:
                    self._gini_blocked.discard(netuid)
                if netuid in self._gini_blocked:
                    logger.debug(
                        f"EMA[{self._cfg.tag}]: SN{netuid} skipped — "
                        f"Gini {gini:.4f} in hysteresis band (re-allow < {reallow:.2f})"
                    )
                    continue
            # --- RSI filter: reject overbought entries ---
            if self._cfg.rsi_filter_enabled:
                rsi = compute_rsi(prices, period=self._cfg.rsi_period)
                current_rsi = rsi[-1] if rsi else 50.0
                if current_rsi > self._cfg.rsi_overbought:
                    logger.info(
                        f"EMA[{self._cfg.tag}]: SN{netuid} RSI reject — "
                        f"RSI {current_rsi:.1f} > {self._cfg.rsi_overbought}"
                    )
                    continue
            # --- MACD momentum filter: reject declining momentum ---
            if self._cfg.macd_filter_enabled:
                _, _, histogram = compute_macd(
                    prices,
                    fast=self._cfg.macd_fast,
                    slow=self._cfg.macd_slow,
                    signal_period=self._cfg.macd_signal,
                )
                if len(histogram) >= 2 and histogram[-1] < histogram[-2]:
                    logger.info(
                        f"EMA[{self._cfg.tag}]: SN{netuid} MACD reject — "
                        f"histogram declining {histogram[-1]:.6f} < {histogram[-2]:.6f}"
                    )
                    continue
            # --- Bollinger Band filter: reject price near upper band ---
            if self._cfg.bb_filter_enabled:
                bb_upper, bb_middle, bb_lower = compute_bollinger_bands(
                    prices, period=self._cfg.bb_period,
                )
                bb_range = bb_upper[-1] - bb_lower[-1]
                bb_position = (prices[-1] - bb_lower[-1]) / bb_range if bb_range > 0 else 0.5
                if bb_position > self._cfg.bb_upper_reject:
                    logger.info(
                        f"EMA[{self._cfg.tag}]: SN{netuid} BB reject — "
                        f"BB% {bb_position:.2f} > {self._cfg.bb_upper_reject}"
                    )
                    continue
            bars = bars_above_below_ema(prices, self._cfg.slow_period)
            bars = max(bars, self._cfg.confirm_bars)  # clamp floor
            freshness = self._cfg.confirm_bars / bars  # 1.0 at confirm_bars, decays
            score = freshness * _math.log1p(tao_in_pool)
            # Volatility penalty: deprioritize high-vol subnets in entry queue
            if self._cfg.vol_sizing_enabled:
                seven_day = snap_data.get("seven_day_prices", [])
                vol_prices = [float(e["price"]) for e in seven_day if e.get("price")]
                vol = rolling_volatility(vol_prices, window=self._cfg.vol_window)
                if vol is not None and vol > 0:
                    vol_penalty = max(0.5, 1.0 - (vol / self._cfg.vol_scoring_penalty_at))
                    score *= vol_penalty
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
                f"tao_in_pool={float(snap_data.get('total_tao', 0) or 0) / 1e9:.1f}"
            )

            # Deep history confirmation: re-verify crossover with 14-day data
            if settings.SUBNET_HISTORY_ENABLED and settings.SUBNET_HISTORY_ON_ENTRY:
                confirmed = await self._confirm_with_deep_history(netuid)
                if not confirmed:
                    logger.info(
                        f"EMA[{self._cfg.tag}]: SN{netuid} — deep history did not confirm crossover, skipping"
                    )
                    continue

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

            # Snapshot on-chain alpha to track emission yield
            if not self._cfg.dry_run and pos.staked_hotkey:
                await self._snapshot_emissions(pos, onchain_price)

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

    async def _snapshot_emissions(self, pos: EmaPosition, alpha_price: float) -> None:
        """Query on-chain alpha stake and update emission tracking fields.

        Called each watcher cycle for live positions. The delta between
        on-chain alpha and entry alpha is emission yield from staking.
        """
        onchain_alpha = await self._executor.get_onchain_stake(pos.staked_hotkey, pos.netuid)
        if onchain_alpha is None:
            return  # RPC failed — skip this cycle

        em_alpha = max(0.0, onchain_alpha - pos.amount_alpha)
        em_tao = em_alpha * alpha_price

        # Update in-memory position
        async with self._state_lock:
            target = next((p for p in self._open if p.position_id == pos.position_id), None)
            if target:
                target.current_alpha = onchain_alpha
                target.emission_alpha = em_alpha
                target.emission_tao = em_tao

        # Persist to DB
        await self._db.update_emission_snapshot(
            position_id=pos.position_id,
            current_alpha=onchain_alpha,
            emission_alpha=em_alpha,
            emission_tao=em_tao,
        )

    # ── Entry watcher ───────────────────────────────────────────────

    def _detect_new_crossovers(
        self,
        raw_prices: dict[int, list],
    ) -> list[int]:
        """Compare current EMA state to last-known state per subnet.

        Returns netuids that have a *new* bullish crossover (was bearish/unknown,
        now fast EMA > slow EMA for the most recent candle).
        """
        new_crossovers: list[int] = []
        for netuid, seven_day in raw_prices.items():
            if netuid == 0:
                continue
            candles = build_sampled_candles(seven_day, self._cfg.candle_timeframe_hours)
            prices = candle_close_prices(candles)
            if not prices:
                continue
            ema_fast = compute_ema(prices, self._cfg.fast_period)
            ema_slow = compute_ema(prices, self._cfg.slow_period)
            if not ema_fast or not ema_slow:
                continue
            current = "bullish" if ema_fast[-1] > ema_slow[-1] else "bearish"
            prev = self._last_crossover_state.get(netuid, "unknown")
            if current == "bullish" and prev in ("bearish", "unknown"):
                new_crossovers.append(netuid)
            self._last_crossover_state[netuid] = current
        return new_crossovers

    async def run_entry_watch(self) -> dict:
        """Lightweight entry signal polling — called every EMA_ENTRY_WATCHER_SEC.

        Fetches fresh prices, scans for new bullish EMA crossovers, and triggers
        an early run_cycle() restricted to those subnets if any are found.
        All risk checks (gini, correlation, balance, pool impact) remain in run_cycle.
        """
        summary: dict = {"checked": False, "new_crossovers": []}

        if len(self._open) >= self._cfg.max_positions:
            return summary

        if self.is_breaker_active:
            return summary

        prices, raw_prices = await self._taostats.get_alpha_prices(include_raw=True)
        if not prices:
            return summary

        new_crossovers = self._detect_new_crossovers(raw_prices)
        summary["checked"] = True
        summary["new_crossovers"] = new_crossovers

        if new_crossovers:
            # Fetch companion strategy's netuids for cross-exclusion
            companion_netuids: set[int] | None = None
            if self._companion_netuids_cb:
                try:
                    companion_netuids = await self._companion_netuids_cb()
                except Exception:
                    pass
            logger.info(
                f"EMA[{self._cfg.tag}] entry watcher: new crossovers",
                data={"netuids": new_crossovers},
            )
            await self.run_cycle(
                globally_occupied=companion_netuids,
                target_netuids=new_crossovers,
            )

        return summary

    def _compute_flow_delta(self, netuid: int) -> float | None:
        """Compute TAO flow as percentage of pool depth between current and previous snapshot.

        Returns positive for inflow, negative for outflow, None if data unavailable.
        """
        cur_snap = self._taostats._pool_snapshot.get(netuid, {})
        prev_snap = self._taostats._prev_pool_snapshot.get(netuid, {})

        cur_tao = float(cur_snap.get("total_tao", 0) or 0) / 1e9
        prev_tao = float(prev_snap.get("total_tao", 0) or 0) / 1e9

        if prev_tao <= 0 or cur_tao <= 0:
            return None

        flow_pct = (cur_tao - prev_tao) / prev_tao * 100.0
        return flow_pct

    def _compute_atr_trail_pct(self, netuid: int, current_price: float) -> float | None:
        """Derive trailing stop % from ATR using cached warm history candles.

        Returns None if insufficient data (caller falls back to ROI-based tiers).
        """
        history = self._warm_history.get(netuid)
        if not history:
            return None

        candles = build_candles_from_history(
            history, candle_hours=self._cfg.candle_timeframe_hours
        )
        if len(candles) < self._cfg.atr_period:
            return None

        atr_values = compute_atr(candles, period=self._cfg.atr_period)
        if not atr_values or current_price <= 0:
            return None

        atr = atr_values[-1]
        trailing_pct = (atr / current_price) * self._cfg.atr_multiplier * 100.0
        trailing_pct = max(self._cfg.trailing_min_pct, min(trailing_pct, self._cfg.trailing_max_pct))
        return trailing_pct

    def _dynamic_trail_pct(self, pnl_pct: float, netuid: int = 0, current_price: float = 0.0, override_pct: float | None = None) -> float:
        """Return trailing stop percentage.

        Priority:
        1. override_pct (set after partial scale-out to tighten trailing)
        2. ATR-based (when trailing_stop_dynamic is True and candle data exists)
        3. ROI-based tiers (legacy fallback)
        4. Flat configured trailing_stop_pct
        """
        if override_pct is not None:
            return override_pct

        if not self._cfg.trailing_stop_dynamic:
            return self._cfg.trailing_stop_pct

        # ATR-based trailing: use candle data for per-subnet volatility
        if netuid > 0 and current_price > 0:
            atr_trail = self._compute_atr_trail_pct(netuid, current_price)
            if atr_trail is not None:
                return atr_trail

        # Fallback: ROI-based tiers
        if pnl_pct >= 90:
            return 6.0
        if pnl_pct >= 75:
            return 8.0
        if pnl_pct >= 60:
            return 10.0
        return self._cfg.trailing_stop_pct

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

        # Trailing stop: once in profit, exit if price drops from peak
        # Uses ATR-based or dynamic trail that adapts to subnet volatility
        if pnl_pct > 0 and pos.peak_price > pos.entry_price:
            drawdown = (pos.peak_price - cur) / pos.peak_price * 100.0
            trail = self._dynamic_trail_pct(
                pnl_pct,
                netuid=pos.netuid,
                current_price=cur,
                override_pct=pos.trailing_override_pct,
            )
            if drawdown >= trail:
                return "TRAILING_STOP"

        return None

    def _check_exit(self, pos: EmaPosition, cur: float, prices: list[float]) -> str | None:
        price_reason = self._check_price_exit(pos, cur)
        if price_reason == "TAKE_PROFIT":
            return price_reason
        if price_reason == "STOP_LOSS":
            # RSI oversold suppression: defer stop-loss if RSI is oversold
            # (potential bounce), but enforce a hard floor at 1.5x stop-loss
            pnl_pct = (cur - pos.entry_price) / pos.entry_price * 100.0
            hard_floor = -self._cfg.stop_loss_pct * 1.5
            if (
                self._cfg.rsi_filter_enabled
                and pnl_pct > hard_floor
                and prices
            ):
                rsi = compute_rsi(prices, period=self._cfg.rsi_period)
                if rsi[-1] < self._cfg.rsi_oversold:
                    logger.info(
                        f"EMA[{self._cfg.tag}]: SN{pos.netuid} RSI suppress stop-loss — "
                        f"RSI {rsi[-1]:.1f} < {self._cfg.rsi_oversold}, PnL {pnl_pct:.1f}%"
                    )
                    return None  # defer stop-loss, let RSI recover
            return price_reason

        entry_dt = parse_iso(pos.entry_ts)
        hours_held = (utc_now() - entry_dt).total_seconds() / 3600.0

        # Hybrid time exit: stage 1 = partial exit, stage 2 = full close
        if not pos.scaled_out and hours_held >= self._cfg.partial_exit_hours:
            return "PARTIAL_TIME_EXIT"
        if hours_held >= self._cfg.final_time_stop_hours:
            return "TIME_STOP"

        if price_reason == "TRAILING_STOP":
            return price_reason

        # Flow reversal exit: consecutive negative flow readings
        if self._cfg.flow_reversal_exit_enabled:
            history = self._flow_history.get(pos.netuid, [])
            if len(history) >= self._cfg.flow_reversal_consecutive:
                recent = history[-self._cfg.flow_reversal_consecutive:]
                if all(flow < -self._cfg.flow_reversal_min_outflow_pct for _, flow in recent):
                    return "FLOW_REVERSAL"

        # EMA cross exit: consecutive closes below EMA
        sig = ema_signal(prices, self._cfg.slow_period, self._cfg.confirm_bars)
        if sig == "SELL":
            return "EMA_CROSS"

        return None

    async def _partial_exit(self, pos: EmaPosition, cur: float) -> dict | None:
        """Stage 1 of hybrid time exit: close a fraction of the position.

        Unstakes partial_exit_pct of the alpha, tightens trailing stop for the
        remainder, and updates position state (does NOT close the position).
        """
        exit_pct = self._cfg.partial_exit_pct
        alpha_to_exit = pos.amount_alpha * exit_pct

        # If remaining alpha would be dust, just do a full exit instead
        remaining_alpha = pos.amount_alpha - alpha_to_exit
        if remaining_alpha < 0.01:
            logger.info(
                f"EMA[{self._cfg.tag}] partial_exit SN{pos.netuid}: "
                f"remainder too small ({remaining_alpha:.4f}), doing full TIME_STOP"
            )
            return await self._exit(pos, cur, "TIME_STOP")

        pnl_pct = (cur - pos.entry_price) / pos.entry_price * 100.0

        logger.info(
            f"EMA[{self._cfg.tag}] PARTIAL_EXIT: SN{pos.netuid}",
            data={
                "alpha_to_exit": alpha_to_exit,
                "remaining_alpha": remaining_alpha,
                "exit_pct": exit_pct,
                "pnl_pct": pnl_pct,
            },
        )

        if self._cfg.dry_run:
            # Simulate partial exit
            tao_received = alpha_to_exit * cur
        else:
            import bittensor as _bt

            bal_before = await self._executor.get_tao_balance()
            try:
                loop = asyncio.get_running_loop()
                sub = self._executor._substrate
                wallet = self._executor._wallet
                chunk_bal = _bt.Balance.from_tao(alpha_to_exit)
                result = await loop.run_in_executor(
                    None,
                    lambda: sub.unstake(
                        wallet=wallet,
                        netuid=pos.netuid,
                        hotkey_ss58=pos.staked_hotkey,
                        amount=chunk_bal,
                        allow_partial_stake=True,
                        safe_unstaking=True,
                        rate_tolerance=self._cfg.max_slippage_pct / 100.0,
                        wait_for_inclusion=True,
                        wait_for_finalization=False,
                    ),
                )
                if hasattr(result, "success") and not result.success:
                    logger.error(
                        f"EMA[{self._cfg.tag}] partial_unstake_failed SN{pos.netuid}: "
                        f"{getattr(result, 'message', result)}"
                    )
                    return None
            except Exception as e:
                logger.error(f"EMA[{self._cfg.tag}] partial_exit_error SN{pos.netuid}: {e}")
                return None

            bal_after = await self._executor.get_tao_balance()
            tao_received = max(0.0, bal_after - bal_before)

        partial_pnl = tao_received - (pos.amount_tao * exit_pct)

        # Compute tightened trailing stop for the remainder
        current_trail = self._dynamic_trail_pct(
            pnl_pct, netuid=pos.netuid, current_price=cur,
        )
        tightened_trail = current_trail * self._cfg.partial_trailing_tighten

        now_iso = utc_iso()

        # Update in-memory position
        async with self._state_lock:
            target = next((p for p in self._open if p.position_id == pos.position_id), None)
            if target:
                target.amount_alpha = remaining_alpha
                target.scaled_out = True
                target.scaled_out_ts = now_iso
                target.partial_pnl_tao = partial_pnl
                target.trailing_override_pct = tightened_trail

        # Persist to DB
        await self._db.update_partial_exit(
            position_id=pos.position_id,
            new_amount_alpha=remaining_alpha,
            partial_pnl_tao=partial_pnl,
            scaled_out_ts=now_iso,
        )
        self._realized_pnl += partial_pnl

        await send_alert(
            f"⏱️ <b>[{self._cfg.tag.upper()}] PARTIAL EXIT</b>: SN{pos.netuid} | "
            f"Exited {exit_pct*100:.0f}% at {pnl_pct:+.1f}% | "
            f"{tao_received:.4f} τ out ({partial_pnl:+.4f} τ PnL) | "
            f"Trailing tightened {current_trail:.1f}% → {tightened_trail:.1f}%"
        )

        return {
            "netuid": pos.netuid,
            "reason": "PARTIAL_TIME_EXIT",
            "pnl_pct": round(pnl_pct, 2),
            "pnl_tao": round(partial_pnl, 4),
            "tao_out": round(tao_received, 4),
            "partial": True,
            "exit_pct": exit_pct,
            "trailing_tightened_to": round(tightened_trail, 2),
        }

    async def _exit(self, pos: EmaPosition, cur: float, reason: str) -> dict | None:
        pnl_pct = (cur - pos.entry_price) / pos.entry_price * 100.0

        # Ghost detection: if on-chain alpha is effectively zero, the position
        # was already exited (e.g. companion strategy's unstake_all consumed it).
        # IMPORTANT: only ghost-close when we get a *confirmed* zero balance.
        # If the query fails (returns None), skip ghost detection entirely to
        # avoid closing real positions due to RPC errors or deferred substrate.
        if not self._cfg.dry_run and pos.staked_hotkey:
            onchain_alpha = await self._executor.get_onchain_stake(pos.staked_hotkey, pos.netuid)
            if onchain_alpha is None:
                logger.warning(
                    f"EMA[{self._cfg.tag}] Ghost check SKIPPED SN{pos.netuid}: "
                    f"could not query on-chain stake — proceeding with normal exit"
                )
            elif onchain_alpha < 0.001:
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
                self._flow_history.pop(pos.netuid, None)
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

        # --- Just-in-time pool refresh for exit chunk sizing ---
        fresh_alpha_pool: float | None = None
        if settings.EMA_FRESH_POOL_ON_TRADE and not self._cfg.dry_run:
            fresh_pool = await self._taostats.get_fresh_pool(pos.netuid)
            if fresh_pool:
                fresh_alpha_pool = float(fresh_pool.get("alpha_in_pool", 0) or 0) / 1e9
                logger.info(
                    f"EMA[{self._cfg.tag}] fresh_pool_exit SN{pos.netuid}",
                    data={"fresh_alpha_in_pool": round(fresh_alpha_pool, 2)},
                )
            else:
                logger.warning(
                    f"EMA[{self._cfg.tag}] fresh_pool_fetch_failed SN{pos.netuid}: "
                    "executor will use cached snapshot for chunking"
                )

        try:
            swap = await self._executor.execute_swap(
                origin_netuid=pos.netuid,
                destination_netuid=0,
                amount_tao=pos.amount_tao,
                max_slippage_pct=self._cfg.max_slippage_pct,
                dry_run=self._cfg.dry_run,
                hotkey_ss58=pos.staked_hotkey or None,
                alpha_in_pool_override=fresh_alpha_pool,
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
            # Emission yield: extra alpha accumulated beyond entry alpha
            exit_em_alpha = max(0.0, exit_alpha - pos.amount_alpha)
            exit_em_tao = exit_em_alpha * cur

            # Split PnL: price-only PnL (what you'd get without emissions)
            # vs emission contribution. Total PnL = price_pnl + emission_tao.
            price_pnl_tao = actual_pnl_tao - exit_em_tao

            await self._db.close_ema_position(
                position_id=pos.position_id,
                exit_price=cur,
                amount_tao_out=my_received,
                pnl_tao=actual_pnl_tao,
                pnl_pct=actual_pnl_pct,
                exit_reason=reason,
                exit_slippage_pct=exit_slippage_pct,
            )
            # Persist final emission data alongside the close
            await self._db.update_exit_emission(
                position_id=pos.position_id,
                emission_alpha=exit_em_alpha,
                emission_tao=exit_em_tao,
            )
            self._realized_pnl += actual_pnl_tao
            self._flow_history.pop(pos.netuid, None)
            expires = utc_now() + timedelta(hours=self._cfg.cooldown_hours)
            async with self._state_lock:
                self._open = [p for p in self._open if p.position_id != pos.position_id]
                self._cooldowns[pos.netuid] = expires
            await self._db.set_cooldown(self._cfg.tag, pos.netuid, expires.isoformat())
            yield_note = f" (emissions: {exit_em_tao:+.4f} τ, price: {price_pnl_tao:+.4f} τ)" if exit_em_tao > 0.01 else ""
            await send_alert(
                f"📉 <b>[{self._cfg.tag.upper()}] EXIT {reason}</b>: SN{pos.netuid} | "
                f"Price {actual_pnl_pct:+.2f}% | {actual_pnl_tao:+.4f} τ{yield_note} | "
                f"{my_received:.4f} τ out"
            )
            # Fire post-exit verification as background task
            if self._cfg.post_exit_verify and not self._cfg.dry_run and pos.staked_hotkey:
                asyncio.create_task(
                    self._verify_exit(
                        netuid=pos.netuid,
                        hotkey=pos.staked_hotkey,
                        expected_tao_received=my_received,
                        exit_record_id=pos.position_id,
                    )
                )
            return {
                "netuid": pos.netuid,
                "reason": reason,
                "pnl_pct": round(actual_pnl_pct, 2),
                "pnl_tao": round(actual_pnl_tao, 4),
                "tao_out": my_received,
                "emission_alpha": round(exit_em_alpha, 4),
                "emission_tao": round(exit_em_tao, 4),
                "price_pnl_tao": round(price_pnl_tao, 4),
            }
        return None

    # ── Entry logic ────────────────────────────────────────────────

    async def _enter(self, netuid: int, cur: float, snap_data: dict) -> dict | None:
        # --- Price drift check: cancel if price moved up too much since scoring ---
        if cur > 0:
            live_price = await self._executor.get_onchain_alpha_price(netuid)
            if live_price > 0:
                drift_pct = (live_price - cur) / cur * 100.0
                if drift_pct > self._cfg.entry_price_drift_pct:
                    logger.warning(
                        f"EMA[{self._cfg.tag}]: SN{netuid} entry cancelled — "
                        f"price drifted +{drift_pct:.1f}% since scoring "
                        f"(scored={cur:.6f}, live={live_price:.6f})"
                    )
                    return None

        # --- Volatility-based sizing ---
        if self._cfg.vol_sizing_enabled:
            seven_day = snap_data.get("seven_day_prices", [])
            vol_prices = [float(e["price"]) for e in seven_day if e.get("price")]
            vol = rolling_volatility(vol_prices, window=self._cfg.vol_window)
            if vol is not None:
                vol_clamped = max(self._cfg.vol_floor, min(vol, self._cfg.vol_cap))
                vol_pct = self._cfg.vol_target_risk / vol_clamped
                vol_pct = max(self._cfg.vol_min_size_pct,
                              min(vol_pct, self._cfg.vol_max_size_pct))
                full_amount = round(self._cfg.pot_tao * vol_pct, 6)
                logger.info(
                    f"EMA[{self._cfg.tag}] VOL SIZED SN{netuid}: ann_vol={vol:.4f}, "
                    f"size_pct={vol_pct:.4f}, size_tao={full_amount:.4f}"
                )
            else:
                logger.warning(
                    f"EMA[{self._cfg.tag}] VOL DATA INSUFFICIENT SN{netuid}: "
                    f"falling back to flat size"
                )
                full_amount = round(self._cfg.pot_tao * self._cfg.position_size_pct, 6)
        else:
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

            # Guard: check for orphan on-chain stake that isn't tracked in the DB.
            # This catches positions left behind by swap hangs or unclean restarts.
            try:
                existing_alpha = await self._executor.get_onchain_stake(validator_hk, netuid)
                if existing_alpha is not None and existing_alpha > 0:
                    # Check if ANY strategy already tracks a position on this netuid
                    has_db_pos = any(p.netuid == netuid for p in self._open)
                    if not has_db_pos:
                        logger.error(
                            f"EMA[{self._cfg.tag}] ORPHAN STAKE SN{netuid}: "
                            f"{existing_alpha:.4f} alpha on-chain but no DB position — skipping entry",
                            data={"netuid": netuid, "orphan_alpha": existing_alpha, "hotkey": validator_hk},
                        )
                        await send_alert(
                            f"⚠️ <b>[{self._cfg.tag.upper()}] ORPHAN STAKE</b>: SN{netuid} has "
                            f"{existing_alpha:.2f} alpha on-chain but no tracked position. "
                            f"Entry skipped. Manual review needed."
                        )
                        return None
            except Exception as e:
                logger.warning(f"EMA[{self._cfg.tag}] orphan check failed SN{netuid}: {e}")

        # --- Just-in-time pool refresh for accurate sizing ---
        stale_tao = tao_in_pool
        stale_size = amount_tao
        pre_trade_slippage_est: float | None = None
        if settings.EMA_FRESH_POOL_ON_TRADE:
            fresh_pool = await self._taostats.get_fresh_pool(netuid)
            if fresh_pool:
                fresh_tao_in_pool = float(fresh_pool.get("total_tao", 0) or 0) / 1e9
                fresh_alpha_in_pool = float(fresh_pool.get("alpha_in_pool", 0) or 0) / 1e9

                # Re-compute adaptive size with fresh reserves
                if fresh_tao_in_pool > 0:
                    safe_tao = fresh_tao_in_pool * max_impact
                    if amount_tao > safe_tao:
                        amount_tao = round(max(safe_tao, full_amount * 0.25), 6)

                # Pre-trade slippage estimate (constant-product, 0.3% fee)
                if fresh_tao_in_pool > 0 and fresh_alpha_in_pool > 0:
                    fee_rate = 0.003
                    tao_after_fee = amount_tao * (1 - fee_rate)
                    alpha_out = (fresh_alpha_in_pool * tao_after_fee) / (fresh_tao_in_pool + tao_after_fee)
                    ideal_alpha = amount_tao / (fresh_tao_in_pool / fresh_alpha_in_pool)
                    pre_trade_slippage_est = max(0.0, (1 - alpha_out / ideal_alpha) * 100) if ideal_alpha > 0 else 0.0

                    if pre_trade_slippage_est > settings.EMA_PRE_TRADE_MAX_SLIPPAGE_PCT:
                        logger.warning(
                            f"EMA[{self._cfg.tag}] PRE-TRADE SLIPPAGE REJECT SN{netuid}: "
                            f"est={pre_trade_slippage_est:.2f}% > {settings.EMA_PRE_TRADE_MAX_SLIPPAGE_PCT}%",
                            data={
                                "netuid": netuid,
                                "est_slippage": pre_trade_slippage_est,
                                "threshold": settings.EMA_PRE_TRADE_MAX_SLIPPAGE_PCT,
                                "fresh_tao_in_pool": fresh_tao_in_pool,
                                "amount_tao": amount_tao,
                            },
                        )
                        return None

                delta_pct = ((fresh_tao_in_pool - stale_tao) / stale_tao * 100) if stale_tao > 0 else 0.0
                logger.info(
                    f"EMA[{self._cfg.tag}] fresh_pool_refresh SN{netuid}",
                    data={
                        "stale_tao_in_pool": round(stale_tao, 2),
                        "fresh_tao_in_pool": round(fresh_tao_in_pool, 2),
                        "delta_pct": round(delta_pct, 1),
                        "stale_size_tao": round(stale_size, 4),
                        "fresh_size_tao": round(amount_tao, 4),
                        "pre_trade_slippage_est": round(pre_trade_slippage_est, 2) if pre_trade_slippage_est is not None else None,
                    },
                )
            else:
                logger.warning(
                    f"EMA[{self._cfg.tag}] fresh_pool_fetch_failed SN{netuid}: "
                    "proceeding with cycle-start snapshot"
                )

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

    # ── Deep history confirmation ─────────────────────────────────

    async def _confirm_with_deep_history(self, netuid: int) -> bool:
        """Fetch 14-day history and verify the EMA crossover still holds.

        Returns True if the crossover is confirmed (or history is unavailable
        — we don't block entries when the endpoint fails).
        """
        try:
            history = await self._taostats.get_subnet_history(
                netuid=netuid,
                interval=settings.SUBNET_HISTORY_INTERVAL,
                limit=settings.SUBNET_HISTORY_LIMIT,
            )
            if not history:
                logger.debug(f"EMA[{self._cfg.tag}]: SN{netuid} deep history empty, allowing entry")
                return True

            # Cache the history for the subnet (used in exit checks too)
            self._warm_history[netuid] = history

            # Build candles at the strategy's timeframe from deep history
            deep_candles = build_candles_from_history(
                history, candle_hours=self._cfg.candle_timeframe_hours
            )
            if not deep_candles:
                logger.debug(f"EMA[{self._cfg.tag}]: SN{netuid} no candles from deep history, allowing entry")
                return True

            deep_prices = candle_close_prices(deep_candles)
            if len(deep_prices) < self._cfg.confirm_bars:
                logger.debug(
                    f"EMA[{self._cfg.tag}]: SN{netuid} deep history too short "
                    f"({len(deep_prices)} prices), allowing entry"
                )
                return True

            signal = dual_ema_signal(
                deep_prices,
                self._cfg.fast_period,
                self._cfg.slow_period,
                self._cfg.confirm_bars,
            )
            if signal == "BUY":
                logger.info(
                    f"EMA[{self._cfg.tag}]: SN{netuid} deep history confirmed crossover "
                    f"({len(deep_candles)} candles, {len(history)} data points)"
                )
                return True

            logger.info(
                f"EMA[{self._cfg.tag}]: SN{netuid} deep history signal={signal} "
                f"({len(deep_candles)} candles) — crossover not confirmed"
            )
            return False

        except Exception as exc:
            # Don't block entries on history endpoint failures
            logger.warning(f"EMA[{self._cfg.tag}]: SN{netuid} deep history check failed: {exc}")
            return True

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

    # ── Gini filter (two-tier) ────────────────────────────────────

    async def _get_gini(self, netuid: int) -> float | None:
        """Return Gini coefficient with two-tier refresh.

        Tier 1: On-chain metagraph (authoritative, cached with TTL).
        Tier 2: Pool concentration alert — if alpha_in_pool shifted >threshold
                 since last cycle, force an immediate Tier 1 refresh.
        Fallback: Taostats stake API if chain query fails and API fallback enabled.
        """
        import time as _time

        now = _time.time()
        cached = self._gini_cache.get(netuid)

        # Tier 2: Check for pool concentration alert (force refresh)
        force_refresh = False
        snap_data = self._taostats._pool_snapshot.get(netuid)
        if snap_data and cached:
            force_refresh = self._taostats.pool_concentration_alert(
                netuid, snap_data, self._cfg.gini_pool_delta_threshold
            )
            if force_refresh:
                logger.warning(
                    f"EMA[{self._cfg.tag}] gini_force_refresh SN{netuid}: "
                    f"pool concentration alert, cached_gini={cached[0]:.4f}"
                )

        # Tier 1: Use cache if fresh enough and no alert
        if cached and not force_refresh and (now - cached[1]) < self._cfg.gini_cache_ttl_sec:
            return cached[0]

        # Tier 1 refresh: on-chain metagraph query
        gini = await self._fetch_gini_from_chain(netuid)
        if gini is not None:
            self._gini_cache[netuid] = (gini, now)
            return gini

        # Fall back to Taostats API estimate if enabled
        if self._cfg.gini_api_fallback:
            api_gini = await self._fetch_gini_from_api(netuid)
            if api_gini is not None:
                self._gini_cache[netuid] = (api_gini, now)
                return api_gini

        return cached[0] if cached else None

    async def _fetch_gini_from_chain(self, netuid: int) -> float | None:
        """Fetch Gini coefficient from on-chain metagraph (authoritative)."""
        try:
            await self._executor._ensure_substrate()
            sub = self._executor._substrate
            if sub is None:
                return None
            loop = asyncio.get_running_loop()
            mg = await loop.run_in_executor(None, sub.metagraph, netuid)
            stakes = [float(s) for s in mg.S if float(s) > 0]
            return gini_coefficient(stakes)
        except Exception as e:
            logger.warning(f"EMA Gini chain lookup failed SN{netuid}: {e}")
            return None

    async def _fetch_gini_from_api(self, netuid: int) -> float | None:
        """Fetch Gini from Taostats stake distribution (fallback)."""
        try:
            stakes = await self._taostats.get_stake_distribution(netuid)
            if stakes:
                return gini_coefficient(stakes)
        except Exception as e:
            logger.warning(f"EMA Gini API fallback failed SN{netuid}: {e}")
        return None

    async def _prefetch_gini(self, candidates: list[int]) -> None:
        """Pre-fetch Gini for entry candidates to warm cache before scoring."""
        if self._cfg.gini_prefetch_top_n <= 0:
            return
        import time as _time

        now = _time.time()
        to_fetch = []
        for netuid in candidates[: self._cfg.gini_prefetch_top_n]:
            cached = self._gini_cache.get(netuid)
            if not cached or (now - cached[1]) > self._cfg.gini_cache_ttl_sec:
                to_fetch.append(self._get_gini(netuid))
        if to_fetch:
            await asyncio.gather(*to_fetch, return_exceptions=True)

    # ── Portfolio summary ──────────────────────────────────────────

    def get_portfolio_summary(self, alpha_prices: dict[int, float]) -> dict:
        deployed = sum(p.amount_tao for p in self._open)
        raw_pot = self._cfg.pot_tao + self._realized_pnl
        unstaked = max(0.0, raw_pot - deployed)
        total_emission_tao = 0.0
        open_positions = []
        for p in self._open:
            cur = alpha_prices.get(p.netuid, p.entry_price)
            pnl_pct = (cur - p.entry_price) / p.entry_price * 100.0 if p.entry_price else 0.0
            hours = (utc_now() - parse_iso(p.entry_ts)).total_seconds() / 3600.0
            # Use latest snapshot emission data; revalue at current price
            em_alpha = p.emission_alpha
            em_tao = em_alpha * cur if em_alpha > 0 else 0.0
            total_emission_tao += em_tao
            open_positions.append({
                "position_id": p.position_id,
                "netuid": p.netuid,
                "entry_price": p.entry_price,
                "current_price": cur,
                "pnl_pct": round(pnl_pct, 4),
                "amount_tao": p.amount_tao,
                "amount_alpha": round(p.amount_alpha, 4),
                "current_alpha": round(p.current_alpha, 4) if p.current_alpha else None,
                "emission_alpha": round(em_alpha, 4),
                "emission_tao": round(em_tao, 6),
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
            "total_emission_tao": round(total_emission_tao, 6),
            "breaker_active": self.is_breaker_active,
        }

    async def _open_positions_snapshot(self) -> list[EmaPosition]:
        async with self._state_lock:
            return list(self._open)

    async def _find_open_position(self, position_id: int) -> EmaPosition | None:
        async with self._state_lock:
            return next((p for p in self._open if p.position_id == position_id), None)

    # ── Post-exit verification ─────────────────────────────────

    async def _verify_exit(
        self,
        netuid: int,
        hotkey: str,
        expected_tao_received: float,
        exit_record_id: int,
        attempt: int = 1,
    ) -> None:
        """Poll on-chain state to confirm exit completed."""
        delay = self._cfg.post_exit_verify_delay_sec * (2 ** (attempt - 1))
        await asyncio.sleep(delay)

        actual_alpha = await self._executor.get_onchain_stake(hotkey, netuid)
        if actual_alpha is None:
            logger.warning(
                f"EMA[{self._cfg.tag}] exit_verify_skip SN{netuid}: "
                "could not query on-chain stake"
            )
            return

        if actual_alpha > self._cfg.post_exit_alpha_threshold:
            logger.error(
                f"EMA[{self._cfg.tag}] exit_verification_failed",
                data={
                    "netuid": netuid,
                    "actual_alpha": actual_alpha,
                    "attempt": attempt,
                },
            )
            if attempt < self._cfg.post_exit_max_retries:
                await self._retry_exit(
                    netuid, hotkey, actual_alpha, exit_record_id, attempt + 1
                )
            else:
                await self._flag_stuck_position(
                    netuid, hotkey, actual_alpha, exit_record_id
                )
        else:
            logger.info(
                f"EMA[{self._cfg.tag}] exit_verified SN{netuid}",
                data={"remaining_alpha": actual_alpha},
            )
            await self._db.update_exit_verified(exit_record_id, verified=True)

    async def _retry_exit(
        self,
        netuid: int,
        hotkey: str,
        remaining_alpha: float,
        record_id: int,
        attempt: int,
    ) -> None:
        """Attempt to unstake residual alpha after a partial fill."""
        logger.warning(
            f"EMA[{self._cfg.tag}] exit_retry SN{netuid}",
            data={"remaining_alpha": remaining_alpha, "attempt": attempt},
        )
        try:
            swap = await self._executor.execute_swap(
                origin_netuid=netuid,
                destination_netuid=0,
                amount_tao=0,
                max_slippage_pct=self._cfg.max_slippage_pct,
                dry_run=False,
                hotkey_ss58=hotkey,
            )
            if swap.success and swap.received_tao > 0:
                logger.info(
                    f"EMA[{self._cfg.tag}] exit_retry_success SN{netuid}",
                    data={"tao_recovered": swap.received_tao, "attempt": attempt},
                )
                await self._db.update_exit_tao_recovered(record_id, swap.received_tao)
                self._realized_pnl += swap.received_tao
                await send_alert(
                    f"🔄 <b>[{self._cfg.tag.upper()}] EXIT RETRY OK</b>: SN{netuid} | "
                    f"Recovered {swap.received_tao:.4f} τ (attempt {attempt})"
                )
                await self._verify_exit(
                    netuid, hotkey, swap.received_tao, record_id, attempt
                )
            else:
                logger.error(
                    f"EMA[{self._cfg.tag}] exit_retry_failed SN{netuid}",
                    data={"error": swap.error, "attempt": attempt},
                )
                if attempt < self._cfg.post_exit_max_retries:
                    await self._verify_exit(
                        netuid, hotkey, 0.0, record_id, attempt + 1
                    )
                else:
                    await self._flag_stuck_position(
                        netuid, hotkey, remaining_alpha, record_id
                    )
        except Exception as e:
            logger.error(
                f"EMA[{self._cfg.tag}] exit_retry_error SN{netuid}: {e}",
                data={"attempt": attempt},
            )
            if attempt < self._cfg.post_exit_max_retries:
                await self._verify_exit(
                    netuid, hotkey, 0.0, record_id, attempt + 1
                )
            else:
                await self._flag_stuck_position(
                    netuid, hotkey, remaining_alpha, record_id
                )

    async def _flag_stuck_position(
        self,
        netuid: int,
        hotkey: str,
        remaining_alpha: float,
        record_id: int,
    ) -> None:
        """Mark position as STUCK for manual intervention."""
        logger.error(
            f"EMA[{self._cfg.tag}] position_stuck SN{netuid}",
            data={
                "hotkey": hotkey,
                "remaining_alpha": remaining_alpha,
                "record_id": record_id,
            },
        )
        await self._db.update_exit_verified(record_id, verified=False)
        await self._db.update_position_status(record_id, "STUCK")
        self._stuck_positions[netuid] = {
            "record_id": record_id,
            "netuid": netuid,
            "hotkey": hotkey,
            "remaining_alpha": remaining_alpha,
            "flagged_at": utc_iso(),
        }
        await send_alert(
            f"🚨 <b>[{self._cfg.tag.upper()}] STUCK POSITION</b>: SN{netuid} | "
            f"{remaining_alpha:.4f} alpha remaining | manual intervention required"
        )

    async def _verify_unverified_exits(self) -> None:
        """On startup, re-verify any exits that didn't complete verification."""
        if not self._cfg.post_exit_verify or self._cfg.dry_run:
            return
        rows = await self._db.get_unverified_exits()
        for row in rows:
            hotkey = row.get("staked_hotkey") or ""
            if not hotkey:
                continue
            logger.info(
                f"EMA[{self._cfg.tag}] re-verifying unverified exit",
                data={"id": row["id"], "netuid": row["netuid"]},
            )
            asyncio.create_task(
                self._verify_exit(
                    netuid=row["netuid"],
                    hotkey=hotkey,
                    expected_tao_received=row.get("amount_tao_out") or 0,
                    exit_record_id=row["id"],
                )
            )

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
