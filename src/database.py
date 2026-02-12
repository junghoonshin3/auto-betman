from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import aiosqlite

from src.models import BetSlip, MatchBet

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    discord_user_id  TEXT PRIMARY KEY,
    betman_user_id   TEXT NOT NULL,
    betman_user_pw   TEXT NOT NULL,
    notify_via       TEXT NOT NULL DEFAULT 'dm',
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS bet_slips (
    slip_id           TEXT NOT NULL,
    discord_user_id   TEXT NOT NULL DEFAULT '',
    game_type         TEXT NOT NULL DEFAULT '',
    round_number      TEXT NOT NULL DEFAULT '',
    status            TEXT NOT NULL DEFAULT '',
    purchase_datetime TEXT NOT NULL DEFAULT '',
    total_amount      INTEGER NOT NULL DEFAULT 0,
    potential_payout  INTEGER NOT NULL DEFAULT 0,
    combined_odds     REAL NOT NULL DEFAULT 0,
    result            TEXT,
    actual_payout     INTEGER NOT NULL DEFAULT 0,
    purchase_notified INTEGER NOT NULL DEFAULT 0,
    result_notified   INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at        TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (slip_id, discord_user_id)
);

CREATE TABLE IF NOT EXISTS match_bets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    slip_id         TEXT NOT NULL,
    discord_user_id TEXT NOT NULL DEFAULT '',
    match_number    INTEGER NOT NULL DEFAULT 0,
    sport           TEXT NOT NULL DEFAULT '',
    league          TEXT NOT NULL DEFAULT '',
    home_team       TEXT NOT NULL DEFAULT '',
    away_team       TEXT NOT NULL DEFAULT '',
    bet_selection   TEXT NOT NULL DEFAULT '',
    odds            REAL NOT NULL DEFAULT 0,
    match_datetime  TEXT NOT NULL DEFAULT '',
    result          TEXT,
    UNIQUE(slip_id, discord_user_id, match_number)
);

CREATE TABLE IF NOT EXISTS notification_filters (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);
"""

_MIGRATION_ADD_DISCORD_USER_ID = """
-- Add discord_user_id to bet_slips if it doesn't exist
ALTER TABLE bet_slips ADD COLUMN discord_user_id TEXT NOT NULL DEFAULT '';
"""


class Database:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def init(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_SCHEMA)
        await self._db.commit()
        logger.info("Database initialized at %s", self._db_path)

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Database not initialized. Call init() first.")
        return self._db

    # ------------------------------------------------------------------
    # User CRUD
    # ------------------------------------------------------------------

    async def register_user(
        self,
        discord_user_id: str,
        betman_user_id: str,
        betman_user_pw: str,
        notify_via: str = "dm",
    ) -> None:
        await self.db.execute(
            """INSERT INTO users (discord_user_id, betman_user_id, betman_user_pw, notify_via)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(discord_user_id) DO UPDATE SET
                   betman_user_id = excluded.betman_user_id,
                   betman_user_pw = excluded.betman_user_pw,
                   notify_via = excluded.notify_via""",
            (discord_user_id, betman_user_id, betman_user_pw, notify_via),
        )
        await self.db.commit()

    async def remove_user(self, discord_user_id: str) -> None:
        await self.db.execute(
            "DELETE FROM users WHERE discord_user_id = ?", (discord_user_id,)
        )
        await self.db.commit()

    async def get_user(self, discord_user_id: str) -> aiosqlite.Row | None:
        async with self.db.execute(
            "SELECT * FROM users WHERE discord_user_id = ?", (discord_user_id,)
        ) as cursor:
            return await cursor.fetchone()

    async def get_all_users(self) -> list[aiosqlite.Row]:
        async with self.db.execute("SELECT * FROM users") as cursor:
            return await cursor.fetchall()

    # ------------------------------------------------------------------
    # Upsert
    # ------------------------------------------------------------------

    async def upsert_slip(self, slip: BetSlip, discord_user_id: str = "") -> bool:
        """Insert or update a slip. Returns True if it was newly inserted."""
        row = await self._get_slip_row(slip.slip_id, discord_user_id)
        is_new = row is None

        await self.db.execute(
            """
            INSERT INTO bet_slips
                (slip_id, discord_user_id, game_type, round_number, status,
                 purchase_datetime, total_amount, potential_payout, combined_odds,
                 result, actual_payout, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(slip_id, discord_user_id) DO UPDATE SET
                status = excluded.status,
                result = COALESCE(excluded.result, result),
                actual_payout = CASE WHEN excluded.actual_payout > 0
                                     THEN excluded.actual_payout ELSE actual_payout END,
                updated_at = datetime('now')
            """,
            (
                slip.slip_id,
                discord_user_id,
                slip.game_type,
                slip.round_number,
                slip.status,
                slip.purchase_datetime,
                slip.total_amount,
                slip.potential_payout,
                slip.combined_odds,
                slip.result,
                slip.actual_payout,
            ),
        )

        # Upsert match bets
        for m in slip.matches:
            await self.db.execute(
                """
                INSERT INTO match_bets
                    (slip_id, discord_user_id, match_number, sport, league,
                     home_team, away_team, bet_selection, odds, match_datetime, result)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(slip_id, discord_user_id, match_number) DO UPDATE SET
                    result = COALESCE(excluded.result, result)
                """,
                (
                    slip.slip_id,
                    discord_user_id,
                    m.match_number,
                    m.sport,
                    m.league,
                    m.home_team,
                    m.away_team,
                    m.bet_selection,
                    m.odds,
                    m.match_datetime,
                    m.result,
                ),
            )

        await self.db.commit()
        return is_new

    async def _get_slip_row(
        self, slip_id: str, discord_user_id: str = ""
    ) -> aiosqlite.Row | None:
        async with self.db.execute(
            "SELECT * FROM bet_slips WHERE slip_id = ? AND discord_user_id = ?",
            (slip_id, discord_user_id),
        ) as cursor:
            return await cursor.fetchone()

    async def get_latest_purchase_datetime(
        self, discord_user_id: str = ""
    ) -> str | None:
        """Return the latest purchase_datetime for a user, or None if no slips."""
        async with self.db.execute(
            "SELECT MAX(purchase_datetime) FROM bet_slips WHERE discord_user_id = ?",
            (discord_user_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row and row[0] else None

    # ------------------------------------------------------------------
    # Notification tracking
    # ------------------------------------------------------------------

    async def mark_purchase_notified(
        self, slip_id: str, discord_user_id: str = ""
    ) -> None:
        await self.db.execute(
            "UPDATE bet_slips SET purchase_notified = 1 WHERE slip_id = ? AND discord_user_id = ?",
            (slip_id, discord_user_id),
        )
        await self.db.commit()

    async def mark_result_notified(
        self, slip_id: str, discord_user_id: str = ""
    ) -> None:
        await self.db.execute(
            "UPDATE bet_slips SET result_notified = 1 WHERE slip_id = ? AND discord_user_id = ?",
            (slip_id, discord_user_id),
        )
        await self.db.commit()

    async def get_unnotified_purchases(
        self, discord_user_id: str = ""
    ) -> list[BetSlip]:
        """Get slips where purchase has not been notified yet."""
        async with self.db.execute(
            "SELECT slip_id FROM bet_slips WHERE purchase_notified = 0 AND discord_user_id = ?",
            (discord_user_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            await self._load_slip(row["slip_id"], discord_user_id) for row in rows
        ]

    async def get_unnotified_results(
        self, discord_user_id: str = ""
    ) -> list[BetSlip]:
        """Get slips that have a result but result notification not yet sent."""
        async with self.db.execute(
            """SELECT slip_id FROM bet_slips
               WHERE result IS NOT NULL AND result != '' AND result_notified = 0
                     AND discord_user_id = ?""",
            (discord_user_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            await self._load_slip(row["slip_id"], discord_user_id) for row in rows
        ]

    async def get_pending_results(
        self, discord_user_id: str = ""
    ) -> list[BetSlip]:
        """Get slips still awaiting results."""
        async with self.db.execute(
            """SELECT slip_id FROM bet_slips
               WHERE status IN ('발매중', '발매마감')
                     AND (result IS NULL OR result = '')
                     AND discord_user_id = ?""",
            (discord_user_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            await self._load_slip(row["slip_id"], discord_user_id) for row in rows
        ]

    async def update_result(
        self,
        slip_id: str,
        result: str,
        actual_payout: int = 0,
        discord_user_id: str = "",
    ) -> None:
        await self.db.execute(
            """UPDATE bet_slips
               SET result = ?, actual_payout = ?, updated_at = datetime('now')
               WHERE slip_id = ? AND discord_user_id = ?""",
            (result, actual_payout, slip_id, discord_user_id),
        )
        await self.db.commit()

    # ------------------------------------------------------------------
    # Load full slip with matches
    # ------------------------------------------------------------------

    async def _load_slip(
        self, slip_id: str, discord_user_id: str = ""
    ) -> BetSlip:
        row = await self._get_slip_row(slip_id, discord_user_id)
        if row is None:
            raise ValueError(f"Slip {slip_id} not found")

        async with self.db.execute(
            "SELECT * FROM match_bets WHERE slip_id = ? AND discord_user_id = ? ORDER BY match_number",
            (slip_id, discord_user_id),
        ) as cursor:
            match_rows = await cursor.fetchall()

        matches = [
            MatchBet(
                match_number=mr["match_number"],
                sport=mr["sport"],
                league=mr["league"],
                home_team=mr["home_team"],
                away_team=mr["away_team"],
                bet_selection=mr["bet_selection"],
                odds=mr["odds"],
                match_datetime=mr["match_datetime"],
                result=mr["result"],
            )
            for mr in match_rows
        ]

        return BetSlip(
            slip_id=row["slip_id"],
            game_type=row["game_type"],
            round_number=row["round_number"],
            status=row["status"],
            purchase_datetime=row["purchase_datetime"],
            total_amount=row["total_amount"],
            potential_payout=row["potential_payout"],
            combined_odds=row["combined_odds"],
            result=row["result"],
            actual_payout=row["actual_payout"],
            matches=matches,
        )

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------

    async def get_statistics(self, discord_user_id: str = "") -> dict[str, Any]:
        """Overall statistics."""
        async with self.db.execute(
            """SELECT
                COUNT(*) as total,
                SUM(CASE WHEN result = '적중' THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN result = '미적중' THEN 1 ELSE 0 END) as losses,
                SUM(CASE WHEN result = '취소' THEN 1 ELSE 0 END) as cancelled,
                SUM(CASE WHEN result IS NOT NULL AND result != '' THEN 1 ELSE 0 END) as settled,
                SUM(total_amount) as total_spent,
                SUM(actual_payout) as total_payout
            FROM bet_slips WHERE discord_user_id = ?""",
            (discord_user_id,),
        ) as cursor:
            row = await cursor.fetchone()

        total = row["total"] or 0
        wins = row["wins"] or 0
        losses = row["losses"] or 0
        cancelled = row["cancelled"] or 0
        settled = row["settled"] or 0
        total_spent = row["total_spent"] or 0
        total_payout = row["total_payout"] or 0

        decided = wins + losses
        win_rate = (wins / decided * 100) if decided > 0 else 0.0

        return {
            "total": total,
            "wins": wins,
            "losses": losses,
            "cancelled": cancelled,
            "settled": settled,
            "pending": total - settled,
            "win_rate": win_rate,
            "total_spent": total_spent,
            "total_payout": total_payout,
            "profit": total_payout - total_spent,
        }

    async def get_daily_stats(
        self, days: int = 7, discord_user_id: str = ""
    ) -> list[dict[str, Any]]:
        async with self.db.execute(
            """SELECT
                DATE(purchase_datetime) as day,
                COUNT(*) as total,
                SUM(CASE WHEN result = '적중' THEN 1 ELSE 0 END) as wins,
                SUM(total_amount) as spent,
                SUM(actual_payout) as payout
            FROM bet_slips
            WHERE purchase_datetime >= date('now', ?) AND discord_user_id = ?
            GROUP BY DATE(purchase_datetime)
            ORDER BY day DESC""",
            (f"-{days} days", discord_user_id),
        ) as cursor:
            rows = await cursor.fetchall()

        return [
            {
                "day": r["day"],
                "total": r["total"],
                "wins": r["wins"] or 0,
                "spent": r["spent"] or 0,
                "payout": r["payout"] or 0,
                "profit": (r["payout"] or 0) - (r["spent"] or 0),
            }
            for r in rows
        ]

    async def get_monthly_stats(
        self, months: int = 6, discord_user_id: str = ""
    ) -> list[dict[str, Any]]:
        async with self.db.execute(
            """SELECT
                STRFTIME('%Y-%m', purchase_datetime) as month,
                COUNT(*) as total,
                SUM(CASE WHEN result = '적중' THEN 1 ELSE 0 END) as wins,
                SUM(total_amount) as spent,
                SUM(actual_payout) as payout
            FROM bet_slips
            WHERE purchase_datetime >= date('now', ?) AND discord_user_id = ?
            GROUP BY STRFTIME('%Y-%m', purchase_datetime)
            ORDER BY month DESC""",
            (f"-{months} months", discord_user_id),
        ) as cursor:
            rows = await cursor.fetchall()

        return [
            {
                "month": r["month"],
                "total": r["total"],
                "wins": r["wins"] or 0,
                "spent": r["spent"] or 0,
                "payout": r["payout"] or 0,
                "profit": (r["payout"] or 0) - (r["spent"] or 0),
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Notification filters
    # ------------------------------------------------------------------

    async def get_filter(self, key: str) -> str | None:
        async with self.db.execute(
            "SELECT value FROM notification_filters WHERE key = ?", (key,)
        ) as cursor:
            row = await cursor.fetchone()
        return row["value"] if row else None

    async def set_filter(self, key: str, value: str) -> None:
        await self.db.execute(
            "INSERT INTO notification_filters (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self.db.commit()

    async def delete_filter(self, key: str) -> None:
        await self.db.execute(
            "DELETE FROM notification_filters WHERE key = ?", (key,)
        )
        await self.db.commit()

    async def get_all_filters(self) -> dict[str, str]:
        async with self.db.execute(
            "SELECT key, value FROM notification_filters"
        ) as cursor:
            rows = await cursor.fetchall()
        return {r["key"]: r["value"] for r in rows}

    # ------------------------------------------------------------------
    # Migration from last_notified.json
    # ------------------------------------------------------------------

    async def migrate_from_json(self, json_path: Path) -> int:
        """Import notified IDs from legacy JSON file. Returns count imported."""
        if not json_path.exists():
            return 0
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                return 0
            count = 0
            for slip_id in data:
                await self.db.execute(
                    """INSERT INTO bet_slips (slip_id, discord_user_id, purchase_notified)
                       VALUES (?, '', 1)
                       ON CONFLICT(slip_id, discord_user_id) DO UPDATE SET purchase_notified = 1""",
                    (str(slip_id),),
                )
                count += 1
            await self.db.commit()
            logger.info("Migrated %d IDs from %s", count, json_path)
            return count
        except Exception as exc:
            logger.warning("Migration from JSON failed: %s", exc)
            return 0
