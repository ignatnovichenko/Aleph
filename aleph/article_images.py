"""Find reusable photos on Wikimedia Commons for Telegram articles."""
from html import escape
import re
from urllib.parse import urlsplit, urlunsplit

import aiohttp
from bs4 import BeautifulSoup


TOOL_NAME = "find_article_photo"
MAX_PHOTOS = 2
API_URL = "https://commons.wikimedia.org/w/api.php"
USER_AGENT = "AlephAgent/1.0 (https://github.com/ignatnovichenko/Aleph)"
_IMAGE_HOSTS = {"upload.wikimedia.org", "thumb.wikimedia.org"}
_PHOTO_TYPES = {"image/jpeg", "image/png"}
_LICENSE = re.compile(r"(?:CC BY(?:-SA)? [1-4]\.0|CC0(?: 1\.0)?|Public domain|PDM)", re.IGNORECASE)


def _text(value):
    return " ".join(BeautifulSoup(value or "", "html.parser").get_text(" ", strip=True).split())


def _public_url(url, hosts, path_prefix):
    if not isinstance(url, str) or len(url) > 1500 or any(c.isspace() for c in url):
        return ""
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc not in hosts or not parts.path.startswith(path_prefix):
        return ""
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _candidate(page):
    info = (page.get("imageinfo") or [{}])[0]
    if info.get("mime") not in _PHOTO_TYPES:
        return None
    width = info.get("thumbwidth") or info.get("width") or 0
    height = info.get("thumbheight") or info.get("height") or 0
    if min(width, height) < 300 or max(width, height) > 3 * min(width, height):
        return None
    url = _public_url(info.get("thumburl") or info.get("url"), _IMAGE_HOSTS, "/wikipedia/commons/")
    source = _public_url(info.get("descriptionurl"), {"commons.wikimedia.org"}, "/wiki/File:")
    title = page.get("title", "").removeprefix("File:").rsplit(".", 1)[0]
    meta = info.get("extmetadata") or {}

    def value(key):
        return _text(meta.get(key, {}).get("value", ""))

    license_name = value("LicenseShortName")
    license_url = _public_url(meta.get("LicenseUrl", {}).get("value"), {"creativecommons.org"}, "/")
    artist = value("Attribution") or value("Artist")
    if not url or not source or not _LICENSE.fullmatch(license_name):
        return None
    if license_name.upper().startswith("CC BY") and (not artist or not license_url):
        return None
    return {"url": url, "source": source, "title": title[:180], "artist": artist[:180],
            "license": license_name, "license_url": license_url}


async def search(fetch_json, query):
    terms = re.findall(r"[^\W_]+", query, flags=re.UNICODE)
    if not terms or len(terms) > 10 or len(query) > 100:
        return {"found": False}
    params = {
        "action": "query", "format": "json", "formatversion": 2,
        "generator": "search", "gsrnamespace": 6, "gsrlimit": 8,
        "gsrsearch": " ".join(terms) + " filetype:bitmap",
        "prop": "imageinfo", "iiprop": "url|mime|size|extmetadata", "iiurlwidth": 960,
        "iiextmetadatalanguage": "en",
        "iiextmetadatafilter": "Attribution|Artist|LicenseShortName|LicenseUrl",
    }
    try:
        data = await fetch_json(API_URL, params, timeout=4, user_agent=USER_AGENT)
    except (aiohttp.ClientError, TimeoutError):
        return {"found": False}
    pages = data.get("query", {}).get("pages", [])
    for page in sorted(pages, key=lambda entry: entry.get("index", 99)):
        photo = _candidate(page)
        if photo:
            return {"found": True, **photo}
    return {"found": False}


def caption_html(photo):
    license_label = escape(photo["license"])
    if photo["license_url"]:
        license_label = f'<a href="{escape(photo["license_url"])}">{license_label}</a>'
    credit = [escape(photo["artist"])] if photo["artist"] else []
    credit.extend([f'<a href="{escape(photo["source"])}">Wikimedia Commons</a>', license_label])
    return " · ".join(credit)
