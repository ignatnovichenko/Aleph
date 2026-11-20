"""Inspect and edit document content without opening office applications."""
from bisect import bisect_right
from collections import Counter
import csv
import io
import json
from pathlib import PurePosixPath
import re
import shutil
import subprocess
import zipfile

from lxml import etree

from . import office_formats, source_text


MAX_PDF_PAGES = 250
MAX_TEXT = 120_000
MAX_OCR_PAGES = 6
OOXML = {"docx", "xlsx", "pptx"}
ODF = {"odt", "ods", "odp"}
SPREADSHEETS = {"xlsx", "ods", "csv", "tsv"}
WORD_P = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
SLIDE_P = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
ODF_P = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
ODF_T = "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}"
ODF_O = "{urn:oasis:names:tc:opendocument:xmlns:office:1.0}"
ODF_D = "{urn:oasis:names:tc:opendocument:xmlns:drawing:1.0}"


def package(raw, fmt, *, editing=False):
    archive = zipfile.ZipFile(io.BytesIO(raw))
    try:
        parts = archive.infolist()
        total = sum(part.file_size for part in parts)
        names = archive.namelist()
        if (len(parts) > 2600 or total > 64 * 1024 * 1024 or len(names) != len(set(names))
                or any(p.file_size > 24 * 1024 * 1024 or p.flag_bits & 1 for p in parts)
                or any(name.startswith("/") or "\\" in name or ".." in PurePosixPath(name).parts for name in names)):
            raise ValueError("Офисный файл слишком большой, повреждён или защищён.")
        expected = {"docx": "word/document.xml", "xlsx": "xl/workbook.xml", "pptx": "ppt/presentation.xml"}
        if fmt in expected:
            archive.getinfo(expected[fmt])
        else:
            mime = archive.read("mimetype")
            expected_mime = "application/vnd.oasis.opendocument.graphics" if fmt == "odg" else office_formats.MIMES[fmt]
            if mime != expected_mime.encode():
                raise ValueError("Содержимое документа не соответствует его расширению.")
            archive.getinfo("content.xml")
        if editing and any(re.search(r"vbaproject|/(?:activex|embeddings|externallinks)/|script-lb|^Scripts/", name, re.I)
                           for name in names):
            raise ValueError("Сохрани копию без макросов, встроенных объектов и внешних источников для редактирования.")
        return archive
    except BaseException:
        archive.close()
        raise


def _tree(data):
    root = etree.fromstring(data, parser=etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False))
    if root.getroottree().docinfo.doctype:
        raise ValueError("Этот файл содержит неподдерживаемое XML-описание.")
    return root


def _parts(archive, fmt):
    if fmt == "docx":
        return sorted(name for name in archive.namelist() if re.fullmatch(
            r"word/(?:document|header\d+|footer\d+|footnotes|endnotes)\.xml", name))
    if fmt == "pptx":
        names = [name for name in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)]
        return sorted(names, key=lambda name: int(re.search(r"\d+", name).group()))
    return ["content.xml"]


def _paragraphs(root, fmt):
    if fmt == "docx":
        return root.iter(WORD_P + "p")
    if fmt == "pptx":
        return root.iter(SLIDE_P + "p")
    return root.iter((ODF_P + "p", ODF_P + "h"))


def _xml_slots(paragraph, fmt):
    if fmt in OOXML:
        ns = WORD_P if fmt == "docx" else SLIDE_P
        group = []
        for node in paragraph.iter():
            if node.tag == ns + "p" and node is not paragraph:
                continue
            if node.tag in (ns + "br", ns + "tab", ns + "cr"):
                if group:
                    yield group
                group = []
            if node.tag == ns + "t" and next(node.iterancestors(ns + "p"), None) is paragraph and node.text:
                group.append((node, "text"))
        if group:
            yield group
        return

    def walk(node):
        if node.text:
            yield node, "text"
        for child in node:
            if child.tag not in (ODF_P + "p", ODF_P + "h"):
                yield from walk(child)
            if child.tail:
                yield child, "tail"
    yield list(walk(paragraph))


def _ooxml_text(raw, fmt, *, labeled=False):
    sections = []
    with package(raw, fmt) as archive:
        for i, part in enumerate(_parts(archive, fmt), 1):
            root = _tree(archive.read(part))
            lines = []
            for paragraph in _paragraphs(root, fmt):
                raw_text = "\t".join("".join(getattr(node, attr) for node, attr in group)
                                     for group in _xml_slots(paragraph, fmt)).strip()
                if not raw_text:
                    continue
                if fmt == "docx" and not labeled:
                    style = paragraph.find(WORD_P + "pPr/" + WORD_P + "pStyle")
                    name = style.get(WORD_P + "val", "") if style is not None else ""
                    heading = re.fullmatch(r"Heading([1-6])", name)
                    if heading:
                        raw_text = "#" * int(heading.group(1)) + " " + raw_text
                    elif name == "Title":
                        raw_text = "# " + raw_text
                lines.append(raw_text)
            if lines and fmt == "pptx":
                lines.insert(0, f"## Слайд {i}")
            sections.extend(lines)
            if sum(len(line) for line in sections) > MAX_TEXT:
                break
    return "\n\n".join(sections)


def _odf_text(raw, fmt):
    if fmt == "ods":
        return _spreadsheet_text(list(_ods_rows(raw)))
    with package(raw, fmt) as archive:
        root = _tree(archive.read("content.xml"))
    lines = []
    scopes = root.iter(ODF_D + "page") if fmt == "odp" else [root]
    for i, scope in enumerate(scopes, 1):
        if fmt == "odp":
            lines.append(f"## Слайд {i}")
        for p in _paragraphs(scope, fmt):
            value = "".join(p.itertext()).strip()
            if value:
                lines.append(value)
    return "\n\n".join(lines)


def _column(number):
    from openpyxl.utils import get_column_letter
    return get_column_letter(number)


def _ods_rows(raw, sheet_name=None):
    with package(raw, "ods") as archive:
        root = _tree(archive.read("content.xml"))
    for sheet in list(root.iter(ODF_T + "table"))[:12]:
        name = sheet.get(ODF_T + "name", "Данные")
        if sheet_name is not None and sheet_name != name:
            continue
        rows = []
        for node in sheet.iter(ODF_T + "table-row"):
            values = []
            for cell in node:
                if cell.tag not in (ODF_T + "table-cell", ODF_T + "covered-table-cell"):
                    continue
                if len(values) >= 64:
                    break
                repeats = min(64 - len(values), int(cell.get(ODF_T + "number-columns-repeated", "1")))
                kind = cell.get(ODF_O + "value-type")
                if kind in ("float", "percentage", "currency"):
                    raw_value = cell.get(ODF_O + "value")
                    value = float(raw_value) if raw_value is not None else None
                elif kind == "boolean":
                    value = cell.get(ODF_O + "boolean-value") == "true"
                else:
                    value = "\n".join("".join(p.itertext()) for p in cell.iter(ODF_P + "p")) or None
                values.extend([value] * repeats)
            if len(rows) >= 1500:
                break
            repeats = min(1500 - len(rows), int(node.get(ODF_T + "number-rows-repeated", "1")))
            rows.extend([values or [None]] * repeats)
        while rows and not any(value is not None for value in rows[-1]):
            rows.pop()
        yield name, rows or [[None]]


def _xlsx_rows(raw, sheet_name=None, *, formulas=False):
    from openpyxl import load_workbook

    with package(raw, "xlsx"):
        pass
    workbook = load_workbook(io.BytesIO(raw), read_only=True, data_only=not formulas, keep_links=False)
    try:
        for sheet in workbook.worksheets[:12]:
            if sheet_name is not None and sheet.title != sheet_name:
                continue
            rows = []
            for values in sheet.iter_rows(max_row=min(sheet.max_row or 1500, 1500), max_col=min(sheet.max_column or 64, 64), values_only=True):
                row = [v if v is None or type(v) in (str, int, float, bool) else str(v) for v in values]
                while row and row[-1] is None:
                    row.pop()
                rows.append(row or [None])
            while rows and not any(value is not None for value in rows[-1]):
                rows.pop()
            yield sheet.title, rows or [[None]]
    finally:
        workbook.close()


def _delimited_rows(raw, fmt):
    delim = "\t" if fmt == "tsv" else ","
    text = source_text.decode_text(raw)
    if fmt == "csv":
        try:
            delim = csv.Sniffer().sniff(text[:4096], delimiters=",;").delimiter
        except csv.Error:
            pass
    return [("Данные", list(csv.reader(io.StringIO(text, newline=""), delimiter=delim)))]


def _spreadsheet_text(sheets):
    lines, size = [], 0
    for name, rows in sheets:
        lines.append("## Лист " + name)
        for row, values in enumerate(rows, 1):
            parts = [f"{_column(i)}{row}: {str(value).replace(chr(10), ' ')[:1000]}"
                     for i, value in enumerate(values[:64], 1) if value is not None and value != ""]
            if not parts:
                continue
            line = " | ".join(parts)
            lines.append(line)
            size += len(line) + 1
            if size > MAX_TEXT:
                return "\n".join(lines)
    return "\n".join(lines)


def _pdf_reader(raw):
    from pypdf import PdfReader

    if not raw.startswith(b"%PDF-"):
        raise ValueError("Не удалось распознать PDF.")
    reader = PdfReader(io.BytesIO(raw), strict=False)
    if reader.is_encrypted:
        raise ValueError("PDF защищён паролем. Пришли расшифрованную копию.")
    if len(reader.pages) > MAX_PDF_PAGES:
        raise ValueError(f"Для PDF лимит {MAX_PDF_PAGES} страниц. Раздели его на части.")
    return reader


def _ocr(path, page_number):
    png = f"ocr-{page_number}"
    try:
        subprocess.run(["pdftoppm", "-f", str(page_number), "-l", str(page_number), "-scale-to", "2000",
                        "-gray", "-singlefile", "-png", str(path), png], timeout=8, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        langs = subprocess.run(["tesseract", "--list-langs"], capture_output=True, timeout=3, check=True).stdout.decode().splitlines()
        enabled = "+".join(lang for lang in ("eng", "rus") if lang in langs)
        if not enabled:
            return ""
        packet = subprocess.run(["tesseract", png + ".png", "stdout", "-l", enabled], capture_output=True, timeout=15, check=True)
        return packet.stdout.decode("utf-8", errors="replace").strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _page_window(args, count):
    start = args.get("start_page", 1)
    end = args.get("end_page", count)
    if type(start) is not int or type(end) is not int or not 1 <= start <= end <= count:
        raise ValueError("Укажи страницы в пределах файла и по порядку.")
    return start, end


def _pdf_text(raw, args, *, labeled=False, path=None):
    pdf = _pdf_reader(raw)
    if not pdf.pages:
        raise ValueError("В PDF нет страниц.")
    start, end = _page_window(args, len(pdf.pages))
    ocr_count, scanned, total = 0, 0, 0
    unread = []
    lines = []
    can_ocr = path and shutil.which("pdftoppm") and shutil.which("tesseract")
    for i in range(start, end + 1):
        text = (pdf.pages[i - 1].extract_text() or "").strip()
        if not text:
            scanned += 1
            if can_ocr and ocr_count < MAX_OCR_PAGES:
                ocr_count += 1
                text = _ocr(path, i)
        if text:
            page = f"## Страница {i}\n{text}" if labeled else text
            lines.append(page)
            total += len(page)
            if total > MAX_TEXT:
                break
        else:
            unread.append(i)
    joined = "\n\n".join(lines)
    if not joined:
        raise ValueError("На выбранных страницах нет текста или распознанного скана. Можно извлечь страницы PDF как есть.")
    info = {"total_pages": len(pdf.pages), "ocr_pages_attempted": ocr_count, "unread_pages": unread}
    if scanned:
        info["scan_note"] = "OCR is approximate and limited to six pages per read. Use start_page/end_page for other scan pages."
    return joined, info


def read(raw, fmt, args, *, labeled=False, path=None):
    info = {}
    if fmt == "pdf":
        return _pdf_text(raw, args, labeled=labeled, path=path)
    if fmt in ("docx", "pptx"):
        content = _ooxml_text(raw, fmt, labeled=labeled)
    elif fmt in ODF:
        content = _odf_text(raw, fmt)
    elif fmt == "xlsx":
        content = _spreadsheet_text(list(_xlsx_rows(raw, args.get("sheet"), formulas=True)))
    elif fmt in ("csv", "tsv") and labeled:
        content = _spreadsheet_text(_delimited_rows(raw, fmt))
    elif fmt in ("html", "htm"):
        content = source_text.html_text(raw).text
    elif fmt == "rtf":
        from striprtf.striprtf import rtf_to_text
        content = rtf_to_text(raw.decode("latin-1"), errors="ignore")
    else:
        content = source_text.decode_text(raw)
    return source_text.CONTROL.sub("", content).strip(), info


def inspect(raw, fmt, args, *, path=None):
    offset = args.get("offset", 0)
    if type(offset) is not int or not 0 <= offset <= MAX_TEXT:
        raise ValueError("Некорректное смещение текста.")
    text, info = read(raw, fmt, args, labeled=True, path=path)
    limit = min(len(text), offset + 8000, MAX_TEXT)
    return {**info, "text": text[offset:limit], "offset": offset, "next_offset": limit if limit < min(len(text), MAX_TEXT) else None,
            "truncated": len(text) > MAX_TEXT}


def _replacements(items):
    if not isinstance(items, list) or not 1 <= len(items) <= 40:
        raise ValueError("Укажи от одной до 40 замен.")
    texts = {}
    for item in items:
        if not isinstance(item, dict) or set(item) != {"find", "replace"}:
            raise ValueError("Укажи исходный и новый текст для каждой замены.")
        before = office_formats.checked_text(item["find"], 4000)
        after = office_formats.checked_text(item["replace"], 6000, blank=True)
        if before in texts:
            raise ValueError("Одно совпадение можно заменить только одним способом.")
        texts[before] = after
    pattern = re.compile("|".join(re.escape(before) for before in sorted(texts, key=len, reverse=True)))
    return texts, pattern


def _slots_replace(group, values, pattern, hits):
    starts, strings, length = [], [], 0
    for node, field in group:
        s = getattr(node, field) or ""
        starts.append(length)
        strings.append(s)
        length += len(s)
    for match in reversed(list(pattern.finditer("".join(strings)))):
        old = match.group()
        hits[old] += 1
        first = bisect_right(starts, match.start()) - 1
        last = bisect_right(starts, match.end() - 1) - 1
        left = strings[first][:match.start() - starts[first]]
        right = strings[last][match.end() - starts[last]:]
        if first == last:
            strings[first] = left + values[old] + right
        else:
            strings[first] = left + values[old]
            for i in range(first + 1, last):
                strings[i] = ""
            strings[last] = right
    for (node, field), text in zip(group, strings):
        if getattr(node, field) != text:
            if field == "text" and node.tag == WORD_P + "t" and re.search(r"[\t\r\n]", text):
                pieces = re.split(r"(\r\n|[\t\r\n])", text)
                node.text = pieces[0]
                node.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
                previous = node
                for separator, part in zip(pieces[1::2], pieces[2::2]):
                    marker = etree.Element(WORD_P + ("tab" if separator == "\t" else "br"))
                    entry = etree.Element(WORD_P + "t")
                    entry.text = part
                    entry.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
                    previous.addnext(marker)
                    marker.addnext(entry)
                    previous = entry
            else:
                setattr(node, field, text)
                if field == "text" and text != text.strip():
                    node.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")


def _replace_xml(raw, fmt, values, pattern, hits):
    out = io.BytesIO()
    with package(raw, fmt, editing=True) as src, zipfile.ZipFile(out, "w") as dst:
        parts = set(_parts(src, fmt))
        for file in src.infolist():
            part = src.read(file)
            if file.filename in parts:
                old_count = sum(hits.values())
                root = _tree(part)
                for paragraph in _paragraphs(root, fmt):
                    for group in _xml_slots(paragraph, fmt):
                        _slots_replace(group, values, pattern, hits)
                if old_count != sum(hits.values()):
                    part = etree.tostring(root, encoding="UTF-8", xml_declaration=True)
            dst.writestr(file, part)
    return out.getvalue()


def replace_text(raw, fmt, args, *, path=None):
    texts, pattern = _replacements(args.get("replacements"))
    hits = Counter()
    rebuilt = fmt in ("pdf", "rtf", "html", "htm")
    if fmt in {"docx", "pptx", "odt", "odp", "odg"}:
        output = _replace_xml(raw, fmt, texts, pattern, hits)
    elif fmt in ("xlsx", "ods"):
        raise ValueError("Для электронных таблиц используй изменения ячеек.")
    else:
        text, info = read(raw, fmt, args, path=path) if rebuilt else (source_text.decode_text(raw), {})
        if fmt == "pdf" and (len(text) > MAX_TEXT or info.get("unread_pages")):
            raise ValueError("Для замены выбери часть PDF, где удалось прочитать все страницы.")

        def replace(match):
            old = match.group()
            hits[old] += 1
            return texts[old]

        text = pattern.sub(replace, text)
        if rebuilt and fmt in ("pdf", "rtf", "html", "htm"):
            output = office_formats.render("html" if fmt == "htm" else fmt, {"content": text})
            if fmt == "pdf" and ("start_page" in args or "end_page" in args):
                output = _replace_pdf_range(raw, output, args)
        else:
            if fmt == "json":
                json.loads(text)
            if raw.startswith(b"\xfe\xff"):
                output = b"\xfe\xff" + text.encode("utf-16-be")
            elif raw.startswith(b"\xff\xfe"):
                output = b"\xff\xfe" + text.encode("utf-16-le")
            else:
                output = text.encode("utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8")
    misses = [text[:100] for text in texts if not hits[text]]
    if misses:
        raise ValueError("Текст для замены не найден: " + " | ".join(misses))
    return output, {"replacements_applied": sum(hits.values()), "layout_rebuilt": rebuilt}


def _replace_pdf_range(original, updated, args):
    from pypdf import PdfWriter

    pdf = _pdf_reader(original)
    start, end = _page_window(args, len(pdf.pages))
    if start == 1 and end == len(pdf.pages):
        return updated
    writer = PdfWriter()
    if start > 1:
        writer.append(pdf, pages=(0, start - 1), import_outline=False)
    writer.append(_pdf_reader(updated), import_outline=False)
    if end < len(pdf.pages):
        writer.append(pdf, pages=(end, len(pdf.pages)), import_outline=False)
    if len(writer.pages) > MAX_PDF_PAGES:
        raise ValueError("После изменений PDF слишком длинный. Выбери меньший диапазон.")
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def replace_pdf_layout(raw, args):
    from . import office_native

    if "start_page" in args or "end_page" in args:
        return None
    count = len(_pdf_reader(raw).pages)
    if count > 40:
        return None
    drawing = office_native.convert(raw, "pdf", "odg")
    if not drawing:
        return None
    try:
        updated, info = replace_text(drawing, "odg", args)
        output = office_native.convert(updated, "odg", "pdf")
        if output and len(_pdf_reader(output).pages) == count:
            return output, {**info, "layout_rebuilt": True}
    except ValueError:
        return None
    return None


def _formula(cell, value):
    from openpyxl.formula import Tokenizer

    value = office_formats.checked_text(value, 2000)
    if not value.startswith("=") or re.search(r"[\[\]|]|https?://", value, re.I):
        raise ValueError("Укажи формулу Excel без внешних ссылок.")
    allowed = set("SUM SUMIF SUMIFS IF IFERROR COUNT COUNTA COUNTIF COUNTIFS AVERAGE AVERAGEIF MIN MAX ROUND "
                  "ROUNDUP ROUNDDOWN ABS AND OR NOT CONCAT CONCATENATE LEN LEFT RIGHT MID CEILING FLOOR POWER "
                  "SQRT MOD INT MEDIAN SUBTOTAL VLOOKUP HLOOKUP INDEX MATCH XLOOKUP TODAY DATE YEAR MONTH DAY "
                  "ISNUMBER ISBLANK TEXT TRIM UPPER LOWER UNIQUE SORT".split())
    for token in Tokenizer(value).items:
        if token.type == "FUNC" and token.subtype == "OPEN" and token.value[:-1].upper() not in allowed:
            raise ValueError("Эта функция Excel недоступна. Используй обычные вычисления, ссылки и функции поиска.")
        if token.subtype == "RANGE" and not re.fullmatch(
                r"(?:(?:'[^'\r\n]+?'|[\w.]+)!)?\$?[a-z]{1,3}\$?[1-9]\d{0,6}(?::\$?[a-z]{1,3}\$?[1-9]\d{0,6})?", token.value, re.I):
            raise ValueError("Для формулы укажи явные ссылки на ячейки или диапазоны.")
    cell.value = value


def _changes(items):
    if not isinstance(items, list) or not 1 <= len(items) <= 1200:
        raise ValueError("Укажи от одной до 1200 ячеек.")
    for item in items:
        if (not isinstance(item, dict) or set(item) - {"sheet", "cell", "value", "formula"}
                or ("value" in item) == ("formula" in item)):
            raise ValueError("Укажи лист, адрес и значение или формулу.")
        from openpyxl.utils.cell import coordinate_from_string, column_index_from_string
        address = office_formats.checked_text(item.get("cell"), 12).upper()
        if not re.fullmatch(r"[A-Z]{1,3}[1-9]\d{0,6}", address):
            raise ValueError("Укажи адрес ячейки, например B3.")
        column, row = coordinate_from_string(address)
        column = column_index_from_string(column)
        if row > 1_048_576 or column > 16_384:
            raise ValueError("Адрес вне листа.")
        yield item, address, row, column


def _xlsx_edit(raw, args):
    from openpyxl import load_workbook
    from openpyxl.cell.cell import MergedCell

    with package(raw, "xlsx", editing=True):
        pass
    wb = load_workbook(io.BytesIO(raw), keep_links=False, rich_text=True)
    for change, address, _, _ in _changes(args.get("changes")):
        name = change.get("sheet") or wb.active.title
        if name not in wb.sheetnames:
            raise ValueError(f"Лист {name!r} не найден.")
        cell = wb[name][address]
        if isinstance(cell, MergedCell):
            raise ValueError("Укажи верхнюю левую ячейку объединённого диапазона.")
        if "formula" in change:
            _formula(cell, change["formula"])
        else:
            office_formats.set_cell(cell, change.get("value"))
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue(), {"cells_changed": len(args["changes"])}


def _ods_edit(raw, args):
    with package(raw, "ods", editing=True) as src:
        root = _tree(src.read("content.xml"))
        sheets = {node.get(ODF_T + "name"): node for node in root.iter(ODF_T + "table")}
        for change, _, row_index, col_index in _changes(args.get("changes")):
            if "formula" in change:
                raise ValueError("Для ODS можно менять значения, формулы доступны в XLSX.")
            name = change.get("sheet") or next(iter(sheets), "")
            if name not in sheets or row_index > 1500 or col_index > 64:
                raise ValueError("Укажи лист и ячейку в пределах 1500 строк и 64 столбцов.")
            sheet = sheets[name]
            rows = _split_repeat(sheet, ODF_T + "table-row", ODF_T + "number-rows-repeated", row_index)
            cell = _split_repeat(rows, ODF_T + "table-cell", ODF_T + "number-columns-repeated", col_index,
                                 extra=ODF_T + "covered-table-cell")
            if cell.tag != ODF_T + "table-cell" or cell.get(ODF_T + "number-columns-spanned") or cell.get(ODF_T + "number-rows-spanned"):
                raise ValueError("Для ODS укажи ячейку вне объединённого диапазона.")
            for key in list(cell.attrib):
                if key.startswith(ODF_O) or key == ODF_T + "formula":
                    del cell.attrib[key]
            for child in list(cell):
                if child.tag in (ODF_P + "p", ODF_P + "h"):
                    cell.remove(child)
            value = office_formats.cell_value(change["value"])
            if value is None:
                continue
            if type(value) is bool:
                cell.set(ODF_O + "value-type", "boolean")
                cell.set(ODF_O + "boolean-value", "true" if value else "false")
            elif isinstance(value, (int, float)):
                cell.set(ODF_O + "value-type", "float")
                cell.set(ODF_O + "value", str(value))
            else:
                cell.set(ODF_O + "value-type", "string")
            etree.SubElement(cell, ODF_P + "p").text = str(value)
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as dst:
            for part in src.infolist():
                dst.writestr(part, etree.tostring(root, encoding="UTF-8", xml_declaration=True)
                             if part.filename == "content.xml" else src.read(part))
    return out.getvalue(), {"cells_changed": len(args["changes"])}


def _split_repeat(parent, tag, attr, position, *, extra=""):
    from copy import deepcopy

    remaining = position
    for node in parent.iterchildren():
        if node.tag not in (tag, extra):
            continue
        count = int(node.get(attr, "1"))
        if count < remaining:
            remaining -= count
            continue
        if remaining > 1:
            left = deepcopy(node)
            left.set(attr, str(remaining - 1))
            node.addprevious(left)
        if count > remaining:
            right = deepcopy(node)
            right.set(attr, str(count - remaining))
            node.addnext(right)
        node.attrib.pop(attr, None)
        return node
    if remaining > 1:
        etree.SubElement(parent, tag).set(attr, str(remaining - 1))
    return etree.SubElement(parent, tag)


def set_cells(raw, fmt, args):
    if fmt == "xlsx":
        return _xlsx_edit(raw, args)
    if fmt == "ods":
        return _ods_edit(raw, args)
    if fmt not in ("csv", "tsv"):
        raise ValueError("Изменение ячеек доступно для XLSX, ODS, CSV и TSV.")
    _, rows = _delimited_rows(raw, fmt)[0]
    for change, _, row, col in _changes(args.get("changes")):
        if "formula" in change or row > 1500 or col > 64:
            raise ValueError("В CSV и TSV можно менять значения в пределах 1500 строк и 64 столбцов.")
        while len(rows) < row:
            rows.append([])
        rows[row - 1].extend([""] * max(0, col - len(rows[row - 1])))
        value = office_formats.cell_value(change["value"])
        rows[row - 1][col - 1] = "" if value is None else str(value)
    from .documents import delimited
    return delimited([row or [""] for row in rows], fmt).encode("utf-8"), {"cells_changed": len(args["changes"])}


def pdf_pages(inputs, args):
    from pypdf import PdfWriter

    readers = {}
    for file_id, (raw, fmt, _) in inputs.items():
        if fmt != "pdf":
            raise ValueError("Для этой операции нужны файлы PDF.")
        readers[file_id] = _pdf_reader(raw)
    pages = args.get("pages")
    if pages is None:
        pages = [{"file_id": i} for i in readers]
    if not isinstance(pages, list) or not 1 <= len(pages) <= MAX_PDF_PAGES:
        raise ValueError("Укажи PDF и нужные диапазоны страниц.")
    writer = PdfWriter()
    for entry in pages:
        if not isinstance(entry, dict) or type(entry.get("file_id")) is not int or entry["file_id"] not in readers:
            raise ValueError("Укажи файлы из списка вложений.")
        pdf = readers[entry["file_id"]]
        start, end = _page_window(entry, len(pdf.pages))
        rotation = entry.get("rotate", 0)
        if type(rotation) is not int or rotation not in (0, 90, -90, 180, 270):
            raise ValueError("Доступные углы поворота: 90, 180 и 270 градусов.")
        if len(writer.pages) + end - start + 1 > MAX_PDF_PAGES:
            raise ValueError(f"В готовом PDF может быть до {MAX_PDF_PAGES} страниц.")
        before = len(writer.pages)
        writer.append(pdf, pages=(start - 1, end), import_outline=False)
        if rotation:
            for page in writer.pages[before:]:
                page.rotate(rotation)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue(), {"pages": len(writer.pages)}


def fill_pdf(raw, args):
    from pypdf import PdfWriter

    pdf = _pdf_reader(raw)
    fields = args.get("fields")
    available = pdf.get_fields() or {}
    if not available:
        raise ValueError("В PDF нет интерактивных полей формы.")
    if not isinstance(fields, list) or not 1 <= len(fields) <= 100:
        raise ValueError("Укажи названия и значения полей PDF.")
    values = {}
    for field in fields:
        if not isinstance(field, dict) or not isinstance(field.get("name"), str) or field["name"] not in available:
            raise ValueError("Поле PDF не найдено. Проверь точное название через просмотр файла.")
        known = available[field["name"]]
        if known.get("/FT") not in ("/Tx", "/Ch", "/Btn") or known.get("/Ff", 0) & 65536:
            raise ValueError("Поддерживаются текстовые поля, варианты и флажки PDF.")
        value = office_formats.checked_text(field.get("value"), 3000, blank=True)
        if known.get("/FT") == "/Btn":
            states = [str(state) for state in known.get("/_States_", [])]
            if value not in states:
                raise ValueError("Укажи один из вариантов флажка из описания формы.")
        values[field["name"]] = value
    writer = PdfWriter(clone_from=pdf)
    writer.update_page_form_field_values(None, values, auto_regenerate=False)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue(), {"fields_updated": len(values)}


def form_fields(raw):
    pdf = _pdf_reader(raw)
    return {"fields": [{"name": key, "type": str(field.get("/FT", "")),
                         "options": [str(x) for x in (field.get("/_States_") or field.get("/Opt") or [])[:20]]}
                       for key, field in list((pdf.get_fields() or {}).items())[:100]]}


def conversion_inputs(raw, fmt, target, args, *, path=None):
    if fmt in SPREADSHEETS and target in SPREADSHEETS:
        sheet = args.get("sheet")
        if fmt == "xlsx":
            sheets = list(_xlsx_rows(raw, sheet, formulas=False))
        elif fmt == "ods":
            sheets = list(_ods_rows(raw, sheet))
        else:
            sheets = _delimited_rows(raw, fmt)
        if not sheets:
            raise ValueError("Указанный лист не найден.")
        if target in ("csv", "tsv"):
            if len(sheets) > 1:
                raise ValueError("Для CSV или TSV укажи имя одного листа.")
            return {"rows": [["" if value is None else str(value) for value in row] for row in sheets[0][1]]}, True
        return {"sheets": [{"name": name, "rows": rows} for name, rows in sheets]}, True
    if target in SPREADSHEETS:
        raise ValueError("Для таблицы используй источник XLSX, ODS, CSV или TSV.")
    text, info = read(raw, fmt, args, path=path)
    if not text:
        raise ValueError("Не найден текст для преобразования.")
    if len(text) > MAX_TEXT or info.get("unread_pages"):
        raise ValueError("Выбери часть документа для преобразования.")
    if target == "json" and fmt != "json":
        text = json.dumps({"text": text}, ensure_ascii=False, indent=2)
    return {"content": text}, target in ("pdf", "docx", "odt", "pptx", "odp", "rtf", "html") or fmt not in ("txt", "md")
