from __future__ import annotations

import logging
from pathlib import Path

from playwright.async_api import Page

from src.browser import BrowserManager
from src.config import Config

logger = logging.getLogger(__name__)

# Candidate selectors — tried in order so we adapt to minor DOM changes.
_LOGIN_LINK_SELECTORS = [
    'a:has-text("로그인")',
    "#header .login",
    'a[href*="login"]',
    ".header_login a",
]

_USER_ID_SELECTORS = [
    'input[name="userId"]',
    'input[name="user_id"]',
    'input[name="loginId"]',
    "#userId",
    "#loginId",
    'input[placeholder*="아이디"]',
    'input[type="text"][name*="id" i]',
    'input[type="text"]',  # fallback: any text input
]

_LOGIN_DIRECT_URLS = [
    "/sub/subPage/mbrsvc/loginUsrPop.do",   # 실제 사이트 로그인 팝업 URL
    "/main/mainPage/login/loginPage.do",
    "/main/mainPage/mbrsvc/loginForm.do",
]

_USER_PW_SELECTORS = [
    'input[name="userPw"]',
    'input[name="password"]',
    "#userPw",
    'input[placeholder*="비밀번호"]',
    'input[type="password"]',
]

_LOGIN_BTN_SELECTORS = [
    'button:has-text("로그인")',
    'input[type="submit"][value*="로그인"]',
    'a:has-text("로그인")',
    ".btn_login",
    "#loginBtn",
]


async def _try_selectors(page: Page, selectors: list[str], *, timeout: int = 3000) -> str | None:
    """Return the first selector that matches a visible element, or None."""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=timeout):
                return sel
        except Exception:
            continue
    return None


async def _try_selectors_in_frame(frame, selectors: list[str], *, timeout: int = 3000) -> str | None:
    """Like _try_selectors but works on a Frame object."""
    for sel in selectors:
        try:
            loc = frame.locator(sel).first
            if await loc.is_visible(timeout=timeout):
                return sel
        except Exception:
            continue
    return None


class BetmanAuth:
    def __init__(self, config: Config, browser_manager: BrowserManager) -> None:
        self._config = config
        self._bm = browser_manager

    async def is_logged_in(self, page: Page) -> bool:
        """Check login status by navigating to main page and looking for auth indicators."""
        try:
            await page.goto(self._config.base_url, wait_until="domcontentloaded", timeout=60000)

            # Wait for the JS variable to be defined (fastest check)
            try:
                await page.wait_for_function(
                    "() => typeof isLogin !== 'undefined'", timeout=10000
                )
                is_login = await page.evaluate("() => isLogin === true")
                if is_login:
                    logger.debug("Logged in detected via JS isLogin variable")
                    return True
                # isLogin is defined but false → not logged in
                logger.debug("JS isLogin is false")
                return False
            except Exception:
                pass

            # Fallback: check DOM selectors
            logged_in_selectors = [
                ".stateLogIn",                    # logged-in header div
                'a:has-text("로그아웃")',           # logout link
                'a:has-text("마이페이지")',          # mypage link
            ]
            for sel in logged_in_selectors:
                try:
                    if await page.locator(sel).first.is_visible(timeout=3000):
                        logger.debug("Logged-in indicator found: %s", sel)
                        return True
                except Exception:
                    continue

            return False
        except Exception as exc:
            logger.debug("Login check failed: %s", exc)
            return False

    async def login(
        self,
        page: Page,
        betman_user_id: str | None = None,
        betman_user_pw: str | None = None,
    ) -> None:
        """Perform full login flow.

        Credentials can be passed explicitly (multi-user mode) or
        fall back to config values (legacy single-user mode).
        """
        user_id = betman_user_id or self._config.betman_user_id
        user_pw = betman_user_pw or self._config.betman_user_pw
        if not user_id or not user_pw:
            raise RuntimeError("Betman credentials not provided")

        logger.info("Starting login flow …")
        await page.goto(self._config.base_url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_load_state("networkidle", timeout=15000)

        # Save main page HTML for debugging
        debug_dir = Path("storage")
        debug_dir.mkdir(parents=True, exist_ok=True)
        main_html = await page.content()
        (debug_dir / "debug_mainpage.html").write_text(main_html, encoding="utf-8")
        logger.info("Main page HTML saved. URL: %s", page.url)

        # Dismiss any popups / modals
        await self._dismiss_popups(page)

        login_target = page  # the page/frame where login form lives

        # Strategy 1: Open login modal via JavaScript (site uses BUICommon.openModalByUrl)
        id_sel = None
        login_frame = None

        logger.info("Opening login modal via openLoginPop() …")
        try:
            await page.evaluate("openLoginPop()")
            # Wait for login form to appear in modal
            id_sel = await _try_selectors(page, _USER_ID_SELECTORS, timeout=10000)
            if id_sel:
                logger.info("Login form found in modal")
        except Exception as exc:
            logger.info("openLoginPop() failed: %s", exc)

        # Strategy 2: Click login link (may trigger modal or popup)
        if not id_sel:
            link_sel = await _try_selectors(page, _LOGIN_LINK_SELECTORS, timeout=3000)
            if link_sel:
                logger.info("Clicking login link: %s", link_sel)
                await page.locator(link_sel).first.click()
                id_sel = await _try_selectors(page, _USER_ID_SELECTORS, timeout=10000)
                if id_sel:
                    logger.info("Login form found after clicking link")

        # Strategy 3: Search in iframes (login form may be in iframe)
        if not id_sel:
            for frame in page.frames:
                if frame == page.main_frame:
                    continue
                id_sel = await _try_selectors_in_frame(frame, _USER_ID_SELECTORS, timeout=3000)
                if id_sel:
                    login_frame = frame
                    logger.info("Login form found in iframe: %s", frame.url)
                    break

        # Strategy 4: Navigate directly to known login URLs
        if not id_sel:
            logger.info("Login form not found on page, trying direct URLs …")
            for url_path in _LOGIN_DIRECT_URLS:
                try:
                    await page.goto(
                        f"{self._config.base_url}{url_path}",
                        wait_until="domcontentloaded",
                        timeout=60000,
                    )
                    await page.wait_for_load_state("networkidle", timeout=10000)
                    if await page.locator(".errorArea").count() > 0:
                        logger.info("URL %s returned error page, skipping", url_path)
                        continue
                    login_target = page
                    id_sel = await _try_selectors(page, _USER_ID_SELECTORS, timeout=10000)
                    if id_sel:
                        logger.info("Login form found at direct URL: %s", url_path)
                        break
                except Exception:
                    continue

        if not id_sel:
            # Save debug HTML for investigation
            html = await login_target.content()
            (debug_dir / "debug_login.html").write_text(html, encoding="utf-8")
            logger.error("Cannot find user ID input. HTML saved to storage/debug_login.html")
            logger.error("Current URL: %s", login_target.url)
            raise RuntimeError("Cannot find user ID input field")

        # Use the frame/page where the form was found
        form_target = login_frame or login_target
        await form_target.locator(id_sel).first.fill(user_id)
        logger.info("User ID filled")

        # Fill PW
        pw_sel = await _try_selectors(form_target, _USER_PW_SELECTORS, timeout=10000)
        if not pw_sel:
            raise RuntimeError("Cannot find password input field")
        await form_target.locator(pw_sel).first.fill(user_pw)
        logger.info("Password filled")

        # Click login button
        btn_sel = await _try_selectors(form_target, _LOGIN_BTN_SELECTORS, timeout=10000)
        if not btn_sel:
            raise RuntimeError("Cannot find login button")
        await form_target.locator(btn_sel).first.click()
        logger.info("Login button clicked, waiting for response …")

        # Wait for login to process — isLogin JS variable becomes true
        try:
            await page.wait_for_function("() => typeof isLogin !== 'undefined' && isLogin === true", timeout=15000)
        except Exception:
            # Fallback: wait for network to settle
            await page.wait_for_load_state("networkidle", timeout=10000)

        # Dismiss any post-login popups
        await self._dismiss_popups(page)

        # Verify login succeeded
        logged_in = await self.is_logged_in(page)
        if not logged_in:
            raise RuntimeError("Login failed — could not verify logged-in state")

        # Save session
        await self._bm.save_session()
        logger.info("Login successful, session saved.")

    async def ensure_logged_in(
        self,
        page: Page,
        betman_user_id: str | None = None,
        betman_user_pw: str | None = None,
    ) -> None:
        """Restore session if valid, otherwise re-login."""
        if await self.is_logged_in(page):
            logger.info("Already logged in (session restored).")
            return
        logger.info("Session expired or invalid, logging in …")
        await self.login(page, betman_user_id, betman_user_pw)

    @staticmethod
    async def _dismiss_popups(page: Page) -> None:
        """Close common Betman popups (responsible gambling warnings, etc.)."""
        popup_selectors = [
            'button:has-text("확인")',
            'button:has-text("닫기")',
            ".popup_close",
            ".layer_close",
            'a:has-text("닫기")',
            ".modal .close",
        ]
        for sel in popup_selectors:
            try:
                loc = page.locator(sel).first
                if await loc.is_visible(timeout=1000):
                    await loc.click()
                    await page.wait_for_timeout(300)
            except Exception:
                continue
