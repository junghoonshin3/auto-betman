from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, TypeVar

from dotenv import load_dotenv
from playwright.async_api import Browser, BrowserContext, async_playwright
from playwright_stealth import Stealth

from src import auth
from src.analysis import probe_purchase_analysis_token, scrape_purchase_analysis
from src.bot import Bot
from src.games import scrape_sale_games_summary
from src.models import BetSlip, PurchaseAnalysis, SaleGamesSnapshot
from src.purchases import probe_recent_purchases_token, scrape_purchase_history

logger = logging.getLogger(__name__)
SESSION_DIR = Path("storage")
LOGOUT_WAIT_TIMEOUT_SEC = 5.0
LOGOUT_WAIT_POLL_SEC = 0.1
CACHE_TTL_SECONDS = 60.0
CACHE_MAX_STALE_SECONDS = 600.0
KEEPALIVE_INTERVAL_SECONDS = 300.0
KEEPALIVE_TIMEOUT_SECONDS = 25.0
KEEPALIVE_TRANSIENT_RETRIES = 2
_SESSION_EXPIRED_MESSAGE = "세션이 만료되었습니다. /login으로 다시 로그인해주세요."

T = TypeVar("T")


@dataclass
class PurchasesCacheEntry:
    slips: list[BetSlip]
    token: str
    fetched_at_monotonic: float


@dataclass
class AnalysisCacheEntry:
    result: PurchaseAnalysis
    token: str
    fetched_at_monotonic: float


@dataclass
class UserSession:
    context: BrowserContext
    login_ok: bool
    storage_state_path: Path
    meta_lock: asyncio.Lock
    active_requests: int = 0
    closing: bool = False
    purchases_cache: PurchasesCacheEntry | None = None
    analysis_cache_by_month: dict[int, AnalysisCacheEntry] = field(default_factory=dict)
    refresh_tasks: dict[str, asyncio.Task[object]] = field(default_factory=dict)
    keepalive_task: asyncio.Task[None] | None = None
    last_session_expired_at: float | None = None
    last_keepalive_ok_at: float | None = None
    has_authenticated: bool = False


def _parse_sync_guild_id(raw_value: str | None) -> int | None:
    if raw_value is None:
        return None
    text = raw_value.strip()
    if not text:
        return None
    try:
        guild_id = int(text)
    except ValueError:
        logger.warning("Invalid DISCORD_GUILD_ID value (not an integer): %r", raw_value)
        return None
    if guild_id <= 0:
        logger.warning("Invalid DISCORD_GUILD_ID value (must be > 0): %r", raw_value)
        return None
    return guild_id


def _session_state_path(discord_user_id: str) -> Path:
    safe_user_id = re.sub(r"[^0-9A-Za-z_-]", "_", str(discord_user_id))
    return SESSION_DIR / f"session_state_{safe_user_id}.json"


def _legacy_session_state_path(discord_user_id: str) -> Path:
    safe_user_id = re.sub(r"[^0-9A-Za-z_-]", "_", str(discord_user_id))
    return SESSION_DIR / f"session_{safe_user_id}.json"


def _remove_user_session_files(discord_user_id: str) -> None:
    paths = [
        _session_state_path(discord_user_id),
        _legacy_session_state_path(discord_user_id),
    ]
    for path in paths:
        try:
            if path.exists():
                path.unlink()
                logger.info("Removed user session file: discord_user_id=%s path=%s", discord_user_id, path)
        except Exception as exc:
            logger.warning(
                "Failed to remove user session file: discord_user_id=%s path=%s error=%s",
                discord_user_id,
                path,
                exc,
            )


async def _get_or_create_user_session(
    user_sessions: dict[str, UserSession],
    creating_sessions: dict[str, asyncio.Task[UserSession]],
    sessions_lock: asyncio.Lock,
    discord_user_id: str,
    create_session: Callable[[str], Awaitable[UserSession]],
) -> UserSession:
    create_task: asyncio.Task[UserSession] | None = None
    async with sessions_lock:
        existing = user_sessions.get(discord_user_id)
        if existing is not None:
            logger.info("Reusing user session: discord_user_id=%s", discord_user_id)
            return existing
        create_task = creating_sessions.get(discord_user_id)
        if create_task is None:
            create_task = asyncio.create_task(create_session(discord_user_id))
            creating_sessions[discord_user_id] = create_task
            logger.info("User session creation started: discord_user_id=%s", discord_user_id)
        else:
            logger.info("Awaiting existing user session creation: discord_user_id=%s", discord_user_id)

    assert create_task is not None
    try:
        created = await create_task
    except Exception:
        async with sessions_lock:
            if creating_sessions.get(discord_user_id) is create_task:
                creating_sessions.pop(discord_user_id, None)
        raise

    async with sessions_lock:
        existing = user_sessions.get(discord_user_id)
        if existing is None:
            user_sessions[discord_user_id] = created
            logger.info("Created user session: discord_user_id=%s", discord_user_id)
            existing = created
        if creating_sessions.get(discord_user_id) is create_task:
            creating_sessions.pop(discord_user_id, None)
        return existing


async def _create_user_session(
    browser: Browser,
    stealth: Stealth,
    discord_user_id: str,
) -> UserSession:
    storage_state_path = _session_state_path(discord_user_id)
    has_login_state = storage_state_path.exists() and storage_state_path.stat().st_size > 0
    context_kwargs: dict[str, object] = {
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "viewport": {"width": 1920, "height": 1080},
    }
    if storage_state_path.exists() and storage_state_path.stat().st_size > 0:
        context_kwargs["storage_state"] = str(storage_state_path)

    try:
        context = await browser.new_context(**context_kwargs)
    except Exception as exc:
        logger.warning(
            "Failed to load user session state, recreating clean context: discord_user_id=%s error=%s",
            discord_user_id,
            exc,
        )
        context_kwargs.pop("storage_state", None)
        context = await browser.new_context(**context_kwargs)

    await stealth.apply_stealth_async(context)
    page = await context.new_page()
    login_ok = False
    try:
        login_ok = await auth.is_logged_in(page)
    except auth.TransientNetworkError as exc:
        logger.warning(
            "Transient network error while restoring user session login state: discord_user_id=%s error=%s",
            discord_user_id,
            exc,
        )
        login_ok = False
    finally:
        try:
            await page.close()
        except Exception:
            pass

    if login_ok:
        logger.info("User session restored as logged in: discord_user_id=%s", discord_user_id)

    return UserSession(
        context=context,
        login_ok=login_ok,
        storage_state_path=storage_state_path,
        meta_lock=asyncio.Lock(),
        has_authenticated=bool(login_ok or has_login_state),
    )


async def _cancel_tasks(tasks: list[asyncio.Task[object]]) -> None:
    for task in tasks:
        if not task.done():
            task.cancel()
    for task in tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass


async def _mark_session_expired(
    session: UserSession,
    reason: str,
    now_monotonic: Callable[[], float] = time.monotonic,
) -> None:
    refresh_tasks: list[asyncio.Task[object]] = []
    async with session.meta_lock:
        session.login_ok = False
        session.last_session_expired_at = now_monotonic()
        session.purchases_cache = None
        session.analysis_cache_by_month.clear()
        refresh_tasks = list(session.refresh_tasks.values())
        session.refresh_tasks.clear()
    logger.warning("Session expired: reason=%s", reason)
    await _cancel_tasks(refresh_tasks)


async def _keepalive_loop(
    session: UserSession,
    discord_user_id: str,
    *,
    interval_seconds: float = KEEPALIVE_INTERVAL_SECONDS,
    timeout_seconds: float = KEEPALIVE_TIMEOUT_SECONDS,
    transient_retries: int = KEEPALIVE_TRANSIENT_RETRIES,
    sleep_func: Callable[[float], Awaitable[object]] = asyncio.sleep,
    now_monotonic: Callable[[], float] = time.monotonic,
    is_logged_in_func: Callable[..., Awaitable[bool]] = auth.is_logged_in,
) -> None:
    logger.info("Keepalive loop started: discord_user_id=%s", discord_user_id)
    try:
        while True:
            await sleep_func(max(1.0, float(interval_seconds)))
            async with session.meta_lock:
                if session.closing:
                    logger.info("Keepalive loop stopped by closing flag: discord_user_id=%s", discord_user_id)
                    return
                if not session.login_ok:
                    logger.info("Keepalive loop stopped because login is false: discord_user_id=%s", discord_user_id)
                    return

            page = await session.context.new_page()
            try:
                logged_in: bool | None = None
                for attempt in range(max(0, transient_retries) + 1):
                    try:
                        logged_in = await asyncio.wait_for(
                            is_logged_in_func(page, retries=1, base_delay=0.3),
                            timeout=max(1.0, float(timeout_seconds)),
                        )
                        break
                    except (auth.TransientNetworkError, asyncio.TimeoutError) as exc:
                        if attempt < max(0, transient_retries):
                            logger.warning(
                                "Keepalive transient error, retrying: discord_user_id=%s attempt=%d/%d error=%s",
                                discord_user_id,
                                attempt + 1,
                                max(0, transient_retries) + 1,
                                exc,
                            )
                            await sleep_func(min(1.5, 0.5 * (attempt + 1)))
                            continue
                        logger.warning(
                            "Keepalive transient retries exhausted: discord_user_id=%s error=%s",
                            discord_user_id,
                            exc,
                        )
                        logged_in = None
                        break
                    except Exception as exc:
                        logger.warning("Keepalive check failed (non-fatal): discord_user_id=%s error=%s", discord_user_id, exc)
                        logged_in = None
                        break

                if logged_in is True:
                    async with session.meta_lock:
                        if session.closing:
                            return
                        session.login_ok = True
                        session.has_authenticated = True
                        session.last_session_expired_at = None
                        session.last_keepalive_ok_at = now_monotonic()
                    continue

                if logged_in is False:
                    await _mark_session_expired(
                        session,
                        reason=f"keepalive-login-false:{discord_user_id}",
                        now_monotonic=now_monotonic,
                    )
                    return

                # Transient/network instability case: keep session state unchanged.
                continue
            finally:
                try:
                    await page.close()
                except Exception:
                    pass
    except asyncio.CancelledError:
        logger.info("Keepalive loop cancelled: discord_user_id=%s", discord_user_id)
        raise
    finally:
        this_task = asyncio.current_task()
        async with session.meta_lock:
            if this_task is not None and session.keepalive_task is this_task:
                session.keepalive_task = None
        logger.info("Keepalive loop ended: discord_user_id=%s", discord_user_id)


async def _start_keepalive_if_needed(session: UserSession, discord_user_id: str) -> None:
    async with session.meta_lock:
        if session.closing or not session.login_ok:
            return
        existing = session.keepalive_task
        if existing is not None and not existing.done():
            return
        session.keepalive_task = asyncio.create_task(_keepalive_loop(session, discord_user_id))
    logger.info("Keepalive task started: discord_user_id=%s", discord_user_id)


async def _stop_keepalive(session: UserSession) -> None:
    task: asyncio.Task[None] | None = None
    async with session.meta_lock:
        task = session.keepalive_task
        session.keepalive_task = None
    if task is None:
        return
    if not task.done():
        task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        pass


async def _begin_user_request(session: UserSession) -> None:
    async with session.meta_lock:
        if session.closing:
            raise RuntimeError("현재 로그아웃 처리 중입니다. 잠시 후 다시 시도해주세요.")
        session.active_requests += 1


async def _end_user_request(session: UserSession) -> None:
    async with session.meta_lock:
        session.active_requests = max(0, session.active_requests - 1)


async def _wait_until_no_active_requests(
    session: UserSession,
    timeout_seconds: float = LOGOUT_WAIT_TIMEOUT_SEC,
    poll_seconds: float = LOGOUT_WAIT_POLL_SEC,
) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, timeout_seconds)

    while True:
        async with session.meta_lock:
            if session.active_requests <= 0:
                return True
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(max(0.01, poll_seconds))


async def _ensure_logged_in(session: UserSession) -> None:
    has_authenticated = False
    expired_at: float | None = None
    async with session.meta_lock:
        if session.closing:
            raise RuntimeError("현재 로그아웃 처리 중입니다. 잠시 후 다시 시도해주세요.")
        if session.login_ok:
            return
        has_authenticated = session.has_authenticated
        expired_at = session.last_session_expired_at

    if expired_at is not None:
        raise RuntimeError(_SESSION_EXPIRED_MESSAGE)

    probe_page = await session.context.new_page()
    logged_in = False
    try:
        logged_in = await auth.is_logged_in(probe_page)
    except auth.TransientNetworkError as exc:
        raise RuntimeError("Betman 접속이 불안정합니다. 잠시 후 다시 시도해주세요.") from exc
    finally:
        try:
            await probe_page.close()
        except Exception:
            pass

    async with session.meta_lock:
        if session.closing:
            raise RuntimeError("현재 로그아웃 처리 중입니다. 잠시 후 다시 시도해주세요.")
        session.login_ok = logged_in
        if logged_in:
            session.has_authenticated = True
            session.last_session_expired_at = None

    if logged_in:
        return

    if has_authenticated:
        await _mark_session_expired(session, reason="ensure-logged-in-false")
        raise RuntimeError(_SESSION_EXPIRED_MESSAGE)

    raise RuntimeError("먼저 /login 으로 로그인해주세요.")


def _is_transient_error_message(message: str) -> bool:
    text = (message or "").lower()
    signals = (
        "err_connection_refused",
        "net::err_",
        "timeout",
        "timed out",
        "connection reset",
        "connection aborted",
        "connection closed",
        "econnreset",
        "enotfound",
        "requestclient",
        "execution context was destroyed",
    )
    return any(sig in text for sig in signals)


def _should_use_stale_cache_on_error(exc: Exception) -> bool:
    return _is_transient_error_message(str(exc))


async def _run_session_refresh_task(
    session: UserSession,
    key: str,
    refresh_coro_factory: Callable[[], Awaitable[T]],
) -> T:
    created_task = False
    task: asyncio.Task[object]
    async with session.meta_lock:
        existing = session.refresh_tasks.get(key)
        if existing is None:
            task = asyncio.create_task(refresh_coro_factory())
            session.refresh_tasks[key] = task
            created_task = True
            logger.info("cache refresh task created: key=%s", key)
        else:
            task = existing
            logger.info("cache refresh task joined: key=%s", key)

    try:
        result = await task
        return result  # type: ignore[return-value]
    finally:
        if created_task:
            async with session.meta_lock:
                if session.refresh_tasks.get(key) is task:
                    session.refresh_tasks.pop(key, None)


async def _resolve_purchases_with_cache(
    session: UserSession,
    probe_fetch: Callable[[], Awaitable[str]],
    full_fetch: Callable[[], Awaitable[list[BetSlip]]],
    now_monotonic: Callable[[], float] = time.monotonic,
) -> list[BetSlip]:
    now = now_monotonic()
    async with session.meta_lock:
        if session.last_session_expired_at is not None:
            raise RuntimeError(_SESSION_EXPIRED_MESSAGE)
        cache = session.purchases_cache
        if cache is not None and (now - cache.fetched_at_monotonic) <= CACHE_TTL_SECONDS:
            logger.info("purchases cache hit: age=%.2fs", now - cache.fetched_at_monotonic)
            return cache.slips

    async def refresh() -> list[BetSlip]:
        token = await probe_fetch()
        if not token:
            raise RuntimeError("purchase probe failed: empty token")

        async with session.meta_lock:
            current = session.purchases_cache
            if current is not None and current.token == token:
                current.fetched_at_monotonic = now_monotonic()
                logger.info("purchases cache unchanged by probe")
                return current.slips

        slips = await full_fetch()
        async with session.meta_lock:
            session.purchases_cache = PurchasesCacheEntry(
                slips=slips,
                token=token,
                fetched_at_monotonic=now_monotonic(),
            )
        logger.info("purchases cache refreshed: size=%d", len(slips))
        return slips

    try:
        return await _run_session_refresh_task(session, "purchases:recent5", refresh)
    except Exception as exc:
        if _should_use_stale_cache_on_error(exc):
            now_retry = now_monotonic()
            async with session.meta_lock:
                if session.last_session_expired_at is not None:
                    raise RuntimeError(_SESSION_EXPIRED_MESSAGE)
                stale = session.purchases_cache
                if stale is not None and (now_retry - stale.fetched_at_monotonic) <= CACHE_MAX_STALE_SECONDS:
                    logger.warning(
                        "purchases stale cache used due to transient error: age=%.2fs error=%s",
                        now_retry - stale.fetched_at_monotonic,
                        exc,
                    )
                    return stale.slips
        raise


async def _resolve_analysis_with_cache(
    session: UserSession,
    months: int,
    probe_fetch: Callable[[], Awaitable[tuple[str, PurchaseAnalysis | None]]],
    full_fetch: Callable[[], Awaitable[PurchaseAnalysis]],
    now_monotonic: Callable[[], float] = time.monotonic,
) -> PurchaseAnalysis:
    months = int(months)
    now = now_monotonic()
    async with session.meta_lock:
        if session.last_session_expired_at is not None:
            raise RuntimeError(_SESSION_EXPIRED_MESSAGE)
        cache = session.analysis_cache_by_month.get(months)
        if cache is not None and (now - cache.fetched_at_monotonic) <= CACHE_TTL_SECONDS:
            logger.info("analysis cache hit: months=%d age=%.2fs", months, now - cache.fetched_at_monotonic)
            return cache.result

    async def refresh() -> PurchaseAnalysis:
        token, _parsed = await probe_fetch()
        if not token:
            raise RuntimeError("analysis probe failed: empty token")

        async with session.meta_lock:
            current = session.analysis_cache_by_month.get(months)
            if current is not None and current.token == token:
                current.fetched_at_monotonic = now_monotonic()
                logger.info("analysis cache unchanged by probe: months=%d", months)
                return current.result

        result = await full_fetch()
        async with session.meta_lock:
            session.analysis_cache_by_month[months] = AnalysisCacheEntry(
                result=result,
                token=token,
                fetched_at_monotonic=now_monotonic(),
            )
        logger.info("analysis cache refreshed: months=%d", months)
        return result

    try:
        return await _run_session_refresh_task(session, f"analysis:{months}", refresh)
    except Exception as exc:
        if _should_use_stale_cache_on_error(exc):
            now_retry = now_monotonic()
            async with session.meta_lock:
                if session.last_session_expired_at is not None:
                    raise RuntimeError(_SESSION_EXPIRED_MESSAGE)
                stale = session.analysis_cache_by_month.get(months)
                if stale is not None and (now_retry - stale.fetched_at_monotonic) <= CACHE_MAX_STALE_SECONDS:
                    logger.warning(
                        "analysis stale cache used due to transient error: months=%d age=%.2fs error=%s",
                        months,
                        now_retry - stale.fetched_at_monotonic,
                        exc,
                    )
                    return stale.result
        raise


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    load_dotenv()
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN is not set")

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True)
    stealth = Stealth()
    user_sessions: dict[str, UserSession] = {}
    creating_sessions: dict[str, asyncio.Task[UserSession]] = {}
    sessions_lock = asyncio.Lock()
    games_cache: SaleGamesSnapshot | None = None
    games_cache_at_monotonic: float | None = None
    games_refresh_task: asyncio.Task[SaleGamesSnapshot] | None = None
    games_lock = asyncio.Lock()

    bot = Bot()
    bot.sync_guild_id = _parse_sync_guild_id(os.environ.get("DISCORD_GUILD_ID"))
    if bot.sync_guild_id is not None:
        logger.info("Guild slash command sync enabled. guild_id=%s", bot.sync_guild_id)

    async def get_user_session(discord_user_id: str) -> UserSession:
        session = await _get_or_create_user_session(
            user_sessions=user_sessions,
            creating_sessions=creating_sessions,
            sessions_lock=sessions_lock,
            discord_user_id=discord_user_id,
            create_session=lambda uid: _create_user_session(browser, stealth, uid),
        )
        await _start_keepalive_if_needed(session, discord_user_id)
        return session

    async def do_login(discord_user_id: str, user_id: str, user_pw: str) -> bool:
        session = await get_user_session(discord_user_id)
        start_keepalive = False
        async with session.meta_lock:
            if session.closing:
                return False
            login_page = await session.context.new_page()
            ok = False
            try:
                ok = await auth.login(login_page, user_id, user_pw)
            finally:
                try:
                    await login_page.close()
                except Exception:
                    pass
            session.login_ok = ok
            if ok:
                session.has_authenticated = True
                session.last_session_expired_at = None
                session.purchases_cache = None
                session.analysis_cache_by_month.clear()
                start_keepalive = True
                try:
                    session.storage_state_path.parent.mkdir(parents=True, exist_ok=True)
                    await session.context.storage_state(path=str(session.storage_state_path))
                    logger.info(
                        "User session state saved: discord_user_id=%s path=%s",
                        discord_user_id,
                        session.storage_state_path,
                    )
                except Exception as exc:
                    logger.warning(
                        "Failed to persist user session state: discord_user_id=%s error=%s",
                        discord_user_id,
                        exc,
                    )
        if start_keepalive:
            await _start_keepalive_if_needed(session, discord_user_id)
        return ok

    async def do_purchases(discord_user_id: str) -> list:
        session = await get_user_session(discord_user_id)
        await _begin_user_request(session)
        try:
            await _ensure_logged_in(session)
            logger.info("purchases cache check start: discord_user_id=%s", discord_user_id)

            async def probe_fetch() -> str:
                page = await session.context.new_page()
                try:
                    return await probe_recent_purchases_token(page, limit=5)
                finally:
                    try:
                        await page.close()
                    except Exception:
                        pass

            async def full_fetch() -> list[BetSlip]:
                page = await session.context.new_page()
                try:
                    return await scrape_purchase_history(page, limit=5)
                finally:
                    try:
                        await page.close()
                    except Exception:
                        pass

            return await _resolve_purchases_with_cache(session, probe_fetch=probe_fetch, full_fetch=full_fetch)
        finally:
            await _end_user_request(session)

    async def do_analysis(discord_user_id: str, months: int) -> PurchaseAnalysis:
        session = await get_user_session(discord_user_id)
        await _begin_user_request(session)
        try:
            await _ensure_logged_in(session)
            logger.info("analysis cache check start: discord_user_id=%s months=%d", discord_user_id, months)

            async def probe_fetch() -> tuple[str, PurchaseAnalysis | None]:
                page = await session.context.new_page()
                try:
                    return await probe_purchase_analysis_token(page, months=months)
                finally:
                    try:
                        await page.close()
                    except Exception:
                        pass

            async def full_fetch() -> PurchaseAnalysis:
                page = await session.context.new_page()
                try:
                    return await scrape_purchase_analysis(page, months=months)
                finally:
                    try:
                        await page.close()
                    except Exception:
                        pass

            return await _resolve_analysis_with_cache(
                session,
                months=months,
                probe_fetch=probe_fetch,
                full_fetch=full_fetch,
            )
        finally:
            await _end_user_request(session)

    async def do_logout(discord_user_id: str) -> bool:
        try:
            async with sessions_lock:
                session = user_sessions.get(discord_user_id)
            if session is not None:
                async with session.meta_lock:
                    session.closing = True
                if not await _wait_until_no_active_requests(session):
                    async with session.meta_lock:
                        session.closing = False
                    logger.warning("Logout delayed by active requests: discord_user_id=%s", discord_user_id)
                    return False

                async with sessions_lock:
                    session = user_sessions.pop(discord_user_id, None)

                if session is not None:
                    async with session.meta_lock:
                        refresh_tasks = list(session.refresh_tasks.values())
                        session.refresh_tasks.clear()
                    await _stop_keepalive(session)
                    for task in refresh_tasks:
                        if not task.done():
                            task.cancel()
                    try:
                        await session.context.close()
                        logger.info("Closed user session context on logout: discord_user_id=%s", discord_user_id)
                    except Exception as exc:
                        logger.warning(
                            "Failed closing user context on logout: discord_user_id=%s error=%s",
                            discord_user_id,
                            exc,
                        )
            _remove_user_session_files(discord_user_id)
            return True
        except Exception as exc:
            logger.exception("Logout failed: discord_user_id=%s error=%s", discord_user_id, exc)
            return False

    async def do_games() -> SaleGamesSnapshot:
        nonlocal games_cache
        nonlocal games_cache_at_monotonic
        nonlocal games_refresh_task

        now = time.monotonic()
        task: asyncio.Task[SaleGamesSnapshot]
        async with games_lock:
            if games_cache is not None and games_cache_at_monotonic is not None:
                age = now - games_cache_at_monotonic
                if age <= CACHE_TTL_SECONDS:
                    logger.info("games cache hit: age=%.2fs", age)
                    return games_cache

            if games_refresh_task is None or games_refresh_task.done():
                async def refresh_games() -> SaleGamesSnapshot:
                    context = await browser.new_context(
                        user_agent=(
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                        ),
                        viewport={"width": 1920, "height": 1080},
                    )
                    page = None
                    try:
                        await stealth.apply_stealth_async(context)
                        page = await context.new_page()
                        return await scrape_sale_games_summary(page, nearest_limit=20)
                    finally:
                        if page is not None:
                            try:
                                await page.close()
                            except Exception:
                                pass
                        try:
                            await context.close()
                        except Exception:
                            pass

                games_refresh_task = asyncio.create_task(refresh_games())
                logger.info("games refresh task created")
            else:
                logger.info("games refresh task joined")
            task = games_refresh_task

        try:
            snapshot = await task
            async with games_lock:
                games_cache = snapshot
                games_cache_at_monotonic = time.monotonic()
                if games_refresh_task is task:
                    games_refresh_task = None
            return snapshot
        except Exception as exc:
            async with games_lock:
                if games_refresh_task is task:
                    games_refresh_task = None
                stale = games_cache
                stale_age = (
                    time.monotonic() - games_cache_at_monotonic
                    if stale is not None and games_cache_at_monotonic is not None
                    else None
                )
            if (
                stale is not None
                and stale_age is not None
                and stale_age <= CACHE_MAX_STALE_SECONDS
                and _should_use_stale_cache_on_error(exc)
            ):
                logger.warning("games stale cache used due to transient error: age=%.2fs error=%s", stale_age, exc)
                return stale
            raise

    bot.login_callback = do_login
    bot.purchase_callback = do_purchases
    bot.analysis_callback = do_analysis
    bot.games_callback = do_games
    bot.logout_callback = do_logout

    try:
        await bot.start(token)
    finally:
        async with sessions_lock:
            pending_creations = list(creating_sessions.values())
        for task in pending_creations:
            if not task.done():
                task.cancel()
        async with games_lock:
            refresh_task = games_refresh_task
            games_refresh_task = None
        if refresh_task is not None and not refresh_task.done():
            refresh_task.cancel()
            try:
                await refresh_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        for discord_user_id, session in list(user_sessions.items()):
            async with session.meta_lock:
                refresh_tasks = list(session.refresh_tasks.values())
                session.refresh_tasks.clear()
            await _stop_keepalive(session)
            for task in refresh_tasks:
                if not task.done():
                    task.cancel()
            try:
                await session.context.close()
                logger.info("Closed user session context: discord_user_id=%s", discord_user_id)
            except Exception as exc:
                logger.warning("Failed to close user session context: discord_user_id=%s error=%s", discord_user_id, exc)
        await browser.close()
        await pw.stop()


if __name__ == "__main__":
    asyncio.run(main())
