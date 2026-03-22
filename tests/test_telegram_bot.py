"""Tests for the EMA-only Telegram command bot."""
import asyncio
from unittest.mock import AsyncMock

import pytest

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
            history=AsyncMock(return_value="history text"),
        )
        bot = TelegramBot(handlers, token="token", chat_id="12345")
        bot.send_message = AsyncMock()
        bot.send_document = AsyncMock()
        return bot, handlers

    # ── Security ───────────────────────────────────────────────────

    def test_ignores_messages_from_other_chat(self):
        bot, handlers = self._make_bot()

        asyncio.run(bot._handle_update({"message": {"chat": {"id": 999}, "text": "/status"}}))

        handlers.status.assert_not_awaited()
        bot.send_message.assert_not_awaited()

    def test_ignores_non_command_messages(self):
        bot, handlers = self._make_bot()

        asyncio.run(bot._handle_update({"message": {"chat": {"id": 12345}, "text": "hello"}}))

        handlers.status.assert_not_awaited()
        bot.send_message.assert_not_awaited()

    def test_ignores_empty_message(self):
        bot, handlers = self._make_bot()

        asyncio.run(bot._handle_update({"message": {"chat": {"id": 12345}, "text": ""}}))

        bot.send_message.assert_not_awaited()

    # ── Core command dispatch ──────────────────────────────────────

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

    # ── Alias commands ────────────────────────────────────────────

    def test_help_aliases(self):
        bot, handlers = self._make_bot()

        asyncio.run(bot._dispatch_command("/start"))
        asyncio.run(bot._dispatch_command("/help"))

        assert bot.send_message.await_count == 2
        for call in bot.send_message.await_args_list:
            assert call.args[0] == "help text"

    def test_health_alias_for_status(self):
        bot, handlers = self._make_bot()

        asyncio.run(bot._dispatch_command("/health"))

        handlers.status.assert_awaited_once()

    def test_cycle_alias_for_run(self):
        bot, handlers = self._make_bot()

        asyncio.run(bot._dispatch_command("/cycle"))

        handlers.run_cycle.assert_awaited_once()

    def test_tax_alias_for_export(self):
        bot, handlers = self._make_bot()

        asyncio.run(bot._dispatch_command("/tax"))

        handlers.export_csv.assert_awaited_once()

    # ── /positions argument handling ──────────────────────────────

    def test_positions_default_limit_is_5(self):
        bot, handlers = self._make_bot()

        asyncio.run(bot._dispatch_command("/positions"))

        handlers.positions.assert_awaited_once_with(5)

    def test_positions_clamps_limit_to_20(self):
        bot, handlers = self._make_bot()

        asyncio.run(bot._dispatch_command("/positions 99"))

        handlers.positions.assert_awaited_once_with(20)

    def test_positions_clamps_limit_to_1(self):
        bot, handlers = self._make_bot()

        asyncio.run(bot._dispatch_command("/positions 0"))

        handlers.positions.assert_awaited_once_with(1)

    def test_positions_invalid_arg_returns_usage(self):
        bot, handlers = self._make_bot()

        asyncio.run(bot._dispatch_command("/positions abc"))

        handlers.positions.assert_not_awaited()
        bot.send_message.assert_awaited_once()
        assert "Usage" in bot.send_message.await_args.args[0]

    # ── /close argument handling ──────────────────────────────────

    def test_close_no_arg_returns_usage(self):
        bot, handlers = self._make_bot()

        asyncio.run(bot._dispatch_command("/close"))

        handlers.close.assert_not_awaited()
        bot.send_message.assert_awaited_once()
        assert "Usage" in bot.send_message.await_args.args[0]

    def test_close_with_hash_prefix(self):
        bot, handlers = self._make_bot()

        asyncio.run(bot._dispatch_command("/close #5"))

        handlers.close.assert_awaited_once_with("#5")

    def test_close_with_sn_format(self):
        bot, handlers = self._make_bot()

        asyncio.run(bot._dispatch_command("/close sn42"))

        handlers.close.assert_awaited_once_with("sn42")

    # ── /history command ──────────────────────────────────────────

    def test_history_default_limit_is_5(self):
        bot, handlers = self._make_bot()

        asyncio.run(bot._dispatch_command("/history"))

        handlers.history.assert_awaited_once_with(5)

    def test_history_with_custom_limit(self):
        bot, handlers = self._make_bot()

        asyncio.run(bot._dispatch_command("/history 10"))

        handlers.history.assert_awaited_once_with(10)

    def test_history_clamps_limit(self):
        bot, handlers = self._make_bot()

        asyncio.run(bot._dispatch_command("/history 50"))

        handlers.history.assert_awaited_once_with(20)

    def test_history_invalid_arg_returns_usage(self):
        bot, handlers = self._make_bot()

        asyncio.run(bot._dispatch_command("/history bad"))

        handlers.history.assert_not_awaited()
        bot.send_message.assert_awaited_once()
        assert "Usage" in bot.send_message.await_args.args[0]

    # ── /export document handling ─────────────────────────────────

    def test_export_command_sends_document_when_available(self):
        bot, handlers = self._make_bot()
        handlers.export_csv = AsyncMock(
            return_value=TelegramDocument(path="/tmp/ema_trades.csv", caption="csv ready")
        )

        asyncio.run(bot._dispatch_command("/export"))

        handlers.export_csv.assert_awaited_once()
        bot.send_document.assert_awaited_once_with("/tmp/ema_trades.csv", caption="csv ready")
        bot.send_message.assert_not_awaited()

    def test_export_command_sends_text_when_no_trades(self):
        bot, handlers = self._make_bot()

        asyncio.run(bot._dispatch_command("/export"))

        bot.send_message.assert_awaited_once_with("export text")
        bot.send_document.assert_not_awaited()

    # ── Unknown command ───────────────────────────────────────────

    def test_unknown_command_returns_help_hint(self):
        bot, handlers = self._make_bot()

        asyncio.run(bot._dispatch_command("/notacommand"))

        bot.send_message.assert_awaited_once()
        assert "/help" in bot.send_message.await_args.args[0]

    # ── Bot@username stripping ────────────────────────────────────

    def test_command_with_bot_username_suffix(self):
        bot, handlers = self._make_bot()

        asyncio.run(bot._dispatch_command("/status@mybot"))

        handlers.status.assert_awaited_once()
