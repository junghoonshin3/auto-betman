from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from src.bot import Bot


class TestBotCommandSync:
    async def test_sync_application_commands_global_only(self) -> None:
        bot = Bot()
        sync_mock = AsyncMock(return_value=[object()])
        copy_mock = MagicMock()
        bot.tree.sync = sync_mock
        bot.tree.copy_global_to = copy_mock

        await bot._sync_application_commands()

        copy_mock.assert_not_called()
        sync_mock.assert_awaited_once_with()

    async def test_sync_application_commands_with_guild(self) -> None:
        bot = Bot()
        bot.sync_guild_id = 123456789

        sync_mock = AsyncMock(side_effect=[[object(), object()], [object()]])
        copy_mock = MagicMock()
        bot.tree.sync = sync_mock
        bot.tree.copy_global_to = copy_mock

        await bot._sync_application_commands()

        copy_mock.assert_called_once()
        guild_arg = copy_mock.call_args.kwargs["guild"]
        assert guild_arg.id == 123456789

        assert sync_mock.await_count == 2
        first_call = sync_mock.await_args_list[0]
        second_call = sync_mock.await_args_list[1]
        assert first_call.kwargs["guild"].id == 123456789
        assert second_call.kwargs == {}
