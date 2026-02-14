from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

from src.main import (
    AutoNotifyState,
    _SESSION_EXPIRED_MESSAGE,
    _build_auto_notify_content,
    _build_session_expired_content,
    _load_fake_purchases,
    _parse_notify_channel_id,
    _parse_polling_interval_minutes,
    _restore_watch_user_ids_from_session_files,
    _run_auto_notify_cycle,
    _select_and_commit_new_purchase_slips_for_user,
    _track_user_for_auto_notify,
    _untrack_user_for_auto_notify,
)
from src.models import BetSlip


def _slip(slip_id: str, purchase_datetime: str) -> BetSlip:
    return BetSlip(
        slip_id=slip_id,
        game_type="프로토",
        round_number="1회차",
        status="발매중",
        purchase_datetime=purchase_datetime,
        total_amount=5000,
        potential_payout=10000,
        combined_odds=2.0,
    )


async def test_auto_notify_first_cycle_sets_baseline_without_sending() -> None:
    state = AutoNotifyState(watch_user_ids={"111"})
    fetch = AsyncMock(return_value=[_slip("A", "2026.02.13 10:00")])
    send_new = AsyncMock()
    send_expired = AsyncMock()

    await _run_auto_notify_cycle(state, fetch, send_new, send_expired)

    fetch.assert_awaited_once_with("111")
    send_new.assert_not_awaited()
    send_expired.assert_not_awaited()
    assert state.seen_slip_ids_by_user["111"] == {"A"}


async def test_auto_notify_second_cycle_sends_only_new_slips_in_old_to_new_order() -> None:
    state = AutoNotifyState(
        watch_user_ids={"111"},
        seen_slip_ids_by_user={"111": {"A"}},
    )
    # Source list order is newest -> oldest. Notification must be oldest -> newest for new slips.
    fetch = AsyncMock(return_value=[_slip("C", "2026.02.13 12:00"), _slip("B", "2026.02.13 11:00"), _slip("A", "2026.02.13 10:00")])
    send_new = AsyncMock()
    send_expired = AsyncMock()

    await _run_auto_notify_cycle(state, fetch, send_new, send_expired)

    send_expired.assert_not_awaited()
    send_new.assert_awaited_once()
    sent_user_id, sent_slips = send_new.await_args.args
    assert sent_user_id == "111"
    assert [s.slip_id for s in sent_slips] == ["B", "C"]
    assert state.seen_slip_ids_by_user["111"] == {"A", "B", "C"}


async def test_auto_notify_no_changes_sends_nothing() -> None:
    state = AutoNotifyState(
        watch_user_ids={"111"},
        seen_slip_ids_by_user={"111": {"A", "B"}},
    )
    fetch = AsyncMock(return_value=[_slip("B", "2026.02.13 11:00"), _slip("A", "2026.02.13 10:00")])
    send_new = AsyncMock()
    send_expired = AsyncMock()

    await _run_auto_notify_cycle(state, fetch, send_new, send_expired)

    send_new.assert_not_awaited()
    send_expired.assert_not_awaited()


async def test_track_and_untrack_user_for_auto_notify() -> None:
    state = AutoNotifyState()
    await _track_user_for_auto_notify(state, "111")

    assert "111" in state.watch_user_ids
    assert "111" not in state.seen_slip_ids_by_user

    state.seen_slip_ids_by_user["111"] = {"A"}
    state.session_expired_notified_user_ids.add("111")
    await _track_user_for_auto_notify(state, "111", reset_baseline=False)
    assert "111" in state.watch_user_ids
    assert state.seen_slip_ids_by_user["111"] == {"A"}
    assert "111" not in state.session_expired_notified_user_ids

    await _untrack_user_for_auto_notify(state, "111")
    assert "111" not in state.watch_user_ids
    assert "111" not in state.seen_slip_ids_by_user
    assert "111" not in state.session_expired_notified_user_ids


def test_restore_watch_user_ids_from_session_files(tmp_path: Path) -> None:
    (tmp_path / "session_state_111.json").write_text("{}", encoding="utf-8")
    (tmp_path / "session_state_222.json").write_text("", encoding="utf-8")
    (tmp_path / "session_state_abc-XYZ.json").write_text("{}", encoding="utf-8")
    (tmp_path / "not_a_session_file.json").write_text("{}", encoding="utf-8")

    restored = _restore_watch_user_ids_from_session_files(tmp_path)
    assert restored == {"111", "abc-XYZ"}


async def test_auto_notify_session_expired_untracks_and_sends_notice() -> None:
    state = AutoNotifyState(
        watch_user_ids={"111"},
        seen_slip_ids_by_user={"111": {"A"}},
    )
    fetch = AsyncMock(side_effect=RuntimeError(_SESSION_EXPIRED_MESSAGE))
    send_new = AsyncMock()
    send_expired = AsyncMock()

    await _run_auto_notify_cycle(state, fetch, send_new, send_expired)

    send_new.assert_not_awaited()
    send_expired.assert_awaited_once_with("111")
    assert "111" not in state.watch_user_ids
    assert state.seen_slip_ids_by_user["111"] == {"A"}


async def test_select_and_commit_new_purchase_slips_for_user_tracks_seen_and_returns_new() -> None:
    state = AutoNotifyState(
        watch_user_ids={"111"},
        seen_slip_ids_by_user={"111": {"A"}},
    )
    slips = [_slip("C", "2026.02.13 12:00"), _slip("B", "2026.02.13 11:00"), _slip("A", "2026.02.13 10:00")]

    new_slips, baseline_only = await _select_and_commit_new_purchase_slips_for_user(state, "111", slips)

    assert baseline_only is False
    assert [s.slip_id for s in new_slips] == ["B", "C"]
    assert state.seen_slip_ids_by_user["111"] == {"A", "B", "C"}


async def test_relogin_uses_previous_seen_baseline_and_detects_new_slips() -> None:
    state = AutoNotifyState(
        watch_user_ids=set(),
        seen_slip_ids_by_user={"111": {"A"}},
    )

    await _track_user_for_auto_notify(state, "111", reset_baseline=False)
    slips = [_slip("C", "2026.02.13 12:00"), _slip("B", "2026.02.13 11:00"), _slip("A", "2026.02.13 10:00")]
    new_slips, baseline_only = await _select_and_commit_new_purchase_slips_for_user(state, "111", slips)

    assert baseline_only is False
    assert [s.slip_id for s in new_slips] == ["B", "C"]
    assert state.seen_slip_ids_by_user["111"] == {"A", "B", "C"}


def test_auto_notify_message_has_no_mention() -> None:
    assert "<@" not in _build_auto_notify_content("111", 2)
    assert "<@" not in _build_session_expired_content("111")


def test_parse_notify_channel_id() -> None:
    assert _parse_notify_channel_id("12345") == 12345
    assert _parse_notify_channel_id(" 12345 ") == 12345
    assert _parse_notify_channel_id(None) is None
    assert _parse_notify_channel_id("") is None
    assert _parse_notify_channel_id("abc") is None
    assert _parse_notify_channel_id("-1") is None


def test_parse_polling_interval_minutes() -> None:
    assert _parse_polling_interval_minutes(None, default=5) == 5
    assert _parse_polling_interval_minutes("", default=5) == 5
    assert _parse_polling_interval_minutes("5", default=5) == 5
    assert _parse_polling_interval_minutes("0", default=5) == 1
    assert _parse_polling_interval_minutes("99", default=5) == 60
    assert _parse_polling_interval_minutes("oops", default=5) == 5


def test_load_fake_purchases_from_list_json(tmp_path: Path) -> None:
    fake_path = tmp_path / "fake.json"
    fake_path.write_text(
        """
[
  {"slip_id": "S-2", "game_type": "프로토", "status": "발매중", "purchase_datetime": "2026.02.14 10:00", "total_amount": 7000},
  {"slip_id": "S-1", "game_type": "프로토", "status": "발매중", "purchase_datetime": "2026.02.14 09:00", "total_amount": 5000}
]
""".strip(),
        encoding="utf-8",
    )

    slips = _load_fake_purchases(str(fake_path), "111", limit=30)
    assert slips is not None
    assert [s.slip_id for s in slips] == ["S-2", "S-1"]


def test_load_fake_purchases_supports_by_user_and_default(tmp_path: Path) -> None:
    fake_path = tmp_path / "fake.json"
    fake_path.write_text(
        """
{
  "default": [{"slip_id": "DEF-1", "total_amount": 1000}],
  "by_user": {
    "111": [{"slip_id": "U-111-1", "total_amount": 2000}]
  }
}
""".strip(),
        encoding="utf-8",
    )

    user_slips = _load_fake_purchases(str(fake_path), "111", limit=30)
    other_slips = _load_fake_purchases(str(fake_path), "999", limit=30)
    assert user_slips is not None
    assert other_slips is not None
    assert [s.slip_id for s in user_slips] == ["U-111-1"]
    assert [s.slip_id for s in other_slips] == ["DEF-1"]
