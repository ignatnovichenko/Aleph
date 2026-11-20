"""Render editable documents and PDF from structured, model-authored content."""
from html import escape
import io
import math
from pathlib import Path
import re

from bs4 import BeautifulSoup, NavigableString
from markdown_it import MarkdownIt


MIMES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "odt": "application/vnd.oasis.opendocument.text",
    "ods": "application/vnd.oasis.opendocument.spreadsheet",
    "odp": "application/vnd.oasis.opendocument.presentation",
    "rtf": "application/rtf", "html": "text/html", "json": "application/json", "tsv": "text/tab-separated-values",
}
MARKDOWN = MarkdownIt("commonmark", {"html": False, "maxNesting": 12}).enable("table").disable("image")


def checked_text(value, limit=128_000, *, blank=False):
    if not isinstance(value, str) or len(value) > limit or (not blank and not value.strip()):
        raise ValueError("Проверь содержимое и размер документа.")
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff]", value):
        raise ValueError("В тексте есть неподдерживаемые управляющие символы.")
    return value


def cell_value(value):
    if value is None or type(value) is bool:
        return value
    if type(value) in (int, float):
        if not math.isfinite(value) or abs(value) >= 1e100:
            raise ValueError("Слишком большое значение ячейки.")
        return value
    return checked_text(value, 8192, blank=True)


def sheet_rows(rows):
    if not isinstance(rows, list) or not 1 <= len(rows) <= 1500:
        raise ValueError("Добавь от 1 до 1500 строк на лист.")
    for row in rows:
        if not isinstance(row, list) or not 1 <= len(row) <= 64:
            raise ValueError("В строке должно быть от 1 до 64 ячеек.")
        yield [cell_value(value) for value in row]


def workbook_data(args):
    sheets = args.get("sheets")
    if sheets is None:
        sheets = [{"name": "Данные", "rows": args.get("rows")}]
    if not isinstance(sheets, list) or not 1 <= len(sheets) <= 12:
        raise ValueError("Добавь от 1 до 12 листов.")
    names = set()
    for sheet in sheets:
        if not isinstance(sheet, dict):
            raise ValueError("Укажи название и строки каждого листа.")
        name = checked_text(sheet.get("name"), 31)
        if re.search(r"[\\/*?:\[\]]", name) or name.casefold() in names:
            raise ValueError("Выбери уникальные имена листов без специальных символов.")
        names.add(name.casefold())
        yield name, sheet_rows(sheet.get("rows"))


def set_cell(cell, value):
    cell.value = cell_value(value)
    if isinstance(value, str):
        # Text cells stay text; explicit formulas use the dedicated formula field.
        cell.data_type = "s"


def _markdown_nodes(text):
    return BeautifulSoup(MARKDOWN.render(text), "html.parser").contents


def _text(node):
    return node.get_text() if not isinstance(node, NavigableString) else str(node)


def _docx(text):
    from docx import Document
    from docx.shared import Inches, Pt

    document = Document()
    normal = document.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(11)
    normal.paragraph_format.space_after = Pt(7)

    def runs(paragraph, node, bold=False, italic=False, code=False):
        if isinstance(node, NavigableString):
            run = paragraph.add_run(str(node))
            run.bold = True if bold else None
            run.italic = True if italic else None
            if code:
                run.font.name = "Consolas"
            return
        if node.name == "br":
            paragraph.add_run().add_break()
            return
        for child in node.children:
            runs(paragraph, child, bold or node.name in ("b", "strong"), italic or node.name in ("i", "em"),
                 code or node.name == "code")

    def blocks(nodes, level=0):
        for node in nodes:
            if isinstance(node, NavigableString):
                continue
            tag = node.name
            if re.fullmatch(r"h[1-6]", tag):
                runs(document.add_heading(level=int(tag[1])), node)
            elif tag in ("ul", "ol"):
                style = "List Bullet" if tag == "ul" else "List Number"
                for item in node.find_all("li", recursive=False):
                    paragraph = document.add_paragraph(style=style)
                    if level:
                        paragraph.paragraph_format.left_indent = Inches(min(level, 5) * .25)
                    for child in item.children:
                        if getattr(child, "name", None) in ("ul", "ol"):
                            blocks([child], level + 1)
                        else:
                            runs(paragraph, child)
            elif tag == "table":
                rows = node.find_all("tr")
                if not rows:
                    continue
                table = document.add_table(rows=0, cols=max(len(r.find_all(["th", "td"], recursive=False)) for r in rows))
                table.style = "Light Shading Accent 1"
                for row in rows:
                    cells = table.add_row().cells
                    for cell, value in zip(cells, row.find_all(["th", "td"], recursive=False)):
                        runs(cell.paragraphs[0], value, bold=value.name == "th")
            elif tag == "blockquote":
                runs(document.add_paragraph(style="Quote"), node)
            elif tag == "hr":
                document.add_paragraph()
            elif tag == "pre":
                runs(document.add_paragraph(), node, code=True)
            else:
                runs(document.add_paragraph(), node)

    blocks(_markdown_nodes(text))
    out = io.BytesIO()
    document.save(out)
    return out.getvalue()


def _pdf(text):
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import ListFlowable, ListItem, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    fonts = Path("/usr/share/fonts/truetype/dejavu")
    try:
        for variant, filename in (("Aleph", "DejaVuSans.ttf"), ("AlephBold", "DejaVuSans-Bold.ttf"),
                                  ("AlephItalic", "DejaVuSans-Oblique.ttf"), ("AlephBoldItalic", "DejaVuSans-BoldOblique.ttf")):
            pdfmetrics.registerFont(TTFont(variant, str(fonts / filename)))
    except (OSError, ValueError) as error:
        raise ValueError("Для PDF установи шрифты DejaVu Sans на сервере.") from error
    pdfmetrics.registerFontFamily("Aleph", normal="Aleph", bold="AlephBold", italic="AlephItalic", boldItalic="AlephBoldItalic")
    normal = ParagraphStyle("body", fontName="Aleph", fontSize=10, leading=15, spaceAfter=7, textColor=colors.HexColor("#21252b"))
    table_text = ParagraphStyle("table", parent=normal, fontSize=8, leading=11, spaceAfter=0)
    quote = ParagraphStyle("quote", parent=normal, leftIndent=18, textColor=colors.HexColor("#475569"))

    def inline(node):
        if isinstance(node, NavigableString):
            return escape(str(node))
        if node.name == "br":
            return "<br/>"
        content = "".join(inline(child) for child in node.children)
        if node.name in ("b", "strong", "i", "em"):
            tag = "b" if node.name in ("b", "strong") else "i"
            return f"<{tag}>{content}</{tag}>"
        if node.name == "a":
            href = node.get("href", "")
            if href.startswith(("https://", "http://")):
                return f'<a href="{escape(href)}" color="#254f7c">{content}</a>'
        return content

    def blocks(nodes):
        for node in nodes:
            if isinstance(node, NavigableString):
                continue
            tag = node.name
            if re.fullmatch(r"h[1-6]", tag):
                size = max(12, 24 - 3 * (int(tag[1]) - 1))
                style = ParagraphStyle(tag, parent=normal, fontName="AlephBold", fontSize=size, leading=size + 5,
                                       spaceBefore=10, keepWithNext=True)
                yield Paragraph(inline(node), style)
            elif tag in ("ul", "ol"):
                items = []
                for item in node.find_all("li", recursive=False):
                    own = "".join(inline(child) for child in item.children if getattr(child, "name", None) not in ("ul", "ol"))
                    nested = list(blocks(child for child in item.children if getattr(child, "name", None) in ("ul", "ol")))
                    items.append(ListItem([Paragraph(own, normal), *nested]))
                if items:
                    yield ListFlowable(items, bulletType="bullet" if tag == "ul" else "1", bulletFontName="Aleph",
                                       leftIndent=18, spaceAfter=6)
            elif tag == "table":
                rows = [[Paragraph(inline(cell), table_text) for cell in row.find_all(["th", "td"])]
                        for row in node.find_all("tr")]
                if rows and rows[0]:
                    count = max(len(row) for row in rows)
                    table = Table([row + [""] * (count - len(row)) for row in rows], colWidths=[499 / count] * count,
                                  repeatRows=1, splitInRow=1, hAlign="LEFT")
                    table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e9edf4")),
                        ("LINEBELOW", (0, 0), (-1, -1), .35, colors.HexColor("#c9d2db")),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 6),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
                    yield table
                    yield Spacer(1, 8)
            elif tag == "hr":
                yield Spacer(1, 8)
            elif tag == "pre":
                yield Paragraph(escape(node.get_text()).replace("\n", "<br/>"), quote)
            else:
                yield Paragraph(inline(node), quote if tag == "blockquote" else normal)

    def footer(canvas, doc):
        canvas.setFont("Aleph", 8)
        canvas.setFillColor(colors.HexColor("#546171"))
        canvas.drawCentredString(A4[0] / 2, 25, str(doc.page))

    out = io.BytesIO()
    document = SimpleDocTemplate(out, pagesize=A4, leftMargin=42, rightMargin=42, topMargin=48, bottomMargin=46,
                                 title="Aleph")
    document.build(list(blocks(_markdown_nodes(text))) or [Paragraph(escape(text), normal)],
                   onFirstPage=footer, onLaterPages=footer)
    return out.getvalue()


def _xlsx(args):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    workbook = Workbook()
    workbook.remove(workbook.active)
    for name, rows in workbook_data(args):
        sheet = workbook.create_sheet(name)
        widths = {}
        for i, row in enumerate(rows, 1):
            for col, value in enumerate(row, 1):
                cell = sheet.cell(i, col)
                set_cell(cell, value)
                if i == 1:
                    cell.font = Font(bold=True, color="FFFFFF")
                    cell.fill = PatternFill("solid", fgColor="263951")
                width = min(54, max(11, len(str(value or "")) + 3))
                letter = cell.column_letter
                widths[letter] = max(widths.get(letter, 0), width)
        sheet.freeze_panes = "A2"
        for letter, width in widths.items():
            sheet.column_dimensions[letter].width = width
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def slides_data(args):
    slides = args.get("slides")
    if slides is None:
        text = checked_text(args.get("content"))
        slides = []
        for segment in re.split(r"(?m)(?=^#{1,3}\s+)", text):
            rows = [line.strip().lstrip("#-• ").strip() for line in segment.splitlines() if line.strip()]
            if rows:
                for start in range(1, max(2, len(rows)), 6):
                    slides.append({"title": rows[0], "bullets": rows[start:start+6]})
    if not isinstance(slides, list) or not 1 <= len(slides) <= 30:
        raise ValueError("Добавь от 1 до 30 слайдов.")
    for slide in slides:
        if not isinstance(slide, dict) or not isinstance(slide.get("bullets", []), list) or len(slide.get("bullets", [])) > 8:
            raise ValueError("Добавь заголовок и до восьми пунктов для каждого слайда.")
        yield checked_text(slide.get("title"), 180), [checked_text(b, 500) for b in slide.get("bullets", [])]


def _pptx(args):
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.util import Inches, Pt

    deck = Presentation()
    deck.slide_width, deck.slide_height = Inches(13.333), Inches(7.5)
    for title, bullets in slides_data(args):
        slide = deck.slides.add_slide(deck.slide_layouts[1])
        slide.background.fill.solid()
        slide.background.fill.fore_color.rgb = RGBColor.from_string("F6F4EF")
        heading = slide.shapes.title
        heading.left, heading.width = Inches(.8), Inches(11.73)
        heading.text = title
        heading.text_frame.paragraphs[0].font.color.rgb = RGBColor.from_string("263951")
        heading.text_frame.paragraphs[0].font.size = Pt(34)
        body_shape = slide.placeholders[1]
        body_shape.left, body_shape.width = Inches(.88), Inches(11.57)
        body = body_shape.text_frame
        body.word_wrap = True
        for number, bullet in enumerate(bullets):
            paragraph = body.paragraphs[0] if number == 0 else body.add_paragraph()
            paragraph.text = bullet
            paragraph.font.size = Pt(20 if len(bullets) < 6 else 17)
            paragraph.font.color.rgb = RGBColor.from_string("263951")
    output = io.BytesIO()
    deck.save(output)
    return output.getvalue()


def _odf(fmt, args):
    from odf import draw, style, table, text
    from odf.opendocument import OpenDocumentPresentation, OpenDocumentSpreadsheet, OpenDocumentText

    if fmt == "odt":
        document = OpenDocumentText()
        for node in _markdown_nodes(checked_text(args.get("content"))):
            if isinstance(node, NavigableString):
                continue
            if re.fullmatch(r"h[1-6]", node.name):
                document.text.addElement(text.H(outlinelevel=int(node.name[1]), text=node.get_text()))
            elif node.name == "table":
                data = table.Table()
                for item in node.find_all("tr"):
                    row = table.TableRow()
                    for entry in item.find_all(["th", "td"]):
                        cell = table.TableCell(valuetype="string")
                        cell.addElement(text.P(text=entry.get_text()))
                        row.addElement(cell)
                    data.addElement(row)
                document.text.addElement(data)
            elif node.name in ("ol", "ul"):
                values = text.List()
                for entry in node.find_all("li", recursive=False):
                    item = text.ListItem()
                    item.addElement(text.P(text=entry.get_text()))
                    values.addElement(item)
                document.text.addElement(values)
            else:
                document.text.addElement(text.P(text=node.get_text()))
    elif fmt == "ods":
        document = OpenDocumentSpreadsheet()
        for name, rows in workbook_data(args):
            sheet = table.Table(name=name)
            for values in rows:
                row = table.TableRow()
                for value in values:
                    if type(value) is bool:
                        cell = table.TableCell(valuetype="boolean", booleanvalue=value)
                    elif type(value) in (int, float):
                        cell = table.TableCell(valuetype="float", value=value)
                    else:
                        cell = table.TableCell(valuetype="string")
                    if value is not None:
                        cell.addElement(text.P(text=str(value)))
                    row.addElement(cell)
                sheet.addElement(row)
            document.spreadsheet.addElement(sheet)
    else:
        document = OpenDocumentPresentation()
        layout = style.PageLayout(name="Widescreen")
        layout.addElement(style.PageLayoutProperties(pagewidth="33.867cm", pageheight="19.05cm", printorientation="landscape"))
        document.automaticstyles.addElement(layout)
        master = style.MasterPage(name="Standard", pagelayoutname=layout)
        document.masterstyles.addElement(master)
        title_style = style.Style(name="AlephTitle", family="paragraph")
        title_style.addElement(style.TextProperties(fontsize="26pt", fontweight="bold", color="#263951"))
        document.styles.addElement(title_style)
        body_style = style.Style(name="AlephBody", family="paragraph")
        body_style.addElement(style.TextProperties(fontsize="17pt", color="#263951"))
        body_style.addElement(style.ParagraphProperties(marginbottom="0.35cm"))
        document.styles.addElement(body_style)
        for index, (title, bullets) in enumerate(slides_data(args), 1):
            page = draw.Page(name=f"Slide {index}", masterpagename=master)
            frame = draw.Frame(x="1.8cm", y="1.4cm", width="30cm", height="15cm")
            box = draw.TextBox()
            box.addElement(text.P(stylename=title_style, text=title))
            for bullet in bullets:
                box.addElement(text.P(stylename=body_style, text="• " + bullet))
            frame.addElement(box)
            page.addElement(frame)
            document.presentation.addElement(page)
    output = io.BytesIO()
    document.save(output)
    return output.getvalue()


def _rtf(text):
    # RTF signed UTF-16 units preserve Cyrillic and symbols in Word and LibreOffice.
    result = [r"{\rtf1\ansi\uc1\deff0{\fonttbl{\f0 Calibri;}}\f0\fs22 "]
    for char in text:
        if char in "\\{}":
            result.append("\\" + char)
        elif char == "\n":
            result.append("\\par\n")
        elif char == "\t":
            result.append(r"\tab ")
        elif char != "\r":
            if ord(char) < 128:
                result.append(char)
            else:
                raw = char.encode("utf-16-le")
                result.extend(r"\u" + str(int.from_bytes(raw[i:i+2], "little", signed=True)) + "?" for i in range(0, len(raw), 2))
    return ("".join(result) + "}").encode("ascii")


def render(fmt, args):
    if fmt == "xlsx":
        return _xlsx(args)
    if fmt == "pptx":
        return _pptx(args)
    if fmt in ("odt", "ods", "odp"):
        return _odf(fmt, args)
    body = checked_text(args.get("content"))
    if fmt == "docx":
        return _docx(body)
    if fmt == "pdf":
        return _pdf(body)
    if fmt == "rtf":
        return _rtf(body)
    if fmt == "html":
        markup = MARKDOWN.render(body)
        return ('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
                '<title>Aleph</title><main>' + markup + '</main>').encode()
    raise ValueError("Формат документа не поддерживается.")
