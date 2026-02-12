from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import discord
from discord import TextStyle, app_commands
from discord.ext import commands

from src.models import BetSlip, GameSchedule

if TYPE_CHECKING:
    from src.database import Database
    from src.config import Config

logger = logging.getLogger(__name__)

# Status → embed colour mapping
_STATUS_COLOURS = {
    "발매중": discord.Colour.green(),
    "발매마감": discord.Colour.orange(),
    "적중": discord.Colour.gold(),
    "적중안됨": discord.Colour.red(),
    "미적중": discord.Colour.red(),
    "적중확인중": discord.Colour.purple(),
    "구매예약중": discord.Colour.teal(),
    "취소": discord.Colour.greyple(),
}


class BetmanBot(commands.Bot):
    def __init__(self, config: Config) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

        self.config = config
        self.database: Database | None = None
        self._channel: discord.TextChannel | None = None
        # Callback set by main.py so slash commands can trigger a scrape
        # Signature: async def callback(discord_user_id: str) -> list[BetSlip]
        self.scrape_callback = None
        # Callback for /games command
        # Signature: async def callback(discord_user_id: str) -> tuple[str, list[GameSchedule]]
        self.games_callback = None

    async def setup_hook(self) -> None:
        self.tree.add_command(_setup_group)
        self.tree.add_command(_purchases_command)
        self.tree.add_command(_stats_command)
        self.tree.add_command(_games_command)
        # Global sync (can take up to 1 hour to propagate)
        await self.tree.sync()
        logger.info("Slash commands synced globally")

    async def on_ready(self) -> None:
        logger.info("Discord bot logged in as %s", self.user)
        channel = self.get_channel(self.config.discord_channel_id)
        if channel is None:
            channel = await self.fetch_channel(self.config.discord_channel_id)
        self._channel = channel

        # Guild-specific sync for instant slash command availability
        if hasattr(channel, "guild"):
            guild = channel.guild
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            logger.info("Slash commands synced to guild: %s (instant)", guild.name)

        await self._channel.send("**Betman Tracker** 시작됨 ✔")

    @property
    def target_channel(self) -> discord.TextChannel | None:
        return self._channel

    # ------------------------------------------------------------------
    # Notification delivery (DM vs channel)
    # ------------------------------------------------------------------

    async def _send_notification(
        self,
        discord_user_id: str,
        embed: discord.Embed,
    ) -> None:
        """Send an embed to the user via DM or channel based on their preference."""
        notify_via = "dm"
        if self.database:
            user_row = await self.database.get_user(discord_user_id)
            if user_row:
                notify_via = user_row["notify_via"]

        if notify_via == "dm":
            try:
                user = await self.fetch_user(int(discord_user_id))
                await user.send(embed=embed)
                return
            except Exception as exc:
                logger.warning(
                    "Failed to DM user %s, falling back to channel: %s",
                    discord_user_id,
                    exc,
                )

        # Fallback or explicit channel mode
        if self._channel:
            content = f"<@{discord_user_id}>"
            await self._channel.send(content=content, embed=embed)

    # ------------------------------------------------------------------
    # Sending purchase slips (DB-based, per-user)
    # ------------------------------------------------------------------

    async def send_slips(
        self, slips: list[BetSlip], discord_user_id: str = ""
    ) -> int:
        """Send new (non-duplicate) bet slips. Returns count sent."""
        if not self.database:
            # Legacy fallback for backward compat (single user, no DB)
            if self._channel:
                return await self._send_slips_json(slips)
            return 0

        return await self._send_slips_db(slips, discord_user_id)

    async def _send_slips_db(
        self, slips: list[BetSlip], discord_user_id: str = ""
    ) -> int:
        db = self.database
        sent = 0
        for slip in slips:
            is_new = await db.upsert_slip(slip, discord_user_id)
            if not is_new:
                row = await db._get_slip_row(slip.slip_id, discord_user_id)
                if row and row["purchase_notified"]:
                    continue

            embed = _build_embed(slip)
            if discord_user_id:
                await self._send_notification(discord_user_id, embed)
            elif self._channel:
                await self._channel.send(embed=embed)
            await db.mark_purchase_notified(slip.slip_id, discord_user_id)
            sent += 1

        logger.info("Sent %d new slip(s) to Discord", sent)
        return sent

    async def _send_slips_json(self, slips: list[BetSlip]) -> int:
        """Legacy JSON-based dedup (fallback)."""
        notified_ids = _load_notified_ids(self.config.last_notified_path)
        sent = 0

        for slip in slips:
            if slip.slip_id in notified_ids:
                continue

            embed = _build_embed(slip)
            await self._channel.send(embed=embed)
            notified_ids.add(slip.slip_id)
            sent += 1

        _save_notified_ids(self.config.last_notified_path, notified_ids)
        logger.info("Sent %d new slip(s) to Discord", sent)
        return sent

    # ------------------------------------------------------------------
    # Sending result notifications
    # ------------------------------------------------------------------

    async def send_results(
        self, slips: list[BetSlip], discord_user_id: str = ""
    ) -> int:
        """Send result notifications for settled slips. Returns count sent."""
        if not self.database:
            return 0

        sent = 0
        for slip in slips:
            embed = _build_result_embed(slip)
            if discord_user_id:
                await self._send_notification(discord_user_id, embed)
            elif self._channel:
                await self._channel.send(embed=embed)
            await self.database.mark_result_notified(slip.slip_id, discord_user_id)
            sent += 1

        logger.info("Sent %d result notification(s) to Discord", sent)
        return sent

    async def send_no_results(self) -> None:
        if self._channel:
            await self._channel.send("현재 **발매중/발매마감** 상태의 구매내역이 없습니다.")

    # ------------------------------------------------------------------
    # Filter logic
    # ------------------------------------------------------------------

    def _should_notify(self, slip: BetSlip) -> bool:
        return True


# ------------------------------------------------------------------
# /setup command group
# ------------------------------------------------------------------

_setup_group = app_commands.Group(name="setup", description="베트맨 계정 관리")


class _SetupModal(discord.ui.Modal, title="베트맨 계정 등록"):
    user_id = discord.ui.TextInput(
        label="베트맨 아이디",
        placeholder="betman 사이트 아이디를 입력하세요",
        required=True,
        max_length=50,
    )
    user_pw = discord.ui.TextInput(
        label="베트맨 비밀번호",
        placeholder="비밀번호를 입력하세요",
        style=TextStyle.short,
        required=True,
        max_length=100,
    )
    notify = discord.ui.TextInput(
        label="알림 방식 (dm 또는 channel)",
        placeholder="dm",
        default="dm",
        required=False,
        max_length=10,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        bot: BetmanBot = interaction.client  # type: ignore[assignment]
        if not bot.database:
            await interaction.response.send_message(
                "데이터베이스가 초기화되지 않았습니다.", ephemeral=True
            )
            return

        notify_via = (self.notify.value or "dm").strip().lower()
        if notify_via not in ("dm", "channel"):
            notify_via = "dm"

        await bot.database.register_user(
            discord_user_id=str(interaction.user.id),
            betman_user_id=self.user_id.value.strip(),
            betman_user_pw=self.user_pw.value,
            notify_via=notify_via,
        )
        await interaction.response.send_message(
            f"등록 완료! (알림: **{notify_via}**)", ephemeral=True
        )


@_setup_group.command(name="register", description="베트맨 계정을 등록합니다")
async def _setup_register(interaction: discord.Interaction) -> None:
    await interaction.response.send_modal(_SetupModal())


@_setup_group.command(name="remove", description="베트맨 계정 등록을 해제합니다")
async def _setup_remove(interaction: discord.Interaction) -> None:
    bot: BetmanBot = interaction.client  # type: ignore[assignment]
    if not bot.database:
        await interaction.response.send_message(
            "데이터베이스가 초기화되지 않았습니다.", ephemeral=True
        )
        return

    user = await bot.database.get_user(str(interaction.user.id))
    if not user:
        await interaction.response.send_message(
            "등록된 계정이 없습니다.", ephemeral=True
        )
        return

    await bot.database.remove_user(str(interaction.user.id))
    await interaction.response.send_message("등록이 해제되었습니다.", ephemeral=True)


@_setup_group.command(name="status", description="베트맨 계정 등록 상태를 확인합니다")
async def _setup_status(interaction: discord.Interaction) -> None:
    bot: BetmanBot = interaction.client  # type: ignore[assignment]
    if not bot.database:
        await interaction.response.send_message(
            "데이터베이스가 초기화되지 않았습니다.", ephemeral=True
        )
        return

    user = await bot.database.get_user(str(interaction.user.id))
    if not user:
        await interaction.response.send_message(
            "등록된 계정이 없습니다. `/setup register`로 등록해주세요.",
            ephemeral=True,
        )
        return

    embed = discord.Embed(title="계정 등록 상태", colour=discord.Colour.green())
    embed.add_field(name="베트맨 아이디", value=user["betman_user_id"], inline=True)
    embed.add_field(name="알림 방식", value=user["notify_via"], inline=True)
    embed.add_field(name="등록일", value=user["created_at"], inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ------------------------------------------------------------------
# /purchases command — 구매내역 상세 조회
# ------------------------------------------------------------------

@app_commands.command(name="purchases", description="구매내역을 상세하게 조회합니다 (경기/배당/선택 포함)")
@app_commands.describe(filter="조회 범위 (기본: 현재 회차 구매경기)")
@app_commands.choices(filter=[
    app_commands.Choice(name="현재 회차 구매경기 (기본)", value="active"),
    app_commands.Choice(name="전체", value="all"),
    app_commands.Choice(name="최근 1시간", value="recent"),
])
async def _purchases_command(
    interaction: discord.Interaction,
    filter: str = "active",
) -> None:
    bot: BetmanBot = interaction.client  # type: ignore[assignment]
    await interaction.response.defer(thinking=True)

    if bot.scrape_callback is None:
        await interaction.followup.send("스크래핑 콜백이 등록되지 않았습니다.")
        return

    discord_user_id = str(interaction.user.id)

    if bot.database:
        user = await bot.database.get_user(discord_user_id)
        if not user:
            await interaction.followup.send(
                "등록된 계정이 없습니다. `/setup register`로 등록해주세요."
            )
            return

    try:
        slips = await bot.scrape_callback(discord_user_id)
        slips = [s for s in slips if s.status != "적중안됨"]
        if not slips:
            await interaction.followup.send("구매내역이 없습니다.")
            return

        if filter == "recent":
            KST = timezone(timedelta(hours=9))
            cutoff = datetime.now(KST) - timedelta(hours=1)
            filtered = []
            for s in slips:
                m = re.match(r"(\d{2})\.(\d{2})\.(\d{2})\(.\)\s*(\d{2}):(\d{2})", s.purchase_datetime)
                if m:
                    dt = datetime(
                        2000 + int(m.group(1)), int(m.group(2)), int(m.group(3)),
                        int(m.group(4)), int(m.group(5)), tzinfo=KST,
                    )
                    if dt >= cutoff:
                        filtered.append(s)
            if not filtered:
                await interaction.followup.send("최근 1시간 내 구매내역이 없습니다.")
                return
            slips = filtered
            header = f"**최근 1시간 구매내역 {len(slips)}건**"
        elif filter == "active":
            # 가장 최신 회차 번호를 슬립에서 찾아서 필터링
            rounds = [int(s.round_number) for s in slips if s.round_number.isdigit()]
            if rounds:
                latest = str(max(rounds))
                slips = [s for s in slips if s.round_number == latest]
                header = f"**{latest}회차 구매경기 {len(slips)}건**"
            else:
                header = f"**구매내역 {len(slips)}건 조회 완료**"
        else:
            header = f"**구매내역 {len(slips)}건 조회 완료**"

        # 구매일시 오름차순 정렬
        slips.sort(key=lambda s: s.purchase_datetime)

        embed = _build_summary_embed(header, slips)
        await interaction.followup.send(embed=embed)
    except Exception as exc:
        logger.exception("Purchases command failed")
        await interaction.followup.send(f"조회 실패: {exc}")


# ------------------------------------------------------------------
# /games command — 구매 가능한 경기 목록 조회
# ------------------------------------------------------------------

@app_commands.command(name="games", description="현재 구매 가능한 경기 목록을 조회합니다")
@app_commands.describe(sport="종목 필터 (기본: 전체)")
@app_commands.choices(sport=[
    app_commands.Choice(name="전체", value="all"),
    app_commands.Choice(name="축구", value="축구"),
    app_commands.Choice(name="농구", value="농구"),
    app_commands.Choice(name="야구", value="야구"),
    app_commands.Choice(name="배구", value="배구"),
])
async def _games_command(
    interaction: discord.Interaction,
    sport: str = "all",
) -> None:
    bot: BetmanBot = interaction.client  # type: ignore[assignment]
    await interaction.response.defer(thinking=True)

    if bot.games_callback is None:
        await interaction.followup.send("경기 조회 콜백이 등록되지 않았습니다.")
        return

    discord_user_id = str(interaction.user.id)

    if bot.database:
        user = await bot.database.get_user(discord_user_id)
        if not user:
            await interaction.followup.send(
                "등록된 계정이 없습니다. `/setup register`로 등록해주세요."
            )
            return

    try:
        round_title, games = await bot.games_callback(discord_user_id)

        if not games:
            await interaction.followup.send("현재 구매 가능한 경기가 없습니다.")
            return

        # Apply sport filter
        if sport != "all":
            games = [g for g in games if g.sport == sport]
            if not games:
                await interaction.followup.send(f"**{sport}** 종목의 구매 가능한 경기가 없습니다.")
                return

        embeds = _build_games_embeds(round_title, games)
        for embed in embeds:
            await interaction.followup.send(embed=embed)

    except Exception as exc:
        logger.exception("Games command failed")
        await interaction.followup.send(f"경기 목록 조회 실패: {exc}")


# ------------------------------------------------------------------
# /stats command (user-aware)
# ------------------------------------------------------------------

@app_commands.command(name="stats", description="베팅 통계를 조회합니다")
@app_commands.describe(period="조회 기간")
@app_commands.choices(period=[
    app_commands.Choice(name="전체", value="all"),
    app_commands.Choice(name="일별 (7일)", value="daily"),
    app_commands.Choice(name="월별 (6개월)", value="monthly"),
])
async def _stats_command(interaction: discord.Interaction, period: str = "all") -> None:
    bot: BetmanBot = interaction.client  # type: ignore[assignment]
    await interaction.response.defer(thinking=True)

    if not bot.database:
        await interaction.followup.send("데이터베이스가 초기화되지 않았습니다.")
        return

    discord_user_id = str(interaction.user.id)

    try:
        if period == "daily":
            data = await bot.database.get_daily_stats(
                days=7, discord_user_id=discord_user_id
            )
            embed = _build_daily_stats_embed(data)
        elif period == "monthly":
            data = await bot.database.get_monthly_stats(
                months=6, discord_user_id=discord_user_id
            )
            embed = _build_monthly_stats_embed(data)
        else:
            data = await bot.database.get_statistics(
                discord_user_id=discord_user_id
            )
            embed = _build_stats_embed(data)

        await interaction.followup.send(embed=embed)
    except Exception as exc:
        logger.exception("Stats command failed")
        await interaction.followup.send(f"통계 조회 실패: {exc}")


# ------------------------------------------------------------------
# Embed builders
# ------------------------------------------------------------------

def _build_summary_embed(header: str, slips: list[BetSlip]) -> discord.Embed:
    """모든 슬립을 하나의 embed에 요약."""
    total_amount = sum(s.total_amount for s in slips)
    total_payout = sum(s.potential_payout for s in slips)

    embed = discord.Embed(title=header, colour=discord.Colour.blue())

    for slip in slips:
        # 경기 목록 한 줄씩
        match_lines = []
        for m in slip.matches:
            if m.score:
                # 경기 끝남 — 스코어 + 적중 여부
                hit = m.bet_selection == m.game_result
                icon = "✅" if hit else "❌"
                line = f"{icon} `{m.home_team}` {m.score} `{m.away_team}` ({m.game_result}) | 선택: **{m.bet_selection}** ({m.odds:.2f})"
            else:
                # 경기 전
                line = f"⏳ `{m.home_team}` vs `{m.away_team}` | 선택: **{m.bet_selection}** ({m.odds:.2f})"
            match_lines.append(line)

        if not match_lines:
            match_lines.append("상세 정보 없음")

        # 슬립 요약
        status_icon = {"발매중": "🟢", "발매마감": "🟠", "적중": "🏆", "미적중": "❌", "적중안됨": "❌", "취소": "🚫"}.get(slip.status, "⚪")
        slip_header = f"{status_icon} {slip.purchase_datetime or '-'} | {slip.total_amount:,}원"
        if slip.combined_odds:
            slip_header += f" | 배당 {slip.combined_odds:.2f}"
        if slip.potential_payout:
            slip_header += f" | 예상 {slip.potential_payout:,}원"

        value = slip_header + "\n" + "\n".join(match_lines)
        embed.add_field(name=f"🎫 {slip.slip_id}", value=value, inline=False)

    # 합계 footer
    footer = f"총 {len(slips)}건 | 총 구매: {total_amount:,}원"
    if total_payout:
        footer += f" | 총 예상적중: {total_payout:,}원"
    embed.set_footer(text=footer)

    return embed


def _build_embed(slip: BetSlip) -> discord.Embed:
    colour = _STATUS_COLOURS.get(slip.status, discord.Colour.blurple())

    embed = discord.Embed(
        title=f"{slip.game_type} {slip.round_number}" + ("" if "회" in slip.round_number else "회차") if slip.round_number else slip.title,
        colour=colour,
    )
    embed.add_field(name="상태", value=slip.status, inline=True)
    embed.add_field(name="구매일시", value=slip.purchase_datetime or "-", inline=True)
    embed.add_field(name="티켓번호", value=slip.slip_id, inline=False)

    # Match details
    for m in slip.matches:
        name = f"#{m.match_number} {m.league}" if m.league else f"#{m.match_number} {m.sport}"
        value = f"{m.home_team} vs {m.away_team}\n선택: **{m.bet_selection}** | 배당: {m.odds:.2f}"
        if m.match_datetime:
            value += f"\n경기시간: {m.match_datetime}"
        embed.add_field(name=name, value=value, inline=False)

    if not slip.matches:
        embed.add_field(name="경기 정보", value="상세 정보 없음", inline=False)

    # Footer summary
    footer_parts = []
    if slip.total_amount:
        footer_parts.append(f"구매금액: {slip.total_amount:,}원")
    if slip.potential_payout:
        footer_parts.append(f"예상적중금: {slip.potential_payout:,}원")
    if slip.combined_odds:
        footer_parts.append(f"합산배당: {slip.combined_odds:.2f}")
    embed.set_footer(text=" | ".join(footer_parts) if footer_parts else slip.slip_id)

    return embed


def _build_games_embeds(round_title: str, games: list[GameSchedule]) -> list[discord.Embed]:
    """Build embeds for available games, grouped by sport."""
    # Group by sport
    by_sport: dict[str, list[GameSchedule]] = {}
    for g in games:
        by_sport.setdefault(g.sport, []).append(g)

    embeds: list[discord.Embed] = []
    embed = discord.Embed(
        title=f"구매 가능 경기 — {round_title}" if round_title else "구매 가능 경기",
        colour=discord.Colour.blue(),
    )
    embed.description = f"총 {len(games)}경기"
    field_count = 0

    _TYPE_EMOJI = {"일반": "", "핸디캡": "[H]", "언더오버": "[U/O]", "SUM": "[SUM]"}

    for sport, sport_games in by_sport.items():
        for g in sport_games:
            type_tag = _TYPE_EMOJI.get(g.game_type, f"[{g.game_type}]")
            name = f"#{g.match_seq} {g.league} {type_tag}"

            odds_parts = [f"{k}:{v:.2f}" for k, v in g.odds.items()]
            odds_str = " | ".join(odds_parts) if odds_parts else "-"

            lines = [f"**{g.home_team}** vs **{g.away_team}**"]
            if g.handicap:
                lines[0] += f"  ({g.handicap})"
            lines.append(odds_str)
            lines.append(f"{g.deadline}  {g.stadium}" if g.stadium else g.deadline)

            embed.add_field(name=name, value="\n".join(lines), inline=False)
            field_count += 1

            # Discord embed limit: 25 fields
            if field_count >= 25:
                embeds.append(embed)
                embed = discord.Embed(
                    title=f"구매 가능 경기 (계속)",
                    colour=discord.Colour.blue(),
                )
                field_count = 0

    if field_count > 0:
        embeds.append(embed)

    return embeds


def _build_result_embed(slip: BetSlip) -> discord.Embed:
    result = slip.result or "알 수 없음"
    colour = _STATUS_COLOURS.get(result, discord.Colour.blurple())

    title_prefix = {
        "적중": "🎉 적중!",
        "미적중": "😢 미적중",
        "적중안됨": "😢 적중안됨",
        "취소": "🚫 취소",
    }.get(result, result)
    embed = discord.Embed(
        title=f"{title_prefix} — {slip.title}",
        colour=colour,
    )
    embed.add_field(name="결과", value=result, inline=True)
    embed.add_field(name="구매금액", value=f"{slip.total_amount:,}원", inline=True)

    if result == "적중" and slip.actual_payout:
        embed.add_field(name="적중금액", value=f"{slip.actual_payout:,}원", inline=True)
        profit = slip.actual_payout - slip.total_amount
        embed.add_field(name="수익", value=f"{profit:+,}원", inline=True)
    elif result in ("미적중", "적중안됨"):
        embed.add_field(name="손실", value=f"-{slip.total_amount:,}원", inline=True)

    embed.set_footer(text=f"구매일: {slip.purchase_datetime}")
    return embed


def _build_stats_embed(stats: dict) -> discord.Embed:
    embed = discord.Embed(title="베팅 통계 (전체)", colour=discord.Colour.blue())
    embed.add_field(name="총 베팅 수", value=str(stats["total"]), inline=True)
    embed.add_field(name="적중", value=str(stats["wins"]), inline=True)
    embed.add_field(name="미적중", value=str(stats["losses"]), inline=True)
    embed.add_field(name="적중률", value=f"{stats['win_rate']:.1f}%", inline=True)
    embed.add_field(name="총 구매금액", value=f"{stats['total_spent']:,}원", inline=True)
    embed.add_field(name="총 적중금액", value=f"{stats['total_payout']:,}원", inline=True)

    profit = stats["profit"]
    profit_str = f"{profit:+,}원"
    embed.add_field(name="손익", value=profit_str, inline=True)
    embed.add_field(name="대기 중", value=str(stats["pending"]), inline=True)

    return embed


def _build_daily_stats_embed(data: list[dict]) -> discord.Embed:
    embed = discord.Embed(title="일별 통계 (최근 7일)", colour=discord.Colour.blue())

    if not data:
        embed.description = "데이터가 없습니다."
        return embed

    for d in data:
        profit = d["profit"]
        profit_str = f"{profit:+,}원"
        embed.add_field(
            name=d["day"],
            value=f"베팅: {d['total']}건 | 적중: {d['wins']}건\n투자: {d['spent']:,}원 | 손익: {profit_str}",
            inline=False,
        )

    return embed


def _build_monthly_stats_embed(data: list[dict]) -> discord.Embed:
    embed = discord.Embed(title="월별 통계 (최근 6개월)", colour=discord.Colour.blue())

    if not data:
        embed.description = "데이터가 없습니다."
        return embed

    for d in data:
        profit = d["profit"]
        profit_str = f"{profit:+,}원"
        embed.add_field(
            name=d["month"],
            value=f"베팅: {d['total']}건 | 적중: {d['wins']}건\n투자: {d['spent']:,}원 | 손익: {profit_str}",
            inline=False,
        )

    return embed


# ------------------------------------------------------------------
# Legacy JSON duplicate tracking helpers (kept for backwards compat)
# ------------------------------------------------------------------

def _load_notified_ids(path: Path) -> set[str]:
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return set(data)
        except Exception:
            pass
    return set()


def _save_notified_ids(path: Path, ids: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(ids), ensure_ascii=False, indent=2), encoding="utf-8")
