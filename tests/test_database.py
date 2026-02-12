from __future__ import annotations

import json

import pytest

from src.database import Database
from src.models import BetSlip, MatchBet
from tests.conftest import DISCORD_USER_A, DISCORD_USER_B


def _make_slip(
    slip_id: str = "001",
    status: str = "발매중",
    result: str | None = None,
    total_amount: int = 5000,
    actual_payout: int = 0,
    purchase_datetime: str = "2025-03-15 10:00",
    matches: list[MatchBet] | None = None,
) -> BetSlip:
    return BetSlip(
        slip_id=slip_id,
        game_type="프로토 승부식",
        round_number="제100회",
        status=status,
        purchase_datetime=purchase_datetime,
        total_amount=total_amount,
        potential_payout=10000,
        combined_odds=2.0,
        result=result,
        actual_payout=actual_payout,
        matches=matches or [
            MatchBet(
                match_number=1,
                sport="축구",
                league="K리그1",
                home_team="전북",
                away_team="울산",
                bet_selection="홈승",
                odds=2.0,
                match_datetime="2025-03-15 19:00",
            )
        ],
    )


class TestDatabaseInit:
    async def test_tables_created(self, db: Database):
        async with db.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ) as cursor:
            tables = {row["name"] for row in await cursor.fetchall()}
        assert "users" in tables
        assert "bet_slips" in tables
        assert "match_bets" in tables
        assert "notification_filters" in tables


class TestUserCRUD:
    async def test_register_and_get_user(self, db: Database):
        await db.register_user(DISCORD_USER_A, "betman_a", "pw_a", "dm")
        user = await db.get_user(DISCORD_USER_A)
        assert user is not None
        assert user["betman_user_id"] == "betman_a"
        assert user["notify_via"] == "dm"

    async def test_register_updates_existing(self, db: Database):
        await db.register_user(DISCORD_USER_A, "betman_a", "pw_a", "dm")
        await db.register_user(DISCORD_USER_A, "betman_a2", "pw_a2", "channel")
        user = await db.get_user(DISCORD_USER_A)
        assert user["betman_user_id"] == "betman_a2"
        assert user["notify_via"] == "channel"

    async def test_remove_user(self, db: Database):
        await db.register_user(DISCORD_USER_A, "betman_a", "pw_a")
        await db.remove_user(DISCORD_USER_A)
        user = await db.get_user(DISCORD_USER_A)
        assert user is None

    async def test_get_nonexistent_user(self, db: Database):
        user = await db.get_user("999999999999999999")
        assert user is None

    async def test_get_all_users(self, db: Database):
        await db.register_user(DISCORD_USER_A, "a", "pa")
        await db.register_user(DISCORD_USER_B, "b", "pb")
        users = await db.get_all_users()
        assert len(users) == 2


class TestUpsert:
    async def test_insert_new_slip(self, db: Database):
        slip = _make_slip()
        is_new = await db.upsert_slip(slip, DISCORD_USER_A)
        assert is_new is True

    async def test_duplicate_insert_returns_false(self, db: Database):
        slip = _make_slip()
        await db.upsert_slip(slip, DISCORD_USER_A)
        is_new = await db.upsert_slip(slip, DISCORD_USER_A)
        assert is_new is False

    async def test_same_slip_different_users(self, db: Database):
        slip = _make_slip()
        is_new_a = await db.upsert_slip(slip, DISCORD_USER_A)
        is_new_b = await db.upsert_slip(slip, DISCORD_USER_B)
        assert is_new_a is True
        assert is_new_b is True

    async def test_upsert_preserves_matches(self, db: Database):
        slip = _make_slip()
        await db.upsert_slip(slip, DISCORD_USER_A)
        loaded = await db._load_slip("001", DISCORD_USER_A)
        assert len(loaded.matches) == 1
        assert loaded.matches[0].sport == "축구"

    async def test_upsert_updates_status(self, db: Database):
        slip = _make_slip(status="발매중")
        await db.upsert_slip(slip, DISCORD_USER_A)

        slip2 = _make_slip(status="발매마감")
        await db.upsert_slip(slip2, DISCORD_USER_A)

        loaded = await db._load_slip("001", DISCORD_USER_A)
        assert loaded.status == "발매마감"

    async def test_upsert_updates_result(self, db: Database):
        slip = _make_slip(status="발매중")
        await db.upsert_slip(slip, DISCORD_USER_A)

        slip2 = _make_slip(status="적중", result="적중", actual_payout=10000)
        await db.upsert_slip(slip2, DISCORD_USER_A)

        loaded = await db._load_slip("001", DISCORD_USER_A)
        assert loaded.result == "적중"
        assert loaded.actual_payout == 10000


class TestNotifications:
    async def test_unnotified_purchases(self, db: Database):
        await db.upsert_slip(_make_slip("A"), DISCORD_USER_A)
        await db.upsert_slip(_make_slip("B"), DISCORD_USER_A)

        unnotified = await db.get_unnotified_purchases(DISCORD_USER_A)
        assert len(unnotified) == 2

    async def test_unnotified_purchases_user_isolation(self, db: Database):
        await db.upsert_slip(_make_slip("A"), DISCORD_USER_A)
        await db.upsert_slip(_make_slip("B"), DISCORD_USER_B)

        unnotified_a = await db.get_unnotified_purchases(DISCORD_USER_A)
        unnotified_b = await db.get_unnotified_purchases(DISCORD_USER_B)
        assert len(unnotified_a) == 1
        assert len(unnotified_b) == 1

    async def test_mark_purchase_notified(self, db: Database):
        await db.upsert_slip(_make_slip("A"), DISCORD_USER_A)
        await db.mark_purchase_notified("A", DISCORD_USER_A)

        unnotified = await db.get_unnotified_purchases(DISCORD_USER_A)
        assert len(unnotified) == 0

    async def test_unnotified_results(self, db: Database):
        await db.upsert_slip(_make_slip("A", result="적중"), DISCORD_USER_A)
        await db.upsert_slip(_make_slip("B", result="미적중"), DISCORD_USER_A)
        await db.upsert_slip(_make_slip("C"), DISCORD_USER_A)  # no result

        unnotified = await db.get_unnotified_results(DISCORD_USER_A)
        assert len(unnotified) == 2

    async def test_mark_result_notified(self, db: Database):
        await db.upsert_slip(_make_slip("A", result="적중"), DISCORD_USER_A)
        await db.mark_result_notified("A", DISCORD_USER_A)

        unnotified = await db.get_unnotified_results(DISCORD_USER_A)
        assert len(unnotified) == 0

    async def test_pending_results(self, db: Database):
        await db.upsert_slip(_make_slip("A", status="발매중"), DISCORD_USER_A)
        await db.upsert_slip(_make_slip("B", status="발매마감"), DISCORD_USER_A)
        await db.upsert_slip(
            _make_slip("C", status="적중", result="적중"), DISCORD_USER_A
        )

        pending = await db.get_pending_results(DISCORD_USER_A)
        assert len(pending) == 2


class TestUpdateResult:
    async def test_update_result(self, db: Database):
        await db.upsert_slip(_make_slip("A"), DISCORD_USER_A)
        await db.update_result("A", "적중", 10000, DISCORD_USER_A)

        loaded = await db._load_slip("A", DISCORD_USER_A)
        assert loaded.result == "적중"
        assert loaded.actual_payout == 10000


class TestStatistics:
    async def test_empty_stats(self, db: Database):
        stats = await db.get_statistics(DISCORD_USER_A)
        assert stats["total"] == 0
        assert stats["win_rate"] == 0.0

    async def test_stats_with_data(self, db: Database):
        await db.upsert_slip(
            _make_slip("A", result="적중", actual_payout=10000, total_amount=5000),
            DISCORD_USER_A,
        )
        await db.upsert_slip(
            _make_slip("B", result="미적중", total_amount=3000), DISCORD_USER_A
        )
        await db.upsert_slip(
            _make_slip("C", result="적중", actual_payout=8000, total_amount=4000),
            DISCORD_USER_A,
        )
        await db.upsert_slip(
            _make_slip("D", status="발매중", total_amount=2000), DISCORD_USER_A
        )

        stats = await db.get_statistics(DISCORD_USER_A)
        assert stats["total"] == 4
        assert stats["wins"] == 2
        assert stats["losses"] == 1
        assert stats["settled"] == 3
        assert stats["pending"] == 1
        assert stats["win_rate"] == pytest.approx(66.67, abs=0.01)
        assert stats["total_spent"] == 14000
        assert stats["total_payout"] == 18000
        assert stats["profit"] == 4000

    async def test_stats_user_isolation(self, db: Database):
        await db.upsert_slip(
            _make_slip("A", result="적중", actual_payout=10000, total_amount=5000),
            DISCORD_USER_A,
        )
        await db.upsert_slip(
            _make_slip("B", result="미적중", total_amount=3000), DISCORD_USER_B
        )

        stats_a = await db.get_statistics(DISCORD_USER_A)
        stats_b = await db.get_statistics(DISCORD_USER_B)
        assert stats_a["total"] == 1
        assert stats_a["wins"] == 1
        assert stats_b["total"] == 1
        assert stats_b["losses"] == 1

    async def test_daily_stats(self, db: Database):
        await db.upsert_slip(
            _make_slip("A", purchase_datetime="2025-03-15 10:00"),
            DISCORD_USER_A,
        )
        data = await db.get_daily_stats(days=365, discord_user_id=DISCORD_USER_A)
        assert len(data) >= 1

    async def test_monthly_stats(self, db: Database):
        await db.upsert_slip(
            _make_slip("A", purchase_datetime="2025-03-15 10:00"),
            DISCORD_USER_A,
        )
        data = await db.get_monthly_stats(
            months=12, discord_user_id=DISCORD_USER_A
        )
        assert len(data) >= 1


class TestFilters:
    async def test_set_and_get_filter(self, db: Database):
        await db.set_filter("min_amount", "5000")
        val = await db.get_filter("min_amount")
        assert val == "5000"

    async def test_get_missing_filter(self, db: Database):
        val = await db.get_filter("nonexistent")
        assert val is None

    async def test_delete_filter(self, db: Database):
        await db.set_filter("min_amount", "5000")
        await db.delete_filter("min_amount")
        val = await db.get_filter("min_amount")
        assert val is None

    async def test_get_all_filters(self, db: Database):
        await db.set_filter("min_amount", "5000")
        await db.set_filter("sports", '["축구"]')
        filters = await db.get_all_filters()
        assert filters == {"min_amount": "5000", "sports": '["축구"]'}

    async def test_upsert_filter(self, db: Database):
        await db.set_filter("min_amount", "5000")
        await db.set_filter("min_amount", "10000")
        val = await db.get_filter("min_amount")
        assert val == "10000"


class TestMigration:
    async def test_migrate_from_json(self, db: Database, tmp_path):
        json_path = tmp_path / "notified.json"
        json_path.write_text(json.dumps(["id1", "id2", "id3"]), encoding="utf-8")

        count = await db.migrate_from_json(json_path)
        assert count == 3

        # Verify they're marked as purchase_notified
        row = await db._get_slip_row("id1", "")
        assert row is not None
        assert row["purchase_notified"] == 1

    async def test_migrate_nonexistent_file(self, db: Database, tmp_path):
        count = await db.migrate_from_json(tmp_path / "nope.json")
        assert count == 0
