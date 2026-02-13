from __future__ import annotations

from src.bot import _build_games_summary_embed
from src.models import SaleGameMatch, SaleGamesSnapshot


def _sample_match(idx: int) -> SaleGameMatch:
    return SaleGameMatch(
        gm_id="G101",
        gm_ts="260019",
        game_name="승부식 19회차",
        sport="축구",
        league="EPL",
        match_seq=idx,
        home_team=f"HOME{idx}",
        away_team=f"AWAY{idx}",
        bet_type="일반",
        odds_home=1.8,
        odds_draw=3.0,
        odds_away=4.1,
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
    embed = _build_games_summary_embed(snapshot)

    fields = {field.name: field.value for field in embed.fields}
    assert fields["수집시각"] == "2026.02.13 19:00:00"
    assert fields["전체 게임/전체 경기"] == "5 / 42"
    assert "축구: 20" in fields["종목별 경기수"]
    assert "부분 실패" in fields
    assert "마감 임박 2경기" in (embed.description or "")


def test_games_summary_embed_hides_partial_failures_when_zero() -> None:
    snapshot = SaleGamesSnapshot(
        fetched_at="2026.02.13 19:00:00",
        total_games=1,
        total_matches=1,
        sport_counts={"축구": 1},
        nearest_matches=[_sample_match(1)],
        partial_failures=0,
    )
    embed = _build_games_summary_embed(snapshot)
    assert all(field.name != "부분 실패" for field in embed.fields)
