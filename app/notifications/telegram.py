"""
Telegram notification helper.

Sends fire-and-forget alerts to a configured Telegram bot.
Silently no-ops when TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is not set.
"""
from __future__ import annotations

import httpx

from app.config import settings
from app.logging.logger import logger


async def send_alert(text: str) -> None:
    """Send a Telegram message. Never raises — errors are logged at DEBUG level."""
    token = settings.TELEGRAM_BOT_TOKEN
    chat_id = settings.TELEGRAM_CHAT_ID
    if not token or not chat_id:
        return
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            )
    except Exception as e:
        logger.debug(f"Telegram alert failed: {e}")
