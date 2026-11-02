"""Offline, opt-in encryption of compact workspace SQLite files.

The original generation and a verified backup remain available for rollback.
This module does not activate encryption on any existing installation.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import shutil
import time
from pathlib import Path

from mempalace.fsio import atomic_write_json, fsync_directory
from mempalace.sqlite_crypto import (
    EncryptionError,
    cipher_connect,
    driver,
    protected_key,
    unwrap_key,
    validate_database_paths,
    wrap_key,
)

from .backup import (
    _backup_locked,
    _inventory,
    _is_sqlite,
    _verify_offline,
    data_lock,
    verify_backup,
)
from .workspace_store import (
    POINTER,
    POINTER_PREVIOUS,
    SETTINGS,
    generation_path,
    read_json,
    resolve_workspace,
    settings_for,
    workspace_root,
)


def _export(source, key):
    """SQLCipher export is used once for migration, never for regular backups."""
    sqlcipher = driver()
    target = source.with_name(source.name + ".encrypting")
    if target.exists():
        raise EncryptionError("Encryption staging file already exists")
    conn = sqlcipher.connect(str(source))
    try:
        conn.execute("PRAGMA temp_store=MEMORY")
        application_id = conn.execute("PRAGMA application_id").fetchone()[0]
        user_version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.execute(f"ATTACH DATABASE ? AS encrypted KEY \"x'{key.hex()}'\"", (str(target),))
        target.chmod(0o600)
        conn.execute("PRAGMA encrypted.cipher_page_size=16384")
        conn.execute("PRAGMA encrypted.cipher_hmac_algorithm=HMAC_SHA256")
        conn.execute("SELECT sqlcipher_export('encrypted')").fetchone()
        conn.execute(f"PRAGMA encrypted.application_id={application_id}")
        conn.execute(f"PRAGMA encrypted.user_version={user_version}")
        conn.commit()
        conn.execute("DETACH DATABASE encrypted")
    finally:
        conn.close()
    conn = cipher_connect(str(target), key)
    try:
        if conn.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise EncryptionError("Encrypted migration integrity check failed")
        if conn.execute("PRAGMA cipher_integrity_check").fetchall():
            raise EncryptionError("Encrypted migration page authentication failed")
    finally:
        conn.close()
    os.replace(target, source)


def encrypt_workspace(
    data_dir,
    workspace_id,
    *,
    backup_dir,
    key_store,
    server_key_file,
    recovery_key_file,
    authorize,
    checkpoint=lambda step: None,
):
    """Back up, export to a new generation, validate both keyslots, then publish.

    authorize resolves the workspace reference and enforces ordinary owner
    authorization while holding the offline lock. Wrapping keys are supplied by
    the caller; this function never generates or retains a recovery credential.
    """
    data = Path(data_dir).resolve()
    external = [Path(p).absolute() for p in (key_store, server_key_file, recovery_key_file)]
    backup_path = Path(backup_dir).resolve()
    for path in external:
        if path.resolve().is_relative_to(data) or path.resolve().is_relative_to(backup_path):
            raise EncryptionError(
                "Key store and wrapping keys must be separate from data and backup"
            )
    driver()
    server_key, recovery_key = protected_key(external[1]), protected_key(external[2])
    if hmac.compare_digest(server_key, recovery_key):
        raise EncryptionError("Server and recovery wrapping keys must be different")
    key_dir = external[0]
    if key_dir.is_symlink():
        raise EncryptionError("Key store cannot be a symlink")
    key_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt" and key_dir.stat().st_mode & 0o077:
        raise EncryptionError("Key store directory must be private")
    with data_lock(data, exclusive=True):
        workspace_id = authorize(workspace_id)
        root = workspace_root(data, workspace_id)
        source = resolve_workspace(root)
        settings = settings_for(source)
        if settings.get("encryption"):
            raise EncryptionError("Workspace already requests encryption")
        files = list(source.rglob("*")) if source.exists() else []
        if settings.get("store", "v1") != "compact" and any(
            path.suffix == ".sqlite3" for path in files
        ):
            raise EncryptionError("Switch an existing workspace to compact storage first")
        _verify_offline(data)
        record_path = key_dir / (workspace_id + ".json")
        if record_path.exists() or record_path.is_symlink():
            raise EncryptionError("Workspace already has keyslots; reconcile them before retrying")
        _backup_locked(data, backup_path)
        verify_backup(backup_path)
        checkpoint("backup")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        name = root.name + ".generation-" + secrets.token_hex(12)
        staging = generation_path(root, name)
        key = secrets.token_bytes(32)
        info = {
            "version": 1,
            "cipher": "sqlcipher",
            "workspace_id": workspace_id,
            "key_id": secrets.token_hex(16),
        }
        record = {
            **{name: info[name] for name in ("version", "workspace_id", "key_id")},
            "algorithm": "AES-256-GCM",
            "slots": {
                "server": wrap_key(key, server_key, info, "server"),
                "recovery": wrap_key(key, recovery_key, info, "recovery"),
            },
        }
        for slot, wrapping in (("server", server_key), ("recovery", recovery_key)):
            if not hmac.compare_digest(key, unwrap_key(record, wrapping, info, slot)):
                raise EncryptionError("Workspace keyslot verification failed")
        publishing = False
        slots_created = False
        try:
            saved = backup_path / source.relative_to(data)
            if saved.exists():
                shutil.copytree(saved, staging)
            else:
                staging.mkdir(mode=0o700)
            # Historical pointer/state files belong to the original root only.
            for filename in (
                POINTER,
                POINTER_PREVIOUS,
                ".store-backfill.json",
                ".store-comparison.json",
            ):
                (staging / filename).unlink(missing_ok=True)
            (staging / "palace" / ".store-comparison.json").unlink(missing_ok=True)
            staged_files, _ = _inventory(staging)
            databases = [path for path in staged_files if _is_sqlite(path)]
            if any(path.name == "chroma.sqlite3" for path in databases):
                raise EncryptionError("Chroma files cannot be encrypted by the compact store")
            # Capture names before in-place exports; publish only after all succeed.
            info["database_paths"] = sorted(
                path.relative_to(staging).as_posix() for path in databases
            )
            validate_database_paths(staging, info["database_paths"])
            for path in databases:
                _export(path, key)
            checkpoint("encrypted")
            atomic_write_json(
                staging / SETTINGS, {**settings, "store": "compact", "encryption": info}
            )
            # Exclusive create: a concurrent/provisioned slot record is never overwritten.
            fd = os.open(record_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            slots_created = True
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(record, stream)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            fsync_directory(key_dir)
            from .store_switch import _sync_tree

            _sync_tree(staging)
            _verify_offline(data)
            checkpoint("verified")
            previous = read_json(root / POINTER, {"active": root.name, "retained": []})
            pointer = {
                "active": name,
                "retained": previous.get("retained", [])
                + [{"name": source.name, "retired_at": time.time()}],
            }
            # A publish can replace the pointer and then raise during fsync.
            # From this point on, preserve both the generation and its slots.
            publishing = True
            atomic_write_json(root / POINTER, pointer, follow_symlinks=False)
            atomic_write_json(root / POINTER_PREVIOUS, pointer, follow_symlinks=False)
            return {"state": "encrypted", "databases": len(databases), "key_id": info["key_id"]}
        finally:
            if not publishing:
                shutil.rmtree(staging, ignore_errors=True)
                if slots_created:
                    record_path.unlink()
                    fsync_directory(key_dir)
