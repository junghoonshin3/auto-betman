(() => {
  "use strict";

  const STORAGE_KEYS = Object.freeze({
    webhookUrl: "webhook_url",
    sentMap: "sent_map_v1",
    lastSeenHeadSlipId: "last_seen_head_slip_id_v1",
  });

  const SESSION_KEYS = Object.freeze({
    pendingCapture: "betman_pending_capture_v1",
    pendingCaptureQueue: "betman_pending_capture_queue_v1",
  });

  const SENT_TTL_MS = 24 * 60 * 60 * 1000;
  const DETECT_DEBOUNCE_MS = 500;
  const STABLE_WAIT_TIMEOUT_MS = 2000;
  const STABLE_WAIT_INTERVAL_MS = 250;
  const HISTORY_READY_TIMEOUT_MS = 9000;
  const PAPER_READY_TIMEOUT_MS = 5000;
  const API_REQUEST_TIMEOUT_MS = 10000;
  const PENDING_CAPTURE_TTL_MS = 90 * 1000;
  const MAX_PENDING_RETRIES = 3;
  const MAX_UPLOAD_BYTES = 7_500_000;
  const PRIMARY_MAX_WIDTH = 1600;
  const FALLBACK_MAX_WIDTH = 1200;

  const PURCHASE_HISTORY_PATH = "/main/mainPage/mypage/myPurchaseWinList.do";
  const PAYMENT_RESULT_PATH = "/main/mainPage/mypage/myPaymentResult.do";
  const PURCHASE_TABLE_SELECTOR = "#purchaseWinTable tbody tr";
  const PAYMENT_RESULT_ROWS_SELECTOR = "#purchaseResultTableBody tr";
  const PAYMENT_RESULT_SUCCESS_SELECTOR = "#purchaseSuccess";
  const BRIDGE_REQUEST_EVENT = "betman_push_open_game_paper_request";
  const BRIDGE_RESPONSE_EVENT = "betman_push_open_game_paper_response";
  const BRIDGE_REQUEST_POST_METHOD_REQUEST_EVENT = "betman_push_request_post_method_request";
  const BRIDGE_REQUEST_POST_METHOD_RESPONSE_EVENT = "betman_push_request_post_method_response";
  const ALERT_CONTENT_TEXT = "🚨 응큼픽 감지!!!!!!!!!!!!!!!!!!!!!!! 🚨";

  const PAPER_AREA_SELECTORS = ["#paperTr #paperArea", "#paperArea"];

  const SLIP_ID_PATTERN = /[A-Z0-9]{4}(?:-[A-Z0-9]{4}){3}/g;
  const GO_MY_PUR_WIN_DETAIL_PATTERN = /goMyPurWinDetail\s*\(([\s\S]*?)\)\s*;?/i;

  const ERROR_CODE = Object.freeze({
    EXTENSION_RUNTIME_UNAVAILABLE: "extension_runtime_unavailable",
    EXTENSION_MESSAGE_FAILED: "extension_message_failed",
    SCREENSHOT_CAPTURE_FAILED: "screenshot_capture_failed",
    PAPER_AREA_CAPTURE_FAILED: "paperArea_capture_failed",
    HISTORY_POLL_FAILED: "history_poll_failed",
    ROW_NOT_FOUND: "row_not_found",
    OPEN_GAME_PAPER_FAILED: "openGamePaper_failed",
    PAPER_AREA_NOT_READY: "paperArea_not_ready",
    WEBHOOK_SEND_FAILED: "webhook_send_failed",
  });

  const RETRIABLE_PENDING_QUEUE_CODES = new Set([
    ERROR_CODE.ROW_NOT_FOUND,
    ERROR_CODE.PAPER_AREA_NOT_READY,
    ERROR_CODE.HISTORY_POLL_FAILED,
    ERROR_CODE.OPEN_GAME_PAPER_FAILED,
  ]);

  const frameKind = (() => {
    try {
      return window.top === window ? "top" : "iframe";
    } catch (_error) {
      return "unknown";
    }
  })();

  const extensionVersion = (() => {
    try {
      return String(chrome.runtime.getManifest().version || "unknown");
    } catch (_error) {
      return "unknown";
    }
  })();

  const extensionId = (() => {
    try {
      return String(chrome.runtime.id || "");
    } catch (_error) {
      return "";
    }
  })();

  const extensionRuntimeAvailable = (() => {
    try {
      return Boolean(
        chrome &&
        chrome.runtime &&
        typeof chrome.runtime.sendMessage === "function" &&
        chrome.storage &&
        chrome.storage.local,
      );
    } catch (_error) {
      return false;
    }
  })();

  let detectTimer = null;
  let detectInProgress = false;
  let pushInProgress = false;
  let pendingResumeInProgress = false;
  let bridgeReadyPromise = null;
  let cachedWebhookUrl = "";
  let navigationHooksInstalled = false;

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  function normalizeText(input) {
    return String(input || "").replace(/\s+/g, " ").trim();
  }

  function createError(code, details) {
    const error = new Error(code);
    error.code = code;
    if (details) {
      error.details = details;
    }
    return error;
  }

  function getErrorCode(error) {
    return normalizeText((error && (error.code || error.message)) || String(error || ""));
  }

  function isDocumentNode(value) {
    return Boolean(value) && value.nodeType === 9 && typeof value.querySelectorAll === "function";
  }

  function isElementNode(value) {
    return Boolean(value) && value.nodeType === 1;
  }

  function isVisible(element) {
    if (!isElementNode(element)) {
      return false;
    }
    const view = element.ownerDocument?.defaultView || window;
    const style = view.getComputedStyle(element);
    if (style.display === "none" || style.visibility === "hidden" || style.opacity === "0") {
      return false;
    }
    const rect = element.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }

  function hashString(text) {
    let hash = 2166136261;
    for (let i = 0; i < text.length; i += 1) {
      hash ^= text.charCodeAt(i);
      hash += (hash << 1) + (hash << 4) + (hash << 7) + (hash << 8) + (hash << 24);
    }
    return (hash >>> 0).toString(16);
  }

  function extractSlipId(text) {
    const matches = String(text || "").match(SLIP_ID_PATTERN);
    return matches && matches.length > 0 ? matches[0] : "";
  }

  function isValidWebhookUrl(value) {
    const text = String(value || "").trim();
    const pattern = /^https:\/\/(?:canary\.|ptb\.)?discord\.com\/api\/webhooks\/\d+\/[A-Za-z0-9._-]+/;
    return pattern.test(text);
  }

  function getPurchaseHistoryUrl() {
    return `${location.origin}${PURCHASE_HISTORY_PATH}`;
  }

  function isPurchaseHistoryPage() {
    return location.pathname.includes(PURCHASE_HISTORY_PATH);
  }

  function isPaymentResultPage() {
    return location.pathname.includes(PAYMENT_RESULT_PATH);
  }

  function splitFunctionArguments(raw) {
    const text = String(raw || "");
    const out = [];
    let current = "";
    let quote = "";
    let escaped = false;
    for (let i = 0; i < text.length; i += 1) {
      const ch = text[i];
      if (escaped) {
        current += ch;
        escaped = false;
        continue;
      }
      if (ch === "\\") {
        current += ch;
        escaped = true;
        continue;
      }
      if (quote) {
        current += ch;
        if (ch === quote) {
          quote = "";
        }
        continue;
      }
      if (ch === "'" || ch === "\"") {
        quote = ch;
        current += ch;
        continue;
      }
      if (ch === ",") {
        out.push(current.trim());
        current = "";
        continue;
      }
      current += ch;
    }
    if (current.trim()) {
      out.push(current.trim());
    }
    return out;
  }

  function decodeFunctionToken(token) {
    const text = normalizeText(token || "");
    if (!text) {
      return "";
    }
    if (
      (text.startsWith("'") && text.endsWith("'")) ||
      (text.startsWith("\"") && text.endsWith("\""))
    ) {
      return text.slice(1, -1);
    }
    return text;
  }

  function extractSlipIdFromGoMyPurWinDetailScript(scriptText) {
    const text = String(scriptText || "");
    if (!text) {
      return "";
    }
    const match = text.match(GO_MY_PUR_WIN_DETAIL_PATTERN);
    if (match && match[1]) {
      const args = splitFunctionArguments(match[1]).map((token) => decodeFunctionToken(token));
      if (args.length >= 4) {
        const fromFourthArg = extractSlipId(args[3]) || normalizeText(args[3]);
        if (fromFourthArg) {
          return fromFourthArg;
        }
      }
      const fromJoinedArgs = extractSlipId(args.join(" "));
      if (fromJoinedArgs) {
        return fromJoinedArgs;
      }
    }
    return extractSlipId(text);
  }

  function collectPaymentResultSlipIds(doc = document) {
    if (!isDocumentNode(doc)) {
      return [];
    }
    const rows = Array.from(doc.querySelectorAll(PAYMENT_RESULT_ROWS_SELECTOR));
    const slipIds = [];
    const seen = new Set();
    for (const row of rows) {
      let rowSlipId = "";
      const links = Array.from(row.querySelectorAll("a[onclick*='goMyPurWinDetail'],a[href*='goMyPurWinDetail']"));
      for (const link of links) {
        const scriptText = normalizeText(link.getAttribute("onclick") || link.getAttribute("href") || "");
        rowSlipId = extractSlipIdFromGoMyPurWinDetailScript(scriptText);
        if (rowSlipId) {
          break;
        }
      }

      if (!rowSlipId) {
        const attrs = Array.from(row.querySelectorAll("[onclick],[href]"))
          .map((element) => normalizeText(element.getAttribute("onclick") || element.getAttribute("href") || ""))
          .join(" ");
        rowSlipId = extractSlipId(`${getRowText(row)} ${attrs}`);
      }

      if (!rowSlipId || seen.has(rowSlipId)) {
        continue;
      }
      seen.add(rowSlipId);
      slipIds.push(rowSlipId);
    }
    return slipIds;
  }

  function isPaymentResultSuccessReady(doc = document) {
    if (!isPaymentResultPage() || !isDocumentNode(doc)) {
      return false;
    }
    const successArea = doc.querySelector(PAYMENT_RESULT_SUCCESS_SELECTOR);
    if (!isVisible(successArea)) {
      return false;
    }
    return doc.querySelectorAll(PAYMENT_RESULT_ROWS_SELECTOR).length > 0;
  }

  function extractPurchaseNosFromInlineScript(doc = document) {
    if (!isDocumentNode(doc)) {
      return "";
    }
    const scripts = Array.from(doc.querySelectorAll("script"));
    const patterns = [
      /\bvar\s+purchaseNos\s*=\s*"([^"]+)"/i,
      /\bvar\s+purchaseNos\s*=\s*'([^']+)'/i,
      /\bpurchaseNos\s*[:=]\s*"([^"]+)"/i,
      /\bpurchaseNos\s*[:=]\s*'([^']+)'/i,
    ];
    for (const script of scripts) {
      const text = script && typeof script.textContent === "string" ? script.textContent : "";
      if (!text) {
        continue;
      }
      for (const pattern of patterns) {
        const match = text.match(pattern);
        if (match && match[1]) {
          return normalizeText(match[1]);
        }
      }
    }
    return "";
  }

  function extractPurchaseNosFromDom(doc = document) {
    if (!isDocumentNode(doc)) {
      return "";
    }
    const values = Array.from(
      doc.querySelectorAll("#purchaseResultTableBody input[name='itemChk'][value]"),
    )
      .map((element) => normalizeText(element.value || ""))
      .filter(Boolean);
    return normalizeSlipIdList(values).join(",");
  }

  function getPaymentResultPurchaseNos(doc = document) {
    const inlineValue = extractPurchaseNosFromInlineScript(doc);
    if (inlineValue) {
      return inlineValue;
    }
    return extractPurchaseNosFromDom(doc);
  }

  function pickFirstValue(source, keys, fallback = "") {
    if (!source || typeof source !== "object") {
      return fallback;
    }
    for (const key of keys) {
      if (source[key] != null && source[key] !== "") {
        return source[key];
      }
    }
    return fallback;
  }

  function extractPaymentResultBuyList(payload) {
    if (!payload || typeof payload !== "object") {
      return [];
    }
    if (Array.isArray(payload.buyList)) {
      return payload.buyList;
    }
    if (payload.result && typeof payload.result === "object" && Array.isArray(payload.result.buyList)) {
      return payload.result.buyList;
    }
    if (payload.items && typeof payload.items === "object" && Array.isArray(payload.items.buyList)) {
      return payload.items.buyList;
    }
    return [];
  }

  function createSlipEntryFromBuyItem(item) {
    if (!item || typeof item !== "object") {
      return null;
    }
    const slipId = normalizeText(pickFirstValue(item, ["btkNum", "buyNo", "slipId"], ""));
    if (!slipId) {
      return null;
    }
    return { slipId };
  }

  function normalizeSlipIdList(input) {
    const values = Array.isArray(input) ? input : [];
    const next = [];
    const seen = new Set();
    for (const raw of values) {
      const text = normalizeText(raw || "");
      const slipId = extractSlipId(text) || text;
      if (!slipId || seen.has(slipId)) {
        continue;
      }
      seen.add(slipId);
      next.push(slipId);
    }
    return next;
  }

  function buildPendingQueueFingerprint(slipIds) {
    return normalizeSlipIdList(slipIds).join("|");
  }

  function normalizePendingQueuePayload(payload) {
    if (!payload || typeof payload !== "object") {
      return null;
    }
    const slipIds = normalizeSlipIdList(payload.slipIds);
    if (!slipIds.length) {
      return null;
    }
    const createdAtRaw = Number(payload.createdAt || 0);
    const createdAt = Number.isFinite(createdAtRaw) && createdAtRaw > 0 ? createdAtRaw : Date.now();
    const reason = normalizeText(payload.reason || "");
    const attemptsSource = payload.attemptsBySlip && typeof payload.attemptsBySlip === "object"
      ? payload.attemptsBySlip
      : {};
    const attemptsBySlip = {};
    for (const slipId of slipIds) {
      const attemptRaw = Number(attemptsSource[slipId] || 0);
      attemptsBySlip[slipId] = Number.isFinite(attemptRaw) && attemptRaw >= 0 ? Math.floor(attemptRaw) : 0;
    }
    const fingerprint = normalizeText(payload.fingerprint || "") || buildPendingQueueFingerprint(slipIds);
    return {
      slipIds,
      createdAt,
      reason,
      attemptsBySlip,
      fingerprint,
    };
  }

  async function sendRuntimeMessage(type, payload = null) {
    if (!extensionRuntimeAvailable) {
      throw createError(ERROR_CODE.EXTENSION_RUNTIME_UNAVAILABLE);
    }
    return new Promise((resolve, reject) => {
      try {
        chrome.runtime.sendMessage({ type, payload }, (response) => {
          const lastError = chrome.runtime.lastError;
          if (lastError) {
            reject(createError(ERROR_CODE.EXTENSION_MESSAGE_FAILED, { message: String(lastError.message || "") }));
            return;
          }
          resolve(response || {});
        });
      } catch (error) {
        reject(createError(ERROR_CODE.EXTENSION_MESSAGE_FAILED, { message: String(error && error.message ? error.message : error) }));
      }
    });
  }

  async function refreshWebhookCache() {
    if (!extensionRuntimeAvailable) {
      cachedWebhookUrl = "";
      return cachedWebhookUrl;
    }
    const data = await chrome.storage.local.get([STORAGE_KEYS.webhookUrl]);
    cachedWebhookUrl = normalizeText(data[STORAGE_KEYS.webhookUrl] || "");
    return cachedWebhookUrl;
  }

  async function getWebhookUrl() {
    if (cachedWebhookUrl) {
      return cachedWebhookUrl;
    }
    return refreshWebhookCache();
  }

  async function setLastSeenHeadSlipId(slipId) {
    if (!extensionRuntimeAvailable) {
      return;
    }
    await chrome.storage.local.set({
      [STORAGE_KEYS.lastSeenHeadSlipId]: normalizeText(slipId || ""),
    });
  }

  function loadPendingCapture() {
    try {
      const raw = sessionStorage.getItem(SESSION_KEYS.pendingCapture);
      if (!raw) {
        return null;
      }
      const parsed = JSON.parse(raw);
      if (!parsed || typeof parsed !== "object") {
        return null;
      }
      const slipId = normalizeText(parsed.slipId || "");
      if (!slipId) {
        return null;
      }
      const createdAt = Number(parsed.createdAt || 0);
      const attempts = Number(parsed.attempts || 0);
      const reason = normalizeText(parsed.reason || "");
      return {
        slipId,
        createdAt: Number.isFinite(createdAt) && createdAt > 0 ? createdAt : Date.now(),
        attempts: Number.isFinite(attempts) && attempts >= 0 ? attempts : 0,
        reason,
      };
    } catch (_error) {
      return null;
    }
  }

  function savePendingCapture(payload) {
    try {
      const normalized = {
        slipId: normalizeText(payload && payload.slipId ? payload.slipId : ""),
        createdAt: Number(payload && payload.createdAt ? payload.createdAt : Date.now()),
        attempts: Number(payload && payload.attempts ? payload.attempts : 0),
        reason: normalizeText(payload && payload.reason ? payload.reason : ""),
      };
      if (!normalized.slipId) {
        sessionStorage.removeItem(SESSION_KEYS.pendingCapture);
        return;
      }
      sessionStorage.setItem(SESSION_KEYS.pendingCapture, JSON.stringify(normalized));
    } catch (_error) {
      // Ignore sessionStorage errors.
    }
  }

  function clearPendingCapture() {
    try {
      sessionStorage.removeItem(SESSION_KEYS.pendingCapture);
    } catch (_error) {
      // Ignore sessionStorage errors.
    }
  }

  function isPendingCaptureFresh(payload) {
    if (!payload || !payload.createdAt) {
      return false;
    }
    return Date.now() - Number(payload.createdAt) <= PENDING_CAPTURE_TTL_MS;
  }

  function loadPendingCaptureQueue() {
    try {
      const raw = sessionStorage.getItem(SESSION_KEYS.pendingCaptureQueue);
      if (raw) {
        const parsed = JSON.parse(raw);
        const normalizedQueue = normalizePendingQueuePayload(parsed);
        if (normalizedQueue) {
          return normalizedQueue;
        }
      }
    } catch (_error) {
      // Ignore sessionStorage parse errors.
    }

    const legacyPending = loadPendingCapture();
    if (!legacyPending) {
      return null;
    }
    if (!isPendingCaptureFresh(legacyPending)) {
      clearPendingCapture();
      return null;
    }

    const migratedQueue = normalizePendingQueuePayload({
      slipIds: [legacyPending.slipId],
      createdAt: legacyPending.createdAt,
      reason: legacyPending.reason || "legacy_pending_capture",
      attemptsBySlip: { [legacyPending.slipId]: Number(legacyPending.attempts || 0) },
      fingerprint: legacyPending.slipId,
    });
    if (migratedQueue) {
      savePendingCaptureQueue(migratedQueue);
    }
    clearPendingCapture();
    return migratedQueue;
  }

  function savePendingCaptureQueue(payload) {
    try {
      const normalizedQueue = normalizePendingQueuePayload(payload);
      if (!normalizedQueue) {
        sessionStorage.removeItem(SESSION_KEYS.pendingCaptureQueue);
        return;
      }
      sessionStorage.setItem(SESSION_KEYS.pendingCaptureQueue, JSON.stringify(normalizedQueue));
    } catch (_error) {
      // Ignore sessionStorage errors.
    }
  }

  function clearPendingCaptureQueue() {
    try {
      sessionStorage.removeItem(SESSION_KEYS.pendingCaptureQueue);
      sessionStorage.removeItem(SESSION_KEYS.pendingCapture);
    } catch (_error) {
      // Ignore sessionStorage errors.
    }
  }

  function getFreshPendingCaptureQueue() {
    const queue = loadPendingCaptureQueue();
    if (!queue) {
      return null;
    }
    if (!isPendingCaptureFresh(queue)) {
      clearPendingCaptureQueue();
      return null;
    }
    return queue;
  }

  function enqueuePendingCaptureQueue({ slipIds, reason, reset = false }) {
    const incomingSlipIds = normalizeSlipIdList(slipIds);
    if (!incomingSlipIds.length) {
      return null;
    }

    let mergedSlipIds = [];
    let attemptsBySlip = {};
    if (!reset) {
      const currentQueue = getFreshPendingCaptureQueue();
      if (currentQueue) {
        mergedSlipIds = currentQueue.slipIds.slice();
        attemptsBySlip = { ...currentQueue.attemptsBySlip };
      }
    }

    const mergedSet = new Set(mergedSlipIds);
    for (const slipId of incomingSlipIds) {
      if (mergedSet.has(slipId)) {
        continue;
      }
      mergedSet.add(slipId);
      mergedSlipIds.push(slipId);
      attemptsBySlip[slipId] = Number(attemptsBySlip[slipId] || 0);
    }

    const queue = normalizePendingQueuePayload({
      slipIds: mergedSlipIds,
      createdAt: Date.now(),
      reason: normalizeText(reason || ""),
      attemptsBySlip,
      fingerprint: buildPendingQueueFingerprint(mergedSlipIds),
    });
    if (!queue) {
      return null;
    }
    savePendingCaptureQueue(queue);
    console.warn("[BetmanPushExt] pending_queue_saved", {
      reason: queue.reason || "-",
      slipId: incomingSlipIds[0] || "-",
      attempts: Number(queue.attemptsBySlip[incomingSlipIds[0]] || 0),
      queueSize: queue.slipIds.length,
      doc_url: location.href,
    });
    return queue;
  }

  function isRetriablePendingQueueCode(code) {
    return RETRIABLE_PENDING_QUEUE_CODES.has(normalizeText(code || ""));
  }

  async function loadSentMap() {
    if (!extensionRuntimeAvailable) {
      return {};
    }
    const data = await chrome.storage.local.get([STORAGE_KEYS.sentMap]);
    const value = data[STORAGE_KEYS.sentMap];
    if (!value || typeof value !== "object" || Array.isArray(value)) {
      return {};
    }
    return value;
  }

  function pruneSentMap(map, now = Date.now()) {
    const next = {};
    for (const [key, timestamp] of Object.entries(map || {})) {
      if (typeof timestamp === "number" && now - timestamp <= SENT_TTL_MS) {
        next[key] = timestamp;
      }
    }
    return next;
  }

  async function isDuplicate(key) {
    if (!extensionRuntimeAvailable) {
      return false;
    }
    const cleaned = pruneSentMap(await loadSentMap());
    await chrome.storage.local.set({ [STORAGE_KEYS.sentMap]: cleaned });
    return Boolean(cleaned[key]);
  }

  async function markSent(key) {
    if (!extensionRuntimeAvailable) {
      return;
    }
    const cleaned = pruneSentMap(await loadSentMap());
    cleaned[key] = Date.now();
    await chrome.storage.local.set({ [STORAGE_KEYS.sentMap]: cleaned });
  }

  async function waitForStableContent(element) {
    const deadline = Date.now() + STABLE_WAIT_TIMEOUT_MS;
    let stableCount = 0;
    let lastSignature = "";
    while (Date.now() < deadline) {
      const text = normalizeText(element.innerText || element.textContent).slice(0, 1200);
      const signature = `${element.childElementCount}:${hashString(text)}`;
      if (signature === lastSignature) {
        stableCount += 1;
        if (stableCount >= 2) {
          return;
        }
      } else {
        stableCount = 0;
        lastSignature = signature;
      }
      await sleep(STABLE_WAIT_INTERVAL_MS);
    }
  }

  function resizeCanvas(sourceCanvas, maxWidth) {
    if (sourceCanvas.width <= maxWidth) {
      return sourceCanvas;
    }
    const ratio = maxWidth / sourceCanvas.width;
    const width = Math.max(1, Math.floor(sourceCanvas.width * ratio));
    const height = Math.max(1, Math.floor(sourceCanvas.height * ratio));
    const resized = document.createElement("canvas");
    resized.width = width;
    resized.height = height;
    const ctx = resized.getContext("2d");
    if (!ctx) {
      return sourceCanvas;
    }
    ctx.drawImage(sourceCanvas, 0, 0, width, height);
    return resized;
  }

  function canvasToBlob(canvas, quality) {
    return new Promise((resolve) => {
      canvas.toBlob((blob) => resolve(blob), "image/jpeg", quality);
    });
  }

  async function canvasToCompressedJpegBlob(canvas) {
    const firstCanvas = resizeCanvas(canvas, PRIMARY_MAX_WIDTH);
    const qualityCandidates = [0.85, 0.72, 0.6];
    let bestBlob = null;
    for (const quality of qualityCandidates) {
      const blob = await canvasToBlob(firstCanvas, quality);
      if (!blob) {
        continue;
      }
      bestBlob = blob;
      if (blob.size <= MAX_UPLOAD_BYTES) {
        return blob;
      }
    }

    const fallbackCanvas = resizeCanvas(firstCanvas, FALLBACK_MAX_WIDTH);
    for (const quality of [0.68, 0.56]) {
      const blob = await canvasToBlob(fallbackCanvas, quality);
      if (!blob) {
        continue;
      }
      bestBlob = blob;
      if (blob.size <= MAX_UPLOAD_BYTES) {
        return blob;
      }
    }
    return bestBlob;
  }

  async function captureVisibleTabDataUrl() {
    const response = await sendRuntimeMessage("CAPTURE_VISIBLE_TAB");
    if (!response.ok || !response.dataUrl) {
      console.warn("[BetmanPushExt] CAPTURE_VISIBLE_TAB failed", {
        code: response && response.code ? String(response.code) : "",
        details: response && response.details ? String(response.details) : "",
      });
      throw createError(ERROR_CODE.SCREENSHOT_CAPTURE_FAILED, {
        captureCode: response && response.code ? String(response.code) : "",
        captureDetails: response && response.details ? String(response.details) : "",
        response,
      });
    }
    return response.dataUrl;
  }

  function dataUrlToImage(dataUrl) {
    return new Promise((resolve, reject) => {
      const image = new Image();
      image.onload = () => resolve(image);
      image.onerror = () => reject(new Error("image_decode_failed"));
      image.src = dataUrl;
    });
  }

  async function captureElementToCanvas(element) {
    const dpr = Math.max(1, Number(window.devicePixelRatio || 1));
    const originalX = window.scrollX;
    const originalY = window.scrollY;

    const firstRect = element.getBoundingClientRect();
    const absTop = window.scrollY + firstRect.top;
    const absLeft = window.scrollX + firstRect.left;
    const totalWidth = Math.max(1, Math.floor(firstRect.width));
    const totalHeight = Math.max(1, Math.floor(firstRect.height));

    const output = document.createElement("canvas");
    output.width = Math.max(1, Math.floor(totalWidth * dpr));
    output.height = Math.max(1, Math.floor(totalHeight * dpr));
    const ctx = output.getContext("2d");
    if (!ctx) {
      throw createError(ERROR_CODE.PAPER_AREA_CAPTURE_FAILED);
    }

    const step = Math.max(240, Math.floor(window.innerHeight * 0.72));
    const visited = new Set();

    try {
      for (let offset = 0; offset < totalHeight; offset += step) {
        const targetY = Math.max(0, Math.floor(absTop + offset - 80));
        window.scrollTo({ top: targetY, left: Math.max(0, Math.floor(absLeft - 12)), behavior: "auto" });
        await sleep(220);

        const rect = element.getBoundingClientRect();
        const visibleLeft = Math.max(0, rect.left);
        const visibleTop = Math.max(0, rect.top);
        const visibleRight = Math.min(window.innerWidth, rect.right);
        const visibleBottom = Math.min(window.innerHeight, rect.bottom);

        if (visibleRight <= visibleLeft || visibleBottom <= visibleTop) {
          continue;
        }

        const visibleAbsLeft = window.scrollX + visibleLeft;
        const visibleAbsTop = window.scrollY + visibleTop;
        const signature = `${Math.round(visibleAbsLeft)}:${Math.round(visibleAbsTop)}`;
        if (visited.has(signature)) {
          continue;
        }
        visited.add(signature);

        const dataUrl = await captureVisibleTabDataUrl();
        const image = await dataUrlToImage(dataUrl);

        const clipWidth = visibleRight - visibleLeft;
        const clipHeight = visibleBottom - visibleTop;

        const sx = Math.max(0, Math.floor(visibleLeft * dpr));
        const sy = Math.max(0, Math.floor(visibleTop * dpr));
        const sw = Math.max(1, Math.floor(clipWidth * dpr));
        const sh = Math.max(1, Math.floor(clipHeight * dpr));

        const dx = Math.max(0, Math.floor((visibleAbsLeft - absLeft) * dpr));
        const dy = Math.max(0, Math.floor((visibleAbsTop - absTop) * dpr));

        ctx.drawImage(image, sx, sy, sw, sh, dx, dy, sw, sh);

        if (dy + sh >= output.height - Math.ceil(2 * dpr)) {
          break;
        }
      }
    } finally {
      window.scrollTo({ top: originalY, left: originalX, behavior: "auto" });
    }

    if (visited.size === 0) {
      throw createError(ERROR_CODE.SCREENSHOT_CAPTURE_FAILED);
    }

    return output;
  }

  async function captureElementAsBlob(element) {
    await waitForStableContent(element);
    const canvas = await captureElementToCanvas(element);
    return canvasToCompressedJpegBlob(canvas);
  }

  function buildDedupKey(meta) {
    if (meta.slipId && meta.purchaseTime) {
      return `${meta.slipId}|${meta.purchaseTime}`;
    }
    if (meta.slipId) {
      return `slip:${meta.slipId}`;
    }
    return `hash:${hashString(meta.text.slice(0, 800))}`;
  }

  function buildFileName(meta) {
    const slip = meta.slipId || "unknown";
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    return `betman_purchase_${slip}_${stamp}.jpg`;
  }

  function isDetailAreaRow(row) {
    if (!isElementNode(row)) {
      return true;
    }
    if (row.id === "paperTr") {
      return true;
    }
    const className = normalizeText(row.className || "");
    return className.includes("detailArea");
  }

  function getRowText(row) {
    if (!isElementNode(row)) {
      return "";
    }
    return normalizeText(row.innerText || row.textContent || "");
  }

  function extractOpenGamePaperScriptTextFromElement(element) {
    if (!isElementNode(element)) {
      return "";
    }
    const attrs = ["onclick", "href", "data-onclick", "data-href"];
    for (const attr of attrs) {
      const value = normalizeText(element.getAttribute(attr) || "");
      if (value && /openGamePaper\s*\(/i.test(value)) {
        return value;
      }
    }
    return "";
  }

  function extractRowSlipId(row) {
    if (!isElementNode(row)) {
      return "";
    }

    const rowTextSlipId = extractSlipId(getRowText(row));
    if (rowTextSlipId) {
      return rowTextSlipId;
    }

    const sources = [row, ...Array.from(row.querySelectorAll("[onclick],[href],a,button"))];
    for (const source of sources) {
      const script = extractOpenGamePaperScriptTextFromElement(source);
      if (!script) {
        continue;
      }
      const fromScript = extractSlipId(script);
      if (fromScript) {
        return fromScript;
      }
    }

    return "";
  }

  function collectPurchaseRows(doc = document) {
    if (!isDocumentNode(doc)) {
      return [];
    }
    const rows = Array.from(doc.querySelectorAll(PURCHASE_TABLE_SELECTOR));
    return rows.filter((row) => !isDetailAreaRow(row));
  }

  async function ensurePageBridgeLoaded() {
    if (bridgeReadyPromise) {
      return bridgeReadyPromise;
    }

    bridgeReadyPromise = new Promise((resolve, reject) => {
      try {
        if (window.__betmanPushBridgeReady || window.__betmanPushBridgeInstalled) {
          resolve(true);
          return;
        }

        const existing = document.querySelector("script[data-betman-push-bridge='1']");
        if (existing) {
          const waitStart = Date.now();
          const wait = () => {
            if (window.__betmanPushBridgeReady || window.__betmanPushBridgeInstalled) {
              resolve(true);
              return;
            }
            if (Date.now() - waitStart > 2000) {
              reject(new Error("bridge_load_timeout"));
              return;
            }
            setTimeout(wait, 80);
          };
          wait();
          return;
        }

        const script = document.createElement("script");
        script.src = chrome.runtime.getURL("page_bridge.js");
        script.async = false;
        script.dataset.betmanPushBridge = "1";
        script.onload = () => {
          console.warn("[BetmanPushExt] bridge load success");
          resolve(true);
        };
        script.onerror = () => {
          console.error("[BetmanPushExt] bridge load failed");
          reject(new Error("bridge_load_failed"));
        };
        (document.head || document.documentElement).appendChild(script);
      } catch (error) {
        reject(error);
      }
    });

    return bridgeReadyPromise;
  }

  async function requestOpenGamePaper(scriptText, sourceElement) {
    if (!scriptText || !/openGamePaper\s*\(/i.test(scriptText)) {
      return { ok: false, reason: "openGamePaper_script_missing" };
    }

    await ensurePageBridgeLoaded();

    const requestId = `req_${Date.now()}_${Math.random().toString(16).slice(2)}`;
    const openElementId = `betman_open_${requestId}`;

    if (isElementNode(sourceElement)) {
      sourceElement.setAttribute("data-betman-open-id", openElementId);
    }

    try {
      const response = await new Promise((resolve) => {
        let done = false;

        const finish = (value) => {
          if (done) {
            return;
          }
          done = true;
          document.removeEventListener(BRIDGE_RESPONSE_EVENT, onResponse, false);
          clearTimeout(timer);
          resolve(value);
        };

        const onResponse = (event) => {
          const detail = event && event.detail && typeof event.detail === "object" ? event.detail : {};
          if (String(detail.requestId || "") !== requestId) {
            return;
          }
          finish({
            ok: Boolean(detail.ok),
            reason: normalizeText(detail.reason || ""),
            error: normalizeText(detail.error || ""),
          });
        };

        const timer = setTimeout(() => {
          finish({ ok: false, reason: "bridge_timeout" });
        }, 2000);

        document.addEventListener(BRIDGE_RESPONSE_EVENT, onResponse, false);
        document.dispatchEvent(new CustomEvent(BRIDGE_REQUEST_EVENT, {
          detail: {
            requestId,
            scriptText,
            openElementId,
          },
        }));
      });

      return response;
    } finally {
      if (isElementNode(sourceElement)) {
        sourceElement.removeAttribute("data-betman-open-id");
      }
    }
  }

  async function requestPostMethodViaBridge({ endpoint, params, timeoutMs = API_REQUEST_TIMEOUT_MS }) {
    const normalizedEndpoint = normalizeText(endpoint || "");
    if (!normalizedEndpoint) {
      return { ok: false, reason: "endpoint_missing" };
    }

    await ensurePageBridgeLoaded();

    const requestId = `req_post_${Date.now()}_${Math.random().toString(16).slice(2)}`;
    return new Promise((resolve) => {
      let done = false;
      const finish = (value) => {
        if (done) {
          return;
        }
        done = true;
        document.removeEventListener(BRIDGE_REQUEST_POST_METHOD_RESPONSE_EVENT, onResponse, false);
        clearTimeout(timer);
        resolve(value);
      };

      const onResponse = (event) => {
        const detail = event && event.detail && typeof event.detail === "object" ? event.detail : {};
        if (String(detail.requestId || "") !== requestId) {
          return;
        }
        finish({
          ok: Boolean(detail.ok),
          data: detail.data,
          reason: normalizeText(detail.reason || ""),
          error: normalizeText(detail.error || ""),
        });
      };

      const timer = window.setTimeout(() => {
        finish({ ok: false, reason: "bridge_timeout" });
      }, Math.max(1000, Number(timeoutMs || API_REQUEST_TIMEOUT_MS) + 300));

      document.addEventListener(BRIDGE_REQUEST_POST_METHOD_RESPONSE_EVENT, onResponse, false);
      document.dispatchEvent(new CustomEvent(BRIDGE_REQUEST_POST_METHOD_REQUEST_EVENT, {
        detail: {
          requestId,
          endpoint: normalizedEndpoint,
          params: params && typeof params === "object" ? params : {},
          timeoutMs: Math.max(1000, Number(timeoutMs || API_REQUEST_TIMEOUT_MS)),
        },
      }));
    });
  }

  async function waitForPurchaseRowsReady(timeoutMs = HISTORY_READY_TIMEOUT_MS) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      const rows = collectPurchaseRows(document);
      if (rows.length > 0) {
        return rows;
      }
      await sleep(250);
    }
    return [];
  }

  function findTargetRow(rows, targetSlipId) {
    if (!Array.isArray(rows) || rows.length === 0) {
      return null;
    }

    const normalizedTarget = normalizeText(targetSlipId || "");
    if (normalizedTarget) {
      const exact = rows.find((row) => extractRowSlipId(row) === normalizedTarget);
      if (exact) {
        return exact;
      }
    }

    return rows[0] || null;
  }

  async function openPaperForRow(row, targetSlipId) {
    if (!isElementNode(row)) {
      return false;
    }

    const seenScripts = new Set();
    const sources = [
      row,
      ...Array.from(row.querySelectorAll("[onclick*='openGamePaper'],a[href*='openGamePaper'],button[onclick],a[onclick],[href^='javascript:']")),
    ];

    for (const source of sources) {
      const scriptText = extractOpenGamePaperScriptTextFromElement(source);
      if (!scriptText || seenScripts.has(scriptText)) {
        continue;
      }
      seenScripts.add(scriptText);

      const result = await requestOpenGamePaper(scriptText, source);
      if (result.ok) {
        console.warn("[BetmanPushExt] openGamePaper route=bridge ok", {
          targetSlipId: normalizeText(targetSlipId || "-") || "-",
          rowSlipId: extractRowSlipId(row) || "-",
        });
        return true;
      }

      console.warn("[BetmanPushExt] openGamePaper route=bridge fail", {
        targetSlipId: normalizeText(targetSlipId || "-") || "-",
        rowSlipId: extractRowSlipId(row) || "-",
        reason: normalizeText(result.reason || "unknown") || "unknown",
      });
    }

    return false;
  }

  function findVisiblePaperAreaTarget(doc = document) {
    for (const selector of PAPER_AREA_SELECTORS) {
      const candidates = Array.from(doc.querySelectorAll(selector));
      for (const element of candidates) {
        if (isVisible(element)) {
          return element;
        }
      }
    }
    return null;
  }

  function isLoadingVisible(root) {
    const loadingNodes = Array.from(root.querySelectorAll(".loading, [class*='loading'], [aria-busy='true']"));
    return loadingNodes.some((node) => isVisible(node));
  }

  function readPaperAreaReadyState(doc = document) {
    const target = findVisiblePaperAreaTarget(doc);
    if (!target) {
      return {
        ready: false,
        ready_mode: "none",
        row_count: 0,
        target: null,
      };
    }

    if (isLoadingVisible(target)) {
      return {
        ready: false,
        ready_mode: "none",
        row_count: 0,
        target,
      };
    }

    const victoryRows = target.querySelectorAll("#tbd_gmBuySlipList tr[data-matchseq]").length;
    const recordRows = Math.max(
      target.querySelectorAll("#winrstResultListTbody tr").length,
      target.querySelectorAll("#winrstViewTotalTblDiv tbody tr").length,
    );

    if (victoryRows > 0) {
      return {
        ready: true,
        ready_mode: "victory_rows",
        row_count: victoryRows,
        target,
      };
    }

    if (recordRows > 0) {
      return {
        ready: true,
        ready_mode: "record_rows",
        row_count: recordRows,
        target,
      };
    }

    return {
      ready: false,
      ready_mode: "none",
      row_count: 0,
      target,
    };
  }

  async function waitForPaperAreaReady(timeoutMs = PAPER_READY_TIMEOUT_MS) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      const state = readPaperAreaReadyState(document);
      if (state.ready) {
        console.warn("[BetmanPushExt] paperArea ready", {
          ready_mode: state.ready_mode,
          row_count: state.row_count,
          doc_url: location.href,
        });
        return state;
      }
      await sleep(250);
    }

    const latest = readPaperAreaReadyState(document);
    console.warn("[BetmanPushExt] paperArea timeout", {
      ready_mode: latest.ready_mode,
      row_count: latest.row_count,
      doc_url: location.href,
    });
    return null;
  }

  async function sendBlobToWebhook(webhookUrl, blob, filename, content = "") {
    const dataUrl = await new Promise((resolve) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result || ""));
      reader.readAsDataURL(blob);
    });

    const response = await sendRuntimeMessage("SEND_WEBHOOK_IMAGE", {
      webhookUrl,
      dataUrl,
      filename,
      content: String(content || "").trim().slice(0, 1900),
    });

    if (response && response.ok) {
      return { ok: true };
    }

    return {
      ok: false,
      code: normalizeText((response && response.code) || ERROR_CODE.WEBHOOK_SEND_FAILED),
      details: normalizeText((response && (response.details || response.body)) || ""),
    };
  }

  async function captureHistorySlipAndSend({ webhookUrl, targetSlipId, reason }) {
    const normalizedTargetSlipId = normalizeText(targetSlipId || "");

    const earlyDedupKey = normalizedTargetSlipId ? `slip:${normalizedTargetSlipId}` : "";
    if (earlyDedupKey && await isDuplicate(earlyDedupKey)) {
      console.warn("[BetmanPushExt] poll skip reason=already_sent", {
        slipId: normalizedTargetSlipId,
      });
      return {
        ok: true,
        duplicate: true,
        slipId: normalizedTargetSlipId,
      };
    }

    const rows = await waitForPurchaseRowsReady(HISTORY_READY_TIMEOUT_MS);
    if (!rows.length) {
      throw createError(ERROR_CODE.ROW_NOT_FOUND, { reason: "rows_empty" });
    }

    const targetRow = findTargetRow(rows, normalizedTargetSlipId);
    if (!targetRow) {
      throw createError(ERROR_CODE.ROW_NOT_FOUND, { reason: "target_missing" });
    }

    const rowSlipId = extractRowSlipId(targetRow) || normalizedTargetSlipId;
    if (!rowSlipId) {
      throw createError(ERROR_CODE.ROW_NOT_FOUND, { reason: "slip_id_missing" });
    }

    const opened = await openPaperForRow(targetRow, rowSlipId);
    if (!opened) {
      throw createError(ERROR_CODE.OPEN_GAME_PAPER_FAILED, { slipId: rowSlipId });
    }

    const readyState = await waitForPaperAreaReady(PAPER_READY_TIMEOUT_MS);
    if (!readyState || !readyState.ready || !readyState.target) {
      throw createError(ERROR_CODE.PAPER_AREA_NOT_READY, { slipId: rowSlipId });
    }

    const targetElement = readyState.target;
    const meta = {
      text: normalizeText(targetElement.innerText || targetElement.textContent),
      slipId: rowSlipId,
      purchaseTime: "",
      selector: "#paperArea",
    };

    const dedupKey = buildDedupKey(meta);
    if (await isDuplicate(dedupKey)) {
      console.warn("[BetmanPushExt] poll skip reason=duplicate_after_open", {
        slipId: rowSlipId,
      });
      return {
        ok: true,
        duplicate: true,
        slipId: rowSlipId,
      };
    }

    const blob = await captureElementAsBlob(targetElement);
    if (!blob) {
      throw createError(ERROR_CODE.PAPER_AREA_CAPTURE_FAILED, { slipId: rowSlipId });
    }

    const sendResult = await sendBlobToWebhook(
      webhookUrl,
      blob,
      buildFileName(meta),
      ALERT_CONTENT_TEXT,
    );
    if (!sendResult.ok) {
      console.warn("[BetmanPushExt] webhook send fail", {
        slipId: rowSlipId,
        code: sendResult.code || ERROR_CODE.WEBHOOK_SEND_FAILED,
        details: sendResult.details || "",
      });
      throw createError(ERROR_CODE.WEBHOOK_SEND_FAILED, sendResult);
    }

    await markSent(dedupKey);
    console.warn("[BetmanPushExt] webhook send ok", {
      reason,
      slipId: rowSlipId,
      size: blob.size,
      ready_mode: readyState.ready_mode,
      row_count: readyState.row_count,
      doc_url: location.href,
    });

    return {
      ok: true,
      duplicate: false,
      slipId: rowSlipId,
    };
  }

  function scheduleDetect(reason) {
    if (detectTimer !== null) {
      clearTimeout(detectTimer);
    }
    detectTimer = window.setTimeout(() => {
      void detectAndPush(reason);
    }, DETECT_DEBOUNCE_MS);
  }

  async function fetchPaymentResultData(purchaseNos, reason) {
    const normalizedPurchaseNos = normalizeText(purchaseNos || "");
    if (!normalizedPurchaseNos) {
      console.warn("[BetmanPushExt] payment_result_api_fetch_fail", {
        reason: "purchaseNos_missing",
        slipId: "-",
        attempts: 0,
        queueSize: 0,
        doc_url: location.href,
      });
      return { ok: false, reason: "purchaseNos_missing" };
    }

    const normalizedReason = normalizeText(reason || "payment_result_detected");
    console.warn("[BetmanPushExt] payment_result_api_fetch_start", {
      reason: normalizedReason,
      slipId: "-",
      attempts: 0,
      queueSize: 0,
      doc_url: location.href,
    });
    const response = await requestPostMethodViaBridge({
      endpoint: "/mypgPayment/paymentResult.do",
      params: { purchaseNos: normalizedPurchaseNos },
      timeoutMs: API_REQUEST_TIMEOUT_MS,
    });
    if (!response.ok || !response.data || typeof response.data !== "object") {
      console.warn("[BetmanPushExt] payment_result_api_fetch_fail", {
        reason: response.reason || normalizedReason,
        slipId: "-",
        attempts: 0,
        queueSize: 0,
        doc_url: location.href,
      });
      return { ok: false, reason: response.reason || "payment_result_api_failed" };
    }

    const buyList = extractPaymentResultBuyList(response.data);
    console.warn("[BetmanPushExt] payment_result_api_fetch_success", {
      reason: normalizedReason,
      slipId: "-",
      attempts: 0,
      queueSize: buyList.length,
      doc_url: location.href,
    });

    return {
      ok: true,
      buyList,
    };
  }

  async function handlePaymentResultEntry(reason) {
    if (!isPaymentResultPage()) {
      return false;
    }
    if (!isPaymentResultSuccessReady(document)) {
      return false;
    }

    const normalizedReason = normalizeText(reason || "payment_result_detected") || "payment_result_detected";
    const purchaseNos = getPaymentResultPurchaseNos(document);
    const apiResult = await fetchPaymentResultData(purchaseNos, normalizedReason);

    const entries = [];
    const seenSlipIds = new Set();
    if (apiResult.ok) {
      for (const item of apiResult.buyList) {
        const entry = createSlipEntryFromBuyItem(item);
        if (!entry || seenSlipIds.has(entry.slipId)) {
          continue;
        }
        seenSlipIds.add(entry.slipId);
        entries.push(entry);
      }
    }

    const fallbackSlipIds = collectPaymentResultSlipIds(document);
    for (const fallbackSlipId of fallbackSlipIds) {
      if (!fallbackSlipId || seenSlipIds.has(fallbackSlipId)) {
        continue;
      }
      seenSlipIds.add(fallbackSlipId);
      entries.push({ slipId: fallbackSlipId });
    }

    const slipIds = normalizeSlipIdList(entries.map((entry) => entry.slipId));
    if (!slipIds.length) {
      return false;
    }

    const fingerprint = buildPendingQueueFingerprint(slipIds);
    const existingQueue = getFreshPendingCaptureQueue();
    console.warn("[BetmanPushExt] payment_result_detected", {
      reason: normalizedReason,
      slipId: slipIds[0] || "-",
      attempts: Number(existingQueue?.attemptsBySlip?.[slipIds[0]] || 0),
      queueSize: slipIds.length,
      doc_url: location.href,
    });
    if (existingQueue && existingQueue.fingerprint === fingerprint) {
      return true;
    }

    const queue = enqueuePendingCaptureQueue({
      slipIds,
      reason: normalizedReason,
      reset: true,
    });
    if (!queue) {
      return false;
    }

    console.warn("[BetmanPushExt] payment_result_force_nav", {
      reason: normalizedReason,
      slipId: queue.slipIds[0] || "-",
      attempts: Number(queue.attemptsBySlip[queue.slipIds[0]] || 0),
      queueSize: queue.slipIds.length,
      doc_url: location.href,
    });
    location.assign(getPurchaseHistoryUrl());
    return true;
  }

  async function detectAndPush(reason) {
    if (detectInProgress) {
      return;
    }
    detectInProgress = true;
    try {
      const webhookUrl = await getWebhookUrl();
      if (!isValidWebhookUrl(webhookUrl)) {
        return;
      }

      const normalizedReason = normalizeText(reason || "detect") || "detect";

      if (isPaymentResultPage()) {
        await handlePaymentResultEntry(`payment_result_${normalizedReason}`);
        return;
      }
      if (isPurchaseHistoryPage()) {
        await resumePendingCapture(`pending_${normalizedReason}`);
      }
    } catch (error) {
      console.error("[BetmanPushExt] detect/push failed", error);
    } finally {
      detectInProgress = false;
    }
  }

  async function resumePendingCapture(reason) {
    if (pendingResumeInProgress || pushInProgress) {
      return;
    }

    let queue = getFreshPendingCaptureQueue();
    if (!queue) {
      return;
    }
    if (!isPurchaseHistoryPage()) {
      return;
    }

    const webhookUrl = await getWebhookUrl();
    if (!isValidWebhookUrl(webhookUrl)) {
      return;
    }

    pendingResumeInProgress = true;
    pushInProgress = true;
    try {
      while (queue.slipIds.length > 0) {
        const targetSlipId = queue.slipIds[0];
        const attempts = Number(queue.attemptsBySlip[targetSlipId] || 0);
        const normalizedReason = normalizeText(reason || queue.reason || "pending_resume");

        try {
          const result = await captureHistorySlipAndSend({
            webhookUrl,
            targetSlipId,
            reason: normalizedReason,
          });
          if (!result.ok) {
            throw createError(ERROR_CODE.WEBHOOK_SEND_FAILED, result);
          }

          await setLastSeenHeadSlipId(result.slipId || targetSlipId);
          queue.slipIds.shift();
          delete queue.attemptsBySlip[targetSlipId];
          queue.createdAt = Date.now();
          queue.reason = normalizedReason;
          queue.fingerprint = buildPendingQueueFingerprint(queue.slipIds);
          if (queue.slipIds.length > 0) {
            savePendingCaptureQueue(queue);
          } else {
            clearPendingCaptureQueue();
          }
          console.warn("[BetmanPushExt] pending_queue_item_sent", {
            reason: normalizedReason,
            slipId: targetSlipId,
            attempts,
            queueSize: queue.slipIds.length,
            doc_url: location.href,
          });
          continue;
        } catch (error) {
          const code = getErrorCode(error);
          const nextAttempts = attempts + 1;
          if (isRetriablePendingQueueCode(code) && nextAttempts < MAX_PENDING_RETRIES) {
            queue.attemptsBySlip[targetSlipId] = nextAttempts;
            queue.createdAt = Date.now();
            queue.reason = normalizedReason;
            queue.fingerprint = buildPendingQueueFingerprint(queue.slipIds);
            savePendingCaptureQueue(queue);
            console.warn("[BetmanPushExt] pending_queue_item_retry", {
              reason: normalizedReason,
              slipId: targetSlipId,
              attempts: nextAttempts,
              queueSize: queue.slipIds.length,
              doc_url: location.href,
            });
            return;
          }

          queue.slipIds.shift();
          delete queue.attemptsBySlip[targetSlipId];
          queue.createdAt = Date.now();
          queue.reason = normalizedReason;
          queue.fingerprint = buildPendingQueueFingerprint(queue.slipIds);
          if (queue.slipIds.length > 0) {
            savePendingCaptureQueue(queue);
          } else {
            clearPendingCaptureQueue();
          }
          await setLastSeenHeadSlipId(targetSlipId);
          console.warn("[BetmanPushExt] pending_queue_item_dropped", {
            reason: normalizedReason,
            slipId: targetSlipId,
            attempts: nextAttempts,
            queueSize: queue.slipIds.length,
            doc_url: location.href,
          });
        }
      }

      console.warn("[BetmanPushExt] pending_queue_completed", {
        reason: normalizeText(reason || queue.reason || "pending_resume"),
        slipId: "-",
        attempts: 0,
        queueSize: 0,
        doc_url: location.href,
      });
    } finally {
      pendingResumeInProgress = false;
      pushInProgress = false;
    }
  }

  function setupObserver() {
    const observer = new MutationObserver(() => {
      if (!isPaymentResultPage()) {
        return;
      }
      if (!document.querySelector(PAYMENT_RESULT_SUCCESS_SELECTOR)) {
        return;
      }
      scheduleDetect("mutation_payment_result");
    });

    observer.observe(document.documentElement, {
      childList: true,
      subtree: true,
      attributes: true,
      attributeFilter: ["class", "style"],
    });
  }

  function setupNavigationHooks() {
    if (navigationHooksInstalled) {
      return;
    }
    navigationHooksInstalled = true;

    const scheduleRouteDetect = (trigger) => {
      if (document.visibilityState !== "hidden") {
        scheduleDetect(trigger);
      }
    };

    const wrapHistoryMethod = (methodName) => {
      const original = history[methodName];
      if (typeof original !== "function") {
        return;
      }
      history[methodName] = function patchedHistoryMethod(...args) {
        const result = original.apply(this, args);
        scheduleRouteDetect(`history_${methodName}`);
        return result;
      };
    };

    wrapHistoryMethod("pushState");
    wrapHistoryMethod("replaceState");

    window.addEventListener("popstate", () => {
      scheduleRouteDetect("popstate");
    });

    window.addEventListener(
      "load",
      () => {
        scheduleRouteDetect("load");
      },
      { once: true },
    );

    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") {
        scheduleRouteDetect("visible");
      }
    });

    scheduleRouteDetect("init");
  }

  if (extensionRuntimeAvailable && chrome.storage && chrome.storage.onChanged) {
    chrome.storage.onChanged.addListener((changes, areaName) => {
      if (areaName !== "local") {
        return;
      }
      if (STORAGE_KEYS.webhookUrl in changes) {
        cachedWebhookUrl = normalizeText(changes[STORAGE_KEYS.webhookUrl].newValue || "");
      }
    });
  }

  console.warn(`[BetmanPushExt] boot version=${extensionVersion} ext_id=${extensionId || "-"} frame=${frameKind} url=${location.href}`);

  if (!extensionRuntimeAvailable) {
    console.error("[BetmanPushExt] extension runtime unavailable; extension-only mode halted");
    return;
  }

  if (frameKind !== "top") {
    console.warn("[BetmanPushExt] non-top frame detected; skipped");
    return;
  }

  void refreshWebhookCache();
  setupObserver();
  setupNavigationHooks();
})();
