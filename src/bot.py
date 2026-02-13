from __future__ import annotations

import logging
from typing import Awaitable, Callable, Literal

import discord
from discord import app_commands

from src.models import BetSlip, MatchBet

logger = logging.getLogger(__name__)

_STATUS_COLOR = {
    "발매중": discord.Color.green(),
    "발매마감": discord.Color.orange(),
    "구매예약중": discord.Color.teal(),
    "적중": discord.Color.gold(),
    "미적중": discord.Color.red(),
    "적중안됨": discord.Color.red(),
    "취소": discord.Color.dark_grey(),
}

_STATUS_ICON = {
    "발매중": "🟢",
    "발매마감": "🟠",
    "구매예약중": "🔵",
    "적중": "🏆",
    "미적중": "❌",
    "적중안됨": "❌",
    "취소": "🚫",
}

_MATCH_RESULT_ICON = {
    "적중": "✅",
    "미적중": "❌",
}


class LoginModal(discord.ui.Modal, title="베트맨 로그인"):
    user_id = discord.ui.TextInput(label="아이디", placeholder="betman ID")
    user_pw = discord.ui.TextInput(label="비밀번호", placeholder="betman PW", max_length=50)

    def __init__(self, login_callback: Callable[[str, str], Awaitable[bool]]) -> None:
        super().__init__()
        self._login_callback = login_callback

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        success = await self._login_callback(self.user_id.value, self.user_pw.value)
        if success:
            await interaction.followup.send("로그인 성공", ephemeral=True)
        else:
            await interaction.followup.send("로그인 실패", ephemeral=True)


def _format_won(value: int) -> str:
    return f"{value:,}원"


def _status_text(slip: BetSlip) -> str:
    status = (slip.status or "-").strip()
    if slip.result:
        status = f"{status} (결과: {slip.result})"
    return status


def _slip_icon(slip: BetSlip) -> str:
    if slip.result == "적중":
        return "🏆"
    if slip.result == "미적중":
        return "❌"
    return _STATUS_ICON.get(slip.status, "🎫")


def _embed_color(slip: BetSlip) -> discord.Color:
    if slip.result == "적중":
        return discord.Color.gold()
    if slip.result == "미적중":
        return discord.Color.red()
    return _STATUS_COLOR.get(slip.status, discord.Color.blurple())


def _match_result_text(match: MatchBet) -> str:
    icon = _MATCH_RESULT_ICON.get(match.result or "", "⏳")
    return f"{icon} {match.result or '대기'}"


def _actual_result_text(match: MatchBet) -> str:
    parts: list[str] = []
    if match.game_result:
        parts.append(match.game_result)
    if match.score:
        parts.append(match.score)
    if not parts:
        return "대기"
    return " | ".join(parts)


def _build_summary_embed(slips: list[BetSlip], mode_label: str) -> discord.Embed:
    total_purchase = sum(max(s.total_amount, 0) for s in slips)
    total_expected = sum(max(s.potential_payout, 0) for s in slips)
    total_actual = sum(max(s.actual_payout, 0) for s in slips)

    wins = sum(1 for s in slips if s.result == "적중" or s.status == "적중")
    losses = sum(1 for s in slips if s.result == "미적중" or s.status in {"미적중", "적중안됨"})
    pending = len(slips) - wins - losses

    embed = discord.Embed(
        title=f"구매내역 조회 결과 ({mode_label})",
        colour=discord.Color.blurple(),
    )
    embed.add_field(name="조회 건수", value=f"{len(slips)}건", inline=True)
    embed.add_field(name="적중/미적중/대기", value=f"{wins}/{losses}/{pending}", inline=True)
    embed.add_field(name="총 구매금액", value=_format_won(total_purchase), inline=True)
    embed.add_field(name="총 예상적중금", value=_format_won(total_expected), inline=True)
    embed.add_field(name="총 실제적중금", value=_format_won(total_actual), inline=True)
    embed.add_field(name="총 손익", value=_format_won(total_actual - total_purchase), inline=True)
    return embed


def _build_slip_embed(index: int, slip: BetSlip) -> discord.Embed:
    title = f"{_slip_icon(slip)} [{index}] {slip.slip_id}"
    subtitle = f"{slip.game_type or '-'} {slip.round_number or ''}".strip()
    if subtitle:
        title = f"{title} - {subtitle}"

    embed = discord.Embed(title=title, colour=_embed_color(slip))
    embed.add_field(name="상태", value=_status_text(slip), inline=True)
    embed.add_field(name="구매시각", value=slip.purchase_datetime or "-", inline=True)
    embed.add_field(name="조합배당", value=f"{slip.combined_odds:.2f}" if slip.combined_odds else "-", inline=True)

    payout_text = _format_won(slip.actual_payout) if slip.actual_payout else "-"
    embed.add_field(
        name="금액",
        value=(
            f"구매: {_format_won(slip.total_amount)}\n"
            f"예상: {_format_won(slip.potential_payout)}\n"
            f"실제: {payout_text}"
        ),
        inline=False,
    )

    if not slip.matches:
        embed.add_field(name="경기 정보", value="상세 경기 정보를 찾지 못했습니다.", inline=False)
        return embed

    for match in slip.matches[:12]:
        league = f"{match.sport}/{match.league}".strip("/")
        field_name = f"{match.match_number}. {league}" if league else f"{match.match_number}. 경기"

        lines = [
            f"{match.home_team} vs {match.away_team}",
            f"내 선택: {match.bet_selection or '-'} ({match.odds:.2f})" if match.odds else f"내 선택: {match.bet_selection or '-'}",
            f"실제 결과: {_actual_result_text(match)}",
            f"내 베팅 결과: {_match_result_text(match)}",
        ]
        if match.match_datetime:
            lines.insert(1, f"경기시각: {match.match_datetime}")

        value = "\n".join(lines)
        if len(value) > 1024:
            value = value[:1010] + "..."
        embed.add_field(name=field_name[:256], value=value, inline=False)

    if len(slip.matches) > 12:
        embed.add_field(name="추가 경기", value=f"외 {len(slip.matches) - 12}경기", inline=False)

    return embed


class Bot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.login_callback: Callable[[str, str], Awaitable[bool]] | None = None
        self.purchase_callback: Callable[[Literal["recent5", "month30"]], Awaitable[list[BetSlip]]] | None = None

    async def setup_hook(self) -> None:
        @self.tree.command(name="login", description="베트맨 로그인")
        async def login_command(interaction: discord.Interaction) -> None:
            if self.login_callback is None:
                await interaction.response.send_message("로그인 기능이 준비되지 않았습니다.", ephemeral=True)
                return
            await interaction.response.send_modal(LoginModal(self.login_callback))

        @self.tree.command(name="purchases", description="구매내역 조회")
        @app_commands.describe(mode="조회 방식 선택")
        @app_commands.choices(
            mode=[
                app_commands.Choice(name="가장 최근 5개", value="recent5"),
                app_commands.Choice(name="최근 1개월 (최대 30개)", value="month30"),
            ]
        )
        async def purchases_command(
            interaction: discord.Interaction,
            mode: app_commands.Choice[str] | None = None,
        ) -> None:
            if self.purchase_callback is None:
                await interaction.response.send_message("구매내역 기능이 준비되지 않았습니다.", ephemeral=True)
                return

            await interaction.response.defer(ephemeral=True, thinking=True)
            selected_mode = mode.value if mode else "recent5"
            mode_label = "최근 5개" if selected_mode == "recent5" else "최근 1개월(최대 30개)"

            try:
                slips = await self.purchase_callback(selected_mode)  # type: ignore[arg-type]
            except Exception as exc:
                logger.exception("Failed to scrape purchases")
                await interaction.followup.send(f"구매내역 조회 실패: {exc}", ephemeral=True)
                return

            if not slips:
                await interaction.followup.send("조회된 구매내역이 없습니다.", ephemeral=True)
                return

            summary = _build_summary_embed(slips, mode_label)
            await interaction.followup.send(embed=summary, ephemeral=True)

            detail_embeds = [_build_slip_embed(i, slip) for i, slip in enumerate(slips, start=1)]
            for i in range(0, len(detail_embeds), 5):
                await interaction.followup.send(embeds=detail_embeds[i:i + 5], ephemeral=True)

        await self.tree.sync()
        logger.info("Slash commands synced.")

    async def on_ready(self) -> None:
        logger.info("Bot ready: %s", self.user)
