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


def _sample_slip(slip_id: str = "A1", status: str = "발매중") -> BetSlip:
    return BetSlip(
        slip_id=slip_id,
        game_type="프로토",
        round_number="1회차",
        status=status,
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

    bot.purchase_callback.assert_awaited_once_with("777", 5)
    interaction.response.defer.assert_awaited_once_with(thinking=True)
    kwargs = interaction.followup.send.await_args.kwargs
    assert "embeds" not in kwargs
    assert kwargs["content"] == "[구매 요약] 조회 1건 · 발매중 1 · 발매마감 0 · 적중/미적중 0/0 · 첨부 0건"
    assert "files" not in kwargs
    assert "ephemeral" not in kwargs


async def test_purchases_command_sends_snapshot_file_when_available() -> None:
    bot = Bot()
    bot._sync_application_commands = AsyncMock()  # type: ignore[method-assign]
    bot.purchase_callback = AsyncMock(
        return_value=[
            _sample_slip("A1", "발매중"),
            _sample_slip("A2", "발매마감"),
            _sample_slip("A3", "적중"),
            _sample_slip("A4", "적중안됨"),
        ]
    )
    bot.purchase_snapshot_callback = AsyncMock(
        return_value={
            "files": [
                ("paper_A1.png", b"png-1"),
                ("paper_A2.png", b"png-2"),
            ],
            "attempted_count": 2,
            "success_count": 2,
            "failed_count": 0,
            "exact_success_count": 1,
            "fallback_success_count": 1,
        }
    )

    await bot.setup_hook()
    command = bot.tree.get_command("purchases")
    assert command is not None

    interaction = _make_interaction(777)
    await command.callback(interaction)

    bot.purchase_callback.assert_awaited_once_with("777", 5)
    bot.purchase_snapshot_callback.assert_awaited_once_with("777", ["A1", "A2"])
    kwargs = interaction.followup.send.await_args.kwargs
    assert "embeds" not in kwargs
    assert "files" in kwargs
    assert kwargs["content"] == "[구매 요약] 조회 4건 · 발매중 1 · 발매마감 1 · 적중/미적중 1/1 · 첨부 2건"
    assert len(kwargs["files"]) == 2
    assert [f.filename for f in kwargs["files"]] == ["paper_A1.png", "paper_A2.png"]


async def test_purchases_command_passes_requested_count_option() -> None:
    bot = Bot()
    bot._sync_application_commands = AsyncMock()  # type: ignore[method-assign]
    bot.purchase_callback = AsyncMock(return_value=[_sample_slip("A1", "발매중")])
    bot.purchase_snapshot_callback = AsyncMock(return_value={"files": [], "attempted_count": 1, "success_count": 0, "failed_count": 1})

    await bot.setup_hook()
    command = bot.tree.get_command("purchases")
    assert command is not None

    interaction = _make_interaction(777)
    await command.callback(interaction, 10)

    bot.purchase_callback.assert_awaited_once_with("777", 10)


async def test_purchases_command_passes_all_sale_targets_without_slice() -> None:
    bot = Bot()
    bot._sync_application_commands = AsyncMock()  # type: ignore[method-assign]
    sale_slips = [_sample_slip(f"A{i}", "발매중" if i % 2 else "발매마감") for i in range(1, 7)]
    bot.purchase_callback = AsyncMock(return_value=sale_slips)
    bot.purchase_snapshot_callback = AsyncMock(
        return_value={
            "files": [],
            "attempted_count": 6,
            "success_count": 0,
            "failed_count": 6,
            "exact_success_count": 0,
            "fallback_success_count": 0,
        }
    )

    await bot.setup_hook()
    command = bot.tree.get_command("purchases")
    assert command is not None

    interaction = _make_interaction(777)
    await command.callback(interaction)

    bot.purchase_callback.assert_awaited_once_with("777", 5)
    bot.purchase_snapshot_callback.assert_awaited_once_with("777", [f"A{i}" for i in range(1, 7)])
    kwargs = interaction.followup.send.await_args.kwargs
    assert "embeds" not in kwargs
    assert "files" not in kwargs
    assert kwargs["content"] == "[구매 요약] 조회 6건 · 발매중 3 · 발매마감 3 · 적중/미적중 0/0 · 첨부 0건"


async def test_purchases_command_splits_snapshot_files_when_over_limit() -> None:
    bot = Bot()
    bot._sync_application_commands = AsyncMock()  # type: ignore[method-assign]
    sale_slips = [_sample_slip(f"A{i}", "발매중") for i in range(1, 13)]
    snapshots = [(f"paper_A{i}.png", f"png-{i}".encode("utf-8")) for i in range(1, 13)]
    bot.purchase_callback = AsyncMock(return_value=sale_slips)
    bot.purchase_snapshot_callback = AsyncMock(
        return_value={
            "files": snapshots,
            "attempted_count": 12,
            "success_count": 12,
            "failed_count": 0,
            "exact_success_count": 8,
            "fallback_success_count": 4,
        }
    )

    await bot.setup_hook()
    command = bot.tree.get_command("purchases")
    assert command is not None

    interaction = _make_interaction(777)
    await command.callback(interaction)

    bot.purchase_callback.assert_awaited_once_with("777", 5)
    bot.purchase_snapshot_callback.assert_awaited_once_with("777", [f"A{i}" for i in range(1, 13)])
    assert interaction.followup.send.await_count == 2

    first_kwargs = interaction.followup.send.await_args_list[0].kwargs
    assert "embeds" not in first_kwargs
    assert first_kwargs["content"] == "[구매 요약] 조회 12건 · 발매중 12 · 발매마감 0 · 적중/미적중 0/0 · 첨부 12건"
    assert len(first_kwargs["files"]) == 10
    assert [f.filename for f in first_kwargs["files"]] == [f"paper_A{i}.png" for i in range(1, 11)]

    second_kwargs = interaction.followup.send.await_args_list[1].kwargs
    assert "embeds" not in second_kwargs
    assert "content" not in second_kwargs
    assert len(second_kwargs["files"]) == 2
    assert [f.filename for f in second_kwargs["files"]] == ["paper_A11.png", "paper_A12.png"]


async def test_purchases_command_snapshot_failure_sends_minimum_message() -> None:
    bot = Bot()
    bot._sync_application_commands = AsyncMock()  # type: ignore[method-assign]
    bot.purchase_callback = AsyncMock(return_value=[_sample_slip()])
    bot.purchase_snapshot_callback = AsyncMock(side_effect=RuntimeError("snapshot failed"))

    await bot.setup_hook()
    command = bot.tree.get_command("purchases")
    assert command is not None

    interaction = _make_interaction(777)
    await command.callback(interaction)

    bot.purchase_callback.assert_awaited_once_with("777", 5)
    bot.purchase_snapshot_callback.assert_awaited_once_with("777", ["A1"])
    kwargs = interaction.followup.send.await_args.kwargs
    assert "embeds" not in kwargs
    assert kwargs["content"] == "[구매 요약] 조회 1건 · 발매중 1 · 발매마감 0 · 적중/미적중 0/0 · 첨부 0건"
    assert "file" not in kwargs
    assert "files" not in kwargs


async def test_purchases_command_does_not_call_snapshot_when_no_sale_status_targets() -> None:
    bot = Bot()
    bot._sync_application_commands = AsyncMock()  # type: ignore[method-assign]
    bot.purchase_callback = AsyncMock(return_value=[_sample_slip("A3", "적중"), _sample_slip("A4", "적중안됨")])
    bot.purchase_snapshot_callback = AsyncMock(return_value={"files": [("paper_A3.png", b"png")]})

    await bot.setup_hook()
    command = bot.tree.get_command("purchases")
    assert command is not None

    interaction = _make_interaction(777)
    await command.callback(interaction)

    bot.purchase_callback.assert_awaited_once_with("777", 5)
    bot.purchase_snapshot_callback.assert_not_awaited()
    kwargs = interaction.followup.send.await_args.kwargs
    assert "embeds" not in kwargs
    assert kwargs["content"] == "[구매 요약] 조회 2건 · 발매중 0 · 발매마감 0 · 적중/미적중 1/1 · 첨부 0건"
    assert "files" not in kwargs


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
