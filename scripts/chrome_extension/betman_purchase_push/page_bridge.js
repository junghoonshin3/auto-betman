(() => {
  "use strict";

  if (window.__betmanPushBridgeInstalled) {
    return;
  }
  window.__betmanPushBridgeInstalled = true;

  const REQUEST_EVENT = "betman_push_open_game_paper_request";
  const RESPONSE_EVENT = "betman_push_open_game_paper_response";
  const REQUEST_POST_METHOD_REQUEST_EVENT = "betman_push_request_post_method_request";
  const REQUEST_POST_METHOD_RESPONSE_EVENT = "betman_push_request_post_method_response";

  function safeString(value) {
    return String(value == null ? "" : value);
  }

  function toResult(detail) {
    document.dispatchEvent(new CustomEvent(RESPONSE_EVENT, { detail }));
  }

  function toRequestPostMethodResult(detail) {
    document.dispatchEvent(new CustomEvent(REQUEST_POST_METHOD_RESPONSE_EVENT, { detail }));
  }

  function cloneJsonSafe(value) {
    try {
      return JSON.parse(JSON.stringify(value));
    } catch (_error) {
      return null;
    }
  }

  function splitArgs(raw) {
    const text = safeString(raw);
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

  function decodeToken(token, sourceElement) {
    const text = safeString(token).trim();
    if (!text) {
      return "";
    }
    if (text === "this") {
      return sourceElement || null;
    }
    if (text === "null") {
      return null;
    }
    if (text === "undefined") {
      return undefined;
    }
    if (text === "true") {
      return true;
    }
    if (text === "false") {
      return false;
    }
    if (/^-?\d+(\.\d+)?$/.test(text)) {
      return Number(text);
    }
    if (
      (text.startsWith("'") && text.endsWith("'")) ||
      (text.startsWith("\"") && text.endsWith("\""))
    ) {
      return text.slice(1, -1);
    }
    return text;
  }

  function parseOpenGamePaperArgs(scriptText, sourceElement) {
    const text = safeString(scriptText)
      .replace(/^javascript:/i, "")
      .trim();
    const match = text.match(/openGamePaper\s*\(([\s\S]*?)\)\s*;?/i);
    if (!match || match.length < 2) {
      return { ok: false, reason: "openGamePaper_args_not_found", args: [] };
    }
    const tokens = splitArgs(match[1]);
    const args = tokens.map((token) => decodeToken(token, sourceElement));
    return { ok: true, args };
  }

  function callOpenGamePaper(args) {
    const fn = window.openGamePaper;
    if (typeof fn !== "function") {
      return { ok: false, reason: "openGamePaper_not_function" };
    }
    try {
      fn.apply(window, args || []);
      return { ok: true };
    } catch (error) {
      return {
        ok: false,
        reason: "openGamePaper_throw",
        error: safeString(error && error.message ? error.message : error),
      };
    }
  }

  document.addEventListener(REQUEST_EVENT, (event) => {
    const detail = event && event.detail && typeof event.detail === "object" ? event.detail : {};
    const requestId = safeString(detail.requestId || "");
    const rawScriptText = safeString(detail.scriptText || "");
    const openElementId = safeString(detail.openElementId || "");
    const sourceElement = openElementId ? document.querySelector(`[data-betman-open-id=\"${openElementId}\"]`) : null;

    const parsed = parseOpenGamePaperArgs(rawScriptText, sourceElement);
    if (!parsed.ok) {
      toResult({
        requestId,
        ok: false,
        reason: parsed.reason || "openGamePaper_args_not_found",
      });
      return;
    }

    const args = Array.isArray(parsed.args) ? parsed.args : [];
    const result = callOpenGamePaper(args);
    toResult({ requestId, ...result });
  });

  document.addEventListener(REQUEST_POST_METHOD_REQUEST_EVENT, (event) => {
    const detail = event && event.detail && typeof event.detail === "object" ? event.detail : {};
    const requestId = safeString(detail.requestId || "");
    const endpoint = safeString(detail.endpoint || "");
    const params = detail.params && typeof detail.params === "object" ? detail.params : {};
    const timeoutMs = Math.max(1000, Number(detail.timeoutMs || 10000));

    if (!endpoint) {
      toRequestPostMethodResult({
        requestId,
        ok: false,
        reason: "endpoint_missing",
      });
      return;
    }

    if (typeof requestClient === "undefined" || typeof requestClient.requestPostMethod !== "function") {
      toRequestPostMethodResult({
        requestId,
        ok: false,
        reason: "requestClient_unavailable",
      });
      return;
    }

    let done = false;
    const finish = (payload) => {
      if (done) {
        return;
      }
      done = true;
      clearTimeout(timer);
      toRequestPostMethodResult({
        requestId,
        ...payload,
      });
    };

    const timer = window.setTimeout(() => {
      finish({
        ok: false,
        reason: "request_timeout",
      });
    }, timeoutMs);

    try {
      requestClient.requestPostMethod(
        endpoint,
        params,
        true,
        (data) => {
          finish({
            ok: true,
            data: cloneJsonSafe(data),
          });
        },
        (error) => {
          finish({
            ok: false,
            reason: "request_error",
            error: safeString(error && error.message ? error.message : error),
          });
        },
      );
    } catch (error) {
      finish({
        ok: false,
        reason: "request_throw",
        error: safeString(error && error.message ? error.message : error),
      });
    }
  });

  window.__betmanPushBridgeReady = true;
})();
