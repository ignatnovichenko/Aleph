import io
import json
import unittest
from unittest.mock import AsyncMock, patch

from docx import Document
from openpyxl import load_workbook
from pypdf import PdfReader

from aleph import bot, document_chat, documents, projects
from aleph.llm import Aleph
from aleph.store import Store
from test_bot import update
from test_documents import pdf


class OfficeOperations(unittest.IsolatedAsyncioTestCase):
    async def test_word_replacement_preserves_other_runs_and_table(self):
        doc = Document()
        paragraph = doc.add_paragraph()
        paragraph.add_run("Номер ").bold = True
        paragraph.add_run("договора ")
        paragraph.add_run("42").italic = True
        doc.add_table(rows=1, cols=1).cell(0, 0).text = "Приложение"
        out = io.BytesIO()
        doc.save(out)

        result, edited = await documents.run_office("edit", {
            "action": "replace_text", "filename": "Договор", "replacements": [{"find": "договора 42", "replace": "соглашения 73"}],
        }, {1: ("docx", out.getvalue())})
        changed = Document(io.BytesIO(edited))
        self.assertEqual(changed.paragraphs[0].text, "Номер соглашения 73")
        self.assertTrue(changed.paragraphs[0].runs[0].bold)
        self.assertEqual(changed.tables[0].cell(0, 0).text, "Приложение")
        self.assertEqual(result["filename"], "Договор.docx")
        self.assertFalse(result["layout_rebuilt"])

    async def test_spreadsheet_output_retains_data_types_and_uses_safe_explicit_formulas(self):
        result, book = await documents.run_office("create", {
            "format": "xlsx", "filename": "Бюджет", "sheets": [{"name": "Январь", "rows": [["Сумма"], [30], ["=literal"]]}],
        }, {})
        self.assertEqual(result["filename"], "Бюджет.xlsx")
        result, edited = await documents.run_office("edit", {
            "action": "set_cells", "filename": "Бюджет 2", "changes": [
                {"sheet": "Январь", "cell": "A2", "value": 60},
                {"sheet": "Январь", "cell": "B2", "formula": "=SUM(A2:A5)"},
            ],
        }, {1: ("xlsx", book)})
        loaded = load_workbook(io.BytesIO(edited))
        sheet = loaded["Январь"]
        self.assertEqual((sheet["A2"].value, sheet["B2"].value, sheet["A3"].data_type), (60, "=SUM(A2:A5)", "s"))
        self.assertEqual(result["cells_changed"], 2)
        with self.assertRaises(ValueError):
            await documents.run_office("edit", {
                "action": "set_cells", "filename": "invalid", "changes": [{"cell": "A1", "formula": "=WEBSERVICE(A2)"}],
            }, {1: ("xlsx", book)})

    async def test_pdf_can_reorder_and_rotate_pages_and_export_cyrillic(self):
        result, data = await documents.run_office("edit", {
            "action": "pdf_pages", "filename": "Merged", "pages": [{"file_id": 4, "rotate": 90}, {"file_id": 2}],
        }, {2: ("pdf", pdf("A")), 4: ("pdf", pdf("B"))})
        pages = list(PdfReader(io.BytesIO(data)).pages)
        self.assertEqual(result["pages"], 2)
        self.assertEqual((pages[0].extract_text(), pages[0].rotation, pages[1].extract_text()), ("B", 90, "A"))
        _, changed = await documents.run_office("edit", {"action": "replace_text", "filename": "Changed",
            "start_page": 2, "end_page": 2, "replacements": [{"find": "A", "replace": "Revised"}]}, {1: ("pdf", data)})
        updated_pages = list(PdfReader(io.BytesIO(changed)).pages)
        self.assertEqual((len(updated_pages), updated_pages[0].extract_text(), updated_pages[0].rotation), (2, "B", 90))
        self.assertIn("Revised", updated_pages[1].extract_text())
        _, data = await documents.run_office("create", {"filename": "Отчёт", "format": "pdf", "content": "# Отчёт\n\nВыручка за квартал."}, {})
        self.assertIn("Выручка за квартал.", PdfReader(io.BytesIO(data)).pages[0].extract_text())

    async def test_malformed_and_missing_exact_text_return_errors(self):
        with self.assertRaises(ValueError):
            await documents.run_office("inspect", {}, {1: ("docx", b"not-a-package")})
        with self.assertRaisesRegex(ValueError, "не найден"):
            await documents.run_office("edit", {
                "action": "replace_text", "filename": "edited", "replacements": [{"find": "absent", "replace": "new"}],
            }, {1: ("txt", b"unchanged")})


class DocumentChatTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.store.ensure_user(1)
        self.store.ensure_user(2)
        self.inbox = bot.Inbox(self.store)

    def test_album_metadata_and_caption_survive_a_retry(self):
        first = update(500, 1, "")
        second = update(501, 1, "")
        for index, event in enumerate((first, second), 1):
            event["message"].update(message_id=index, media_group_id="same-album",
                                    document={"file_id": f"tg-{index}", "file_name": f"part-{index}.pdf"})
        second["message"]["caption"] = "Объедини эти два PDF."
        self.inbox.ingest([first, second])
        self.store.db.execute("UPDATE bot_inbox SET available=0")
        row = self.inbox.pending()[0]
        source = json.loads(row["message"])
        scope = self.inbox.bind_project(row["id"], row["uid"])
        original, docs = document_chat.collect(self.store, 1, scope, row["id"], source)
        retried, retry_docs = document_chat.collect(self.store, 1, scope, row["id"], source)
        self.assertEqual((original["caption"], retried["caption"]), ("Объедини эти два PDF.",) * 2)
        self.assertEqual([item["filename"] for item in docs], ["part-1.pdf", "part-2.pdf"])
        self.assertEqual(docs, retry_docs)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM bot_inbox").fetchone()[0], 1)

    async def test_chat_edit_delivers_copy_and_outbound_attachment_can_be_opened(self):
        project = projects.create(self.store, 1, "Lab")["id"]
        projects.choose(self.store, 1, project)
        incoming = update(504, 1, "Замени январь на март.")
        incoming["message"].update(message_id=3, document={"file_id": "inbound", "file_name": "plan.txt"})
        self.inbox.ingest([incoming])
        agent = Aleph(None, "mock")

        def tool_call(name, arguments):
            return {"role": "assistant", "content": None, "tool_calls": [{"type": "function", "id": name,
                    "function": {"name": name, "arguments": json.dumps(arguments)}}]}

        class FakeTelegram:
            session, token = None, "test"

            async def call(self, method, **params):
                return {"message_id": 101}

            async def download(self, metadata, limit):
                return ("январь" if metadata["file_id"] == "inbound" else "март").encode()

        async def completion(messages, *args, **kwargs):
            request = messages[-1]
            if request["role"] == "user":
                [entry] = json.loads(request["content"].split("[Files for this request]\n")[-1])
                if entry["filename"] == "plan.txt":
                    return tool_call("edit_document", {"action": "replace_text", "filename": "updated",
                        "file_ids": [entry["id"]], "replacements": [{"find": "январь", "replace": "март"}]})
                return tool_call("inspect_document", {"file_id": entry["id"]})
            response = json.loads(request["content"])
            if "prepared_attachment" in response:
                self.assertEqual(response["prepared_attachment"], "updated.txt")
            else:
                self.assertEqual(response.get("text"), "март")
            return {"role": "assistant", "content": "Готово."}

        agent.completion = AsyncMock(side_effect=completion)
        result_document = {"chat": {"id": 1, "type": "private"}, "message_id": 7,
                           "document": {"file_id": "outbound", "file_name": "updated.txt"}}
        tg = FakeTelegram()
        with patch.object(documents, "send_document", AsyncMock(return_value=result_document)) as upload:
            await bot._handle(self.inbox.pending()[0], self.store, self.inbox, tg, agent)
        self.assertEqual(upload.await_args.args[5].decode(), "март")
        self.assertEqual([item["filename"] for item in document_chat.recent(self.store, 1, project)], ["updated.txt", "plan.txt"])

        followup = update(505, 1, "Прочитай исправленную копию.")
        followup["message"]["reply_to_message"] = result_document
        self.inbox.ingest([followup])
        await bot._handle(self.inbox.pending()[0], self.store, self.inbox, tg, agent)
        self.assertEqual(agent.completion.await_count, 4)
        self.assertFalse(self.inbox.pending())
        file_id = document_chat.recent(self.store, 1, project)[0]["id"]
        with self.assertRaisesRegex(ValueError, "недоступен"):
            await document_chat.execute(self.store, 2, 0, "inspect_document", {"file_id": file_id}, "tg:505:0:0", tg.download)
        with self.assertRaisesRegex(ValueError, "недоступен"):
            await document_chat.execute(self.store, 1, 0, "inspect_document", {"file_id": file_id}, "tg:505:0:0", tg.download)
