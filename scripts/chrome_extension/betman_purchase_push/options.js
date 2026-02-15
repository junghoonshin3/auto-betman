const STORAGE_KEY_WEBHOOK = "webhook_url";

function normalizeText(input) {
  return String(input || "").trim();
}

function isValidWebhookUrl(value) {
  const text = normalizeText(value);
  const pattern = /^https:\/\/(?:canary\.|ptb\.)?discord\.com\/api\/webhooks\/\d+\/[A-Za-z0-9._-]+/;
  return pattern.test(text);
}

function setStatus(message, kind = "") {
  const el = document.getElementById("status");
  if (!el) {
    return;
  }
  el.textContent = message;
  el.className = `status ${kind}`.trim();
}

async function load() {
  const input = document.getElementById("webhook");
  const data = await chrome.storage.local.get([STORAGE_KEY_WEBHOOK]);
  input.value = normalizeText(data[STORAGE_KEY_WEBHOOK] || "");
}

async function save() {
  const input = document.getElementById("webhook");
  const value = normalizeText(input.value);

  if (!value) {
    setStatus("Webhook URL을 입력하세요.", "error");
    return;
  }
  if (!isValidWebhookUrl(value)) {
    setStatus("유효한 Discord Webhook URL 형식이 아닙니다.", "error");
    return;
  }

  await chrome.storage.local.set({ [STORAGE_KEY_WEBHOOK]: value });
  setStatus("저장 완료", "ok");
}

async function clearWebhook() {
  await chrome.storage.local.remove([STORAGE_KEY_WEBHOOK]);
  const input = document.getElementById("webhook");
  input.value = "";
  setStatus("삭제 완료", "ok");
}

function openBetman() {
  chrome.tabs.create({ url: "https://www.betman.co.kr/" });
}

document.getElementById("save").addEventListener("click", () => {
  void save();
});

document.getElementById("clear").addEventListener("click", () => {
  void clearWebhook();
});

document.getElementById("open-betman").addEventListener("click", openBetman);

void load();
