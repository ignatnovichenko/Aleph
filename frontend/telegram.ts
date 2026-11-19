declare global {
  interface Window {
    alephTelegramVisible?: boolean;
  }
}

export function revealMiniApp() {
  const telegram = window.Telegram?.WebApp;
  if (!telegram || window.alephTelegramVisible) return;
  telegram.ready();
  telegram.expand();
  window.alephTelegramVisible = true;
}
