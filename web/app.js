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
const sorted = () =>
  [...state.tasks].sort(
    (a, b) =>
      (a.due_at ? Date.parse(a.due_at) : Infinity) -
        (b.due_at ? Date.parse(b.due_at) : Infinity) || b.id - a.id,
  );
const openTasks = () => sorted().filter((x) => x.status === "open");
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
  ["home", t("Обзор", "Overview")],
  ["tasks", t("Поручения", "Tasks")],
  ["settings", t("Настройки", "Settings")],
];
const isDark = () =>
  webapp()?.colorScheme
    ? webapp().colorScheme === "dark"
    : matchMedia("(prefers-color-scheme: dark)").matches;
function setTheme() {
  const dark = isDark();
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  document.querySelector("meta[name=theme-color]").content = dark
    ? "#11161e"
    : "#f5f6f9";
  try {
    webapp()?.setHeaderColor?.(dark ? "#11161e" : "#f5f6f9");
    webapp()?.setBackgroundColor?.(dark ? "#11161e" : "#f5f6f9");
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
      sheet.close();
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
function brand() {
  return `<button class="brand" data-page="home" aria-label="${t("Aleph, обзор", "Aleph, overview")}"><img alt="" src="./aleph.svg"><span class="brand-text">Aleph<small class="brand-credit">by Lab One</small></span></button>`;
}
function nav() {
  return navs()
    .map(
      ([id, label]) =>
        `<button class="nav-item ${page === id ? "active" : ""}" data-page="${id}" ${page === id ? 'aria-current="page"' : ""}>${icon(id)}<span>${label}</span></button>`,
    )
    .join("");
}
function chatButton() {
  return `<button class="button mobile-chat" data-action="chat">${icon("plane")} ${t("В чат", "Open chat")}</button>`;
}
function navigate(id) {
  if (!navs().some(([key]) => key === id)) return;
  page = id;
  render();
  window.scrollTo({ top: 0, behavior: "instant" });
  syncBack();
}
function render() {
  if (!state) return;
  const title = navs().find((x) => x[0] === page)[1];
  const date = new Intl.DateTimeFormat(locale(), {
    timeZone: zone(),
    weekday: "long",
    day: "numeric",
    month: "long",
  }).format(new Date());
  const firstName = String(state.user.first_name || "Telegram");
  app.innerHTML = `<aside class="sidebar">${brand()}<nav aria-label="${t("Навигация", "Navigation")}" class="nav">${nav()}</nav><div class="sidebar-footer"><span class="identity">${e(firstName.charAt(0))}</span><div class="who">${e(firstName)}<small>${demo ? t("Демо-режим", "Demo mode") : t("Telegram-аккаунт", "Telegram account")}</small></div></div></aside>
  <main class="workspace"><div class="main-inner"><div class="mobile-brand"><div class="actions">${brand()}${demo ? `<span class="demo-tag">${t("Демо", "Demo")}</span>` : ""}</div>${chatButton()}</div>
  <header class="page-head"><div><div class="dateline"><i class="date-dot"></i>${e(date)}</div><h1 class="page-title">${title}</h1></div><div class="header-actions">${chatButton()}</div></header>
  ${page === "home" ? overview() : page === "tasks" ? tasksPage() : settingsPage()}</div></main>
  <nav aria-label="${t("Навигация", "Navigation")}" class="dock">${nav()}</nav>`;
  app.setAttribute("aria-busy", "false");
  syncBack();
}
function taskRow(task) {
  const done = task.status === "done",
    late = !done && task.due_at && Date.parse(task.due_at) < Date.now();
  return `<div class="task-row ${done ? "done" : ""}"><button class="task-toggle" data-action="toggle-task" data-id="${e(task.id)}" aria-label="${e((done ? t("Вернуть в работу", "Reopen") : t("Завершить", "Complete")) + " · " + task.title)}" aria-pressed="${done}"><span class="ring">${done ? icon("check") : ""}</span></button><button class="task-body" data-action="edit-task" data-id="${e(task.id)}"><span class="task-title">${e(task.title)}</span><span class="task-date ${late ? "late" : ""}">${task.due_at ? icon("clock") : ""}${e(due(task.due_at))}</span></button><span class="arrow">${icon("arrow")}</span></div>`;
}
function taskEmpty(done = false) {
  return `<div class="empty"><strong>${done ? t("Завершённых поручений пока нет", "No completed tasks yet") : t("Пока всё свободно", "Nothing on the list yet")}</strong><p>${done ? t("Здесь появятся отмеченные галочкой задачи.", "Tasks you check off will appear here.") : t("Напиши Алефу в чате или добавь поручение здесь.", "Message Aleph in chat or add a task here.")}</p></div>`;
}
function addLine() {
  return `<button class="add-line" data-action="new-task">${icon("plus")}${t("Добавить поручение", "Add task")}</button>`;
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
  const tasks = openTasks(),
    focus = tasks[0];
  return `<article class="hero"><img class="hero-art" alt="" src="./orbit.webp" fetchpriority="high"><div class="hero-label">${icon("sparkle")}${focus ? t("В фокусе", "In focus") : "ALEPH"}</div><h2>${focus ? e(focus.title) : t("Что у тебя в планах?", "What’s on your mind?")}</h2><p class="hero-meta">${focus ? e(due(focus.due_at)) : t("Расскажи Алефу, о чём нужно позаботиться.", "Tell Aleph what you’d like to get done.")}</p><button class="hero-button" data-action="${focus ? "edit-task" : "chat"}" ${focus ? `data-id="${e(focus.id)}"` : ""}>${focus ? t("Открыть поручение", "View task") : t("Перейти в чат", "Open chat")}${icon("up")}</button></article>
  <div class="overview-columns"><section>${sectionHead(t("Мои поручения", "My tasks"), "tasks", t("Все", "View all"))}<div class="panel">${tasks.length ? tasks.slice(0, 4).map(taskRow).join("") : taskEmpty()}${addLine()}</div>${snapshotNotice()}</section>
  <div class="right-side"><section class="right-section">${sectionHead(t("Ритм дня", "Daily rhythm"))}<div class="panel summary-panel"><div class="summary-top">${glyph("activity", "violet")}<div><strong>${t("Проактивность", "Proactive updates")}</strong><small>${state.settings.proactive ? t("Включена", "On") : t("На паузе", "Paused")}</small></div>${switcher()}</div>
  <div class="stat-line"><span>${icon("sun")}${t("Утренний обзор", "Morning brief")}</span><b>${e(timeLabel(state.settings.digest_time))}</b></div><div class="stat-line"><span>${icon("moon")}${t("Тихие часы", "Quiet hours")}</span><b>${e(timeLabel(state.settings.quiet_start))} – ${e(timeLabel(state.settings.quiet_end))}</b></div></div></section>
  <section class="right-section">${sectionHead(t("Подключения", "Connections"))}<div class="panel">${integrationRows()}</div></section></div></div>`;
}
function snapshotNotice() {
  if (!bridge() || Number(state.snapshot_total || 0) <= state.tasks.length)
    return "";
  return `<p class="snapshot-note">${t("В этом плане показано", "This plan shows")} ${state.tasks.length} ${t("из", "of")} ${Number(state.snapshot_total)} ${t("поручений. Полный список можно получить в чате.", "tasks. Ask in chat for the full list.")}</p>`;
}
function tasksPage() {
  const tasks = sorted(),
    visible = tasks.filter((x) => x.status === filter),
    groups = new Map();
  for (const task of visible) {
    const key = due(task.due_at, false);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(task);
  }
  return `<div class="task-tools"><div class="segmented" aria-label="${t("Статус поручений", "Task status")}">${[
    ["open", t("В работе", "Open")],
    ["done", t("Готово", "Done")],
  ]
    .map(
      ([key, name]) =>
        `<button aria-pressed="${filter === key}" data-filter="${key}" class="segment ${filter === key ? "active" : ""}">${name}<small>${tasks.filter((x) => x.status === key).length}</small></button>`,
    )
    .join(
      "",
    )}</div><button class="button" data-action="new-task">${icon("plus")}${t("Добавить", "Add task")}</button></div>
  ${visible.length ? [...groups].map(([k, list]) => `<section class="task-group"><h2 class="group-label">${e(k)}</h2><div class="panel">${list.map(taskRow).join("")}</div></section>`).join("") : `<div class="panel">${taskEmpty(filter === "done")}</div>`}${snapshotNotice()}`;
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
  returnFocus = document.activeElement;
  modal = type;
  sheet.innerHTML = `<div class="sheet-body"><div class="sheet-head"><h2 id="sheet-heading">${title}</h2><button class="icon-button" aria-label="${t("Закрыть", "Close")}" data-action="close">${icon("close")}</button></div>${html}</div>`;
  sheet.setAttribute("aria-labelledby", "sheet-heading");
  if (!sheet.open) sheet.showModal();
  syncBack();
}
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
async function write(path, method, body, updateDemo, success) {
  if (pending) return false;
  pending = true;
  revision++;
  sheet
    .querySelectorAll("button,input,select")
    .forEach((x) => (x.disabled = true));
  try {
    if (demo) {
      updateDemo();
      saveDemo(state);
      render();
    } else {
      await request(path, method, body);
      if (!bridge()) await load();
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
    pending = false;
    sheet
      .querySelectorAll("button,input,select")
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
  if (sheet.open) sheet.close();
  message(t("Календарь подготовлен", "Calendar prepared"));
}
async function act(action, id) {
  if (pending && action !== "chat") return;
  switch (action) {
    case "close":
      sheet.close();
      break;
    case "chat":
      openChat();
      break;
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
          () => (state.settings.timezone = timezone),
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
        sheet.close();
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
        () => (item.status = status),
        status === "done"
          ? t("Поручение выполнено", "Task complete")
          : t("Поручение снова в работе", "Task reopened"),
      );
      break;
    }
    case "proactive": {
      const proactive = !state.settings.proactive;
      await write(
        "/api/settings",
        "PATCH",
        { proactive },
        () => (state.settings.proactive = proactive),
        proactive
          ? t("Проактивность включена", "Proactive updates on")
          : t("Проактивность на паузе", "Proactive updates paused"),
      );
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
  if (!el || el.disabled) return;
  if (el.dataset.page) navigate(el.dataset.page);
  else if (el.dataset.filter) {
    filter = el.dataset.filter;
    render();
  } else if (el.dataset.action) void act(el.dataset.action, el.dataset.id);
});
sheet.addEventListener("close", () => {
  modal = null;
  syncBack();
  if (returnFocus?.isConnected) returnFocus.focus({ preventScroll: true });
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
    sheet.close();
});
sheet.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (pending) return;
  const form = event.target,
    data = Object.fromEntries(new FormData(form));
  let ok = false;
  try {
    if (form.id === "task-form") {
      const title = data.title.trim();
      if (!title) {
        const input = form.elements.title;
        input.value = "";
        input.reportValidity();
        return;
      }
      const body = { title, due_at: zonedISO(data.due_at, zone()) };
      const id = form.dataset.id;
      ok = await write(
        "/api/tasks" + (id ? "/" + id : ""),
        id ? "PATCH" : "POST",
        body,
        () => {
          if (id) Object.assign(byId(id), body);
          else
            state.tasks.push({
              ...body,
              id: Math.max(0, ...state.tasks.map((x) => Number(x.id))) + 1,
              status: "open",
              notes: "",
            });
        },
        t("Поручение сохранено", "Task saved"),
      );
    } else if (form.id === "schedule-form") {
      ok = await write(
        "/api/settings",
        "PATCH",
        data,
        () => Object.assign(state.settings, data),
        t("Расписание обновлено", "Schedule updated"),
      );
    } else if (form.id === "feed-form") {
      ok = await write(
        "/api/feeds",
        "POST",
        { url: data.url },
        () => {
          const source = state.feed_catalog.find((x) => x.url === data.url);
          state.feeds.push({ ...source, id: Date.now() });
        },
        t("Источник подключён", "Source connected"),
      );
      if (ok) {
        feedsSheet();
        return;
      }
    }
    if (ok) sheet.close();
  } catch (error) {
    const node = form.querySelector(".form-error");
    if (node) node.textContent = errorMessage(error);
  }
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
    app.innerHTML = `<main class="failure"><img src="./aleph.svg" width="45" alt=""><h1>Aleph</h1><p>${e(errorMessage(error))}</p><div class="actions"><button class="button" data-action="retry">${t("Повторить", "Try again")}</button><button class="button primary" data-action="chat">${t("В чат", "Open chat")}</button></div></main>`;
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
    loading
  )
    return;
  const sequence = revision;
  try {
    const fresh = await request("/api/state");
    if (!pending && !sheet.open && sequence === revision) {
      state = fresh;
      render();
    }
  } catch {}
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
