"""Scope Telegram file references and delivered artifacts to their chat workspace."""
import json
from pathlib import PurePath
import re
import time

from . import document_tools, documents


MAX_FILES = 8
MAX_BATCH = 24 * 1024 * 1024
RECENT = 16
KEEP_SECONDS = 30 * 86400
_PROMPT_MARKER = "[Files for this request]"
_INPUT_MIMES = {mime: fmt for fmt, mime in documents.MIMES.items()}
_INPUT_MIMES.update({"text/rtf": "rtf", "text/x-markdown": "md", "application/calendar": "ics"})


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS bot_file_refs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          uid INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          project_id INTEGER NOT NULL,
          message_id INTEGER NOT NULL,
          metadata TEXT NOT NULL,
          format TEXT NOT NULL,
          available REAL NOT NULL,
          UNIQUE(uid,project_id,message_id)
        );
        CREATE INDEX IF NOT EXISTS bot_file_scope ON bot_file_refs(uid,project_id,id);
        CREATE INDEX IF NOT EXISTS bot_file_expiry ON bot_file_refs(available);
    """)


def _metadata(document):
    filename = document.get("file_name") or ""
    mime = str(document.get("mime_type") or "").split(";", 1)[0].lower()
    file_id = document.get("file_id")
    if not isinstance(filename, str) or not isinstance(file_id, str) or not 1 <= len(file_id) <= 512:
        raise ValueError("Telegram не передал данные файла. Отправь его ещё раз.")
    filename = re.sub(r"[\x00-\x1f\x7f\ud800-\udfff]", "", filename).replace("\\", "/").split("/")[-1][:160]
    suffix = PurePath(filename).suffix.lower().lstrip(".")
    fmt = suffix if suffix in documents.MIMES or suffix == "htm" else _INPUT_MIMES.get(mime) if not suffix else None
    if not fmt:
        raise ValueError("Пришли PDF, Word, Excel, PowerPoint, OpenDocument, RTF, TXT, Markdown, CSV, TSV, JSON или HTML.")
    size = document.get("file_size") or 0
    if type(size) is not int or not 0 <= size <= documents.MAX_INPUT:
        raise ValueError("Можно загрузить документ до 16 МБ. Пришли нужные страницы или раздели его на части.")
    return {"file_id": file_id, "file_name": filename or f"Файл.{fmt}", "file_size": size}, fmt


def register(store, uid, project_id, message_id, document, *, parent=None, task=None):
    if not isinstance(document, dict) or type(message_id) is not int or message_id <= 0:
        return None
    metadata, fmt = _metadata(document)
    from . import file_versions
    file_versions.record(store,uid,project_id,message_id,metadata,parent,task)
    now = time.time()
    db = store.db
    db.execute("DELETE FROM bot_file_refs WHERE available<=?", (now,))
    row = db.execute("""INSERT INTO bot_file_refs(uid,project_id,message_id,metadata,format,available)
        VALUES(?,?,?,?,?,?) ON CONFLICT(uid,project_id,message_id) DO UPDATE SET
        metadata=excluded.metadata, format=excluded.format, available=excluded.available RETURNING id""",
        (uid, project_id, message_id, json.dumps(metadata), fmt, now + KEEP_SECONDS)).fetchone()
    db.execute("""DELETE FROM bot_file_refs WHERE uid=? AND project_id=? AND id IN (
        SELECT id FROM bot_file_refs WHERE uid=? AND project_id=?
        ORDER BY available DESC,id DESC LIMIT -1 OFFSET ?)""", (uid, project_id, uid, project_id, RECENT))
    return {"id": row["id"], "filename": metadata["file_name"], "format": fmt}


def _item(row):
    document = json.loads(row["metadata"])
    return {"id": row["id"], "filename": document["file_name"], "format": row["format"]}


def recent(store, uid, project_id):
    rows = store.db.execute("""SELECT id,metadata,format FROM bot_file_refs
        WHERE uid=? AND project_id=? AND available>? ORDER BY available DESC,id DESC LIMIT ?""",
        (uid, project_id, time.time(), RECENT))
    return [_item(row) for row in rows]


def collect(store, uid, project_id, update_id, original):
    """Register files explicitly attached or quoted, including a Telegram album."""
    db = store.db
    saved = db.execute("SELECT file_context FROM bot_inbox WHERE id=? AND uid=?", (update_id, uid)).fetchone()
    if saved and saved[0]:
        snapshot = json.loads(saved[0])
        return {**original, "caption": snapshot["caption"]}, snapshot["files"]
    msg = original
    files, siblings = [], []
    quoted = msg.get("reply_to_message") or {}
    group = msg.get("media_group_id")
    if group and msg.get("document"):
        siblings = [(row["id"], json.loads(row["message"])) for row in store.db.execute("""
            SELECT id,message FROM bot_inbox
            WHERE uid=? AND id>? AND json_extract(message,'$.media_group_id')=? AND json_type(message,'$.document')='object'
            ORDER BY id LIMIT ?""", (uid, update_id, group, MAX_FILES - 1))]
    matches = [msg, *(item for _, item in siblings)]
    if (quoted.get("chat") or {}).get("id") == uid and len(matches) < MAX_FILES:
        matches.append(quoted)
    db.execute("SAVEPOINT collect_files")
    try:
        for item in matches:
            if document := item.get("document"):
                ref = register(store, uid, project_id, item.get("message_id"), document)
                if ref and ref not in files:
                    files.append(ref)
        if siblings:
            db.executemany("DELETE FROM bot_inbox WHERE id=? AND uid=?", [(id_, uid) for id_, _ in siblings])
            if not msg.get("caption"):
                caption = next((item.get("caption") for _, item in siblings if item.get("caption")), "")
                msg = {**msg, "caption": caption}
        if files:
            snapshot = {"files": files, "caption": msg.get("caption")}
            db.execute("UPDATE bot_inbox SET file_context=? WHERE id=? AND uid=?", (json.dumps(snapshot), update_id, uid))
    except BaseException:
        db.execute("ROLLBACK TO collect_files")
        raise
    finally:
        db.execute("RELEASE collect_files")
    return msg, files


def prompt_section(files):
    return "\n\n" + _PROMPT_MARKER + "\n" + json.dumps(files, ensure_ascii=False) if files else ""


def _scoped_ref(store, uid, project_id, file_id):
    if type(file_id) is not int or not 0 < file_id < 2**63:
        raise ValueError("Выбери файл из списка вложений.")
    row = store.db.execute("""SELECT * FROM bot_file_refs WHERE id=? AND uid=? AND project_id=? AND available>?""",
                           (file_id, uid, project_id, time.time())).fetchone()
    if not row:
        raise ValueError("Этот файл недоступен в текущем проекте. Пришли его ещё раз.")
    return row


async def _inputs(store, uid, project_id, ids, download, update_id):
    if (not isinstance(ids, list) or not 1 <= len(ids) <= MAX_FILES
            or any(type(id_) is not int for id_ in ids) or len(ids) != len(set(ids))):
        raise ValueError("Выбери до восьми файлов из списка вложений.")
    local = {}
    for ident in ids:
        if ident < 0:
            row = store.db.execute("""SELECT f.format,f.data FROM folder_inputs f JOIN bot_inbox i ON i.id=f.update_id
                WHERE f.id=? AND i.id=? AND i.uid=? AND i.project_id=?""",(-ident,update_id,uid,project_id)).fetchone()
            if not row:
                raise ValueError("Заново открой файл в папке проекта.")
            local[ident] = (row[0],row[1])
    if sum(len(raw) for _, raw in local.values()) > MAX_BATCH:
        raise ValueError("За один раз можно обработать до 24 МБ.")
    refs = [_scoped_ref(store, uid, project_id, id_) for id_ in ids if id_>=0]
    pairs = [(row, json.loads(row["metadata"])) for row in refs]
    if sum(meta["file_size"] for _, meta in pairs) + sum(len(raw) for _,raw in local.values()) > MAX_BATCH:
        raise ValueError("За один раз можно обработать до 24 МБ.")
    if pairs and download is None:
        raise ValueError("Сейчас не удалось открыть файлы из Telegram.")
    results, total = local, sum(len(raw) for _,raw in local.values())
    for row, meta in pairs:
        try:
            raw = await download(meta, min(documents.MAX_INPUT, MAX_BATCH - total))
        except documents.MediaError as error:
            raise ValueError(str(error)) from None
        if not raw:
            raise ValueError("Файл пустой.")
        total += len(raw)
        if len(raw) > documents.MAX_INPUT or total > MAX_BATCH:
            raise ValueError("Файл превышает лимит размера.")
        results[row["id"]] = (row["format"], raw)
    return {ident:results[ident] for ident in ids}


async def execute(store, uid, project_id, name, args, request_key, download):
    schema = next(tool["function"]["parameters"] for tool in document_tools.TOOLS if tool["function"]["name"] == name)
    if (not isinstance(args, dict) or set(args) - set(schema["properties"])
            or not set(schema["required"]).issubset(args)):
        raise ValueError("Проверь параметры документа.")
    if name == "list_chat_files":
        return {"files": recent(store, uid, project_id)}
    if name == "inspect_document":
        inputs = await _inputs(store, uid, project_id, [args["file_id"]], download,int(request_key.split(":")[1]))
        result, _ = await documents.run_office("inspect", args, inputs)
        return {**(_item(_scoped_ref(store, uid, project_id, args["file_id"])) if args["file_id"]>0 else {"file_id":args["file_id"]}), **result}
    update_id = int(request_key.split(":")[1])
    db = store.db
    queued = db.execute("""SELECT d.filename, length(d.data) AS size FROM bot_documents d
        JOIN bot_inbox i ON i.id=d.update_id WHERE d.key=? AND i.uid=?""", (request_key, uid)).fetchone()
    if queued:
        return {"prepared_attachment": queued["filename"], "bytes": queued["size"]}
    if db.execute("SELECT count(*) FROM bot_documents WHERE update_id=?", (update_id,)).fetchone()[0] >= 3:
        raise ValueError("Можно приложить до трёх файлов за один ответ.")
    if name == "edit_document":
        ids = args["file_ids"]
        if isinstance(ids, list) and len(ids) > 1 and args["action"] != "pdf_pages":
            raise ValueError("Для этой операции выбери один файл.")
        inputs = await _inputs(store, uid, project_id, ids, download, update_id)
        result, blob = await documents.run_office("edit", args, inputs)
    elif args["format"] in ("txt", "md", "csv", "tsv", "ics", "json"):
        filename, mime, blob = documents.render_document(args)
        result = {"filename": filename, "mime": mime}
    else:
        result, blob = await documents.run_office("create", args, {})
    if not db.execute("SELECT 1 FROM bot_inbox WHERE id=? AND uid=?", (update_id, uid)).fetchone():
        raise ValueError("Этот запрос уже закрыт.")
    db.execute("INSERT OR IGNORE INTO bot_documents(key,update_id,filename,mime,data) VALUES (?,?,?,?,?)",
               (request_key, update_id, result.pop("filename"), result.pop("mime"), blob))
    if name == "edit_document" and ids[0] > 0:
        db.execute("""UPDATE bot_documents SET parent_version=(SELECT v.id FROM bot_file_refs f JOIN file_versions v
            ON v.uid=f.uid AND v.project=f.project_id AND v.message=f.message_id
            WHERE f.uid=? AND f.project_id=? AND f.id=?) WHERE key=?""",(uid,project_id,ids[0],request_key))
    filename = db.execute("SELECT filename FROM bot_documents WHERE key=?", (request_key,)).fetchone()[0]
    return {"prepared_attachment": filename, "bytes": len(blob), **result}
