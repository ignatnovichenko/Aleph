import unittest
import base64
import gzip
import json
from aleph.bot import Inbox
from aleph.bridge import apply_web_action, keyboard_markup
from aleph.privacy import privacy_reply
from aleph.store import Store


class PersonalWorkspace(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.inbox = Inbox(self.store)
        for uid in (123, 456):
            self.store.ensure_user(uid, str(uid), "ru")

    def test_keyboard_snapshot_and_idempotent_saving(self):
        payload = json.dumps({"v": 1, "method": "POST", "path": "/api/tasks", "body": {"title": "Встреча"}})
        for _ in range(2):
            apply_web_action(self.store, 123, payload, "tg:12:web")
        for i in range(30):
            self.store.create_task(456, "Другое" + str(i))
        url = keyboard_markup(self.store, 123, "https://example.org/aleph")["keyboard"][0][0]["web_app"]["url"]
        packed = url.split("#aleph=")[1]
        state = json.loads(gzip.decompress(base64.urlsafe_b64decode(packed + "=" * (-len(packed) % 4))))
        self.assertEqual(["Встреча"], [x["title"] for x in state["tasks"]])
        self.assertLess(len(url), 2000)
        self.assertEqual(len(self.store.list_tasks(456)), 30)

    def test_two_step_erasure_is_scoped(self):
        for uid in (123,456):
            self.store.create_task(uid, "Задача")
            self.store.create_routine(uid, "Утро", "agenda", "09:00")
        privacy_reply(self.store,123,"удалить мои данные навсегда",50)
        self.assertEqual(len(self.store.list_tasks(123)), 1)
        privacy_reply(self.store,123,"удали мои данные",51)
        self.assertIn("удалены", privacy_reply(self.store,123,"удалить мои данные навсегда",52))
        self.assertEqual(self.store.list_tasks(123), [])
        self.assertEqual(len(self.store.list_tasks(456)), 1)
        self.assertEqual(len(self.store.list_routines(456)), 1)
