from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, parse_qs

from playwright.async_api import Page, Response

from src.config import Config
from src.models import BetSlip, GameSchedule, MatchBet

logger = logging.getLogger(__name__)

# Known purchase-history page paths (tried in order)
_PURCHASE_HISTORY_PATHS = [
    "/main/mainPage/mypage/myPurchaseWinList.do",   # 실제 사이트 링크 (구매/적중내역)
    "/main/mainPage/mypage/gameBuyList.do",
    "/main/mainPage/mypage/gameBuyListPop.do",
    "/mypage/gameBuyList.do",
]

# Status groups
_PURCHASE_STATUSES = {"발매중", "발매마감", "구매예약중"}
_RESULT_STATUSES = {"적중", "적중안됨", "미적중", "취소", "적중확인중"}
_ALL_STATUSES = _PURCHASE_STATUSES | _RESULT_STATUSES


class BetmanScraper:
    def __init__(self, config: Config) -> None:
        self._config = config
        self._captured_responses: list[dict[str, Any]] = []
        self.current_round: str = ""  # e.g. "19" — set during navigation

    async def scrape_purchase_history(self, page: Page) -> list[BetSlip]:
        """Main entry: navigate to purchase history and return BetSlips with purchase statuses."""
        return await self._scrape(page, statuses=_PURCHASE_STATUSES)

    async def scrape_all_history(self, page: Page) -> list[BetSlip]:
        """Scrape all statuses (purchase + result) for DB tracking."""
        return await self._scrape(page, statuses=_ALL_STATUSES)

    async def _scrape(self, page: Page, statuses: set[str]) -> list[BetSlip]:
        """Navigate to purchase history and return filtered BetSlips."""
        self._captured_responses.clear()
        page.on("response", self._on_response)

        try:
            await self._navigate_to_purchase_history(page)
            await self._dismiss_popups(page)

            # Expand page size to avoid server-side pagination (default 10)
            try:
                await page.evaluate("""() => {
                    const inp = document.getElementById('inp_pageCnt');
                    if (inp) {
                        inp.value = '100';
                        if (typeof purchaseWinTableObj !== 'undefined' && purchaseWinTableObj.page) {
                            purchaseWinTableObj.page.len(100).draw();
                        }
                    }
                }""")
                try:
                    await page.wait_for_function(
                        "() => document.querySelectorAll('#purchaseWinTable tbody tr').length > 0",
                        timeout=10000,
                    )
                except Exception:
                    await page.wait_for_load_state("networkidle", timeout=10000)
            except Exception as exc:
                logger.debug("Failed to expand page size: %s", exc)

            # Save debug HTML for inspection
            html = await page.content()
            debug_path = Path("storage/debug_purchase.html")
            debug_path.parent.mkdir(parents=True, exist_ok=True)
            debug_path.write_text(html, encoding="utf-8")
            logger.info("Purchase page HTML saved (%d bytes). URL: %s", len(html), page.url)

            # Strategy 1: Try to parse captured XHR JSON
            slips = self._parse_xhr_responses(statuses)
            if slips:
                logger.info("Parsed %d slips from XHR responses", len(slips))
                return slips

            # Strategy 2: Fallback to DOM parsing
            logger.info("No XHR data captured, falling back to DOM parsing")
            slips, detail_params_list = await self._parse_dom(page, statuses)
            logger.info("Parsed %d slips from DOM", len(slips))

            # Fetch match details from marking papers
            if slips and detail_params_list:
                await self._fetch_match_details(page, slips, detail_params_list)

            return slips
        finally:
            page.remove_listener("response", self._on_response)

    async def _on_response(self, response: Response) -> None:
        """Capture JSON responses from purchase history API calls."""
        url = response.url
        if not any(kw in url for kw in (
            "gameBuyList", "buyList", "purchaseList", "gameList",
            "PurchaseWin", "purchaseWin", "myPurchase",
            "mypgPurWin", "getGameList",
            "MarkingPaper", "markingPaper",
        )):
            return
        try:
            ct = response.headers.get("content-type", "")
            if "json" in ct or "javascript" in ct:
                body = await response.text()
                data = json.loads(body)
                self._captured_responses.append(data)
                logger.debug("Captured XHR response from %s", url)
        except Exception:
            pass

    async def _navigate_to_purchase_history(self, page: Page) -> None:
        """Navigate to purchase history from main page context.

        The site requires navigation from the main page (SPA-style).
        Direct page.goto() to inner .do URLs returns error pages.
        """
        # Ensure we're on the main page first (login check already navigates here)
        current_url = page.url
        if not current_url or "betman.co.kr" not in current_url:
            await page.goto(self._config.base_url, wait_until="domcontentloaded", timeout=60000)

        # Always wait for networkidle — is_logged_in only waits for domcontentloaded
        await page.wait_for_load_state("networkidle", timeout=15000)

        await self._dismiss_popups(page)

        # Grab current round from main page before navigating away
        try:
            title_el = page.locator("#mainProtoTitle")
            if await title_el.count() > 0:
                title_text = (await title_el.text_content() or "").strip()
                round_match = re.search(r"(\d+)회차", title_text)
                if round_match:
                    self.current_round = round_match.group(1)
                    logger.info("Current round from main page: %s (%s)", self.current_round, title_text)
        except Exception as exc:
            logger.debug("Failed to get current round from main page: %s", exc)

        # Strategy 1: Click "구매/적중내역" link from header (most reliable)
        buy_link_selectors = [
            'a:has-text("구매/적중내역")',   # 실제 사이트 헤더 링크 텍스트
            'a:has-text("구매내역")',
            'a:has-text("게임구매내역")',
            'a[href*="PurchaseWin"]',
            'a[href*="gameBuyList"]',
        ]
        for sel in buy_link_selectors:
            try:
                loc = page.locator(sel).first
                if await loc.is_visible(timeout=3000):
                    await loc.click()
                    try:
                        await page.wait_for_selector("#purchaseWinTable", timeout=15000)
                    except Exception:
                        await page.wait_for_load_state("networkidle", timeout=10000)
                    if await page.locator(".errorArea").count() == 0:
                        logger.info("Navigated to purchase history via link: %s", page.url)
                        return
                    logger.info("Link %s led to error page, trying next", sel)
            except Exception:
                continue

        # Strategy 2: Use JavaScript movePageUrl() (site's own navigation function)
        for path in _PURCHASE_HISTORY_PATHS:
            try:
                logger.info("Trying JS navigation to %s", path)
                await page.evaluate(f"movePageUrl('{path}')")
                try:
                    await page.wait_for_selector("#purchaseWinTable", timeout=15000)
                except Exception:
                    await page.wait_for_load_state("networkidle", timeout=10000)
                if await page.locator(".errorArea").count() == 0:
                    logger.info("Navigated to purchase history via JS: %s", page.url)
                    return
                logger.info("JS navigation to %s returned error page", path)
                # Go back to main page for next attempt
                await page.goto(self._config.base_url, wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_load_state("networkidle", timeout=10000)
            except Exception as exc:
                logger.debug("JS navigation to %s failed: %s", path, exc)
                continue

        # Strategy 3: Direct page.goto() as last resort
        for path in _PURCHASE_HISTORY_PATHS:
            url = f"{self._config.base_url}{path}"
            try:
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                if resp and resp.ok:
                    await page.wait_for_load_state("networkidle", timeout=10000)
                    if await page.locator(".errorArea").count() > 0:
                        logger.info("Direct path %s returned error page, skipping", path)
                        continue
                    logger.info("Navigated to purchase history (direct): %s", url)
                    return
            except Exception as exc:
                logger.debug("Direct path %s failed: %s", path, exc)
                continue

        raise RuntimeError("Could not navigate to purchase history page")

    # ------------------------------------------------------------------
    # XHR parsing
    # ------------------------------------------------------------------

    def _parse_xhr_responses(self, statuses: set[str] | None = None) -> list[BetSlip]:
        """Parse captured XHR JSON data into BetSlip objects."""
        target = statuses or _PURCHASE_STATUSES
        slips: list[BetSlip] = []
        for data in self._captured_responses:
            extracted = self._extract_slips_from_json(data)
            slips.extend(extracted)

        return [s for s in slips if s.status in target]

    def _extract_slips_from_json(self, data: Any) -> list[BetSlip]:
        """Attempt to extract slips from various JSON structures."""
        slips: list[BetSlip] = []

        # Try different common patterns
        items: list[dict] = []
        if isinstance(data, dict):
            for key in ("list", "data", "result", "items", "gameList", "buyList"):
                if key in data and isinstance(data[key], list):
                    items = data[key]
                    break
            if not items and isinstance(data.get("body"), dict):
                body = data["body"]
                for key in ("list", "data", "result", "items"):
                    if key in body and isinstance(body[key], list):
                        items = body[key]
                        break
        elif isinstance(data, list):
            items = data

        for item in items:
            try:
                slip = self._item_to_betslip(item)
                if slip:
                    slips.append(slip)
            except Exception as exc:
                logger.debug("Skipping item: %s", exc)
        return slips

    @staticmethod
    def _item_to_betslip(item: dict) -> BetSlip | None:
        """Convert a single JSON item into a BetSlip."""

        def _get(keys: list[str], default: Any = "") -> Any:
            for k in keys:
                if k in item:
                    return item[k]
            return default

        slip_id = str(_get(["buyNo", "slipId", "purchaseNo", "gameNo", "id"], ""))
        if not slip_id:
            return None

        status = _get(["statusNm", "status", "gameStatus", "statNm"], "")
        game_type = _get(["gameNm", "gameType", "gameName", "typeNm"], "")
        round_number = str(_get(["roundNo", "round", "trdNo", "roundNum"], ""))
        purchase_dt = _get(["buyDt", "purchaseDate", "regDt", "buyDate"], "")
        total_amount = int(_get(["buyAmt", "totalAmount", "amount", "totAmt"], 0))
        potential_payout = int(_get(["expectAmt", "potentialPayout", "winAmt", "hitAmt"], 0))
        combined_odds = float(_get(["totOdds", "combinedOdds", "allotRate", "odds"], 0))

        # Result fields
        result: str | None = None
        if status in _RESULT_STATUSES:
            result = status
        actual_payout = int(_get(["realHitAmt", "actualPayout", "hitAmt", "payAmt"], 0))

        matches: list[MatchBet] = []
        match_items = _get(["detailList", "matches", "gameDetail", "details"], [])
        if isinstance(match_items, list):
            for idx, m in enumerate(match_items, start=1):
                try:
                    matches.append(MatchBet(
                        match_number=int(m.get("matchNo", m.get("gameSeq", idx))),
                        sport=m.get("sportNm", m.get("sport", "")),
                        league=m.get("leagueNm", m.get("league", "")),
                        home_team=m.get("homeTeamNm", m.get("homeTeam", "")),
                        away_team=m.get("awayTeamNm", m.get("awayTeam", "")),
                        bet_selection=m.get("selectNm", m.get("betType", m.get("choice", ""))),
                        odds=float(m.get("odds", m.get("allotRate", 0))),
                        match_datetime=m.get("gameDt", m.get("matchDate", "")),
                    ))
                except Exception:
                    pass

        return BetSlip(
            slip_id=slip_id,
            game_type=game_type,
            round_number=round_number,
            status=status,
            purchase_datetime=purchase_dt,
            total_amount=total_amount,
            potential_payout=potential_payout,
            combined_odds=combined_odds,
            result=result,
            actual_payout=actual_payout,
            matches=matches,
        )

    # ------------------------------------------------------------------
    # DOM parsing (fallback)
    # ------------------------------------------------------------------

    async def _parse_dom(self, page: Page, statuses: set[str] | None = None) -> tuple[list[BetSlip], list[dict]]:
        """Parse purchase history from the DOM table.

        Returns (slips, detail_params_list) where detail_params_list contains
        the query parameters needed to fetch each slip's marking paper.
        """
        target = statuses or _PURCHASE_STATUSES
        slips: list[BetSlip] = []
        detail_params_list: list[dict] = []

        # Primary: #purchaseWinTable (구매/적중내역 page)
        # Columns: checkbox | game name | datetime | ticket number | amount | status | button
        row_selectors = [
            "#purchaseWinTable tbody tr",
            "table.tbl.dataTable tbody tr",
            "table tbody tr",
        ]

        rows = None
        for sel in row_selectors:
            locs = page.locator(sel)
            count = await locs.count()
            if count > 0:
                rows = locs
                logger.info("Found %d rows with selector: %s", count, sel)
                break

        if rows is None or await rows.count() == 0:
            logger.warning("No purchase rows found in DOM. Page title: %s", await page.title())
            return [], []

        count = await rows.count()
        for i in range(count):
            row = rows.nth(i)
            try:
                slip, detail_params = await self._parse_row(page, row, i)
                if slip and slip.status in target:
                    slips.append(slip)
                    detail_params_list.append(detail_params)
                elif slip:
                    logger.debug("Row %d status '%s' not in target %s", i, slip.status, target)
            except Exception as exc:
                logger.debug("Failed to parse row %d: %s", i, exc)

        return slips, detail_params_list

    async def _parse_row(self, page: Page, row: Any, index: int) -> tuple[BetSlip | None, dict]:
        """Parse a single table row from #purchaseWinTable.

        Known column layout:
        [0] checkbox  [1] game name  [2] datetime  [3] ticket number
        [4] amount    [5] status     [6] button

        Returns (slip, detail_params) where detail_params contains gmId, gmTs,
        purchaseNo, btkNum extracted from the detail link in column 1.
        """
        cells = row.locator("td")
        cell_count = await cells.count()
        if cell_count < 5:
            return None, {}

        texts = []
        for j in range(cell_count):
            t = (await cells.nth(j).text_content() or "").strip()
            texts.append(t)

        # Extract detail link params from column 1 (game name cell)
        detail_params: dict = {}
        try:
            link = cells.nth(1).locator("a").first
            href = await link.get_attribute("href", timeout=5000)
            if href:
                qs = parse_qs(urlparse(href).query)
                detail_params = {
                    "gmId": qs.get("gmId", [""])[0],
                    "gmTs": qs.get("gmTs", [""])[0],
                    "purchaseNo": qs.get("purchaseNo", [""])[0],
                    "btkNum": qs.get("btkNum", [""])[0],
                }
        except Exception:
            pass

        # Also try the button column (last column) for detail link
        if not detail_params.get("btkNum"):
            try:
                btn = cells.nth(cell_count - 1).locator("a, button").first
                onclick = await btn.get_attribute("onclick", timeout=5000)
                if onclick:
                    # Extract params from onclick like openMarkingPaper('B30B-4867-2431-D450', ...)
                    params_match = re.findall(r"'([^']*)'", onclick)
                    if params_match:
                        detail_params["btkNum"] = params_match[0] if len(params_match) > 0 else ""
                        detail_params["purchaseNo"] = params_match[1] if len(params_match) > 1 else ""
                        detail_params["gmId"] = params_match[2] if len(params_match) > 2 else ""
                        detail_params["gmTs"] = params_match[3] if len(params_match) > 3 else ""
            except Exception:
                pass

        # Column-based parsing (for 7-column purchaseWinTable layout)
        if cell_count >= 6:
            game_name = texts[1].strip()  # e.g., "승부식프로토 승부식 19회차"
            purchase_dt = texts[2].strip()  # e.g., "26.02.12(목) 10:04"
            slip_id = texts[3].strip()  # e.g., "B30B-4867-2431-D450"
            amount_text = texts[4].strip()  # e.g., "5,000"
            status = texts[5].strip()  # e.g., "적중안됨"

            # Parse amount (may or may not have "원" suffix)
            total_amount = int(re.sub(r"[^\d]", "", amount_text)) if amount_text else 0

            # Extract game type and round from game name
            game_type = ""
            round_number = ""
            for keyword in ("프로토 승부식", "프로토 기록식", "축구토토", "야구토토",
                            "농구토토", "배구토토", "골프토토", "승부식", "기록식"):
                if keyword in game_name:
                    game_type = keyword
                    break
            round_match = re.search(r"(\d+)회차", game_name)
            if round_match:
                round_number = round_match.group(1)

            # Determine result
            result: str | None = None
            if status in _RESULT_STATUSES:
                result = status

            if not slip_id:
                slip_id = f"row_{index}"

            # Use slip_id as btkNum fallback
            if not detail_params.get("btkNum") and slip_id != f"row_{index}":
                detail_params["btkNum"] = slip_id

            return BetSlip(
                slip_id=slip_id,
                game_type=game_type,
                round_number=round_number,
                status=status,
                purchase_datetime=purchase_dt,
                total_amount=total_amount,
                potential_payout=0,
                combined_odds=0.0,
                result=result,
                actual_payout=0,
                matches=[],
            ), detail_params

        # Fallback: heuristic parsing for unknown table layouts
        return self._parse_row_heuristic(texts, index), detail_params

    # ------------------------------------------------------------------
    # Match detail fetching via game detail API
    # ------------------------------------------------------------------

    async def _fetch_match_details(
        self,
        page: Page,
        slips: list[BetSlip],
        detail_params_list: list[dict],
    ) -> None:
        """Fetch match details for each slip via /mypgPurWin/getGameDetail.do API.

        Uses a single page.evaluate() with Promise.all to fetch all details
        in parallel, instead of serial per-slip calls.
        """
        # Build list of (index, params) for slips that have btkNum
        valid_entries: list[tuple[int, dict]] = [
            (i, params)
            for i, (slip, params) in enumerate(zip(slips, detail_params_list))
            if params.get("btkNum")
        ]
        if not valid_entries:
            return

        params_list = [params for _, params in valid_entries]

        try:
            # Batch all API calls into a single evaluate with Promise.all
            results = await page.evaluate(
                """(paramsList) => {
                    return Promise.all(paramsList.map(params =>
                        new Promise((resolve) => {
                            const timeout = setTimeout(() => resolve(null), 30000);
                            try {
                                if (typeof requestClient !== 'undefined' && requestClient.requestPostMethod) {
                                    requestClient.requestPostMethod(
                                        "/mypgPurWin/getGameDetail.do",
                                        params,
                                        true,
                                        function(data) {
                                            clearTimeout(timeout);
                                            resolve(JSON.stringify(data));
                                        }
                                    );
                                } else {
                                    clearTimeout(timeout);
                                    resolve(null);
                                }
                            } catch(e) {
                                clearTimeout(timeout);
                                resolve(null);
                            }
                        })
                    ));
                }""",
                params_list,
            )
        except Exception as exc:
            logger.warning("Batch detail fetch failed: %s, falling back to serial", exc)
            await self._fetch_match_details_serial(page, slips, detail_params_list)
            return

        # Process results
        debug_saved = False
        for (slip_idx, _params), result in zip(valid_entries, results):
            if result is None:
                continue
            try:
                # Save first response for debugging
                if not debug_saved:
                    debug_path = Path("storage/debug_game_detail.json")
                    debug_path.parent.mkdir(parents=True, exist_ok=True)
                    debug_path.write_text(result, encoding="utf-8")
                    logger.info(
                        "Game detail debug saved (%d bytes) for %s",
                        len(result),
                        slips[slip_idx].slip_id,
                    )
                    debug_saved = True

                data = json.loads(result)
                matches, slip_meta = self._parse_game_detail(data)
                if matches:
                    slips[slip_idx].matches = matches
                    if slip_meta.get("combined_odds"):
                        slips[slip_idx].combined_odds = slip_meta["combined_odds"]
                    if slip_meta.get("potential_payout"):
                        slips[slip_idx].potential_payout = slip_meta["potential_payout"]
                    logger.info(
                        "Parsed %d matches for slip %s",
                        len(matches),
                        slips[slip_idx].slip_id,
                    )
            except Exception as exc:
                logger.debug("Failed to parse detail for %s: %s", slips[slip_idx].slip_id, exc)

    async def _fetch_match_details_serial(
        self,
        page: Page,
        slips: list[BetSlip],
        detail_params_list: list[dict],
    ) -> None:
        """Fallback: fetch match details one at a time (used if batch fails)."""
        for slip, params in zip(slips, detail_params_list):
            if not params.get("btkNum"):
                continue
            try:
                result = await page.evaluate(
                    """(params) => {
                        return new Promise((resolve, reject) => {
                            const timeout = setTimeout(() => reject('timeout'), 30000);
                            try {
                                if (typeof requestClient !== 'undefined' && requestClient.requestPostMethod) {
                                    requestClient.requestPostMethod(
                                        "/mypgPurWin/getGameDetail.do",
                                        params,
                                        true,
                                        function(data) {
                                            clearTimeout(timeout);
                                            resolve(JSON.stringify(data));
                                        }
                                    );
                                } else {
                                    clearTimeout(timeout);
                                    reject('requestClient not available');
                                }
                            } catch(e) {
                                clearTimeout(timeout);
                                reject(e.message);
                            }
                        });
                    }""",
                    params,
                )
                data = json.loads(result)
                matches, slip_meta = self._parse_game_detail(data)
                if matches:
                    slip.matches = matches
                    if slip_meta.get("combined_odds"):
                        slip.combined_odds = slip_meta["combined_odds"]
                    if slip_meta.get("potential_payout"):
                        slip.potential_payout = slip_meta["potential_payout"]
                    logger.info("Parsed %d matches for slip %s", len(matches), slip.slip_id)
            except Exception as exc:
                logger.debug("Failed to fetch detail for %s: %s", slip.slip_id, exc)

    # Sport item codes → readable names
    _ITEM_CODES = {
        "SC": "축구", "BK": "농구", "BB": "야구",
        "VB": "배구", "GF": "골프",
    }

    # Proto victory mark codes → selection names
    _MARK_LABELS = {"1": "승", "2": "무", "3": "패"}

    def _parse_game_detail(self, data: dict) -> tuple[list[MatchBet], dict]:
        """Parse /mypgPurWin/getGameDetail.do JSON response.

        Returns (matches, slip_meta) where slip_meta contains additional
        slip-level info like combined_odds, potential_payout.
        """
        matches: list[MatchBet] = []
        slip_meta: dict = {}

        purchase = data.get("purchase", {})
        sports_lottery = purchase.get("sportsLottery", {})
        marking_data = data.get("markingData", {})

        # Extract slip-level metadata
        proto_total_allot = sports_lottery.get("protoVicTotalAllot")
        if proto_total_allot:
            slip_meta["combined_odds"] = float(proto_total_allot)

        buy_amount = purchase.get("buyAmount", {})
        total_buy = buy_amount.get("totalBuyAmount", 0) if isinstance(buy_amount, dict) else 0
        if total_buy and proto_total_allot:
            slip_meta["potential_payout"] = int(float(proto_total_allot) * int(total_buy))

        # Match details from slipPaperAndScheduleSetList (primary source)
        spssl = marking_data.get("slipPaperAndScheduleSetList", [])

        for idx, item in enumerate(spssl):
            try:
                sched = item.get("schedule", {})
                slip_paper = item.get("slipPaper", {})

                match_seq = int(sched.get("matchSeq", idx + 1))
                item_code = sched.get("itemCode", "")
                sport = self._ITEM_CODES.get(item_code, item_code)
                league = sched.get("leagueName", "")
                home_team = sched.get("homeName", "")
                away_team = sched.get("awayName", "")

                # Bet selection from markInfo: [matchSeq, selectionCode]
                mark_info = slip_paper.get("markInfo", [])
                bet_selection = ""
                if len(mark_info) >= 2:
                    bet_selection = self._MARK_LABELS.get(str(mark_info[1]), str(mark_info[1]))

                # Odds: use the slip paper's allot (user's selected odds)
                odds = float(slip_paper.get("allot", 0))

                # Convert gameDate timestamp (ms) to readable string
                game_date_ms = sched.get("gameDate")
                match_dt = ""
                if game_date_ms:
                    from datetime import datetime, timezone, timedelta
                    kst = timezone(timedelta(hours=9))
                    dt = datetime.fromtimestamp(game_date_ms / 1000, tz=kst)
                    match_dt = dt.strftime("%m/%d %H:%M")

                # Win status for this match
                win_status = slip_paper.get("winStatus")

                # Score and actual game result
                score = sched.get("mchScore") or ""
                game_result_code = sched.get("gameResult")
                game_result = ""
                if score and ":" in score:
                    h, a = score.split(":")[:2]
                    try:
                        hi, ai = int(h), int(a)
                        if hi > ai:
                            game_result = "승"
                        elif hi == ai:
                            game_result = "무"
                        else:
                            game_result = "패"
                    except ValueError:
                        pass

                matches.append(MatchBet(
                    match_number=match_seq,
                    sport=sport,
                    league=league,
                    home_team=home_team,
                    away_team=away_team,
                    bet_selection=bet_selection,
                    odds=odds,
                    match_datetime=match_dt,
                    result=win_status,
                    score=score,
                    game_result=game_result,
                ))
            except Exception as exc:
                logger.debug("Failed to parse match %d: %s", idx, exc)

        return matches, slip_meta

    @staticmethod
    def _parse_row_heuristic(texts: list[str], index: int) -> BetSlip | None:
        """Fallback heuristic parser for unknown table structures."""
        slip_id = ""
        status = ""
        game_type = ""
        round_number = ""
        total_amount = 0
        potential_payout = 0
        combined_odds = 0.0
        purchase_dt = ""

        for t in texts:
            if t in _ALL_STATUSES:
                status = t
            elif re.match(r"^[\dA-Fa-f]{4}(-[\dA-Fa-f]{4}){3}$", t):
                slip_id = t  # ticket number like "B30B-4867-2431-D450"
            elif re.match(r"^\d{8,}$", t.replace("-", "")):
                if not slip_id:
                    slip_id = t
            elif re.match(r"^\d{2,4}[.-]\d{2}[.-]\d{2}", t):
                purchase_dt = t
            elif "회" in t:
                round_number = t
            elif re.match(r"^[\d,]+원?$", t) and re.search(r"\d", t):
                amount = int(re.sub(r"[^\d]", "", t))
                if amount > 0:
                    if total_amount == 0:
                        total_amount = amount
                    else:
                        potential_payout = amount
            elif re.match(r"^\d+\.\d+$", t):
                combined_odds = float(t)

        if not slip_id:
            slip_id = f"row_{index}"

        for keyword in ("프로토", "토토", "승부식", "기록식", "축구", "야구", "농구"):
            for t in texts:
                if keyword in t:
                    game_type = t
                    break
            if game_type:
                break

        result: str | None = None
        if status in _RESULT_STATUSES:
            result = status

        return BetSlip(
            slip_id=slip_id,
            game_type=game_type,
            round_number=round_number,
            status=status,
            purchase_datetime=purchase_dt,
            total_amount=total_amount,
            potential_payout=potential_payout,
            combined_odds=combined_odds,
            result=result,
            actual_payout=0,
            matches=[],
        )

    # ------------------------------------------------------------------
    # Available games scraping (main page)
    # ------------------------------------------------------------------

    async def scrape_available_games(self, page: Page) -> tuple[str, list[GameSchedule]]:
        """Scrape available games from gameSlip.do page for full list.

        The main page only shows a subset (e.g. 5/34). The gameSlip.do page
        accessed via the sidebar link shows all games.

        Returns (round_title, games).
        """
        # 1. Navigate to main page
        await page.goto(self._config.base_url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_load_state("networkidle", timeout=15000)
        await self._dismiss_popups(page)

        # 2. Round title (e.g. "승부식 19회차")
        round_title = ""
        try:
            title_el = page.locator("#mainProtoTitle")
            if await title_el.count() > 0:
                round_title = (await title_el.text_content() or "").strip()
        except Exception:
            pass

        # 3. Try to navigate to gameSlip.do for full game list
        navigated_to_slip = False
        try:
            slip_link_el = page.locator('.asideGameList a[data-url*="gmId=G101"]').first
            if await slip_link_el.count() > 0:
                slip_url = await slip_link_el.get_attribute("data-url")
                if slip_url:
                    logger.info("Found gameSlip.do link: %s", slip_url)
                    await page.evaluate(f"location.href = '{slip_url}'")
                    await page.wait_for_load_state("domcontentloaded")
                    try:
                        await page.wait_for_selector("#tbd_gmBuySlipList tr", timeout=15000)
                    except Exception:
                        await page.wait_for_load_state("networkidle", timeout=10000)
                    await self._dismiss_popups(page)

                    # Check for error page
                    if await page.locator(".errorArea").count() > 0:
                        logger.warning("gameSlip.do returned error page, falling back to main page")
                        await page.goto(self._config.base_url, wait_until="domcontentloaded", timeout=60000)
                        await page.wait_for_load_state("networkidle", timeout=15000)
                        await self._dismiss_popups(page)
                    else:
                        navigated_to_slip = True
                        logger.info("Navigated to gameSlip.do: %s", page.url)

                        # Save debug HTML
                        html = await page.content()
                        debug_path = Path("storage/debug_gameslip.html")
                        debug_path.parent.mkdir(parents=True, exist_ok=True)
                        debug_path.write_text(html, encoding="utf-8")
                        logger.info("gameSlip.do HTML saved (%d bytes)", len(html))
            else:
                logger.info("No sidebar link for gmId=G101 found, using main page")
        except Exception as exc:
            logger.warning("Failed to navigate to gameSlip.do: %s, using main page", exc)

        # 4. Parse game rows from #tbd_gmBuySlipList (same table on both pages)
        rows = page.locator("#tbd_gmBuySlipList tr[data-matchseq]")
        count = await rows.count()
        page_name = "gameSlip.do" if navigated_to_slip else "main page"
        logger.info("Found %d game rows on %s", count, page_name)

        games: list[GameSchedule] = []
        for i in range(count):
            try:
                game = await self._parse_game_row(rows.nth(i))
                if game:
                    games.append(game)
            except Exception as exc:
                logger.debug("Failed to parse game row %d: %s", i, exc)

        logger.info("Parsed %d available games (%s)", len(games), round_title)
        return round_title, games

    async def _parse_game_row(self, row) -> GameSchedule | None:
        """Parse a single <tr> from #tbd_gmBuySlipList.

        DOM structure per row (td indices):
        [0] match_seq number
        [1] deadline (e.g. "02.12 (목)\n19:00 마감")
        [2] sport icon + league
        [3] game type badge (일반/핸디캡/언더오버/SUM)
        [4] home vs away (+ handicap info)
        [5] odds buttons
        [6] game datetime
        [7] stadium tooltip
        [8] detail button (정보)
        """
        cells = row.locator("td")
        cell_count = await cells.count()
        if cell_count < 7:
            return None

        # [0] match_seq
        match_seq_text = (await cells.nth(0).text_content() or "").strip()
        # Remove non-numeric chars (e.g. "긴급 공지닫기162" → "162")
        match_seq_digits = re.sub(r"[^\d]", "", match_seq_text)
        if not match_seq_digits:
            return None
        match_seq = int(match_seq_digits)

        # [1] deadline
        deadline_raw = (await cells.nth(1).inner_text() or "").strip()
        deadline = deadline_raw.replace("\n", " ").replace("마감", "마감").strip()

        # [2] sport + league
        sport_cell = cells.nth(2)
        sport_icon = sport_cell.locator("span.icoGame")
        sport = ""
        if await sport_icon.count() > 0:
            sport = (await sport_icon.first.text_content() or "").strip()
        league_el = sport_cell.locator("span.db")
        league = ""
        if await league_el.count() > 0:
            league = (await league_el.first.text_content() or "").strip()

        # [3] game type
        game_type_cell = cells.nth(3)
        badge = game_type_cell.locator("span.badge")
        game_type = ""
        if await badge.count() > 0:
            game_type = (await badge.first.text_content() or "").strip()

        # [4] home vs away + handicap
        team_cell = cells.nth(4)
        cell_divs = team_cell.locator("div.scoreDiv div.cell")
        home_team = ""
        away_team = ""
        if await cell_divs.count() >= 2:
            # Home team: first cell > first direct span text
            home_span = cell_divs.nth(0).locator("> span").first
            if await home_span.count() > 0:
                home_team = (await home_span.text_content() or "").strip()
            # Away team: second cell > first direct span text
            away_span = cell_divs.nth(1).locator("> span").first
            if await away_span.count() > 0:
                away_team = (await away_span.text_content() or "").strip()

        # Handicap value
        handicap = ""
        handicap_el = team_cell.locator("span.udPoint")
        if await handicap_el.count() > 0:
            handicap = (await handicap_el.first.text_content() or "").strip()

        # [5] odds buttons
        odds: dict[str, float] = {}
        buttons = cells.nth(5).locator("button.btnChk")
        btn_count = await buttons.count()
        for j in range(btn_count):
            btn = buttons.nth(j)
            # Button has: <span>승</span><span class="db">2.75...</span>
            spans = btn.locator("> span")
            span_count = await spans.count()
            if span_count >= 2:
                # Label span (skip .blind)
                label = ""
                for s in range(span_count):
                    sp = spans.nth(s)
                    cls = await sp.get_attribute("class") or ""
                    if "blind" in cls or "icoG" in cls:
                        continue
                    text = (await sp.text_content() or "").strip()
                    if text and not label:
                        label = text
                    elif text and label:
                        # This is the odds value, strip non-numeric suffixes
                        odds_match = re.match(r"([\d.]+)", text)
                        if odds_match:
                            odds[label] = float(odds_match.group(1))
                        break

        # [6] game datetime
        game_datetime_raw = (await cells.nth(6).inner_text() or "").strip()
        game_datetime = game_datetime_raw.replace("\n", " ")

        # [7] stadium (from tooltip)
        stadium = ""
        if cell_count > 7:
            stadium_tooltip = cells.nth(7).locator("div.ttHLayer span")
            if await stadium_tooltip.count() > 0:
                stadium = (await stadium_tooltip.first.text_content() or "").strip()

        return GameSchedule(
            match_seq=match_seq,
            sport=sport,
            league=league,
            game_type=game_type,
            home_team=home_team,
            away_team=away_team,
            odds=odds,
            deadline=deadline,
            game_datetime=game_datetime,
            stadium=stadium,
            handicap=handicap,
        )

    @staticmethod
    async def _dismiss_popups(page: Page) -> None:
        """Close any overlay popups including jQuery UI dialogs."""
        # Force-close jQuery UI dialogs (BUIDynamicModal) via JS
        try:
            await page.evaluate("""() => {
                document.querySelectorAll('.ui-dialog').forEach(d => d.remove());
                document.querySelectorAll('.ui-widget-overlay').forEach(o => o.remove());
            }""")
        except Exception:
            pass

        for sel in ['button:has-text("확인")', 'button:has-text("닫기")', ".popup_close", ".layer_close",
                     ".ui-dialog-titlebar-close"]:
            try:
                loc = page.locator(sel).first
                if await loc.is_visible(timeout=1000):
                    await loc.click()
                    await page.wait_for_timeout(300)
            except Exception:
                continue
