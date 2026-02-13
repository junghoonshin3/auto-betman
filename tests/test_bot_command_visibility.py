from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from discord import app_commands

from src.bot import Bot
from src.models import BetSlip, PurchaseAnalysis, SaleGamesSnapshot


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


def _sample_slip() -> BetSlip:
    return BetSlip(
        slip_id="A1",
        game_type="프로토",
        round_number="1회차",
        status="발매중",
        purchase_datetime="2026.02.13 10:00",
        total_amount=5000,
        potential_payout=10000,
        combined_odds=2.0,
    )


async def test_purchases_command_sends_public_message() -> None:
    bot = Bot()
    bot._sync_application_commands = AsyncMock()  # type: ignore[method-assign]
    bot.purchase_callback = AsyncMock(return_value=[_sample_slip()])

    await bot.setup_hook()
    command = bot.tree.get_command("purchases")
    assert command is not None

    interaction = _make_interaction(777)
    await command.callback(interaction)

    bot.purchase_callback.assert_awaited_once_with("777")
    interaction.response.defer.assert_awaited_once_with(thinking=True)
    kwargs = interaction.followup.send.await_args.kwargs
    assert "ephemeral" not in kwargs


async def test_analysis_command_sends_public_message() -> None:
    bot = Bot()
    bot._sync_application_commands = AsyncMock()  # type: ignore[method-assign]
    bot.analysis_callback = AsyncMock(return_value=PurchaseAnalysis(months=3, purchase_amount=1000, winning_amount=500))

    await bot.setup_hook()
    command = bot.tree.get_command("analysis")
    assert command is not None

    interaction = _make_interaction(888)
    await command.callback(interaction, 3)

    bot.analysis_callback.assert_awaited_once_with("888", 3)
    interaction.response.defer.assert_awaited_once_with(thinking=True)
    kwargs = interaction.followup.send.await_args.kwargs
    assert "ephemeral" not in kwargs


async def test_games_command_sends_public_message() -> None:
    bot = Bot()
    bot._sync_application_commands = AsyncMock()  # type: ignore[method-assign]
    bot.games_callback = AsyncMock(
        return_value=SaleGamesSnapshot(
            fetched_at="2026.02.13 19:00:00",
            total_games=1,
            total_matches=2,
            sport_counts={"축구": 2},
            nearest_matches=[],
            partial_failures=0,
        )
    )

    await bot.setup_hook()
    command = bot.tree.get_command("games")
    assert command is not None

    interaction = _make_interaction(999)
    await command.callback(interaction)

    bot.games_callback.assert_awaited_once_with("victory", "all")
    interaction.response.defer.assert_awaited_once_with(thinking=True)
    kwargs = interaction.followup.send.await_args.kwargs
    assert "ephemeral" not in kwargs


async def test_games_command_passes_selected_game_type() -> None:
    bot = Bot()
    bot._sync_application_commands = AsyncMock()  # type: ignore[method-assign]
    bot.games_callback = AsyncMock(
        return_value=SaleGamesSnapshot(
            fetched_at="2026.02.13 19:00:00",
            total_games=1,
            total_matches=1,
            sport_counts={"축구": 1},
            nearest_matches=[],
            partial_failures=0,
        )
    )

    await bot.setup_hook()
    command = bot.tree.get_command("games")
    assert command is not None

    interaction = _make_interaction(999)
    await command.callback(
        interaction,
        app_commands.Choice(name="기록식", value="record"),
        app_commands.Choice(name="농구", value="basketball"),
    )

    bot.games_callback.assert_awaited_once_with("record", "basketball")


async def test_games_command_empty_message_includes_selected_type() -> None:
    bot = Bot()
    bot._sync_application_commands = AsyncMock()  # type: ignore[method-assign]
    bot.games_callback = AsyncMock(
        return_value=SaleGamesSnapshot(
            fetched_at="2026.02.13 19:00:00",
            total_games=0,
            total_matches=0,
            sport_counts={},
            nearest_matches=[],
            partial_failures=0,
        )
    )

    await bot.setup_hook()
    command = bot.tree.get_command("games")
    assert command is not None

    interaction = _make_interaction(1111)
    await command.callback(interaction)

    kwargs = interaction.followup.send.await_args.kwargs
    assert kwargs == {}
    args = interaction.followup.send.await_args.args
    assert args
    assert "조회 타입(승부식), 종목(전체)" in args[0]
