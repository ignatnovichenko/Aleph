"""File access explicitly granted on the paired computer to a single project."""

import hashlib
import os
from pathlib import Path
import stat
import tempfile

FORMATS = {"pdf", "docx", "xlsx", "pptx", "txt", "md", "csv", "tsv", "odt", "ods", "odp", "rtf"}
LIMIT = 16 * 1024**2


def filename(name):
    if (
        not isinstance(name, str)
        or not name
        or name.startswith(".")
        or "/" in name
        or "\\" in name
        or len(name.encode()) > 240
        or any(ord(c) < 32 for c in name)
        or Path(name).suffix[1:].lower() not in FORMATS
    ):
        raise ValueError("Выбери документ из рабочей папки.")
    return name


def list_files(root):
    with os.scandir(root) as entries:
        names = []
        for entry in entries:
            try:
                filename(entry.name)
            except ValueError:
                continue
            if entry.is_file(follow_symlinks=False) and entry.stat(follow_symlinks=False).st_size <= LIMIT:
                names.append(entry.name)
            if len(names) == 200:
                break
    return sorted(names)


def read(root, name):
    directory = os.open(root, os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fd = os.open(filename(name), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, "rb") as file:
            meta = os.fstat(fd)
            if not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1 or meta.st_size > LIMIT:
                raise ValueError("Файл недоступен или слишком большой.")
            raw = file.read(LIMIT + 1)
            if not 0 < len(raw) <= LIMIT:
                raise ValueError("Проверь размер файла.")
            return raw
    finally:
        os.close(directory)


def save(root, name, raw):
    filename(name)
    if not 0 < len(raw) <= LIMIT:
        raise ValueError("Проверь размер результата.")
    digest = hashlib.sha256(raw).hexdigest()[:12]
    with tempfile.NamedTemporaryFile(dir=root) as tmp:
        tmp.write(raw)
        tmp.flush()
        os.fsync(tmp.fileno())
        for target in (name, f"{Path(name).stem[:70]}-{digest}{Path(name).suffix}"):
            try:
                os.link(tmp.name, root / target)
                return target
            except FileExistsError:
                if not (root / target).is_symlink() and (root / target).is_file():
                    try:
                        if read(root, target) == raw:
                            return target
                    except (ValueError, OSError):
                        pass
    raise ValueError("Выбери новое имя результата.")
