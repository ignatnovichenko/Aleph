"""Regression coverage for the chat-to-project flow and external actions."""
import base64
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer
from cryptography.fernet import Fernet

from aleph import bot, capture, connections, drive_picker, integrations, projects, reminders
from aleph.__main__ import application
from aleph.llm import Aleph
from aleph.scheduler import delivery_text, queue_messages
from aleph.store import UTC, Store, timestamp
from test_backend import TEST_TOKEN, auth
from test_bot import update


class TelegramStub:
    session = None

    def __init__(self):
        self.calls = []
        self.reject_article = False

    async def call(self, method, **params):
        self.calls.append((method, params))
        if method == "sendRichMessage" and self.reject_article:
            raise bot.TelegramError(400)
        return {"message_id": 83}

    def messages(self):
        return [(method, params) for method, params in self.calls if method in {"sendMessage", "sendRichMessage"}]


def callback(data, uid=1):
    return {"id": "mock", "from": {"id": uid, "language_code": "ru"}, "data": data,
            "message": {"message_id": 83, "chat": {"id": uid, "type": "private"}}}


class WorkspaceFixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.store.ensure_user(1)
        self.store.ensure_user(2)
        self.inbox = bot.Inbox(self.store)
        self.telegram = TelegramStub()
        self.agent = Aleph(None, "test")
        self.client = TestClient(TestServer(application(self.store, TEST_TOKEN)))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    def auth(self, uid=1):
        return {"Authorization": "tma " + auth(uid)}


class Workflows(WorkspaceFixture):
    async def test_voice_capture_choice_plan_and_reminder(self):
        project = projects.create(self.store, 1, "Лендинг", "Утвердить макет первой страницы.")
        deadline = datetime.now(UTC) + timedelta(hours=2)
        reminder = datetime.now(UTC) - timedelta(minutes=1)
        call = {"id": "capture1", "type": "function", "function": {
            "name": "capture_project_update", "arguments": json.dumps({
                "decision": "Оставляем белый фон.", "question": "Проверить цены.",
                "title": "Отправить макет", "due_at": timestamp(deadline), "remind_at": timestamp(reminder),
            })}}
        self.agent.completion = AsyncMock(side_effect=[
            {"role": "assistant", "content": None, "tool_calls": [call]},
            {"role": "assistant", "content": "Выбери проект."},
        ])
        incoming = update(11, 1, "")
        del incoming["message"]["text"]
        incoming["message"]["voice"] = {"file_id": "example", "duration": 7}
        self.telegram.download = AsyncMock(return_value=b"synthetic audio")
        self.inbox.ingest([incoming])
        voice = "Запиши: белый фон оставляем, цены отложим, напомни отправить макет."
        with patch.object(bot.media, "transcribe", AsyncMock(return_value=voice)):
            await bot._handle(self.inbox.pending()[0], self.store, self.inbox, self.telegram, self.agent)
        self.assertEqual(self.store.list_tasks(1), [])
        captured = self.telegram.messages()[-1][1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        await capture.handle(self.store, self.telegram, callback(captured, uid=2))
        self.assertEqual(self.store.list_tasks(1), [])
        await capture.handle(self.store, self.telegram, callback(captured))
        await capture.handle(self.store, self.telegram, callback(captured))
        self.assertEqual(projects.active(self.store, 1), 0)
        entries_path = f"/api/projects/{project['id']}/entries"
        entries = await (await self.client.get(entries_path, headers=self.auth())).json()
        self.assertEqual({entry["kind"] for entry in entries}, {"decision", "question"})
        self.assertTrue(all(entry["source"] == voice for entry in entries))
        self.assertEqual((await self.client.get(entries_path, headers=self.auth(2))).status, 404)
        [task] = self.store.list_tasks(1)
        self.assertEqual(task["project_id"], project["id"])
        self.assertEqual(task["due_at"], timestamp(deadline))
        self.assertEqual(task["remind_at"], timestamp(reminder))
        now = datetime.now(UTC)
        queue_messages(self.store, now)
        delivery = self.store.db.execute("SELECT * FROM deliveries WHERE kind='reminder'").fetchone()
        self.assertIn("Отправить макет", delivery_text(self.store, delivery, now))
        done, later = reminders.keyboard(task["id"], task["remind_at"], True)["inline_keyboard"][0]
        await reminders.handle(self.store, self.telegram, callback(later["callback_data"]))
        postponed = self.store.get_task(1, task["id"])
        self.assertEqual(postponed["due_at"], task["due_at"])
        self.assertGreater(postponed["remind_at"], timestamp(now))
        await reminders.handle(self.store, self.telegram, callback(done["callback_data"]))
        self.assertEqual(self.store.get_task(1, task["id"])["status"], "open")
        done = reminders.keyboard(task["id"], postponed["remind_at"], True)["inline_keyboard"][0][0]
        await reminders.handle(self.store, self.telegram, callback(done["callback_data"]))
        self.assertEqual(self.store.get_task(1, task["id"])["status"], "done")

    async def test_rich_article_and_plain_fallback_without_model_replay(self):
        body = "\n\n".join(["# Итог", "Короткое решение.",
                            "## Сравнение", "| Метод | Оценка |\n| --- | --- |\n| Обычный | 4 |",
                            "## Подробности", "Описание и практический вывод. " * 25,
                            "<script>alert(1)</script>"])
        self.agent.completion = AsyncMock(return_value={"role": "assistant", "content": body})
        self.inbox.ingest([update(11, 1, "Сравни два варианта")])
        await bot._handle(self.inbox.pending()[0], self.store, self.inbox, self.telegram, self.agent)
        [(method, report)] = self.telegram.messages()
        self.assertEqual(method, "sendRichMessage")
        html = report["rich_message"]["html"]
        self.assertIn("<table>", html)
        self.assertIn("<h2>Сравнение</h2>", html)
        self.assertNotIn("<script>", html)
        self.telegram.reject_article = True
        self.telegram.calls.clear()
        self.inbox.ingest([update(12, 1, "Нужны подробности")])
        await bot._handle(self.inbox.pending()[0], self.store, self.inbox, self.telegram, self.agent)
        self.assertEqual([method for method, _ in self.telegram.messages()], ["sendRichMessage", "sendMessage"])
        self.assertIn("Итог", self.telegram.messages()[1][1]["text"])
        self.assertEqual(self.agent.completion.await_count, 2)

    async def test_legacy_task_keeps_scheduled_delivery_on_upgrade(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "legacy.db"
            old = Store(path)
            old.ensure_user(1)
            task = old.create_task(1, "До обновления", "2027-01-15T10:00Z")
            old.db.execute("DROP INDEX tasks_reminder")
            old.db.execute("ALTER TABLE tasks DROP COLUMN remind_at")
            old.close()
            updated = Store(path)
            try:
                migrated = updated.get_task(1, task["id"])
                self.assertEqual(migrated["due_at"], migrated["remind_at"])
            finally:
                updated.close()


class ExternalWorkflows(WorkspaceFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        env = patch.dict(os.environ, {
            "ALEPH_INTEGRATIONS_ORIGIN": "https://example.test",
            "ALEPH_INTEGRATIONS_KEY": Fernet.generate_key().decode(),
            "ALEPH_GOOGLE_CLIENT_ID": "fake-google",
            "ALEPH_GOOGLE_CLIENT_SECRET": "synthetic-google-secret",
            "ALEPH_NOTION_CLIENT_ID": "fake-notion",
            "ALEPH_NOTION_CLIENT_SECRET": "synthetic-notion-secret",
            "ALEPH_GOOGLE_PROJECT_NUMBER": "123456789",
            "ALEPH_GOOGLE_PICKER_KEY": "synthetic-picker-key",
        })
        env.start()
        self.addCleanup(env.stop)

    async def check_oauth(self):
        self.assertEqual((await self.client.post("/api/connections/calendar/start")).status, 401)
        counter = 0
        for provider in connections.PROVIDERS:
            response = await self.client.post(f"/api/connections/{provider}/start", headers=self.auth())
            url = (await response.json())["url"]
            params = parse_qs(urlsplit(url).query)
            self.assertEqual(params["redirect_uri"], [f"https://example.test/oauth/{provider}/callback"])
            self.assertEqual("code_challenge" in params, provider != "notion")
            counter += 1
            tokens = {"access_token": f"synthetic-{provider}", "refresh_token": f"refresh-{provider}", "expires_in": 3600}
            exchange = AsyncMock(return_value=tokens)
            with patch.object(connections, "exchange", exchange):
                endpoint = f"/oauth/{provider}/callback"
                data = {"state": params["state"][0], "code": f"code-{counter}"}
                self.assertEqual((await self.client.get(endpoint, params=data)).status, 200)
                self.assertEqual((await self.client.get(endpoint, params=data)).status, 400)
                exchange.assert_awaited_once()
                self.assertEqual("code_verifier" in exchange.await_args.args[-1], provider != "notion")
            stored = self.store.db.execute("SELECT credentials FROM connections WHERE provider=?", (provider,)).fetchone()[0]
            self.assertNotIn(tokens["access_token"], stored)
            self.assertEqual(connections.statuses(self.store, 1)[provider], "connected")
            self.assertEqual(connections.statuses(self.store, 2)[provider], "disconnected")

    async def test_oauth_action_and_read_for_each_service(self):
        await self.check_oauth()
        page_id = "a" * 32
        calls = []

        async def remote(_store, _session, uid, provider, method, path, **kwargs):
            self.assertEqual(uid, 1)
            calls.append((provider, method, path, kwargs))
            if path.endswith("/calendars"):
                return {"id": "alex@example.test"}
            if path.endswith("/events"):
                return {"items": [{"summary": "Встреча", "start": {"dateTime": "2027-01-02T12:00:00Z"}}]} if method == "GET" else {"htmlLink": "https://calendar.google.com/calendar/example"}
            if path.endswith("/markdown"):
                return {"markdown": "# План проекта"}
            if path.endswith("/export"):
                return "Текст выбранного документа"
            if provider == "drive":
                return {"id": "Doc1", "mimeType": integrations.DOCUMENT}
            if path.endswith("/drafts"):
                return {"id": "Draft1"}
            if path.endswith("/messages"):
                return {"messages": [{"id": "Msg1"}]}
            if path.endswith("/Msg1"):
                return {"id": "Msg1", "payload": {"headers": [{"name": "Subject", "value": "Предложение"}]}}
            return {}

        with patch.object(connections, "call", side_effect=remote):
            calendar = await integrations.read(self.store, None, 1, {"provider": "calendar", "start": "2027-01-02"})
            self.assertEqual(calendar["events"][0]["title"], "Встреча")
            self.assertIn("План", (await integrations.read(self.store, None, 1, {"provider": "notion", "item": page_id}))["text"])
            self.assertIn("документа", (await integrations.read(self.store, None, 1, {"provider": "drive", "item": "Doc1"}))["text"])
            self.assertEqual((await integrations.read(self.store, None, 1, {"provider": "gmail"}))["messages"][0]["subject"], "Предложение")
            for n, fields in enumerate([
                {"provider": "calendar", "title": "Тест", "start": "2027-01-02T12:00Z", "end": "2027-01-02T13:00Z"},
                {"provider": "notion", "item": page_id, "text": "Запись"},
                {"provider": "drive", "title": "Отчёт", "text": "Текст"},
                {"provider": "gmail", "title": "Ответ", "text": "Привет", "recipient": "to@example.test"},
            ], start=20):
                old_count = len(calls)
                self.assertTrue(integrations.prepare(self.store, 1, 0, f"tg:{n}:0:0", fields)["awaiting_confirmation"])
                self.assertEqual(len(calls), old_count)
                pending = integrations.for_update(self.store, 1, n)
                button = integrations.keyboard(pending, True)["inline_keyboard"][0][0]
                await integrations.handle(self.store, self.telegram, callback(button["callback_data"], uid=2))
                self.assertEqual(len(calls), old_count)
                await integrations.handle(self.store, self.telegram, callback(button["callback_data"]))
                after = len(calls)
                self.assertGreater(after, old_count)
                await integrations.handle(self.store, self.telegram, callback(button["callback_data"]))
                self.assertEqual(len(calls), after)
            draft = next(kwargs for _, method, path, kwargs in calls if path.endswith("/drafts") and method=="POST")
            raw = draft["json"]["message"]["raw"]
            self.assertIn(b"To: to@example.test", base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
            self.assertFalse(any(path.endswith("/send") for _, _, path, _ in calls))

        url = drive_picker.start(self.store, 1)
        ticket = parse_qs(urlsplit(url).query)["ticket"][0]
        html, policy = await drive_picker.page(self.store, None, ticket)
        self.assertIn("setAppId", html)
        self.assertIn("frame-ancestors 'none'", policy)
        with self.assertRaises(connections.ConnectionError):
            await drive_picker.page(self.store, None, ticket)
        response = await self.client.delete("/api/connections/gmail", headers=self.auth())
        self.assertEqual((await response.json())["gmail"], "disconnected")
        self.assertEqual(connections.statuses(self.store, 1)["drive"], "connected")
