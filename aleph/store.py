"""SQLite-backed private workspaces for Telegram users."""
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .feeds import CATALOG
from . import capture, connections, memory, projects, reports, skills
from .routines import day_names

UTC = timezone.utc
DEFAULT_REMINDER = object()
REPLY_STYLES = ("short", "balanced", "detailed")
SETTING_FLAGS = ("proactive", "daily_digest", "personal_memory")
INSTRUCTIONS_LIMIT = 600
TASK_REPORT_FIELDS = """(SELECT state FROM scheduled_reports WHERE task_id=tasks.id) AS report_state,
    (SELECT started_at FROM scheduled_reports WHERE task_id=tasks.id) AS report_started_at,
    (SELECT revision FROM scheduled_reports WHERE task_id=tasks.id) AS report_revision,
    EXISTS(SELECT 1 FROM scheduled_reports WHERE task_id=tasks.id AND result IS NOT NULL) AS report_has_result"""


def timestamp(dt=None):
    return (dt or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_due(value, zone):
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("Укажи дату и время в формате ISO.")
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            local = dt.replace(tzinfo=ZoneInfo(zone))
            if local.astimezone(UTC).astimezone(local.tzinfo).replace(tzinfo=None) != dt:
                raise ValueError
            dt = local
        return timestamp(dt)
    except (ValueError, OverflowError):
        raise ValueError("Проверь дату, время и часовой пояс.") from None


def bounded_text(value, limit, *, required=False):
    if not isinstance(value, str) or len(value) > limit or "\x00" in value:
        raise ValueError(f"Ожидается текст до {limit} символов.")
    value = value.strip()
    if required and not value:
        raise ValueError("Введи название задачи.")
    return value


class Store:
    def __init__(self, path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, isolation_level=None, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY, first_name TEXT NOT NULL DEFAULT '',
                language_code TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                proactive INTEGER NOT NULL DEFAULT 1, timezone TEXT NOT NULL DEFAULT 'UTC',
                quiet_start TEXT NOT NULL DEFAULT '22:00', quiet_end TEXT NOT NULL DEFAULT '08:00',
                digest_time TEXT NOT NULL DEFAULT '09:00', blocked_until TEXT,
                theme TEXT NOT NULL DEFAULT 'auto' CHECK(theme IN ('auto','white','dark'))
            );
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
                title TEXT NOT NULL, notes TEXT NOT NULL, due_at TEXT,
                status TEXT NOT NULL CHECK(status IN ('open','done')) DEFAULT 'open',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, request_key TEXT,
                UNIQUE(user_id, request_key)
            );
            CREATE INDEX IF NOT EXISTS tasks_user ON tasks(user_id, status, due_at);
            CREATE INDEX IF NOT EXISTS tasks_due ON tasks(status, due_at);
            CREATE TABLE IF NOT EXISTS feeds (
                id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
                url TEXT NOT NULL, title TEXT NOT NULL, UNIQUE(user_id, url)
            );
            CREATE TABLE IF NOT EXISTS feed_cache (
                url TEXT PRIMARY KEY, items TEXT NOT NULL, refreshed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
                role TEXT NOT NULL, content TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS history_user ON history(user_id, id);
            CREATE TABLE IF NOT EXISTS runtime_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS model_settings (
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                provider TEXT NOT NULL DEFAULT '', selections TEXT NOT NULL DEFAULT '{}', last_reply TEXT
            );
            CREATE TABLE IF NOT EXISTS routines (
                id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
                title TEXT NOT NULL, kind TEXT NOT NULL, time TEXT NOT NULL, days TEXT NOT NULL,
                city TEXT NOT NULL, country_code TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
                deleted INTEGER NOT NULL DEFAULT 0, active_since TEXT NOT NULL, request_key TEXT,
                UNIQUE(user_id,request_key)
            );
            CREATE INDEX IF NOT EXISTS routines_user ON routines(user_id,deleted,active);
            CREATE TABLE IF NOT EXISTS deliveries (
                key TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
                kind TEXT NOT NULL, task_id INTEGER, due_at TEXT, body TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', available_at TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS deliveries_pending ON deliveries(status, available_at);
        """)

        if "routine_id" not in {row["name"] for row in self.db.execute("PRAGMA table_info(deliveries)")}:
            self.db.execute("ALTER TABLE deliveries ADD COLUMN routine_id INTEGER REFERENCES routines(id)")

        user_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(users)")}
        for name, definition in {
            "theme": "TEXT NOT NULL DEFAULT 'auto' CHECK(theme IN ('auto','white','dark'))",
            "reply_style": "TEXT NOT NULL DEFAULT 'balanced' CHECK(reply_style IN ('short','balanced','detailed'))",
            "assistant_instructions": "TEXT NOT NULL DEFAULT ''",
            "personal_memory": "INTEGER NOT NULL DEFAULT 1",
            "daily_digest": "INTEGER NOT NULL DEFAULT 1",
            "timezone_confirmed": "INTEGER NOT NULL DEFAULT 0",
        }.items():
            if name not in user_columns:
                self.db.execute(f"ALTER TABLE users ADD COLUMN {name} {definition}")

        memory.initialize(self.db)
        projects.initialize(self.db)
        skills.initialize(self.db)
        connections.initialize(self.db)
        capture.initialize(self.db)
        reports.initialize(self.db)
        task_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(tasks)")}
        if "remind_at" not in task_columns:
            self.db.executescript("""
                BEGIN IMMEDIATE;
                ALTER TABLE tasks ADD COLUMN remind_at TEXT;
                UPDATE tasks SET remind_at=due_at;
                COMMIT;
            """)
        if "source" not in task_columns:
            self.db.execute("ALTER TABLE tasks ADD COLUMN source TEXT NOT NULL DEFAULT ''")
        self.db.execute("CREATE INDEX IF NOT EXISTS tasks_reminder ON tasks(status,remind_at)")

    def close(self):
        self.db.close()

    def ensure_user(self, user_id, first_name="", language_code=""):
        uid = int(user_id)
        if uid <= 0:
            raise ValueError("Нужен личный чат в Telegram.")
        self.db.execute("""INSERT INTO users(id, first_name, language_code, created_at) VALUES (?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET first_name=COALESCE(NULLIF(excluded.first_name,''),users.first_name),
            language_code=COALESCE(NULLIF(excluded.language_code,''),users.language_code)
            WHERE (excluded.first_name<>'' AND excluded.first_name<>users.first_name)
               OR (excluded.language_code<>'' AND excluded.language_code<>users.language_code)""",
            (uid, (first_name or "")[:128], (language_code or "")[:32], timestamp()))
        return self.get_user(uid)

    def get_user(self, uid):
        row = self.db.execute("SELECT id,first_name,language_code FROM users WHERE id=?", (uid,)).fetchone()
        if row is None:
            raise KeyError("Пользователь не найден.")
        return dict(row)

    def get_settings(self, uid):
        row = self.db.execute("""SELECT proactive,timezone,quiet_start,quiet_end,digest_time,theme,
            reply_style,assistant_instructions,personal_memory,daily_digest,timezone_confirmed
            FROM users WHERE id=?""", (uid,)).fetchone()
        if row is None:
            raise KeyError("Пользователь не найден.")
        return {**dict(row), **{name: bool(row[name]) for name in SETTING_FLAGS},
                "timezone_confirmed": bool(row["timezone_confirmed"] or row["timezone"] != "UTC")}

    def patch_settings(self, uid, fields):
        allowed = {*SETTING_FLAGS, "quiet_start", "quiet_end", "digest_time", "timezone",
                   "theme", "reply_style", "assistant_instructions"}
        if not fields or fields.keys() - allowed:
            raise ValueError("Неизвестная настройка.")
        fields = dict(fields)
        for key, value in fields.items():
            if key in SETTING_FLAGS:
                if type(value) is not bool:
                    raise ValueError("Для переключателя ожидается true или false.")
            elif key == "reply_style":
                if value not in REPLY_STYLES:
                    raise ValueError("Выбери стиль ответов.")
            elif key == "assistant_instructions":
                fields[key] = bounded_text(value, INSTRUCTIONS_LIMIT)
            elif key == "theme":
                if value not in ("auto", "white", "dark"):
                    raise ValueError("Выбери фон мини-приложения.")
            elif key == "timezone":
                try:
                    if not isinstance(value, str) or len(value) > 80:
                        raise ValueError
                    ZoneInfo(value)
                except (ZoneInfoNotFoundError, ValueError):
                    raise ValueError("Неизвестный часовой пояс.") from None
            elif not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
                raise ValueError("Укажи время в формате ЧЧ:ММ.")
        if "timezone" in fields:
            fields["timezone_confirmed"] = 1
        sql = ",".join(key + "=?" for key in fields)
        self.db.execute(f"UPDATE users SET {sql} WHERE id=?", (*fields.values(), uid))
        return self.get_settings(uid)

    def list_tasks(self, uid, include_done=True, *, project_id=None):
        condition = "" if include_done else " AND status='open'"
        args = (uid,)
        if project_id is not None:
            condition += " AND project_id=?"
            args += (project_id,)
        rows = self.db.execute(f"""SELECT id,title,notes,due_at,status,created_at,updated_at,project_id,remind_at,source,
            {TASK_REPORT_FIELDS}
            FROM tasks WHERE user_id=?""" + condition + """
            ORDER BY status='done', due_at IS NULL, due_at, id DESC""", args)
        return [dict(row) for row in rows]

    def get_task(self, uid, task_id):
        row = self.db.execute(f"""SELECT id,title,notes,due_at,status,created_at,updated_at,project_id,remind_at,source,
            {TASK_REPORT_FIELDS}
            FROM tasks WHERE user_id=? AND id=?""", (uid, task_id)).fetchone()
        if row is None:
            raise KeyError("Задача не найдена.")
        return dict(row)

    def save_task_timezone(self, uid, fields, save):
        if "timezone" not in fields:
            return save(fields)
        fields = dict(fields)
        zone = fields.pop("timezone")
        self.db.execute("SAVEPOINT task_timezone")
        try:
            self.patch_settings(uid, {"timezone": zone})
            return save(fields)
        except BaseException:
            self.db.execute("ROLLBACK TO task_timezone")
            raise
        finally:
            self.db.execute("RELEASE task_timezone")

    def create_task(self, uid, title, due_at=None, notes="", request_key=None, *, project_id=0, remind_at=DEFAULT_REMINDER, source=""):
        projects.get(self, uid, project_id)
        title = bounded_text(title, 300, required=True)
        notes = bounded_text(notes, 4000)
        zone = self.get_settings(uid)["timezone"]
        due_at = parse_due(due_at, zone)
        remind_at = due_at if remind_at is DEFAULT_REMINDER else parse_due(remind_at, zone)
        source = bounded_text(source, 900)
        now = timestamp()
        if request_key:
            row = self.db.execute("SELECT id FROM tasks WHERE user_id=? AND request_key=?", (uid, request_key)).fetchone()
            if row:
                return self.get_task(uid, row["id"])
        if self.db.execute("SELECT COUNT(*) FROM tasks WHERE user_id=? AND status='open'", (uid,)).fetchone()[0] >= 500:
            raise ValueError("Достигнут лимит в 500 открытых задач.")
        self.db.execute("""INSERT INTO tasks(user_id,title,notes,due_at,created_at,updated_at,request_key,project_id,remind_at,source)
            VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id,request_key) DO NOTHING""",
            (uid, title, notes, due_at, now, now, request_key, project_id, remind_at, source))
        if request_key:
            task_id = self.db.execute("SELECT id FROM tasks WHERE user_id=? AND request_key=?", (uid, request_key)).fetchone()[0]
        else:
            task_id = self.db.execute("SELECT last_insert_rowid()").fetchone()[0]
        return self.get_task(uid, task_id)

    def update_task(self, uid, task_id, fields):
        old = self.get_task(uid, task_id)
        fields = dict(fields)
        if not fields or fields.keys() - {"title", "notes", "status", "due_at", "remind_at", "project_id"}:
            raise ValueError("Неизвестное поле задачи.")
        if "title" in fields:
            fields["title"] = bounded_text(fields["title"], 300, required=True)
        if "notes" in fields:
            fields["notes"] = bounded_text(fields["notes"], 4000, required=bool(old["report_state"]))
        if "status" in fields and fields["status"] not in ("open", "done"):
            raise ValueError("Неизвестный статус задачи.")
        zone = self.get_settings(uid)["timezone"]
        if "project_id" in fields:
            projects.get(self, uid, fields["project_id"])
        if "due_at" in fields:
            fields["due_at"] = parse_due(fields["due_at"], zone)
            if "remind_at" not in fields and old["due_at"] == old["remind_at"]:
                fields["remind_at"] = fields["due_at"]
        if "remind_at" in fields:
            fields["remind_at"] = parse_due(fields["remind_at"], zone)
        if old["report_state"]:
            if old["report_state"] == "sent" and fields != {"status": "done"}:
                raise ValueError("Этот ответ уже доставлен. Для повтора создай новое поручение.")
            if "due_at" in fields and fields["due_at"] is None:
                raise ValueError("У поручения Алефу должно быть время запуска.")
            if fields.get("remind_at"):
                raise ValueError("Для поручения Алефу укажи время запуска.")
            if "due_at" in fields and fields["due_at"] != old["due_at"] and fields["due_at"] <= timestamp():
                raise ValueError("Укажи будущее время запуска.")
        report_changed = old["report_state"] in ("pending", "failed") and any(
            key in fields and fields[key] != old[key] for key in ("status", "due_at", "title", "notes", "project_id")
        )
        fields["updated_at"] = timestamp()
        sql = ",".join(key + "=?" for key in fields)
        self.db.execute("SAVEPOINT edit_task")
        try:
            self.db.execute(f"UPDATE tasks SET {sql} WHERE id=? AND user_id=?", (*fields.values(), task_id, uid))
            if old["report_state"] == "failed" and fields.get("due_at", "") > timestamp():
                self.db.execute("UPDATE scheduled_reports SET state='pending' WHERE task_id=?", (task_id,))
            if report_changed:
                self.db.execute("""UPDATE scheduled_reports SET started_at=NULL, revision=revision+1
                    WHERE task_id=?""", (task_id,))
        except BaseException:
            self.db.execute("ROLLBACK TO edit_task")
            raise
        finally:
            self.db.execute("RELEASE edit_task")
        return self.get_task(uid, task_id)

    def list_routines(self, uid):
        rows = self.db.execute("""SELECT id,title,kind,time,days,city,country_code,active,project_id FROM routines
            WHERE user_id=? AND deleted=0 ORDER BY time,id""", (uid,))
        return [{**dict(row), "active":bool(row["active"])} for row in rows]

    def get_routine(self, uid, routine_id):
        row = self.db.execute("""SELECT id,title,kind,time,days,city,country_code,active,project_id FROM routines
            WHERE id=? AND user_id=? AND deleted=0""", (routine_id,uid)).fetchone()
        if not row:
            raise KeyError("Расписание не найдено.")
        return {**dict(row), "active":bool(row["active"])}

    def create_routine(self, uid, title, kind, time, days="daily", city="", country_code="", request_key=None, *, project_id=0):
        projects.get(self, uid, project_id)
        title = bounded_text(title,300,required=True)
        if kind not in ("weather","agenda","news","reminder"):
            raise ValueError("Выбери прогноз погоды, план, новости или напоминание.")
        if not isinstance(time,str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d",time):
            raise ValueError("Укажи время в формате ЧЧ:ММ.")
        days = ",".join(day_names(days))
        city = bounded_text(city,100,required=kind == "weather")
        if not isinstance(country_code,str) or (country_code and not re.fullmatch("[a-zA-Z]{2}", country_code)):
            raise ValueError("Код страны должен состоять из двух букв.")
        if request_key:
            existing = self.db.execute("SELECT id FROM routines WHERE user_id=? AND request_key=?", (uid,request_key)).fetchone()
            if existing:
                return self.get_routine(uid,existing["id"])
        if len(self.list_routines(uid)) >= 25:
            raise ValueError("Достигнут лимит в 25 расписаний.")
        self.db.execute("""INSERT INTO routines(user_id,title,kind,time,days,city,country_code,active_since,request_key,project_id)
            VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id,request_key) DO NOTHING""",
            (uid,title,kind,time,days,city,country_code.upper(),timestamp(),request_key,project_id))
        if request_key:
            rid = self.db.execute("SELECT id FROM routines WHERE user_id=? AND request_key=?", (uid,request_key)).fetchone()[0]
        else:
            rid = self.db.execute("SELECT last_insert_rowid()").fetchone()[0]
        return self.get_routine(uid,rid)

    def set_routine_active(self, uid, routine_id, active):
        if type(active) is not bool:
            raise ValueError("Статус должен быть true или false.")
        row = self.get_routine(uid,routine_id)
        if row["active"] != active:
            self.db.execute("UPDATE routines SET active=?,active_since=? WHERE id=? AND user_id=?",
                            (active,timestamp(),routine_id,uid))
        return self.get_routine(uid,routine_id)

    def delete_routine(self, uid, routine_id):
        self.get_routine(uid,routine_id)
        self.db.execute("UPDATE routines SET deleted=1,active=0 WHERE id=? AND user_id=?", (routine_id,uid))

    def list_feeds(self, uid):
        return [dict(row) for row in self.db.execute("SELECT id,url,title FROM feeds WHERE user_id=? ORDER BY id", (uid,))]

    def add_feed(self, uid, url):
        if not isinstance(url, str) or url not in CATALOG:
            raise ValueError("Выбери источник из каталога.")
        self.db.execute("INSERT OR IGNORE INTO feeds(user_id,url,title) VALUES(?,?,?)", (uid, url, CATALOG[url]))
        return dict(self.db.execute("SELECT id,url,title FROM feeds WHERE user_id=? AND url=?", (uid, url)).fetchone())

    def delete_feed(self, uid, feed_id):
        if not self.db.execute("DELETE FROM feeds WHERE id=? AND user_id=?", (feed_id, uid)).rowcount:
            raise KeyError("Источник не найден.")

    def feed_items(self, uid):
        results = []
        for row in self.db.execute("""SELECT f.title,f.url,c.items,c.refreshed_at FROM feeds f
            JOIN feed_cache c ON f.url=c.url WHERE f.user_id=? ORDER BY f.id""", (uid,)):
            results.append({"title": row["title"], "url": row["url"], "items": json.loads(row["items"]), "refreshed_at":row["refreshed_at"]})
        return results

    def history(self, uid, limit=16, *, project_id=0):
        rows = self.db.execute("SELECT role,content FROM history WHERE user_id=? AND project_id=? ORDER BY id DESC LIMIT ?", (uid, project_id, limit))
        return list(reversed([dict(row) for row in rows]))

    def add_history(self, uid, role, content, *, project_id=0):
        if role not in ("user", "assistant"):
            raise ValueError("Invalid history role")
        self.db.execute("INSERT INTO history(user_id,role,content,project_id) VALUES(?,?,?,?)", (uid, role, content[:18000], project_id))
        self.db.execute("""DELETE FROM history WHERE user_id=? AND project_id=? AND id < (SELECT id FROM history
            WHERE user_id=? AND project_id=? ORDER BY id DESC LIMIT 1 OFFSET 79)""", (uid, project_id, uid, project_id))

    def get_offset(self):
        row = self.db.execute("SELECT value FROM runtime_state WHERE key='telegram_offset'").fetchone()
        return int(row[0]) if row else 0

    def set_offset(self, next_offset):
        self.db.execute("""INSERT INTO runtime_state(key,value) VALUES('telegram_offset',?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value WHERE CAST(excluded.value AS INTEGER)>CAST(value AS INTEGER)""", (str(next_offset),))
