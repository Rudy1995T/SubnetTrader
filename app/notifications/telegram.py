"""Telegram alerts and EMA-only command bot support."""
from __future__ import annotations

import asyncio
import json
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.config import settings
from app.logging.logger import logger


@dataclass(slots=True)
class TelegramDocument:
    """Descriptor for a document response sent back to Telegram."""

    path: str
    caption: str = ""


@dataclass(slots=True)
class TelegramCommandHandlers:
    """Async callbacks used by the EMA-only Telegram command bot."""

    help_text: str
    status: Callable[[], Awaitable[str]]
    positions: Callable[[int], Awaitable[str]]
    close: Callable[[str], Awaitable[str]]
    pause: Callable[[], Awaitable[str]]
    resume: Callable[[], Awaitable[str]]
    run_cycle: Callable[[], Awaitable[str]]
    export_csv: Callable[[], Awaitable[str | TelegramDocument]]
    history: Callable[[int], Awaitable[str]]


async def send_alert(text: str) -> None:
    """Send a Telegram alert if optional credentials are configured."""
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
    except Exception as exc:
        logger.debug(f"Telegram alert failed: {exc}")


class TelegramBot:
    """Minimal Telegram long-polling bot for EMA control commands."""

    def __init__(
        self,
        handlers: TelegramCommandHandlers,
        *,
        token: str | None = None,
        chat_id: str | None = None,
        poll_timeout_sec: int = 20,
        retry_delay_sec: float = 3.0,
    ) -> None:
        self._handlers = handlers
        self._token = token or settings.TELEGRAM_BOT_TOKEN
        self._chat_id = str(chat_id or settings.TELEGRAM_CHAT_ID)
        self._poll_timeout_sec = poll_timeout_sec
        self._retry_delay_sec = retry_delay_sec
        self._offset: int | None = None
        self._client: httpx.AsyncClient | None = None

    @property
    def enabled(self) -> bool:
        return bool(self._token and self._chat_id)

    async def run(self, stop_event: asyncio.Event) -> None:
        """Start Telegram polling until stop_event is set."""
        if not self.enabled:
            logger.info("Telegram bot disabled; missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
            return

        timeout = max(float(self._poll_timeout_sec) + 5.0, 30.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            self._client = client
            await self._set_my_commands()
            await self._discard_pending_updates()
            logger.info("Telegram command bot polling started")

            try:
                while not stop_event.is_set():
                    try:
                        updates = await self._fetch_updates(timeout=self._poll_timeout_sec)
                        for update in updates:
                            await self._handle_update(update)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.warning(f"Telegram polling error: {exc}")
                        await asyncio.sleep(self._retry_delay_sec)
            finally:
                self._client = None
                logger.info("Telegram command bot stopped")

    async def send_message(self, text: str) -> None:
        """Send an HTML-formatted Telegram message to the configured chat."""
        if not self.enabled:
            return
        await self._post(
            "sendMessage",
            json_data={"chat_id": self._chat_id, "text": text, "parse_mode": "HTML"},
        )

    async def send_document(self, path: str, caption: str = "") -> None:
        """Send a document file to the configured chat."""
        if not self.enabled:
            return

        file_path = Path(path)
        if not file_path.exists():
            raise FileNotFoundError(path)

        data = {"chat_id": self._chat_id}
        if caption:
            data["caption"] = caption
            data["parse_mode"] = "HTML"

        with file_path.open("rb") as handle:
            files = {"document": (file_path.name, handle, "text/csv")}
            await self._post("sendDocument", data=data, files=files)

    async def _discard_pending_updates(self) -> None:
        """Skip stale commands so a restart does not replay old chat actions."""
        while True:
            updates = await self._fetch_updates(timeout=0)
            if not updates:
                return

    async def _fetch_updates(self, *, timeout: int) -> list[dict]:
        if self._client is None:
            raise RuntimeError("Telegram client not initialized")

        params = {
            "timeout": timeout,
            "allowed_updates": json.dumps(["message"]),
        }
        if self._offset is not None:
            params["offset"] = self._offset

        response = await self._client.get(
            self._url("getUpdates"),
            params=params,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram API error: {payload}")

        updates = payload.get("result", [])
        if updates:
            self._offset = max(int(update["update_id"]) for update in updates) + 1
        return updates

    async def _handle_update(self, update: dict) -> None:
        message = update.get("message") or {}
        text = (message.get("text") or "").strip()
        if not text:
            return

        chat_id = str(message.get("chat", {}).get("id", ""))
        if chat_id != self._chat_id:
            logger.warning("Ignoring Telegram message from unauthorized chat", data={"chat_id": chat_id})
            return

        if not text.startswith("/"):
            return

        await self._dispatch_command(text)

    async def _dispatch_command(self, text: str) -> None:
        parts = shlex.split(text)
        if not parts:
            return

        command = parts[0].split("@", 1)[0].lower()
        args = parts[1:]
        logger.info("Telegram command received", data={"command": command})

        if command in {"/start", "/help"}:
            await self.send_message(self._handlers.help_text)
            return

        if command in {"/status", "/health"}:
            await self.send_message(await self._handlers.status())
            return

        if command == "/positions":
            limit = 5
            if args:
                try:
                    limit = max(1, min(int(args[0]), 20))
                except ValueError:
                    await self.send_message("Usage: <code>/positions [limit]</code>")
                    return
            await self.send_message(await self._handlers.positions(limit))
            return

        if command == "/close":
            if not args:
                await self.send_message("Usage: <code>/close 32</code> (subnet number)")
                return
            await self.send_message(await self._handlers.close(args[0]))
            return

        if command == "/pause":
            await self.send_message(await self._handlers.pause())
            return

        if command == "/resume":
            await self.send_message(await self._handlers.resume())
            return

        if command in {"/run", "/cycle"}:
            await self.send_message(await self._handlers.run_cycle())
            return

        if command in {"/export", "/tax"}:
            result = await self._handlers.export_csv()
            if isinstance(result, TelegramDocument):
                await self.send_document(result.path, caption=result.caption)
            else:
                await self.send_message(result)
            return

        if command == "/history":
            limit = 5
            if args:
                try:
                    limit = max(1, min(int(args[0]), 20))
                except ValueError:
                    await self.send_message("Usage: <code>/history [limit]</code>")
                    return
            await self.send_message(await self._handlers.history(limit))
            return

        await self.send_message(
            "Unknown command. Use <code>/help</code> for the EMA control command list."
        )

    async def _set_my_commands(self) -> None:
        if self._client is None:
            raise RuntimeError("Telegram client not initialized")

        try:
            response = await self._client.post(
                self._url("setMyCommands"),
                json={
                    "commands": [
                        {"command": "status", "description": "EMA bot status"},
                        {"command": "positions", "description": "Open EMA positions"},
                        {"command": "close", "description": "Close a position (e.g. /close 32)"},
                        {"command": "pause", "description": "Pause EMA entries"},
                        {"command": "resume", "description": "Resume EMA entries"},
                        {"command": "run", "description": "Trigger EMA cycle"},
                        {"command": "export", "description": "Export EMA trades CSV"},
                        {"command": "history", "description": "Recent closed trades"},
                        {"command": "help", "description": "Show command help"},
                    ]
                },
            )
            response.raise_for_status()
        except Exception as exc:
            logger.debug(f"Telegram setMyCommands failed: {exc}")

    async def _post(
        self,
        method: str,
        *,
        json_data: dict | None = None,
        data: dict | None = None,
        files: dict | None = None,
    ) -> dict:
        kwargs = {}
        if json_data is not None:
            kwargs["json"] = json_data
        if data is not None:
            kwargs["data"] = data
        if files is not None:
            kwargs["files"] = files

        if self._client is None:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(self._url(method), **kwargs)
        else:
            response = await self._client.post(self._url(method), **kwargs)

        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram API error: {payload}")
        return payload

    def _url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self._token}/{method}"
