"""One-time AI work scheduled as an ordinary, user-owned task."""
from datetime import datetime, timedelta, timezone
import json
from zoneinfo import ZoneInfo


MAX_PARALLEL = 4


def initialize(db):
    db.execute("""
        CREATE TABLE IF NOT EXISTS scheduled_reports (
            task_id INTEGER PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
            state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','failed','sent')),
            started_at TEXT, result TEXT, delivered_at TEXT, revision INTEGER NOT NULL DEFAULT 0
        )
    """)
    columns = {row["name"] for row in db.execute("PRAGMA table_info(scheduled_reports)")}
    for column, kind in {"voice_requested":"INTEGER NOT NULL DEFAULT 0","origin_message":"INTEGER","stage":"TEXT NOT NULL DEFAULT ''","on_server":"INTEGER NOT NULL DEFAULT 0",
                         "needs_folder":"INTEGER NOT NULL DEFAULT 0","inputs":"TEXT NOT NULL DEFAULT '[]'",
                         "started_at": "TEXT", "result": "TEXT", "delivered_at": "TEXT",
                         "revision": "INTEGER NOT NULL DEFAULT 0"}.items():
        if column not in columns:
            db.execute(f"ALTER TABLE scheduled_reports ADD COLUMN {column} {kind}")


def schedule(store, uid, project_id, title, prompt, run_at, request_key, source="", needs_folder=False, input_versions=(), origin_message=None, voice_requested=False):
    from .store import bounded_text

    existing = store.db.execute("""
        SELECT t.id FROM tasks t JOIN scheduled_reports r ON r.task_id=t.id
        WHERE t.user_id=? AND t.request_key=?
    """, (uid, request_key)).fetchone()
    if existing:
        task = store.get_task(uid, existing[0])
        when = datetime.fromisoformat(task["due_at"]).astimezone(ZoneInfo(store.get_settings(uid)["timezone"]))
        return {**task, "scheduled_local": when.isoformat(timespec="minutes")}

    prompt = bounded_text(prompt, 4000, required=True)
    try:
        when = datetime.fromisoformat(run_at)
        if when.utcoffset() is None or when <= datetime.now(timezone.utc):
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        raise ValueError("Укажи будущее время с часовым поясом.") from None
    count = store.db.execute("""
        SELECT count(*) FROM scheduled_reports r JOIN tasks t ON t.id=r.task_id
        WHERE t.user_id=? AND t.status='open' AND r.state='pending'
    """, (uid,)).fetchone()[0]
    if count >= 25:
        raise ValueError("Можно запланировать до 25 поручений Алефу.")
    store.db.execute("SAVEPOINT schedule_report")
    try:
        task = store.create_task(uid, title, run_at, prompt, request_key, project_id=project_id,
                                 remind_at=None, source=source)
        store.db.execute("INSERT INTO scheduled_reports(task_id,needs_folder,inputs,origin_message,voice_requested) VALUES(?,?,?,?,?)", (task["id"],int(needs_folder),json.dumps(input_versions),origin_message,int(voice_requested)))
        result = store.get_task(uid, task["id"])
    except BaseException:
        store.db.execute("ROLLBACK TO schedule_report")
        raise
    finally:
        store.db.execute("RELEASE schedule_report")
    return {**result, "scheduled_local": when.isoformat(timespec="minutes")}


def queue_status(store, uid, russian):
    from .store import timestamp

    rows = store.db.execute("""
        SELECT t.title, r.stage, r.started_at FROM scheduled_reports r JOIN tasks t ON t.id=r.task_id
        WHERE t.user_id=? AND t.status='open' AND r.state='pending' AND t.due_at<=?
        ORDER BY r.started_at IS NULL, t.due_at, t.id
    """, (uid, timestamp(datetime.now(timezone.utc) + timedelta(seconds=2)))).fetchall()
    if not rows:
        return "Сейчас фоновых поручений нет." if russian else "There are no background tasks right now."
    lines = []
    for row in rows:
        if row["stage"] == "offline":
            state = "Жду компьютер" if russian else "Waiting for computer"
        elif row["started_at"]:
            state = "В работе" if russian else "Running"
        else:
            state = "В очереди" if russian else "Queued"
        lines.append(f"{state} · {row['title']}")
    return "\n".join(lines)


def enqueue(db, now=None):
    """Translate due reports into the bot's existing generation and delivery queue."""
    from .store import timestamp

    at = timestamp(now)
    db.execute("""
        INSERT OR IGNORE INTO bot_inbox(id,uid,message,project_id,voice_requested)
        SELECT -t.id, t.user_id,
            json_object('from',json_object('id',t.user_id,'language_code',u.language_code),
                        'due_at',t.due_at,'notes',t.notes,'title',t.title,'revision',r.revision,'origin_message',r.origin_message), t.project_id, r.voice_requested
        FROM tasks t JOIN scheduled_reports r ON r.task_id=t.id
        JOIN users u ON u.id=t.user_id
        WHERE t.status='open' AND r.state='pending' AND t.due_at<=?
          AND COALESCE(u.blocked_until,'')<=?
          AND NOT EXISTS (SELECT 1 FROM bot_inbox b WHERE b.id=-t.id)
        ORDER BY t.due_at,t.id LIMIT 24
    """, (at, at))


def current(store, row, *, allow_failed=False):
    try:
        task = store.get_task(row["uid"], -row["id"])
    except KeyError:
        return None
    snapshot = json.loads(row["message"])
    states = ("pending", "failed") if allow_failed else ("pending",)
    if (task["report_state"] not in states or task["status"] != "open"
            or task["project_id"] != row["project_id"]
            or task["report_revision"] != snapshot.get("revision", 0)
            or any(task[key] != snapshot.get(key) for key in ("due_at", "title", "notes"))):
        return None
    return task


def start(db, task_id):
    from .store import timestamp
    db.execute("UPDATE scheduled_reports SET stage='running',started_at=COALESCE(started_at,?) WHERE task_id=?",
               (timestamp(), task_id))


def wait_for_computer(db, task_id):
    db.execute("UPDATE scheduled_reports SET stage='offline',started_at=NULL WHERE task_id=? AND state='pending'", (task_id,))


def fail(db, uid, update_id):
    db.execute("""UPDATE scheduled_reports SET state='failed', started_at=NULL WHERE task_id=? AND state='pending'
        AND EXISTS (SELECT 1 FROM tasks WHERE id=task_id AND user_id=?)
    """, (-update_id, uid))


def finish(store, row):
    from .store import timestamp
    if not current(store, row):
        return
    db = store.db
    if db.execute("""UPDATE scheduled_reports SET state='sent', started_at=NULL, result=?, delivered_at=?
        WHERE task_id=? AND state='pending'""", (row["reply"], timestamp(), -row["id"])).rowcount:
        store.update_task(row["uid"], -row["id"], {"status": "done"})
        store.add_history(row["uid"], "assistant", row["reply"], project_id=row["project_id"])


def result(store, uid, task_id):
    row = store.db.execute("""SELECT r.result AS text, r.delivered_at FROM scheduled_reports r
        JOIN tasks t ON t.id=r.task_id WHERE t.id=? AND t.user_id=? AND r.state='sent'""", (task_id, uid)).fetchone()
    if row is None:
        raise KeyError("Результат не найден.")
    return dict(row)


def pins(store,uid,project,context):
    if not context:
        return []
    ids = [item["id"] for item in json.loads(context)["files"]]
    versions = []
    for ident in ids:
        row = store.db.execute("""SELECT v.id FROM file_versions v JOIN bot_file_refs f ON f.uid=v.uid AND
            f.project_id=v.project AND f.message_id=v.message WHERE f.uid=? AND f.project_id=? AND f.id=?""",
            (uid,project,ident)).fetchone()
        if row:
            versions.append(row[0])
    return versions


def input_context(store,task):
    from . import document_chat
    row = store.db.execute("SELECT inputs FROM scheduled_reports WHERE task_id=?",(task["id"],)).fetchone()
    refs = []
    for ident in json.loads(row[0]):
        version = store.db.execute("SELECT * FROM file_versions WHERE id=? AND project=? AND uid=(SELECT user_id FROM tasks WHERE id=?)",
                                   (ident,task["project_id"],task["id"])).fetchone()
        if not version:
            raise ValueError("Исходная версия документа недоступна.")
        refs.append(document_chat.register(store,version["uid"],version["project"],version["message"],json.loads(version["metadata"]),update_id=-task["id"]))
    return document_chat.prompt_section(refs)
