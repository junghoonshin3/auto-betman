from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from src.main import (
    _AUTO_RELOGIN_FAILED_MESSAGE,
    _SESSION_EXPIRED_MESSAGE,
    UserSession,
    _delete_saved_login_credentials,
    _ensure_logged_in_with_auto_relogin,
    _get_saved_login_credentials,
    _load_login_credentials_map,
    _set_saved_login_credentials,
)


def _session() -> UserSession:
    return UserSession(
        context=object(),
        login_ok=False,
        storage_state_path=Path("/tmp/session.json"),
        meta_lock=asyncio.Lock(),
    )


def test_login_credentials_map_save_load_delete(tmp_path: Path) -> None:
    path = tmp_path / "login_credentials_map.json"

    _set_saved_login_credentials("111", "alpha", "pw-1", path)
    _set_saved_login_credentials("222", "beta", "pw-2", path)

    assert _get_saved_login_credentials("111", path) == ("alpha", "pw-1")
    assert _get_saved_login_credentials("222", path) == ("beta", "pw-2")

    assert _delete_saved_login_credentials("111", path) is True
    assert _get_saved_login_credentials("111", path) is None
    assert _get_saved_login_credentials("222", path) == ("beta", "pw-2")


def test_login_credentials_map_corrupted_file_recovers_empty(tmp_path: Path) -> None:
    path = tmp_path / "login_credentials_map.json"
    path.write_text("{invalid-json", encoding="utf-8")

    assert _load_login_credentials_map(path) == {}


def test_login_credentials_empty_values_are_ignored(tmp_path: Path) -> None:
    path = tmp_path / "login_credentials_map.json"
    _set_saved_login_credentials("111", "   ", "pw", path)
    _set_saved_login_credentials("111", "alpha", "   ", path)
    assert _get_saved_login_credentials("111", path) is None


async def test_auto_relogin_skipped_when_already_logged_in(monkeypatch) -> None:
    session = _session()
    session.login_ok = True
    relogin = AsyncMock(return_value=True)

    await _ensure_logged_in_with_auto_relogin(
        session,
        "111",
        relogin,
        allow_auto_relogin=True,
    )

    relogin.assert_not_awaited()


async def test_auto_relogin_succeeds_with_saved_credentials(monkeypatch) -> None:
    session = _session()
    monkeypatch.setattr(
        "src.main._ensure_logged_in",
        AsyncMock(
            side_effect=[
                RuntimeError(_SESSION_EXPIRED_MESSAGE),
                RuntimeError(_SESSION_EXPIRED_MESSAGE),
                None,
            ]
        ),
    )
    monkeypatch.setattr("src.main._get_saved_login_credentials", Mock(return_value=("saved-id", "saved-pw")))
    relogin = AsyncMock(return_value=True)

    await _ensure_logged_in_with_auto_relogin(
        session,
        "111",
        relogin,
        allow_auto_relogin=True,
    )

    relogin.assert_awaited_once_with("111", "saved-id", "saved-pw")


async def test_auto_relogin_keeps_original_error_when_no_saved_credentials(monkeypatch) -> None:
    session = _session()
    monkeypatch.setattr("src.main._ensure_logged_in", AsyncMock(side_effect=RuntimeError(_SESSION_EXPIRED_MESSAGE)))
    monkeypatch.setattr("src.main._get_saved_login_credentials", Mock(return_value=None))
    relogin = AsyncMock(return_value=True)

    with pytest.raises(RuntimeError, match="세션이 만료되었습니다"):
        await _ensure_logged_in_with_auto_relogin(
            session,
            "111",
            relogin,
            allow_auto_relogin=True,
        )

    relogin.assert_not_awaited()


async def test_auto_relogin_failure_deletes_saved_credentials_and_raises(monkeypatch) -> None:
    session = _session()
    monkeypatch.setattr(
        "src.main._ensure_logged_in",
        AsyncMock(
            side_effect=[
                RuntimeError(_SESSION_EXPIRED_MESSAGE),
                RuntimeError(_SESSION_EXPIRED_MESSAGE),
            ]
        ),
    )
    monkeypatch.setattr(
        "src.main._get_saved_login_credentials",
        Mock(side_effect=[("saved-id", "saved-pw"), ("saved-id", "saved-pw")]),
    )
    delete_mock = Mock(return_value=True)
    monkeypatch.setattr("src.main._delete_saved_login_credentials", delete_mock)
    relogin = AsyncMock(return_value=False)

    with pytest.raises(RuntimeError, match=_AUTO_RELOGIN_FAILED_MESSAGE):
        await _ensure_logged_in_with_auto_relogin(
            session,
            "111",
            relogin,
            allow_auto_relogin=True,
        )

    relogin.assert_awaited_once_with("111", "saved-id", "saved-pw")
    delete_mock.assert_called_once_with("111")
