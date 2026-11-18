import json
import unittest
from unittest.mock import AsyncMock
import zlib

from aleph import bot, widgets
from aleph.store import Store


CHECKLIST = {"kind": "checklist", "title": "К поездке", "items": [
    {"label": "Проверить паспорт"}, {"label": "Сверить время вылета"},
]}


class NativeWidgets(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.inbox = bot.Inbox(self.store)
        self.store.ensure_user(89)
        self.inbox.ingest([{"update_id": 10, "message": {
            "from": {"id": 89}, "chat": {"id": 89, "type": "private"}, "text": "Виджет",
        }}])
        widgets.enqueue(self.store, 89, 0, "tg:10:0:0", CHECKLIST)

    async def test_send_once_and_scope_checklist_taps(self):
        telegram = AsyncMock()
        telegram.call.return_value = {"message_id": 200}
        self.assertEqual(await bot._send_widget(self.store, telegram, 89, 10, "План готов.", False), "")
        self.assertEqual(await bot._send_widget(self.store, telegram, 89, 10, "План готов.", False), "")
        telegram.call.assert_awaited_once()
        self.assertEqual(telegram.call.await_args.args, ("sendWidget",))
        self.assertEqual(telegram.call.await_args.kwargs["caption"], "План готов.")
        self.assertTrue(telegram.call.await_args.kwargs["allow_queries"])

        query = {"id": "q", "from": {"id": 89}, "chat": {"id": 89, "type": "private"},
                 "message_id": 200, "data": "check:1:1", "type": {"type": "data"}}
        self.assertIsNone(widgets.change_check(self.store, {**query, "from": {"id": 55}}))
        self.assertIsNone(widgets.change_check(self.store, {**query, "type": {"type": "sign_data"}}))
        self.assertIsNone(widgets.change_check(self.store, {**query, "data": "check:5:1"}))
        await widgets.handle_query(self.store, telegram, query)
        self.assertEqual(telegram.call.await_args_list[-2].args, ("answerWidgetQuery",))
        self.assertEqual(telegram.call.await_args.kwargs["chat_data"], '{"checked":2}')
        self.assertEqual(widgets.change_check(self.store, query), (89, 200, '{"checked":2}'))
        self.inbox.done(10)
        self.assertEqual(widgets.change_check(self.store, {**query, "data": "check:1:0"})[2], '{"checked":0}')

    async def test_two_native_rejections_do_not_block_same_chat(self):
        async def send(method, **params):
            if method == "sendWidget":
                raise bot.TelegramError(400, description="Bad Request: INPUT_MEDIA_WIDGET_NOT_ALLOWED")
            return {"message_id": self.next_message}

        self.next_message = 201
        telegram = AsyncMock()
        telegram.call.side_effect = send
        self.assertEqual(await bot._send_widget(self.store, telegram, 89, 10, "Вот план.", False), "")
        self.assertEqual(telegram.call.await_args.args, ("sendMessage",))
        self.assertIn("Проверить паспорт", telegram.call.await_args.kwargs["text"])
        self.inbox.done(10)
        self.inbox.ingest([{"update_id": 11, "message": {
            "from": {"id": 89}, "chat": {"id": 89, "type": "private"}, "text": "Ещё виджет",
        }}])
        widgets.enqueue(self.store, 89, 0, "tg:11:0:0", CHECKLIST)
        self.next_message = 202
        self.assertEqual(await bot._send_widget(self.store, telegram, 89, 11, "Вот ещё.", False), "")
        sent = widgets.for_update(self.store, 11)
        self.assertEqual(sent["message_id"], 202)
        self.assertEqual(sent["fallback"], 1)
        taps = telegram.call.await_args.kwargs["reply_markup"]["inline_keyboard"]
        callback = {"id": "q", "from": {"id": 89}, "message": {
            "chat": {"id": 89, "type": "private"}, "message_id": 202,
        }, "data": taps[1][0]["callback_data"]}
        await widgets.handle_fallback_query(self.store, telegram, callback)
        self.assertEqual(telegram.call.await_args.args, ("editMessageText",))
        self.assertIn("1/2", telegram.call.await_args.kwargs["text"])
        self.assertEqual(widgets.for_update(self.store, 11)["checked"], 2)
        await widgets.handle_fallback_query(self.store, telegram, {**callback, "from": {"id": 90}})
        self.assertEqual(telegram.call.await_args.args, ("answerCallbackQuery",))
        self.assertEqual(widgets.for_update(self.store, 11)["checked"], 2)

    async def test_old_failure_marker_is_migrated(self):
        self.store.db.execute("UPDATE widgets SET message_id=-1 WHERE update_id=10")
        self.inbox.done(10)
        widgets.initialize(self.store.db)
        saved = self.store.db.execute("SELECT message_id,fallback FROM widgets").fetchone()
        self.assertEqual(tuple(saved), (None, 1))

    async def test_render_keeps_content_out_of_script_and_checks_payload(self):
        spec = widgets.clean({**CHECKLIST, "title": "</script><img src=x>"})
        html = widgets.render(spec)
        self.assertNotIn("</script><img", html)
        self.assertEqual(html.count("</script>"), 1)
        self.assertLessEqual(len(zlib.compress(html.encode(), 9)), 4096)
        with self.assertRaises(ValueError):
            widgets.clean({"kind": "chart", "title": "test", "items": [
                {"label": "x", "value": 1}, {"label": "y", "value": float("nan")},
            ]})
