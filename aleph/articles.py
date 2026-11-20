"""Article formatting for Telegram's native Rich Messages."""
from html import escape
import re

from markdown_it import MarkdownIt

from .article_images import caption_html

MARKDOWN = MarkdownIt("commonmark", {"html": False, "maxNesting": 12}).enable("table").disable("image")
MARKDOWN.validateLink = lambda url: url.lower().startswith(("https://", "http://", "#"))
HEADING = re.compile(r"^#{1,4}\s+\S", re.MULTILINE)


def rich_content(reply):
    length = len(reply)
    if length < 650 or (length < 1550 and not HEADING.search(reply)):
        return None
    rendered = MARKDOWN.render(reply)
    if len(rendered) > 30000:
        return None
    return {"html": rendered}


def illustrated(rich, photos):
    def figure(photo):
        return (f'<figure><img src="{escape(photo["url"])}"/>'
                f'<figcaption>{escape(photo["title"])}<cite>{caption_html(photo)}</cite></figcaption></figure>\n')

    html = rich["html"]
    lead = re.match(r"(?:<h[1-6]>.*?</h[1-6]>\n)?(?:<p>.*?</p>\n)?", html, re.DOTALL).end()
    body = html[lead:]
    if len(photos) > 1:
        headings = list(re.finditer(r"^<h[1-6]>", body, re.MULTILINE))
        offset = headings[len(headings) // 2].start() if len(headings) > 1 else len(body)
        body = body[:offset] + figure(photos[1]) + body[offset:]
    return {"html": html[:lead] + figure(photos[0]) + body}


# Keep short Markdown and article fallbacks readable in Telegram's regular messages.
_BASIC_MARKUP = re.compile(r"\*\*|__|\x60|^#{1,6}\s|\]\(https?://", re.MULTILINE)


def basic_content(chunk):
    if not _BASIC_MARKUP.search(chunk):
        return None
    return _regular_html(MARKDOWN.render(chunk))


def _regular_html(markup):
    from html.parser import HTMLParser

    class TelegramText(HTMLParser):
        inline = {"strong": "b", "em": "i", "s": "s", "code": "code", "pre": "pre", "a": "a"}
        headings = {"h1", "h2", "h3", "h4", "h5", "h6"}
        paragraphs = {"p", "tr", "blockquote", "ul", "ol"}

        def __init__(self):
            super().__init__()
            self.parts = []

        def handle_starttag(self, tag, attrs):
            if tag == "a":
                href = dict(attrs).get("href", "")
                self.parts.append(f'<a href="{escape(href, quote=True)}">')
            elif tag in self.inline:
                self.parts.append(f"<{self.inline[tag]}>")
            elif tag in self.headings:
                self.parts.append("<b>")
            elif tag == "li":
                self.parts.append("• ")
            elif tag == "br":
                self.parts.append("\n")
            elif tag == "hr":
                self.parts.append("\n—\n")

        def handle_endtag(self, tag):
            if tag in self.inline:
                self.parts.append(f"</{self.inline[tag]}>")
            elif tag in self.headings:
                self.parts.append("</b>\n")
            elif tag in self.paragraphs:
                self.parts.append("\n")
            elif tag in ("td", "th"):
                self.parts.append(" · ")

        def handle_data(self, data):
            self.parts.append(escape(data, quote=False))

    parser = TelegramText()
    parser.feed(markup)
    parser.close()
    return "".join(parser.parts).strip()
