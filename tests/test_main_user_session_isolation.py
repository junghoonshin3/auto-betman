from __future__ import annotations

import asyncio
import time
from pathlib import Path

from src.main import UserSession, _get_or_create_user_session, _session_state_path


def test_session_state_path_is_user_scoped() -> None:
    assert _session_state_path("111").name == "session_state_111.json"
    assert _session_state_path("user:abc").name == "session_state_user_abc.json"


async def test_get_or_create_user_session_reuses_same_user() -> None:
    sessions: dict[str, UserSession] = {}
    creating_sessions: dict[str, asyncio.Task[UserSession]] = {}
    sessions_lock = asyncio.Lock()
    created: list[str] = []

    async def create_session(discord_user_id: str) -> UserSession:
        created.append(discord_user_id)
        return UserSession(
            context=object(),
            login_ok=False,
            storage_state_path=Path(f"/tmp/{discord_user_id}.json"),
            meta_lock=asyncio.Lock(),
        )

    s1 = await _get_or_create_user_session(sessions, creating_sessions, sessions_lock, "111", create_session)
    s2 = await _get_or_create_user_session(sessions, creating_sessions, sessions_lock, "111", create_session)

    assert s1 is s2
    assert created == ["111"]


async def test_get_or_create_user_session_separates_users() -> None:
    sessions: dict[str, UserSession] = {}
    creating_sessions: dict[str, asyncio.Task[UserSession]] = {}
    sessions_lock = asyncio.Lock()

    async def create_session(discord_user_id: str) -> UserSession:
        return UserSession(
            context=object(),
            login_ok=False,
            storage_state_path=Path(f"/tmp/{discord_user_id}.json"),
            meta_lock=asyncio.Lock(),
        )

    s1 = await _get_or_create_user_session(sessions, creating_sessions, sessions_lock, "111", create_session)
    s2 = await _get_or_create_user_session(sessions, creating_sessions, sessions_lock, "222", create_session)

    assert s1 is not s2


async def test_get_or_create_user_session_parallel_for_different_users() -> None:
    sessions: dict[str, UserSession] = {}
    creating_sessions: dict[str, asyncio.Task[UserSession]] = {}
    sessions_lock = asyncio.Lock()

    async def create_session(discord_user_id: str) -> UserSession:
        await asyncio.sleep(1.0)
        return UserSession(
            context=object(),
            login_ok=False,
            storage_state_path=Path(f"/tmp/{discord_user_id}.json"),
            meta_lock=asyncio.Lock(),
        )

    started = time.perf_counter()
    await asyncio.gather(
        _get_or_create_user_session(sessions, creating_sessions, sessions_lock, "111", create_session),
        _get_or_create_user_session(sessions, creating_sessions, sessions_lock, "222", create_session),
    )
    elapsed = time.perf_counter() - started

    # Should run closer to 1s parallel path than old 2s serialized path.
    assert elapsed < 1.8


async def test_get_or_create_user_session_deduplicates_same_user_creation() -> None:
    sessions: dict[str, UserSession] = {}
    creating_sessions: dict[str, asyncio.Task[UserSession]] = {}
    sessions_lock = asyncio.Lock()
    call_count = 0

    async def create_session(discord_user_id: str) -> UserSession:
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.2)
        return UserSession(
            context=object(),
            login_ok=False,
            storage_state_path=Path(f"/tmp/{discord_user_id}.json"),
            meta_lock=asyncio.Lock(),
        )

    s1, s2 = await asyncio.gather(
        _get_or_create_user_session(sessions, creating_sessions, sessions_lock, "111", create_session),
        _get_or_create_user_session(sessions, creating_sessions, sessions_lock, "111", create_session),
    )

    assert s1 is s2
    assert call_count == 1


async def test_get_or_create_user_session_cleans_failed_creation_task() -> None:
    sessions: dict[str, UserSession] = {}
    creating_sessions: dict[str, asyncio.Task[UserSession]] = {}
    sessions_lock = asyncio.Lock()

    async def create_session(discord_user_id: str) -> UserSession:
        raise RuntimeError(f"boom-{discord_user_id}")

    for _ in range(2):
        try:
            await _get_or_create_user_session(sessions, creating_sessions, sessions_lock, "111", create_session)
        except RuntimeError:
            pass

    assert "111" not in creating_sessions
