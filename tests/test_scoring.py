"""
Tests for scoring normalization, value_band_boost, signal computation.
"""
import math
import pytest

from app.strategy.signals import (
    value_band_boost,
    trend_momentum_signal,
    support_resistance_signal,
    fibonacci_signal,
    volatility_breakout_signal,
    mean_reversion_signal,
)
from app.strategy.scoring import (
    normalize_signal,
    compute_signals,
    rank_subnets,
    select_entries,
    ScoredSubnet,
)


# ── normalize_signal ───────────────────────────────────────────────

class TestNormalizeSignal:
    def test_clamp_above_one(self):
        assert normalize_signal(1.5) == 1.0

    def test_clamp_below_zero(self):
        assert normalize_signal(-0.3) == 0.0

    def test_in_range(self):
        assert normalize_signal(0.5) == 0.5

    def test_boundary_zero(self):
        assert normalize_signal(0.0) == 0.0

    def test_boundary_one(self):
        assert normalize_signal(1.0) == 1.0


# ── value_band_boost ──────────────────────────────────────────────

class TestValueBandBoost:
    def test_inside_band(self):
        """Price inside [0.0035, 0.005] should return 1.0."""
        assert value_band_boost(0.004) == 1.0
        assert value_band_boost(0.0035) == 1.0
        assert value_band_boost(0.005) == 1.0

    def test_at_band_edges(self):
        assert value_band_boost(0.0035, 0.0035, 0.005) == 1.0
        assert value_band_boost(0.005, 0.0035, 0.005) == 1.0

    def test_outside_band_below(self):
        """Price below band should decay smoothly."""
        v = value_band_boost(0.002, 0.0035, 0.005, decay=0.001)
        assert 0 < v < 1.0

    def test_outside_band_above(self):
        """Price above band should decay smoothly."""
        v = value_band_boost(0.007, 0.0035, 0.005, decay=0.001)
        assert 0 < v < 1.0

    def test_far_outside_approaches_zero(self):
        """Very far from band should be near zero."""
        v = value_band_boost(0.02, 0.0035, 0.005, decay=0.001)
        assert v < 0.01

    def test_zero_price(self):
        assert value_band_boost(0.0) == 0.0

    def test_negative_price(self):
        assert value_band_boost(-1.0) == 0.0

    def test_decay_symmetry(self):
        """Equal distance below and above should give equal boost."""
        v_below = value_band_boost(0.003, 0.0035, 0.005, decay=0.001)
        v_above = value_band_boost(0.0055, 0.0035, 0.005, decay=0.001)
        assert abs(v_below - v_above) < 0.01  # approximately equal

    def test_smooth_decay(self):
        """Boost should decrease monotonically as price moves away from band."""
        v1 = value_band_boost(0.003, 0.0035, 0.005, decay=0.001)
        v2 = value_band_boost(0.002, 0.0035, 0.005, decay=0.001)
        v3 = value_band_boost(0.001, 0.0035, 0.005, decay=0.001)
        assert v1 > v2 > v3

    def test_gaussian_shape(self):
        """Verify the decay follows Gaussian shape."""
        band_low, band_high, decay = 0.0035, 0.005, 0.001
        distance = 0.001  # 1 decay width from band
        expected = math.exp(-0.5 * (distance / decay) ** 2)
        actual = value_band_boost(band_low - distance, band_low, band_high, decay)
        assert abs(actual - expected) < 0.01


# ── Signal functions return valid range ────────────────────────────

class TestSignalRanges:
    """All signals should return values in [0, 1]."""

    @pytest.fixture
    def uptrend_prices(self):
        """Generate an uptrend price series."""
        return [1.0 + i * 0.02 for i in range(100)]

    @pytest.fixture
    def downtrend_prices(self):
        """Generate a downtrend price series."""
        return [3.0 - i * 0.02 for i in range(100)]

    @pytest.fixture
    def flat_prices(self):
        """Flat price series."""
        return [1.0] * 100

    @pytest.fixture
    def volatile_prices(self):
        """Volatile oscillating price."""
        import math as m
        return [1.0 + 0.3 * m.sin(i * 0.5) for i in range(100)]

    def _assert_range(self, value: float):
        assert 0.0 <= value <= 1.0, f"Signal out of range: {value}"

    def test_trend_uptrend(self, uptrend_prices):
        v = trend_momentum_signal(uptrend_prices)
        self._assert_range(v)
        assert v > 0.5  # should be bullish

    def test_trend_downtrend(self, downtrend_prices):
        v = trend_momentum_signal(downtrend_prices)
        self._assert_range(v)
        assert v < 0.5  # should be bearish

    def test_trend_flat(self, flat_prices):
        v = trend_momentum_signal(flat_prices)
        self._assert_range(v)

    def test_trend_empty(self):
        v = trend_momentum_signal([])
        self._assert_range(v)
        assert v == 0.5  # neutral for empty

    def test_trend_short(self):
        v = trend_momentum_signal([1.0, 1.1])
        self._assert_range(v)

    def test_support_resistance(self, uptrend_prices):
        self._assert_range(support_resistance_signal(uptrend_prices))

    def test_support_resistance_short(self):
        self._assert_range(support_resistance_signal([1.0] * 3))

    def test_fibonacci(self, uptrend_prices):
        self._assert_range(fibonacci_signal(uptrend_prices))

    def test_fibonacci_short(self):
        self._assert_range(fibonacci_signal([1.0] * 5))

    def test_volatility(self, volatile_prices):
        self._assert_range(volatility_breakout_signal(volatile_prices))

    def test_volatility_flat(self, flat_prices):
        self._assert_range(volatility_breakout_signal(flat_prices))

    def test_mean_reversion(self, volatile_prices):
        self._assert_range(mean_reversion_signal(volatile_prices))

    def test_mean_reversion_short(self):
        self._assert_range(mean_reversion_signal([1.0] * 5))


# ── compute_signals ───────────────────────────────────────────────

class TestComputeSignals:
    def test_composite_in_range(self):
        prices = [1.0 + i * 0.01 for i in range(50)]
        signals = compute_signals(netuid=1, prices=prices, alpha_price=0.004)
        assert 0.0 <= signals.composite <= 1.0

    def test_all_signals_populated(self):
        prices = [1.0 + i * 0.01 for i in range(50)]
        signals = compute_signals(netuid=5, prices=prices, alpha_price=0.003)
        assert signals.netuid == 5
        assert 0.0 <= signals.trend <= 1.0
        assert 0.0 <= signals.support_resistance <= 1.0
        assert 0.0 <= signals.fibonacci <= 1.0
        assert 0.0 <= signals.volatility <= 1.0
        assert 0.0 <= signals.mean_reversion <= 1.0
        assert 0.0 <= signals.value_band <= 1.0

    def test_to_dict(self):
        prices = [1.0] * 30
        signals = compute_signals(netuid=10, prices=prices, alpha_price=0.004)
        d = signals.to_dict()
        assert "netuid" in d
        assert "composite" in d
        assert isinstance(d["composite"], float)


# ── rank_subnets ──────────────────────────────────────────────────

class TestRankSubnets:
    def test_ranking_order(self):
        """Subnets should be ranked by composite score descending."""
        data = [
            {"netuid": 1, "prices": [1.0 + i * 0.05 for i in range(50)], "alpha_price": 0.004},
            {"netuid": 2, "prices": [2.0 - i * 0.01 for i in range(50)], "alpha_price": 0.010},
            {"netuid": 3, "prices": [1.0 + i * 0.03 for i in range(50)], "alpha_price": 0.0042},
        ]
        ranked = rank_subnets(data)
        assert len(ranked) == 3
        assert ranked[0].rank == 1
        assert ranked[0].score >= ranked[1].score >= ranked[2].score

    def test_entry_eligibility(self):
        data = [
            {"netuid": 1, "prices": [1.0] * 50, "alpha_price": 0.004},
        ]
        ranked = rank_subnets(data, enter_threshold=0.0)
        assert bool(ranked[0].eligible_entry) is True

        ranked2 = rank_subnets(data, enter_threshold=2.0)
        assert bool(ranked2[0].eligible_entry) is False

    def test_high_conviction(self):
        data = [
            {"netuid": 1, "prices": [1.0 + i * 0.05 for i in range(80)], "alpha_price": 0.004},
        ]
        ranked = rank_subnets(data, high_conviction_threshold=0.0)
        assert bool(ranked[0].high_conviction) is True

    def test_empty_input(self):
        ranked = rank_subnets([])
        assert ranked == []


# ── select_entries ────────────────────────────────────────────────

class TestSelectEntries:
    def _make_scored(self, netuid: int, score: float, eligible: bool, hc: bool) -> ScoredSubnet:
        from app.strategy.scoring import SubnetSignals
        return ScoredSubnet(
            netuid=netuid,
            signals=SubnetSignals(netuid=netuid, composite=score),
            score=score,
            eligible_entry=eligible,
            high_conviction=hc,
        )

    def test_respects_slot_count(self):
        ranked = [
            self._make_scored(1, 0.9, True, False),
            self._make_scored(2, 0.8, True, False),
            self._make_scored(3, 0.7, True, False),
        ]
        selected = select_entries(ranked, available_slots=2)
        assert len(selected) == 2
        assert selected[0].netuid == 1
        assert selected[1].netuid == 2

    def test_skips_current_positions(self):
        ranked = [
            self._make_scored(1, 0.9, True, False),
            self._make_scored(2, 0.8, True, False),
        ]
        selected = select_entries(ranked, available_slots=2, current_positions={1})
        assert len(selected) == 1
        assert selected[0].netuid == 2

    def test_skips_cooldown(self):
        ranked = [
            self._make_scored(1, 0.9, True, False),
            self._make_scored(2, 0.8, True, False),
        ]
        selected = select_entries(ranked, available_slots=2, cooldown_netuids={1})
        assert len(selected) == 1
        assert selected[0].netuid == 2

    def test_skips_non_eligible(self):
        ranked = [
            self._make_scored(1, 0.9, False, False),
            self._make_scored(2, 0.8, True, False),
        ]
        selected = select_entries(ranked, available_slots=2)
        assert len(selected) == 1
        assert selected[0].netuid == 2

    def test_double_slot_high_conviction(self):
        ranked = [
            self._make_scored(1, 0.95, True, True),
            self._make_scored(2, 0.7, True, False),
        ]
        selected = select_entries(ranked, available_slots=4, allow_double=True)
        # First entry uses 2 slots (high conviction), second uses 1
        assert len(selected) == 2

    def test_double_slot_insufficient_slots(self):
        ranked = [
            self._make_scored(1, 0.95, True, True),
            self._make_scored(2, 0.7, True, False),
        ]
        # Only 1 slot available, can't double
        selected = select_entries(ranked, available_slots=1, allow_double=True)
        assert len(selected) == 1
