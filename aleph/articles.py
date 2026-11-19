"""Article formatting for Telegram's native Rich Messages."""
import re

from markdown_it import MarkdownIt

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


# Keep short Markdown and article fallbacks readable in Telegram's regular messages.
_BASIC_MARKUP = re.compile(r"\*\*|__|\x60|^#{1,6}\s|\]\(https?://", re.MULTILINE)


def basic_content(chunk):
    if not _BASIC_MARKUP.search(chunk):
        return None
    return _regular_html(MARKDOWN.render(chunk))


def _regular_html(markup):
    from html import escape
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
