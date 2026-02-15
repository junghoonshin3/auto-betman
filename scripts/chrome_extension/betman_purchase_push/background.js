const MAX_RETRIES = 3;
const DEFAULT_RETRY_MS = 700;

function normalizeText(input) {
  return String(input || "").replace(/\s+/g, " ").trim();
}

function parseRetryAfterMs(status, responseText, headers) {
  if (status === 429 && responseText) {
    try {
      const parsed = JSON.parse(responseText);
      if (parsed && typeof parsed.retry_after === "number") {
        return parsed.retry_after > 1000
          ? parsed.retry_after
          : Math.ceil(parsed.retry_after * 1000);
      }
    } catch (_error) {
      // Ignore JSON parsing errors.
    }
  }

  const retryAfter = headers.get("retry-after");
  if (retryAfter) {
    const value = Number(retryAfter);
    if (Number.isFinite(value) && value >= 0) {
      return value > 1000 ? value : Math.ceil(value * 1000);
    }
  }

  return DEFAULT_RETRY_MS;
}

async function captureVisibleTab(windowId) {
  const options = {
    format: "jpeg",
    quality: 90,
  };

  const classifyCaptureErrorCode = (error) => {
    const text = normalizeText(error instanceof Error ? error.message : String(error)).toLowerCase();
    if (
      text.includes("permission") ||
      text.includes("denied") ||
      text.includes("not allowed") ||
      text.includes("active tab")
    ) {
      return "capture_permission_denied";
    }
    if (
      text.includes("window") ||
      text.includes("focused") ||
      text.includes("active") ||
      text.includes("no tab") ||
      text.includes("no window") ||
      text.includes("closed")
    ) {
      return "capture_tab_not_active";
    }
    return "capture_visible_tab_failed";
  };

  const tryCapture = async (targetWindowId) => {
    const dataUrl = await chrome.tabs.captureVisibleTab(targetWindowId, options);
    return { ok: true, dataUrl };
  };

  const errors = [];
  const hasWindowId = windowId !== undefined && windowId !== null;
  try {
    return await tryCapture(windowId);
  } catch (error) {
    console.warn("[BetmanPushExt] captureVisibleTab first attempt failed", {
      windowId: hasWindowId ? windowId : "(auto)",
      reason: normalizeText(error instanceof Error ? error.message : String(error)).slice(0, 200),
    });
    errors.push(error);
  }

  if (hasWindowId) {
    console.warn("[BetmanPushExt] captureVisibleTab retry with auto window");
    try {
      return await tryCapture(undefined);
    } catch (error) {
      console.warn("[BetmanPushExt] captureVisibleTab retry failed", {
        reason: normalizeText(error instanceof Error ? error.message : String(error)).slice(0, 200),
      });
      errors.push(error);
    }
  }

  const lastError = errors.length > 0 ? errors[errors.length - 1] : new Error("capture failed");
  const details = errors
    .map((error, index) => `${index + 1}:${normalizeText(error instanceof Error ? error.message : String(error))}`)
    .join(" | ")
    .slice(0, 500);

  const result = {
    ok: false,
    code: classifyCaptureErrorCode(lastError),
    details: details || "capture_visible_tab_failed",
  };

  console.error("[BetmanPushExt] captureVisibleTab failed", {
    windowId: hasWindowId ? windowId : "(auto)",
    retried: hasWindowId,
    code: result.code,
    details: result.details,
  });
  return result;
}

async function preflightWebhook(webhookUrl) {
  const response = await fetch(webhookUrl, {
    method: "GET",
    credentials: "omit",
    cache: "no-store",
  });
  const text = await response.text();
  return {
    status: response.status,
    body: normalizeText(text).slice(0, 200),
  };
}

async function sendWebhookImage({ webhookUrl, dataUrl, filename, content }) {
  const preflight = await preflightWebhook(webhookUrl);
  if (!(preflight.status >= 200 && preflight.status < 300)) {
    console.error("[BetmanPushExt] webhook preflight failed", preflight);
    return {
      ok: false,
      code: "webhook_preflight_failed",
      details: preflight,
    };
  }

  const blobResponse = await fetch(dataUrl);
  const imageBlob = await blobResponse.blob();

  for (let attempt = 1; attempt <= MAX_RETRIES; attempt += 1) {
    const formData = new FormData();
    const messageContent = String(content || "").trim().slice(0, 1900);
    formData.append(
      "payload_json",
      JSON.stringify({
        username: "응큼픽 딱걸렸네",
        ...(messageContent ? { content: messageContent } : {}),
      }),
    );
    formData.append("files[0]", imageBlob, filename || `betman_capture_${Date.now()}.jpg`);

    try {
      const response = await fetch(
        `${webhookUrl}${webhookUrl.includes("?") ? "&" : "?"}wait=true`,
        {
          method: "POST",
          body: formData,
          credentials: "omit",
          cache: "no-store",
        },
      );
      const text = await response.text();
      if (response.ok) {
        return { ok: true };
      }

      const retryMs = parseRetryAfterMs(response.status, text, response.headers);
      const retriable = response.status === 429 || response.status >= 500;
      if (!retriable || attempt >= MAX_RETRIES) {
        console.error("[BetmanPushExt] webhook failed", {
          attempt,
          status: response.status,
          body: normalizeText(text).slice(0, 200),
        });
        return {
          ok: false,
          code: "webhook_send_failed",
          status: response.status,
          body: normalizeText(text).slice(0, 200),
        };
      }

      await new Promise((resolve) => setTimeout(resolve, Math.max(400, retryMs)));
    } catch (error) {
      if (attempt >= MAX_RETRIES) {
        console.error("[BetmanPushExt] webhook request error", error);
        return {
          ok: false,
          code: "webhook_send_failed",
          details: String(error),
        };
      }
      await new Promise((resolve) => setTimeout(resolve, 500 * 2 ** (attempt - 1)));
    }
  }

  return {
    ok: false,
    code: "webhook_send_failed",
  };
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  void (async () => {
    try {
      if (!message || typeof message !== "object") {
        sendResponse({ ok: false, code: "bad_request" });
        return;
      }

      if (message.type === "CAPTURE_VISIBLE_TAB") {
        const windowId = sender && sender.tab ? sender.tab.windowId : undefined;
        const result = await captureVisibleTab(windowId);
        sendResponse(result);
        return;
      }

      if (message.type === "SEND_WEBHOOK_IMAGE") {
        const result = await sendWebhookImage(message.payload || {});
        sendResponse(result);
        return;
      }

      if (message.type === "OPEN_OPTIONS") {
        await chrome.runtime.openOptionsPage();
        sendResponse({ ok: true });
        return;
      }

      sendResponse({ ok: false, code: "unsupported_message" });
    } catch (error) {
      sendResponse({
        ok: false,
        code: "runtime_error",
        details: error instanceof Error ? error.message : String(error),
      });
    }
  })();

  return true;
});
