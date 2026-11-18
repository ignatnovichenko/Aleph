import type { TelegramWidget, WidgetFrameConfig, WidgetFrameReply, WidgetFrameRequest } from "./types.js";

declare const __WIDGET_FRAME_CONFIG__: WidgetFrameConfig;
const frameConfig = __WIDGET_FRAME_CONFIG__;
const { originKey } = frameConfig;
let sequence = 0;
const pending = new Map<number, {
  resolve: (response: { result: string }) => void;
  reject: () => void;
}>();

function notify(message: WidgetFrameRequest) {
  parent.postMessage(message, "*");
}

function size() {
  notify({ originKey, type: "size", height: document.body.scrollHeight });
}

const widget: TelegramWidget = {
  chatData: frameConfig.chatData,
  ready() {
    size();
    new ResizeObserver(size).observe(document.body);
  },
  onEvent() {
    // In the Web App, each mutation reply includes the authoritative checklist state.
  },
  request(action) {
    return new Promise((resolve, reject) => {
      const id = ++sequence;
      pending.set(id, { resolve, reject });
      notify({ originKey, type: "action", id, action });
    });
  },
};
window.TelegramWidget = widget;

addEventListener("message", (event: MessageEvent<WidgetFrameReply | null>) => {
  const message = event.data;
  if (event.source !== parent || message?.originKey !== originKey) return;
  if (message.type === "theme") {
    if (message.scheme === "light" || message.scheme === "dark") document.documentElement.style.colorScheme = message.scheme;
  } else if (message.type === "answer") {
    const call = pending.get(message.id);
    pending.delete(message.id);
    if (message.ok && typeof message.chatData === "string") {
      widget.chatData = message.chatData;
      call?.resolve({ result: message.chatData });
    } else call?.reject();
  }
});
