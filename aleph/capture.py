"""Save one chat update as project decisions, open questions and a task."""
import json
import re
import secrets
import time

from . import projects
from .links import task_button

TASK_FIELDS = {"title", "notes", "due_at", "remind_at"}


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS pending_captures (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            update_id INTEGER NOT NULL,
            request_key TEXT NOT NULL UNIQUE,
            payload TEXT NOT NULL,
            source TEXT NOT NULL,
            expires REAL NOT NULL
        );
    """)


def save(store, uid, project_id, args, source, key):
    project = projects.get(store, uid, project_id)
    result = {"title": project["name"], "project": project["name"]}
    for kind in ("decision", "question"):
        if args.get(kind):
            result[kind] = projects.add_entry(store, uid, project_id, kind, args[kind], source,
                                              request_key=f"{key}:{kind}")
    if args.get("title"):
        fields = {name: value for name, value in args.items() if name in TASK_FIELDS}
        result["task"] = store.create_task(uid, **fields, request_key=key, project_id=project_id, source=source)
    return result


def create(store, uid, project_id, key, args):
    target = projects.named(store, uid, args["project"]) if args.get("project") else project_id
    if not any(args.get(field) for field in ("decision", "question", "title")):
        raise ValueError("Уточни, что сохранить в проекте.")
    update_id = int(key.split(":")[1])
    source = store.db.execute("SELECT source FROM bot_inbox WHERE id=? AND uid=?", (update_id, uid)).fetchone()
    excerpt = source[0] if source else ""
    if target:
        return save(store, uid, target, args, excerpt, key)
    if not projects.list_projects(store, uid):
        raise ValueError("Для этой записи сначала создай проект или укажи его название.")
    store.db.execute("DELETE FROM pending_captures WHERE expires<=?", (time.time(),))
    if not for_update(store, uid, update_id):
        store.db.execute("""INSERT OR IGNORE INTO pending_captures
            (id,user_id,update_id,request_key,payload,source,expires) VALUES(?,?,?,?,?,?,?)
        """, (secrets.token_urlsafe(12), uid, update_id, key, json.dumps(args, ensure_ascii=False), excerpt, time.time() + 1800))
    return {"awaiting_project_choice": True}


def for_update(store, uid, update_id):
    return store.db.execute("""SELECT * FROM pending_captures
        WHERE user_id=? AND update_id=? AND expires>? LIMIT 1
    """, (uid, update_id, time.time())).fetchone()


def preview(store, uid, row, ru):
    data = json.loads(row["payload"])
    title = "К какому проекту это относится?" if ru else "Which project is this for?"
    labels = (("decision", "Решение" if ru else "Decision"),
              ("question", "Открытый вопрос" if ru else "Open question"),
              ("title", "Поручение" if ru else "Task"))
    items = [title] + [f"{label} — {data[key][:450]}" for key, label in labels if data.get(key)]
    links = [[{"text": p["name"], "callback_data": f"cap:{row['id']}:{p['id']}"}]
             for p in projects.list_projects(store, uid)]
    links.append([{"text": "Отмена" if ru else "Cancel", "callback_data": f"cap:{row['id']}:0"}])
    return {"text": "\n\n".join(items), "reply_markup": {"inline_keyboard": links}}


async def handle(store, telegram, query):
    match = re.fullmatch(r"cap:([A-Za-z0-9_-]{16}):([0-9]{1,18})", str(query.get("data", "")))
    message = query.get("message") or {}
    chat = message.get("chat") or {}
    uid = (query.get("from") or {}).get("id")
    if chat.get("type") != "private" or uid != chat.get("id") or not match:
        return
    identifier, project_id = match[1], int(match[2])
    ru = ((query.get("from") or {}).get("language_code") or "ru").startswith(("ru", "uk", "be", "kk"))
    error_text = ""
    keyboard = []
    store.db.execute("SAVEPOINT apply_capture")
    try:
        request = store.db.execute("""DELETE FROM pending_captures WHERE id=? AND user_id=? AND expires>?
            RETURNING *""", (identifier, uid, time.time())).fetchone()
        if not request:
            raise ValueError("Запись уже обработана или истекла.")
        if project_id:
            result = save(store, uid, project_id, json.loads(request["payload"]), request["source"], request["request_key"])
            name = result["project"]
            text = f"Сохранил в проекте «{name}»." if ru else f"Saved in “{name}”."
            if result.get("task"):
                button = task_button(result["task"]["id"], ru)
                if button:
                    keyboard.append([button])
        else:
            text = "Отменено." if ru else "Cancelled."
    except (ValueError, KeyError) as error:
        store.db.execute("ROLLBACK TO apply_capture")
        error_text = str(error.args[0])[:160]
    except BaseException:
        store.db.execute("ROLLBACK TO apply_capture")
        raise
    finally:
        store.db.execute("RELEASE apply_capture")
    if error_text:
        await telegram.call("answerCallbackQuery", callback_query_id=query["id"], text=error_text)
        return
    await telegram.call("answerCallbackQuery", callback_query_id=query["id"])
    await telegram.call("editMessageText", chat_id=uid, message_id=message["message_id"], text=text,
                        reply_markup={"inline_keyboard": keyboard})
