"""Project document versions keep Telegram references, never expiring download URLs."""

import json
import re
import os


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS file_versions(
            id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            project INTEGER NOT NULL, message INTEGER NOT NULL, name TEXT NOT NULL, metadata TEXT NOT NULL,
            parent INTEGER, task INTEGER, created TEXT NOT NULL, UNIQUE(uid,project,message));
        CREATE INDEX IF NOT EXISTS versions_scope ON file_versions(uid,project,id);
    """)


def record(store, uid, project, message, meta, parent=None, task=None):
    from .store import timestamp

    store.db.execute(
        """INSERT INTO file_versions(uid,project,message,name,metadata,parent,task,created)
        VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(uid,project,message) DO NOTHING""",
        (uid, project, message, meta["file_name"], json.dumps(meta), parent, task, timestamp()),
    )


def link(ident):
    bot = os.getenv("ALEPH_BOT_USERNAME", "AlephAgentBot").lstrip("@")
    return f"https://t.me/{bot}?start=file_{ident}" if re.fullmatch(r"[a-zA-Z0-9_]{5,32}", bot) else ""


def listing(store, uid, project, task=None):
    rows = store.db.execute(
        """SELECT id,name,parent,created FROM file_versions WHERE uid=? AND project=?
        AND (? IS NULL OR task=?) ORDER BY id DESC LIMIT 100""",
        (uid, project, task, task),
    )
    return [{**dict(row), "url": link(row["id"])} for row in rows]


async def stage_restore(store, uid, project, ident, key, download):
    from . import documents

    row = store.db.execute(
        "SELECT * FROM file_versions WHERE id=? AND uid=? AND project=?", (ident, uid, project)
    ).fetchone()
    if not row or not download:
        raise ValueError("Эта версия недоступна.")
    meta = json.loads(row["metadata"])
    existing = store.db.execute("SELECT filename FROM bot_documents WHERE key=?",(key,)).fetchone()
    if existing:
        return {"prepared_attachment":existing[0],"restored_version":ident}
    blob = await download(meta, documents.MAX_INPUT)
    if not blob or len(blob) > documents.MAX_INPUT:
        raise ValueError("Проверь размер файла.")
    fmt = row["name"].rsplit(".", 1)[-1].lower()
    store.db.execute(
        """INSERT OR IGNORE INTO bot_documents(key,update_id,filename,mime,data,parent_version)
        VALUES(?,?,?,?,?,?)""",
        (
            key,
            int(key.split(":")[1]),
            row["name"],
            documents.MIMES.get(fmt, "application/octet-stream"),
            blob,
            ident,
        ),
    )
    return {"prepared_attachment": row["name"], "restored_version": ident}
