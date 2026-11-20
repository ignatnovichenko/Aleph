"""Worker protocol: bounded JSON in, one result file and bounded metadata out."""
import json
from pathlib import Path
import resource
import re
import sys
import zipfile

# -I excludes the working directory (where user files live) from imports.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def run(packet):
    from aleph import documents, office_native, office_ops

    args = packet["arguments"]
    if not isinstance(args, dict):
        raise ValueError("Проверь параметры документа.")
    inputs = {}
    for item in packet["inputs"]:
        file_id, fmt = item["id"], item["format"]
        path = Path(f"input-{file_id}.{fmt}")
        raw = path.read_bytes()
        if not 0 < len(raw) <= documents.MAX_INPUT:
            raise ValueError("Файл пустой или превышает 16 МБ.")
        inputs[file_id] = (raw, fmt, path)
    operation = packet["operation"]
    if operation == "create":
        filename, mime, blob = documents.render_document(args)
        extra = {}
    else:
        if not inputs:
            raise ValueError("Пришли файл или ответь на сообщение с ним.")
        raw, fmt, path = next(iter(inputs.values()))
        original_format = fmt
        action = args.get("action")
        target = args.get("format") if action == "convert" else None
        if action == "convert" and (not isinstance(target, str) or target not in documents.MIMES or target in (fmt, "ics")):
            raise ValueError("Выбери другой поддерживаемый целевой формат.")
        native_output = office_native.convert(raw, fmt, target) if target else None
        if fmt in office_native.LEGACY and not native_output and action != "pdf_pages":
            fmt = office_native.LEGACY[fmt]
            raw = office_native.convert(raw, original_format, fmt)
            if not raw:
                raise ValueError("Не удалось открыть старый формат Office. Сохрани копию в DOCX, XLSX или PPTX.")
        if operation == "source":
            content, notes = office_ops.read(raw, fmt, {}, labeled=True, path=path)
            return {"text": content[:office_ops.MAX_TEXT], "truncated": len(content) > office_ops.MAX_TEXT or bool(notes.get("unread_pages"))}
        if operation == "inspect":
            if args.get("pdf_fields"):
                if fmt != "pdf":
                    raise ValueError("Поля формы доступны для PDF.")
                return office_ops.form_fields(raw)
            return office_ops.inspect(raw, fmt, args, path=path)
        if action == "pdf_pages":
            blob, extra = office_ops.pdf_pages(inputs, args)
            fmt = "pdf"
        else:
            if len(inputs) != 1:
                raise ValueError("Для этой операции выбери один файл.")
            if action == "replace_text":
                result = office_ops.replace_pdf_layout(raw, args) if fmt == "pdf" else None
                blob, extra = result or office_ops.replace_text(raw, fmt, args, path=path)
                if fmt == "htm":
                    fmt = "html"
            elif action == "set_cells":
                blob, extra = office_ops.set_cells(raw, fmt, args)
            elif action == "fill_pdf":
                if fmt != "pdf":
                    raise ValueError("Заполнение полей доступно для PDF.")
                blob, extra = office_ops.fill_pdf(raw, args)
            elif action == "convert":
                if native_output:
                    blob, extra = native_output, {"layout_rebuilt": False}
                else:
                    converted, rebuilt = office_ops.conversion_inputs(raw, fmt, office_native.LEGACY.get(target, target), args, path=path)
                    _, _, blob = documents.render_document({**converted, "format": target, "filename": args.get("filename")})
                    extra = {"layout_rebuilt": rebuilt}
                fmt = target
            else:
                raise ValueError("Укажи операцию с документом.")
            if action != "convert" and original_format in office_native.LEGACY:
                blob = office_native.convert(blob, fmt, original_format)
                if not blob:
                    raise ValueError("Не удалось сохранить правки в старом формате Office. Преобразуй его в новый формат.")
                fmt = original_format
        filename = documents.document_name(args.get("filename"), fmt)
        mime = documents.MIMES[fmt]
    if not 0 < len(blob) <= documents.MAX_OUTPUT:
        raise ValueError("Готовый документ превышает 16 МБ или пуст.")
    Path("result.bin").write_bytes(blob)
    return {"filename": filename, "mime": mime, **extra}


if __name__ == "__main__":
    resource.setrlimit(resource.RLIMIT_AS, (1536 * 1024**2, 4096 * 1024**2))
    resource.setrlimit(resource.RLIMIT_CPU, (24, 26))
    resource.setrlimit(resource.RLIMIT_FSIZE, (48 * 1024**2, 48 * 1024**2))
    try:
        result = run(json.loads(sys.stdin.buffer.read(1024 * 1024 + 1)))
    except (OSError, zipfile.BadZipFile, KeyError, TypeError, OverflowError, UnicodeError):
        result = {"error": "Не удалось прочитать документ. Попробуй сохранить его заново."}
    except ValueError as exc:
        reason = str(exc)
        # Tools can surface expected, user-facing validation messages.
        result = {"error": reason[:360] if re.match(r"[А-ЯЁ]", reason)
                  else "Проверь формат и содержимое документа."}
    except Exception:
        result = {"error": "Не удалось обработать документ. Попробуй сохранить его заново."}
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=True).encode())
