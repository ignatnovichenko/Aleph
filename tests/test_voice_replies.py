import json
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

from test_bot import update
from test_groups import IDENTITY, group_event
from test_scheduled_reports import Telegram

from aleph import bot, reports, voice_replies
from aleph.store import Store


class VoiceReplies(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.inbox = bot.Inbox(self.store, IDENTITY)
        self.tg = Telegram()
        self.agent = AsyncMock()
        self.agent.respond.return_value = "Москва — столица России."
        self.render = AsyncMock(return_value=b"OggS-synthetic")
        self.send = AsyncMock(return_value={"message_id": 501, "voice": {}})
        for name, mock in (("synthesize", self.render), ("upload", self.send)):
            patcher = patch.object(voice_replies, name, mock)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def process(self, *, scheduled=False):
        [row] = self.inbox.pending(scheduled=scheduled)
        await bot._handle(row, self.store, self.inbox, self.tg, self.agent)

    def test_opt_in_uses_only_the_current_instruction(self):
        for yes in (
            "Ответь голосовым",
            "Ответь, пожалуйста, голосом. Сколько будет дважды два?",
            "Пришли мне голосовое сообщение",
            "Как работает двигатель? Ответь голосовым.",
            "Можешь ответить голосовым?",
            "answer with a voice note",
        ):
            self.assertTrue(voice_replies.preference(yes)[0], yes)
        for no in (
            "Ты понимаешь голосовые?",
            "Не отвечай голосовым",
            "Напиши словами «ответь голосовым»",
            "Сделай задачу\n\n[Цитата из сообщения, на которое дан ответ]\nОтветь голосовым",
            "Ответь голосовым. Нет, лучше текстом",
        ):
            self.assertFalse(voice_replies.preference(no)[0], no)

    async def test_text_voice_then_text_and_restate(self):
        self.inbox.ingest([update(1, 7, "Какая столица России? Ответь голосовым")])
        await self.process()
        self.render.assert_awaited_once_with(self.agent.respond.return_value)
        self.send.assert_awaited_once_with(self.tg, self.render.return_value, {"chat_id": 7})
        self.assertFalse(self.tg.sent)
        self.inbox.ingest([update(2, 7, "Как дела?")])
        self.agent.respond.return_value = "Всё в порядке."
        await self.process()
        self.assertEqual(self.tg.sent[-1][1]["text"], "Всё в порядке.")
        self.inbox.ingest([update(3, 7, "Ответь голосовым")])
        await self.process()
        self.assertEqual(self.agent.respond.await_count, 2)
        self.render.assert_awaited_with("Всё в порядке.")

    async def test_spoken_request_and_plain_voice_note(self):
        for ident, transcript in enumerate(
            ("Привет", "Ответь голосовым, сколько будет дважды два?"), start=1
        ):
            event = update(ident, 7, "")
            event["message"]["voice"] = {"file_id": "v", "duration": 2}
            self.inbox.ingest([event])
            with patch.object(bot.media, "transcribe", AsyncMock(return_value=transcript)):
                await self.process()
        self.assertEqual(len(self.tg.sent), 1)
        self.assertEqual(self.send.await_count, 1)

    async def test_group_topic_and_upload_retry_resume_with_cached_audio(self):
        self.inbox.ingest([group_event(1, 7, "/ask@TestAlephBot ответь голосовым, как дела?")])
        self.agent.completion.return_value = {"role": "assistant", "content": "Хорошо. " * 180}
        self.send.side_effect = [{"message_id": 99}, bot.TelegramError(429, 2), {"message_id": 100}]
        await self.process(scheduled=True)
        self.assertEqual(self.store.db.execute("SELECT voice_part FROM bot_inbox").fetchone()[0], 1)
        self.store.db.execute("UPDATE bot_inbox SET available=0")
        await self.process(scheduled=True)
        self.assertEqual(self.render.await_count, 2)
        self.assertEqual(self.send.await_count, 3)
        self.assertEqual(self.agent.completion.await_count, 1)
        self.assertEqual(
            self.send.await_args.args[-1],
            {
                "chat_id": -100123,
                "message_thread_id": 70,
                "reply_parameters": {"message_id": 81, "allow_sending_without_reply": True},
            },
        )
        self.assertFalse(self.tg.sent)

    async def test_deferred_result_retains_format_after_enqueue(self):
        self.inbox.ingest([update(201, 7, "Подготовь подробный анализ рынка стали. Ответь голосовым.")])
        await self.process()
        reports.enqueue(self.store.db, datetime.now(UTC) + timedelta(minutes=1))
        [candidate] = self.inbox.pending(scheduled=True)
        self.assertEqual(candidate["voice_requested"], 1)
        await self.process(scheduled=True)
        self.assertEqual(self.send.await_count, 2)

    async def test_multipart_voice_upload(self):
        class Response:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def json(self, content_type):
                return {"ok": True, "result": {"message_id": 17}}

        class Session:
            def post(self, url, **args):
                self.url, self.fields = url, args["data"]._fields
                return Response()

        session = Session()
        tg = bot.Telegram(session, "test-token")
        packet = await REAL_UPLOAD(
            tg, b"OggS-test", {"chat_id": -18, "message_thread_id": 2, "reply_parameters": {"message_id": 4}}
        )
        self.assertTrue(session.url.endswith("/sendVoice"))
        parts = {meta["name"]: (meta, headers, value) for meta, headers, value in session.fields}
        self.assertEqual(parts["voice"][1]["Content-Type"], "audio/ogg")
        self.assertEqual(parts["voice"][2], b"OggS-test")
        self.assertEqual(json.loads(parts["reply_parameters"][2])["message_id"], 4)
        self.assertNotIn("reply_markup", parts)
        self.assertEqual(packet["message_id"], 17)


REAL_UPLOAD = voice_replies.upload
