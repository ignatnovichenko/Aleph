"""Actions on a delivered reminder, scoped to its original user and schedule."""
from datetime import datetime, timedelta, timezone
import re

from .store import timestamp


def keyboard(task_id, scheduled, russian):
    version = int(datetime.fromisoformat(scheduled).timestamp())
    return {"inline_keyboard": [[
        {"text": "Готово" if russian else "Done", "callback_data": f"rem:done:{task_id}:{version}"},
        {"text": "Через час" if russian else "In an hour", "callback_data": f"rem:later:{task_id}:{version}"},
    ]]}


async def handle(store, telegram, query):
    match = re.fullmatch(r"rem:(done|later):([0-9]{1,18}):(-?[0-9]{1,12})", str(query.get("data", "")))
    message = query.get("message") or {}
    chat = message.get("chat") or {}
    uid = (query.get("from") or {}).get("id")
    if chat.get("type") != "private" or uid != chat.get("id") or not match:
        return
    ru = ((query.get("from") or {}).get("language_code") or "ru").startswith(("ru", "uk", "be", "kk"))
    action, task_id, version = match.groups()
    try:
        task = store.get_task(uid, int(task_id))
        reminder = task["remind_at"]
        if task["status"] != "open" or not reminder or int(datetime.fromisoformat(reminder).timestamp()) != int(version):
            raise KeyError
    except KeyError:
        feedback = "Поручение уже изменилось." if ru else "This task has already changed."
    else:
        patch = {"status": "done"} if action == "done" else {
            "remind_at": timestamp(datetime.now(timezone.utc) + timedelta(hours=1)),
        }
        store.update_task(uid, int(task_id), patch)
        feedback = ("Готово." if ru else "Done.") if action == "done" else (
            "Напомню через час." if ru else "I’ll remind you in an hour.")
    await telegram.call("answerCallbackQuery", callback_query_id=query["id"], text=feedback)
    await telegram.call("editMessageReplyMarkup", chat_id=uid, message_id=message["message_id"],
                        reply_markup={"inline_keyboard": []})
