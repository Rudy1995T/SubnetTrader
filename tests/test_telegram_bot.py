"""Tests for the EMA-only Telegram command bot."""
import asyncio
from unittest.mock import AsyncMock

from app.notifications.telegram import TelegramBot, TelegramCommandHandlers, TelegramDocument


class TestTelegramBot:
    def _make_bot(self) -> tuple[TelegramBot, TelegramCommandHandlers]:
        handlers = TelegramCommandHandlers(
            help_text="help text",
            status=AsyncMock(return_value="status text"),
            positions=AsyncMock(return_value="positions text"),
            close=AsyncMock(return_value="close text"),
            pause=AsyncMock(return_value="pause text"),
            resume=AsyncMock(return_value="resume text"),
            run_cycle=AsyncMock(return_value="run text"),
            export_csv=AsyncMock(return_value="export text"),
        )
        bot = TelegramBot(handlers, token="token", chat_id="12345")
        bot.send_message = AsyncMock()
        bot.send_document = AsyncMock()
        return bot, handlers

    def test_ignores_messages_from_other_chat(self):
        bot, handlers = self._make_bot()

        asyncio.run(bot._handle_update({"message": {"chat": {"id": 999}, "text": "/status"}}))

        handlers.status.assert_not_awaited()
        bot.send_message.assert_not_awaited()

    def test_dispatches_core_commands(self):
        bot, handlers = self._make_bot()

        asyncio.run(bot._dispatch_command("/status"))
        asyncio.run(bot._dispatch_command("/positions 7"))
        asyncio.run(bot._dispatch_command("/close sn42"))
        asyncio.run(bot._dispatch_command("/pause"))
        asyncio.run(bot._dispatch_command("/resume"))
        asyncio.run(bot._dispatch_command("/run"))

        handlers.status.assert_awaited_once()
        handlers.positions.assert_awaited_once_with(7)
        handlers.close.assert_awaited_once_with("sn42")
        handlers.pause.assert_awaited_once()
        handlers.resume.assert_awaited_once()
        handlers.run_cycle.assert_awaited_once()
        assert bot.send_message.await_count == 6

    def test_export_command_sends_document_when_available(self):
        bot, handlers = self._make_bot()
        handlers.export_csv = AsyncMock(
            return_value=TelegramDocument(path="/tmp/ema_trades.csv", caption="csv ready")
        )

        asyncio.run(bot._dispatch_command("/export"))

        handlers.export_csv.assert_awaited_once()
        bot.send_document.assert_awaited_once_with("/tmp/ema_trades.csv", caption="csv ready")
        bot.send_message.assert_not_awaited()
