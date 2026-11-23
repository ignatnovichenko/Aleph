import base64
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer
from aleph import (
    bot,
    connections,
    document_chat,
    file_versions,
    folder,
    initiative,
    mail_events,
    price_rules,
    profile_sources,
    project_sources,
    projects,
    reports,
)
from aleph.__main__ import application
from aleph.scheduler import deliver
from aleph.store import Store, timestamp
from test_backend import TEST_TOKEN, OkSession
from test_bot import update


class ProactiveWork(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.store.ensure_user(1, language_code="ru")
        self.store.ensure_user(2)
        self.inbox = bot.Inbox(self.store)
        self.project = projects.create(self.store, 1, "Offers")["id"]

    async def test_gmail_push_to_delivery(self):
        db = self.store.db
        db.execute("INSERT INTO connections(user_id,provider,credentials) VALUES (1,'gmail','fake')")
        msg = {
            "id": "matched",
            "internalDate": str(int(time.time() * 1000) + 1000),
            "labelIds": ["INBOX"],
            "payload": {
                "headers": [
                    {"name": "From", "value": "Alice <sales@example.org>"},
                    {"name": "Subject", "value": "New quote"},
                ]
            },
        }

        async def google(store, session, uid, provider, method, path, **kwargs):
            self.assertEqual(uid, 1)
            if path.endswith("/profile"):
                return {"emailAddress": "me@example.org", "historyId": "540"}
            if path.endswith("/watch"):
                return {"historyId": "540", "expiration": int(time.time() + 86400 * 5) * 1000}
            if path.endswith("/history"):
                return {
                    "historyId": "545",
                    "history": [{"messagesAdded": [{"message": {"id": k}} for k in ["other", "matched"]]}],
                }
            if path.endswith("/other"):
                return {
                    **msg,
                    "payload": {
                        "headers": [
                            {"name": "From", "value": "other@example.org"},
                            {"name": "Subject", "value": "quote"},
                        ]
                    },
                }
            return msg

        with (
            patch.dict(
                "os.environ",
                {"ALEPH_GMAIL_TOPIC": "projects/test/topics/mail", "ALEPH_GMAIL_PUSH_SECRET": "x" * 40},
            ),
            patch.object(connections, "statuses", return_value={"gmail": "connected"}),
            patch.object(connections, "call", side_effect=google),
        ):
            task = await mail_events.watch(
                self.store,
                None,
                1,
                self.project,
                {"title": "Wait for offer", "subject": "quote", "sender": "sales@example.org"},
                "k",
            )
            client = TestClient(TestServer(application(self.store, TEST_TOKEN)))
            await client.start_server()
            self.addAsyncCleanup(client.close)
            packet = {
                "message": {
                    "data": base64.b64encode(
                        json.dumps({"emailAddress": "me@example.org", "historyId": 545}).encode()
                    ).decode()
                }
            }
            self.assertEqual((await client.post("/events/gmail", json=packet)).status, 403)
            for _ in range(2):
                self.assertEqual(
                    (await client.post("/events/gmail?token=" + "x" * 40, json=packet)).status, 204
                )
                row = db.execute(
                    "SELECT s.*,c.account_epoch FROM gmail_streams s JOIN connections c ON c.user_id=s.uid"
                ).fetchone()
                if row["target"] > row["cursor"]:
                    await mail_events.sync(self.store, None, row)
            self.store.patch_settings(1, {"initiative": "quiet", "proactive": False})
            tg = OkSession()
            await deliver(self.store, tg, TEST_TOKEN)
            await deliver(self.store, tg, TEST_TOKEN)
            self.assertEqual(len(tg.messages), 1)
            self.assertIn("New quote", tg.messages[0]["text"])
            self.assertEqual(self.store.get_task(1, task["id"])["status"], "done")
            connections.disconnect(self.store, 1, "gmail")
            self.assertEqual(db.execute("SELECT count(*) FROM gmail_streams").fetchone()[0], 0)

    async def test_price_file_update_and_replay(self):
        async def upload(n):
            return await project_sources.add_file(
                self.store, 1, self.project, "Offers.csv", f"SKU;Price\nA;{n}".encode()
            )

        source = await upload(100)
        price_rules.create(
            self.store,
            1,
            self.project,
            {"source_id": source["id"], "title": "Price", "label": "A", "column": 2, "percent": 5},
            "rule",
        )
        for n in [105, 125, 125]:
            await upload(n)
        rows = self.store.db.execute("SELECT body FROM deliveries WHERE kind='price'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertIn("105 → 125", rows[0][0])

    async def test_levels_and_source_opt_in(self):
        now = datetime.now(timezone.utc)
        self.store.patch_settings(
            1, {"quiet_start": "00:00", "quiet_end": "00:00", "focus": "Apollo", "initiative": "quiet"}
        )
        self.store.create_task(1, "Apollo review", timestamp(now + timedelta(hours=1)), remind_at=None)
        initiative.queue(self.store, now)
        self.assertFalse(self.store.db.execute("SELECT 1 FROM deliveries").fetchone())
        self.store.patch_settings(1, {"initiative": "balanced"})
        initiative.queue(self.store, now)
        self.assertIn("Apollo", self.store.db.execute("SELECT body FROM deliveries").fetchone()[0])
        self.store.create_task(2, "Other project", timestamp(now + timedelta(hours=1)), remind_at=None)
        self.store.patch_settings(2, {"initiative": "active", "quiet_start": "00:00", "quiet_end": "00:00"})
        initiative.queue(self.store, now)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM deliveries").fetchone()[0], 2)
        self.store.patch_settings(1, {"context_sources": True})
        self.store.db.execute(
            "INSERT INTO connections(user_id,provider,credentials) VALUES (1,'drive','abc')"
        )
        with patch(
            "aleph.integrations.read",
            AsyncMock(side_effect=[{"files": [{"name": "Apollo", "id": "file1"}]}, {"text": "Decision"}]),
        ):
            items = await profile_sources.collect(self.store, None, 1, "drive", "Apollo")
        self.store.db.execute(
            "INSERT INTO profile_sources VALUES(?,?,?,?,?)",
            (1, "drive", "Apollo", json.dumps(items), time.time()),
        )
        self.assertEqual(profile_sources.current(self.store, 1)["drive"][0]["excerpt"], "Decision")
        self.assertFalse(profile_sources.current(self.store, 2))
        self.store.patch_settings(1, {"context_sources": False})
        self.assertFalse(profile_sources.current(self.store, 1))

    async def test_pinned_telegram_versions_survive_reference_eviction(self):
        project = self.project
        meta = {"file_name": "Original.txt", "file_id": "telegram-original", "file_size": 3}
        ref = document_chat.register(self.store, 1, project, 11, meta)
        pins = reports.pins(self.store, 1, project, json.dumps({"files": [ref]}))
        report = reports.schedule(
            self.store,
            1,
            project,
            "Copy",
            "Copy it",
            timestamp(datetime.now(timezone.utc) + timedelta(hours=1)),
            "r",
            input_versions=pins,
        )
        self.store.db.execute("DELETE FROM bot_file_refs")
        self.assertIn("Original.txt", reports.input_context(self.store, report))
        self.inbox.ingest([update(44, 1, "restore")])
        with self.assertRaises(ValueError):
            await file_versions.stage_restore(self.store, 2, project, pins[0], "tg:44:restore", AsyncMock())
        await file_versions.stage_restore(
            self.store, 1, project, pins[0], "tg:44:restore", AsyncMock(return_value=b"abc")
        )
        self.assertEqual(
            self.store.db.execute("SELECT parent_version FROM bot_documents").fetchone()[0], pins[0]
        )

    def test_folder_scope_and_non_destructive_retries(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "source.txt").write_bytes(b"original")
            (root / "alias.txt").symlink_to(root / "source.txt")
            self.assertEqual(folder.list_files(root), ["source.txt"])
            with self.assertRaises(OSError):
                folder.read(root, "alias.txt")
            with self.assertRaises(ValueError):
                folder.read(root, "../other.txt")
            name = folder.save(root, "source.txt", b"edited")
            self.assertNotEqual(name, "source.txt")
            self.assertEqual(folder.save(root, "source.txt", b"edited"), name)
            self.assertEqual(folder.read(root, "source.txt"), b"original")
