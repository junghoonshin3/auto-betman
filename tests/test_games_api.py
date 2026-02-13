from __future__ import annotations

from src.games import (
    _build_game_detail_params_candidates,
    _extract_buyable_games,
    _extract_schedule_rows,
    _normalize_game_type,
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


def test_extract_schedule_rows_from_marking_data_schedules() -> None:
    payload = {
        "markingData": {
            "schedules": [
                {"matchSeq": 11, "homeName": "A", "awayName": "B"},
                {"matchSeq": 12, "homeName": "C", "awayName": "D"},
            ]
        }
    }
    rows = _extract_schedule_rows(payload)
    assert len(rows) == 2
    assert {int(row["matchSeq"]) for row in rows} == {11, 12}


def test_extract_schedule_rows_from_comp_schedules_matrix() -> None:
    payload = {
        "compSchedules": {
            "keys": ["itemCode", "leagueName", "matchSeq", "homeName", "awayName", "protoStatus", "winAllot", "drawAllot", "loseAllot", "endDate"],
            "datas": [
                ["SC", "EPL", 31, "홈A", "원정B", "2", 1.8, 3.3, 4.1, 1760000000000],
                ["SC", "EPL", 32, "홈C", "원정D", "3", 1.9, 3.1, 3.9, 1760000001000],
            ],
        }
    }
    rows = _extract_schedule_rows(payload)
    assert len(rows) == 2
    assert rows[0]["homeName"] == "홈A"
    assert rows[0]["protoStatus"] == "2"


def test_build_game_detail_params_candidates_prefers_g102_round_and_year() -> None:
    game_row = {
        "gmId": "G102",
        "gmTs": 999999,
        "gmOsidTs": 547,
        "gmOsidTsYear": 2026,
    }
    params = _build_game_detail_params_candidates(game_row)
    assert params
    assert params[0]["gmTs"] == 547
    assert params[0]["year"] == "2026"
    assert params[0]["gameYear"] == "2026"


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
        "gameDate": 1760003600000,
        "endDate": 1760000000000,
        "winAllot": 1.8,
        "drawAllot": 3.2,
        "loseAllot": 4.0,
        "protoStatus": "2",
    }
    match = _to_sale_game_match(schedule_row, game_row)
    assert match.gm_id == "G101"
    assert match.game_type == "승부식"
    assert match.sport == "축구"
    assert match.home_team == "리버풀"
    assert match.away_team == "아스날"
    assert match.match_name == "리버풀 vs 아스날"
    assert match.round_label == "19회차"
    assert match.start_at != "-"
    assert match.status == "발매중"


def test_normalize_game_type_standardizes_known_labels() -> None:
    assert _normalize_game_type("프로토 승부식") == "승부식"
    assert _normalize_game_type("기록식") == "기록식"
    assert _normalize_game_type(" 승무패 ") == "승무패"
    assert _normalize_game_type("기타타입") == "기타타입"


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


async def test_scrape_sale_games_summary_reads_marking_data_and_retries_alt_params() -> None:
    page = _FakePage(
        endpoint_responses={
            "/buyPsblGame/inqCacheBuyAbleGameInfoList.do": [
                {
                    "protoGames": [
                        {
                            "gmId": "G102",
                            "gmTs": 999999,
                            "gmOsidTs": 547,
                            "gmOsidTsYear": 2026,
                            "gameMaster": {"gameNickName": "기록식"},
                            "protoStatus": "2",
                        }
                    ],
                    "totoGames": [],
                }
            ],
            "/buyPsblGame/gameInfoInq.do": [
                {"__timeout": True},
                {
                    "markingData": {
                        "schedules": [
                            {
                                "matchSeq": 22,
                                "itemCode": "BK",
                                "leagueName": "NBA",
                                "homeName": "홈A",
                                "awayName": "원정B",
                                "saleEndDate": 1760000000000,
                                "winAllot": 1.7,
                                "drawAllot": 0,
                                "loseAllot": 1.8,
                                "protoStatus": "2",
                            }
                        ]
                    }
                },
            ],
        }
    )

    snapshot = await scrape_sale_games_summary(page, nearest_limit=20)
    assert snapshot.total_games == 1
    assert snapshot.total_matches == 1
    assert snapshot.partial_failures == 0
    assert snapshot.nearest_matches[0].sport == "농구"


async def test_scrape_sale_games_summary_handles_comp_schedules_and_status_missing_rows() -> None:
    page = _FakePage(
        endpoint_responses={
            "/buyPsblGame/inqCacheBuyAbleGameInfoList.do": [
                {
                    "currentTime": 1770990000000,
                    "protoGames": [{"gmId": "G101", "gmTs": 260020, "gmOsidTs": 20, "gmOsidTsYear": 2026, "gameMaster": {"gameNickName": "승부식"}}],
                    "totoGames": [{"gmId": "G011", "gmTs": 260011, "gmOsidTs": 11, "gmOsidTsYear": 2026, "gameMaster": {"gameNickName": "승무패"}}],
                }
            ],
            "/buyPsblGame/gameInfoInq.do": [
                {
                    "compSchedules": {
                        "keys": ["itemCode", "leagueName", "matchSeq", "homeName", "awayName", "protoStatus", "winAllot", "drawAllot", "loseAllot", "endDate", "handi"],
                        "datas": [
                            ["SC", "프리그1", 31, "스타드렌", "PSG", "2", 6.2, 4.55, 1.28, 1770991200000, 0]
                        ],
                    }
                },
                {
                    "schedulesList": [
                        {
                            "itemCode": "SC",
                            "leagueName": "세리에A",
                            "matchSeq": 91,
                            "homeName": "코모1907",
                            "awayName": "피오렌티",
                            "protoStatus": None,
                            "winAllot": 2.2,
                            "drawAllot": 3.0,
                            "loseAllot": 2.4,
                            "endDate": 1770980000000,
                            "handi": 0,
                        }
                    ]
                },
            ],
        }
    )

    snapshot = await scrape_sale_games_summary(page, nearest_limit=20)
    assert snapshot.partial_failures == 0
    # endDate가 currentTime보다 과거인 경기는 제외되어야 한다.
    assert snapshot.total_matches == 1
    assert snapshot.total_games == 1


async def test_scrape_sale_games_summary_includes_status_1_excludes_0_3_4_and_uses_time_fallback() -> None:
    page = _FakePage(
        endpoint_responses={
            "/buyPsblGame/inqCacheBuyAbleGameInfoList.do": [
                {
                    "currentTime": 1770990000000,
                    "protoGames": [{"gmId": "G101", "gmTs": 260020, "gmOsidTs": 20, "gmOsidTsYear": 2026, "gameMaster": {"gameNickName": "승부식"}}],
                    "totoGames": [],
                }
            ],
            "/buyPsblGame/gameInfoInq.do": [
                {
                    "schedulesList": [
                        # status=1 should be treated as open
                        {"itemCode": "SC", "matchSeq": 11, "homeName": "A", "awayName": "B", "protoStatus": "1", "gameDate": 1771005600000, "endDate": 1770991200000},
                        # status=2 open
                        {"itemCode": "SC", "matchSeq": 12, "homeName": "C", "awayName": "D", "protoStatus": "2", "gameDate": 1771005600001, "endDate": 1770991300000},
                        # status=0/3/4 should be excluded
                        {"itemCode": "SC", "matchSeq": 13, "homeName": "E", "awayName": "F", "protoStatus": "0", "gameDate": 1771005600002, "endDate": 1770991400000},
                        {"itemCode": "SC", "matchSeq": 14, "homeName": "G", "awayName": "H", "protoStatus": "3", "gameDate": 1771005600003, "endDate": 1770991500000},
                        {"itemCode": "SC", "matchSeq": 15, "homeName": "I", "awayName": "J", "protoStatus": "4", "gameDate": 1771005600004, "endDate": 1770991600000},
                        # missing status -> include by endDate fallback (future)
                        {"itemCode": "SC", "matchSeq": 16, "homeName": "K", "awayName": "L", "protoStatus": None, "gameDate": 1771005600005, "endDate": 1770991700000},
                        # missing status + past endDate -> exclude
                        {"itemCode": "SC", "matchSeq": 17, "homeName": "M", "awayName": "N", "protoStatus": None, "gameDate": 1771005600006, "endDate": 1770980000000},
                    ]
                }
            ],
        }
    )

    snapshot = await scrape_sale_games_summary(page, nearest_limit=None)
    names = {(m.home_team, m.away_team) for m in snapshot.nearest_matches}
    assert ("A", "B") in names
    assert ("C", "D") in names
    assert ("K", "L") in names
    assert ("E", "F") not in names
    assert ("G", "H") not in names
    assert ("I", "J") not in names
    assert ("M", "N") not in names
    assert snapshot.total_matches == 3


async def test_scrape_sale_games_summary_dedupes_same_match_across_bet_variants() -> None:
    page = _FakePage(
        endpoint_responses={
            "/buyPsblGame/inqCacheBuyAbleGameInfoList.do": [
                {
                    "currentTime": 1770990000000,
                    "protoGames": [{"gmId": "G101", "gmTs": 260020, "gmOsidTs": 20, "gmOsidTsYear": 2026, "gameMaster": {"gameNickName": "승부식"}}],
                    "totoGames": [],
                }
            ],
            "/buyPsblGame/gameInfoInq.do": [
                {
                    "compSchedules": {
                        "keys": ["itemCode", "matchSeq", "homeName", "awayName", "protoStatus", "gameDate", "endDate", "handi"],
                        "datas": [
                            ["SC", 31, "스타드렌", "PSG", "2", 1771005600000, 1770991200000, 0],
                            ["SC", 31, "스타드렌", "PSG", "2", 1771005600000, 1770991200000, 2],
                            ["SC", 31, "스타드렌", "PSG", "2", 1771005600000, 1770991200000, 9],
                        ],
                    }
                }
            ],
        }
    )

    snapshot = await scrape_sale_games_summary(page, nearest_limit=None)
    assert snapshot.total_matches == 1
    assert len(snapshot.nearest_matches) == 1
