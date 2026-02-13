from __future__ import annotations

from src.bot import _build_slip_embed, _build_summary_embed
from src.models import BetSlip, MatchBet


def _sample_slip(result: str | None = "적중") -> BetSlip:
    return BetSlip(
        slip_id="A1B2-C3D4-E5F6-0001",
        game_type="프로토 승부식",
        round_number="19회차",
        status="적중" if result == "적중" else "적중안됨" if result == "미적중" else "발매마감",
        purchase_datetime="2026.02.13 10:30",
        total_amount=5000,
        potential_payout=12000,
        combined_odds=2.40,
        result=result,
        actual_payout=12000 if result == "적중" else 0,
        matches=[
            MatchBet(
                match_number=1,
                sport="축구",
                league="K리그1",
                home_team="전북",
                away_team="울산",
                bet_selection="승",
                odds=2.10,
                match_datetime="2026.02.14 19:00",
                result="적중" if result == "적중" else None,
                score="2:1",
                game_result="승",
            )
        ],
    )


def test_summary_embed_fields_and_values() -> None:
    slips = [_sample_slip("적중"), _sample_slip("미적중")]
    embed = _build_summary_embed(slips, "최근 1개월(최대 30개)")

    assert "최근 1개월" in embed.title
    fields = {f.name: f.value for f in embed.fields}
    assert fields["조회 건수"] == "2건"
    assert fields["총 구매금액"] == "10,000원"
    assert fields["총 실제적중금"] == "12,000원"


def test_slip_embed_contains_required_detail_lines() -> None:
    slip = _sample_slip("적중")
    embed = _build_slip_embed(1, slip)
    assert "A1B2-C3D4-E5F6-0001" in embed.title

    combined_values = "\n".join(field.value for field in embed.fields)
    assert "내 선택:" in combined_values
    assert "실제 결과:" in combined_values
    assert "내 베팅 결과:" in combined_values


def test_slip_embed_shows_pending_when_match_result_not_explicit() -> None:
    slip = _sample_slip(None)
    slip.matches[0].result = None
    embed = _build_slip_embed(1, slip)

    combined_values = "\n".join(field.value for field in embed.fields)
    assert "대기" in combined_values
