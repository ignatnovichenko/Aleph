import { bridge, bridgeRequest } from "./bridge.js";
import { apiRequest, statePath } from "./transport.js";
import type { RequestOptions, WriteMethod } from "./transport.js";
import type { Computer, ComputerPairing } from "./types.js";
import type { CalendarEvent, ChatGPTConnection, ChatGPTPairing, Connections, Service, ProjectEntry, ProjectResult, ProjectResultDetail, ProjectSource, SourcePreview, Feed, ModelCatalog, ModelSummary, Project, ProjectSkills, SavedMemory, Settings, Skill, SkillDraft, SkillSummary, Task, WidgetState, WorkspaceState } from "./types.js";

export { bridge };
export type { RequestOptions, WriteMethod };

type Version = {id:number; name:string; parent:number|null; url:string; created:string};
type Reads = {
  [path: `/api/projects/${number}/versions`]: Version[];
  "/api/state": WorkspaceState;
  [statePath]: WorkspaceState;
  "/api/calendar.ics": Blob;
  "/api/chatgpt": ChatGPTConnection;
  "/api/computers": Computer[];
  "/api/models": ModelCatalog;
  "/api/memory": SavedMemory[];
  [path: `/api/tasks/${number}/result`]: {html: string | null; files?: Version[]; delivered_at: string | null};
  [path: `/api/connections/calendar/events?${string}`]: {events: CalendarEvent[]; more: boolean};
  [path: `/api/projects/${number}/entries`]: ProjectEntry[];
  [path: `/api/projects/${number}/results`]: ProjectResult[];
  [path: `/api/projects/${number}/results/${number}`]: ProjectResultDetail;
  [path: `/api/projects/${number}/sources`]: ProjectSource[];
  [path: `/api/projects/${number}/sources/${number}`]: SourcePreview;
  [path: `/api/projects/${number}/skills`]: ProjectSkills;
  "/api/skills": SkillSummary[];
  [path: `/api/skills/${number}`]: Skill;
  [path: `/api/widgets/${string}`]: WidgetState & { html: string };
};

export type Writes = {
  "/api/introduction": {show: boolean};
  "/api/projects": Project;
  "/api/projects/active": Project;
  [path: `/api/projects/${number}`]: Project;
  [path: `/api/projects/${number}/computer`]: Project;
  "/api/computers/pair": ComputerPairing;
  [path: `/api/computers/${number}`]: null;
  "/api/tasks": Task;
  [path: `/api/tasks/${string}`]: Task;
  "/api/settings": Settings;
  "/api/feeds": Feed;
  "/api/chatgpt": ChatGPTConnection;
  "/api/chatgpt/pair": ChatGPTPairing;
  "/api/models": ModelSummary;
  "/api/connections/drive/picker": {url: string};
  [path: `/api/projects/${number}/entries`]: ProjectEntry;
  [path: `/api/projects/${number}/entries/${number}`]: null;
  [path: `/api/projects/${number}/results/${number}`]: null;
  [path: `/api/projects/${number}/sources`]: ProjectSource;
  [path: `/api/projects/${number}/sources/file`]: ProjectSource;
  [path: `/api/projects/${number}/sources/${number}/transcript`]: ProjectSource;
  [path: `/api/projects/${number}/sources/${number}/reader`]: ProjectSource;
  [path: `/api/projects/${number}/sources/${number}`]: ProjectSource;
  [path: `/api/projects/${number}/skills`]: {selected: number[]};
  "/api/skills": Skill;
  "/api/skills/preview": SkillDraft;
  [path: `/api/skills/${number}`]: Skill;
  [path: `/api/feeds/${string}`]: null;
  [path: `/api/memory/${number}`]: null;
  [path: `/api/widgets/${string}`]: WidgetState;
} & { [path in `/api/connections/${Service}/start`]: {url: string} }
  & { [path in `/api/connections/${Service}`]: Connections };

const hash = new URLSearchParams(location.hash.slice(1));
export const signed = () =>
  window.Telegram?.WebApp?.initData || hash.get("tgWebAppData") || "";
export const demo = !bridge() && !signed() && !hash.has("tgWebAppVersion");
export const demoKey = "aleph-demo-v2";
export const catalog = [
  { title: "NASA", url: "https://www.nasa.gov/feed/" },
  { title: "Hacker News", url: "https://hnrss.org/frontpage" },
  { title: "arXiv · AI", url: "https://rss.arxiv.org/rss/cs.AI" },
  { title: "Хабр", url: "https://habr.com/ru/rss/articles/?fl=ru" },
  {
    title: "BBC News Русская служба",
    url: "https://feeds.bbci.co.uk/russian/rss.xml",
  },
];
export function request<Path extends keyof Reads>(path: Path): Promise<Reads[Path]>;
export function request<Path extends keyof Writes>(path: Path, method: WriteMethod, body?: object, options?: RequestOptions): Promise<Writes[Path]>;
export async function request(path: string, method: "GET" | WriteMethod = "GET", body?: object, options?: RequestOptions): Promise<unknown> {
  if (bridge()) return bridgeRequest(path, method, body);
  return apiRequest(path, signed(), method, body, options);
}

export async function workspaceState() {
  const startup = window.alephStartup;
  delete window.alephStartup;
  if (bridge()) return request("/api/state");
  if (startup?.initData === signed()) {
    const result = await startup.response;
    if ("error" in result) throw result.error;
    return result.state;
  }
  return request(statePath);
}
function sampleDate(days: number, hour: number, minute: number) {
  const d = new Date();
  d.setDate(d.getDate() + days);
  d.setHours(hour, minute, 0, 0);
  return d.toISOString();
}
export function demoState(ru: boolean): WorkspaceState {
  try {
    const value = JSON.parse(localStorage.getItem(demoKey) || "null") as WorkspaceState | null;
    if (
      value?.settings &&
      Array.isArray(value.tasks) &&
      Array.isArray(value.feeds)
    )
      return value;
  } catch {
    /* Storage may be disabled in webviews. */
  }
  const texts = ru
    ? [
        "Сверить макеты к встрече",
        "Посмотреть варианты перелёта",
        "Разобрать заметки после звонка",
        "Отправить материалы команде",
      ]
    : [
        "Review the designs",
        "Compare flight options",
        "Review notes from the call",
        "Share materials with the team",
      ];
  return {
    user: {
      first_name: ru ? "Гость" : "Guest",
      language_code: ru ? "ru" : "en",
    },
    settings: {
      proactive: true,
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
      quiet_start: "22:00",
      quiet_end: "08:00",
      digest_time: "09:00",
    },
    active_project_id: 0,
    projects: [{id:1,name:ru ? "Пилот" : "Pilot",context:ru ? "Подготовить первый прототип и собрать отзывы команды." : "Prepare the first prototype and gather feedback from the team."}],
    tasks: texts.map((title, i) => ({
      id: i + 1,
      title,
      project_id: i === 2 ? 1 : 0,
      status: i === 3 ? "done" : "open",
      notes: "",
      due_at: [
        new Date(Date.now() + 1000 * 60 * 75).toISOString(),
        sampleDate(1, 11, 0),
        null,
        null,
      ][i],
    })),
    feeds: [],
    feed_catalog: catalog,
    integrations: { rss: true, calendar: true },
  };
}
export function saveDemo(state: WorkspaceState) {
  try {
    localStorage.setItem(demoKey, JSON.stringify(state));
  } catch {
    /* Demo still works in memory. */
  }
}
const htmlEntities: Record<string, string> = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
export function escapeHtml(value: unknown): string {
  return String(value ?? "").replace(/[&<>"']/g, (character) => htmlEntities[character]);
}
const wallClockFormatters = new Map<string, Intl.DateTimeFormat>();
export function localDateTime(iso: string | number | Date | null, zone: string) {
  if (!iso) return "";
  let formatter = wallClockFormatters.get(zone);
  if (!formatter) {
    formatter = new Intl.DateTimeFormat("en-CA", {
      timeZone: zone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
    });
    wallClockFormatters.set(zone, formatter);
  }
  const parts = Object.fromEntries(
    formatter.formatToParts(new Date(iso)).map((p) => [p.type, p.value]),
  );
  return (
    parts.year +
    "-" +
    parts.month +
    "-" +
    parts.day +
    "T" +
    parts.hour +
    ":" +
    parts.minute
  );
}
export function zonedISO(value: string, zone: string) {
  if (!value) return null;
  const utcParts = Date.parse(value + ":00Z");
  let candidate = utcParts;
  for (let i = 0; i < 4; i++) {
    const wall = localDateTime(new Date(candidate), zone);
    if (wall === value) return new Date(candidate).toISOString();
    candidate += utcParts - Date.parse(wall + ":00Z");
  }
  throw new Error("invalid-date");
}
export function demoCalendar(tasks: Task[]) {
  const safe = (text: string) =>
    String(text)
      .replace(/\\/g, "\\\\")
      .replace(/\r?\n/g, "\\n")
      .replace(/[,;]/g, "\\$&");
  const date = (v: string | number) =>
    new Date(v)
      .toISOString()
      .replace(/[-:]/g, "")
      .replace(/\.\d{3}/, "");
  const events = tasks
    .filter((task): task is Task & { due_at: string } => Boolean(task.due_at) && task.status === "open")
    .flatMap((x) => [
      "BEGIN:VEVENT",
      "UID:" + x.id + "@aleph-demo",
      "DTSTAMP:" + date(Date.now()),
      "DTSTART:" + date(x.due_at),
      "SUMMARY:" + safe(x.title),
      "END:VEVENT",
    ]);
  return new Blob(
    [
      [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Aleph//Calendar//EN",
        ...events,
        "END:VCALENDAR",
        "",
      ].join("\r\n"),
    ],
    { type: "text/calendar;charset=utf-8" },
  );
}
