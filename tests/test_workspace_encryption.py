"""Synthetic owner/workspace checks against the real optional SQLCipher driver."""

import json
import os
import sqlite3
import sys
from contextlib import ExitStack, closing
from pathlib import Path

import pytest

from commonplace.backup import BackupError, backup, restore, verify_backup
from commonplace.cli import main
from commonplace.control_db import ControlDB
from commonplace.handoff.models import ThreadMessage
from commonplace.workspace_encryption import encrypt_workspace
from commonplace.workspace_store import POINTER, open_threads, workspace_path, workspace_root
from mempalace import sqlite_crypto as crypto


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    pytest.importorskip("sqlcipher3")
    data = tmp_path / "data"
    with closing(ControlDB(str(data / "control.sqlite3"))) as db:
        owner = db.create_user("owner@example.test")
        wid = db.create_workspace("Synthetic workspace", owner)
    server = tmp_path / "server.key"
    recovery = tmp_path / "recovery.key"
    for path in (server, recovery):
        path.write_bytes(os.urandom(32))
        path.chmod(0o600)
    slots = tmp_path / "slots"
    monkeypatch.setenv("COMMONPLACE_KEY_STORE", str(slots))
    monkeypatch.setenv("COMMONPLACE_WRAPPING_KEY_FILE", str(server))
    monkeypatch.delenv("COMMONPLACE_KEY_SLOT", raising=False)
    return data, owner, wid, server, recovery, slots


@pytest.fixture
def encryption_metadata(tmp_path):
    directory = tmp_path / "workspaces" / ("ws_synthetic.generation-" + "a" * 24)
    directory.mkdir(parents=True)
    info = {
        "version": 1,
        "cipher": "sqlcipher",
        "workspace_id": "ws_synthetic",
        "key_id": "a" * 32,
        "database_paths": [],
    }
    config = directory / "workspace.json"
    config.write_text(json.dumps({"encryption": info}))
    return directory, config, info


def enable(workspace, *, checkpoint=lambda step: None):
    data, owner, wid, server, recovery, slots = workspace

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
        checkpoint=checkpoint,
    )


def store_message(path, wid, text="verbatim snowman ☃\x00"):
    with closing(open_threads(path)) as store:
        thread = store.store(
            wid,
            "synthetic-source",
            [ThreadMessage(0, "user", "message", text, "raw:" + text)],
            actor="person:synthetic",
        )
        return thread.id


def test_fresh_non_admin_owner_cli_encrypted_reopen(workspace):
    data, owner, wid, server, recovery, slots = workspace
    assert (
        main(
            [
                "--data",
                str(data),
                "workspace",
                "encrypt",
                wid,
                "--by",
                owner,
                "--backup-dir",
                str(data.parent / "before-encryption"),
                "--key-store",
                str(slots),
                "--server-key-file",
                str(server),
                "--recovery-key-file",
                str(recovery),
            ]
        )
        == 0
    )
    active = workspace_path(data, wid)
    assert crypto.encryption_settings(active / "threads.v2.sqlite3")[1]["database_paths"] == []
    thread = store_message(active, wid)
    with closing(open_threads(active)) as store:
        assert store.messages(thread)[0].text == "verbatim snowman ☃\x00"
        assert store.messages(thread)[0].raw == "raw:verbatim snowman ☃\x00"
        assert store.search([thread], "snowman")[0].stored_thread_id == thread
        for pragma, value in (
            ("cipher_page_size", "16384"),
            ("cipher_hmac_algorithm", "HMAC_SHA256"),
            ("cache_size", "-65536"),
            ("temp_store", "2"),
        ):
            assert str(store._conn.execute("PRAGMA " + pragma).fetchone()[0]) == value
    path = active / "threads.v2.sqlite3"
    assert not path.read_bytes().startswith(b"SQLite format 3\x00")
    with closing(sqlite3.connect(path)) as plain, pytest.raises(sqlite3.DatabaseError):
        plain.execute("SELECT * FROM sqlite_master").fetchall()
    record = json.loads((slots / (wid + ".json")).read_text())
    assert set(record["slots"]) == {"server", "recovery"}
    assert str(server) not in json.dumps(record)
    assert not (data / "slots").exists()


def test_migration_preserves_compact_threads_boards_rules_graph_and_palace(workspace):
    from commonplace.boards.store import BoardStore
    from commonplace.rules.store import RuleStore
    from mempalace.backend_copy import open_collection
    from mempalace.backends.compact import CompactBackend
    from mempalace.knowledge_graph import KnowledgeGraph

    data, _, wid, *_ = workspace
    root = workspace_root(data, wid)
    root.mkdir(parents=True)
    (root / "workspace.json").write_text('{"store":"compact"}')
    thread = store_message(root, wid)
    BoardStore(root)
    RuleStore(root)
    with KnowledgeGraph(str(root / "palace" / "knowledge_graph.sqlite3")) as graph:
        graph.add_entity("Synthetic person", "person")
    backend = CompactBackend()
    try:
        col = open_collection(backend, root / "palace", "test", create=True)
        col.add(ids=["synthetic"], documents=["a verbatim drawer"], embeddings=[[1.0, 0.0]])
    finally:
        backend.close()
    result = enable(workspace)
    assert result["databases"] == 5
    active = workspace_path(data, wid)
    assert active != root
    with closing(open_threads(active)) as store:
        assert store.messages(thread)[0].raw == "raw:verbatim snowman ☃\x00"
    with BoardStore(active).connection() as conn:
        assert conn.execute("SELECT count(*) FROM boards").fetchone()[0] == 0
    with RuleStore(active).connection() as conn:
        assert conn.execute("SELECT count(*) FROM rules").fetchone()[0] == 0
    backend = CompactBackend()
    try:
        assert backend.detect(str(active / "palace"))
        col = open_collection(backend, active / "palace", "test")
        assert col.get(ids=["synthetic"]).documents == ["a verbatim drawer"]
    finally:
        backend.close()
    with KnowledgeGraph(str(active / "palace" / "knowledge_graph.sqlite3")) as graph:
        assert (
            graph._conn()
            .execute("SELECT name FROM entities WHERE name=?", ("Synthetic person",))
            .fetchone()[0]
            == "Synthetic person"
        )
    # Plaintext rollback generations are retained deliberately, not called encrypted.
    with closing(sqlite3.connect(root / "threads.v2.sqlite3")) as original:
        assert original.execute("PRAGMA quick_check").fetchone() == ("ok",)


@pytest.mark.parametrize("failure", ["wrong", "missing", "missing-slots", "missing-driver"])
def test_key_and_driver_fail_closed(workspace, monkeypatch, failure):
    data, _, wid, server, _, slots = workspace
    enable(workspace)
    active = workspace_path(data, wid)
    if failure == "wrong":
        server.write_bytes(os.urandom(32))
    elif failure == "missing":
        monkeypatch.delenv("COMMONPLACE_WRAPPING_KEY_FILE")
    elif failure == "missing-slots":
        (slots / (wid + ".json")).unlink()
    else:
        monkeypatch.setitem(sys.modules, "sqlcipher3", None)
    target = active / "not-created.sqlite3"
    with pytest.raises(crypto.EncryptionError):
        crypto.connect(target)
    assert not target.exists()


def test_wrong_raw_data_key_fails_closed(workspace):
    data, _, wid, *_ = workspace
    enable(workspace)
    active = workspace_path(data, wid)
    store_message(active, wid)
    path = active / "threads.v2.sqlite3"
    before = path.read_bytes()
    with pytest.raises(crypto.EncryptionError):
        crypto.cipher_connect(path, os.urandom(32))
    assert path.read_bytes() == before


def test_ciphertext_backup_restore_without_keys_then_recovery_reopen(workspace, monkeypatch):
    from commonplace import backup as backups

    data, _, wid, server, recovery, slots = workspace
    root = workspace_root(data, wid)
    root.mkdir(parents=True)
    (root / "workspace.json").write_text('{"store":"compact"}')
    migrated = ("ciphertext.unusual", "nested/archive.memory", "control.sqlite3")
    for name in migrated:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute("CREATE TABLE fixture(content TEXT)")
    auxiliaries = {
        "aux/revisions.jsonl": b'{"text":"verbatim"}\n',
        "aux/registry.json": b'{"person":"Synthetic"}\n',
        "aux/service.log": b"verbatim log\n",
        "aux/opaque.bin": b"\x00\xffopaque bytes\n",
    }
    for name, content in auxiliaries.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    opaque_db = data / "opaque-aux.db"
    opaque_db.write_bytes(b"opaque non-SQLite auxiliary\x00\xff")
    enable(workspace)
    active = workspace_path(data, wid)
    directory, info = crypto.encryption_settings(active / migrated[1])
    assert directory == active != root
    assert info["database_paths"] == sorted(migrated)
    settings_before = (active / "workspace.json").read_bytes()
    thread = store_message(active, wid)
    # Future known-suffix stores need no inventory write, but must be backed up.
    future = ("future.sqlite3", "future.sqlite", "nested/future.db")
    databases = (*migrated, *future)
    text = "committed verbatim WAL row ☃\x00\n"
    with ExitStack() as stack:
        writers = []
        for name in databases:
            path = active / name
            writer = stack.enter_context(closing(crypto.connect(path, isolation_level=None)))
            writers.append(writer)
            assert type(writer).__module__.startswith("sqlcipher3")
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            if name in future:
                writer.execute("CREATE TABLE fixture(content TEXT)")
            main_before = path.read_bytes()
            writer.execute("INSERT INTO fixture VALUES (?)", (text,))
            assert Path(str(path) + "-wal").stat().st_size > 32
            assert path.read_bytes() == main_before
        snapshot = data.parent / "encrypted-backup"
        manifest = backup(data, snapshot)
        assert verify_backup(snapshot) == manifest
        for name, writer in zip(databases, writers):
            relative = active.relative_to(data) / name
            assert (snapshot / relative).read_bytes() == (data / relative).read_bytes()
            assert not (snapshot / relative).read_bytes().startswith(b"SQLite format 3\x00")
            assert writer.execute("SELECT content FROM fixture").fetchall() == [(text,)]
        assert not any(
            path.name.endswith(("-wal", "-shm", "-journal")) for path in snapshot.rglob("*")
        )
    assert (active / "workspace.json").read_bytes() == settings_before
    monkeypatch.delenv("COMMONPLACE_WRAPPING_KEY_FILE")
    monkeypatch.delenv("COMMONPLACE_KEY_STORE")
    restored = data.parent / "restored"
    with monkeypatch.context() as no_keys:
        no_keys.setattr(crypto, "driver", lambda: pytest.fail("driver during keyless restore"))
        no_keys.setattr(
            crypto, "workspace_key", lambda *a: pytest.fail("key during keyless restore")
        )
        assert verify_backup(snapshot) == manifest
        # Matching hashes must not let unusual-name sidecars pass verification.
        manifest_path = snapshot / "manifest.json"
        manifest_bytes = manifest_path.read_bytes()
        for name in migrated[:2]:
            for suffix in ("-wal", "-shm", "-journal"):
                sidecar = snapshot / active.relative_to(data) / (name + suffix)
                sidecar.write_bytes(b"unexpected SQLite sidecar")
                manifest_path.write_text(
                    json.dumps(
                        {
                            **manifest,
                            "files": [*manifest["files"], backups._file_entry(sidecar, snapshot)],
                        }
                    )
                )
                with pytest.raises(BackupError, match="SQLite sidecars"):
                    verify_backup(snapshot)
                sidecar.unlink()
        manifest_path.write_bytes(manifest_bytes)
        restore(snapshot, restored)
    recovered = workspace_path(restored, wid)
    for name, content in auxiliaries.items():
        assert (active / name).read_bytes() == (recovered / name).read_bytes() == content
    assert (restored / opaque_db.name).read_bytes() == opaque_db.read_bytes()
    for path in ("threads.v2.sqlite3", *databases):
        assert (recovered / path).read_bytes() == (
            snapshot / recovered.relative_to(restored) / path
        ).read_bytes()
    with pytest.raises(crypto.EncryptionError):
        open_threads(recovered)
    # Only a recovery slot and its independently held credential are needed.
    record_path = slots / (wid + ".json")
    record = json.loads(record_path.read_text())
    del record["slots"]["server"]
    record_path.write_text(json.dumps(record))
    server.unlink()
    monkeypatch.setenv("COMMONPLACE_KEY_STORE", str(slots))
    monkeypatch.setenv("COMMONPLACE_WRAPPING_KEY_FILE", str(recovery))
    monkeypatch.setenv("COMMONPLACE_KEY_SLOT", "recovery")
    with closing(open_threads(recovered)) as store:
        assert store.messages(thread)[0].text == "verbatim snowman ☃\x00"
    for name in databases:
        with closing(crypto.connect(recovered / name)) as conn:
            assert conn.execute("SELECT content FROM fixture").fetchall() == [(text,)]


def test_byte_copy_retains_unchanged_cipher_pages(workspace):
    data, _, wid, *_ = workspace
    enable(workspace)
    active = workspace_path(data, wid)
    path = active / "dedup.sqlite3"
    with closing(crypto.connect(path)) as conn, conn:
        conn.execute("CREATE TABLE fixture(id INTEGER PRIMARY KEY, value BLOB)")
        conn.executemany(
            "INSERT INTO fixture VALUES (?,?)", [(i, os.urandom(12000)) for i in range(100)]
        )
    first, second = data.parent / "backup-one", data.parent / "backup-two"
    backup(data, first)
    with closing(crypto.connect(path)) as conn, conn:
        conn.execute("UPDATE fixture SET value=? WHERE id=50", (os.urandom(12000),))
    backup(data, second)
    relative = path.relative_to(data)
    before, after = (first / relative).read_bytes(), (second / relative).read_bytes()
    page = 16384
    same = sum(before[i : i + page] == after[i : i + page] for i in range(0, len(before), page))
    total = len(before) // page
    print(f"ciphertext dedup: {same}/{total} unchanged pages ({same / total:.1%})")
    assert same / total > 0.9
    assert before != after


def test_migration_failure_keeps_original_and_verified_backup(workspace):
    data, _, wid, _, _, slots = workspace
    root = workspace_root(data, wid)
    root.mkdir(parents=True)
    (root / "workspace.json").write_text('{"store":"compact"}')
    thread = store_message(root, wid)

    def fail(step):
        if step == "encrypted":
            raise RuntimeError("synthetic interruption")

    with pytest.raises(RuntimeError, match="synthetic"):
        enable(workspace, checkpoint=fail)
    assert workspace_path(data, wid) == root
    assert not (root / POINTER).exists()
    assert not list(root.parent.glob(wid + ".generation-*"))
    assert not list(slots.iterdir())
    with closing(open_threads(root)) as store:
        assert store.messages(thread)[0].text == "verbatim snowman ☃\x00"
    restored = data.parent / "rollback"
    restore(data.parent / "before-encryption", restored)
    with closing(open_threads(workspace_path(restored, wid))) as store:
        assert store.messages(thread)[0].raw == "raw:verbatim snowman ☃\x00"


def test_non_owner_cannot_enable_encryption(workspace):
    data, _, wid, server, recovery, slots = workspace
    with closing(ControlDB(str(data / "control.sqlite3"))) as db:
        other = db.create_user("other@example.test")
    assert (
        main(
            [
                "--data",
                str(data),
                "workspace",
                "encrypt",
                wid,
                "--by",
                other,
                "--backup-dir",
                str(data.parent / "before-encryption"),
                "--key-store",
                str(slots),
                "--server-key-file",
                str(server),
                "--recovery-key-file",
                str(recovery),
            ]
        )
        == 2
    )
    assert not (workspace_root(data, wid) / POINTER).exists()


def test_keyslot_binding_rejects_workspace_or_slot_substitution():
    key, wrapping = os.urandom(32), os.urandom(32)
    info = {"workspace_id": "ws_synthetic", "key_id": "a" * 32}
    wrapped = crypto.wrap_key(key, wrapping, info, "server")
    record = {
        "version": 1,
        "algorithm": "AES-256-GCM",
        **info,
        "slots": {"server": wrapped, "recovery": wrapped},
    }
    assert crypto.unwrap_key(record, wrapping, info, "server") == key
    with pytest.raises(crypto.EncryptionError):
        crypto.unwrap_key(record, wrapping, info, "recovery")
    with pytest.raises(crypto.EncryptionError):
        crypto.unwrap_key(record, wrapping, {**info, "workspace_id": "ws_other"}, "server")


def test_plaintext_driver_cannot_satisfy_cipher_request(tmp_path, monkeypatch):
    monkeypatch.setattr(crypto, "driver", lambda: sqlite3)
    with pytest.raises(crypto.EncryptionError, match="no SQLCipher"):
        crypto.cipher_connect(tmp_path / "unkeyed.sqlite3", os.urandom(32))


def test_backup_refuses_active_writer(workspace):
    data, _, wid, *_ = workspace
    enable(workspace)
    active = workspace_path(data, wid)
    thread = store_message(active, wid)
    with closing(crypto.connect(active / "threads.v2.sqlite3", isolation_level=None)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        with pytest.raises(BackupError, match="checkpoint|changed"):
            backup(data, data.parent / "busy-backup")
    assert not (data.parent / "busy-backup").exists()
    with closing(open_threads(active)) as store:
        assert store.messages(thread)[0].text == "verbatim snowman ☃\x00"


def test_publish_fsync_failure_preserves_selected_ciphertext_and_slots(workspace, monkeypatch):
    from commonplace import workspace_encryption as migration

    data, _, wid, _, _, slots = workspace
    write = migration.atomic_write_json

    def fail_after_replace(path, value, **kwargs):
        write(path, value, **kwargs)
        if path.name == POINTER:
            raise OSError("synthetic post-replace failure")

    monkeypatch.setattr(migration, "atomic_write_json", fail_after_replace)
    with pytest.raises(OSError, match="post-replace"):
        enable(workspace)
    active = workspace_path(data, wid)
    assert active != workspace_root(data, wid)
    assert (slots / (wid + ".json")).is_file()
    thread = store_message(active, wid)
    with closing(open_threads(active)) as store:
        assert store.messages(thread)[0].text == "verbatim snowman ☃\x00"


@pytest.mark.parametrize("failure", ["stat", "read_text", "resolve"])
@pytest.mark.parametrize("reader", [False, True])
def test_metadata_io_failure_never_opens_a_database(tmp_path, monkeypatch, failure, reader):
    from mempalace.config import connect_sqlite_read

    directory = tmp_path / "workspaces" / "ws_synthetic"
    directory.mkdir(parents=True)
    config = directory / "workspace.json"
    config.write_text(
        json.dumps(
            {
                "encryption": {
                    "version": 1,
                    "cipher": "sqlcipher",
                    "workspace_id": directory.name,
                    "key_id": "a" * 32,
                    "database_paths": [],
                }
            }
        )
    )
    target = directory / "not-created.sqlite3"
    original = getattr(Path, failure)

    def denied(path, *args, **kwargs):
        if path == (target if failure == "resolve" else config):
            raise PermissionError("synthetic metadata access denied")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, failure, denied)
    monkeypatch.setattr(crypto.sqlite3, "connect", lambda *a, **k: pytest.fail("SQLite fallback"))
    monkeypatch.setattr(crypto, "driver", lambda: pytest.fail("driver before metadata validation"))
    with pytest.raises(crypto.EncryptionError, match="settings"):
        (connect_sqlite_read if reader else crypto.connect)(target)
    assert not target.exists()


@pytest.mark.parametrize(
    "paths",
    [
        pytest.param("missing", id="missing-key"),
        pytest.param(None, id="null"),
        pytest.param({}, id="object"),
        pytest.param("database.unusual", id="string"),
        pytest.param([None], id="null-entry"),
        pytest.param([123], id="number-entry"),
        pytest.param([""], id="empty"),
        pytest.param(["/absolute.unusual"], id="absolute"),
        pytest.param(["C:/absolute.unusual"], id="windows-absolute"),
        pytest.param(["C:relative.unusual"], id="windows-drive"),
        pytest.param(["nested\\database.unusual"], id="backslash"),
        pytest.param(["nul\x00.unusual"], id="nul"),
        pytest.param(["."], id="dot"),
        pytest.param([".."], id="dotdot"),
        pytest.param(["../escape.unusual"], id="escape"),
        pytest.param(["nested/../database.unusual"], id="nested-dotdot"),
        pytest.param(["nested/./database.unusual"], id="nested-dot"),
        pytest.param(["nested//database.unusual"], id="empty-segment"),
        pytest.param(["nested/database.unusual/"], id="trailing-slash"),
        pytest.param(["same.unusual", "same.unusual"], id="duplicate"),
        pytest.param(["workspace.json"], id="settings"),
        pytest.param(["nested/workspace.json"], id="nested-settings"),
        pytest.param(["database-wal"], id="wal"),
        pytest.param(["nested/database-shm"], id="shm"),
        pytest.param(["database-journal"], id="journal"),
    ],
)
def test_invalid_database_inventory_fails_before_database_access(
    encryption_metadata, monkeypatch, paths
):
    from commonplace import backup as backups

    directory, config, info = encryption_metadata
    if paths == "missing":
        del info["database_paths"]
    else:
        info["database_paths"] = paths
    config.write_text(json.dumps({"encryption": info}))
    target = directory / "not-created.unusual"
    monkeypatch.setattr(crypto, "driver", lambda: pytest.fail("driver before inventory validation"))
    monkeypatch.setattr(crypto, "workspace_key", lambda *a: pytest.fail("key before validation"))
    monkeypatch.setattr(crypto.sqlite3, "connect", lambda *a, **k: pytest.fail("SQLite fallback"))
    for operation in (crypto.encryption_settings, crypto.connect, backups._encrypted_database):
        with pytest.raises(crypto.EncryptionError, match="settings"):
            operation(target)
    assert not target.exists()


def test_inventory_symlink_escape_fails_closed(encryption_metadata, tmp_path, monkeypatch):
    directory, config, info = encryption_metadata
    outside = tmp_path / "outside"
    outside.mkdir()
    (directory / "escaped").symlink_to(outside, target_is_directory=True)
    info["database_paths"] = ["escaped/database.unusual"]
    config.write_text(json.dumps({"encryption": info}))
    monkeypatch.setattr(crypto, "driver", lambda: pytest.fail("driver before inventory validation"))
    target = directory / "not-created.sqlite3"
    with pytest.raises(crypto.EncryptionError, match="settings"):
        crypto.connect(target)
    assert not target.exists()
    assert not list(outside.iterdir())


@pytest.mark.parametrize("uri", [False, True])
def test_unlisted_unknown_suffix_refused_before_key_driver_or_open(
    encryption_metadata, monkeypatch, uri
):
    from commonplace import backup as backups

    directory, config, info = encryption_metadata
    info["database_paths"] = ["nested/inventoried.unusual"]
    config.write_text(json.dumps({"encryption": info}))
    listed = directory / info["database_paths"][0]
    assert backups._encrypted_database(listed)
    monkeypatch.setattr(crypto, "driver", lambda: pytest.fail("driver before path refusal"))
    monkeypatch.setattr(crypto, "workspace_key", lambda *a: pytest.fail("key before path refusal"))
    monkeypatch.setattr(crypto, "cipher_connect", lambda *a, **k: pytest.fail("database open"))
    monkeypatch.setattr(crypto.sqlite3, "connect", lambda *a, **k: pytest.fail("SQLite fallback"))
    # A matching basename in another directory is not an inventory match.
    for name in ("future.unusual", "inventoried.unusual", "other/inventoried.unusual"):
        target = directory / name
        assert not backups._encrypted_database(target)
        with pytest.raises(crypto.EncryptionError, match="Unlisted"):
            crypto.connect(target.as_uri() if uri else target, uri=uri)
        assert not target.exists()


@pytest.mark.parametrize(
    "name,plaintext",
    [
        ("future.sqlite3", True),
        ("future.sqlite", False),
        ("future.db", True),
        ("nested/inventoried.unusual", False),
    ],
)
def test_backup_refuses_plaintext_or_corrupt_encrypted_database_paths(workspace, name, plaintext):
    data, _, wid, *_ = workspace
    root = workspace_root(data, wid)
    root.mkdir(parents=True)
    unusual = root / "nested/inventoried.unusual"
    unusual.parent.mkdir()
    with closing(sqlite3.connect(unusual)) as conn, conn:
        conn.execute("CREATE TABLE fixture(content TEXT)")
    enable(workspace)
    path = workspace_path(data, wid) / name
    path.unlink(missing_ok=True)
    if plaintext:
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute("CREATE TABLE fixture(content TEXT)")
    else:
        path.write_bytes(b"opaque corruption, not a database")
    before = path.read_bytes()
    snapshot = data.parent / "refused-backup"
    with pytest.raises(crypto.EncryptionError):
        backup(data, snapshot)
    assert path.read_bytes() == before
    assert not snapshot.exists()
    assert not list(data.parent.glob(".refused-backup.partial-*"))


def test_unknown_suffix_encrypted_snapshot_never_uses_plaintext_api(workspace, monkeypatch):
    from commonplace import backup as backups

    data, _, wid, *_ = workspace
    root = workspace_root(data, wid)
    root.mkdir(parents=True)
    source = root / "ciphertext.unusual"
    with closing(sqlite3.connect(source)) as conn, conn:
        conn.execute("CREATE TABLE fixture(content TEXT)")
        conn.execute("INSERT INTO fixture VALUES ('verbatim ciphertext')")
    enable(workspace)
    active = workspace_path(data, wid)
    source = active / "ciphertext.unusual"
    assert crypto.encryption_settings(source)[1]["database_paths"] == [source.name]
    before = source.read_bytes()
    monkeypatch.setattr(backups.sqlite3, "connect", lambda *a, **k: pytest.fail("plaintext API"))
    target = data.parent / "snapshot.unusual"
    backups._snapshot(source, target)
    assert target.read_bytes() == source.read_bytes() == before
    assert not before.startswith(b"SQLite format 3\x00")
    assert target.stat().st_mode & 0o777 == 0o600


def test_encrypted_snapshot_refuses_wal_written_after_checkpoint(workspace, monkeypatch):
    from commonplace import backup as backups

    data, _, wid, *_ = workspace
    enable(workspace)
    active = workspace_path(data, wid)
    source = active / "race.sqlite3"
    with closing(crypto.connect(source)) as conn, conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE fixture(content TEXT)")
    connect = backups.connect
    opened = []

    class Connection:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, sql):
            if sql == "BEGIN IMMEDIATE":
                # Inject a committed writer in the checkpoint/lock gap.
                with closing(crypto.connect(source)) as writer, writer:
                    writer.execute("INSERT INTO fixture VALUES ('late committed WAL row')")
            return self.conn.execute(sql)

        def close(self):
            self.conn.close()

    def racing_connect(*args, **kwargs):
        conn = Connection(connect(*args, **kwargs))
        opened.append(conn)
        return conn

    monkeypatch.setattr(backups, "connect", racing_connect)
    target = data.parent / "refused.sqlite3"
    with pytest.raises(BackupError, match="changed after checkpoint"):
        backups._snapshot(source, target)
    assert len(opened) == 1
    assert not target.exists()
    with closing(crypto.connect(source)) as conn:
        assert conn.execute("SELECT content FROM fixture").fetchone() == ("late committed WAL row",)


def test_b3_marker_never_uses_v1_provider(workspace):
    """The same synthetic B2 bytes open before, but never after, the marker."""
    data, _, wid, *_ = workspace
    enable(workspace)
    active = workspace_path(data, wid)
    source = active / "guard.sqlite3"
    with closing(crypto.connect(source)) as conn, conn:
        conn.execute("CREATE TABLE fixture(content BLOB)")
        conn.execute("INSERT INTO fixture VALUES (?)", (b"exact\x00synthetic",))
    with closing(crypto.connect(source)) as conn:
        assert conn.execute("SELECT content FROM fixture").fetchone()[0] == b"exact\x00synthetic"
    config = active / "workspace.json"
    settings = json.loads(config.read_text())
    settings["encryption"]["key_mode"] = "token-v2"
    config.write_text(json.dumps(settings))
    before = source.read_bytes()
    blocked = False
    try:
        with closing(crypto.connect(source)):
            pass
    except crypto.EncryptionError:
        blocked = True
    assert blocked, "B3 marker reached the operator-held B2 provider"
    assert source.read_bytes() == before


@pytest.mark.parametrize("mode", ["token-v2", "unknown", None, False])
@pytest.mark.parametrize("operation", ["connect", "snapshot", "classify"])
def test_unsupported_key_modes_refuse_backup_and_connect(workspace, tmp_path, mode, operation):
    from commonplace import backup as backups

    data, _, wid, *_ = workspace
    enable(workspace)
    directory = workspace_path(data, wid)
    source = directory / "source.sqlite3"
    with closing(crypto.connect(source)) as conn, conn:
        conn.execute("CREATE TABLE fixture(content BLOB)")
        conn.execute("INSERT INTO fixture VALUES (?)", (b"exact synthetic\x00payload",))
    with closing(crypto.connect(source)) as conn:
        assert (
            conn.execute("SELECT content FROM fixture").fetchone()[0]
            == b"exact synthetic\x00payload"
        )
    assert backups._encrypted_database(source)
    target = tmp_path / "snapshot.sqlite3"
    backups._snapshot(source, target)
    assert target.read_bytes() == source.read_bytes()
    target.unlink()
    before = source.read_bytes()
    config = directory / "workspace.json"
    settings = json.loads(config.read_text())
    settings["encryption"]["key_mode"] = mode
    config.write_text(json.dumps(settings))
    blocked = False
    try:
        if operation == "connect":
            with closing(crypto.connect(source)):
                pass
        elif operation == "snapshot":
            backups._snapshot(source, target)
        else:
            backups._encrypted_database(source)
    except crypto.EncryptionError:
        blocked = True
    assert blocked, f"marked workspace reached B2 through {operation}"
    assert not target.exists()
    assert source.read_bytes() == before


@pytest.mark.parametrize("provider", ["wrap", "unwrap", "workspace"])
def test_b3_direct_v1_providers_refuse_before_keys(workspace, monkeypatch, provider):
    data, _, wid, server, _, slots = workspace
    enable(workspace)
    directory = workspace_path(data, wid)
    _, info = crypto.encryption_settings(directory / "source.sqlite3")
    record = json.loads((slots / (wid + ".json")).read_text())
    wrapping = server.read_bytes()  # Synthetic fixture only.
    key = crypto.workspace_key(directory, info)
    assert crypto.unwrap_key(record, wrapping, info, "server") == key
    assert crypto.wrap_key(key, wrapping, info, "server")
    marked = dict(info, key_mode="token-v2")
    key_reads = []
    protected_key = crypto.protected_key

    def observe_key_read(path):
        key_reads.append(path)
        return protected_key(path)

    monkeypatch.setattr(crypto, "protected_key", observe_key_read)
    blocked = False
    try:
        if provider == "wrap":
            crypto.wrap_key(key, wrapping, marked, "server")
        elif provider == "unwrap":
            crypto.unwrap_key(record, wrapping, marked, "server")
        else:
            crypto.workspace_key(directory, marked)
    except crypto.EncryptionError:
        blocked = True
    assert blocked, f"marked workspace reached direct B2 {provider}"
    assert not key_reads, "marked provider read operator wrapping keys before refusing"


def test_b3_settings_refuse_during_classification(encryption_metadata):
    directory, config, info = encryption_metadata
    info["key_mode"] = "token-v2"
    config.write_text(json.dumps({"encryption": info}))
    with pytest.raises(crypto.EncryptionError):
        crypto.encryption_settings(directory / "content.sqlite3")


@pytest.mark.parametrize("provider", ["wrap", "unwrap"])
@pytest.mark.parametrize("info", [None, [], "invalid", 2])
def test_invalid_direct_provider_metadata_is_encryption_error(provider, info):
    blocked = False
    try:
        if provider == "wrap":
            crypto.wrap_key(b"s" * 32, b"w" * 32, info, "server")
        else:
            crypto.unwrap_key({}, b"w" * 32, info, "server")
    except crypto.EncryptionError:
        blocked = True
    except (TypeError, AttributeError):
        pass
    assert blocked, "malformed metadata did not produce EncryptionError"


@pytest.mark.parametrize("provider", ["wrap", "unwrap"])
def test_version_two_direct_provider_refused(encryption_metadata, provider):
    _, _, info = encryption_metadata
    key = b"s" * 32
    wrapped = crypto.wrap_key(key, key, info, "server")
    record = dict(info, algorithm="AES-256-GCM", slots={"server": wrapped})
    assert crypto.unwrap_key(record, key, info, "server") == key
    info = dict(info, version=2)
    blocked = False
    try:
        if provider == "wrap":
            crypto.wrap_key(key, key, info, "server")
        else:
            crypto.unwrap_key(record, key, info, "server")
    except crypto.EncryptionError:
        blocked = True
    assert blocked, "B2 direct provider accepted version two metadata"


def test_b3_marker_has_locked_diagnostic(encryption_metadata):
    directory, config, info = encryption_metadata
    info["key_mode"] = "token-v2"
    config.write_text(json.dumps({"encryption": info}))
    message = ""
    try:
        crypto.connect(directory / "source.sqlite3")
    except crypto.EncryptionError as exc:
        message = str(exc)
    assert "B3 remains locked" in message
