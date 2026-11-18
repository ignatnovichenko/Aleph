"""Facts and preferences explicitly saved by each Telegram user."""

import sqlite3
import unicodedata


MAX_MEMORIES = 48
MAX_TEXT = 400


def initialize(db: sqlite3.Connection) -> None:
    db.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            text TEXT NOT NULL,
            normalized TEXT NOT NULL,
            UNIQUE(user_id, normalized)
        )
    """)


def list_memories(db: sqlite3.Connection, uid: int) -> list[dict]:
    rows = db.execute("SELECT id, text FROM memories WHERE user_id = ? ORDER BY id", (uid,))
    return [{"id": row[0], "text": row[1]} for row in rows]


def remember(db: sqlite3.Connection, uid: int, text: str) -> dict:
    if not isinstance(text, str) or len(text) > MAX_TEXT or "\x00" in text:
        raise ValueError(f"Одна заметка может содержать до {MAX_TEXT} символов.")
    text = unicodedata.normalize("NFC", " ".join(text.split()))
    if not text:
        raise ValueError("Что именно запомнить?")
    key = text.casefold()
    # One SQL statement enforces the cap for concurrent writers and keeps duplicates idempotent.
    row = db.execute("""
        INSERT INTO memories(user_id, text, normalized)
        SELECT ?, ?, ?
        WHERE EXISTS(SELECT 1 FROM memories WHERE user_id = ? AND normalized = ?)
           OR (SELECT count(*) FROM memories WHERE user_id = ?) < ?
        ON CONFLICT(user_id, normalized) DO UPDATE SET normalized = excluded.normalized
        RETURNING id, text
    """, (uid, text, key, uid, key, uid, MAX_MEMORIES)).fetchone()
    if row is None:
        raise ValueError("Память заполнена. Сначала попроси меня забыть одну из старых заметок.")
    return {"id": row[0], "text": row[1]}


def forget(db: sqlite3.Connection, uid: int, memory_id: int) -> dict:
    row = db.execute("DELETE FROM memories WHERE user_id = ? AND id = ? RETURNING id, text",
                     (uid, memory_id)).fetchone()
    if row is None:
        raise KeyError("Заметка не найдена.")
    return {"id": row[0], "text": row[1]}
