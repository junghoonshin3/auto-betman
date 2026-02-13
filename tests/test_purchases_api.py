from __future__ import annotations

from src.purchases import (
    _build_recent_purchases_token_from_items,
    _extract_purchase_items,
    _next_start_row,
    _recent5_range_ymd,
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
