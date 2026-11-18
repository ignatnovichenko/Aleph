"""RSS sources offered by Aleph and bounded, read-only feed retrieval."""
import asyncio
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

import aiohttp


CATALOG = {
    "https://hnrss.org/frontpage": "Hacker News",
    "https://www.nasa.gov/feed/": "NASA",
    "https://rss.arxiv.org/rss/cs.AI": "arXiv · AI",
    "https://feeds.bbci.co.uk/russian/rss.xml": "BBC News Русская служба",
    "https://habr.com/ru/rss/articles/?fl=ru": "Хабр",
}
BYTE_LIMIT = 2 * 1024 * 1024


def feed_catalog():
    return [{"url": url, "title": title} for url, title in CATALOG.items()]


class FeedTree(ET.TreeBuilder):
    def doctype(self, name, public_id, system_id):
        raise ValueError("DTDs are not accepted")


def parse_feed(data):
    # External entities, DTDs and entity expansion have no role in a news reader.
    if len(data) > BYTE_LIMIT:
        raise ValueError("Unsupported XML document")
    root = ET.fromstring(data, parser=ET.XMLParser(target=FeedTree()))
    items = []
    for entry in root.iter():
        if entry.tag.rsplit("}", 1)[-1] not in {"entry", "item"}:
            continue
        title, url = "", ""
        for child in entry:
            tag = child.tag.rsplit("}", 1)[-1]
            if tag == "title":
                title = " ".join("".join(child.itertext()).split())[:220]
            elif tag == "link" and child.get("rel", "alternate") == "alternate":
                url = (child.get("href") or child.text or "").strip()
        if title:
            try:
                if urlsplit(url).scheme != "https" or any(c.isspace() for c in url):
                    url = ""
            except ValueError:
                url = ""
            items.append({"title": title, "url": url[:1500]})
        if len(items) == 5:
            break
    return items


async def fetch_feed(session, url):
    if url not in CATALOG:
        raise ValueError("Выбери источник из каталога.")
    async with session.get(
        url, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=10),
        headers={"User-Agent": "Aleph-RSS/1.0", "Accept": "application/rss+xml, application/atom+xml, application/xml"},
    ) as response:
        if response.status != 200:
            raise ValueError(f"RSS source returned {response.status}")
        body = bytearray()
        async for chunk in response.content.iter_chunked(32768):
            body.extend(chunk)
            if len(body) > BYTE_LIMIT:
                raise ValueError("RSS response too large")
    return await asyncio.to_thread(parse_feed, bytes(body))
