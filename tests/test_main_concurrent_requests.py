from __future__ import annotations

import asyncio
from pathlib import Path
import time

import pytest

from src.main import UserSession, _begin_user_request, _end_user_request, _run_session_refresh_task


async def test_begin_end_user_request_allows_parallel_requests() -> None:
    session = UserSession(
        context=object(),
        login_ok=True,
        storage_state_path=Path('/tmp/test.json'),
        meta_lock=asyncio.Lock(),
    )
    active_snapshots: list[int] = []
    release = asyncio.Event()

    async def worker() -> None:
        await _begin_user_request(session)
        async with session.meta_lock:
            active_snapshots.append(session.active_requests)
        await release.wait()
        await _end_user_request(session)

    tasks = [asyncio.create_task(worker()) for _ in range(2)]
    await asyncio.sleep(0.05)
    release.set()
    await asyncio.gather(*tasks)

    assert max(active_snapshots) >= 2
    async with session.meta_lock:
        assert session.active_requests == 0


async def test_begin_user_request_rejects_when_closing() -> None:
    session = UserSession(
        context=object(),
        login_ok=True,
        storage_state_path=Path('/tmp/test.json'),
        meta_lock=asyncio.Lock(),
        closing=True,
    )

    with pytest.raises(RuntimeError, match='로그아웃 처리 중'):
        await _begin_user_request(session)


async def test_run_session_refresh_task_deduplicates_same_key() -> None:
    session = UserSession(
        context=object(),
        login_ok=True,
        storage_state_path=Path('/tmp/test.json'),
        meta_lock=asyncio.Lock(),
    )
    called = 0
    release = asyncio.Event()

    async def refresh() -> str:
        nonlocal called
        called += 1
        await release.wait()
        return "ok"

    async def worker() -> str:
        return await _run_session_refresh_task(session, "k1", refresh)

    t1 = asyncio.create_task(worker())
    t2 = asyncio.create_task(worker())
    await asyncio.sleep(0.05)
    release.set()
    r1, r2 = await asyncio.gather(t1, t2)

    assert r1 == "ok"
    assert r2 == "ok"
    assert called == 1


async def test_run_session_refresh_task_parallel_for_different_keys() -> None:
    session = UserSession(
        context=object(),
        login_ok=True,
        storage_state_path=Path('/tmp/test.json'),
        meta_lock=asyncio.Lock(),
    )

    async def refresh(delay: float) -> str:
        await asyncio.sleep(delay)
        return "ok"

    started = time.perf_counter()
    await asyncio.gather(
        _run_session_refresh_task(session, "k1", lambda: refresh(0.25)),
        _run_session_refresh_task(session, "k2", lambda: refresh(0.25)),
    )
    elapsed = time.perf_counter() - started
    assert elapsed < 0.45
