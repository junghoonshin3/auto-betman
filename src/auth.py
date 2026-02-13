from __future__ import annotations

import logging
from pathlib import Path

from playwright.async_api import Page

logger = logging.getLogger(__name__)

DEBUG_DIR = Path("storage")


async def _block_kos(route) -> None:
    """Block KOS keyboard-security requests."""
    logger.info("Blocking KOS: %s (navigation=%s)", route.request.url, route.request.is_navigation_request())
    if route.request.is_navigation_request():
        await route.fulfill(status=204)
    else:
        await route.abort()


async def login(page: Page, user_id: str, user_pw: str) -> bool:
    """Log in to betman.co.kr. Returns True on success, False on failure."""
    try:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)

        # Block KOS keyboard-security scripts and .exe downloads
        await page.route("**/*.exe", _block_kos)
        await page.route("**/kos-ng*.js", _block_kos)
        await page.route("**/KOS_*", _block_kos)
        await page.route("**/kings/**", _block_kos)

        logger.info("Navigating to betman.co.kr …")
        await page.goto("https://www.betman.co.kr", wait_until="networkidle", timeout=30000)

        # Save debug HTML
        html = await page.content()
        (DEBUG_DIR / "debug_mainpage.html").write_text(html, encoding="utf-8")
        logger.info("Page loaded — URL: %s, title: %s", page.url, await page.title())

        # Check for error page
        error_count = await page.locator(".errorArea").count()
        if error_count > 0:
            logger.error("Error page detected!")
            return False

        # Close existing jQuery UI dialogs properly, then remove leftover overlays
        await page.evaluate("""() => {
            document.querySelectorAll('.ui-dialog-content').forEach(el => {
                try { $(el).dialog('close'); } catch(e) {}
            });
            document.querySelectorAll('.ui-widget-overlay, .ui-dialog-overlay')
                    .forEach(el => el.remove());
        }""")

        # Wait for openLoginPop to be defined, then open login modal
        logger.info("Waiting for openLoginPop …")
        await page.wait_for_function("() => typeof openLoginPop === 'function'", timeout=15000)
        logger.info("Opening login modal …")
        result = await page.evaluate("""() => {
            try { openLoginPop(); return 'ok'; }
            catch(e) { return 'error: ' + e.message; }
        }""")
        logger.info("openLoginPop() returned: %s", result)

        # Wait for the login form to appear in the DOM (via AJAX .load()).
        # Use wait_for_function instead of wait_for_selector — the latter
        # can stall when KOS-related resource requests are pending/aborted.
        logger.info("Waiting for login form …")
        await page.wait_for_function(
            "() => document.querySelector('#loginPopId') !== null",
            timeout=15000,
        )
        logger.info("Login form found")

        # Cancel any pending navigations triggered by KOS security module
        # (e.g. KOS_Setup.exe download). window.stop() is the JS equivalent
        # of clicking the browser stop button — kills pending navigations.
        await page.evaluate("window.stop()")

        # Fill credentials and click submit via JS to bypass Playwright's
        # actionability checks (which also stall on pending navigations).
        await page.evaluate("""(creds) => {
            const id = document.querySelector('#loginPopId');
            const pw = document.querySelector('#loginPopPwd');
            id.value = creds.id;
            pw.value = creds.pw;
            id.dispatchEvent(new Event('input', {bubbles: true}));
            pw.dispatchEvent(new Event('input', {bubbles: true}));
            document.querySelector('#doLogin').click();
        }""", {"id": user_id, "pw": user_pw})
        logger.info("Login submitted via JS, waiting …")

        # Wait for JS variable isLogin to become true (always on main frame)
        await page.wait_for_function(
            "() => typeof isLogin !== 'undefined' && isLogin === true",
            timeout=10000,
        )

        logger.info("Login successful.")
        return True

    except Exception as exc:
        logger.error("Login failed: %s", exc)
        return False
