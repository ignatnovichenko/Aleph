import { icon } from "./icons.js";
import { revealMiniApp } from "./telegram.js";
import type { IconName } from "./icons.js";
import type { RequestOptions, WriteMethod, Writes } from "./store.js";
import type { CalendarEvent, ChatGPTConnection, ChatGPTPairing, ProjectEntry, ReplyStyle, SavedMemory, Service, ModelCatalog, ModelChoice, ModelProvider, Project, ProviderModel, Settings, Task, ThemeMode, WorkspaceState } from "./types.js";
import {
  bridge,
  demo,
  request,
  signed,
  workspaceState,
  demoState,
  saveDemo,
  escapeHtml as e,
  localDateTime,
  zonedISO,
} from "./store.js";

declare const __ALEPH_MARK_URL__: string;
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
const initialView = new URLSearchParams(location.search);
let initialTaskOpened = false;
let page: Page = initialView.get("view") === "settings" ? "settings" : "home";
type PlanFilter = "today" | "upcoming" | "inbox" | "done";
let filter: PlanFilter = "today";
let projectDetail: number | null = null;
let projectListScroll = 0;
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
  revealMiniApp();
  setTheme();
  tg.onEvent?.("themeChanged", setTheme);
  tg.BackButton?.onClick(() => {
    if (sheet.open) {
      if (!pending) closeSheet();
    } else if (page === "projects" && projectDetail !== null) showProjectList();
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
  origin: {position: number; x: number} | undefined;
  x: number;
  time: number;
  velocity: number;
  frame: number;
}

let stage: HTMLElement | undefined;
let track: HTMLElement, dock: HTMLElement, lens: HTMLElement, tabs: HTMLButtonElement[];
let views: View[] = [];
let sizeObserver: ResizeObserver | undefined;
let warmingViews = false;
let trackWidth = 0, tabWidth = 0, suppressTap = false;
let swipe: Swipe | undefined;
let navigationFrame = 0;
let navigationPosition = 0;
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
          <header class="page-head"><img class="app-mark" src="${__ALEPH_MARK_URL__}" width="30" height="30" alt="Aleph" decoding="async">
            <h1 class="page-title" id="title-${key}" tabindex="-1">${label}</h1>
            ${key === "home" ? '<p class="page-kicker"></p>' : ""}
          </header>
          <div class="page-content"></div>${footer}
        </div>
      </section>`).join("")}</div></main>
    <nav aria-label="${t("Навигация", "Navigation")}" class="dock">
      <span aria-hidden="true" class="dock-surface"></span>
      <span aria-hidden="true" class="dock-lens"><span class="dock-lens-glass"></span></span>${nav()}
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
  showPage(activeView()?.index ?? 0, false);
}

function renderView(view: View) {
  const markup = view.key === "home" ? overview() : view.key === "projects" ? projectsPage() : settingsPage();
  if (view.markup === markup) return;
  const restore = view.content.contains(document.activeElement) ? controlSelector(document.activeElement) : null;
  view.content.innerHTML = markup;
  view.markup = markup;
  if (restore) view.content.querySelector<HTMLElement>(restore)?.focus({preventScroll: true});
}

function render() {
  if (!state) return;
  if (!stage?.isConnected) mountShell();
  const date = formatDate("date", Date.now());
  const kicker = requiredElement(track, ".page-kicker");
  if (kicker.textContent !== date) kicker.textContent = date;
  for (const view of views) {
    if (view.key === page || view.markup !== undefined) renderView(view);
  }
  app.setAttribute("aria-busy", "false");
  syncBack();
  if (!warmingViews && views.some(view => view.markup === undefined)) {
    warmingViews = true;
    // Give the first screen a paint before preparing the other tabs.
    requestAnimationFrame(() => requestAnimationFrame(() => {
      const warm = () => {
        warmingViews = false;
        for (const view of views) if (view.markup === undefined) renderView(view);
      };
      if (typeof requestIdleCallback === "function") requestIdleCallback(warm, {timeout: 250});
      else warm();
    }));
  }
}

function paintNavigation(position: number) {
  navigationPosition = position;
  const bounded = clamp(position, 0, views.length - 1);
  track.style.transform = `translate3d(${-position * 100}%,0,0)`;
  lens.style.transform = `translate3d(${(bounded + (position - bounded) * .35) * 100}%,0,0)`;
}

function stopNavigation() {
  cancelAnimationFrame(navigationFrame);
  navigationFrame = 0;
}

function settleNavigation(index: number, velocity: number) {
  const distance = navigationPosition - index;
  const started = performance.now();
  // A critically damped spring preserves release velocity and settles without oscillating.
  const stiffness = 18;
  const impulse = velocity + stiffness * distance;
  const tick = (now: number) => {
    const elapsed = (now - started) / 1000;
    const decay = Math.exp(-stiffness * elapsed);
    const offset = (distance + impulse * elapsed) * decay;
    const speed = (velocity - stiffness * impulse * elapsed) * decay;
    if (reducedMotion.matches || elapsed > .45 || (Math.abs(offset) < .002 && Math.abs(speed) < .04)) {
      paintNavigation(index);
      navigationFrame = 0;
      return;
    }
    paintNavigation(clamp(index + offset, -.055, views.length - 1 + .055));
    navigationFrame = requestAnimationFrame(tick);
  };
  navigationFrame = requestAnimationFrame(tick);
}

function showPage(index: number, animate = true, velocity = 0) {
  const previous = activeView();
  const current = views[index];
  if (!previous || !current) return;
  stopNavigation();
  dock.classList.remove("is-tracking");
  if (animate && !reducedMotion.matches && navigationPosition !== index) settleNavigation(index, velocity);
  else paintNavigation(index);
  if (current !== previous) {
    if (current.markup === undefined) renderView(current);
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
  showPage(destination.index);
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
  const {index, origin} = swipe;
  resetSwipe();
  if (origin) showPage(index);
}

function swipePosition(gesture: Swipe, x: number) {
  const origin = gesture.origin;
  if (!origin) return gesture.index;
  return origin.position + (x - origin.x) / gesture.width * (gesture.onDock ? 1 : -1);
}

function drawSwipe() {
  const gesture = swipe;
  if (!gesture) return;
  gesture.frame = 0;
  if (reducedMotion.matches) return;
  const position = swipePosition(gesture, gesture.x);
  const bounded = clamp(position, 0, views.length - 1);
  const over = position - bounded;
  paintNavigation(bounded + over * .11 / (1 + Math.abs(over) * 1.6));
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
    startX: event.clientX, startY: event.clientY, x: event.clientX, time: event.timeStamp, velocity: 0, frame: 0, origin: undefined};
}

function claimHorizontalTouch(event: TouchEvent) {
  if (event.touches.length === 1 && event.cancelable) event.preventDefault();
}

function moveSwipe(event: PointerEvent) {
  const gesture = swipe;
  if (!gesture || gesture.pointerId !== event.pointerId) return;
  if (!gesture.origin) {
    const dx = event.clientX - gesture.startX;
    const x = Math.abs(dx), y = Math.abs(event.clientY - gesture.startY);
    if (Math.max(x, y) < 9) return;
    if (y >= x || (y > 18 && x < 1.35 * y)) { resetSwipe(); return; }
    if (x < 1.35 * y) return;
    for (const view of views) if (view.markup === undefined) renderView(view);
    gesture.origin = {position: navigationPosition, x: gesture.startX + Math.sign(dx) * 7};
    suppressTap = true;
    stopNavigation();
    if (gesture.onDock) dock.classList.add("is-tracking");
    // WebViews can absorb the next tap after an uncancelled horizontal fling.
    app.addEventListener("touchmove", claimHorizontalTouch, {passive: false});
    try { app.setPointerCapture(event.pointerId); } catch {}
  }
  const elapsed = event.timeStamp - gesture.time;
  const instantaneous = (event.clientX - gesture.x) / Math.max(8, elapsed);
  gesture.velocity = elapsed > 100 ? 0 : gesture.velocity * .35 + instantaneous * .65;
  gesture.time = event.timeStamp;
  gesture.x = event.clientX;
  if (!gesture.frame) gesture.frame = requestAnimationFrame(drawSwipe);
}

function releaseSwipe(event: PointerEvent) {
  const gesture = swipe;
  if (!gesture || event.pointerId !== gesture.pointerId) return;
  if (!gesture.origin) { resetSwipe(); return; }
  const position = swipePosition(gesture, event.clientX);
  const travel = Math.abs(event.clientX - gesture.startX);
  const velocity = travel >= 18 && event.timeStamp - gesture.time < 90
    ? clamp(gesture.velocity * 1000 / gesture.width * (gesture.onDock ? 1 : -1), -5.5, 5.5) : 0;
  const projected = position + (gesture.onDock ? clamp(velocity * .07, -.22, .22) : velocity * .18);
  const target = Math.round(projected + (gesture.onDock ? 0 : Math.sign(projected - gesture.index) * .11));
  const index = gesture.onDock ? clamp(target, 0, views.length - 1)
    : clamp(target, Math.max(0, gesture.index - 1), Math.min(views.length - 1, gesture.index + 1));
  resetSwipe();
  showPage(index, true, velocity);
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

function reportStatus(task: Task) {
  if (task.report_state === "sent") return t("Готово", "Ready");
  if (task.report_state === "failed") return t("Нужен повтор", "Needs a retry");
  if (task.status === "done") return t("На паузе", "Paused");
  if (task.report_started_at) return t("Выполняется", "In progress");
  return t("Запланировано", "Scheduled");
}
function needsAttention(task: Task) {
  return task.report_state === "failed" ||
    (!task.report_state && task.status === "open" && !!task.due_at && Date.parse(task.due_at) < Date.now());
}
function taskDone(task: Task) {
  return task.report_state === "sent" || (!task.report_state && task.status === "done");
}
function taskFilter(task: Task): PlanFilter {
  if (taskDone(task)) return "done";
  if (needsAttention(task)) return "today";
  const when = task.due_at || task.remind_at;
  if (!when) return "inbox";
  return localDateTime(when,zone()).slice(0,10) > localDateTime(Date.now(),zone()).slice(0,10) ? "upcoming" : "today";
}
function taskDate(value: string) {
  const day = localDateTime(value,zone()).slice(0,10);
  const today = localDateTime(Date.now(),zone()).slice(0,10);
  return day === today ? formatDate("clock",value) : formatDate("day",value) + " · " + formatDate("clock",value);
}
function taskRow(task: Task, withProject = true) {
  const report = Boolean(task.report_state);
  const done = taskDone(task);
  const urgent = needsAttention(task);
  const status = report ? reportStatus(task) : urgent ? t("Просрочено","Overdue") :
    task.remind_at && !task.due_at ? t("Напоминание","Reminder") : "";
  const due = task.due_at ? `${report ? t("Запуск","Run") : t("Срок","Due")} ${taskDate(task.due_at)}` : "";
  const reminder = task.remind_at && !done ? t("Напомню в","Reminder at") + " " + taskDate(task.remind_at) : "";
  const toggle = report ? glyph("sparkle") :
    `<button class="task-toggle" data-action="toggle-task" data-id="${task.id}" aria-label="${e((done ? t("Вернуть в работу","Reopen") : t("Отметить готовой","Mark complete")) + " · " + task.title)}" aria-pressed="${done}">
      <span class="ring">${done ? icon("check") : ""}</span></button>`;
  return `<article class="task-row ${done ? "done" : ""}">
    ${toggle}
    <button class="task-body" data-action="edit-task" data-id="${task.id}">
      <span class="task-title" dir="auto">${e(task.title)}</span>
      <span class="task-meta">${withProject ? `<span>${e(projectName(task.project_id))}</span>` : ""}
        ${due ? `<span>${e(due)}</span>` : ""}${reminder ? `<span>${e(reminder)}</span>` : ""}</span>
      ${status ? `<span class="task-status ${urgent ? "task-status-alert" : ""}">${report ? t("Алеф","Aleph") + " · " : ""}${status}</span>` : ""}
    </button><span class="arrow">${icon("arrow")}</span></article>`;
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
  const tasks = sorted();
  const groups: Record<PlanFilter, Task[]> = {today: [], upcoming: [], inbox: [], done: []};
  for (const task of tasks) groups[taskFilter(task)].push(task);
  const list = groups[filter];
  const attention = filter === "today" ? list.filter(needsAttention) : [];
  const regular = list.filter(task => !attention.includes(task));
  const filters: [PlanFilter, string][] = [["today", t("Сегодня", "Today")], ["upcoming", t("Скоро", "Upcoming")],
    ["inbox", t("Без даты", "No date")], ["done", t("Готово", "Done")]];
  const empty = {today: t("На сегодня всё спокойно", "You’re clear for today"),
    upcoming: t("Впереди пока ничего", "Nothing scheduled yet"),
    inbox: t("Задач без даты нет", "No undated tasks"), done: t("Здесь будут выполненные задачи", "Completed tasks will appear here")};
  const first = tasks.length === 0 && filter === "today";
  const next = filter === "today" && !list.length ? groups.upcoming[0] : undefined;
  return `<div class="tasks-toolbar">
    <button class="active-context" data-action="view-project" data-id="${activeId()}">${icon(activeId() ? "projects" : "person")}
      <span>${t("В чате", "In chat")} · <strong dir="auto">${e(projectName(activeId()))}</strong></span>${icon("arrow")}</button>
    ${first ? "" : `<button class="action-plus" data-action="new-task" aria-label="${t("Добавить задачу","Add task")}">${icon("plus")}</button>`}
    </div>
    ${attention.length ? `<section class="attention-block"><h2 class="section-title">${t("Требует внимания","Needs attention")}</h2>
      <div class="panel">${attention.map(task => taskRow(task)).join("")}</div></section>` : ""}
    <div class="plan-tabs" role="group" aria-label="${t("Разделы задач", "Task views")}">${filters.map(([key,label]) => {
      const count = groups[key].length;
      return `<button data-filter="${key}" aria-pressed="${key === filter}">${label}${count ? `<small>${count}</small>` : ""}</button>`;
    }).join("")}</div>
    ${regular.length ? `<div class="agenda panel">${regular.map(task => taskRow(task)).join("")}</div>` :
      attention.length ? "" : `<div class="empty surface">
        <strong>${first ? t("С чего начнём?", "What shall we start with?") : empty[filter]}</strong>
        ${first ? `<p>${t("Попробуй: «Завтра в 18:00 пришли разбор SpaceX».", "Try: “Send me a SpaceX analysis tomorrow at 6 PM.”")}</p>
          <button class="button primary" data-action="chat">${t("Поручить в чате","Ask in chat")}</button>
          <button class="small-action" data-action="new-task">${t("Добавить задачу вручную","Add a task manually")}</button>`
          : next ? `<p>${t("Ближайшее","Up next")} · ${e(taskDate(next.due_at || next.remind_at || ""))}</p>
            <button class="small-action" data-action="edit-task" data-id="${next.id}">${e(next.title)} ${icon("arrow")}</button>` : ""}
      </div>`}
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
  const items = [{id:0,name:t("Личное","Personal"),context:""}, ...(state.projects || [])]
    .sort((a,b) => Number(b.id === activeId()) - Number(a.id === activeId()));
  return `<div class="plan-heading"><h2 class="section-title">${t("Рабочие пространства","Workspaces")}</h2><button class="action-plus" data-action="new-project" aria-label="${t("Новый проект","New project")}">${icon("plus")}</button></div>
    <div class="project-list">${items.map(item => {
      const count = state.tasks.filter(task => (task.project_id || 0) === item.id && task.status === "open").length;
      return `<button class="project-card surface ${item.id === activeId() ? "active-project-card" : ""}" data-action="view-project" data-id="${item.id}">${glyph(item.id ? "projects" : "person")}
        <span><strong dir="auto">${e(item.name)}</strong><small>${count ? count + " · " + t("в работе","open") : t("Пока без задач","No open tasks")}
        ${item.id === activeId() ? ' <i class="signal-dot"></i> ' + t("В чате","In chat") : ""}</small></span>${icon("arrow")}</button>`;
    }).join("")}</div>`;
}

function showProjectList() {
  projectDetail = null;
  render();
  activeView()?.panel.scrollTo({top: projectListScroll});
}

function projectPage(project?: Project) {
  const id = project?.id || 0;
  const entries = projectEntries.get(id);
  const tasks = sorted(state.tasks.filter(task => (task.project_id || 0) === id && !taskDone(task)));
  const decision = entries?.find(entry => entry.kind === "decision");
  const next = tasks.find(task => task.status === "open");
  return `<button class="small-action back-project" data-action="project-list">← ${t("Все проекты","All projects")}</button>
    <section class="project-hero surface">
      <div>${glyph(id ? "projects" : "person")}${id === activeId() ? `<span class="live-project"><i class="signal-dot"></i>${t("Активен в чате","Active in chat")}</span>` : ""}</div>
      <h2 dir="auto">${e(project?.name || projectName(0))}</h2>
      <button class="button primary" data-action="continue-project" data-id="${id}">${icon("plane")}${t("Продолжить в чате","Continue in chat")}</button>
    </section>
    ${decision || next ? `<section class="workspace-section">${sectionHead(t("С чего продолжить","Pick up where you left off"))}
      <div class="panel resume-panel">${decision ? `<p><small>${t("Последнее решение","Latest decision")}</small><strong dir="auto">${e(decision.text)}</strong></p>` : ""}
      ${next ? `<p><small>${t("Следующий шаг","Next step")}</small><button class="resume-task" data-action="edit-task" data-id="${next.id}">${e(next.title)} ${icon("arrow")}</button></p>` : ""}</div></section>` : ""}
    ${project?.context ? `<details class="project-context panel"><summary>${t("Описание и инструкции", "Description and instructions")}</summary>
      <p class="project-goal" dir="auto">${e(project.context)}</p></details>` : ""}
    <section class="workspace-section"><div class="plan-heading">${sectionHead(t("Задачи проекта","Project tasks"))}<button class="action-plus compact" data-action="new-task" data-id="${id}" aria-label="${t("Добавить поручение","Add task")}">${icon("plus")}</button></div>
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
      return `<div class="setting-row"><span class="setting-copy"><strong>${e(item.title)}</strong><span class="setting-detail">${e(days)} · ${e(timeLabel(item.time))}${!state.settings.proactive ? " · " + t("Уведомления на паузе", "Updates paused") : item.active ? "" : " · " + t("На паузе", "Paused")}</span></span></div>`;
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
  return [["auto", t("Как в Telegram", "Match Telegram")], ["white", t("Светлый", "Light")], ["dark", t("Тёмный", "Dark")]];
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
          : t("Утренний обзор выключен", "Morning brief off")) : t("Уведомления на паузе", "Updates paused") +
            (s.daily_digest !== false ? " · " + t("обзор в", "brief at") + " " + timeLabel(s.digest_time) : ""), "notifications") +
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
  viewedReportId = undefined;
  if (!sheet.open) {
    returnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    focusTarget = controlSelector(returnFocus);
  }
  clearTimeout(closeTimer);
  if (closeListener) sheet.removeEventListener("animationend",closeListener);
  sheet.classList.remove("closing");
  sheet.style.removeProperty("--sheet-release");
  sheet.innerHTML = `<div class="sheet-body"><div class="sheet-grab" aria-hidden="true"></div><div class="sheet-head"><h2 id="sheet-heading">${e(title)}</h2><button class="icon-button" aria-label="${t("Закрыть", "Close")}" data-action="close">${icon("close")}</button></div>${html}</div>`;
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
let viewedReportId: number | undefined;
function taskSheet(task?: Task, projectId = activeId()) {
  if (!task?.report_state) {
    taskEditor(task, projectId);
    return;
  }
  openSheet(task.title, `<div class="report-summary">
      <p class="task-status ${needsAttention(task) ? "task-status-alert" : ""}">${t("Алеф","Aleph")} · ${reportStatus(task)}</p>
      <p class="sheet-desc">${e(projectName(task.project_id))}${task.due_at ? " · " + e(taskDate(task.due_at)) + " · " + e(zone()) : ""}</p>
    </div>
    ${task.report_state === "sent" ? `<button class="button primary" data-action="open-result" data-id="${task.id}">
      ${t("Открыть результат","Open result")}</button>`
      : `<div class="submit-row"><button class="button primary" data-action="edit-report" data-id="${task.id}">
        ${task.report_state === "failed" ? t("Назначить новое время","Set a new time") : t("Изменить поручение","Edit task")}</button>
        ${task.report_state === "pending" ? `<button class="button" data-action="toggle-report" data-id="${task.id}">
          ${task.status === "done" ? t("Возобновить","Resume") : t("Приостановить","Pause")}</button>` : ""}</div>`}
    ${task.notes ? `<details class="task-details"><summary>${t("Исходное поручение","Original instructions")}</summary>
      <p class="sheet-desc" dir="auto">${e(task.notes)}</p></details>` : ""}`);
  viewedReportId = task.id;
}
async function resultSheet(task: Task) {
  if (!task.report_has_result || bridge()) {
    openSheet(task.title, `<p class="sheet-desc">${t("Этот результат есть в переписке с Алефом.","Find this result in your chat with Aleph.")}</p>
      <button class="button primary" data-action="chat">${t("Открыть чат","Open chat")}</button>`);
    return;
  }
  openSheet(task.title, `<div data-result aria-live="polite"><p class="sheet-desc">${t("Загружаю результат…","Loading result…")}</p></div>`);
  const node = sheet.querySelector("[data-result]");
  if (!node) return;
  try {
    const result = await request(`/api/tasks/${task.id}/result`);
    if (!node.isConnected || !sheet.open) return;
    node.innerHTML = result.html ? `<article class="task-result">${result.html}</article>` :
      `<p class="sheet-desc">${t("Результат в переписке с Алефом.","Find the result in your chat with Aleph.")}</p>
       <button class="button primary" data-action="chat">${t("Открыть чат","Open chat")}</button>`;
    node.querySelectorAll<HTMLAnchorElement>("a[href^='http']").forEach(link => {
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.dataset.externalLink = "";
    });
  } catch (error) {
    if (node.isConnected) node.innerHTML = `<p class="sheet-desc">${e(errorMessage(error))}</p>
      <button class="button" data-action="open-result" data-id="${task.id}">${t("Повторить","Try again")}</button>`;
  }
}
function taskEditor(task?: Task, projectId = activeId()) {
  const report = Boolean(task?.report_state);
  const retry = task?.report_state === "failed";
  const fields = [ {id:0,name:t("Личное","Personal")}, ...(state.projects || []) ];
  const selected = task?.project_id ?? projectId;
  const due = retry ? localDateTime(Date.now()+3600000, zone()) : localDateTime(task?.due_at ?? null,zone());
  openSheet(report ? t("Поручение Алефу","Task for Aleph") : task ? t("Задача","Task") : t("Новая задача","New task"),
    `<form id="task-form" data-id="${task?.id || ""}">
      <label class="field">${t("Что нужно сделать?","What needs to happen?")}<input name="title" value="${e(task?.title || "")}" required maxlength="300" placeholder="${t("Например, сверить макеты","For example, review the designs")}" dir="auto"></label>
      <label class="field">${t("Проект","Project")}<select name="project_id">${fields.map(project => `<option value="${project.id}" ${project.id === selected ? "selected" : ""}>${e(project.name)}</option>`).join("")}</select></label>
      <details class="task-details" ${report || task?.due_at || task?.remind_at || task?.notes ? "open" : ""}>
        <summary>${report ? t("Запуск и поручение","Schedule and instructions") : t("Срок, напоминание и заметки","Due date, reminder and notes")}</summary>
        <label class="field">${report ? t("Время запуска","Run at") : t("Срок","Due date")}<input name="due_at" type="datetime-local" ${report ? "required" : ""} value="${e(due)}"></label>
        ${report ? "" : `<label class="field">${t("Напомнить","Remind me")}<input name="remind_at" type="datetime-local" value="${e(localDateTime(task?.remind_at ?? null,zone()))}"></label>`}
        ${!(state.settings.timezone_confirmed ?? zone() !== "UTC") ? `<label class="field task-zone" data-task-zone ${due || task?.remind_at ? "" : "hidden"}>
          ${t("Подтверди часовой пояс","Confirm your time zone")}
          <select name="timezone" ${due || task?.remind_at ? "required" : ""}>
            <option value="">${t("Выбрать часовой пояс","Choose time zone")}</option>${timezoneOptions("")}
          </select></label>` : `<p class="task-zone sheet-desc">${t("Часовой пояс","Time zone")} · ${e(zone())}</p>`}
        <label class="field">${report ? t("Что подготовить","What to prepare") : t("Заметки","Notes")}<textarea name="notes" maxlength="4000" rows="3" dir="auto" ${report ? "required" : ""}>${e(task?.notes || "")}</textarea></label>
      </details>
      ${task?.source ? `<details class="task-details"><summary>${t("Исходное сообщение","Original message")}</summary><p class="sheet-desc" dir="auto">${e(task.source)}</p></details>` : ""}
      ${submit(retry ? t("Запланировать повтор","Schedule retry") : undefined)}</form>`);
}
function entrySheet() {
  openSheet(t("Запись проекта", "Project note"), `<form id="entry-form">
    <label class="field">${t("Тип","Type")}<select name="kind"><option value="decision">${t("Решение","Decision")}</option><option value="question">${t("Открытый вопрос","Open question")}</option><option value="material">${t("Материал","Material")}</option></select></label>
    <label class="field">${t("Текст или ссылка","Text or link")}<textarea name="text" maxlength="2000" required rows="4" dir="auto"></textarea></label>${submit()}</form>`);
}
function timezoneOptions(current: string) {
  const guessed = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const zones = [...new Set([...(current ? [current] : []), ...(guessed ? [guessed] : []), "UTC",
    ...(Intl.supportedValuesOf?.("timeZone") ?? ["Europe/Moscow", "Europe/London", "Europe/Berlin", "Asia/Yerevan", "America/New_York", "Asia/Tokyo"])])];
  return zones.map(z => `<option value="${e(z)}" ${z === current ? "selected" : ""}>${e(z.replaceAll("_", " "))}${z === guessed ? " · " + t("На устройстве", "Device") : ""}</option>`).join("");
}
function timezoneSheet() {
  openSheet(t("Часовой пояс", "Time zone"), `<form id="timezone-form">
    <p class="sheet-desc">${t("По этому времени Алеф присылает напоминания и выполняет запланированные задачи.", "Aleph uses this time zone for reminders and scheduled tasks.")}</p>
    <label class="field">${t("Часовой пояс", "Time zone")}<select name="timezone">${timezoneOptions(zone())}</select></label>${submit()}</form>`);
}
function notificationsSheet() {
  const s = state.settings;
  openSheet(t("Расписание уведомлений", "Notification schedule"), `<form id="notifications-form">
    <div class="panel">
      <label class="setting-row notification-option"><span class="setting-copy"><strong id="automatic-label">${t("Автоматические уведомления", "Automatic updates")}</strong>
        <span class="setting-detail" id="automatic-detail">${t("Утренний обзор и регулярные сообщения в чате.", "Morning brief and recurring messages in chat.")}</span></span>
        <input type="checkbox" name="proactive" aria-labelledby="automatic-label" aria-describedby="automatic-detail" ${s.proactive ? "checked" : ""}></label>
      <label class="setting-row notification-option"><span class="setting-copy"><strong id="digest-label">${t("Утренний обзор", "Morning brief")}</strong>
        <span class="setting-detail" id="digest-detail"></span></span>
        <input type="checkbox" name="daily_digest" aria-labelledby="digest-label" aria-describedby="digest-detail" ${s.daily_digest !== false ? "checked" : ""}></label>
    </div>
    <label class="field">${t("Время утреннего обзора", "Morning brief time")}<input type="time" required name="digest_time" value="${e(timeLabel(s.digest_time))}"></label>
    <fieldset class="setting-choices"><legend>${t("Тихие часы", "Quiet hours")}</legend><div class="quiet-hours">
      <label class="field">${t("С", "From")}<input type="time" required name="quiet_start" value="${e(timeLabel(s.quiet_start))}"></label>
      <label class="field">${t("До", "Until")}<input type="time" required name="quiet_end" value="${e(timeLabel(s.quiet_end))}"></label>
    </div></fieldset>
    <p class="sheet-desc">${t("Уведомления по расписанию учитывают тихие часы. Разовые напоминания приходят в указанное время.", "Automatic updates respect quiet hours. One-time reminders arrive at the time you set.")}</p>
    ${submit()}</form>`);
  syncNotificationForm(requiredElement<HTMLFormElement>(sheet, "#notifications-form"));
}
function syncNotificationForm(form: HTMLFormElement) {
  const automatic = requiredElement<HTMLInputElement>(form, '[name="proactive"]');
  const digest = requiredElement<HTMLInputElement>(form, '[name="daily_digest"]');
  const time = requiredElement<HTMLInputElement>(form, '[name="digest_time"]');
  digest.disabled = !automatic.checked;
  time.disabled = !automatic.checked || !digest.checked;
  requiredElement(form,"#digest-detail").textContent = !digest.checked
    ? t("Обзор выключен.","Morning brief is off.")
    : !automatic.checked
      ? t("На паузе · время сохранено:", "Paused · saved time:") + " " + timeLabel(time.value)
      : t("Открытые задачи и новости из выбранных источников.", "Open tasks and news from your chosen sources.");
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
    state = await workspaceState();
    setLanguage(
      state.user.language_code ||
        webapp()?.initDataUnsafe?.user?.language_code ||
        "ru",
    );
  }
  chatgpt = state.chatgpt_connection;
  state.feeds ??= [];
  state.settings.digest_time ??= "09:00";
  if (!initialTaskOpened) {
    initialTaskOpened = true;
    const task = initialView.get("task");
    const target = task && /^\d{1,18}$/.test(task) ? byId(task) : undefined;
    if (target) {
      page = "home";
      filter = taskFilter(target);
      setTheme();
      render();
      if (initialView.get("result") === "1" && target.report_state === "sent") void resultSheet(target);
      else taskSheet(target);
    } else if (task) message(t("Задача не найдена","Task not found"));
  }
  setTheme();
  render();
  if (state.model_configurable && !chatgpt && !bridge()) void syncChatGPT();
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
  const controls = sheet.querySelectorAll<HTMLInputElement | HTMLButtonElement | HTMLSelectElement | HTMLTextAreaElement>(
    ":is(button,input,select,textarea):enabled");
  controls.forEach(control => { control.disabled = true; });
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
    controls.forEach(control => { control.disabled = false; });
  }
}
function saveSettings(fields: Partial<Settings>, success = t("Настройки сохранены", "Settings saved")) {
  return write("/api/settings", "PATCH", fields, result => {
    Object.assign(state.settings, result ?? (fields.timezone ? {...fields, timezone_confirmed: true} : fields));
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
      showProjectList();
      break;
    case "view-project":
    case "reload-project":
      if (page === "projects" && projectDetail === null) projectListScroll = activeView()?.panel.scrollTop || 0;
      projectDetail = Number(id);
      if (page !== "projects") navigate("projects");
      render();
      activeView()?.panel.scrollTo({top: 0});
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
    case "edit-report": {
      const item = byId(id);
      if (item?.report_state && item.report_state !== "sent") taskEditor(item);
      break;
    }
    case "open-result": {
      const item = byId(id);
      if (item?.report_state === "sent") await resultSheet(item);
      break;
    }
    case "toggle-report": {
      const item = byId(id);
      if (item?.report_state !== "pending") break;
      const status = item.status === "done" ? "open" : "done";
      const notice = status === "done" ? t("Поручение на паузе", "Task paused") : t("Поручение возобновлено", "Task resumed");
      if (await write(`/api/tasks/${item.id}`,"PATCH",{status},task => Object.assign(item,task || {status}),notice) && !bridge()) taskSheet(item);
      break;
    }
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
      if (!item || item.report_state) return;
      const before = item.status;
      const status = before === "done" ? "open" : "done";
      const row = activeView()?.content.querySelector(`[data-action="toggle-task"][data-id="${item.id}"]`)?.closest(".task-row");
      const animation = row && !reducedMotion.matches
        ? row.animate([{opacity:1,transform:"translateX(0)"},{opacity:.12,transform:"translateX(8px)"}],
                      {duration:150,easing:"ease-out",fill:"both"})
        : undefined;
      const displayAfter = animation?.finished.catch(() => undefined);
      const notice = status === "done" ? t("Задача выполнена","Task complete") : t("Задача снова в работе","Task reopened");
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
    if (!link.closest(".task-result")) closeSheet();
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
  viewedReportId = undefined;
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
      const timeZone = field("timezone");
      const due = field("due_at"), reminder = field("remind_at");
      const confirmedZone = due || reminder ? timeZone : "";
      const id = form.dataset.id;
      const body = { title, due_at: zonedISO(due, confirmedZone || zone()), remind_at: zonedISO(reminder, confirmedZone || zone()),
        project_id: Number(field("project_id")), notes: field("notes"),
        ...(confirmedZone ? {timezone: confirmedZone} : {}),
        ...(byId(id)?.report_state === "failed" ? {status: "open" as const} : {}) };
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
          if (confirmedZone) Object.assign(state.settings, {timezone: confirmedZone, timezone_confirmed: true});
          if (page === "home") filter = taskFilter(record);
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
        daily_digest: requiredElement<HTMLInputElement>(form,'[name="daily_digest"]').checked,
        digest_time: requiredElement<HTMLInputElement>(form,'[name="digest_time"]').value,
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
  if (picker instanceof HTMLInputElement && picker.form?.id === "notifications-form") {
    syncNotificationForm(picker.form);
    return;
  }
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
sheet.addEventListener("input", event => {
  const input = event.target;
  if (!(input instanceof HTMLInputElement) || input.form?.id !== "task-form" || input.type !== "datetime-local") return;
  const zoneField = input.form.querySelector<HTMLElement>("[data-task-zone]");
  if (!zoneField) return;
  const needsZone = [...input.form.querySelectorAll<HTMLInputElement>('input[type="datetime-local"]')].some(item => item.value);
  zoneField.hidden = !needsZone;
  requiredElement<HTMLSelectElement>(zoneField,"select").required = needsZone;
});
let loading = false;
async function waitForTelegramAuth() {
  const sdk = document.getElementById("telegram-sdk");
  if (!sdk || webapp() || signed()) return;
  await new Promise<void>(resolve => {
    const finish = () => {
      clearTimeout(timer);
      sdk.removeEventListener("load", finish);
      sdk.removeEventListener("error", finish);
      resolve();
    };
    const timer = setTimeout(finish, 2500);
    sdk.addEventListener("load", finish, {once: true});
    sdk.addEventListener("error", finish, {once: true});
  });
}
async function start() {
  if (loading) return;
  loading = true;
  app.setAttribute("aria-busy", "true");
  try {
    initTelegram();
    if (!demo && !bridge() && location.hostname.endsWith(".github.io"))
      throw new Error("static-plan");
    if (!demo && !bridge() && !signed()) await waitForTelegramAuth();
    await load();
  } catch (error) {
    app.setAttribute("aria-busy", "false");
    sizeObserver?.disconnect();
    stage = undefined;
    views = [];
    app.innerHTML = `<main class="failure"><img src="${__ALEPH_MARK_URL__}" width="45" alt=""><h1>Aleph</h1><p>${e(errorMessage(error))}</p><div class="actions"><button class="button" data-action="retry">${t("Повторить", "Try again")}</button><button class="button primary" data-action="chat">${t("В чат", "Open chat")}</button></div></main>`;
  } finally {
    loading = false;
  }
}
async function refresh(reportsOnly = false) {
  const viewing = sheet.open && viewedReportId !== undefined ? byId(String(viewedReportId)) : undefined;
  if (
    document.hidden ||
    demo ||
    bridge() ||
    !state ||
    (sheet.open && (!viewing || viewing.report_state === "sent")) ||
    pending ||
    loading || refreshRunning || swipe?.origin
  )
    return;
  const sequence = revision;
  refreshRunning = true;
  try {
    const fresh = await workspaceState();
    if (!pending && (!sheet.open || (viewing && viewing.id === viewedReportId)) && !swipe?.origin && sequence === revision) {
      const previouslyConnected = state.connections?.calendar;
      state = fresh;
      if (!reportsOnly) chatgpt = fresh.chatgpt_connection;
      if (!reportsOnly) setTheme();
      render();
      const current = viewing && byId(String(viewing.id));
      if (sheet.open && current && viewing && reportStatus(current) !== reportStatus(viewing)) taskSheet(current);
      if (reportsOnly) return;
      if (page === "projects" && projectDetail) void loadProject(projectDetail);
      if (fresh.connections?.calendar === "connected" && (previouslyConnected !== "connected" || page === "home")) void loadAgenda();
      if (previouslyConnected === "connected" && fresh.connections?.calendar !== "connected") agenda = undefined;
      if (fresh.model_configurable && !chatgpt) void syncChatGPT();
    }
  } catch {} finally { refreshRunning = false; }
}
document.addEventListener("visibilitychange", () => void refresh());
window.addEventListener("focus", () => void refresh());
setInterval(() => {
  if (state?.tasks.some(task => task.status === "open" && task.report_state === "pending" &&
      !!task.due_at && Date.parse(task.due_at) <= Date.now())) void refresh(true);
}, 12000);
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
