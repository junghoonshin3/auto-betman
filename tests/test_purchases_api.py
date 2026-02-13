from __future__ import annotations

from datetime import datetime

from src.purchases import (
    KST,
    _extract_purchase_items,
    _month_range_ymd,
    _next_start_row,
    _status_result_from_buy_status_info,
    _status_result_from_list_item,
)


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


def test_month_range_ymd_kst() -> None:
    now = datetime(2026, 2, 13, 10, 0, tzinfo=KST)
    start, end = _month_range_ymd(now)
    assert start == "20260113"
    assert end == "20260213"


def test_next_start_row_pagination() -> None:
    assert _next_start_row(1, 30) == 31
    assert _next_start_row(31, 5) == 36
