import json
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

from aleph import bot, projects, reports
from aleph.llm import Aleph, ModelUnavailable, REPORT_TOOL_NAMES
from aleph.store import Store, UTC, timestamp
from test_bot import update


ARTICLE = "# SpaceX\n\n## В чём бизнес\n\n" + "Модель выручки и источники оценки. " * 26


class Telegram:
    def __init__(self):
        self.sent = []
        self.unavailable = False
        self.download = AsyncMock(return_value=b"voice")

    async def call(self, method, **params):
        if method in ("sendMessage", "sendRichMessage"):
            if self.unavailable:
                raise bot.TelegramError(429, 2)
            self.sent.append((method, params))
        return {"message_id": len(self.sent)}


class ScheduledReports(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.store.ensure_user(7)
        self.store.patch_settings(7, {"timezone": "Europe/Moscow"})
        self.project = projects.create(self.store, 7, "Исследования")["id"]
        projects.choose(self.store, 7, self.project)
        self.inbox = bot.Inbox(self.store)
        self.tg = Telegram()
        self.agent = Aleph(None, "synthetic")
        self.run_at = datetime.now(UTC) + timedelta(seconds=60)

    def schedule(self):
        return reports.schedule(self.store, 7, self.project, "Разбор SpaceX", "Подготовь разбор SpaceX.",
                                timestamp(self.run_at), "tg:200:0:0")

    def queued(self):
        reports.enqueue(self.store.db, self.run_at + timedelta(seconds=1))
        return self.inbox.pending(scheduled=True)[0]

    async def test_schedule_voice_then_run_report_at_due_time(self):
        mock_call = {"id": "schedule", "type": "function", "function": {"name": "schedule_report", "arguments": json.dumps({
            "title": "Разбор SpaceX", "prompt": "Подготовь разбор SpaceX.", "run_at": timestamp(self.run_at),
        })}}
        self.agent.completion = AsyncMock(side_effect=[
            {"role": "assistant", "content": None, "tool_calls": [mock_call]},
            {"role": "assistant", "content": "Запланировал разбор."},
            {"role": "assistant", "content": ARTICLE},
        ])
        packet = update(19, 7, "")
        packet["message"].pop("text")
        packet["message"]["voice"] = {"file_id": "voice", "duration": 6}
        self.inbox.ingest([packet])
        text = "Завтра в 18:00 пришли разбор компании SpaceX."
        with patch.object(bot.media, "transcribe", AsyncMock(return_value=text)):
            await bot._handle(self.inbox.pending()[0], self.store, self.inbox, self.tg, self.agent)
        [task] = self.store.list_tasks(7)
        self.assertEqual(task["source"], text)
        self.assertEqual(task["project_id"], self.project)
        self.assertEqual(task["report_state"], "pending")
        self.assertIsNone(task["remind_at"])
        reports.enqueue(self.store.db, self.run_at - timedelta(seconds=1))
        self.assertFalse(self.inbox.pending(scheduled=True))
        projects.choose(self.store, 7, 0)
        row = self.queued()
        self.assertFalse(self.inbox.pending())
        self.assertEqual(row["project_id"], self.project)
        await bot._handle(row, self.store, self.inbox, self.tg, self.agent)
        self.assertEqual(self.tg.sent[-1][0], "sendRichMessage")
        self.assertIn("<h2>В чём бизнес</h2>", self.tg.sent[-1][1]["rich_message"]["html"])
        self.assertEqual(self.tg.sent[-1][1]["chat_id"], 7)
        tools = self.agent.completion.await_args.kwargs["tools"]
        self.assertEqual({t["function"]["name"] for t in tools}, REPORT_TOOL_NAMES)
        self.assertEqual(self.store.get_task(7, task["id"])["status"], "done")
        self.assertEqual(self.store.get_task(7, task["id"])["report_state"], "sent")
        self.assertEqual(self.store.history(7, project_id=self.project)[-1]["content"], ARTICLE.strip())
        self.assertEqual(self.store.history(7), [])
        reports.enqueue(self.store.db, self.run_at + timedelta(seconds=2))
        self.assertFalse(self.inbox.pending(scheduled=True))

    async def test_retry_delivery_reuses_the_generated_result(self):
        task = self.schedule()
        self.assertEqual(self.schedule()["id"], task["id"])
        self.agent.completion = AsyncMock(return_value={"role": "assistant", "content": ARTICLE})
        row = self.queued()
        self.tg.unavailable = True
        await bot._handle(row, self.store, self.inbox, self.tg, self.agent)
        self.assertFalse(self.tg.sent)
        self.assertEqual(self.store.get_task(7, task["id"])["report_state"], "pending")
        self.store.db.execute("UPDATE bot_inbox SET available=0")
        self.tg.unavailable = False
        await bot._handle(self.queued(), self.store, self.inbox, self.tg, self.agent)
        self.assertEqual(len(self.tg.sent), 1)
        self.agent.completion.assert_awaited_once()
        self.assertEqual(self.store.get_task(7, task["id"])["report_state"], "sent")

    async def test_rescheduling_discards_the_old_queue_item(self):
        task = self.schedule()
        row = self.queued()
        later = self.run_at + timedelta(days=1)
        self.store.update_task(7, task["id"], {"due_at": timestamp(later)})
        self.agent.completion = AsyncMock()
        await bot._handle(row, self.store, self.inbox, self.tg, self.agent)
        self.agent.completion.assert_not_awaited()
        self.assertFalse(self.tg.sent)
        self.assertFalse(self.inbox.pending(scheduled=True))
        self.assertIsNone(self.store.get_task(7, task["id"])["remind_at"])
        reports.enqueue(self.store.db, later)
        self.assertEqual(len(self.inbox.pending(scheduled=True)), 1)

    async def test_failed_report_can_be_rescheduled(self):
        task = self.schedule()
        self.agent.completion = AsyncMock(side_effect=ModelUnavailable(400))
        await bot._handle(self.queued(), self.store, self.inbox, self.tg, self.agent)
        failed = self.store.get_task(7, task["id"])
        self.assertEqual((failed["status"], failed["report_state"]), ("open", "failed"))
        self.assertIn("новое время", self.tg.sent[-1][1]["text"])
        reports.enqueue(self.store.db, self.run_at + timedelta(minutes=1))
        self.assertFalse(self.inbox.pending(scheduled=True))
        edited = self.store.update_task(7, task["id"], {"due_at": timestamp(self.run_at + timedelta(days=1))})
        self.assertEqual(edited["report_state"], "pending")

    async def test_scheduled_turn_cannot_create_tasks_even_if_model_requests_it(self):
        self.schedule()
        tool = {"id": "wrong", "type": "function", "function": {
            "name": "create_task", "arguments": '{"title":"Simulated forbidden action"}',
        }}
        self.agent.completion = AsyncMock(side_effect=[
            {"role": "assistant", "content": None, "tool_calls": [tool]},
            {"role": "assistant", "content": ARTICLE},
        ])
        await bot._handle(self.queued(), self.store, self.inbox, self.tg, self.agent)
        self.assertEqual(len(self.store.list_tasks(7)), 1)
        second_prompt = self.agent.completion.await_args.args[0]
        self.assertIn("unavailable in a scheduled report", second_prompt[-1]["content"])
