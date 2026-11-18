"""One-use encrypted transfer of a locally obtained ChatGPT session to its private VM."""
from dataclasses import dataclass
import hashlib
import json
import os
import secrets
import time
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.exceptions import InvalidTag

from .chatgpt_auth import b64, unb64

LIFETIME = 600
IMPORT_PATH = "/api/chatgpt/import"
MAX_PACKET = 64_000


def owner(uid):
    value = os.getenv("ALEPH_CHATGPT_OWNER_ID", "").strip()
    return value.isdecimal() and str(uid) == value


def _cipher(private, public, secret):
    shared = private.exchange(X25519PublicKey.from_public_bytes(unb64(public)))
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=secret, info=b"Aleph ChatGPT session transfer v1").derive(shared)
    return AESGCM(key)


@dataclass
class Pairing:
    uid: int
    secret: bytes
    private: X25519PrivateKey
    expires: float

    @property
    def id(self):
        return hashlib.sha256(self.secret).hexdigest()

    def code(self, url, vm_host):
        parts = urlsplit(url)
        endpoint = urlunsplit((parts.scheme, parts.netloc, IMPORT_PATH, "", ""))
        return b64(json.dumps({"endpoint": endpoint, "key": b64(self.private.public_key().public_bytes_raw()),
                               "secret": b64(self.secret), "vm_host": vm_host}, separators=(",", ":")).encode())

    def decrypt(self, packet):
        cipher = _cipher(self.private, packet["key"], self.secret)
        data = cipher.decrypt(unb64(packet["nonce"]), unb64(packet["ciphertext"]), self.id.encode())
        return json.loads(data)


class PairingStore:
    def __init__(self):
        self.pending = None

    def create(self, uid, url, vm_host):
        if not owner(uid) or not url:
            raise ValueError("ChatGPT connection is unavailable.")
        self.pending = Pairing(uid, secrets.token_bytes(32), X25519PrivateKey.generate(), time.monotonic() + LIFETIME)
        return {"code": self.pending.code(url, vm_host), "expires_in": LIFETIME}

    def consume(self, packet):
        pending = self.pending
        if not pending or not owner(pending.uid) or pending.expires < time.monotonic():
            self.pending = None
            raise ValueError("Pairing code expired. Create a new code in Aleph.")
        try:
            if not secrets.compare_digest(pending.id, packet["id"]):
                raise ValueError
            data = pending.decrypt(packet)
            if not isinstance(data, dict):
                raise ValueError
        except (InvalidTag, UnicodeError, ValueError, TypeError, KeyError):
            raise ValueError("Invalid pairing packet.") from None
        self.pending = None
        return pending.uid, data

    def cancel(self, uid):
        if self.pending and self.pending.uid == uid:
            self.pending = None


def pairing_info(code):
    try:
        if len(code) > 2000:
            raise ValueError
        config = json.loads(unb64(code))
        endpoint = config["endpoint"]
        parsed = urlsplit(endpoint)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError
        if parsed.path != IMPORT_PATH or parsed.query or parsed.fragment:
            raise ValueError
        secret = unb64(config["secret"])
        if len(secret) != 32 or len(unb64(config["key"])) != 32 or UUID(config["vm_host"]).urn != config["vm_host"]:
            raise ValueError
        return config
    except (AttributeError, ValueError, TypeError, KeyError, UnicodeError):
        raise ValueError("Invalid Aleph pairing code.") from None


def encrypt_for_vm(code, data):
    config = pairing_info(code)
    try:
        secret = unb64(config["secret"])
        private = X25519PrivateKey.generate()
        cipher = _cipher(private, config["key"], secret)
        nonce = secrets.token_bytes(12)
        identifier = hashlib.sha256(secret).hexdigest()
        encoded = json.dumps(data, ensure_ascii=False).encode()
        ciphertext = cipher.encrypt(nonce, encoded, identifier.encode())
        return config["endpoint"], {"id": identifier, "key": b64(private.public_key().public_bytes_raw()),
                                    "nonce": b64(nonce), "ciphertext": b64(ciphertext)}
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise ValueError("Invalid Aleph pairing code.") from None
