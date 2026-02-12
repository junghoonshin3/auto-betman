from __future__ import annotations

import json

import pytest

from src.database import Database
from src.discord_bot import BetmanBot, _build_result_embed, _build_stats_embed
from src.models import BetSlip, MatchBet


def _make_slip(
    slip_id: str = "001",
    game_type: str = "프로토 승부식",
    total_amount: int = 5000,
    sports: list[str] | None = None,
) -> BetSlip:
    sport_list = sports or ["축구"]
    matches = [
        MatchBet(
            match_number=i + 1,
            sport=s,
            league="리그",
            home_team="홈",
            away_team="원정",
            bet_selection="홈승",
            odds=2.0,
            match_datetime="2025-03-15 19:00",
        )
        for i, s in enumerate(sport_list)
    ]
    return BetSlip(
        slip_id=slip_id,
        game_type=game_type,
        round_number="제1회",
        status="발매중",
        purchase_datetime="2025-03-15 10:00",
        total_amount=total_amount,
        potential_payout=10000,
        combined_odds=2.0,
        matches=matches,
    )


class TestCheckFilters:
    """Test the BetmanBot.check_filters async method."""

    async def _make_bot_with_db(self, db: Database) -> BetmanBot:
        from unittest.mock import MagicMock
        config = MagicMock()
        config.discord_channel_id = 123
        bot = BetmanBot.__new__(BetmanBot)
        bot.config = config
        bot.database = db
        return bot

    async def test_no_filters_passes(self, db: Database):
        bot = await self._make_bot_with_db(db)
        slip = _make_slip()
        assert await bot.check_filters(slip) is True

    async def test_min_amount_filter_passes(self, db: Database):
        await db.set_filter("min_amount", "3000")
        bot = await self._make_bot_with_db(db)
        slip = _make_slip(total_amount=5000)
        assert await bot.check_filters(slip) is True

    async def test_min_amount_filter_blocks(self, db: Database):
        await db.set_filter("min_amount", "10000")
        bot = await self._make_bot_with_db(db)
        slip = _make_slip(total_amount=5000)
        assert await bot.check_filters(slip) is False

    async def test_game_type_filter_passes(self, db: Database):
        await db.set_filter("game_types", json.dumps(["프로토"]))
        bot = await self._make_bot_with_db(db)
        slip = _make_slip(game_type="프로토 승부식")
        assert await bot.check_filters(slip) is True

    async def test_game_type_filter_blocks(self, db: Database):
        await db.set_filter("game_types", json.dumps(["토토"]))
        bot = await self._make_bot_with_db(db)
        slip = _make_slip(game_type="프로토 승부식")
        assert await bot.check_filters(slip) is False

    async def test_sport_filter_passes(self, db: Database):
        await db.set_filter("sports", json.dumps(["축구"]))
        bot = await self._make_bot_with_db(db)
        slip = _make_slip(sports=["축구", "야구"])
        assert await bot.check_filters(slip) is True

    async def test_sport_filter_blocks(self, db: Database):
        await db.set_filter("sports", json.dumps(["농구"]))
        bot = await self._make_bot_with_db(db)
        slip = _make_slip(sports=["축구", "야구"])
        assert await bot.check_filters(slip) is False

    async def test_combined_filters(self, db: Database):
        await db.set_filter("min_amount", "3000")
        await db.set_filter("sports", json.dumps(["축구"]))
        bot = await self._make_bot_with_db(db)

        # Passes both
        slip1 = _make_slip(total_amount=5000, sports=["축구"])
        assert await bot.check_filters(slip1) is True

        # Fails amount
        slip2 = _make_slip(total_amount=1000, sports=["축구"])
        assert await bot.check_filters(slip2) is False

        # Fails sport
        slip3 = _make_slip(total_amount=5000, sports=["야구"])
        assert await bot.check_filters(slip3) is False


class TestResultEmbed:
    def test_win_embed(self):
        slip = BetSlip(
            slip_id="W1",
            game_type="프로토",
            round_number="1회",
            status="적중",
            purchase_datetime="2025-01-01",
            total_amount=5000,
            potential_payout=10000,
            combined_odds=2.0,
            result="적중",
            actual_payout=10000,
        )
        embed = _build_result_embed(slip)
        assert "적중" in embed.title
        field_names = [f.name for f in embed.fields]
        assert "적중금액" in field_names
        assert "수익" in field_names

    def test_loss_embed(self):
        slip = BetSlip(
            slip_id="L1",
            game_type="토토",
            round_number="2회",
            status="미적중",
            purchase_datetime="2025-01-02",
            total_amount=3000,
            potential_payout=6000,
            combined_odds=2.0,
            result="미적중",
            actual_payout=0,
        )
        embed = _build_result_embed(slip)
        assert "미적중" in embed.title
        field_names = [f.name for f in embed.fields]
        assert "손실" in field_names


class TestStatsEmbed:
    def test_stats_embed(self):
        stats = {
            "total": 10,
            "wins": 4,
            "losses": 5,
            "cancelled": 0,
            "settled": 9,
            "pending": 1,
            "win_rate": 44.4,
            "total_spent": 50000,
            "total_payout": 40000,
            "profit": -10000,
        }
        embed = _build_stats_embed(stats)
        assert embed.title == "베팅 통계 (전체)"
        field_names = [f.name for f in embed.fields]
        assert "총 베팅 수" in field_names
        assert "적중률" in field_names
        assert "손익" in field_names
