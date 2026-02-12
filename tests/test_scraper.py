from __future__ import annotations

from src.scraper import BetmanScraper


class TestExtractSlipsFromJson:
    def _make_scraper(self) -> BetmanScraper:
        # Config is not used in the extract methods directly, pass a minimal mock
        from unittest.mock import MagicMock
        return BetmanScraper(MagicMock())

    def test_extract_from_list_key(self, sample_xhr_response: dict):
        scraper = self._make_scraper()
        slips = scraper._extract_slips_from_json(sample_xhr_response)
        assert len(slips) == 1
        assert slips[0].slip_id == "20250315001"
        assert slips[0].game_type == "프로토 승부식"
        assert len(slips[0].matches) == 1

    def test_extract_from_data_key(self):
        scraper = self._make_scraper()
        data = {
            "data": [
                {
                    "buyNo": "999",
                    "statusNm": "적중",
                    "gameNm": "토토",
                    "roundNo": "50",
                    "buyDt": "2025-01-01",
                    "buyAmt": 3000,
                    "expectAmt": 6000,
                    "totOdds": 2.0,
                }
            ]
        }
        slips = scraper._extract_slips_from_json(data)
        assert len(slips) == 1
        assert slips[0].slip_id == "999"

    def test_extract_from_body_nested(self):
        scraper = self._make_scraper()
        data = {
            "body": {
                "result": [
                    {
                        "buyNo": "555",
                        "statusNm": "발매중",
                        "gameNm": "프로토",
                        "roundNo": "10",
                        "buyDt": "2025-02-01",
                        "buyAmt": 1000,
                        "expectAmt": 2000,
                        "totOdds": 2.0,
                    }
                ]
            }
        }
        slips = scraper._extract_slips_from_json(data)
        assert len(slips) == 1
        assert slips[0].slip_id == "555"

    def test_extract_from_raw_list(self):
        scraper = self._make_scraper()
        data = [
            {
                "buyNo": "111",
                "statusNm": "발매마감",
                "gameNm": "승부식",
                "roundNo": "1",
                "buyDt": "2025-01-15",
                "buyAmt": 2000,
                "expectAmt": 4000,
                "totOdds": 2.0,
            }
        ]
        slips = scraper._extract_slips_from_json(data)
        assert len(slips) == 1

    def test_extract_empty_data(self):
        scraper = self._make_scraper()
        assert scraper._extract_slips_from_json({}) == []
        assert scraper._extract_slips_from_json({"list": []}) == []
        assert scraper._extract_slips_from_json([]) == []

    def test_item_missing_id_returns_none(self):
        result = BetmanScraper._item_to_betslip({"statusNm": "발매중"})
        assert result is None


class TestItemToBetslip:
    def test_parses_all_fields(self):
        item = {
            "buyNo": "ABC123",
            "gameNm": "프로토 승부식",
            "roundNo": "200",
            "statusNm": "발매마감",
            "buyDt": "2025-06-01 12:00",
            "buyAmt": 10000,
            "expectAmt": 50000,
            "totOdds": 5.0,
            "detailList": [
                {
                    "matchNo": 1,
                    "sportNm": "농구",
                    "leagueNm": "NBA",
                    "homeTeamNm": "LAL",
                    "awayTeamNm": "GSW",
                    "selectNm": "홈승",
                    "odds": 1.5,
                    "gameDt": "2025-06-02 10:00",
                },
                {
                    "matchNo": 2,
                    "sportNm": "축구",
                    "leagueNm": "EPL",
                    "homeTeamNm": "리버풀",
                    "awayTeamNm": "맨시티",
                    "selectNm": "무승부",
                    "odds": 3.33,
                    "gameDt": "2025-06-02 21:00",
                },
            ],
        }
        slip = BetmanScraper._item_to_betslip(item)
        assert slip is not None
        assert slip.slip_id == "ABC123"
        assert slip.total_amount == 10000
        assert slip.potential_payout == 50000
        assert slip.combined_odds == 5.0
        assert len(slip.matches) == 2
        assert slip.matches[0].sport == "농구"
        assert slip.matches[1].league == "EPL"


class TestStatusFiltering:
    def _make_scraper(self) -> BetmanScraper:
        from unittest.mock import MagicMock
        return BetmanScraper(MagicMock())

    def test_filters_by_target_statuses(self):
        scraper = self._make_scraper()
        scraper._captured_responses = [
            {
                "list": [
                    {"buyNo": "1", "statusNm": "발매중", "buyAmt": 1000, "expectAmt": 2000, "totOdds": 2.0, "gameNm": "A", "roundNo": "1", "buyDt": ""},
                    {"buyNo": "2", "statusNm": "적중", "buyAmt": 1000, "expectAmt": 2000, "totOdds": 2.0, "gameNm": "B", "roundNo": "2", "buyDt": ""},
                    {"buyNo": "3", "statusNm": "발매마감", "buyAmt": 1000, "expectAmt": 2000, "totOdds": 2.0, "gameNm": "C", "roundNo": "3", "buyDt": ""},
                    {"buyNo": "4", "statusNm": "미적중", "buyAmt": 1000, "expectAmt": 2000, "totOdds": 2.0, "gameNm": "D", "roundNo": "4", "buyDt": ""},
                ]
            }
        ]
        slips = scraper._parse_xhr_responses()
        assert len(slips) == 2
        ids = {s.slip_id for s in slips}
        assert ids == {"1", "3"}
