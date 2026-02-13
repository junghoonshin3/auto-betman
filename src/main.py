from __future__ import annotations

import asyncio
import logging
import os
from typing import Literal

from dotenv import load_dotenv
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

from src import auth
from src.bot import Bot
from src.purchases import scrape_purchase_history

logger = logging.getLogger(__name__)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    load_dotenv()
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN is not set")

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True)
    stealth = Stealth()
    context = await browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        viewport={"width": 1920, "height": 1080},
    )
    await stealth.apply_stealth_async(context)
    page = await context.new_page()
    page_lock = asyncio.Lock()
    login_state = {"ok": False}

    bot = Bot()

    async def do_login(user_id: str, user_pw: str) -> bool:
        async with page_lock:
            ok = await auth.login(page, user_id, user_pw)
            login_state["ok"] = ok
            return ok

    async def do_purchases(mode: Literal["recent5", "month30"]) -> list:
        if not login_state["ok"]:
            raise RuntimeError("먼저 /login 으로 로그인해주세요.")

        limit = 5 if mode == "recent5" else 30
        async with page_lock:
            return await scrape_purchase_history(page, limit=limit, mode=mode)

    bot.login_callback = do_login
    bot.purchase_callback = do_purchases

    try:
        await bot.start(token)
    finally:
        await browser.close()
        await pw.stop()


if __name__ == "__main__":
    asyncio.run(main())
