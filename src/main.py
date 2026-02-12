"""
Betman Purchase History Scraper + Discord Bot

Usage:
    python -m src.main              # 1회 스크래핑 후 Discord 전송
    python -m src.main --schedule   # 주기적 반복 실행
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.auth import BetmanAuth
from src.browser import BrowserManager
from src.config import Config, STORAGE_DIR
from src.database import Database
from src.discord_bot import BetmanBot
from src.health import start_health_server
from src.scraper import BetmanScraper

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


class Orchestrator:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.browser_manager = BrowserManager(config)
        self.auth = BetmanAuth(config, self.browser_manager)
        self.scraper = BetmanScraper(config)
        self.bot = BetmanBot(config)
        self.database = Database(config.db_path)
        self._scheduler: AsyncIOScheduler | None = None
        self._health_runner = None

    async def _init_db(self) -> None:
        await self.database.init()
        self.bot.database = self.database
        # Migrate legacy JSON if exists
        await self.database.migrate_from_json(self.config.last_notified_path)

    async def _close_db(self) -> None:
        await self.database.close()

    # ------------------------------------------------------------------
    # Per-user scrape helpers
    # ------------------------------------------------------------------

    async def _scrape_for_user(self, user_row) -> list:
        """Scrape all history for a single registered user."""
        discord_user_id = user_row["discord_user_id"]
        betman_user_id = user_row["betman_user_id"]
        betman_user_pw = user_row["betman_user_pw"]

        session_path = STORAGE_DIR / f"session_{discord_user_id}.json"
        context, page = await self.browser_manager.create_user_context(session_path)
        try:
            await self.auth.ensure_logged_in(page, betman_user_id, betman_user_pw)
            slips = await self.scraper.scrape_all_history(page)
            return slips
        finally:
            await self.browser_manager.close_user_context(context, session_path)

    async def fetch_games_for_user(self, discord_user_id: str) -> tuple[str, list]:
        """Fetch available games for a user (called from /games command)."""
        user_row = await self.database.get_user(discord_user_id)
        if not user_row:
            logger.warning("User %s not registered", discord_user_id)
            return "", []

        session_path = STORAGE_DIR / f"session_{discord_user_id}.json"
        context, page = await self.browser_manager.create_user_context(session_path)
        try:
            await self.auth.ensure_logged_in(
                page, user_row["betman_user_id"], user_row["betman_user_pw"]
            )
            round_title, games = await self.scraper.scrape_available_games(page)
            return round_title, games
        finally:
            await self.browser_manager.close_user_context(context, session_path)

    async def run_scrape_cycle_for_user(self, discord_user_id: str) -> list:
        """Run a scrape cycle for a single user (called from slash commands)."""
        user_row = await self.database.get_user(discord_user_id)
        if not user_row:
            logger.warning("User %s not registered", discord_user_id)
            return []
        return await self._scrape_for_user(user_row)

    # ------------------------------------------------------------------
    # Core scrape cycle (multi-user)
    # ------------------------------------------------------------------

    async def scrape_and_notify(self) -> None:
        """Scrape all history for all registered users, upsert to DB,
        send purchase + result notifications."""
        try:
            users = await self.database.get_all_users()
            if not users:
                logger.info("No registered users, skipping scrape cycle")
                return

            for user_row in users:
                await self._scrape_and_notify_for_user(user_row)

        except Exception:
            logger.exception("Scrape cycle failed")

    async def _scrape_and_notify_for_user(self, user_row) -> None:
        """Scrape + notify for a single user."""
        discord_user_id = user_row["discord_user_id"]
        try:
            slips = await self._scrape_for_user(user_row)

            if not slips:
                logger.info("No slips found for user %s", discord_user_id)
                return

            # Upsert all slips to DB
            for slip in slips:
                await self.database.upsert_slip(slip, discord_user_id)

            # Send purchase notifications for unnotified slips
            unnotified_purchases = await self.database.get_unnotified_purchases(
                discord_user_id
            )
            if unnotified_purchases:
                await self.bot.send_slips(unnotified_purchases, discord_user_id)

            # Send result notifications
            unnotified_results = await self.database.get_unnotified_results(
                discord_user_id
            )
            if unnotified_results:
                await self.bot.send_results(unnotified_results, discord_user_id)

        except Exception:
            logger.exception(
                "Scrape cycle failed for user %s", discord_user_id
            )

    # ------------------------------------------------------------------
    # Legacy single-user scrape (kept for backward compat)
    # ------------------------------------------------------------------

    async def run_scrape_cycle(self) -> list:
        """Run a single scrape cycle using the default browser context."""
        page = self.browser_manager.page
        await self.auth.ensure_logged_in(page)
        slips = await self.scraper.scrape_purchase_history(page)
        await self.browser_manager.save_session()
        return slips

    # ------------------------------------------------------------------
    # Run modes
    # ------------------------------------------------------------------

    async def run_once(self) -> None:
        """Manual mode: start bot, run one scrape for all users, then shut down."""
        await self._init_db()
        bot_task = asyncio.create_task(self.bot.start(self.config.discord_bot_token))

        # Wait until the bot is ready
        while not self.bot.is_ready():
            await asyncio.sleep(0.5)

        try:
            await self.browser_manager.start()
            await self.scrape_and_notify()
        except Exception:
            logger.exception("Single run failed")
        finally:
            await self.browser_manager.shutdown()
            await self._close_db()
            await self.bot.close()
            bot_task.cancel()

    async def run_scheduled(self) -> None:
        """Schedule mode: run bot + periodic scraping."""
        self._health_runner = await start_health_server()
        await self._init_db()

        # Register callbacks for slash commands (per-user)
        self.bot.scrape_callback = self.run_scrape_cycle_for_user
        self.bot.games_callback = self.fetch_games_for_user

        # Start browser first
        await self.browser_manager.start()

        # Set up scheduler
        self._scheduler = AsyncIOScheduler()
        self._scheduler.add_job(
            self.scrape_and_notify,
            "interval",
            minutes=self.config.polling_interval_minutes,
            id="betman_scrape",
            max_instances=1,
        )

        # Run initial scrape after bot is ready
        original_on_ready = self.bot.on_ready

        async def _on_ready_with_scrape() -> None:
            await original_on_ready()
            self._scheduler.start()
            logger.info(
                "Scheduler started: scraping every %d minutes",
                self.config.polling_interval_minutes,
            )
            # Run immediately on startup
            await self.scrape_and_notify()

        self.bot.on_ready = _on_ready_with_scrape

        # Handle graceful shutdown
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(self._shutdown()))

        try:
            await self.bot.start(self.config.discord_bot_token)
        except asyncio.CancelledError:
            pass
        finally:
            await self._cleanup()

    async def _shutdown(self) -> None:
        logger.info("Shutdown signal received …")
        if self._scheduler:
            self._scheduler.shutdown(wait=False)
        await self.bot.close()

    async def _cleanup(self) -> None:
        if self._health_runner:
            await self._health_runner.cleanup()
        await self.browser_manager.shutdown()
        await self._close_db()
        logger.info("Cleanup complete")


def main() -> None:
    parser = argparse.ArgumentParser(description="Betman Tracker")
    parser.add_argument("--schedule", action="store_true", help="주기적 반복 실행 모드")
    args = parser.parse_args()

    config = Config.from_env()
    orchestrator = Orchestrator(config)

    if args.schedule:
        logger.info("Starting in SCHEDULE mode (every %d min)", config.polling_interval_minutes)
        asyncio.run(orchestrator.run_scheduled())
    else:
        logger.info("Starting in SINGLE-RUN mode")
        asyncio.run(orchestrator.run_once())


if __name__ == "__main__":
    main()
