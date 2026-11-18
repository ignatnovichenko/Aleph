"""First-party HTML cards inside Telegram messages."""
import json
import math
from pathlib import Path
import re
import zlib

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
    return store.db.execute("SELECT * FROM widgets WHERE update_id=?", (update_id,)).fetchone()


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
    bit = 1 << index
    mask = (row["checked"] | bit) if on else (row["checked"] & ~bit)
    store.db.execute("UPDATE widgets SET checked=? WHERE key=?", (mask, row["key"]))
    return uid, message_id, chat_state(mask)


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
