"""Project context lives with Aleph's user-scoped data, outside Hermes."""
import re
import unicodedata


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS projects (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          name TEXT NOT NULL, normalized TEXT NOT NULL, context TEXT NOT NULL DEFAULT '',
          UNIQUE(user_id, normalized)
        );
        CREATE TABLE IF NOT EXISTS project_facts (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          text TEXT NOT NULL, normalized TEXT NOT NULL,
          UNIQUE(project_id, normalized)
        );
        CREATE TABLE IF NOT EXISTS project_entries (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          kind TEXT NOT NULL CHECK(kind IN ('decision','question','material')),
          text TEXT NOT NULL,
          source TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL,
          request_key TEXT,
          UNIQUE(user_id,request_key)
        );
        CREATE INDEX IF NOT EXISTS project_entries_scope ON project_entries(user_id,project_id,id);
    """)
    for table, column in (("users", "active_project_id"), ("tasks", "project_id"),
                          ("history", "project_id"), ("routines", "project_id")):
        if column not in {r["name"] for r in db.execute(f"PRAGMA table_info({table})")}:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0")
    db.execute("CREATE INDEX IF NOT EXISTS history_project ON history(user_id,project_id,id)")
    db.execute("CREATE INDEX IF NOT EXISTS tasks_project ON tasks(user_id,project_id,status)")


def normalized(name):
    return unicodedata.normalize("NFC", " ".join(name.split())).casefold()


def get(store, uid, project_id):
    if type(project_id) is not int or not 0 <= project_id < 2**63:
        raise ValueError("Некорректный проект.")
    if not project_id:
        return {"id": 0, "name": "", "context": ""}
    row = store.db.execute("SELECT id,name,context FROM projects WHERE user_id=? AND id=?", (uid, project_id)).fetchone()
    if not row:
        raise KeyError("Проект не найден.")
    return dict(row)


def active(store, uid):
    return store.db.execute("SELECT active_project_id FROM users WHERE id=?", (uid,)).fetchone()[0]


def choose(store, uid, project_id):
    chosen = get(store, uid, project_id)
    store.db.execute("UPDATE users SET active_project_id=? WHERE id=?", (project_id, uid))
    return chosen


def list_projects(store, uid):
    return [dict(row) for row in store.db.execute("""
        SELECT p.id,p.name,p.context,
          (SELECT count(*) FROM tasks t WHERE t.user_id=p.user_id AND t.project_id=p.id AND t.status='open') AS open_tasks
        FROM projects p WHERE p.user_id=? ORDER BY p.id DESC
    """, (uid,))]


def project_fields(name, context):
    from .store import bounded_text
    name = " ".join(bounded_text(name, 64, required=True).split())
    return name, normalized(name), bounded_text(context, 10000)


def create(store, uid, name, context=""):
    name, key, context = project_fields(name, context)
    existing = store.db.execute("SELECT id FROM projects WHERE user_id=? AND normalized=?", (uid, key)).fetchone()
    if existing:
        return get(store, uid, existing["id"])
    if store.db.execute("SELECT count(*) FROM projects WHERE user_id=?", (uid,)).fetchone()[0] >= 40:
        raise ValueError("Достигнут лимит в 40 проектов.")
    row = store.db.execute("INSERT INTO projects(user_id,name,normalized,context) VALUES(?,?,?,?) RETURNING id",
                           (uid, name, key, context)).fetchone()
    return get(store, uid, row["id"])


def edit(store, uid, project_id, fields):
    if not project_id or not fields or fields.keys() - {"name", "context"}:
        raise ValueError("Проверь параметры проекта.")
    old = get(store, uid, project_id)
    name, key, context = project_fields(fields.get("name", old["name"]), fields.get("context", old["context"]))
    duplicate = store.db.execute("SELECT 1 FROM projects WHERE user_id=? AND normalized=? AND id<>?",
                                 (uid, key, project_id)).fetchone()
    if duplicate:
        raise ValueError("Такое имя уже используется.")
    store.db.execute("UPDATE projects SET name=?,normalized=?,context=? WHERE user_id=? AND id=?",
                     (name, key, context, uid, project_id))
    return get(store, uid, project_id)


def chat_command(store, uid, text, russian=True):
    value = (text or "").strip()
    command = value.casefold().rstrip(".!?")
    if command in {"покажи проекты", "мои проекты", "list projects", "show my projects"}:
        rows = list_projects(store, uid)
        current = active(store, uid)
        if not rows:
            return ("У тебя ещё нет проектов. Напиши «создай проект Личный сайт»." if russian
                    else "You have no projects yet. Try “create project Personal site”.")
        return ("Твои проекты" if russian else "Your projects") + "\n" + "\n".join(
            ("● " if row["id"] == current else "• ") + row["name"] for row in rows)
    if command in {"выйди из проекта", "вернись в личное", "переключись на личное", "leave project", "switch to personal"}:
        choose(store, uid, 0)
        return "Вернулся в личный контекст." if russian else "Switched to your personal workspace."
    match = re.fullmatch(r"(создай проект|create project|переключись на(?: проект)?|открой проект|switch to(?: project)?)\s+(.{1,72})", value, re.I)
    if not match:
        return None
    name = match[2].rstrip('.!?').strip(' «»"“”')
    if match[1].casefold() in {"создай проект", "create project"}:
        chosen = create(store, uid, name)
    else:
        row = store.db.execute("SELECT id FROM projects WHERE user_id=? AND normalized=?", (uid, normalized(name))).fetchone()
        if not row:
            return (f"Проект «{name}» не найден. Посмотри список в мини-аппе." if russian
                    else f"I couldn’t find “{name}”. Your projects are in the Mini App.")
        chosen = get(store, uid, row["id"])
    choose(store, uid, chosen["id"])
    return (f"Сейчас работаю в проекте «{chosen['name']}»." if russian
            else f"Now working in “{chosen['name']}”.")


def facts(store, uid, project_id):
    return [dict(row) for row in store.db.execute(
        "SELECT id,text FROM project_facts WHERE user_id=? AND project_id=? ORDER BY id", (uid, project_id))]


def remember(store, uid, project_id, text):
    from .store import bounded_text
    get(store, uid, project_id)
    text = bounded_text(text, 400, required=True)
    key = normalized(text)
    old = store.db.execute("SELECT id,text FROM project_facts WHERE user_id=? AND project_id=? AND normalized=?",
                           (uid, project_id, key)).fetchone()
    if old:
        return dict(old)
    if len(facts(store, uid, project_id)) >= 40:
        raise ValueError("Память этого проекта заполнена.")
    return dict(store.db.execute("INSERT INTO project_facts(project_id,user_id,text,normalized) VALUES (?,?,?,?) RETURNING id,text",
                                 (project_id, uid, text, key)).fetchone())


def forget(store, uid, project_id, fact_id):
    row = store.db.execute("DELETE FROM project_facts WHERE user_id=? AND project_id=? AND id=? RETURNING id,text",
                           (uid, project_id, fact_id)).fetchone()
    if not row:
        raise KeyError("Заметка не найдена.")
    return dict(row)


def named(store, uid, name):
    match = store.db.execute("SELECT id FROM projects WHERE user_id=? AND normalized=?",
                             (uid, normalized(name))).fetchone()
    if not match:
        raise KeyError("Проект не найден. Уточни его название.")
    return match["id"]


def entries(store, uid, project_id, *, limit=40, kind=None):
    get(store, uid, project_id)
    fields = (uid, project_id, limit) if kind is None else (uid, project_id, kind, limit)
    condition = "" if kind is None else " AND kind=?"
    return [dict(row) for row in store.db.execute("""
        SELECT id,kind,text,source,created_at FROM project_entries
        WHERE user_id=? AND project_id=?""" + condition + """ ORDER BY id DESC LIMIT ?
    """, fields)]


def add_entry(store, uid, project_id, kind, text, source="", *, request_key=None):
    from .store import bounded_text, timestamp
    get(store, uid, project_id)
    if kind not in ("decision", "question", "material") or not project_id:
        raise ValueError("Выбери проект и тип записи.")
    text = bounded_text(text, 2000, required=True)
    source = bounded_text(source, 900)
    saved = store.db.execute("""
        INSERT INTO project_entries(user_id,project_id,kind,text,source,created_at,request_key)
        VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id,request_key) DO NOTHING
        RETURNING id,kind,text,source,created_at
    """, (uid, project_id, kind, text, source, timestamp(), request_key)).fetchone()
    if saved is None:
        saved = store.db.execute("""SELECT id,kind,text,source,created_at FROM project_entries
            WHERE user_id=? AND project_id=? AND request_key=?""", (uid, project_id, request_key)).fetchone()
    if saved is None:
        raise ValueError("Этот запрос уже использован в другом проекте.")
    return dict(saved)


def remove_entry(store, uid, project_id, entry_id):
    get(store, uid, project_id)
    changed = store.db.execute("DELETE FROM project_entries WHERE user_id=? AND project_id=? AND id=?",
                               (uid, project_id, entry_id)).rowcount
    if not changed:
        raise KeyError("Запись не найдена.")
