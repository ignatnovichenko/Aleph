import { icon } from "./icons.js";
import { revealMiniApp } from "./telegram.js";
import { bindSheetMotion } from "./sheet-motion.js";
import { ApiMessage } from "./transport.js";
import type { IconName } from "./icons.js";
import type { RequestOptions, WriteMethod, Writes } from "./store.js";
import type { CalendarEvent, ChatGPTConnection, ChatGPTPairing, ProjectEntry, ReplyStyle, SavedMemory, Service, ModelCatalog, ModelChoice, ModelProvider, Project, ProviderModel, Settings, Task, ThemeMode, WorkspaceState } from "./types.js";
import type { ProjectSkills, ProjectSource, SkillDraft, SkillSummary, SourcePreview } from "./types.js";
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
type SelectableTheme = Exclude<ThemeMode, "auto">;

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
type TaskSection = "today" | "upcoming" | "inbox" | "done";
type PlanFilter = "all" | TaskSection;
let filter: PlanFilter = "all";
let taskScope: number | null = null;
let projectDetail: number | null = null;
let projectPane: "tasks" | "notes" | "sources" = "tasks";
let projectDoneOpen = false;
let projectListScroll = 0;
let undoTask: number | undefined;
const projectEntries = new Map<number, ProjectEntry[]>();
const loadingProjects = new Set<number>();
const projectLoadErrors = new Set<number>();
const projectSources = new Map<number, ProjectSource[]>();
const loadingSources = new Set<number>();
const sourceLoadErrors = new Set<number>();
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
let previewTheme: SelectableTheme | undefined;
function visibleTheme(): SelectableTheme {
  const theme = previewTheme ?? state?.settings.theme;
  return theme === "white" || theme === "dark" ? theme : prefersDark() ? "dark" : "white";
}

function setTheme() {
  const theme = visibleTheme();
  const root = document.documentElement;
  const tg = webapp();
  root.dataset.theme = theme === "dark" ? "dark" : "light";
  for (const card of sheet.querySelectorAll<HTMLButtonElement>('[data-action="choose-theme"]')) {
    card.setAttribute("aria-pressed", String(card.dataset.id === theme));
  }
  const bg = getComputedStyle(root).getPropertyValue("--bg").trim();
  const meta = document.querySelector<HTMLMetaElement>("meta[name=theme-color]");
  if (meta) meta.content = bg;
  try {
    tg?.setHeaderColor?.(bg);
    tg?.setBackgroundColor?.(bg);
    tg?.setBottomBarColor?.(bg);
  } catch {}
}
function applySystemTheme() {
  setTheme();
  render();
}
function initTelegram() {
  const tg = webapp();
  if (!tg || sdkBound) return;
  sdkBound = true;
  revealMiniApp();
  applySystemTheme();
  tg.onEvent?.("themeChanged", applySystemTheme);
  tg.BackButton?.onClick(() => {
    if (sheet.open) {
      if (!pending) closeSheet();
    } else if (page === "projects" && projectDetail !== null) showProjectList();
    else navigate("home");
  });
  syncBack();
}
let restoreTelegramSwipes: (() => void) | undefined;
function unlockSheetSwipes() {
  restoreTelegramSwipes?.();
  restoreTelegramSwipes = undefined;
}
function syncSheetSwipes() {
  if (!sheet.open) {
    unlockSheetSwipes();
    return;
  }
  const tg = webapp();
  if (restoreTelegramSwipes || !tg?.disableVerticalSwipes || !tg.enableVerticalSwipes ||
      tg.isVerticalSwipesEnabled === false || tg.isVersionAtLeast?.("7.7") === false) return;
  try {
    tg.disableVerticalSwipes();
    restoreTelegramSwipes = () => {
      try { tg.enableVerticalSwipes?.(); } catch {}
    };
  } catch {}
}
window.addEventListener("pagehide", unlockSheetSwipes);
window.addEventListener("pageshow", syncSheetSwipes);
function syncBack() {
  syncSheetSwipes();
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
  if (error instanceof ApiMessage) return error.message;
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
const sheetMotion = bindSheetMotion(sheet, reducedMotion, () => pending);
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
let focusTarget: string | null = null;
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
          <header class="page-head">
            ${key === "projects" ? `<div class="project-backbar">
              <button class="small-action back-project" data-action="project-list">${icon("arrow")}${t("Все проекты","All projects")}</button>
              <span class="live-project" hidden><i class="signal-dot"></i>${t("В чате","In chat")}</span>
            </div>` : ""}
            <img class="app-mark" src="${__ALEPH_MARK_URL__}" width="30" height="30" alt="Aleph" decoding="async">
            <h1 class="page-title" id="title-${key}" tabindex="-1" dir="auto">${label}</h1>
            ${key === "projects" ? `<button class="project-chat-action" data-action="continue-project">${icon("plane")}${t("В чат","Open chat")}</button>` : ""}
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
  if (view.key === "projects") {
    const id = projectDetail;
    const detail = !bridge() && id !== null && (id === 0 || !!selectedProject());
    const title = detail ? projectName(id) : t("Проекты", "Projects");
    view.panel.classList.toggle("project-detail-view", detail);
    if (view.heading.textContent !== title) {
      view.heading.textContent = title;
      if (page === view.key) document.title = title + " · Aleph";
    }
    requiredElement(view.panel, ".live-project").hidden = !detail || id !== activeId();
    requiredElement(view.panel, ".project-chat-action").dataset.id = detail ? String(id) : "";
  }
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
function taskFilter(task: Task, today = localDateTime(Date.now(), zone()).slice(0, 10)): TaskSection {
  if (taskDone(task)) return "done";
  if (needsAttention(task)) return "today";
  const when = task.due_at || task.remind_at;
  if (!when) return "inbox";
  return localDateTime(when,zone()).slice(0,10) > today ? "upcoming" : "today";
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
      <span class="task-meta">${withProject ? `<span class="task-project-name" dir="auto">${e(projectName(task.project_id))}</span>` : ""}
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
function taskLabels(): Record<PlanFilter, string> {
  return {all: t("Все", "All"), today: t("Сегодня", "Today"), upcoming: t("Скоро", "Upcoming"),
    inbox: t("Без даты", "No date"), done: t("Готово", "Done")};
}

function taskScopeName(scope: number | null) {
  return scope === null ? t("Все проекты", "All projects") : projectName(scope);
}

function taskScopeIcon(scope: number | null): IconName {
  return scope === null ? "taskList" : scope === 0 ? "person" : "projects";
}

function taskScopeSheet() {
  const counts = new Map<number, number>();
  let total = 0;
  for (const task of state.tasks) {
    if (taskDone(task)) continue;
    const id = task.project_id || 0;
    counts.set(id, (counts.get(id) || 0) + 1);
    total++;
  }
  const choice = (scope: number | null) => {
    const selected = taskScope === scope;
    const count = scope === null ? total : counts.get(scope) || 0;
    return `<button class="task-scope-option setting-row" data-action="select-task-scope" data-id="${scope ?? "all"}" aria-pressed="${selected}">
      ${glyph(taskScopeIcon(scope))}<strong dir="auto">${e(taskScopeName(scope))}</strong>
      <span class="task-scope-option-count" aria-label="${t("В работе", "Open")} ${count}">${count || ""}</span>
      <span class="task-scope-check" aria-hidden="true">${selected ? icon("check") : ""}</span>
      </button>`;
  };
  openSheet(t("Задачи по проектам", "Tasks by project"), `
    <div class="task-scope-choices" role="group" aria-label="${t("Проект для списка задач", "Project for the task list")}">
      <div class="panel task-scope-all">${choice(null)}</div>
      <div class="panel">${[0, ...(state.projects || []).map(project => project.id)].map(choice).join("")}</div>
    </div>`);
}

function selectTaskScope(value?: string) {
  const next = value === "all" ? null : Number(value);
  if (next !== null && next !== 0 && !state.projects?.some(project => project.id === next)) return;
  if (next === taskScope) {
    closeSheet();
    return;
  }
  taskScope = next;
  selectionFeedback();
  render();
  closeSheet();
  if (reducedMotion.matches) return;
  const view = activeView();
  view?.content.querySelector(".task-scope-copy")?.animate(
    [{opacity: .3, transform: "translateY(5px)"}, {opacity: 1, transform: "translateY(0)"}],
    {delay: 45, duration: 290, fill: "backwards", easing: "cubic-bezier(.16,1,.3,1)"});
  view?.content.querySelector(".task-results")?.animate(
    [{opacity: .25, transform: "translateY(10px)"}, {opacity: 1, transform: "translateY(0)"}],
    {delay: 70, duration: 345, fill: "backwards", easing: "cubic-bezier(.16,1,.3,1)"});
}

function taskSection(title: string, items: Task[], withProject: boolean, attention = false) {
  if (!items.length) return "";
  return `<section class="task-section${attention ? " attention-block" : ""}">
    <h2 class="section-title">${title}<small>${items.length}</small></h2>
    <div class="agenda panel">${items.map(task => taskRow(task, withProject)).join("")}</div>
  </section>`;
}

function groupedTasks(tasks: Task[]) {
  const groups: Record<TaskSection, Task[]> = {today: [], upcoming: [], inbox: [], done: []};
  const attention: Task[] = [];
  const today = localDateTime(Date.now(), zone()).slice(0, 10);
  for (const task of tasks) {
    const group = taskFilter(task, today);
    if (group === "today" && needsAttention(task)) attention.push(task);
    else groups[group].push(task);
  }
  return {groups, attention};
}

function taskEmpty(first: boolean, next?: Task) {
  const title = {
    all: t("Задач пока нет", "No tasks yet"), today: t("На сегодня всё спокойно", "You’re clear for today"),
    upcoming: t("Впереди пока ничего", "Nothing scheduled yet"), inbox: t("Задач без даты нет", "No undated tasks"),
    done: t("Здесь будут выполненные задачи", "Completed tasks will appear here"),
  }[filter];
  return `<div class="empty surface">
    <strong>${first ? t("С чего начнём?", "What shall we start with?") : title}</strong>
    ${first ? `<p>${t("Попробуй: «Завтра в 18:00 пришли разбор SpaceX».", "Try: “Send me a SpaceX analysis tomorrow at 6 PM.”")}</p>
      <button class="button primary" data-action="chat">${t("Поручить в чате","Ask in chat")}</button>
      <button class="small-action" data-action="new-task">${t("Добавить задачу вручную","Add a task manually")}</button>`
      : next ? `<p>${t("Ближайшее","Up next")} · ${e(taskDate(next.due_at || next.remind_at || ""))}</p>
        <button class="small-action" data-action="edit-task" data-id="${next.id}">${e(next.title)} ${icon("arrow")}</button>` : ""}
  </div>`;
}

function overview() {
  const allProjects = taskScope === null;
  const tasks = sorted(allProjects ? state.tasks : state.tasks.filter(task => (task.project_id || 0) === taskScope));
  const {groups, attention} = groupedTasks(tasks);
  const labels = taskLabels();
  const first = allProjects && !state.tasks.length && (filter === "all" || filter === "today");
  const open = tasks.length - groups.done.length;
  const scope = taskScopeName(taskScope);
  const attentionSection = filter === "all" || filter === "today"
    ? taskSection(t("Требует внимания", "Needs attention"), attention, allProjects, true) : "";
  const results = filter === "all"
    ? (Object.keys(groups) as TaskSection[]).map(key => taskSection(labels[key], groups[key], allProjects)).join("")
    : groups[filter].length ? `<div class="agenda panel">${groups[filter].map(task => taskRow(task, allProjects)).join("")}</div>` : "";
  return `<div class="tasks-toolbar">
    <button class="task-scope" data-action="task-scope" aria-haspopup="dialog" aria-controls="sheet"
      aria-label="${e(t("Задачи по проектам", "Tasks by project") + " · " + scope)}">
      ${glyph(taskScopeIcon(taskScope))}
      <span class="task-scope-copy"><strong dir="auto">${e(scope)}</strong>
        <small>${open ? t("В работе", "Open") + " · " + open : t("Задач в работе нет", "No open tasks")}</small></span>
      <span class="task-scope-arrow">${icon("arrow")}</span>
    </button>
    ${first ? "" : `<button class="action-plus" data-action="new-task"${taskScope === null ? "" : ` data-id="${taskScope}"`}
      aria-label="${t("Добавить задачу","Add task")}">${icon("plus")}</button>`}
    </div>
    <button class="active-context" data-action="view-project" data-id="${activeId()}">${icon(activeId() ? "projects" : "person")}
      <span>${t("В чате", "In chat")} · <strong dir="auto">${e(projectName(activeId()))}</strong></span>${icon("arrow")}</button>
    <div class="plan-tabs" role="group" aria-label="${t("Разделы задач", "Task views")}">${Object.entries(labels).map(([key,label]) =>
      `<button data-filter="${key}" aria-pressed="${key === filter}">${label}</button>`).join("")}</div>
    <div class="task-results" role="region" aria-label="${e(scope)}">
      ${attentionSection + results || taskEmpty(first, filter === "today" ? groups.upcoming[0] : undefined)}
      ${allProjects && (filter === "all" || filter === "today") ? calendarSection() + routinesPanel() : ""}
    </div>`;
}
function calendarSection() {
  if (state.connections?.calendar !== "connected") return "";
  return `<section class="workspace-section">${sectionHead("Google Calendar")}
    ${agenda ? (agenda.length ? `<div class="panel">${agenda.map(item =>
      `<div class="calendar-event"><strong>${e(item.title)}</strong><small>${item.start.dateTime ? e(formatDate("clock",item.start.dateTime)) : t("Весь день","All day")}${item.location ? " · " + e(item.location) : ""}</small></div>`).join("")}</div>`
      : `<p class="muted">${t("Событий на сегодня нет","No events today")}</p>`)
      : `<button class="small-action" data-action="load-agenda">${agendaError ? t("Повторить загрузку","Try again") : t("Показать события","Show events")}</button>`}</section>`;
}

function projectAdd(action: string, label: string, id?: number) {
  return `<button class="project-add" data-action="${action}"${id === undefined ? "" : ` data-id="${id}"`}>${icon("plus")}${label}</button>`;
}

function projectsPage() {
  if (bridge()) return `<div class="empty surface"><strong>${t("Проекты в чате","Projects in chat")}</strong><button class="button" data-action="chat">${t("В чат","Open chat")}</button></div>`;
  const project = selectedProject();
  if (projectDetail !== null && (!projectDetail || project)) return projectPage(project);

  const active = activeId();
  const items = [{id:0,name:t("Личное","Personal")}, ...(state.projects || [])]
    .sort((a,b) => Number(b.id === active) - Number(a.id === active));
  const counts = new Map<number, number>();
  for (const task of state.tasks) {
    if (taskDone(task)) continue;
    const id = task.project_id || 0;
    counts.set(id, (counts.get(id) || 0) + 1);
  }

  return `<div class="project-index-action">${projectAdd("new-project", t("Новый проект","New project"))}</div>
    <div class="panel project-list">${items.map(item => {
    const count = counts.get(item.id) || 0;
    return `<button class="project-row" data-action="view-project" data-id="${item.id}">
      ${glyph(item.id ? "projects" : "person")}
      <span class="setting-copy"><strong dir="auto">${e(item.name)}</strong>
        ${count ? `<small>${t("В работе","Open tasks")} · ${count}</small>` : ""}</span>
      ${item.id === active ? `<span class="live-project"><i class="signal-dot"></i>${t("В чате","In chat")}</span>` : ""}
      <span class="tail">${icon("arrow")}</span></button>`;
  }).join("")}</div>`;
}

function showProjectList() {
  const id = projectDetail;
  projectDetail = null;
  render();
  const view = activeView();
  view?.panel.scrollTo({top: projectListScroll});
  const row = view?.content.querySelector<HTMLElement>(`[data-action="view-project"][data-id="${id}"]`);
  (row || view?.heading)?.focus({preventScroll: true});
}

function projectPage(project?: Project) {
  const id = project?.id || 0;
  const entries = projectEntries.get(id);
  const tasks = sorted(state.tasks.filter(task => (task.project_id || 0) === id));
  const done = tasks.filter(taskDone);
  const open = tasks.filter(task => !taskDone(task));
  const skillRow = `<div class="panel project-skill-link">${settingRow("skills", t("Скиллы", "Skills"),
    t("Выбрать для этого проекта", "Choose for this workspace"), "project-skills", String(id))}</div>`;
  if (!project) return skillRow + projectTasks(id, open, done);

  const hasContext = !!project.context.trim();
  return `<div class="panel project-configuration">
      <button class="project-context setting-row" data-action="project-context" data-id="${id}">
      ${glyph("document")}<span class="setting-copy">
        <strong>${t("Контекст и инструкции","Context and instructions")}</strong>
        <span class="setting-detail" dir="auto">${hasContext ? e(project.context) : t("Добавить контекст","Add context")}</span>
      </span><span class="tail">${icon("arrow")}</span>
      </button>
      ${settingRow("skills", t("Скиллы", "Skills"), undefined, "project-skills", String(id))}
    </div>
    <div class="project-panes" role="group" aria-label="${t("Разделы проекта","Project sections")}">
      ${([["tasks", t("Задачи","Tasks"), open.length], ["notes", t("Записи","Notes"), entries?.length],
          ["sources", t("Источники","Sources"), projectSources.get(id)?.length]] as const)
        .map(([key,label,count]) => `<button aria-pressed="${projectPane === key}" aria-controls="project-work"
          data-action="project-pane" data-id="${key}">${label}${count ? `<small>${count}</small>` : ""}</button>`).join("")}
    </div>
    <div id="project-work">${projectPane === "tasks" ? projectTasks(id, open, done, entries?.find(entry => entry.kind === "decision"))
      : projectPane === "notes" ? projectNotes(id, entries) : sourcesPane(id)}</div>`;
}

function projectTasks(id: number, open: Task[], done: Task[], decision?: ProjectEntry) {
  return `<section aria-labelledby="project-tasks-title">
    <div class="project-section-head"><h2 id="project-tasks-title">${t("В работе","In progress")}</h2>
      ${projectAdd("new-task", t("Добавить задачу","Add task"), id)}</div>
    ${open.length ? `<div class="panel">${open.map(task => taskRow(task,false)).join("")}</div>` :
      `<div class="project-empty panel">${glyph("home")}<div><strong>${t("Пока задач нет","No open tasks")}</strong>
        <p>${t("Добавь задачу здесь или поручи её Алефу в чате.","Add a task here or ask Aleph in chat.")}</p></div></div>`}
    ${done.length ? `<details class="project-done"${projectDoneOpen ? " open" : ""}>
      <summary>${t("Завершённые","Completed")}<small>${done.length}</small>${icon("arrow")}</summary>
      <div class="panel">${done.map(task => taskRow(task,false)).join("")}</div></details>` : ""}
    ${decision ? `<button class="setting-row project-resume" data-action="project-entry" data-id="${decision.id}">
      ${glyph("bookmark")}<span class="setting-copy"><strong>${t("Последнее решение","Latest decision")}</strong>
      <span class="setting-detail" dir="auto">${e(decision.text)}</span></span><span class="tail">${icon("arrow")}</span>
    </button>` : ""}
  </section>`;
}

function entryKindLabel(kind: ProjectEntry["kind"]) {
  return {
    decision: t("Решение", "Decision"),
    question: t("Открытый вопрос", "Open question"),
    material: t("Материал", "Material"),
  }[kind];
}

function projectNotes(id: number, entries?: ProjectEntry[]) {
  const groups = [
    ["decision", t("Решения","Decisions")],
    ["question", t("Открытые вопросы","Open questions")],
    ["material", t("Материалы","Materials")],
  ] as const;

  return `<section aria-labelledby="project-notes-title">
    <div class="project-section-head"><h2 id="project-notes-title">${t("Заметки и ссылки","Notes and links")}</h2>
      ${projectAdd("new-entry", t("Добавить запись","Add note"))}</div>
    ${entries?.length ? groups.map(([kind,label]) => {
      const notes = entries.filter(entry => entry.kind === kind);
      return notes.length ? `<section class="project-note-group"><h3>${label}</h3><div class="panel">${notes.map(entry =>
        `<button class="setting-row project-note-row" data-action="project-entry" data-id="${entry.id}">
          <span class="project-note-text" dir="auto">${e(entry.text)}</span><span class="tail">${icon("arrow")}</span>
        </button>`).join("")}</div></section>` : "";
    }).join("") : entries ? `<div class="project-empty panel">${glyph("bookmark")}<div>
      <strong>${t("Пока записей нет","No notes yet")}</strong><p>${t("Сохрани решение, открытый вопрос или ссылку на материал.","Save a decision, an open question or a link.")}</p></div></div>`
      : projectLoadErrors.has(id) ? `<p class="muted" role="status">${t("Не удалось загрузить записи.","Couldn’t load notes.")}</p>
        <button class="small-action" data-action="reload-project" data-id="${id}">${t("Повторить","Try again")}</button>`
        : `<p class="muted" role="status">${t("Загрузка…","Loading…")}</p>`}
  </section>`;
}

const sourceIcons: Record<ProjectSource["kind"], IconName> = {
  web: "link", youtube: "video", notion: "document", drive: "table", file: "document",
};
const importOptions = {showApiMessage: true, timeoutMs: 35000};
const sourceFormats = ".pdf,.docx,.xlsx,.pptx,.csv,.tsv,.txt,.md,.json,.html,.htm,.srt,.vtt";

function sourceLabel(kind: ProjectSource["kind"]) {
  return {
    web: t("Веб-страница","Web page"), youtube: "YouTube", notion: "Notion",
    drive: "Google Drive", file: t("Файл","File"),
  }[kind];
}

function sourceStatus(status: ProjectSource["status"]) {
  return {
    ready: t("Доступен Алефу","Available to Aleph"),
    partial: t("Доступен частично","Partially available"),
    link: t("Ссылка сохранена","Link saved"),
  }[status];
}

function sourceReason(reason: string) {
  const messages: Record<string, string> = {
    captions: t("Расшифровка видео недоступна. Можно прикрепить TXT, SRT или VTT к этой ссылке.",
      "The video’s transcript is unavailable. You can attach a TXT, SRT or VTT file to this link."),
    notion_access: t("Чтобы прочитать закрытую страницу, подключи Notion и дай Алефу доступ. Затем обнови источник.",
      "To read a private page, connect Notion and grant Aleph access, then refresh the source."),
    drive_access: t("Дай доступ к файлу через Google Drive и нажми «Обновить». Можно также загрузить его экспорт.",
      "Grant access through Google Drive, then choose Refresh. You can also upload an exported copy."),
    access: t("Сайт ограничил доступ к тексту. Можно загрузить файл или попробовать обновить ссылку.",
      "The site restricted access to the text. Upload a copy or try refreshing the link."),
    truncated: t("Сохранена часть содержимого. Для работы с остальными данными загрузи нужный фрагмент отдельно.",
      "Part of the content was saved. Upload another excerpt to work with the remaining data."),
    format: t("Содержимое в этом формате недоступно. Можно загрузить текстовый экспорт.",
      "This format is unavailable for reading. You can upload a text export."),
    too_large: t("Источник слишком большой. Можно загрузить нужный фрагмент отдельным файлом.",
      "This source is too large. You can upload an excerpt as a separate file."),
    unavailable: t("Источник пока недоступен. Можешь обновить его позже.", "The source is unavailable for now. Try refreshing it later."),
    no_text: t("На странице не найден читаемый текст. Можно загрузить файл с содержимым.",
      "No readable text was found. You can upload a file containing the text."),
  };
  return messages[reason] || "";
}

function sourcesPane(projectId: number) {
  const items = projectSources.get(projectId);
  return `<section aria-labelledby="sources-heading">
    <div class="project-section-head"><h2 id="sources-heading">${t("Источники","Sources")}</h2>
      ${projectAdd("add-source", t("Добавить источник","Add source"), projectId)}</div>
    ${items?.length ? `<div class="panel source-list">${items.map(item => `
      <button class="setting-row source-row" data-action="view-source" data-id="${item.id}">
        ${glyph(sourceIcons[item.kind])}<span class="setting-copy">
          <strong dir="auto">${e(item.title)}</strong>
          <span class="setting-detail">${sourceLabel(item.kind)} <span class="source-dot">·</span> ${sourceStatus(item.status)}</span>
        </span><span class="tail">${icon("arrow")}</span>
      </button>`).join("")}</div>`
      : items ? `<div class="project-empty panel">${glyph("link")}<div>
        <strong>${t("Дай Алефу материалы для работы","Give Aleph reference material")}</strong>
        <p>${t("Добавь файл или ссылку на статью, YouTube, Notion или Google Drive.","Add a file or link to an article, YouTube, Notion or Google Drive.")}</p>
      </div></div>`
      : sourceLoadErrors.has(projectId) ? `<button class="small-action" data-action="load-sources" data-id="${projectId}">${t("Повторить загрузку","Try again")}</button>`
      : `<p class="muted" role="status">${t("Загружаю источники…","Loading sources…")}</p>`}
  </section>`;
}

async function loadSources(projectId: number) {
  if (loadingSources.has(projectId)) return;
  if (demo) { projectSources.set(projectId, []); render(); return; }
  loadingSources.add(projectId);
  sourceLoadErrors.delete(projectId);
  const previous = projectSources.get(projectId);
  try {
    const items = await request(`/api/projects/${projectId}/sources`);
    if (projectSources.get(projectId) === previous) projectSources.set(projectId, items);
  } catch { sourceLoadErrors.add(projectId); }
  finally { loadingSources.delete(projectId); }
  if (projectDetail === projectId && projectPane === "sources") render();
}

function saveSource(projectId: number, item: ProjectSource | null) {
  if (!item) return;
  projectSources.set(projectId, [item, ...(projectSources.get(projectId) || []).filter(source => source.id !== item.id)]
    .sort((a,b) => b.id - a.id));
}

function sourceFileField(projectId: number, transcript?: number) {
  return `<label class="button file-label source-file-action">${icon("download")}
    ${transcript === undefined ? t("Загрузить файл","Upload a file") : t("Прикрепить расшифровку","Attach a transcript")}
    <input type="file" name="project_source_file" accept="${transcript === undefined ? sourceFormats : ".srt,.vtt,.txt"}"
      data-project="${projectId}"${transcript === undefined ? "" : ` data-source="${transcript}"`}></label>`;
}

function newSourceSheet(projectId: number) {
  if (demo) {
    openSheet(t("Источники проекта","Project sources"), `<p class="sheet-desc">${t("Чтобы добавить источник, открой Алефа в Telegram.","Open Aleph in Telegram to add a source.")}</p>
      <button class="button primary" data-action="chat">${t("Открыть Алефа","Open Aleph")}</button>`);
    return;
  }
  openSheet(t("Добавить источник","Add a source"), `
    <form id="source-link-form" data-project="${projectId}">
      <p class="sheet-desc source-intro">${t("Подойдут веб-страницы, YouTube, Notion и файлы из Google Drive.",
        "Paste a link to a web page, YouTube video, Notion page or Google Drive file.")}</p>
      <label class="field">${t("Ссылка","Link")}<input name="url" required type="url" maxlength="3000" inputmode="url"
        spellcheck="false" autocomplete="off" autocapitalize="off" placeholder="https://…"></label>
      ${submit(t("Добавить ссылку","Add link"))}
    </form>
    <div class="source-upload">
      ${sourceFileField(projectId)}<p class="sheet-desc">${t("PDF, документы, таблицы, презентации и текст до 4 МБ.",
        "PDFs, documents, spreadsheets, presentations and text files up to 4 MB.")}</p>
    </div><p class="muted source-progress" data-source-progress role="status"></p>`);
}

function sourceAccess(item: ProjectSource) {
  const drive = state.connections?.drive, notion = state.connections?.notion;
  if (item.reason === "drive_access") {
    if (drive === "connected") return `<button class="button" data-action="drive-picker">${t("Выбрать файл в Google Drive","Choose in Google Drive")}</button>`;
    if (drive === "disconnected" || drive === "reconnect") return `<button class="button" data-action="connect-service" data-id="drive">Google Drive</button>`;
  }
  if (item.reason === "notion_access" && (notion === "disconnected" || notion === "reconnect")) {
    return `<button class="button" data-action="connect-service" data-id="notion">${t("Подключить Notion","Connect Notion")}</button>`;
  }
  return "";
}

function showSource(projectId: number, item: SourcePreview) {
  openSheet(item.title, `
    <div class="source-detail">
      <div class="source-meta">${glyph(sourceIcons[item.kind])}<span>${sourceLabel(item.kind)} · ${sourceStatus(item.status)}</span></div>
      ${item.reason ? `<p class="sheet-desc">${sourceReason(item.reason)}</p>` : ""}
      <div class="source-controls">
        ${item.url ? `<a class="button" href="${e(item.url)}" data-external-link target="_blank" rel="noopener noreferrer">${icon("up")}${t("Открыть","Open")}</a>
          <button class="button" data-action="refresh-source" data-id="${item.id}">${t("Обновить","Refresh")}</button>` : ""}
        ${sourceAccess(item)}
        ${item.kind === "youtube" ? sourceFileField(projectId, item.id) : ""}
      </div>
      ${item.content ? `<details class="task-details source-preview">
        <summary>${t("Просмотреть сохранённый текст","Preview saved text")}</summary>
        <div class="project-reading" dir="auto">${e(item.content)}${item.size > item.content.length ? "…" : ""}</div>
      </details>` : ""}
      ${item.kind === "web" && item.status === "link" ? `<div class="source-reader">
        <p class="sheet-desc">${t("Для чтения публичной статьи можно передать её ссылку сервису Jina Reader.",
          "To read a public article, you can share its link with Jina Reader.")}</p>
        <button class="button" data-action="read-source-article" data-id="${item.id}">${t("Извлечь текст статьи","Extract article text")}</button>
      </div>` : ""}
      <p class="muted source-progress" data-source-progress role="status"></p>
      <p class="form-error" role="alert"></p>
      <button class="small-action project-entry-remove" data-action="remove-source" data-id="${item.id}">
        ${icon("trash")}${t("Удалить источник","Remove source")}</button>
    </div>`);
}

async function viewSource(projectId: number, itemId: number) {
  openSheet(t("Источник","Source"), `<p class="sheet-desc" data-source-loading role="status">${t("Загружаю источник…","Loading source…")}</p>`);
  const marker = requiredElement(sheet, "[data-source-loading]");
  try {
    const item = await request(`/api/projects/${projectId}/sources/${itemId}`);
    if (marker.isConnected && sheet.open && !sheet.classList.contains("closing")) showSource(projectId, item);
  } catch (error) {
    if (marker.isConnected) marker.textContent = errorMessage(error);
  }
}

function sourceProgress(value: string) {
  const node = sheet.querySelector("[data-source-progress]");
  if (node) node.textContent = value;
}

async function uploadSource(picker: HTMLInputElement) {
  const file = picker.files?.[0];
  if (!file || pending) return;
  const error = requiredElement(sheet, ".form-error");
  if (!file.size || file.size > 4 * 1024 * 1024) {
    error.textContent = t("Выбери файл до 4 МБ.","Choose a file up to 4 MB.");
    picker.value = "";
    return;
  }
  const id = Number(picker.dataset.project);
  const source = Number(picker.dataset.source);
  const path = source ? `/api/projects/${id}/sources/${source}/transcript` as const : `/api/projects/${id}/sources/file` as const;
  let itemId = 0;
  error.textContent = "";
  sourceProgress(t("Извлекаю текст и данные…","Extracting text and data…"));
  await write(path, "POST", file, item => {
    saveSource(id, item);
    itemId = item?.id || 0;
  }, "", {...importOptions, showApiMessage: ru});
  sourceProgress("");
  picker.value = "";
  if (itemId) await viewSource(id, itemId);
}

async function loadProject(id: number) {
  if (!id || loadingProjects.has(id)) return;
  if (demo) { if (!projectEntries.has(id)) projectEntries.set(id, []); render(); return; }
  loadingProjects.add(id);
  if (projectLoadErrors.delete(id) && projectDetail === id) render();
  const previous = projectEntries.get(id);
  let changed = false;
  try {
    const entries = await request(`/api/projects/${id}/entries`);
    changed = projectEntries.get(id) !== previous;
    if (!changed) projectEntries.set(id, entries);
  } catch { projectLoadErrors.add(id); }
  finally { loadingProjects.delete(id); }
  if (changed) { void loadProject(id); return; }
  if (projectDetail === id) render();
}

function projectContextSheet(project: Project) {
  if (!project.context.trim()) {
    projectSheet(project);
    return;
  }
  openSheet(t("Контекст и инструкции", "Context and instructions"), `
    <div class="project-reading" dir="auto">${e(project.context)}</div>
    <div class="submit-row"><button class="button" data-action="edit-project" data-id="${project.id}">${icon("edit")}${t("Изменить","Edit")}</button></div>`);
}

function projectEntrySheet(entry: ProjectEntry) {
  const link = entry.kind === "material" && /^https:\/\/\S+$/.test(entry.text);
  openSheet(entryKindLabel(entry.kind), `
    <div class="project-entry-body">
      <div class="project-reading" dir="auto">${link
        ? `<a href="${e(entry.text)}" target="_blank" rel="noopener noreferrer" data-external-link>${e(entry.text)}</a>` : e(entry.text)}</div>
      ${entry.source ? `<details class="task-details"><summary>${t("Из сообщения","From the message")}</summary>
        <p class="project-reading" dir="auto">${e(entry.source)}</p></details>` : ""}
    </div>
    <p class="form-error" role="alert"></p>
    <button class="small-action project-entry-remove" data-action="remove-entry" data-id="${entry.id}">${icon("trash")}${t("Удалить запись","Remove note")}</button>`);
}

function projectSheet(item?: Project) {
  openSheet(
    item ? t("Редактировать проект", "Edit project") : t("Новый проект", "New project"),
    `<form id="project-form" data-id="${e(item?.id || "")}">
      <label class="field">${t("Название", "Name")}<input name="name" maxlength="64" required value="${e(item?.name || "")}" dir="auto" placeholder="${t("Например, Пилот", "For example, Pilot")}"></label>
      <label class="field">${t("Контекст и инструкции", "Context and instructions")}<textarea name="context" maxlength="10000" rows="6" dir="auto" placeholder="${t("Цель проекта, важные детали и правила для Алефа", "Goal, important details and instructions for Aleph")}">${e(item?.context || "")}</textarea></label>
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
function themeName(theme: SelectableTheme) {
  return theme === "dark" ? t("Тёмная", "Dark") : t("Светлая", "Light");
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
  const connections = serviceRows() + chatgptRow();
  return `<div class="settings-grid">
    ${settingsGroup(t("Ассистент", "Assistant"), modelRow() +
      settingRow("edit", t("Персонализация ответов", "Personalize replies"), responseLabel, "reply-preferences") +
      (bridge() ? "" : settingRow("skills", t("Скиллы", "Skills"),
        t("Библиотека, создание и импорт", "Library, creation and import"), "skill-library")) +
      settingRow("bookmark", t("Личная память", "Personal memory"), enabledLabel(s.personal_memory !== false), "memory"))}
    ${settingsGroup(t("Приложение", "App"), settingRow("sun", t("Оформление", "Appearance"), themeName(visibleTheme()), "appearance") +
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
function skillScope(id?: string) {
  return id === undefined ? undefined : Number(id);
}

function skillBack(scope?: number) {
  return `<button class="small-action back-project" data-action="skill-list"${scope === undefined ? "" : ` data-id="${scope}"`}>${icon("arrow")}${scope === undefined ? t("Библиотека","Library") : t("Скиллы проекта","Project skills")}</button>`;
}

function skillOption(item: SkillSummary, selected?: number[]) {
  const copy = `<span class="setting-copy"><strong dir="auto">${e(item.name)}</strong>
    <span class="setting-detail skill-description" dir="auto">${e(item.description)}</span></span>`;
  return selected
    ? `<label class="setting-row skill-option">${copy}<input type="checkbox" name="skill_ids"
        value="${item.id}"${selected.includes(item.id) ? " checked" : ""}></label>`
    : `<button class="setting-row skill-option" data-action="skill-edit" data-id="${item.id}">
        ${glyph("skills")}${copy}<span class="tail">${icon("arrow")}</span></button>`;
}

async function skillList(scope?: number, newlyAdded?: number) {
  const title = scope === undefined ? t("Библиотека скиллов", "Skill library") : t("Скиллы проекта", "Project skills");
  if (demo) {
    openSheet(title, `<p class="sheet-desc">${t("Открой Алефа в Telegram, чтобы создать скиллы и выбрать их для своих проектов.","Open Aleph in Telegram to create skills and choose which projects use them.")}</p>
      <button class="button primary" data-action="chat">${t("Открыть Алефа","Open Aleph")}</button>`);
    return;
  }
  openSheet(title, `<p class="sheet-desc" role="status" data-skill-loading>${t("Загрузка…","Loading…")}</p>`);
  const loading = requiredElement(sheet, "[data-skill-loading]");
  const isCurrent = () => loading.isConnected && sheet.open && !sheet.classList.contains("closing");
  const attr = scope === undefined ? "" : ` data-id="${scope}"`;
  try {
    const project = scope === undefined ? undefined : await request(`/api/projects/${scope}/skills`);
    const items = project?.skills ?? await request("/api/skills");
    if (!isCurrent()) return;
    const intro = scope === undefined
      ? t("Сохраняй инструкции для повторяющихся задач и подключай их в нужных проектах.", "Save instructions for repeated tasks and enable them in the projects that need them.")
      : t("Алеф применяет выбранные скиллы, когда они подходят к запросу в этом проекте.", "Aleph uses the selected skills when they match a request in this project.");
    const actions = `<div class="skill-actions">
      <button class="button" data-action="skill-create"${attr}>${icon("plus")}${t("Создать","Create")}</button>
      <button class="button" data-action="skill-import"${attr}>${icon("download")}${t("Импортировать","Import")}</button>
    </div>`;
    openSheet(title, `
      ${scope === undefined ? "" : `<p class="skill-workspace" dir="auto">${e(projectName(scope))}</p>`}
      <p class="sheet-desc">${intro}</p>
      ${actions}
      ${project && scope !== undefined && items.length ? skillSelectionForm(scope, project, newlyAdded)
        : items.length ? `<div class="panel skill-list">${items.map(item => skillOption(item)).join("")}</div>`
        : `<div class="empty surface skill-empty"><strong>${t("Пока скиллов нет","No skills yet")}</strong>
          <p>${t("Начни с повторяющейся задачи. Например, приводи заметки после встреч к одному формату.","Start with a repeated task, such as turning meeting notes into a consistent recap.")}</p>
          <button class="small-action" data-action="skill-template"${attr}>${t("Открыть шаблон","Open template")}${icon("arrow")}</button>
        </div>`}`);
  } catch (error) {
    if (isCurrent()) openSheet(title, `<p class="form-error" role="alert">${e(errorMessage(error))}</p>
      <button class="small-action" data-action="skill-list"${attr}>${t("Повторить","Try again")}</button>`);
  }
}

function skillSelectionForm(scope: number, project: ProjectSkills, newlyAdded?: number) {
  const selected = newlyAdded && project.selected.length < project.limit && !project.selected.includes(newlyAdded)
    ? [...project.selected, newlyAdded] : project.selected;
  return `<form id="project-skills-form" data-project="${scope}" data-limit="${project.limit}">
    <div class="panel skill-list">${project.skills.map(item => skillOption(item, selected)).join("")}</div>
    ${submit(t("Сохранить выбор","Save selection"))}</form>`;
}

function skillEditor(draft?: SkillDraft, scope?: number, id?: number) {
  const title = id === undefined ? (draft?.source_url ? t("Предпросмотр скилла","Preview skill") : t("Новый скилл","New skill"))
    : t("Редактировать скилл","Edit skill");
  openSheet(title, `${skillBack(scope)}
    <form id="skill-form"${scope === undefined ? "" : ` data-project="${scope}"`}${id === undefined ? "" : ` data-id="${id}"`}>
      ${draft?.source_url ? `<p class="sheet-desc skill-source">
        <a href="${e(draft.source_url)}" data-external-link target="_blank" rel="noopener noreferrer">${t("Источник на GitHub","View source on GitHub")}</a>
        <input type="hidden" name="source_url" value="${e(draft.source_url)}">
      </p>` : ""}
      <label class="field">${t("Название","Name")}<input name="name" value="${e(draft?.name)}" required maxlength="64" dir="auto" placeholder="${t("Например, Итоги встречи","For example, Meeting recap")}"></label>
      <label class="field">${t("Когда применять","When to use it")}<textarea name="description" required rows="2" maxlength="1024" dir="auto"
        placeholder="${t("Когда я прошу подвести итоги встречи или присылаю её заметки.","When I ask for a meeting recap or share meeting notes.")}">${e(draft?.description)}</textarea></label>
      <label class="field">${t("Инструкции для Алефа","Instructions for Aleph")}<textarea class="skill-instructions" name="instructions" required rows="7" maxlength="12000" dir="auto"
        placeholder="${t("Опиши шаги и формат результата.","Describe the steps and the output format.")}">${e(draft?.instructions)}</textarea></label>
      ${submit(id === undefined ? t("Добавить в библиотеку","Add to library") : saveLabel())}
    </form>
    ${id === undefined ? "" : `<button class="small-action project-entry-remove" data-action="skill-remove" data-id="${id}">${icon("trash")}${t("Удалить из библиотеки","Remove from library")}</button>`}`);
}

async function editSkill(id: number) {
  const title = t("Редактировать скилл","Edit skill");
  openSheet(title, `${skillBack()}<p class="sheet-desc" data-skill-loading role="status">${t("Загрузка…","Loading…")}</p>`);
  const loading = requiredElement(sheet, "[data-skill-loading]");
  try {
    const item = await request(`/api/skills/${id}`);
    if (loading.isConnected && sheet.open && !sheet.classList.contains("closing")) skillEditor(item, undefined, id);
  } catch (error) {
    if (loading.isConnected) loading.textContent = errorMessage(error);
  }
}

function skillImport(scope?: number) {
  openSheet(t("Импорт скилла","Import a skill"), `${skillBack(scope)}
    <p class="sheet-desc skill-import-desc">${t("Вставь публичную ссылку на SKILL.md или папку скилла в GitHub. Можно загрузить SKILL.md с устройства.","Paste a public link to a SKILL.md file or skill folder on GitHub, or upload SKILL.md from your device.")}</p>
    <form id="skill-import-form"${scope === undefined ? "" : ` data-project="${scope}"`}>
      <label class="field">${t("Ссылка GitHub","GitHub link")}<input type="url" name="url" inputmode="url" autocomplete="off" spellcheck="false" placeholder="https://github.com/…"></label>
      <label class="file-label skill-file">${icon("document")}<span>${t("Выбрать SKILL.md","Choose SKILL.md")}</span>
        <input type="file" name="skill_file" accept=".md,.markdown,text/markdown,text/plain"></label>
      ${submit(t("Предпросмотр","Preview"))}
    </form>`);
}

function meetingSkill(): SkillDraft {
  return {
    name: t("Итоги встречи","Meeting recap"),
    description: t("Когда я присылаю заметки со встречи или прошу подвести итоги обсуждения.","When I share meeting notes or ask for a recap of a discussion."),
    instructions: t(
      "Кратко сформулируй итог встречи. Затем перечисли принятые решения, следующие шаги и открытые вопросы. Добавляй ответственных и сроки только если они названы. Если информации не хватает, пометь это. Сохраняй задачи только по моей просьбе.",
      "Start with a short recap. Then list decisions, next steps and open questions. Include owners and dates only when given. Flag missing details. Create tasks only if I ask.",
    ),
    source_url: "",
  };
}

function appearanceSheet() {
  const current = visibleTheme();
  const card = (theme: SelectableTheme) => `<button class="theme-card" type="button" data-action="choose-theme"
      data-id="${theme}" aria-pressed="${theme === current}">
    <span class="theme-preview" data-preview="${theme}" aria-hidden="true">
      <span class="theme-preview-head"><img src="${__ALEPH_MARK_URL__}" alt="" width="15" height="15">
        <span>${t("Задачи", "Tasks")}</span></span>
      <span class="theme-preview-tabs"><i></i><i></i><i></i></span>
      <span class="theme-preview-list"><i></i><i></i></span>
      <span class="theme-preview-dock">${icon("home")}${icon("projects")}${icon("settings")}</span>
    </span>
    <span class="theme-card-caption">${themeName(theme)}<span class="theme-card-check" aria-hidden="true">${icon("check")}</span></span>
  </button>`;
  openSheet(t("Оформление", "Appearance"), `<div class="theme-picker" role="group" aria-label="${t("Цветовая тема", "Color theme")}">
    ${card("white")}${card("dark")}</div><p class="form-error" role="alert"></p>`);
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
  sheetMotion.reset();
  sheet.innerHTML = `<div class="sheet-body"><div class="sheet-header">
    <div class="sheet-grab" aria-hidden="true"></div>
    <div class="sheet-head"><h2 id="sheet-heading">${e(title)}</h2>
      <button class="icon-button" aria-label="${t("Закрыть", "Close")}" data-action="close">${icon("close")}</button>
    </div>
  </div>${html}</div>`;
  sheet.setAttribute("aria-labelledby", "sheet-heading");
  if (!sheet.open) sheetMotion.open();
  else sheet.scrollTop = 0;
  syncBack();
}
function closeSheet() {
  sheetMotion.close();
}
sheet.addEventListener("cancel", event => {
  event.preventDefault();
  if (!pending) closeSheet();
});
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
    case "task-scope":
      taskScopeSheet();
      break;
    case "select-task-scope":
      selectTaskScope(id);
      break;
    case "skill-library":
    case "project-skills":
    case "skill-list":
      await skillList(skillScope(id));
      break;
    case "skill-create":
      skillEditor(undefined, skillScope(id));
      break;
    case "skill-template":
      skillEditor(meetingSkill(), skillScope(id));
      break;
    case "skill-import":
      skillImport(skillScope(id));
      break;
    case "skill-edit":
      await editSkill(Number(id));
      break;
    case "skill-remove":
      if (window.confirm(t("Удалить скилл из библиотеки и всех проектов?", "Remove this skill from the library and every project?")) &&
          await write(`/api/skills/${Number(id)}`, "DELETE", undefined, () => {}, t("Скилл удалён","Skill removed"))) await skillList();
      break;
    case "new-project":
      if (bridge()) openChat();
      else {
        projectListScroll = activeView()?.panel.scrollTop || 0;
        projectSheet();
      }
      break;
    case "edit-project":
    case "project-context": {
      const project = selectedProject();
      if (!project || project.id !== Number(id)) break;
      if (action === "edit-project") projectSheet(project);
      else projectContextSheet(project);
      break;
    }
    case "project-list":
      showProjectList();
      break;
    case "view-project": {
      const next = Number(id);
      if (next !== 0 && !state.projects?.some(project => project.id === next)) break;
      if (page === "projects" && projectDetail === null) projectListScroll = activeView()?.panel.scrollTop || 0;
      if (projectDetail !== next) {
        projectPane = "tasks";
        projectDoneOpen = false;
        projectDetail = next;
      }
      if (page !== "projects") navigate("projects");
      render();
      activeView()?.panel.scrollTo({top: 0});
      activeView()?.heading.focus({preventScroll: true});
      await loadProject(next);
      break;
    }
    case "reload-project":
      if (projectDetail === Number(id)) await loadProject(projectDetail);
      break;
    case "project-pane":
      if (!selectedProject() || (id !== "tasks" && id !== "notes" && id !== "sources") || id === projectPane) break;
      projectPane = id;
      selectionFeedback();
      render();
      if (id === "sources" && projectDetail) await loadSources(projectDetail);
      break;
    case "load-sources":
      if (projectDetail && projectDetail === Number(id)) await loadSources(projectDetail);
      break;
    case "add-source":
      if (projectDetail && projectDetail === Number(id)) newSourceSheet(projectDetail);
      break;
    case "view-source":
      if (projectDetail) await viewSource(projectDetail, Number(id));
      break;
    case "refresh-source":
    case "read-source-article": {
      const project = projectDetail, source = Number(id);
      if (!project) break;
      const error = sheet.querySelector(".form-error");
      if (error) error.textContent = "";
      const reader = action === "read-source-article";
      sourceProgress(reader ? t("Извлекаю текст статьи…","Extracting article text…") : t("Обновляю источник…","Refreshing source…"));
      const path = reader ? `/api/projects/${project}/sources/${source}/reader` as const : `/api/projects/${project}/sources/${source}` as const;
      const ok = await write(path, "POST", undefined,
        item => saveSource(project, item), "", {...importOptions, showApiMessage: ru});
      sourceProgress("");
      if (ok) await viewSource(project, source);
      break;
    }
    case "remove-source": {
      const project = projectDetail, source = Number(id);
      if (!project || !window.confirm(t("Удалить источник из проекта?","Remove this source from the project?"))) break;
      if (await write(`/api/projects/${project}/sources/${source}`, "DELETE", undefined, () => {
        projectSources.set(project, (projectSources.get(project) || []).filter(item => item.id !== source));
      }, t("Источник удалён","Source removed"))) closeSheet();
      break;
    }
    case "continue-project": {
      const project_id = Number(id);
      if (await write("/api/projects/active","PATCH",{project_id},
        () => { state.active_project_id = project_id; }, "")) openChat();
      break;
    }
    case "new-entry":
      if (selectedProject()) entrySheet();
      break;
    case "project-entry": {
      const project = selectedProject();
      const entry = project && projectEntries.get(project.id)?.find(item => item.id === Number(id));
      if (entry) projectEntrySheet(entry);
      break;
    }
    case "remove-entry": {
      const selected = selectedProject();
      if (!selected || !window.confirm(t("Удалить эту запись?","Remove this note?"))) return;
      if (await write(`/api/projects/${selected.id}/entries/${Number(id)}`,"DELETE",undefined,
        () => projectEntries.set(selected.id,(projectEntries.get(selected.id) || []).filter(entry => entry.id !== Number(id))),
        t("Запись удалена","Note removed"))) closeSheet();
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
    case "choose-theme":
      if ((id === "white" || id === "dark") && state.settings.theme !== id) {
        sheet.querySelector(".form-error")?.replaceChildren();
        previewTheme = id;
        applySystemTheme();
        try {
          if (await saveSettings({theme: id}, "") && bridge()) state.settings.theme = id;
        } finally {
          previewTheme = undefined;
          applySystemTheme();
        }
      }
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
    if (!link.closest(".task-result, .project-entry-body, .skill-source, .source-detail")) closeSheet();
    return;
  }
  const el = target?.closest("button");
  if (!el || el.disabled || sheet.classList.contains("closing")) return;
  if (el.dataset.page) navigate(el.dataset.page);
  else if (el.dataset.filter && Object.hasOwn(taskLabels(), el.dataset.filter)) {
    if(filter===el.dataset.filter)return;
    selectionFeedback();
    filter = el.dataset.filter as PlanFilter;
    render();
  } else if (el.dataset.action) void act(el.dataset.action, el.dataset.id);
});
app.addEventListener("toggle", event => {
  const target = event.target;
  if (target instanceof HTMLDetailsElement && target.isConnected && target.matches(".project-done")) {
    projectDoneOpen = target.open;
  }
}, true);
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
    if (form.id === "source-link-form") {
      const project = Number(form.dataset.project);
      const url = field("url").trim();
      if (!url || project !== projectDetail) return;
      requiredElement(form, ".form-error").textContent = "";
      sourceProgress(t("Читаю содержимое ссылки…","Reading the linked source…"));
      let itemId = 0;
      await write(`/api/projects/${project}/sources`, "POST", {url}, item => {
        saveSource(project, item);
        itemId = item?.id || 0;
      }, "", {...importOptions, showApiMessage: ru});
      sourceProgress("");
      if (itemId) await viewSource(project, itemId);
      return;
    } else if (form.id === "skill-import-form") {
      const file = data.get("skill_file");
      const hasFile = file instanceof File && file.size > 0;
      const url = field("url").trim();
      const error = requiredElement(form, ".form-error");
      if ((!hasFile && !url) || (hasFile && file.size > 48 * 1024)) {
        error.textContent = hasFile
          ? t("Выбери SKILL.md размером до 48 КБ.", "Choose a SKILL.md file up to 48 KB.")
          : t("Вставь ссылку или выбери файл SKILL.md.", "Paste a link or choose a SKILL.md file.");
        return;
      }
      let preview: SkillDraft | null = null;
      let input: {markdown: string} | {url: string} = {url};
      if (hasFile) {
        try {
          input = {markdown: new TextDecoder("utf-8", {fatal: true}).decode(await file.arrayBuffer())};
        } catch {
          error.textContent = t("Выбери текстовый SKILL.md в UTF-8.", "Choose a UTF-8 SKILL.md text file.");
          return;
        }
      }
      if (!form.isConnected || !sheet.open || sheet.classList.contains("closing")) return;
      await write("/api/skills/preview", "POST", input, result => { preview = result; }, "", {showApiMessage: ru});
      if (preview) skillEditor(preview, skillScope(form.dataset.project));
      return;
    } else if (form.id === "skill-form") {
      const id = form.dataset.id;
      const scope = skillScope(form.dataset.project);
      const payload = {name: field("name").trim(), description: field("description").trim(),
        instructions: field("instructions").trim(), source_url: field("source_url")};
      let saved: number | undefined;
      if (await write(id ? `/api/skills/${Number(id)}` : "/api/skills", id ? "PATCH" : "POST", payload,
        result => { saved = result?.id; }, t("Скилл сохранён","Skill saved"), {showApiMessage: ru})) await skillList(scope, id ? undefined : saved);
      return;
    } else if (form.id === "project-skills-form") {
      const scope = Number(form.dataset.project);
      const skill_ids = data.getAll("skill_ids").map(Number);
      const limit = Number(form.dataset.limit);
      if (skill_ids.length > limit) {
        requiredElement(form, ".form-error").textContent = t(`Выбери до ${limit} скиллов.`, `Choose up to ${limit} skills.`);
        return;
      }
      ok = await write(`/api/projects/${scope}/skills`, "PATCH", {skill_ids}, () => {},
        t("Скиллы проекта сохранены","Project skills saved"), {showApiMessage: ru});
    } else if (form.id === "project-form") {
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
        if (!id) {
          projectPane = "tasks";
          projectDoneOpen = false;
          projectEntries.set(project.id, []);
          activeView()?.panel.scrollTo({top: 0});
        }
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
          if (page === "home" && filter !== "all") filter = taskFilter(record);
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
  if (picker instanceof HTMLInputElement && picker.name === "project_source_file") {
    await uploadSource(picker);
    return;
  }
  if (picker instanceof HTMLInputElement && picker.form?.id === "skill-import-form" && picker.name === "skill_file") {
    const file = picker.files?.[0];
    requiredElement(picker.form, ".skill-file span").textContent = file?.name || t("Выбрать SKILL.md","Choose SKILL.md");
    if (file) requiredElement<HTMLInputElement>(picker.form, "[name=url]").value = "";
    return;
  }
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
  if (input instanceof HTMLInputElement && input.form?.id === "skill-import-form" && input.name === "url" && input.value) {
    requiredElement<HTMLInputElement>(input.form, "[name=skill_file]").value = "";
    requiredElement(input.form, ".skill-file span").textContent = t("Выбрать SKILL.md","Choose SKILL.md");
    return;
  }
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
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", applySystemTheme);
setTheme();
setLanguage(
  webapp()?.initDataUnsafe?.user?.language_code ||
    (demo ? "ru" : navigator.language),
);
void start();
