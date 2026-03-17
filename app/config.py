"""
Central configuration for the EMA-only trading bot.
"""
from __future__ import annotations

from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    EMA_ENABLED: bool = True
    EMA_DRY_RUN: bool = True
    EMA_DRY_RUN_STARTING_TAO: float = 2.0
    EMA_PERIOD: int = 18
    EMA_CONFIRM_BARS: int = 3
    EMA_POT_TAO: float = 10.0
    EMA_POSITION_SIZE_PCT: float = 0.20
    EMA_MAX_POSITIONS: int = 5
    EMA_STOP_LOSS_PCT: float = 8.0
    EMA_TAKE_PROFIT_PCT: float = 20.0
    EMA_MAX_HOLDING_HOURS: int = 168
    EMA_COOLDOWN_HOURS: float = 4.0
    EMA_FAST_PERIOD: int = 6
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

    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""

    LOG_LEVEL: str = "INFO"
    DB_PATH: str = "data/ledger.db"
    JSONL_DIR: str = "data/logs"
    HEALTH_PORT: int = 8080
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
