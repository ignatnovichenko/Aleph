"""Bound resource use while extracting text from a Telegram PDF in an isolated process."""
import io
import json
import resource
import sys

MAX_BYTES = 4 * 1024 * 1024
MAX_PAGES = 40
MAX_TEXT = 30_000


def main():
    resource.setrlimit(resource.RLIMIT_AS, (256 * 1024**2, 256 * 1024**2))
    resource.setrlimit(resource.RLIMIT_CPU, (6, 7))
    from pypdf import PdfReader

    data = sys.stdin.buffer.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES or not data.startswith(b"%PDF-"):
        return "invalid", ""
    reader = PdfReader(io.BytesIO(data), strict=False)
    if reader.is_encrypted:
        return "encrypted", ""
    if len(reader.pages) > MAX_PAGES:
        return "long", ""
    pages, length = [], 0
    for page in reader.pages:
        text = (page.extract_text() or "").strip()
        if text:
            length += len(text) + (2 if pages else 0)
            if length > MAX_TEXT:
                return "long", ""
            pages.append(text)
    if not pages:
        return "scanned", ""
    return "ok", "\n\n".join(pages)


if __name__ == "__main__":
    try:
        result = main()
    except Exception:
        result = ("invalid", "")
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode("utf-8", errors="replace"))
