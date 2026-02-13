from __future__ import annotations

import calendar
import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from playwright.async_api import Page

from src.models import BetSlip, MatchBet

logger = logging.getLogger(__name__)

_PURCHASE_HISTORY_PATHS = [
    "/main/mainPage/mypage/myPurchaseWinList.do",
    "/main/mainPage/mypage/gameBuyList.do",
    "/main/mainPage/mypage/gameBuyListPop.do",
    "/mypage/gameBuyList.do",
]

KST = timezone(timedelta(hours=9))

_ITEM_CODES = {
    "SC": "축구",
    "BK": "농구",
    "BB": "야구",
    "VB": "배구",
    "GF": "골프",
}

_MARK_LABELS = {
    "1": "승",
    "2": "무",
    "3": "패",
}


def _pick(data: dict[str, Any], keys: list[str], default: Any = "") -> Any:
    for key in keys:
        value = data.get(key)
        if value is not None:
            return value
    return default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        text = str(value)
        digits = re.sub(r"[^0-9-]", "", text)
        return int(digits) if digits else default
    except Exception:
        return default


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        text = str(value)
        cleaned = re.sub(r"[^0-9.-]", "", text)
        return float(cleaned) if cleaned else default
    except Exception:
        return default


def _strip_html(text: str) -> str:
    no_tag = re.sub(r"<[^>]*>", "", text)
    no_br = no_tag.replace("\r", " ").replace("\n", " ")
    return re.sub(r"\s+", " ", no_br).strip()


def _parse_dt_for_sort(raw: str) -> datetime:
    candidates = [
        "%Y-%m-%d %H:%M",
        "%Y.%m.%d %H:%M",
        "%y.%m.%d %H:%M",
        "%Y-%m-%d",
        "%Y.%m.%d",
        "%y.%m.%d",
    ]
    text = re.sub(r"\(.*?\)", "", (raw or "")).strip()
    for fmt in candidates:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return datetime.min


def _format_buy_datetime(raw: Any) -> str:
    if raw is None:
        return ""

    text = str(raw).strip()
    digits = re.sub(r"[^0-9]", "", text)

    if len(digits) >= 12:
        yyyy = digits[0:4]
        mm = digits[4:6]
        dd = digits[6:8]
        hh = digits[8:10]
        mi = digits[10:12]
        return f"{yyyy}.{mm}.{dd} {hh}:{mi}"

    # Already formatted text
    return text


def _subtract_years(date_value: datetime, years: int) -> datetime:
    year = date_value.year - years
    last_day = calendar.monthrange(year, date_value.month)[1]
    day = min(date_value.day, last_day)
    return date_value.replace(year=year, day=day)


def _recent5_range_ymd(now: datetime | None = None) -> tuple[str, str]:
    base = now.astimezone(KST) if now else datetime.now(KST)
    start = _subtract_years(base, 5)
    return start.strftime("%Y%m%d"), base.strftime("%Y%m%d")


def _status_result_from_buy_status_info(
    buy_status_code: int,
    buy_status_name: str,
) -> tuple[str, str | None]:
    if buy_status_code == 1:
        return "구매예약중", None
    if buy_status_code == 2:
        return "경기취소", None
    if buy_status_code == 3:
        return "발매중", None
    if buy_status_code == 4:
        return "발매마감", None
    if buy_status_code == 5:
        return "적중", "적중"
    if buy_status_code in {6, 7}:
        return "적중안됨", "미적중"

    status_text = _strip_html(buy_status_name)
    if "적중안됨" in status_text or "미적중" in status_text:
        return "적중안됨", "미적중"
    if "적중" in status_text:
        return "적중", "적중"
    if "발매중" in status_text:
        return "발매중", None
    if "발매마감" in status_text:
        return "발매마감", None
    if "구매예약" in status_text:
        return "구매예약중", None
    if "취소" in status_text:
        return "경기취소", None

    return "", None


def _status_result_from_list_item(item: dict[str, Any]) -> tuple[str, str | None]:
    buy_status_code = _to_int(item.get("buyStatusCode"), -1)
    buy_status_name = str(_pick(item, ["buyStatusName", "statusNm", "statusName"], "")).strip()

    status, result = _status_result_from_buy_status_info(buy_status_code, buy_status_name)
    if status:
        return status, result

    btk_num = str(item.get("btkNum") or "").strip()
    buy_prgs = str(item.get("buyPrgsStCd") or "").strip()
    proc_result = str(item.get("procRsltClCd") or "").strip()
    gm_status = str(item.get("gmStCd") or "").strip()

    if not btk_num:
        return "구매예약중", None
    if buy_prgs == "3":
        return "경기취소", None
    if gm_status == "2":
        return "발매중", None

    # Winner branch from site JS: closed game and processed purchase.
    if gm_status in {"3", "4", "52"} and buy_prgs == "2" and proc_result in {"1", "2", "3", "4", "5", "6", "7", "8"}:
        return "적중", "적중"

    if buy_prgs == "2" and proc_result == "0":
        return "발매마감", None

    if proc_result and proc_result != "0":
        return "적중안됨", "미적중"

    return "발매마감", None


def _normalize_match_result_exact(raw: Any) -> str | None:
    value = str(raw or "").strip().lower()
    if not value:
        return None

    if value in {"win", "w", "hit", "true", "y", "적중"}:
        return "적중"
    if value in {"lose", "loss", "fail", "miss", "l", "n", "false", "미적중", "적중안됨"}:
        return "미적중"
    return None


def _game_result_from_score_or_code(score: str, code: Any) -> str:
    # Score is the most reliable source.
    if ":" in score:
        home_score, away_score = score.split(":", maxsplit=1)
        try:
            hi = int(home_score.strip())
            ai = int(away_score.strip())
            if hi > ai:
                return "승"
            if hi == ai:
                return "무"
            return "패"
        except ValueError:
            pass

    mapping = {
        "1": "승",
        "2": "패",
        "3": "무",
        "H": "승",
        "A": "패",
        "D": "무",
        "HOME": "승",
        "AWAY": "패",
        "DRAW": "무",
    }
    return mapping.get(str(code or "").strip().upper(), "")


def _next_start_row(current: int, returned_count: int) -> int:
    return current + max(returned_count, 0)


def _merge_slip(base: BetSlip, incoming: BetSlip) -> BetSlip:
    if not base.game_type and incoming.game_type:
        base.game_type = incoming.game_type
    if not base.round_number and incoming.round_number:
        base.round_number = incoming.round_number
    if not base.status and incoming.status:
        base.status = incoming.status
    if not base.purchase_datetime and incoming.purchase_datetime:
        base.purchase_datetime = incoming.purchase_datetime
    if base.total_amount == 0 and incoming.total_amount:
        base.total_amount = incoming.total_amount
    if base.potential_payout == 0 and incoming.potential_payout:
        base.potential_payout = incoming.potential_payout
    if base.combined_odds == 0 and incoming.combined_odds:
        base.combined_odds = incoming.combined_odds
    if (not base.result) and incoming.result:
        base.result = incoming.result
    if base.actual_payout == 0 and incoming.actual_payout:
        base.actual_payout = incoming.actual_payout
    if len(incoming.matches) > len(base.matches):
        base.matches = incoming.matches
    return base


def _list_item_to_slip(item: dict[str, Any]) -> tuple[BetSlip | None, dict[str, str] | None]:
    slip_id = str(_pick(item, ["btkNum", "buyNo", "slipId"], "")).strip()
    if not slip_id:
        return None, None

    status, result = _status_result_from_list_item(item)

    game_name = str(_pick(item, ["gmNm", "gameNm", "gameName"], "")).strip()
    round_number = str(_pick(item, ["gmOsidTs", "roundNo", "round"], "")).strip()

    slip = BetSlip(
        slip_id=slip_id,
        game_type=game_name,
        round_number=f"{round_number}회차" if round_number and "회" not in round_number else round_number,
        status=status,
        purchase_datetime=_format_buy_datetime(_pick(item, ["buyDtm", "buyDt", "purchaseDate"], "")),
        total_amount=_to_int(_pick(item, ["buyAmt", "totalBuyAmount", "amount"], 0), 0),
        potential_payout=0,
        combined_odds=0.0,
        result=result,
        actual_payout=0,
        matches=[],
    )

    detail = {
        "btkNum": slip_id,
        "purchaseNo": str(_pick(item, ["buyCartSn", "purchaseNo"], "")).strip(),
        "gmId": str(_pick(item, ["gmId"], "")).strip(),
        "gmTs": str(_pick(item, ["gmTs", "gmOsidTs"], "")).strip(),
    }
    return slip, detail


def _extract_purchase_items(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return []

    for key in ("purchaseWin", "list", "data", "result", "items", "buyList", "gameList"):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]

    body = data.get("body")
    if isinstance(body, dict):
        for key in ("purchaseWin", "list", "data", "result", "items"):
            value = body.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]

    return []


def _build_recent_purchases_token_from_items(items: list[dict[str, Any]], limit: int = 5) -> str:
    rows: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "btkNum": str(item.get("btkNum") or "").strip(),
                "buyDtm": str(item.get("buyDtm") or "").strip(),
                "buyStatusCode": str(item.get("buyStatusCode") or "").strip(),
                "buyAmt": str(item.get("buyAmt") or "").strip(),
                "procRsltClCd": str(item.get("procRsltClCd") or "").strip(),
                "gmStCd": str(item.get("gmStCd") or "").strip(),
            }
        )

    rows.sort(
        key=lambda row: (
            _parse_dt_for_sort(_format_buy_datetime(row["buyDtm"])),
            row["btkNum"],
        ),
        reverse=True,
    )
    selected = rows[: max(1, limit)]
    raw = json.dumps(selected, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _dismiss_popups(page: Page) -> None:
    await page.evaluate(
        """() => {
            document.querySelectorAll('.ui-dialog-content').forEach(el => {
                try { $(el).dialog('close'); } catch(e) {}
            });
            document.querySelectorAll('.ui-widget-overlay, .ui-dialog-overlay').forEach(el => el.remove());
        }"""
    )


async def navigate_to_purchase_history(page: Page) -> None:
    await _dismiss_popups(page)

    selectors = [
        'a:has-text("구매/적중내역")',
        'a:has-text("구매내역")',
        'a:has-text("게임구매내역")',
        'a[href*="PurchaseWin"]',
        'a[href*="gameBuyList"]',
    ]

    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=1500):
                await loc.click()
                await page.wait_for_load_state("networkidle", timeout=12000)
                return
        except Exception:
            continue

    for path in _PURCHASE_HISTORY_PATHS:
        try:
            await page.evaluate(f"movePageUrl('{path}')")
            await page.wait_for_load_state("networkidle", timeout=12000)
            return
        except Exception:
            continue

    base = "https://www.betman.co.kr"
    for path in _PURCHASE_HISTORY_PATHS:
        try:
            await page.goto(f"{base}{path}", wait_until="networkidle", timeout=25000)
            return
        except Exception:
            continue

    raise RuntimeError("구매내역 페이지로 이동하지 못했습니다.")


async def _request_post_method(page: Page, endpoint: str, params: dict[str, Any]) -> Any:
    return await page.evaluate(
        """({endpoint, params}) => {
            return new Promise((resolve) => {
                const timeout = setTimeout(() => resolve({__timeout: true}), 20000);
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


async def _get_base_search_params(page: Page) -> dict[str, Any]:
    result = await page.evaluate(
        """() => {
            try {
                if (typeof getSearchCondValues === 'function') {
                    return getSearchCondValues() || {};
                }
            } catch (e) {}
            return {};
        }"""
    )
    return result if isinstance(result, dict) else {}


async def probe_recent_purchases_token(page: Page, limit: int = 5) -> str:
    limit = max(1, min(limit, 30))
    await navigate_to_purchase_history(page)
    await page.wait_for_function(
        """() => typeof requestClient !== 'undefined' && typeof requestClient.requestPostMethod === 'function'""",
        timeout=10000,
    )

    base_params = await _get_base_search_params(page)
    params = _build_search_params(start_row=1, page_cnt=max(limit, 5), base_params=base_params)
    result = await _request_post_method(page, "/mypgPurWin/getGameList.do", params)

    if not isinstance(result, dict):
        raise RuntimeError("purchase probe failed: list api returned non-dict")
    if result.get("__error"):
        raise RuntimeError(f"purchase probe failed: {result.get('__error')}")
    if result.get("__timeout"):
        raise RuntimeError("purchase probe failed: timeout")

    items = _extract_purchase_items(result)
    token = _build_recent_purchases_token_from_items(items, limit=limit)
    logger.info("purchase probe token generated: limit=%d items=%d", limit, len(items))
    return token


def _build_search_params(
    start_row: int,
    page_cnt: int,
    base_params: dict[str, Any],
) -> dict[str, Any]:
    now = datetime.now(KST)
    start_date, end_date = _recent5_range_ymd(now)

    params = dict(base_params)
    params.update(
        {
            "orderColumn": "buyDtm",
            "orderDir": "desc",
            "gmIds": str(base_params.get("gmIds") or ""),
            "startDate": start_date,
            "endDate": end_date,
            "stAllYn": "Y",
            "stSaleYn": "N",
            "stCloseYn": "N",
            "stWinYn": "N",
            "stLoseYn": "N",
            "stCancelYn": "N",
            "stReserveYn": "N",
            "stPayoYn": "N",
            "startRow": start_row,
            "pageCnt": page_cnt,
        }
    )
    return params


def _dedup_and_merge(slips: list[BetSlip]) -> list[BetSlip]:
    merged: dict[str, BetSlip] = {}
    for slip in slips:
        existing = merged.get(slip.slip_id)
        if existing is None:
            merged[slip.slip_id] = slip
        else:
            merged[slip.slip_id] = _merge_slip(existing, slip)
    return list(merged.values())


async def _collect_slips_via_list_api(
    page: Page,
    limit: int,
) -> tuple[list[BetSlip], dict[str, dict[str, str]], dict[str, Any]]:
    await page.wait_for_function(
        """() => typeof requestClient !== 'undefined' && typeof requestClient.requestPostMethod === 'function'""",
        timeout=10000,
    )

    base_params = await _get_base_search_params(page)
    start_row = 1
    page_cnt = 5

    merged: dict[str, BetSlip] = {}
    detail_params: dict[str, dict[str, str]] = {}

    diagnostics: dict[str, Any] = {
        "pages": 0,
        "api_ok": True,
        "fail_reason": "",
    }

    max_calls = 12
    for _ in range(max_calls):
        if len(merged) >= limit:
            break

        params = _build_search_params(start_row, page_cnt, base_params)
        result = await _request_post_method(page, "/mypgPurWin/getGameList.do", params)
        diagnostics["pages"] += 1

        if not isinstance(result, dict):
            diagnostics["api_ok"] = False
            diagnostics["fail_reason"] = "list api returned non-dict"
            break
        if result.get("__error"):
            diagnostics["api_ok"] = False
            diagnostics["fail_reason"] = str(result["__error"])
            break
        if result.get("__timeout"):
            diagnostics["api_ok"] = False
            diagnostics["fail_reason"] = "list api timeout"
            break

        items = _extract_purchase_items(result)
        total_cnt = 0
        if items and isinstance(items[0], dict):
            total_cnt = _to_int(items[0].get("totalCnt"), 0)

        logger.info(
            "getGameList page=%d startRow=%d pageCnt=%d returned=%d total=%d",
            diagnostics["pages"],
            start_row,
            page_cnt,
            len(items),
            total_cnt,
        )

        if not items:
            break

        for item in items:
            slip, detail = _list_item_to_slip(item)
            if slip is None:
                continue
            existing = merged.get(slip.slip_id)
            if existing is None:
                merged[slip.slip_id] = slip
            else:
                merged[slip.slip_id] = _merge_slip(existing, slip)

            if detail and detail.get("btkNum"):
                detail_params[slip.slip_id] = detail

        returned_count = len(items)
        if returned_count < page_cnt:
            break

        start_row = _next_start_row(start_row, returned_count)
        if total_cnt and start_row > total_cnt:
            break

    slips = list(merged.values())
    slips.sort(key=lambda s: _parse_dt_for_sort(s.purchase_datetime), reverse=True)
    return slips[:limit], detail_params, diagnostics


def _parse_dom_status(joined_text: str) -> tuple[str, str | None]:
    if "적중안됨" in joined_text or "미적중" in joined_text:
        return "적중안됨", "미적중"
    if "적중" in joined_text:
        return "적중", "적중"
    if "발매중" in joined_text:
        return "발매중", None
    if "발매마감" in joined_text:
        return "발매마감", None
    if "구매예약" in joined_text:
        return "구매예약중", None
    if "취소" in joined_text:
        return "경기취소", None
    return "", None


async def _parse_dom_slips(page: Page, limit: int) -> list[BetSlip]:
    rows = page.locator("#purchaseWinTable tbody tr")
    count = await rows.count()
    if count == 0:
        rows = page.locator("table tbody tr")
        count = await rows.count()

    slips: list[BetSlip] = []
    for i in range(count):
        row = rows.nth(i)
        cells = row.locator("td")
        cell_count = await cells.count()
        if cell_count < 4:
            continue

        texts = [t.strip() for t in await cells.all_text_contents() if t.strip()]
        joined = " | ".join(texts)

        slip_id = ""
        if cell_count >= 4:
            slip_id = (await cells.nth(3).text_content() or "").strip()
        if not slip_id:
            m = re.search(r"[A-Z0-9]{4,}(?:-[A-Z0-9]{2,})+|\d{8,}", joined)
            slip_id = m.group(0) if m else f"dom-{i+1}"

        status, result = _parse_dom_status(joined)

        amount_match = re.search(r"([\d,]+)\s*원?", joined)
        slips.append(
            BetSlip(
                slip_id=slip_id,
                game_type=texts[1] if len(texts) > 1 else (texts[0] if texts else ""),
                round_number="",
                status=status,
                purchase_datetime=texts[2] if len(texts) > 2 else "",
                total_amount=_to_int(amount_match.group(1) if amount_match else 0, 0),
                potential_payout=0,
                combined_odds=0.0,
                result=result,
                actual_payout=0,
                matches=[],
            )
        )

    slips = _dedup_and_merge(slips)
    slips.sort(key=lambda s: _parse_dt_for_sort(s.purchase_datetime), reverse=True)
    return slips[:limit]


def _parse_game_detail(data: dict[str, Any]) -> tuple[list[MatchBet], dict[str, Any]]:
    matches: list[MatchBet] = []
    meta: dict[str, Any] = {}

    purchase = data.get("purchase", {}) if isinstance(data, dict) else {}
    if not isinstance(purchase, dict):
        return matches, meta

    buy_status_code = _to_int(purchase.get("buyStatusCode"), -1)
    buy_status_name = str(purchase.get("buyStatusName") or "")
    status, result = _status_result_from_buy_status_info(buy_status_code, buy_status_name)
    if status:
        meta["status"] = status
    if result:
        meta["result"] = result

    buy_amount = purchase.get("buyAmount") or {}
    if isinstance(buy_amount, dict):
        total_buy = _to_int(_pick(buy_amount, ["totalBuyAmount", "buyAmt"], 0), 0)
        if total_buy > 0:
            meta["total_amount"] = total_buy

    winning = purchase.get("winning") or {}
    if isinstance(winning, dict):
        winning_amount = _to_int(_pick(winning, ["winningAmount", "winningAmountTax"], 0), 0)
        if winning_amount > 0:
            meta["actual_payout"] = winning_amount
        winning_status = str(winning.get("winningStatus") or "").strip().lower()
        if not meta.get("result"):
            if winning_status in {"win", "success", "2"}:
                meta["result"] = "적중"
            elif winning_status in {"fail", "lose", "miss", "3"}:
                meta["result"] = "미적중"

    sports_lottery = purchase.get("sportsLottery") or {}
    if isinstance(sports_lottery, dict):
        combined_odds = _to_float(_pick(sports_lottery, ["protoVicTotalAllot", "totalAllot"], 0.0), 0.0)
        if combined_odds > 0:
            meta["combined_odds"] = combined_odds

    if meta.get("total_amount") and meta.get("combined_odds"):
        meta["potential_payout"] = int(float(meta["total_amount"]) * float(meta["combined_odds"]))

    marking_data = data.get("markingData") or {}
    if not isinstance(marking_data, dict):
        return matches, meta

    schedule_set = marking_data.get("slipPaperAndScheduleSetList") or []
    if not isinstance(schedule_set, list):
        return matches, meta

    for idx, item in enumerate(schedule_set, start=1):
        if not isinstance(item, dict):
            continue

        sched = item.get("schedule") or {}
        slip_paper = item.get("slipPaper") or {}
        if not isinstance(sched, dict) or not isinstance(slip_paper, dict):
            continue

        match_number = _to_int(_pick(sched, ["matchSeq", "matchNo"], idx), idx)
        sport_code = str(_pick(sched, ["itemCode"], "")).strip()
        sport = _ITEM_CODES.get(sport_code, sport_code)

        league = str(_pick(sched, ["leagueName", "leagueNm"], "")).strip()
        home_team = str(_pick(sched, ["homeName", "homeTeamNm"], "")).strip()
        away_team = str(_pick(sched, ["awayName", "awayTeamNm"], "")).strip()

        mark_info = slip_paper.get("markInfo") if isinstance(slip_paper.get("markInfo"), list) else []
        bet_selection = ""
        if len(mark_info) >= 2:
            bet_selection = _MARK_LABELS.get(str(mark_info[1]), str(mark_info[1]))
        if not bet_selection:
            bet_selection = str(_pick(slip_paper, ["markName", "selectNm"], "")).strip()

        odds = _to_float(_pick(slip_paper, ["allot", "odds"], 0.0), 0.0)

        game_date = _pick(sched, ["gameDate", "gameDt"], "")
        match_datetime = ""
        if isinstance(game_date, (int, float)) and game_date > 0:
            match_datetime = datetime.fromtimestamp(game_date / 1000, tz=KST).strftime("%Y.%m.%d %H:%M")
        elif isinstance(game_date, str):
            match_datetime = game_date

        score = str(_pick(sched, ["mchScore", "score"], "")).strip()
        game_result = _game_result_from_score_or_code(score, _pick(sched, ["gameResult"], ""))

        # Important: only trust explicit winStatus for per-match hit/miss.
        match_result = _normalize_match_result_exact(_pick(slip_paper, ["winStatus", "result"], ""))

        matches.append(
            MatchBet(
                match_number=match_number,
                sport=sport,
                league=league,
                home_team=home_team,
                away_team=away_team,
                bet_selection=bet_selection,
                odds=odds,
                match_datetime=match_datetime,
                result=match_result,
                score=score,
                game_result=game_result,
            )
        )

    return matches, meta


async def _fetch_match_details_for_slips(
    page: Page,
    slips: list[BetSlip],
    detail_params_by_slip: dict[str, dict[str, str]],
) -> tuple[int, int]:
    entries: list[tuple[int, dict[str, str]]] = []

    for idx, slip in enumerate(slips):
        params = detail_params_by_slip.get(slip.slip_id)
        if not params:
            continue
        if not params.get("btkNum"):
            continue
        entries.append((idx, params))

    if not entries:
        return 0, 0

    params_list = [params for _, params in entries]

    raw_results = await page.evaluate(
        """(paramsList) => {
            return Promise.all(paramsList.map((params) => {
                return new Promise((resolve) => {
                    const timeout = setTimeout(() => resolve(null), 20000);
                    try {
                        if (typeof requestClient !== 'undefined' && requestClient.requestPostMethod) {
                            requestClient.requestPostMethod('/mypgPurWin/getGameDetail.do', params, true, function(data) {
                                clearTimeout(timeout);
                                try {
                                    resolve(JSON.stringify(data));
                                } catch (e) {
                                    resolve(null);
                                }
                            });
                        } else {
                            clearTimeout(timeout);
                            resolve(null);
                        }
                    } catch (e) {
                        clearTimeout(timeout);
                        resolve(null);
                    }
                });
            }));
        }""",
        params_list,
    )

    success = 0
    failed = 0

    for (idx, _params), raw in zip(entries, raw_results):
        if not raw:
            failed += 1
            continue

        try:
            parsed = json.loads(raw)
            matches, meta = _parse_game_detail(parsed)
            slip = slips[idx]

            if matches:
                slip.matches = matches
            if meta.get("status"):
                slip.status = str(meta["status"])
            if meta.get("result"):
                slip.result = str(meta["result"])
            if meta.get("actual_payout"):
                slip.actual_payout = int(meta["actual_payout"])
            if meta.get("combined_odds"):
                slip.combined_odds = float(meta["combined_odds"])
            if meta.get("potential_payout"):
                slip.potential_payout = int(meta["potential_payout"])
            if meta.get("total_amount") and slip.total_amount == 0:
                slip.total_amount = int(meta["total_amount"])

            success += 1
        except Exception as exc:
            failed += 1
            logger.debug("Failed to parse detail response for %s: %s", slips[idx].slip_id, exc)

    return success, failed


async def scrape_purchase_history(
    page: Page,
    limit: int,
) -> list[BetSlip]:
    limit = max(1, min(limit, 30))

    await navigate_to_purchase_history(page)

    # Source 1: official list API (most accurate)
    try:
        slips, detail_params, diagnostics = await _collect_slips_via_list_api(page, limit)
        if slips:
            success, failed = await _fetch_match_details_for_slips(page, slips, detail_params)
            logger.info(
                "purchase scrape api items=%d list_pages=%d detail_success=%d detail_failed=%d",
                len(slips),
                diagnostics.get("pages", 0),
                success,
                failed,
            )
            slips.sort(key=lambda s: _parse_dt_for_sort(s.purchase_datetime), reverse=True)
            return slips[:limit]

        if not diagnostics.get("api_ok", True):
            logger.warning("list api unavailable, fallback to DOM: %s", diagnostics.get("fail_reason", "unknown"))
        else:
            logger.info("list api returned 0 items, fallback to DOM")
    except Exception as exc:
        logger.warning("list api scrape failed, fallback to DOM: %s", exc)

    # Source 2: DOM fallback
    dom_slips = await _parse_dom_slips(page, limit)
    if not dom_slips:
        logger.info("DOM fallback found no purchase rows")
    return dom_slips
