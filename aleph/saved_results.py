"""Private reply snapshots saved to a workspace from Telegram."""
from datetime import timedelta
import os
import re

from . import projects

META = "id,title,saved_at"
ACTION = re.compile(r"save:(\d{1,18})(?::(\d{1,18}))?")
OPEN = re.compile(r"/start(?:@\w+)?\s+answer_(\d{1,18})")


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS saved_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            update_id INTEGER NOT NULL,
            origin_project INTEGER NOT NULL,
            origin_message INTEGER,
            chooser_message INTEGER,
            project_id INTEGER,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            created_at TEXT NOT NULL,
            saved_at TEXT,
            UNIQUE(user_id,update_id)
        );
        CREATE INDEX IF NOT EXISTS saved_results_workspace ON saved_results(user_id,project_id,saved_at,id);
        CREATE INDEX IF NOT EXISTS saved_results_expiry ON saved_results(created_at) WHERE saved_at IS NULL;
    """)


def _title(body, source):
    heading = re.search(r"^#{1,3}\s+(.+)$", body, re.M)
    label = heading[1] if heading and len(heading[1]) <= 140 else source or body
    label = re.sub(r"[*_`#]", "", label.split("\n", 1)[0]).strip()
    return (label[:119].rstrip() + "…") if len(label) > 120 else (label or "Aleph")


def prepare(store, uid, update_id, project_id, body, source="", *, report_title=""):
    from .store import timestamp
    if not body.strip() or (not report_title and len(body) < 650):
        return
    db = store.db
    title = report_title[:120] if report_title else _title(body, source)
    db.execute("""INSERT INTO saved_results(user_id,update_id,origin_project,title,body,created_at)
        VALUES(?,?,?,?,?,?) ON CONFLICT(user_id,update_id) DO UPDATE SET
        title=excluded.title,body=excluded.body,origin_project=excluded.origin_project
        WHERE saved_results.origin_message IS NULL AND saved_results.saved_at IS NULL""",
        (uid, update_id, project_id, title, body[:14000], timestamp()))
    # Bound the unsaved snapshots while keeping everything the user saved.
    db.execute("""DELETE FROM saved_results WHERE id IN (
        SELECT id FROM saved_results WHERE user_id=? AND saved_at IS NULL ORDER BY id DESC LIMIT -1 OFFSET 100)""", (uid,))


def prune(store, now):
    from .store import timestamp
    cutoff = timestamp(now - timedelta(days=45))
    store.db.execute("DELETE FROM saved_results WHERE saved_at IS NULL AND created_at<?", (cutoff,))


def for_update(store, uid, update_id, body):
    row = store.db.execute("SELECT id FROM saved_results WHERE user_id=? AND update_id=? AND body=?",
                           (uid, update_id, body[:14000])).fetchone()
    return row["id"] if row else None


def remember_message(store, uid, answer_id, response, *, chooser=False):
    message_id = response.get("message_id") if isinstance(response, dict) else None
    if type(message_id) is not int or message_id <= 0:
        return
    field = "chooser_message" if chooser else "origin_message"
    store.db.execute(f"UPDATE saved_results SET {field}=? WHERE id=? AND user_id=? AND {field} IS NULL",
                     (message_id, answer_id, uid))


def library(store, uid, project_id):
    projects.get(store, uid, project_id)
    return [dict(row) for row in store.db.execute(f"""
        SELECT {META} FROM saved_results
        WHERE user_id=? AND project_id=? AND saved_at IS NOT NULL ORDER BY saved_at DESC,id DESC
    """, (uid, project_id))]


def saved(store, uid, answer_id, project_id=None):
    row = store.db.execute("""SELECT * FROM saved_results WHERE user_id=? AND id=? AND saved_at IS NOT NULL
           AND (? IS NULL OR project_id=?)""", (uid, answer_id, project_id, project_id)).fetchone()
    if not row:
        raise KeyError("Результат не найден.")
    return row


def remove(store, uid, project_id, answer_id):
    row = saved(store, uid, answer_id, project_id)
    store.db.execute("UPDATE saved_results SET project_id=NULL,saved_at=NULL,chooser_message=NULL WHERE id=?", (row["id"],))


def chat_url(answer_id):
    username = os.getenv("ALEPH_BOT_USERNAME", "AlephAgentBot").strip().lstrip("@")
    return f"https://t.me/{username}?start=answer_{answer_id}" if re.fullmatch(r"[A-Za-z0-9_]{5,32}", username) else ""


async def handle(store, telegram, query):
    from .store import timestamp
    found = ACTION.fullmatch(str(query.get("data", "")))
    user = query.get("from") or {}
    uid = user.get("id")
    message = query.get("message") or {}
    chat = message.get("chat") or {}
    ru = (user.get("language_code") or "ru").lower().startswith(("ru","uk","be","kk"))

    async def reply(russian, english):
        await telegram.call("answerCallbackQuery", callback_query_id=query["id"], text=russian if ru else english)

    if not found or type(uid) is not int or chat.get("id") != uid or chat.get("type") != "private":
        await reply("Результат недоступен.", "This result is unavailable.")
        return
    answer_id = int(found[1])
    row = store.db.execute("SELECT * FROM saved_results WHERE user_id=? AND id=?", (uid, answer_id)).fetchone()
    if row is None:
        await reply("Результат недоступен.", "This result is unavailable.")
        return
    if found[2] is None:
        remember_message(store, uid, answer_id, message)
    if row["saved_at"]:
        await reply("Ответ уже сохранён.", "This answer is already saved.")
        return
    project_id = int(found[2]) if found[2] is not None else row["origin_project"]
    active = projects.list_projects(store, uid)
    if not project_id and found[2] is None and active:
        if row["chooser_message"]:
            await reply("Выбери папку под ответом.", "Choose a folder under the answer.")
            return
        await reply("Выбери проект для ответа.", "Choose a project for this answer.")
        items = [{"id":0,"name":"Личное" if ru else "Personal"}, *active]
        response = await telegram.call("sendMessage", chat_id=uid, text="Куда сохранить ответ?" if ru else "Where should I save this answer?",
            reply_markup={"inline_keyboard": [[{"text": item["name"], "callback_data": f"save:{answer_id}:{item['id']}"}]
                                               for item in items]},
            **({"reply_parameters":{"message_id":message["message_id"],"allow_sending_without_reply":True}}
               if message.get("message_id") else {}))
        remember_message(store, uid, answer_id, response, chooser=True)
        return
    try:
        project = projects.get(store, uid, project_id)
    except KeyError:
        await reply("Проект недоступен.", "This project is unavailable.")
        return
    changed = store.db.execute("""UPDATE saved_results SET project_id=?,saved_at=? WHERE user_id=? AND id=? AND saved_at IS NULL""",
                               (project_id, timestamp(), uid, answer_id)).rowcount
    if not changed:
        await reply("Ответ уже сохранён.", "This answer is already saved.")
        return
    name = project["name"] or ("Личное" if ru else "Personal")
    await reply(f"Сохранено в «{name}».", f"Saved to “{name}”.")
