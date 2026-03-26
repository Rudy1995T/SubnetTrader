"""
Central configuration for the EMA-only trading bot.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


@dataclass(frozen=True)
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
    correlation_threshold: float
    candle_timeframe_hours: int
    dry_run: bool
    max_slippage_pct: float
    max_entry_price_tao: float
    drawdown_breaker_pct: float
    drawdown_pause_hours: float
    fee_reserve_tao: float


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    FLAMEWIRE_API_KEY: str = ""
    FLAMEWIRE_CHAIN: str = "bittensor"
    FLAMEWIRE_HTTP_TEMPLATE: str = (
        "https://gateway-dev.flamewire.io/public/rpc/{chain}/{api_key}"
    )
    FLAMEWIRE_WS_TEMPLATE: str = (
        "wss://gateway-dev.flamewire.io/public/rpc/{chain}/{api_key}"
    )
    FLAMEWIRE_TIMEOUT: float = 30.0
    FLAMEWIRE_RETRIES: int = 3
    FLAMEWIRE_RETRY_DELAY: float = 2.0
    FLAMEWIRE_WS_PING_INTERVAL: float = 20.0
    FLAMEWIRE_WS_RECONNECT_DELAY: float = 5.0

    SUBTENSOR_FALLBACK_NETWORK: str = "wss://entrypoint-finney.opentensor.ai:443"

    TAOSTATS_API_KEY: str = ""
    TAOSTATS_BASE_URL: str = "https://api.taostats.io"
    TAOSTATS_RATE_LIMIT_PER_MIN: int = 30
    TAOSTATS_CACHE_TTL_SEC: int = 300

    PREFERRED_VALIDATORS: list[str] = [
        "5GKH9FPPnWSUoeeTJp19wVtd84XqFW4pyK2ijV2GsFbhTrP1",
        "5F4tQyWrhfGVcNhoqeiNsR6KjD4wMZ2kfhLj4oHYuyHbZAc3",
        "5Hddm3iBFD2GLT5ik7LZnT3XJUnRnN8PoeCFgGQgawUVKNm8",
    ]

    BT_WALLET_NAME: str = "default"
    BT_WALLET_HOTKEY: str = "default"
    BT_WALLET_PATH: str = str(Path.home() / ".bittensor" / "wallets")
    BT_WALLET_PASSWORD: str = ""

    SCAN_INTERVAL_MIN: int = 15
    MAX_ENTRY_PRICE_TAO: float = 0.1
    MAX_SLIPPAGE_PCT: float = 5.0
    FEE_RESERVE_TAO: float = 0.5

    # Strategy A — "Scalper" (fast=3, slow=9)
    EMA_ENABLED: bool = True
    EMA_DRY_RUN: bool = True
    EMA_DRY_RUN_STARTING_TAO: float = 2.0
    EMA_STRATEGY_TAG: str = "scalper"
    EMA_PERIOD: int = 9
    EMA_FAST_PERIOD: int = 3
    EMA_CONFIRM_BARS: int = 3
    EMA_POT_TAO: float = 5.0
    EMA_POSITION_SIZE_PCT: float = 0.33
    EMA_MAX_POSITIONS: int = 3
    EMA_STOP_LOSS_PCT: float = 8.0
    EMA_TAKE_PROFIT_PCT: float = 20.0
    EMA_MAX_HOLDING_HOURS: int = 168
    EMA_COOLDOWN_HOURS: float = 4.0
    EMA_DRAWDOWN_BREAKER_PCT: float = 15.0
    EMA_DRAWDOWN_PAUSE_HOURS: float = 6.0
    EMA_EXIT_WATCHER_ENABLED: bool = True
    EMA_EXIT_WATCHER_SEC: int = 15
    EMA_CORRELATION_THRESHOLD: float = 0.80
    EMA_CANDLE_TIMEFRAME_HOURS: int = 4
    EMA_BREAKEVEN_TRIGGER_PCT: float = 3.0
    EMA_TRAILING_STOP_PCT: float = 5.0
    EMA_BOUNCE_ENABLED: bool = True
    EMA_BOUNCE_TOUCH_TOLERANCE_PCT: float = 1.0
    EMA_BOUNCE_REQUIRE_GREEN: bool = True
    EMA_MAX_GINI: float = 0.82
    EMA_GINI_CACHE_TTL_SEC: int = 3600

    # Strategy B — "Trend" (fast=3, slow=18)
    EMA_B_ENABLED: bool = True
    EMA_B_DRY_RUN: bool = True
    EMA_B_STRATEGY_TAG: str = "trend"
    EMA_B_PERIOD: int = 18
    EMA_B_FAST_PERIOD: int = 3
    EMA_B_CONFIRM_BARS: int = 3
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
    EMA_B_BOUNCE_ENABLED: bool = True
    EMA_B_BOUNCE_TOUCH_TOLERANCE_PCT: float = 1.0
    EMA_B_BOUNCE_REQUIRE_GREEN: bool = True
    EMA_B_MAX_GINI: float = 0.82

    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""

    LOG_LEVEL: str = "INFO"
    DB_PATH: str = "data/ledger.db"
    JSONL_DIR: str = "data/logs"
    HEALTH_PORT: int = 8081
    KILL_SWITCH_PATH: str = "./KILL_SWITCH"

    @property
    def flamewire_http_url(self) -> str:
        if self.FLAMEWIRE_API_KEY:
            return self.FLAMEWIRE_HTTP_TEMPLATE.format(
                chain=self.FLAMEWIRE_CHAIN,
                api_key=self.FLAMEWIRE_API_KEY,
            )
        base = self.FLAMEWIRE_HTTP_TEMPLATE.split("/{api_key}")[0]
        return base.format(chain=self.FLAMEWIRE_CHAIN)

    @property
    def flamewire_ws_url(self) -> str:
        if self.FLAMEWIRE_API_KEY:
            return self.FLAMEWIRE_WS_TEMPLATE.format(
                chain=self.FLAMEWIRE_CHAIN,
                api_key=self.FLAMEWIRE_API_KEY,
            )
        base = self.FLAMEWIRE_WS_TEMPLATE.split("/{api_key}")[0]
        return base.format(chain=self.FLAMEWIRE_CHAIN)

    @field_validator("LOG_LEVEL")
    @classmethod
    def validate_log_level(cls, value: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        normalized = value.upper()
        if normalized not in allowed:
            raise ValueError(f"LOG_LEVEL must be one of {allowed}")
        return normalized


settings = Settings()


def strategy_a_config() -> StrategyConfig:
    """Build StrategyConfig for Strategy A (Scalper) from settings."""
    return StrategyConfig(
        tag=settings.EMA_STRATEGY_TAG,
        fast_period=settings.EMA_FAST_PERIOD,
        slow_period=settings.EMA_PERIOD,
        confirm_bars=settings.EMA_CONFIRM_BARS,
        pot_tao=settings.EMA_POT_TAO,
        position_size_pct=settings.EMA_POSITION_SIZE_PCT,
        max_positions=settings.EMA_MAX_POSITIONS,
        stop_loss_pct=settings.EMA_STOP_LOSS_PCT,
        take_profit_pct=settings.EMA_TAKE_PROFIT_PCT,
        trailing_stop_pct=settings.EMA_TRAILING_STOP_PCT,
        breakeven_trigger_pct=settings.EMA_BREAKEVEN_TRIGGER_PCT,
        max_holding_hours=settings.EMA_MAX_HOLDING_HOURS,
        cooldown_hours=settings.EMA_COOLDOWN_HOURS,
        bounce_enabled=settings.EMA_BOUNCE_ENABLED,
        bounce_touch_tolerance_pct=settings.EMA_BOUNCE_TOUCH_TOLERANCE_PCT,
        bounce_require_green=settings.EMA_BOUNCE_REQUIRE_GREEN,
        max_gini=settings.EMA_MAX_GINI,
        gini_cache_ttl_sec=settings.EMA_GINI_CACHE_TTL_SEC,
        correlation_threshold=settings.EMA_CORRELATION_THRESHOLD,
        candle_timeframe_hours=settings.EMA_CANDLE_TIMEFRAME_HOURS,
        dry_run=settings.EMA_DRY_RUN,
        max_slippage_pct=settings.MAX_SLIPPAGE_PCT,
        max_entry_price_tao=settings.MAX_ENTRY_PRICE_TAO,
        drawdown_breaker_pct=settings.EMA_DRAWDOWN_BREAKER_PCT,
        drawdown_pause_hours=settings.EMA_DRAWDOWN_PAUSE_HOURS,
        fee_reserve_tao=settings.FEE_RESERVE_TAO,
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
        breakeven_trigger_pct=settings.EMA_B_BREAKEVEN_TRIGGER_PCT,
        max_holding_hours=settings.EMA_B_MAX_HOLDING_HOURS,
        cooldown_hours=settings.EMA_B_COOLDOWN_HOURS,
        bounce_enabled=settings.EMA_B_BOUNCE_ENABLED,
        bounce_touch_tolerance_pct=settings.EMA_B_BOUNCE_TOUCH_TOLERANCE_PCT,
        bounce_require_green=settings.EMA_B_BOUNCE_REQUIRE_GREEN,
        max_gini=settings.EMA_B_MAX_GINI,
        gini_cache_ttl_sec=settings.EMA_GINI_CACHE_TTL_SEC,
        correlation_threshold=settings.EMA_B_CORRELATION_THRESHOLD,
        candle_timeframe_hours=settings.EMA_B_CANDLE_TIMEFRAME_HOURS,
        dry_run=settings.EMA_B_DRY_RUN,
        max_slippage_pct=settings.MAX_SLIPPAGE_PCT,
        max_entry_price_tao=settings.MAX_ENTRY_PRICE_TAO,
        drawdown_breaker_pct=settings.EMA_B_DRAWDOWN_BREAKER_PCT,
        drawdown_pause_hours=settings.EMA_B_DRAWDOWN_PAUSE_HOURS,
        fee_reserve_tao=settings.FEE_RESERVE_TAO,
    )
