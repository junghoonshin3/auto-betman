from __future__ import annotations

from src.games import (
    _extract_buyable_games,
    _extract_schedule_rows,
    _to_sale_game_match,
    scrape_sale_games_summary,
)


def test_extract_buyable_games_from_proto_and_toto() -> None:
    payload = {
        "protoGames": [{"gmId": "G101"}],
        "totoGames": [{"gmId": "G011"}],
    }
    rows = _extract_buyable_games(payload)
    assert len(rows) == 2
    assert {row["gmId"] for row in rows} == {"G101", "G011"}


def test_extract_schedule_rows_from_known_keys() -> None:
    payload = {
        "data": {
            "dl_schedulesList": [{"matchSeq": 1}, {"matchSeq": 2}],
            "orgScheduleList": {"3": {"matchSeq": 3}},
        }
    }
    rows = _extract_schedule_rows(payload)
    assert len(rows) == 3
    assert {int(row["matchSeq"]) for row in rows} == {1, 2, 3}


def test_to_sale_game_match_maps_core_fields() -> None:
    game_row = {
        "gmId": "G101",
        "gmTs": 260019,
        "gmOsidTs": 19,
        "gameMaster": {"gameNickName": "승부식"},
        "saleEndDate": 1760000000000,
    }
    schedule_row = {
        "matchSeq": 7,
        "mchSportCd": "SC",
        "leagueNm": "EPL",
        "homeName": "리버풀",
        "awayName": "아스날",
        "handiTypeNm": "일반",
        "winAllot": 1.8,
        "drawAllot": 3.2,
        "loseAllot": 4.0,
        "protoStatus": "2",
    }
    match = _to_sale_game_match(schedule_row, game_row)
    assert match.gm_id == "G101"
    assert match.sport == "축구"
    assert match.league == "EPL"
    assert match.home_team == "리버풀"
    assert match.away_team == "아스날"
    assert match.odds_home == 1.8
    assert match.status == "발매중"


class _FakePage:
    def __init__(self, endpoint_responses: dict[str, list[object]]) -> None:
        self._endpoint_responses = {k: list(v) for k, v in endpoint_responses.items()}

    async def evaluate(self, script: str, arg=None):  # type: ignore[no-untyped-def]
        if isinstance(arg, dict) and "endpoint" in arg:
            endpoint = str(arg["endpoint"])
            responses = self._endpoint_responses.get(endpoint, [])
            if not responses:
                return {"__error": f"no-mock-for:{endpoint}"}
            return responses.pop(0)
        return None

    async def wait_for_load_state(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        return None

    async def goto(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        return None

    async def wait_for_function(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        return None


async def test_scrape_sale_games_summary_filters_status_and_counts_failures() -> None:
    page = _FakePage(
        endpoint_responses={
            "/buyPsblGame/inqCacheBuyAbleGameInfoList.do": [
                {
                    "protoGames": [
                        {"gmId": "G101", "gmTs": 260019, "gmOsidTs": 19, "gameMaster": {"gameNickName": "승부식"}},
                        {"gmId": "G011", "gmTs": 260011, "gmOsidTs": 11, "gameMaster": {"gameNickName": "승무패"}},
                    ],
                    "totoGames": [],
                }
            ],
            "/buyPsblGame/gameInfoInq.do": [
                {
                    "data": {
                        "dl_schedulesList": [
                            {
                                "matchSeq": 1,
                                "mchSportCd": "SC",
                                "leagueNm": "EPL",
                                "homeName": "A",
                                "awayName": "B",
                                "handiTypeNm": "일반",
                                "saleEndDate": 1760000000000,
                                "winAllot": 1.8,
                                "drawAllot": 3.1,
                                "loseAllot": 4.2,
                                "protoStatus": "2",
                            },
                            {
                                "matchSeq": 2,
                                "mchSportCd": "SC",
                                "leagueNm": "EPL",
                                "homeName": "C",
                                "awayName": "D",
                                "protoStatus": "3",
                            },
                        ]
                    }
                },
                {"__error": "detail-failed"},
            ],
        }
    )

    snapshot = await scrape_sale_games_summary(page, nearest_limit=20)
    assert snapshot.total_matches == 1
    assert snapshot.total_games == 1
    assert snapshot.partial_failures == 1
    assert snapshot.sport_counts.get("축구") == 1
    assert len(snapshot.nearest_matches) == 1


async def test_scrape_sale_games_summary_applies_nearest_limit() -> None:
    rows = [
        {
            "matchSeq": 1,
            "mchSportCd": "SC",
            "homeName": "A",
            "awayName": "B",
            "saleEndDate": 1760000001000,
            "protoStatus": "2",
        },
        {
            "matchSeq": 2,
            "mchSportCd": "SC",
            "homeName": "C",
            "awayName": "D",
            "saleEndDate": 1760000000000,
            "protoStatus": "2",
        },
        {
            "matchSeq": 3,
            "mchSportCd": "SC",
            "homeName": "E",
            "awayName": "F",
            "saleEndDate": 1760000002000,
            "protoStatus": "2",
        },
    ]
    page = _FakePage(
        endpoint_responses={
            "/buyPsblGame/inqCacheBuyAbleGameInfoList.do": [
                {"protoGames": [{"gmId": "G101", "gmTs": 260019, "gmOsidTs": 19, "gameMaster": {"gameNickName": "승부식"}}], "totoGames": []}
            ],
            "/buyPsblGame/gameInfoInq.do": [{"data": {"dl_schedulesList": rows}}],
        }
    )

    snapshot = await scrape_sale_games_summary(page, nearest_limit=2)
    assert len(snapshot.nearest_matches) == 2
    assert snapshot.nearest_matches[0].match_seq == 2
    assert snapshot.nearest_matches[1].match_seq == 1
