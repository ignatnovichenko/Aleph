"""Schemas exposed to the model for document work."""
from .documents import MIMES


def _object(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


def _string(description):
    return {"type": "string", "description": description}


def _integer(description):
    return {"type": "integer", "description": description}


def _array(items, description=""):
    return {"type": "array", "items": items, "description": description}


def _tool(name, description, properties, required=()):
    return {"type": "function", "function": {
        "name": name, "description": description, "parameters": _object(properties, required),
    }}


FORMATS = {"type": "string", "enum": list(MIMES)}
FILENAME = _string("Descriptive output filename without directories. Use the desired file extension.")
FILE_ID = _integer("ID of a file from [Files for this request] or list_chat_files.")
CELL = {"type": ["string", "number", "boolean", "null"]}
ROWS = _array(_array(CELL), "A header and data rows. Use JSON numbers for numeric spreadsheet values.")
START_PAGE = _integer("First PDF page, 1-based, inclusive. Defaults to 1.")
END_PAGE = _integer("Last PDF page, 1-based, inclusive. Defaults to the last page.")
SHEET = _string("Exact worksheet name as shown by inspect_document. For CSV export from a multi-sheet workbook, choose one worksheet.")

CREATE = _tool("create_document",
    "Build and attach a new downloadable document when the user asks for a file. Fill in the actual content. For Word, PDF and ODT use Markdown for readable formatting.",
    {
        "filename": FILENAME, "format": FORMATS,
        "content": _string("Complete body. Markdown for DOCX, PDF, ODT, HTML, TXT or MD. Valid JSON for JSON. Plain text for RTF."),
        "rows": ROWS,
        "sheets": _array(_object({"name": _string("Worksheet name"), "rows": ROWS}, ["name", "rows"]),
                         "Named worksheets for XLSX or ODS. Use rows directly for a single worksheet."),
        "slides": _array(_object({"title": _string("Short slide title"), "bullets": _array(_string("Concise point"))}, ["title"]),
                         "Slides for PPTX or ODP, up to eight short bullets per slide."),
        "events": _array(_object({"title": _string("Event title"), "start": _string("ISO date or date-time with timezone"),
                                  "end": _string("Optional exclusive end"), "description": _string("Details")}, ["title", "start"]),
                         "Events for ICS calendar files."),
    }, ["filename", "format"])
LIST = _tool("list_chat_files", "List recent Telegram documents uploaded or generated in this workspace. Use only when the user refers to a file and it is not listed for the current request. Never guess IDs.", {})
INSPECT = _tool("inspect_document",
    "Read an attached or recent document by ID. Returns PDF page text or OCR, Word paragraphs, spreadsheet cell addresses and values, or slide text. A large document has next_offset. Set pdf_fields for fillable PDF metadata.",
    {"file_id": FILE_ID, "offset": _integer("Character offset from next_offset, default 0"),
     "start_page": START_PAGE, "end_page": END_PAGE, "sheet": SHEET, "pdf_fields": {"type": "boolean"}}, ["file_id"])
EDIT = _tool("edit_document",
    "Attach an edited copy of an existing file. replace_text edits exact matches in Word, slides, ODT and text; PDF, RTF and HTML rebuild the layout from extracted text. set_cells edits a spreadsheet. pdf_pages combines, extracts, removes, reorders or rotates pages. fill_pdf fills an interactive PDF form. convert exports into a new format and may reflow layout. Use only the file IDs the user means.",
    {
        "action": {"type": "string", "enum": ["replace_text", "set_cells", "pdf_pages", "fill_pdf", "convert"]},
        "file_ids": _array(FILE_ID, "Input files in requested order. Only pdf_pages can accept multiple files."),
        "filename": FILENAME,
        "replacements": _array(_object({"find": _string("Exact original text"), "replace": _string("Replacement, or empty to remove")}, ["find", "replace"]),
                               "All occurrences of each exact match are changed; use inspect_document if uncertain."),
        "changes": _array(_object({"sheet": SHEET, "cell": _string("A1 address such as B4"), "value": CELL,
                                   "formula": _string("An explicit XLSX formula such as =SUM(B2:B8). Use either formula or value.")}, ["cell"]),
                          "Cell edits. Set value to null to clear. Use plain string for text, JSON numbers for numeric values."),
        "pages": _array(_object({"file_id": FILE_ID, "start_page": START_PAGE, "end_page": END_PAGE,
                                 "rotate": _integer("Added clockwise rotation (0, 90, 180, 270).")}, ["file_id"]),
                        "Page ranges in desired output order. Omit pages to combine entire PDFs by file_ids order."),
        "fields": _array(_object({"name": _string("Exact PDF field name from inspect_document"), "value": _string("Field value")}, ["name", "value"])),
        "format": FORMATS, "sheet": SHEET, "start_page": START_PAGE, "end_page": END_PAGE,
    }, ["action", "file_ids", "filename"])

TOOLS = [CREATE, LIST, INSPECT, EDIT]
NAMES = {tool["function"]["name"] for tool in TOOLS}
