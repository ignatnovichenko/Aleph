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

    async def test_old_bot_api_gets_full_plain_text(self):
        telegram = AsyncMock()
        telegram.call.side_effect = bot.TelegramError(404)
        content = await bot._send_widget(self.store, telegram, 89, 10, "Вот план.", False)
        self.assertIn("Проверить паспорт", content)
        self.assertIn("Сверить время вылета", content)
        self.assertEqual(content, await bot._send_widget(self.store, telegram, 89, 10, "Вот план.", False))
        telegram.call.assert_awaited_once()

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
