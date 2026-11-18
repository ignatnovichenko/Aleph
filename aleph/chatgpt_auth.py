"""OAuth and protected session storage for Sign in with ChatGPT."""
import base64
import hashlib
import json
import os
import secrets
import stat
import tempfile
import time
from pathlib import Path
from urllib.parse import urlencode
from uuid import uuid4

import aiohttp
import jwt

ISSUER = "https://auth.openai.com"
AUTHORIZE = ISSUER + "/api/accounts/authorize"
TOKEN = ISSUER + "/api/accounts/oauth/token"
JWKS = ISSUER + "/.well-known/jwks.json"
REVOCATION = ISSUER + "/api/accounts/oauth/revoke"
RESOURCE = "https://api.openai.com/v1"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
SCOPES = f"openid profile email offline_access resource.invoke {PLAN_SCOPE}"
AUTH_TIMEOUT = aiohttp.ClientTimeout(total=12)
INVALID_REFRESH = {"invalid_grant", "invalid_refresh_token", "token_expired", "refresh_token_expired",
                   "refresh_token_invalidated", "refresh_token_reused"}


class ChatGPTError(Exception):
    def __init__(self, status=0, code=""):
        self.status, self.code = status, code
        super().__init__("ChatGPT plan request failed")


def b64(value):
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def unb64(value):
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)


def directory():
    return Path(os.getenv("ALEPH_CHATGPT_DIR") or Path.home() / ".config/telegram-agent/chatgpt")


def _private_directory():
    path = directory()
    path.mkdir(mode=0o700, exist_ok=True, parents=True)
    details = path.lstat()
    if not stat.S_ISDIR(details.st_mode) or not _user_only(details):
        raise ValueError("The ChatGPT credential directory must be private to its owner (0700).")
    return path


def _user_only(details):
    return os.name != "posix" or (details.st_uid == os.getuid() and not details.st_mode & 0o077)


def read_record(name):
    path = directory() / name
    if not path.exists():
        return None
    root = _private_directory()
    details = path.lstat()
    if not stat.S_ISREG(details.st_mode) or details.st_size > 64_000 or not _user_only(details):
        raise ValueError("The ChatGPT credential file must be private to its owner (0600).")
    with (root / name).open(encoding="utf-8") as stream:
        data = json.load(stream)
    if not isinstance(data, dict):
        raise ValueError("Invalid ChatGPT credential file.")
    return data


def write_record(name, data):
    destination = _private_directory() / name
    with tempfile.NamedTemporaryFile(dir=destination.parent, mode="w", encoding="utf-8", delete=False) as stream:
        temp = Path(stream.name)
        try:
            json.dump(data, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
    try:
        os.replace(temp, destination)
    finally:
        temp.unlink(missing_ok=True)


def host_id():
    saved = read_record("host.json")
    if saved is None:
        saved = {"id": uuid4().urn}
        write_record("host.json", saved)
    if not isinstance(saved.get("id"), str):
        raise ValueError("Invalid ChatGPT host ID.")
    return saved["id"]


def authorize_url(redirect, state, nonce, verifier, registration=None, agent_host=None):
    params = {"client_id": "dynamic_agent_client", "response_type": "code", "redirect_uri": redirect,
              "resource": RESOURCE, "scope": SCOPES, "state": state, "nonce": nonce, "ext_agent_host_id": agent_host or host_id(),
              "code_challenge_method": "S256", "code_challenge": b64(hashlib.sha256(verifier.encode()).digest())}
    if registration:
        params["client_id"] = registration["client_id"]
        if registration.get("email"):
            params["login_hint"] = registration["email"]
        if PLAN_SCOPE not in registration.get("scopes", []):
            params["prompt"] = "consent"
    else:
        params["agent_name_hint"] = "Aleph"
    return AUTHORIZE + "?" + urlencode(params)


def grant_client(value):
    return isinstance(value, str) and 0 < len(value) <= 300 and value != "dynamic_agent_client"


def error_code(data):
    if not isinstance(data, dict):
        return ""
    error = data.get("error", data)
    if isinstance(error, dict):
        error = error.get("code") or error.get("type")
    return error if isinstance(error, str) else ""


async def request_json(session, method, url, **kwargs):
    try:
        async with session.request(method, url, allow_redirects=False, timeout=AUTH_TIMEOUT, **kwargs) as response:
            try:
                data = await response.json(content_type=None)
            except (ValueError, UnicodeError):
                data = None
            if response.status != 200:
                raise ChatGPTError(response.status, error_code(data))
            if not isinstance(data, dict):
                raise ChatGPTError(response.status)
            return data
    except (TimeoutError, aiohttp.ClientError) as exc:
        raise ChatGPTError() from exc


async def validate_id_token(session, token, client, nonce=None):
    keys = await request_json(session, "GET", JWKS)
    try:
        key = jwt.PyJWKSet.from_dict(keys)[jwt.get_unverified_header(token)["kid"]]
        claims = jwt.decode(token, key, algorithms=["RS256"], issuer=ISSUER, audience=client, leeway=30,
                            options={"require": ["iss", "aud", "sub", "exp"]})
        if nonce is not None and not secrets.compare_digest(str(claims.get("nonce")), nonce):
            raise ValueError
        if not isinstance(claims["sub"], str) or not claims["sub"]:
            raise ValueError
    except (jwt.PyJWTError, ValueError, TypeError, KeyError):
        raise ChatGPTError(401, "invalid_identity") from None
    return claims


def apply_tokens(record, data):
    access, refresh, duration = (data.get(name) for name in ("access_token", "refresh_token", "expires_in"))
    if not access or not refresh or not isinstance(access, str) or not isinstance(refresh, str):
        raise ChatGPTError(401, "invalid_token_response")
    if type(duration) is not int or duration <= 0 or str(data.get("token_type")).lower() != "bearer":
        raise ChatGPTError(401, "invalid_token_response")
    scopes = data["scope"].split() if isinstance(data.get("scope"), str) else record.get("scopes", [])
    if not {PLAN_SCOPE, "offline_access", "resource.invoke"}.issubset(scopes):
        raise ChatGPTError(403, "plan_not_granted")
    now = time.time()
    return {**record, "access_token": access, "refresh_token": refresh, "scopes": scopes,
            "expires_at": now + duration, "earliest_refresh_at": data.get("earliest_refresh_at"), "state": "connected"}


async def models_for(session, token):
    data = await request_json(session, "GET", RESOURCE + "/models", headers={"Authorization": "Bearer " + token})
    if not isinstance(data.get("models"), list):
        raise ChatGPTError(403, "missing_models")
    return [item for item in data["models"] if isinstance(item, dict) and item.get("visibility") == "list"
            and isinstance(item.get("slug"), str) and isinstance(item.get("display_name"), str)]


async def choose_model(session, record, preferred=""):
    models = await models_for(session, record["access_token"])
    selected = next((item for item in models if item["slug"] == preferred), None)
    if selected is None:
        selected = next((item for item in models if item["slug"] == "gpt-6.2-sol"), models[0] if models else None)
    if selected is None:
        raise ChatGPTError(403, "missing_models")
    return {**record, "model": selected["slug"], "model_name": selected["display_name"]}


def clear_tokens(record, state="disabled"):
    return {**{key: record[key] for key in ("client_id", "subject", "email", "scopes") if key in record}, "state": state}
