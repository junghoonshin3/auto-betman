from __future__ import annotations

from unittest.mock import AsyncMock

from src.games import (
    _capture_games_detail_files_from_href,
    _capture_games_detail_row_batches,
    capture_sale_games_list_screenshots,
    normalize_games_capture_game_type,
)


class _FakePage:
    async def wait_for_load_state(self, *_args, **_kwargs) -> None:
        return None


def _row(text: str, href: str, sport_code: str = "") -> dict[str, object]:
    return {"text": text, "href": href, "sportCode": sport_code}


async def test_normalize_games_capture_game_type_maps_legacy_all_to_victory() -> None:
    assert normalize_games_capture_game_type("all") == "victory"
    assert normalize_games_capture_game_type("victory") == "victory"


async def test_capture_sale_games_list_screenshots_collects_gameslip_targets_and_captures(monkeypatch) -> None:
    page = _FakePage()
    captured_hrefs: list[str] = []

    async def _tables(_page: _FakePage) -> list[dict[str, str]]:
        return [
            {"table_key": "proto", "wrapper_selector": "w1", "table_selector": "t1", "capture_selector": "w1"},
            {"table_key": "toto", "wrapper_selector": "w2", "table_selector": "t2", "capture_selector": "w2"},
        ]

    async def _rows(_page: _FakePage, table_selector: str) -> list[dict[str, object]]:
        if table_selector == "t1":
            return [
                _row("프로토 승부식 20회차", "/main/mainPage/gamebuy/gameSlip.do?gmId=G101&gmTs=260020"),
                _row("프로토 승부식 20회차", "/main/mainPage/gamebuy/gameSlip.do?gmId=G101&gmTs=260020"),
                _row("프로토 기록식 14회차", "/main/mainPage/gamebuy/gameSlip.do?gmId=G102&gmTs=14&year=2026"),
            ]
        return [
            _row("축구 스페셜 트리플 7회차", "/main/mainPage/gamebuy/gameSlip.do?gmId=G016&gmTs=260007", "SC"),
            _row("축구 승무패 11회차", "/main/mainPage/gamebuy/gameSlip.do?gmId=G011&gmTs=260011", "SC"),
        ]

    async def _capture_detail(
        _page: _FakePage,
        *,
        href: str,
        gm_id: str,
        game_type: str,
        sport: str,
        seq: int,
        image_slots: int,
    ) -> list[tuple[str, bytes]]:
        captured_hrefs.append(href)
        return [(f"games_{game_type}_{sport}_{gm_id.lower()}_{seq:02d}.jpg", f"img-{seq}".encode("utf-8"))]

    monkeypatch.setattr("src.games._navigate_to_buyable_game_list", AsyncMock())
    monkeypatch.setattr("src.games._resolve_games_table_targets", _tables)
    monkeypatch.setattr("src.games._wait_for_games_tables_stable", AsyncMock(return_value={"rowCount": 5, "signature": "stable"}))
    monkeypatch.setattr("src.games._collect_games_rows_meta", _rows)
    monkeypatch.setattr("src.games._capture_games_detail_files_from_href", _capture_detail)

    result = await capture_sale_games_list_screenshots(page, "victory", "all")

    assert result.captured_count == 2
    assert result.truncated is False
    assert captured_hrefs == [
        "https://www.betman.co.kr/main/mainPage/gamebuy/gameSlip.do?gmId=G101&gmTs=260020",
        "https://www.betman.co.kr/main/mainPage/gamebuy/gameSlip.do?gmId=G016&gmTs=260007",
    ]


async def test_capture_sale_games_list_screenshots_filters_record(monkeypatch) -> None:
    page = _FakePage()
    captured: list[str] = []

    monkeypatch.setattr("src.games._navigate_to_buyable_game_list", AsyncMock())
    monkeypatch.setattr(
        "src.games._resolve_games_table_targets",
        AsyncMock(return_value=[{"table_key": "proto", "wrapper_selector": "w", "table_selector": "t", "capture_selector": "w"}]),
    )
    monkeypatch.setattr("src.games._wait_for_games_tables_stable", AsyncMock(return_value={"rowCount": 2, "signature": "stable"}))
    monkeypatch.setattr(
        "src.games._collect_games_rows_meta",
        AsyncMock(
            return_value=[
                _row("프로토 승부식 20회차", "/main/mainPage/gamebuy/gameSlip.do?gmId=G101&gmTs=260020"),
                _row("프로토 기록식 14회차", "/main/mainPage/gamebuy/gameSlip.do?gmId=G102&gmTs=14&year=2026"),
            ]
        ),
    )

    async def _capture_detail(
        _page: _FakePage, *, href: str, gm_id: str, game_type: str, sport: str, seq: int, image_slots: int
    ) -> list[tuple[str, bytes]]:
        captured.append(gm_id)
        return [(f"games_{game_type}_{sport}_{gm_id.lower()}_{seq:02d}.jpg", b"img")]

    monkeypatch.setattr("src.games._capture_games_detail_files_from_href", _capture_detail)

    result = await capture_sale_games_list_screenshots(page, "record", "all")

    assert result.captured_count == 1
    assert captured == ["G102"]


async def test_capture_sale_games_list_screenshots_filters_sport_excluding_unknown(monkeypatch) -> None:
    page = _FakePage()
    captured: list[str] = []

    monkeypatch.setattr("src.games._navigate_to_buyable_game_list", AsyncMock())
    monkeypatch.setattr(
        "src.games._resolve_games_table_targets",
        AsyncMock(return_value=[{"table_key": "all", "wrapper_selector": "w", "table_selector": "t", "capture_selector": "w"}]),
    )
    monkeypatch.setattr("src.games._wait_for_games_tables_stable", AsyncMock(return_value={"rowCount": 2, "signature": "stable"}))
    monkeypatch.setattr(
        "src.games._collect_games_rows_meta",
        AsyncMock(
            return_value=[
                _row("프로토 승부식 20회차", "/main/mainPage/gamebuy/gameSlip.do?gmId=G101&gmTs=260020"),
                _row("축구 스페셜 트리플 7회차", "/main/mainPage/gamebuy/gameSlip.do?gmId=G016&gmTs=260007", "SC"),
            ]
        ),
    )

    async def _capture_detail(
        _page: _FakePage, *, href: str, gm_id: str, game_type: str, sport: str, seq: int, image_slots: int
    ) -> list[tuple[str, bytes]]:
        captured.append(gm_id)
        return [(f"games_{game_type}_{sport}_{gm_id.lower()}_{seq:02d}.jpg", b"img")]

    monkeypatch.setattr("src.games._capture_games_detail_files_from_href", _capture_detail)

    result = await capture_sale_games_list_screenshots(page, "victory", "soccer")

    assert result.captured_count == 1
    assert captured == ["G016"]


async def test_capture_sale_games_list_screenshots_respects_max_images(monkeypatch) -> None:
    page = _FakePage()
    calls = 0

    monkeypatch.setattr("src.games._navigate_to_buyable_game_list", AsyncMock())
    monkeypatch.setattr(
        "src.games._resolve_games_table_targets",
        AsyncMock(return_value=[{"table_key": "all", "wrapper_selector": "w", "table_selector": "t", "capture_selector": "w"}]),
    )
    monkeypatch.setattr("src.games._wait_for_games_tables_stable", AsyncMock(return_value={"rowCount": 3, "signature": "stable"}))
    monkeypatch.setattr(
        "src.games._collect_games_rows_meta",
        AsyncMock(
            return_value=[
                _row("프로토 승부식 20회차", "/main/mainPage/gamebuy/gameSlip.do?gmId=G101&gmTs=260020"),
                _row("축구 스페셜 트리플 7회차", "/main/mainPage/gamebuy/gameSlip.do?gmId=G016&gmTs=260007", "SC"),
                _row("농구 매치 30회차", "/main/mainPage/gamebuy/gameSlip.do?gmId=G015&gmTs=260030", "BK"),
            ]
        ),
    )

    async def _capture_detail(
        _page: _FakePage, *, href: str, gm_id: str, game_type: str, sport: str, seq: int, image_slots: int
    ) -> list[tuple[str, bytes]]:
        nonlocal calls
        calls += 1
        return [(f"games_{game_type}_{sport}_{gm_id.lower()}_{seq:02d}.jpg", f"img-{seq}".encode("utf-8"))]

    monkeypatch.setattr("src.games._capture_games_detail_files_from_href", _capture_detail)

    result = await capture_sale_games_list_screenshots(page, "victory", "all", max_images=2)

    assert result.captured_count == 2
    assert result.truncated is True
    assert calls == 2


async def test_capture_sale_games_list_screenshots_fails_when_list_selector_missing(monkeypatch) -> None:
    page = _FakePage()
    monkeypatch.setattr("src.games._navigate_to_buyable_game_list", AsyncMock())
    monkeypatch.setattr("src.games._resolve_games_table_targets", AsyncMock(return_value=[]))

    result = await capture_sale_games_list_screenshots(page, "victory", "all")

    assert result.captured_count == 0
    assert result.files == []


async def test_capture_sale_games_list_screenshots_handles_not_ready(monkeypatch) -> None:
    page = _FakePage()
    monkeypatch.setattr("src.games._navigate_to_buyable_game_list", AsyncMock())
    monkeypatch.setattr(
        "src.games._resolve_games_table_targets",
        AsyncMock(return_value=[{"table_key": "all", "wrapper_selector": "w", "table_selector": "t", "capture_selector": "w"}]),
    )
    monkeypatch.setattr("src.games._wait_for_games_tables_stable", AsyncMock(return_value=None))

    result = await capture_sale_games_list_screenshots(page, "victory", "all")

    assert result.captured_count == 0
    assert result.files == []


async def test_capture_games_detail_row_batches_splits_every_8_rows(monkeypatch) -> None:
    class _FakeLocator:
        def __init__(self) -> None:
            self.calls = 0

        async def screenshot(self, *, type: str, quality: int) -> bytes:
            self.calls += 1
            assert type == "jpeg"
            assert quality == 80
            return f"img-{self.calls}".encode("utf-8")

    class _FakeLocatorProxy:
        def __init__(self, locator: _FakeLocator) -> None:
            self._locator = locator

        @property
        def first(self) -> _FakeLocator:
            return self._locator

    class _FakeCapturePage:
        def __init__(self) -> None:
            self.locator_impl = _FakeLocator()

        def locator(self, _selector: str) -> _FakeLocatorProxy:
            return _FakeLocatorProxy(self.locator_impl)

        async def wait_for_timeout(self, _ms: int) -> None:
            return None

    page = _FakeCapturePage()
    applied_batches: list[list[int]] = []
    restore_mock = AsyncMock(return_value=True)
    monkeypatch.setattr("src.games._read_games_detail_visible_row_indices", AsyncMock(return_value=list(range(20))))

    async def _set_rows(_page: _FakeCapturePage, _selector: str, visible_indices: list[int]) -> bool:
        applied_batches.append(list(visible_indices))
        return True

    monkeypatch.setattr("src.games._set_games_detail_visible_rows", _set_rows)
    monkeypatch.setattr("src.games._restore_games_detail_rows_visibility", restore_mock)

    files = await _capture_games_detail_row_batches(
        page,
        capture_selector="#tabs-1",
        filename_prefix="games_victory_all_g101_01",
        gm_id="G101",
        slots_left=10,
        rows_per_image=8,
    )

    assert [name for name, _ in files] == [
        "games_victory_all_g101_01_p01.jpg",
        "games_victory_all_g101_01_p02.jpg",
        "games_victory_all_g101_01_p03.jpg",
    ]
    assert [len(batch) for batch in applied_batches] == [8, 8, 4]
    restore_mock.assert_awaited_once_with(page, "#tabs-1")


async def test_capture_games_detail_files_from_href_falls_back_to_single_when_no_row_batches(monkeypatch) -> None:
    class _FakeLocator:
        async def count(self) -> int:
            return 1

        async def screenshot(self, *, type: str, quality: int) -> bytes:
            assert type == "jpeg"
            assert quality == 80
            return b"jpeg-bytes"

    class _FakeLocatorProxy:
        @property
        def first(self) -> _FakeLocator:
            return _FakeLocator()

    class _FakeCapturePage:
        def locator(self, _selector: str) -> _FakeLocatorProxy:
            return _FakeLocatorProxy()

    monkeypatch.setattr("src.games._open_gameslip_detail_page", AsyncMock(return_value=True))
    monkeypatch.setattr("src.games._wait_for_games_detail_capture_selector", AsyncMock(return_value="#div_gmBuySlip"))
    monkeypatch.setattr("src.games._capture_games_detail_row_batches", AsyncMock(return_value=[]))

    files = await _capture_games_detail_files_from_href(
        _FakeCapturePage(),
        href="https://www.betman.co.kr/main/mainPage/gamebuy/gameSlip.do?gmId=G101&gmTs=260020",
        gm_id="G101",
        game_type="victory",
        sport="all",
        seq=1,
        image_slots=5,
    )

    assert files == [("games_victory_all_g101_01.jpg", b"jpeg-bytes")]


async def test_capture_games_detail_files_from_href_prefers_row_batches(monkeypatch) -> None:
    class _FailingFallbackLocator:
        async def count(self) -> int:
            return 1

        async def screenshot(self, *, type: str, quality: int) -> bytes:
            raise AssertionError("single screenshot fallback should not be called when row batches succeed")

    class _FailingLocatorProxy:
        @property
        def first(self) -> _FailingFallbackLocator:
            return _FailingFallbackLocator()

    class _FakeCapturePage:
        def locator(self, _selector: str) -> _FailingLocatorProxy:
            return _FailingLocatorProxy()

    monkeypatch.setattr("src.games._open_gameslip_detail_page", AsyncMock(return_value=True))
    monkeypatch.setattr("src.games._wait_for_games_detail_capture_selector", AsyncMock(return_value="#tabs-1"))
    monkeypatch.setattr(
        "src.games._capture_games_detail_row_batches",
        AsyncMock(return_value=[("games_record_all_g102_02_p01.jpg", b"p1"), ("games_record_all_g102_02_p02.jpg", b"p2")]),
    )

    files = await _capture_games_detail_files_from_href(
        _FakeCapturePage(),
        href="https://www.betman.co.kr/main/mainPage/gamebuy/gameSlip.do?gmId=G102&gmTs=14&year=2026",
        gm_id="G102",
        game_type="record",
        sport="all",
        seq=2,
        image_slots=5,
    )

    assert files == [("games_record_all_g102_02_p01.jpg", b"p1"), ("games_record_all_g102_02_p02.jpg", b"p2")]
