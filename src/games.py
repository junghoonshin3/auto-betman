from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from urllib.parse import parse_qsl, parse_qs, urlencode, urlparse, urlunparse
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from playwright.async_api import Page

from src.models import GamesCaptureResult, SaleGameMatch, SaleGamesSnapshot

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
_BUYABLE_GAME_LIST_PATH = "/main/mainPage/gamebuy/buyableGameList.do"
_REQUEST_RETRIES = 2
_REQUEST_BASE_DELAY_SECONDS = 0.4

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
OPEN_SCHEDULE_STATUSES = {"1", "2"}
_GAME_TYPE_OPTION_LABELS = {
    "victory": "승부식",
    "windrawlose": "승무패",
    "record": "기록식",
}
_SPORT_OPTION_LABELS = {
    "all": "전체",
    "soccer": "축구",
    "baseball": "야구",
    "basketball": "농구",
    "volleyball": "배구",
}
_GAMES_CAPTURE_MAX_IMAGES = 30
_GAMES_CAPTURE_WAIT_TIMEOUT_MS = 10000
_GAMES_CAPTURE_STABLE_ROUNDS = 2
_GAMES_CAPTURE_SAMPLE_INTERVAL_MS = 250
_GAMES_TABLE_TARGETS = (
    ("proto", "#tbl_protoBuyAbleGameList_wrapper", "#tbl_protoBuyAbleGameList"),
    ("toto", "#tbl_totoBuyAbleGameList_wrapper", "#tbl_totoBuyAbleGameList"),
)
_GAMES_DETAIL_WAIT_TIMEOUT_MS = 12000
_GAMES_DETAIL_SAMPLE_INTERVAL_MS = 300
_GAMES_DETAIL_STABLE_ROUNDS = 2
_GAMES_DETAIL_ROWS_PER_IMAGE = 8
_GAMES_SPORT_CODES_BY_OPTION = {
    "soccer": {"SC"},
    "baseball": {"BS"},
    "basketball": {"BK"},
    "volleyball": {"VL", "VB"},
}
_GAMES_SPORT_KEYWORDS_BY_OPTION = {
    "soccer": ("축구",),
    "baseball": ("야구",),
    "basketball": ("농구",),
    "volleyball": ("배구",),
}
_GAMES_DETAIL_SELECTORS_BY_GMID = {
    "G101": ("#div_gmBuySlip", "#tbl_gmBuySlipList", "#tabs-1"),
    "G102": ("#tabs-1", "#content #tabs-1"),
}
_GAMES_DETAIL_SELECTORS_DEFAULT = ("#grid_victory_div", "#grid_victory", "#tabs-1")


def _normalize_game_type(value: Any) -> str:
    text = _strip_html(str(value or "")).strip()
    compact = re.sub(r"\s+", "", text)
    if "승무패" in compact:
        return "승무패"
    if "승부식" in compact:
        return "승부식"
    if "기록식" in compact:
        return "기록식"
    return text or "기타"

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
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    digits = re.sub(r"[^0-9]", "", text)
    if not digits:
        return None

    # 13/10 digits are typical epoch ms/sec values.
    if len(digits) == 13:
        return int(digits)
    if len(digits) == 10 and digits.startswith("1"):
        return int(digits) * 1000

    # currentTime sometimes comes as YYYYMMDDHHMMSS / YYYYMMDDHHMM strings.
    try:
        if len(digits) >= 14:
            dt = datetime.strptime(digits[:14], "%Y%m%d%H%M%S").replace(tzinfo=KST)
            return int(dt.timestamp() * 1000)
        if len(digits) == 12:
            dt = datetime.strptime(digits, "%Y%m%d%H%M").replace(tzinfo=KST)
            return int(dt.timestamp() * 1000)
        if len(digits) == 10 and not digits.startswith("1"):
            dt = datetime.strptime(digits, "%y%m%d%H%M").replace(tzinfo=KST)
            return int(dt.timestamp() * 1000)
    except ValueError:
        return None

    iv = _to_int(digits)
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


def _is_sale_open_status(status: Any) -> bool:
    text = str(status or "").strip()
    return text in OPEN_SCHEDULE_STATUSES


def _status_from_proto_status(proto_status: Any) -> str:
    return "발매중" if _is_sale_open_status(proto_status) else "기타"


def _is_schedule_sale_open(schedule_row: dict[str, Any], game_row: dict[str, Any], now_ms: int | None) -> bool:
    schedule_status = _pick(schedule_row, ["protoStatus", "gmStCd", "mainState"], None)
    if schedule_status is not None and str(schedule_status).strip():
        return _is_sale_open_status(schedule_status)

    if now_ms is not None:
        schedule_end = _epoch_ms(_pick(schedule_row, ["endDate", "saleEndDate", "saleEndDt"], None))
        if schedule_end is not None:
            return schedule_end > now_ms

    game_status = _pick(game_row, ["protoStatus", "mainState", "saleStatusCode"], None)
    if game_status is not None and str(game_status).strip():
        return _is_sale_open_status(game_status)

    if now_ms is not None:
        game_end = _epoch_ms(_pick(game_row, ["saleEndDate", "saleEndDt", "endDate"], None))
        if game_end is not None:
            return game_end > now_ms

    # 구매가능 목록 경로에서 상태/마감 정보가 없으면 포함한다.
    return True


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

    roots: list[dict[str, Any]] = []
    queue: list[dict[str, Any]] = [detail_payload]
    seen_ids: set[int] = set()
    while queue:
        root = queue.pop(0)
        root_id = id(root)
        if root_id in seen_ids:
            continue
        seen_ids.add(root_id)
        roots.append(root)
        for key in ("data", "body", "result", "markingData"):
            child = root.get(key)
            if isinstance(child, dict):
                queue.append(child)

    rows: list[dict[str, Any]] = []
    for root in roots:
        for key in ("dl_schedulesList", "scheduleList", "schedules", "schedulesList"):
            value = root.get(key)
            if isinstance(value, list):
                rows.extend([x for x in value if isinstance(x, dict)])
        comp_schedules = root.get("compSchedules")
        if isinstance(comp_schedules, dict):
            keys = comp_schedules.get("keys")
            datas = comp_schedules.get("datas")
            if isinstance(keys, list) and isinstance(datas, list):
                for raw_row in datas:
                    if isinstance(raw_row, dict):
                        rows.append(raw_row)
                        continue
                    if not isinstance(raw_row, list):
                        continue
                    mapped: dict[str, Any] = {}
                    for idx, key in enumerate(keys):
                        if not isinstance(key, str):
                            continue
                        mapped[key] = raw_row[idx] if idx < len(raw_row) else None
                    if mapped:
                        rows.append(mapped)
        games = root.get("games")
        if isinstance(games, list):
            for row in games:
                if not isinstance(row, dict):
                    continue
                schedule = row.get("schedule")
                if isinstance(schedule, dict):
                    rows.append(schedule)
        org_schedule = root.get("orgScheduleList")
        if isinstance(org_schedule, dict):
            rows.extend([x for x in org_schedule.values() if isinstance(x, dict)])
        elif isinstance(org_schedule, list):
            rows.extend([x for x in org_schedule if isinstance(x, dict)])
        slip_schedule = root.get("slipPaperAndScheduleSetList")
        if isinstance(slip_schedule, list):
            for item in slip_schedule:
                if not isinstance(item, dict):
                    continue
                schedule = item.get("schedule")
                if isinstance(schedule, dict):
                    rows.append(schedule)

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
    ts_keys = ["gmOsidTs", "gmTs", "gameTs"] if gm_id == "G102" else ["gmTs", "gmOsidTs", "gameTs"]
    gm_ts_raw = _pick(game_row, ts_keys, "")
    gm_ts = str(gm_ts_raw).strip()
    round_text = str(_pick(game_row, ["gmOsidTs", "roundNo", "gmTs"], "")).strip()
    round_label = f"{round_text}회차" if round_text else "-"
    return gm_id, gm_ts, round_label


def _extract_game_type(game_row: dict[str, Any]) -> str:
    game_master = game_row.get("gameMaster")
    raw_type: Any = ""
    if isinstance(game_master, dict):
        raw_type = _pick(game_master, ["gameNickName", "nickName", "gameName", "name"], "")
    if not raw_type:
        raw_type = _pick(game_row, ["gameNickName", "gameName", "gmNm"], "")
    return _normalize_game_type(raw_type)


def _build_game_detail_params_candidates(game_row: dict[str, Any]) -> list[dict[str, Any]]:
    gm_id = str(_pick(game_row, ["gmId", "gameId"], "")).strip()
    params_candidates: list[dict[str, Any]] = []
    ts_values: list[Any] = []

    if gm_id == "G102":
        ts_values.extend(
            [
                _pick(game_row, ["gmOsidTs", "gmTs", "gameTs"], ""),
                _pick(game_row, ["gmTs", "gmOsidTs", "gameTs"], ""),
            ]
        )
    else:
        ts_values.extend(
            [
                _pick(game_row, ["gmTs", "gmOsidTs", "gameTs"], ""),
                _pick(game_row, ["gmOsidTs", "gmTs", "gameTs"], ""),
            ]
        )

    game_year = str(_pick(game_row, ["gmOsidTsYear", "year", "gameYear"], "")).strip()

    for ts_value in ts_values:
        gm_ts = _to_int(ts_value)
        params: dict[str, Any] = {"gmId": gm_id}
        if gm_ts is not None:
            params["gmTs"] = gm_ts
        else:
            params["gmTs"] = str(ts_value).strip()
        if game_year:
            # G102(기록식)은 year 키를 쓰고, 다른 타입은 gameYear를 쓰므로 둘 다 제공한다.
            params["year"] = game_year
            params["gameYear"] = game_year
        params_candidates.append(params)

    unique: list[dict[str, Any]] = []
    seen: set[tuple[tuple[str, str], ...]] = set()
    for params in params_candidates:
        key = tuple(sorted((k, str(v)) for k, v in params.items()))
        if key in seen:
            continue
        seen.add(key)
        unique.append(params)
    return unique


def _normalize_team_name(value: Any) -> str:
    return _strip_html(str(value or ""))


def _match_name(home_team: str, away_team: str) -> str:
    home = (home_team or "").strip() or "홈팀 미상"
    away = (away_team or "").strip() or "원정팀 미상"
    return f"{home} vs {away}"


def _to_sale_game_match(schedule_row: dict[str, Any], game_row: dict[str, Any]) -> SaleGameMatch:
    gm_id, gm_ts, round_label = _extract_game_meta(game_row)
    game_type = _extract_game_type(game_row)
    match_seq = _to_int(_pick(schedule_row, ["matchSeq", "gmSeq", "matchNo"], 0)) or 0

    sports_item = schedule_row.get("sportsItem")
    sports_item_id = ""
    sports_item_name = ""
    if isinstance(sports_item, dict):
        sports_item_id = str(_pick(sports_item, ["id", "sportsItemCd"], "")).strip()
        sports_item_name = str(_pick(sports_item, ["sportsItemName", "name"], "")).strip()

    sport_code = _pick(schedule_row, ["mchSportCd", "itemCode", "sportsItemCd"], sports_item_id)
    sport_name = _pick(schedule_row, ["mchSportNm", "sportNm", "itemName"], sports_item_name)
    sport = _sport_name_from_code(sport_code, sport_name)

    home_team = _normalize_team_name(_pick(schedule_row, ["homeName", "homeShortName", "homeTeamNm", "mchHomeNm"], ""))
    away_team = _normalize_team_name(_pick(schedule_row, ["awayName", "awayShortName", "awayTeamNm", "mchAwayNm"], ""))
    if (not home_team or not away_team) and isinstance(schedule_row.get("gmNm"), str):
        gm_name = str(schedule_row.get("gmNm") or "")
        if ":" in gm_name:
            left, right = gm_name.split(":", 1)
            if not home_team:
                home_team = _normalize_team_name(left)
            if not away_team:
                away_team = _normalize_team_name(right)

    start_source = _pick(schedule_row, ["gameDate", "startDate", "gameDateStr"], _pick(game_row, ["gameDate", "startDate"], ""))
    start_at = _format_sale_end_at(start_source)
    start_epoch_ms = _epoch_ms(start_source)

    sale_end_source = _pick(
        schedule_row,
        ["endDate", "saleEndDate", "saleEndDt", "gameDate", "gameDateStr"],
        _pick(game_row, ["saleEndDate", "saleEndDt", "endDate"], ""),
    )
    sale_end_at = _format_sale_end_at(sale_end_source)
    sale_end_epoch_ms = _epoch_ms(sale_end_source)
    status_source = _pick(schedule_row, ["protoStatus", "gmStCd", "mainState"], "")

    return SaleGameMatch(
        gm_id=gm_id,
        gm_ts=gm_ts,
        game_type=game_type,
        sport=sport,
        match_name=_match_name(home_team, away_team),
        round_label=round_label,
        match_seq=match_seq,
        home_team=home_team,
        away_team=away_team,
        start_at=start_at or "-",
        start_epoch_ms=start_epoch_ms,
        sale_end_at=sale_end_at,
        sale_end_epoch_ms=sale_end_epoch_ms,
        status=_status_from_proto_status(status_source),
    )


def _extract_current_time_ms(payload: dict[str, Any]) -> int | None:
    roots: list[dict[str, Any]] = [payload]
    for key in ("data", "body", "result"):
        child = payload.get(key)
        if isinstance(child, dict):
            roots.append(child)
    for root in roots:
        value = root.get("currentTime")
        if value is None:
            continue
        epoch = _epoch_ms(value)
        if epoch is not None:
            return epoch
    return None


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


def _is_request_payload_ok(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("__timeout"):
        return False
    if payload.get("__error"):
        return False
    return True


async def _request_post_with_retry(
    page: Page,
    endpoint: str,
    params: dict[str, Any],
    retries: int = _REQUEST_RETRIES,
    base_delay: float = _REQUEST_BASE_DELAY_SECONDS,
) -> Any:
    last_payload: Any = None
    max_attempts = max(1, int(retries) + 1)
    for attempt in range(1, max_attempts + 1):
        payload = await _request_post_method(page, endpoint, params)
        last_payload = payload
        if _is_request_payload_ok(payload):
            if attempt > 1:
                logger.info("games request recovered: endpoint=%s attempt=%d/%d", endpoint, attempt, max_attempts)
            return payload
        logger.warning(
            "games request failed: endpoint=%s attempt=%d/%d reason=%s",
            endpoint,
            attempt,
            max_attempts,
            payload,
        )
        if attempt < max_attempts:
            await asyncio.sleep(max(0.05, float(base_delay) * attempt))
    return last_payload


async def _navigate_to_buyable_game_list(page: Page) -> None:
    try:
        await page.evaluate(f"movePageUrl('{_BUYABLE_GAME_LIST_PATH}')")
        await page.wait_for_load_state("networkidle", timeout=12000)
        if _BUYABLE_GAME_LIST_PATH in str(page.url):
            return
    except Exception:
        pass
    await page.goto(f"https://www.betman.co.kr{_BUYABLE_GAME_LIST_PATH}", wait_until="networkidle", timeout=25000)


async def scrape_sale_games_summary(page: Page, nearest_limit: int | None = None) -> SaleGamesSnapshot:
    await _navigate_to_buyable_game_list(page)
    await page.wait_for_function(
        """() => typeof requestClient !== 'undefined' && typeof requestClient.requestPostMethod === 'function'""",
        timeout=10000,
    )

    list_payload = await _request_post_with_retry(page, "/buyPsblGame/inqCacheBuyAbleGameInfoList.do", {})
    if not isinstance(list_payload, dict):
        raise RuntimeError("games list api failed: non-dict response")
    if list_payload.get("__error"):
        raise RuntimeError(f"games list api failed: {list_payload.get('__error')}")
    if list_payload.get("__timeout"):
        raise RuntimeError("games list api failed: timeout")

    game_rows = _extract_buyable_games(list_payload)
    if not game_rows:
        raise RuntimeError("games list api returned no games")
    now_ms = _extract_current_time_ms(list_payload)

    matches: list[SaleGameMatch] = []
    seen_match_keys: set[tuple[Any, ...]] = set()
    partial_failures = 0
    included_game_keys: set[str] = set()
    schedule_status_counts: Counter[str] = Counter()
    open_included = 0
    filtered_out = 0
    deduped_out = 0

    for game_row in game_rows:
        game_status = _pick(game_row, ["protoStatus", "mainState", "saleStatusCode"], None)
        if game_status is not None and str(game_status).strip() and not _is_sale_open_status(game_status):
            continue

        schedule_rows: list[dict[str, Any]] = []
        used_key = ""
        last_failure: Any = None
        for params in _build_game_detail_params_candidates(game_row):
            gm_key = f"{params.get('gmId', '')}:{params.get('gmTs', '')}"
            detail_payload = await _request_post_with_retry(page, "/buyPsblGame/gameInfoInq.do", params)
            if not isinstance(detail_payload, dict) or detail_payload.get("__error") or detail_payload.get("__timeout"):
                last_failure = detail_payload
                logger.warning("games detail api failed: gm=%s reason=%s", gm_key, detail_payload)
                continue
            rows = _extract_schedule_rows(detail_payload)
            if rows:
                schedule_rows = rows
                used_key = gm_key
                break
            last_failure = {"__error": "no-schedules"}
            logger.warning("games detail api no schedules: gm=%s", gm_key)

        if not schedule_rows:
            partial_failures += 1
            logger.warning(
                "games detail api all candidates failed: gmId=%s reason=%s",
                _pick(game_row, ["gmId", "gameId"], ""),
                last_failure,
            )
            continue

        before = len(matches)
        for schedule_row in schedule_rows:
            schedule_status = str(_pick(schedule_row, ["protoStatus", "gmStCd", "mainState"], "")).strip()
            if schedule_status:
                schedule_status_counts[schedule_status] += 1
            if not _is_schedule_sale_open(schedule_row, game_row, now_ms):
                filtered_out += 1
                continue
            match = _to_sale_game_match(schedule_row, game_row)
            match_key = (
                match.gm_id,
                match.round_label,
                match.home_team,
                match.away_team,
                match.start_epoch_ms,
            )
            if match_key in seen_match_keys:
                deduped_out += 1
                continue
            seen_match_keys.add(match_key)
            matches.append(match)
            open_included += 1
        if len(matches) > before:
            included_game_keys.add(used_key or f"{_pick(game_row, ['gmId', 'gameId'], '')}:{_pick(game_row, ['gmTs', 'gmOsidTs'], '')}")

    sport_counts: dict[str, int] = {}
    for match in matches:
        sport_counts[match.sport] = sport_counts.get(match.sport, 0) + 1

    sorted_matches = sorted(
        matches,
        key=lambda m: (
            m.sale_end_epoch_ms is None,
            m.sale_end_epoch_ms if m.sale_end_epoch_ms is not None else 9_999_999_999_999,
            m.start_epoch_ms if m.start_epoch_ms is not None else 9_999_999_999_999,
            m.match_seq,
        ),
    )

    if nearest_limit is not None:
        limit = max(1, min(int(nearest_limit), 5000))
        sorted_matches = sorted_matches[:limit]

    logger.info(
        "games collection summary: list_games=%d included_games=%d total_rows=%d open_included=%d filtered_out=%d deduped_out=%d status_counts=%s partial_failures=%d",
        len(game_rows),
        len(included_game_keys),
        len(matches) + filtered_out + deduped_out,
        open_included,
        filtered_out,
        deduped_out,
        dict(sorted(schedule_status_counts.items(), key=lambda kv: kv[0])),
        partial_failures,
    )

    now_text = datetime.now(KST).strftime("%Y.%m.%d %H:%M:%S")
    return SaleGamesSnapshot(
        fetched_at=now_text,
        total_games=len(included_game_keys),
        total_matches=len(matches),
        sport_counts=dict(sorted(sport_counts.items(), key=lambda kv: kv[0])),
        nearest_matches=sorted_matches,
        partial_failures=partial_failures,
    )


def normalize_games_capture_game_type(value: str | None) -> str:
    text = str(value or "").strip().lower()
    if text in _GAME_TYPE_OPTION_LABELS:
        return text
    if text:
        logger.warning("games type normalized_from_legacy raw=%s normalized=victory", text)
    return "victory"


def normalize_games_capture_sport(value: str | None) -> str:
    text = str(value or "").strip().lower()
    if text in _SPORT_OPTION_LABELS:
        return text
    return "all"


def _normalize_compact_text(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).strip()


def _normalize_gameslip_href(raw_href: str | None) -> str:
    href = str(raw_href or "").strip()
    if not href:
        return ""
    if href.startswith("http://") or href.startswith("https://"):
        return href
    if href.startswith("/"):
        return f"https://www.betman.co.kr{href}"
    return f"https://www.betman.co.kr/{href.lstrip('/')}"


def _canonical_gameslip_href(href: str) -> str:
    if not href:
        return ""
    parsed = urlparse(href)
    sorted_query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", sorted_query, ""))


def _extract_gameslip_query_values(href: str) -> dict[str, str]:
    if not href:
        return {"gm_id": "", "gm_ts": "", "year": ""}
    query = parse_qs(urlparse(href).query)
    gm_id = str((query.get("gmId") or [""])[0]).strip().upper()
    gm_ts = str((query.get("gmTs") or [""])[0]).strip()
    year = str((query.get("year") or [""])[0]).strip()
    return {"gm_id": gm_id, "gm_ts": gm_ts, "year": year}


def _classify_row_game_type(gm_id: str, row_text: str) -> str:
    compact = _normalize_compact_text(row_text)
    gm = str(gm_id or "").strip().upper()
    if gm == "G102" or "기록식" in compact:
        return "record"
    if "승무패" in compact:
        return "windrawlose"
    return "victory"


def _detect_row_sport_option(sport_code: str | None, row_text: str) -> str | None:
    code = str(sport_code or "").strip().upper()
    for option, codes in _GAMES_SPORT_CODES_BY_OPTION.items():
        if code in codes:
            return option
    compact = _normalize_compact_text(row_text)
    for option, keywords in _GAMES_SPORT_KEYWORDS_BY_OPTION.items():
        if any(keyword in compact for keyword in keywords):
            return option
    return None


def _row_matches_games_filters(row_meta: dict[str, Any], normalized_type: str, normalized_sport: str) -> bool:
    row_text = str(row_meta.get("text") or "")
    gm_id = str(row_meta.get("gmId") or "")
    game_type = _classify_row_game_type(gm_id, row_text)
    if game_type != normalized_type:
        return False

    detected_sport = _detect_row_sport_option(str(row_meta.get("sportCode") or ""), row_text)
    if normalized_sport != "all" and detected_sport != normalized_sport:
        return False

    return True


async def _resolve_games_table_targets(page: Page) -> list[dict[str, str]]:
    targets: list[dict[str, str]] = []
    for table_key, wrapper_selector, table_selector in _GAMES_TABLE_TARGETS:
        try:
            table_locator = page.locator(table_selector).first
            if await table_locator.count() <= 0:
                continue
            wrapper_locator = page.locator(wrapper_selector).first
            capture_selector = wrapper_selector if await wrapper_locator.count() > 0 else table_selector
            targets.append(
                {
                    "table_key": table_key,
                    "wrapper_selector": wrapper_selector,
                    "table_selector": table_selector,
                    "capture_selector": capture_selector,
                }
            )
        except Exception:
            continue
    if not targets:
        logger.warning("games capture list_root_not_found")
    return targets


async def _read_games_tables_state(page: Page, table_selectors: list[str]) -> dict[str, Any] | None:
    try:
        state = await page.evaluate(
            """({ tableSelectors }) => {
                const normalizeText = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
                let foundTable = false;
                const rowTexts = [];
                for (const selector of tableSelectors || []) {
                    const table = document.querySelector(String(selector));
                    if (!(table instanceof HTMLElement)) continue;
                    foundTable = true;

                    const loadingNodes = table.querySelectorAll('.loading, [class*="loading"], [aria-busy="true"]');
                    for (const node of loadingNodes) {
                        if (!(node instanceof HTMLElement)) continue;
                        const style = window.getComputedStyle(node);
                        const isVisible =
                            style.display !== 'none' &&
                            style.visibility !== 'hidden' &&
                            (node.offsetWidth > 0 || node.offsetHeight > 0 || node.getClientRects().length > 0);
                        if (isVisible) {
                            return { ready: false, rowCount: 0, signature: '' };
                        }
                    }

                    const rows = Array.from(table.querySelectorAll('tbody tr'));
                    for (const row of rows) {
                        if (!(row instanceof HTMLElement)) continue;
                        const text = normalizeText(row.textContent || '');
                        if (!text) continue;
                        rowTexts.push(text);
                    }
                }

                if (!foundTable) return { ready: false, rowCount: 0, signature: '' };
                const rowCount = rowTexts.length;
                const preview = rowTexts.slice(0, 30).join(' || ').slice(0, 1800);
                return {
                    ready: rowCount > 0,
                    rowCount,
                    signature: `${rowCount}|${preview}`,
                };
            }""",
            {"tableSelectors": table_selectors},
        )
    except Exception:
        return None
    if not isinstance(state, dict):
        return None
    return state


async def _wait_for_games_tables_stable(
    page: Page,
    *,
    table_selectors: list[str],
    timeout_ms: int = _GAMES_CAPTURE_WAIT_TIMEOUT_MS,
    stable_rounds: int = _GAMES_CAPTURE_STABLE_ROUNDS,
    sample_interval_ms: int = _GAMES_CAPTURE_SAMPLE_INTERVAL_MS,
) -> dict[str, Any] | None:
    timeout_ms = max(1, int(timeout_ms))
    stable_rounds = max(2, int(stable_rounds))
    sample_interval_ms = max(50, int(sample_interval_ms))

    deadline = asyncio.get_running_loop().time() + (timeout_ms / 1000)
    last_signature = ""
    stable_hits = 0
    last_state: dict[str, Any] | None = None

    while True:
        state = await _read_games_tables_state(page, table_selectors)
        if not isinstance(state, dict) or not bool(state.get("ready")):
            last_signature = ""
            stable_hits = 0
            last_state = None
        else:
            signature = str(state.get("signature") or "").strip()
            if signature and signature == last_signature:
                stable_hits += 1
                last_state = state
            elif signature:
                last_signature = signature
                stable_hits = 1
                last_state = state
            else:
                last_signature = ""
                stable_hits = 0
                last_state = None

        if stable_hits >= stable_rounds and last_state is not None:
            return last_state

        remain = deadline - asyncio.get_running_loop().time()
        if remain <= 0:
            return None
        await asyncio.sleep(min(sample_interval_ms / 1000, remain))


async def _collect_games_rows_meta(page: Page, table_selector: str) -> list[dict[str, Any]]:
    try:
        raw_rows = await page.evaluate(
            """({ tableSelector }) => {
                const table = document.querySelector(String(tableSelector));
                if (!(table instanceof HTMLElement)) return [];
                const normalizeText = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
                const pickSportCode = (row) => {
                    const node = row.querySelector('.icoGame');
                    if (!(node instanceof HTMLElement)) return '';
                    const tokens = String(node.className || '').split(/\\s+/);
                    for (const token of tokens) {
                        const upper = String(token || '').trim().toUpperCase();
                        if (['SC', 'BS', 'BK', 'VL', 'VB'].includes(upper)) return upper;
                    }
                    return '';
                };

                return Array.from(table.querySelectorAll('tbody tr')).map((row, rowIndex) => {
                    if (!(row instanceof HTMLElement)) return null;
                    const text = normalizeText(row.textContent || '');
                    if (!text) return null;

                    let href = '';
                    for (const link of Array.from(row.querySelectorAll('a[href]'))) {
                        const hrefValue = link.getAttribute('href') || '';
                        if (hrefValue.includes('gameSlip.do')) {
                            href = hrefValue;
                            break;
                        }
                    }

                    return {
                        rowIndex,
                        text,
                        href,
                        sportCode: pickSportCode(row),
                    };
                }).filter(Boolean);
            }""",
            {"tableSelector": table_selector},
        )
    except Exception:
        return []
    if not isinstance(raw_rows, list):
        return []

    rows: list[dict[str, Any]] = []
    for item in raw_rows:
        if not isinstance(item, dict):
            continue
        try:
            row_index = int(item.get("rowIndex"))
        except Exception:
            continue
        rows.append(
            {
                "rowIndex": row_index,
                "text": str(item.get("text") or ""),
                "href": _normalize_gameslip_href(str(item.get("href") or "")),
                "sportCode": str(item.get("sportCode") or "").strip().upper(),
            }
        )
    return rows


def _select_gameslip_targets(
    rows_meta: list[dict[str, Any]],
    normalized_type: str,
    normalized_sport: str,
) -> list[dict[str, str]]:
    targets: list[dict[str, str]] = []
    seen_keys: set[str] = set()
    for row_meta in rows_meta:
        href = _normalize_gameslip_href(str(row_meta.get("href") or ""))
        if not href:
            continue
        query_values = _extract_gameslip_query_values(href)
        row_with_gm = dict(row_meta)
        row_with_gm["gmId"] = query_values["gm_id"]
        if not _row_matches_games_filters(row_with_gm, normalized_type, normalized_sport):
            continue
        canonical = _canonical_gameslip_href(href)
        if canonical in seen_keys:
            continue
        seen_keys.add(canonical)
        targets.append(
            {
                "href": href,
                "gm_id": query_values["gm_id"],
                "gm_ts": query_values["gm_ts"],
                "year": query_values["year"],
            }
        )
    return targets


async def _open_gameslip_detail_page(page: Page, href: str) -> bool:
    if not href:
        return False
    try:
        await page.goto(href, wait_until="networkidle", timeout=30000)
        return True
    except Exception:
        return False


def _detail_selector_candidates(gm_id: str) -> tuple[str, ...]:
    gm = str(gm_id or "").strip().upper()
    if gm in _GAMES_DETAIL_SELECTORS_BY_GMID:
        return _GAMES_DETAIL_SELECTORS_BY_GMID[gm]
    return _GAMES_DETAIL_SELECTORS_DEFAULT


async def _read_detail_selector_state(page: Page, selector: str) -> dict[str, Any] | None:
    try:
        state = await page.evaluate(
            """(selector) => {
                const root = document.querySelector(String(selector));
                if (!(root instanceof HTMLElement)) return null;
                const normalizeText = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
                const loadingNodes = root.querySelectorAll('.loading, [class*="loading"], [aria-busy="true"]');
                for (const node of loadingNodes) {
                    if (!(node instanceof HTMLElement)) continue;
                    const style = window.getComputedStyle(node);
                    const isVisible =
                        style.display !== 'none' &&
                        style.visibility !== 'hidden' &&
                        (node.offsetWidth > 0 || node.offsetHeight > 0 || node.getClientRects().length > 0);
                    if (isVisible) return { ready: false, rowCount: 0, signature: '' };
                }
                const rows = Array.from(root.querySelectorAll('tbody tr')).filter((row) => {
                    if (!(row instanceof HTMLElement)) return false;
                    return normalizeText(row.textContent || '').length > 0;
                });
                const rowCount = rows.length;
                const preview = rows.slice(0, 20).map((row) => normalizeText(row.textContent || '')).join(' || ').slice(0, 1400);
                return {
                    ready: rowCount > 0,
                    rowCount,
                    signature: `${selector}|${rowCount}|${preview}`,
                };
            }""",
            selector,
        )
    except Exception:
        return None
    if not isinstance(state, dict):
        return None
    return state


async def _resolve_detail_selector_fallback(page: Page) -> str | None:
    try:
        selector = await page.evaluate(
            """() => {
                const root = document.querySelector('#tabs-1') || document;
                const tables = Array.from(root.querySelectorAll('table'));
                let bestIndex = -1;
                let bestRows = 0;
                for (let i = 0; i < tables.length; i++) {
                    const rows = tables[i].querySelectorAll('tbody tr').length;
                    if (rows > bestRows) {
                        bestRows = rows;
                        bestIndex = i;
                    }
                }
                if (bestIndex < 0 || bestRows <= 0) return null;
                return root === document
                    ? `table:nth-of-type(${bestIndex + 1})`
                    : `#tabs-1 table:nth-of-type(${bestIndex + 1})`;
            }""",
        )
    except Exception:
        return None
    if not isinstance(selector, str):
        return None
    text = selector.strip()
    return text or None


async def _wait_for_games_detail_capture_selector(
    page: Page,
    gm_id: str,
    timeout_ms: int = _GAMES_DETAIL_WAIT_TIMEOUT_MS,
) -> str | None:
    timeout_ms = max(1, int(timeout_ms))
    deadline = asyncio.get_running_loop().time() + (timeout_ms / 1000)
    stable_hits = 0
    last_signature = ""
    last_selector: str | None = None

    selectors = list(_detail_selector_candidates(gm_id))
    fallback_selector = await _resolve_detail_selector_fallback(page)
    if fallback_selector:
        selectors.append(fallback_selector)

    while True:
        chosen_selector: str | None = None
        chosen_state: dict[str, Any] | None = None
        for selector in selectors:
            state = await _read_detail_selector_state(page, selector)
            if not isinstance(state, dict):
                continue
            if not bool(state.get("ready")):
                continue
            chosen_selector = selector
            chosen_state = state
            break

        if chosen_selector and chosen_state:
            signature = str(chosen_state.get("signature") or "").strip()
            if signature and signature == last_signature and chosen_selector == last_selector:
                stable_hits += 1
            else:
                stable_hits = 1
                last_signature = signature
                last_selector = chosen_selector
            if stable_hits >= _GAMES_DETAIL_STABLE_ROUNDS:
                return chosen_selector
        else:
            stable_hits = 0
            last_signature = ""
            last_selector = None

        remain = deadline - asyncio.get_running_loop().time()
        if remain <= 0:
            return None
        await asyncio.sleep(min(_GAMES_DETAIL_SAMPLE_INTERVAL_MS / 1000, remain))


async def _read_games_detail_visible_row_indices(page: Page, capture_selector: str) -> list[int]:
    try:
        raw_indices = await page.evaluate(
            """(selector) => {
                const root = document.querySelector(String(selector));
                if (!(root instanceof HTMLElement)) return [];
                const normalizeText = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
                const rows = Array.from(root.querySelectorAll('tbody tr'));
                const indices = [];
                for (let i = 0; i < rows.length; i++) {
                    const row = rows[i];
                    if (!(row instanceof HTMLElement)) continue;
                    const text = normalizeText(row.textContent || '');
                    if (!text) continue;
                    const style = window.getComputedStyle(row);
                    const hidden =
                        style.display === 'none' ||
                        style.visibility === 'hidden' ||
                        row.hidden ||
                        row.getAttribute('aria-hidden') === 'true';
                    if (!hidden) indices.push(i);
                }
                return indices;
            }""",
            capture_selector,
        )
    except Exception:
        return []
    if not isinstance(raw_indices, list):
        return []
    normalized: list[int] = []
    for item in raw_indices:
        try:
            value = int(item)
        except Exception:
            continue
        if value >= 0:
            normalized.append(value)
    return normalized


async def _set_games_detail_visible_rows(page: Page, capture_selector: str, visible_indices: list[int]) -> bool:
    try:
        applied = await page.evaluate(
            """({ selector, visibleIndices }) => {
                const root = document.querySelector(String(selector));
                if (!(root instanceof HTMLElement)) return false;
                const wanted = new Set((visibleIndices || []).map((value) => Number(value)));
                const rows = Array.from(root.querySelectorAll('tbody tr'));
                for (let i = 0; i < rows.length; i++) {
                    const row = rows[i];
                    if (!(row instanceof HTMLElement)) continue;
                    if (!row.hasAttribute('data-codex-prev-display')) {
                        row.setAttribute('data-codex-prev-display', row.style.display || '');
                    }
                    if (wanted.has(i)) {
                        const prev = row.getAttribute('data-codex-prev-display');
                        if (prev) row.style.display = prev;
                        else row.style.removeProperty('display');
                    } else {
                        row.style.display = 'none';
                    }
                }
                return true;
            }""",
            {"selector": capture_selector, "visibleIndices": visible_indices},
        )
    except Exception:
        return False
    return bool(applied)


async def _restore_games_detail_rows_visibility(page: Page, capture_selector: str) -> bool:
    try:
        restored = await page.evaluate(
            """(selector) => {
                const root = document.querySelector(String(selector));
                if (!(root instanceof HTMLElement)) return false;
                const rows = Array.from(root.querySelectorAll('tbody tr'));
                for (const row of rows) {
                    if (!(row instanceof HTMLElement)) continue;
                    const prev = row.getAttribute('data-codex-prev-display');
                    if (prev !== null) {
                        if (prev) row.style.display = prev;
                        else row.style.removeProperty('display');
                        row.removeAttribute('data-codex-prev-display');
                    }
                }
                return true;
            }""",
            capture_selector,
        )
    except Exception:
        return False
    return bool(restored)


async def _capture_games_detail_row_batches(
    page: Page,
    *,
    capture_selector: str,
    filename_prefix: str,
    gm_id: str,
    slots_left: int,
    rows_per_image: int = _GAMES_DETAIL_ROWS_PER_IMAGE,
) -> list[tuple[str, bytes]]:
    if slots_left <= 0:
        return []
    rows_per_image = max(1, int(rows_per_image))
    row_indices = await _read_games_detail_visible_row_indices(page, capture_selector)
    if not row_indices:
        return []

    logger.info(
        "games detail rows detected gm_id=%s selector=%s row_count=%d",
        gm_id or "-",
        capture_selector,
        len(row_indices),
    )

    locator = page.locator(capture_selector).first
    files: list[tuple[str, bytes]] = []
    restored = False
    try:
        starts = range(0, len(row_indices), rows_per_image)
        for part, start_index in enumerate(starts, start=1):
            if len(files) >= slots_left:
                break
            batch_indices = row_indices[start_index : start_index + rows_per_image]
            if not batch_indices:
                continue
            applied = await _set_games_detail_visible_rows(page, capture_selector, batch_indices)
            if not applied:
                logger.warning(
                    "games detail batch skipped reason=row_filter_apply_failed gm_id=%s selector=%s part=%d",
                    gm_id or "-",
                    capture_selector,
                    part,
                )
                break
            with contextlib.suppress(Exception):
                await page.wait_for_timeout(60)
            try:
                image = await locator.screenshot(type="jpeg", quality=80)
            except Exception:
                logger.warning(
                    "games detail batch skipped reason=batch_capture_failed gm_id=%s selector=%s part=%d",
                    gm_id or "-",
                    capture_selector,
                    part,
                )
                continue
            files.append((f"{filename_prefix}_p{part:02d}.jpg", image))
            logger.info(
                "games detail batch captured part=%d rows=%d gm_id=%s selector=%s",
                part,
                len(batch_indices),
                gm_id or "-",
                capture_selector,
            )
    finally:
        restored = await _restore_games_detail_rows_visibility(page, capture_selector)
        logger.info(
            "games detail rows restore done gm_id=%s selector=%s restored=%s",
            gm_id or "-",
            capture_selector,
            restored,
        )
    return files


async def _capture_games_detail_files_from_href(
    page: Page,
    *,
    href: str,
    gm_id: str,
    game_type: str,
    sport: str,
    seq: int,
    image_slots: int,
) -> list[tuple[str, bytes]]:
    if image_slots <= 0:
        return []

    opened = await _open_gameslip_detail_page(page, href)
    if not opened:
        logger.warning("games detail skipped reason=open_failed href=%s", href)
        return []
    logger.info("games detail open href=%s gm_id=%s", href, gm_id or "-")

    selector = await _wait_for_games_detail_capture_selector(page, gm_id=gm_id)
    if not selector:
        logger.warning("games detail skipped reason=list_root_not_found href=%s gm_id=%s", href, gm_id or "-")
        return []

    locator = page.locator(selector).first
    if await locator.count() <= 0:
        logger.warning(
            "games detail skipped reason=list_root_missing href=%s gm_id=%s selector=%s",
            href,
            gm_id or "-",
            selector,
        )
        return []

    base_name = f"games_{game_type}_{sport}_{(gm_id or 'unknown').lower()}_{seq:02d}"
    batch_images = await _capture_games_detail_row_batches(
        page,
        capture_selector=selector,
        filename_prefix=base_name,
        gm_id=gm_id,
        slots_left=image_slots,
        rows_per_image=_GAMES_DETAIL_ROWS_PER_IMAGE,
    )
    if batch_images:
        logger.info(
            "games detail captured gm_id=%s files=%d selector=%s mode=row_batches",
            gm_id or "-",
            len(batch_images),
            selector,
        )
        return batch_images

    try:
        image = await locator.screenshot(type="jpeg", quality=80)
        logger.info("games detail captured gm_id=%s files=1 selector=%s mode=single_fallback", gm_id or "-", selector)
        return [(f"{base_name}.jpg", image)]
    except Exception:
        logger.warning(
            "games detail skipped reason=capture_failed href=%s gm_id=%s selector=%s",
            href,
            gm_id or "-",
            selector,
        )
        return []


async def capture_sale_games_list_screenshots(
    page: Page,
    game_type: str,
    sport: str,
    max_images: int | None = None,
) -> GamesCaptureResult:
    normalized_type = normalize_games_capture_game_type(game_type)
    normalized_sport = normalize_games_capture_sport(sport)
    now_text = datetime.now(KST).strftime("%Y.%m.%d %H:%M:%S")
    image_limit = _GAMES_CAPTURE_MAX_IMAGES if max_images is None else max(1, min(int(max_images), 60))

    await _navigate_to_buyable_game_list(page)
    with contextlib.suppress(Exception):
        await page.wait_for_load_state("networkidle", timeout=2500)

    table_targets = await _resolve_games_table_targets(page)
    if not table_targets:
        return GamesCaptureResult(
            fetched_at=now_text,
            game_type=normalized_type,
            sport=normalized_sport,
            files=[],
            captured_count=0,
            truncated=False,
        )

    state = await _wait_for_games_tables_stable(
        page,
        table_selectors=[item["table_selector"] for item in table_targets],
    )
    if not isinstance(state, dict):
        logger.warning(
            "games capture list_not_ready: game_type=%s sport=%s table_count=%d",
            normalized_type,
            normalized_sport,
            len(table_targets),
        )
        return GamesCaptureResult(
            fetched_at=now_text,
            game_type=normalized_type,
            sport=normalized_sport,
            files=[],
            captured_count=0,
            truncated=False,
        )

    if int(state.get("rowCount") or 0) <= 0:
        logger.warning(
            "games capture no_rows: game_type=%s sport=%s table_count=%d",
            normalized_type,
            normalized_sport,
            len(table_targets),
        )
        return GamesCaptureResult(
            fetched_at=now_text,
            game_type=normalized_type,
            sport=normalized_sport,
            files=[],
            captured_count=0,
            truncated=False,
        )

    all_rows_meta: list[dict[str, Any]] = []
    for table_target in table_targets:
        table_key = table_target["table_key"]
        table_selector = table_target["table_selector"]
        rows_meta = await _collect_games_rows_meta(page, table_selector)
        logger.info(
            "games buyable rows loaded: table=%s total_rows=%d",
            table_key,
            len(rows_meta),
        )
        all_rows_meta.extend(rows_meta)

    gameslip_targets = _select_gameslip_targets(
        all_rows_meta,
        normalized_type=normalized_type,
        normalized_sport=normalized_sport,
    )
    logger.info(
        "games capture targets selected: type=%s sport=%s matched_rows=%d target_links=%d",
        normalized_type,
        normalized_sport,
        len(all_rows_meta),
        len(gameslip_targets),
    )

    files: list[tuple[str, bytes]] = []
    truncated = False
    for seq, target in enumerate(gameslip_targets, start=1):
        if len(files) >= image_limit:
            truncated = True
            break
        slots_left = image_limit - len(files)
        captured_files = await _capture_games_detail_files_from_href(
            page,
            href=target["href"],
            gm_id=target["gm_id"],
            game_type=normalized_type,
            sport=normalized_sport,
            seq=seq,
            image_slots=slots_left,
        )
        if not captured_files:
            continue
        files.extend(captured_files[:slots_left])
        if len(files) >= image_limit:
            truncated = True
            break

    if not files:
        logger.warning("games capture no_rows: game_type=%s sport=%s", normalized_type, normalized_sport)

    logger.info(
        "games capture result: game_type=%s sport=%s target_links=%d captured_files=%d truncated=%s",
        normalized_type,
        normalized_sport,
        len(gameslip_targets),
        len(files),
        truncated,
    )

    return GamesCaptureResult(
        fetched_at=now_text,
        game_type=normalized_type,
        sport=normalized_sport,
        files=files,
        captured_count=len(files),
        truncated=truncated,
    )
