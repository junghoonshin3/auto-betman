from __future__ import annotations

import asyncio
import time

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from src.purchases import (
    _build_recent_purchases_token_from_items,
    _extract_purchase_items,
    _next_start_row,
    _recent5_range_ymd,
    _status_result_from_buy_status_info,
    _status_result_from_list_item,
    capture_purchase_paper_area_snapshots,
)


@pytest.fixture(autouse=True)
def _fast_stable_wait(monkeypatch) -> None:
    monkeypatch.setattr("src.purchases._PAPER_STABLE_TIMEOUT_MS", 40)
    monkeypatch.setattr("src.purchases._PAPER_STABLE_SAMPLE_INTERVAL_MS", 1)
    monkeypatch.setattr("src.purchases._PAPER_STABLE_ROUNDS", 2)


def test_extract_purchase_items_from_purchase_win_key() -> None:
    payload = {
        "purchaseWin": [
            {"btkNum": "AAA-1111-2222-3333"},
            {"btkNum": "BBB-1111-2222-3333"},
        ]
    }
    items = _extract_purchase_items(payload)
    assert len(items) == 2
    assert items[0]["btkNum"] == "AAA-1111-2222-3333"


def test_status_loss_string_is_not_treated_as_win() -> None:
    status, result = _status_result_from_list_item({"buyStatusName": "적중안됨"})
    assert status == "적중안됨"
    assert result == "미적중"


def test_status_mapping_from_buy_status_code() -> None:
    assert _status_result_from_buy_status_info(5, "") == ("적중", "적중")
    assert _status_result_from_buy_status_info(6, "") == ("적중안됨", "미적중")
    assert _status_result_from_buy_status_info(7, "") == ("적중안됨", "미적중")


def test_recent5_range_ymd() -> None:
    start, end = _recent5_range_ymd()
    assert len(start) == 8
    assert len(end) == 8
    assert start <= end


def test_next_start_row_pagination() -> None:
    assert _next_start_row(1, 30) == 31
    assert _next_start_row(31, 5) == 36


def test_recent_purchases_probe_token_is_order_stable() -> None:
    rows_a = [
        {"btkNum": "A", "buyDtm": "20260213120000", "buyStatusCode": "5", "buyAmt": "1000", "procRsltClCd": "1", "gmStCd": "4"},
        {"btkNum": "B", "buyDtm": "20260212120000", "buyStatusCode": "3", "buyAmt": "2000", "procRsltClCd": "0", "gmStCd": "2"},
    ]
    rows_b = list(reversed(rows_a))

    token_a = _build_recent_purchases_token_from_items(rows_a, limit=5)
    token_b = _build_recent_purchases_token_from_items(rows_b, limit=5)
    assert token_a == token_b


def test_recent_purchases_probe_token_changes_when_core_field_changes() -> None:
    base = [
        {"btkNum": "A", "buyDtm": "20260213120000", "buyStatusCode": "5", "buyAmt": "1000", "procRsltClCd": "1", "gmStCd": "4"},
    ]
    changed = [
        {"btkNum": "A", "buyDtm": "20260213120000", "buyStatusCode": "6", "buyAmt": "1000", "procRsltClCd": "1", "gmStCd": "4"},
    ]
    assert _build_recent_purchases_token_from_items(base, limit=5) != _build_recent_purchases_token_from_items(changed, limit=5)


class _FakeCell:
    def __init__(self, text: str) -> None:
        self._text = text

    async def text_content(self) -> str:
        return self._text


class _FakeCells:
    def __init__(self, slip_id: str) -> None:
        self._slip_id = slip_id

    async def count(self) -> int:
        return 4

    async def all_text_contents(self) -> list[str]:
        return ["row", "type", "time", self._slip_id]

    def nth(self, index: int) -> _FakeCell:
        return _FakeCell(self._slip_id if index == 3 else "")


def _extract_open_game_paper_arg(value: str) -> str:
    text = str(value or "")
    marker = "openGamePaper("
    if marker not in text:
        return ""
    part = text.split(marker, maxsplit=1)[1].split(")", maxsplit=1)[0]
    raw_args = [seg.strip().strip("'").strip('"') for seg in part.split(",")]
    ignored = {"", "this", "undefined", "null"}
    for arg in raw_args:
        if arg.lower() in ignored:
            continue
        if "-" in arg:
            return arg
    for arg in raw_args:
        if arg.lower() in ignored:
            continue
        return arg
    return ""


class _FakeTrigger:
    def __init__(self, row: "_FakeRow", route: str, *, exists: bool) -> None:
        self._row = row
        self._route = route
        self._exists = exists

    @property
    def first(self) -> "_FakeTrigger":
        return self

    def nth(self, _index: int) -> "_FakeTrigger":
        return self

    async def count(self) -> int:
        return 1 if self._exists else 0

    async def click(self, **_kwargs: object) -> None:
        self._row.page.clicked_trigger_routes.append(self._route)
        if self._route == "vote-link" and self._row.vote_link_click_fails:
            raise RuntimeError("vote link click failed")
        if self._route == "vote-button" and self._row.vote_button_click_fails:
            raise RuntimeError("vote button click failed")
        if self._route == "openGamePaper-href" and self._row.href_click_fails:
            raise RuntimeError("href click failed")
        if self._route == "openGamePaper" and self._row.click_fails:
            raise RuntimeError("click failed")
        open_slip_id = self._row.slip_id
        if self._route == "openGamePaper":
            open_slip_id = _extract_open_game_paper_arg(self._row.onclick) or open_slip_id
        elif self._route == "openGamePaper-href":
            open_slip_id = _extract_open_game_paper_arg(self._row.href_open_code) or open_slip_id
        elif self._route == "vote-link":
            open_slip_id = _extract_open_game_paper_arg(self._row.vote_link_onclick) or open_slip_id
        elif self._route == "vote-button":
            open_slip_id = _extract_open_game_paper_arg(self._row.vote_button_onclick) or open_slip_id
        if self._row.click_open_slip_id:
            open_slip_id = self._row.click_open_slip_id
        self._row.page._open(
            open_slip_id,
            paper_btk_num=self._row.paper_btk_num,
            row_key=self._row.row_key,
        )

    async def dispatch_event(self, _event: str) -> None:
        await self.click()

    async def get_attribute(self, name: str) -> str | None:
        if name == "onclick" and self._route == "openGamePaper":
            return self._row.onclick
        if name == "onclick" and self._route == "vote-link":
            return self._row.vote_link_onclick
        if name == "onclick" and self._route == "vote-button":
            return self._row.vote_button_onclick
        if name == "href" and self._route == "openGamePaper-href":
            return self._row.href_open_code
        return None


class _FakeRow:
    def __init__(
        self,
        page: "_FakePaperPage",
        slip_id: str,
        *,
        click_fails: bool = False,
        has_trigger: bool = True,
        has_href_trigger: bool = False,
        has_vote_link: bool = False,
        has_vote_button: bool = False,
        vote_link_click_fails: bool = False,
        vote_button_click_fails: bool = False,
        href_click_fails: bool = False,
        row_class: str = "",
        row_id: str = "",
        row_key: str = "",
        paper_btk_num: str | None = None,
        row_onclick: str | None = None,
        row_href: str = "",
        click_open_slip_id: str | None = None,
    ) -> None:
        self.page = page
        self.slip_id = slip_id
        self.click_fails = click_fails
        self.has_trigger = has_trigger
        self.has_href_trigger = has_href_trigger
        self.has_vote_link = has_vote_link
        self.has_vote_button = has_vote_button
        self.vote_link_click_fails = vote_link_click_fails
        self.vote_button_click_fails = vote_button_click_fails
        self.href_click_fails = href_click_fails
        self.row_class = row_class
        self.row_id = row_id
        self.row_key = row_key or slip_id
        self.paper_btk_num = paper_btk_num or slip_id
        self.onclick = (
            row_onclick
            if row_onclick is not None
            else (f"openGamePaper('{slip_id}');" if has_trigger else "")
        )
        self.vote_link_onclick = ""
        self.vote_button_onclick = ""
        self.href_open_code = f"javascript:openGamePaper('{slip_id}');"
        self.row_href = row_href
        self.click_open_slip_id = str(click_open_slip_id or "").strip()

    def locator(self, selector: str) -> object:
        if selector == "td":
            return _FakeCells(self.slip_id)
        if selector == 'a:has-text("투표지")':
            return _FakeTrigger(self, "vote-link", exists=self.has_vote_link)
        if selector == 'button:has-text("투표지")':
            return _FakeTrigger(self, "vote-button", exists=self.has_vote_button)
        if selector == 'a[href*="openGamePaper"]':
            return _FakeTrigger(self, "openGamePaper-href", exists=self.has_href_trigger)
        if "openGamePaper" in selector:
            return _FakeTrigger(self, "openGamePaper", exists=self.has_trigger)
        return _FakeTrigger(self, "unknown", exists=False)

    async def get_attribute(self, name: str) -> str | None:
        if name == "onclick":
            return self.onclick
        if name == "class":
            return self.row_class
        if name == "id":
            return self.row_id
        if name == "href":
            return self.row_href
        return None


class _FakeRows:
    def __init__(self, rows: list[_FakeRow]) -> None:
        self._rows = rows

    async def count(self) -> int:
        return len(self._rows)

    def nth(self, index: int) -> _FakeRow:
        return self._rows[index]


class _FakePaperArea:
    def __init__(self, page: "_FakePaperPage") -> None:
        self._page = page

    @property
    def first(self) -> "_FakePaperArea":
        return self

    async def count(self) -> int:
        return 1 if self._page.paper_exists else 0

    async def wait_for(self, **_kwargs: object) -> None:
        if self._page.active_slip_id in self._page.paper_invisible_for:
            raise RuntimeError("paper not visible")

    async def screenshot(self, **_kwargs: object) -> bytes:
        slip_id = self._page.active_slip_id or ""
        if slip_id in self._page.paper_capture_fail_for:
            raise RuntimeError("paper screenshot failed")
        return self._page.paper_bytes_by_slip.get(slip_id, b"default-paper")


class _FakeRequest:
    def __init__(self, url: str, post_data: str) -> None:
        self.url = url
        self.post_data = post_data


class _FakeResponse:
    def __init__(self, *, url: str, request: _FakeRequest, status: int, body: str) -> None:
        self.url = url
        self.request = request
        self.status = status
        self._body = body

    async def text(self) -> str:
        return self._body


class _FakePaperPage:
    def __init__(self, slip_ids: list[str]) -> None:
        self.rows = [_FakeRow(self, slip_id) for slip_id in slip_ids]
        self.paper_exists = True
        self.paper_bytes_by_slip: dict[str, bytes] = {slip_id: f"img-{slip_id}".encode("utf-8") for slip_id in slip_ids}
        self.paper_invisible_for: set[str] = set()
        self.paper_capture_fail_for: set[str] = set()
        self.paper_state_not_ready_always_for: set[str] = set()
        self.paper_state_unstable_first_open_for: set[str] = set()
        self.paper_state_signature_sequences: dict[str, list[str]] = {}
        self.paper_state_call_counts: dict[str, int] = {}
        self.active_slip_id: str | None = None
        self.active_paper_btk_num: str | None = None
        self.opened_slip_ids: list[str] = []
        self.opened_row_keys: list[str] = []
        self.evaluated_onclick: list[str] = []
        self.clicked_trigger_routes: list[str] = []
        self.response_timeout_for: set[str] = set()
        self.response_wait_calls: int = 0
        self.response_post_data_by_slip: dict[str, str] = {}
        self.response_body_by_slip: dict[str, str] = {}
        self.paper_state_ready_mode_by_slip: dict[str, str] = {}
        self.paper_state_record_rows_missing_markers_for: set[str] = set()

    def _open(self, slip_id: str, *, paper_btk_num: str | None = None, row_key: str = "") -> None:
        self.active_slip_id = slip_id
        self.active_paper_btk_num = paper_btk_num or slip_id
        self.opened_slip_ids.append(slip_id)
        self.opened_row_keys.append(row_key or slip_id)

    def locator(self, selector: str) -> object:
        if selector == "#purchaseWinTable tbody tr":
            return _FakeRows(self.rows)
        if selector == "table tbody tr":
            return _FakeRows([])
        if selector in {"#paperArea", "#paperTr #paperArea"}:
            return _FakePaperArea(self)
        return _FakeRows([])

    async def evaluate(self, _script: str, arg: object | None = None) -> object:
        script = str(_script or "")
        if "#paperArea" in script and arg is None:
            slip_id = str(self.active_slip_id or "")
            btk_num = str(self.active_paper_btk_num or slip_id)
            call_count = self.paper_state_call_counts.get(slip_id, 0) + 1
            self.paper_state_call_counts[slip_id] = call_count

            if slip_id in self.paper_state_not_ready_always_for:
                return {
                    "ready": False,
                    "readyMode": "none",
                    "rowCount": 0,
                    "btkNum": btk_num,
                    "signature": "",
                    "recordRowsFound": False,
                    "recordMarkersMissing": False,
                }

            if slip_id in self.paper_state_record_rows_missing_markers_for:
                return {
                    "ready": False,
                    "readyMode": "none",
                    "rowCount": 0,
                    "btkNum": btk_num,
                    "signature": "",
                    "recordRowsFound": True,
                    "recordMarkersMissing": True,
                }

            ready_mode = self.paper_state_ready_mode_by_slip.get(slip_id, "victory_rows")

            open_count = self.opened_slip_ids.count(slip_id)
            if slip_id in self.paper_state_unstable_first_open_for and open_count <= 1:
                signature = f"unstable-{call_count}"
                return {
                    "ready": True,
                    "readyMode": ready_mode,
                    "rowCount": 1,
                    "titleText": f"title-{slip_id}",
                    "totalText": f"total-{slip_id}",
                    "btkNum": btk_num,
                    "signature": signature,
                    "recordRowsFound": ready_mode == "record_rows",
                    "recordMarkersMissing": False,
                }

            sequence = self.paper_state_signature_sequences.get(slip_id)
            if sequence:
                idx = min(call_count - 1, len(sequence) - 1)
                signature = sequence[idx]
            else:
                signature = f"stable-{slip_id}"
            return {
                "ready": True,
                "readyMode": ready_mode,
                "rowCount": 2,
                "titleText": f"title-{slip_id}",
                "totalText": f"total-{slip_id}",
                "btkNum": btk_num,
                "signature": f"{ready_mode}:{signature}",
                "recordRowsFound": ready_mode == "record_rows",
                "recordMarkersMissing": False,
            }

        code = str(arg or "")
        self.evaluated_onclick.append(code)
        match = None
        if "openGamePaper" in code:
            start = code.find("openGamePaper(")
            if start >= 0:
                body = code[start + len("openGamePaper(") :]
                body = body.split(")", maxsplit=1)[0]
                body = body.strip().strip("'").strip('"')
                if body:
                    match = body
        if match:
            self._open(match)
            return True
        return False

    async def wait_for_function(self, *args: object, **_kwargs: object) -> None:
        return None

    async def wait_for_response(self, predicate: object, *, timeout: int = 30000) -> _FakeResponse:
        self.response_wait_calls += 1
        start = time.monotonic()
        timeout_sec = max(0.001, float(timeout) / 1000)

        while True:
            slip_id = str(self.active_slip_id or "")
            if slip_id:
                if slip_id in self.response_timeout_for:
                    break
                post_data = self.response_post_data_by_slip.get(slip_id, f"btkNum={slip_id}")
                body = self.response_body_by_slip.get(slip_id, f'{{"btkNum":"{slip_id}"}}')
                request = _FakeRequest(url="/mypgPurWin/getGameDetail.do", post_data=post_data)
                response = _FakeResponse(
                    url="/mypgPurWin/getGameDetail.do",
                    request=request,
                    status=200,
                    body=body,
                )
                accepted = bool(predicate(response)) if callable(predicate) else True
                if accepted:
                    return response
            if (time.monotonic() - start) >= timeout_sec:
                break
            await asyncio.sleep(0)

        raise PlaywrightTimeoutError("wait_for_response timed out")


async def test_capture_purchase_paper_area_snapshots_matches_targets_and_captures(monkeypatch) -> None:
    async def _noop_navigate(_page: _FakePaperPage) -> None:
        return None

    monkeypatch.setattr("src.purchases.navigate_to_purchase_history", _noop_navigate)
    page = _FakePaperPage(["S-1", "S-2", "S-3"])

    result = await capture_purchase_paper_area_snapshots(page, ["S-2", "S-1"], max_count=5)  # type: ignore[arg-type]

    assert [name for name, _ in result["files"]] == ["paper_S-2.png", "paper_S-1.png"]
    assert [data for _, data in result["files"]] == [b"img-S-2", b"img-S-1"]
    assert result["attempted_count"] == 2
    assert result["success_count"] == 2
    assert result["failed_count"] == 0
    assert result["exact_success_count"] == 2
    assert result["fallback_success_count"] == 0
    assert page.response_wait_calls >= 2


async def test_capture_purchase_paper_area_snapshots_uses_network_btk_priority(monkeypatch) -> None:
    async def _noop_navigate(_page: _FakePaperPage) -> None:
        return None

    monkeypatch.setattr("src.purchases.navigate_to_purchase_history", _noop_navigate)
    page = _FakePaperPage(["S-1"])
    page.response_post_data_by_slip["S-1"] = "btkNum=Z-1"

    result = await capture_purchase_paper_area_snapshots(page, ["S-1"], max_count=5)  # type: ignore[arg-type]

    assert result["files"] == []


async def test_capture_purchase_paper_area_snapshots_skips_detail_area_candidates(monkeypatch) -> None:
    async def _noop_navigate(_page: _FakePaperPage) -> None:
        return None

    monkeypatch.setattr("src.purchases.navigate_to_purchase_history", _noop_navigate)
    page = _FakePaperPage(["S-1"])
    page.rows = [
        _FakeRow(
            page,
            "S-1",
            row_class="detailArea active",
            row_id="paperTr",
            row_key="detail-row",
            has_trigger=True,
        ),
        _FakeRow(page, "S-1", row_key="normal-row", has_trigger=True),
    ]

    result = await capture_purchase_paper_area_snapshots(page, ["S-1"], max_count=5)  # type: ignore[arg-type]

    assert result["files"] == [("paper_S-1.png", b"img-S-1")]
    assert page.opened_row_keys == ["normal-row"]


async def test_capture_purchase_paper_area_snapshots_uses_target_row_only(monkeypatch) -> None:
    async def _noop_navigate(_page: _FakePaperPage) -> None:
        return None

    monkeypatch.setattr("src.purchases.navigate_to_purchase_history", _noop_navigate)
    page = _FakePaperPage(["S-1"])
    page.rows = [
        _FakeRow(page, "X-9", row_key="other-row", has_trigger=True),
        _FakeRow(page, "S-1", row_key="match-row", paper_btk_num="S-1", has_trigger=True),
    ]

    result = await capture_purchase_paper_area_snapshots(page, ["S-1"], max_count=5)  # type: ignore[arg-type]

    assert result["files"] == [("paper_S-1.png", b"img-S-1")]
    assert page.opened_row_keys == ["match-row"]


async def test_capture_purchase_paper_area_snapshots_maps_row_by_open_game_paper_argument(monkeypatch) -> None:
    async def _noop_navigate(_page: _FakePaperPage) -> None:
        return None

    monkeypatch.setattr("src.purchases.navigate_to_purchase_history", _noop_navigate)
    page = _FakePaperPage(["BAD"])
    page.rows = [
        _FakeRow(
            page,
            "BAD-TEXT-SLIP",
            row_key="mapped-by-onclick",
            row_onclick="openGamePaper(this, 'S-777');",
            paper_btk_num="S-777",
            has_trigger=True,
        )
    ]

    result = await capture_purchase_paper_area_snapshots(page, ["S-777"], max_count=5)  # type: ignore[arg-type]

    assert result["files"] == [("paper_S-777.png", b"default-paper")]
    assert page.opened_row_keys == ["mapped-by-onclick"]


async def test_capture_purchase_paper_area_snapshots_supports_record_ready_mode(monkeypatch) -> None:
    async def _noop_navigate(_page: _FakePaperPage) -> None:
        return None

    monkeypatch.setattr("src.purchases.navigate_to_purchase_history", _noop_navigate)
    page = _FakePaperPage(["R-1"])
    page.paper_state_ready_mode_by_slip["R-1"] = "record_rows"

    result = await capture_purchase_paper_area_snapshots(page, ["R-1"], max_count=5)  # type: ignore[arg-type]

    assert result["files"] == [("paper_R-1.png", b"img-R-1")]


async def test_capture_purchase_paper_area_snapshots_skips_record_when_markers_missing(monkeypatch, caplog) -> None:
    async def _noop_navigate(_page: _FakePaperPage) -> None:
        return None

    monkeypatch.setattr("src.purchases.navigate_to_purchase_history", _noop_navigate)
    page = _FakePaperPage(["R-1"])
    page.paper_state_record_rows_missing_markers_for.add("R-1")
    caplog.set_level("WARNING")

    result = await capture_purchase_paper_area_snapshots(page, ["R-1"], max_count=5)  # type: ignore[arg-type]

    assert result["files"] == []
    assert "reason=record_not_ready" in caplog.text


async def test_capture_purchase_paper_area_snapshots_skips_when_target_row_not_found_exact(monkeypatch) -> None:
    async def _noop_navigate(_page: _FakePaperPage) -> None:
        return None

    monkeypatch.setattr("src.purchases.navigate_to_purchase_history", _noop_navigate)
    page = _FakePaperPage(["S-1"])

    result = await capture_purchase_paper_area_snapshots(page, ["S-999"], max_count=5)  # type: ignore[arg-type]

    assert result["files"] == []
    assert page.opened_row_keys == []


async def test_capture_purchase_paper_area_snapshots_uses_order_fallback_when_exact_not_found(monkeypatch) -> None:
    async def _noop_navigate(_page: _FakePaperPage) -> None:
        return None

    monkeypatch.setattr("src.purchases.navigate_to_purchase_history", _noop_navigate)
    page = _FakePaperPage(["X-1", "X-2"])
    page.rows = [
        _FakeRow(page, "X-1", row_key="fallback-1", has_trigger=False, has_vote_link=True, click_open_slip_id="S-1"),
        _FakeRow(page, "X-2", row_key="fallback-2", has_trigger=False, has_vote_link=True, click_open_slip_id="S-2"),
    ]

    result = await capture_purchase_paper_area_snapshots(page, ["S-1", "S-2"], max_count=5)  # type: ignore[arg-type]

    assert [name for name, _ in result["files"]] == ["paper_S-1.png", "paper_S-2.png"]
    assert page.opened_row_keys == ["fallback-1", "fallback-2"]
    assert result["exact_success_count"] == 0
    assert result["fallback_success_count"] == 2


async def test_capture_purchase_paper_area_snapshots_fallback_exhausted(monkeypatch) -> None:
    async def _noop_navigate(_page: _FakePaperPage) -> None:
        return None

    monkeypatch.setattr("src.purchases.navigate_to_purchase_history", _noop_navigate)
    page = _FakePaperPage(["X-1"])
    page.rows = [_FakeRow(page, "X-1", row_key="fallback-1", has_trigger=False, has_vote_link=True, click_open_slip_id="S-1")]

    result = await capture_purchase_paper_area_snapshots(page, ["S-1", "S-2"], max_count=5)  # type: ignore[arg-type]

    assert [name for name, _ in result["files"]] == ["paper_S-1.png"]
    assert result["attempted_count"] == 2
    assert result["success_count"] == 1
    assert result["failed_count"] == 1


async def test_capture_purchase_paper_area_snapshots_falls_back_to_onclick_eval(monkeypatch) -> None:
    async def _noop_navigate(_page: _FakePaperPage) -> None:
        return None

    monkeypatch.setattr("src.purchases.navigate_to_purchase_history", _noop_navigate)
    page = _FakePaperPage(["S-1"])
    page.rows[0].click_fails = True

    result = await capture_purchase_paper_area_snapshots(page, ["S-1"], max_count=5)  # type: ignore[arg-type]

    assert result["files"]
    assert page.evaluated_onclick
    assert "openGamePaper('S-1')" in page.evaluated_onclick[0]


async def test_capture_purchase_paper_area_snapshots_prefers_vote_link_trigger(monkeypatch) -> None:
    async def _noop_navigate(_page: _FakePaperPage) -> None:
        return None

    monkeypatch.setattr("src.purchases.navigate_to_purchase_history", _noop_navigate)
    page = _FakePaperPage(["S-1"])
    page.rows[0].has_vote_link = True

    result = await capture_purchase_paper_area_snapshots(page, ["S-1"], max_count=5)  # type: ignore[arg-type]

    assert result["files"] == [("paper_S-1.png", b"img-S-1")]
    assert page.clicked_trigger_routes == ["vote-link"]
    assert page.opened_slip_ids == ["S-1"]


async def test_capture_purchase_paper_area_snapshots_prefers_vote_button_trigger(monkeypatch) -> None:
    async def _noop_navigate(_page: _FakePaperPage) -> None:
        return None

    monkeypatch.setattr("src.purchases.navigate_to_purchase_history", _noop_navigate)
    page = _FakePaperPage(["S-1"])
    page.rows[0].has_vote_button = True

    result = await capture_purchase_paper_area_snapshots(page, ["S-1"], max_count=5)  # type: ignore[arg-type]

    assert result["files"] == [("paper_S-1.png", b"img-S-1")]
    assert page.clicked_trigger_routes == ["vote-button"]
    assert page.opened_slip_ids == ["S-1"]


async def test_capture_purchase_paper_area_snapshots_falls_back_to_open_game_paper_when_vote_ui_fails(monkeypatch) -> None:
    async def _noop_navigate(_page: _FakePaperPage) -> None:
        return None

    monkeypatch.setattr("src.purchases.navigate_to_purchase_history", _noop_navigate)
    page = _FakePaperPage(["S-1"])
    page.rows[0].has_vote_link = True
    page.rows[0].vote_link_click_fails = True

    result = await capture_purchase_paper_area_snapshots(page, ["S-1"], max_count=5)  # type: ignore[arg-type]

    assert result["files"] == [("paper_S-1.png", b"img-S-1")]
    assert page.clicked_trigger_routes.count("vote-link") >= 1
    assert page.clicked_trigger_routes[-1] == "openGamePaper"
    assert page.opened_slip_ids == ["S-1"]


async def test_capture_purchase_paper_area_snapshots_uses_href_open_game_paper_trigger(monkeypatch) -> None:
    async def _noop_navigate(_page: _FakePaperPage) -> None:
        return None

    monkeypatch.setattr("src.purchases.navigate_to_purchase_history", _noop_navigate)
    page = _FakePaperPage(["S-1"])
    page.rows[0].has_trigger = False
    page.rows[0].has_href_trigger = True

    result = await capture_purchase_paper_area_snapshots(page, ["S-1"], max_count=5)  # type: ignore[arg-type]

    assert result["files"] == [("paper_S-1.png", b"img-S-1")]
    assert page.clicked_trigger_routes == ["openGamePaper-href"]
    assert page.opened_slip_ids == ["S-1"]


async def test_capture_purchase_paper_area_snapshots_skips_when_paper_not_visible(monkeypatch) -> None:
    async def _noop_navigate(_page: _FakePaperPage) -> None:
        return None

    monkeypatch.setattr("src.purchases.navigate_to_purchase_history", _noop_navigate)
    page = _FakePaperPage(["S-1"])
    page.paper_invisible_for.add("S-1")

    result = await capture_purchase_paper_area_snapshots(page, ["S-1"], max_count=5)  # type: ignore[arg-type]

    assert result["files"] == []


async def test_capture_purchase_paper_area_snapshots_retries_when_detail_not_ready_once(monkeypatch) -> None:
    async def _noop_navigate(_page: _FakePaperPage) -> None:
        return None

    monkeypatch.setattr("src.purchases.navigate_to_purchase_history", _noop_navigate)
    page = _FakePaperPage(["S-1"])
    page.paper_state_unstable_first_open_for.add("S-1")

    result = await capture_purchase_paper_area_snapshots(page, ["S-1"], max_count=5)  # type: ignore[arg-type]

    assert result["files"] == [("paper_S-1.png", b"img-S-1")]
    assert page.opened_slip_ids == ["S-1", "S-1"]
    assert page.paper_state_call_counts["S-1"] >= 3


async def test_capture_purchase_paper_area_snapshots_skips_when_detail_not_ready_after_retry(monkeypatch) -> None:
    async def _noop_navigate(_page: _FakePaperPage) -> None:
        return None

    monkeypatch.setattr("src.purchases.navigate_to_purchase_history", _noop_navigate)
    page = _FakePaperPage(["S-1"])
    page.paper_state_not_ready_always_for.add("S-1")

    result = await capture_purchase_paper_area_snapshots(page, ["S-1"], max_count=5)  # type: ignore[arg-type]

    assert result["files"] == []
    assert page.opened_slip_ids == ["S-1", "S-1"]
    assert page.paper_state_call_counts["S-1"] >= 2


async def test_capture_purchase_paper_area_snapshots_waits_until_signature_stabilizes(monkeypatch) -> None:
    async def _noop_navigate(_page: _FakePaperPage) -> None:
        return None

    monkeypatch.setattr("src.purchases.navigate_to_purchase_history", _noop_navigate)
    page = _FakePaperPage(["S-1"])
    page.paper_state_signature_sequences["S-1"] = ["A", "B", "B"]

    result = await capture_purchase_paper_area_snapshots(page, ["S-1"], max_count=5)  # type: ignore[arg-type]

    assert result["files"] == [("paper_S-1.png", b"img-S-1")]
    assert page.opened_slip_ids == ["S-1"]
    assert page.paper_state_call_counts["S-1"] >= 3


async def test_capture_purchase_paper_area_snapshots_respects_max_count(monkeypatch) -> None:
    async def _noop_navigate(_page: _FakePaperPage) -> None:
        return None

    monkeypatch.setattr("src.purchases.navigate_to_purchase_history", _noop_navigate)
    slip_ids = [f"S-{i}" for i in range(1, 7)]
    page = _FakePaperPage(slip_ids)

    result = await capture_purchase_paper_area_snapshots(page, slip_ids, max_count=5)  # type: ignore[arg-type]

    assert len(result["files"]) == 5
    assert [name for name, _ in result["files"]] == [f"paper_S-{i}.png" for i in range(1, 6)]


async def test_capture_purchase_paper_area_snapshots_without_max_count_captures_all(monkeypatch) -> None:
    async def _noop_navigate(_page: _FakePaperPage) -> None:
        return None

    monkeypatch.setattr("src.purchases.navigate_to_purchase_history", _noop_navigate)
    slip_ids = [f"S-{i}" for i in range(1, 7)]
    page = _FakePaperPage(slip_ids)

    result = await capture_purchase_paper_area_snapshots(page, slip_ids, max_count=None)  # type: ignore[arg-type]

    assert len(result["files"]) == len(slip_ids)
    assert [name for name, _ in result["files"]] == [f"paper_S-{i}.png" for i in range(1, 7)]
