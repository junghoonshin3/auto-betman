from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.analysis import (
    _build_analysis_token,
    _extract_amounts_from_text,
    _is_execution_context_destroyed_error,
    _month_range_ym,
    _parse_purchase_analysis_payload,
    _to_int_amount,
    _to_int_amount_or_zero,
)


def test_to_int_amount_parses_comma_currency() -> None:
    assert _to_int_amount("1,234원") == 1234
    assert _to_int_amount("구매금액: 55,000") == 55000


def test_extract_amounts_from_same_node_text() -> None:
    text = "최근 12개월 구매금액 120,000원 / 적중금액 95,500원"
    purchase, winning = _extract_amounts_from_text(text)
    assert purchase == 120000
    assert winning == 95500


def test_extract_amounts_from_label_value_split_lines() -> None:
    text = """
    구매금액
    10,000원
    적중금액
    0원
    """
    purchase, winning = _extract_amounts_from_text(text)
    assert purchase == 10000
    assert winning == 0


def test_extract_amounts_with_label_variants() -> None:
    text = "총구매금액: 77,000원 | 환급금액: 12,300원"
    purchase, winning = _extract_amounts_from_text(text)
    assert purchase == 77000
    assert winning == 12300


def test_extract_amounts_returns_none_when_missing() -> None:
    purchase, winning = _extract_amounts_from_text("분석 데이터가 없습니다.")
    assert purchase is None
    assert winning is None


def test_context_destroyed_error_detection() -> None:
    assert _is_execution_context_destroyed_error(Exception("Page.evaluate: Execution context was destroyed"))
    assert not _is_execution_context_destroyed_error(Exception("Some other playwright error"))


def test_month_range_ym_for_1_month() -> None:
    kst = timezone(timedelta(hours=9))
    now = datetime(2026, 2, 13, 10, 0, 0, tzinfo=kst)
    assert _month_range_ym(now, 1) == ("2026", "01", "2026", "02")


def test_month_range_ym_for_12_months() -> None:
    kst = timezone(timedelta(hours=9))
    now = datetime(2026, 2, 13, 10, 0, 0, tzinfo=kst)
    assert _month_range_ym(now, 12) == ("2025", "02", "2026", "02")


def test_to_int_amount_or_zero() -> None:
    assert _to_int_amount_or_zero("1,234") == 1234
    assert _to_int_amount_or_zero("0") == 0
    assert _to_int_amount_or_zero(None) == 0
    assert _to_int_amount_or_zero("not-a-number") == 0


def test_parse_purchase_analysis_payload_purchase_info_priority() -> None:
    payload = {
        "purchaseInfo": {
            "buyAmt": "2,229,600",
            "winAmt": "1,557,140",
        },
        "readGameBuyRateHitAmount": [
            {"buyAmt": "111", "winAmt": "222"}
        ],
    }
    purchase, winning = _parse_purchase_analysis_payload(payload)
    assert purchase == 2229600
    assert winning == 1557140


def test_parse_purchase_analysis_payload_fallback_to_list_item() -> None:
    payload = {
        "purchaseInfo": {},
        "readGameBuyRateHitAmount": [
            {"buyAmt": "977,500", "winAmt": "467,500"}
        ],
    }
    purchase, winning = _parse_purchase_analysis_payload(payload)
    assert purchase == 977500
    assert winning == 467500


def test_parse_purchase_analysis_payload_missing_returns_none() -> None:
    purchase, winning = _parse_purchase_analysis_payload({"foo": "bar"})
    assert purchase is None
    assert winning is None


def test_build_analysis_token_includes_months_and_amounts() -> None:
    assert _build_analysis_token(1, 10000, 5000) == "1:10000:5000"
    assert _build_analysis_token(12, 0, 0) == "12:0:0"
