import { bindTelegramColors } from "./telegram.js";
import {request, signed} from "./store.js";
import type { WidgetFrameConfig, WidgetFrameReply, WidgetFrameRequest, WidgetState } from "./types.js";

declare const __WIDGET_FRAME_SOURCE__: string;

const tg = window.Telegram?.WebApp;
const frame = document.querySelector<HTMLIFrameElement>("#card")!;
const status = document.querySelector<HTMLParagraphElement>("#status")!;
const back = document.querySelector<HTMLButtonElement>("#back")!;
const id = new URLSearchParams(location.search).get("id") || "";
const originKey = crypto.randomUUID();
const endpoint = `/api/widgets/${id}` as const;
const russian = !tg?.initDataUnsafe?.user?.language_code || /^(ru|be|uk)/.test(tg.initDataUnsafe.user.language_code);
const text = (ru: string, en: string) => russian ? ru : en;

back.textContent = text("В чат","Back to chat");
back.addEventListener("click", () => {
  if (tg?.platform !== "unknown" && tg?.close) tg.close();
  else location.assign("https://t.me/AlephAgentBot");
});
bindTelegramColors();
tg?.ready?.();

addEventListener("message", async (event: MessageEvent<WidgetFrameRequest | null>) => {
  if (event.source !== frame.contentWindow || event.data?.originKey !== originKey) return;
  const message = event.data;
  if (message.type === "size" && Number.isFinite(message.height)) {
    frame.style.height = `${Math.min(900, Math.max(92, message.height + 2))}px`;
  }
  if (message.type !== "action" || !Number.isSafeInteger(message.id)) return;
  const action = typeof message.action === "string" && /^check:([0-5]):([01])$/.exec(message.action);
  if (!action) return;
  let result: WidgetState | undefined;
  try {
    result = await request(endpoint, "PATCH", {index:Number(action[1]), checked:action[2] === "1"});
  } catch {}
  frame.contentWindow?.postMessage({
    originKey, type: "answer", id: message.id, ok: !!result, chatData: result?.chat_data,
  } satisfies WidgetFrameReply, "*");
});
try {
  if (!/^\d{1,18}$/.test(id) || !signed()) throw new Error("401");
  const result = await request(endpoint);
  const csp = '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; script-src \'unsafe-inline\'; form-action \'none\'; base-uri \'none\'">';
  const config: WidgetFrameConfig = { originKey, chatData: result.chat_data };
  const safeConfig = JSON.stringify(config).replace(/</g, "\\u003c");
  const frameScript = __WIDGET_FRAME_SOURCE__.replace("__WIDGET_FRAME_CONFIG__", () => safeConfig);
  const shim = `<script>${frameScript}</script>`;
  frame.srcdoc = result.html.replace('<meta charset="utf-8">', '<meta charset="utf-8">' + csp + shim);
  frame.hidden = false;
  status.hidden = true;
} catch (error) {
  status.textContent = error instanceof Error && (error.message === "401" || error.message === "404")
    ? text("Открой виджет из своего чата с Алефом.","Open this widget from your chat with Aleph.")
    : text("Виджет не загрузился. Закрой и открой его ещё раз.","The widget didn’t load. Close and reopen it.");
}
