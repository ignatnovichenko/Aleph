"""OAuth sessions for the four external services, scoped to a Telegram identity."""
import asyncio
import base64
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import secrets
import time
from urllib.parse import urlencode, urlsplit

import aiohttp
from cryptography.fernet import Fernet, InvalidToken

GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
NOTION_AUTH = "https://api.notion.com/v1/oauth/authorize"
NOTION_TOKEN = "https://api.notion.com/v1/oauth/token"
GOOGLE_APIS = "https://www.googleapis.com"
NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2026-03-11"
GOOGLE_SCOPES = {
    "calendar": ("calendar.events.owned.readonly", "calendar.app.created"),
    "drive": ("drive.file",),
    "gmail": ("gmail.readonly", "gmail.compose"),
}
PROVIDERS = ("calendar", "notion", "drive", "gmail")
NAMES = {"calendar": "Google Calendar", "notion": "Notion", "drive": "Google Drive", "gmail": "Gmail"}


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS connections (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            provider TEXT NOT NULL,
            credentials TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'connected',
            calendar_id TEXT,
            PRIMARY KEY(user_id,provider)
        );
        CREATE TABLE IF NOT EXISTS connection_requests (
            hash TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            provider TEXT NOT NULL,
            expires REAL NOT NULL,
            verifier TEXT NOT NULL,
            claimed INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS connection_actions (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            project_id INTEGER NOT NULL DEFAULT 0,
            update_id INTEGER NOT NULL,
            provider TEXT NOT NULL,
            action TEXT NOT NULL,
            payload TEXT NOT NULL,
            expires REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            request_key TEXT NOT NULL UNIQUE
        );
        CREATE INDEX IF NOT EXISTS connection_actions_update ON connection_actions(user_id,update_id,status);
    """)


def setting(name):
    value = os.getenv(name, "").strip()
    path = os.getenv(name + "_FILE", "").strip()
    if not value and path:
        value = Path(path).read_text().strip()
    return value


class ConnectionError(ValueError):
    def __init__(self, message, status=0):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Config:
    origin: str
    client_id: str
    secret: str
    cipher: Fernet

    def callback(self, provider):
        return f"{self.origin}/oauth/{provider}/callback"


def config(provider):
    if provider not in PROVIDERS:
        raise KeyError("Подключение не найдено.")
    origin = os.getenv("ALEPH_INTEGRATIONS_ORIGIN", "").rstrip("/")
    prefix = "ALEPH_NOTION" if provider == "notion" else "ALEPH_GOOGLE"
    try:
        parsed = urlsplit(origin)
        cipher_key = setting("ALEPH_INTEGRATIONS_KEY")
        client_id, secret = setting(prefix + "_CLIENT_ID"), setting(prefix + "_CLIENT_SECRET")
        if (parsed.scheme == "https" and parsed.hostname and not parsed.path and not parsed.query
                and not parsed.fragment and not parsed.username and cipher_key and client_id and secret):
            return Config(origin, client_id, secret, Fernet(cipher_key.encode()))
    except (ValueError, OSError):
        return None
    return None


def configured(provider):
    result = config(provider)
    if not result:
        raise ConnectionError(f"{NAMES[provider]} ещё не настроен на сервере.")
    return result


def statuses(store, uid):
    states = {row["provider"]: row["state"] for row in store.db.execute(
        "SELECT provider,state FROM connections WHERE user_id=?", (uid,))}
    return {p: states.get(p, "disconnected") if config(p) else "unavailable" for p in PROVIDERS}


def start(store, uid, provider):
    settings = configured(provider)
    now = time.time()
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64) if provider != "notion" else ""
    key = hashlib.sha256(state.encode()).hexdigest()
    store.db.execute("DELETE FROM connection_requests WHERE expires<=? OR (user_id=? AND provider=?)",
                     (now, uid, provider))
    store.db.execute("INSERT INTO connection_requests(hash,user_id,provider,expires,verifier) VALUES(?,?,?,?,?)",
                     (key, uid, provider, now + 600, verifier))
    base = {"client_id": settings.client_id, "redirect_uri": settings.callback(provider),
            "state": state, "response_type": "code"}
    if provider == "notion":
        return NOTION_AUTH + "?" + urlencode({**base, "owner": "user"})
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    scopes = " ".join("https://www.googleapis.com/auth/" + s for s in GOOGLE_SCOPES[provider])
    return GOOGLE_AUTH + "?" + urlencode({
        **base, "scope": scopes, "access_type": "offline", "prompt": "select_account consent",
        "code_challenge": challenge, "code_challenge_method": "S256",
    })


async def request(session, method, url, *, headers=None, as_text=False, **kwargs):
    try:
        async with session.request(method, url, headers=headers, allow_redirects=False,
                                   timeout=aiohttp.ClientTimeout(total=25), **kwargs) as response:
            if response.status >= 300:
                raise ConnectionError("Внешний сервис отклонил запрос.", response.status)
            body = bytearray()
            async for chunk in response.content.iter_chunked(16384):
                body.extend(chunk)
                if len(body) > 2_000_000:
                    raise ConnectionError("Ответ сервиса слишком большой.")
            return body.decode("utf-8", errors="replace") if as_text else json.loads(body) if body else {}
    except (ValueError, UnicodeError) as error:
        if isinstance(error, ConnectionError):
            raise
        raise ConnectionError("Ответ сервиса не удалось прочитать.") from None
    except (aiohttp.ClientError, TimeoutError):
        raise ConnectionError("Внешний сервис временно недоступен.") from None


async def exchange(session, settings, provider, fields):
    if provider == "notion":
        return await request(session, "POST", NOTION_TOKEN, json=fields,
                             auth=aiohttp.BasicAuth(settings.client_id, settings.secret),
                             headers={"Notion-Version": NOTION_VERSION})
    return await request(session, "POST", GOOGLE_TOKEN, data={
        **fields, "client_id": settings.client_id, "client_secret": settings.secret,
    })


def check_scopes(provider, tokens):
    if provider not in GOOGLE_SCOPES:
        return
    expected = {"https://www.googleapis.com/auth/" + scope for scope in GOOGLE_SCOPES[provider]}
    actual = set(tokens.get("scope", "").split())
    if actual and (not expected <= actual or provider == "drive" and actual != expected):
        raise ConnectionError("Google выдал другой набор разрешений. Подключи сервис заново.")


def pack(settings, data, previous=None):
    token = data.get("access_token")
    refresh = data.get("refresh_token") or (previous or {}).get("refresh_token")
    if not isinstance(token, str) or not token or not isinstance(refresh, str) or not refresh:
        raise ConnectionError("Сервис не выдал долгосрочный доступ. Подключи аккаунт заново.")
    duration = data.get("expires_in", 0)
    expiry = time.time() + duration if type(duration) is int and duration > 0 else 0
    return settings.cipher.encrypt(json.dumps({
        "access_token": token, "refresh_token": refresh, "expires": expiry,
    }).encode()).decode()


async def finish(store, session, provider, state, code):
    settings = configured(provider)
    if not 30 <= len(state) <= 90 or not 1 <= len(code) <= 4096:
        raise ConnectionError("Ссылка истекла. Начни подключение из Алефа заново.")
    key = hashlib.sha256(state.encode()).hexdigest()
    row = store.db.execute("""
        UPDATE connection_requests SET claimed=1
        WHERE hash=? AND provider=? AND expires>? AND claimed=0 RETURNING user_id,verifier
    """, (key, provider, time.time())).fetchone()
    if not row:
        raise ConnectionError("Ссылка истекла. Начни подключение из Алефа заново.")
    fields = {"grant_type": "authorization_code", "code": code, "redirect_uri": settings.callback(provider)}
    if row["verifier"]:
        fields["code_verifier"] = row["verifier"]
    try:
        tokens = await exchange(session, settings, provider, fields)
        check_scopes(provider, tokens)
        credentials = pack(settings, tokens)
        if not store.db.execute("DELETE FROM connection_requests WHERE hash=?", (key,)).rowcount:
            raise ConnectionError("Подключение отменено.")
        store.db.execute("""INSERT INTO connections(user_id,provider,credentials) VALUES(?,?,?)
            ON CONFLICT(user_id,provider) DO UPDATE SET credentials=excluded.credentials,state='connected',calendar_id=NULL
        """, (row["user_id"], provider, credentials))
        store.db.execute("DELETE FROM connection_actions WHERE user_id=? AND provider=?", (row["user_id"], provider))
    finally:
        store.db.execute("DELETE FROM connection_requests WHERE hash=?", (key,))


def disconnect(store, uid, provider):
    if provider not in PROVIDERS:
        raise KeyError("Подключение не найдено.")
    for table in ("connections", "connection_requests", "connection_actions"):
        store.db.execute(f"DELETE FROM {table} WHERE user_id=? AND provider=?", (uid, provider))
    if provider == "drive":
        store.db.execute("DELETE FROM connection_requests WHERE user_id=? AND provider='drive-picker'", (uid,))


async def access(store, session, uid, provider, *, rejected=None):
    settings = configured(provider)
    if not hasattr(store, "connection_lock"):
        store.connection_lock = asyncio.Lock()
    async with store.connection_lock:
        row = store.db.execute("SELECT credentials,state FROM connections WHERE user_id=? AND provider=?",
                               (uid, provider)).fetchone()
        if not row or row["state"] != "connected":
            raise ConnectionError(f"Подключи {NAMES[provider]} в настройках Алефа.")
        try:
            data = json.loads(settings.cipher.decrypt(row["credentials"].encode()))
        except (ValueError, InvalidToken):
            raise ConnectionError("Подключи аккаунт заново.") from None
        token = data["access_token"]
        if token != rejected and (not data["expires"] or data["expires"] > time.time() + 60):
            return token
        try:
            new = await exchange(session, settings, provider, {
                "grant_type": "refresh_token", "refresh_token": data["refresh_token"],
            })
            check_scopes(provider, new)
            credentials = pack(settings, new, data)
        except ConnectionError as error:
            if error.status in (400, 401, 403):
                store.db.execute("UPDATE connections SET state='reconnect' WHERE user_id=? AND provider=? AND credentials=?",
                                 (uid, provider, row["credentials"]))
                raise ConnectionError(f"Подключи {NAMES[provider]} заново.") from None
            raise
        updated = store.db.execute("""UPDATE connections SET credentials=?
            WHERE user_id=? AND provider=? AND credentials=?
        """, (credentials, uid, provider, row["credentials"])).rowcount
        if not updated:
            raise ConnectionError("Подключение изменилось. Повтори запрос.")
        return new["access_token"]


async def call(store, session, uid, provider, method, path, **kwargs):
    token = await access(store, session, uid, provider)
    base = NOTION_API if provider == "notion" else GOOGLE_APIS
    headers = {"Authorization": f"Bearer {token}"}
    if provider == "notion":
        headers["Notion-Version"] = NOTION_VERSION
    try:
        return await request(session, method, base + path, headers=headers, **kwargs)
    except ConnectionError as error:
        if error.status != 401:
            raise
    headers["Authorization"] = "Bearer " + await access(store, session, uid, provider, rejected=token)
    return await request(session, method, base + path, headers=headers, **kwargs)
