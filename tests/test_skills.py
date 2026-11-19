import json
import unittest
from unittest.mock import AsyncMock

from aiohttp.test_utils import TestClient, TestServer

from aleph import bot, projects, skills
from aleph.__main__ import application
from aleph.llm import Aleph, REPORT_TOOL_NAMES
from aleph.store import Store
from test_backend import TEST_TOKEN, auth
from test_bot import update


MARKDOWN = """---
name: meeting-recap
description: >-
  Use for meeting notes
  and discussion recaps.
allowed-tools: Bash
---

## Recap
List decisions and open questions. Prefix the result with RECAP-14.
"""
DRAFT = {"name": "Итоги встречи", "description": "Для заметок со встреч.", "instructions": "Отметь решения RECAP-14."}


class Skills(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.store.ensure_user(1)
        self.store.ensure_user(2)
        self.work = projects.create(self.store, 1, "Work")["id"]
        self.home = projects.create(self.store, 1, "Home")["id"]
        self.client = TestClient(TestServer(application(self.store, TEST_TOKEN)))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    def request(self, method, path, uid=1, **kwargs):
        return self.client.request(method, path, headers={"Authorization": "tma " + auth(uid)}, **kwargs)

    async def test_library_and_memberships_are_private_and_delete_cascades(self):
        self.assertEqual((await self.client.get("/api/skills")).status, 401)
        created = await self.request("POST", "/api/skills", json=DRAFT)
        self.assertEqual(created.status, 201)
        item = await created.json()
        item_url = f"/api/skills/{item['id']}"
        project_url = f"/api/projects/{self.work}/skills"
        instructions = "Обновлённые инструкции. " * 500
        updated = await self.request("PATCH", item_url, json={**DRAFT, "instructions": instructions})
        self.assertEqual(updated.status, 200)
        self.assertEqual((await updated.json())["instructions"], instructions.strip())
        dupe = await self.request("POST", "/api/skills", json={**DRAFT, "name": "  ИТОГИ  ВСТРЕЧИ "})
        self.assertEqual(dupe.status, 400)
        other_skill = await (await self.request("POST", "/api/skills", uid=2, json=DRAFT)).json()
        self.assertEqual((await self.request("GET", item_url, uid=2)).status, 404)
        self.assertEqual((await self.request("DELETE", item_url, uid=2)).status, 404)
        self.assertEqual((await self.request("GET", project_url, uid=2)).status, 404)
        before = await (await self.request("GET", project_url)).json()
        self.assertEqual(before["selected"], [])
        self.assertNotIn("instructions", before["skills"][0])
        self.assertEqual((await self.request("PATCH", project_url, json={"skill_ids": [item["id"]]})).status, 200)
        self.assertEqual((await self.request("PATCH", project_url, json={"skill_ids": [other_skill["id"]]})).status, 404)
        after = await (await self.request("GET", project_url)).json()
        self.assertEqual(after["selected"], [item["id"]])
        self.assertEqual(skills.for_project(self.store, 1, self.home), [])
        skills.set_project(self.store, 1, 0, [item["id"]])
        skills.set_project(self.store, 2, 0, [other_skill["id"]])
        skills.set_project(self.store, 1, 0, [])
        self.assertEqual(skills.for_project(self.store, 2, 0)[0]["id"], other_skill["id"])
        self.assertEqual((await self.request("DELETE", item_url)).status, 204)
        self.assertEqual(skills.for_project(self.store, 1, self.work), [])

    async def test_preview_parses_standard_yaml_and_does_not_install(self):
        response = await self.request("POST", "/api/skills/preview", json={"markdown": MARKDOWN})
        self.assertEqual(response.status, 200)
        draft = await response.json()
        self.assertEqual(draft["name"], "meeting-recap")
        self.assertEqual(draft["description"], "Use for meeting notes and discussion recaps.")
        self.assertTrue(draft["instructions"].startswith("## Recap"))
        self.assertNotIn("allowed-tools", draft)
        self.assertEqual(skills.library(self.store, 1), [])
        oversized = await self.request("POST", "/api/skills/preview", json={"markdown": MARKDOWN + "a" * 50000})
        self.assertEqual(oversized.status, 400)
        self.assertEqual((await self.request("POST", "/api/skills/preview", json={"markdown": "# No metadata"})).status, 400)

    def test_github_links_are_normalized_and_fetch_only_from_github(self):
        source = "https://github.com/org/repo/blob/main/skills/meeting-recap/SKILL.md"
        raw = "https://raw.githubusercontent.com/org/repo/main/skills/meeting-recap/SKILL.md"
        self.assertEqual(skills.github_source(source + "?plain=1#L4"), (source, raw))
        self.assertEqual(skills.github_source("https://github.com/org/repo/tree/main/skills/meeting-recap")[1], raw)
        self.assertEqual(skills.github_source("https://github.com/org/repo")[1],
                         "https://api.github.com/repos/org/repo/contents/SKILL.md")
        for url in ("https://github.com@127.0.0.1/org/repo", "https://github.com.evil.org/org/repo",
                    "https://github.com/org/repo/tree/main/%2e%2e/private", "http://github.com/org/repo",
                    "https://raw.githubusercontent.com:443/org/repo/main/SKILL.md",
                    "https://github.com/org/repo/blob/main/script.py"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                skills.github_source(url)

    async def test_turn_loads_only_the_selected_projects_instructions(self):
        skill = skills.save(self.store, 1, DRAFT)
        skills.set_project(self.store, 1, self.work, [skill["id"]])
        projects.choose(self.store, 1, self.work)
        inbox = bot.Inbox(self.store)
        inbox.ingest([update(420, 1, "Подведи итоги встречи")])
        agent = Aleph(None, "synthetic")
        call = {"id": "load", "type": "function", "function": {
            "name": "load_skill", "arguments": json.dumps({"id": skill["id"]}),
        }}

        async def completion(messages, model, **kwargs):
            if messages[-1]["role"] == "user":
                self.assertIn(DRAFT["description"], messages[0]["content"])
                self.assertNotIn("RECAP-14", str(messages))
                return {"role": "assistant", "tool_calls": [call]}
            tool = json.loads(messages[-1]["content"])
            self.assertEqual(tool["instructions"], DRAFT["instructions"])
            return {"role": "assistant", "content": "RECAP-14. Итоги встречи."}

        agent.completion = AsyncMock(side_effect=completion)
        self.assertEqual(await agent.respond(self.store, 1, "Подведи итоги встречи", None, inbox, 420),
                         "RECAP-14. Итоги встречи.")
        agent.completion.assert_awaited()
        for uid, project_id in ((1, self.home), (1, 0), (2, 0)):
            with self.assertRaises(KeyError):
                await agent.use_tool(self.store, uid, "load_skill", {"id": skill["id"]}, "test", project_id=project_id)
        self.assertNotIn(DRAFT["name"], str(agent.turn_context(self.store, 1, "Привет", None, self.home)[0]))
        self.assertIn(DRAFT["name"], str(agent.turn_context(self.store, 1, "Итоги", None, self.work, scheduled=True)[0]))
        self.assertIn("load_skill", REPORT_TOOL_NAMES)
        self.assertNotIn("prepare_connection_action", REPORT_TOOL_NAMES)
