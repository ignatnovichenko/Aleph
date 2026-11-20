import json
import unittest
from unittest.mock import AsyncMock

from aleph import article_images, articles, bot
from aleph.llm import Aleph, REPORT_TOOL_NAMES
from aleph.store import Store
from test_bot import update


IMAGE = "https://thumb.wikimedia.org/wikipedia/commons/thumb/a/a2/Launch.jpg/960px-Launch.jpg"
SOURCE = "https://commons.wikimedia.org/wiki/File:Launch.jpg"
LICENSE = "https://creativecommons.org/licenses/by/4.0/"
ARTICLE = "# Apollo 11\n\nThe first crewed Moon landing.\n\n## Launch\n\n" + "The Saturn V carried the crew into orbit. " * 25


def commons_page(index, *, url=IMAGE, license_name="CC BY 4.0"):
    return {"index": index, "title": "File:Apollo 11 <Launch>.jpg", "imageinfo": [{
        "mime": "image/jpeg", "thumbwidth": 960, "thumbheight": 720,
        "thumburl": url + "?utm_source=commons.wikimedia.org", "descriptionurl": SOURCE,
        "extmetadata": {"Artist": {"value": '<a href="https://example.test/">A &amp; B</a>'},
                        "LicenseShortName": {"value": license_name}, "LicenseUrl": {"value": LICENSE}},
    }]}


class ArticleImageTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_drops_unlicensed_files_and_untrusted_hosts(self):
        fetch = AsyncMock(return_value={"query": {"pages": [
            commons_page(3), commons_page(1, url="https://thumb.wikimedia.org.evil.test/wikipedia/commons/a.jpg"),
            commons_page(2, license_name="CC BY-NC 4.0"),
        ]}})
        photo = await article_images.search(fetch, "Apollo 11 Saturn V launch")
        self.assertEqual(photo["url"], IMAGE)
        self.assertEqual(photo["artist"], "A & B")
        self.assertEqual(photo["source"], SOURCE)
        self.assertEqual(fetch.call_args.args[0], article_images.API_URL)
        self.assertEqual(fetch.call_args.args[1]["gsrsearch"], "Apollo 11 Saturn V launch filetype:bitmap")
        self.assertLessEqual(fetch.call_args.kwargs["timeout"], 4)
        self.assertIn(article_images.TOOL_NAME, REPORT_TOOL_NAMES)

    async def deliver(self, *, reply=ARTICLE, pages=None, rejected=(), searches=1, interrupted_photo=False):
        class TelegramStub:
            async def call(self, method, **payload):
                if method == "sendRichMessage":
                    has_photo = "<img" in payload["rich_message"]["html"]
                    if has_photo and interrupted_photo:
                        raise bot.TelegramError()
                    if has_photo in rejected:
                        raise bot.TelegramError(400)
                return {"message_id": 101}

        store = Store(":memory:")
        self.addCleanup(store.close)
        inbox = bot.Inbox(store)
        inbox.ingest([update(12, 1, "Расскажи, как летал Apollo 11")])
        tg = TelegramStub()
        tg.call = AsyncMock(wraps=tg.call)
        agent = Aleph(None, "test")
        agent._get_public_json = AsyncMock(return_value={"query": {"pages": [commons_page(1)] if pages is None else pages}})
        agent.completion = AsyncMock(side_effect=[
            {"role": "assistant", "tool_calls": [{"id": f"photo{i}", "type": "function", "function": {
                "name": article_images.TOOL_NAME, "arguments": json.dumps({"query": "Apollo 11 Saturn V launch"}),
            }} for i in range(searches)]},
            {"role": "assistant", "content": reply},
        ])
        await bot._handle(inbox.pending()[0], store, inbox, tg, agent)
        if interrupted_photo:
            store.db.execute("UPDATE bot_inbox SET available=0 WHERE id=12")
            await bot._handle(inbox.pending()[0], store, inbox, tg, agent)
            self.assertEqual(agent.completion.await_count, 2)
        self.assertFalse(inbox.pending())
        result = [(call.args[0], call.kwargs) for call in tg.call.call_args_list if call.args[0] != "sendChatAction"]
        return result, agent._get_public_json.await_count

    async def test_detailed_answer_embeds_credited_photo_after_intro_with_save_button(self):
        [(method, params)], queries = await self.deliver(searches=3)
        self.assertEqual(queries, 2)
        self.assertEqual(method, "sendRichMessage")
        html = params["rich_message"]["html"]
        self.assertEqual(html.count("<figure>"), 1)
        self.assertIn(f'<img src="{IMAGE}"/>', html)
        self.assertIn("A &amp; B", html)
        self.assertIn("Apollo 11 &lt;Launch&gt;", html)
        self.assertIn(SOURCE, html)
        self.assertIn(LICENSE, html)
        self.assertLess(html.index("first crewed Moon landing"), html.index("<figure>"))
        self.assertLess(html.index("</figure>"), html.index("<h2>"))
        self.assertTrue(params["reply_markup"]["inline_keyboard"][0][0]["callback_data"].startswith("save:"))

    async def test_rejected_photo_is_sent_as_a_reply_after_the_article(self):
        calls, _ = await self.deliver(rejected=(True,))
        self.assertEqual([method for method, _ in calls], ["sendRichMessage", "sendRichMessage", "sendPhoto"])
        self.assertNotIn("<img", calls[1][1]["rich_message"]["html"])
        self.assertEqual(calls[0][1]["reply_markup"], calls[1][1]["reply_markup"])
        self.assertEqual(calls[2][1]["reply_parameters"]["message_id"], 101)
        self.assertEqual(calls[2][1]["photo"], IMAGE)

    async def test_text_delivery_survives_native_article_rejection(self):
        calls, _ = await self.deliver(rejected=(True, False))
        self.assertEqual([method for method, _ in calls], ["sendRichMessage", "sendRichMessage", "sendMessage", "sendPhoto"])
        self.assertIn("The Saturn V", calls[2][1]["text"])

    async def test_transport_retry_sends_article_without_repeating_the_photo_attempt(self):
        calls, queries = await self.deliver(interrupted_photo=True)
        self.assertEqual(queries, 1)
        self.assertEqual([method for method, _ in calls], ["sendRichMessage", "sendRichMessage", "sendPhoto"])
        self.assertNotIn("<img", calls[1][1]["rich_message"]["html"])

    def test_two_photos_are_spaced_across_sections(self):
        rich = articles.rich_content(ARTICLE + "\n\n## Orbit\n\nThey reached an orbit around Earth.")
        photo = {"url": IMAGE, "source": SOURCE, "artist": "NASA", "license": "Public domain", "license_url": ""}
        html = articles.illustrated(rich, [{**photo, "title": "Launch photo"}, {**photo, "title": "Orbit photo"}])["html"]
        self.assertLess(html.index("Launch photo"), html.index("<h2>Launch"))
        self.assertLess(html.index("<h2>Launch"), html.index("Orbit photo"))

    async def test_short_reply_and_empty_results(self):
        calls, _ = await self.deliver(reply="Apollo 11 landed in July 1969.")
        self.assertEqual([method for method, _ in calls], ["sendMessage"])
        [(method, params)], _ = await self.deliver(pages=[])
        self.assertEqual(method, "sendRichMessage")
        self.assertNotIn("<figure>", params["rich_message"]["html"])
