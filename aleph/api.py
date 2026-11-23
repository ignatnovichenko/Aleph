"""Authenticated Mini App API. Telegram signs the initData received by the client."""
import asyncio
from datetime import datetime
import hashlib
import hmac
import json
import os
import re
import time
from urllib.parse import parse_qsl, unquote

import aiohttp
from aiohttp import web

from .chatgpt import plan as chatgpt
from .chatgpt_auth import ChatGPTError
from .chatgpt_pair import IMPORT_PATH, MAX_PACKET, owner as chatgpt_owner
from .feeds import feed_catalog
from .links import mini_app_url
from . import articles, computers, connections, drive_picker, integrations, memory, models, project_sources, projects, public_fetch, reports, saved_results, skills, source_text, widgets
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
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Aleph//Calendar//EN",
             "CALSCALE:GREGORIAN", "X-WR-CALNAME:Aleph"]

    def fmt(value):
        return value.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")

    for task in tasks:
        if not task["due_at"] or task["status"] != "open":
            continue
        start = datetime.fromisoformat(task["due_at"])
        lines.extend(["BEGIN:VEVENT", f"UID:{uid}-{task['id']}@aleph.invalid",
                      "DTSTAMP:" + fmt(datetime.fromisoformat(task["updated_at"])),
                      "DTSTART:" + fmt(start), "DURATION:PT15M",
                      "SUMMARY:" + ical_text(task["title"]), "DESCRIPTION:" + ical_text(task["notes"]),
                      "END:VEVENT"])
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold_ical(line) for line in lines) + "\r\n"


def install_api(app, store, token, model_key=None):
    from . import mail_events
    mail_events.install(app, store)
    catalogs = models.ModelCatalogs(model_key)
    github_imports = asyncio.Semaphore(4)
    source_imports = asyncio.Semaphore(4)

    async def source_slot(awaitable, *args):
        if source_imports.locked():
            raise ValueError("Сейчас загружается много источников. Попробуй чуть позже.")
        async with source_imports:
            return await awaitable(*args)

    async def body(request, limit=16384):
        if request.content_type != "application/json":
            raise ValueError("Ожидается JSON.")
        if request.content_length is not None and request.content_length > limit:
            raise ValueError("Слишком большой запрос.")
        chunks = bytearray()
        async for chunk in request.content.iter_chunked(4096):
            chunks.extend(chunk)
            if len(chunks) > limit:
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
        compact = request.query.get("compact") == "1"
        result = {"user": user, "tasks": store.list_tasks(uid),
                                  "projects": projects.list_projects(store, uid), "active_project_id": projects.active(store, uid),
                                  "routines": store.list_routines(uid), "settings": store.get_settings(uid), "integrations": {"rss": True, "calendar": True},
                                  "feeds": store.list_feeds(uid), "feed_catalog": feed_catalog(),
                                  "connections": connections.statuses(store, uid)}
        result["model"] = models.describe(store, uid, catalogs.opencode_model)
        result["model_configurable"] = chatgpt_owner(uid)
        if not compact:
            result["feed_items"] = store.feed_items(uid)
        elif result["model_configurable"]:
            result["chatgpt_connection"] = chatgpt.status(uid)
        response = web.Response(text=json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                                content_type="application/json", zlib_executor_size=64 * 1024)
        if response.content_length > 1024:
            response.enable_compression()
            response.headers["Vary"] = "Accept-Encoding"
        return response

    async def connection(request, user):
        provider = request.match_info["provider"]
        uid = user["id"]
        if request.method == "DELETE":
            connections.disconnect(store, uid, provider)
            return web.json_response(connections.statuses(store, uid))
        return web.json_response({"url": connections.start(store, uid, provider)})

    async def agenda(request, user):
        try:
            async with aiohttp.ClientSession(trust_env=True) as session:
                data = await integrations.google_agenda(store, session, user["id"],
                    request.query.get("start", ""), request.query.get("end", ""))
        except connections.ConnectionError as error:
            return web.json_response({"error": str(error)}, status=503)
        return web.json_response(data)

    async def picker_start(request, user):
        return web.json_response({"url": drive_picker.start(store, user["id"])})

    async def picker_page(request):
        try:
            async with aiohttp.ClientSession(trust_env=True) as session:
                html, policy = await drive_picker.page(store, session, request.query.get("ticket", ""))
        except connections.ConnectionError as error:
            return web.Response(text=str(error), status=400, headers={"Cache-Control": "no-store"})
        return web.Response(text=html, content_type="text/html", headers={
            "Cache-Control": "no-store", "Referrer-Policy": "strict-origin-when-cross-origin",
            "Content-Security-Policy": policy, "X-Content-Type-Options": "nosniff",
        })

    async def oauth_callback(request):
        from html import escape

        provider = request.match_info["provider"]
        try:
            async with aiohttp.ClientSession(trust_env=True) as session:
                await connections.finish(store, session, provider, request.query.get("state", ""), request.query.get("code", ""))
        except (ValueError, KeyError) as error:
            status = 400
            text = escape(str(error.args[0]) if error.args else "Открой Алефа и попробуй ещё раз.")
        else:
            status = 200
            text = f"{connections.NAMES[provider]} подключён. Вернись в Telegram, чтобы продолжить."
        username = os.getenv("ALEPH_BOT_USERNAME", "AlephAgentBot").lstrip("@")
        return_link = (f'<a href="https://t.me/{username}">Открыть Алефа в Telegram</a>'
                       if re.fullmatch(r"[A-Za-z0-9_]{5,32}", username) else "")
        page = (f'<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
                f'<title>Aleph</title><main><h1>Aleph</h1><p>{text}</p>{return_link}</main></html>')
        return web.Response(text=page, status=status, content_type="text/html", headers={
            "Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
        })

    async def project_entries(request, user):
        uid, project_id = user["id"], int(request.match_info["id"])
        if request.method == "GET":
            return web.json_response(projects.entries(store, uid, project_id))
        if request.method == "DELETE":
            projects.remove_entry(store, uid, project_id, int(request.match_info["entry"]))
            return web.Response(status=204)
        fields = await body(request)
        if fields.keys() != {"kind", "text"}:
            raise ValueError("Укажи тип и текст записи.")
        return web.json_response(projects.add_entry(store, uid, project_id, fields["kind"], fields["text"]), status=201)

    async def create(request, user):
        fields = await body(request)
        if fields.keys() - {"title", "notes", "due_at", "project_id", "remind_at", "timezone"}:
            raise ValueError("Неизвестное поле задачи.")
        request_key = request.headers.get("Idempotency-Key", "")
        if request_key and not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", request_key):
            raise ValueError("Проверь идентификатор запроса.")
        uid = user["id"]
        def save(values):
            reminder = {"remind_at": values["remind_at"]} if "remind_at" in values else {}
            return store.create_task(uid, values.get("title"), values.get("due_at"),
                                     values.get("notes", ""), "api:" + request_key if request_key else None,
                                     project_id=values.get("project_id", 0), **reminder)
        task = store.save_task_timezone(uid, fields, save)
        return web.json_response(task, status=201)

    async def update(request, user):
        uid, task_id = user["id"], int(request.match_info["id"])
        fields = await body(request)
        return web.json_response(store.save_task_timezone(uid, fields,
                                 lambda values: store.update_task(uid, task_id, values)))

    async def versions(request,user):
        from . import file_versions
        pid = int(request.match_info["id"])
        projects.get(store,user["id"],pid)
        return web.json_response(file_versions.listing(store,user["id"],pid))

    async def result(request, user):
        from . import file_versions
        task = store.get_task(user["id"],int(request.match_info["id"]))
        files = file_versions.listing(store,user["id"],task["project_id"],task["id"])
        saved = reports.result(store, user["id"], int(request.match_info["id"]))
        return web.json_response({"html": articles.MARKDOWN.render(saved["text"]) if saved["text"] else None,
                                  "files":files, "delivered_at": saved["delivered_at"]})

    async def settings(request, user):
        return web.json_response(store.patch_settings(user["id"], await body(request)))

    async def memories(request, user):
        return web.json_response(memory.list_memories(store.db, user["id"]))

    async def forget_memory(request, user):
        memory.forget(store.db, user["id"], int(request.match_info["id"]))
        return web.Response(status=204)

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
        if fields.keys() - {"name", "context", "activate"} or type(fields.get("activate", True)) is not bool:
            raise ValueError("Неизвестное поле проекта.")
        uid = user["id"]
        item = projects.create(store, uid, fields.get("name"), fields.get("context", ""))
        if fields.get("activate", True):
            projects.choose(store, uid, item["id"])
        return web.json_response(item, status=201)

    async def update_project(request, user):
        return web.json_response(projects.edit(store, user["id"], int(request.match_info["id"]), await body(request)))

    async def computer_list(request, user):
        return web.json_response(computers.list_owned(store, user["id"]))

    async def computer_pairing(request, user):
        fields = await body(request)
        if set(fields) != {"name"}:
            raise ValueError("Укажи имя компьютера.")
        return web.json_response(computers.prepare(store, user["id"], fields["name"]))

    async def remove_computer(request, user):
        computers.remove(store, user["id"], int(request.match_info["id"]))
        return web.Response(status=204)

    async def project_computer(request, user):
        fields = await body(request)
        if set(fields) != {"computer_id"}:
            raise ValueError("Выбери компьютер или сервер Алефа.")
        return web.json_response(computers.assign(store, user["id"], int(request.match_info["id"]), fields["computer_id"]))

    async def register_runner(request):
        try:
            fields = await body(request, limit=1024)
            return web.json_response(computers.pair(store, fields.get("code")), headers={"Cache-Control": "no-store"})
        except ValueError:
            raise web.HTTPUnauthorized() from None

    async def runner(request):
        auth = request.headers.get("Authorization", "")
        runner_session = request.headers.get("X-Aleph-Runner", "")
        try:
            computers.validate_session(runner_session)
            computer_id = computers.authenticate(store, auth[7:] if auth.startswith("Bearer ") else "")
        except ValueError:
            raise web.HTTPUnauthorized() from None
        hub = store.computer_hub
        hub.touch(computer_id)
        command = request.match_info.get("command")
        if command is None:
            return web.json_response(await hub.claim(computer_id, runner_session), headers={"Cache-Control": "no-store"})
        job = hub.job(computer_id, runner_session, request.match_info["job"])
        if not job:
            raise web.HTTPNotFound()
        if command == "alive":
            return web.Response(status=204)
        try:
            fields = await body(request, limit=computers.JOB_REQUEST_LIMIT if command in ("complete","folder") else 262144)
        except ValueError:
            raise web.HTTPBadRequest() from None
        if hub.job(computer_id, runner_session, request.match_info["job"]) is not job:
            raise web.HTTPNotFound()
        if command == "complete":
            return await job.broker.complete_payload(fields)
        if command == "tool":
            return await job.broker.tool_payload(fields)
        if command in ("folder","artifact"):
            import base64
            from . import folder
            broker = job.broker
            match = broker.calls.get(fields.get("id"))
            if not match:
                raise web.HTTPForbidden()
            step,n,call = match
            key = f"tg:{broker.update_id}:{step}:{n}"
            if command == "artifact":
                if call["function"]["name"] not in ("create_document","edit_document"):
                    raise web.HTTPForbidden()
                row = store.db.execute("SELECT filename,data FROM bot_documents WHERE update_id=? AND key=?", (broker.update_id,key)).fetchone()
                if not row:
                    raise web.HTTPNotFound()
                return web.json_response({"filename":row[0],"data":base64.b64encode(row[1]).decode()})
            if call["function"]["name"]!="open_folder_file" or fields.get("arguments")!=json.loads(call["function"]["arguments"]):
                raise web.HTTPForbidden()
            if broker.update_id<0:
                store.db.execute("UPDATE scheduled_reports SET needs_folder=1 WHERE task_id=?",(-broker.update_id,))
            name = folder.filename(fields["arguments"]["filename"])
            data = base64.b64decode(fields["content"],validate=True)
            if not 0<len(data)<=folder.LIMIT:
                raise web.HTTPBadRequest()
            existing = store.db.execute("SELECT id FROM folder_inputs WHERE call_key=?",(key,)).fetchone()
            if existing:
                ident = existing[0]
            else:
                size = store.db.execute("SELECT coalesce(sum(length(data)),0) FROM folder_inputs WHERE update_id=?",(broker.update_id,)).fetchone()[0]
                if size + len(data)>24*1024**2:
                    return web.json_response({"error":"Лимит входных файлов для поручения 24 МБ."})
                ident = store.db.execute("INSERT INTO folder_inputs(update_id,call_key,format,data) VALUES(?,?,?,?) RETURNING id",
                    (broker.update_id,key,name.rsplit(".",1)[1].lower(),data)).fetchone()[0]
            return web.json_response({"file_id":-ident,"filename":name})
        answer = fields.get("answer")
        if isinstance(answer, str) and 0 < len(answer.strip()) <= 14000:
            job.result.set_result({"answer": answer.strip()})
        else:
            job.result.set_result({"error_type": "remote_turn_failed"})
        return web.Response(status=204)

    async def choose_project(request, user):
        fields = await body(request)
        if set(fields) != {"project_id"}:
            raise ValueError("Выбери проект.")
        return web.json_response(projects.enter(store, user["id"], fields["project_id"]))

    async def skill_library(request, user):
        uid = user["id"]
        if request.method == "GET":
            return web.json_response(skills.library(store, uid))
        fields = await body(request, limit=65536)
        return web.json_response(skills.save(store, uid, fields), status=201)

    async def skill_item(request, user):
        uid, skill_id = user["id"], int(request.match_info["id"])
        if request.method == "GET":
            return web.json_response(skills.detail(store, uid, skill_id))
        if request.method == "DELETE":
            skills.remove(store, uid, skill_id)
            return web.Response(status=204)
        return web.json_response(skills.save(store, uid, await body(request, limit=65536), skill_id))

    async def skill_preview(request, user):
        fields = await body(request, limit=100000)
        if fields.keys() == {"markdown"}:
            return web.json_response(skills.parse_markdown(fields["markdown"]))
        if fields.keys() != {"url"}:
            raise ValueError("Вставь ссылку GitHub или выбери SKILL.md.")
        if github_imports.locked():
            raise ValueError("Сейчас много импортов. Попробуй через несколько секунд.")
        async with github_imports, aiohttp.ClientSession(trust_env=True) as session:
            return web.json_response(await skills.preview_github(session, fields["url"]))

    async def project_skills(request, user):
        uid, project_id = user["id"], int(request.match_info["id"])
        if request.method == "PATCH":
            fields = await body(request)
            if fields.keys() != {"skill_ids"}:
                raise ValueError("Укажи скиллы для этого проекта.")
            return web.json_response({"selected": skills.set_project(store, uid, project_id, fields["skill_ids"])})
        selected = [skill["id"] for skill in skills.for_project(store, uid, project_id)]
        return web.json_response({"skills": skills.library(store, uid), "selected": selected, "limit": skills.MAX_ENABLED})

    async def project_results(request, user):
        return web.json_response(saved_results.library(store, user["id"], int(request.match_info["id"])))

    async def project_result(request, user):
        uid, project_id, answer_id = user["id"], int(request.match_info["id"]), int(request.match_info["answer"])
        if request.method == "DELETE":
            saved_results.remove(store, uid, project_id, answer_id)
            return web.Response(status=204)
        row = saved_results.saved(store, uid, answer_id, project_id)
        return web.json_response({"id": row["id"], "title": row["title"], "saved_at": row["saved_at"],
                                  "html": articles.MARKDOWN.render(row["body"]),
                                  "chat_url": saved_results.chat_url(answer_id)})

    async def sources(request, user):
        uid, project_id = user["id"], int(request.match_info["id"])
        if request.method == "GET":
            return web.json_response(project_sources.list_sources(store, uid, project_id))
        fields = await body(request)
        if fields.keys() != {"url"}:
            raise ValueError("Вставь ссылку на источник.")
        item = await source_slot(project_sources.add_link, store, uid, project_id, fields["url"])
        return web.json_response(item, status=201)

    async def source_item(request, user):
        uid, project_id, source_id = user["id"], int(request.match_info["id"]), int(request.match_info["source"])
        if request.method == "DELETE":
            project_sources.delete(store, uid, project_id, source_id)
            return web.Response(status=204)
        if request.method == "POST":
            item = await source_slot(project_sources.refresh, store, uid, project_id, source_id)
            return web.json_response(item)
        return web.json_response(project_sources.preview(store, uid, project_id, source_id))

    async def source_article(request, user):
        item = await source_slot(project_sources.read_article, store, user["id"],
                                 int(request.match_info["id"]), int(request.match_info["source"]))
        return web.json_response(item)

    async def source_file(request, user):
        uid, project_id = user["id"], int(request.match_info["id"])
        project_sources.authorize(store, uid, project_id)
        if request.content_type != "application/octet-stream":
            raise ValueError("Выбери файл для загрузки.")
        limit = source_text.MAX_FILE
        if request.content_length is not None and request.content_length > limit:
            raise ValueError("Выбери файл до 4 МБ.")
        if source_imports.locked():
            raise ValueError("Сейчас загружается много источников. Попробуй чуть позже.")
        try:
            name = unquote(request.headers.get("X-Source-Name", ""), errors="strict")
            async with source_imports, asyncio.timeout(26):
                raw = await public_fetch.bounded(request.content, limit)
                if "source" in request.match_info:
                    item = await project_sources.attach_transcript(store, uid, project_id, int(request.match_info["source"]), name, raw)
                else:
                    item = await project_sources.add_file(store, uid, project_id, name, raw)
        except public_fetch.FetchError:
            raise ValueError("Выбери файл до 4 МБ.") from None
        except (TimeoutError, UnicodeError):
            raise ValueError("Не удалось загрузить файл. Попробуй ещё раз.") from None
        return web.json_response(item, status=201)

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

    async def chatgpt_status(request, user):
        uid = user["id"]
        status = chatgpt.status(uid)
        if status["available"]:
            status["model_settings"] = models.describe(store, uid, catalogs.opencode_model)
        return web.json_response(status)

    async def model_settings(request, user):
        uid = user["id"]
        if not chatgpt_owner(uid):
            raise KeyError("Выбор модели доступен владельцу бота.")
        async with aiohttp.ClientSession(trust_env=True, headers={"User-Agent": "AlephAgent/1.0"}) as session:
            if request.method == "PATCH":
                return web.json_response(await catalogs.choose(session, store, uid, await body(request)))
            providers = await catalogs.all(session, uid)
        _, selections, _ = models.saved(store, uid)
        return web.json_response({**models.describe(store, uid, catalogs.opencode_model),
                                  "providers": providers, "selections": selections})

    async def chatgpt_pair(request, user):
        if not chatgpt_owner(user["id"]):
            raise KeyError("ChatGPT connection is unavailable.")
        return web.json_response(chatgpt.prepare(user["id"], mini_app_url()))

    async def chatgpt_disconnect(request, user):
        if not chatgpt_owner(user["id"]):
            raise KeyError("ChatGPT connection is unavailable.")
        async with aiohttp.ClientSession(trust_env=True) as session:
            status = await chatgpt.disconnect(session, user["id"])
        models.disconnect_chatgpt(store, user["id"])
        status["model_settings"] = models.describe(store, user["id"], catalogs.opencode_model)
        return web.json_response(status)

    async def chatgpt_import(request):
        # The local CLI proves possession of the one-use code with the encrypted
        # packet. A signed Telegram session is required to create that code.
        raw = bytearray()
        try:
            if request.content_type != "application/json":
                raise ValueError
            async for block in request.content.iter_chunked(4096):
                raw.extend(block)
                if len(raw) > MAX_PACKET:
                    raise ValueError
            packet = json.loads(raw)
            async with aiohttp.ClientSession(trust_env=True) as session:
                status = await chatgpt.install(session, packet)
            result, code = status, 200
        except (ValueError, UnicodeError, OSError, RecursionError):
            result, code = {"error": "Pairing could not be completed. Create a new code in Aleph."}, 400
        except ChatGPTError as error:
            result, code = {"error": "ChatGPT did not validate the connection."}, 503 if error.status in {0, 503} else 403
        return web.json_response(result, status=code, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    routes = [("GET", "/api/state", state), ("POST", "/api/tasks", create),
              ("PATCH", r"/api/tasks/{id:\d{1,18}}", update), ("PATCH", "/api/settings", settings),
              ("GET", r"/api/tasks/{id:\d{1,18}}/result", result),
              ("GET", "/api/memory", memories), ("DELETE", r"/api/memory/{id:\d{1,18}}", forget_memory),
              ("POST", "/api/projects", create_project), ("PATCH", "/api/projects/active", choose_project),
              ("PATCH", r"/api/projects/{id:\d{1,18}}", update_project),
              ("GET", "/api/computers", computer_list), ("POST", "/api/computers/pair", computer_pairing),
              ("DELETE", r"/api/computers/{id:\d{1,18}}", remove_computer),
              ("PATCH", r"/api/projects/{id:\d{1,18}}/computer", project_computer),
              ("GET", "/api/calendar.ics", calendar), ("GET", "/api/feeds", list_feeds),
              ("POST", "/api/feeds", add_feed), ("DELETE", r"/api/feeds/{id:\d{1,18}}", remove_feed)]
    routes += [("GET", r"/api/widgets/{id:\d{1,18}}", widget_view),
               ("PATCH", r"/api/widgets/{id:\d{1,18}}", widget_check),
               ("GET", "/api/models", model_settings), ("PATCH", "/api/models", model_settings),
               ("GET", "/api/chatgpt", chatgpt_status), ("POST", "/api/chatgpt/pair", chatgpt_pair),
               ("DELETE", "/api/chatgpt", chatgpt_disconnect)]
    routes += [("GET", "/api/skills", skill_library), ("POST", "/api/skills", skill_library),
               ("POST", "/api/skills/preview", skill_preview)]
    routes += [(method, r"/api/skills/{id:\d{1,18}}", skill_item) for method in ("GET", "PATCH", "DELETE")]
    routes += [(method, r"/api/projects/{id:\d{1,18}}/skills", project_skills) for method in ("GET", "PATCH")]
    result_path = r"/api/projects/{id:\d{1,18}}/results"
    routes.append(("GET", result_path, project_results))
    routes += [(method, result_path + r"/{answer:\d{1,18}}", project_result) for method in ("GET", "DELETE")]
    source_path = r"/api/projects/{id:\d{1,18}}/sources"
    routes += [(method, source_path, sources) for method in ("GET", "POST")]
    routes += [(method, source_path + r"/{source:\d{1,18}}", source_item) for method in ("GET", "DELETE", "POST")]
    routes += [("POST", source_path + "/file", source_file),
               ("POST", source_path + r"/{source:\d{1,18}}/transcript", source_file),
               ("POST", source_path + r"/{source:\d{1,18}}/reader", source_article)]
    provider_path = r"{provider:calendar|notion|drive|gmail}"
    app.router.add_post(f"/api/connections/{provider_path}/start", scoped(connection))
    app.router.add_delete(f"/api/connections/{provider_path}", scoped(connection))
    app.router.add_get(f"/oauth/{provider_path}/callback", oauth_callback)
    app.router.add_get("/api/connections/calendar/events", scoped(agenda))
    app.router.add_post("/api/connections/drive/picker", scoped(picker_start))
    app.router.add_get("/oauth/drive/picker", picker_page)
    for method in ("GET", "POST"):
        app.router.add_route(method, "/api/projects/{id:[0-9]+}/entries", scoped(project_entries))
    app.router.add_delete("/api/projects/{id:[0-9]+}/entries/{entry:[0-9]+}", scoped(project_entries))

    for method, path, handler in routes:
        app.router.add_route(method, path, scoped(handler))
    app.router.add_post(IMPORT_PATH, chatgpt_import)
    app.router.add_get(r"/api/projects/{id:\d{1,18}}/versions",scoped(versions))
    app.router.add_post(computers.RUNNER_PATH + "/pair", register_runner)
    app.router.add_post(computers.RUNNER_PATH + "/claim", runner)
    app.router.add_post(computers.RUNNER_PATH + r"/{job:[A-Za-z0-9_-]{32}}/{command:complete|tool|folder|artifact|result|alive}", runner)
