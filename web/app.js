import { icon } from "./icons.js";
import {
  signed,
  bridge,
  demo,
  request,
  demoState,
  saveDemo,
  escapeHtml as e,
  localDateTime,
  zonedISO,
  demoCalendar,
} from "./store.js";

const app = document.querySelector("#app"),
  sheet = document.querySelector("#sheet"),
  toastEl = document.querySelector("#toast");
let calendarFile;
let state,
  page = "home",
  filter = "open",
  ru = true,
  pending = false,
  modal = null,
  toastTimer,
  revision = 0,
  returnFocus,
  sdkBound = false;
const t = (r, en) => (ru ? r : en),
  locale = () => (ru ? "ru-RU" : "en-US");
const webapp = () => window.Telegram?.WebApp;
const zone = () => state?.settings.timezone || "UTC";
const byId = (id) => state.tasks.find((x) => String(x.id) === String(id));
const scopedTasks = () => state.tasks.filter((task) => Number(task.project_id || 0) === activeId());
const sorted = () =>
  scopedTasks().sort(
    (a, b) =>
      (a.due_at ? Date.parse(a.due_at) : Infinity) -
        (b.due_at ? Date.parse(b.due_at) : Infinity) || b.id - a.id,
  );
const activeId = () => Number(state.active_project_id || 0);
const currentProject = () => (state.projects || []).find((item) => item.id === activeId());
const readableDay = (iso) =>
  new Intl.DateTimeFormat(locale(), {
    timeZone: zone(),
    day: "numeric",
    month: "short",
  }).format(new Date(iso));
const clock = (iso) =>
  new Intl.DateTimeFormat(locale(), {
    timeZone: zone(),
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).format(new Date(iso));
const timeLabel = (time) => String(time || "").slice(0, 5);
const navs = () => [
  ["home", t("План", "Plan")],
  ["projects", t("Проекты", "Projects")],
  ["settings", t("Настройки", "Settings")],
];
const isDark = () =>
  webapp()?.platform && webapp().platform !== "unknown" && webapp().colorScheme
    ? webapp().colorScheme === "dark"
    : matchMedia("(prefers-color-scheme: dark)").matches;
function setTheme() {
  const dark = isDark();
  const root = document.documentElement;
  const tg = webapp();
  root.dataset.theme = dark ? "dark" : "light";
  const bg = dark ? "#000000" : "#edede8";
  document.querySelector("meta[name=theme-color]").content = bg;
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
    } else navigate("home");
  });
  syncBack();
}
function syncBack() {
  const back = webapp()?.BackButton;
  if (!back || !webapp()?.isVersionAtLeast?.("6.1")) return;
  if (sheet.open || page !== "home") back.show();
  else back.hide();
}
function setLanguage(code) {
  ru = !code || ["ru", "uk", "be"].includes(code.toLowerCase().split("-")[0]);
  document.documentElement.lang = ru ? "ru" : "en";
}
function due(iso, withTime = true) {
  if (!iso) return t("Без срока", "No due date");
  const date = localDateTime(iso, zone()).slice(0, 10),
    today = localDateTime(Date.now(), zone()).slice(0, 10);
  const tomorrow = new Date(Date.parse(today + "T12:00Z") + 86400000)
    .toISOString()
    .slice(0, 10);
  const day =
    date === today
      ? t("Сегодня", "Today")
      : date === tomorrow
        ? t("Завтра", "Tomorrow")
        : readableDay(iso);
  return withTime ? day + " · " + clock(iso) : day;
}
function message(text) {
  clearTimeout(toastTimer);
  toastEl.textContent = text;
  toastEl.classList.add("show");
  toastTimer = setTimeout(() => toastEl.classList.remove("show"), 3300);
}
function errorMessage(error) {
  if (error.message === "static-plan") {
    return t(
      "Напиши Алефу «открой план» и нажми появившуюся кнопку в чате.",
      "Send “open plan” to Aleph, then tap the button in your chat.",
    );
  }
  if (error.message === "invalid-date")
    return t(
      "Такого времени нет в выбранном часовом поясе. Выбери другое время.",
      "This time does not exist in this time zone. Choose a different time.",
    );
  if (["401", "403"].includes(error.message))
    return t(
      "Открой приложение заново из чата с Aleph.",
      "Open the app again from your chat with Aleph.",
    );
  if (error.message === "422" || error.message === "400")
    return t("Проверь введённые данные.", "Check the details and try again.");
  return t(
    "Не удалось выполнить запрос. Проверь соединение и попробуй ещё раз.",
    "Could not complete the request. Check your connection and try again.",
  );
}
function nav() {
  return navs().map(([id, label]) =>
    `<button class="nav-item ${page === id ? "active" : ""}" data-page="${id}" ${page === id ? 'aria-current="page"' : ""}>${icon(id)}<span>${label}</span></button>`
  ).join("");
}
const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");
const scrollByPage = new Map();
let workspace, pageContent, dock, toolbarAction, mountedPage, lastContent, refreshRunning = false;
let closeTimer, closeListener, focusTarget, sheetDrag;

function selectionFeedback() {
  if (!demo && !reducedMotion.matches && webapp()?.isVersionAtLeast?.("6.1")) {
    try { webapp().HapticFeedback?.selectionChanged(); } catch {}
  }
}

function navigate(id) {
  if (!navs().some(([key]) => key === id)) return;
  if (page === id) {
    window.scrollTo({top: 0, behavior: reducedMotion.matches ? "auto" : "smooth"});
    return;
  }
  scrollByPage.set(page, window.scrollY);
  page = id;
  selectionFeedback();
  render();
  window.scrollTo({top: scrollByPage.get(id) || 0, behavior: "instant"});
}

function mountShell() {
  app.innerHTML = `
    <header class="toolbar"><div class="toolbar-inner">
      <span class="wordmark"><img src="./aleph.webp" alt="" width="29" height="29"><span>Aleph</span><small>LAB ONE</small></span>
      ${demo ? `<span class="demo-badge">${t("Демо", "Demo")}</span>` : ""}
      <button class="toolbar-action"></button>
    </div></header>
    <main class="workspace" id="view"><header class="page-head"><div><h1 class="page-title" tabindex="-1"></h1><p class="page-kicker" hidden></p></div></header><div class="page-content"></div></main>
    <nav aria-label="${t("Навигация", "Navigation")}" class="dock glass"><span aria-hidden="true" class="dock-lens"></span>${nav()}</nav>`;
  workspace = app.querySelector("#view");
  pageContent = workspace.querySelector(".page-content");
  dock = app.querySelector(".dock");
  toolbarAction = app.querySelector(".toolbar-action");
}

// Keep both the native navigation surface and its lens mounted as content changes.
function render() {
  if (!state) return;
  if (!workspace?.isConnected) mountShell();
  const tabs = navs(), index = tabs.findIndex(([key]) => key === page);
  if (page !== mountedPage) {
    const previous = tabs.findIndex(([key]) => key === mountedPage);
    workspace.classList.remove("view-enter-forward","view-enter-backward");
    if (mountedPage && !reducedMotion.matches) {
      void workspace.offsetWidth;
      workspace.classList.add(index > previous ? "view-enter-forward" : "view-enter-backward");
    }
    const title = tabs[index][1];
    workspace.querySelector("h1").textContent = title;
    document.title = title + " · Aleph";
    dock.style.setProperty("--tab-index", index);
    for(const tab of dock.querySelectorAll("[data-page]")){
      const selected = tab.dataset.page === page;
      tab.classList.toggle("active",selected);
      if(selected) tab.setAttribute("aria-current","page");
      else tab.removeAttribute("aria-current");
    }
    const projectAction = page === "projects" && !bridge();
    toolbarAction.dataset.action = projectAction ? "new-project" : "chat";
    toolbarAction.innerHTML = projectAction ? icon("plus") + t("Новый","New") : icon("plane") + t("В чат","Chat");
    mountedPage = page;
  }
  const kicker = workspace.querySelector(".page-kicker");
  kicker.hidden = page !== "home";
  if (!kicker.hidden) {
    kicker.textContent = new Intl.DateTimeFormat(locale(), {timeZone:zone(), weekday:"long",day:"numeric",month:"long"}).format(new Date());
  }
  const markup = page === "home" ? overview() : page === "projects" ? projectsPage() : settingsPage();
  if (markup !== lastContent) {
    const restore = pageContent.contains(document.activeElement) ? controlSelector(document.activeElement) : null;
    pageContent.innerHTML = markup;
    lastContent = markup;
    if(restore) pageContent.querySelector(restore)?.focus({preventScroll:true});
  }
  app.setAttribute("aria-busy","false");
  syncBack();
}

function controlSelector(el){
  if(!el?.matches("button")) return null;
  const attrs = ["data-action","data-page","data-filter","data-id"].filter(key=>el.hasAttribute(key));
  return attrs.length ? "button"+attrs.map(key=>"["+key+'="'+CSS.escape(el.getAttribute(key))+'"]').join("") : null;
}

function taskRow(task) {
  const done = task.status === "done",
    late = !done && task.due_at && Date.parse(task.due_at) < Date.now();
  return `<div class="task-row ${done ? "done" : ""}"><button class="task-toggle" data-action="toggle-task" data-id="${e(task.id)}" aria-label="${e((done ? t("Вернуть в работу", "Reopen") : t("Завершить", "Complete")) + " · " + task.title)}" aria-pressed="${done}"><span class="ring">${done ? icon("check") : ""}</span></button><button class="task-body" data-action="edit-task" data-id="${e(task.id)}"><span class="task-title">${e(task.title)}</span>${task.due_at ? `<span class="task-date ${late ? "late" : ""}">${icon("clock")}${e(clock(task.due_at))}</span>` : ""}</button><span class="arrow">${icon("arrow")}</span></div>`;
}
function taskEmpty(done = false) {
  return `<div class="empty"><span class="empty-symbol" aria-hidden="true">${icon(done ? "check" : "calendar")}</span><strong>${done ? t("Завершённых поручений пока нет", "No completed tasks yet") : t("Пока всё свободно", "Nothing on the list yet")}</strong><p>${done ? t("Здесь появятся отмеченные галочкой задачи.", "Tasks you check off will appear here.") : t("Напиши Алефу в чате или добавь поручение здесь.", "Message Aleph in chat or add a task here.")}</p></div>`;
}
function switcher() {
  return `<button class="switch" role="switch" aria-label="${t("Проактивность", "Proactive updates")}" aria-checked="${Boolean(state.settings.proactive)}" data-action="proactive"></button>`;
}
function glyph(symbol, color) {
  return `<span class="glyph ${color}">${icon(symbol)}</span>`;
}
function integrationRows() {
  return `<button class="integration-row" data-action="calendar">${glyph("calendar", "blue")}<span><span class="integration-label">${t("Календарь", "Calendar")}</span><small class="integration-info">${t("Экспорт поручений в .ics", "Export tasks as .ics")}</small></span><span class="tail">${icon("download")}</span></button><button class="integration-row" data-action="feeds">${glyph("rss", "orange")}<span><span class="integration-label">RSS</span><small class="integration-info">${state.feeds.length ? t("Подключено", "Connected") + " · " + state.feeds.length : t("Добавить источник", "Add a source")}</small></span><span class="tail">${icon("arrow")}</span></button>`;
}
function sectionHead(title, action = "", label = "") {
  return `<div class="section-head"><h2>${title}</h2>${action ? `<button class="small-action" data-page="${action}">${label}${icon("right")}</button>` : ""}</div>`;
}
function overview() {
  const current = currentProject();
  const tasks = sorted();
  const visible = tasks.filter((item) => item.status === filter);
  const today = due(Date.now(), false);
  const groups = new Map();
  for (const item of visible) {
    const day = due(item.due_at, false);
    if (!groups.has(day)) groups.set(day, []);
    groups.get(day).push(item);
  }
  const filters = [["open", t("В работе", "Open")], ["done", t("Готово", "Done")]];
  return `<button class="context-row panel" data-page="projects">${glyph(current ? "projects" : "person", "blue")}<span class="context-copy">
      <small><i class="signal-dot"></i>${t("Контекст в чате", "Active in chat")}</small><strong>${e(current?.name || t("Личное", "Personal"))}</strong></span>${icon("arrow")}</button>
    <div class="task-tools">
      <div class="segmented" aria-label="${t("Статус поручений", "Task status")}">${filters.map(([key, label]) =>
        `<button aria-pressed="${filter === key}" data-filter="${key}" class="segment ${filter === key ? "active" : ""}">${label}<small>${tasks.filter((x) => x.status === key).length}</small></button>`).join("")}</div>
      <button class="action-plus glass" aria-label="${t("Добавить поручение", "Add task")}" data-action="new-task">${icon("plus")}</button>
    </div>
    ${groups.size ? `<div class="agenda panel">${[...groups].map(([day,items]) => `<section class="task-group"><h2 class="group-label ${day === today ? "today" : ""}">${e(day)}</h2>${items.map(taskRow).join("")}</section>`).join("")}</div>`
      : `<div class="panel">${taskEmpty(filter === "done")}</div>`}
    ${bridge() && Number(state.snapshot_total || 0) > state.tasks.length ? `<p class="settings-note">${t("Полный список доступен в чате.", "See the complete list in chat.")}</p>` : ""}
    <section class="brief-section">${sectionHead(t("Уведомления", "Notifications"))}
      <div class="panel"><button class="integration-row" data-page="settings">${glyph("bell", "green")}<span><span class="integration-label">${t("Утренний обзор", "Morning brief")}</span><small class="integration-info">${state.settings.proactive ? e(timeLabel(state.settings.digest_time)) + " · " + t("В чате с Алефом", "In Aleph chat") : t("На паузе", "Paused")}</small></span><span class="tail">${icon("arrow")}</span></button></div>
    </section>`;
}
function projectsPage() {
  const projects = state.projects || [];
  const current = currentProject();
  if (bridge()) return `<div class="panel"><div class="empty"><span class="empty-symbol">${icon("projects")}</span><strong>${t("Проекты в чате","Projects in chat")}</strong><p>${t("Напиши «покажи проекты», чтобы выбрать контекст разговора.","Send “show my projects” to choose a conversation context.")}</p><button class="button" data-action="chat">${t("В чат","Open chat")}</button></div></div>`;
  const counts = new Map();
  for (const task of state.tasks) {
    const id = Number(task.project_id || 0);
    if (task.status === "open") counts.set(id, (counts.get(id) || 0) + 1);
  }
  const workspaces = [{ id:0, name:t("Личное","Personal") }, ...projects];
  return `<p class="page-intro">${t("Выбранный проект задаёт контекст разговора с Алефом.", "Your selected project sets the context for your conversation with Aleph.")}</p>
    <div class="panel workspace-list">${workspaces.map((item) => {
      const selected = item.id === activeId();
      return `<button class="project-row" data-action="choose-project" data-id="${item.id}" ${selected ? 'aria-current="true"' : ""}>
        ${glyph(item.id ? "projects" : "person", item.id ? "violet" : "blue")}
        <span class="setting-copy"><strong>${e(item.name)}</strong><small>${selected ? `<i class="signal-dot"></i>` + t("Активен в чате", "Active in chat") : counts.has(item.id) ? t("В работе", "Open") + " · " + counts.get(item.id) : t("Нет открытых поручений", "No open tasks")}</small></span>
        <span class="tail ${selected ? "selected" : ""}">${icon(selected ? "check" : "arrow")}</span>
      </button>`;
    }).join("")}</div>
    ${current ? `<section class="project-context">${sectionHead(t("Контекст проекта", "Project context"))}
      <div class="panel">
        <p class="context-text" dir="auto">${current.context ? e(current.context) : t("Добавь цели, материалы и то, что Алефу важно учитывать.", "Add goals, reference material and anything Aleph should keep in mind.")}</p>
        <button class="add-line" data-action="edit-project" data-id="${current.id}">${icon("edit")}${t("Редактировать контекст", "Edit context")}</button>
      </div><button class="button project-chat" data-action="chat">${icon("plane")}${t("Продолжить в чате", "Continue in chat")}</button></section>`
    : ""}`;
}
function projectSheet(item) {
  openSheet("project", item ? t("О проекте", "About the project") : t("Новый проект", "New project"),
    `<form id="project-form" data-id="${e(item?.id || "")}">
      <label class="field">${t("Название", "Name")}<input name="name" maxlength="64" required value="${e(item?.name || "")}" dir="auto" placeholder="${t("Например, Lab One", "For example, Lab One")}"></label>
      <label class="field">${t("Контекст", "Context")}<textarea name="context" maxlength="10000" rows="6" dir="auto" placeholder="${t("Цели проекта, важные детали, ссылки…", "Goals, important details, links…")}">${e(item?.context || "")}</textarea></label>
      <label class="file-label">${icon("plus")}${t("Добавить текстовый файл", "Add a text file")}<input type="file" accept=".txt,.md,.csv,.json,.tsv" name="reference"></label>
      ${submit(item ? saveLabel() : t("Создать и открыть", "Create and open"))}
    </form>`);
}
function routinesPanel() {
  const items = state.routines || [];
  if (!items.length) return "";
  const names = {
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
function settingsPage() {
  const s = state.settings;
  const detected = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const row = (label, detail, val) =>
    `<button class="setting-row" data-action="schedule"><span class="setting-copy"><strong>${label}</strong><span class="setting-detail">${detail}</span></span><span class="setting-value">${e(val)}${icon("arrow")}</span></button>`;
  return `<div class="settings-grid"><section class="settings-block"><h2 class="settings-title">${t("Ритм дня", "Daily rhythm")}</h2><div class="panel"><div class="setting-row"><span><strong>${t("Проактивность", "Proactive updates")}</strong><span class="setting-detail">${t("Обзоры и инициативы в чате", "Briefings and check-ins in chat")}</span></span>${switcher()}</div>
  ${row(t("Утренний обзор", "Morning brief"), t("Когда присылать план на день", "When to send your daily plan"), timeLabel(s.digest_time))}${row(t("Тихие часы", "Quiet hours"), t("Время для отдыха", "Your time to disconnect"), timeLabel(s.quiet_start) + " – " + timeLabel(s.quiet_end))}${row(t("Часовой пояс", "Time zone"), t("Для поручений и обзоров", "For tasks and briefings"), zone())}</div>${zone() === "UTC" && detected && detected !== "UTC" ? `<button class="small-action tz-suggest" data-action="detected-zone">${icon("globe")}${t("Использовать часовой пояс устройства", "Use device time zone")} · ${e(detected)}</button>` : ""}</section>
  <section class="settings-block"><h2 class="settings-title">${t("Подключения", "Connections")}</h2><div class="panel">${integrationRows()}</div>${demo ? `<p class="settings-note">${t("Демо-режим. Изменения сохраняются в этом браузере.", "Demo mode. Changes are saved in this browser.")}</p>` : ""}<a class="privacy-link" href="./privacy.html" target="_blank" rel="noopener noreferrer">${t("Конфиденциальность", "Privacy")}</a></section>${routinesPanel()}</div>`;
}
function openSheet(type, title, html) {
  if (!sheet.open) {
    returnFocus = document.activeElement;
    focusTarget = controlSelector(returnFocus);
  }
  clearTimeout(closeTimer);
  if (closeListener) sheet.removeEventListener("animationend",closeListener);
  sheet.classList.remove("closing");
  sheet.style.removeProperty("--sheet-release");
  modal = type;
  sheet.innerHTML = `<div class="sheet-body"><div class="sheet-grab" aria-hidden="true"></div><div class="sheet-head"><h2 id="sheet-heading">${title}</h2><button class="icon-button" aria-label="${t("Закрыть", "Close")}" data-action="close">${icon("close")}</button></div>${html}</div>`;
  sheet.setAttribute("aria-labelledby", "sheet-heading");
  if (!sheet.open) sheet.showModal();
  syncBack();
}
function closeSheet() {
  if (!sheet.open || sheet.classList.contains("closing")) return;
  if (reducedMotion.matches) { sheet.close(); return; }
  function finish(event) {
    if (event && (event.target !== sheet || event.pseudoElement)) return;
    clearTimeout(closeTimer);
    sheet.removeEventListener("animationend", finish);
    closeListener = null;
    sheet.close();
    sheet.classList.remove("closing");
    sheet.style.removeProperty("--sheet-release");
  }
  closeListener = finish;
  sheet.classList.add("closing");
  sheet.addEventListener("animationend", finish);
  closeTimer=setTimeout(finish,220);
}
sheet.addEventListener("cancel", event=>{
  event.preventDefault();
  if(!pending) closeSheet();
});
sheet.addEventListener("pointerdown", (event) => {
  if (pending || sheetDrag || sheet.classList.contains("closing") ||
      !event.isPrimary || event.button !== 0 || !event.target.closest(".sheet-grab")) return;
  sheetDrag = { id: event.pointerId, start: event.clientY, offset: 0 };
  sheet.setPointerCapture(event.pointerId);
  sheet.classList.add("dragging");
});
sheet.addEventListener("pointermove", (event) => {
  if (sheetDrag?.id !== event.pointerId) return;
  sheetDrag.offset = Math.max(0, event.clientY - sheetDrag.start);
  sheet.style.transform = `translateY(${sheetDrag.offset}px)`;
});
function releaseSheet(event) {
  if (sheetDrag?.id !== event.pointerId) return;
  const offset = sheetDrag.offset;
  sheetDrag = null;
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
function taskSheet(task) {
  openSheet(
    "task",
    task ? t("Поручение", "Task") : t("Новое поручение", "New task"),
    `<form id="task-form" data-id="${e(task?.id ?? "")}"><label class="field">${t("Что нужно сделать?", "What needs to happen?")}<input name="title" value="${e(task?.title || "")}" required maxlength="240" placeholder="${t("Например, сверить макеты", "For example, review the designs")}" dir="auto"></label><label class="field">${t("Срок", "Due")}<input name="due_at" type="datetime-local" value="${e(task?.due_at ? localDateTime(task.due_at, zone()) : "")}"><small>${t("Часовой пояс", "Time zone")} · ${e(zone())}</small></label>${task?.notes ? `<p class="sheet-desc" dir="auto">${e(task.notes)}</p>` : ""}${submit()}</form>`,
  );
}
function schedule() {
  const s = state.settings,
    current = zone(),
    guessed = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const zones = [
    ...new Set(
      [
        current,
        guessed,
        "UTC",
        ...(Intl.supportedValuesOf
          ? Intl.supportedValuesOf("timeZone")
          : [
              "Europe/Moscow",
              "Europe/London",
              "Europe/Berlin",
              "Asia/Yerevan",
              "America/New_York",
              "Asia/Tokyo",
            ]),
      ].filter(Boolean),
    ),
  ];
  openSheet(
    "schedule",
    t("Расписание", "Schedule"),
    `<form id="schedule-form">${[
      ["digest_time", t("Утренний обзор", "Morning brief")],
      ["quiet_start", t("Начало тихих часов", "Quiet hours start")],
      ["quiet_end", t("Конец тихих часов", "Quiet hours end")],
    ]
      .map(
        ([k, l]) =>
          `<label class="field">${l}<input type="time" required name="${k}" value="${e(timeLabel(s[k]))}"></label>`,
      )
      .join(
        "",
      )}<label class="field">${t("Часовой пояс", "Time zone")}<select name="timezone">${zones.map((z) => `<option value="${e(z)}" ${z === current ? "selected" : ""}>${e(z.replaceAll("_", " "))}</option>`).join("")}</select></label>${submit()}</form>`,
  );
}
function feedsSheet() {
  const catalog = state.feed_catalog || [],
    choices = catalog.filter((x) => !state.feeds.some((f) => f.url === x.url));
  openSheet(
    "feeds",
    t("Источники RSS", "RSS sources"),
    `<p class="sheet-desc">${t("Выбери источники для новостей в утреннем обзоре.", "Choose news sources for your morning brief.")}</p>${state.feeds.length ? `<div class="feed-items">${state.feeds.map((f) => `<div class="feed-item"><span>${e(f.title || f.url)}</span><button class="feed-remove" aria-label="${e(t("Отключить", "Disconnect") + " " + (f.title || f.url))}" data-action="remove-feed" data-id="${e(f.id)}">${icon("trash")}</button></div>`).join("")}</div>` : ""}${choices.length ? `<form id="feed-form"><label class="field">${t("Источник", "Source")}<select name="url">${choices.map((c) => `<option value="${e(c.url)}">${e(c.title)}</option>`).join("")}</select></label>${submit(bridge() ? t("Подключить и вернуться в чат", "Connect and return to chat") : t("Подключить", "Connect"))}</form>` : `<p class="settings-note">${t("Все доступные источники подключены.", "All available sources are connected.")}</p>`}`,
  );
}
function openChat() {
  if (!demo && webapp()?.close) {
    webapp().close();
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
  render();
}
function mergeRecord(items, record) {
  const existing=items.find(item=>item.id===record.id);
  if(existing) Object.assign(existing,record);
  else items.unshift(record);
}

async function write(path, method, body, apply, success, options) {
  if (pending) return false;
  pending = true;
  const origin=document.activeElement?.closest("button");
  origin?.setAttribute("aria-busy","true");
  app.classList.add("saving");
  revision++;
  sheet
    .querySelectorAll("button,input,select,textarea")
    .forEach((x) => (x.disabled = true));
  try {
    const result = demo ? null : await request(path, method, body, options);
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
      .querySelectorAll("button,input,select,textarea")
      .forEach((x) => (x.disabled = false));
  }
}
async function exportCalendar() {
  if (pending) return;
  pending = true;
  try {
    const blob = demo
      ? demoCalendar(state.tasks)
      : await request("/api/calendar.ics");
    if (bridge() && !(blob instanceof Blob)) return;
    calendarFile = new File([blob], "aleph-calendar.ics", {
      type: "text/calendar",
    });
    if (navigator.canShare?.({ files: [calendarFile] })) {
      openSheet(
        "calendar",
        t("Календарь готов", "Calendar ready"),
        `<p class="sheet-desc">${t("Открой файл в приложении календаря или сохрани копию.", "Open the file in your calendar or save a copy.")}</p><div class="submit-row"><button class="button" data-action="download-calendar">${t("Скачать", "Download")}</button><button class="button primary" data-action="share-calendar">${t("Поделиться", "Share")}</button></div>`,
      );
    } else downloadCalendar();
  } catch (error) {
    message(errorMessage(error));
  } finally {
    pending = false;
  }
}
function downloadCalendar() {
  if (!calendarFile) return;
  const url = URL.createObjectURL(calendarFile),
    link = document.createElement("a");
  link.href = url;
  link.download = "aleph-calendar.ics";
  link.hidden = true;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 60000);
  if (sheet.open) closeSheet();
  message(t("Календарь подготовлен", "Calendar prepared"));
}
async function act(action, id) {
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
    case "choose-project": {
      const project_id = Number(id);
      if (project_id === activeId()) break;
      await write("/api/projects/active", "PATCH", {project_id},
        chosen => {
          state.active_project_id = chosen?.id ?? project_id;
          if (chosen?.id) mergeRecord(state.projects, chosen);
        },
        t("Контекст в чате обновлён", "Chat context updated"));
      break;
    }
    case "new-task":
      taskSheet();
      break;
    case "edit-task":
      if (byId(id)) taskSheet(byId(id));
      break;
    case "schedule":
      schedule();
      break;
    case "detected-zone": {
      const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
      if (timezone)
        await write(
          "/api/settings",
          "PATCH",
          { timezone },
          settings => Object.assign(state.settings,settings || {timezone}),
          t("Часовой пояс обновлён", "Time zone updated"),
        );
      break;
    }
    case "feeds":
      feedsSheet();
      break;
    case "calendar":
      await exportCalendar();
      break;
    case "share-calendar":
      try {
        await navigator.share({ files: [calendarFile], title: "Aleph" });
        closeSheet();
      } catch (error) {
        if (error.name !== "AbortError")
          message(t("Используй кнопку «Скачать»", "Use the Download button"));
      }
      break;
    case "download-calendar":
      downloadCalendar();
      break;
    case "toggle-task": {
      const item = byId(id);
      if (!item) return;
      const status = item.status === "done" ? "open" : "done";
      await write(
        "/api/tasks/" + id,
        "PATCH",
        { status },
        task => Object.assign(item,task || {status}),
        status === "done"
          ? t("Поручение выполнено", "Task complete")
          : t("Поручение снова в работе", "Task reopened"),
      );
      break;
    }
    case "proactive": {
      const proactive = !state.settings.proactive;
      const control=app.querySelector('[data-action="proactive"]');
      control?.setAttribute("aria-checked",String(proactive));
      const saved=await write(
        "/api/settings",
        "PATCH",
        { proactive },
        settings => Object.assign(state.settings,settings || {proactive}),
        proactive
          ? t("Проактивность включена", "Proactive updates on")
          : t("Проактивность на паузе", "Proactive updates paused"),
      );
      if(!saved)control?.setAttribute("aria-checked",String(state.settings.proactive));
      break;
    }
    case "remove-feed": {
      if (
        await write(
          "/api/feeds/" + id,
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
  const el = event.target.closest("button");
  if (!el || el.disabled || sheet.classList.contains("closing")) return;
  if (el.dataset.page) navigate(el.dataset.page);
  else if (el.dataset.filter) {
    if(filter===el.dataset.filter)return;
    selectionFeedback();
    filter = el.dataset.filter;
    render();
  } else if (el.dataset.action) void act(el.dataset.action, el.dataset.id);
});
sheet.addEventListener("close", () => {
  modal = null;
  syncBack();
  const target = returnFocus?.isConnected ? returnFocus : focusTarget ? app.querySelector(focusTarget) : null;
  (target || workspace?.querySelector("h1"))?.focus({preventScroll:true});
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
  if (pending) return;
  const form = event.target,
    data = Object.fromEntries(new FormData(form));
  let ok = false;
  try {
    if (form.id === "project-form") {
      const name = data.name.trim();
      if (!name) { form.elements.name.value = ""; form.elements.name.reportValidity(); return; }
      const context = data.context.trim();
      const id = Number(form.dataset.id);
      ok = await write("/api/projects" + (id ? "/" + id : ""), id ? "PATCH" : "POST", {name, context}, result => {
        state.projects ||= [];
        const project=result || {id:id || Date.now(),name,context};
        mergeRecord(state.projects,project);
        if (!id) state.active_project_id=project.id;
      }, id ? t("Контекст сохранён", "Context saved") : t("Проект создан", "Project created"));
    } else if (form.id === "task-form") {
      const title = data.title.trim();
      if (!title) {
        const input = form.elements.title;
        input.value = "";
        input.reportValidity();
        return;
      }
      const body = { title, due_at: zonedISO(data.due_at, zone()) };
      const id = form.dataset.id;
      const fields=id ? body : {...body,project_id:activeId()};
      // A retry after a dropped response must reuse the same creation on the server.
      if (!id && JSON.stringify(fields)!==form.dataset.submission) {
        form.dataset.submission=JSON.stringify(fields);
        form.dataset.requestKey=crypto.randomUUID?.() || (Date.now().toString(36)+Math.random().toString(36).slice(2));
      }
      ok = await write(
        "/api/tasks" + (id ? "/" + id : ""),
        id ? "PATCH" : "POST", fields,
        result => {
          const record=result || (id ? {...byId(id),...body} : {
            ...fields, id:Math.max(0,...state.tasks.map(x=>Number(x.id)))+1,status:"open",notes:""
          });
          mergeRecord(state.tasks,record);
        },
        t("Поручение сохранено","Task saved"),
        {requestKey: id ? null : form.dataset.requestKey}
      );
    } else if (form.id === "schedule-form") {
      ok = await write(
        "/api/settings",
        "PATCH",
        data,
        settings => Object.assign(state.settings,settings || data),
        t("Расписание обновлено", "Schedule updated"),
      );
    } else if (form.id === "feed-form") {
      ok = await write(
        "/api/feeds",
        "POST",
        { url: data.url },
        result => {
          const source=result || {...state.feed_catalog.find(x=>x.url===data.url),id:Date.now()};
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
  if (picker.name !== "reference" || !picker.files?.[0]) return;
  const note = picker.form.elements.context;
  const file = picker.files[0];
  const error = picker.form.querySelector(".form-error");
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
    workspace = null; mountedPage = null; lastContent = null;
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
    loading || refreshRunning
  )
    return;
  const sequence = revision;
  refreshRunning = true;
  try {
    const fresh = await request("/api/state");
    if (!pending && !sheet.open && sequence === revision) {
      state = fresh;
      render();
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
