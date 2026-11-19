import { icon } from "./icons.js";
import type { IconName } from "./icons.js";
import type { RequestOptions, WriteMethod, Writes } from "./store.js";
import type { CalendarEvent, ChatGPTConnection, ChatGPTPairing, ProjectEntry, ReplyStyle, SavedMemory, Service, ModelCatalog, ModelChoice, ModelProvider, Project, ProviderModel, Settings, Task, ThemeMode, WorkspaceState } from "./types.js";
import {
  bridge,
  demo,
  request,
  demoState,
  saveDemo,
  escapeHtml as e,
  localDateTime,
  zonedISO,
} from "./store.js";

type Page = "home" | "projects" | "settings";

function requiredElement<T extends HTMLElement>(scope: ParentNode, selector: string): T {
  const node = scope.querySelector<T>(selector);
  if (!node) throw new Error(`Missing Mini App element: ${selector}`);
  return node;
}

const app = requiredElement(document, "#app");
const sheet = requiredElement<HTMLDialogElement>(document, "#sheet");
const toastEl = requiredElement(document, "#toast");
let state: WorkspaceState;
let page: Page = new URLSearchParams(location.search).get("view") === "settings" ? "settings" : "home";
type PlanFilter = "today" | "upcoming" | "inbox" | "done";
let filter: PlanFilter = "today";
let projectDetail: number | null = null;
let undoTask: number | undefined;
const projectEntries = new Map<number, ProjectEntry[]>();
let projectLoadError = 0;
let agenda: CalendarEvent[] | undefined;
let agendaError = false;
let chatgpt: ChatGPTConnection | undefined;
let modelCatalog: ModelCatalog | undefined;
let ru = true, pending = false, revision = 0, sdkBound = false;
let toastTimer: number | undefined;
let returnFocus: HTMLElement | null = null;
const t = (r: string, en: string) => (ru ? r : en);
const locale = () => (ru ? "ru-RU" : "en-US");
const webapp = () => window.Telegram?.WebApp;
const zone = () => state?.settings.timezone || "UTC";
const byId = (id?: string) => state.tasks.find((x) => String(x.id) === String(id));
const sorted = (tasks = state.tasks) => [...tasks].sort((a,b) =>
  (Date.parse(a.due_at || a.remind_at || "") || Infinity) -
  (Date.parse(b.due_at || b.remind_at || "") || Infinity) || b.id - a.id);
const activeId = () => Number(state.active_project_id || 0);
const projectName = (id?: number) => (state.projects || []).find(item => item.id === id)?.name || t("Личное", "Personal");
const selectedProject = () => (state.projects || []).find(item => item.id === projectDetail);
const dateFormats = {
  date: { weekday: "long", day: "numeric", month: "long" },
  day: { day: "numeric", month: "short" },
  clock: { hour: "2-digit", minute: "2-digit", hourCycle: "h23" },
} satisfies Record<string, Intl.DateTimeFormatOptions>;
const dateFormatters = new Map<string, Intl.DateTimeFormat>();
function formatDate(kind: keyof typeof dateFormats, value: string | number) {
  const language = locale(), timeZone = zone();
  const key = `${kind}/${language}/${timeZone}`;
  let formatter = dateFormatters.get(key);
  if (!formatter) {
    formatter = new Intl.DateTimeFormat(language, { timeZone, ...dateFormats[kind] });
    dateFormatters.set(key, formatter);
  }
  return formatter.format(new Date(value));
}
const timeLabel = (time?: string) => String(time || "").slice(0, 5);
const navs = (): [Page, string][] => [
  ["home", t("Задачи", "Tasks")],
  ["projects", t("Проекты", "Projects")],
  ["settings", t("Настройки", "Settings")],
];
function prefersDark() {
  const tg = webapp();
  return tg?.platform && tg.platform !== "unknown" && tg.colorScheme
    ? tg.colorScheme === "dark"
    : matchMedia("(prefers-color-scheme: dark)").matches;
}
const themeMode = () => state?.settings.theme ?? "auto";

function setTheme() {
  const mode = themeMode();
  const dark = mode === "dark" || (mode === "auto" && prefersDark());
  const root = document.documentElement;
  const tg = webapp();
  root.dataset.theme = dark ? "dark" : "light";
  root.toggleAttribute("data-white-theme", mode === "white");
  const bg = getComputedStyle(root).getPropertyValue("--bg").trim();
  const meta = document.querySelector<HTMLMetaElement>("meta[name=theme-color]");
  if (meta) meta.content = bg;
  try {
    tg?.setHeaderColor?.(bg);
    tg?.setBackgroundColor?.(bg);
    tg?.setBottomBarColor?.(bg);
  } catch {}
}
function initTelegram() {
  const tg = webapp();
  if (!tg || sdkBound) return;
  sdkBound = true;
  tg.ready();
  tg.expand();
  setTheme();
  tg.onEvent?.("themeChanged", setTheme);
  tg.BackButton?.onClick(() => {
    if (sheet.open) {
      if (!pending) closeSheet();
    } else if (page === "projects" && projectDetail !== null) { projectDetail = null; render(); }
    else navigate("home");
  });
  syncBack();
}
function syncBack() {
  const back = webapp()?.BackButton;
  if (!back || !webapp()?.isVersionAtLeast?.("6.1")) return;
  if (sheet.open || page !== "home") back.show();
  else back.hide();
}
function setLanguage(code?: string) {
  ru = !code || ["ru", "uk", "be"].includes(code.toLowerCase().split("-")[0]);
  document.documentElement.lang = ru ? "ru" : "en";
}
function message(text: string, duration=3300) {
  clearTimeout(toastTimer);
  toastEl.textContent = text;
  toastEl.classList.add("show");
  toastTimer = window.setTimeout(() => {
    toastEl.classList.remove("show");
    toastEl.replaceChildren();
  }, duration);
}
function errorMessage(error: unknown) {
  const reason = error instanceof Error ? error.message : "";
  if (reason === "static-plan") {
    return t(
      "Напиши Алефу «открой план» и нажми появившуюся кнопку в чате.",
      "Send “open plan” to Aleph, then tap the button in your chat.",
    );
  }
  if (reason === "invalid-date")
    return t(
      "Такого времени нет в выбранном часовом поясе. Выбери другое время.",
      "This time does not exist in this time zone. Choose a different time.",
    );
  if (["401", "403"].includes(reason))
    return t(
      "Открой приложение заново из чата с Aleph.",
      "Open the app again from your chat with Aleph.",
    );
  if (reason === "422" || reason === "400")
    return t("Проверь введённые данные.", "Check the details and try again.");
  return t(
    "Не удалось выполнить запрос. Проверь соединение и попробуй ещё раз.",
    "Could not complete the request. Check your connection and try again.",
  );
}
function nav() {
  return navs().map(([id, label]) =>
    `<button class="nav-item" data-page="${id}" ${page === id ? 'aria-current="page"' : ""}>${icon(id)}<span>${label}</span></button>`
  ).join("");
}
const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");
const clamp = (value: number, min: number, max: number) => Math.min(max, Math.max(min, value));

interface View {
  key: Page;
  index: number;
  panel: HTMLElement;
  heading: HTMLHeadingElement;
  content: HTMLElement;
  markup?: string;
}

interface Swipe {
  pointerId: number;
  onDock: boolean;
  width: number;
  index: number;
  startX: number;
  startY: number;
  x: number;
  time: number;
  velocity: number;
  frame: number;
  locked: boolean;
}

let stage: HTMLElement | undefined;
let track: HTMLElement, dock: HTMLElement, lens: HTMLElement, tabs: HTMLButtonElement[];
let views: View[] = [];
let sizeObserver: ResizeObserver | undefined;
let trackWidth = 0, tabWidth = 0, suppressTap = false;
let swipe: Swipe | undefined;
let trackMotion: Animation | undefined, lensMotion: Animation | undefined;
let closeTimer: number | undefined;
let closeListener: ((event?: AnimationEvent) => void) | undefined;
let focusTarget: string | null = null;
let sheetDrag: { id: number; start: number; offset: number } | undefined;
let refreshRunning = false;
const activeView = () => views.find((view) => view.key === page);

function selectionFeedback() {
  if (!demo && !reducedMotion.matches && webapp()?.isVersionAtLeast?.("6.1")) {
    try { webapp()?.HapticFeedback?.selectionChanged(); } catch {}
  }
}

function mountShell() {
  const footer = demo ? `<footer class="demo-note">${t("Демо-режим", "Demo mode")} · <a href="https://t.me/AlephAgentBot" target="_blank" rel="noopener noreferrer">${t("Открыть бота", "Open bot")}</a></footer>` : "";
  app.innerHTML = `
    <main class="page-stage"><div class="page-track">${navs().map(([key, label]) => `
      <section class="page-panel" data-view="${key}" aria-labelledby="title-${key}"${page === key ? "" : ' inert aria-hidden="true"'}>
        <div class="workspace">
          <header class="page-head"><img class="app-mark" src="./aleph.webp" width="30" height="30" alt="Aleph">
            <h1 class="page-title" id="title-${key}" tabindex="-1">${label}</h1>
            ${key === "home" ? '<p class="page-kicker"></p>' : ""}
          </header>
          <div class="page-content"></div>${footer}
        </div>
      </section>`).join("")}</div></main>
    <nav aria-label="${t("Навигация", "Navigation")}" class="dock">
      <span aria-hidden="true" class="dock-surface"></span><span aria-hidden="true" class="dock-lens"></span>${nav()}
    </nav>`;
  stage = requiredElement(app, ".page-stage");
  track = requiredElement(stage, ".page-track");
  dock = requiredElement(app, ".dock");
  lens = requiredElement(dock, ".dock-lens");
  tabs = [...dock.querySelectorAll<HTMLButtonElement>(".nav-item")];
  views = navs().map(([key], index) => {
    const panel = requiredElement(track, `[data-view="${key}"]`);
    return { key, index, panel, heading: requiredElement(panel, "h1"), content: requiredElement(panel, ".page-content") };
  });
  sizeObserver?.disconnect();
  sizeObserver = new ResizeObserver((entries) => {
    for (const {target, contentRect} of entries) {
      if (target === stage) trackWidth = contentRect.width;
      else tabWidth = contentRect.width / tabs.length;
    }
  });
  sizeObserver.observe(stage);
  sizeObserver.observe(dock);
  showPage(activeView()?.index ?? 0, 0);
}

function render() {
  if (!state) return;
  if (!stage?.isConnected) mountShell();
  const date = formatDate("date", Date.now());
  const kicker = requiredElement(track, ".page-kicker");
  if (kicker.textContent !== date) kicker.textContent = date;
  // Each view keeps its own scroller and stays mounted during navigation.
  for (const view of views) {
    const markup = view.key === "home" ? overview() : view.key === "projects" ? projectsPage() : settingsPage();
    if (view.markup === markup) continue;
    const restore = view.content.contains(document.activeElement) ? controlSelector(document.activeElement) : null;
    view.content.innerHTML = markup;
    view.markup = markup;
    if (restore) view.content.querySelector<HTMLElement>(restore)?.focus({preventScroll: true});
  }
  app.setAttribute("aria-busy", "false");
  syncBack();
}

const trackTransform = (position: number) => `translate3d(${-position * 100}%,0,0)`;
const lensTransform = (position: number, speed = 0) =>
  `translate3d(${position * 100}%,0,0) scale(${1 + speed * .1},${1 - speed * .1})`;

function showPage(index: number, duration: number) {
  const previous = activeView();
  const current = views[index];
  if (!previous || !current) return;
  const fromTrack = track.style.transform || trackTransform(previous.index);
  const fromLens = lens.style.transform || lensTransform(previous.index);
  trackMotion?.cancel();
  lensMotion?.cancel();
  dock.classList.remove("is-tracking");
  track.style.transform = trackTransform(index);
  lens.style.transform = lensTransform(index);
  if (duration && !reducedMotion.matches) {
    if (fromTrack !== track.style.transform) {
      trackMotion = track.animate([{transform: fromTrack}, {transform: track.style.transform}], {
        duration, easing: "cubic-bezier(.2,.75,.25,1)",
      });
    }
    if (fromLens !== lens.style.transform) {
      lensMotion = lens.animate([{transform: fromLens}, {transform: lens.style.transform}], {
        duration: Math.max(220, duration), easing: "cubic-bezier(.18,.8,.28,1.07)",
      });
    }
  }
  if (current !== previous) {
    const transferFocus = previous.panel.contains(document.activeElement);
    previous.panel.inert = true;
    previous.panel.setAttribute("aria-hidden", "true");
    current.panel.inert = false;
    current.panel.removeAttribute("aria-hidden");
    tabs[previous.index].removeAttribute("aria-current");
    tabs[index].setAttribute("aria-current", "page");
    page = current.key;
    if (transferFocus) current.heading.focus({preventScroll: true});
    selectionFeedback();
  }
  document.title = current.heading.textContent + " · Aleph";
  syncBack();
}

function navigate(id: string) {
  const destination = views.find((view) => view.key === id);
  if (!destination) return;
  if (swipe) cancelSwipe();
  if (id === page) {
    destination.panel.scrollTo({top: 0, behavior: reducedMotion.matches ? "auto" : "smooth"});
    return;
  }
  showPage(destination.index, 240);
}

function resetSwipe() {
  if (!swipe) return;
  const {frame, pointerId} = swipe;
  swipe = undefined;
  cancelAnimationFrame(frame);
  app.removeEventListener("touchmove", claimHorizontalTouch);
  if (app.hasPointerCapture?.(pointerId)) app.releasePointerCapture(pointerId);
}

function cancelSwipe() {
  if (!swipe) return;
  const {index, locked} = swipe;
  resetSwipe();
  if (locked) showPage(index, 160);
}

// Let the browser own vertical gestures. A locked horizontal pan writes only
// the two transform properties, with no layout reads or content rendering.
function drawSwipe() {
  const gesture = swipe;
  if (!gesture) return;
  gesture.frame = 0;
  const {index, x, startX, onDock, width, velocity} = gesture;
  const progress = index + (onDock ? x - startX : startX - x) / width;
  const bounded = clamp(progress, 0, views.length - 1);
  const overshoot = clamp((progress - bounded) * .16, -.055, .055);
  if (!onDock && !reducedMotion.matches) track.style.transform = trackTransform(bounded + overshoot);
  if (!reducedMotion.matches) {
    const stretch = clamp(Math.abs(velocity) * .48 + Math.abs(overshoot) * 3 + .18, 0, 1);
    lens.style.transform = lensTransform(bounded, stretch);
  }
}

function startSwipe(event: PointerEvent) {
  if (!event.isPrimary) { cancelSwipe(); return; }
  suppressTap = false;
  if (swipe || !stage?.isConnected || sheet.open || pending || event.button !== 0) return;
  const target = event.target;
  if (!(target instanceof Element)) return;
  const onDock = dock.contains(target);
  if (!onDock && (!stage.contains(target) || event.pointerType === "mouse")) return;
  if (target.closest("input,textarea,select,[contenteditable=true],a,[data-no-swipe]") || window.getSelection()?.isCollapsed === false) return;
  if (event.clientX < 24 || event.clientX > innerWidth - 24) return;
  const width = onDock ? tabWidth : trackWidth;
  if (!width) return;
  const current = activeView();
  if (!current) return;
  swipe = {pointerId: event.pointerId, onDock, width, index: current.index,
    startX: event.clientX, startY: event.clientY, x: event.clientX, time: event.timeStamp, velocity: 0, frame: 0, locked: false};
}

function claimHorizontalTouch(event: TouchEvent) {
  if (event.touches.length === 1 && event.cancelable) event.preventDefault();
}

function moveSwipe(event: PointerEvent) {
  const gesture = swipe;
  if (!gesture || gesture.pointerId !== event.pointerId) return;
  if (!gesture.locked) {
    const x = Math.abs(event.clientX - gesture.startX), y = Math.abs(event.clientY - gesture.startY);
    if (Math.max(x, y) < 9) return;
    if (y >= x || (y > 18 && x < 1.35 * y)) { resetSwipe(); return; }
    if (x < 1.35 * y) return;
    gesture.locked = true;
    suppressTap = true;
    trackMotion?.cancel();
    lensMotion?.cancel();
    dock.classList.add("is-tracking");
    // WebViews can absorb the next tap after an uncancelled horizontal fling.
    app.addEventListener("touchmove", claimHorizontalTouch, {passive: false});
    try { app.setPointerCapture(event.pointerId); } catch {}
  }
  const elapsed = Math.max(1, event.timeStamp - gesture.time);
  gesture.velocity = (elapsed > 100 ? 0 : gesture.velocity * .30) + .70 * (event.clientX - gesture.x) / elapsed;
  gesture.time = event.timeStamp;
  gesture.x = event.clientX;
  if (!gesture.frame) gesture.frame = requestAnimationFrame(drawSwipe);
}

function releaseSwipe(event: PointerEvent) {
  const gesture = swipe;
  if (!gesture || event.pointerId !== gesture.pointerId) return;
  if (!gesture.locked) { resetSwipe(); return; }
  const dx = event.clientX - gesture.startX;
  const distance = gesture.onDock ? dx : -dx;
  const velocity = event.timeStamp - gesture.time < 110 ? gesture.velocity : 0;
  const flick = Math.abs(dx) >= 28 && Math.abs(velocity) > .48 && Math.sign(velocity) === Math.sign(dx);
  const threshold = gesture.onDock ? gesture.width * .38 : Math.min(90, gesture.width * .28);
  const offset = Math.abs(distance) < threshold && !flick ? 0 : Math.sign(distance) *
    (gesture.onDock ? Math.max(1, Math.round(Math.abs(distance) / gesture.width)) : 1);
  const index = clamp(gesture.index + offset, 0, views.length - 1);
  if (gesture.frame) {
    cancelAnimationFrame(gesture.frame);
    gesture.x = event.clientX;
    drawSwipe();
  }
  resetSwipe();
  const remaining = Math.abs(index - gesture.index - distance / gesture.width);
  showPage(index, gesture.onDock ? 225 : clamp(150 + remaining * 90, 150, 245));
}

app.addEventListener("pointerdown", startSwipe);
window.addEventListener("pointermove", moveSwipe, {passive: true});
window.addEventListener("pointerup", releaseSwipe);
window.addEventListener("pointercancel", (event) => {
  if (swipe?.pointerId === event.pointerId) cancelSwipe();
});
window.addEventListener("resize", cancelSwipe);
window.addEventListener("blur", cancelSwipe);
app.addEventListener("click", (event) => {
  if (suppressTap && event.detail !== 0) {
    suppressTap = false;
    event.preventDefault();
    event.stopImmediatePropagation();
  }
}, true);
app.addEventListener("keydown", (event) => {
  if (!(event.target instanceof Element) || !event.target.closest(".dock [data-page]") || event.altKey || event.ctrlKey || event.metaKey) return;
  const index = activeView()?.index ?? 0;
  const shortcuts: Record<string, number> = {ArrowLeft: index - 1, ArrowRight: index + 1, Home: 0, End: tabs.length - 1};
  if (!Object.hasOwn(shortcuts, event.key)) return;
  event.preventDefault();
  const next = clamp(shortcuts[event.key], 0, tabs.length - 1);
  if (next !== index) navigate(views[next].key);
  tabs[next].focus({preventScroll: true});
});

function controlSelector(el: Element | null){
  if(!el?.matches("button")) return null;
  const attrs = ["data-action","data-page","data-filter","data-id"].filter(key=>el.hasAttribute(key));
  return attrs.length ? "button"+attrs.map(key=>"["+key+'="'+CSS.escape(el.getAttribute(key) ?? "")+'"]').join("") : null;
}

function taskRow(task: Task, withProject = true) {
  const done = task.status === "done";
  const late = !done && task.due_at && Date.parse(task.due_at) < Date.now();
  const today = localDateTime(Date.now(),zone()).slice(0,10);
  const when = (value: string) => localDateTime(value,zone()).slice(0,10) === today
    ? formatDate("clock",value) : formatDate("day",value) + " · " + formatDate("clock",value);
  const sharedTime = task.remind_at === task.due_at;
  const date = task.due_at ? `<span class="task-date ${late ? "late" : ""}">${e(when(task.due_at))}${sharedTime && !done ? `<span aria-label="${t("Есть напоминание","Has reminder")}">${icon("bell")}</span>` : ""}</span>` : "";
  const reminder = task.remind_at && !sharedTime && !done ? `<span class="task-time" title="${t("Напоминание","Reminder")}">${icon("bell")}${e(when(task.remind_at))}</span>` : "";
  const report = task.report_state;
  const reportLabels = {
    pending: done ? t("На паузе","Paused") : t("Алеф подготовит ответ", "Aleph will prepare the result"),
    failed: t("Нужно новое время", "Needs a new time"),
    sent: t("Доставлено в чат", "Delivered to chat"),
  };
  const kind = report ? `<span class="task-time report-status ${report === "failed" ? "late" : ""}">${icon("sparkle")}${reportLabels[report]}</span>` : "";
  const toggleLabel = report
    ? (done ? t("Возобновить","Resume") : t("Приостановить","Pause"))
    : (done ? t("Вернуть в работу","Reopen") : t("Завершить","Complete"));
  const toggle = report === "sent"
    ? `<span class="task-toggle" aria-hidden="true"><span class="ring">${icon("check")}</span></span>`
    : `<button class="task-toggle" data-action="toggle-task" data-id="${task.id}" aria-label="${e(toggleLabel + " · " + task.title)}" aria-pressed="${done}"><span class="ring">${done ? icon("check") : ""}</span></button>`;
  return `<article class="task-row ${done ? "done" : ""}">${toggle}
  <button class="task-body" data-action="edit-task" data-id="${task.id}"><span class="task-title" dir="auto">${e(task.title)}</span><span class="task-meta">${withProject ? `<span class="task-project">${e(projectName(task.project_id))}</span>` : ""}${date}${reminder}${kind}</span></button><span class="arrow">${icon("arrow")}</span></article>`;
}
function glyph(symbol: IconName) {
  return `<span class="glyph">${icon(symbol)}</span>`;
}

const services: Record<Service, {name:string; icon:IconName}> = {
  calendar:{name:"Google Calendar",icon:"calendar"}, notion:{name:"Notion",icon:"edit"},
  drive:{name:"Google Drive",icon:"projects"}, gmail:{name:"Gmail",icon:"plane"},
};
function serviceKey(value?: string): Service | null {
  return value && Object.hasOwn(services, value) ? value as Service : null;
}
function serviceRows() {
  if (!state.connections) return "";
  return (Object.keys(services) as Service[]).map(key => {
    const stateLabel = {connected:t("Подключён","Connected"), disconnected:t("Подключить","Connect"),
      reconnect:t("Повторить вход","Reconnect"), unavailable:t("Пока недоступен","Currently unavailable")};
    const info = services[key], status = state.connections?.[key] || "unavailable";
    return settingRow(info.icon, info.name, stateLabel[status], "service", key);
  }).join("");
}
function serviceSheet(key: Service) {
  const status = state.connections?.[key];
  const what = {calendar:t("События на сегодня и новые встречи из чата.","Today’s events and new appointments from chat."),
    notion:t("Выбранные страницы, заметки и итоги проектов.","Selected pages, notes and project summaries."),
    drive:t("Выбранные файлы и документы, которые создаёт Алеф.","Files you choose and documents Aleph creates."),
    gmail:t("Поиск писем, разбор и сохранение черновиков.","Find emails, review them and save drafts.")};
  const content = status === "connected"
    ? `<p class="sheet-desc">${what[key]}</p>${key === "drive" ? `<button class="button" data-action="drive-picker">${t("Выбрать файлы","Choose files")}</button>` : ""}
      <button class="button" data-action="disconnect-service" data-id="${key}">${t("Отключить","Disconnect")}</button>`
    : status === "unavailable" ? `<p class="sheet-desc">${t("Подключение появится после настройки OAuth на сервере.","This service will be available after the server is configured.")}</p>`
    : `<p class="sheet-desc">${what[key]} ${t("По твоему запросу нужные фрагменты будут передаваться выбранной модели.","Relevant excerpts are sent to your selected model when you request them.")}</p><button class="button primary" data-action="connect-service" data-id="${key}">${t("Подключить","Connect")}</button>`;
  openSheet(services[key].name, content);
}
async function connectionLink(service: Service, picker = false) {
  try {
    const reply = picker ? await request("/api/connections/drive/picker","POST") :
      await request(`/api/connections/${service}/start`,"POST");
    const url = new URL(reply.url);
    if (url.protocol !== "https:") throw new Error("invalid-url");
    openSheet(services[service].name, `<p class="sheet-desc">${t("После подтверждения вернись в Алефа.","Return to Aleph after approving access.")}</p><a class="button primary" href="${e(url.href)}" target="_blank" rel="noopener noreferrer" data-external-link>${t("Открыть в браузере","Open in browser")}</a>`);
  } catch (error) { message(errorMessage(error)); }
}
async function loadAgenda() {
  if (demo || state.connections?.calendar !== "connected") return;
  const today = localDateTime(Date.now(), zone()).slice(0,10);
  const tomorrow = new Date(Date.parse(today + "T12:00Z") + 86400000).toISOString().slice(0,10);
  const params = new URLSearchParams({start:today,end:tomorrow});
  try { agenda = (await request(`/api/connections/calendar/events?${params}`)).events; agendaError = false; }
  catch { agendaError = true; }
  render();
}

function chatgptRow() {
  if (!chatgpt?.available) return "";
  const info = chatgpt.connected
    ? chatgpt.email || t("Аккаунт подключён", "Account connected")
    : chatgpt.needs_reauth ? t("Требуется повторный вход", "Sign in again")
      : t("Войти со своим аккаунтом", "Sign in with your account");
  return settingRow("person", t("Аккаунт ChatGPT", "ChatGPT account"), info, "chatgpt");
}

function chatgptSheet(pairing?: ChatGPTPairing) {
  if (!chatgpt?.available) return;
  if (chatgpt.connected) {
    const account = chatgpt.email;
    openSheet("ChatGPT", `<p class="sheet-desc">${e(account || t("Аккаунт подключён", "Account connected"))}</p>
      <p class="sheet-desc">${state.model?.active.provider === "opencode"
        ? t("ChatGPT можно выбрать в настройках модели.", "Select ChatGPT in the model settings.")
        : t("Ответы в чате используют твой тариф ChatGPT.", "Chat replies use your ChatGPT plan.")}</p>
      <p class="sheet-desc"><a href="https://chatgpt.com/settings/usage" target="_blank" rel="noopener noreferrer">${t("Лимиты тарифа", "Plan usage")}</a></p>
      <button class="button" data-action="disconnect-chatgpt">${t("Отключить аккаунт", "Disconnect account")}</button>`);
    return;
  }
  const setup = `${chatgpt.needs_reauth ? `<p class="sheet-desc">${t("Срок действия подключения истёк. Войди в ChatGPT снова.", "Your connection expired. Sign in to ChatGPT again.")}</p>` : ""}
    <p class="sheet-desc">${pairing
      ? t("Запусти помощник из папки Алефа и вставь код. Страница входа в ChatGPT откроется в браузере.", "Run the helper from the Aleph folder and paste the code. The ChatGPT sign-in page will open in your browser.")
      : t("При первом подключении сначала скачай Aleph и установи зависимости на компьютере. Получи одноразовый код, когда помощник будет готов к запуску.", "For your first connection, download Aleph and install its dependencies on your computer. Generate the one-time code when you are ready to run the helper.")}</p>
    <p class="sheet-desc"><a href="https://github.com/ignatnovichenko/Aleph#connect-chatgpt-on-a-self-hosted-instance" target="_blank" rel="noopener noreferrer">${t("Инструкция по подключению", "Connection instructions")}</a></p>`;
  const code = pairing ? `<label class="field">${t("Одноразовый код", "One-time code")}<input id="chatgpt-pair-code" readonly value="${e(pairing.code)}" spellcheck="false" dir="ltr"></label>
    <p class="sheet-desc">${t("Срок действия, мин.", "Valid for, min.")} ${Math.ceil(pairing.expires_in / 60)}</p>` : "";
  const primary = pairing
    ? `<button class="button primary" data-action="copy-chatgpt-code">${t("Скопировать код", "Copy code")}</button>`
    : `<button class="button primary" data-action="pair-chatgpt">${t("Получить код", "Get code")}</button>`;
  openSheet("ChatGPT", `${setup}${code}<div class="submit-row">${primary}
    <button class="button" data-action="refresh-chatgpt">${t("Проверить вход", "Check sign-in")}</button></div>
    ${pairing ? `<button class="small-action" data-action="pair-chatgpt">${t("Получить новый код", "Get a new code")}</button>` : ""}
    ${chatgpt.needs_reauth ? `<button class="small-action" data-action="disconnect-chatgpt">${t("Отключить аккаунт", "Disconnect account")}</button>` : ""}`);
}

const providerName = (provider: ModelProvider) => provider === "chatgpt" ? "ChatGPT" : "OpenCode Go";

function reasoningName(effort: string) {
  const labels: Record<string, [string, string]> = {
    "": ["По умолчанию", "Default"], none: ["Без рассуждений", "None"], minimal: ["Минимальный", "Minimal"],
    low: ["Низкий", "Low"], medium: ["Средний", "Medium"], high: ["Высокий", "High"],
    xhigh: ["Очень высокий", "Extra high"], max: ["Максимальный", "Maximum"], ultra: ["Ультра", "Ultra"],
  };
  const label = labels[effort];
  return label ? t(...label) : effort;
}

function modelSummary(choice: ModelChoice) {
  return `${providerName(choice.provider)} · ${choice.name} · ${reasoningName(choice.reasoning)}`;
}

function modelRow() {
  const active = state.model?.active;
  if (!active) return "";
  const configurable = state.model_configurable ?? chatgpt?.available;
  return settingRow("sparkle", t("Модель и рассуждения", "Model and reasoning"),
    [active.name, providerName(active.provider), reasoningName(active.reasoning)].join(" · "),
    configurable ? "model-settings" : undefined);
}

function reasoningFields(model: ProviderModel, selected = "") {
  const chosen = model.levels.includes(selected) ? selected : "";
  const standard = reasoningName("") + (model.default ? " · " + reasoningName(model.default) : "");
  const training = model.training ? `<p class="sheet-desc">${t("Провайдер может использовать запросы к этой модели для обучения.", "The provider may use requests to this model for training.")}</p>` : "";
  return `<label class="field">${t("Рассуждения", "Reasoning")}<select name="reasoning"><option value="">${e(standard)}</option>${model.levels.map((level) =>
    `<option value="${e(level)}" ${level === chosen ? "selected" : ""}>${e(reasoningName(level))}</option>`).join("")}</select></label>
    <p class="sheet-desc">${model.images ? t("Принимает текст и фото", "Accepts text and images") : t("Принимает текст", "Accepts text")}</p>${training}`;
}

function modelFields(catalog: ModelCatalog, provider: ModelProvider) {
  const available = catalog.providers[provider];
  if (!available.models.length) {
    const hint = available.status === "disconnected"
      ? t("Подключи аккаунт, чтобы загрузить модели.", "Connect the account to load models.")
      : t("Список моделей временно недоступен. Попробуй обновить.", "The model list is temporarily unavailable. Try refreshing.");
    return `<p class="sheet-desc">${hint}</p><button type="button" class="button" data-action="${provider === "chatgpt" && available.status === "disconnected" ? "chatgpt" : "model-settings"}">${
      provider === "chatgpt" && available.status === "disconnected" ? t("Подключить ChatGPT", "Connect ChatGPT") : t("Обновить список", "Refresh list")}</button>`;
  }
  const prior = catalog.selections[provider] || (catalog.active.provider === provider ? catalog.active : undefined);
  const model = available.models.find((item) => item.id === prior?.model) || available.models[0];
  return `<label class="field">${t("Модель", "Model")}<select name="model">${available.models.map((item) =>
    `<option value="${e(item.id)}" ${item.id === model.id ? "selected" : ""}>${e(item.name)}</option>`).join("")}</select></label>
    <div data-model-reasoning>${reasoningFields(model, prior?.model === model.id ? prior.reasoning : "")}</div>${submit()}`;
}

async function openModelSettings() {
  const title = t("Модель и рассуждения", "Model and reasoning");
  openSheet(title, `<p data-model-loading class="sheet-desc" role="status">${t("Загружаю доступные модели…", "Loading available models…")}</p>`);
  const loading = sheet.querySelector("[data-model-loading]");
  try {
    const catalog = await request("/api/models");
    if (!sheet.open || !loading?.isConnected || sheet.classList.contains("closing")) return;
    modelCatalog = catalog;
    state.model = catalog;
    render();
    const provider = catalog.active.provider;
    const last = catalog.last ? `<p class="sheet-desc">${t("Последний ответ модели", "Last model reply")}<br>${e(modelSummary(catalog.last))}</p>` : "";
    openSheet(title, `<form id="model-form"><label class="field">${t("Провайдер", "Provider")}<select name="provider">
      <option value="chatgpt" ${provider === "chatgpt" ? "selected" : ""}>ChatGPT</option>
      <option value="opencode" ${provider === "opencode" ? "selected" : ""}>OpenCode Go</option></select></label>
      <div data-model-fields>${modelFields(catalog, provider)}</div></form>${last}`);
  } catch (error) {
    if (loading?.isConnected) loading.textContent = errorMessage(error);
  }
}
function sectionHead(title: string) {
  return `<h2 class="section-title">${title}</h2>`;
}
function overview() {
  const today = localDateTime(Date.now(), zone()).slice(0, 10);
  const day = (task: Task) => localDateTime(task.due_at || task.remind_at || null, zone()).slice(0, 10);
  const tasks = sorted();
  const sections: Record<PlanFilter, Task[]> = {
    today: tasks.filter(task => task.status === "open" && day(task) && day(task) <= today),
    upcoming: tasks.filter(task => task.status === "open" && day(task) > today),
    inbox: tasks.filter(task => task.status === "open" && !day(task)),
    done: tasks.filter(task => task.status === "done"),
  };
  const filters: [PlanFilter, string][] = [["today", t("Сегодня", "Today")], ["upcoming", t("Скоро", "Upcoming")],
    ["inbox", t("Входящие", "Inbox")], ["done", t("Готово", "Done")]];
  const empty = {today: t("На сегодня всё спокойно", "You’re clear for today"),
    upcoming: t("Впереди пока ничего", "Nothing scheduled yet"),
    inbox: t("Входящие разобраны", "Inbox clear"), done: t("Здесь будут выполненные", "Completed tasks will appear here")};
  return `<div class="plan-tabs" role="group" aria-label="${t("Разделы задач", "Task views")}">${filters.map(([key,label]) =>
    `<button data-filter="${key}" aria-pressed="${key === filter}">${label}${sections[key].length ? `<small>${sections[key].length}</small>` : ""}</button>`).join("")}
    </div>
    <div class="plan-heading">${sectionHead(t("Поручения","Tasks"))}<button class="action-plus" data-action="new-task" aria-label="${t("Добавить поручение","Add task")}">${icon("plus")}</button></div>
    ${sections[filter].length ? `<div class="agenda panel">${sections[filter].map(task => taskRow(task)).join("")}</div>` :
      `<div class="empty surface"><span class="empty-symbol">${icon(filter === "done" ? "check" : "calendar")}</span><strong>${empty[filter]}</strong>
       ${filter === "done" ? "" : `<button class="small-action" data-action="new-task">${t("Добавить поручение","Add task")}</button>`}</div>`}
    ${filter === "today" ? calendarSection() + routinesPanel() : ""}`;
}

function calendarSection() {
  if (state.connections?.calendar !== "connected") return "";
  return `<section class="workspace-section">${sectionHead("Google Calendar")}
    ${agenda ? (agenda.length ? `<div class="panel">${agenda.map(item =>
      `<div class="calendar-event"><strong>${e(item.title)}</strong><small>${item.start.dateTime ? e(formatDate("clock",item.start.dateTime)) : t("Весь день","All day")}${item.location ? " · " + e(item.location) : ""}</small></div>`).join("")}</div>`
      : `<p class="muted">${t("Событий на сегодня нет","No events today")}</p>`)
      : `<button class="small-action" data-action="load-agenda">${agendaError ? t("Повторить загрузку","Try again") : t("Показать события","Show events")}</button>`}</section>`;
}

function projectsPage() {
  if (bridge()) return `<div class="empty surface"><strong>${t("Проекты в чате","Projects in chat")}</strong><button class="button" data-action="chat">${t("В чат","Open chat")}</button></div>`;
  const project = selectedProject();
  if (projectDetail !== null && (!projectDetail || project)) return projectPage(project);
  const items = [{id:0,name:t("Личное","Personal"),context:""}, ...(state.projects || [])];
  return `<div class="plan-heading"><h2 class="section-title">${t("Рабочие пространства","Workspaces")}</h2><button class="action-plus" data-action="new-project" aria-label="${t("Новый проект","New project")}">${icon("plus")}</button></div>
    <div class="project-list">${items.map(item => {
      const count = state.tasks.filter(task => (task.project_id || 0) === item.id && task.status === "open").length;
      return `<button class="project-card surface" data-action="view-project" data-id="${item.id}">${glyph(item.id ? "projects" : "person")}
        <span><strong dir="auto">${e(item.name)}</strong><small>${count ? count + " · " + t("в работе","open") : t("Пока без задач","No open tasks")}
        ${item.id === activeId() ? ' <i class="signal-dot"></i> ' + t("В чате","In chat") : ""}</small></span>${icon("arrow")}</button>`;
    }).join("")}</div>`;
}

function projectPage(project?: Project) {
  const id = project?.id || 0;
  const entries = projectEntries.get(id);
  const tasks = sorted(state.tasks.filter(task => (task.project_id || 0) === id && task.status === "open"));
  const decision = entries?.find(entry => entry.kind === "decision");
  const next = tasks[0];
  return `<button class="small-action back-project" data-action="project-list">← ${t("Все проекты","All projects")}</button>
    <section class="project-hero surface">
      <div>${glyph(id ? "projects" : "person")}${id === activeId() ? `<span class="live-project"><i class="signal-dot"></i>${t("Активен в чате","Active in chat")}</span>` : ""}</div>
      <h2 dir="auto">${e(project?.name || projectName(0))}</h2>
      ${project?.context ? `<p class="project-goal" dir="auto">${e(project.context)}</p>` : ""}
      <button class="button primary" data-action="continue-project" data-id="${id}">${icon("plane")}${t("Продолжить в чате","Continue in chat")}</button>
    </section>
    ${decision || next ? `<section class="workspace-section">${sectionHead(t("С чего продолжить","Pick up where you left off"))}
      <div class="panel resume-panel">${decision ? `<p><small>${t("Последнее решение","Latest decision")}</small><strong dir="auto">${e(decision.text)}</strong></p>` : ""}
      ${next ? `<p><small>${t("Следующий шаг","Next step")}</small><button class="resume-task" data-action="edit-task" data-id="${next.id}">${e(next.title)} ${icon("arrow")}</button></p>` : ""}</div></section>` : ""}
    <section class="workspace-section"><div class="plan-heading">${sectionHead(t("Поручения","Tasks"))}<button class="action-plus compact" data-action="new-task" data-id="${id}" aria-label="${t("Добавить поручение","Add task")}">${icon("plus")}</button></div>
      ${tasks.length ? `<div class="panel">${tasks.map(task => taskRow(task,false)).join("")}</div>` : `<p class="muted">${t("Здесь появятся задачи проекта","Project tasks will appear here")}</p>`}</section>
    ${project ? `<section class="workspace-section"><div class="plan-heading">${sectionHead(t("Записи и материалы","Notes and materials"))}<button class="small-action" data-action="new-entry">${t("Добавить","Add")}</button></div>
      ${entries ? entries.map(entryRow).join("") || `<p class="muted">${t("Попроси Алефа сохранить решение или добавь материал.","Ask Aleph to save a decision or add a source.")}</p>`
        : projectLoadError === id ? `<button class="small-action" data-action="reload-project" data-id="${id}">${t("Повторить","Try again")}</button>`
        : `<p class="muted" role="status">${t("Загрузка…","Loading…")}</p>`}
      <button class="small-action" data-action="edit-project" data-id="${id}">${icon("edit")}${t("Изменить описание и инструкции","Edit description and instructions")}</button>
    </section>` : ""}`;
}

function entryRow(entry: ProjectEntry) {
  const label = {decision:t("Решение","Decision"), question:t("Открытый вопрос","Open question"), material:t("Материал","Material")}[entry.kind];
  const material = entry.kind === "material" && /^https:\/\/\S+$/.test(entry.text);
  return `<article class="entry-card surface"><small>${label}</small><div dir="auto">${material
    ? `<a href="${e(entry.text)}" target="_blank" rel="noopener noreferrer">${e(entry.text)}</a>` : e(entry.text)}</div>
    ${entry.source ? `<details><summary>${t("Из сообщения","From the message")}</summary><p dir="auto">${e(entry.source)}</p></details>` : ""}
    <button class="entry-remove" aria-label="${t("Удалить запись","Remove entry")}" data-action="remove-entry" data-id="${entry.id}">${icon("trash")}</button>
  </article>`;
}

async function loadProject(id: number) {
  if (!id) return;
  if (demo) { if (!projectEntries.has(id)) projectEntries.set(id, []); render(); return; }
  projectLoadError = 0;
  try {
    const entries = await request(`/api/projects/${id}/entries`);
    projectEntries.set(id, entries);
  } catch { projectLoadError = id; }
  if (projectDetail === id) render();
}
function projectSheet(item?: Project) {
  openSheet(
    item ? t("О проекте", "About the project") : t("Новый проект", "New project"),
    `<form id="project-form" data-id="${e(item?.id || "")}">
      <label class="field">${t("Название", "Name")}<input name="name" maxlength="64" required value="${e(item?.name || "")}" dir="auto" placeholder="${t("Например, Пилот", "For example, Pilot")}"></label>
      <label class="field">${t("Контекст", "Context")}<textarea name="context" maxlength="10000" rows="6" dir="auto" placeholder="${t("Цели проекта, важные детали, ссылки…", "Goals, important details, links…")}">${e(item?.context || "")}</textarea></label>
      <label class="file-label">${icon("plus")}${t("Добавить текстовый файл", "Add a text file")}<input type="file" accept=".txt,.md,.csv,.json,.tsv" name="reference"></label>
      ${submit(item ? saveLabel() : t("Создать проект", "Create project"))}
    </form>`);
}
function routinesPanel() {
  const items = state.routines || [];
  if (!items.length) return "";
  const names: Record<string, string> = {
    daily: t("Каждый день", "Daily"),
    weekdays: t("По будням", "Weekdays"),
    weekends: t("По выходным", "Weekends"),
    mon: t("пн", "Mon"),
    tue: t("вт", "Tue"),
    wed: t("ср", "Wed"),
    thu: t("чт", "Thu"),
    fri: t("пт", "Fri"),
    sat: t("сб", "Sat"),
    sun: t("вс", "Sun"),
  };
  const rows = items
    .map((item) => {
      const days = String(item.days || "")
        .split(",")
        .map((d) => names[d.trim()] || d)
        .join(", ");
      return `<div class="setting-row"><span class="setting-copy"><strong>${e(item.title)}</strong><span class="setting-detail">${e(days)} · ${e(timeLabel(item.time))}${item.active ? "" : " · " + t("На паузе", "Paused")}</span></span></div>`;
    })
    .join("");
  return `<section class="settings-block routine-block"><h2 class="settings-title">${t("Регулярные поручения", "Recurring tasks")}</h2><div class="panel">${rows}<button class="add-line" data-action="chat">${icon("plane")}${t("Изменить в чате", "Manage in chat")}</button></div></section>`;
}
function settingRow(symbol: IconName, label: string, detail?: string, action?: string, id?: string) {
  const contents = `${glyph(symbol)}<span class="setting-copy"><strong>${e(label)}</strong>${
    detail ? `<span class="setting-detail" dir="auto">${e(detail)}</span>` : ""
  }</span>`;
  return action
    ? `<button class="setting-row" data-action="${action}"${id ? ` data-id="${e(id)}"` : ""}>${contents}<span class="tail">${icon("arrow")}</span></button>`
    : `<div class="setting-row">${contents}</div>`;
}
function settingsGroup(title: string, rows: string) {
  return `<section class="settings-block"><h2 class="settings-title">${title}</h2><div class="panel">${rows}</div></section>`;
}
function replyStyleOptions(): [ReplyStyle, string][] {
  return [["short", t("Кратко", "Concise")], ["balanced", t("Стандартно", "Standard")], ["detailed", t("Подробно", "Detailed")]];
}
function themeOptions(): [ThemeMode, string][] {
  return [["auto", t("Как в Telegram", "Match Telegram")], ["white", t("Белый", "White")], ["dark", t("Тёмный", "Dark")]];
}
function settingChoices<T extends string>(name: string, label: string, options: [T, string][], selected: T) {
  return `<fieldset class="setting-choices"><legend>${label}</legend>${options.map(([value, title]) =>
    `<label class="setting-choice"><input type="radio" name="${name}" value="${value}" ${value === selected ? "checked" : ""}>${title}</label>`).join("")}</fieldset>`;
}
function enabledLabel(on: boolean) {
  return on ? t("Включено", "On") : t("Выключено", "Off");
}
function settingsPage() {
  const s = state.settings;
  const responseLabel = replyStyleOptions().find(([key]) => key === (s.reply_style ?? "balanced"))?.[1];
  const themeLabel = themeOptions().find(([key]) => key === themeMode())?.[1];
  const connections = serviceRows() + chatgptRow();
  return `<div class="settings-grid">
    ${settingsGroup(t("Ассистент", "Assistant"), modelRow() +
      settingRow("edit", t("Персонализация ответов", "Personalize replies"), responseLabel, "reply-preferences") +
      settingRow("bookmark", t("Личная память", "Personal memory"), enabledLabel(s.personal_memory !== false), "memory"))}
    ${settingsGroup(t("Приложение", "App"), settingRow("sun", t("Оформление", "Appearance"), themeLabel, "appearance") +
      settingRow("globe", t("Часовой пояс", "Time zone"), zone().replaceAll("_", " "), "timezone"))}
    ${settingsGroup(t("Уведомления", "Notifications"),
      settingRow("bell", t("Расписание уведомлений", "Notification schedule"),
        s.proactive ? (s.daily_digest !== false ? t("Утренний обзор в", "Morning brief at") + " " + timeLabel(s.digest_time)
          : t("Регулярные уведомления", "Recurring updates")) : t("Автоматические уведомления на паузе", "Automatic updates paused"), "notifications") +
      settingRow("rss", t("Источники новостей", "News sources"),
        state.feeds.length ? t("Подключено", "Connected") + " · " + state.feeds.length : t("Для утреннего обзора", "For your morning brief"), "feeds"))}
    ${connections ? settingsGroup(t("Подключения", "Connections"), connections) : ""}
    ${settingsGroup(t("Конфиденциальность", "Privacy"), settingRow("shield", t("Управление данными", "Manage data"),
      t("Что хранится и как это удалить", "What is stored and how to delete it"), "privacy"))}
  </div>`;
}
function appearanceSheet() {
  openSheet(t("Оформление", "Appearance"), `<form id="appearance-form">
    ${settingChoices("theme", t("Фон мини-приложения", "Mini App background"), themeOptions(), themeMode())}${submit()}</form>`);
}
function replyPreferencesSheet() {
  openSheet(t("Персонализация ответов", "Personalize replies"), `<form id="reply-form">
    ${settingChoices("reply_style", t("Длина ответа по умолчанию", "Default reply length"), replyStyleOptions(), state.settings.reply_style ?? "balanced")}
    ${bridge() ? "" : `<label class="field">${t("Личные инструкции", "Custom instructions")}
      <textarea name="assistant_instructions" dir="auto" rows="3" maxlength="600"
        placeholder="${t("Например: обращайся на «ты» и приводи примеры.", "For example: keep it casual and give examples.")}">${e(state.settings.assistant_instructions)}</textarea></label>`}
    ${submit()}</form>`);
}
function memoryList(items: SavedMemory[]) {
  if (!items.length) return `<p class="sheet-desc">${t("Сохранённых фактов пока нет.", "No saved facts yet.")}</p>`;
  return `<ul class="panel settings-memory-list">${items.map(item =>
    `<li class="setting-row"><span dir="auto">${e(item.text)}</span><button class="icon-button" data-action="forget-memory" data-id="${item.id}"
      aria-label="${e(t("Удалить факт", "Delete fact") + " «" + item.text + "»")}">${icon("trash")}</button></li>`).join("")}</ul>`;
}
async function memorySheet() {
  const enabled = state.settings.personal_memory !== false;
  openSheet(t("Личная память", "Personal memory"), `<div class="panel"><div class="setting-row">
    <span class="setting-copy"><strong id="memory-switch-label">${t("Использовать личную память", "Use personal memory")}</strong>
      <span class="setting-detail" id="memory-switch-detail">${t("Учитывать сохранённые факты и запоминать новые по твоей просьбе.", "Use saved facts and remember new ones when you ask.")}</span></span>
    <button class="switch" role="switch" aria-labelledby="memory-switch-label" aria-describedby="memory-switch-detail" aria-checked="${enabled}" data-action="toggle-memory"></button></div></div>
    ${bridge() ? ""
      : `<div class="memory-records"><h3>${t("Сохранённые факты", "Saved facts")}</h3>
        <div data-memory-list aria-live="polite"><p class="sheet-desc">${t("Загружаю…", "Loading…")}</p></div></div>`}`);
  const list = sheet.querySelector("[data-memory-list]");
  if (!list) return;
  try {
    const items = demo ? [] : await request("/api/memory");
    if (list.isConnected) list.innerHTML = memoryList(items);
  } catch (error) {
    if (list.isConnected) list.innerHTML = `<p class="sheet-desc">${e(errorMessage(error))}</p>
      <button class="button" data-action="memory">${t("Повторить", "Try again")}</button>`;
  }
}
function privacySheet() {
  openSheet(t("Управление данными", "Manage data"),
    `<p class="sheet-desc">${t("Настройки, проекты и задачи привязаны к твоему аккаунту Telegram.", "Settings, projects and tasks are linked to your Telegram account.")}</p>
    <p class="sheet-desc">${t("Чтобы удалить данные Алефа, напиши в личном чате «удали мои данные» и подтверди удаление.",
      "To delete your Aleph data, send “delete my data” in your private chat and confirm.")}</p>
    <div class="submit-row"><a class="button" href="./privacy.html" target="_blank" rel="noopener noreferrer">${t("Политика конфиденциальности", "Privacy policy")}</a>
      <button class="button primary" data-action="chat">${t("Открыть чат", "Open chat")}</button></div>`);
}
function openSheet(title: string, html: string) {
  if (!sheet.open) {
    returnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    focusTarget = controlSelector(returnFocus);
  }
  clearTimeout(closeTimer);
  if (closeListener) sheet.removeEventListener("animationend",closeListener);
  sheet.classList.remove("closing");
  sheet.style.removeProperty("--sheet-release");
  sheet.innerHTML = `<div class="sheet-body"><div class="sheet-grab" aria-hidden="true"></div><div class="sheet-head"><h2 id="sheet-heading">${title}</h2><button class="icon-button" aria-label="${t("Закрыть", "Close")}" data-action="close">${icon("close")}</button></div>${html}</div>`;
  sheet.setAttribute("aria-labelledby", "sheet-heading");
  if (!sheet.open) sheet.showModal();
  syncBack();
}
function closeSheet() {
  if (!sheet.open || sheet.classList.contains("closing")) return;
  if (reducedMotion.matches) { sheet.close(); return; }
  function finish(event?: AnimationEvent) {
    if (event && (event.target !== sheet || event.pseudoElement)) return;
    clearTimeout(closeTimer);
    sheet.removeEventListener("animationend", finish);
    closeListener = undefined;
    sheet.close();
    sheet.classList.remove("closing");
    sheet.style.removeProperty("--sheet-release");
  }
  closeListener = finish;
  sheet.classList.add("closing");
  sheet.addEventListener("animationend", finish);
  closeTimer=window.setTimeout(finish,220);
}
sheet.addEventListener("cancel", event=>{
  event.preventDefault();
  if(!pending) closeSheet();
});
sheet.addEventListener("pointerdown", (event) => {
  if (pending || sheetDrag || sheet.classList.contains("closing") ||
      !event.isPrimary || event.button !== 0 || !(event.target instanceof Element) || !event.target.closest(".sheet-grab")) return;
  sheetDrag = { id: event.pointerId, start: event.clientY, offset: 0 };
  sheet.setPointerCapture(event.pointerId);
  sheet.classList.add("dragging");
});
sheet.addEventListener("pointermove", (event) => {
  if (sheetDrag?.id !== event.pointerId) return;
  sheetDrag.offset = Math.max(0, event.clientY - sheetDrag.start);
  sheet.style.transform = `translateY(${sheetDrag.offset}px)`;
});
function releaseSheet(event: PointerEvent) {
  if (sheetDrag?.id !== event.pointerId) return;
  const offset = sheetDrag.offset;
  sheetDrag = undefined;
  sheet.classList.remove("dragging");
  sheet.style.removeProperty("transform");
  if (event.type === "pointerup" && offset > 70 && !pending) {
    sheet.style.setProperty("--sheet-release", `${offset}px`);
    closeSheet();
  } else if (offset && !reducedMotion.matches) {
    sheet.animate([{transform:`translateY(${offset}px)`},{transform:"translateY(0)"}],
                  {duration:175,easing:"cubic-bezier(.2,.8,.2,1)"});
  }
}
sheet.addEventListener("pointerup", releaseSheet);
sheet.addEventListener("pointercancel", releaseSheet);
const saveLabel = () =>
  bridge()
    ? t("Сохранить и вернуться в чат", "Save and return to chat")
    : t("Сохранить", "Save");
function submit(label = saveLabel()) {
  return `<p class="form-error" role="alert"></p><div class="submit-row"><button type="submit" class="button primary">${label}</button></div>`;
}
function taskSheet(task?: Task, projectId = activeId()) {
  if (task?.report_state === "sent") {
    openSheet(task.title, `<p class="sheet-desc">${t("Ответ доставлен в чат с Алефом.","The result was delivered to your Aleph chat.")}</p>
      <p class="sheet-desc" dir="auto">${e(task.notes)}</p>`);
    return;
  }
  const report = Boolean(task?.report_state);
  const fields = [ {id:0,name:t("Личное","Personal")}, ...(state.projects || []) ];
  const selected = task?.project_id ?? projectId;
  openSheet(task ? t("Поручение", "Task") : t("Новое поручение", "New task"),
    `${report ? `<p class="sheet-desc">${task?.report_state === "failed"
      ? t("Алеф не смог выполнить задачу. Назначь новое время запуска.","Aleph couldn’t finish. Set a new time to retry.")
      : t("Алеф подготовит и пришлёт результат в чат.","Aleph will prepare the result and send it in chat.")}</p>` : ""}
    <form id="task-form" data-id="${task?.id || ""}">
      <label class="field">${t("Что нужно сделать?","What needs to happen?")}<input name="title" value="${e(task?.title || "")}" required maxlength="300" placeholder="${t("Например, сверить макеты","For example, review the designs")}" dir="auto"></label>
      <label class="field">${t("Проект","Project")}<select name="project_id">${fields.map(project => `<option value="${project.id}" ${project.id === selected ? "selected" : ""}>${e(project.name)}</option>`).join("")}</select></label>
      <details class="task-details" ${task?.due_at || task?.remind_at || task?.notes ? "open" : ""}><summary>${report ? t("Запуск и поручение","Schedule and instructions") : t("Срок, напоминание и заметки","Due date, reminder and notes")}</summary>
        <label class="field">${report ? t("Время запуска","Run at") : t("Срок","Due date")}<input name="due_at" type="datetime-local" ${report ? "required" : ""} value="${e(localDateTime(task?.due_at ?? null,zone()))}">${report ? `<small>${e(zone())}</small>` : ""}</label>
        ${report ? "" : `<label class="field">${t("Напомнить","Remind me")}<input name="remind_at" type="datetime-local" value="${e(localDateTime(task?.remind_at ?? null,zone()))}"><small>${e(zone())}</small></label>`}
        <label class="field">${report ? t("Что подготовить","What to prepare") : t("Заметки","Notes")}<textarea name="notes" maxlength="4000" rows="3" dir="auto" ${report ? "required" : ""}>${e(task?.notes || "")}</textarea></label>
      </details>
      ${task?.source ? `<details class="task-details"><summary>${t("Исходное сообщение","Original message")}</summary><p class="sheet-desc" dir="auto">${e(task.source)}</p></details>` : ""}
      ${submit()}</form>`);
}
function entrySheet() {
  openSheet(t("Запись проекта", "Project note"), `<form id="entry-form">
    <label class="field">${t("Тип","Type")}<select name="kind"><option value="decision">${t("Решение","Decision")}</option><option value="question">${t("Открытый вопрос","Open question")}</option><option value="material">${t("Материал","Material")}</option></select></label>
    <label class="field">${t("Текст или ссылка","Text or link")}<textarea name="text" maxlength="2000" required rows="4" dir="auto"></textarea></label>${submit()}</form>`);
}
function timezoneSheet() {
  const current = zone();
  const guessed = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const zones = [...new Set([current, "UTC", ...(guessed ? [guessed] : []),
    ...(Intl.supportedValuesOf?.("timeZone") ?? ["Europe/Moscow", "Europe/London", "Europe/Berlin", "Asia/Yerevan", "America/New_York", "Asia/Tokyo"])])];
  openSheet(t("Часовой пояс", "Time zone"), `<form id="timezone-form">
    <p class="sheet-desc">${t("По этому времени Алеф присылает напоминания и выполняет запланированные задачи.", "Aleph uses this time zone for reminders and scheduled tasks.")}</p>
    <label class="field">${t("Часовой пояс", "Time zone")}<select name="timezone">${zones.map(z =>
      `<option value="${e(z)}" ${z === current ? "selected" : ""}>${e(z.replaceAll("_", " "))}${z === guessed ? " · " + t("На устройстве", "Device") : ""}</option>`).join("")}</select></label>${submit()}</form>`);
}
function notificationsSheet() {
  const s = state.settings;
  openSheet(t("Расписание уведомлений", "Notification schedule"), `<form id="notifications-form">
    <div class="panel">
      <label class="setting-row notification-option"><span class="setting-copy"><strong id="automatic-label">${t("Автоматические уведомления", "Automatic updates")}</strong>
        <span class="setting-detail" id="automatic-detail">${t("Утренний обзор и регулярные сообщения в чате.", "Morning brief and recurring messages in chat.")}</span></span>
        <input type="checkbox" name="proactive" aria-labelledby="automatic-label" aria-describedby="automatic-detail" ${s.proactive ? "checked" : ""}></label>
      <label class="setting-row notification-option"><span class="setting-copy"><strong id="digest-label">${t("Утренний обзор", "Morning brief")}</strong>
        <span class="setting-detail" id="digest-detail">${t("Открытые задачи и новости из выбранных источников.", "Open tasks and news from your chosen sources.")}</span></span>
        <input type="checkbox" name="daily_digest" aria-labelledby="digest-label" aria-describedby="digest-detail" ${s.daily_digest !== false ? "checked" : ""}></label>
    </div>
    <label class="field">${t("Время утреннего обзора", "Morning brief time")}<input type="time" required name="digest_time" value="${e(timeLabel(s.digest_time))}"></label>
    <fieldset class="setting-choices"><legend>${t("Тихие часы", "Quiet hours")}</legend><div class="quiet-hours">
      <label class="field">${t("С", "From")}<input type="time" required name="quiet_start" value="${e(timeLabel(s.quiet_start))}"></label>
      <label class="field">${t("До", "Until")}<input type="time" required name="quiet_end" value="${e(timeLabel(s.quiet_end))}"></label>
    </div></fieldset>
    <p class="sheet-desc">${t("Уведомления по расписанию учитывают тихие часы. Разовые напоминания приходят в указанное время.", "Automatic updates respect quiet hours. One-time reminders arrive at the time you set.")}</p>
    ${submit()}</form>`);
}
function feedsSheet() {
  const catalog = state.feed_catalog || [],
    choices = catalog.filter((x) => !state.feeds.some((f) => f.url === x.url));
  openSheet(
    t("Источники новостей", "News sources"),
    `<p class="sheet-desc">${t("Выбери источники для новостей в утреннем обзоре.", "Choose news sources for your morning brief.")}</p>${state.feeds.length ? `<div class="feed-items">${state.feeds.map((f) => `<div class="feed-item"><span>${e(f.title || f.url)}</span><button class="feed-remove" aria-label="${e(t("Отключить", "Disconnect") + " " + (f.title || f.url))}" data-action="remove-feed" data-id="${e(f.id)}">${icon("trash")}</button></div>`).join("")}</div>` : ""}${choices.length ? `<form id="feed-form"><label class="field">${t("Источник", "Source")}<select name="url">${choices.map((c) => `<option value="${e(c.url)}">${e(c.title)}</option>`).join("")}</select></label>${submit(bridge() ? t("Подключить и вернуться в чат", "Connect and return to chat") : t("Подключить", "Connect"))}</form>` : `<p class="settings-note">${t("Все доступные источники подключены.", "All available sources are connected.")}</p>`}`,
  );
}
function openChat() {
  const tg = webapp();
  if (!demo && tg?.close) {
    tg.close();
    return;
  }
  window.open("https://t.me/AlephAgentBot", "_blank", "noopener,noreferrer");
}
async function load() {
  if (demo) {
    state = demoState(ru);
  } else {
    state = await request("/api/state");
    setLanguage(
      state.user.language_code ||
        webapp()?.initDataUnsafe?.user?.language_code ||
        "ru",
    );
  }
  state.feeds ??= [];
  state.settings.digest_time ??= "09:00";
  setTheme();
  render();
  if (!demo && !bridge()) void syncChatGPT();
}

async function syncChatGPT(showStatus = false) {
  const sequence = revision;
  try {
    const connection = await request("/api/chatgpt");
    if (sequence !== revision) return;
    chatgpt = connection;
    if (connection.model_settings) state.model = connection.model_settings;
    render();
    if (showStatus) {
      if (chatgpt.connected) chatgptSheet();
      else message(t("Жду завершения входа в браузере", "Waiting for browser sign-in"));
    }
  } catch (error) {
    if (showStatus) message(errorMessage(error));
  }
}
function mergeRecord<T extends { id: number }>(items: T[], record: T) {
  const existing=items.find(item=>item.id===record.id);
  if(existing) Object.assign(existing,record);
  else items.unshift(record);
}

async function write<Path extends keyof Writes>(
  path: Path,
  method: WriteMethod,
  body: object | undefined,
  apply: (result: Writes[Path] | null) => void,
  success: string,
  options?: RequestOptions & {displayAfter?: Promise<unknown>},
) {
  if (pending) return false;
  pending = true;
  const origin=document.activeElement?.closest("button");
  origin?.setAttribute("aria-busy","true");
  app.classList.add("saving");
  revision++;
  sheet
    .querySelectorAll<HTMLInputElement | HTMLButtonElement | HTMLSelectElement | HTMLTextAreaElement>("button,input,select,textarea")
    .forEach((x) => (x.disabled = true));
  try {
    const result = demo ? null : await request(path, method, body, options);
    if (options?.displayAfter) await options.displayAfter;
    if (!bridge()) {
      apply(result);
      if (demo) saveDemo(state);
      render();
    }
    try {
      if (!demo) webapp()?.HapticFeedback?.notificationOccurred("success");
    } catch {}
    if (success) message(success);
    return true;
  } catch (error) {
    const errorNode = sheet.open && sheet.querySelector(".form-error");
    if (errorNode) errorNode.textContent = errorMessage(error);
    else message(errorMessage(error));
    return false;
  } finally {
    origin?.removeAttribute("aria-busy");
    pending = false;
    app.classList.remove("saving");
    sheet
      .querySelectorAll<HTMLInputElement | HTMLButtonElement | HTMLSelectElement | HTMLTextAreaElement>("button,input,select,textarea")
      .forEach((x) => (x.disabled = false));
  }
}
function saveSettings(fields: Partial<Settings>, success = t("Настройки сохранены", "Settings saved")) {
  return write("/api/settings", "PATCH", fields, result => {
    Object.assign(state.settings, result ?? fields);
  }, success);
}
async function act(action: string, id?: string) {
  if (pending) return;
  switch (action) {
    case "close":
      closeSheet();
      break;
    case "chat":
      openChat();
      break;
    case "new-project":
      if (bridge()) openChat();
      else projectSheet();
      break;
    case "edit-project":
      if (bridge()) openChat();
      else projectSheet((state.projects || []).find((p) => String(p.id) === String(id)));
      break;
    case "project-list":
      projectDetail = null;
      render();
      break;
    case "view-project":
    case "reload-project":
      projectDetail = Number(id);
      render();
      await loadProject(projectDetail);
      break;
    case "continue-project": {
      const project_id = Number(id);
      if (project_id === activeId() || await write("/api/projects/active","PATCH",{project_id},
        () => { state.active_project_id = project_id; }, "")) openChat();
      break;
    }
    case "new-entry":
      if (selectedProject()) entrySheet();
      break;
    case "remove-entry": {
      const selected = selectedProject();
      if (!selected || !window.confirm(t("Удалить эту запись?","Remove this note?"))) return;
      await write(`/api/projects/${selected.id}/entries/${Number(id)}`,"DELETE",undefined,
        () => projectEntries.set(selected.id,(projectEntries.get(selected.id) || []).filter(entry => entry.id !== Number(id))), "");
      break;
    }
    case "load-agenda":
      await loadAgenda();
      break;
    case "service": {
      const selected = serviceKey(id);
      if (selected) serviceSheet(selected);
      break;
    }
    case "connect-service": {
      const selected = serviceKey(id);
      if (selected) await connectionLink(selected);
      break;
    }
    case "drive-picker":
      await connectionLink("drive",true);
      break;
    case "disconnect-service": {
      const selected = serviceKey(id);
      if (!selected) return;
      if (await write(`/api/connections/${selected}`,"DELETE",undefined,result => {
        if (result) state.connections = result;
        if (selected === "calendar") agenda = undefined;
      },t("Отключено","Disconnected"))) closeSheet();
      break;
    }
    case "new-task":
      taskSheet(undefined,id === undefined ? activeId() : Number(id));
      break;
    case "edit-task":
      if (byId(id)) taskSheet(byId(id));
      break;
    case "appearance":
      appearanceSheet();
      break;
    case "reply-preferences":
      replyPreferencesSheet();
      break;
    case "memory":
      await memorySheet();
      break;
    case "toggle-memory": {
      const personal_memory = state.settings.personal_memory === false;
      if (await saveSettings({personal_memory}, "")) {
        sheet.querySelector('[data-action="toggle-memory"]')?.setAttribute("aria-checked", String(personal_memory));
      }
      break;
    }
    case "forget-memory": {
      const memoryId = Number(id);
      if (!Number.isSafeInteger(memoryId) || memoryId <= 0 || !window.confirm(t("Удалить этот факт из памяти?", "Remove this saved fact?"))) break;
      if (await write(`/api/memory/${memoryId}`, "DELETE", undefined, () => {}, t("Факт удалён", "Fact deleted"))) await memorySheet();
      break;
    }
    case "timezone":
      timezoneSheet();
      break;
    case "notifications":
      notificationsSheet();
      break;
    case "privacy":
      privacySheet();
      break;
    case "feeds":
      feedsSheet();
      break;
    case "chatgpt":
      chatgptSheet();
      break;
    case "model-settings":
      await openModelSettings();
      break;
    case "pair-chatgpt":
      await write("/api/chatgpt/pair", "POST", undefined, (pairing) => {
        if (pairing) chatgptSheet(pairing);
      }, "");
      break;
    case "copy-chatgpt-code": {
      const input = sheet.querySelector<HTMLInputElement>("#chatgpt-pair-code");
      if (!input) break;
      try {
        await navigator.clipboard.writeText(input.value);
        message(t("Код скопирован", "Code copied"));
      } catch {
        input.focus();
        input.select();
      }
      break;
    }
    case "refresh-chatgpt":
      await syncChatGPT(true);
      break;
    case "disconnect-chatgpt":
      await write("/api/chatgpt", "DELETE", undefined, (connection) => {
        if (!connection) return;
        chatgpt = connection;
        if (connection.model_settings) state.model = connection.model_settings;
        chatgptSheet();
        message(connection.revoked === false
          ? t("Отключено в Алефе. Отозвать разрешение можно в настройках ChatGPT.", "Disconnected from Aleph. Revoke access in your ChatGPT settings.")
          : t("ChatGPT отключён в Алефе", "ChatGPT disconnected from Aleph"));
      }, "");
      break;
    case "toggle-task":
    case "undo-task": {
      const item = byId(action === "undo-task" ? String(undoTask) : id);
      if (!item) return;
      const before = item.status;
      const status = before === "done" ? "open" : "done";
      const row = activeView()?.content.querySelector(`[data-action="toggle-task"][data-id="${item.id}"]`)?.closest(".task-row");
      const animation = row && !reducedMotion.matches
        ? row.animate([{opacity:1,transform:"translateX(0)"},{opacity:.12,transform:"translateX(8px)"}],
                      {duration:150,easing:"ease-out",fill:"both"})
        : undefined;
      const displayAfter = animation?.finished.catch(() => undefined);
      const notice = item.report_state
        ? (status === "done" ? t("Запуск на паузе","Scheduled run paused") : t("Запуск возобновлён","Scheduled run resumed"))
        : (status === "done" ? t("Поручение выполнено","Task complete") : t("Поручение в работе","Task reopened"));
      const ok = await write(`/api/tasks/${item.id}`,"PATCH",{status},
        task => Object.assign(item,task || {status}), notice, {displayAfter});
      animation?.cancel();
      if (ok && !bridge() && action === "toggle-task") {
        message(notice, 6500);
        undoTask = item.id;
        const button = document.createElement("button");
        button.textContent = t("Отменить","Undo");
        button.dataset.action = "undo-task";
        toastEl.append(button);
      } else undoTask = undefined;
      break;
    }
    case "remove-feed": {
      if (
        await write(
          `/api/feeds/${id}`,
          "DELETE",
          undefined,
          () =>
            (state.feeds = state.feeds.filter(
              (f) => String(f.id) !== String(id),
            )),
          t("Источник отключён", "Source disconnected"),
        )
      )
        feedsSheet();
      break;
    }
    case "retry":
      await start();
      break;
  }
}
document.addEventListener("click", (event) => {
  const target = event.target instanceof Element ? event.target : null;
  const link = target?.closest<HTMLAnchorElement>("[data-external-link]");
  if (link && webapp()?.openLink) {
    event.preventDefault();
    webapp()?.openLink?.(link.href);
    closeSheet();
    return;
  }
  const el = target?.closest("button");
  if (!el || el.disabled || sheet.classList.contains("closing")) return;
  if (el.dataset.page) navigate(el.dataset.page);
  else if (el.dataset.filter && ["today","upcoming","inbox","done"].includes(el.dataset.filter)) {
    if(filter===el.dataset.filter)return;
    selectionFeedback();
    filter = el.dataset.filter as PlanFilter;
    render();
  } else if (el.dataset.action) void act(el.dataset.action, el.dataset.id);
});
sheet.addEventListener("close", () => {
  syncBack();
  const target = returnFocus?.isConnected ? returnFocus : focusTarget ? app.querySelector<HTMLElement>(focusTarget) : null;
  (target || activeView()?.heading)?.focus({preventScroll:true});
  returnFocus=null;
  focusTarget=null;
});
sheet.addEventListener("click", (event) => {
  if (event.target !== sheet || pending) return;
  const r = sheet.getBoundingClientRect();
  if (
    event.clientX < r.left ||
    event.clientX > r.right ||
    event.clientY < r.top ||
    event.clientY > r.bottom
  )
    closeSheet();
});
sheet.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target;
  if (pending || !(form instanceof HTMLFormElement)) return;
  const data = new FormData(form);
  const field = (name: string) => {
    const value = data.get(name);
    return typeof value === "string" ? value : "";
  };
  let ok = false;
  try {
    if (form.id === "project-form") {
      const name = field("name").trim();
      if (!name) {
        const input = requiredElement<HTMLInputElement>(form, 'input[name="name"]');
        input.value = "";
        input.reportValidity();
        return;
      }
      const context = field("context").trim();
      const id = Number(form.dataset.id);
      ok = await write(id ? `/api/projects/${id}` : "/api/projects", id ? "PATCH" : "POST", id ? {name, context} : {name,context,activate:false}, result => {
        state.projects ||= [];
        const project=result || {id:id || Date.now(),name,context};
        mergeRecord(state.projects,project);
        projectDetail = project.id;
        if (!id) projectEntries.set(project.id, []);
      }, id ? t("Контекст сохранён", "Context saved") : t("Проект создан", "Project created"));
    } else if (form.id === "entry-form" && projectDetail) {
      const id = projectDetail;
      const kind = field("kind") as ProjectEntry["kind"];
      const text = field("text").trim();
      if (!text) return;
      ok = await write(`/api/projects/${id}/entries`,"POST",{kind,text},result => {
        projectEntries.set(id,[result ?? {id:Date.now(),kind,text,source:"",created_at:new Date().toISOString()},
                              ...(projectEntries.get(id) || [])]);
      },t("Запись сохранена","Note saved"));
    } else if (form.id === "task-form") {
      const title = field("title").trim();
      if (!title) {
        const input = requiredElement<HTMLInputElement>(form, 'input[name="title"]');
        input.value = "";
        input.reportValidity();
        return;
      }
      const body = { title, due_at: zonedISO(field("due_at"), zone()), remind_at: zonedISO(field("remind_at"),zone()),
        project_id: Number(field("project_id")), notes: field("notes") };
      const id = form.dataset.id;
      const fields=body;
      // A retry after a dropped response must reuse the same creation on the server.
      if (!id && JSON.stringify(fields)!==form.dataset.submission) {
        form.dataset.submission=JSON.stringify(fields);
        form.dataset.requestKey=crypto.randomUUID?.() || (Date.now().toString(36)+Math.random().toString(36).slice(2));
      }
      ok = await write(
        id ? `/api/tasks/${id}` : "/api/tasks",
        id ? "PATCH" : "POST", fields,
        result => {
          const existing = byId(id);
          const record: Task = result ?? (existing ? { ...existing, ...body } : {
            ...fields, id: Math.max(0, ...state.tasks.map((task) => task.id)) + 1, status: "open", notes: "",
          });
          mergeRecord(state.tasks, record);
          if (page === "home" && !id) {
            const scheduled = fields.due_at || fields.remind_at;
            filter = !scheduled ? "inbox" : localDateTime(scheduled,zone()).slice(0,10) > localDateTime(Date.now(),zone()).slice(0,10) ? "upcoming" : "today";
          }
        },
        t("Поручение сохранено","Task saved"),
        {requestKey: id ? null : form.dataset.requestKey}
      );
    } else if (form.id === "model-form") {
      ok = await write("/api/models", "PATCH", {
        provider: field("provider"), model: field("model"), reasoning: field("reasoning"),
      }, (result) => {
        if (result) state.model = result;
      }, t("Настройки модели сохранены", "Model settings saved"));
    } else if (form.id === "appearance-form") {
      const theme = field("theme") as ThemeMode;
      ok = await saveSettings({theme});
      if (ok) setTheme();
    } else if (form.id === "reply-form") {
      const fields: Partial<Settings> = {reply_style: field("reply_style") as ReplyStyle};
      if (data.has("assistant_instructions")) fields.assistant_instructions = field("assistant_instructions");
      ok = await saveSettings(fields);
    } else if (form.id === "timezone-form") {
      ok = await saveSettings({timezone: field("timezone")});
    } else if (form.id === "notifications-form") {
      ok = await saveSettings({
        proactive: data.has("proactive"),
        daily_digest: data.has("daily_digest"),
        digest_time: field("digest_time"),
        quiet_start: field("quiet_start"),
        quiet_end: field("quiet_end"),
      });
    } else if (form.id === "feed-form") {
      const url = field("url");
      const catalogEntry = state.feed_catalog.find((source) => source.url === url);
      ok = await write(
        "/api/feeds",
        "POST",
        { url },
        result => {
          const source = result ?? { url, title: catalogEntry?.title ?? url, id: Date.now() };
          mergeRecord(state.feeds,source);
        },
        t("Источник подключён", "Source connected"),
      );
      if (ok) {
        feedsSheet();
        return;
      }
    }
    if (ok) closeSheet();
  } catch (error) {
    const node = form.querySelector(".form-error");
    if (node) node.textContent = errorMessage(error);
  }
});
sheet.addEventListener("change", async (event) => {
  const picker = event.target;
  if (picker instanceof HTMLSelectElement && picker.form?.id === "model-form" && modelCatalog) {
    if (picker.name === "provider" && (picker.value === "chatgpt" || picker.value === "opencode")) {
      requiredElement(picker.form, "[data-model-fields]").innerHTML = modelFields(modelCatalog, picker.value);
    } else if (picker.name === "model") {
      const provider = picker.form.querySelector<HTMLSelectElement>('[name="provider"]')?.value;
      if (provider !== "chatgpt" && provider !== "opencode") return;
      const model = modelCatalog.providers[provider].models.find((item) => item.id === picker.value);
      const prior = modelCatalog.selections[provider];
      if (model) requiredElement(picker.form, "[data-model-reasoning]").innerHTML =
        reasoningFields(model, prior?.model === model.id ? prior.reasoning : "");
    }
    return;
  }
  if (!(picker instanceof HTMLInputElement) || picker.name !== "reference" || !picker.form || !picker.files?.[0]) return;
  const note = requiredElement<HTMLTextAreaElement>(picker.form, 'textarea[name="context"]');
  const file = picker.files[0];
  const error = requiredElement(picker.form, ".form-error");
  error.textContent = "";
  try {
    if (file.size > 40000) throw new Error();
    const text = await file.text();
    const combined = [note.value.trim(), file.name + "\n" + text.trim()].filter(Boolean).join("\n\n");
    if (combined.length > 10000 || text.includes("\u0000")) throw new Error();
    note.value = combined;
  } catch {
    error.textContent = t("Контекст должен быть текстом до 10 000 символов. Выбери файл поменьше.", "Use a text file that keeps the context under 10,000 characters.");
  } finally { picker.value = ""; }
});
let loading = false;
async function start() {
  if (loading) return;
  loading = true;
  app.setAttribute("aria-busy", "true");
  try {
    initTelegram();
    if (!demo && !bridge() && location.hostname.endsWith(".github.io"))
      throw new Error("static-plan");
    await load();
  } catch (error) {
    app.setAttribute("aria-busy", "false");
    sizeObserver?.disconnect();
    stage = undefined;
    views = [];
    app.innerHTML = `<main class="failure"><img src="./aleph.webp" width="45" alt=""><h1>Aleph</h1><p>${e(errorMessage(error))}</p><div class="actions"><button class="button" data-action="retry">${t("Повторить", "Try again")}</button><button class="button primary" data-action="chat">${t("В чат", "Open chat")}</button></div></main>`;
  } finally {
    loading = false;
  }
}
async function refresh() {
  if (
    document.hidden ||
    demo ||
    bridge() ||
    !state ||
    sheet.open ||
    pending ||
    loading || refreshRunning || swipe?.locked
  )
    return;
  const sequence = revision;
  refreshRunning = true;
  try {
    const fresh = await request("/api/state");
    if (!pending && !sheet.open && !swipe?.locked && sequence === revision) {
      const previouslyConnected = state.connections?.calendar;
      state = fresh;
      setTheme();
      render();
      if (page === "projects" && projectDetail) void loadProject(projectDetail);
      if (fresh.connections?.calendar === "connected" && (previouslyConnected !== "connected" || page === "home")) void loadAgenda();
      if (previouslyConnected === "connected" && fresh.connections?.calendar !== "connected") agenda = undefined;
      if (chatgpt?.available) void syncChatGPT();
    }
  } catch {} finally { refreshRunning = false; }
}
document.addEventListener("visibilitychange", refresh);
window.addEventListener("focus", refresh);
document
  .querySelector('script[src*="telegram-web-app"]')
  ?.addEventListener("load", initTelegram, { once: true });
window.addEventListener("load", initTelegram, { once: true });
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", setTheme);
setTheme();
setLanguage(
  webapp()?.initDataUnsafe?.user?.language_code ||
    (demo ? "ru" : navigator.language),
);
void start();
