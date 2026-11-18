import unittest

from aleph import bot, projects
from aleph.llm import Aleph
from aleph.store import Store


class ProjectIsolation(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.inbox = bot.Inbox(self.store)
        for uid in (10, 11):
            self.store.ensure_user(uid, "", "ru")
        self.first = projects.create(self.store, 10, "Lab One", "Отвечай от лица редактора.")["id"]
        self.second = projects.create(self.store, 10, "Дом")["id"]

    async def test_projects_scope_context_tools_and_history(self):
        project = self.first
        other = self.second
        llm = Aleph(None, "unused")
        projects.chat_command(self.store, 10, "переключись на проект Дом")
        self.assertEqual(projects.active(self.store, 10), other)
        self.store.add_history(10, "user", "ЛИЧНОЕ")
        self.store.add_history(10, "user", "СОСЕДНЕЕ", project_id=other)
        self.store.add_history(10, "user", "ПРОЕКТНОЕ", project_id=project)
        await llm.use_tool(self.store, 10, "remember_fact", {"text": "ПАМЯТЬ"}, "test", project_id=project)
        messages, _ = llm.turn_context(self.store, 10, "Привет", None, project)
        context = str(messages)
        for value in ("Отвечай от лица редактора", "ПАМЯТЬ", "ПРОЕКТНОЕ"):
            self.assertIn(value, context)
        for value in ("СОСЕДНЕЕ", "ЛИЧНОЕ"):
            self.assertNotIn(value, context)
        task = self.store.create_task(10, "Ремонт", project_id=other)
        with self.assertRaises(KeyError):
            await llm.use_tool(self.store, 10, "update_task", {"id": task["id"], "status": "done"},
                               "test", project_id=project)
        with self.assertRaises(KeyError):
            projects.choose(self.store, 11, project)

    async def test_update_keeps_original_project_after_switch(self):
        self.inbox.ingest([{"update_id": 101, "message": {
            "from": {"id": 10}, "chat": {"type": "private", "id": 10}, "text": "Привет",
        }}])
        projects.choose(self.store, 10, self.first)
        self.assertEqual(self.inbox.bind_project(101, 10), self.first)
        projects.choose(self.store, 10, self.second)
        self.assertEqual(self.inbox.bind_project(101, 10), self.first)
        self.inbox.save_reply(self.inbox.pending()[0], "Привет", "Привет!")
        self.assertEqual(len(self.store.history(10, project_id=self.first)), 2)
        self.assertEqual(self.store.history(10, project_id=self.second), [])
