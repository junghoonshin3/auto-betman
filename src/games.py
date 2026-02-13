from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from playwright.async_api import Page

from src.models import SaleGameMatch, SaleGamesSnapshot

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
_BUYABLE_GAME_LIST_PATH = "/main/mainPage/gamebuy/buyableGameList.do"

_SPORT_NAME_BY_CODE = {
    "SC": "축구",
    "BS": "야구",
    "BK": "농구",
    "VL": "배구",
    "VB": "배구",
    "GF": "골프",
    "MI": "OX",
    "UO": "UO",
    "PT": "프로토",
}

_BET_TYPE_BY_HANDI = {
    "0": "일반",
    "1": "핸디캡",
    "2": "언더오버",
    "3": "스페셜",
}


def _pick(data: dict[str, Any], keys: list[str], default: Any = "") -> Any:
    for key in keys:
        value = data.get(key)
        if value is not None:
            return value
    return default


def _to_int(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    digits = re.sub(r"[^0-9-]", "", text)
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    cleaned = re.sub(r"[^0-9.-]", "", text)
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _strip_html(text: str) -> str:
    no_tag = re.sub(r"<[^>]*>", "", text or "")
    return re.sub(r"\s+", " ", no_tag).strip()


def _epoch_ms(value: Any) -> int | None:
    iv = _to_int(value)
    if iv is None:
        return None
    if iv > 10_000_000_000:
        return iv
    if iv > 1_000_000_000:
        return iv * 1000
    return None


def _format_sale_end_at(value: Any) -> str:
    epoch = _epoch_ms(value)
    if epoch is not None:
        dt = datetime.fromtimestamp(epoch / 1000, tz=KST)
        return dt.strftime("%m.%d %H:%M")

    text = str(value or "").strip()
    digits = re.sub(r"[^0-9]", "", text)
    if len(digits) >= 12:
        mm = digits[4:6]
        dd = digits[6:8]
        hh = digits[8:10]
        mi = digits[10:12]
        return f"{mm}.{dd} {hh}:{mi}"
    return text


def _sport_name_from_code(code: Any, fallback_name: Any) -> str:
    text_code = str(code or "").strip().upper()
    if text_code and text_code in _SPORT_NAME_BY_CODE:
        return _SPORT_NAME_BY_CODE[text_code]
    text_name = _strip_html(str(fallback_name or ""))
    if text_name:
        return text_name
    return text_code or "기타"


def _status_from_proto_status(proto_status: Any) -> str:
    return "발매중" if str(proto_status or "").strip() == "2" else "기타"


def _odds_or_none(value: Any) -> float | None:
    odds = _to_float(value)
    if odds is None:
        return None
    if odds <= 0:
        return None
    return odds


def _extract_buyable_games(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []

    candidates: list[dict[str, Any]] = []
    roots: list[dict[str, Any]] = [payload]
    for key in ("data", "body", "result"):
        child = payload.get(key)
        if isinstance(child, dict):
            roots.append(child)

    for root in roots:
        proto_games = root.get("protoGames")
        toto_games = root.get("totoGames")
        if isinstance(proto_games, list):
            candidates.extend([x for x in proto_games if isinstance(x, dict)])
        if isinstance(toto_games, list):
            candidates.extend([x for x in toto_games if isinstance(x, dict)])

    if candidates:
        return candidates

    any_games = payload.get("gameList")
    if isinstance(any_games, list):
        return [x for x in any_games if isinstance(x, dict)]
    return []


def _extract_schedule_rows(detail_payload: Any) -> list[dict[str, Any]]:
    if not isinstance(detail_payload, dict):
        return []

    roots: list[dict[str, Any]] = [detail_payload]
    for key in ("data", "body", "result"):
        child = detail_payload.get(key)
        if isinstance(child, dict):
            roots.append(child)

    rows: list[dict[str, Any]] = []
    for root in roots:
        for key in ("dl_schedulesList", "scheduleList", "schedules"):
            value = root.get(key)
            if isinstance(value, list):
                rows.extend([x for x in value if isinstance(x, dict)])
        org_schedule = root.get("orgScheduleList")
        if isinstance(org_schedule, dict):
            rows.extend([x for x in org_schedule.values() if isinstance(x, dict)])

    if rows:
        return rows

    for root in roots:
        for value in root.values():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                sample = value[0]
                if any(k in sample for k in ("matchSeq", "homeName", "awayName", "winAllot", "protoStatus")):
                    rows.extend([x for x in value if isinstance(x, dict)])
    return rows


def _extract_game_meta(game_row: dict[str, Any]) -> tuple[str, str, str]:
    gm_id = str(_pick(game_row, ["gmId", "gameId"], "")).strip()
    gm_ts_raw = _pick(game_row, ["gmTs", "gmOsidTs", "gameTs"], "")
    gm_ts = str(gm_ts_raw).strip()

    game_master = game_row.get("gameMaster")
    game_nick = ""
    if isinstance(game_master, dict):
        game_nick = _strip_html(str(game_master.get("gameNickName") or ""))
    if not game_nick:
        game_nick = _strip_html(str(_pick(game_row, ["gameNickName", "gmNm"], "")))

    round_text = str(_pick(game_row, ["gmOsidTs", "gmTs", "roundNo"], "")).strip()
    game_name = f"{game_nick} {round_text}회차".strip()
    return gm_id, gm_ts, game_name


def _build_game_detail_params(game_row: dict[str, Any]) -> dict[str, Any]:
    gm_id = str(_pick(game_row, ["gmId", "gameId"], "")).strip()
    gm_ts = _to_int(_pick(game_row, ["gmTs", "gmOsidTs", "gameTs"], ""))
    params: dict[str, Any] = {"gmId": gm_id}
    if gm_ts is not None:
        params["gmTs"] = gm_ts
    else:
        params["gmTs"] = str(_pick(game_row, ["gmTs", "gmOsidTs", "gameTs"], "")).strip()

    game_year = _pick(game_row, ["gmOsidTsYear", "gameYear", "year"], "")
    if str(game_year).strip():
        params["gameYear"] = str(game_year).strip()
    return params


def _to_sale_game_match(schedule_row: dict[str, Any], game_row: dict[str, Any]) -> SaleGameMatch:
    gm_id, gm_ts, game_name = _extract_game_meta(game_row)
    match_seq = _to_int(_pick(schedule_row, ["matchSeq", "gmSeq", "matchNo"], 0)) or 0

    sport_code = _pick(schedule_row, ["mchSportCd", "itemCode", "sportsItemCd"], "")
    sport_name = _pick(schedule_row, ["mchSportNm", "sportNm", "itemName"], "")
    sport = _sport_name_from_code(sport_code, sport_name)

    league = _strip_html(str(_pick(schedule_row, ["leagueName", "leagueNm", "mchLeagueNm"], "")))
    home_team = _strip_html(str(_pick(schedule_row, ["homeName", "homeTeamNm", "mchHomeNm"], "")))
    away_team = _strip_html(str(_pick(schedule_row, ["awayName", "awayTeamNm", "mchAwayNm"], "")))

    bet_type = _strip_html(str(_pick(schedule_row, ["handiTypeNm", "gameTypeNm", "betTypeNm"], "")))
    if not bet_type:
        handi = str(_pick(schedule_row, ["handiType", "handi"], "")).strip()
        bet_type = _BET_TYPE_BY_HANDI.get(handi, "일반")

    sale_end_source = _pick(schedule_row, ["saleEndDate", "saleEndDt"], _pick(game_row, ["saleEndDate", "saleEndDt"], ""))
    sale_end_at = _format_sale_end_at(sale_end_source)
    sale_end_epoch_ms = _epoch_ms(sale_end_source)

    return SaleGameMatch(
        gm_id=gm_id,
        gm_ts=gm_ts,
        game_name=game_name,
        sport=sport,
        league=league,
        match_seq=match_seq,
        home_team=home_team,
        away_team=away_team,
        bet_type=bet_type,
        odds_home=_odds_or_none(_pick(schedule_row, ["winAllot", "homeAllot"], None)),
        odds_draw=_odds_or_none(_pick(schedule_row, ["drawAllot"], None)),
        odds_away=_odds_or_none(_pick(schedule_row, ["loseAllot", "awayAllot"], None)),
        sale_end_at=sale_end_at,
        sale_end_epoch_ms=sale_end_epoch_ms,
        status=_status_from_proto_status(_pick(schedule_row, ["protoStatus", "gmStCd"], "")),
    )


async def _request_post_method(page: Page, endpoint: str, params: dict[str, Any]) -> Any:
    return await page.evaluate(
        """({endpoint, params}) => {
            return new Promise((resolve) => {
                const timeout = setTimeout(() => resolve({__timeout: true}), 15000);
                try {
                    if (typeof requestClient !== 'undefined' && requestClient.requestPostMethod) {
                        requestClient.requestPostMethod(endpoint, params, true, function(data) {
                            clearTimeout(timeout);
                            resolve(data ?? {});
                        });
                    } else {
                        clearTimeout(timeout);
                        resolve({__error: 'requestClient unavailable'});
                    }
                } catch (e) {
                    clearTimeout(timeout);
                    resolve({__error: String(e)});
                }
            });
        }""",
        {"endpoint": endpoint, "params": params},
    )


async def _navigate_to_buyable_game_list(page: Page) -> None:
    try:
        await page.evaluate(f"movePageUrl('{_BUYABLE_GAME_LIST_PATH}')")
        await page.wait_for_load_state("networkidle", timeout=12000)
        return
    except Exception:
        pass
    await page.goto(f"https://www.betman.co.kr{_BUYABLE_GAME_LIST_PATH}", wait_until="networkidle", timeout=25000)


async def scrape_sale_games_summary(page: Page, nearest_limit: int = 20) -> SaleGamesSnapshot:
    nearest_limit = max(1, min(int(nearest_limit), 50))
    await _navigate_to_buyable_game_list(page)
    await page.wait_for_function(
        """() => typeof requestClient !== 'undefined' && typeof requestClient.requestPostMethod === 'function'""",
        timeout=10000,
    )

    list_payload = await _request_post_method(page, "/buyPsblGame/inqCacheBuyAbleGameInfoList.do", {})
    if not isinstance(list_payload, dict):
        raise RuntimeError("games list api failed: non-dict response")
    if list_payload.get("__error"):
        raise RuntimeError(f"games list api failed: {list_payload.get('__error')}")
    if list_payload.get("__timeout"):
        raise RuntimeError("games list api failed: timeout")

    game_rows = _extract_buyable_games(list_payload)
    if not game_rows:
        raise RuntimeError("games list api returned no games")

    matches: list[SaleGameMatch] = []
    partial_failures = 0
    included_game_keys: set[str] = set()

    for game_row in game_rows:
        params = _build_game_detail_params(game_row)
        gm_key = f"{params.get('gmId','')}:{params.get('gmTs','')}"
        detail_payload = await _request_post_method(page, "/buyPsblGame/gameInfoInq.do", params)
        if not isinstance(detail_payload, dict) or detail_payload.get("__error") or detail_payload.get("__timeout"):
            partial_failures += 1
            logger.warning("games detail api failed: gm=%s reason=%s", gm_key, detail_payload)
            continue

        schedule_rows = _extract_schedule_rows(detail_payload)
        if not schedule_rows:
            partial_failures += 1
            logger.warning("games detail api no schedules: gm=%s", gm_key)
            continue

        before = len(matches)
        for schedule_row in schedule_rows:
            if str(_pick(schedule_row, ["protoStatus", "gmStCd"], "")).strip() != "2":
                continue
            match = _to_sale_game_match(schedule_row, game_row)
            matches.append(match)
        if len(matches) > before:
            included_game_keys.add(gm_key)

    sport_counts: dict[str, int] = {}
    for match in matches:
        sport_counts[match.sport] = sport_counts.get(match.sport, 0) + 1

    nearest_matches = sorted(
        matches,
        key=lambda m: (
            m.sale_end_epoch_ms is None,
            m.sale_end_epoch_ms if m.sale_end_epoch_ms is not None else 9_999_999_999_999,
            m.match_seq,
        ),
    )[:nearest_limit]

    now_text = datetime.now(KST).strftime("%Y.%m.%d %H:%M:%S")
    return SaleGamesSnapshot(
        fetched_at=now_text,
        total_games=len(included_game_keys),
        total_matches=len(matches),
        sport_counts=dict(sorted(sport_counts.items(), key=lambda kv: kv[0])),
        nearest_matches=nearest_matches,
        partial_failures=partial_failures,
    )
