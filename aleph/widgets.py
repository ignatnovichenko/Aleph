"""First-party HTML cards inside Telegram messages."""
import json
import math
from pathlib import Path
import re
import zlib

from .links import widget_url

TEMPLATE = Path(__file__).with_name("widget_template.html").read_text(encoding="utf-8")
KINDS = {"checklist": 6, "compare": 4, "chart": 7}
TOOL = {"type": "function", "function": {
    "name": "create_widget",
    "description": (
        "Answer with a native interactive HTML widget inside this Telegram message. "
        "Use when tapping is useful or the user requests a widget. A checklist saves its own check marks; "
        "compare provides selectable options or expandable sections; chart lets the user inspect data points. "
        "Provide complete widget content. Do not repeat it in the final reply."
    ),
    "parameters": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "kind": {"type": "string", "enum": list(KINDS)},
            "title": {"type": "string", "description": "Clear title, at most 90 characters."},
            "unit": {"type": "string", "description": "Chart only. Unit such as ₽, hours, %. Up to 18 characters."},
            "items": {
                "type": "array", "minItems": 2, "maxItems": 7,
                "items": {
                    "type": "object", "additionalProperties": False, "required": ["label"],
                    "properties": {
                        "label": {"type": "string", "description": "Up to 72 characters. For compare tabs use a short name."},
                        "detail": {"type": "string", "description": "Compare: option description, up to 420 chars. Checklist: short optional detail, up to 100 chars."},
                        "value": {"type": "number", "description": "Chart only, numeric value."},
                    },
                },
            },
        },
        "required": ["kind", "title", "items"],
    },
}}


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS widgets (
            key TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            project_id INTEGER NOT NULL,
            update_id INTEGER REFERENCES bot_inbox(id) ON DELETE SET NULL,
            message_id INTEGER,
            spec TEXT NOT NULL,
            checked INTEGER NOT NULL DEFAULT 0,
            UNIQUE(user_id, message_id),
            UNIQUE(update_id)
        );
    """)
    columns = {column[1] for column in db.execute("PRAGMA table_info(widgets)")}
    if "fallback" not in columns:
        db.execute("ALTER TABLE widgets ADD COLUMN fallback INTEGER NOT NULL DEFAULT 0")
    if "selected" not in columns:
        db.execute("ALTER TABLE widgets ADD COLUMN selected INTEGER NOT NULL DEFAULT 0")
    db.execute("UPDATE widgets SET fallback=1,message_id=NULL WHERE message_id=-1")


def render(spec):
    # No model-provided HTML or JavaScript enters the Telegram sandbox.
    payload = json.dumps(spec, ensure_ascii=False, separators=(",", ":"))
    for character in "<>&\u2028\u2029":
        payload = payload.replace(character, f"\\u{ord(character):04x}")
    body = TEMPLATE.replace("__WIDGET_SPEC__", payload, 1)
    raw = body.encode("utf-8")
    if len(raw) > 32768 or len(zlib.compress(raw, 9)) > 4096:
        raise ValueError("Виджет слишком большой. Сократи подписи или детали.")
    return body


def clean(fields):
    from .store import bounded_text

    if not isinstance(fields, dict) or fields.keys() - {"kind", "title", "unit", "items"}:
        raise ValueError("Некорректные поля виджета.")
    kind, items = fields.get("kind"), fields.get("items")
    if not isinstance(kind, str) or kind not in KINDS:
        raise ValueError("Доступны чек-лист, сравнение и диаграмма.")
    if not isinstance(items, list) or not 2 <= len(items) <= KINDS[kind]:
        raise ValueError(f"Нужно от двух до {KINDS[kind]} элементов.")
    result = {"kind": kind, "title": bounded_text(fields.get("title"), 90, required=True), "items": []}
    if kind == "chart":
        result["unit"] = bounded_text(fields.get("unit", ""), 18)
    for item in items:
        if not isinstance(item, dict) or item.keys() - {"label", "detail", "value"}:
            raise ValueError("Некорректный элемент виджета.")
        entry = {"label": bounded_text(item.get("label"), 72, required=True)}
        if kind == "chart":
            value = item.get("value")
            if type(value) not in (int, float) or abs(value) > 1e12 or not math.isfinite(value):
                raise ValueError("Каждому элементу графика нужно конечное число по модулю до 10¹².")
            entry["value"] = value
        else:
            entry["detail"] = bounded_text(item.get("detail", ""), 420 if kind == "compare" else 100,
                                           required=kind == "compare")
        result["items"].append(entry)
    render(result)
    return result


def fallback_text(spec):
    def line(item):
        if spec["kind"] == "chart":
            return f"• {item['label']} — {item['value']:g} {spec['unit']}".rstrip()
        detail = item["detail"]
        return f"• {item['label']}" + (" — " + detail if detail else "")
    return spec["title"] + "\n\n" + "\n".join(map(line, spec["items"]))


def enqueue(store, uid, project_id, request_key, fields):
    update_id = int(request_key.split(":")[1])
    saved = for_update(store, update_id)
    if saved and saved["key"] != request_key:
        raise ValueError("Для одного ответа доступен один виджет.")
    spec = clean(fields)
    store.db.execute("INSERT OR IGNORE INTO widgets(key,user_id,project_id,update_id,spec) VALUES (?,?,?,?,?)",
                     (request_key, uid, project_id, update_id, json.dumps(spec, ensure_ascii=False)))
    return {"widget_prepared": spec["kind"], "title": spec["title"]}


def for_update(store, update_id):
    return store.db.execute("SELECT rowid AS id,* FROM widgets WHERE update_id=?", (update_id,)).fetchone()


def mark_sent(store, key, message_id):
    store.db.execute("UPDATE widgets SET message_id=? WHERE key=?", (message_id, key))


def chat_state(mask):
    return json.dumps({"checked": mask}, separators=(",", ":"))


def change_check(store, query):
    sender, chat = query.get("from", {}), query.get("chat", {})
    uid, message_id = sender.get("id"), query.get("message_id")
    kind = query.get("type")
    data = query.get("data")
    if (type(uid) is not int or not uid or chat.get("type") != "private" or chat.get("id") != uid
            or type(message_id) is not int or sender.get("is_bot") or kind != {"type": "data"}
            or not isinstance(data, str)):
        return None
    action = re.fullmatch(r"check:([0-5]):([01])", data)
    if not action:
        return None
    row = store.db.execute("SELECT * FROM widgets WHERE user_id=? AND message_id=?", (uid, message_id)).fetchone()
    if not row:
        return None
    spec = json.loads(row["spec"])
    index, on = int(action[1]), action[2] == "1"
    if spec["kind"] != "checklist" or index >= len(spec["items"]):
        return None
    mask = set_checked(store, row, index, on)
    return uid, message_id, chat_state(mask)


def view(store, uid, widget_id):
    row = store.db.execute("SELECT rowid AS id,* FROM widgets WHERE rowid=? AND user_id=?", (widget_id, uid)).fetchone()
    if row is None:
        raise KeyError("Виджет недоступен.")
    return row


def set_checked(store, row, index, on):
    spec = json.loads(row["spec"])
    if (spec["kind"] != "checklist" or type(index) is not int or type(on) is not bool
            or not 0 <= index < len(spec["items"])):
        raise ValueError("Некорректная отметка.")
    bit = 1 << index
    mask = (row["checked"] | bit) if on else (row["checked"] & ~bit)
    store.db.execute("UPDATE widgets SET checked=? WHERE key=?", (mask, row["key"]))
    return mask


async def handle_query(store, telegram, query):
    result = change_check(store, query)
    if result is None:
        await telegram.call("answerWidgetQuery", widget_query_id=query["id"], data='{"error":"unavailable"}')
        return
    uid, message_id, state = result
    try:
        await telegram.call("answerWidgetQuery", widget_query_id=query["id"], data=state)
    finally:
        await telegram.call("editMessageWidgetData", chat_id=uid, message_id=message_id, chat_data=state)


def mark_fallback(store, key):
    store.db.execute("UPDATE widgets SET fallback=1 WHERE key=?", (key,))


def card(row, *, russian=True):
    spec = json.loads(row["spec"])
    rows = spec["items"]
    identity = row["id"]
    bullets = []
    buttons = []
    if spec["kind"] == "checklist":
        for index, item in enumerate(rows):
            checked = bool(row["checked"] & (1 << index))
            icon = "✓" if checked else "○"
            bullets.append(f"{icon} {item['label']}" + (f" — {item['detail']}" if item["detail"] else ""))
            buttons.append([{"text": f"{icon} {item['label'][:36]}", "callback_data": f"w:{identity}:{index}:{int(not checked)}"}])
        count = sum(bool(row["checked"] & (1 << i)) for i in range(len(rows)))
        title = spec["title"] + f" · {count}/{len(rows)}"
    else:
        title = spec["title"]
        index = min(row["selected"], len(rows) - 1)
        selected = rows[index]
        if spec["kind"] == "compare":
            bullets = [selected["label"], selected["detail"]]
        else:
            bullets = [
                ("● " if i == index else "○ ") + f"{item['label']} — {item['value']:g} {spec['unit']}".rstrip()
                for i, item in enumerate(rows)
            ]
        opts = [{"text": ("✓ " if i == index else "") + item["label"][:22],
                 "callback_data": f"w:{identity}:{i}"} for i, item in enumerate(rows)]
        buttons = [opts[i:i+2] for i in range(0, len(opts), 2)]
    url = widget_url(identity)
    if url:
        buttons.append([{"text": "Открыть HTML-виджет" if russian else "Open HTML widget", "web_app": {"url": url}}])
    note = ("Telegram не позволил встроить HTML. Кнопки работают в этом сообщении." if russian
            else "Telegram didn’t allow inline HTML here. Use the buttons on this message.")
    return {"text": title + "\n\n" + "\n".join(bullets) + "\n\n" + note,
            "reply_markup": {"inline_keyboard": buttons}, "link_preview_options": {"is_disabled": True}}


async def handle_fallback_query(store, telegram, query):
    action = re.fullmatch(r"w:([1-9]\d{0,17}):([0-6])(?::([01]))?", query.get("data") or "")
    if not action:
        return
    sender = query.get("from") or {}
    uid = sender.get("id")
    message = query.get("message") or {}
    chat = message.get("chat") or {}
    if chat.get("type") != "private" or chat.get("id") != uid or type(uid) is not int or sender.get("is_bot"):
        await telegram.call("answerCallbackQuery", callback_query_id=query["id"])
        return
    row = store.db.execute("""SELECT rowid AS id,* FROM widgets
        WHERE rowid=? AND user_id=? AND message_id=? AND fallback=1""",
        (int(action[1]), uid, message.get("message_id"))).fetchone()
    if not row:
        await telegram.call("answerCallbackQuery", callback_query_id=query["id"], text="Карточка недоступна.")
        return
    spec = json.loads(row["spec"])
    index = int(action[2])
    checklist = spec["kind"] == "checklist"
    if index >= len(spec["items"]) or checklist != (action[3] is not None):
        await telegram.call("answerCallbackQuery", callback_query_id=query["id"])
        return
    if checklist:
        set_checked(store, row, index, action[3] == "1")
    else:
        store.db.execute("UPDATE widgets SET selected=? WHERE key=?", (index, row["key"]))
    await telegram.call("answerCallbackQuery", callback_query_id=query["id"])
    await refresh_card(store, telegram, view(store, uid, row["id"]))


async def refresh_card(store, telegram, row):
    uid = row["user_id"]
    language = store.get_user(uid)["language_code"].lower().split("-")[0]
    await telegram.call("editMessageText", chat_id=uid, message_id=row["message_id"],
                        **card(row, russian=language in {"", "ru", "be", "uk"}))
