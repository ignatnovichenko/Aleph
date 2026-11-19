import io
import json
import socket
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import quote
import zipfile

from aiohttp.test_utils import TestClient, TestServer
import openpyxl

from aleph import bot, connections, project_sources, projects, public_fetch, source_import, source_text
from aleph.__main__ import application
from aleph.llm import Aleph
from aleph.store import Store
from test_backend import TEST_TOKEN, auth
from test_bot import update


def sheet_file():
    book = openpyxl.Workbook()
    book.active.title = "Метрики"
    book.active.append(["Квартал", "Продажи"])
    book.active.append(["Q3", 18741.25])
    book.create_sheet("Скидки").append(["Дилеры", 12.5])
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


class Sources(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        for uid in (10, 11):
            self.store.ensure_user(uid, language_code="ru")
        self.primary = projects.create(self.store, 10, "Пилот")["id"]
        self.other = projects.create(self.store, 10, "Соседний")["id"]
        self.client = TestClient(TestServer(application(self.store, TEST_TOKEN)))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    def path(self, scope=None):
        return f"/api/projects/{scope or self.primary}/sources"

    def request(self, method, path, uid=10, **kwargs):
        headers = kwargs.pop("headers", {})
        return self.client.request(method, path, headers={"Authorization": "tma " + auth(uid), **headers}, **kwargs)

    def file_request(self, filename, payload, path=None):
        return self.request("POST", path or self.path() + "/file", data=payload,
                            headers={"Content-Type": "application/octet-stream", "X-Source-Name": quote(filename)})

    async def test_large_file_and_book_values_remain_private_and_searchable(self):
        self.assertEqual((await self.client.get(self.path())).status, 401)
        upload = await self.file_request("Отчёт.xlsx", sheet_file())
        self.assertEqual(upload.status, 201)
        sheet = await upload.json()
        self.assertEqual(sheet["title"], "Отчёт.xlsx")
        self.assertEqual(sheet["status"], "ready")
        self.assertNotIn("content", sheet)
        large = await self.file_request("Данные.txt", ("Это тестовая строка.\n" * 1800).encode())
        self.assertEqual(large.status, 201)
        url = f"{self.path()}/{sheet['id']}"
        preview = await (await self.request("GET", url)).json()
        self.assertIn("Квартал\tПродажи\nQ3\t18741.25", preview["content"])
        self.assertIn("## Скидки", preview["content"])
        self.assertEqual((await self.request("GET", url, uid=11)).status, 404)
        self.assertEqual((await self.request("GET", f"{self.path(self.other)}/{sheet['id']}")).status, 404)
        self.assertEqual((await self.request("DELETE", url, uid=11)).status, 404)
        self.assertEqual(project_sources.search(self.store, 10, self.primary, "продажи")["matches"][0]["id"], sheet["id"])
        self.assertEqual((await self.request("DELETE", url)).status, 204)
        self.assertEqual(project_sources.search(self.store, 10, self.primary, "продажи")["matches"], [])

    async def test_video_link_deduplicates_and_retains_an_attached_transcript(self):
        link = "https://youtu.be/jNQXAC9IVRw?t=41"
        with patch.object(source_import, "load", AsyncMock(return_value=source_text.Contents("Тестовое видео", reason="captions"))) as loader:
            item = await (await self.request("POST", self.path(), json={"url": link})).json()
            self.assertEqual(item["status"], "link")
            again = await (await self.request("POST", self.path(), json={"url": item["url"]})).json()
            self.assertEqual(again["id"], item["id"])
            loader.assert_awaited_once()
            item_path = f"{self.path()}/{item['id']}"
            response = await self.file_request("transcript.vtt", b"WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nActual words", item_path + "/transcript")
            self.assertEqual(response.status, 201)
            self.assertEqual((await response.json())["status"], "ready")
            self.assertEqual((await self.request("POST", item_path)).status, 400)
            detail = await (await self.request("GET", item_path)).json()
            self.assertIn("Actual words", detail["content"])
            self.assertIn("youtube.com/watch", detail["url"])

    async def test_agent_reads_only_active_project_source_and_gets_actual_data(self):
        file = await project_sources.add_file(self.store, 10, self.primary, "Метрики.xlsx", sheet_file())
        projects.choose(self.store, 10, self.primary)
        inbox = bot.Inbox(self.store)
        inbox.ingest([update(501, 10, "Сколько продаж в Q3 по таблице?")])
        agent = Aleph(None, "unused")
        call = {"id": "read", "type": "function", "function": {
            "name": "read_project_source", "arguments": json.dumps({"id": file["id"]}),
        }}
        async def completion(messages, model, **kwargs):
            if messages[-1]["role"] != "tool":
                self.assertIn("Метрики.xlsx", str(messages))
                self.assertNotIn("18741.25", str(messages))
                return {"role": "assistant", "tool_calls": [call]}
            result = json.loads(messages[-1]["content"])
            self.assertIn("18741.25", result["excerpt"])
            return {"role": "assistant", "content": "Продажи в Q3 — 18 741,25. Источник — Метрики.xlsx."}
        agent.completion = AsyncMock(side_effect=completion)
        self.assertIn("18 741,25", await agent.respond(self.store, 10, "Сколько продаж в Q3 по таблице?", None, inbox, 501))
        for uid, scope in ((10, self.other), (11, self.primary), (10, 0)):
            with self.assertRaises(KeyError):
                await agent.use_tool(self.store, uid, "read_project_source", {"id": file["id"]}, "test", project_id=scope)

    async def test_external_reader_runs_only_on_request_and_stays_project_scoped(self):
        with patch.object(source_import, "load", AsyncMock(return_value=source_text.Contents("Статья", reason="access"))):
            with patch.object(source_import, "via_reader", AsyncMock(return_value=source_text.prepared("Рынок", "Спрос вырос на 5%."))) as reader:
                response = await self.request("POST", self.path(), json={"url": "https://example.org/article"})
                item = await response.json()
                reader.assert_not_awaited()
                endpoint = f"{self.path()}/{item['id']}/reader"
                self.assertEqual((await self.request("POST", endpoint, uid=11)).status, 404)
                reader.assert_not_awaited()
                result = await (await self.request("POST", endpoint)).json()
                self.assertEqual(result["status"], "ready")
                self.assertEqual(result["reader"], 1)
                await self.request("POST", f"{self.path()}/{item['id']}")
                self.assertEqual(reader.await_count, 2)

    async def test_provider_documents_use_account_grants_and_keep_table_data(self):
        async def private_call(store, session, uid, provider, method, path, **kwargs):
            self.assertEqual(uid, 10)
            if provider == "drive":
                if path.endswith("/export"):
                    self.assertTrue(kwargs["as_bytes"])
                    return sheet_file()
                return {"name": "Закрытая ведомость", "mimeType": source_import.SHEETS}
            if path.endswith("/markdown"):
                return {"markdown": "Решение: проверить поставщика.\n", "truncated": False}
            return {"properties": {"Name": {"type": "title", "title": [{"plain_text": "Решения команды"}]}}}
        with patch.object(connections, "statuses", return_value={"drive":"connected","notion":"connected"}), patch.object(
            connections, "call", AsyncMock(side_effect=private_call)
        ):
            notion = await project_sources.add_link(self.store,10,self.primary,"https://workspace.notion.site/Notes-0123456789abcdef0123456789abcdef")
            drive = await project_sources.add_link(self.store,10,self.primary,"https://docs.google.com/spreadsheets/d/partner_deals/edit")
        self.assertEqual(notion["title"], "Решения команды")
        self.assertIn("поставщика", project_sources.read(self.store,10,self.primary,notion["id"])["excerpt"])
        self.assertEqual(drive["title"], "Закрытая ведомость")
        self.assertIn("18741.25", project_sources.read(self.store,10,self.primary,drive["id"])["excerpt"])

    async def test_youtube_reads_public_caption_tracks_and_timestamps(self):
        player = {"videoDetails": {"title":"Video","shortDescription":"Description"}, "captions": {
            "playerCaptionsTracklistRenderer": {"captionTracks": [{"languageCode":"ru","baseUrl":"https://www.youtube.com/api/timedtext?v=jNQXAC9IVRw"}]}
        }}
        async def get(client, url, **kwargs):
            if "/api/timedtext" in url:
                packet = {"events": [{"tStartMs": 75000, "segs": [{"utf8":"Факт из видео"}]}]}
                return json.dumps(packet).encode(), "application/json", None
            return ("ytInitialPlayerResponse = " + json.dumps(player)).encode(), "text/html", None
        with patch.object(public_fetch, "get", AsyncMock(side_effect=get)):
            result = await source_import._youtube(None,source_import.address("https://youtu.be/jNQXAC9IVRw"),"ru")
        self.assertEqual(result.status, "ready")
        self.assertIn("[01:15] Факт из видео", result.text)

    async def test_public_fetch_rejects_private_addresses_and_redirects(self):
        for url in ("https://127.0.0.1/test", "http://169.254.169.254/latest", "https://[::1]/", "http://0177.0.0.1/",
                    "https://user:secret@example.org/test", "https://example.org:8443/", "file:///etc/passwd"):
            with self.subTest(url=url), self.assertRaises(public_fetch.PublicURL):
                public_fetch.public_url(url)
        import asyncio
        loop = asyncio.get_running_loop()
        local = [(socket.AF_INET,socket.SOCK_STREAM,6,"",("10.1.2.3",443))]
        with patch.object(loop, "getaddrinfo", AsyncMock(return_value=local)):
            with self.assertRaises(public_fetch.PublicURL):
                await public_fetch._pinned(public_fetch.public_url("https://example.org/"))
        class Redirect:
            status = 302
            headers = {"Location":"http://169.254.169.254/metadata"}
            async def __aenter__(self):
                return self
            async def __aexit__(self,*args):
                pass
        class Client:
            def get(self, url, **kwargs):
                self.url = str(url)
                self.host = kwargs["headers"]["Host"]
                return Redirect()
        client = Client()
        public = [(socket.AF_INET,socket.SOCK_STREAM,6,"",("93.184.215.14",443))]
        with patch.object(loop, "getaddrinfo", AsyncMock(return_value=public)) as dns:
            with self.assertRaises(public_fetch.PublicURL):
                await public_fetch.get(client, "https://example.org/", proxy=object())
            dns.assert_awaited_once()
        self.assertEqual(client.url, "https://93.184.215.14/")
        self.assertEqual(client.host, "example.org")

    async def test_office_and_article_extraction(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as doc:
            doc.writestr("word/document.xml", '<w:document xmlns:w="urn:word"><w:p><w:r><w:t>Условия поставки</w:t></w:r></w:p></w:document>')
        document = await source_text.read_file("Документ.docx", archive.getvalue())
        self.assertEqual(document.text, "Условия поставки")
        article = source_text.html_text(b"<title>Example</title><nav>Menu</nav><article><p>A useful <b>fact</b> from the page.</p></article>")
        self.assertEqual(article.title, "Example")
        self.assertEqual(article.text, "A useful fact from the page.")
