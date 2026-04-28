"""
Central configuration for the EMA-only trading bot.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


@dataclass
class StrategyConfig:
    """Immutable config bundle for a single EMA strategy instance."""
    tag: str
    fast_period: int
    slow_period: int
    confirm_bars: int
    pot_tao: float
    position_size_pct: float
    max_positions: int
    stop_loss_pct: float
    take_profit_pct: float
    trailing_stop_pct: float
    breakeven_trigger_pct: float
    max_holding_hours: int
    cooldown_hours: float
    bounce_enabled: bool
    bounce_touch_tolerance_pct: float
    bounce_require_green: bool
    max_gini: float
    gini_cache_ttl_sec: int
    gini_api_fallback: bool
    gini_pool_delta_threshold: float
    gini_prefetch_top_n: int
    correlation_threshold: float
    candle_timeframe_hours: int
    dry_run: bool
    max_slippage_pct: float
    max_entry_price_tao: float
    drawdown_breaker_pct: float
    drawdown_pause_hours: float
    fee_reserve_tao: float
    mtf_enabled: bool
    mtf_lower_tf_hours: int
    mtf_confirm_bars: int
    vol_sizing_enabled: bool
    vol_target_risk: float
    vol_floor: float
    vol_cap: float
    vol_min_size_pct: float
    vol_max_size_pct: float
    vol_window: int
    vol_scoring_penalty_at: float
    post_exit_verify: bool
    post_exit_verify_delay_sec: int
    post_exit_max_retries: int
    post_exit_alpha_threshold: float
    # Indicator filters
    rsi_filter_enabled: bool
    rsi_period: int
    rsi_overbought: float
    rsi_oversold: float
    macd_filter_enabled: bool
    macd_fast: int
    macd_slow: int
    macd_signal: int
    bb_filter_enabled: bool
    bb_period: int
    bb_upper_reject: float
    min_pool_depth_tao: float
    parabolic_guard_mult: float
    entry_price_drift_pct: float
    # Flow reversal exit
    flow_reversal_exit_enabled: bool
    flow_reversal_consecutive: int
    flow_reversal_min_outflow_pct: float
    # Momentum pre-filters
    momentum_filters_enabled: bool
    reject_day_and_week_negative_pct: float
    reject_structural_decline_pct: float
    trailing_stop_dynamic: bool
    atr_period: int
    atr_multiplier: float
    trailing_min_pct: float
    trailing_max_pct: float
    # Hybrid time exit (partial scale-out)
    partial_exit_hours: int
    partial_exit_pct: float
    final_time_stop_hours: int
    partial_trailing_tighten: float
    # Mean-reversion specific (defaults keep existing EMA configs unchanged)
    strategy_type: str = "ema"
    bb_std: float = 2.0
    bb_mid_exit: bool = False
    # HTF (higher-timeframe) confirmation — used when Taostats history resolution
    # is coarser than the strategy's live candle TF (today: daily vs. 2h/1h).
    htf_fast_period: int = 5
    htf_slow_period: int = 20
    htf_confirm_bars: int = 2


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    SUBTENSOR_NETWORK: str = "wss://entrypoint-finney.opentensor.ai:443"

    TAOSTATS_API_KEY: str = ""
    TAOSTATS_BASE_URL: str = "https://api.taostats.io"
    TAOSTATS_RATE_LIMIT_PER_MIN: int = 30
    TAOSTATS_CACHE_TTL_SEC: int = 60

    PREFERRED_VALIDATORS: list[str] = [
        "5GKH9FPPnWSUoeeTJp19wVtd84XqFW4pyK2ijV2GsFbhTrP1",
        "5F4tQyWrhfGVcNhoqeiNsR6KjD4wMZ2kfhLj4oHYuyHbZAc3",
        "5Hddm3iBFD2GLT5ik7LZnT3XJUnRnN8PoeCFgGQgawUVKNm8",
    ]

    BT_WALLET_NAME: str = "default"
    BT_WALLET_HOTKEY: str = "default"
    BT_WALLET_PATH: str = str(Path.home() / ".bittensor" / "wallets")
    BT_WALLET_PASSWORD: str = ""

    SCAN_INTERVAL_MIN: int = 5
    MAX_ENTRY_PRICE_TAO: float = 0.1
    MAX_SLIPPAGE_PCT: float = 5.0
    FEE_RESERVE_TAO: float = 0.5

    # Strategy A — "Mean-Reversion" (replaces Scalper)
    MR_ENABLED: bool = True
    MR_STRATEGY_TAG: str = "meanrev"
    MR_POT_TAO: float = 5.0
    # Pot sizing mode — "fixed" (use MR_POT_TAO/EMA_B_POT_TAO literally) or
    # "wallet_split" (compute pots from live wallet balance minus fee reserve).
    EMA_POT_MODE: str = "fixed"
    EMA_FEE_RESERVE_TAO: float = 1.0
    # wallet_split weights across the three strategies. Disabled strategies
    # drop out and the remainder is renormalized, so these need not sum to 1.
    EMA_MEANREV_WEIGHT: float = 0.25
    EMA_TREND_WEIGHT: float = 0.50
    EMA_FLOW_WEIGHT: float = 0.25
    MR_POSITION_SIZE_PCT: float = 0.25
    MR_MAX_POSITIONS: int = 4
    MR_STOP_LOSS_PCT: float = 5.0
    MR_TAKE_PROFIT_PCT: float = 8.0
    MR_MAX_HOLDING_HOURS: int = 24
    MR_COOLDOWN_HOURS: float = 2.0
    MR_CANDLE_TIMEFRAME_HOURS: int = 1
    MR_DRAWDOWN_BREAKER_PCT: float = 15.0
    MR_DRAWDOWN_PAUSE_HOURS: float = 6.0
    MR_CORRELATION_THRESHOLD: float = 0.80
    MR_MAX_GINI: float = 0.82
    MR_MIN_POOL_DEPTH_TAO: float = 3000.0
    # Mean-reversion signal parameters
    MR_RSI_ENTRY: float = 30.0
    MR_RSI_EXIT: float = 65.0
    MR_RSI_PERIOD: int = 14
    MR_BB_PERIOD: int = 20
    MR_BB_STD: float = 2.0
    MR_BB_MID_EXIT: bool = True
    # Vol-sizing
    MR_VOL_SIZING_ENABLED: bool = True
    MR_VOL_TARGET_RISK: float = 0.02
    MR_VOL_FLOOR: float = 0.10
    MR_VOL_CAP: float = 1.50
    MR_VOL_MIN_SIZE_PCT: float = 0.15
    MR_VOL_MAX_SIZE_PCT: float = 0.40
    MR_VOL_WINDOW: int = 24
    # Shared exit watcher / entry watcher settings
    EMA_EXIT_WATCHER_ENABLED: bool = True
    EMA_EXIT_WATCHER_SEC: int = 15
    EMA_ENTRY_WATCHER_ENABLED: bool = True
    EMA_ENTRY_WATCHER_SEC: int = 90
    EMA_GINI_CACHE_TTL_SEC: int = 1800
    EMA_GINI_API_FALLBACK: bool = True
    EMA_GINI_POOL_DELTA_THRESHOLD: float = 0.15
    EMA_GINI_PREFETCH_TOP_N: int = 10

    # Post-exit verification polling
    EMA_POST_EXIT_VERIFY: bool = True
    EMA_POST_EXIT_VERIFY_DELAY_SEC: int = 30
    EMA_POST_EXIT_MAX_RETRIES: int = 3
    EMA_POST_EXIT_ALPHA_THRESHOLD: float = 0.001

    # Strategy B — "Trend" (fast=3, slow=18)
    EMA_B_ENABLED: bool = True
    EMA_B_DRY_RUN: bool = True
    EMA_B_STRATEGY_TAG: str = "trend"
    EMA_B_PERIOD: int = 18
    EMA_B_FAST_PERIOD: int = 3
    EMA_B_CONFIRM_BARS: int = 2
    EMA_B_POT_TAO: float = 5.0
    EMA_B_POSITION_SIZE_PCT: float = 0.33
    EMA_B_MAX_POSITIONS: int = 3
    EMA_B_STOP_LOSS_PCT: float = 8.0
    EMA_B_TAKE_PROFIT_PCT: float = 20.0
    EMA_B_MAX_HOLDING_HOURS: int = 168
    EMA_B_COOLDOWN_HOURS: float = 4.0
    EMA_B_DRAWDOWN_BREAKER_PCT: float = 15.0
    EMA_B_DRAWDOWN_PAUSE_HOURS: float = 6.0
    EMA_B_CORRELATION_THRESHOLD: float = 0.80
    EMA_B_CANDLE_TIMEFRAME_HOURS: int = 4
    EMA_B_BREAKEVEN_TRIGGER_PCT: float = 3.0
    EMA_B_TRAILING_STOP_PCT: float = 5.0
    EMA_B_TRAILING_STOP_DYNAMIC: bool = True
    EMA_B_ATR_PERIOD: int = 14
    EMA_B_ATR_MULTIPLIER: float = 2.0
    EMA_B_TRAILING_MIN_PCT: float = 3.0
    EMA_B_TRAILING_MAX_PCT: float = 15.0
    EMA_B_PARTIAL_EXIT_HOURS: int = 120
    EMA_B_PARTIAL_EXIT_PCT: float = 0.50
    EMA_B_FINAL_TIME_STOP_HOURS: int = 168
    EMA_B_PARTIAL_TRAILING_TIGHTEN: float = 0.60
    EMA_B_BOUNCE_ENABLED: bool = True
    EMA_B_BOUNCE_TOUCH_TOLERANCE_PCT: float = 1.0
    EMA_B_BOUNCE_REQUIRE_GREEN: bool = True
    EMA_B_MAX_GINI: float = 0.82
    EMA_B_MTF_ENABLED: bool = True
    EMA_B_MTF_LOWER_TF_HOURS: int = 1
    EMA_B_MTF_CONFIRM_BARS: int = 2
    EMA_B_VOL_SIZING_ENABLED: bool = True
    EMA_B_VOL_TARGET_RISK: float = 0.02
    EMA_B_VOL_FLOOR: float = 0.10
    EMA_B_VOL_CAP: float = 1.50
    EMA_B_VOL_MIN_SIZE_PCT: float = 0.10
    EMA_B_VOL_MAX_SIZE_PCT: float = 0.40
    EMA_B_VOL_WINDOW: int = 24
    EMA_B_VOL_SCORING_PENALTY_AT: float = 1.0

    # Strategy B indicator filters
    EMA_B_RSI_FILTER_ENABLED: bool = False
    EMA_B_RSI_PERIOD: int = 14
    EMA_B_RSI_OVERBOUGHT: float = 75.0
    EMA_B_RSI_OVERSOLD: float = 25.0
    EMA_B_MACD_FILTER_ENABLED: bool = False
    EMA_B_MACD_FAST: int = 12
    EMA_B_MACD_SLOW: int = 26
    EMA_B_MACD_SIGNAL: int = 9
    EMA_B_BB_FILTER_ENABLED: bool = False
    EMA_B_BB_PERIOD: int = 20
    EMA_B_BB_UPPER_REJECT: float = 0.90
    EMA_B_MIN_POOL_DEPTH_TAO: float = 3000.0
    EMA_B_PARABOLIC_GUARD_MULT: float = 1.5
    EMA_B_ENTRY_PRICE_DRIFT_PCT: float = 5.0
    EMA_B_MOMENTUM_FILTERS_ENABLED: bool = True
    EMA_B_REJECT_DAY_AND_WEEK_NEGATIVE_PCT: float = 5.0
    EMA_B_REJECT_STRUCTURAL_DECLINE_PCT: float = 10.0

    # Strategy B flow reversal exit
    EMA_B_FLOW_REVERSAL_EXIT_ENABLED: bool = True
    EMA_B_FLOW_REVERSAL_CONSECUTIVE: int = 3
    EMA_B_FLOW_REVERSAL_MIN_OUTFLOW_PCT: float = 1.0

    # Per-subnet history (deep lookback for EMA warmup & entry confirmation)
    SUBNET_HISTORY_ENABLED: bool = True
    SUBNET_HISTORY_INTERVAL: str = "1h"
    SUBNET_HISTORY_LIMIT: int = 336          # 14 days at 1h (upstream currently returns ~daily regardless)
    SUBNET_HISTORY_CACHE_TTL_SEC: int = 300  # 5 min per-subnet cache
    SUBNET_HISTORY_ON_ENTRY: bool = True     # fetch deep history before entering
    SUBNET_HISTORY_ON_STARTUP: bool = True   # warm up open positions on restart

    # HTF (higher-timeframe) confirmation gate — adapts to whatever native
    # resolution the Taostats subnet-history endpoint returns. Kept behind a
    # flag so the old (strict crossover) path is recoverable for one release.
    EMA_HTF_CONFIRM_ENABLED: bool = True
    EMA_B_HTF_FAST_PERIOD: int = 5      # ~5 days at daily resolution
    EMA_B_HTF_SLOW_PERIOD: int = 20     # ~4 weeks at daily resolution
    EMA_B_HTF_CONFIRM_BARS: int = 2
    MR_HTF_FAST_PERIOD: int = 5
    MR_HTF_SLOW_PERIOD: int = 20
    MR_HTF_CONFIRM_BARS: int = 2

    # Just-in-time pool refresh before trades (live slippage estimation)
    EMA_FRESH_POOL_ON_TRADE: bool = True
    EMA_PRE_TRADE_MAX_SLIPPAGE_PCT: float = 4.0

    # ── Pool Flow Momentum (v2) ─────────────────────────────────
    # Master switches
    FLOW_ENABLED: bool = False
    FLOW_DRY_RUN: bool = True
    FLOW_STRATEGY_TAG: str = "flow"
    FLOW_PERSIST_SNAPSHOTS: bool = True
    FLOW_SNAPSHOT_RETENTION_HOURS: int = 336  # 14 days
    # Slots & sizing
    FLOW_SLOTS: int = 3
    FLOW_POT_TAO: float = 10.0
    FLOW_POSITION_SIZE_PCT: float = 0.33
    FLOW_MAX_POOL_IMPACT_PCT: float = 1.5
    FLOW_MIN_POOL_DEPTH_TAO: float = 5000.0
    FLOW_MAX_GINI: float = 0.82
    FLOW_CORRELATION_THRESHOLD: float = 0.80
    # Signal thresholds
    FLOW_Z_ENTRY: float = 2.0
    FLOW_Z_EXIT: float = -1.5
    FLOW_MIN_TAO_PCT: float = 2.0
    FLOW_EXIT_PCT: float = 0.5
    FLOW_MAGNITUDE_CAP: float = 10.0
    # Windows (in snapshots)
    FLOW_WINDOW_1H_SNAPS: int = 12
    FLOW_WINDOW_4H_SNAPS: int = 48
    FLOW_BASELINE_SNAPS: int = 576
    FLOW_COLD_START_SNAPS: int = 624
    FLOW_MAX_GAP_MIN: int = 30
    # Emission normalization
    FLOW_EMISSION_ADJUST: bool = True
    FLOW_EMISSION_SOLD_FRACTION: float = 0.60
    # Risk / exits
    FLOW_STOP_LOSS_PCT: float = 6.0
    FLOW_TAKE_PROFIT_PCT: float = 12.0
    FLOW_TRAILING_PCT: float = 4.0
    FLOW_TRAILING_TRIGGER_PCT: float = 3.0
    FLOW_TIME_SOFT_HOURS: int = 6
    FLOW_TIME_HARD_HOURS: int = 24
    FLOW_REQUIRE_EMA_CONFIRM: bool = True
    FLOW_EMA_FAST_PERIOD: int = 5
    FLOW_EMA_SLOW_PERIOD: int = 18
    FLOW_CANDLE_TIMEFRAME_HOURS: int = 4
    FLOW_DRAWDOWN_BREAKER_PCT: float = 15.0
    FLOW_DRAWDOWN_PAUSE_HOURS: float = 6.0
    # Cooldowns (asymmetric)
    FLOW_COOLDOWN_STOP_HOURS: float = 12.0
    FLOW_COOLDOWN_WIN_HOURS: float = 4.0
    FLOW_COOLDOWN_TIME_HOURS: float = 6.0
    # Regime
    FLOW_REGIME_FILTER_ENABLED: bool = True
    FLOW_REGIME_INDEX_THRESHOLD: float = 0.95
    FLOW_REGIME_LOOKBACK_SNAPS: int = 288

    # ── Volatility Regime Filter (meta-strategy) ───────────────────
    # See specs/strategy-volatility-regime-filter.md and
    # specs/NewSpecs/implement-regime-classifier.md for full design.
    REGIME_ENABLED: bool = True
    REGIME_VOL_WINDOW_HOURS: int = 24
    REGIME_VOL_TREND_THRESHOLD: float = 0.30
    REGIME_VOL_CHOP_FLOOR: float = 0.10
    REGIME_VOL_DEAD_THRESHOLD: float = 0.05
    REGIME_DIR_THRESHOLD: float = 0.02
    REGIME_DISP_THRESHOLD: float = 0.015
    REGIME_DEBOUNCE_CYCLES: int = 2
    REGIME_REFRESH_SECONDS: int = 60
    REGIME_MIN_SUBNETS: int = 10
    REGIME_MIN_BUCKETS: int = 6
    REGIME_BUCKET_HOURS: int = 4
    REGIME_GATE_EMA: str = "trending,dispersed"
    REGIME_GATE_FLOW: str = "dispersed"
    REGIME_GATE_MR: str = "choppy,dispersed"
    REGIME_GATE_YIELD: str = "all"

    # SSE live price feed
    PRICE_FEED_INTERVAL_SEC: int = 30
    PRICE_FEED_MAX_CONNECTIONS: int = 5

    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""

    LOG_LEVEL: str = "INFO"
    LOG_RETENTION_DAYS: int = 3
    DB_PATH: str = "data/ledger.db"
    JSONL_DIR: str = "data/logs"
    HEALTH_PORT: int = 8081
    KILL_SWITCH_PATH: str = "./KILL_SWITCH"

    @field_validator("LOG_LEVEL")
    @classmethod
    def validate_log_level(cls, value: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        normalized = value.upper()
        if normalized not in allowed:
            raise ValueError(f"LOG_LEVEL must be one of {allowed}")
        return normalized


settings = Settings()


def meanrev_config() -> StrategyConfig:
    """Build StrategyConfig for the Mean-Reversion strategy from settings."""
    return StrategyConfig(
        tag=settings.MR_STRATEGY_TAG,
        strategy_type="meanrev",
        # EMA fields are unused for mean-reversion entry but required by dataclass;
        # set to harmless defaults so shared code (e.g. scoring) doesn't break.
        fast_period=3,
        slow_period=9,
        confirm_bars=2,
        pot_tao=settings.MR_POT_TAO,
        position_size_pct=settings.MR_POSITION_SIZE_PCT,
        max_positions=settings.MR_MAX_POSITIONS,
        stop_loss_pct=settings.MR_STOP_LOSS_PCT,
        take_profit_pct=settings.MR_TAKE_PROFIT_PCT,
        # No trailing stop for mean-reversion
        trailing_stop_pct=0.0,
        trailing_stop_dynamic=False,
        atr_period=14,
        atr_multiplier=2.0,
        trailing_min_pct=0.0,
        trailing_max_pct=0.0,
        # No partial time exit
        partial_exit_hours=9999,
        partial_exit_pct=0.0,
        final_time_stop_hours=settings.MR_MAX_HOLDING_HOURS,
        partial_trailing_tighten=1.0,
        breakeven_trigger_pct=0.0,
        max_holding_hours=settings.MR_MAX_HOLDING_HOURS,
        cooldown_hours=settings.MR_COOLDOWN_HOURS,
        # Bounce / MTF not used for mean-reversion
        bounce_enabled=False,
        bounce_touch_tolerance_pct=0.0,
        bounce_require_green=False,
        max_gini=settings.MR_MAX_GINI,
        gini_cache_ttl_sec=settings.EMA_GINI_CACHE_TTL_SEC,
        gini_api_fallback=settings.EMA_GINI_API_FALLBACK,
        gini_pool_delta_threshold=settings.EMA_GINI_POOL_DELTA_THRESHOLD,
        gini_prefetch_top_n=settings.EMA_GINI_PREFETCH_TOP_N,
        correlation_threshold=settings.MR_CORRELATION_THRESHOLD,
        candle_timeframe_hours=settings.MR_CANDLE_TIMEFRAME_HOURS,
        dry_run=False,  # no dry_run for mean-reversion per spec
        max_slippage_pct=settings.MAX_SLIPPAGE_PCT,
        max_entry_price_tao=settings.MAX_ENTRY_PRICE_TAO,
        drawdown_breaker_pct=settings.MR_DRAWDOWN_BREAKER_PCT,
        drawdown_pause_hours=settings.MR_DRAWDOWN_PAUSE_HOURS,
        fee_reserve_tao=settings.FEE_RESERVE_TAO,
        mtf_enabled=False,
        mtf_lower_tf_hours=1,
        mtf_confirm_bars=2,
        vol_sizing_enabled=settings.MR_VOL_SIZING_ENABLED,
        vol_target_risk=settings.MR_VOL_TARGET_RISK,
        vol_floor=settings.MR_VOL_FLOOR,
        vol_cap=settings.MR_VOL_CAP,
        vol_min_size_pct=settings.MR_VOL_MIN_SIZE_PCT,
        vol_max_size_pct=settings.MR_VOL_MAX_SIZE_PCT,
        vol_window=settings.MR_VOL_WINDOW,
        vol_scoring_penalty_at=1.0,
        post_exit_verify=settings.EMA_POST_EXIT_VERIFY,
        post_exit_verify_delay_sec=settings.EMA_POST_EXIT_VERIFY_DELAY_SEC,
        post_exit_max_retries=settings.EMA_POST_EXIT_MAX_RETRIES,
        post_exit_alpha_threshold=settings.EMA_POST_EXIT_ALPHA_THRESHOLD,
        # RSI used as primary signal for mean-reversion (not optional filter)
        rsi_filter_enabled=False,
        rsi_period=settings.MR_RSI_PERIOD,
        rsi_overbought=settings.MR_RSI_EXIT,
        rsi_oversold=settings.MR_RSI_ENTRY,
        macd_filter_enabled=False,
        macd_fast=12,
        macd_slow=26,
        macd_signal=9,
        bb_filter_enabled=False,
        bb_period=settings.MR_BB_PERIOD,
        bb_upper_reject=0.90,
        bb_std=settings.MR_BB_STD,
        bb_mid_exit=settings.MR_BB_MID_EXIT,
        htf_fast_period=settings.MR_HTF_FAST_PERIOD,
        htf_slow_period=settings.MR_HTF_SLOW_PERIOD,
        htf_confirm_bars=settings.MR_HTF_CONFIRM_BARS,
        min_pool_depth_tao=settings.MR_MIN_POOL_DEPTH_TAO,
        parabolic_guard_mult=99.0,  # disabled for mean-reversion
        entry_price_drift_pct=5.0,
        momentum_filters_enabled=False,
        reject_day_and_week_negative_pct=0.0,
        reject_structural_decline_pct=0.0,
        # No flow reversal for mean-reversion
        flow_reversal_exit_enabled=False,
        flow_reversal_consecutive=3,
        flow_reversal_min_outflow_pct=1.0,
    )


def strategy_b_config() -> StrategyConfig:
    """Build StrategyConfig for Strategy B (Trend) from settings."""
    return StrategyConfig(
        tag=settings.EMA_B_STRATEGY_TAG,
        fast_period=settings.EMA_B_FAST_PERIOD,
        slow_period=settings.EMA_B_PERIOD,
        confirm_bars=settings.EMA_B_CONFIRM_BARS,
        pot_tao=settings.EMA_B_POT_TAO,
        position_size_pct=settings.EMA_B_POSITION_SIZE_PCT,
        max_positions=settings.EMA_B_MAX_POSITIONS,
        stop_loss_pct=settings.EMA_B_STOP_LOSS_PCT,
        take_profit_pct=settings.EMA_B_TAKE_PROFIT_PCT,
        trailing_stop_pct=settings.EMA_B_TRAILING_STOP_PCT,
        trailing_stop_dynamic=settings.EMA_B_TRAILING_STOP_DYNAMIC,
        atr_period=settings.EMA_B_ATR_PERIOD,
        atr_multiplier=settings.EMA_B_ATR_MULTIPLIER,
        trailing_min_pct=settings.EMA_B_TRAILING_MIN_PCT,
        trailing_max_pct=settings.EMA_B_TRAILING_MAX_PCT,
        partial_exit_hours=settings.EMA_B_PARTIAL_EXIT_HOURS,
        partial_exit_pct=settings.EMA_B_PARTIAL_EXIT_PCT,
        final_time_stop_hours=settings.EMA_B_FINAL_TIME_STOP_HOURS,
        partial_trailing_tighten=settings.EMA_B_PARTIAL_TRAILING_TIGHTEN,
        breakeven_trigger_pct=settings.EMA_B_BREAKEVEN_TRIGGER_PCT,
        max_holding_hours=settings.EMA_B_MAX_HOLDING_HOURS,
        cooldown_hours=settings.EMA_B_COOLDOWN_HOURS,
        bounce_enabled=settings.EMA_B_BOUNCE_ENABLED,
        bounce_touch_tolerance_pct=settings.EMA_B_BOUNCE_TOUCH_TOLERANCE_PCT,
        bounce_require_green=settings.EMA_B_BOUNCE_REQUIRE_GREEN,
        max_gini=settings.EMA_B_MAX_GINI,
        gini_cache_ttl_sec=settings.EMA_GINI_CACHE_TTL_SEC,
        gini_api_fallback=settings.EMA_GINI_API_FALLBACK,
        gini_pool_delta_threshold=settings.EMA_GINI_POOL_DELTA_THRESHOLD,
        gini_prefetch_top_n=settings.EMA_GINI_PREFETCH_TOP_N,
        correlation_threshold=settings.EMA_B_CORRELATION_THRESHOLD,
        candle_timeframe_hours=settings.EMA_B_CANDLE_TIMEFRAME_HOURS,
        dry_run=settings.EMA_B_DRY_RUN,
        max_slippage_pct=settings.MAX_SLIPPAGE_PCT,
        max_entry_price_tao=settings.MAX_ENTRY_PRICE_TAO,
        drawdown_breaker_pct=settings.EMA_B_DRAWDOWN_BREAKER_PCT,
        drawdown_pause_hours=settings.EMA_B_DRAWDOWN_PAUSE_HOURS,
        fee_reserve_tao=settings.FEE_RESERVE_TAO,
        mtf_enabled=settings.EMA_B_MTF_ENABLED,
        mtf_lower_tf_hours=settings.EMA_B_MTF_LOWER_TF_HOURS,
        mtf_confirm_bars=settings.EMA_B_MTF_CONFIRM_BARS,
        vol_sizing_enabled=settings.EMA_B_VOL_SIZING_ENABLED,
        vol_target_risk=settings.EMA_B_VOL_TARGET_RISK,
        vol_floor=settings.EMA_B_VOL_FLOOR,
        vol_cap=settings.EMA_B_VOL_CAP,
        vol_min_size_pct=settings.EMA_B_VOL_MIN_SIZE_PCT,
        vol_max_size_pct=settings.EMA_B_VOL_MAX_SIZE_PCT,
        vol_window=settings.EMA_B_VOL_WINDOW,
        vol_scoring_penalty_at=settings.EMA_B_VOL_SCORING_PENALTY_AT,
        post_exit_verify=settings.EMA_POST_EXIT_VERIFY,
        post_exit_verify_delay_sec=settings.EMA_POST_EXIT_VERIFY_DELAY_SEC,
        post_exit_max_retries=settings.EMA_POST_EXIT_MAX_RETRIES,
        post_exit_alpha_threshold=settings.EMA_POST_EXIT_ALPHA_THRESHOLD,
        rsi_filter_enabled=settings.EMA_B_RSI_FILTER_ENABLED,
        rsi_period=settings.EMA_B_RSI_PERIOD,
        rsi_overbought=settings.EMA_B_RSI_OVERBOUGHT,
        rsi_oversold=settings.EMA_B_RSI_OVERSOLD,
        macd_filter_enabled=settings.EMA_B_MACD_FILTER_ENABLED,
        macd_fast=settings.EMA_B_MACD_FAST,
        macd_slow=settings.EMA_B_MACD_SLOW,
        macd_signal=settings.EMA_B_MACD_SIGNAL,
        bb_filter_enabled=settings.EMA_B_BB_FILTER_ENABLED,
        bb_period=settings.EMA_B_BB_PERIOD,
        bb_upper_reject=settings.EMA_B_BB_UPPER_REJECT,
        min_pool_depth_tao=settings.EMA_B_MIN_POOL_DEPTH_TAO,
        parabolic_guard_mult=settings.EMA_B_PARABOLIC_GUARD_MULT,
        entry_price_drift_pct=settings.EMA_B_ENTRY_PRICE_DRIFT_PCT,
        momentum_filters_enabled=settings.EMA_B_MOMENTUM_FILTERS_ENABLED,
        reject_day_and_week_negative_pct=settings.EMA_B_REJECT_DAY_AND_WEEK_NEGATIVE_PCT,
        reject_structural_decline_pct=settings.EMA_B_REJECT_STRUCTURAL_DECLINE_PCT,
        flow_reversal_exit_enabled=settings.EMA_B_FLOW_REVERSAL_EXIT_ENABLED,
        flow_reversal_consecutive=settings.EMA_B_FLOW_REVERSAL_CONSECUTIVE,
        flow_reversal_min_outflow_pct=settings.EMA_B_FLOW_REVERSAL_MIN_OUTFLOW_PCT,
        htf_fast_period=settings.EMA_B_HTF_FAST_PERIOD,
        htf_slow_period=settings.EMA_B_HTF_SLOW_PERIOD,
        htf_confirm_bars=settings.EMA_B_HTF_CONFIRM_BARS,
    )


def flow_config() -> StrategyConfig:
    """Build StrategyConfig for the Pool Flow Momentum strategy.

    Reuses the shared StrategyConfig dataclass so slot management, exit
    watcher plumbing, etc. work unchanged. Flow-specific thresholds live on
    ``settings`` and are read by the flow signal path directly.
    """
    return StrategyConfig(
        tag=settings.FLOW_STRATEGY_TAG,
        strategy_type="flow",
        # EMA trend-confirmation gate uses these
        fast_period=settings.FLOW_EMA_FAST_PERIOD,
        slow_period=settings.FLOW_EMA_SLOW_PERIOD,
        confirm_bars=1,
        pot_tao=settings.FLOW_POT_TAO,
        position_size_pct=settings.FLOW_POSITION_SIZE_PCT,
        max_positions=settings.FLOW_SLOTS,
        stop_loss_pct=settings.FLOW_STOP_LOSS_PCT,
        take_profit_pct=settings.FLOW_TAKE_PROFIT_PCT,
        trailing_stop_pct=settings.FLOW_TRAILING_PCT,
        trailing_stop_dynamic=False,
        atr_period=14,
        atr_multiplier=2.0,
        trailing_min_pct=settings.FLOW_TRAILING_PCT,
        trailing_max_pct=settings.FLOW_TRAILING_PCT,
        partial_exit_hours=settings.FLOW_TIME_SOFT_HOURS,
        partial_exit_pct=0.50,
        final_time_stop_hours=settings.FLOW_TIME_HARD_HOURS,
        partial_trailing_tighten=0.50,
        breakeven_trigger_pct=settings.FLOW_TRAILING_TRIGGER_PCT,
        max_holding_hours=settings.FLOW_TIME_HARD_HOURS,
        cooldown_hours=settings.FLOW_COOLDOWN_TIME_HOURS,
        bounce_enabled=False,
        bounce_touch_tolerance_pct=0.0,
        bounce_require_green=False,
        max_gini=settings.FLOW_MAX_GINI,
        gini_cache_ttl_sec=settings.EMA_GINI_CACHE_TTL_SEC,
        gini_api_fallback=settings.EMA_GINI_API_FALLBACK,
        gini_pool_delta_threshold=settings.EMA_GINI_POOL_DELTA_THRESHOLD,
        gini_prefetch_top_n=settings.EMA_GINI_PREFETCH_TOP_N,
        correlation_threshold=settings.FLOW_CORRELATION_THRESHOLD,
        candle_timeframe_hours=settings.FLOW_CANDLE_TIMEFRAME_HOURS,
        dry_run=settings.FLOW_DRY_RUN,
        max_slippage_pct=settings.MAX_SLIPPAGE_PCT,
        max_entry_price_tao=settings.MAX_ENTRY_PRICE_TAO,
        drawdown_breaker_pct=settings.FLOW_DRAWDOWN_BREAKER_PCT,
        drawdown_pause_hours=settings.FLOW_DRAWDOWN_PAUSE_HOURS,
        fee_reserve_tao=settings.FEE_RESERVE_TAO,
        mtf_enabled=False,
        mtf_lower_tf_hours=1,
        mtf_confirm_bars=1,
        vol_sizing_enabled=False,
        vol_target_risk=0.02,
        vol_floor=0.10,
        vol_cap=1.50,
        vol_min_size_pct=0.15,
        vol_max_size_pct=0.40,
        vol_window=24,
        vol_scoring_penalty_at=1.0,
        post_exit_verify=settings.EMA_POST_EXIT_VERIFY,
        post_exit_verify_delay_sec=settings.EMA_POST_EXIT_VERIFY_DELAY_SEC,
        post_exit_max_retries=settings.EMA_POST_EXIT_MAX_RETRIES,
        post_exit_alpha_threshold=settings.EMA_POST_EXIT_ALPHA_THRESHOLD,
        rsi_filter_enabled=False,
        rsi_period=14,
        rsi_overbought=70.0,
        rsi_oversold=30.0,
        macd_filter_enabled=False,
        macd_fast=12,
        macd_slow=26,
        macd_signal=9,
        bb_filter_enabled=False,
        bb_period=20,
        bb_upper_reject=0.90,
        bb_std=2.0,
        bb_mid_exit=False,
        min_pool_depth_tao=settings.FLOW_MIN_POOL_DEPTH_TAO,
        parabolic_guard_mult=99.0,
        entry_price_drift_pct=5.0,
        momentum_filters_enabled=False,
        reject_day_and_week_negative_pct=0.0,
        reject_structural_decline_pct=0.0,
        flow_reversal_exit_enabled=True,
        flow_reversal_consecutive=2,
        flow_reversal_min_outflow_pct=settings.FLOW_EXIT_PCT,
        htf_fast_period=5,
        htf_slow_period=20,
        htf_confirm_bars=2,
    )
