from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from playwright.async_api import Page

from src.models import SaleGameMatch, SaleGamesSnapshot

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
