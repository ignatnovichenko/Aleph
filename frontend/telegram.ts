declare global {
  interface Window {
    alephTelegramVisible?: boolean;
  }
}

export function bindTelegramColors() {
  const telegram = window.Telegram?.WebApp;
  const background = document.querySelector<HTMLMetaElement>('meta[name="theme-color"]')?.content;
  if (!telegram || !background) return;
  const apply = () => {
    for (const method of ["setHeaderColor", "setBackgroundColor", "setBottomBarColor"] as const) {
      try { telegram[method]?.(background); } catch { /* Older clients support fewer colors. */ }
    }
  };
  apply();
  telegram.onEvent?.("themeChanged", apply);
}

export function revealMiniApp() {
  const telegram = window.Telegram?.WebApp;
  if (!telegram || window.alephTelegramVisible) return;
  bindTelegramColors();
  telegram.ready();
  telegram.expand();
  window.alephTelegramVisible = true;
}
