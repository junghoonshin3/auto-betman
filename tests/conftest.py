from __future__ import annotations

import pytest

from src.models import BetSlip, MatchBet


@pytest.fixture
def sample_match_bet() -> MatchBet:
    return MatchBet(
        match_number=1,
        sport="축구",
        league="K리그1",
        home_team="전북현대",
        away_team="울산현대",
        bet_selection="홈승",
        odds=2.10,
        match_datetime="2025-03-15 19:00",
    )


@pytest.fixture
def sample_bet_slip(sample_match_bet: MatchBet) -> BetSlip:
    return BetSlip(
        slip_id="20250315001",
        game_type="프로토 승부식",
        round_number="제100회",
        status="발매중",
        purchase_datetime="2025-03-15 10:00",
        total_amount=5000,
        potential_payout=10500,
        combined_odds=2.10,
        matches=[sample_match_bet],
    )


@pytest.fixture
def sample_xhr_response() -> dict:
    return {
        "list": [
            {
                "buyNo": "20250315001",
                "gameNm": "프로토 승부식",
                "roundNo": "100",
                "statusNm": "발매중",
                "buyDt": "2025-03-15 10:00",
                "buyAmt": 5000,
                "expectAmt": 10500,
                "totOdds": 2.10,
                "detailList": [
                    {
                        "matchNo": 1,
                        "sportNm": "축구",
                        "leagueNm": "K리그1",
                        "homeTeamNm": "전북현대",
                        "awayTeamNm": "울산현대",
                        "selectNm": "홈승",
                        "odds": 2.10,
                        "gameDt": "2025-03-15 19:00",
                    }
                ],
            }
        ]
    }


@pytest.fixture
def mock_config(tmp_path):
    """Create a Config with temporary paths."""
    from src.config import Config

    return Config(
        discord_bot_token="fake-token",
        discord_channel_id=123456789,
        betman_user_id="testuser",
        betman_user_pw="testpass",
        session_state_path=tmp_path / "session.json",
        last_notified_path=tmp_path / "notified.json",
        db_path=tmp_path / "test.db",
    )


@pytest.fixture
async def db(tmp_path):
    """Create an initialized Database for testing."""
    from src.database import Database

    database = Database(tmp_path / "test.db")
    await database.init()
    yield database
    await database.close()


DISCORD_USER_A = "111111111111111111"
DISCORD_USER_B = "222222222222222222"
