from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import AsyncMock

from aiohttp.test_utils import TestClient, TestServer

from aleph import bot, projects, reports, saved_results
from aleph.__main__ import application
from aleph.store import Store
from test_backend import TEST_TOKEN, auth
from test_bot import update


ARTICLE = "# Итоги исследования\n\n" + "Проверили цены, спрос и поставщиков. " * 25


class SavedResults(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.uid = 71
        self.store.ensure_user(self.uid, language_code="ru")
        self.project = projects.create(self.store, self.uid, "Рынок")["id"]
        self.other = projects.create(self.store, self.uid, "Другой")["id"]
        self.inbox = bot.Inbox(self.store)
        self.telegram = AsyncMock()
        self.agent = AsyncMock()
        self.calls = []

        async def call(method, **params):
            self.calls.append((method, params))
            return {"message_id": len(self.calls) + 100}
        self.telegram.call.side_effect = call

    async def ask(self, update_id, text):
        self.inbox.ingest([update(update_id, self.uid, text)])
        await bot._handle(self.inbox.pending()[0], self.store, self.inbox, self.telegram, self.agent)

    async def tap(self, data, user=None, message_id=123):
        user = user or self.uid
        packet = {"id": "callback", "data": data, "from": {"id": user, "language_code": "ru"},
                  "message": {"message_id": message_id, "chat": {"id": user, "type": "private"}}}
        await bot._widget_updates(self.store, self.telegram, [{"callback_query": packet}])

    async def test_long_reply_saves_once_in_original_workspace_and_anchors_chat_message(self):
        projects.choose(self.store, self.uid, self.project)
        self.agent.respond.return_value = ARTICLE
        await self.ask(901, "Проведи подробный анализ")
        index, packet = next((i, p) for i, (m,p) in enumerate(self.calls, 101) if m == "sendRichMessage")
        button = packet["reply_markup"]["inline_keyboard"][-1][0]
        self.assertEqual(button["text"], "Сохранить в проект")
        self.assertEqual(saved_results.library(self.store, self.uid, self.project), [])
        await self.tap(button["callback_data"], user=72)
        self.assertEqual(saved_results.library(self.store, self.uid, self.project), [])
        projects.choose(self.store, self.uid, self.other)
        await self.tap(button["callback_data"])
        await self.tap(button["callback_data"])
        self.assertEqual(self.calls[-1][1]["text"], "Ответ уже сохранён.")
        [saved] = saved_results.library(self.store, self.uid, self.project)
        self.assertEqual(saved["title"], "Итоги исследования")
        self.assertEqual(saved_results.library(self.store, self.uid, self.other), [])

        client = TestClient(TestServer(application(self.store, TEST_TOKEN)))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        owner = {"Authorization": "tma " + auth(self.uid)}
        stranger = {"Authorization": "tma " + auth(72)}
        base = f"/api/projects/{self.project}/results"
        self.assertEqual((await client.get(base)).status, 401)
        self.assertEqual((await client.get(base, headers=stranger)).status, 404)
        response = await client.get(f"{base}/{saved['id']}", headers=owner)
        value = await response.json()
        self.assertIn("<h1>Итоги исследования</h1>", value["html"])
        self.assertTrue(value["chat_url"].endswith(f"?start=answer_{saved['id']}"))
        self.assertEqual((await client.get(f"/api/projects/{self.other}/results/{saved['id']}",headers=owner)).status, 404)
        await self.ask(902, f"/start answer_{saved['id']}")
        method, answer = self.calls[-1]
        self.assertEqual(method, "sendMessage")
        self.assertEqual(answer["reply_parameters"]["message_id"], index)
        self.assertEqual((await client.delete(f"{base}/{saved['id']}", headers=owner)).status, 204)
        self.assertEqual(saved_results.library(self.store,self.uid,self.project), [])

    async def test_personal_response_can_be_saved_to_chosen_project(self):
        self.agent.respond.return_value = ARTICLE
        await self.ask(901, "Дай разбор")
        button = self.calls[-1][1]["reply_markup"]["inline_keyboard"][-1][0]
        await self.tap(button["callback_data"])
        keyboard = self.calls[-1][1]["reply_markup"]["inline_keyboard"]
        destination = next(item for row in keyboard for item in row if item["text"] == "Другой")
        await self.tap(destination["callback_data"])
        [saved] = saved_results.library(self.store,self.uid,self.other)
        self.assertEqual(saved["title"], "Итоги исследования")
        self.assertEqual(projects.active(self.store,self.uid),0)

    async def test_short_chat_stays_unchanged_and_scheduled_answer_can_be_saved(self):
        self.agent.respond.return_value = "Готово."
        await self.ask(901, "Привет!")
        self.assertNotIn("reply_markup", self.calls[-1][1])
        at = datetime.now(timezone.utc) + timedelta(minutes=1)
        report = reports.schedule(self.store,self.uid,self.project,"Ежедневный отчёт","Дай данные",at.isoformat(),"qa-save")
        reports.enqueue(self.store.db, at + timedelta(seconds=2))
        [scheduled] = self.inbox.pending(scheduled=True)
        await bot._handle(scheduled,self.store,self.inbox,self.telegram,self.agent)
        button = self.calls[-1][1]["reply_markup"]["inline_keyboard"][-1][0]
        self.assertIn("Сохранить в проект",button["text"])
        await self.tap(button["callback_data"])
        [saved] = saved_results.library(self.store,self.uid,self.project)
        self.assertEqual(saved["title"],"Ежедневный отчёт")
        self.assertEqual(self.store.get_task(self.uid,report["id"])["report_state"],"sent")

    async def test_save_button_survives_plain_text_delivery_fallback(self):
        projects.choose(self.store, self.uid, self.project)
        self.agent.respond.return_value = ARTICLE * 6
        deliver = self.telegram.call.side_effect

        async def without_rich(method, **params):
            if method == "sendRichMessage":
                raise bot.TelegramError(400)
            return await deliver(method, **params)

        self.telegram.call.side_effect = without_rich
        await self.ask(906, "Разбери рынок подробно")
        messages = [params for method, params in self.calls if method == "sendMessage"]
        self.assertGreater(len(messages), 1)
        self.assertNotIn("reply_markup", messages[0])
        button = messages[-1]["reply_markup"]["inline_keyboard"][0][0]
        await self.tap(button["callback_data"])
        self.assertEqual(len(saved_results.library(self.store, self.uid, self.project)), 1)
