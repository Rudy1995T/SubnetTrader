"""
Tests for portfolio slot sizing and time-stop exit logic.
"""
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from app.portfolio.manager import PortfolioManager, Slot
from app.config import settings


class TestSlotSizing:
    """Tests for portfolio slot allocation."""

    def test_default_four_slots(self):
        """Default config gives 4 slots = 25% each."""
        mgr = PortfolioManager.__new__(PortfolioManager)
        mgr._slots = [Slot(slot_id=i) for i in range(4)]

        total_tao = 10.0
        per_slot = mgr.slot_allocation_tao(total_tao)
        assert per_slot == pytest.approx(2.5)

    def test_slot_allocation_proportional(self):
        """Slot allocation scales with total TAO."""
        mgr = PortfolioManager.__new__(PortfolioManager)
        mgr._slots = [Slot(slot_id=i) for i in range(4)]

        assert mgr.slot_allocation_tao(100.0) == pytest.approx(25.0)
        assert mgr.slot_allocation_tao(0.0) == pytest.approx(0.0)
        assert mgr.slot_allocation_tao(1.0) == pytest.approx(0.25)

    def test_available_slots_all_cash(self):
        mgr = PortfolioManager.__new__(PortfolioManager)
        mgr._slots = [Slot(slot_id=i, status="CASH") for i in range(4)]

        available = mgr.available_slots()
        assert len(available) == 4

    def test_available_slots_mixed(self):
        mgr = PortfolioManager.__new__(PortfolioManager)
        mgr._slots = [
            Slot(slot_id=0, status="ALPHA", netuid=5),
            Slot(slot_id=1, status="CASH"),
            Slot(slot_id=2, status="ALPHA", netuid=10),
            Slot(slot_id=3, status="CASH"),
        ]

        available = mgr.available_slots()
        assert len(available) == 2
        assert available[0].slot_id == 1
        assert available[1].slot_id == 3

    def test_available_slots_none_free(self):
        mgr = PortfolioManager.__new__(PortfolioManager)
        mgr._slots = [
            Slot(slot_id=i, status="ALPHA", netuid=i + 1)
            for i in range(4)
        ]

        available = mgr.available_slots()
        assert len(available) == 0

    def test_occupied_netuids(self):
        mgr = PortfolioManager.__new__(PortfolioManager)
        mgr._slots = [
            Slot(slot_id=0, status="ALPHA", netuid=5),
            Slot(slot_id=1, status="CASH"),
            Slot(slot_id=2, status="ALPHA", netuid=10),
            Slot(slot_id=3, status="CASH"),
        ]

        occupied = mgr.occupied_netuids()
        assert occupied == {5, 10}

    def test_occupied_netuids_empty(self):
        mgr = PortfolioManager.__new__(PortfolioManager)
        mgr._slots = [Slot(slot_id=i, status="CASH") for i in range(4)]

        assert mgr.occupied_netuids() == set()


class TestExitConditions:
    """Tests for the _check_exit_conditions method."""

    def _make_mgr(self) -> PortfolioManager:
        mgr = PortfolioManager.__new__(PortfolioManager)
        return mgr

    def test_stop_loss_triggered(self):
        """Stop-loss fires when price drops below threshold."""
        mgr = self._make_mgr()
        now = datetime.now(timezone.utc)
        pos = {
            "entry_price": 1.0,
            "peak_price": 1.0,
            "entry_ts": now.isoformat(),
        }
        # Price dropped 10% (settings.STOP_LOSS_PCT default is 8%)
        current_price = 0.90
        result = mgr._check_exit_conditions(pos, current_price)
        assert result == "STOP_LOSS"

    def test_stop_loss_not_triggered(self):
        """Small loss shouldn't trigger stop-loss."""
        mgr = self._make_mgr()
        now = datetime.now(timezone.utc)
        pos = {
            "entry_price": 1.0,
            "peak_price": 1.05,
            "entry_ts": now.isoformat(),
        }
        current_price = 0.95  # -5%, below 8% threshold
        result = mgr._check_exit_conditions(pos, current_price)
        assert result is None

    def test_time_stop_72h(self):
        """Position held > 72h should trigger time stop."""
        mgr = self._make_mgr()
        old = datetime.now(timezone.utc) - timedelta(hours=73)
        pos = {
            "entry_price": 1.0,
            "peak_price": 1.0,
            "entry_ts": old.isoformat(),
        }
        current_price = 1.0  # flat, no P&L trigger
        result = mgr._check_exit_conditions(pos, current_price)
        assert result == "TIME_STOP"

    def test_time_stop_not_triggered(self):
        """Position held < 72h should NOT trigger time stop."""
        mgr = self._make_mgr()
        recent = datetime.now(timezone.utc) - timedelta(hours=10)
        pos = {
            "entry_price": 1.0,
            "peak_price": 1.0,
            "entry_ts": recent.isoformat(),
        }
        current_price = 1.0
        result = mgr._check_exit_conditions(pos, current_price)
        assert result is None

    def test_trailing_stop(self):
        """Price dropping from peak by TRAILING_STOP_PCT triggers trailing stop."""
        mgr = self._make_mgr()
        now = datetime.now(timezone.utc)
        pos = {
            "entry_price": 1.0,
            "peak_price": 1.20,  # Was up 20%
            "entry_ts": now.isoformat(),
        }
        # Price dropped 6% from peak (TRAILING_STOP_PCT default 5%)
        current_price = 1.12  # (1.20 - 1.12) / 1.20 = 6.67%
        result = mgr._check_exit_conditions(pos, current_price)
        assert result == "TRAILING_STOP"

    def test_trailing_stop_not_triggered_when_in_loss(self):
        """Trailing stop should NOT trigger when overall position is in loss."""
        mgr = self._make_mgr()
        now = datetime.now(timezone.utc)
        pos = {
            "entry_price": 1.0,
            "peak_price": 0.98,
            "entry_ts": now.isoformat(),
        }
        current_price = 0.95
        result = mgr._check_exit_conditions(pos, current_price)
        # pnl_pct = -5%, not enough for stop_loss (8%)
        # Not in profit, so trailing stop shouldn't fire
        assert result is None

    def test_take_profit(self):
        """Take-profit fires at TAKE_PROFIT_PCT."""
        mgr = self._make_mgr()
        now = datetime.now(timezone.utc)
        pos = {
            "entry_price": 1.0,
            "peak_price": 1.16,
            "entry_ts": now.isoformat(),
        }
        current_price = 1.16  # +16%, above 15% take-profit
        result = mgr._check_exit_conditions(pos, current_price)
        assert result == "TAKE_PROFIT"

    def test_priority_stop_loss_over_time(self):
        """Stop-loss has priority over time stop."""
        mgr = self._make_mgr()
        old = datetime.now(timezone.utc) - timedelta(hours=80)
        pos = {
            "entry_price": 1.0,
            "peak_price": 1.0,
            "entry_ts": old.isoformat(),
        }
        current_price = 0.85  # -15% loss AND >72h
        result = mgr._check_exit_conditions(pos, current_price)
        assert result == "STOP_LOSS"  # stop-loss checked first

    def test_no_exit_when_price_slightly_up(self):
        """Slightly positive P&L within time window = hold."""
        mgr = self._make_mgr()
        now = datetime.now(timezone.utc) - timedelta(hours=5)
        pos = {
            "entry_price": 1.0,
            "peak_price": 1.05,
            "entry_ts": now.isoformat(),
        }
        current_price = 1.04
        result = mgr._check_exit_conditions(pos, current_price)
        assert result is None


class TestKillSwitch:
    """Tests for kill switch detection."""

    def test_kill_switch_detected(self, tmp_path):
        mgr = PortfolioManager.__new__(PortfolioManager)
        mgr._risk = MagicMock()
        mgr._risk.halted = False

        kill_file = tmp_path / "KILL_SWITCH"
        kill_file.touch()

        with patch.object(settings, "KILL_SWITCH_PATH", str(kill_file)):
            assert mgr.check_kill_switch() is True
            assert mgr._risk.halted is True

    def test_kill_switch_not_present(self, tmp_path):
        mgr = PortfolioManager.__new__(PortfolioManager)
        mgr._risk = MagicMock()
        mgr._risk.halted = False

        with patch.object(settings, "KILL_SWITCH_PATH", str(tmp_path / "NO_SUCH_FILE")):
            assert mgr.check_kill_switch() is False


class TestPortfolioStatus:
    """Test the status reporting method."""

    def test_status_structure(self):
        mgr = PortfolioManager.__new__(PortfolioManager)
        mgr._slots = [
            Slot(slot_id=0, status="ALPHA", netuid=5, position_id=1, amount_tao=2.5),
            Slot(slot_id=1, status="CASH"),
            Slot(slot_id=2, status="CASH"),
            Slot(slot_id=3, status="CASH"),
        ]
        from app.portfolio.manager import RiskState
        mgr._risk = RiskState(
            start_of_day_nav=10.0,
            current_nav=9.8,
            trades_today=3,
            halted=False,
        )

        status = mgr.status()
        assert len(status["slots"]) == 4
        assert status["slots"][0]["status"] == "ALPHA"
        assert status["slots"][0]["netuid"] == 5
        assert status["risk"]["trades_today"] == 3
        assert status["risk"]["halted"] is False
