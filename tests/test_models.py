from __future__ import annotations

from src.models import BetSlip, MatchBet


class TestMatchBet:
    def test_creation(self):
        m = MatchBet(
            match_number=3,
            sport="야구",
            league="KBO",
            home_team="LG",
            away_team="두산",
            bet_selection="원정승",
            odds=1.85,
            match_datetime="2025-04-01 18:30",
        )
        assert m.match_number == 3
        assert m.sport == "야구"


class TestBetSlip:
    def test_title(self, sample_bet_slip: BetSlip):
        assert sample_bet_slip.title == "프로토 승부식 제100회"

    def test_creation_defaults(self):
        slip = BetSlip(
            slip_id="1",
            game_type="토토",
            round_number="1회",
            status="발매중",
            purchase_datetime="",
            total_amount=1000,
            potential_payout=2000,
            combined_odds=2.0,
        )
        assert slip.matches == []
        assert slip.title == "토토 1회"
