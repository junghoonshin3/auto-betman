from __future__ import annotations

import asyncio
from pathlib import Path

from src import auth
from src.main import (
    AnalysisCacheEntry,
    PurchasesCacheEntry,
    UserSession,
    _keepalive_loop,
    _start_keepalive_if_needed,
    _stop_keepalive,
)
from src.models import BetSlip, PurchaseAnalysis


class _FakePage:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _FakeContext:
    def __init__(self) -> None:
        self.pages: list[_FakePage] = []

    async def new_page(self) -> _FakePage:
        page = _FakePage()
        self.pages.append(page)
        return page


def _session() -> UserSession:
    return UserSession(
        context=_FakeContext(),
        login_ok=True,
        storage_state_path=Path("/tmp/session.json"),
        meta_lock=asyncio.Lock(),
        has_authenticated=True,
    )


async def test_start_keepalive_starts_once(monkeypatch) -> None:
    session = _session()
    started = 0

    async def fake_loop(_session: UserSession, _uid: str) -> None:
        nonlocal started
        started += 1
        await asyncio.sleep(3600)

    monkeypatch.setattr("src.main._keepalive_loop", fake_loop)

    await _start_keepalive_if_needed(session, "111")
    await asyncio.sleep(0)
    first_task = session.keepalive_task
    assert first_task is not None

    await _start_keepalive_if_needed(session, "111")
    await asyncio.sleep(0)
    assert session.keepalive_task is first_task
    assert started == 1

    await _stop_keepalive(session)


async def test_keepalive_success_updates_last_ok() -> None:
    session = _session()
    calls = 0
    sleep_calls = 0

    async def fake_sleep(_delay: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= 2:
            async with session.meta_lock:
                session.closing = True

    async def fake_is_logged_in(_page, retries: int = 1, base_delay: float = 0.0) -> bool:  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        return True

    await _keepalive_loop(
        session,
        "111",
        interval_seconds=0.01,
        timeout_seconds=1.0,
        transient_retries=0,
        sleep_func=fake_sleep,
        now_monotonic=lambda: 123.0,
        is_logged_in_func=fake_is_logged_in,
    )

    assert calls >= 1
    assert session.last_keepalive_ok_at == 123.0
    assert session.login_ok is True


async def test_keepalive_marks_expired_and_clears_cache_on_login_false() -> None:
    session = _session()
    session.purchases_cache = PurchasesCacheEntry(slips=[], token="t", fetched_at_monotonic=1.0)
    session.analysis_cache_by_month[1] = AnalysisCacheEntry(
        result=PurchaseAnalysis(months=1, purchase_amount=1000, winning_amount=500),
        token="1:1000:500",
        fetched_at_monotonic=1.0,
    )

    async def fake_sleep(_delay: float) -> None:
        return None

    async def fake_is_logged_in(_page, retries: int = 1, base_delay: float = 0.0) -> bool:  # type: ignore[no-untyped-def]
        return False

    await _keepalive_loop(
        session,
        "111",
        interval_seconds=0.01,
        timeout_seconds=1.0,
        transient_retries=0,
        sleep_func=fake_sleep,
        now_monotonic=lambda: 999.0,
        is_logged_in_func=fake_is_logged_in,
    )

    assert session.login_ok is False
    assert session.last_session_expired_at == 999.0
    assert session.purchases_cache is None
    assert session.analysis_cache_by_month == {}


async def test_keepalive_transient_error_does_not_immediately_expire() -> None:
    session = _session()
    is_logged_in_calls = 0
    sleep_calls = 0

    async def fake_sleep(_delay: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= 3:
            async with session.meta_lock:
                session.closing = True

    async def fake_is_logged_in(_page, retries: int = 1, base_delay: float = 0.0) -> bool:  # type: ignore[no-untyped-def]
        nonlocal is_logged_in_calls
        is_logged_in_calls += 1
        if is_logged_in_calls == 1:
            raise auth.TransientNetworkError("timeout")
        return True

    await _keepalive_loop(
        session,
        "111",
        interval_seconds=0.01,
        timeout_seconds=1.0,
        transient_retries=1,
        sleep_func=fake_sleep,
        now_monotonic=lambda: 555.0,
        is_logged_in_func=fake_is_logged_in,
    )

    assert is_logged_in_calls >= 2
    assert session.login_ok is True
    assert session.last_session_expired_at is None


async def test_stop_keepalive_cancels_task_safely() -> None:
    session = _session()
    session.keepalive_task = asyncio.create_task(asyncio.sleep(3600))

    await _stop_keepalive(session)

    assert session.keepalive_task is None
    await _stop_keepalive(session)


async def test_start_keepalive_skips_when_not_logged_in() -> None:
    session = _session()
    session.login_ok = False

    await _start_keepalive_if_needed(session, "111")

    assert session.keepalive_task is None
