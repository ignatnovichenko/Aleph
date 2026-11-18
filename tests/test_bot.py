import os
import time
import unittest
from unittest.mock import AsyncMock, patch

from aleph import bot, models
from aleph.llm import Aleph
from aleph.store import Store

def update(n, uid, text):
    return {"update_id": n, "message": {
        "from": {"id": uid, "language_code": "ru"}, "chat": {"id": uid, "type": "private"},
        "text": text,
    }}

class FakeTelegram:
    def __init__(self):
        self.sent = []
        self.fail = False

    async def call(self, method, **kwargs):
        if method == "sendMessage":
            if self.fail:
                raise bot.TelegramError(429, 2)
            self.sent.append(kwargs)

class FakeAgent(Aleph):
    """Exercise the real tool dispatcher and chat serialization."""
    def __init__(self):
        super().__init__(None, "unused")
        self.calls = []

    async def completion(self, messages, model, **options):
        self.calls.append(messages.copy())
        if messages[-1]["role"] == "tool":
            return {"role": "assistant", "content": "Записал."}
        return {"role": "assistant", "reasoning_content": "transient provider context",
                "content": None, "tool_calls": [{"id": "tool1", "type": "function", "function": {
                    "name": "create_task", "arguments": '{"title":"Забрать книгу"}',
                }}]}

class BotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.inbox = bot.Inbox(self.store)
        self.tg = FakeTelegram()
        self.agent = FakeAgent()
        self.env = patch.dict(os.environ, {"MINI_APP_BRIDGE_URL": ""})
        self.env.start()
        self.addCleanup(self.env.stop)

    async def test_create_task_and_retry_delivery_without_model_replay(self):
        telegram_update = update(40, 11, "Напомни забрать книгу")
        self.inbox.ingest([telegram_update])
        self.tg.fail = True
        await bot._handle(self.inbox.pending()[0], self.store, self.inbox, self.tg, self.agent)
        self.assertEqual(len(self.store.list_tasks(11)), 1)
        self.assertEqual(self.store.history(11)[-1]["content"], "Записал.")
        self.assertEqual(len(self.agent.calls), 2)
        self.assertEqual(self.agent.calls[-1][-2]["reasoning_content"], "transient provider context")

        self.store.db.execute("UPDATE bot_inbox SET available=0")
        self.tg.fail = False
        await bot._handle(self.inbox.pending()[0], self.store, self.inbox, self.tg, self.agent)
        self.assertEqual(len(self.agent.calls), 2)
        self.assertEqual(self.tg.sent[0]["text"], "Записал.")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM bot_steps").fetchone()[0], 0)
        self.inbox.ingest([telegram_update])
        self.assertEqual(self.inbox.pending(), [])

    async def test_attachment_delivery_is_resumed_without_recreating_document(self):
        self.inbox.ingest([update(80, 70, "Пришли файл")])
        self.store.ensure_user(70)
        await self.agent.use_tool(self.store, 70, "create_document",
                                  {"format":"txt", "filename":"план", "content":"Позвонить"},
                                  "tg:80:0:0")
        row = self.inbox.pending()[0]
        self.inbox.save_reply(row, "Пришли файл", "Прикрепил план.")
        self.tg.session = None
        self.tg.token = "unused"
        sender = AsyncMock(side_effect=[bot.documents.DocumentDeliveryError(429, 2), 123])
        with patch.object(bot.documents, "send_document", sender):
            await bot._handle(self.inbox.pending()[0], self.store, self.inbox, self.tg, self.agent)
            self.assertEqual(self.tg.sent, [])
            self.store.db.execute("UPDATE bot_inbox SET available=0")
            self.tg.fail = True
            await bot._handle(self.inbox.pending()[0], self.store, self.inbox, self.tg, self.agent)
            self.assertEqual(self.store.db.execute("SELECT sent FROM bot_documents").fetchone()[0], 1)
            self.store.db.execute("UPDATE bot_inbox SET available=0")
            self.tg.fail = False
            await bot._handle(self.inbox.pending()[0], self.store, self.inbox, self.tg, self.agent)
        self.assertEqual(sender.await_count, 2)
        self.assertEqual(self.tg.sent[0]["text"], "Прикрепил план.")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM bot_documents").fetchone()[0], 0)
        self.assertEqual(self.agent.calls, [])

    async def test_scoping_external_and_duplicate_updates(self):
        other = update(23, 2, "Не личный чат")
        other["message"]["chat"]["type"] = "group"
        self.inbox.ingest([update(21, 1, "Первое"), update(22, 1, "Второе"), other])
        self.assertEqual([r["id"] for r in self.inbox.pending()], [21])
        self.assertEqual(self.inbox.offset(), 24)
        self.store.ensure_user(1)
        self.store.ensure_user(2)
        task = self.store.create_task(1, "private")
        with self.assertRaises(KeyError):
            await self.agent.use_tool(self.store, 2, "update_task", {"id":task["id"],"title":"wrong"}, "test")
        with self.assertRaises(ValueError):
            await self.agent.use_tool(self.store, 2, "workspace", {"user_id":1}, "test")
        self.store.db.execute("UPDATE runtime_state SET value=? WHERE key='telegram_last_batch'", (str(time.time()-8*86400),))
        self.assertEqual(self.inbox.offset(), 0)

    async def test_start_and_long_unicode_messages(self):
        self.inbox.ingest([update(7, 22, "/start")])
        await bot._handle(self.inbox.pending()[0], self.store, self.inbox, self.tg, self.agent)
        self.assertIn("Алеф", self.tg.sent[0]["text"])
        self.assertEqual(self.agent.calls, [])
        parts = list(bot.chunks("🎉" * 10000))
        self.assertEqual("".join(parts), "🎉"*10000)
        self.assertTrue(all(len(p.encode("utf-16-le"))//2 <= 4096 for p in parts))

    async def test_model_question_reads_saved_selection_after_voice_transcription(self):
        uid = 24
        self.store.ensure_user(uid)
        catalog = models.ModelCatalogs("unused")
        catalog.get = AsyncMock(return_value={"status": "ready", "models": [
            {"id": "gpt-6.2-sol", "name": "GPT-6.2-Sol", "levels": ["high"], "images": True},
        ]})
        await catalog.choose(None, self.store, uid, {
            "provider": "chatgpt", "model": "gpt-6.2-sol", "reasoning": "high",
        })
        previous = models.model_option("chatgpt", "gpt-6.2-sol", "GPT-6.2-Sol", "high")
        models.remember_reply(self.store, uid, previous)
        voice = update(1, uid, "")
        del voice["message"]["text"]
        voice["message"]["voice"] = {"file_id": "synthetic", "duration": 2}
        self.tg.download = AsyncMock(return_value=b"audio")
        self.inbox.ingest([voice, update(2, uid, "Какая модель сейчас работает?")])
        with patch.object(bot.media, "transcribe", AsyncMock(return_value="Привет, какая ты модель?")) as transcribe:
            for _ in range(2):
                await bot._handle(self.inbox.pending()[0], self.store, self.inbox, self.tg, self.agent)
        transcribe.assert_awaited_once_with(b"audio")
        for message in self.tg.sent:
            self.assertEqual(message["text"],
                             "Сейчас выбрана модель GPT-6.2-Sol через ChatGPT. Уровень рассуждений — высокий.")
        self.assertEqual(len(self.tg.sent), 2)
        self.assertEqual(self.agent.calls, [])
        self.assertEqual(models.describe(self.store, uid, "claude-haiku-5-5")["last"], previous)
