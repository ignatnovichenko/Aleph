"""Telegram long polling, bounded parallel private chats and durable reply delivery."""
import asyncio
from contextlib import suppress
import json
import logging
import os
import re
import time

import aiohttp

from .chatgpt import plan as chatgpt
from .chatgpt_pair import owner as chatgpt_owner
from .hermes import create_agent
from .links import settings_url
from . import models, projects
from .llm import ChatGPTUnavailable, ModelRejectsPhoto, ModelUnavailable, RegionUnavailable
from . import articles, capture, integrations, media, memory, documents, reminders, reports, widgets

log = logging.getLogger(__name__)
PLAN_ALIASES = {"/plan", "/tasks", "открой задачи", "открыть задачи", "open tasks", "открой план", "открыть план", "открой мини-апп", "open plan", "open mini app"}
MODEL_QUESTION_RU = re.compile(
    r"(?:(?:привет|здравствуй|слушай)[,.!]?\s+)?"
    r"(?:какая (?:ты модель|у тебя модель|модель у тебя|модель (?:сейчас )?работает|сейчас (?:у тебя )?модель)"
    r"|на какой модели ты (?:сейчас )?работаешь|какую модель ты (?:сейчас )?используешь)[.!?]*",
    re.IGNORECASE,
)
MODEL_QUESTION_EN = re.compile(
    r"(?:(?:hi|hey|hello)[,.!]?\s+)?"
    r"(?:what (?:ai )?model are you(?: (?:using|running))?|which model are you (?:using|running))[.!?]*",
    re.IGNORECASE,
)
HELLO_RU = ("Привет, я Алеф. Можешь писать мне как обычно или отправить голосовое. "
            "Могу напомнить о деле или сам подготовить и прислать результат в указанное время.\n\n"
            "Что сейчас на уме? Для задач по местному времени можешь сразу назвать свой город.")
HELLO_EN = ("Hey, I’m Aleph. Send me a message or a voice note. I can remind you about something or "
            "prepare a result and send it at a time you choose.\n\nWhat’s on your mind? You can tell me your city to use local time.")

class TelegramError(Exception):
    def __init__(self, code=0, retry_after=0, description=""):
        self.code = code
        self.retry_after = retry_after
        self.description = description
        super().__init__(f"Telegram API status {code}")

class Telegram:
    def __init__(self, session, token):
        self.session = session
        self.token = token

    async def call(self, method, **params):
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        try:
            async with self.session.post(url, json=params, allow_redirects=False,
                                         timeout=aiohttp.ClientTimeout(total=params.get("timeout", 0) + 22)) as response:
                if response.status >= 500:
                    raise TelegramError(response.status)
                data = await response.json(content_type=None)
                if not data.get("ok"):
                    raise TelegramError(int(data.get("error_code", response.status)),
                                        int(data.get("parameters", {}).get("retry_after", 0)),
                                        data.get("description", ""))
                return data["result"]
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError) as exc:
            raise TelegramError() from exc

    async def download(self, file, max_bytes):
        if file.get("file_size", 0) > max_bytes:
            raise media.MediaError("Файл слишком большой. Пришли версию поменьше.")
        info = await self.call("getFile", file_id=file["file_id"])
        path = info.get("file_path", "")
        if info.get("file_size", 0) > max_bytes or not re.fullmatch(r"[A-Za-z0-9_./-]+", path) or ".." in path:
            raise media.MediaError("Не удалось загрузить файл. Пришли его ещё раз.")
        url = f"https://api.telegram.org/file/bot{self.token}/{path}"
        try:
            async with self.session.get(url, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=25)) as response:
                if response.status != 200:
                    raise TelegramError(response.status)
                body = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        raise media.MediaError("Файл слишком большой. Пришли версию поменьше.")
                return bytes(body)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise TelegramError() from exc

class Inbox:
    """Pending inputs, tool checkpoints and unsent replies survive restarts."""
    def __init__(self, store):
        self.store = store
        self.db = store.db
        memory.initialize(self.db)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS bot_inbox (
              id INTEGER PRIMARY KEY, uid INTEGER NOT NULL, message TEXT NOT NULL, reply TEXT,
              sent_chunks INTEGER NOT NULL DEFAULT 0, retries INTEGER NOT NULL DEFAULT 0,
              available REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS bot_receipts (
              id INTEGER PRIMARY KEY, received_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS bot_uid ON bot_inbox(uid,id);
            CREATE TABLE IF NOT EXISTS bot_documents (
                key TEXT PRIMARY KEY, update_id INTEGER REFERENCES bot_inbox(id) ON DELETE CASCADE,
                filename TEXT NOT NULL, mime TEXT NOT NULL, data BLOB NOT NULL, sent INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS bot_steps (
              update_id INTEGER REFERENCES bot_inbox(id) ON DELETE CASCADE, step INTEGER,
              message TEXT NOT NULL, PRIMARY KEY(update_id,step)
            );
            CREATE TABLE IF NOT EXISTS bot_tool_results (
              key TEXT PRIMARY KEY, update_id INTEGER REFERENCES bot_inbox(id) ON DELETE CASCADE,
              result TEXT NOT NULL
            );
        """)

        widgets.initialize(self.db)

        columns = {r["name"] for r in self.db.execute("PRAGMA table_info(bot_inbox)")}
        if "project_id" not in columns:
            self.db.execute("ALTER TABLE bot_inbox ADD COLUMN project_id INTEGER")
        if "chatgpt_client" not in columns:
            self.db.execute("ALTER TABLE bot_inbox ADD COLUMN chatgpt_client TEXT")
        if "approval_outbox" not in columns:
            self.db.execute("ALTER TABLE bot_inbox ADD COLUMN approval_outbox TEXT")
        if "article_fallback" not in columns:
            self.db.execute("ALTER TABLE bot_inbox ADD COLUMN article_fallback INTEGER NOT NULL DEFAULT 0")
        if "source" not in columns:
            self.db.execute("ALTER TABLE bot_inbox ADD COLUMN source TEXT NOT NULL DEFAULT ''")
        if "model_choice" not in columns:
            self.db.execute("ALTER TABLE bot_inbox ADD COLUMN model_choice TEXT")

    def bind_project(self, update_id, uid):
        self.db.execute("UPDATE bot_inbox SET project_id=? WHERE id=? AND uid=? AND project_id IS NULL",
                        (projects.active(self.store, uid), update_id, uid))
        return self.db.execute("SELECT project_id FROM bot_inbox WHERE id=? AND uid=?", (update_id, uid)).fetchone()[0]

    def bind_chatgpt(self, update_id, uid, client):
        self.db.execute("UPDATE bot_inbox SET chatgpt_client=? WHERE id=? AND uid=? AND chatgpt_client IS NULL",
                        (client or "", update_id, uid))
        return self.db.execute("SELECT chatgpt_client FROM bot_inbox WHERE id=? AND uid=?", (update_id, uid)).fetchone()[0]

    def bind_model(self, update_id, uid, choice, client, opencode_model):
        row = self.db.execute("SELECT model_choice,chatgpt_client FROM bot_inbox WHERE id=? AND uid=?",
                              (update_id, uid)).fetchone()
        if row[0] is not None:
            return json.loads(row[0]), row[1]
        if row[1] is not None:
            # Turns started before model selection kept their original billing source.
            provider = "chatgpt" if row[1] else "opencode"
            if choice["provider"] != provider:
                choice = models.legacy_option(uid, provider, opencode_model)
        client = self.bind_chatgpt(update_id, uid, client)
        self.db.execute("UPDATE bot_inbox SET model_choice=? WHERE id=? AND uid=?",
                        (json.dumps(choice), update_id, uid))
        return choice, client

    def ingest(self, updates):
        if not updates:
            return
        self.db.execute("BEGIN")
        try:
            now = time.time()
            self.db.execute("DELETE FROM bot_receipts WHERE received_at < ?", (now-172800,))
            for event in updates:
                if not self.db.execute("INSERT OR IGNORE INTO bot_receipts VALUES (?,?)", (event["update_id"], now)).rowcount:
                    continue
                message = event.get("message") or {}
                sender = message.get("from") or {}
                chat = message.get("chat") or {}
                if chat.get("type") == "private" and sender.get("id") == chat.get("id") and not sender.get("is_bot"):
                    self.db.execute("INSERT OR IGNORE INTO bot_inbox(id,uid,message) VALUES (?,?,?)",
                                    (event["update_id"], chat["id"], json.dumps(message, ensure_ascii=False)))
            self.store.set_offset(max(e["update_id"] for e in updates) + 1)
            self.db.execute("INSERT OR REPLACE INTO runtime_state VALUES ('telegram_last_batch',?)", (str(now),))
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def offset(self):
        last = self.db.execute("SELECT value FROM runtime_state WHERE key='telegram_last_batch'").fetchone()
        if last and time.time() - float(last[0]) > 6 * 86400:
            # Telegram may randomize update IDs after a week of inactivity.
            self.db.execute("DELETE FROM runtime_state WHERE key IN ('telegram_offset','telegram_last_batch')")
        return self.store.get_offset()

    def pending(self, *, scheduled=False):
        if scheduled:
            return self.db.execute("""SELECT b.* FROM bot_inbox b
                WHERE b.id<0 AND b.available<=?
                  AND NOT EXISTS (SELECT 1 FROM bot_inbox p WHERE p.uid=b.uid AND p.id<0
                    AND (json_extract(p.message,'$.due_at'), -p.id) < (json_extract(b.message,'$.due_at'), -b.id))
                ORDER BY json_extract(b.message,'$.due_at'), -b.id LIMIT 24""", (time.time(),)).fetchall()
        return self.db.execute("""SELECT b.* FROM bot_inbox b
            WHERE b.id>=0 AND b.available<=?
              AND NOT EXISTS (SELECT 1 FROM bot_inbox p WHERE p.uid=b.uid AND p.id>=0 AND p.id<b.id)
            ORDER BY b.id LIMIT 24""", (time.time(),)).fetchall()

    def get_step(self, update_id, step):
        row = self.db.execute("SELECT message FROM bot_steps WHERE update_id=? AND step=?", (update_id, step)).fetchone()
        return json.loads(row[0]) if row else None

    def save_step(self, update_id, step, reply):
        self.db.execute("INSERT INTO bot_steps VALUES (?,?,?)", (update_id, step, json.dumps(reply)))

    def get_tool(self, key):
        row = self.db.execute("SELECT result FROM bot_tool_results WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_tool(self, key, update_id, result):
        self.db.execute("INSERT INTO bot_tool_results VALUES (?,?,?)", (key, update_id, json.dumps(result)))

    def save_reply(self, row, query, reply):
        self.db.execute("BEGIN")
        try:
            self.db.execute("UPDATE bot_inbox SET reply=?,retries=0 WHERE id=?", (reply, row["id"]))
            if query:
                last = self.db.execute("SELECT message FROM bot_steps WHERE update_id=? ORDER BY step DESC LIMIT 1", (row["id"],)).fetchone()
                if last:
                    step = json.loads(last[0])
                    if step.get("_aleph_model") and not step.get("tool_calls"):
                        models.remember_reply(self.store, row["uid"], step["_aleph_model"])
                scope = self.bind_project(row["id"], row["uid"])
                self.store.add_history(row["uid"], "user", query, project_id=scope)
                widget = widgets.for_update(self.store, row["id"])
                context_reply = reply
                if widget:
                    context_reply += "\n\n[Виджет в сообщении]\n" + widgets.fallback_text(json.loads(widget["spec"]))
                self.store.add_history(row["uid"], "assistant", context_reply, project_id=scope)
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def done(self, update_id, *, delivered=False):
        if update_id >= 0 or not delivered:
            self.db.execute("DELETE FROM bot_inbox WHERE id=?", (update_id,))
            return
        self.db.execute("SAVEPOINT finish_report")
        try:
            row = self.db.execute("SELECT * FROM bot_inbox WHERE id=?", (update_id,)).fetchone()
            if row:
                reports.finish(self.store, row)
                self.db.execute("DELETE FROM bot_inbox WHERE id=?", (update_id,))
        except BaseException:
            self.db.execute("ROLLBACK TO finish_report")
            raise
        finally:
            self.db.execute("RELEASE finish_report")

    def retry(self, row, delay):
        self.db.execute("UPDATE bot_inbox SET available=?, retries=retries+1 WHERE id=?",
                        (time.time()+delay, row["id"]))

    def opened_plan(self, update_id):
        rows = self.db.execute("SELECT message FROM bot_steps WHERE update_id=?", (update_id,))
        return any(call.get("function", {}).get("name") == "open_plan" for row in rows
                   for call in json.loads(row[0]).get("tool_calls") or [])

    def successful_mutations(self, update_id):
        results = []
        for row in self.db.execute("SELECT step,message FROM bot_steps WHERE update_id=? ORDER BY step", (update_id,)):
            for i, call in enumerate(json.loads(row["message"]).get("tool_calls") or []):
                name = call.get("function", {}).get("name")
                result = self.get_tool(f"tg:{update_id}:{row['step']}:{i}")
                if name in {"create_task","update_task","change_settings","subscribe_feed","unsubscribe_feed","remember_fact","forget_fact","create_routine","set_routine_active","delete_routine","create_document","set_project_context","capture_project_update","schedule_report"} and isinstance(result, dict) and "error" not in result:
                    results.append(result.get("title") or {"change_settings": "Настройки", "remember_fact": "Память", "forget_fact": "Память", "create_document": "Файл", "delete_routine": "Расписание", "set_routine_active": "Расписание", "set_project_context": "Контекст проекта"}.get(name, "Подписка"))
        return results

async def _typing(telegram, uid):
    while True:
        with suppress(TelegramError):
            await telegram.call("sendChatAction", chat_id=uid, action="typing")
        await asyncio.sleep(4)

def _uses_russian(msg):
    return (msg.get("from", {}).get("language_code") or "ru").lower().startswith(("ru", "uk", "be", "kk"))

async def _message_text(tg, msg):
    ru = _uses_russian(msg)
    text = msg.get("text") or msg.get("caption") or ""
    photo = None
    if "voice" in msg:
        if msg["voice"].get("duration", 0) > media.MAX_VOICE_SECONDS:
            raise media.MediaError("Голосовое длиннее пяти минут. Пришли его частями.")
        audio = await tg.download(msg["voice"], media.MAX_VOICE_BYTES)
        text = await media.transcribe(audio)
    elif "photo" in msg:
        candidates = [p for p in msg["photo"] if p.get("file_size", 0) <= media.MAX_PHOTO_BYTES]
        if not candidates:
            raise media.MediaError("Фото слишком большое. Пришли копию поменьше.")
        largest = max(candidates, key=lambda p: p.get("width", 0) * p.get("height", 0))
        photo = await media.normalize_photo(await tg.download(largest, media.MAX_PHOTO_BYTES))
        text = text or ("Что на этой фотографии?" if ru else "What is in this photo?")
    elif "document" in msg:
        attachment = await documents.read_document(msg["document"], tg.download)
        prompt = "Помоги кратко разобрать этот файл." if ru else "Give me a brief summary of this file."
        text = (text or prompt) + "\n\n[Attached document content]\n" + attachment
    if not text.strip():
        raise media.MediaError("Можешь прислать текст, голосовое, фотографию или текстовый файл.")
    quoted = msg.get("reply_to_message") or {}
    quote = (quoted.get("text") or quoted.get("caption") or "")[:1800]
    if quote:
        text += "\n\n[Цитата из сообщения, на которое дан ответ]\n" + quote
    return text, photo

def chunks(text):
    remaining = text.strip()
    while remaining:
        # Telegram measures message length in UTF-16 units. 1950 code points
        # also leave room for a message consisting entirely of astral symbols.
        end = len(remaining)
        if len(remaining.encode("utf-16-le")) // 2 > 3900:
            end = 1950
            boundary = max(remaining.rfind("\n", 0, end), remaining.rfind(" ", 0, end))
            if boundary > end // 2:
                end = boundary
        yield remaining[:end]
        remaining = remaining[end:].lstrip()

async def _compose(row, store, inbox, tg, agent):
    uid = row["uid"]
    msg = json.loads(row["message"])
    sender = msg["from"]
    store.ensure_user(uid, sender.get("first_name", ""), sender.get("language_code", ""))
    bridge_url = os.getenv("MINI_APP_BRIDGE_URL", "").strip()
    from .privacy import privacy_reply
    erasure = privacy_reply(store, uid, msg.get("text") or "", row["id"])
    if erasure:
        if chatgpt_owner(uid) and not store.db.execute("SELECT 1 FROM users WHERE id=?", (uid,)).fetchone():
            await chatgpt.disconnect(tg.session, uid, erase=True)
        return None, erasure
    try:
        project_reply = projects.chat_command(store, uid, msg.get("text"), _uses_russian(msg))
    except (KeyError, ValueError) as error:
        return None, str(error)
    if project_reply is not None:
        return None, project_reply
    web_data = msg.get("web_app_data")
    if web_data:
        if not bridge_url:
            return None, "Открой актуальный план в меню чата."
        from .bridge import apply_web_action
        try:
            return None, apply_web_action(store, uid, web_data.get("data", ""), f"tg:{row['id']}:web")
        except (ValueError, KeyError, TypeError):
            return None, "Не удалось применить изменение. Открой свежую версию плана."
    if (msg.get("text") or "").strip().lower().split("@",1)[0].rstrip(".!?") in PLAN_ALIASES:
        if not _uses_russian(msg):
            return None, ("Tap Open tasks below the chat." if bridge_url else "Open Tasks from the Aleph menu.")
        return None, ("Нажми «Открыть задачи» внизу чата." if bridge_url else "Открой «Задачи» кнопкой Aleph внизу чата.")
    command = (msg.get("text") or "").partition(" ")[0].partition("@")[0]
    if command in ("/start", "/help"):
        ru = _uses_russian(msg)
        reply = HELLO_RU if ru else HELLO_EN
        if command == "/start" and chatgpt_owner(uid) and settings_url():
            reply += ("\n\nСвою подписку ChatGPT можно подключить в настройках Алефа." if ru else
                      "\n\nYou can connect your ChatGPT plan in Aleph settings.")
        return None, reply
    query, photo = await _message_text(tg, msg)
    if "voice" in msg:
        try:
            project_reply = projects.chat_command(store, uid, query, _uses_russian(msg))
        except (KeyError, ValueError) as error:
            return None, str(error)
        if project_reply is not None:
            return None, project_reply
    if not photo:
        russian_question = MODEL_QUESTION_RU.fullmatch(query.strip())
        if russian_question or MODEL_QUESTION_EN.fullmatch(query.strip()):
            return query, models.identity_reply(store, uid, agent.model, russian=bool(russian_question))
    source = query.split("\n\n[Attached document content]", 1)[0][:900]
    store.db.execute("UPDATE bot_inbox SET source=? WHERE id=?", (source, row["id"]))
    answer = await agent.respond(store, uid, query, photo, inbox, row["id"])
    historical = ("[Фото] " if photo else "") + query
    return historical, answer

async def _send_widget(store, tg, uid, update_id, reply, show_plan, *, russian=True):
    widget = widgets.for_update(store, update_id)
    if not widget:
        return reply
    spec = json.loads(widget["spec"])
    cap = reply if not show_plan and len(reply.encode("utf-16-le")) // 2 <= 850 else ""
    if widget["message_id"] is None and not widget["fallback"]:
        params = {"chat_id": uid, "html": widgets.render(spec), "protect_content": True}
        if cap:
            params["caption"] = cap
        if spec["kind"] == "checklist":
            params.update(allow_queries=True, chat_data=widgets.chat_state(widget["checked"]))
        try:
            sent = await tg.call("sendWidget", **params)
        except TelegramError as error:
            if error.code not in (400, 404, 405):
                raise
            reason = re.search(r"\b(?:INPUT_MEDIA_WIDGET|WIDGET)_[A-Z_]+\b", error.description or "")
            log.warning("Telegram declined native widget status=%s reason=%s",
                        error.code, reason[0] if reason else "unspecified")
            widgets.mark_fallback(store, widget["key"])
            widget = widgets.for_update(store, update_id)
        else:
            widgets.mark_sent(store, widget["key"], sent["message_id"])
            return "" if cap else reply
    if widget["fallback"] and widget["message_id"] is None:
        sent = await tg.call("sendMessage", chat_id=uid, **widgets.card(widget, russian=russian))
        widgets.mark_sent(store, widget["key"], sent["message_id"])
    return "" if cap else reply


async def _handle(row, store, inbox, tg, agent):
    uid, update_id = row["uid"], row["id"]
    original = json.loads(row["message"])
    ru = _uses_russian(original)
    scheduled = update_id < 0
    report = reports.current(store, row) if scheduled else None
    if scheduled and report is None:
        inbox.done(update_id)
        return
    try:
        reply = row["reply"]
        if reply is None:
            typing = asyncio.create_task(_typing(tg, uid))
            query = None
            try:
                if report:
                    reply = await agent.respond(store, uid, report["notes"], None, inbox, update_id, scheduled=True)
                else:
                    query, reply = await _compose(row, store, inbox, tg, agent)
            except media.MediaError as error:
                if ru:
                    reply = str(error)
                elif "voice" in original:
                    reply = "I couldn’t transcribe this voice note. Try a clearer recording under five minutes or send the text."
                elif "photo" in original or "document" in original:
                    reply = "I couldn’t read this attachment. Try a smaller photo, a text PDF (up to 4 MB) or a text file."
                else:
                    reply = "Send me a message, a voice note, a photo or a text document."
            except TelegramError as error:
                if error.code != 400:
                    raise
                reply = "Telegram не смог открыть это вложение. Пришли файл ещё раз." if ru else "Telegram couldn’t retrieve this file. Please send it again."
            except ModelUnavailable as error:
                using_chatgpt = isinstance(error, ChatGPTUnavailable)
                log.warning("%s unavailable status=%d", "ChatGPT" if using_chatgpt else "OpenCode", error.status)
                retry_statuses = (0, 500, 502, 503, 504) if using_chatgpt else (0, 429, 500, 502, 503, 504)
                limit = 2 if scheduled else 1
                if error.status in retry_statuses and row["retries"] < limit:
                    delay = (20 * (row["retries"] + 1)) if scheduled else 3
                    inbox.retry(row, delay)
                    return
                saved = inbox.successful_mutations(update_id)
                if report:
                    if reports.current(store, row):
                        reports.fail(store.db, uid, update_id)
                    reply = (f"Не удалось подготовить «{report['title']}». Можно назначить новое время во вкладке «Задачи»."
                             if ru else f"I couldn’t finish “{report['title']}”. You can set a new time in Tasks.")
                elif widgets.for_update(store, update_id):
                    reply = "Подготовил виджет." if ru else "Here’s the widget."
                elif saved:
                    reply = "Сохранил изменения: " + ", ".join(saved) + ". Подробности есть во вкладке «Задачи»."
                elif using_chatgpt and error.status == 429:
                    reply = ("Лимит ChatGPT для Алефа достигнут. Проверь его в настройках ChatGPT: https://chatgpt.com/settings/usage" if ru else
                             "Aleph has reached a ChatGPT limit. Check it at https://chatgpt.com/settings/usage")
                elif using_chatgpt and error.status == 401:
                    reply = ("Открой настройки Алефа и подключи ChatGPT заново." if ru else "Open Aleph settings and reconnect ChatGPT.")
                elif using_chatgpt:
                    reply = ("ChatGPT сейчас не принял запрос. Проверь доступ к подписке в настройках Алефа." if ru else
                             "ChatGPT couldn't accept this request. Check your plan connection in Aleph settings.")
                elif isinstance(error, ModelRejectsPhoto):
                    reply = ("Выбранная модель принимает только текст. Для фото выбери модель с поддержкой изображений в настройках." if ru else
                             "Your selected model accepts text only. Choose a model that supports images in Settings.")
                elif isinstance(error, RegionUnavailable):
                    reply = ("OpenCode отклонил модель из-за региональных настроек. Выбери другую модель в настройках Алефа." if ru else
                             "OpenCode declined this model due to region settings. Choose another model in Aleph Settings.")
                elif error.status in (400, 422) and "photo" in original:
                    reply = "Модель сейчас не смогла разобрать фотографию. Напиши, что на ней нужно проверить." if ru else "I couldn’t read this image. Can you describe what you’d like me to check?"
                else:
                    reply = "Сейчас не удаётся получить ответ модели. Попробуй ещё раз чуть позже." if ru else "I couldn’t reach the model just now. Please try again in a moment."
            finally:
                typing.cancel()
                with suppress(asyncio.CancelledError):
                    await typing
            if scheduled and not reports.current(store, row):
                inbox.done(update_id)
                return
            inbox.save_reply(row, query, reply)
        for artifact in inbox.db.execute("SELECT * FROM bot_documents WHERE update_id=? AND sent=0 ORDER BY rowid", (update_id,)).fetchall():
            try:
                await documents.send_document(tg.session, tg.token, uid, artifact["filename"], artifact["mime"], artifact["data"])
            except documents.DocumentDeliveryError as error:
                if error.status != 400:
                    raise TelegramError(error.status, error.retry_after) from None
                reply = "Telegram не принял файл. Попробуй попросить другой формат или вывести результат в чат."
                inbox.db.execute("UPDATE bot_inbox SET reply=? WHERE id=?", (reply, update_id))
                inbox.db.execute("DELETE FROM bot_documents WHERE update_id=? AND sent=0", (update_id,))
                break
            inbox.db.execute("UPDATE bot_documents SET sent=1 WHERE key=?", (artifact["key"],))

        command = (original.get("text") or "").strip().lower().split("@",1)[0].rstrip(".!")
        show_plan = (command.split(" ", 1)[0] in ("/start", "/help", "/plan", "/tasks") or command in PLAN_ALIASES
                     or bool(original.get("web_app_data")) or inbox.opened_plan(update_id))
        approvals = json.loads(row["approval_outbox"]) if row["approval_outbox"] else []
        if not approvals:
            pending = []
            project_choice = capture.for_update(store, uid, update_id)
            external = integrations.for_update(store, uid, update_id)
            if project_choice:
                pending.append(capture.preview(store, uid, project_choice, ru))
            if external:
                pending.append({"text": integrations.preview(external, ru), "reply_markup": integrations.keyboard(external, ru)})
            for item in pending:
                parts = list(chunks(item["text"]))
                approvals.extend({"text": part} for part in parts[:-1])
                approvals.append({"text": parts[-1], "reply_markup": item["reply_markup"]})
            if approvals:
                inbox.db.execute("UPDATE bot_inbox SET approval_outbox=? WHERE id=?", (json.dumps(approvals), update_id))
        if approvals:
            for n, packet in enumerate(approvals):
                if n < row["sent_chunks"]:
                    continue
                await tg.call("sendMessage", chat_id=uid, link_preview_options={"is_disabled": True}, **packet)
                inbox.db.execute("UPDATE bot_inbox SET sent_chunks=? WHERE id=?", (n+1, update_id))
            inbox.done(update_id)
            return
        reply = await _send_widget(store, tg, uid, update_id, reply, show_plan, russian=ru)
        if (not show_plan and not row["sent_chunks"] and not row["article_fallback"]
                and not widgets.for_update(store, update_id)):
            rich = articles.rich_content(reply)
            if rich:
                try:
                    await tg.call("sendRichMessage", chat_id=uid, rich_message=rich)
                except TelegramError as error:
                    if error.code not in (400, 404, 405, 413):
                        raise
                    log.info("Telegram rejected rich formatting status=%s", error.code)
                    inbox.db.execute("UPDATE bot_inbox SET article_fallback=1 WHERE id=?", (update_id,))
                else:
                    inbox.done(update_id, delivered=True)
                    return
        for n, chunk in enumerate(chunks(reply)):
            if n < row["sent_chunks"]:
                continue
            if scheduled and not reports.current(store, row):
                inbox.done(update_id)
                return
            params = {"chat_id": uid, "text": chunk, "link_preview_options": {"is_disabled": True}}
            bridge_url = os.getenv("MINI_APP_BRIDGE_URL", "").strip()
            if n == 0 and bridge_url and show_plan:
                from .bridge import keyboard_markup
                params["reply_markup"] = keyboard_markup(store, uid, bridge_url)
            if n == 0 and command.split(" ", 1)[0] == "/start" and chatgpt_owner(uid):
                url = settings_url()
                if url:
                    params["reply_markup"] = {"inline_keyboard": [[{"text": "Настройки" if ru else "Settings",
                                                                     "web_app": {"url": url}}]]}
            formatted = articles.basic_content(chunk)
            if formatted:
                try:
                    await tg.call("sendMessage", **{**params, "text":formatted, "parse_mode":"HTML"})
                except TelegramError as error:
                    if error.code != 400:
                        raise
                    await tg.call("sendMessage", **params)
            else:
                await tg.call("sendMessage", **params)
            inbox.db.execute("UPDATE bot_inbox SET sent_chunks=? WHERE id=?", (n+1, update_id))
        inbox.done(update_id, delivered=True)
    except TelegramError as error:
        if error.code in (400, 403):
            log.info("Telegram rejected conversation delivery status=%s", error.code)
            if scheduled and reports.current(store, row):
                reports.fail(store.db, uid, update_id)
            inbox.done(update_id)
        else:
            inbox.retry(row, max(error.retry_after, min(300, 2 ** min(row["retries"]+1, 8))))
    except asyncio.CancelledError:
        raise
    except Exception as error:
        log.error("Chat processing failed type=%s", type(error).__name__)
        if row["retries"] < 2:
            inbox.retry(row, 5)
        elif row["reply"] is None:
            if scheduled and reports.current(store, row):
                reports.fail(store.db, uid, update_id)
            inbox.save_reply(row, None, "Не удалось закончить поручение. Проверь вкладку «Задачи» и попробуй ещё раз."
                             if ru else "I couldn't finish this request. Check Tasks and try again.")
        else:
            inbox.retry(row, 60)

async def _work(store, inbox, tg, agent, changed):
    interactive, scheduled = {}, {}
    try:
        while True:
            changed.clear()
            reports.enqueue(inbox.db)
            for is_scheduled, active, limit in ((False, interactive, 8), (True, scheduled, 2)):
                for uid, task in list(active.items()):
                    if task.done():
                        await task
                        del active[uid]
                for row in inbox.pending(scheduled=is_scheduled):
                    if len(active) >= limit:
                        break
                    if row["uid"] not in active:
                        task = asyncio.create_task(_handle(row, store, inbox, tg, agent))
                        task.add_done_callback(lambda _: changed.set())
                        active[row["uid"]] = task
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(changed.wait(), 1)
    finally:
        tasks = [*interactive.values(), *scheduled.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

async def _widget_updates(store, telegram, updates, queue_external=None):
    # Process independent messages concurrently. Serialize taps on one widget
    # so an earlier edit cannot win after a later one.
    lanes = {}
    for update in updates:
        native = update.get("widget_query")
        query = native or update.get("callback_query")
        if isinstance(query, dict) and isinstance(query.get("id"), str):
            message = query if native else (query.get("message") or {})
            chat_id = (message.get("chat") or {}).get("id", 0)
            lanes.setdefault((chat_id, message.get("message_id", 0)), []).append((native, query))
    limit = asyncio.Semaphore(16)

    async def lane(queries):
        async with limit:
            for native, query in queries:
                try:
                    if native:
                        await widgets.handle_query(store, telegram, query)
                    elif str(query.get("data", "")).startswith("cap:"):
                        await capture.handle(store, telegram, query)
                    elif str(query.get("data", "")).startswith("ext:"):
                        if queue_external:
                            queue_external(query)
                        else:
                            await integrations.handle(store, telegram, query)
                    elif str(query.get("data", "")).startswith("rem:"):
                        await reminders.handle(store, telegram, query)
                    else:
                        await widgets.handle_fallback_query(store, telegram, query)
                except TelegramError as error:
                    log.warning("Widget update delivery status=%s", error.code)

    await asyncio.gather(*(lane(queries) for queries in lanes.values()))


async def run_bot(store, session, token, model_key):
    inbox = Inbox(store)
    telegram = Telegram(session, token)
    changed = asyncio.Event()
    agent = create_agent(session, model_key)
    log.info("Telegram conversation engine: %s", type(agent).__name__)
    worker = asyncio.create_task(_work(store, inbox, telegram, agent, changed))
    confirmations = set()
    external_slots = asyncio.Semaphore(6)

    async def confirm_external(query):
        async with external_slots:
            try:
                await integrations.handle(store, telegram, query)
            except TelegramError as error:
                log.info("External confirmation delivery status=%s", error.code)
            except Exception as error:
                log.error("External confirmation failed type=%s", type(error).__name__)

    def queue_external(query):
        task = asyncio.create_task(confirm_external(query))
        confirmations.add(task)
        task.add_done_callback(confirmations.discard)

    try:
        while True:
            if worker.done():
                await worker
            try:
                updates = await telegram.call("getUpdates", offset=inbox.offset(), timeout=25, allowed_updates=["message", "widget_query", "callback_query"])
                if updates:
                    inbox.ingest(updates)
                    changed.set()
                    await _widget_updates(store, telegram, updates, queue_external)
            except TelegramError as error:
                log.warning("Polling Telegram status=%s", error.code)
                await asyncio.sleep(max(4, error.retry_after))
    finally:
        worker.cancel()
        for task in confirmations:
            task.cancel()
        await asyncio.gather(worker, *confirmations, return_exceptions=True)
