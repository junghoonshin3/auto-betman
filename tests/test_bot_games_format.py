from __future__ import annotations

from src.bot import _build_games_message, _build_games_summary_embed
from src.models import SaleGameMatch, SaleGamesSnapshot


def _sample_match(idx: int) -> SaleGameMatch:
    return SaleGameMatch(
        gm_id="G101",
        gm_ts="260019",
        game_type="승부식",
        sport="축구",
        match_name=f"HOME{idx} vs AWAY{idx}",
        round_label="19회차",
        match_seq=idx,
        home_team=f"HOME{idx}",
        away_team=f"AWAY{idx}",
        start_at="02.13 16:00",
        start_epoch_ms=1760000000000 + idx,
        sale_end_at="02.13 18:00",
        sale_end_epoch_ms=1760000000000 + idx,
        status="발매중",
    )


def test_games_summary_embed_contains_core_sections() -> None:
    snapshot = SaleGamesSnapshot(
        fetched_at="2026.02.13 19:00:00",
        total_games=5,
        total_matches=42,
        sport_counts={"축구": 20, "농구": 22},
        nearest_matches=[_sample_match(1), _sample_match(2)],
        partial_failures=1,
    )
    embed = _build_games_summary_embed(snapshot, "승무패", "전체")

    fields = {field.name: field.value for field in embed.fields}
    assert fields["조회 타입"] == "승무패"
    assert fields["조회 종목"] == "전체"
    assert fields["수집시각"] == "2026.02.13 19:00:00"
    assert fields["전체 게임/전체 경기"] == "5 / 42"
    assert "축구: 20" in fields["종목별 경기수"]
    assert "부분 실패" in fields
    assert embed.description in (None, "")


def test_games_summary_embed_hides_partial_failures_when_zero() -> None:
    snapshot = SaleGamesSnapshot(
        fetched_at="2026.02.13 19:00:00",
        total_games=1,
        total_matches=1,
        sport_counts={"축구": 1},
        nearest_matches=[_sample_match(1)],
        partial_failures=0,
    )
    embed = _build_games_summary_embed(snapshot, "승부식", "축구")
    fields = {field.name: field.value for field in embed.fields}
    assert fields["조회 타입"] == "승부식"
    assert fields["조회 종목"] == "축구"
    assert all(field.name != "부분 실패" for field in embed.fields)


def test_games_message_uses_only_required_fields() -> None:
    snapshot = SaleGamesSnapshot(
        fetched_at="2026.02.13 19:00:00",
        total_games=1,
        total_matches=1,
        sport_counts={"축구": 1},
        nearest_matches=[_sample_match(1)],
        partial_failures=0,
    )
    embed, file_obj = _build_games_message(snapshot, "전체", "전체")
    description = embed.description or ""
    assert "[축구]" in description
    assert "HOME1 vs AWAY1" in description
    assert "유형 승부식" in description
    assert "19회차" in description
    assert "시작 02.13 16:00" in description
    assert "마감 02.13 18:00" in description
    assert "핸디캡" not in description
    assert "언더오버" not in description
    assert file_obj is None


def test_games_message_attaches_txt_when_too_long() -> None:
    matches = [_sample_match(i) for i in range(1, 400)]
    snapshot = SaleGamesSnapshot(
        fetched_at="2026.02.13 19:00:00",
        total_games=10,
        total_matches=len(matches),
        sport_counts={"축구": len(matches)},
        nearest_matches=matches,
        partial_failures=0,
    )
    embed, file_obj = _build_games_message(snapshot, "기록식", "농구")
    assert file_obj is not None
    assert "첨부파일" in (embed.description or "")
    assert file_obj.filename.startswith("games_")
