export interface TelegramUser {
  id?: number;
  first_name?: string;
  language_code?: string;
}

interface TelegramWebApp {
  platform: string;
  colorScheme: "light" | "dark";
  initData: string;
  initDataUnsafe?: { user?: TelegramUser };
  ready(): void;
  expand(): void;
  close(): void;
  sendData(data: string): void;
  openLink?(url: string): void;
  openTelegramLink?(url: string): void;
  isVersionAtLeast?(version: string): boolean;
  isVerticalSwipesEnabled?: boolean;
  disableVerticalSwipes?(): void;
  enableVerticalSwipes?(): void;
  onEvent?(event: "themeChanged", callback: () => void): void;
  setHeaderColor?(color: string): void;
  setBackgroundColor?(color: string): void;
  setBottomBarColor?(color: string): void;
  BackButton?: { show(): void; hide(): void; onClick(callback: () => void): void };
  HapticFeedback?: { selectionChanged(): void; notificationOccurred(kind: "success"): void };
}

declare global {
  interface Window {
    Telegram?: { WebApp?: TelegramWebApp };
    TelegramWidget?: TelegramWidget;
  }
}

export type TaskStatus = "open" | "done";

export interface Task {
  id: number;
  title: string;
  notes: string;
  status: TaskStatus;
  due_at: string | null;
  remind_at?: string | null;
  source?: string;
  report_state?: "pending" | "failed" | "sent" | null;
  report_started_at?: string | null;
  report_has_result?: boolean | 0 | 1;
  project_id?: number;
}

export interface Project {
  id: number;
  name: string;
  context: string;
}

export interface SkillSummary {
  id: number;
  name: string;
  description: string;
  source_url: string;
}

export type SkillDraft = Omit<SkillSummary, "id"> & {instructions: string};
export type Skill = SkillSummary & {instructions: string};
export interface ProjectSkills {
  skills: SkillSummary[];
  selected: number[];
  limit: number;
}

export type Service = "calendar" | "notion" | "drive" | "gmail";
export type ConnectionStatus = "unavailable" | "disconnected" | "connected" | "reconnect";
export type Connections = Record<Service, ConnectionStatus>;
export interface CalendarEvent {
  title: string;
  start: { dateTime?: string; date?: string };
  end: { dateTime?: string; date?: string };
  url: string;
  location: string;
}
export interface ProjectEntry {
  id: number;
  kind: "decision" | "question" | "material";
  text: string;
  source: string;
  created_at: string;
}

export interface ProjectResult {
  id: number;
  title: string;
  saved_at: string;
}
export type ProjectResultDetail = ProjectResult & {html: string; chat_url: string};

export interface ProjectSource {
  id: number;
  kind: "web" | "youtube" | "notion" | "drive" | "file";
  title: string;
  url: string;
  status: "ready" | "partial" | "link";
  reason: string;
  size: number;
  updated_at: string;
}

export type SourcePreview = ProjectSource & {content: string};

export type ThemeMode = "auto" | "white" | "dark";
export type ReplyStyle = "short" | "balanced" | "detailed";

export interface Settings {
  theme?: ThemeMode;
  reply_style?: ReplyStyle;
  assistant_instructions?: string;
  personal_memory?: boolean;
  daily_digest?: boolean;
  proactive: boolean;
  timezone: string;
  timezone_confirmed?: boolean;
  quiet_start: string;
  quiet_end: string;
  digest_time?: string;
}

export interface SavedMemory {
  id: number;
  text: string;
}

export interface FeedSource {
  title: string;
  url: string;
}

export interface Feed extends FeedSource {
  id: number;
}

export interface Routine {
  title: string;
  days: string;
  time: string;
  active: boolean | 0 | 1;
}

export interface WorkspaceState {
  chatgpt_connection?: ChatGPTConnection;
  user: TelegramUser;
  tasks: Task[];
  settings: Settings;
  feeds: Feed[];
  feed_catalog: FeedSource[];
  projects?: Project[];
  active_project_id?: number;
  routines?: Routine[];
  snapshot_total?: number;
  integrations?: { rss: boolean; calendar: boolean };
  model?: ModelSummary;
  model_configurable?: boolean;
  connections?: Connections;
}

export type ModelProvider = "chatgpt" | "opencode";

export interface ModelChoice {
  provider: ModelProvider;
  model: string;
  name: string;
  reasoning: string;
}

export interface ModelSummary {
  active: ModelChoice;
  last: ModelChoice | null;
}

export interface ProviderModel {
  id: string;
  name: string;
  levels: string[];
  default?: string;
  images: boolean;
  training?: boolean;
}

export interface ModelCatalog extends ModelSummary {
  providers: Record<ModelProvider, { status: "ready" | "disconnected" | "unavailable"; models: ProviderModel[] }>;
  selections: Partial<Record<ModelProvider, ModelChoice>>;
}

export interface ChatGPTConnection {
  available: boolean;
  connected: boolean;
  mode: "self_hosted";
  needs_reauth?: boolean;
  email?: string;
  model?: string;
  model_settings?: ModelSummary;
  revoked?: boolean;
}

export interface ChatGPTPairing {
  code: string;
  expires_in: number;
}

export interface WidgetState {
  chat_data: string;
}

export interface TelegramWidget {
  chatData: string | null;
  ready(): void;
  onEvent(kind: "chatDataChanged", callback: () => void): void;
  request(action: string): Promise<{ result: string }>;
}

interface WidgetItem {
  label: string;
  detail?: string;
}

export type WidgetSpec = { title: string } & (
  | { kind: "checklist"; items: WidgetItem[] }
  | { kind: "compare"; items: WidgetItem[] }
  | { kind: "chart"; items: (WidgetItem & { value: number })[]; unit: string }
);

export type Theme = "light" | "dark";

export interface WidgetFrameConfig {
  originKey: string;
  chatData: string;
}

export type WidgetFrameRequest = { originKey: string } & (
  | { type: "size"; height: number }
  | { type: "action"; id: number; action: string }
);

export type WidgetFrameReply = { originKey: string } & (
  | { type: "theme"; scheme: Theme }
  | { type: "answer"; id: number; ok: boolean; chatData?: string }
);
