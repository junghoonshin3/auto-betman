from __future__ import annotations

import json
import logging
from pathlib import Path

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright
from playwright_stealth import Stealth

_stealth = Stealth()

from src.config import Config

logger = logging.getLogger(__name__)

_CONTEXT_KWARGS_BASE: dict = {
    "viewport": {"width": 1280, "height": 900},
    "locale": "ko-KR",
    "timezone_id": "Asia/Seoul",
    "user_agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
}


class BrowserManager:
    def __init__(self, config: Config) -> None:
        self._config = config
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    async def start(self) -> Page:
        self._playwright = await async_playwright().start()

        launch_args = [
            "--disable-blink-features=AutomationControlled",
        ]

        self._browser = await self._playwright.chromium.launch(
            headless=self._config.headless,
            args=launch_args,
        )

        context_kwargs = dict(_CONTEXT_KWARGS_BASE)

        session_path = self._config.session_state_path
        if session_path.exists():
            logger.info("Restoring browser session from %s", session_path)
            context_kwargs["storage_state"] = str(session_path)

        self._context = await self._browser.new_context(**context_kwargs)
        self._page = await self._context.new_page()
        await _stealth.apply_stealth_async(self._page)

        logger.info("Browser started (headless=%s)", self._config.headless)
        return self._page

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("Browser not started. Call start() first.")
        return self._page

    @property
    def context(self) -> BrowserContext:
        if self._context is None:
            raise RuntimeError("Browser not started. Call start() first.")
        return self._context

    async def save_session(self) -> None:
        if self._context is None:
            return
        session_path = self._config.session_state_path
        session_path.parent.mkdir(parents=True, exist_ok=True)
        state = await self._context.storage_state()
        session_path.write_text(json.dumps(state, ensure_ascii=False, indent=2))
        logger.info("Session saved to %s", session_path)

    async def shutdown(self) -> None:
        if self._context:
            await self._context.close()
            self._context = None
            self._page = None
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
        logger.info("Browser shut down")

    # ------------------------------------------------------------------
    # Per-user browser context management
    # ------------------------------------------------------------------

    async def _ensure_browser(self) -> None:
        """Ensure the Playwright browser is running (start if needed)."""
        if self._browser is None or not self._browser.is_connected():
            if self._playwright is None:
                self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=self._config.headless,
                args=["--disable-blink-features=AutomationControlled"],
            )

    async def create_user_context(
        self, session_path: Path
    ) -> tuple[BrowserContext, Page]:
        """Create a new BrowserContext for a specific user.

        Args:
            session_path: Path to the user's session state JSON file.

        Returns:
            (context, page) tuple. Caller must close via close_user_context().
        """
        await self._ensure_browser()

        context_kwargs = dict(_CONTEXT_KWARGS_BASE)
        if session_path.exists():
            logger.info("Restoring user session from %s", session_path)
            context_kwargs["storage_state"] = str(session_path)

        context = await self._browser.new_context(**context_kwargs)
        page = await context.new_page()
        await _stealth.apply_stealth_async(page)
        return context, page

    async def close_user_context(
        self, context: BrowserContext, session_path: Path
    ) -> None:
        """Save session state and close a user's BrowserContext."""
        try:
            session_path.parent.mkdir(parents=True, exist_ok=True)
            state = await context.storage_state()
            session_path.write_text(
                json.dumps(state, ensure_ascii=False, indent=2)
            )
            logger.info("User session saved to %s", session_path)
        except Exception as exc:
            logger.warning("Failed to save user session: %s", exc)
        finally:
            await context.close()
