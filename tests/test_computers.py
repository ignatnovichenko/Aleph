import asyncio
from datetime import datetime, timedelta
import json
import sys
import unittest
from unittest.mock import AsyncMock, patch

import aiohttp
from aiohttp.test_utils import TestClient, TestServer

from aleph import bot, computers, projects, reports
from aleph.__main__ import application
from aleph.computer import Revoked, Runner
from aleph.hermes import HermesAleph
from aleph.store import Store, UTC, timestamp
from test_backend import TEST_TOKEN, auth
from test_bot import update
from test_scheduled_reports import Telegram


class ProjectComputers(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.store.ensure_user(1)
        self.store.ensure_user(2)
        self.project = projects.create(self.store, 1, "Research")["id"]
        projects.choose(self.store, 1, self.project)
        self.inbox = bot.Inbox(self.store)
        self.client = TestClient(TestServer(application(self.store, TEST_TOKEN)))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        self.url = str(self.client.make_url("/")).rstrip("/")

    @staticmethod
    def scoped(uid=1):
        return {"Authorization": "tma " + auth(uid)}

    async def pair(self, uid=1, name="Home Mac"):
        request = await self.client.post("/api/computers/pair", json={"name": name}, headers=self.scoped(uid))
        self.assertEqual(request.status, 200)
        packet = await request.json()
        data = await Runner(self.client.session, self.url, None).request("/pair", {"code": packet["code"]})
        return data, packet["code"], Runner(self.client.session, self.url, data["token"])

    async def test_pairing_is_one_use_and_assignment_and_revocation_are_scoped(self):
        device, code, runner = await self.pair()
        hidden = await (await self.client.get("/api/computers", headers=self.scoped(2))).json()
        self.assertEqual(hidden, [])
        visible = await (await self.client.get("/api/computers", headers=self.scoped())).json()
        self.assertEqual(visible, [{"id": device["id"], "name": "Home Mac", "online": False}])
        with self.assertRaises(Revoked):
            await Runner(self.client.session, self.url, None).request("/pair", {"code": code})
        foreign, _, _ = await self.pair(uid=2)
        endpoint = f"/api/projects/{self.project}/computer"
        self.assertEqual((await self.client.patch(endpoint, headers=self.scoped(), json={"computer_id": foreign["id"]})).status, 404)
        assigned = await self.client.patch(endpoint, headers=self.scoped(), json={"computer_id": device["id"]})
        self.assertEqual((await assigned.json())["computer_name"], "Home Mac")
        delete = f"/api/computers/{device['id']}"
        self.assertEqual((await self.client.delete(delete, headers=self.scoped(2))).status, 404)
        self.assertEqual((await self.client.delete(delete, headers=self.scoped())).status, 204)
        self.assertIsNone(projects.get(self.store, 1, self.project)["computer_id"])
        with self.assertRaises(Revoked):
            await runner.request("/claim")

    async def test_project_turn_reaches_only_assigned_runner_and_returns_through_normal_handler(self):
        device, _, runner = await self.pair()
        _, _, other = await self.pair(uid=2)
        computers.assign(self.store, 1, self.project, device["id"])
        self.inbox.ingest([update(700, 1, "Проверь выполнение")])
        agent = HermesAleph(self.client.session, "synthetic", sys.executable)
        tg = Telegram()
        completed = {"role": "assistant", "content": "Запрос выполнен на подключённом компьютере."}
        call = {"id": "remember", "type": "function", "function": {
            "name": "remember_fact", "arguments": json.dumps({"text": "Предпочитаю текстовые отчёты."}),
        }}
        agent.complete_turn = AsyncMock(side_effect=[{"role": "assistant", "content": None, "tool_calls": [call]}, completed])
        job_poll = asyncio.create_task(runner.request("/claim"))
        async with asyncio.timeout(3):
            while not self.store.computer_hub.online(device["id"]):
                await asyncio.sleep(.01)
            handle = asyncio.create_task(bot._handle(self.inbox.pending()[0], self.store, self.inbox, tg, agent))
            try:
                job = await job_poll
                self.assertIn("Проверь выполнение", job["turn"]["messages"][-1]["content"])
                self.assertNotIn("key", job["turn"])
                self.assertNotIn("endpoint", job["turn"])
                with self.assertRaises(aiohttp.ClientResponseError) as foreign:
                    await other.request(f"/{job['id']}/alive")
                self.assertEqual(foreign.exception.status, 404)
                self.assertEqual((await runner.request("/claim"))["id"], job["id"], "lost poll response is recoverable")

                async def fake_worker(python, turn):
                    headers = {"Authorization": "Bearer " + turn["key"]}
                    payload = {"model": turn["model"], "messages": turn["messages"], "tools": turn["tools"]}
                    url = turn["endpoint"] + "/v1/chat/completions"
                    first = await (await self.client.session.post(url, json=payload, headers=headers)).json()
                    tool_call = first["choices"][0]["message"]["tool_calls"][0]
                    response = await self.client.session.post(turn["endpoint"] + "/tool", json={
                        "name": tool_call["function"]["name"], "arguments": json.loads(tool_call["function"]["arguments"]),
                        "id": tool_call["id"],
                    }, headers=headers)
                    self.assertEqual(response.status, 200)
                    second = await (await self.client.session.post(url, json=payload, headers=headers)).json()
                    return {"answer": second["choices"][0]["message"]["content"]}

                with patch("aleph.computer.run_worker", side_effect=fake_worker):
                    await runner.execute(sys.executable, job)
                await handle
            finally:
                handle.cancel()
                await asyncio.gather(handle, return_exceptions=True)
        self.assertEqual(tg.sent[-1][1]["text"], completed["content"])
        self.assertEqual(len(projects.facts(self.store, 1, self.project)), 1)
        self.assertFalse(self.inbox.pending())

    async def test_chat_notifies_when_offline_and_scheduled_work_stays_pending(self):
        device, _, runner = await self.pair()
        computers.assign(self.store, 1, self.project, device["id"])
        agent = HermesAleph(self.client.session, "synthetic", sys.executable)
        agent.run_hermes = AsyncMock()
        tg = Telegram()
        self.inbox.ingest([update(701, 1, "Объясни это")])
        await bot._handle(self.inbox.pending()[0], self.store, self.inbox, tg, agent)
        self.assertIn("«Home Mac» сейчас не на связи", tg.sent[-1][1]["text"])
        run_at = datetime.now(UTC) + timedelta(minutes=1)
        report = reports.schedule(self.store, 1, self.project, "Review", "Summarize the project", timestamp(run_at), "test")
        reports.enqueue(self.store.db, run_at + timedelta(seconds=2))
        await bot._handle(self.inbox.pending(scheduled=True)[0], self.store, self.inbox, tg, agent)
        current = self.store.get_task(1, report["id"])
        self.assertEqual(current["report_state"], "pending")
        self.assertIsNone(current["report_started_at"])
        self.assertFalse(self.inbox.pending(scheduled=True))
        agent.run_hermes.assert_not_awaited()

        self.store.db.execute("UPDATE bot_inbox SET available=0 WHERE id=?", (-report["id"],))
        poll = asyncio.create_task(runner.request("/claim"))
        async with asyncio.timeout(3):
            while not self.store.computer_hub.online(device["id"]):
                await asyncio.sleep(.01)
            handle = asyncio.create_task(bot._handle(self.inbox.pending(scheduled=True)[0], self.store, self.inbox, tg, agent))
            try:
                job = await poll
                tools = {tool["function"]["name"] for tool in job["turn"]["tools"]}
                self.assertNotIn("create_task", tools)
                with patch("aleph.computer.run_worker", AsyncMock(return_value={"answer": "Review complete."})):
                    await runner.execute(sys.executable, job)
                await handle
            finally:
                handle.cancel()
                await asyncio.gather(handle, return_exceptions=True)
        self.assertEqual(self.store.get_task(1, report["id"])["report_state"], "sent")
        self.assertEqual(tg.sent[-1][1]["text"], "Review complete.")
