from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.bot import Bot


def _make_interaction(user_id: int = 111) -> SimpleNamespace:
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        response=SimpleNamespace(
            defer=AsyncMock(),
            send_message=AsyncMock(),
            send_modal=AsyncMock(),
        ),
        followup=SimpleNamespace(send=AsyncMock()),
    )


async def test_logout_command_calls_callback_with_user_id() -> None:
    bot = Bot()
    bot._sync_application_commands = AsyncMock()  # type: ignore[method-assign]
    bot.logout_callback = AsyncMock(return_value=True)

    await bot.setup_hook()
    command = bot.tree.get_command("logout")
    assert command is not None

    interaction = _make_interaction(12345)
    await command.callback(interaction)

    bot.logout_callback.assert_awaited_once_with("12345")
    interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    interaction.followup.send.assert_awaited_once_with("로그아웃 완료", ephemeral=True)


async def test_logout_command_without_callback_sends_ready_message() -> None:
    bot = Bot()
    bot._sync_application_commands = AsyncMock()  # type: ignore[method-assign]

    await bot.setup_hook()
    command = bot.tree.get_command("logout")
    assert command is not None

    interaction = _make_interaction(12345)
    await command.callback(interaction)

    interaction.response.send_message.assert_awaited_once_with("로그아웃 기능이 준비되지 않았습니다.", ephemeral=True)
