"""Authenticated Mini App API. Telegram signs the initData received by the client."""
import asyncio
from datetime import datetime
import hashlib
import hmac
import json
import re
import time
from urllib.parse import parse_qsl

import aiohttp
from aiohttp import web

from .feeds import feed_catalog
from . import projects, widgets
from .store import UTC


def signed_user(raw, token, *, now=None):
    if not raw or len(raw) > 16384:
        raise ValueError("Missing Telegram session")
    pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True, max_num_fields=64)
    data = dict(pairs)
    if len(data) != len(pairs):
        raise ValueError("Duplicate Telegram parameters")
    digest = data.pop("hash", "")
    if not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError("Invalid Telegram signature")
    check = "\n".join(f"{key}={value}" for key, value in sorted(data.items()))
    secret = hmac.digest(b"WebAppData", token.encode(), hashlib.sha256)
    if not hmac.compare_digest(hmac.digest(secret, check.encode(), hashlib.sha256).hex(), digest):
        raise ValueError("Invalid Telegram signature")
    age = (time.time() if now is None else now) - int(data.get("auth_date", "0"))
    if age < -60 or age > 86400:
        raise ValueError("Expired Telegram session")
    user = json.loads(data.get("user", "null"))
    if not isinstance(user, dict) or type(user.get("id")) is not int or not 0 < user["id"] < 2**63:
        raise ValueError("Invalid Telegram user")
    if not isinstance(user.get("first_name", ""), str) or not isinstance(user.get("language_code", ""), str):
        raise ValueError("Invalid Telegram user")
    return user


def ical_text(s):
    return s.replace("\\", "\\\\").replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n").replace(";", "\\;").replace(",", "\\,")


def fold_ical(line):
    result = []
    current = bytearray()
    for ch in line:
        value = ch.encode()
        if len(current) + len(value) > 75:
            result.append(current.decode())
            current = bytearray(b" ")
        current.extend(value)
    result.append(current.decode())
    return "\r\n".join(result)


def calendar_export(tasks, uid):
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Lab One//Aleph//EN",
             "CALSCALE:GREGORIAN", "X-WR-CALNAME:Aleph"]

    def fmt(value):
        return value.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")

    for task in tasks:
        if not task["due_at"] or task["status"] != "open":
            continue
        start = datetime.fromisoformat(task["due_at"])
        lines.extend(["BEGIN:VEVENT", f"UID:{uid}-{task['id']}@aleph.labone",
                      "DTSTAMP:" + fmt(datetime.fromisoformat(task["updated_at"])),
                      "DTSTART:" + fmt(start), "DURATION:PT15M",
                      "SUMMARY:" + ical_text(task["title"]), "DESCRIPTION:" + ical_text(task["notes"]),
                      "END:VEVENT"])
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold_ical(line) for line in lines) + "\r\n"


def install_api(app, store, token):
    async def body(request):
        if request.content_type != "application/json":
            raise ValueError("Ожидается JSON.")
        if request.content_length is not None and request.content_length > 16384:
            raise ValueError("Слишком большой запрос.")
        chunks = bytearray()
        async for chunk in request.content.iter_chunked(4096):
            chunks.extend(chunk)
            if len(chunks) > 16384:
                raise ValueError("Слишком большой запрос.")
        try:
            fields = json.loads(chunks.decode())
        except (ValueError, RecursionError):
            raise ValueError("Проверь поля запроса.") from None
        if not isinstance(fields, dict):
            raise ValueError("Ожидается объект JSON.")
        return fields

    def scoped(handler):
        async def wrapped(request):
            auth = request.headers.get("Authorization", "")
            try:
                if not auth.startswith("tma "):
                    raise ValueError
                user = signed_user(auth[4:], token)
            except (ValueError, OverflowError, RecursionError):
                return web.json_response({"error": "Открой мини-приложение заново из Telegram."}, status=401,
                                         headers={"Cache-Control": "no-store"})
            profile = store.ensure_user(user["id"], user.get("first_name", ""), user.get("language_code", ""))
            try:
                response = await handler(request, profile)
            except KeyError as error:
                response = web.json_response({"error": str(error.args[0])}, status=404)
            except ValueError as error:
                response = web.json_response({"error": str(error)}, status=400)
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
            return response
        return wrapped

    async def state(request, user):
        uid = user["id"]
        return web.json_response({"user": user, "tasks": store.list_tasks(uid),
                                  "projects": projects.list_projects(store, uid), "active_project_id": projects.active(store, uid),
                                  "routines": store.list_routines(uid), "settings": store.get_settings(uid), "integrations": {"rss": True, "calendar": True},
                                  "feeds": store.list_feeds(uid), "feed_catalog": feed_catalog(),
                                  "feed_items": store.feed_items(uid)})

    async def create(request, user):
        fields = await body(request)
        if fields.keys() - {"title", "notes", "due_at", "project_id"}:
            raise ValueError("Неизвестное поле задачи.")
        request_key = request.headers.get("Idempotency-Key", "")
        if request_key and not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", request_key):
            raise ValueError("Проверь идентификатор запроса.")
        task = store.create_task(user["id"], fields.get("title"), fields.get("due_at"),
                                 fields.get("notes", ""), "api:" + request_key if request_key else None,
                                 project_id=fields.get("project_id", 0))
        return web.json_response(task, status=201)

    async def update(request, user):
        return web.json_response(store.update_task(user["id"], int(request.match_info["id"]), await body(request)))

    async def settings(request, user):
        return web.json_response(store.patch_settings(user["id"], await body(request)))

    async def calendar(request, user):
        data = calendar_export(store.list_tasks(user["id"], include_done=False), user["id"])
        return web.Response(text=data, content_type="text/calendar", charset="utf-8",
                            headers={"Content-Disposition": 'attachment; filename="aleph.ics"'})

    async def list_feeds(request, user):
        return web.json_response({"feeds": store.list_feeds(user["id"]), "feed_catalog": feed_catalog()})

    async def add_feed(request, user):
        fields = await body(request)
        return web.json_response(store.add_feed(user["id"], fields.get("url")), status=201)

    async def remove_feed(request, user):
        store.delete_feed(user["id"], int(request.match_info["id"]))
        return web.Response(status=204)

    async def create_project(request, user):
        fields = await body(request)
        if fields.keys() - {"name", "context"}:
            raise ValueError("Неизвестное поле проекта.")
        uid = user["id"]
        item = projects.create(store, uid, fields.get("name"), fields.get("context", ""))
        projects.choose(store, uid, item["id"])
        return web.json_response(item, status=201)

    async def update_project(request, user):
        return web.json_response(projects.edit(store, user["id"], int(request.match_info["id"]), await body(request)))

    async def choose_project(request, user):
        fields = await body(request)
        if set(fields) != {"project_id"}:
            raise ValueError("Выбери проект.")
        return web.json_response(projects.choose(store, user["id"], fields["project_id"]))

    async def widget_view(request, user):
        row = widgets.view(store, user["id"], int(request.match_info["id"]))
        html = widgets.render(json.loads(row["spec"]))
        return web.json_response({"html": html, "chat_data": widgets.chat_state(row["checked"])})

    async def widget_check(request, user):
        row = widgets.view(store, user["id"], int(request.match_info["id"]))
        fields = await body(request)
        if set(fields) != {"index", "checked"}:
            raise ValueError("Некорректная отметка.")
        mask = widgets.set_checked(store, row, fields["index"], fields["checked"])
        if row["fallback"] and row["message_id"]:
            from .bot import Telegram, TelegramError
            async with aiohttp.ClientSession(trust_env=True) as session:
                current = widgets.view(store, user["id"], row["id"])
                try:
                    await asyncio.wait_for(widgets.refresh_card(store, Telegram(session, token), current), 4)
                except (TimeoutError, TelegramError):
                    pass
        return web.json_response({"chat_data": widgets.chat_state(mask)})

    routes = [("GET", "/api/state", state), ("POST", "/api/tasks", create),
              ("PATCH", r"/api/tasks/{id:\d{1,18}}", update), ("PATCH", "/api/settings", settings),
              ("POST", "/api/projects", create_project), ("PATCH", "/api/projects/active", choose_project),
              ("PATCH", r"/api/projects/{id:\d{1,18}}", update_project),
              ("GET", "/api/calendar.ics", calendar), ("GET", "/api/feeds", list_feeds),
              ("POST", "/api/feeds", add_feed), ("DELETE", r"/api/feeds/{id:\d{1,18}}", remove_feed)]
    routes += [("GET", r"/api/widgets/{id:\d{1,18}}", widget_view),
               ("PATCH", r"/api/widgets/{id:\d{1,18}}", widget_check)]
    for method, path, handler in routes:
        app.router.add_route(method, path, scoped(handler))
