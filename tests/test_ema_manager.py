"""
Tests for EMA manager exit behavior and 3-bar EMA signal preservation.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.config import StrategyConfig
from app.portfolio.ema_manager import EmaManager, EmaPosition
from app.strategy.ema_signals import Candle, build_sampled_candles, bullish_ema_bounce, candle_close_prices


def _test_config(**overrides) -> StrategyConfig:
    defaults = {
        "tag": "test",
        "fast_period": 3,
        "slow_period": 9,
        "confirm_bars": 3,
        "pot_tao": 10.0,
        "position_size_pct": 0.20,
        "max_positions": 5,
        "stop_loss_pct": 8.0,
        "take_profit_pct": 20.0,
        "trailing_stop_pct": 5.0,
        "breakeven_trigger_pct": 3.0,
        "max_holding_hours": 168,
        "cooldown_hours": 4.0,
        "bounce_enabled": True,
        "bounce_touch_tolerance_pct": 1.0,
        "bounce_require_green": True,
        "max_gini": 0.82,
        "gini_cache_ttl_sec": 3600,
        "correlation_threshold": 0.80,
        "candle_timeframe_hours": 4,
        "dry_run": True,
        "max_slippage_pct": 5.0,
        "max_entry_price_tao": 0.1,
        "drawdown_breaker_pct": 15.0,
        "drawdown_pause_hours": 6.0,
    }
    defaults.update(overrides)
    return StrategyConfig(**defaults)


class TestEmaExitLogic:
    def _make_manager(self, **overrides) -> EmaManager:
        return EmaManager(AsyncMock(), AsyncMock(), AsyncMock(), _test_config(**overrides))

    def test_ema_cross_still_drives_signal_exit(self):
        mgr = self._make_manager(slow_period=3, confirm_bars=3)
        pos = EmaPosition(
            position_id=1,
            netuid=7,
            entry_price=5.1,
            amount_tao=2.0,
            amount_alpha=0.4,
            peak_price=5.2,
            entry_ts=datetime.now(timezone.utc).isoformat(),
        )
        prices = [10.0, 10.0, 10.0, 5.0, 5.0, 5.0]
        assert mgr._check_exit(pos, 5.0, prices) == "EMA_CROSS"

    def test_price_exit_helper_triggers_take_profit(self):
        mgr = self._make_manager(take_profit_pct=10.0)
        pos = EmaPosition(
            position_id=1,
            netuid=7,
            entry_price=1.0,
            amount_tao=2.0,
            amount_alpha=2.0,
            peak_price=1.11,
            entry_ts=datetime.now(timezone.utc).isoformat(),
        )
        assert mgr._check_price_exit(pos, 1.11) == "TAKE_PROFIT"


class TestEmaExitWatcher:
    def test_exit_watcher_uses_onchain_price_for_take_profit(self):
        db = AsyncMock()
        executor = AsyncMock()
        taostats = AsyncMock()
        mgr = EmaManager(db, executor, taostats, _test_config(take_profit_pct=10.0, cooldown_hours=4.0))
        mgr._open = [
            EmaPosition(
                position_id=11,
                netuid=42,
                entry_price=1.0,
                amount_tao=2.0,
                amount_alpha=2.0,
                peak_price=1.0,
                entry_ts=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
            )
        ]
        executor.get_onchain_alpha_price = AsyncMock(return_value=1.11)
        executor.execute_swap = AsyncMock(
            return_value=SimpleNamespace(success=True, received_tao=2.22)
        )

        with patch("app.portfolio.ema_manager.send_alert", new=AsyncMock()):
            summary = asyncio.run(mgr.run_price_exit_watch())

        assert summary["exits"]
        assert summary["exits"][0]["reason"] == "TAKE_PROFIT"
        assert mgr._open == []
        db.update_ema_peak_price.assert_awaited_once_with(11, 1.11)
        db.close_ema_position.assert_awaited_once()
        executor.execute_swap.assert_awaited_once()


class TestEmaBounceHelpers:
    def test_build_sampled_candles_ignores_incomplete_last_bucket(self):
        points = [
            {"timestamp": "2026-03-11T00:05:00Z", "price": 1.00},
            {"timestamp": "2026-03-11T04:05:00Z", "price": 1.02},
            {"timestamp": "2026-03-11T08:05:00Z", "price": 1.04},
            {"timestamp": "2026-03-11T12:05:00Z", "price": 1.06},
            {"timestamp": "2026-03-11T16:05:00Z", "price": 1.10},
            {"timestamp": "2026-03-11T19:05:00Z", "price": 1.03},
        ]

        candles = build_sampled_candles(points, timeframe_hours=4)

        assert candle_close_prices(candles) == [1.00, 1.02, 1.04, 1.06, 1.10]
        assert candles[-1].end_ts.startswith("2026-03-11T16:05:00")

    def test_bullish_ema_bounce_accepts_touch_and_reclaim(self):
        candles = [
            Candle("2026-03-10T20:05:00+00:00", "2026-03-11T00:05:00+00:00", 10.0, 10.2, 9.9, 10.0),
            Candle("2026-03-11T00:05:00+00:00", "2026-03-11T04:05:00+00:00", 10.0, 11.1, 10.0, 11.0),
            Candle("2026-03-11T04:05:00+00:00", "2026-03-11T08:05:00+00:00", 11.0, 12.1, 10.9, 12.0),
            Candle("2026-03-11T08:05:00+00:00", "2026-03-11T12:05:00+00:00", 12.0, 13.1, 12.0, 13.0),
        ]

        assert bullish_ema_bounce(candles, period=3, touch_tolerance_pct=1.0)

    def test_bullish_ema_bounce_rejects_red_pullback(self):
        candles = [
            Candle("2026-03-10T20:05:00+00:00", "2026-03-11T00:05:00+00:00", 8.0, 8.1, 7.9, 8.0),
            Candle("2026-03-11T00:05:00+00:00", "2026-03-11T04:05:00+00:00", 8.0, 9.1, 8.0, 9.0),
            Candle("2026-03-11T04:05:00+00:00", "2026-03-11T08:05:00+00:00", 9.0, 10.1, 9.0, 10.0),
            Candle("2026-03-11T08:05:00+00:00", "2026-03-11T12:05:00+00:00", 10.0, 12.1, 10.0, 12.0),
            Candle("2026-03-11T12:05:00+00:00", "2026-03-11T16:05:00+00:00", 12.0, 13.1, 12.0, 13.0),
            Candle("2026-03-11T16:05:00+00:00", "2026-03-11T20:05:00+00:00", 13.0, 13.1, 12.1, 12.5),
        ]

        assert not bullish_ema_bounce(candles, period=3, touch_tolerance_pct=1.0)


class TestEmaBounceEntryFilter:
    def _make_manager(self, **overrides) -> tuple[EmaManager, AsyncMock]:
        db = AsyncMock()
        executor = AsyncMock()
        taostats = AsyncMock()
        mgr = EmaManager(db, executor, taostats, _test_config(**overrides))
        return mgr, taostats

    def test_run_cycle_skips_candidate_without_bullish_bounce(self):
        mgr, taostats = self._make_manager(
            fast_period=2, slow_period=3, confirm_bars=3,
            candle_timeframe_hours=4, bounce_enabled=True,
            bounce_touch_tolerance_pct=1.0, bounce_require_green=True,
            max_entry_price_tao=100.0,
        )
        taostats.get_alpha_prices = AsyncMock(return_value={42: 12.5})
        taostats._pool_snapshot = {
            42: {
                "netuid": 42,
                "tao_in_pool": 1_000.0,
                "seven_day_prices": [
                    {"timestamp": "2026-03-11T00:05:00Z", "price": 8.0},
                    {"timestamp": "2026-03-11T04:05:00Z", "price": 9.0},
                    {"timestamp": "2026-03-11T08:05:00Z", "price": 10.0},
                    {"timestamp": "2026-03-11T12:05:00Z", "price": 12.0},
                    {"timestamp": "2026-03-11T16:05:00Z", "price": 13.0},
                    {"timestamp": "2026-03-11T20:05:00Z", "price": 12.5},
                ],
            }
        }
        mgr._enter = AsyncMock(return_value={"netuid": 42, "amount_tao": 4.0, "price": 12.5})

        with (
            patch("app.portfolio.ema_manager.send_alert", new=AsyncMock()),
            patch.object(mgr, "_is_correlated_with_holdings", return_value=False),
        ):
            summary = asyncio.run(mgr.run_cycle())

        assert summary["entries"] == []
        mgr._enter.assert_not_awaited()

    def test_run_cycle_allows_candidate_after_bullish_ema_reclaim(self):
        mgr, taostats = self._make_manager(
            fast_period=2, slow_period=3, confirm_bars=3,
            candle_timeframe_hours=4, bounce_enabled=True,
            bounce_touch_tolerance_pct=1.0, bounce_require_green=True,
            max_entry_price_tao=100.0,
        )
        taostats.get_alpha_prices = AsyncMock(return_value={42: 13.0})
        taostats._pool_snapshot = {
            42: {
                "netuid": 42,
                "tao_in_pool": 1_000.0,
                "seven_day_prices": [
                    {"timestamp": "2026-03-11T00:05:00Z", "price": 8.0},
                    {"timestamp": "2026-03-11T04:05:00Z", "price": 9.0},
                    {"timestamp": "2026-03-11T08:05:00Z", "price": 10.0},
                    {"timestamp": "2026-03-11T12:05:00Z", "price": 12.0},
                    {"timestamp": "2026-03-11T16:05:00Z", "price": 12.2},
                    {"timestamp": "2026-03-11T19:05:00Z", "price": 12.0},
                    {"timestamp": "2026-03-11T20:05:00Z", "price": 13.0},
                ],
            }
        }
        mgr._enter = AsyncMock(return_value={"netuid": 42, "amount_tao": 4.0, "price": 13.0})

        with (
            patch("app.portfolio.ema_manager.send_alert", new=AsyncMock()),
            patch.object(mgr, "_is_correlated_with_holdings", return_value=False),
        ):
            summary = asyncio.run(mgr.run_cycle())

        assert len(summary["entries"]) == 1
        mgr._enter.assert_awaited_once()
