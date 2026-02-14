from __future__ import annotations

from src.bot import _build_compact_purchase_embeds, _build_slip_embed, _build_summary_embed
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
    embed = _build_summary_embed(slips, "최근 5개")

    assert "최근 5개" in embed.title
    fields = {f.name: f.value for f in embed.fields}
    assert fields["조회 건수"] == "2건"
    assert fields["총 구매금액"] == "10,000원"
    assert fields["총 실제적중금"] == "12,000원"
    assert fields["게임유형별 건수"] == "승부식: 2건"
    assert "총 예상적중금" not in fields


def test_compact_embed_contains_all_matches() -> None:
    slip = _sample_slip("적중")
    slip.matches.append(
        MatchBet(
            match_number=2,
            sport="농구",
            league="KBL",
            home_team="A",
            away_team="B",
            bet_selection="패",
            odds=1.87,
            match_datetime="2026.02.14 20:00",
            result="미적중",
            score="80:75",
            game_result="승",
        )
    )

    embeds = _build_compact_purchase_embeds([slip])
    assert len(embeds) >= 2

    details = "\n".join((e.description or "") for e in embeds[1:])
    assert "=== 승부식 ===" in details
    assert "[1] 🏆 `A1B2-C3D4-E5F6-0001` · 적중 (결과: 적중)" in details
    assert "구매시각 2026.02.13 10:30 · 구매 5,000원 · 배당 2.40" in details
    assert "1. 🎯 **전북** vs 울산 | 선택 승(2.10) | 실제 승 | 2:1 | 내결과 적중" in details
    assert "2. A vs 🎯 **B** | 선택 패(1.87) | 실제 승 | 80:75 | 내결과 미적중" in details


def test_compact_embed_hides_match_result_when_not_explicit() -> None:
    slip = _sample_slip(None)
    slip.matches[0].result = None
    slip.matches[0].score = ""
    slip.matches[0].game_result = ""
    embeds = _build_compact_purchase_embeds([slip])

    details = "\n".join((e.description or "") for e in embeds[1:])
    assert "1. 🎯 **전북** vs 울산 | 선택 승(2.10) | 실제 대기" in details
    assert "내결과:" not in details
    assert "내결과 " not in details


def test_slip_embed_hides_pending_match_result_line() -> None:
    slip = _sample_slip(None)
    slip.matches[0].result = None
    slip.matches[0].score = ""
    slip.matches[0].game_result = ""

    embed = _build_slip_embed(1, slip)
    values = "\n".join(field.value for field in embed.fields)
    assert "🎯 **전북** vs 울산" in values
    assert "실제 결과: 대기" in values
    assert "내 베팅 결과:" not in values


def test_compact_embed_chunks_when_too_long() -> None:
    slips: list[BetSlip] = []
    for i in range(1, 6):
        matches: list[MatchBet] = []
        for j in range(1, 40):
            matches.append(
                MatchBet(
                    match_number=j,
                    sport="축구",
                    league="리그",
                    home_team=f"홈{j}",
                    away_team=f"원정{j}",
                    bet_selection="승",
                    odds=2.10,
                    match_datetime="2026.02.14 19:00",
                    result="적중",
                    score="2:1",
                    game_result="승",
                )
            )

        slips.append(
            BetSlip(
                slip_id=f"S-{i:04d}",
                game_type="프로토",
                round_number="19회차",
                status="적중",
                purchase_datetime="2026.02.13 10:30",
                total_amount=5000,
                potential_payout=12000,
                combined_odds=2.40,
                result="적중",
                actual_payout=12000,
                matches=matches,
            )
        )

    embeds = _build_compact_purchase_embeds(slips)
    assert len(embeds) > 2


def test_compact_embed_uses_custom_mode_label() -> None:
    embeds = _build_compact_purchase_embeds([_sample_slip("적중")], mode_label="신규 구매")
    assert embeds
    assert embeds[0].title == "구매내역 조회 결과 (신규 구매)"


def test_compact_embed_groups_by_standard_game_type_order() -> None:
    slip_record = _sample_slip("적중")
    slip_record.slip_id = "REC-1"
    slip_record.game_type = "기록식"

    slip_victory_1 = _sample_slip("적중")
    slip_victory_1.slip_id = "VIC-1"
    slip_victory_1.game_type = "프로토 승부식"

    slip_other = _sample_slip("적중")
    slip_other.slip_id = "OTH-1"
    slip_other.game_type = "특수타입"

    slip_victory_2 = _sample_slip("적중")
    slip_victory_2.slip_id = "VIC-2"
    slip_victory_2.game_type = "승부식"

    slip_windrawlose = _sample_slip("적중")
    slip_windrawlose.slip_id = "WDL-1"
    slip_windrawlose.game_type = "승무패"

    embeds = _build_compact_purchase_embeds(
        [slip_record, slip_victory_1, slip_other, slip_victory_2, slip_windrawlose]
    )
    fields = {f.name: f.value for f in embeds[0].fields}
    assert fields["게임유형별 건수"] == "승부식: 2건\n승무패: 1건\n기록식: 1건\n기타: 1건"

    details = "\n".join((e.description or "") for e in embeds[1:])
    victory_pos = details.index("=== 승부식 ===")
    windrawlose_pos = details.index("=== 승무패 ===")
    record_pos = details.index("=== 기록식 ===")
    other_pos = details.index("=== 기타 ===")
    assert victory_pos < windrawlose_pos < record_pos < other_pos
    assert details.index("`VIC-1`") < details.index("`VIC-2`")


def test_compact_embed_draw_pick_marks_draw_without_team_highlight() -> None:
    slip = _sample_slip("적중")
    slip.matches[0].bet_selection = "무"

    embeds = _build_compact_purchase_embeds([slip])
    details = "\n".join((e.description or "") for e in embeds[1:])
    assert "1. 전북 vs 울산 (🎯 **무승부 픽**)" in details
    assert "🎯 **전북**" not in details
    assert "🎯 **울산**" not in details


def test_compact_embed_unknown_selection_does_not_highlight_team() -> None:
    slip = _sample_slip("적중")
    slip.matches[0].bet_selection = "오버"

    embeds = _build_compact_purchase_embeds([slip])
    details = "\n".join((e.description or "") for e in embeds[1:])
    assert "1. 전북 vs 울산 | 선택 오버(2.10) | 실제 승 | 2:1 | 내결과 적중" in details
    assert "🎯 **전북**" not in details
    assert "🎯 **울산**" not in details
