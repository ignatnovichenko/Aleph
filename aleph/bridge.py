"""Telegram's native keyboard Mini App transport for static hosting."""
import base64
import gzip
import json
import re

from .feeds import feed_catalog
from . import projects


def keyboard_markup(store, uid, base_url):
    who = store.get_user(uid)
    settings = store.get_settings(uid)
    settings.pop("assistant_instructions")
    state = {"user": {"first_name": who["first_name"], "language_code": who["language_code"]},
             "settings": settings, "feeds": store.list_feeds(uid),
             "feed_catalog": feed_catalog(), "integrations": {"rss": True, "calendar": True},
             "tasks": [], "feed_items": [], "routines": []}
    current = projects.active(store, uid)
    state["active_project_id"] = current
    state["projects"] = [{"id": x["id"], "name": x["name"][:64], "context": "", "open_tasks": x["open_tasks"]}
                         for x in projects.list_projects(store, uid) if x["id"] == current]
    tasks = store.list_tasks(uid)

    def link():
        data = json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode()
        compressed = base64.urlsafe_b64encode(gzip.compress(data, mtime=0)).decode().rstrip("=")
        return base_url.rstrip("/") + "/#aleph=" + compressed

    # Keep Telegram's button URL compact. Preserve complete tasks for editing.
    state["snapshot_total"] = len(tasks)
    for routine in store.list_routines(uid)[:8]:
        state["routines"].append(routine)
        if len(link()) > 1080:
            state["routines"].pop()
            break
    for task in tasks[:30]:
        state["tasks"].append({key: value for key, value in task.items() if key != "source"})
        if len(link()) > 1800:
            state["tasks"].pop()
            break
    label = "Открыть задачи" if who["language_code"] in ("", "ru", "uk", "be") else "Open tasks"
    return {"keyboard": [[{"text": label, "web_app": {"url": link()}}]],
            "resize_keyboard": True, "one_time_keyboard": True, "is_persistent": False}


def apply_web_action(store, uid, raw_data, request_key):
    if not isinstance(raw_data, str) or len(raw_data.encode()) > 4096:
        raise ValueError("Некорректное действие.")
    packet = json.loads(raw_data)
    if not isinstance(packet, dict) or packet.get("v") != 1:
        raise ValueError("Открой план заново.")
    path, method, fields = packet.get("path"), packet.get("method"), packet.get("body", {})
    if not isinstance(fields, dict) or not isinstance(path, str) or not isinstance(method, str):
        raise ValueError("Некорректное действие.")
    ru = store.get_user(uid)["language_code"] in ("", "ru", "uk", "be")
    if (method, path) == ("POST", "/api/tasks"):
        if fields.keys() - {"title", "notes", "due_at", "project_id", "remind_at"}:
            raise ValueError("Неизвестное поле задачи.")
        reminder = {"remind_at": fields["remind_at"]} if "remind_at" in fields else {}
        task = store.create_task(uid, fields.get("title"), fields.get("due_at"), fields.get("notes", ""), request_key=request_key, project_id=fields.get("project_id", 0), **reminder)
        return ("Добавил в план: " if ru else "Added to your plan: ") + task["title"]
    match = re.fullmatch(r"/api/tasks/(\d{1,18})", path)
    if method == "PATCH" and match:
        task = store.update_task(uid, int(match[1]), fields)
        return ("Сохранил изменения: " if ru else "Updated: ") + task["title"]
    if (method, path) == ("PATCH", "/api/settings"):
        store.patch_settings(uid, fields)
        return "Настройки сохранены." if ru else "Settings saved."
    if (method, path) == ("POST", "/api/feeds"):
        feed = store.add_feed(uid, fields.get("url"))
        return ("Подключил " if ru else "Subscribed to ") + feed["title"]
    match = re.fullmatch(r"/api/feeds/(\d{1,18})", path)
    if method == "DELETE" and match:
        store.delete_feed(uid, int(match[1]))
        return "Отписался от источника." if ru else "Unsubscribed."
    raise ValueError("Неизвестное действие.")
