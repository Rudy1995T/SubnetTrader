"""
Config API — read, validate, write .env settings from the browser.

Mounted as /api/config/* in main.py.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.config import Settings, settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/config", tags=["config"])

ENV_PATH = Path(".env")

# ── Field definitions ────────────────────────────────────────────────────────

REQUIRED_FIELDS = {"BT_WALLET_NAME", "BT_WALLET_HOTKEY", "BT_WALLET_PATH"}

SECRET_PATTERNS = ("PASSWORD", "TOKEN", "API_KEY")

FIELD_TYPES: dict[str, str] = {
    "BT_WALLET_NAME": "wallet_name",
    "BT_WALLET_HOTKEY": "wallet_name",
    "BT_WALLET_PATH": "path",
    "BT_WALLET_PASSWORD": "str",
    "FLAMEWIRE_API_KEY": "str",
    "TAOSTATS_API_KEY": "str",
    "TELEGRAM_BOT_TOKEN": "telegram_token",
    "TELEGRAM_CHAT_ID": "telegram_chat_id",
    # Strategy A — Scalper
    "EMA_DRY_RUN": "bool",
    "EMA_STRATEGY_TAG": "str",
    "EMA_POT_TAO": "float_pos",
    "EMA_MAX_POSITIONS": "int_range",
    "EMA_POSITION_SIZE_PCT": "float_range",
    "MAX_SLIPPAGE_PCT": "float_range",
    "EMA_STOP_LOSS_PCT": "float_range",
    "EMA_TAKE_PROFIT_PCT": "float_range",
    "EMA_TRAILING_STOP_PCT": "float_range",
    "EMA_MAX_HOLDING_HOURS": "int_range",
    "EMA_COOLDOWN_HOURS": "float_range",
    "EMA_PERIOD": "int_range",
    "EMA_FAST_PERIOD": "int_range",
    "EMA_CONFIRM_BARS": "int_range",
    "EMA_CANDLE_TIMEFRAME_HOURS": "int_enum",
    "EMA_DRAWDOWN_BREAKER_PCT": "float_range",
    "EMA_DRAWDOWN_PAUSE_HOURS": "float_range",
    # Strategy B — Trend
    "EMA_B_ENABLED": "bool",
    "EMA_B_DRY_RUN": "bool",
    "EMA_B_STRATEGY_TAG": "str",
    "EMA_B_POT_TAO": "float_pos",
    "EMA_B_MAX_POSITIONS": "int_range",
    "EMA_B_POSITION_SIZE_PCT": "float_range",
    "EMA_B_STOP_LOSS_PCT": "float_range",
    "EMA_B_TAKE_PROFIT_PCT": "float_range",
    "EMA_B_TRAILING_STOP_PCT": "float_range",
    "EMA_B_MAX_HOLDING_HOURS": "int_range",
    "EMA_B_COOLDOWN_HOURS": "float_range",
    "EMA_B_PERIOD": "int_range",
    "EMA_B_FAST_PERIOD": "int_range",
    "EMA_B_CONFIRM_BARS": "int_range",
    "EMA_B_CANDLE_TIMEFRAME_HOURS": "int_enum",
    "EMA_B_DRAWDOWN_BREAKER_PCT": "float_range",
    "EMA_B_DRAWDOWN_PAUSE_HOURS": "float_range",
    "EMA_B_BREAKEVEN_TRIGGER_PCT": "float_range",
    # Shared
    "SCAN_INTERVAL_MIN": "int_range",
    "LOG_LEVEL": "log_level",
}

FIELD_CONSTRAINTS: dict[str, dict[str, Any]] = {
    "EMA_MAX_POSITIONS": {"min": 1, "max": 20},
    "EMA_POSITION_SIZE_PCT": {"min": 0.01, "max": 1.0},
    "MAX_SLIPPAGE_PCT": {"min": 0.1, "max": 50.0},
    "EMA_STOP_LOSS_PCT": {"min": 1.0, "max": 50.0},
    "EMA_TAKE_PROFIT_PCT": {"min": 1.0, "max": 100.0},
    "EMA_TRAILING_STOP_PCT": {"min": 1.0, "max": 50.0},
    "EMA_MAX_HOLDING_HOURS": {"min": 1, "max": 720},
    "EMA_COOLDOWN_HOURS": {"min": 0, "max": 48.0},
    "EMA_PERIOD": {"min": 2, "max": 100},
    "EMA_FAST_PERIOD": {"min": 2, "max": 100},
    "EMA_CONFIRM_BARS": {"min": 1, "max": 10},
    "EMA_CANDLE_TIMEFRAME_HOURS": {"values": [1, 2, 4, 6, 8, 12, 24]},
    "EMA_DRAWDOWN_BREAKER_PCT": {"min": 1.0, "max": 50.0},
    "EMA_DRAWDOWN_PAUSE_HOURS": {"min": 0.5, "max": 48.0},
    # Strategy B — same ranges
    "EMA_B_MAX_POSITIONS": {"min": 1, "max": 20},
    "EMA_B_POSITION_SIZE_PCT": {"min": 0.01, "max": 1.0},
    "EMA_B_STOP_LOSS_PCT": {"min": 1.0, "max": 50.0},
    "EMA_B_TAKE_PROFIT_PCT": {"min": 1.0, "max": 100.0},
    "EMA_B_TRAILING_STOP_PCT": {"min": 1.0, "max": 50.0},
    "EMA_B_MAX_HOLDING_HOURS": {"min": 1, "max": 720},
    "EMA_B_COOLDOWN_HOURS": {"min": 0, "max": 48.0},
    "EMA_B_PERIOD": {"min": 2, "max": 100},
    "EMA_B_FAST_PERIOD": {"min": 2, "max": 100},
    "EMA_B_CONFIRM_BARS": {"min": 1, "max": 10},
    "EMA_B_CANDLE_TIMEFRAME_HOURS": {"values": [1, 2, 4, 6, 8, 12, 24]},
    "EMA_B_DRAWDOWN_BREAKER_PCT": {"min": 1.0, "max": 50.0},
    "EMA_B_DRAWDOWN_PAUSE_HOURS": {"min": 0.5, "max": 48.0},
    "EMA_B_BREAKEVEN_TRIGGER_PCT": {"min": 1.0, "max": 50.0},
    "SCAN_INTERVAL_MIN": {"min": 1, "max": 60},
}

# Fields that trigger full_restart_required
WALLET_FIELDS = {"BT_WALLET_NAME", "BT_WALLET_HOTKEY", "BT_WALLET_PATH", "BT_WALLET_PASSWORD"}

# .env section template for writing
ENV_TEMPLATE_ORDER = [
    ("# FlameWire RPC", ["FLAMEWIRE_API_KEY"]),
    ("# Taostats", ["TAOSTATS_API_KEY"]),
    ("# Wallet", ["BT_WALLET_NAME", "BT_WALLET_HOTKEY", "BT_WALLET_PATH", "BT_WALLET_PASSWORD"]),
    ("# Scheduler", ["SCAN_INTERVAL_MIN"]),
    ("# Execution", ["MAX_SLIPPAGE_PCT"]),
    (
        "# EMA Strategy A (Scalper)",
        [
            "EMA_DRY_RUN",
            "EMA_STRATEGY_TAG",
            "EMA_POT_TAO",
            "EMA_MAX_POSITIONS",
            "EMA_POSITION_SIZE_PCT",
            "EMA_STOP_LOSS_PCT",
            "EMA_TAKE_PROFIT_PCT",
            "EMA_TRAILING_STOP_PCT",
            "EMA_MAX_HOLDING_HOURS",
            "EMA_COOLDOWN_HOURS",
            "EMA_PERIOD",
            "EMA_FAST_PERIOD",
            "EMA_CONFIRM_BARS",
            "EMA_CANDLE_TIMEFRAME_HOURS",
            "EMA_DRAWDOWN_BREAKER_PCT",
            "EMA_DRAWDOWN_PAUSE_HOURS",
        ],
    ),
    (
        "# EMA Strategy B (Trend)",
        [
            "EMA_B_ENABLED",
            "EMA_B_DRY_RUN",
            "EMA_B_STRATEGY_TAG",
            "EMA_B_POT_TAO",
            "EMA_B_MAX_POSITIONS",
            "EMA_B_POSITION_SIZE_PCT",
            "EMA_B_STOP_LOSS_PCT",
            "EMA_B_TAKE_PROFIT_PCT",
            "EMA_B_TRAILING_STOP_PCT",
            "EMA_B_MAX_HOLDING_HOURS",
            "EMA_B_COOLDOWN_HOURS",
            "EMA_B_PERIOD",
            "EMA_B_FAST_PERIOD",
            "EMA_B_CONFIRM_BARS",
            "EMA_B_CANDLE_TIMEFRAME_HOURS",
            "EMA_B_BREAKEVEN_TRIGGER_PCT",
            "EMA_B_DRAWDOWN_BREAKER_PCT",
            "EMA_B_DRAWDOWN_PAUSE_HOURS",
        ],
    ),
    ("# Telegram alerts + commands (optional)", ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]),
    ("# Observability", ["LOG_LEVEL"]),
]

# ── Helpers ──────────────────────────────────────────────────────────────────


def _is_secret(field: str) -> bool:
    return any(pat in field for pat in SECRET_PATTERNS)


def _mask(value: str) -> str:
    return "••••••••" if value else ""


def _read_env() -> dict[str, str]:
    """Parse .env into a dict, preserving raw string values."""
    result: dict[str, str] = {}
    if not ENV_PATH.exists():
        return result
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        result[key.strip()] = val.strip()
    return result


def _get_defaults() -> dict[str, Any]:
    """Return the default values from the Settings model."""
    defaults: dict[str, Any] = {}
    for field_name in FIELD_TYPES:
        if hasattr(Settings, "model_fields") and field_name in Settings.model_fields:
            field_info = Settings.model_fields[field_name]
            defaults[field_name] = field_info.default
    return defaults


def _coerce_for_json(field: str, raw: str) -> Any:
    """Convert a raw .env string value to the appropriate JSON type."""
    ft = FIELD_TYPES.get(field, "str")
    if ft == "bool":
        return raw.lower() in ("true", "1", "yes")
    if ft in ("float_pos", "float_range"):
        try:
            return float(raw)
        except (ValueError, TypeError):
            return raw
    if ft in ("int_range", "int_enum"):
        try:
            return int(raw)
        except (ValueError, TypeError):
            return raw
    return raw


def _validate_field(field: str, value: Any, all_values: dict[str, Any]) -> str | None:
    """Validate a single field. Returns error message or None."""
    ft = FIELD_TYPES.get(field)
    if ft is None:
        return "Unknown field"

    if ft == "wallet_name":
        s = str(value)
        if not s:
            return "Must not be empty"
        if len(s) > 64:
            return "Max 64 characters"
        if not re.match(r"^[A-Za-z0-9_]+$", s):
            return "Only letters, numbers, and underscores"
        return None

    if ft == "path":
        s = str(value)
        if not s:
            return "Must not be empty"
        return None

    if ft == "str":
        # Any string is fine (may be empty)
        if isinstance(value, str) and ("\n" in value or "=" in value):
            return "Must not contain newlines or '='"
        return None

    if ft == "telegram_token":
        s = str(value)
        if s and not re.match(r"^\d+:[A-Za-z0-9_-]+$", s):
            return "Invalid format (expected 123456:ABC-DEF...)"
        return None

    if ft == "telegram_chat_id":
        s = str(value)
        if s and not re.match(r"^-?\d+$", s):
            return "Must be a numeric ID"
        return None

    if ft == "bool":
        if not isinstance(value, bool) and str(value).lower() not in ("true", "false", "1", "0"):
            return "Must be true or false"
        return None

    if ft == "float_pos":
        try:
            v = float(value)
        except (ValueError, TypeError):
            return "Must be a number"
        if v <= 0:
            return "Must be a positive number"
        return None

    if ft == "float_range":
        try:
            v = float(value)
        except (ValueError, TypeError):
            return "Must be a number"
        c = FIELD_CONSTRAINTS.get(field, {})
        lo, hi = c.get("min", 0), c.get("max", 1e9)
        if v < lo or v > hi:
            return f"Must be between {lo} and {hi}"
        return None

    if ft == "int_range":
        try:
            v = int(value)
        except (ValueError, TypeError):
            return "Must be an integer"
        c = FIELD_CONSTRAINTS.get(field, {})
        lo, hi = c.get("min", 0), c.get("max", 1e9)
        if v < lo or v > hi:
            return f"Must be between {lo} and {hi}"
        # Cross-field: EMA_FAST_PERIOD < EMA_PERIOD
        if field == "EMA_FAST_PERIOD":
            ema_period = all_values.get("EMA_PERIOD", v + 1)
            try:
                if int(ema_period) <= v:
                    return "Must be less than EMA Period"
            except (ValueError, TypeError):
                pass
        if field == "EMA_B_FAST_PERIOD":
            ema_b_period = all_values.get("EMA_B_PERIOD", v + 1)
            try:
                if int(ema_b_period) <= v:
                    return "Must be less than EMA_B Period"
            except (ValueError, TypeError):
                pass
        return None

    if ft == "int_enum":
        try:
            v = int(value)
        except (ValueError, TypeError):
            return "Must be an integer"
        allowed = FIELD_CONSTRAINTS.get(field, {}).get("values", [])
        if v not in allowed:
            return f"Must be one of: {', '.join(str(x) for x in allowed)}"
        return None

    if ft == "log_level":
        if str(value).upper() not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            return "Must be DEBUG, INFO, WARNING, ERROR, or CRITICAL"
        return None

    return None


def _to_env_str(field: str, value: Any) -> str:
    """Convert a typed value to a .env string representation."""
    ft = FIELD_TYPES.get(field, "str")
    if ft == "bool":
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value).lower()
    return str(value)


def _write_env(merged: dict[str, str]) -> None:
    """Write .env atomically with section comments."""
    lines: list[str] = []
    written_keys: set[str] = set()

    for section_comment, keys in ENV_TEMPLATE_ORDER:
        lines.append(section_comment)
        for key in keys:
            if key in merged:
                lines.append(f"{key}={merged[key]}")
                written_keys.add(key)
        lines.append("")

    # Write any remaining keys not covered by the template
    extra = sorted(set(merged.keys()) - written_keys)
    if extra:
        lines.append("# Other")
        for key in extra:
            lines.append(f"{key}={merged[key]}")
        lines.append("")

    tmp_path = ENV_PATH.with_suffix(".tmp")
    tmp_path.write_text("\n".join(lines))
    os.replace(str(tmp_path), str(ENV_PATH))


# ── Endpoints ────────────────────────────────────────────────────────────────


@router.get("/status")
async def config_status():
    has_env = ENV_PATH.exists()
    env_data = _read_env() if has_env else {}

    missing_required = [f for f in REQUIRED_FIELDS if not env_data.get(f)]
    missing_optional = [
        f for f in FIELD_TYPES
        if f not in REQUIRED_FIELDS and not env_data.get(f)
    ]

    return JSONResponse(content={
        "setup_complete": len(missing_required) == 0 and has_env,
        "missing_required": sorted(missing_required),
        "missing_optional": sorted(missing_optional),
        "has_env_file": has_env,
    })


@router.get("")
async def config_get():
    env_data = _read_env()
    defaults = _get_defaults()
    result: dict[str, Any] = {}

    for field in FIELD_TYPES:
        raw = env_data.get(field, "")
        if not raw and field in defaults and defaults[field] is not None:
            raw = str(defaults[field])

        if _is_secret(field):
            result[field] = _mask(raw)
        else:
            result[field] = _coerce_for_json(field, raw) if raw else ""

    return JSONResponse(content=result)


@router.post("")
async def config_post(body: dict[str, Any]):
    values: dict[str, Any] = body.get("values", {})
    do_restart: bool = body.get("restart", False)

    # Validate all fields
    errors: dict[str, str] = {}
    for field, value in values.items():
        err = _validate_field(field, value, values)
        if err:
            errors[field] = err

    if errors:
        return JSONResponse(status_code=422, content={"success": False, "errors": errors})

    # Merge with existing .env
    try:
        existing = _read_env()
    except Exception:
        existing = {}

    # Also pull defaults so the file is complete
    defaults = _get_defaults()
    for field, default_val in defaults.items():
        if field not in existing and field not in values and default_val is not None:
            existing[field] = str(default_val)

    for field, value in values.items():
        existing[field] = _to_env_str(field, value)

    # Write atomically
    try:
        _write_env(existing)
    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": f"Failed to write .env: {exc}"},
        )

    # Hot-reload settings
    restart_triggered = False
    full_restart_required = any(f in WALLET_FIELDS for f in values)
    restart_error = None

    if do_restart:
        try:
            import app.config as config_module
            config_module.settings = Settings()
            restart_triggered = True
        except Exception as exc:
            restart_error = str(exc)

    return JSONResponse(content={
        "success": True,
        "written_fields": sorted(values.keys()),
        "restart_triggered": restart_triggered,
        "full_restart_required": full_restart_required,
        **({"restart_error": restart_error} if restart_error else {}),
    })


@router.post("/test-telegram")
async def config_test_telegram(body: dict[str, str]):
    bot_token = body.get("bot_token", "").strip()
    chat_id = body.get("chat_id", "").strip()

    if not bot_token or not chat_id:
        return JSONResponse(content={"success": False, "error": "Bot token and chat ID are required"})

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": "SubnetTrader test — configuration verified. ✅",
                },
            )
            data = resp.json()
            if data.get("ok"):
                return JSONResponse(content={"success": True, "message": "Test message sent successfully"})
            desc = data.get("description", "Unknown error")
            return JSONResponse(content={"success": False, "error": desc})
    except httpx.TimeoutException:
        return JSONResponse(content={"success": False, "error": "Connection timed out — check your network"})
    except Exception as exc:
        return JSONResponse(content={"success": False, "error": str(exc)})


@router.post("/test-taostats")
async def config_test_taostats(body: dict[str, str]):
    api_key = body.get("api_key", "").strip()

    try:
        headers = {}
        if api_key:
            headers["Authorization"] = api_key

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.taostats.io/api/dtao/pool/latest/v1",
                params={"limit": "1"},
                headers=headers,
            )
            if resp.status_code == 200:
                msg = "Taostats API key is valid" if api_key else "Public access works (no key needed)"
                return JSONResponse(content={"success": True, "message": msg})
            if resp.status_code in (401, 403):
                return JSONResponse(content={"success": False, "error": "Invalid API key"})
            return JSONResponse(content={
                "success": False,
                "error": f"Unexpected status: {resp.status_code}",
            })
    except httpx.TimeoutException:
        return JSONResponse(content={"success": False, "error": "Connection timed out — check your network"})
    except Exception as exc:
        return JSONResponse(content={"success": False, "error": str(exc)})


@router.post("/test-wallet")
async def config_test_wallet(body: dict[str, str]):
    wallet_name = body.get("wallet_name", "").strip()
    hotkey = body.get("hotkey", "").strip()
    wallet_path = body.get("wallet_path", "").strip()
    password = body.get("password", "")

    if not wallet_name or not hotkey:
        return JSONResponse(content={"success": False, "error": "Wallet name and hotkey are required"})

    try:
        import bittensor as bt

        wallet = bt.Wallet(
            name=wallet_name,
            hotkey=hotkey,
            path=wallet_path or "~/.bittensor/wallets",
        )

        # Try loading the coldkey
        try:
            coldkey = wallet.coldkey
        except Exception as exc:
            err = str(exc)
            if "decrypt" in err.lower() or "password" in err.lower():
                if password:
                    try:
                        wallet.unlock_coldkey(password=password)
                        coldkey = wallet.coldkey
                    except Exception as inner:
                        return JSONResponse(content={
                            "success": False,
                            "error": f"Wrong password: {inner}",
                        })
                else:
                    return JSONResponse(content={
                        "success": False,
                        "error": "Coldkey is encrypted — provide password",
                    })
            else:
                return JSONResponse(content={"success": False, "error": err})

        coldkey_ss58 = wallet.coldkeypub.ss58_address

        # Query balance
        balance_tao = 0.0
        try:
            sub = bt.Subtensor()
            balance = sub.get_balance(coldkey_ss58)
            balance_tao = float(balance)
        except Exception:
            pass  # Balance query is best-effort

        return JSONResponse(content={
            "success": True,
            "coldkey_ss58": coldkey_ss58,
            "balance_tao": round(balance_tao, 4),
        })

    except ImportError:
        return JSONResponse(content={
            "success": False,
            "error": "bittensor SDK not installed",
        })
    except Exception as exc:
        return JSONResponse(content={"success": False, "error": str(exc)})


# ── Wallet management endpoints ──────────────────────────────────────────────


@router.get("/wallet/detect")
async def wallet_detect(
    wallet_name: str = "default",
    hotkey: str = "default",
    wallet_path: str = "~/.bittensor/wallets",
):
    """Check whether a wallet already exists at the given path."""
    expanded = os.path.expanduser(wallet_path)
    wallet_dir = Path(expanded) / wallet_name
    coldkey_path = wallet_dir / "coldkey"
    hotkey_path = wallet_dir / "hotkeys" / hotkey

    result = {
        "wallet_path_exists": wallet_dir.is_dir(),
        "coldkey_exists": coldkey_path.exists(),
        "hotkey_exists": hotkey_path.exists(),
        "coldkey_encrypted": None,
        "coldkey_ss58": None,
    }

    if result["coldkey_exists"]:
        try:
            import bittensor as bt

            w = bt.Wallet(name=wallet_name, hotkey=hotkey, path=expanded)
            result["coldkey_ss58"] = w.coldkeypub.ss58_address
            # Check if encrypted by trying to load the raw coldkey
            try:
                _ = w.coldkey
                result["coldkey_encrypted"] = False
            except Exception:
                result["coldkey_encrypted"] = True
        except Exception:
            pass

    return JSONResponse(content=result)


@router.post("/wallet/create")
async def wallet_create(body: dict[str, str]):
    """Create a new Bittensor wallet (coldkey + hotkey)."""
    wallet_name = body.get("wallet_name", "").strip()
    hotkey_name = body.get("hotkey", "").strip()
    wallet_path = body.get("wallet_path", "~/.bittensor/wallets").strip()
    password = body.get("password", "")

    if not wallet_name or not hotkey_name:
        return JSONResponse(content={
            "success": False,
            "error": "Wallet name and hotkey name are required",
        })

    for name, label in [(wallet_name, "Wallet name"), (hotkey_name, "Hotkey name")]:
        if not re.match(r"^[A-Za-z0-9_]+$", name) or len(name) > 64:
            return JSONResponse(content={
                "success": False,
                "error": f"{label}: only letters, numbers, underscores (max 64 chars)",
            })

    expanded = os.path.expanduser(wallet_path)
    coldkey_path = Path(expanded) / wallet_name / "coldkey"
    if coldkey_path.exists():
        return JSONResponse(content={
            "success": False,
            "error": f"Wallet '{wallet_name}' already exists at {wallet_path}",
        })

    try:
        import bittensor as bt

        # Generate mnemonic and keypair (gives us full control)
        mnemonic = bt.Keypair.generate_mnemonic(12)
        keypair = bt.Keypair.create_from_mnemonic(mnemonic)

        # Serialize keypair to the SDK's keyfile format
        keyfile_data = bt.serialized_keypair_to_keyfile_data(keypair)

        # Encrypt if password provided
        if password:
            keyfile_data = bt.encrypt_keyfile_data(keyfile_data, password=password)

        # Create directory structure
        wallet_dir = Path(expanded) / wallet_name
        hotkey_dir = wallet_dir / "hotkeys"
        hotkey_dir.mkdir(parents=True, exist_ok=True)

        # Write coldkey
        with open(wallet_dir / "coldkey", "wb") as f:
            f.write(keyfile_data)
        os.chmod(wallet_dir / "coldkey", 0o600)

        # Write coldkeypub.txt
        pub_data = {
            "accountId": "0x" + keypair.public_key.hex(),
            "publicKey": "0x" + keypair.public_key.hex(),
            "ss58Address": keypair.ss58_address,
        }
        with open(wallet_dir / "coldkeypub.txt", "w") as f:
            json.dump(pub_data, f)

        # Create hotkey using the SDK
        wallet = bt.Wallet(name=wallet_name, hotkey=hotkey_name, path=expanded)
        wallet.create_new_hotkey(n_words=12, use_password=False, overwrite=False, suppress=True)

        coldkey_ss58 = keypair.ss58_address

        logger.info(
            "Wallet created: name=%s hotkey=%s address=%s",
            wallet_name, hotkey_name, coldkey_ss58,
        )

        return JSONResponse(content={
            "success": True,
            "coldkey_ss58": coldkey_ss58,
            "mnemonic": mnemonic,
            "message": "Wallet created successfully. SAVE YOUR MNEMONIC — it cannot be recovered.",
        })

    except ImportError:
        return JSONResponse(content={
            "success": False,
            "error": "bittensor SDK not installed",
        })
    except Exception as exc:
        return JSONResponse(content={
            "success": False,
            "error": str(exc),
        })


@router.post("/go-live")
async def go_live_preflight(body: dict):
    """Pre-flight checks for enabling live trading."""
    wallet_name = body.get("wallet_name", "").strip()
    hotkey_name = body.get("hotkey", "").strip()
    wallet_path = body.get("wallet_path", "~/.bittensor/wallets").strip()
    password = body.get("password", "")
    pot_tao = float(body.get("pot_tao", 10.0))

    checks: dict[str, dict] = {}

    # 1. Wallet configured
    if wallet_name and hotkey_name:
        checks["wallet_configured"] = {
            "ok": True,
            "detail": f"{wallet_name} / {hotkey_name}",
        }
    else:
        checks["wallet_configured"] = {
            "ok": False,
            "detail": "Wallet name and hotkey are required",
        }

    # 2. Wallet unlockable + 3. Balance sufficient
    ss58 = None
    try:
        import bittensor as bt

        expanded = os.path.expanduser(wallet_path)
        wallet = bt.Wallet(name=wallet_name, hotkey=hotkey_name, path=expanded)

        try:
            if password:
                wallet.unlock_coldkey(password=password)
            else:
                _ = wallet.coldkey
            ss58 = wallet.coldkeypub.ss58_address
            checks["wallet_unlockable"] = {"ok": True, "detail": ss58}
        except Exception as exc:
            checks["wallet_unlockable"] = {"ok": False, "detail": str(exc)}
            checks["balance_sufficient"] = {"ok": False, "detail": "Cannot check — wallet locked"}

        if ss58:
            try:
                sub = bt.Subtensor()
                balance = float(sub.get_balance(ss58))
                sufficient = balance >= pot_tao
                checks["balance_sufficient"] = {
                    "ok": sufficient,
                    "detail": f"{balance:.4f} TAO (pot: {pot_tao} TAO)"
                        + ("" if sufficient else f" — short {pot_tao - balance:.4f} TAO"),
                }
            except Exception:
                checks["balance_sufficient"] = {
                    "ok": False,
                    "detail": "Chain unreachable — cannot verify balance",
                }
    except ImportError:
        checks["wallet_unlockable"] = {"ok": False, "detail": "bittensor SDK not installed"}
        checks["balance_sufficient"] = {"ok": False, "detail": "SDK unavailable"}

    # 4. RPC connected
    try:
        from app.chain.flamewire_rpc import FlameWireRPC

        temp_rpc = FlameWireRPC()
        rpc_ok = await temp_rpc.health_check()
        checks["rpc_connected"] = {
            "ok": rpc_ok,
            "detail": "FlameWire healthy" if rpc_ok else "FlameWire unreachable",
        }
        await temp_rpc.close()
    except Exception as exc:
        checks["rpc_connected"] = {"ok": False, "detail": str(exc)}

    # 5. Taostats reachable
    try:
        taostats_key = settings.TAOSTATS_API_KEY or ""
        headers = {"Authorization": taostats_key} if taostats_key else {}
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                "https://api.taostats.io/api/dtao/pool/latest/v1?limit=1",
                headers=headers,
            )
            if r.status_code == 200:
                checks["taostats_reachable"] = {"ok": True, "detail": "Taostats API responding"}
            else:
                checks["taostats_reachable"] = {
                    "ok": False,
                    "detail": f"HTTP {r.status_code} — check API key",
                }
    except Exception as exc:
        checks["taostats_reachable"] = {"ok": False, "detail": str(exc)}

    # 6. Telegram configured
    tg_configured = bool(settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHAT_ID)
    checks["telegram_configured"] = {
        "ok": tg_configured,
        "detail": "Configured" if tg_configured else "Not configured (optional)",
        "optional": True,
    }

    can_go_live = all(
        checks[k]["ok"]
        for k in ("wallet_configured", "wallet_unlockable", "balance_sufficient")
        if k in checks
    )

    return JSONResponse(content={"checks": checks, "can_go_live": can_go_live})


@router.post("/wallet/validate")
async def wallet_validate(body: dict[str, str]):
    """Deep validation: coldkey existence, unlock test, balance check."""
    wallet_name = body.get("wallet_name", "").strip()
    hotkey_name = body.get("hotkey", "").strip()
    wallet_path = body.get("wallet_path", "~/.bittensor/wallets").strip()
    password = body.get("password", "")

    if not wallet_name:
        return JSONResponse(content={"success": False, "error": "Wallet name is required"})

    expanded = os.path.expanduser(wallet_path)
    wallet_dir = Path(expanded) / wallet_name
    coldkey_file = wallet_dir / "coldkey"
    hotkey_file = wallet_dir / "hotkeys" / hotkey_name

    checks: dict[str, Any] = {
        "coldkey_exists": coldkey_file.exists(),
        "hotkey_exists": hotkey_file.exists(),
        "coldkey_unlockable": False,
        "coldkey_ss58": None,
        "balance_tao": None,
        "balance_sufficient": False,
    }
    warnings: list[str] = []

    if not checks["coldkey_exists"]:
        return JSONResponse(content={
            "success": False,
            "error": f"Wallet not found at {wallet_path}/{wallet_name}",
        })

    if not checks["hotkey_exists"]:
        warnings.append(f"Hotkey '{hotkey_name}' not found — create it or check the name")

    try:
        import bittensor as bt

        wallet = bt.Wallet(name=wallet_name, hotkey=hotkey_name, path=expanded)

        # Try unlocking coldkey
        try:
            _ = wallet.coldkey
            checks["coldkey_unlockable"] = True
        except Exception as exc:
            err = str(exc).lower()
            if "decrypt" in err or "password" in err or "nacl" in err:
                if password:
                    try:
                        wallet.unlock_coldkey(password=password)
                        checks["coldkey_unlockable"] = True
                    except Exception:
                        warnings.append("Wrong password — could not unlock coldkey")
                else:
                    warnings.append("Coldkey is encrypted — provide password to verify")
            else:
                warnings.append(f"Could not load coldkey: {exc}")

        # Get SS58 address (from public key — doesn't need unlock)
        try:
            checks["coldkey_ss58"] = wallet.coldkeypub.ss58_address
        except Exception:
            pass

        # Check balance
        if checks["coldkey_ss58"]:
            try:
                sub = bt.Subtensor()
                balance = sub.get_balance(checks["coldkey_ss58"])
                checks["balance_tao"] = round(float(balance), 4)

                pot_val = 10.0
                try:
                    from app.config import settings as live_settings
                    pot_val = live_settings.EMA_POT_TAO
                except Exception:
                    pass

                checks["balance_sufficient"] = checks["balance_tao"] >= pot_val
                if checks["balance_tao"] == 0:
                    warnings.append("Balance is 0 TAO — fund your wallet before trading")
                elif not checks["balance_sufficient"]:
                    warnings.append(
                        f"Balance ({checks['balance_tao']} TAO) is less than "
                        f"trading pot ({pot_val} TAO)"
                    )
            except Exception:
                warnings.append("Could not query balance — chain unreachable")

    except ImportError:
        return JSONResponse(content={
            "success": False,
            "error": "bittensor SDK not installed",
        })

    return JSONResponse(content={
        "success": True,
        "checks": checks,
        "warnings": warnings,
    })
