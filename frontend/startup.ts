import { apiRequest, statePath } from "./transport.js";
import { revealMiniApp } from "./telegram.js";
import type { WorkspaceState } from "./types.js";

declare global {
  interface Window {
    alephStartup?: {
      initData: string;
      response: Promise<{state: WorkspaceState} | {error: unknown}>;
    };
  }
}

// Run from the document head, before the browser needs to load the main bundle.
document.documentElement.dataset.theme = matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
function revealWithTheme() {
  const telegram = window.Telegram?.WebApp;
  if (telegram?.platform && telegram.platform !== "unknown" && telegram.colorScheme &&
      document.querySelector("#app .boot")) {
    document.documentElement.dataset.theme = telegram.colorScheme;
  }
  revealMiniApp();
}
document.addEventListener("load", event => {
  if (!(event.target instanceof HTMLScriptElement) || event.target.id !== "telegram-sdk") return;
  if (document.getElementById("app")) revealWithTheme();
  else document.addEventListener("DOMContentLoaded", revealWithTheme, {once: true});
}, true);
const params = new URLSearchParams(location.hash.slice(1));
const initData = params.get("tgWebAppData");
if (initData && !params.has("aleph") && /^https?:$/.test(location.protocol) && !location.hostname.endsWith(".github.io")) {
  const response = (apiRequest(statePath, initData) as Promise<WorkspaceState>)
    .then(state => ({state}), error => ({error}));
  window.alephStartup = {initData, response};
}
