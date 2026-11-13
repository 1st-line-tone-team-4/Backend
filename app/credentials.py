"""Legacy encrypted credential vault, retained only to clean up old BYOK jobs.

New AI jobs use server-managed provider credentials and never write here. If
cleaning up jobs created by the former BYOK deployment, API and worker need the
same AES-256 key from a secret manager. Rows expire after at most fifteen
minutes; purge runs on every vault operation.
"""

from __future__ import annotations

import os
import base64
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import time
import uuid

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag

from .core import ApiError


def check_key(key: str | None) -> str:
    if (
        not isinstance(key, str)
        or not key.strip()
        or len(key) > 512
        or any(ord(c) < 33 or ord(c) > 126 for c in key)
    ):
        raise ApiError(503, "AI_NOT_CONFIGURED", "A valid service provider credential is required.")
    return key


class Credentials:
    def __init__(self, path: str | Path, encryption_key: str | bytes, *, ttl_seconds: int = 900):
        if not 1 <= ttl_seconds <= 900:
            raise ValueError("Credential TTL must be between 1 and 900 seconds")
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            raw = base64.b64decode(encryption_key, altchars=b"-_", validate=True)
            if len(raw) != 32:
                raise ValueError()
        except (ValueError, TypeError):
            raise ValueError("Credential encryption key must be base64-encoded 32 bytes") from None
        self.cipher = AESGCM(raw)
        self.ttl_seconds = ttl_seconds
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS credentials (ref TEXT PRIMARY KEY, ciphertext BLOB NOT NULL, expires_at REAL NOT NULL)"
            )
        if os.name != "nt":
            self.path.chmod(0o600)

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(str(self.path), timeout=5)
        try:
            with db:
                db.execute("PRAGMA secure_delete=ON")
                yield db
        finally:
            db.close()

    def purge(self):
        with self._connect() as db:
            db.execute("DELETE FROM credentials WHERE expires_at <= ?", (time.time(),))

    def put(self, key: str) -> str:
        check_key(key)
        ref = uuid.uuid4().hex
        expires = time.time() + self.ttl_seconds
        nonce = os.urandom(12)
        token = nonce + self.cipher.encrypt(nonce, key.encode(), f"{ref}:{expires}".encode())
        with self._connect() as db:
            db.execute("DELETE FROM credentials WHERE expires_at <= ?", (time.time(),))
            db.execute("INSERT INTO credentials VALUES (?,?,?)", (ref, token, expires))
        return ref

    def get(self, ref: str | None) -> str | None:
        if not ref:
            return None
        with self._connect() as db:
            db.execute("DELETE FROM credentials WHERE expires_at <= ?", (time.time(),))
            row = db.execute("SELECT ciphertext, expires_at FROM credentials WHERE ref=?", (ref,)).fetchone()
        if not row:
            return None
        try:
            return self.cipher.decrypt(row[0][:12], row[0][12:], f"{ref}:{row[1]}".encode()).decode()
        except (InvalidTag, ValueError, UnicodeDecodeError):
            self.delete(ref)
            return None

    def delete(self, ref: str | None):
        if ref:
            with self._connect() as db:
                db.execute("DELETE FROM credentials WHERE ref=? OR expires_at <= ?", (ref, time.time()))
