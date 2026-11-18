import sqlite3
import tempfile
import unittest
from pathlib import Path

from aleph import memory
from aleph.store import Store


class MemoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "aleph.sqlite"
        self.store = Store(self.path)
        self.addCleanup(lambda: self.store.close())
        self.store.ensure_user(1)
        self.store.ensure_user(2)
        memory.initialize(self.store.db)

    def test_remember_and_forget_are_scoped_and_persistent(self):
        saved = memory.remember(self.store.db, 1, "Пишу  по-русски")
        self.assertEqual(saved, memory.remember(self.store.db, 1, " пишу ПО-РУССКИ "))
        self.assertEqual([], memory.list_memories(self.store.db, 2))
        with self.assertRaises(KeyError):
            memory.forget(self.store.db, 2, saved["id"])
        other = memory.remember(self.store.db, 2, saved["text"])
        self.store.close()
        self.store = Store(self.path)
        memory.initialize(self.store.db)
        self.assertEqual([saved], memory.list_memories(self.store.db, 1))
        self.assertEqual(saved, memory.forget(self.store.db, 1, saved["id"]))
        self.assertEqual([], memory.list_memories(self.store.db, 1))
        self.assertEqual([other], memory.list_memories(self.store.db, 2))

    def test_retries_at_capacity_and_owned_user(self):
        for i in range(memory.MAX_MEMORIES):
            memory.remember(self.store.db, 1, str(i))
        first = memory.list_memories(self.store.db, 1)[0]
        self.assertEqual(first, memory.remember(self.store.db, 1, first["text"]))
        with self.assertRaises(ValueError):
            memory.remember(self.store.db, 1, "extra")
        memory.forget(self.store.db, 1, first["id"])
        self.assertGreater(memory.remember(self.store.db, 1, "extra")["id"], first["id"])
        with self.assertRaises(sqlite3.IntegrityError):
            memory.remember(self.store.db, 999, "orphan")
