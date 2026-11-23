import asyncio
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from aleph import bot, computers, document_chat, progress, reports
from aleph.store import Store
from test_bot import update
from test_scheduled_reports import Telegram


class BackgroundWorkers(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.inbox = bot.Inbox(self.store)
        self.tg = Telegram()

    async def test_fifth_waits_while_chat_and_other_account_can_run(self):
        for uid in (7, 8):
            self.store.ensure_user(uid, language_code="ru")
        gates = {}
        started = asyncio.Queue()
        agent = AsyncMock()

        async def respond(store, uid, prompt, photo, inbox, update_id, *, scheduled=False):
            if scheduled:
                gates[update_id] = asyncio.Event()
                started.put_nowait((uid, update_id))
                await gates[update_id].wait()
            return f"result {update_id}"

        agent.respond.side_effect = respond
        now = datetime.now(timezone.utc) + timedelta(minutes=1)
        for i in range(5):
            message = update(100+i, 7, f"В фоне подготовь подборку по теме {i}")
            message["message"]["message_id"] = 30+i
            self.inbox.ingest([message])
            await bot._handle(self.inbox.pending()[0], self.store, self.inbox, self.tg, agent)
        reports.enqueue(self.store.db, now)
        changed = asyncio.Event()
        worker = asyncio.create_task(bot._work(self.store, self.inbox, self.tg, agent, changed))
        try:
            async with asyncio.timeout(3):
                busy = [await started.get() for _ in range(4)]
                self.assertEqual(busy, [(7, -i) for i in range(1, 5)])
                self.inbox.ingest([update(211, 7, "Привет!")])
                reports.schedule(self.store, 8, 0, "Other user", "Separate", now.isoformat(), "other")
                reports.enqueue(self.store.db, now + timedelta(seconds=1))
                changed.set()
                self.assertEqual(await started.get(), (8, -6))
                while not any(item["text"] == "result 211" for _, item in self.tg.sent):
                    await asyncio.sleep(.01)
                self.inbox.ingest([update(212, 7, "Что сейчас в фоне?")])
                changed.set()
                while not any("В очереди ·" in item["text"] for _, item in self.tg.sent):
                    await asyncio.sleep(.01)
                status = self.tg.sent[-1][1]["text"]
                self.assertEqual(status.count("В работе ·"), 4)
                self.assertNotIn("Other user", status)
                self.assertNotIn(-5, gates)
                self.assertIsNone(self.store.get_task(7, 5)["report_started_at"])
                gates[-3].set()
                self.assertEqual(await started.get(), (7, -5))
                self.assertEqual(self.store.get_task(7, 3)["report_state"], "sent")
                for gate in gates.values():
                    gate.set()
                while self.store.get_task(7, 5)["report_state"] != "sent":
                    await asyncio.sleep(.01)
                replies = {item["text"]: item.get("reply_parameters") for _, item in self.tg.sent}
                for i in range(1, 6):
                    self.assertEqual(replies[f"result {-i}"]["message_id"], 29+i)
                    self.assertEqual(reports.result(self.store, 7, i)["text"], f"result {-i}")
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    async def test_group_and_spreadsheet_inputs_survive_other_conversations(self):
        packet = update(11, 7, "")
        packet["message"].update(document={"file_name":"budget.xlsx","file_id":"xls-input"}, message_id=123,
                                 caption="Отредактируй бюджет: добавь итоговую строку")
        self.inbox.ingest([packet])
        agent = AsyncMock()
        await bot._handle(self.inbox.pending()[0], self.store, self.inbox, self.tg, agent)
        agent.respond.assert_not_awaited()
        [task] = self.store.list_tasks(7)
        reports.enqueue(self.store.db, datetime.now(timezone.utc) + timedelta(minutes=1))
        section = reports.input_context(self.store, task)
        ref = json.loads(section.splitlines()[-1])[0]["id"]
        for i in range(20):
            document_chat.register(self.store, 7, 0, 200+i, {"file_name":f"{i}.xlsx","file_id":f"other{i}"})
        self.assertEqual(json.loads(document_chat._scoped_ref(self.store, 7, 0, ref)["metadata"])["file_id"], "xls-input")
        self.inbox.ingest([update(12, 7, "Сделай параллельно:\n1. Собери цены на сталь\n2. Подбери журналы")])
        await bot._handle(self.inbox.pending()[0], self.store, self.inbox, self.tg, agent)
        self.assertEqual(len(self.store.list_tasks(7)), 3)
        self.assertFalse(progress.background_prompts("Как сделать в фоне анализ рынка?", [], False))
        self.assertFalse(progress.background_prompts("Завтра в 18:00 подготовь исследование рынка", [], False))

    async def test_computer_reserves_chat_listener_and_replays_only_its_own_job(self):
        hub = computers.ComputerHub()
        hub.touch(7)
        turns = [-1, -2, -3, -4, 11]
        pending = [asyncio.create_task(hub.run(7, "Test", {"turn":number}, SimpleNamespace(update_id=number), 4))
                   for number in turns]
        try:
            async with asyncio.timeout(2):
                chat = await hub.claim(7, "chat-session", "chat")
                jobs = await asyncio.gather(*(hub.claim(7, f"slot-{i}", "background") for i in range(4)))
                self.assertEqual(chat["turn"]["turn"], 11)
                self.assertEqual({job["turn"]["turn"] for job in jobs}, set(turns[:4]))
                self.assertEqual(await hub.claim(7, "slot-0", "background"), jobs[0])
                self.assertIsNone(hub.job(7, "slot-0", jobs[1]["id"]))
        finally:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
