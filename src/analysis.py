from __future__ import annotations

import calendar
import logging
import re
from datetime import datetime, timedelta, timezone

from playwright.async_api import Page

from src.models import PurchaseAnalysis

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))

_ANALYSIS_PAGE_PATH = "/main/mainPage/mypage/myPurchaseAnalysis.do"
_PURCHASE_LABELS = ("구매금액", "총구매금액", "구매액")
_WINNING_LABELS = ("적중금액", "총적중금액", "환급금액")


def _is_execution_context_destroyed_error(exc: Exception) -> bool:
    return "Execution context was destroyed" in str(exc)


async def _evaluate_with_retry(page: Page, script: str, arg: object | None = None, retries: int = 3):
    last_exc: Exception | None = None
    for _ in range(max(1, retries)):
        try:
            if arg is None:
                return await page.evaluate(script)
            return await page.evaluate(script, arg)
        except Exception as exc:
            last_exc = exc
            if not _is_execution_context_destroyed_error(exc):
                raise
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=4000)
            except Exception:
                pass
            await page.wait_for_timeout(250)
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("page.evaluate 재시도 중 알 수 없는 오류가 발생했습니다.")


def _to_int_amount(text: str) -> int | None:
    matched = re.search(r"([0-9][0-9,]*)", text or "")
    if not matched:
        return None
    digits = matched.group(1).replace(",", "")
    try:
        return int(digits)
    except ValueError:
        return None


def _to_int_amount_or_zero(value: object) -> int:
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    parsed = _to_int_amount(str(value))
    return parsed if parsed is not None else 0


def _build_analysis_token(months: int, purchase_amount: int, winning_amount: int) -> str:
    return f"{int(months)}:{int(purchase_amount)}:{int(winning_amount)}"


def _find_label_amount(text: str, labels: tuple[str, ...]) -> int | None:
    compact = re.sub(r"\s+", " ", text or "")
    for label in labels:
        escaped = re.escape(label)
        patterns = (
            rf"{escaped}\s*[:：]?\s*([0-9][0-9,]*)\s*원?",
            rf"{escaped}[^0-9]{{0,20}}([0-9][0-9,]*)\s*원?",
        )
        for pattern in patterns:
            match = re.search(pattern, compact)
            if match:
                value = _to_int_amount(match.group(1))
                if value is not None:
                    return value
    return None


def _extract_amounts_from_text(text: str) -> tuple[int | None, int | None]:
    purchase = _find_label_amount(text, _PURCHASE_LABELS)
    winning = _find_label_amount(text, _WINNING_LABELS)
    return purchase, winning


def _subtract_months(base: datetime, months: int) -> datetime:
    total_month = base.month - months
    year = base.year + (total_month - 1) // 12
    month = (total_month - 1) % 12 + 1
    day = min(base.day, calendar.monthrange(year, month)[1])
    return base.replace(year=year, month=month, day=day)


def _month_range_ym(now_kst: datetime, months: int) -> tuple[str, str, str, str]:
    months = max(1, min(12, int(months)))
    start = _subtract_months(now_kst, months)
    return (
        f"{start.year:04d}",
        f"{start.month:02d}",
        f"{now_kst.year:04d}",
        f"{now_kst.month:02d}",
    )


def _parse_purchase_analysis_payload(payload: object) -> tuple[int | None, int | None]:
    if not isinstance(payload, dict):
        return None, None

    purchase_info = payload.get("purchaseInfo")
    if isinstance(purchase_info, dict):
        purchase = _to_int_amount(str(purchase_info.get("buyAmt")))
        winning = _to_int_amount(str(purchase_info.get("winAmt")))
        if purchase is not None and winning is not None:
            return purchase, winning

    game_buy_rate = payload.get("readGameBuyRateHitAmount")
    if isinstance(game_buy_rate, list) and game_buy_rate:
        first = game_buy_rate[0]
        if isinstance(first, dict):
            purchase = _to_int_amount(str(first.get("buyAmt")))
            winning = _to_int_amount(str(first.get("winAmt")))
            if purchase is not None and winning is not None:
                return purchase, winning

    return None, None


async def _request_purchase_analysis_api(page: Page, months: int) -> dict[str, object]:
    now_kst = datetime.now(KST)
    s_year, s_month, e_year, e_month = _month_range_ym(now_kst, months)
    params = {
        "sYear": s_year,
        "sMonth": s_month,
        "eYear": e_year,
        "eMonth": e_month,
    }
    logger.info(
        "analysis api request params: months=%d sYear=%s sMonth=%s eYear=%s eMonth=%s",
        months,
        s_year,
        s_month,
        e_year,
        e_month,
    )
    result = await _evaluate_with_retry(
        page,
        """({sYear, sMonth, eYear, eMonth}) => new Promise((resolve) => {
            const tabType = (document.querySelector('#tabType')?.value || 'gameAll');
            const params = { sYear, sMonth, eYear, eMonth, tabType };
            let done = false;
            const finish = (obj) => {
                if (done) return;
                done = true;
                resolve(obj);
            };
            const timer = setTimeout(() => {
                finish({ ok: false, error: 'timeout', params });
            }, 10000);
            try {
                if (typeof requestClient === 'undefined' || !requestClient || typeof requestClient.requestPostMethod !== 'function') {
                    clearTimeout(timer);
                    finish({ ok: false, error: 'requestClient_unavailable', params });
                    return;
                }
                requestClient.requestPostMethod('/mypgPurAna/getPurAnaInfo.do', params, true, (response) => {
                    clearTimeout(timer);
                    finish({ ok: true, payload: response, params });
                });
            } catch (e) {
                clearTimeout(timer);
                finish({ ok: false, error: String(e), params });
            }
        })""",
        params,
    )
    if isinstance(result, dict):
        return result
    return {"ok": False, "error": "invalid_response_shape", "params": params}


async def probe_purchase_analysis_token(page: Page, months: int) -> tuple[str, PurchaseAnalysis | None]:
    months = max(1, min(12, int(months)))
    await _navigate_to_purchase_analysis(page)
    api_result = await _request_purchase_analysis_api(page, months)
    if not bool(api_result.get("ok")):
        reason = api_result.get("error") or "unknown"
        raise RuntimeError(f"analysis probe failed: {reason}")

    purchase_amount, winning_amount = _parse_purchase_analysis_payload(api_result.get("payload"))
    if purchase_amount is None or winning_amount is None:
        logger.warning("analysis probe parse incomplete: months=%d", months)
        return "", None

    result = PurchaseAnalysis(
        months=months,
        purchase_amount=purchase_amount,
        winning_amount=winning_amount,
    )
    return _build_analysis_token(months, purchase_amount, winning_amount), result


async def _dismiss_popups(page: Page) -> None:
    await _evaluate_with_retry(
        page,
        """() => {
            document.querySelectorAll('.ui-dialog-content').forEach((el) => {
                try { $(el).dialog('close'); } catch (e) {}
            });
            document.querySelectorAll('.ui-widget-overlay, .ui-dialog-overlay').forEach((el) => el.remove());
        }""",
    )


async def _navigate_to_purchase_analysis(page: Page) -> None:
    await _dismiss_popups(page)

    selectors = [
        'a:has-text("구매현황분석")',
        f'a[href*="{_ANALYSIS_PAGE_PATH}"]',
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=1200):
                await loc.click()
                await page.wait_for_load_state("networkidle", timeout=12000)
                break
        except Exception:
            continue
    else:
        try:
            await page.evaluate(f"movePageUrl('{_ANALYSIS_PAGE_PATH}')")
            await page.wait_for_load_state("networkidle", timeout=12000)
        except Exception:
            await page.goto(f"https://www.betman.co.kr{_ANALYSIS_PAGE_PATH}", wait_until="networkidle", timeout=25000)

    if "accessDenied" in page.url:
        raise RuntimeError("구매현황분석 페이지 접근이 거부되었습니다. 로그인 상태를 확인해주세요.")


async def _apply_analysis_period(page: Page, months: int) -> None:
    # Fallback path only: use page-native YYYY.MM format and initializeData trigger.
    months = max(1, min(12, months))
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=5000)
    except Exception:
        pass

    now_kst = datetime.now(KST)
    s_year, s_month, e_year, e_month = _month_range_ym(now_kst, months)
    start_text = f"{s_year}.{s_month}"
    end_text = f"{e_year}.{e_month}"

    await _evaluate_with_retry(
        page,
        """({startText, endText}) => {
            const setDate = (selector, value) => {
                const el = document.querySelector(selector);
                if (!el) return false;
                el.value = value;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                return true;
            };
            setDate('#startDt', startText);
            setDate('#endDt', endText);
        }""",
        {"startText": start_text, "endText": end_text},
    )

    action = await _evaluate_with_retry(
        page,
        """() => {
            try {
                if (typeof initializeData === 'function') {
                    initializeData();
                    return 'initializeData';
                }
            } catch (e) {}

            const initBtn = document.querySelector('button[onclick*="initializeData"], a[onclick*="initializeData"], input[onclick*="initializeData"]');
            if (initBtn) {
                initBtn.click();
                return 'initButton';
            }

            const candidates = Array.from(document.querySelectorAll('button, a, input[type="button"], input[type="submit"]'));
            for (const node of candidates) {
                const text = ((node.innerText || node.textContent || node.value || '') + '').replace(/\\s+/g, ' ').trim();
                if (!text) continue;
                if (text.includes('조회') || text.includes('검색')) {
                    node.click();
                    return 'button';
                }
            }
            return 'none';
        }""",
    )
    logger.info("analysis period fallback applied: months=%d start=%s end=%s trigger=%s", months, start_text, end_text, action)
    if action == "none":
        await page.wait_for_timeout(1200)
        return

    # 검색 액션이 페이지 전환/XHR을 일으키는 동안 evaluate 컨텍스트가 변경될 수 있어 잠시 안정화 대기.
    try:
        await page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        await page.wait_for_timeout(1500)


async def _extract_amounts_via_dom(page: Page) -> dict[str, int | bool]:
    return await _evaluate_with_retry(
        page,
        """() => {
            const purchaseLabels = ['구매금액', '총구매금액', '구매액'];
            const winningLabels = ['적중금액', '총적중금액', '환급금액'];

            const parseNum = (txt) => {
                if (!txt) return null;
                const m = txt.replace(/\\s+/g, ' ').match(/([0-9][0-9,]*)/);
                if (!m) return null;
                const n = Number((m[1] || '').replace(/,/g, ''));
                return Number.isFinite(n) ? n : null;
            };

            const esc = (s) => s.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&');

            const fromLabelNearValue = (labels) => {
                const nodes = Array.from(document.querySelectorAll('th,td,dt,dd,li,strong,span,p,div'));
                for (const node of nodes) {
                    const text = (node.textContent || '').replace(/\\s+/g, ' ').trim();
                    if (!text) continue;
                    for (const label of labels) {
                        if (!text.includes(label)) continue;

                        let n = null;
                        const p1 = new RegExp(`${esc(label)}\\\\s*[:：]?\\\\s*([0-9][0-9,]*)`);
                        const p2 = new RegExp(`${esc(label)}[^0-9]{0,20}([0-9][0-9,]*)`);
                        const m1 = text.match(p1);
                        const m2 = text.match(p2);
                        if (m1) n = parseNum(m1[1]);
                        if (n === null && m2) n = parseNum(m2[1]);
                        if (n !== null) return n;

                        const siblingTexts = [
                            node.nextElementSibling?.textContent || '',
                            node.parentElement?.textContent || '',
                            node.closest('tr,li,dl,div')?.textContent || '',
                        ];
                        for (const s of siblingTexts) {
                            n = parseNum(s);
                            if (n !== null) return n;
                        }
                    }
                }
                return null;
            };

            const bodyText = (document.body?.innerText || '').replace(/\\s+/g, ' ').trim();
            const byBody = (labels) => {
                for (const label of labels) {
                    const p1 = new RegExp(`${esc(label)}\\\\s*[:：]?\\\\s*([0-9][0-9,]*)`);
                    const p2 = new RegExp(`${esc(label)}[^0-9]{0,20}([0-9][0-9,]*)`);
                    const m1 = bodyText.match(p1);
                    const m2 = bodyText.match(p2);
                    if (m1) {
                        const n = parseNum(m1[1]);
                        if (n !== null) return n;
                    }
                    if (m2) {
                        const n = parseNum(m2[1]);
                        if (n !== null) return n;
                    }
                }
                return null;
            };

            let purchase = fromLabelNearValue(purchaseLabels);
            let winning = fromLabelNearValue(winningLabels);

            if (purchase === null) purchase = byBody(purchaseLabels);
            if (winning === null) winning = byBody(winningLabels);

            return {
                purchase_amount: purchase ?? 0,
                winning_amount: winning ?? 0,
                purchase_found: purchase !== null,
                winning_found: winning !== null,
            };
        }""",
    )


async def scrape_purchase_analysis(page: Page, months: int) -> PurchaseAnalysis:
    months = max(1, min(12, int(months)))
    await _navigate_to_purchase_analysis(page)

    api_purchase: int | None = None
    api_winning: int | None = None
    try:
        api_result = await _request_purchase_analysis_api(page, months)
        if bool(api_result.get("ok")):
            api_payload = api_result.get("payload")
            api_purchase, api_winning = _parse_purchase_analysis_payload(api_payload)
            logger.info(
                "analysis api parsed: months=%d purchase_amount=%s winning_amount=%s",
                months,
                api_purchase,
                api_winning,
            )
            if api_purchase is not None and api_winning is not None:
                return PurchaseAnalysis(
                    months=months,
                    purchase_amount=api_purchase,
                    winning_amount=api_winning,
                )
        else:
            logger.warning("analysis api request failed: months=%d reason=%s", months, api_result.get("error"))
    except Exception as exc:
        logger.warning("analysis api request exception: months=%d error=%s", months, exc)

    logger.warning("analysis api incomplete, fallback to DOM parse: months=%d", months)
    await _apply_analysis_period(page, months)

    dom_result = await _extract_amounts_via_dom(page)
    purchase_found = bool(dom_result.get("purchase_found"))
    winning_found = bool(dom_result.get("winning_found"))
    purchase_amount = _to_int_amount_or_zero(dom_result.get("purchase_amount"))
    winning_amount = _to_int_amount_or_zero(dom_result.get("winning_amount"))

    if not (purchase_found and winning_found):
        body_text = await page.inner_text("body")
        fallback_purchase, fallback_winning = _extract_amounts_from_text(body_text)
        if not purchase_found and fallback_purchase is not None:
            purchase_amount = fallback_purchase
            purchase_found = True
        if not winning_found and fallback_winning is not None:
            winning_amount = fallback_winning
            winning_found = True

    if not (purchase_found and winning_found):
        raise RuntimeError(
            f"구매현황분석 금액을 찾지 못했습니다. months={months}, url={page.url}, stage=api+dom+text-fallback"
        )

    logger.info(
        "purchase analysis parsed via fallback: months=%d purchase_amount=%d winning_amount=%d",
        months,
        purchase_amount,
        winning_amount,
    )
    return PurchaseAnalysis(
        months=months,
        purchase_amount=purchase_amount,
        winning_amount=winning_amount,
    )
