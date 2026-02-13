from __future__ import annotations

import io
import json
import logging
from pathlib import Path
from typing import Awaitable, Callable

import discord
from discord import app_commands

from src.models import BetSlip, MatchBet, PurchaseAnalysis, SaleGamesSnapshot

logger = logging.getLogger(__name__)
LOGIN_ID_MAP_PATH = Path("storage/login_id_map.json")

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
_GAME_TYPE_LABEL_BY_VALUE = {
    "windrawlose": "승무패",
    "victory": "승부식",
    "record": "기록식",
    "all": "전체",
}
_SPORT_LABEL_BY_VALUE = {
    "all": "전체",
    "soccer": "축구",
    "baseball": "야구",
    "basketball": "농구",
    "volleyball": "배구",
}


def _load_login_id_map(path: Path = LOGIN_ID_MAP_PATH) -> dict[str, str]:
    try:
        if not path.exists():
            return {}
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}
        return {str(k): str(v) for k, v in raw.items() if str(v).strip()}
    except Exception:
        return {}


def _save_login_id_map(data: dict[str, str], path: Path = LOGIN_ID_MAP_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _get_saved_login_id(discord_user_id: str, path: Path = LOGIN_ID_MAP_PATH) -> str | None:
    data = _load_login_id_map(path)
    value = data.get(str(discord_user_id), "").strip()
    return value or None


def _set_saved_login_id(discord_user_id: str, login_id: str, path: Path = LOGIN_ID_MAP_PATH) -> None:
    value = login_id.strip()
    if not value:
        return
    data = _load_login_id_map(path)
    data[str(discord_user_id)] = value
    _save_login_id_map(data, path)


class LoginModal(discord.ui.Modal, title="베트맨 로그인"):
    user_id = discord.ui.TextInput(label="아이디", placeholder="betman ID")
    user_pw = discord.ui.TextInput(label="비밀번호", placeholder="betman PW", max_length=50)

    def __init__(
        self,
        login_callback: Callable[[str, str, str], Awaitable[bool]],
        discord_user_id: str,
        default_user_id: str | None = None,
    ) -> None:
        super().__init__()
        self._login_callback = login_callback
        self._discord_user_id = str(discord_user_id)
        if default_user_id:
            self.user_id.default = default_user_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        progress_message = await interaction.followup.send("로그인 시도중...", ephemeral=True, wait=True)
        success = await self._login_callback(self._discord_user_id, self.user_id.value, self.user_pw.value)

        if success:
            try:
                _set_saved_login_id(self._discord_user_id, self.user_id.value)
            except Exception as exc:
                logger.warning("Failed to save login id autofill: %s", exc)

        final_text = "로그인 성공" if success else "로그인 실패"
        try:
            await progress_message.edit(content=final_text)
        except Exception:
            await interaction.followup.send(final_text, ephemeral=True)


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


def _match_result_text(match: MatchBet) -> str | None:
    if not match.result:
        return None
    icon = _MATCH_RESULT_ICON.get(match.result, "⏳")
    return f"{icon} {match.result}"


def _actual_result_text(match: MatchBet) -> str:
    parts: list[str] = []
    if match.game_result:
        parts.append(match.game_result)
    if match.score:
        parts.append(match.score)
    if not parts:
        return "대기"
    return " | ".join(parts)


def _format_match_line(match: MatchBet, index: int) -> str:
    odds_text = f"({match.odds:.2f})" if match.odds else ""
    line = (
        f"{index}. {match.home_team} vs {match.away_team} | "
        f"선택 {match.bet_selection or '-'}{odds_text} | "
        f"실제 {_actual_result_text(match)}"
    )
    if match.result:
        line += f" | 내결과 {match.result}"
    return line


def _build_summary_embed(slips: list[BetSlip], mode_label: str) -> discord.Embed:
    total_purchase = sum(max(s.total_amount, 0) for s in slips)
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
    embed.add_field(name="총 실제적중금", value=_format_won(total_actual), inline=True)
    embed.add_field(name="총 손익", value=_format_won(total_actual - total_purchase), inline=True)
    return embed


def _build_analysis_embed(result: PurchaseAnalysis) -> discord.Embed:
    embed = discord.Embed(
        title=f"구매현황분석 (최근 {result.months}개월)",
        colour=discord.Color.dark_blue(),
    )
    embed.add_field(name="구매금액", value=_format_won(result.purchase_amount), inline=True)
    embed.add_field(name="적중금액", value=_format_won(result.winning_amount), inline=True)
    return embed


def _build_games_summary_embed(
    snapshot: SaleGamesSnapshot,
    selected_type_label: str,
    selected_sport_label: str,
) -> discord.Embed:
    embed = discord.Embed(
        title="발매중 전체 경기 요약",
        colour=discord.Color.green(),
    )
    embed.add_field(name="조회 타입", value=selected_type_label, inline=False)
    embed.add_field(name="조회 종목", value=selected_sport_label, inline=False)
    embed.add_field(name="수집시각", value=snapshot.fetched_at, inline=False)
    embed.add_field(name="전체 게임/전체 경기", value=f"{snapshot.total_games} / {snapshot.total_matches}", inline=False)

    if snapshot.sport_counts:
        sport_lines = [f"{sport}: {count}" for sport, count in snapshot.sport_counts.items()]
        embed.add_field(name="종목별 경기수", value="\n".join(sport_lines)[:1024], inline=False)
    else:
        embed.add_field(name="종목별 경기수", value="-", inline=False)
    if snapshot.partial_failures > 0:
        embed.add_field(name="부분 실패", value=f"{snapshot.partial_failures}개 게임 상세 수집 실패", inline=False)

    return embed


def _build_games_lines(snapshot: SaleGamesSnapshot) -> list[str]:
    lines: list[str] = []
    for idx, match in enumerate(snapshot.nearest_matches, start=1):
        sport = (match.sport or "").strip() or "기타"
        match_name = (match.match_name or "").strip() or "홈팀 미상 vs 원정팀 미상"
        game_type = (match.game_type or "").strip() or "-"
        round_label = (match.round_label or "").strip() or "회차 미상"
        start_at = (match.start_at or "").strip() or "-"
        sale_end_at = (match.sale_end_at or "").strip() or "-"
        lines.append(
            f"{idx}. [{sport}] {match_name} · 유형 {game_type} · {round_label} · 시작 {start_at} · 마감 {sale_end_at}"
        )
    return lines


def _build_games_message(
    snapshot: SaleGamesSnapshot,
    selected_type_label: str,
    selected_sport_label: str,
) -> tuple[discord.Embed, discord.File | None]:
    embed = _build_games_summary_embed(snapshot, selected_type_label, selected_sport_label)
    lines = _build_games_lines(snapshot)
    if lines:
        all_text = "\n".join(lines)
        # Keep embed readable and attach full list when too long.
        if len(all_text) <= 3500:
            embed.description = f"발매중 경기 {len(lines)}건\n\n{all_text}"
            file_obj: discord.File | None = None
        else:
            preview = ""
            for line in lines:
                candidate = line if not preview else f"{preview}\n{line}"
                if len(candidate) > 3000:
                    break
                preview = candidate
            embed.description = (
                f"발매중 경기 {len(lines)}건\n\n"
                f"{preview}\n\n"
                "전체 목록은 첨부파일을 확인해주세요."
            )
            stamp = snapshot.fetched_at.replace(".", "").replace(":", "").replace(" ", "_")
            file_obj = discord.File(
                io.BytesIO(all_text.encode("utf-8")),
                filename=f"games_{stamp}.txt",
            )
    else:
        embed.description = "발매중 경기 데이터가 없습니다."
        file_obj = None

    return embed, file_obj


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
        ]
        match_result_text = _match_result_text(match)
        if match_result_text:
            lines.append(f"내 베팅 결과: {match_result_text}")
        if match.match_datetime:
            lines.insert(1, f"경기시각: {match.match_datetime}")

        value = "\n".join(lines)
        if len(value) > 1024:
            value = value[:1010] + "..."
        embed.add_field(name=field_name[:256], value=value, inline=False)

    if len(slip.matches) > 12:
        embed.add_field(name="추가 경기", value=f"외 {len(slip.matches) - 12}경기", inline=False)

    return embed


def _build_compact_purchase_embeds(slips: list[BetSlip]) -> list[discord.Embed]:
    summary = _build_summary_embed(slips, "최근 5개")
    lines: list[str] = []

    for idx, slip in enumerate(slips, start=1):
        status = _status_text(slip)
        odds_text = f"{slip.combined_odds:.2f}" if slip.combined_odds else "-"
        lines.append(
            f"[{idx}] {_slip_icon(slip)} `{slip.slip_id}` · {status}"
        )
        lines.append(
            f"구매시각 {slip.purchase_datetime or '-'} · 구매 {_format_won(slip.total_amount)} · 배당 {odds_text}"
        )

        if not slip.matches:
            lines.append("  - 상세 경기 정보를 찾지 못했습니다.")
            lines.append("")
            continue

        for match_idx, match in enumerate(slip.matches, start=1):
            lines.append(_format_match_line(match, match_idx))
        lines.append("")

    chunks: list[str] = []
    current = ""
    max_len = 3800
    for line in lines:
        candidate = line if not current else f"{current}\n{line}"
        if len(candidate) > max_len:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)

    max_detail_embeds = 9  # summary + 9 detail = 10 embeds/message
    if len(chunks) > max_detail_embeds:
        chunks = chunks[:max_detail_embeds]
        truncated_note = "\n\n... 길이 제한으로 일부 경기는 생략되었습니다."
        if len(chunks[-1]) + len(truncated_note) > max_len:
            chunks[-1] = chunks[-1][: max_len - len(truncated_note)]
        chunks[-1] += truncated_note

    detail_embeds: list[discord.Embed] = []
    for i, text in enumerate(chunks, start=1):
        title = "상세" if len(chunks) == 1 else f"상세 ({i}/{len(chunks)})"
        detail_embeds.append(
            discord.Embed(
                title=title,
                description=text,
                colour=discord.Color.dark_teal(),
            )
        )

    return [summary] + detail_embeds


class Bot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.login_callback: Callable[[str, str, str], Awaitable[bool]] | None = None
        self.purchase_callback: Callable[[str], Awaitable[list[BetSlip]]] | None = None
        self.analysis_callback: Callable[[str, int], Awaitable[PurchaseAnalysis]] | None = None
        self.games_callback: Callable[[str, str], Awaitable[SaleGamesSnapshot]] | None = None
        self.logout_callback: Callable[[str], Awaitable[bool]] | None = None
        self.sync_guild_id: int | None = None

    async def _sync_application_commands(self) -> None:
        if self.sync_guild_id is not None:
            try:
                guild = discord.Object(id=self.sync_guild_id)
                self.tree.copy_global_to(guild=guild)
                guild_commands = await self.tree.sync(guild=guild)
                logger.info(
                    "Guild slash commands synced. guild_id=%s count=%d",
                    self.sync_guild_id,
                    len(guild_commands),
                )
            except Exception:
                logger.exception("Guild slash command sync failed. guild_id=%s", self.sync_guild_id)

        global_commands = await self.tree.sync()
        logger.info("Global slash commands synced. count=%d", len(global_commands))

    async def setup_hook(self) -> None:
        @self.tree.command(name="login", description="베트맨 로그인")
        async def login_command(interaction: discord.Interaction) -> None:
            if self.login_callback is None:
                await interaction.response.send_message("로그인 기능이 준비되지 않았습니다.", ephemeral=True)
                return
            default_user_id = _get_saved_login_id(str(interaction.user.id))
            await interaction.response.send_modal(
                LoginModal(
                    self.login_callback,
                    discord_user_id=str(interaction.user.id),
                    default_user_id=default_user_id,
                )
            )

        @self.tree.command(name="purchases", description="구매내역 조회")
        async def purchases_command(interaction: discord.Interaction) -> None:
            if self.purchase_callback is None:
                await interaction.response.send_message("구매내역 기능이 준비되지 않았습니다.", ephemeral=True)
                return

            await interaction.response.defer(thinking=True)
            try:
                slips = await self.purchase_callback(str(interaction.user.id))
            except Exception as exc:
                logger.exception("Failed to scrape purchases")
                await interaction.followup.send(f"구매내역 조회 실패: {exc}")
                return

            if not slips:
                await interaction.followup.send("조회된 구매내역이 없습니다.")
                return

            embeds = _build_compact_purchase_embeds(slips)
            await interaction.followup.send(embeds=embeds)

        @self.tree.command(name="analysis", description="구매현황분석 조회")
        @app_commands.describe(months="조회 개월 수 (1~12, 기본 12)")
        async def analysis_command(
            interaction: discord.Interaction,
            months: app_commands.Range[int, 1, 12] = 12,
        ) -> None:
            if self.analysis_callback is None:
                await interaction.response.send_message("구매현황분석 기능이 준비되지 않았습니다.", ephemeral=True)
                return

            await interaction.response.defer(thinking=True)
            try:
                result = await self.analysis_callback(str(interaction.user.id), int(months))
            except Exception as exc:
                logger.exception("Failed to scrape purchase analysis")
                await interaction.followup.send(f"구매현황분석 조회 실패: {exc}")
                return

            await interaction.followup.send(embed=_build_analysis_embed(result))

        @self.tree.command(name="games", description="발매중 전체 경기 요약 조회")
        @app_commands.describe(
            game_type="게임 타입 필터 (기본: 승부식)",
            sport="스포츠 종목 필터 (기본: 전체)",
        )
        @app_commands.choices(
            game_type=[
                app_commands.Choice(name="승부식", value="victory"),
                app_commands.Choice(name="승무패", value="windrawlose"),
                app_commands.Choice(name="기록식", value="record"),
                app_commands.Choice(name="전체", value="all"),
            ],
            sport=[
                app_commands.Choice(name="전체", value="all"),
                app_commands.Choice(name="축구", value="soccer"),
                app_commands.Choice(name="야구", value="baseball"),
                app_commands.Choice(name="농구", value="basketball"),
                app_commands.Choice(name="배구", value="volleyball"),
            ],
        )
        async def games_command(
            interaction: discord.Interaction,
            game_type: app_commands.Choice[str] | None = None,
            sport: app_commands.Choice[str] | None = None,
        ) -> None:
            if self.games_callback is None:
                await interaction.response.send_message("경기 조회 기능이 준비되지 않았습니다.", ephemeral=True)
                return

            await interaction.response.defer(thinking=True)
            selected_type = game_type.value if game_type is not None else "victory"
            selected_sport = sport.value if sport is not None else "all"
            selected_type_label = _GAME_TYPE_LABEL_BY_VALUE.get(selected_type, "전체")
            selected_sport_label = _SPORT_LABEL_BY_VALUE.get(selected_sport, "전체")
            try:
                snapshot = await self.games_callback(selected_type, selected_sport)
            except Exception as exc:
                logger.exception("Failed to scrape sale games")
                await interaction.followup.send(f"경기 조회 실패: {exc}")
                return

            if snapshot.total_matches <= 0:
                await interaction.followup.send(
                    f"조회 타입({selected_type_label}), 종목({selected_sport_label})의 발매중 경기가 없습니다."
                )
                return

            embed, file_obj = _build_games_message(snapshot, selected_type_label, selected_sport_label)
            if file_obj is not None:
                await interaction.followup.send(embed=embed, file=file_obj)
            else:
                await interaction.followup.send(embed=embed)

        @self.tree.command(name="logout", description="베트맨 로그아웃")
        async def logout_command(interaction: discord.Interaction) -> None:
            if self.logout_callback is None:
                await interaction.response.send_message("로그아웃 기능이 준비되지 않았습니다.", ephemeral=True)
                return

            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                ok = await self.logout_callback(str(interaction.user.id))
            except Exception as exc:
                logger.exception("Failed to logout")
                await interaction.followup.send(f"로그아웃 실패: {exc}", ephemeral=True)
                return

            if ok:
                await interaction.followup.send("로그아웃 완료", ephemeral=True)
            else:
                await interaction.followup.send("로그아웃 실패", ephemeral=True)

        await self._sync_application_commands()

    async def on_ready(self) -> None:
        logger.info("Bot ready: %s", self.user)
