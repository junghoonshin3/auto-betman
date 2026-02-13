from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.bot import LoginModal, _get_saved_login_id, _load_login_id_map, _set_saved_login_id


def test_login_id_map_user_scoped_save_and_load(tmp_path) -> None:
    path = tmp_path / "login_id_map.json"

    _set_saved_login_id("111", "alpha", path)
    _set_saved_login_id("222", "beta", path)

    assert _get_saved_login_id("111", path) == "alpha"
    assert _get_saved_login_id("222", path) == "beta"


def test_login_id_map_corrupted_file_recovers_empty(tmp_path) -> None:
    path = tmp_path / "login_id_map.json"
    path.write_text("{invalid-json", encoding="utf-8")

    assert _load_login_id_map(path) == {}


def test_login_id_empty_value_is_ignored(tmp_path) -> None:
    path = tmp_path / "login_id_map.json"
    _set_saved_login_id("111", "   ", path)
    assert _get_saved_login_id("111", path) is None


async def test_login_modal_passes_discord_user_id_to_callback(monkeypatch) -> None:
    monkeypatch.setattr("src.bot._set_saved_login_id", lambda *args, **kwargs: None)
    login_callback = AsyncMock(return_value=True)
    modal = LoginModal(login_callback, discord_user_id="999999")
    modal.user_id._value = "sample-id"  # type: ignore[attr-defined]
    modal.user_pw._value = "sample-pw"  # type: ignore[attr-defined]

    progress_message = SimpleNamespace(edit=AsyncMock())
    interaction = SimpleNamespace(
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock(return_value=progress_message)),
    )

    await modal.on_submit(interaction)

    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    login_callback.assert_awaited_once_with("999999", "sample-id", "sample-pw")
