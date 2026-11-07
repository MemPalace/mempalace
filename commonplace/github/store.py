"""Encrypted per-person grants, one-use flows, and durable generation fences."""

from __future__ import annotations

import json
import os
from pathlib import Path

from mempalace.sqlite_crypto import EncryptionError, protected_key

from ..social.providers import SignInError

SCHEMA = """
CREATE TABLE IF NOT EXISTS github_worker_lease (
 id INTEGER PRIMARY KEY CHECK(id=1), owner TEXT NOT NULL, expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS github_lifecycle (
 user_id TEXT PRIMARY KEY REFERENCES users(id), generation INTEGER NOT NULL DEFAULT 0,
 disconnect_pending INTEGER NOT NULL DEFAULT 0, deleting INTEGER NOT NULL DEFAULT 0,
 active_exchanges INTEGER NOT NULL DEFAULT 0, unconfirmed INTEGER NOT NULL DEFAULT 0,
 remote_work INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS github_keys (
 user_id TEXT PRIMARY KEY REFERENCES users(id), version INTEGER NOT NULL, wrapped BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS github_connect_flows (
 state_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
 session_hash TEXT NOT NULL, browser_hash TEXT NOT NULL, workspace_id TEXT NOT NULL,
 card INTEGER NOT NULL, generation INTEGER NOT NULL, expires_at REAL NOT NULL,
 secret BLOB NOT NULL, purpose TEXT NOT NULL DEFAULT 'connect', next TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS github_one_flow ON github_connect_flows(user_id);
CREATE TABLE IF NOT EXISTS github_confirmations (
 user_id TEXT PRIMARY KEY REFERENCES users(id), session_hash TEXT NOT NULL,
 workspace_id TEXT NOT NULL, card INTEGER NOT NULL, generation INTEGER NOT NULL,
 expires_at REAL NOT NULL, secret BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS github_grants (
 user_id TEXT PRIMARY KEY REFERENCES users(id), workspace_id TEXT NOT NULL,
 subject TEXT NOT NULL UNIQUE, login TEXT NOT NULL, expires_at REAL NOT NULL,
 secret BLOB NOT NULL, disabled INTEGER NOT NULL DEFAULT 0
);
"""


def aad(uid, kind):
    return json.dumps(["github", 1, uid, kind], separators=(",", ":")).encode()


def seal(key, uid, kind, raw):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(12)
    return nonce + AESGCM(key).encrypt(nonce, raw, aad(uid, kind))


def open_secret(key, uid, kind, raw):
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    try:
        return AESGCM(key).decrypt(raw[:12], raw[12:], aad(uid, kind))
    except (InvalidTag, ValueError, TypeError):
        raise SignInError("github_key") from None


class Store:
    def __init__(self, db):
        self.db = db
        with db._lock:
            db._conn.executescript(SCHEMA)
            columns = {row[1] for row in db._conn.execute("PRAGMA table_info(github_lifecycle)")}
            if "remote_work" not in columns:
                db._conn.execute(
                    "ALTER TABLE github_lifecycle ADD COLUMN remote_work INTEGER NOT NULL DEFAULT 0"
                )

    def key(self, conn, uid):
        try:
            path = Path(os.environ["COMMONPLACE_GITHUB_KEY_FILE"])
            # The key must be outside the complete control/workspace/backup data tree.
            if path.resolve().is_relative_to(Path(self.db.path).resolve().parent):
                raise ValueError
            if path.stat().st_uid != os.getuid():
                raise ValueError
            wrapping = protected_key(path)
            row = conn.execute("SELECT * FROM github_keys WHERE user_id=?", (uid,)).fetchone()
            if row:
                if row["version"] != 1:
                    raise ValueError
                return open_secret(wrapping, uid, "dek", row["wrapped"])
            key = os.urandom(32)
            conn.execute(
                "INSERT INTO github_keys VALUES (?,1,?)", (uid, seal(wrapping, uid, "dek", key))
            )
            return key
        except (OSError, KeyError, ValueError, EncryptionError, ImportError):
            raise SignInError("github_key") from None

    def encrypt(self, conn, uid, kind, value):
        return seal(self.key(conn, uid), uid, kind, json.dumps(value).encode())

    def decrypt(self, conn, uid, kind, value):
        try:
            return json.loads(open_secret(self.key(conn, uid), uid, kind, value))
        except (ValueError, UnicodeError):
            raise SignInError("github_key") from None

    def lifecycle(self, conn, uid):
        conn.execute("INSERT OR IGNORE INTO github_lifecycle(user_id) VALUES (?)", (uid,))
        return conn.execute("SELECT * FROM github_lifecycle WHERE user_id=?", (uid,)).fetchone()
