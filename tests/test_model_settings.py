import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aleph import bot, models
from aleph.llm import Aleph, ChatGPTUnavailable, ModelRejectsPhoto
from aleph.store import Store


OWNER = 77
CHATGPT = {"id": "gpt-test", "name": "GPT Test", "levels": ["low", "high"], "images": True}
OPENCODE = {"id": "claude-test", "name": "Claude Test", "levels": ["medium"], "images": False}


class ModelSettingsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.store.ensure_user(OWNER)
        self.inbox = bot.Inbox(self.store)
        self.catalogs = models.ModelCatalogs("synthetic-key")
        self.catalogs.get = AsyncMock(side_effect=lambda _, uid, provider: {
            "status": "ready", "models": [CHATGPT if provider == "chatgpt" else OPENCODE],
        })
        self.agent = Aleph(None, "synthetic-key")
        self.agent.completion = AsyncMock(return_value={"role": "assistant", "content": "Ready."})
        self.record = {"client_id": "client-test", "state": "connected", "model": "gpt-test", "model_name": "GPT Test"}

    async def choose(self, provider, model, effort):
        return await self.catalogs.choose(None, self.store, OWNER, {
            "provider": provider, "model": model, "reasoning": effort,
        })

    def ingest(self, *ids):
        self.inbox.ingest([{"update_id": item, "message": {
            "from": {"id": OWNER}, "chat": {"id": OWNER, "type": "private"}, "text": "Test",
        }} for item in ids])

    async def complete(self, update_id, step=0, content="Test"):
        return await self.agent.complete_turn([{"role": "user", "content": content}], "base-opencode", tools=True,
                                              uid=OWNER, project_id=0, ledger=self.inbox, update_id=update_id, step=step)

    async def test_changes_apply_to_next_turn_and_persist_per_provider(self):
        with patch.object(models.plan, "record", return_value=self.record), patch.object(
            models.plan, "complete", AsyncMock(return_value={"role": "assistant", "content": "Ready.", "_model_id": "gpt-test-real"}),
        ) as chatgpt:
            with self.assertRaises(ValueError):
                await self.choose("opencode", "claude-test", "high")
            await self.choose("chatgpt", "gpt-test", "low")
            self.ingest(1, 2)
            await self.complete(1)
            await self.choose("opencode", "claude-test", "medium")
            reply = await self.complete(1, step=1)
            self.assertEqual(chatgpt.await_args.kwargs, {"model": "gpt-test", "reasoning": "low"})
            self.assertEqual(reply["_aleph_model"]["model"], "gpt-test-real")
            self.inbox.save_step(1, 1, reply)
            self.inbox.save_reply(self.inbox.pending()[0], "Test", "Ready.")
            self.inbox.done(1)
            self.assertEqual(models.describe(self.store, OWNER, "base-opencode")["last"]["provider"], "chatgpt")
            newer = await self.complete(2)
            self.assertEqual(newer["_aleph_model"]["provider"], "opencode")
            self.assertEqual(self.agent.completion.await_args.args[1], "claude-test")
            self.assertEqual(self.agent.completion.await_args.kwargs["reasoning"], "medium")
            self.assertFalse(self.agent.completion.await_args.kwargs["fallback"])
            self.assertEqual(models.saved(self.store, OWNER)[1]["chatgpt"]["reasoning"], "low")

    async def test_missing_login_tells_user_to_reconnect_and_never_uses_other_provider(self):
        with patch.object(models.plan, "record", return_value=None):
            await self.choose("chatgpt", "gpt-test", "high")
            self.ingest(1)
            with self.assertRaises(ChatGPTUnavailable) as failure:
                await self.complete(1)
            self.assertEqual(failure.exception.status, 401)
            self.agent.completion.assert_not_awaited()
            models.disconnect_chatgpt(self.store, OWNER)
            self.assertEqual(models.describe(self.store, OWNER, "base-opencode")["active"]["provider"], "opencode")

    async def test_text_only_model_reports_photos_before_using_credits(self):
        await self.choose("opencode", "claude-test", "medium")
        self.ingest(1)
        with self.assertRaises(ModelRejectsPhoto):
            await self.complete(1, content=[{"type": "image_url", "image_url": {"url": "example"}}])
        self.agent.completion.assert_not_awaited()

    async def test_selection_survives_reopening_the_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "aleph.sqlite3"
            current = Store(path)
            try:
                current.ensure_user(OWNER)
                await self.catalogs.choose(None, current, OWNER, {
                    "provider": "opencode", "model": "claude-test", "reasoning": "medium",
                })
            finally:
                current.close()
            reopened = Store(path)
            try:
                active = models.describe(reopened, OWNER, "base-opencode")["active"]
                self.assertEqual((active["provider"], active["model"], active["reasoning"]),
                                 ("opencode", "claude-test", "medium"))
            finally:
                reopened.close()
