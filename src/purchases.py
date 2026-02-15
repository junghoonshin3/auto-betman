from __future__ import annotations

import asyncio
import calendar
import contextlib
import hashlib
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from playwright.async_api import Page, Response, TimeoutError as PlaywrightTimeoutError

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

_PAPER_STABLE_TIMEOUT_MS = 3000
_PAPER_STABLE_ROUNDS = 2
_PAPER_STABLE_SAMPLE_INTERVAL_MS = 350
_GAME_DETAIL_ENDPOINT_TOKEN = "/mypgPurWin/getGameDetail.do"
_OPEN_GAME_PAPER_CALL_PATTERN = re.compile(r"openGamePaper\s*\((.*?)\)", re.IGNORECASE | re.DOTALL)
_OPEN_GAME_PAPER_ARG_TOKEN_PATTERN = re.compile(r"""'([^']*)'|"([^"]*)"|([^,\s()]+)""")
_SLIP_ID_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9]{2,}(?:-[A-Za-z0-9]{2,})+")


class PurchaseSnapshotResult(dict):
    files: list[tuple[str, bytes]]
    attempted_count: int
    success_count: int
    failed_count: int
    exact_success_count: int
    fallback_success_count: int


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


async def capture_purchase_history_snapshot(page: Page) -> bytes:
    await navigate_to_purchase_history(page)

    selectors = [
        "#purchaseWinTable",
        "#purchaseWinTable tbody",
        "table:has(tbody tr)",
    ]
    for selector in selectors:
        try:
            locator = page.locator(selector)
            if await locator.count() <= 0:
                continue
            return await locator.first.screenshot(type="png")
        except Exception:
            continue

    return await page.screenshot(type="png", full_page=False)


def _sanitize_slip_id_for_filename(slip_id: str) -> str:
    safe = re.sub(r"[^0-9A-Za-z_-]+", "_", str(slip_id or "").strip())
    safe = safe.strip("_")
    return safe or "unknown"


def _normalize_slip_token(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "", str(value or "").strip().upper())


def _extract_open_game_paper_args(text: str) -> list[str]:
    source = str(text or "")
    if "openGamePaper" not in source:
        return []

    args: list[str] = []
    for match in _OPEN_GAME_PAPER_CALL_PATTERN.finditer(source):
        raw = str(match.group(1) or "")
        if not raw:
            continue
        for token_match in _OPEN_GAME_PAPER_ARG_TOKEN_PATTERN.finditer(raw):
            candidate = ""
            for group in token_match.groups():
                if group is not None:
                    candidate = str(group)
                    break
            candidate = candidate.strip()
            if candidate:
                args.append(candidate)
    return args


def _pick_best_slip_id_candidate(args: list[str], text: str = "") -> str:
    ignored_tokens = {"this", "undefined", "null", "none", "true", "false"}

    for raw in args:
        candidate = str(raw or "").strip().strip("'").strip('"')
        if not candidate:
            continue
        if candidate.lower() in ignored_tokens:
            continue
        if _SLIP_ID_TOKEN_PATTERN.fullmatch(candidate):
            return candidate

    for raw in args:
        candidate = str(raw or "").strip()
        if not candidate:
            continue
        match = _SLIP_ID_TOKEN_PATTERN.search(candidate)
        if match:
            return str(match.group(0))

    text_match = _SLIP_ID_TOKEN_PATTERN.search(str(text or ""))
    if text_match:
        return str(text_match.group(0))
    return ""


def _extract_open_game_paper_slip_id_from_text(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    args = _extract_open_game_paper_args(text)
    return _pick_best_slip_id_candidate(args, text)


def _extract_btk_num_from_text(value: str) -> str:
    text = str(value or "")
    if not text:
        return ""

    patterns = (
        re.compile(r'["\']?btkNum["\']?\s*[:=]\s*["\']([^"\']+)["\']', re.IGNORECASE),
        re.compile(r"btkNum=([A-Za-z0-9-]+)", re.IGNORECASE),
    )
    for pattern in patterns:
        match = pattern.search(text)
        if match and match.group(1):
            return str(match.group(1)).strip()
    from_open_game_paper = _extract_open_game_paper_slip_id_from_text(text)
    if from_open_game_paper:
        return from_open_game_paper
    return ""


def _code_matches_target_slip(code: str, target_slip_id: str) -> bool:
    normalized_target = _normalize_slip_token(target_slip_id)
    if not normalized_target:
        return True
    text = str(code or "")
    if not text:
        return False

    extracted = _extract_open_game_paper_slip_id_from_text(text)
    if extracted and _normalize_slip_token(extracted) == normalized_target:
        return True
    return normalized_target in _normalize_slip_token(text)


async def _extract_open_game_paper_slip_id_from_row(row: Any) -> str:
    for attr_name in ("onclick", "href"):
        try:
            value = str(await row.get_attribute(attr_name) or "").strip()
        except Exception:
            value = ""
        extracted = _extract_open_game_paper_slip_id_from_text(value)
        if extracted:
            return extracted

    selectors = (
        '[onclick*="openGamePaper"]',
        'a[href*="openGamePaper"]',
        'a:has-text("투표지")',
        'button:has-text("투표지")',
    )
    for selector in selectors:
        try:
            triggers = row.locator(selector)
            trigger_count = await triggers.count()
        except Exception:
            continue
        for idx in range(min(trigger_count, 5)):
            trigger = triggers.nth(idx)
            for attr_name in ("onclick", "href"):
                try:
                    value = str(await trigger.get_attribute(attr_name) or "").strip()
                except Exception:
                    value = ""
                extracted = _extract_open_game_paper_slip_id_from_text(value)
                if extracted:
                    return extracted
    return ""


async def _extract_slip_id_from_row(row: Any, row_index: int) -> str:
    cells = row.locator("td")
    cell_count = await cells.count()
    texts = [t.strip() for t in await cells.all_text_contents() if t and t.strip()]
    joined = " | ".join(texts)

    slip_id = ""
    if cell_count >= 4:
        slip_id = (await cells.nth(3).text_content() or "").strip()
    if not slip_id:
        m = re.search(r"[A-Z0-9]{4,}(?:-[A-Z0-9]{2,})+|\d{8,}", joined)
        slip_id = m.group(0) if m else f"dom-{row_index + 1}"
    return slip_id


async def _extract_btk_num_from_network_response(response: Response) -> str:
    candidates: list[str] = []
    try:
        candidates.append(str(response.url or ""))
    except Exception:
        pass
    try:
        request = response.request
    except Exception:
        request = None

    if request is not None:
        try:
            candidates.append(str(request.url or ""))
        except Exception:
            pass
        try:
            candidates.append(str(request.post_data or ""))
        except Exception:
            pass

    try:
        body = await response.text()
        candidates.append(str(body or ""))
    except Exception:
        pass

    for text in candidates:
        extracted = _extract_btk_num_from_text(text)
        if extracted:
            return extracted
    return ""


async def _open_game_paper_for_row(
    page: Page,
    row: Any,
    slip_id: str = "",
    *,
    request_id: str = "",
    discord_user_id: str = "",
) -> bool:
    async def _eval_onclick(raw_code: str) -> bool:
        if not raw_code:
            return False
        if not _code_matches_target_slip(raw_code, slip_id):
            return False
        try:
            return bool(
                await page.evaluate(
                    """(rawCode) => {
                        const text = String(rawCode || '').trim();
                        if (!text) return false;
                        const code = text.replace(/^javascript:/i, '').trim();
                        if (!code) return false;
                        try {
                            (0, eval)(code);
                            return true;
                        } catch (e) {
                            return false;
                        }
                    }""",
                    raw_code,
                )
            )
        except Exception:
            return False

    trigger_candidates = (
        ("vote-link", 'a:has-text("투표지")'),
        ("vote-button", 'button:has-text("투표지")'),
        ("openGamePaper", '[onclick*="openGamePaper"]'),
        ("openGamePaper-href", 'a[href*="openGamePaper"]'),
    )

    for route, selector in trigger_candidates:
        try:
            triggers = row.locator(selector)
            trigger_count = await triggers.count()
            if trigger_count <= 0:
                continue
            for idx in range(min(trigger_count, 3)):
                trigger = triggers.nth(idx)
                onclick_code = str(await trigger.get_attribute("onclick") or "").strip()
                href_code = str(await trigger.get_attribute("href") or "").strip()
                if slip_id and route in {"openGamePaper", "openGamePaper-href"} and not any(
                    _code_matches_target_slip(code, slip_id) for code in (onclick_code, href_code) if code
                ):
                    continue
                try:
                    await trigger.click(timeout=3000)
                    logger.info(
                        "paperArea open trigger route=%s request_id=%s discord_user_id=%s slip_id=%s idx=%d",
                        route,
                        request_id,
                        discord_user_id,
                        slip_id,
                        idx,
                    )
                    return True
                except Exception:
                    try:
                        await trigger.dispatch_event("click")
                        logger.info(
                            "paperArea open trigger route=%s request_id=%s discord_user_id=%s slip_id=%s idx=%d dispatched=true",
                            route,
                            request_id,
                            discord_user_id,
                            slip_id,
                            idx,
                        )
                        return True
                    except Exception:
                        pass
                    for code in (onclick_code, href_code):
                        if code and await _eval_onclick(code):
                            logger.info(
                                "paperArea open trigger route=%s request_id=%s discord_user_id=%s slip_id=%s idx=%d eval_fallback=true",
                                route,
                                request_id,
                                discord_user_id,
                                slip_id,
                                idx,
                            )
                            return True
        except Exception:
            continue

    onclick_code = ""
    href_code = ""
    try:
        onclick_code = str(await row.get_attribute("onclick") or "").strip()
    except Exception:
        onclick_code = ""
    try:
        href_code = str(await row.get_attribute("href") or "").strip()
    except Exception:
        href_code = ""

    for code in (onclick_code, href_code):
        if code and _code_matches_target_slip(code, slip_id) and await _eval_onclick(code):
            logger.info(
                "paperArea open trigger route=row request_id=%s discord_user_id=%s slip_id=%s eval_fallback=true",
                request_id,
                discord_user_id,
                slip_id,
            )
            return True

    if slip_id:
        try:
            opened = bool(
                await page.evaluate(
                    """(rawSlipId) => {
                        const slipId = String(rawSlipId || '').trim();
                        if (!slipId) return false;
                        const nodes = Array.from(
                            document.querySelectorAll('[onclick*="openGamePaper"], a[href*="openGamePaper"]')
                        );
                        const candidates = nodes.filter((node) => {
                            const onclick = String(node.getAttribute('onclick') || '');
                            const href = String(node.getAttribute('href') || '');
                            return onclick.includes(slipId) || href.includes(slipId);
                        });
                        const evalCode = (rawCode) => {
                            const text = String(rawCode || '').trim();
                            if (!text) return false;
                            const code = text.replace(/^javascript:/i, '').trim();
                            if (!code) return false;
                            try {
                                (0, eval)(code);
                                return true;
                            } catch (e) {
                                return false;
                            }
                        };
                        for (const node of candidates) {
                            const onclick = String(node.getAttribute('onclick') || '');
                            const href = String(node.getAttribute('href') || '');
                            try { node.click(); return true; } catch (e) {}
                            if (evalCode(onclick) || evalCode(href)) return true;
                        }
                        return false;
                    }""",
                    slip_id,
                )
            )
        except Exception:
            opened = False
        if opened:
            logger.info(
                "paperArea open trigger route=page-search request_id=%s discord_user_id=%s slip_id=%s",
                request_id,
                discord_user_id,
                slip_id,
            )
            return True
    return False


async def _row_is_detail_area(row: Any) -> bool:
    try:
        row_id = str(await row.get_attribute("id") or "").strip()
    except Exception:
        row_id = ""
    try:
        class_name = str(await row.get_attribute("class") or "").strip()
    except Exception:
        class_name = ""

    if row_id == "paperTr":
        return True
    class_tokens = {token.strip() for token in class_name.split() if token.strip()}
    return "detailArea" in class_tokens


async def _row_has_openable_trigger(row: Any) -> bool:
    trigger_selectors = (
        'a:has-text("투표지")',
        'button:has-text("투표지")',
        '[onclick*="openGamePaper"]',
        'a[href*="openGamePaper"]',
    )
    for selector in trigger_selectors:
        try:
            if await row.locator(selector).count() > 0:
                return True
        except Exception:
            continue

    for attr_name in ("onclick", "href"):
        try:
            value = str(await row.get_attribute(attr_name) or "").strip()
        except Exception:
            value = ""
        if "openGamePaper" in value:
            return True
    return False


async def _read_paper_area_vote_state(page: Page) -> dict[str, Any] | None:
    try:
        state = await page.evaluate(
            """() => {
                const root = document.querySelector('#paperArea');
                if (!root || !(root instanceof HTMLElement)) {
                    return {
                        ready: false,
                        readyMode: 'none',
                        rowCount: 0,
                        rowsTextHash: '',
                        btkNum: '',
                        signature: '',
                        recordRowsFound: false,
                        recordMarkersMissing: false,
                    };
                }

                const rootStyle = window.getComputedStyle(root);
                const rootVisible =
                    rootStyle.display !== 'none' &&
                    rootStyle.visibility !== 'hidden' &&
                    (root.offsetWidth > 0 || root.offsetHeight > 0 || root.getClientRects().length > 0);
                if (!rootVisible) {
                    return {
                        ready: false,
                        readyMode: 'none',
                        rowCount: 0,
                        rowsTextHash: '',
                        btkNum: '',
                        signature: '',
                        recordRowsFound: false,
                        recordMarkersMissing: false,
                    };
                }

                const loadingNodes = root.querySelectorAll('.loading, [class*="loading"], [aria-busy="true"]');
                for (const node of loadingNodes) {
                    if (!(node instanceof HTMLElement)) continue;
                    const style = window.getComputedStyle(node);
                    const visible =
                        style.display !== 'none' &&
                        style.visibility !== 'hidden' &&
                        (node.offsetWidth > 0 || node.offsetHeight > 0 || node.getClientRects().length > 0);
                    if (visible) {
                        return {
                            ready: false,
                            readyMode: 'none',
                            rowCount: 0,
                            rowsTextHash: '',
                            btkNum: '',
                            signature: '',
                            recordRowsFound: false,
                            recordMarkersMissing: false,
                        };
                    }
                }

                const normalizeText = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
                const ignoredPattern = /(유의사항|데이터없음|조회된 .*없습니다|상세 경기 정보를 찾지 못했습니다)/i;
                const collectValidRows = (selector, minCells) => {
                    const nodes = Array.from(root.querySelectorAll(selector));
                    const texts = [];
                    for (const row of nodes) {
                        if (!(row instanceof HTMLElement)) continue;
                        const cells = row.querySelectorAll('td');
                        if (cells.length < minCells) continue;
                        const text = normalizeText(row.textContent || '');
                        if (!text) continue;
                        if (ignoredPattern.test(text)) continue;
                        texts.push(text);
                    }
                    return texts;
                };

                const victoryTexts = collectValidRows('#tbd_gmBuySlipList tr[data-matchseq]', 2);
                const recordTexts = collectValidRows('#winrstResultListTbody tr, #winrstViewTotalTblDiv tbody tr', 1);

                let recordMarkerText = '';
                const markerSelectors = [
                    '#winrstTotBuyInfo',
                    '#winrstTotCaseCnt',
                    '#winrstAllotTxt',
                    '#winrstBuyAmt',
                    '#winrstAllotReAmt',
                    '#winrstViewTotalTblDiv',
                ];
                for (const selector of markerSelectors) {
                    const node = root.querySelector(selector);
                    if (!node) continue;
                    recordMarkerText += ' ' + normalizeText(node.textContent || '');
                }
                const rootText = normalizeText(root.textContent || '');
                const hasRecordMarker = Boolean(recordMarkerText.trim()) || /(총투표금액|선택경기수|예상배당률|개별투표금액|예상적중금액|배당)/.test(rootText);
                const recordRowsFound = recordTexts.length > 0;
                const recordMarkersMissing = recordRowsFound && !hasRecordMarker;

                let readyMode = 'none';
                let activeTexts = [];
                if (victoryTexts.length > 0) {
                    readyMode = 'victory_rows';
                    activeTexts = victoryTexts;
                } else if (recordRowsFound && hasRecordMarker) {
                    readyMode = 'record_rows';
                    activeTexts = recordTexts;
                }

                const scriptText = Array.from(root.querySelectorAll('script'))
                    .map((node) => node.textContent || '')
                    .join('\\n');
                const sourceText = `${scriptText}\\n${rootText}`;

                let btkNum = '';
                const btkPatterns = [
                    /["']?btkNum["']?\\s*[:=]\\s*["']([^"']+)["']/i,
                    /openGamePaper\\s*\\([^)]*["']([A-Za-z0-9]{2,}(?:-[A-Za-z0-9]{2,})+)["']/i,
                ];
                for (const pattern of btkPatterns) {
                    const match = sourceText.match(pattern);
                    if (match && match[1]) {
                        btkNum = normalizeText(match[1]);
                        break;
                    }
                }

                const rowsText = activeTexts.join(' | ');
                const rowsTextHash = rowsText ? String(rowsText.length) + ':' + rowsText.slice(0, 400) : '';
                const signature = [readyMode, String(activeTexts.length), rowsTextHash, btkNum].join(' || ');

                return {
                    ready: readyMode !== 'none',
                    readyMode,
                    rowCount: activeTexts.length,
                    rowsTextHash,
                    btkNum,
                    signature,
                    recordRowsFound,
                    recordMarkersMissing,
                };
            }"""
        )
    except Exception:
        return None

    if not isinstance(state, dict):
        return None
    return state


def _create_game_detail_response_task(page: Page) -> asyncio.Task[Response] | None:
    predicate = lambda response: _GAME_DETAIL_ENDPOINT_TOKEN in str(response.url or "")
    expect_response = getattr(page, "expect_response", None)
    if callable(expect_response):

        async def _wait_with_expect() -> Response:
            async with expect_response(predicate, timeout=_PAPER_STABLE_TIMEOUT_MS) as response_info:
                await asyncio.sleep(0)
            return await response_info.value

        try:
            return asyncio.create_task(_wait_with_expect())
        except Exception:
            pass

    try:
        return asyncio.create_task(page.wait_for_response(predicate, timeout=_PAPER_STABLE_TIMEOUT_MS))
    except Exception:
        return None


async def _consume_game_detail_response_task(
    response_task: asyncio.Task[Response] | None,
    *,
    request_id: str,
    discord_user_id: str,
    target_slip_id: str,
    attempt: int,
) -> str:
    if response_task is None:
        return ""
    if response_task.cancelled():
        return ""

    if not response_task.done():
        response_task.cancel()
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await response_task
        return ""

    try:
        response = response_task.result()
        network_btk_num = await _extract_btk_num_from_network_response(response)
        logger.info(
            "paperArea getGameDetail response request_id=%s discord_user_id=%s slip_id=%s attempt=%d status=%s btkNum=%s",
            request_id,
            discord_user_id,
            target_slip_id,
            attempt,
            response.status,
            network_btk_num or "-",
        )
        return network_btk_num
    except PlaywrightTimeoutError:
        logger.warning(
            "paperArea getGameDetail timeout request_id=%s discord_user_id=%s slip_id=%s attempt=%d",
            request_id,
            discord_user_id,
            target_slip_id,
            attempt,
        )
    except Exception as exc:
        logger.warning(
            "paperArea getGameDetail wait failed request_id=%s discord_user_id=%s slip_id=%s attempt=%d error=%s",
            request_id,
            discord_user_id,
            target_slip_id,
            attempt,
            exc,
        )
    return ""


async def _wait_for_paper_area_vote_loaded(
    page: Page,
    *,
    timeout_ms: int = _PAPER_STABLE_TIMEOUT_MS,
    stable_rounds: int = _PAPER_STABLE_ROUNDS,
    sample_interval_ms: int = _PAPER_STABLE_SAMPLE_INTERVAL_MS,
) -> dict[str, Any] | None:
    timeout_ms = max(1, int(timeout_ms))
    stable_rounds = max(2, int(stable_rounds))
    sample_interval_ms = max(1, int(sample_interval_ms))

    deadline = time.monotonic() + (timeout_ms / 1000)
    last_signature = ""
    stable_hits = 0
    last_ready_state: dict[str, Any] | None = None

    while True:
        state = await _read_paper_area_vote_state(page)
        if not isinstance(state, dict) or not bool(state.get("ready")):
            last_signature = ""
            stable_hits = 0
            last_ready_state = None
        else:
            signature = str(state.get("signature") or "").strip()
            if not signature:
                last_signature = ""
                stable_hits = 0
                last_ready_state = None
            elif signature == last_signature:
                stable_hits += 1
                last_ready_state = state
            else:
                last_signature = signature
                stable_hits = 1
                last_ready_state = state

        if stable_hits >= stable_rounds and last_ready_state is not None:
            return last_ready_state

        remain = deadline - time.monotonic()
        if remain <= 0:
            return None
        await asyncio.sleep(min(sample_interval_ms / 1000, remain))


async def _open_target_paper_with_retry(
    page: Page,
    row: Any,
    target_slip_id: str,
    *,
    request_id: str,
    discord_user_id: str,
) -> dict[str, Any] | None:
    last_error_reason = "vote_not_ready"
    for attempt in (1, 2):
        if attempt > 1:
            logger.warning(
                "paperArea retry openGamePaper request_id=%s discord_user_id=%s slip_id=%s attempt=%d",
                request_id,
                discord_user_id,
                target_slip_id,
                attempt,
            )
        response_task = _create_game_detail_response_task(page)
        network_btk_num = ""

        opened = await _open_game_paper_for_row(
            page,
            row,
            target_slip_id,
            request_id=request_id,
            discord_user_id=discord_user_id,
        )
        if not opened:
            logger.warning(
                "paperArea open failed request_id=%s discord_user_id=%s slip_id=%s attempt=%d",
                request_id,
                discord_user_id,
                target_slip_id,
                attempt,
            )
            if response_task is not None:
                response_task.cancel()
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await response_task
            continue

        try:
            await page.wait_for_load_state("networkidle", timeout=1500)
        except Exception:
            pass

        logger.info(
            "paperArea vote content load start request_id=%s discord_user_id=%s slip_id=%s attempt=%d timeout_ms=%d stable_rounds=%d",
            request_id,
            discord_user_id,
            target_slip_id,
            attempt,
            _PAPER_STABLE_TIMEOUT_MS,
            _PAPER_STABLE_ROUNDS,
        )
        dom_ready_task = asyncio.create_task(
            _wait_for_paper_area_vote_loaded(
                page,
                timeout_ms=_PAPER_STABLE_TIMEOUT_MS,
                stable_rounds=_PAPER_STABLE_ROUNDS,
                sample_interval_ms=_PAPER_STABLE_SAMPLE_INTERVAL_MS,
            )
        )
        wait_tasks: set[asyncio.Task[Any]] = {dom_ready_task}
        if response_task is not None:
            wait_tasks.add(response_task)
        if response_task is not None:
            done, _pending = await asyncio.wait(wait_tasks, return_when=asyncio.FIRST_COMPLETED)
            if dom_ready_task not in done:
                ready_state = await dom_ready_task
            else:
                ready_state = dom_ready_task.result()
            network_btk_num = await _consume_game_detail_response_task(
                response_task,
                request_id=request_id,
                discord_user_id=discord_user_id,
                target_slip_id=target_slip_id,
                attempt=attempt,
            )
        else:
            ready_state = await dom_ready_task
        if response_task is None:
            network_btk_num = ""
        if ready_state is not None:
            ready_mode = str(ready_state.get("readyMode") or "none").strip() or "none"
            logger.info(
                "paperArea ready_mode=%s request_id=%s discord_user_id=%s slip_id=%s attempt=%d row_count=%s",
                ready_mode,
                request_id,
                discord_user_id,
                target_slip_id,
                attempt,
                ready_state.get("rowCount"),
            )
            logger.info(
                "paperArea vote content load end request_id=%s discord_user_id=%s slip_id=%s attempt=%d ready=true",
                request_id,
                discord_user_id,
                target_slip_id,
                attempt,
            )
            ready_state["networkBtkNum"] = network_btk_num
            return ready_state

        diagnostic_state = await _read_paper_area_vote_state(page)
        ready_mode = str((diagnostic_state or {}).get("readyMode") or "none").strip() or "none"
        record_rows_found = bool((diagnostic_state or {}).get("recordRowsFound"))
        record_markers_missing = bool((diagnostic_state or {}).get("recordMarkersMissing"))
        logger.info(
            "paperArea ready_mode=%s request_id=%s discord_user_id=%s slip_id=%s attempt=%d row_count=%s ready=false",
            ready_mode,
            request_id,
            discord_user_id,
            target_slip_id,
            attempt,
            (diagnostic_state or {}).get("rowCount"),
        )
        if record_rows_found and record_markers_missing:
            last_error_reason = "record_not_ready"
            logger.warning(
                "paperArea record readiness markers missing request_id=%s discord_user_id=%s slip_id=%s attempt=%d",
                request_id,
                discord_user_id,
                target_slip_id,
                attempt,
            )
        logger.warning(
            "paperArea vote content load timeout request_id=%s discord_user_id=%s slip_id=%s attempt=%d",
            request_id,
            discord_user_id,
            target_slip_id,
            attempt,
        )
    return {"ready": False, "errorReason": last_error_reason}


def _build_capture_request_id(targets: list[str]) -> str:
    seed = "|".join(targets[:10]) + f"|{time.time_ns()}"
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]


async def _resolve_capture_area(page: Page) -> Any | None:
    preferred = page.locator("#paperTr #paperArea").first
    try:
        if await preferred.count() > 0:
            return preferred
    except Exception:
        pass

    fallback = page.locator("#paperArea").first
    try:
        if await fallback.count() > 0:
            return fallback
    except Exception:
        pass
    return None


async def _capture_target_slip_image(
    page: Page,
    row: Any,
    target_slip_id: str,
    *,
    request_id: str,
    discord_user_id: str,
) -> bytes | None:
    ready_state = await _open_target_paper_with_retry(
        page,
        row,
        target_slip_id,
        request_id=request_id,
        discord_user_id=discord_user_id,
    )
    if not isinstance(ready_state, dict) or not bool(ready_state.get("ready", True)):
        skipped_reason = str((ready_state or {}).get("errorReason") or "vote_not_ready").strip() or "vote_not_ready"
        logger.warning(
            "paperArea capture skipped reason=%s request_id=%s discord_user_id=%s slip_id=%s",
            skipped_reason,
            request_id,
            discord_user_id,
            target_slip_id,
        )
        return None

    network_btk_num = str(ready_state.get("networkBtkNum") or "").strip()
    dom_btk_num = str(ready_state.get("btkNum") or "").strip()
    loaded_btk_num = network_btk_num or dom_btk_num
    if _normalize_slip_token(loaded_btk_num) != _normalize_slip_token(target_slip_id):
        logger.warning(
            "paperArea capture skipped reason=btk_mismatch request_id=%s discord_user_id=%s target_slip_id=%s loaded_btkNum=%s network_btkNum=%s dom_btkNum=%s",
            request_id,
            discord_user_id,
            target_slip_id,
            loaded_btk_num or "-",
            network_btk_num or "-",
            dom_btk_num or "-",
        )
        return None
    logger.info(
        "paperArea btkNum matched request_id=%s discord_user_id=%s target_slip_id=%s source=%s",
        request_id,
        discord_user_id,
        target_slip_id,
        "network" if network_btk_num else "dom",
    )

    capture_area = await _resolve_capture_area(page)
    if capture_area is None:
        logger.warning(
            "paperArea capture skipped reason=paperArea_not_found request_id=%s discord_user_id=%s slip_id=%s",
            request_id,
            discord_user_id,
            target_slip_id,
        )
        return None

    try:
        await capture_area.wait_for(state="visible", timeout=5000)
        return await capture_area.screenshot(type="png")
    except Exception as exc:
        logger.warning(
            "paperArea capture skipped reason=screenshot_failed request_id=%s discord_user_id=%s slip_id=%s error=%s",
            request_id,
            discord_user_id,
            target_slip_id,
            exc,
        )
        return None


async def _collect_openable_row_candidates(page: Page) -> list[tuple[int, Any, str]]:
    rows = page.locator("#purchaseWinTable tbody tr")
    row_count = await rows.count()
    if row_count <= 0:
        rows = page.locator("table tbody tr")
        row_count = await rows.count()
    if row_count <= 0:
        return []

    candidates: list[tuple[int, Any, str]] = []
    for idx in range(row_count):
        row = rows.nth(idx)
        if await _row_is_detail_area(row):
            logger.info("paperArea row candidate skipped(detailArea) row_idx=%d", idx)
            continue
        if not await _row_has_openable_trigger(row):
            continue
        try:
            row_slip_id = await _extract_open_game_paper_slip_id_from_row(row)
            if not row_slip_id:
                row_slip_id = await _extract_slip_id_from_row(row, idx)
        except Exception:
            row_slip_id = ""
        candidates.append((idx, row, row_slip_id))
    return candidates


def _map_rows_by_slip_id(candidates: list[tuple[int, Any, str]]) -> dict[str, tuple[int, Any, str]]:
    mapped: dict[str, tuple[int, Any, str]] = {}
    for candidate in candidates:
        normalized = _normalize_slip_token(candidate[2])
        if not normalized or normalized in mapped:
            continue
        mapped[normalized] = candidate
    return mapped


def _build_fallback_row_queue(
    candidates: list[tuple[int, Any, str]],
    *,
    exact_rows: set[int],
) -> list[tuple[int, Any, str]]:
    return [candidate for candidate in candidates if candidate[0] not in exact_rows]


async def capture_purchase_paper_area_snapshots(
    page: Page,
    target_slip_ids: list[str],
    max_count: int | None = None,
    discord_user_id: str | None = None,
    request_id: str | None = None,
) -> PurchaseSnapshotResult:
    await navigate_to_purchase_history(page)

    user_id_for_log = str(discord_user_id or "-").strip() or "-"
    target_max = max(1, int(max_count)) if max_count is not None else None
    targets: list[str] = []
    seen_targets: set[str] = set()
    for raw in target_slip_ids:
        slip_id = str(raw or "").strip()
        if not slip_id or slip_id in seen_targets:
            continue
        seen_targets.add(slip_id)
        targets.append(slip_id)
        if target_max is not None and len(targets) >= target_max:
            break

    if not targets:
        return PurchaseSnapshotResult(
            files=[],
            attempted_count=0,
            success_count=0,
            failed_count=0,
            exact_success_count=0,
            fallback_success_count=0,
        )

    capture_request_id = str(request_id or "").strip() or _build_capture_request_id(targets)
    started_at = time.monotonic()
    logger.info(
        "paperArea capture start request_id=%s discord_user_id=%s target_count=%d",
        capture_request_id,
        user_id_for_log,
        len(targets),
    )
    row_candidates = await _collect_openable_row_candidates(page)
    if not row_candidates:
        logger.warning(
            "paperArea capture failed: no openable rows request_id=%s discord_user_id=%s",
            capture_request_id,
            user_id_for_log,
        )
        return PurchaseSnapshotResult(
            files=[],
            attempted_count=len(targets),
            success_count=0,
            failed_count=len(targets),
            exact_success_count=0,
            fallback_success_count=0,
        )
    row_map = _map_rows_by_slip_id(row_candidates)
    fallback_queue = _build_fallback_row_queue(row_candidates, exact_rows=set())
    fallback_cursor = 0
    used_row_indexes: set[int] = set()

    captured: list[tuple[str, bytes]] = []
    exact_success_count = 0
    fallback_success_count = 0
    for slip_id in targets:
        if target_max is not None and len(captured) >= target_max:
            break
        logger.info(
            "paperArea capture target begin request_id=%s discord_user_id=%s slip_id=%s",
            capture_request_id,
            user_id_for_log,
            slip_id,
        )
        mapping_mode = "exact"
        target_row = row_map.get(_normalize_slip_token(slip_id))
        if target_row is None:
            logger.warning(
                "paperArea capture skipped reason=row_not_found_exact request_id=%s discord_user_id=%s slip_id=%s",
                capture_request_id,
                user_id_for_log,
                slip_id,
            )
            if fallback_cursor >= len(fallback_queue):
                logger.warning(
                    "paperArea capture skipped reason=fallback_exhausted request_id=%s discord_user_id=%s slip_id=%s",
                    capture_request_id,
                    user_id_for_log,
                    slip_id,
                )
                continue
            target_row = fallback_queue[fallback_cursor]
            fallback_cursor += 1
            mapping_mode = "fallback"

        row_idx, row, row_slip_id = target_row
        if row_idx in used_row_indexes:
            logger.warning(
                "paperArea capture skipped reason=fallback_exhausted request_id=%s discord_user_id=%s slip_id=%s",
                capture_request_id,
                user_id_for_log,
                slip_id,
            )
            continue
        logger.info(
            "paperArea row candidate try request_id=%s discord_user_id=%s slip_id=%s row_idx=%d row_slip_id=%s mapping=%s",
            capture_request_id,
            user_id_for_log,
            slip_id,
            row_idx,
            row_slip_id or "-",
            mapping_mode,
        )
        slip_started_at = time.monotonic()
        image = await _capture_target_slip_image(
            page,
            row,
            slip_id,
            request_id=capture_request_id,
            discord_user_id=user_id_for_log,
        )
        per_slip_ms = (time.monotonic() - slip_started_at) * 1000
        used_row_indexes.add(row_idx)
        if image is None:
            logger.warning(
                "paperArea capture skipped reason=target_row_failed request_id=%s discord_user_id=%s slip_id=%s row_idx=%d mapping=%s",
                capture_request_id,
                user_id_for_log,
                slip_id,
                row_idx,
                mapping_mode,
            )
            logger.info(
                "paperArea per_slip_ms=%.2f request_id=%s discord_user_id=%s slip_id=%s success=false mapping=%s",
                per_slip_ms,
                capture_request_id,
                user_id_for_log,
                slip_id,
                mapping_mode,
            )
            continue

        filename = f"paper_{_sanitize_slip_id_for_filename(slip_id)}.png"
        captured.append((filename, image))
        if mapping_mode == "exact":
            exact_success_count += 1
        else:
            fallback_success_count += 1
        logger.info(
            "paperArea captured request_id=%s discord_user_id=%s slip_id=%s bytes=%d mapping=%s",
            capture_request_id,
            user_id_for_log,
            slip_id,
            len(image),
            mapping_mode,
        )
        logger.info(
            "paperArea per_slip_ms=%.2f request_id=%s discord_user_id=%s slip_id=%s success=true mapping=%s",
            per_slip_ms,
            capture_request_id,
            user_id_for_log,
            slip_id,
            mapping_mode,
        )

    attempted_count = len(targets)
    success_count = len(captured)
    failed_count = max(0, attempted_count - success_count)
    total_ms = (time.monotonic() - started_at) * 1000
    logger.info(
        "paperArea snapshot_total_ms=%.2f request_id=%s discord_user_id=%s attempted=%d success=%d failed=%d",
        total_ms,
        capture_request_id,
        user_id_for_log,
        attempted_count,
        success_count,
        failed_count,
    )
    return PurchaseSnapshotResult(
        files=captured,
        attempted_count=attempted_count,
        success_count=success_count,
        failed_count=failed_count,
        exact_success_count=exact_success_count,
        fallback_success_count=fallback_success_count,
    )


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
    include_match_details: bool = True,
) -> list[BetSlip]:
    limit = max(1, min(limit, 30))

    await navigate_to_purchase_history(page)

    # Source 1: official list API (most accurate)
    try:
        slips, detail_params, diagnostics = await _collect_slips_via_list_api(page, limit)
        if slips:
            success = 0
            failed = 0
            if include_match_details:
                success, failed = await _fetch_match_details_for_slips(page, slips, detail_params)
            logger.info(
                "purchase scrape api items=%d list_pages=%d detail_success=%d detail_failed=%d include_match_details=%s",
                len(slips),
                diagnostics.get("pages", 0),
                success,
                failed,
                include_match_details,
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
