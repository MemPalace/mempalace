"""Backup, encryption and switch guards for the eval DB (class A).

The eval DB is created here directly with ``sqlite3``/``sqlite_crypto`` as
``<workspace>/evals/runs.sqlite3``, holding uncheckpointed WAL frames, so the
file runs without ``commonplace.evals``. Each test fails under a named
mutation of the base backup, encryption or switch code.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest

from commonplace.backup import BackupError, backup, restore, verify_backup
from commonplace.control_db import ControlDB
from commonplace.workspace_encryption import encrypt_workspace
from commonplace.workspace_store import workspace_path, workspace_root
from mempalace import sqlite_crypto

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")
RUNS = ["run_a", "run_b", "run_c"]


@pytest.fixture
def data(tmp_path, monkeypatch):
    data = tmp_path / "data"
    with closing(ControlDB(str(data / "control.sqlite3"))) as db:
        owner = db.create_user("owner@example.test", is_admin=True)
        wid = db.create_workspace("Synthetic", owner, personal=True)
    root = workspace_root(data, wid)
    root.mkdir(parents=True, mode=0o700)
    (root / "workspace.json").write_text('{"store":"compact"}')
    return data, owner, wid


@pytest.fixture
def keys(tmp_path, monkeypatch):
    pytest.importorskip("sqlcipher3")
    server, recovery = tmp_path / "server.key", tmp_path / "recovery.key"
    for path in (server, recovery):
        path.write_bytes(os.urandom(32))
        path.chmod(0o600)
    monkeypatch.setenv("COMMONPLACE_KEY_STORE", str(tmp_path / "slots"))
    monkeypatch.setenv("COMMONPLACE_WRAPPING_KEY_FILE", str(server))
    monkeypatch.delenv("COMMONPLACE_KEY_SLOT", raising=False)
    return server, recovery, tmp_path / "slots"


def encrypt(data, keys):
    data, owner, wid = data
    server, recovery, slots = keys

    def authorize(ref):
        with closing(ControlDB(str(data / "control.sqlite3"))) as db:
            row = db.workspace(ref)
            db.require_owner(row["id"], owner)
            return row["id"]

    return encrypt_workspace(
        data,
        wid,
        backup_dir=data.parent / "before-encryption",
        key_store=slots,
        server_key_file=server,
        recovery_key_file=recovery,
        authorize=authorize,
    )


def eval_db(directory, *, encrypted=False):
    """Three committed runs: the first checkpointed, two only in the WAL.

    Returns an open reader, which keeps the WAL from being checkpointed when
    the writer closes; the caller closes it.
    """
    evals = Path(directory) / "evals"
    evals.mkdir(mode=0o700, exist_ok=True)
    path = evals / "runs.sqlite3"
    opener = sqlite_crypto.connect if encrypted else sqlite3.connect
    writer = opener(str(path), isolation_level=None)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY, payload BLOB NOT NULL)")
    writer.execute("INSERT INTO runs VALUES (?, ?)", (RUNS[0], b"\x00verbatim"))
    writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    reader = opener(str(path))
    reader.execute("SELECT count(*) FROM runs").fetchone()
    for run in RUNS[1:]:
        writer.execute("INSERT INTO runs VALUES (?, ?)", (run, run.encode() * 100))
    writer.close()
    assert Path(str(path) + "-wal").stat().st_size > 0, "fixture: rows must sit in the WAL"
    return reader


def runs_in(path, *, encrypted=False):
    opener = sqlite_crypto.connect if encrypted else sqlite3.connect
    with closing(opener(str(path))) as conn:
        return sorted(row[0] for row in conn.execute("SELECT run_id FROM runs"))


def test_a8_encryption_conversion_keeps_uncheckpointed_eval_runs(data, keys):
    _, _, wid = data
    root = workspace_root(data[0], wid)
    reader = eval_db(root)
    try:
        encrypt(data, keys)
    finally:
        reader.close()
    active = workspace_path(data[0], wid)
    assert active != root
    converted = active / "evals" / "runs.sqlite3"
    assert converted.read_bytes()[:16] != b"SQLite format 3\x00"
    assert runs_in(converted, encrypted=True) == RUNS, "A8: committed WAL runs were lost"


def test_a8b_encrypted_backup_checkpoints_and_restores_every_run(data, keys, tmp_path):
    _, _, wid = data
    encrypt(data, keys)
    active = workspace_path(data[0], wid)
    reader = eval_db(active, encrypted=True)
    try:
        backup(data[0], tmp_path / "backup")
    finally:
        reader.close()
    restore(tmp_path / "backup", tmp_path / "restored")
    restored = workspace_path(tmp_path / "restored", wid) / "evals" / "runs.sqlite3"
    assert runs_in(restored, encrypted=True) == RUNS, "A8b: encrypted eval runs were lost"


def test_a9_plaintext_backup_restore_keeps_eval_wal_runs(data, tmp_path):
    _, _, wid = data
    reader = eval_db(workspace_root(data[0], wid))
    try:
        backup(data[0], tmp_path / "backup")
    finally:
        reader.close()
    restore(tmp_path / "backup", tmp_path / "restored")
    restored = workspace_path(tmp_path / "restored", wid) / "evals" / "runs.sqlite3"
    assert runs_in(restored) == RUNS, "A9: committed WAL runs were lost"


def test_a9_published_backup_with_eval_sidecar_refuses(data, tmp_path):
    _, _, wid = data
    eval_db(workspace_root(data[0], wid)).close()
    published = tmp_path / "backup"
    backup(data[0], published)
    name = f"workspaces/{wid}/evals/runs.sqlite3-wal"
    sidecar = published / name
    sidecar.write_bytes(b"stray wal")
    manifest = json.loads((published / "manifest.json").read_text())
    manifest["files"].append(
        {"path": name, "size": 9, "sha256": hashlib.sha256(b"stray wal").hexdigest()}
    )
    (published / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(BackupError, match="sidecars"):
        verify_backup(published)


def test_a10_online_backup_preflight_refuses_an_encrypted_eval_db(data, keys):
    from commonplace.backup_online import OnlineBackupError, _Cut

    _, _, wid = data
    encrypt(data, keys)
    eval_db(workspace_path(data[0], wid), encrypted=True).close()
    cut = _Cut.__new__(_Cut)
    cut.root = Path(data[0])
    tree = cut._walk()
    assert any(path.name == "runs.sqlite3" for path in tree.sizes), "fixture: eval DB enumerated"
    with pytest.raises(OnlineBackupError, match="encrypted"):
        cut._refuse_encrypted(tree)


def test_a11_switch_copy_keeps_plaintext_eval_wal_runs(data, tmp_path):
    from commonplace.store_switch import _copy_tree

    _, _, wid = data
    root = workspace_root(data[0], wid)
    reader = eval_db(root)
    try:
        _copy_tree(root, tmp_path / "staging")
    finally:
        reader.close()
    assert runs_in(tmp_path / "staging" / "evals" / "runs.sqlite3") == RUNS, (
        "A11: the switch copy lost committed WAL runs"
    )


def test_a11_switch_refuses_an_encrypted_workspace(data, keys):
    from commonplace.store_switch import switch_workspace

    _, _, wid = data
    encrypt(data, keys)
    eval_db(workspace_path(data[0], wid), encrypted=True).close()
    with pytest.raises(ValueError, match="Encrypted workspace maintenance"):
        switch_workspace(data[0], wid, "v1")
