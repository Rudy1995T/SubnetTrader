"""
Central configuration for the Bittensor Subnet Alpha Trading Bot.
Uses pydantic-settings for typed, validated env-var loading.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── FlameWire RPC ──────────────────────────────────────────────
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

    # ── Taostats ───────────────────────────────────────────────────
    TAOSTATS_API_KEY: str = ""
    TAOSTATS_BASE_URL: str = "https://api.taostats.io"
    TAOSTATS_RATE_LIMIT_PER_MIN: int = 30
    TAOSTATS_CACHE_TTL_SEC: int = 300  # 5 min

    # ── Bittensor Wallet ───────────────────────────────────────────
    BT_WALLET_NAME: str = "default"
    BT_WALLET_HOTKEY: str = "default"
    BT_WALLET_PATH: str = str(Path.home() / ".bittensor" / "wallets")
    BT_WALLET_PASSWORD: str = ""

    # ── Scheduler ──────────────────────────────────────────────────
    SCAN_INTERVAL_MIN: int = 15

    # ── Trading / Portfolio ────────────────────────────────────────
    DRY_RUN: bool = True
    NUM_SLOTS: int = 4
    MAX_HOLDING_HOURS: int = 72
    TARGET_HOLDING_HOURS: int = 48
    MAX_SLIPPAGE_PCT: float = 5.0  # percent
    MAX_TRADES_PER_DAY: int = 20
    COOLDOWN_HOURS: float = 8.0
    DAILY_DRAWDOWN_LIMIT_PCT: float = 10.0  # percent of starting daily NAV
    ALLOW_DOUBLE_SLOT: bool = False

    # ── Strategy thresholds ────────────────────────────────────────
    ENTER_THRESHOLD: float = 0.55
    HIGH_CONVICTION_THRESHOLD: float = 0.80
    STOP_LOSS_PCT: float = 8.0
    TAKE_PROFIT_PCT: float = 15.0
    TRAILING_STOP_PCT: float = 5.0

    # ── Value band heuristic ───────────────────────────────────────
    VALUE_BAND_LOW: float = 0.0035
    VALUE_BAND_HIGH: float = 0.0050
    VALUE_BAND_DECAY: float = 0.001  # width of Gaussian decay outside band

    # ── Signal weights (must sum to ~1.0 for interpretability) ─────
    W_TREND: float = 0.20
    W_SUPPORT_RESISTANCE: float = 0.15
    W_FIBONACCI: float = 0.15
    W_VOLATILITY: float = 0.20
    W_MEAN_REVERSION: float = 0.15
    W_VALUE_BAND: float = 0.15

    # ── Observability ──────────────────────────────────────────────
    LOG_LEVEL: str = "INFO"
    DB_PATH: str = "data/ledger.db"
    JSONL_DIR: str = "data/logs"

    # ── FastAPI health endpoint ────────────────────────────────────
    HEALTH_PORT: int = 8080

    # ── Kill switch ────────────────────────────────────────────────
    KILL_SWITCH_PATH: str = "./KILL_SWITCH"

    # ── Derived properties ─────────────────────────────────────────
    @property
    def flamewire_http_url(self) -> str:
        if self.FLAMEWIRE_API_KEY:
            return self.FLAMEWIRE_HTTP_TEMPLATE.format(
                chain=self.FLAMEWIRE_CHAIN, api_key=self.FLAMEWIRE_API_KEY
            )
        base = self.FLAMEWIRE_HTTP_TEMPLATE.split("/{api_key}")[0]
        return base.format(chain=self.FLAMEWIRE_CHAIN)

    @property
    def flamewire_ws_url(self) -> str:
        if self.FLAMEWIRE_API_KEY:
            return self.FLAMEWIRE_WS_TEMPLATE.format(
                chain=self.FLAMEWIRE_CHAIN, api_key=self.FLAMEWIRE_API_KEY
            )
        base = self.FLAMEWIRE_WS_TEMPLATE.split("/{api_key}")[0]
        return base.format(chain=self.FLAMEWIRE_CHAIN)

    @field_validator("LOG_LEVEL")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        v = v.upper()
        if v not in allowed:
            raise ValueError(f"LOG_LEVEL must be one of {allowed}")
        return v


# Singleton – importable everywhere
settings = Settings()
