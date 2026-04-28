"""
Strategy parameter sets for backtesting.

Defines all configurations from the spec: production (A), alternative EMA (B),
alternative timeframes (C), filter ablation (D), and confirm sensitivity (E).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BacktestStrategyConfig:
    """Lightweight config for backtest — only parameters that affect signals/exits."""

    strategy_id: str
    tag: str
    fast_period: int
    slow_period: int
    confirm_bars: int
    candle_timeframe_hours: int = 4
    # Strategy dispatch: "ema" (default, dual-EMA) or "meanrev" (mean-reversion)
    strategy_type: str = "ema"
    # Risk management
    stop_loss_pct: float = 8.0
    take_profit_pct: float = 20.0
    trailing_stop_pct: float = 5.0
    breakeven_trigger_pct: float = 3.0
    trailing_stop_dynamic: bool = True
    max_holding_hours: int = 168
    cooldown_hours: float = 4.0
    # Position sizing
    pot_tao: float = 10.0
    position_size_pct: float = 0.20
    max_positions: int = 5
    max_entry_price_tao: float = 0.1
    # Entry filters
    bounce_enabled: bool = True
    mtf_enabled: bool = True
    mtf_lower_tf_hours: int = 1
    mtf_confirm_bars: int = 3
    rsi_filter_enabled: bool = False
    rsi_period: int = 14
    rsi_overbought: float = 75.0
    rsi_oversold: float = 25.0
    macd_filter_enabled: bool = False
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    bb_filter_enabled: bool = False
    bb_period: int = 20
    bb_upper_reject: float = 0.90
    momentum_filters_enabled: bool = True
    reject_day_and_week_negative_pct: float = 5.0
    reject_structural_decline_pct: float = 10.0
    min_pool_depth_tao: float = 3000.0
    parabolic_guard_mult: float = 1.5
    # Mean-reversion specific (used when strategy_type == "meanrev")
    rsi_entry: float = 30.0
    rsi_exit: float = 65.0
    bb_std: float = 2.0
    bb_mid_exit: bool = True


# ── A: Current Production Configs ─────────────────────────────────────

A1 = BacktestStrategyConfig(
    strategy_id="A1", tag="scalper", fast_period=3, slow_period=9, confirm_bars=3
)
A2 = BacktestStrategyConfig(
    strategy_id="A2", tag="trend", fast_period=3, slow_period=18, confirm_bars=3
)

# ── B: Alternative EMA Periods ────────────────────────────────────────

B1 = BacktestStrategyConfig(
    strategy_id="B1", tag="fast_cross", fast_period=5, slow_period=13, confirm_bars=2
)
B2 = BacktestStrategyConfig(
    strategy_id="B2", tag="classic", fast_period=8, slow_period=21, confirm_bars=3
)
B3 = BacktestStrategyConfig(
    strategy_id="B3", tag="macd_ema", fast_period=12, slow_period=26, confirm_bars=3
)
B4 = BacktestStrategyConfig(
    strategy_id="B4", tag="slow_trend", fast_period=9, slow_period=50, confirm_bars=4
)
B5 = BacktestStrategyConfig(
    strategy_id="B5", tag="micro", fast_period=2, slow_period=5, confirm_bars=2
)

# ── C: Alternative Timeframes ─────────────────────────────────────────

C1 = BacktestStrategyConfig(
    strategy_id="C1", tag="hourly", fast_period=3, slow_period=9, confirm_bars=3,
    candle_timeframe_hours=1,
)
C2 = BacktestStrategyConfig(
    strategy_id="C2", tag="2h", fast_period=3, slow_period=9, confirm_bars=3,
    candle_timeframe_hours=2,
)
C3 = BacktestStrategyConfig(
    strategy_id="C3", tag="8h", fast_period=3, slow_period=9, confirm_bars=3,
    candle_timeframe_hours=8,
)
C4 = BacktestStrategyConfig(
    strategy_id="C4", tag="daily", fast_period=3, slow_period=9, confirm_bars=2,
    candle_timeframe_hours=24,
)

# ── D: Filter Ablation (scalper 3/9 base) ─────────────────────────────

D1 = BacktestStrategyConfig(
    strategy_id="D1", tag="no_filters", fast_period=3, slow_period=9, confirm_bars=3,
    rsi_filter_enabled=False, macd_filter_enabled=False, bb_filter_enabled=False,
    momentum_filters_enabled=False, bounce_enabled=False, mtf_enabled=False,
)
D2 = BacktestStrategyConfig(
    strategy_id="D2", tag="rsi_only", fast_period=3, slow_period=9, confirm_bars=3,
    rsi_filter_enabled=True, macd_filter_enabled=False, bb_filter_enabled=False,
    momentum_filters_enabled=False, bounce_enabled=False, mtf_enabled=False,
)
D3 = BacktestStrategyConfig(
    strategy_id="D3", tag="macd_only", fast_period=3, slow_period=9, confirm_bars=3,
    rsi_filter_enabled=False, macd_filter_enabled=True, bb_filter_enabled=False,
    momentum_filters_enabled=False, bounce_enabled=False, mtf_enabled=False,
)
D4 = BacktestStrategyConfig(
    strategy_id="D4", tag="momentum_only", fast_period=3, slow_period=9, confirm_bars=3,
    rsi_filter_enabled=False, macd_filter_enabled=False, bb_filter_enabled=False,
    momentum_filters_enabled=True, bounce_enabled=False, mtf_enabled=False,
)
D5 = BacktestStrategyConfig(
    strategy_id="D5", tag="all_filters", fast_period=3, slow_period=9, confirm_bars=3,
    rsi_filter_enabled=True, macd_filter_enabled=True, bb_filter_enabled=True,
    momentum_filters_enabled=True, bounce_enabled=True, mtf_enabled=True,
)
D6 = BacktestStrategyConfig(
    strategy_id="D6", tag="tight_stops", fast_period=3, slow_period=9, confirm_bars=3,
    stop_loss_pct=5.0, take_profit_pct=15.0,
)
D7 = BacktestStrategyConfig(
    strategy_id="D7", tag="wide_stops", fast_period=3, slow_period=9, confirm_bars=3,
    stop_loss_pct=12.0, take_profit_pct=30.0,
)
D8 = BacktestStrategyConfig(
    strategy_id="D8", tag="no_bounce", fast_period=3, slow_period=9, confirm_bars=3,
    bounce_enabled=False,
)

# ── E: Confirm Bar Sensitivity ────────────────────────────────────────

E1 = BacktestStrategyConfig(
    strategy_id="E1", tag="confirm_1", fast_period=3, slow_period=9, confirm_bars=1
)
E2 = BacktestStrategyConfig(
    strategy_id="E2", tag="confirm_2", fast_period=3, slow_period=9, confirm_bars=2
)
E3 = BacktestStrategyConfig(
    strategy_id="E3", tag="confirm_4", fast_period=3, slow_period=9, confirm_bars=4
)
E4 = BacktestStrategyConfig(
    strategy_id="E4", tag="confirm_5", fast_period=3, slow_period=9, confirm_bars=5
)

# ── F: Mean-Reversion ─────────────────────────────────────────────────

F1 = BacktestStrategyConfig(
    strategy_id="F1", tag="meanrev", strategy_type="meanrev",
    fast_period=3, slow_period=9, confirm_bars=2,
    candle_timeframe_hours=1,
    stop_loss_pct=5.0, take_profit_pct=8.0,
    max_holding_hours=24, cooldown_hours=2.0,
    pot_tao=5.0, position_size_pct=0.25, max_positions=4,
    rsi_entry=30.0, rsi_exit=65.0, rsi_period=14,
    bb_period=20, bb_std=2.0, bb_mid_exit=True,
    min_pool_depth_tao=3000.0,
    # Disable EMA-specific filters for mean-reversion
    bounce_enabled=False, mtf_enabled=False,
    momentum_filters_enabled=False,
    rsi_filter_enabled=False, macd_filter_enabled=False, bb_filter_enabled=False,
    parabolic_guard_mult=99.0,
)

def _meanrev_variant(**overrides) -> BacktestStrategyConfig:
    base = F1.__dict__.copy()
    base.update(overrides)
    return BacktestStrategyConfig(**base)


F2 = _meanrev_variant(strategy_id="F2", tag="meanrev_loose", rsi_entry=35.0, bb_std=2.0)
F3 = _meanrev_variant(strategy_id="F3", tag="meanrev_tight", rsi_entry=25.0, bb_std=2.5)
F4 = _meanrev_variant(strategy_id="F4", tag="meanrev_4h", candle_timeframe_hours=4)
F5 = _meanrev_variant(
    strategy_id="F5", tag="meanrev_longhold",
    max_holding_hours=72, take_profit_pct=12.0,
)
F6 = _meanrev_variant(strategy_id="F6", tag="meanrev_tight_stop", stop_loss_pct=3.0)
F7 = _meanrev_variant(
    strategy_id="F7", tag="meanrev_wide_stop",
    stop_loss_pct=8.0, take_profit_pct=12.0,
)
F8 = _meanrev_variant(strategy_id="F8", tag="meanrev_no_bbmid", bb_mid_exit=False)

MEAN_REVERSION = [F1, F2, F3, F4, F5, F6, F7, F8]

# ── Strategy Groups ───────────────────────────────────────────────────

PRODUCTION = [A1, A2]
ALT_EMA = [B1, B2, B3, B4, B5]
ALT_TIMEFRAME = [C1, C2, C3, C4]
FILTER_ABLATION = [D1, D2, D3, D4, D5, D6, D7, D8]
CONFIRM_SENSITIVITY = [E1, E2, E3, E4]

ALL_STRATEGIES = (
    PRODUCTION
    + ALT_EMA
    + ALT_TIMEFRAME
    + FILTER_ABLATION
    + CONFIRM_SENSITIVITY
    + MEAN_REVERSION
)

STRATEGY_MAP = {s.strategy_id: s for s in ALL_STRATEGIES}

LOOKBACK_WINDOWS = [7, 14, 30, 90, 120, 150]


def get_strategy(strategy_id: str) -> BacktestStrategyConfig | None:
    return STRATEGY_MAP.get(strategy_id)
