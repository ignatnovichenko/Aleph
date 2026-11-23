"""Read user-supplied attachments and render files in memory for the requesting chat."""
import asyncio
import csv
import io
import json
import os
import re
import signal
import sys
import tempfile
import unicodedata
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

import aiohttp

from .api import fold_ical, ical_text
from .media import MediaError
from . import office_formats, office_native
from .pdf_worker import MAX_BYTES, MAX_TEXT

_pdf_slots = asyncio.Semaphore(2)
TEXT_BYTES = 128 * 1024
MAX_TEXT_OUTPUT = 128 * 1024
MAX_INPUT = 16 * 1024 * 1024
MAX_OUTPUT = 16 * 1024 * 1024
MIMES = {"md": "text/markdown", "txt": "text/plain", "csv": "text/csv", "ics": "text/calendar",
         **office_formats.MIMES, **office_native.MIMES}
TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".json", ".ics", ".tsv"}
_office_slots = asyncio.Semaphore(3)


async def read_document(metadata, download):
    """Download one Telegram attachment with the existing authenticated, size-limited helper."""
    mime = str(metadata.get("mime_type", "")).split(";", 1)[0].lower()
    extension = Path(str(metadata.get("file_name", ""))).suffix.lower()
    is_pdf = mime == "application/pdf" or extension == ".pdf"
    if not is_pdf and extension not in TEXT_EXTENSIONS and mime not in {*MIMES.values(), "application/json"}:
        raise MediaError("Могу прочитать PDF с текстом, TXT, Markdown, CSV, TSV, JSON или ICS.")
    limit = MAX_BYTES if is_pdf else TEXT_BYTES
    if (metadata.get("file_size") or 0) > limit:
        raise MediaError("Для PDF лимит 4 МБ, для текстового файла — 128 КБ. Пришли нужный фрагмент.")
    data = await download(metadata, limit)
    if len(data) > limit:
        raise MediaError("Этот файл слишком большой. Пришли нужный фрагмент.")
    if is_pdf:
        return await _pdf_text(data)
    try:
        encoding = "utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
        text = data.decode(encoding)
    except UnicodeError:
        raise MediaError("Не удалось прочитать кодировку. Сохрани файл в UTF-8 и пришли его ещё раз.") from None
    if any(ord(c) < 32 and c not in "\t\r\n" for c in text) or not text.strip():
        raise MediaError("В файле нет читаемого текста.")
    if len(text) > MAX_TEXT:
        raise MediaError("Пришли фрагмент до 30 000 символов.")
    return text.strip()


async def _pdf_text(data):
    if not data.startswith(b"%PDF-"):
        raise MediaError("Не удалось распознать PDF. Экспортируй документ заново.")
    async with _pdf_slots:
        worker = await asyncio.create_subprocess_exec(
            sys.executable, "-I", str(Path(__file__).with_name("pdf_worker.py")),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            output, _ = await asyncio.wait_for(worker.communicate(data), timeout=12)
        except TimeoutError:
            raise MediaError("PDF слишком сложный. Пришли несколько нужных страниц отдельно.") from None
        finally:
            if worker.returncode is None:
                try:
                    worker.kill()
                except ProcessLookupError:
                    pass
                await worker.wait()
    errors = {"long": "Для PDF лимит 40 страниц и 30 000 символов. Пришли нужный фрагмент.",
              "encrypted": "PDF защищён паролем. Можешь прислать расшифрованную копию.",
              "scanned": "В PDF только изображения. Пришли нужную страницу как фото."}
    try:
        status, text = json.loads(output)
    except (ValueError, TypeError):
        status, text = "invalid", ""
    if status != "ok" or not text:
        raise MediaError(errors.get(status, "Не удалось прочитать PDF. Экспортируй его заново или пришли текст."))
    return text


def _limited_text(value, limit, *, blank=False):
    if not isinstance(value, str) or len(value) > limit or (not blank and not value.strip()):
        raise ValueError("Проверь содержимое и размер документа.")
    if any(ord(c) < 32 and c not in "\t\r\n" for c in value):
        raise ValueError("В тексте есть неподдерживаемые управляющие символы.")
    return value


def delimited(rows, fmt):
    if not isinstance(rows, list) or not 1 <= len(rows) <= 1500:
        raise ValueError("В таблице должно быть от 1 до 1500 строк.")
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, delimiter="\t" if fmt == "tsv" else ",")
    for row in rows:
        if not isinstance(row, list) or not 1 <= len(row) <= 64:
            raise ValueError("В строке таблицы должно быть от 1 до 64 ячеек.")
        cells = []
        for cell in row:
            value = office_formats.cell_value(cell)
            cell = str(value) if value is not None else ""
            # Preserve ordinary signed numeric values and neutralize spreadsheet formulas.
            if cell.lstrip().startswith(("=", "+", "-", "@")) and not re.fullmatch(r"[+-]?\d+(?:[.,]\d+)?", cell):
                cell = "'" + cell
            cells.append(cell)
        writer.writerow(cells)
        if buf.tell() > MAX_TEXT_OUTPUT:
            raise ValueError("Таблица слишком большая. Раздели её на несколько файлов.")
    return "\ufeff" + buf.getvalue()


def _event_date(value):
    value = _limited_text(value, 40)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        parsed = date.fromisoformat(value)
        return ";VALUE=DATE:", parsed.strftime("%Y%m%d")
    moment = datetime.fromisoformat(value)
    if moment.utcoffset() is None:
        raise ValueError("Для события со временем укажи часовой пояс.")
    return ":", moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _calendar(events):
    if not isinstance(events, list) or not 1 <= len(events) <= 100:
        raise ValueError("В календарном файле должно быть от 1 до 100 событий.")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Aleph//Calendar//EN", "CALSCALE:GREGORIAN"]
    for i, event in enumerate(events):
        if not isinstance(event, dict):
            raise ValueError("Проверь поля события.")
        title = _limited_text(event.get("title"), 300)
        kind, start = _event_date(event.get("start"))
        uid = uuid.uuid5(uuid.NAMESPACE_URL, json.dumps([i, title, start]))
        lines.extend(["BEGIN:VEVENT", f"UID:{uid}@aleph.invalid", f"DTSTAMP:{stamp}", f"DTSTART{kind}{start}",
                      "SUMMARY:" + ical_text(title)])
        if event.get("end"):
            end_kind, end = _event_date(event["end"])
            if end_kind != kind or end <= start:
                raise ValueError("Конец события должен следовать за началом и иметь тот же формат.")
            lines.append(f"DTEND{kind}{end}")
        if event.get("description"):
            lines.append("DESCRIPTION:" + ical_text(_limited_text(event["description"], 2000)))
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold_ical(line) for line in lines) + "\r\n"


def document_name(filename, fmt):
    if not isinstance(fmt, str) or fmt not in MIMES:
        raise ValueError("Формат документа не поддерживается.")
    name = unicodedata.normalize("NFKC", _limited_text(filename, 160))
    if name.lower().endswith("." + fmt):
        name = name[:-(len(fmt) + 1)]
    name = re.sub(r"[^\w -]+", "-", name).strip(" -_")[:72].strip()
    if not name:
        raise ValueError("Укажи имя файла.")
    return f"{name}.{fmt}"


def render_document(arguments):
    """Render model-authored content. No local paths are accepted."""
    fmt = arguments.get("format")
    name = document_name(arguments.get("filename"), fmt)
    if fmt in office_native.LEGACY:
        modern = office_native.LEGACY[fmt]
        content = office_native.convert(office_formats.render(modern, arguments), modern, fmt)
        if not content:
            raise ValueError("Преобразование в старый Office недоступно. Выбери DOCX, XLSX или PPTX.")
    elif fmt in ("csv", "tsv"):
        content = delimited(arguments.get("rows"), fmt).encode("utf-8")
    elif fmt == "ics":
        content = _calendar(arguments.get("events")).encode("utf-8")
    elif fmt in ("txt", "md", "json"):
        text = _limited_text(arguments.get("content"), MAX_TEXT_OUTPUT)
        if fmt == "json":
            json.loads(text)
        content = text.encode("utf-8")
    else:
        content = office_formats.render(fmt, arguments)
    limit = MAX_OUTPUT if fmt in {"docx", "pdf", "xlsx", "pptx", "odt", "ods", "odp", *office_native.LEGACY} else MAX_TEXT_OUTPUT
    if len(content) > limit:
        raise ValueError("Документ слишком большой. Раздели его на несколько частей.")
    return name, MIMES[fmt], content


async def run_office(operation, arguments, inputs):
    """Bound time and memory for parsers, renderers and OCR on untrusted files."""
    async with _office_slots:
        with tempfile.TemporaryDirectory(prefix="aleph-doc-") as work:
            root = Path(work)
            packet = {"operation": operation, "arguments": arguments, "inputs": []}
            for file_id, (fmt, raw) in inputs.items():
                path = f"input-{file_id}.{fmt}"
                await asyncio.to_thread((root / path).write_bytes, raw)
                packet["inputs"].append({"id": file_id, "format": fmt, "path": path})
            env = {"HOME": work, "TMPDIR": work, "PATH": os.defpath, "LANG": "C.UTF-8", "OMP_THREAD_LIMIT": "1"}
            binary = os.getenv("ALEPH_OFFICE_BIN")
            if not binary:
                configured = Path.home() / ".local/share/aleph/soffice-path"
                if configured.is_file():
                    binary = configured.read_text().strip()
            if binary and Path(binary).is_absolute():
                env["ALEPH_OFFICE_BIN"] = binary
            ocr_data = os.getenv("ALEPH_TESSDATA")
            if ocr_data:
                env["TESSDATA_PREFIX"] = ocr_data
            else:
                tessdata = Path.home() / ".local/share/aleph/tessdata"
                if tessdata.is_dir():
                    env["TESSDATA_PREFIX"] = str(tessdata)
            worker = await asyncio.create_subprocess_exec(
                sys.executable, "-I", str(Path(__file__).with_name("office_worker.py")), cwd=work, env=env,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
            try:
                budget = 42 if operation == "edit" or any(fmt == "pdf" for fmt, _ in inputs.values()) else 20
                stdout, _ = await asyncio.wait_for(worker.communicate(json.dumps(packet).encode()), timeout=budget)
            except TimeoutError:
                raise ValueError("Документ слишком сложный. Выбери несколько страниц или раздели его на части.") from None
            finally:
                if worker.returncode is None:
                    try:
                        os.killpg(worker.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    await worker.wait()
            try:
                result = json.loads(stdout)
            except (ValueError, TypeError):
                raise ValueError("Не удалось обработать документ. Попробуй сохранить его заново.") from None
            if reason := result.pop("error", ""):
                raise ValueError(reason)
            blob = None
            if "filename" in result:
                output = root / "result.bin"
                if not output.is_file() or not 0 < output.stat().st_size <= MAX_OUTPUT:
                    raise ValueError("Готовый документ слишком большой или пустой.")
                blob = await asyncio.to_thread(output.read_bytes)
            return result, blob


class DocumentDeliveryError(Exception):
    def __init__(self, status=0, retry_after=0):
        super().__init__("Не удалось доставить документ в Telegram.")
        self.status = status
        self.retry_after = retry_after


async def send_document(session, token, uid, filename, mime, content, *, receipt=False, reply_parameters=None):
    if type(uid) is not int or uid <= 0 or not isinstance(content, bytes) or len(content) > MAX_OUTPUT:
        raise ValueError("Некорректный адресат или размер документа.")
    form = aiohttp.FormData(quote_fields=False)
    form.add_field("chat_id", str(uid))
    if reply_parameters:
        form.add_field("reply_parameters", json.dumps(reply_parameters))
    form.add_field("document", content, filename=filename, content_type=mime)
    response_timeout = aiohttp.ClientTimeout(total=30)
    try:
        async with session.post(f"https://api.telegram.org/bot{token}/sendDocument", data=form,
                                timeout=response_timeout, allow_redirects=False) as response:
            payload = await response.json(content_type=None)
            if response.status != 200 or not payload.get("ok"):
                raise DocumentDeliveryError(int(payload.get("error_code") or response.status),
                                            int((payload.get("parameters") or {}).get("retry_after") or 0))
            result = payload["result"]
            return result if receipt else int(result["message_id"])
    except (aiohttp.ClientError, OSError, ValueError, KeyError, TypeError):
        raise DocumentDeliveryError() from None
