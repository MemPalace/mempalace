"""Opt-in SQLCipher connections; a requested cipher never falls back to SQLite.

Workspace settings contain only a key id. Wrapped keys and wrapping keys live
outside the workspace/data tree, supplied by the embedding application.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import sys
from pathlib import Path, PureWindowsPath
from urllib.parse import unquote, urlsplit


class EncryptionError(ValueError):
    """Encryption configuration, dependency or key validation failed."""


def driver():
    try:
        import sqlcipher3
    except ImportError:
        raise EncryptionError(
            "SQLCipher requested but sqlcipher3 is unavailable; install the sqlcipher extra "
            "on supported Linux systems, or a SQLCipher-linked sqlcipher3 driver"
        ) from None
    return sqlcipher3


def database_path(database, *, uri=False):
    value = os.fspath(database)
    if value == ":memory:" or (uri and value.startswith("file::memory:")):
        return None
    if uri and value.startswith("file:"):
        parsed = urlsplit(value)
        if parsed.netloc not in ("", "localhost"):
            raise EncryptionError("Remote SQLite file URIs are unsupported")
        value = unquote(parsed.path)
    return Path(value).absolute()


def validate_database_paths(directory, paths):
    """Validate the operator-owned inventory relative to a workspace generation."""
    if not isinstance(paths, list):
        raise EncryptionError("Invalid workspace encryption database paths")
    root = Path(directory).resolve()
    seen = set()
    for name in paths:
        if (
            not isinstance(name, str)
            or not name
            or "\\" in name
            or "\x00" in name
            or Path(name).is_absolute()
            or PureWindowsPath(name).drive
            or any(part in ("", ".", "..") for part in name.split("/"))
            or Path(name).name == "workspace.json"
            or name.endswith(("-wal", "-shm", "-journal"))
            or name in seen
            or not (root / name).resolve().is_relative_to(root)
        ):
            raise EncryptionError("Invalid workspace encryption database paths")
        seen.add(name)


def _require_v1(info):
    """B3 has no production unlock path; never consult operator-held keys."""
    if not isinstance(info, dict):
        raise EncryptionError("Invalid workspace encryption metadata")
    if "key_mode" in info or info.get("version", 1) != 1:
        raise EncryptionError("Unsupported workspace key mode; B3 remains locked")


def encryption_settings(path):
    """Find the nearest workspace settings, rejecting malformed opt-in metadata."""
    if path is None:
        return None
    for directory in Path(path).absolute().parents:
        config = directory / "workspace.json"
        try:
            if not config.exists():
                continue
            settings = json.loads(config.read_text(encoding="utf-8"))
            if not isinstance(settings, dict):
                raise ValueError
            if "encryption" not in settings:
                return None
            info = settings["encryption"]
            if (
                not isinstance(info, dict)
                or info.get("version") != 1
                or info.get("cipher") != "sqlcipher"
                or not isinstance(info.get("workspace_id"), str)
                or not info["workspace_id"].startswith("ws_")
                or not info["workspace_id"][3:].isalnum()
                or directory.parent.name != "workspaces"
                or directory.name.split(".generation-", 1)[0] != info["workspace_id"]
                or len(info.get("key_id", "")) != 32
                or any(c not in "0123456789abcdef" for c in info["key_id"])
                or config.is_symlink()
                or not Path(path).resolve().is_relative_to(directory.resolve())
            ):
                raise ValueError
            validate_database_paths(directory, info.get("database_paths"))
        except (OSError, ValueError, TypeError, KeyError):
            raise EncryptionError("Invalid workspace encryption settings") from None
        _require_v1(info)
        return directory, info
    return None


def is_workspace_database(path, directory, info):
    """Classify a path using validated settings, without opening keys or databases.

    Known SQLite extensions are reserved inside encrypted workspaces, including
    future stores. Other names must be listed relative to this exact generation.
    """
    relative = Path(path).resolve().relative_to(Path(directory).resolve()).as_posix()
    return (
        Path(relative).suffix in (".sqlite3", ".sqlite", ".db")
        or relative in info["database_paths"]
    )


def protected_key(path):
    """Read an operator-provisioned random 256-bit wrapping key, never a password."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            mode = os.fstat(stream.fileno()).st_mode
            if not stat.S_ISREG(mode) or (os.name != "nt" and mode & 0o077):
                raise EncryptionError("Wrapping key must be a private regular file")
            value = stream.read(33)
    except OSError:
        raise EncryptionError("Wrapping key file is unavailable") from None
    if len(value) != 32:
        raise EncryptionError("Wrapping key must contain exactly 32 random bytes")
    return value


def _aad(info, slot):
    return json.dumps(
        ["commonplace-keyslot", 1, info["workspace_id"], info["key_id"], slot],
        separators=(",", ":"),
    ).encode("ascii")


def wrap_key(key, wrapping_key, info, slot):
    _require_v1(info)
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(12)
    return {
        "nonce": nonce.hex(),
        "ciphertext": AESGCM(wrapping_key).encrypt(nonce, key, _aad(info, slot)).hex(),
    }


def unwrap_key(record, wrapping_key, info, slot):
    _require_v1(info)
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    try:
        if (
            record["version"] != 1
            or record["algorithm"] != "AES-256-GCM"
            or any(record[name] != info[name] for name in ("workspace_id", "key_id"))
        ):
            raise ValueError
        wrapped = record["slots"][slot]
        nonce = bytes.fromhex(wrapped["nonce"])
        ciphertext = bytes.fromhex(wrapped["ciphertext"])
        if len(nonce) != 12 or len(ciphertext) != 48:
            raise ValueError
        key = AESGCM(wrapping_key).decrypt(nonce, ciphertext, _aad(info, slot))
        if len(key) != 32:
            raise ValueError
        return key
    except (InvalidTag, ValueError, TypeError, KeyError):
        raise EncryptionError("Workspace keyslot could not be unlocked") from None


def workspace_key(directory, info):
    _require_v1(info)
    key_store = os.environ.get("COMMONPLACE_KEY_STORE")
    wrapping_file = os.environ.get("COMMONPLACE_WRAPPING_KEY_FILE")
    slot = os.environ.get("COMMONPLACE_KEY_SLOT", "server")
    if not key_store or not wrapping_file or slot not in ("server", "recovery"):
        raise EncryptionError("Encrypted workspace requires a key store and wrapping key file")
    # Workspace generations are siblings directly below data/workspaces.
    data_root = Path(directory).parent.parent.resolve()
    for external in (Path(key_store), Path(wrapping_file)):
        if external.resolve().is_relative_to(data_root):
            raise EncryptionError("Key store and wrapping keys must be outside the data directory")
    path = Path(key_store) / (info["workspace_id"] + ".json")
    try:
        if path.is_symlink():
            raise EncryptionError("Keyslot file cannot be a symlink")
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise EncryptionError("Workspace keyslot is unavailable") from None
    return unwrap_key(record, protected_key(wrapping_file), info, slot)


def cipher_connect(database, key, **kwargs):
    if not isinstance(key, bytes) or len(key) != 32:
        raise EncryptionError("SQLCipher requires a 256-bit raw data key")
    sqlcipher = driver()
    conn = sqlcipher.connect(database, **kwargs)
    try:
        if not conn.execute("PRAGMA cipher_version").fetchone():
            raise EncryptionError("sqlcipher3 driver has no SQLCipher support")
        conn.execute(f"PRAGMA key = \"x'{key.hex()}'\"")
        conn.execute("PRAGMA cipher_page_size=16384")
        conn.execute("PRAGMA cipher_hmac_algorithm=HMAC_SHA256")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        conn.execute("PRAGMA cache_size=-65536")
        expected = {
            "cipher_page_size": "16384",
            "cipher_hmac_algorithm": "HMAC_SHA256",
            "cache_size": "-65536",
            "temp_store": "2",
        }
        for name, value in expected.items():
            if str(conn.execute(f"PRAGMA {name}").fetchone()[0]) != value:
                raise EncryptionError("SQLCipher did not accept the required settings")
        return conn
    except BaseException as exc:
        conn.close()
        if isinstance(exc, sqlcipher.Error):
            raise EncryptionError("Encrypted database could not be opened with this key") from None
        raise


def connect(database, **kwargs):
    path = database_path(database, uri=kwargs.get("uri", False))
    settings = encryption_settings(path)
    if settings is None:
        return sqlite3.connect(database, **kwargs)
    directory, info = settings
    if not is_workspace_database(path, directory, info):
        raise EncryptionError("Unlisted database path in encrypted workspace")
    # Resolve dependencies and unwrap before opening/creating any database file.
    driver()
    return cipher_connect(database, workspace_key(directory, info), **kwargs)


def row_factory(cursor, row):
    """The two DB-API extensions require their own Row implementation."""
    module = driver() if type(cursor).__module__.startswith("sqlcipher3") else sqlite3
    return module.Row(cursor, row)


def integrity_error_type(connection):
    """Return the connection driver's specific constraint exception class."""
    module = driver() if type(connection).__module__.startswith("sqlcipher3") else sqlite3
    return module.IntegrityError


def operational_error_type(connection):
    """Return the driver's operational exception, including a failed open.

    With no connection yet, SQLCipher can only have raised after its driver
    was loaded. Do not require that optional dependency for plain SQLite.
    """
    if connection is None:
        module = sys.modules.get("sqlcipher3", sqlite3)
    else:
        module = driver() if type(connection).__module__.startswith("sqlcipher3") else sqlite3
    return module.OperationalError
