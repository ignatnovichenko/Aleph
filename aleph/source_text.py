"""Extract text and table values from bounded project files."""
import asyncio
from dataclasses import dataclass
import html
import io
from pathlib import PurePosixPath
import re
import zipfile

from bs4 import BeautifulSoup
from defusedxml.ElementTree import fromstring
import openpyxl


MAX_FILE = 4 * 1024 * 1024
MAX_TEXT = 120000
WORKER_TYPES = {".pdf", ".doc", ".xls", ".ppt", ".odt", ".ods", ".odp", ".rtf"}
FILE_TYPES = WORKER_TYPES | {".docx", ".xlsx", ".pptx", ".csv", ".tsv", ".txt", ".md", ".json", ".html", ".htm", ".srt", ".vtt"}
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ud800-\udfff]")


@dataclass(frozen=True)
class Contents:
    title: str
    text: str = ""
    reason: str = ""

    @property
    def status(self):
        if not self.text:
            return "link"
        return "partial" if self.reason else "ready"


def prepared(title, text, reason=""):
    text = CONTROL.sub("", text).strip()
    if len(text) > MAX_TEXT:
        return Contents(title, text[:MAX_TEXT], "truncated")
    return Contents(title, text, reason)


def html_text(raw, encoding=None):
    page = BeautifulSoup(raw, "html.parser", from_encoding=encoding)
    og = page.find("meta", attrs={"property": "og:title"})
    title = og.get("content", "") if og else (page.title.get_text(" ", strip=True) if page.title else "")
    for removable in page.select('script,style,template,noscript,svg,nav,header,footer,aside,form,[hidden],[aria-hidden="true"]'):
        removable.decompose()
    content = page.select_one('article,main,[role="main"]') or page.body or page
    # Preserve line breaks between paragraphs and between rows without breaking
    # sentences at each inline link or emphasis tag.
    for block in content.find_all(["p", "h1", "h2", "h3", "h4", "li", "tr", "pre", "blockquote", "br"]):
        block.append("\n")
    for cell in content.find_all(["td", "th"]):
        cell.append(" | ")
    text = "\n".join(line.strip() for line in content.get_text().splitlines() if line.strip())
    if title.casefold().strip() in {"just a moment...", "access denied", "attention required!", "sign in", "notion"} and len(text) < 1500:
        return Contents(title, reason="access")
    return prepared(title.strip(), text, "" if text else "no_text")


def decode_text(raw, encoding=None):
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    return raw.decode(encoding if encoding and encoding.lower() != "utf-8" else "utf-8-sig")


def _office(raw, ext):
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        files = archive.infolist()
        if (len(files) > 2200 or sum(item.file_size for item in files) > 20 * 1024 * 1024
                or any(item.file_size > 9 * 1024 * 1024 for item in files)):
            raise ValueError("Файл слишком большой после распаковки. Загрузи нужный фрагмент отдельно.")
        if ext == ".xlsx":
            return _workbook(raw)
        if ext == ".docx":
            parts = ["word/document.xml"]
        else:
            parts = [name for name in archive.namelist() if re.fullmatch(r"ppt/slides/slide[0-9]+\.xml", name)]
            parts.sort(key=lambda name: int(re.search(r"\d+", name)[0]))
        output = []
        size = 0
        for index, part in enumerate(parts, 1):
            root = fromstring(archive.read(part))
            lines = []
            if ext == ".pptx":
                lines.append(f"## Slide {index}")
            for node in root.iter():
                if node.tag.endswith("}p"):
                    text = "".join(run.text or "" for run in node.iter() if run.tag.endswith("}t"))
                    if text.strip():
                        lines.append(text)
            section = "\n".join(lines)
            size += len(section)
            output.append(section)
            if size > MAX_TEXT:
                break
        return "\n\n".join(output), ""


def _workbook(raw):
    book = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True, keep_links=False)
    lines, size = [], 0
    limited = len(book.worksheets) > 20
    try:
        for sheet in book.worksheets[:20]:
            lines.append(f"## {sheet.title}")
            limited |= (sheet.max_column or 0) > 50 or (sheet.max_row or 0) > 1500
            # Stream values in a bounded region. Do not allocate a declared grid.
            for row in sheet.iter_rows(min_row=1, max_row=1501, min_col=1, max_col=50, values_only=True):
                if not any(value is not None for value in row):
                    continue
                values = [str(value).replace("\n", " ").replace("\t", " ") if value is not None else "" for value in row]
                line = "\t".join(values).rstrip("\t")
                size += len(line) + 1
                lines.append(line)
                if size > MAX_TEXT:
                    return "\n".join(lines), "truncated"
            lines.append("")
    finally:
        book.close()
    return ("\n".join(lines) if size else ""), "truncated" if limited else ""


def _regular_file(name, raw, ext):
    if ext in (".docx", ".pptx", ".xlsx"):
        try:
            text, reason = _office(raw, ext)
        except (KeyError, zipfile.BadZipFile, OSError, SyntaxError, ImportError, RuntimeError):
            raise ValueError("Не удалось прочитать офисный файл. Попробуй сохранить его заново.") from None
        return prepared(name, text, reason)
    if ext in (".html", ".htm"):
        result = html_text(raw)
        return Contents(name, result.text, result.reason)
    try:
        text = decode_text(raw)
    except (UnicodeError, LookupError):
        raise ValueError("Сохрани текстовый файл в UTF-8 или UTF-16.") from None
    if CONTROL.search(text):
        raise ValueError("В файле нет читаемого текста.")
    if ext in (".srt", ".vtt"):
        text = "\n".join(html.unescape(line) for line in text.splitlines() if line.strip() not in {"", "WEBVTT"})
    return prepared(name, text)


async def read_file(name, raw):
    if not isinstance(name, str) or not name.strip() or len(name) > 240 or CONTROL.search(name) or any(c in name for c in "/\\\r\n"):
        raise ValueError("Проверь имя файла.")
    if not raw or len(raw) > MAX_FILE:
        raise ValueError("Выбери файл до 4 МБ.")
    ext = PurePosixPath(name).suffix.lower()
    if ext not in FILE_TYPES:
        raise ValueError("Загрузи PDF, Word, Excel, PowerPoint, OpenDocument, RTF, CSV, TSV, TXT, Markdown, JSON, HTML, SRT или VTT.")
    if ext in WORKER_TYPES:
        from .documents import run_office
        packet, _ = await run_office("source", {}, {1: (ext[1:], raw)})
        result = prepared(name, packet["text"], "truncated" if packet["truncated"] else "")
    else:
        result = await asyncio.to_thread(_regular_file, name, raw, ext)
    if not result.text:
        raise ValueError("В файле не найдено текста или значений таблицы.")
    return result
