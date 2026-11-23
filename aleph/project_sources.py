"""Private reference snapshots and bounded model reads for named projects."""
import hashlib
from pathlib import PurePosixPath
import re

from . import projects, source_import, source_text

MAX_SOURCES = 40
MAX_TOTAL = 160
SELECT = "id, kind, title, url, status, reason, reader, updated_at, length(content) AS size"
CHUNK = 5600
STOP = {"какие", "какой", "какая", "этот", "этого", "этом", "тебе", "мне", "проект", "проекта",
        "источник", "источники", "what", "this", "that", "about", "from", "with", "the", "project", "source"}


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS project_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            source_key TEXT NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('web','youtube','notion','drive','file')),
            title TEXT NOT NULL,
            url TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('ready','partial','link')),
            reason TEXT NOT NULL,
            reader INTEGER NOT NULL DEFAULT 0,
            content TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(user_id, project_id, source_key)
        );
        CREATE INDEX IF NOT EXISTS project_source_recent ON project_sources(user_id, project_id, id DESC);
    """)


def authorize(store, uid, project_id):
    if not project_id:
        raise ValueError("Выбери проект для источника.")
    projects.get(store, uid, project_id)


def list_sources(store, uid, project_id):
    authorize(store, uid, project_id)
    return [dict(row) for row in store.db.execute(f"""
        SELECT {SELECT} FROM project_sources WHERE user_id=? AND project_id=? ORDER BY id DESC
    """, (uid, project_id))]


def catalog(store, uid, project_id):
    return [dict(row) for row in store.db.execute("""
        SELECT id,title,kind,status FROM project_sources WHERE user_id=? AND project_id=? ORDER BY id DESC LIMIT 20
    """, (uid, project_id))]


def _row(store, uid, project_id, source_id, *, content=False):
    if type(source_id) is not int or not 0 < source_id < 2**63:
        raise ValueError("Проверь номер источника.")
    fields = SELECT + (", content" if content else "")
    row = store.db.execute(f"SELECT {fields} FROM project_sources WHERE user_id=? AND project_id=? AND id=?",
                           (uid, project_id, source_id)).fetchone()
    if not row:
        raise KeyError("Источник не найден в этом проекте.")
    return dict(row)


def preview(store, uid, project_id, source_id):
    row = _row(store, uid, project_id, source_id, content=True)
    return {**row, "content": row["content"][:4200]}


def read(store, uid, project_id, source_id, offset=0):
    if type(offset) is not int or not 0 <= offset < source_text.MAX_TEXT:
        raise ValueError("Проверь позицию в источнике.")
    row = _row(store, uid, project_id, source_id, content=True)
    text = row.pop("content")
    row["excerpt"] = text[offset:offset + CHUNK]
    row["offset"] = offset
    row["next_offset"] = offset + CHUNK if offset + CHUNK < len(text) else None
    return row


def search(store, uid, project_id, query):
    words = list(dict.fromkeys(re.findall(r"[^\W_]{2,}", query.casefold())))[:20]
    terms = [word for word in words if word not in STOP][:10]
    if not terms:
        return {"query": query, "sources": catalog(store, uid, project_id)}
    hits = []
    for row in store.db.execute(f"""
        SELECT {SELECT},content FROM project_sources WHERE user_id=? AND project_id=? ORDER BY id DESC
    """, (uid, project_id)):
        title, text = row["title"].casefold(), row["content"]
        folded = text.casefold()
        scores = [(folded.find(term), term in title) for term in terms]
        score = sum(4 * in_title + (offset >= 0) for offset, in_title in scores)
        if score:
            offsets = [part[0] for part in scores if part[0] >= 0]
            start = max(0, min(offsets, default=0) - 140)
            info = dict(row)
            info["excerpt"] = info.pop("content")[start:start + 1250]
            hits.append((score, info))
    hits.sort(key=lambda hit: hit[0], reverse=True)
    return {"query": query, "matches": [item for _, item in hits[:5]]}


def _existing(store, uid, project_id, key):
    return store.db.execute(f"SELECT {SELECT} FROM project_sources WHERE user_id=? AND project_id=? AND source_key=?",
                            (uid, project_id, key)).fetchone()


def _title(value, fallback):
    cleaned = " ".join(source_text.CONTROL.sub("", str(value)).split())[:240]
    # Normalization also replaces isolated surrogates in external metadata.
    return cleaned.encode("utf-8", "replace").decode() or fallback


def _add(store, uid, project_id, kind, key, url, result, title):
    from .store import timestamp

    inserted = store.db.execute(f"""
        INSERT INTO project_sources(user_id,project_id,source_key,kind,title,url,status,reason,content,updated_at)
        SELECT ?,?,?,?,?,?,?,?,?,?
        WHERE (SELECT count(*) FROM project_sources WHERE user_id=? AND project_id=?) < ?
          AND (SELECT count(*) FROM project_sources WHERE user_id=?) < ?
        ON CONFLICT(user_id,project_id,source_key) DO NOTHING
        RETURNING {SELECT}
    """, (uid, project_id, key, kind, _title(result.title, title), url, result.status, result.reason,
          result.text, timestamp(), uid, project_id, MAX_SOURCES, uid, MAX_TOTAL)).fetchone()
    row = inserted or _existing(store, uid, project_id, key)
    if row is None:
        raise ValueError(f"Достигнут лимит: {MAX_SOURCES} источников в проекте или {MAX_TOTAL} всего.")
    return dict(row)


async def add_link(store, uid, project_id, url):
    authorize(store, uid, project_id)
    address = source_import.address(url)
    if row := _existing(store, uid, project_id, address.key):
        return dict(row)
    contents = await source_import.load(store, uid, address)
    return _add(store, uid, project_id, address.kind, address.key, address.url, contents, address.title)


async def add_file(store, uid, project_id, name, data):
    authorize(store, uid, project_id)
    key = "file:" + hashlib.sha256(data).hexdigest()
    tracked = store.db.execute("""SELECT DISTINCT s.id FROM project_sources s JOIN price_rules r ON r.source_id=s.id
        JOIN tasks t ON t.id=r.task_id WHERE s.user_id=? AND s.project_id=? AND s.kind='file' AND s.title=? AND t.status='open'""",
        (uid,project_id,name)).fetchall()
    if not tracked:
        if row := _existing(store, uid, project_id, key):
            return dict(row)
    contents = await source_text.read_file(name, data)
    if len(tracked)==1:
        old = _row(store,uid,project_id,tracked[0][0],content=True)
        return _replace(store,uid,project_id,old["id"],old,contents)
    return _add(store, uid, project_id, "file", key, "", contents, name)


def _replace(store, uid, project_id, source_id, old, result, *, reader=False):
    from .store import timestamp

    row = store.db.execute(f"""UPDATE project_sources SET title=?,status=?,reason=?,content=?,updated_at=?,reader=?
        WHERE user_id=? AND project_id=? AND id=? AND updated_at=? AND content=?
        RETURNING {SELECT}""", (_title(result.title, old["title"]), result.status, result.reason, result.text,
                              timestamp(), int(reader), uid, project_id, source_id, old["updated_at"], old["content"])).fetchone()
    if not row:
        return _row(store, uid, project_id, source_id)
    from .price_rules import observe
    observe(store,source_id)
    return dict(row)


async def refresh(store, uid, project_id, source_id):
    old = _row(store, uid, project_id, source_id, content=True)
    if not old["url"]:
        raise ValueError("Для обновления загрузи новую версию файла.")
    address = source_import.address(old["url"])
    contents = await source_import.via_reader(address) if old["reader"] else await source_import.load(store, uid, address)
    if old["content"] and (not contents.text or contents.reason == "captions"):
        raise ValueError("Свежую версию прочитать не удалось. Доступна предыдущая копия.")
    if contents.title == address.title:
        contents = source_text.Contents(old["title"], contents.text, contents.reason)
    return _replace(store, uid, project_id, source_id, old, contents, reader=bool(old["reader"]))


async def read_article(store, uid, project_id, source_id):
    old = _row(store, uid, project_id, source_id, content=True)
    if old["kind"] != "web":
        raise ValueError("Через Reader можно импортировать публичные статьи.")
    contents = await source_import.via_reader(source_import.address(old["url"]))
    return _replace(store, uid, project_id, source_id, old, contents, reader=True)


async def attach_transcript(store, uid, project_id, source_id, name, data):
    old = _row(store, uid, project_id, source_id, content=True)
    if old["kind"] != "youtube" or PurePosixPath(name).suffix.lower() not in (".txt", ".srt", ".vtt"):
        raise ValueError("Для расшифровки YouTube загрузи TXT, SRT или VTT.")
    contents = await source_text.read_file(name, data)
    return _replace(store, uid, project_id, source_id, old,
                    source_text.Contents(old["title"], contents.text, contents.reason))


def delete(store, uid, project_id, source_id):
    _row(store,uid,project_id,source_id)
    store.db.execute("""UPDATE tasks SET status='done' WHERE user_id=? AND id IN
        (SELECT task_id FROM price_rules WHERE source_id=?)""",(uid,source_id))
    if not store.db.execute("DELETE FROM project_sources WHERE user_id=? AND project_id=? AND id=?",
                            (uid, project_id, source_id)).rowcount:
        raise KeyError("Источник не найден в этом проекте.")
