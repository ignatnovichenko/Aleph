import csv
import io
import unittest
from unittest.mock import AsyncMock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from aleph.documents import DocumentDeliveryError, MediaError, read_document, render_document, send_document


def pdf(text="", encrypted=False):
    writer = PdfWriter()
    page = writer.add_blank_page(300, 300)
    if text:
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                                 NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 5 100 Td ({text}) Tj ET".encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    if encrypted:
        writer.encrypt("test")
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


class DocumentTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_and_pdf(self):
        for name, raw in (("hello.txt", "Welcome".encode("utf-16")), ("hello.pdf", pdf("Welcome"))):
            download = AsyncMock(return_value=raw)
            self.assertEqual(await read_document({"file_name": name}, download), "Welcome")
        for raw, message in ((pdf(encrypted=True), "паролем"), (pdf(), "изображения"), (b"not a PDF", "распознать")):
            with self.assertRaisesRegex(MediaError, message):
                await read_document({"file_name": "a.pdf"}, AsyncMock(return_value=raw))

    async def test_limits_before_and_after_download(self):
        download = AsyncMock(return_value=b"x" * 35000)
        with self.assertRaises(MediaError):
            await read_document({"file_name": "big.pdf", "file_size": 5*1024**2}, download)
        download.assert_not_awaited()
        with self.assertRaisesRegex(MediaError, "символов"):
            await read_document({"file_name": "big.txt"}, download)

    def test_csv_quoting_and_formula_handling(self):
        name, mime, content = render_document({"filename":"Моя таблица.csv", "format":"csv",
            "rows":[["Идея","Цена"],["Текст с запятой, и переносом\nстроки","-12"],["=SUM(A2)","@text"]]})
        self.assertEqual((name, mime), ("Моя таблица.csv", "text/csv"))
        self.assertEqual(list(csv.reader(io.StringIO(content.decode("utf-8-sig"))))[1:],
                         [["Текст с запятой, и переносом\nстроки","-12"], ["'=SUM(A2)","'@text"]])

    def test_calendar_timezone_folding_and_escape(self):
        name, mime, content = render_document({"filename":"../план", "format":"ics", "events":[
            {"title":"Длинное описание,\n" * 6, "start":"2027-01-10T09:00:00+03:00", "end":"2027-01-10T10:30:00+03:00"},
            {"title":"День рождения", "start":"2027-01-12", "end":"2027-01-13"}]})
        self.assertEqual((name, mime), ("план.ics", "text/calendar"))
        self.assertIn(b"DTSTART:20270110T060000Z", content)
        self.assertIn(b"DTEND:20270110T073000Z", content)
        self.assertIn(b"DTSTART;VALUE=DATE:20270112", content)
        self.assertTrue(all(len(line)<=75 for line in content.split(b"\r\n")))
        self.assertIn(b"\\,\\n", content.replace(b"\r\n ",b""))
        with self.assertRaises(ValueError):
            render_document({"filename":"test", "format":"ics", "events":[{"title":"x","start":"2027-01-12T01:00:00"}]})

    async def test_telegram_upload_multipart(self):
        async def handler(request):
            if request.path.endswith("/error"):
                return web.json_response({"ok":False,"parameters":{"retry_after":5}},status=429)
            form=await request.multipart()
            chat, doc = await form.next(), None
            self.assertEqual(await chat.text(), "44")
            doc=await form.next()
            self.assertEqual(doc.filename, "Заметка.md")
            self.assertEqual(await doc.read(), b"# Aleph")
            return web.json_response({"ok":True,"result":{"message_id":7}})
        app=web.Application()
        app.router.add_post("/{target}", handler)
        client=TestClient(TestServer(app))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        class LocalUpload:
            route="/sendDocument"
            def post(self, url, **kwargs):
                assert url=="https://api.telegram.org/botfake/sendDocument"
                assert not kwargs["allow_redirects"]
                return client.post(self.route, **kwargs)
        session=LocalUpload()
        artifact=render_document({"filename":"Заметка","format":"md","content":"# Aleph"})
        self.assertEqual(await send_document(session,"fake",44,*artifact), 7)
        session.route="/error"
        with self.assertRaises(DocumentDeliveryError) as ctx:
            await send_document(session,"fake",44,*artifact)
        self.assertEqual((ctx.exception.status,ctx.exception.retry_after),(429,5))
