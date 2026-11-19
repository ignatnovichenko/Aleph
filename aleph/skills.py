"""Private instruction library, project membership, and public SKILL.md imports."""
import re
import sqlite3
import unicodedata
from urllib.parse import quote, unquote, urlsplit

import aiohttp
import yaml

from . import projects

MAX_FILE = 48 * 1024
MAX_INSTRUCTIONS = 12000
MAX_LIBRARY = 50
MAX_ENABLED = 12
FIELDS = "id,name,description,source_url"
REPO_PART = re.compile(r"[A-Za-z0-9_.-]{1,100}")
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ud800-\udfff]")


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS skills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name TEXT NOT NULL, name_key TEXT NOT NULL, description TEXT NOT NULL,
            instructions TEXT NOT NULL, source_url TEXT NOT NULL DEFAULT '',
            UNIQUE(user_id, name_key)
        );
        CREATE TABLE IF NOT EXISTS project_skills (
            project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
            skill_id INTEGER NOT NULL REFERENCES skills(id) ON DELETE CASCADE
        );
        CREATE UNIQUE INDEX IF NOT EXISTS project_skill_membership
            ON project_skills(COALESCE(project_id, 0), skill_id);
        CREATE INDEX IF NOT EXISTS project_skill_scope ON project_skills(project_id, skill_id);
        CREATE INDEX IF NOT EXISTS project_skill_removal ON project_skills(skill_id);
    """)


def _text(value, limit, label):
    if not isinstance(value, str) or len(value) > limit or CONTROL.search(value):
        raise ValueError(f"{label}. Максимум {limit} символов.")
    value = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not value:
        raise ValueError(label)
    return value


def _draft(name, description, instructions, source_url=""):
    return {
        "name": " ".join(_text(name, 64, "Введи название скилла").split()),
        "description": " ".join(_text(description, 1024, "Опиши, когда применять скилл").split()),
        "instructions": _text(instructions, MAX_INSTRUCTIONS, "Добавь инструкции скилла"),
        "source_url": source_url,
    }


def library(store, uid):
    return [dict(row) for row in store.db.execute(
        f"SELECT {FIELDS} FROM skills WHERE user_id=? ORDER BY name COLLATE NOCASE, id", (uid,)
    )]


def detail(store, uid, skill_id):
    row = store.db.execute(f"SELECT {FIELDS},instructions FROM skills WHERE user_id=? AND id=?",
                           (uid, skill_id)).fetchone()
    if row is None:
        raise KeyError("Скилл не найден.")
    return dict(row)


def save(store, uid, fields, skill_id=None):
    if fields.keys() - {"name", "description", "instructions", "source_url"}:
        raise ValueError("Проверь поля скилла.")
    old = detail(store, uid, skill_id) if skill_id is not None else None
    source = old["source_url"] if old else fields.get("source_url", "")
    if source != "":
        source, _ = github_source(source)
    draft = _draft(fields.get("name"), fields.get("description"), fields.get("instructions"), source)
    name_key = unicodedata.normalize("NFC", draft["name"]).casefold()
    values = (draft["name"], name_key, draft["description"], draft["instructions"], source)
    try:
        if old:
            store.db.execute(
                "UPDATE skills SET name=?,name_key=?,description=?,instructions=?,source_url=? WHERE user_id=? AND id=?",
                (*values, uid, skill_id),
            )
        else:
            row = store.db.execute("""
                INSERT INTO skills(user_id,name,name_key,description,instructions,source_url)
                SELECT ?,?,?,?,?,? WHERE (SELECT COUNT(*) FROM skills WHERE user_id=?) < ?
                RETURNING id
            """, (uid, *values, uid, MAX_LIBRARY)).fetchone()
            if row is None:
                raise ValueError(f"В библиотеке помещается до {MAX_LIBRARY} скиллов.")
            skill_id = row["id"]
    except sqlite3.IntegrityError as error:
        if error.sqlite_errorcode != sqlite3.SQLITE_CONSTRAINT_UNIQUE:
            raise
        raise ValueError("Скилл с таким названием уже есть.") from None
    return {"id": skill_id, **draft}


def remove(store, uid, skill_id):
    if not store.db.execute("DELETE FROM skills WHERE user_id=? AND id=?", (uid, skill_id)).rowcount:
        raise KeyError("Скилл не найден.")


def for_project(store, uid, project_id):
    projects.get(store, uid, project_id)
    rows = store.db.execute("""
        SELECT s.id,s.name,s.description FROM skills s JOIN project_skills p ON p.skill_id=s.id
        WHERE s.user_id=? AND p.project_id IS ? ORDER BY s.id
    """, (uid, project_id or None))
    return [dict(row) for row in rows]


def set_project(store, uid, project_id, selected):
    projects.get(store, uid, project_id)
    if (not isinstance(selected, list) or len(selected) > MAX_ENABLED
            or any(type(item) is not int or not 0 < item < 2**63 for item in selected)
            or len(set(selected)) != len(selected)):
        raise ValueError(f"Выбери до {MAX_ENABLED} скиллов для проекта.")
    if not set(selected).issubset(item["id"] for item in library(store, uid)):
        raise KeyError("Один из выбранных скиллов недоступен.")
    scope = project_id or None
    db = store.db
    db.execute("SAVEPOINT project_skill_selection")
    try:
        db.execute("""DELETE FROM project_skills WHERE project_id IS ?
                      AND skill_id IN (SELECT id FROM skills WHERE user_id=?)""", (scope, uid))
        db.executemany("INSERT INTO project_skills(project_id,skill_id) VALUES(?,?)",
                       ((scope, item) for item in selected))
    except BaseException:
        db.execute("ROLLBACK TO project_skill_selection")
        raise
    finally:
        db.execute("RELEASE project_skill_selection")
    return selected


def load(store, uid, project_id, skill_id):
    if type(skill_id) is not int or not 0 < skill_id < 2**63:
        raise ValueError("Некорректный скилл.")
    row = store.db.execute("""
        SELECT s.name,s.instructions FROM skills s JOIN project_skills p ON p.skill_id=s.id
        WHERE s.user_id=? AND p.project_id IS ? AND s.id=?
    """, (uid, project_id or None, skill_id)).fetchone()
    if row is None:
        raise KeyError("Скилл не включён в этом проекте.")
    return dict(row)


def github_source(value):
    """Return a display URL and an upstream URL built from verified path segments."""
    invalid = ValueError("Вставь ссылку на публичный SKILL.md, его папку или репозиторий GitHub.")
    if not isinstance(value, str) or len(value) > 2048 or CONTROL.search(value) or re.search(r"[\r\n\t]", value):
        raise invalid
    try:
        url = urlsplit(value.strip())
        parts = [unquote(part, errors="strict") for part in url.path.strip("/").split("/")]
    except (ValueError, UnicodeError):
        raise invalid from None
    host = url.netloc.lower()
    if (url.scheme != "https" or host not in {"github.com", "raw.githubusercontent.com"}
            or len(parts) < 2 or not all(REPO_PART.fullmatch(part) for part in parts[:2])
            or any(not part.strip() or part.strip() in {".", ".."} or re.search(r"[/\\\r\n\t?#%]", part) or CONTROL.search(part)
                   for part in parts)):
        raise invalid
    path = "/".join(quote(part, safe="") for part in parts)
    display = f"https://{host}/{path}"
    if host == "raw.githubusercontent.com":
        if len(parts) < 4 or parts[-1] != "SKILL.md":
            raise invalid
        return display, display
    if len(parts) == 2:
        return display, f"https://api.github.com/repos/{path}/contents/SKILL.md"
    if len(parts) < 4 or parts[2] not in {"blob", "tree"}:
        raise invalid
    if parts[2] == "blob" and (len(parts) < 5 or parts[-1] != "SKILL.md"):
        raise invalid
    tail = "/".join(quote(part, safe="") for part in parts[3:])
    suffix = "/SKILL.md" if parts[2] == "tree" else ""
    return display, f"https://raw.githubusercontent.com/{parts[0]}/{parts[1]}/{tail}{suffix}"


def parse_markdown(text, source_url=""):
    error = ValueError("SKILL.md должен содержать название (name), описание (description) и инструкции.")
    if not isinstance(text, str) or len(text) > MAX_FILE:
        raise error
    try:
        if len(text.encode()) > MAX_FILE:
            raise error
    except UnicodeError:
        raise error from None
    text = text.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    match = re.match(r"\A---[ \t]*\n(.{1,8192}?)\n---[ \t]*(?:\n|\Z)", text, re.DOTALL)
    if not match:
        raise error
    try:
        # SKILL.md metadata is text. BaseLoader also preserves YAML 1.1 words
        # such as "on" and "yes" as strings without constructing typed objects.
        metadata = yaml.load(match[1], Loader=yaml.BaseLoader)
    except (yaml.YAMLError, RecursionError):
        raise error from None
    if not isinstance(metadata, dict):
        raise error
    return _draft(metadata.get("name"), metadata.get("description"), text[match.end():], source_url)


async def preview_github(session, url):
    source, upstream = github_source(url)
    try:
        async with session.get(upstream, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=10),
                               headers={"Accept": "application/vnd.github.raw+json",
                                        "User-Agent": "AlephAgent/1.0"}) as response:
            if response.status == 404:
                raise ValueError("SKILL.md не найден. Проверь доступ к репозиторию и путь к папке скилла.")
            if response.status in (403, 429):
                raise ValueError("GitHub ограничил запросы. Попробуй позже или загрузи SKILL.md файлом.")
            if response.status != 200:
                raise ValueError("Не удалось открыть SKILL.md на GitHub.")
            raw = bytearray()
            async for chunk in response.content.iter_chunked(4096):
                raw.extend(chunk)
                if len(raw) > MAX_FILE:
                    raise ValueError("Файл слишком большой. Размер SKILL.md должен быть до 48 КБ.")
        text = raw.decode("utf-8")
    except (aiohttp.ClientError, TimeoutError, UnicodeError):
        raise ValueError("Не удалось прочитать SKILL.md. Загрузи текстовый файл в UTF-8 или попробуй снова.") from None
    return parse_markdown(text, source)
