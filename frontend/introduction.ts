import { icon } from "./icons.js";

type Translate = (ru: string, en: string) => string;
type Chapter = { name: string; title: string; description: string; art: string; sample: string };

export function showIntroduction(t: Translate, onClose: () => void) {
  const dialog = document.createElement("dialog");
  dialog.className = "intro";
  dialog.autofocus = true;
  dialog.setAttribute("aria-label", t("Знакомство с Алефом", "Meet Aleph"));
  const chapters: Chapter[] = [
    {
      name: t("Чат", "Chat"),
      title: t("Просто скажи", "Start with a message"),
      description: t("Напиши Алефу в Telegram или отправь голосовое. Попроси разобрать идею, сравнить варианты или подготовить документ.",
        "Message Aleph on Telegram or send a voice note. Talk through an idea, compare your options, or ask for a document."),
      art: "voice",
      sample: `<div class="intro-voice"><div class="intro-wave">${[5,9,15,8,19,24,13,18,7,12,21,15,9,17,11,5,8,14,19,9,5].map(h => `<i style="height:${h}px"></i>`).join("")}<time>0:08</time></div><span>${t("Разбери запись встречи", "Summarize the meeting")}</span></div>`,
    },
    {
      name: t("Проекты", "Projects"),
      title: t("Собери контекст", "Give it context"),
      description: t("Сложи в проект заметки, ссылки и файлы. Когда выберешь его, Алеф учтёт эти материалы в разговоре.",
        "Keep notes, links, and files in a project. Select it and Aleph can use those materials in your conversation."),
      art: "project",
      sample: `<div class="intro-file">${icon("projects")}<span>${t("Запуск продукта", "Product launch")}<small>${t("План запуска.pdf", "Launch brief.pdf")}</small></span></div>`,
    },
    {
      name: t("Задачи", "Tasks"),
      title: t("Оставь поручение", "Leave it with Aleph"),
      description: t("Попроси собрать обзор рынка к завтрашним девяти. Ход работы виден в задачах, а готовый разбор придёт в чат.",
        "Ask for a market brief by nine tomorrow. Follow the work in Tasks and get the finished brief in your chat."),
      art: "task",
      sample: `<div class="intro-result"><span class="intro-tick">${icon("check")}</span><span>${t("Обзор рынка", "Market brief")}<small>${t("Готово к 09:00", "Ready by 09:00")}</small></span></div>`,
    },
  ];
  dialog.innerHTML = `<header class="intro-top"><span class="intro-brand">Aleph</span>
    <button class="intro-skip" type="button">${t("Пропустить", "Skip")}</button></header>
    <div class="intro-rail" tabindex="0" aria-label="${t("Карточки знакомства", "Introduction cards")}">
    ${chapters.map((c, i) => `<article class="intro-page" aria-label="${i + 1} / ${chapters.length}">
      <div class="intro-art intro-art-${c.art}" aria-hidden="true"><div class="intro-orbit"></div>${c.sample}</div>
      <div class="intro-copy"><p class="intro-section">${c.name}</p><h2>${c.title}</h2><p class="intro-description">${c.description}</p></div>
    </article>`).join("")}</div>
    <footer class="intro-footer"><div class="intro-progress" aria-hidden="true">${chapters.map(() => "<i></i>").join("")}</div>
    <div class="intro-actions"><button class="intro-prev" type="button" aria-label="${t("Назад", "Back")}">${icon("arrow")}</button>
    <button class="intro-next" type="button"><span></span>${icon("arrow")}</button></div></footer>
    <span class="intro-sr" role="status" aria-live="polite"></span>`;
  document.body.append(dialog);
  const rail = dialog.querySelector<HTMLElement>(".intro-rail")!;
  const pages = Array.from(rail.children) as HTMLElement[];
  const steps = dialog.querySelectorAll(".intro-progress i");
  const next = dialog.querySelector<HTMLButtonElement>(".intro-next")!;
  const prev = dialog.querySelector<HTMLButtonElement>(".intro-prev")!;
  const skip = dialog.querySelector<HTMLButtonElement>(".intro-skip")!;
  const announce = dialog.querySelector<HTMLElement>(".intro-sr")!;
  let current = -1;
  function sync(index: number) {
    if (index === current) return;
    current = index;
    pages.forEach((page, i) => {
      page.setAttribute("aria-hidden", String(i !== index));
      steps[i].classList.toggle("active", i === index);
    });
    prev.disabled = index === 0;
    next.querySelector("span")!.textContent = index === pages.length - 1 ? t("Открыть Алеф", "Open Aleph") : t("Дальше", "Next");
    announce.textContent = `${index + 1} / ${pages.length} · ${chapters[index].name}`;
  }
  function move(index: number) {
    if (index < 0 || index >= pages.length) return;
    rail.scrollTo({left: index * rail.clientWidth, behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth"});
  }
  function back() {
    if (current > 0) move(current - 1);
    else dialog.close();
  }
  rail.addEventListener("scroll", () => {
    if (rail.clientWidth) sync(Math.max(0, Math.min(pages.length - 1, Math.round(rail.scrollLeft / rail.clientWidth))));
  }, {passive: true});
  next.onclick = () => current === pages.length - 1 ? dialog.close() : move(current + 1);
  prev.onclick = back;
  skip.onclick = () => dialog.close();
  dialog.addEventListener("keydown", event => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    move(current + (event.key === "ArrowLeft" ? -1 : 1));
  });
  dialog.addEventListener("close", () => { dialog.remove(); onClose(); }, {once: true});
  dialog.showModal();
  sync(0);
  return back;
}
