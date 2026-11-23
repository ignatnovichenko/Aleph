import asyncio
import importlib.util
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from test_backend import TEST_TOKEN
from test_bot import update
from test_scheduled_reports import Telegram

from aleph import bot, chrome_local, chrome_runner, computers
from aleph.__main__ import application
from aleph.computer import Runner
from aleph.store import Store


@unittest.skipUnless(
    chrome_local.chrome_binary() and importlib.util.find_spec("playwright"), "Chrome and Playwright required"
)
class BrowserFlows(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / ".config/aleph/browser"
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.store.ensure_user(1)
        self.inbox = bot.Inbox(self.store)
        self.tg = self.store.chrome.tg = Telegram()
        self.client = TestClient(TestServer(application(self.store, TEST_TOKEN)))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        pair = computers.pair(self.store, computers.prepare(self.store, 1, "Тестовый компьютер")["code"])
        self.runner = Runner(self.client.session, str(self.client.make_url("/")).rstrip("/"), pair["token"])
        self.seen, self.actions = [], []
        self.barrier, self.release = asyncio.Event(), asyncio.Event()
        self.hold = False
        self.session_seen = False
        self.awaiting_click = asyncio.Event()

        async def visit(request):
            self.seen.append((request.method, request.path, request.query.get("city")))
            if request.path == "/login":
                response = web.Response(text="Signed in")
                response.set_cookie("fixture_login", "local-only-fixture-value", max_age=3600, httponly=True)
                return response
            if request.path == "/account":
                self.session_seen = request.cookies.get("fixture_login") == "local-only-fixture-value"
                return web.Response(
                    text='<input type="password" value="dummy-password">Account', content_type="text/html"
                )
            if request.path == "/unlock":
                await self.release.wait()
                return web.Response(text="ok")
            pages = {
                "/change": """<form method="post" action="/sent"><input name="recipient" value="Аня">
                   <button>Отправить</button></form><script>fetch('/unlock').then(()=>{
                   document.querySelector('input').value='Иван';fetch('/changed')})</script>""",
                "/slow": """<button disabled onclick="location.href='/clicked'">Далее</button>
                      <script>fetch('/unlock').then(()=>document.querySelector('button').disabled=false)</script>""",
                "/start": '<a href="/form" target="_blank">Открыть форму</a>',
                "/form": '<form action="/result"><label>Город<input name="city"></label><button>Далее</button></form>',
                "/result": '<a href="/file" download>Скачать</a>',
                "/mail": '<form method="post" action="/mail/sent"><button>Отправить</button></form>',
            }
            if request.path == "/file":
                return web.Response(
                    body=b"verified download\n",
                    headers={"Content-Disposition": "attachment; filename=report.txt"},
                )
            return web.Response(text=pages.get(request.path, "Готово"), content_type="text/html")

        site = web.Application()
        site.router.add_route("*", "/{path:.*}", visit)
        fixture = TestServer(site)
        await fixture.start_server()
        self.addAsyncCleanup(fixture.close)
        self.url = str(fixture.make_url("/start"))
        parts = urlsplit(self.url)
        self.site_origin = parts.scheme, parts.netloc
        self.store.chrome.plan = self.script

    async def script(self, job, view):
        self.assertNotIn("fixture-value", str(view))
        self.assertNotIn("dummy-password", str(view))
        controls = {c["label"]: c["id"] for c in view["controls"]}
        path = urlsplit(view["tabs"][view["tab"]]["page"]).path
        if not job["history"]:
            action = {"op": "go", "url": self.url}
        elif path == "/start" and len(view["tabs"]) == 1:
            action = {"op": "click", "id": controls["Открыть форму"]}
        elif path == "/start":
            action = {"op": "switch", "tab": 1}
        elif path == "/form" and "fill" not in self.actions:
            action = {"op": "fill", "id": controls["Город"], "value": "Казань"}
        elif path == "/form":
            self.barrier.set()
            if self.hold:
                await self.release.wait()
            action = {"op": "click", "id": controls["Далее"]}
        elif path == "/result" and not view["downloads"]:
            action = {"op": "download", "id": controls["Скачать"]}
        elif path == "/slow":
            self.awaiting_click.set()
            action = {"op": "click", "id": controls["Далее"]}
        elif path in ("/mail", "/change"):
            action = {"op": "click", "id": controls["Отправить"]}
        else:
            action = {"op": "done", "text": "Готово."}
        self.actions.append(action["op"])
        return action

    async def launch(self, extra=""):
        await self.runner.request("/browser/claim", {"epoch": 0})
        self.inbox.ingest([update(10, 1, "В Chrome открой " + self.url + " и скачай файл" + extra)])
        await bot._handle(self.inbox.pending()[0], self.store, self.inbox, self.tg, None)
        self.assertEqual(self.tg.sent[-1][1]["reply_markup"]["inline_keyboard"][0][0]["text"], "Стоп")
        job = await self.runner.request("/browser/claim", {"epoch": 0})
        return asyncio.create_task(
            chrome_runner.work(
                self.runner, self.root, 0, job, pairing="fixture", headless=True, test_origin=self.site_origin
            )
        )

    async def tap(self, data, uid=1, update_id=20):
        await bot._widget_updates(
            self.store,
            self.tg,
            [
                {
                    "update_id": update_id,
                    "callback_query": {
                        "from": {"id": uid},
                        "id": "test",
                        "data": data,
                        "message": {"chat": {"id": 1, "type": "private"}},
                    },
                }
            ],
        )

    async def test_pages_new_tab_form_download(self):
        await asyncio.wait_for(await self.launch(), 25)
        self.assertIn(("GET", "/result", "Казань"), self.seen)
        self.assertIn("switch", self.actions)
        self.assertEqual(next((self.root / "downloads").iterdir()).read_bytes(), b"verified download\n")
        self.assertIn("Скачано", self.tg.sent[-1][1]["text"])

    async def stopped_flow(self, local):
        self.hold = True
        task = await self.launch()
        try:
            await asyncio.wait_for(self.barrier.wait(), 15)
            requests = list(self.seen)
            if local:
                cmd = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "aleph.chrome_local",
                    "browser-stop",
                    env={**os.environ, "HOME": self.tmp.name},
                    stdout=asyncio.subprocess.DEVNULL,
                )
                self.assertEqual(await cmd.wait(), 0)
            else:
                await self.tap("chrome:stop")
            await asyncio.wait_for(task, 8)
            self.release.set()
            await asyncio.sleep(0.2)
            self.assertEqual(requests, self.seen)
            self.assertNotIn("download", self.actions)
            self.assertFalse((self.root / "downloads").exists())
            if local:
                self.assertEqual(chrome_local.state(self.root), (1, 1))
                chrome_local.state(self.root, False)
            else:
                self.assertIn("заблокирован", self.store.chrome.stage(1, 21, 0, "задание"))
                self.store.chrome.pause(1, 22, False)
                self.assertIn("заблокирован", self.store.chrome.stage(1, 19, 0, "старое задание"))
            self.assertFalse((await self.runner.request("/browser/claim", {"epoch": 2})).get("turn"))
        finally:
            self.release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_telegram_stop(self):
        await self.stopped_flow(False)

    async def test_offline_command(self):
        await self.stopped_flow(True)

    async def test_commit_requires_owner_confirmation(self):
        self.url = self.url.replace("/start", "/mail")
        task = await self.launch()
        try:
            async with asyncio.timeout(12):
                while len(self.tg.sent) < 2:
                    await asyncio.sleep(0.05)
            token = self.tg.sent[-1][1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
            await self.tap(token, uid=2)
            await asyncio.sleep(0.2)
            self.assertFalse(any(r[0] == "POST" for r in self.seen))
            await self.tap(token)
            await asyncio.wait_for(task, 10)
            self.assertEqual(sum(r[0] == "POST" for r in self.seen), 1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_stop_during_inflight_click(self):
        self.url = self.url.replace("/start", "/slow")
        task = await self.launch()
        try:
            await asyncio.wait_for(self.awaiting_click.wait(), 10)
            await asyncio.sleep(0.15)
            await self.tap("chrome:stop")
            await asyncio.wait_for(task, 5)
            self.release.set()
            await asyncio.sleep(0.2)
            self.assertFalse(any(path == "/clicked" for _, path, _ in self.seen))
            self.assertTrue(any("подтвердил остановку" in msg["text"] for _, msg in self.tg.sent))
        finally:
            self.release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_exact_grant_is_honored(self):
        self.url = self.url.replace("/start", "/mail")
        task = await self.launch("\nРазрешаю: Нажать «Отправить» на " + self.url + " → " + self.url + "/sent")
        try:
            await asyncio.wait_for(task, 10)
            self.assertEqual(sum(method == "POST" for method, _, _ in self.seen), 1)
            self.assertFalse(any("Разрешить действие" in msg["text"] for _, msg in self.tg.sent))
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_manual_profile_survives_a_fresh_automation_browser(self):
        from playwright.async_api import async_playwright

        async with async_playwright() as driver:
            profile = chrome_local.profile(self.root, "fixture")
            manual = await driver.chromium.launch_persistent_context(
                str(profile), executable_path=chrome_local.chrome_binary(), headless=True
            )
            await manual.pages[0].goto(self.url.replace("/start", "/login"))
            await manual.close()
        self.url = self.url.replace("/start", "/account")
        await asyncio.wait_for(await self.launch(), 12)
        self.assertTrue(self.session_seen)

    async def test_offline_during_a_turn_is_fail_closed(self):
        self.hold = True
        task = await self.launch()
        try:
            await asyncio.wait_for(self.barrier.wait(), 12)
            requests = list(self.seen)
            await self.client.session.close()
            await asyncio.wait_for(task, 5)
            self.release.set()
            await asyncio.sleep(0.2)
            self.assertEqual(requests, self.seen)
        finally:
            self.release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_changed_recipient_invalidates_confirmation(self):
        self.url = self.url.replace("/start", "/change")
        task = await self.launch()
        try:
            async with asyncio.timeout(10):
                while len(self.tg.sent) < 2:
                    await asyncio.sleep(0.04)
            prompt = self.tg.sent[-1][1]
            self.assertIn("Аня", prompt["text"])
            self.release.set()
            async with asyncio.timeout(5):
                while not any(path == "/changed" for _, path, _ in self.seen):
                    await asyncio.sleep(0.04)
            await self.tap(prompt["reply_markup"]["inline_keyboard"][0][0]["callback_data"])
            await asyncio.wait_for(task, 5)
            self.assertFalse(any(method == "POST" for method, _, _ in self.seen))
            self.assertIn("изменилась", self.tg.sent[-1][1]["text"])
        finally:
            self.release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
