"""User-owned execution computers and the in-process queue for Hermes turns."""
import asyncio
from dataclasses import dataclass
import hashlib
import os
import re
import secrets
import time
from typing import Any

from . import projects

PAIR_TTL = 600
POLL_TIMEOUT = 12
ONLINE_TTL = 40
MAX_COMPUTERS = 8
RUNNER_PATH = "/runner/v1"
JOB_REQUEST_LIMIT = 24 * 1024 * 1024
_TOKEN = re.compile(r"[A-Za-z0-9_-]{24,64}")


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS computers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name TEXT NOT NULL, token_hash BLOB NOT NULL UNIQUE
        );
        CREATE INDEX IF NOT EXISTS computers_user ON computers(user_id,id);
        CREATE TABLE IF NOT EXISTS computer_pairings (
            user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            code_hash BLOB NOT NULL UNIQUE, name TEXT NOT NULL, expires REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS project_computers (
            project_id INTEGER PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
            computer_id INTEGER NOT NULL REFERENCES computers(id) ON DELETE CASCADE
        );
    """)


def _digest(value):
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        raise ValueError("Invalid computer credential")
    return hashlib.sha256(value.encode("ascii")).digest()


def prepare(store, uid, name):
    from .store import bounded_text

    if os.getenv("ALEPH_ENGINE", "hermes") != "hermes":
        raise ValueError("Для подключения компьютеров Алефу нужен движок Hermes.")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Введи имя компьютера.")
    name = " ".join(bounded_text(name, 48, required=True).split())
    if re.search(r"[\x00-\x1f\x7f-\x9f]", name):
        raise ValueError("Проверь имя компьютера.")
    if store.db.execute("SELECT count(*) FROM computers WHERE user_id=?", (uid,)).fetchone()[0] >= MAX_COMPUTERS:
        raise ValueError("Можно подключить до восьми компьютеров.")
    code = secrets.token_urlsafe(24)
    store.db.execute("DELETE FROM computer_pairings WHERE expires<=?", (time.time(),))
    store.db.execute("INSERT OR REPLACE INTO computer_pairings VALUES (?,?,?,?)",
                     (uid, _digest(code), name, time.time() + PAIR_TTL))
    return {"code": code, "expires_in": PAIR_TTL, "name": name}


def pair(store, code):
    db = store.db
    # Atomic even if two computers submit the same code simultaneously.
    db.execute("SAVEPOINT computer_pair")
    try:
        grant = db.execute("DELETE FROM computer_pairings WHERE code_hash=? AND expires>? RETURNING user_id,name",
                           (_digest(code), time.time())).fetchone()
        if not grant:
            raise ValueError("Invalid or expired pairing code")
        token = secrets.token_urlsafe(32)
        row = db.execute("INSERT INTO computers(user_id,name,token_hash) VALUES (?,?,?) RETURNING id,name",
                         (grant["user_id"], grant["name"], _digest(token))).fetchone()
    except BaseException:
        db.execute("ROLLBACK TO computer_pair")
        raise
    finally:
        db.execute("RELEASE computer_pair")
    return {**dict(row), "token": token}


def authenticate(store, token):
    row = store.db.execute("SELECT id FROM computers WHERE token_hash=?", (_digest(token),)).fetchone()
    if row is None:
        raise ValueError("Invalid computer credential")
    return row["id"]


def validate_session(value):
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        raise ValueError("Invalid runner session")


def list_owned(store, uid):
    return [{**dict(row), "online": store.computer_hub.online(row["id"])} for row in store.db.execute(
        "SELECT id,name FROM computers WHERE user_id=? ORDER BY id DESC", (uid,))]


def remove(store, uid, computer_id):
    if not store.db.execute("DELETE FROM computers WHERE id=? AND user_id=?", (computer_id, uid)).rowcount:
        raise KeyError("Компьютер не найден.")
    store.computer_hub.revoke(computer_id)


def assign(store, uid, project_id, computer_id):
    if not project_id or (computer_id is not None and (type(computer_id) is not int or not 0 < computer_id < 2**63)):
        raise ValueError("Выбери компьютер или сервер Алефа.")
    projects.get(store, uid, project_id)
    if computer_id is None:
        store.db.execute("DELETE FROM project_computers WHERE project_id=?", (project_id,))
    else:
        if os.getenv("ALEPH_ENGINE", "hermes") != "hermes":
            raise ValueError("Для запуска на компьютере Алефу нужен движок Hermes.")
        if not store.db.execute("SELECT 1 FROM computers WHERE id=? AND user_id=?", (computer_id, uid)).fetchone():
            raise KeyError("Компьютер не найден.")
        store.db.execute("INSERT OR REPLACE INTO project_computers VALUES (?,?)", (project_id, computer_id))
    return projects.get(store, uid, project_id)


class ComputerUnavailable(Exception):
    def __init__(self, name):
        self.name = name
        super().__init__("Project computer unavailable")


@dataclass
class Job:
    computer_id: int
    turn: dict
    broker: Any
    result: asyncio.Future
    claimed_by: str | None = None
    started: float | None = None
    last_seen: float = 0


class ComputerHub:
    def __init__(self):
        self._seen = {}
        self._jobs = {}
        self._waiters = {}

    def online(self, computer_id):
        return self._seen.get(computer_id, float("-inf")) > time.monotonic() - ONLINE_TTL

    def touch(self, computer_id):
        self._seen[computer_id] = time.monotonic()

    async def claim(self, computer_id, runner_session):
        # One waiting poll and one running turn per computer.
        if computer_id in self._waiters:
            return None
        event = self._waiters[computer_id] = asyncio.Event()
        try:
            while self.online(computer_id):
                jobs = [(key, job) for key, job in self._jobs.items()
                        if job.computer_id == computer_id and not job.result.done()]
                active = next(((key, job) for key, job in jobs if job.claimed_by), None)
                if jobs and (active is None or active[1].claimed_by == runner_session):
                    key, job = active or jobs[0]
                    job.claimed_by = runner_session
                    job.last_seen = time.monotonic()
                    if job.started is None:
                        job.started = job.last_seen
                    return {"id": key, "turn": job.turn}
                event.clear()
                try:
                    await asyncio.wait_for(event.wait(), POLL_TIMEOUT)
                except TimeoutError:
                    return None
            return None
        finally:
            self._waiters.pop(computer_id, None)

    def job(self, computer_id, runner_session, key):
        job = self._jobs.get(key)
        if job and job.computer_id == computer_id and job.claimed_by == runner_session and not job.result.done():
            job.last_seen = time.monotonic()
            return job
        return None

    def _wake(self, computer_id):
        if (event := self._waiters.get(computer_id)) is not None:
            event.set()

    def revoke(self, computer_id):
        self._seen.pop(computer_id, None)
        for job in self._jobs.values():
            if job.computer_id == computer_id and not job.result.done():
                job.result.set_result(None)
        self._wake(computer_id)

    async def run(self, computer_id, name, turn, broker, timeout):
        if not self.online(computer_id):
            raise ComputerUnavailable(name)
        key = secrets.token_urlsafe(24)
        result = asyncio.get_running_loop().create_future()
        job = self._jobs[key] = Job(computer_id, turn, broker, result)
        self._wake(computer_id)
        queued = time.monotonic()
        try:
            while self.online(computer_id):
                now = time.monotonic()
                if job.started is not None and now - job.last_seen >= ONLINE_TTL:
                    break
                remaining = (job.started if job.started is not None else queued) + timeout - now
                if remaining <= 0:
                    raise TimeoutError
                ready, _ = await asyncio.wait({result}, timeout=min(POLL_TIMEOUT, remaining))
                if ready:
                    if result.result() is not None:
                        return result.result()
                    break
            raise ComputerUnavailable(name)
        finally:
            self._jobs.pop(key, None)
            result.cancel()
            self._wake(computer_id)
