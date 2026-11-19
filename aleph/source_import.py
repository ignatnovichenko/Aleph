"""Import public articles and provider documents as reference text."""
import asyncio
from dataclasses import dataclass
import json
from pathlib import PurePosixPath
import re
from urllib.parse import urlencode

import aiohttp

from . import connections, public_fetch, source_text

YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "youtube-nocookie.com", "www.youtube-nocookie.com"}
DRIVE_HOSTS = {"docs.google.com", "drive.google.com"}
SHEETS = "application/vnd.google-apps.spreadsheet"
DOCS = "application/vnd.google-apps.document"
SLIDES = "application/vnd.google-apps.presentation"
EXPORTS = {SHEETS: (".xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
           DOCS: (".txt", "text/plain"),
           SLIDES: (".pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation")}
EXTENSIONS = {mime: ext for ext, mime in EXPORTS.values()} | {
    "application/pdf": ".pdf", "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "text/csv": ".csv", "text/tab-separated-values": ".tsv", "text/markdown": ".md", "application/json": ".json",
}


@dataclass(frozen=True)
class Address:
    url: str
    kind: str
    key: str
    item: str = ""
    subtype: str = ""
    resource: str = ""

    @property
    def title(self):
        if self.kind == "youtube":
            return f"YouTube · {self.item}"
        if self.kind == "notion":
            return "Страница Notion"
        if self.kind == "drive":
            return {"document": "Google Docs", "spreadsheets": "Google Sheets",
                    "presentation": "Google Slides"}.get(self.subtype, "Файл Google Drive")
        url = public_fetch.public_url(self.url)
        leaf = url.path.rstrip("/").rsplit("/", 1)[-1].replace("_", " ")
        return f"{url.host} · {leaf[:100]}" if leaf else url.host


def address(value):
    if not isinstance(value, str):
        raise ValueError("Вставь ссылку на источник.")
    url = public_fetch.public_url(value.strip())
    host = url.host.lower().rstrip(".")
    if host in YT_HOSTS:
        match = re.fullmatch(r"/(?:shorts|live|embed)/([\w-]{11})/?", url.path, re.ASCII)
        item = url.path.strip("/") if host == "youtu.be" else match[1] if match else url.query.get("v", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", item):
            raise ValueError("Вставь ссылку на конкретное видео YouTube.")
        return Address(f"https://www.youtube.com/watch?v={item}", "youtube", "youtube:" + item, item)
    if host in DRIVE_HOSTS:
        match = re.search(r"/d/([A-Za-z0-9_-]{1,150})(?:/|$)", url.path)
        item = match[1] if match else url.query.get("id", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,150}", item):
            raise ValueError("Вставь ссылку на конкретный файл или таблицу Google Drive.")
        subtype = url.path.split("/")[1] if host == "docs.google.com" else ""
        resource = url.query.get("resourcekey", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{0,150}", resource):
            raise ValueError("Проверь ссылку доступа Google Drive.")
        return Address(str(url), "drive", "drive:" + item, item, subtype, resource)
    if host == "notion.so" or host.endswith((".notion.so", ".notion.site")) or host == "notion.site":
        match = re.search(r"(?:([0-9a-f]{32})|([0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}))/?$", url.path, re.I)
        item = match[0].strip("/").replace("-", "").lower() if match else ""
        return Address(str(url), "notion", "notion:" + (item or str(url)), item)
    return Address(str(url), "web", str(url))


def _json(data):
    try:
        return json.loads(data)
    except (ValueError, RecursionError):
        return {}


def _player(raw):
    page = raw.decode("utf-8", "replace")
    match = re.search(r"\bytInitialPlayerResponse\s*=\s*(?=\{)", page)
    if match:
        try:
            return json.JSONDecoder().raw_decode(page[match.end():])[0]
        except (ValueError, RecursionError):
            pass
    return {}


def _caption_lines(data):
    packet = _json(data)
    if not isinstance(packet, dict):
        packet = {}
    result, prior = [], ""
    for event in packet.get("events", []) or []:
        line = " ".join("".join(seg.get("utf8", "") for seg in event.get("segs", [])).split())
        if line and line != prior:
            seconds = max(0, int(event.get("tStartMs", 0)) // 1000)
            result.append(f"[{seconds // 60:02d}:{seconds % 60:02d}] {line}")
            prior = line
    return "\n".join(result)


async def _youtube(client, source, language, proxy=None):
    encoded = urlencode({"url": source.url, "format": "json"})
    embed_host = "https://www.youtube-nocookie.com"
    urls = [source.url, f"{embed_host}/embed/{source.item}", f"{embed_host}/oembed?{encoded}"]
    watch, embed, metadata = await asyncio.gather(
        *(public_fetch.get(client, url, proxy=proxy, limit=3_000_000, domains=YT_HOSTS) for url in urls), return_exceptions=True)
    players = [_player(result[0]) for result in (watch, embed) if isinstance(result, tuple)]
    player = next((p for p in players if p.get("captions")), next((p for p in players if p), {}))
    info = player.get("videoDetails", {}) or {}
    meta = _json(metadata[0]) if isinstance(metadata, tuple) else {}
    title = info.get("title") or (meta.get("title") if isinstance(meta, dict) else "") or source.title
    description = info.get("shortDescription", "") or ""
    tracks = (player.get("captions", {}).get("playerCaptionsTracklistRenderer", {}) or {}).get("captionTracks", []) or []
    tracks = sorted(tracks, key=lambda track: (
        not track.get("languageCode", "").lower().startswith(language),
        track.get("kind") == "asr",
    ))
    for track in tracks[:2]:
        try:
            link = public_fetch.public_url(track.get("baseUrl", ""))
            if link.path != "/api/timedtext":
                continue
            raw, _, _ = await public_fetch.get(client, str(link.update_query(fmt="json3")), proxy=proxy, domains=YT_HOSTS)
            if lines := _caption_lines(raw):
                return source_text.prepared(title, "\n\n".join(filter(None, (description, lines))))
        except (public_fetch.FetchError, ValueError, TypeError):
            continue
    return source_text.prepared(title, description, "captions")


async def _notion(store, session, uid, source, public, proxy):
    if source.item and connections.statuses(store, uid)["notion"] == "connected":
        base = "/pages/" + source.item
        page, body = await asyncio.gather(*(connections.call(store, session, uid, "notion", "GET", path)
            for path in (base, base + "/markdown")), return_exceptions=True)
        if isinstance(body, dict) and body.get("markdown"):
            from .integrations import notion_title
            title = notion_title(page) if isinstance(page, dict) else ""
            return source_text.prepared(title or source.title, body["markdown"], "truncated" if body.get("truncated") else "")
    try:
        result = await _website(public, source, proxy)
        if result.text and result.reason != "access":
            return result
    except (public_fetch.FetchError, ValueError):
        pass
    return source_text.Contents(source.title, reason="notion_access")


async def _drive(store, session, uid, source, public, proxy):
    if connections.statuses(store, uid)["drive"] == "connected":
        path = "/drive/v3/files/" + source.item
        try:
            meta = await connections.call(store, session, uid, "drive", "GET", path, params={"fields": "id,name,mimeType"})
            mime = meta.get("mimeType", "")
            title = meta.get("name") or source.title
            ext = PurePosixPath(title).suffix.lower()
            if mime in EXPORTS:
                suffix, export_mime = EXPORTS[mime]
                raw = await connections.call(store, session, uid, "drive", "GET", path + "/export",
                                             params={"mimeType": export_mime}, as_bytes=True)
            elif ext in source_text.FILE_TYPES or mime in EXTENSIONS:
                suffix = EXTENSIONS.get(mime, ext)
                raw = await connections.call(store, session, uid, "drive", "GET", path, params={"alt": "media"}, as_bytes=True)
            else:
                return source_text.Contents(title, reason="format")
            result = await source_text.read_file("source" + suffix, raw)
            return source_text.Contents(title, result.text, result.reason)
        except (connections.ConnectionError, ValueError):
            pass
    types = {"document": ("txt", ".txt"), "spreadsheets": ("xlsx", ".xlsx"), "presentation": ("pptx", ".pptx")}
    kind, ext = types.get(source.subtype, ("", ""))
    params = {"resourcekey": source.resource} if source.resource else {}
    root = "https://docs.google.com"
    if kind:
        paths = [f"{root}/{source.subtype}/d/{source.item}/export?" + urlencode({"format": kind, **params})]
        if kind == "pptx":
            paths.insert(0, f"{root}/presentation/d/{source.item}/export/pptx?" + urlencode(params))
    else:
        paths = ["https://drive.google.com/uc?" + urlencode({"export": "download", "id": source.item, **params})]
    for path in paths:
        try:
            raw, mime, _, filename = await public_fetch.get(public, path, proxy=proxy, limit=source_text.MAX_FILE, with_filename=True)
            if mime in ("text/html", "application/xhtml+xml"):
                continue
            extension = ext or EXTENSIONS.get(mime, PurePosixPath(filename or "").suffix.lower())
            extension = extension or (".pdf" if raw.startswith(b"%PDF-") else ".txt" if mime.startswith("text/") else "")
            if extension in source_text.FILE_TYPES:
                result = await source_text.read_file("source" + extension, raw)
                return source_text.Contents(filename or source.title, result.text, result.reason)
        except (public_fetch.FetchError, ValueError):
            continue
    return source_text.Contents(source.title, reason="drive_access")


async def _website(client, source, proxy):
    ext = PurePosixPath(public_fetch.public_url(source.url).path).suffix.lower()
    raw, mime, encoding = await public_fetch.get(client, source.url, proxy=proxy, limit=source_text.MAX_FILE)
    if mime in {"text/html", "application/xhtml+xml"}:
        result = await asyncio.to_thread(source_text.html_text, raw, encoding)
        return source_text.Contents(result.title or source.title, result.text, result.reason)
    extension = EXTENSIONS.get(mime, ext)
    if extension in source_text.FILE_TYPES:
        item = await source_text.read_file("source" + extension, raw)
        return source_text.Contents(source.title, item.text, item.reason)
    if mime.startswith("text/"):
        return source_text.prepared(source.title, source_text.decode_text(raw, encoding))
    return source_text.Contents(source.title, reason="format")


async def load(store, uid, source):
    try:
        async with (asyncio.timeout(19), public_fetch.session() as public,
                    public_fetch.session(proxy=True) as proxy, aiohttp.ClientSession(trust_env=True) as session):
            if source.kind == "youtube":
                language = store.get_user(uid)["language_code"].lower().split("-", 1)[0] or "ru"
                return await _youtube(public, source, language, proxy)
            if source.kind == "notion":
                return await _notion(store, session, uid, source, public, proxy)
            if source.kind == "drive":
                return await _drive(store, session, uid, source, public, proxy)
            return await _website(public, source, proxy)
    except public_fetch.PublicURL:
        raise
    except public_fetch.FetchError as error:
        reason = "access" if error.status in (401, 403, 404, 451) else "too_large" if error.status == 413 else "unavailable"
        return source_text.Contents(source.title, reason=reason)
    except TimeoutError:
        return source_text.Contents(source.title, reason="unavailable")
    except (UnicodeError, LookupError, TypeError, ValueError):
        return source_text.Contents(source.title, reason="no_text")


async def via_reader(source):
    """Read a public article through an isolated external service after consent."""
    if source.kind != "web":
        raise ValueError("Через Reader можно импортировать публичные статьи.")
    try:
        async with asyncio.timeout(19), aiohttp.ClientSession(trust_env=True, auto_decompress=False) as session:
            await public_fetch._pinned(public_fetch.public_url(source.url))
            async with session.get("https://r.jina.ai/" + source.url, allow_redirects=False, headers={
                "Accept": "text/plain", "Accept-Encoding": "identity",
            }) as response:
                if response.status != 200 or response.content_type != "text/plain":
                    raise public_fetch.FetchError(response.status)
                raw = await public_fetch.bounded(response.content, 2_000_000, response.headers.get("Content-Encoding", "").lower())
        content = raw.decode("utf-8")
        metadata, marker, text = content.partition("Markdown Content:")
        if not marker or not text.strip() or re.search(r"^Warning:.*(?:error|status code|restricted)\b", metadata, re.I | re.M):
            raise public_fetch.FetchError()
        title = re.search(r"^Title:\s*(.+)", metadata, re.M)
        return source_text.prepared(title[1].strip() if title else source.title, text)
    except (aiohttp.ClientError, OSError, TimeoutError, UnicodeError, public_fetch.FetchError):
        raise ValueError("Не удалось извлечь статью через Reader. Можно загрузить копию файлом.") from None
