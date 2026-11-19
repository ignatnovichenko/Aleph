import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer

from aleph import bot, progress, projects
from aleph.__main__ import application
from aleph.llm import ModelUnavailable
from aleph.store import Store
from test_backend import TEST_TOKEN, auth
from test_bot import update


class ChatReceipts(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.inbox = bot.Inbox(self.store)
        self.sent = asyncio.Queue()
        self.tg = AsyncMock()
        self.agent = AsyncMock()

        async def telegram(method, **params):
            if method in {"sendMessage", "sendRichMessage"}:
                self.sent.put_nowait(params)
            return {"message_id": 34}

        self.tg.call.side_effect = telegram

    async def receipt(self):
        return await asyncio.wait_for(self.sent.get(), 2)

    async def handle(self):
        row = self.inbox.pending()[0]
        await bot._handle(row, self.store, self.inbox, self.tg, self.agent)

    def ingest(self, text, update_id=1, uid=70):
        packet = update(update_id, uid, text)
        packet["message"]["message_id"] = 112
        self.inbox.ingest([packet])

    async def test_workspace_change_from_authenticated_api_is_delivered_once(self):
        uid = 70
        self.store.ensure_user(uid, language_code="ru")
        project = projects.create(self.store, uid, "Север & <рынок>")
        app = TestClient(TestServer(application(self.store, TEST_TOKEN)))
        await app.start_server()
        self.addAsyncCleanup(app.close)
        headers = {"Authorization": "tma " + auth(uid)}
        endpoint = "/api/projects/active"

        async def switch(project_id, account=headers):
            return await app.patch(endpoint, headers=account, json={"project_id": project_id})

        self.assertEqual((await switch(project["id"], {"Authorization": "tma " + auth(71)})).status, 404)
        self.assertEqual(projects.pending_notices(self.store), [])
        response = await switch(project["id"])
        self.assertEqual(await response.json(), project)
        self.assertEqual(projects.active(self.store, uid), project["id"])
        [notice] = projects.pending_notices(self.store)
        self.assertEqual((await switch(project["id"])).status, 200)
        self.assertEqual(projects.pending_notices(self.store)[0]["id"], notice["id"])

        changed = asyncio.Event()
        worker = asyncio.create_task(bot._work(self.store, self.inbox, self.tg, self.agent, changed))
        try:
            message = await self.receipt()
            self.assertEqual(message["chat_id"], uid)
            self.assertEqual(message["parse_mode"], "HTML")
            self.assertEqual(message["text"], "📁 <b>Север &amp; &lt;рынок&gt;</b>\nКонтекст чата переключён.")
            self.assertEqual(projects.pending_notices(self.store), [])
            await switch(project["id"])
            await switch(0)
            changed.set()
            self.assertIn("<b>Личное</b>", (await self.receipt())["text"])
            self.assertTrue(self.sent.empty())
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    async def test_superseded_workspace_notice_and_rate_limit(self):
        uid = 70
        self.store.ensure_user(uid, language_code="en")
        first = projects.create(self.store, uid, "First")["id"]
        second = projects.create(self.store, uid, "Second")["id"]
        projects.enter(self.store, uid, first)
        [old] = projects.pending_notices(self.store)
        projects.enter(self.store, uid, second)
        [current] = projects.pending_notices(self.store)
        await bot._workspace_notice(self.store, self.tg, old["id"])
        self.tg.call.assert_not_called()
        send = self.tg.call.side_effect
        self.tg.call.side_effect = bot.TelegramError(429, 2)
        await bot._workspace_notice(self.store, self.tg, current["id"])
        self.assertEqual(projects.pending_notices(self.store), [])
        self.store.db.execute("UPDATE workspace_notices SET available=0")
        self.tg.call.side_effect = send
        await bot._workspace_notice(self.store, self.tg, current["id"])
        self.assertIn("<b>Second</b>", (await self.receipt())["text"])
        self.assertEqual(projects.pending_notices(self.store), [])

    async def test_contextual_message_precedes_result_and_survives_model_retry(self):
        self.ingest("Проведи подробный ресерч рынка металлургии в России.")
        finish = asyncio.Event()

        async def unavailable(*args, **kwargs):
            await finish.wait()
            raise ModelUnavailable(503)

        self.agent.respond.side_effect = unavailable
        attempt = asyncio.create_task(self.handle())
        try:
            first = await self.receipt()
            self.assertIn("исследование рынка металлургии в России", first["text"])
            self.assertIn("конкурентов", first["text"])
            self.assertEqual(first["reply_parameters"]["message_id"], 112)
            self.assertTrue(first["disable_notification"])
            self.assertFalse(attempt.done())
        finally:
            finish.set()
            await attempt

        self.store.db.execute("UPDATE bot_inbox SET available=0")
        self.agent.respond.side_effect = None
        self.agent.respond.return_value = "Подробный итог исследования."
        await self.handle()
        self.assertEqual((await self.receipt())["text"], "Подробный итог исследования.")
        self.assertTrue(self.sent.empty())
        self.assertEqual([item["content"] for item in self.store.history(70)], [
            "Проведи подробный ресерч рынка металлургии в России.", "Подробный итог исследования.",
        ])

    async def test_quick_reply_has_one_message_and_slow_unclassified_reply_has_two(self):
        self.ingest("Привет!")

        async def greeting(*args, **kwargs):
            await asyncio.sleep(.02)
            return "Привет!"

        self.agent.respond.side_effect = greeting
        with patch.object(bot, "PROGRESS_DELAY", 0):
            await self.handle()
        self.assertEqual((await self.receipt())["text"], "Привет!")
        self.assertTrue(self.sent.empty())

        self.ingest("Как лучше организовать обучение команды?", update_id=2)
        release = asyncio.Event()

        async def slow(*args, **kwargs):
            await release.wait()
            return "Начни с оценки текущих навыков."

        self.agent.respond.side_effect = slow
        with patch.object(bot, "PROGRESS_DELAY", .02):
            work = asyncio.create_task(self.handle())
            try:
                self.assertIn("Разбираю запрос", (await self.receipt())["text"])
            finally:
                release.set()
                await work
        self.assertEqual((await self.receipt())["text"], "Начни с оценки текущих навыков.")

    async def test_voice_research_uses_transcript_but_a_scheduled_request_stays_quiet(self):
        packet = update(1, 70, "")
        packet["message"]["voice"] = {"file_id": "synthetic", "duration": 4}
        self.inbox.ingest([packet])
        self.tg.download.return_value = b"audio"
        self.agent.respond.return_value = "Разбор готов."
        with patch.object(bot.media, "transcribe", AsyncMock(return_value="Проведи исследование рынка стали.")):
            finish = asyncio.Event()

            async def research(*args, **kwargs):
                await finish.wait()
                return "Разбор готов."

            self.agent.respond.side_effect = research
            job = asyncio.create_task(self.handle())
            try:
                self.assertIn("исследование рынка стали", (await self.receipt())["text"])
            finally:
                finish.set()
                await job
        self.assertEqual((await self.receipt())["text"], "Разбор готов.")
        self.ingest("Завтра в 18:00 подготовь и пришли отчёт по рынку стали.", update_id=2)
        self.agent.respond.side_effect = None
        self.agent.respond.return_value = "Запланировал на завтра."
        with patch.object(bot, "PROGRESS_DELAY", 0):
            await self.handle()
        self.assertEqual((await self.receipt())["text"], "Запланировал на завтра.")
        self.assertTrue(self.sent.empty())

    def test_compose_only_uses_user_words_and_handles_english(self):
        text = progress.request_text("Conduct detailed research on the steel market.\n\n[Attached document content]\nConfidential data.")
        self.assertIn("steel market", progress.immediate_ack(text, False))
        self.assertNotIn("Confidential data", text)
        self.assertIsNone(progress.immediate_ack("Сравни 5 и 3", True))
        self.assertTrue(progress.skip_ack("Compare these briefly."))
        self.assertTrue(progress.skip_ack("Напомни сделать анализ рынка через два часа."))
