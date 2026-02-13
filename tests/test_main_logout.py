from __future__ import annotations

import asyncio
from pathlib import Path

from src.main import (
    UserSession,
    _legacy_session_state_path,
    _remove_user_session_files,
    _session_state_path,
    _stop_keepalive,
    _wait_until_no_active_requests,
)


def test_remove_user_session_files_removes_current_and_legacy(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("src.main.SESSION_DIR", tmp_path)
    user_id = "111"
    current = _session_state_path(user_id)
    legacy = _legacy_session_state_path(user_id)
    current.write_text("{}", encoding="utf-8")
    legacy.write_text("{}", encoding="utf-8")

    _remove_user_session_files(user_id)

    assert not current.exists()
    assert not legacy.exists()


async def test_logout_removes_only_target_user_session(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("src.main.SESSION_DIR", tmp_path)

    class _Ctx:
        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    def _make_session(uid: str) -> UserSession:
        path = _session_state_path(uid)
        path.write_text("{}", encoding="utf-8")
        return UserSession(
            context=_Ctx(),
            login_ok=True,
            storage_state_path=path,
            meta_lock=asyncio.Lock(),
        )

    user_sessions: dict[str, UserSession] = {
        "111": _make_session("111"),
        "222": _make_session("222"),
    }
    sessions_lock = asyncio.Lock()

    async def do_logout(discord_user_id: str) -> bool:
        async with sessions_lock:
            session = user_sessions.pop(discord_user_id, None)
        if session is not None:
            await session.context.close()
        _remove_user_session_files(discord_user_id)
        return True

    assert await do_logout("111") is True
    assert "111" not in user_sessions
    assert "222" in user_sessions
    assert not _session_state_path("111").exists()
    assert _session_state_path("222").exists()


async def test_logout_with_missing_session_still_succeeds(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("src.main.SESSION_DIR", tmp_path)
    user_sessions: dict[str, UserSession] = {}
    sessions_lock = asyncio.Lock()

    async def do_logout(discord_user_id: str) -> bool:
        async with sessions_lock:
            _ = user_sessions.pop(discord_user_id, None)
        _remove_user_session_files(discord_user_id)
        return True

    assert await do_logout("999") is True


async def test_wait_until_no_active_requests_returns_true_when_drained() -> None:
    session = UserSession(
        context=object(),
        login_ok=True,
        storage_state_path=Path("/tmp/test.json"),
        meta_lock=asyncio.Lock(),
        active_requests=1,
    )

    async def drain() -> None:
        await asyncio.sleep(0.05)
        async with session.meta_lock:
            session.active_requests = 0

    waiter = asyncio.create_task(_wait_until_no_active_requests(session, timeout_seconds=1.0, poll_seconds=0.01))
    await drain()
    assert await waiter is True


async def test_wait_until_no_active_requests_times_out() -> None:
    session = UserSession(
        context=object(),
        login_ok=True,
        storage_state_path=Path("/tmp/test.json"),
        meta_lock=asyncio.Lock(),
        active_requests=1,
    )
    ok = await _wait_until_no_active_requests(session, timeout_seconds=0.05, poll_seconds=0.01)
    assert ok is False


async def test_logout_stop_keepalive_before_close() -> None:
    class _Ctx:
        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    session = UserSession(
        context=_Ctx(),
        login_ok=True,
        storage_state_path=Path("/tmp/test.json"),
        meta_lock=asyncio.Lock(),
    )
    session.keepalive_task = asyncio.create_task(asyncio.sleep(3600))

    await _stop_keepalive(session)
    await session.context.close()

    assert session.keepalive_task is None
    assert session.context.closed is True
