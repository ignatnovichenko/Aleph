"""User actions available through the four connected workspaces."""
import asyncio
import base64
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
import json
import re
import secrets
import time
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo

import aiohttp

from . import connections
from .store import bounded_text

DOCUMENT = "application/vnd.google-apps.document"
CALENDAR = "/calendar/v3"
GMAIL = "/gmail/v1/users/me"
DRIVE = "/drive/v3/files"
WRITES = {
    "calendar": "create_event", "notion": "append_note", "drive": "create_doc", "gmail": "create_draft",
}


def item_id(value, *, notion=False):
    if notion and value.startswith("https://"):
        parsed = urlsplit(value)
        if parsed.hostname not in ("notion.so", "www.notion.so", "notion.site") and not (
                parsed.hostname or "").endswith(".notion.site"):
            raise ValueError("Укажи ссылку на страницу Notion.")
        value = parsed.path.rstrip("/").rsplit("/", 1)[-1]
        match = re.search(r"([0-9a-fA-F]{32}|[0-9a-fA-F-]{36})$", value)
        value = match[1] if match else ""
    pattern = (r"(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12})"
               if notion else r"[A-Za-z0-9_-]{1,150}")
    if not re.fullmatch(pattern, value):
        raise ValueError("Проверь идентификатор объекта.")
    return value


def date_time(value, zone):
    try:
        when = datetime.fromisoformat(value)
        if when.tzinfo is None:
            when = when.replace(tzinfo=zone)
            if when.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) != datetime.fromisoformat(value):
                raise ValueError
        return when
    except (ValueError, TypeError, OverflowError):
        raise ValueError("Проверь дату, время и часовой пояс.") from None


async def google_agenda(store, session, uid, start="", end=""):
    zone = ZoneInfo(store.get_settings(uid)["timezone"])
    first = date_time(start, zone) if start else datetime.now(zone).replace(hour=0, minute=0, second=0, microsecond=0)
    last = date_time(end, zone) if end else first + timedelta(days=1)
    if not first < last <= first + timedelta(days=32):
        raise ValueError("Укажи период от одного события до 32 дней.")
    params = {"timeMin": first.isoformat(), "timeMax": last.isoformat(), "singleEvents": "true",
              "orderBy": "startTime", "maxResults": 30,
              "fields": "nextPageToken,items(summary,start,end,htmlLink,status,location)"}

    async def fetch(calendar):
        path = CALENDAR + "/calendars/" + quote(calendar, safe="") + "/events"
        try:
            return await connections.call(store, session, uid, "calendar", "GET", path, params=params)
        except connections.ConnectionError as error:
            if calendar == "primary" or error.status not in (403, 404):
                raise
            return {}

    row = store.db.execute("SELECT calendar_id FROM connections WHERE user_id=? AND provider='calendar'", (uid,)).fetchone()
    calendars = ["primary", row[0]] if row and row[0] else ["primary"]
    datasets = await asyncio.gather(*(fetch(calendar) for calendar in calendars))
    events = [{"title": (event.get("summary") or "Событие")[:230],
               "start": event.get("start", {}), "end": event.get("end", {}),
               "url": event.get("htmlLink", ""), "location": event.get("location", "")[:350]}
              for data in datasets for event in data.get("items", []) if event.get("status") != "cancelled"]
    events.sort(key=lambda event: event["start"].get("dateTime") or event["start"].get("date") or "")
    return {"events": events[:30], "more": len(events) > 30 or any(data.get("nextPageToken") for data in datasets)}


def notion_title(item):
    title = next((v.get("title") for v in item.get("properties", {}).values() if v.get("type") == "title"), None)
    return "".join(run.get("plain_text", "") for run in (title or []))[:240]


def gmail_text(part):
    if part.get("mimeType", "").lower() == "text/plain":
        value = part.get("body", {}).get("data", "")
        try:
            return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode("utf-8", errors="replace")[:12000]
        except ValueError:
            return ""
    return next((text for child in part.get("parts", []) if (text := gmail_text(child))), "")


async def read(store, session, uid, args):
    provider = args["provider"]
    query, item = args.get("query", ""), args.get("item", "")

    async def get(method, path, **kwargs):
        return await connections.call(store, session, uid, provider, method, path, **kwargs)

    if provider == "calendar":
        return await google_agenda(store, session, uid, args.get("start", ""), args.get("end", ""))
    if provider == "notion":
        if item:
            page = item_id(item, notion=True)
            data = await get("GET", f"/pages/{page}/markdown")
            return {"id": page, "url": f"https://www.notion.so/{page.replace('-', '')}",
                    "text": data.get("markdown", "")[:13000], "truncated": bool(data.get("truncated"))}
        data = await get("POST", "/search", json={
            "query": query[:240], "page_size": 15, "filter": {"property": "object", "value": "page"},
        })
        return {"pages": [{"id": p["id"], "title": notion_title(p), "url": p.get("url", "")}
                          for p in data.get("results", [])]}
    if provider == "drive":
        if item:
            if item.startswith("https://"):
                parsed = urlsplit(item)
                if parsed.hostname not in ("docs.google.com", "drive.google.com"):
                    raise ValueError("Укажи ссылку на файл Google Drive.")
                match = re.search(r"/d/([A-Za-z0-9_-]+)", parsed.path)
                item = match[1] if match else ""
            file = item_id(item)
            path = f"{DRIVE}/{file}"
            data = await get("GET", path, params={"fields": "id,name,mimeType,webViewLink"})
            mime = data.get("mimeType")
            if mime == DOCUMENT:
                text = await get("GET", path + "/export", params={"mimeType": "text/plain"}, as_text=True)
            elif mime in ("text/plain", "text/markdown", "text/csv", "application/json"):
                text = await get("GET", path, params={"alt": "media"}, as_text=True)
            else:
                raise ValueError("Пока могу прочитать Google Docs, Markdown, TXT, CSV и JSON.")
            return {"id": file, "title": data.get("name", ""), "url": data.get("webViewLink", ""), "text": text[:13000]}
        q = "trashed = false"
        if query:
            escaped = query[:240].replace(chr(92), chr(92)*2).replace("'", chr(92) + "'")
            q += f" and name contains '{escaped}'"
        data = await get("GET", DRIVE, params={"q": q, "pageSize": 20, "fields": "nextPageToken,files(id,name,mimeType,webViewLink)"})
        return {"files": data.get("files", []), "more": bool(data.get("nextPageToken"))}
    if provider == "gmail":
        if item:
            data = await get("GET", f"{GMAIL}/messages/{item_id(item)}", params={"format": "full"})
            payload = data.get("payload", {})
            headers = {h["name"].lower(): h["value"][:350] for h in payload.get("headers", [])
                       if h["name"].lower() in ("from", "subject", "date")}
            return {"id": data.get("id"), "thread": data.get("threadId"), **headers,
                    "text": gmail_text(payload) or data.get("snippet", "")[:800]}
        data = await get("GET", GMAIL + "/messages", params={"q": query[:240] or "is:unread", "maxResults": 8})
        async def summary(message):
            meta = await get("GET", f"{GMAIL}/messages/{item_id(message['id'])}",
                             params=[("format", "metadata"), ("metadataHeaders", "From"), ("metadataHeaders", "Subject"),
                                     ("fields", "id,threadId,snippet,payload(headers)")])
            headers = {h["name"].lower(): h["value"][:350] for h in meta.get("payload", {}).get("headers", [])
                       if h["name"].lower() in ("from", "subject")}
            return {"id": meta["id"], "thread": meta.get("threadId"), **headers, "snippet": meta.get("snippet", "")[:450]}
        messages = await asyncio.gather(*(summary(message) for message in data.get("messages", [])[:8]))
        return {"messages": messages, "more": bool(data.get("nextPageToken"))}
    raise ValueError("Неизвестное подключение.")


def prepare(store, uid, project_id, key, args):
    provider = args["provider"]
    action = WRITES.get(provider)
    if not action:
        raise ValueError("Неизвестное подключение.")
    if connections.statuses(store, uid)[provider] != "connected":
        raise ValueError(f"Подключи {connections.NAMES[provider]} в настройках Алефа.")
    if provider == "calendar":
        zone = ZoneInfo(store.get_settings(uid)["timezone"])
        start, end = date_time(args.get("start"), zone), date_time(args.get("end"), zone)
        if not start < end <= start + timedelta(days=31):
            raise ValueError("Проверь начало и конец события.")
        payload = {"title": bounded_text(args.get("title", ""), 220, required=True),
                   "start": start.isoformat(), "end": end.isoformat(),
                   "text": bounded_text(args.get("text", ""), 1400)}
    elif provider == "notion":
        payload = {"item": item_id(args.get("item", ""), notion=True),
                   "text": bounded_text(args.get("text", ""), 3000, required=True)}
    else:
        payload = {"title": bounded_text(args.get("title", ""), 220, required=True),
                   "text": bounded_text(args.get("text", ""), 3000, required=True)}
        if provider == "gmail":
            recipient = args.get("recipient", "")
            if not re.fullmatch(r"[^\s<>,@]+@[^\s<>,@]+\.[^\s<>,@]+", recipient) or len(recipient) > 254:
                raise ValueError("Укажи один адрес получателя.")
            payload["recipient"] = recipient
    update_id = int(key.split(":")[1])
    store.db.execute("DELETE FROM connection_actions WHERE expires<=?", (time.time(),))
    existing = for_update(store, uid, update_id)
    if existing:
        return {"awaiting_confirmation": True, "service": connections.NAMES[existing["provider"]]}
    store.db.execute("""INSERT OR IGNORE INTO connection_actions
        (id,user_id,project_id,update_id,provider,action,payload,expires,request_key) VALUES(?,?,?,?,?,?,?,?,?)
    """, (secrets.token_urlsafe(12), uid, project_id, update_id, provider, action,
          json.dumps(payload, ensure_ascii=False), time.time() + 1800, key))
    return {"awaiting_confirmation": True, "service": connections.NAMES[provider]}


def for_update(store, uid, update_id):
    return store.db.execute("""SELECT * FROM connection_actions
        WHERE user_id=? AND update_id=? AND status='pending' AND expires>? LIMIT 1
    """, (uid, update_id, time.time())).fetchone()


def preview(action, russian):
    provider = action["provider"]
    payload = json.loads(action["payload"])
    heading = {
        "calendar": ("Создать событие в Google Calendar?", "Create this event in Google Calendar?"),
        "notion": ("Добавить заметку в Notion?", "Append this note to Notion?"),
        "drive": ("Создать документ в Google Drive?", "Create a document in Google Drive?"),
        "gmail": ("Сохранить черновик в Gmail?", "Save this draft in Gmail?"),
    }
    items = [heading[provider][not russian]]
    if "title" in payload:
        items.append(payload["title"])
    if "start" in payload:
        items.append(" → ".join(payload[value].replace("T", " ")[:22] for value in ("start", "end")))
    if "item" in payload:
        items.append("https://www.notion.so/" + payload["item"].replace("-", ""))
    if "recipient" in payload:
        items.append(("Кому: " if russian else "To: ") + payload["recipient"])
    if payload["text"]:
        items.append(payload["text"])
    return "\n\n".join(items)


def keyboard(action, russian):
    return {"inline_keyboard": [[
        {"text": "Подтвердить" if russian else "Confirm", "callback_data": "ext:yes:" + action["id"]},
        {"text": "Отмена" if russian else "Cancel", "callback_data": "ext:no:" + action["id"]},
    ]]}


async def execute(store, session, uid, action):
    provider, identifier = action["provider"], action["id"]
    payload = json.loads(action["payload"])

    async def call(method, path, **kwargs):
        return await connections.call(store, session, uid, provider, method, path, **kwargs)

    if provider == "calendar":
        # Use one secondary calendar per connection. A locally persisted ID keeps retries from creating duplicate calendars.
        if not hasattr(store, "calendar_creation_lock"):
            store.calendar_creation_lock = asyncio.Lock()
        async with store.calendar_creation_lock:
            row = store.db.execute("SELECT calendar_id FROM connections WHERE user_id=? AND provider='calendar'", (uid,)).fetchone()
            if not row:
                raise connections.ConnectionError("Календарь отключён.")
            calendar = row[0]
            if not calendar:
                calendar = (await call("POST", CALENDAR + "/calendars", json={"summary": "Aleph"}))["id"]
                store.db.execute("UPDATE connections SET calendar_id=? WHERE user_id=? AND provider='calendar'", (calendar, uid))
        result = await call("POST", CALENDAR + "/calendars/" + quote(calendar, safe="") + "/events", json={
            "id": identifier.encode().hex(), "summary": payload["title"], "description": payload["text"],
            "start": {"dateTime": payload["start"]}, "end": {"dateTime": payload["end"]},
        })
        return result.get("htmlLink", "")
    if provider == "notion":
        text = payload["text"]
        await call("PATCH", f"/blocks/{payload['item']}/children", json={
            "children": [{"type": "paragraph", "paragraph": {"rich_text": [
                {"type": "text", "text": {"content": text[i:i+1800]}} for i in range(0, len(text), 1800)
            ]}}],
        })
        return f"https://www.notion.so/{payload['item'].replace('-', '')}"
    if provider == "drive":
        content = aiohttp.MultipartWriter("related")
        content.append_json({"name": payload["title"], "mimeType": DOCUMENT})
        content.append(payload["text"], {"Content-Type": "text/plain; charset=utf-8"})
        data = await call("POST", "/upload" + DRIVE, data=content,
                          params={"uploadType": "multipart", "fields": "id,webViewLink"})
        return data.get("webViewLink") or f"https://docs.google.com/document/d/{data['id']}/edit"
    message = EmailMessage()
    message["To"], message["Subject"] = payload["recipient"], payload["title"]
    message.set_content(payload["text"])
    result = await call("POST", GMAIL + "/drafts", json={
        "message": {"raw": base64.urlsafe_b64encode(message.as_bytes()).decode().rstrip("=")},
    })
    return f"https://mail.google.com/mail/u/0/#drafts/{result['id']}"


async def handle(store, telegram, query):
    match = re.fullmatch(r"ext:(yes|no):([A-Za-z0-9_-]{16})", str(query.get("data", "")))
    message = query.get("message") or {}
    chat = message.get("chat") or {}
    uid = (query.get("from") or {}).get("id")
    if chat.get("type") != "private" or uid != chat.get("id") or not match:
        return
    approve, identifier = match.groups()
    ru = ((query.get("from") or {}).get("language_code") or "ru").startswith(("ru", "uk", "be", "kk"))
    status = "running" if approve == "yes" else "cancelled"
    row = store.db.execute("""UPDATE connection_actions SET status=?
        WHERE id=? AND user_id=? AND status='pending' AND expires>? RETURNING *
    """, (status, identifier, uid, time.time())).fetchone()
    if not row:
        await telegram.call("answerCallbackQuery", callback_query_id=query["id"],
                            text="Запрос уже обработан или истёк." if ru else "This request was handled or has expired.")
        return
    from .bot import TelegramError

    try:
        await telegram.call("answerCallbackQuery", callback_query_id=query["id"])
    except TelegramError:
        # A callback acknowledgement can expire while the recorded approval remains valid.
        pass
    if approve == "no":
        result = "Отменено." if ru else "Cancelled."
    else:
        try:
            url = await execute(store, telegram.session, uid, row)
        except (connections.ConnectionError, ValueError, KeyError):
            status = "failed"
            result = "Не удалось подтвердить результат. Проверь сервис перед повтором." if ru else "Couldn’t confirm the result. Check the service before trying again."
        else:
            status = "done"
            result = ("Готово." if ru else "Done.") + ("\n" + url if url else "")
        store.db.execute("UPDATE connection_actions SET status=? WHERE id=?", (status, identifier))
    await telegram.call("editMessageText", chat_id=uid, message_id=message["message_id"], text=result,
                        reply_markup={"inline_keyboard": []})
