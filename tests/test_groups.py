import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from test_bot import update

from aleph import bot, connections, groups, integrations
from aleph.llm import Aleph
from aleph.store import Store

IDENTITY = {"id": 9000, "username": "TestAlephBot", "can_join_groups": True}
GROUP = -100123


def group_event(number, uid, text, **fields):
    event = update(number, uid, text)
    event["message"].update(
        chat={"type": "supergroup", "id": GROUP, "title": "Product team"},
        message_id=number + 80,
        is_topic_message=True,
        message_thread_id=70,
        **fields,
    )
    return event


class GroupFlow(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.inbox = bot.Inbox(self.store, IDENTITY)
        self.agent = Aleph(None, "test")
        self.agent.completion = AsyncMock()
        self.tg = AsyncMock()
        self.sent = []

        async def send(method, **packet):
            if method in {"sendMessage", "editMessageText", "sendRichMessage"}:
                self.sent.append(packet)
            return {"message_id": 1000 + len(self.sent)}

        self.tg.call.side_effect = send

    async def process(self, group=True):
        [row] = self.inbox.pending(scheduled=group)
        await bot._handle(row, self.store, self.inbox, self.tg, self.agent)

    def test_only_addressed_messages_and_telegram_utf16_offsets(self):
        text = "🧑‍💻 @tEsTaLePhBoT, подсчитай сумму"
        request = group_event(8, 1, text, entities=[{"type": "mention", "offset": 6, "length": 13}])
        self.inbox.ingest(
            [
                group_event(1, 1, "untagged"),
                group_event(2, 1, "/ask@OtherBot nope"),
                group_event(3, 1, "/ask@TestAlephBot no", sender_chat={"id": GROUP}),
                group_event(
                    4, 1, "@OtherBot привет", entities=[{"type": "mention", "offset": 0, "length": 9}]
                ),
                group_event(5, 1, "plain", reply_to_message={"from": {"id": 999}}),
                request,
                request,
            ]
        )
        [row] = self.inbox.pending(scheduled=True)
        self.assertEqual(row["id"], 8)
        self.assertEqual(json.loads(row["message"])["text"], "🧑‍💻 , подсчитай сумму")
        self.assertFalse(self.inbox.pending())
        follow = group_event(9, 1, "Подробнее", reply_to_message={"from": {"id": 9000}, "text": "Отчёт"})
        self.assertEqual(groups.addressed(follow["message"], IDENTITY)["quote"], "Отчёт")

    async def test_public_research_returns_to_topic_without_personal_state(self):
        self.store.ensure_user(1)
        self.store.add_history(1, "user", "private workspace secret")
        event = group_event(
            1,
            1,
            "/ask@TestAlephBot проведи исследование рынка",
            reply_to_message={"text": "Phones in Europe"},
            quote={"text": "Europe"},
        )
        self.inbox.ingest([event, event])
        self.agent.completion.side_effect = [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "x",
                        "type": "function",
                        "function": {
                            "name": "reference_search",
                            "arguments": '{"query":"телефоны","language":"ru"}',
                        },
                    }
                ],
            },
            {"role": "assistant", "content": "Ответ участникам."},
        ]
        self.agent.reference_search = AsyncMock(return_value=[{"title": "Phones"}])
        await self.process()
        self.agent.reference_search.assert_awaited_once_with("телефоны", "ru")
        self.assertTrue(
            all(
                p["chat_id"] == GROUP
                and p["message_thread_id"] == 70
                and p["reply_parameters"]["message_id"] == 81
                for p in self.sent
            )
        )
        self.assertEqual(self.sent[-1]["text"], "Ответ участникам.")
        args = self.agent.completion.await_args
        self.assertEqual(
            {t["function"]["name"] for t in args.kwargs["tools"]},
            {"reference_search", "search_publications", "continue_privately"},
        )
        self.assertNotIn("private workspace secret", str(args.args[0]))
        self.assertEqual(json.loads(args.args[0][1]["content"])["quoted_message"], "Europe")
        self.assertEqual(len(self.store.history(1)), 1)
        parent = {"from": {"id": IDENTITY["id"]}, "message_id": 1002}
        follow = group_event(2, 2, "Уточни", reply_to_message=parent)
        self.inbox.ingest([follow])
        context = json.loads(self.inbox.pending(scheduled=True)[0]["message"])["quote"]
        self.assertIn("исследование рынка", context)
        self.assertIn("Ответ участникам", context)
        other = group_event(3, 3, "Уточни", reply_to_message=parent)
        other["message"]["chat"]["id"] -= 1
        self.inbox.ingest([other])
        alien = self.store.db.execute("SELECT message FROM bot_inbox WHERE id=3").fetchone()[0]
        self.assertEqual(json.loads(alien)["quote"], "")

    async def test_first_contact_handoff_is_owner_only_and_consumed_once(self):
        event = group_event(
            12, 2, "/ask@TestAlephBot сохрани это в Notion", reply_to_message={"text": "Issue 123"}
        )
        self.inbox.ingest([event])
        await self.process()
        [notice] = self.sent
        self.assertNotIn("Issue 123", str(notice))
        link = notice["reply_markup"]["inline_keyboard"][0][0]["url"].split("?start=")[1]

        def click(n, uid):
            packet = update(n, uid, "/start " + link)
            packet["message"]["message_id"] = n
            self.inbox.ingest([packet])

        click(15, 3)
        await self.process(False)
        self.assertNotIn("Issue 123", self.sent[-1]["text"])
        click(16, 2)
        [row] = self.inbox.pending()
        self.assertEqual(json.loads(row["message"])["reply_to_message"]["text"], "Issue 123")
        with patch.object(connections, "config", return_value=None):
            await self.process(False)
        self.assertEqual(self.sent[-1]["chat_id"], 2)
        self.assertIn("Notion", self.sent[-1]["text"])
        click(17, 2)
        self.assertTrue(json.loads(self.inbox.pending()[0]["message"])["invalid_group_link"])
        self.assertIsNone(self.store.db.execute("SELECT message FROM group_handoffs").fetchone()[0])

    async def test_existing_members_use_their_own_account_and_confirm_personally(self):
        self.tg.session = None
        for uid, provider in ((1, "drive"), (2, "notion")):
            self.store.ensure_user(uid)
            self.inbox.ingest(
                [
                    group_event(
                        20 + uid,
                        uid,
                        "/ask@TestAlephBot Запиши пункт в " + ("Google Drive" if uid == 1 else "Notion"),
                        reply_to_message={"text": "Issue 989"},
                    )
                ]
            )
            await self.process()
            [row] = self.inbox.pending()
            self.assertEqual(row["uid"], uid)
            self.assertIsNone(row["group_chat"])
            args = {"provider": provider, "title": "Задача", "text": "Issue 989", "item": "a" * 32}
            self.agent.completion.side_effect = [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "a",
                            "type": "function",
                            "function": {"name": "prepare_connection_action", "arguments": json.dumps(args)},
                        }
                    ],
                },
                {"role": "assistant", "content": "Подготовил."},
            ]
            with patch.object(
                connections, "statuses", return_value={p: "connected" for p in connections.PROVIDERS}
            ):
                await self.process(False)
            preview = self.sent[-1]
            self.assertEqual(preview["chat_id"], uid)
            self.assertIn("Issue 989", preview["text"])
            data = preview["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
            confirm = {
                "id": "callback",
                "data": data,
                "from": {"id": uid},
                "message": {"message_id": 654, "chat": {"id": uid, "type": "private"}},
            }
            with patch.object(
                integrations, "execute", AsyncMock(return_value="https://example.test/saved")
            ) as remote:
                await integrations.handle(self.store, self.tg, {**confirm, "from": {"id": uid + 5}})
                await integrations.handle(
                    self.store, self.tg, {**confirm, "message": {"chat": {"id": GROUP, "type": "supergroup"}}}
                )
                remote.assert_not_awaited()
                await integrations.handle(self.store, self.tg, confirm)
                await integrations.handle(self.store, self.tg, confirm)
                remote.assert_awaited_once()
                self.assertEqual(remote.await_args.args[2], uid)
        self.assertTrue(all("Issue 989" not in str(p) for p in self.sent if p["chat_id"] == GROUP))

    async def test_private_chat_can_answer_while_group_is_busy(self):
        self.inbox.ingest([group_event(9, 1, "/ask@TestAlephBot сравни цены")])
        busy, release = asyncio.Event(), asyncio.Event()

        async def slow(*args, **kwargs):
            busy.set()
            await release.wait()
            return {"content": "Group done"}

        self.agent.completion.side_effect = slow
        self.agent.respond = AsyncMock(return_value="Private done")
        changed = asyncio.Event()
        worker = asyncio.create_task(bot._work(self.store, self.inbox, self.tg, self.agent, changed))
        try:
            async with asyncio.timeout(3):
                await busy.wait()
                self.inbox.ingest([update(10, 1, "Привет!")])
                changed.set()
                while not any(m.get("text") == "Private done" for m in self.sent):
                    await asyncio.sleep(0.01)
                self.assertFalse(any(m.get("text") == "Group done" for m in self.sent))
                release.set()
                while not any(m.get("text") == "Group done" for m in self.sent):
                    await asyncio.sleep(0.01)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
