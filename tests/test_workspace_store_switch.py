"""Synthetic workspace cutover, rollback, off-host backup and crash recovery."""

import json
import logging
import os
import shlex
import sqlite3
import stat
import sys
from contextlib import closing
from pathlib import Path

import pytest

from commonplace import store_switch as switch
from commonplace.backup import BackupError, data_lock
from commonplace.handoff.models import ThreadMessage
from commonplace.handoff.thread_store import ThreadStore
from commonplace.handoff.thread_store_migrate import read_store, verify_stores
from commonplace.workspace_store import (
    POINTER,
    POINTER_PREVIOUS,
    RECEIPT,
    SETTINGS,
    open_threads,
    read_json,
    thread_path,
    workspace_path,
    workspace_root,
)
from mempalace.backend_copy import open_collection, verify_palaces
from mempalace.backends import ChromaBackend, CompactBackend

WORKSPACE = "ws_fixture"
REAL_EMBEDDER = switch._embedder


def _maintenance_tree_snapshot(root):
    result = {}
    for path in [root, *root.rglob("*")]:
        mode = path.lstat().st_mode
        content = (
            os.readlink(path)
            if stat.S_ISLNK(mode)
            else path.read_bytes()
            if stat.S_ISREG(mode)
            else None
        )
        result[path.relative_to(root).as_posix()] = (mode, content)
    return result


@pytest.mark.parametrize("operation", ["switch", "prune", "report"])
@pytest.mark.parametrize("pointer_state", ["valid", "missing", "invalid"])
@pytest.mark.parametrize(
    "location,damage",
    [
        *[
            (name, "encrypted")
            for name in (
                "root",
                "active",
                "retained",
                "staging",
                "source-palace",
                "probe",
                "rollback-probe",
                "probe.source-palace",
                "rollback-probe.source-palace",
            )
        ],
        ("root", "null_encryption"),
        ("staging", "empty_encryption"),
        ("retained", "invalid_json"),
        ("probe", "invalid_object"),
        ("staging", "stat_denied"),
        ("source-palace", "read_denied"),
        ("root", "settings_symlink"),
        ("rollback-probe", "dangling_settings_symlink"),
        ("active", "directory_symlink"),
        ("probe.source-palace", "directory_symlink"),
    ],
)
def test_maintenance_metadata_preflight_refuses_without_mutation(
    tmp_path, monkeypatch, operation, pointer_state, location, damage
):
    data = tmp_path / "data"
    root = workspace_root(data, WORKSPACE)
    active, retained, staging = [
        root.with_name(root.name + ".generation-" + char * 24) for char in "abc"
    ]
    directories = {"root": root, "active": active, "retained": retained, "staging": staging}
    directories.update(
        {path.name[len(staging.name) + 1 :]: path for path in switch._staging_paths(staging)[1:]}
    )
    for directory in directories.values():
        directory.mkdir(parents=True, mode=0o750)
        (directory / SETTINGS).write_text('{"store":"compact"}')
        (directory / "payload").write_bytes(b"verbatim untouched\x00")
        (directory / "payload").chmod(0o640)
    pointer = {
        "active": active.name,
        "retained": [{"name": retained.name, "retired_at": 0}],
    }
    (root / POINTER_PREVIOUS).write_text(json.dumps(pointer))
    if pointer_state != "missing":
        (root / POINTER).write_text(json.dumps(pointer) if pointer_state == "valid" else "{}")
    (root / switch.STATE).write_text(json.dumps({"state": "copying", "staging": staging.name}))
    config = directories[location] / SETTINGS
    if damage in ("encrypted", "null_encryption", "empty_encryption"):
        info = {
            "version": 1,
            "cipher": "sqlcipher",
            "workspace_id": WORKSPACE,
            "key_id": "a" * 32,
            "database_paths": [],
        }
        value = info if damage == "encrypted" else None if damage == "null_encryption" else {}
        config.write_text(json.dumps({"store": "compact", "encryption": value}))
    elif damage in ("invalid_json", "invalid_object"):
        config.write_text("{" if damage == "invalid_json" else "[]")
    elif damage in ("settings_symlink", "dangling_settings_symlink"):
        external = tmp_path / "external-settings.json"
        config.rename(external)
        config.symlink_to(external if damage == "settings_symlink" else tmp_path / "missing")
    elif damage == "directory_symlink":
        directory = directories[location]
        external = tmp_path / "external-directory"
        directory.rename(external)
        directory.symlink_to(external, target_is_directory=True)
    # The existing lock file is outside generation maintenance and stays intact.
    with data_lock(data, exclusive=True):
        pass
    before = _maintenance_tree_snapshot(tmp_path)
    monkeypatch.setattr(switch, "_verify_offline", lambda directory: None)

    def forbidden(*args, **kwargs):
        pytest.fail("maintenance ran before metadata validation")

    for name in (
        "_prepare_generations",
        "_recover_publication",
        "_cleanup_recorded_staging",
        "_cleanup_unpublished",
        "_remove_staging",
        "_seal_retired",
        "_finish_deletion",
        "_write_state",
        "_write_pointer",
    ):
        monkeypatch.setattr(switch, name, forbidden)
    with monkeypatch.context() as denied:
        if damage in ("stat_denied", "read_denied"):
            method = "lstat" if damage == "stat_denied" else "read_text"
            original = getattr(Path, method)

            def unavailable(path, *args, **kwargs):
                if path == config:
                    raise PermissionError("synthetic metadata access denied")
                return original(path, *args, **kwargs)

            denied.setattr(Path, method, unavailable)
        with pytest.raises(ValueError, match="Encrypted|metadata|settings|symlink|Expecting"):
            if operation == "switch":
                switch.switch_workspace(data, WORKSPACE, "v1")
            else:
                switch.prune_generations(data, WORKSPACE, report_only=operation == "report")
    assert _maintenance_tree_snapshot(tmp_path) == before


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    data = tmp_path / "data"
    root = workspace_root(data, WORKSPACE)
    root.mkdir(parents=True)
    with closing(ThreadStore(str(root / "threads.sqlite3"))) as store:
        thread = store.store(
            WORKSPACE,
            "source",
            [ThreadMessage(0, "user", "message", "first", "raw first")],
            actor="actor",
        )
        store.append(
            thread.id,
            [
                ThreadMessage(0, "user", "message", "first", "raw first"),
                ThreadMessage(1, "assistant", "message", "second", "raw second"),
            ],
        )
        store.put_doc("handoff", "verbatim handoff")
        store.advance_fact_cursor(thread.id, 1, 1)
    backend = ChromaBackend()
    try:
        for name in ("mempalace_drawers", "mempalace_closets"):
            open_collection(backend, root / "palace", name, create=True).add(
                ids=["a"],
                documents=["verbatim drawer"],
                metadatas=[{"wing": "test", "last_modified": "2026-01-01"}],
                embeddings=[[1.0, 0.3, -0.1]],
            )
    finally:
        backend.close()
    for path in (
        root / "boards.sqlite3",
        root / "palace" / "knowledge_graph.sqlite3",
        root / "palace" / "logstream.sqlite3",
    ):
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute("CREATE TABLE fixture(id TEXT PRIMARY KEY, content TEXT)")
            conn.execute("INSERT INTO fixture VALUES ('a','verbatim board or fact')")
    from commonplace.boards.store import BoardStore

    BoardStore(root)
    with closing(sqlite3.connect(root / "boards.sqlite3")) as conn, conn:
        conn.execute(
            "INSERT INTO boards(id,slug,name,wing,stamp_user,stamp_trust,created_at) "
            "VALUES (1,'fixture','Fixture','test','actor','standard','2026-01-01')"
        )
        conn.execute(
            "INSERT INTO tasks(id,board_id,title,body,status,position,stamp_user,stamp_trust,created_at,updated_at,updated_by) "
            "VALUES (1,1,'Original card','verbatim task','todo',1,'actor','standard','2026-01-01','2026-01-01','actor')"
        )
    (root / "palace" / "replica.json").write_text('{"replica_id":"fixture"}')
    (root / "palace" / "custom-sidecar").write_bytes(b"preserved\0")
    command = tmp_path / "backup_command.py"
    command.write_text(
        "import datetime,json,os\nprint(json.dumps({'completed_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'off_host':True,'nonce':os.environ['COMMONPLACE_BACKUP_NONCE']}))\n"
    )
    monkeypatch.setenv("COMMONPLACE_BACKUP_CMD", shlex.join([sys.executable, str(command)]))
    monkeypatch.setattr(switch, "_verify_offline", lambda directory: None)
    monkeypatch.setattr(
        switch,
        "_embedder",
        lambda directory: (
            lambda documents, metadatas, **kwargs: [[1.0, 0.3, -0.1] for _ in documents]
        ),
    )
    return data, root, thread


def test_switch_write_rollback_every_record(workspace):
    data, root, thread = workspace
    assert switch.switch_workspace(data, WORKSPACE, "compact")["state"] == "complete"
    active = workspace_path(data, WORKSPACE)
    assert active != root
    assert (active / "palace" / "compact.sqlite3").exists()
    assert not (active / "palace" / "chroma.sqlite3").exists()
    assert read_json(active / SETTINGS)["store"] == "compact"
    assert read_json(active / "config" / "config.json")["backend"] == "compact"
    assert read_json(root / RECEIPT)["off_host"] is True
    assert (root / "threads.sqlite3").exists()
    from pathlib import Path

    from mempalace.backend_copy import verify_sidecars

    snapshot = Path(str(active) + ".verify-snapshot")
    switch._copy_tree(root / "palace", snapshot)
    verify_palaces(snapshot, active / "palace", "chroma", "compact")
    verify_sidecars(root / "palace", active / "palace", "chroma", skip={".store-comparison.json"})
    with closing(open_threads(active, sample_every=1)) as store:
        assert store.messages(thread.id)[1].text == "second"
        store.append(
            thread.id,
            store.messages(thread.id)
            + [ThreadMessage(2, "assistant", "message", "after switch", "raw after switch")],
        )
    backend = CompactBackend()
    try:
        open_collection(backend, active / "palace", "mempalace_drawers", create=True).add(
            ids=["new"],
            documents=["after switch"],
            metadatas=[{"last_modified": "2026-01-02"}],
            embeddings=[[0.5, 1.0, 0.7]],
        )
    finally:
        backend.close()
    with closing(sqlite3.connect(active / "boards.sqlite3")) as conn, conn:
        conn.execute("INSERT INTO fixture VALUES ('new','after switch')")
        conn.execute(
            "INSERT INTO tasks(id,board_id,title,body,status,position,stamp_user,stamp_trust,created_at,updated_at,updated_by) "
            "VALUES (2,1,'New card','after switch','todo',2,'actor','standard','2026-01-02','2026-01-02','actor')"
        )
    with closing(sqlite3.connect(active / "palace" / "knowledge_graph.sqlite3")) as conn, conn:
        conn.execute("INSERT INTO fixture VALUES ('new','after switch')")
    assert switch.switch_workspace(data, WORKSPACE, "v1")["state"] == "complete"
    restored = workspace_path(data, WORKSPACE)
    verify_palaces(active / "palace", restored / "palace", "compact", "chroma")
    with (
        read_store(thread_path(active), compact=True) as src,
        read_store(thread_path(restored)) as dst,
    ):
        verify_stores(src, dst)
        assert dst.messages(thread.id)[2].text == "after switch"
    for relative in ("boards.sqlite3", "palace/knowledge_graph.sqlite3"):
        with closing(sqlite3.connect(restored / relative)) as conn, conn:
            assert conn.execute("SELECT count(*) FROM fixture").fetchone()[0] == 2
    with closing(sqlite3.connect(restored / "boards.sqlite3")) as conn, conn:
        assert conn.execute("SELECT title,body FROM tasks ORDER BY id").fetchall() == [
            ("Original card", "verbatim task"),
            ("New card", "after switch"),
        ]
    doctor = switch.doctor_workspace(data, WORKSPACE)
    assert doctor["store"] == "v1" and doctor["backfill"] == "complete"


def test_missing_backup_refuses_before_staging(workspace, monkeypatch):
    data, root, _ = workspace
    monkeypatch.delenv("COMMONPLACE_BACKUP_CMD")
    with pytest.raises(BackupError, match="COMMONPLACE_BACKUP_CMD"):
        switch.switch_workspace(data, WORKSPACE, "compact")
    assert workspace_path(data, WORKSPACE) == root
    assert not list(root.parent.glob("*.generation-*"))


@pytest.mark.parametrize(
    "receipt,exit_code",
    [
        ({"completed_at": "2000-01-01T00:00:00+00:00", "off_host": True}, 0),
        ({"completed_at": "2099-01-01T00:00:00+00:00", "off_host": True}, 0),
        ({"completed_at": "2026-01-01T00:00:00+00:00", "off_host": False}, 0),
        ({"completed_at": "2026-01-01T00:00:00", "off_host": True}, 0),
        ({"completed_at": "invalid", "off_host": True}, 0),
        ({}, 1),
    ],
)
def test_backup_gate_rejects_bad_receipt_or_failed_command(
    workspace, monkeypatch, receipt, exit_code
):
    data, root, _ = workspace
    code = f"import sys; print({json.dumps(json.dumps(receipt))}); sys.exit({exit_code})"
    monkeypatch.setenv("COMMONPLACE_BACKUP_CMD", shlex.join([sys.executable, "-c", code]))
    with pytest.raises(BackupError):
        switch.switch_workspace(data, WORKSPACE, "compact")
    assert workspace_path(data, WORKSPACE) == root
    assert not (root / POINTER).exists()


def test_failed_verification_keeps_live_store_and_is_restartable(workspace, monkeypatch):
    data, root, _ = workspace
    from mempalace import backend_copy

    original = backend_copy.verify_records

    def mismatch(*args, **kwargs):
        raise backend_copy.CopyMismatch("injected mismatch")

    monkeypatch.setattr(backend_copy, "verify_records", mismatch)
    with pytest.raises(backend_copy.CopyMismatch):
        switch.switch_workspace(data, WORKSPACE, "compact")
    assert workspace_path(data, WORKSPACE) == root
    assert switch.doctor_workspace(data, WORKSPACE)["backfill"] == "failed"
    with data_lock(data):
        pass
    monkeypatch.setattr(backend_copy, "verify_records", original)
    switch.switch_workspace(data, WORKSPACE, "compact")
    assert switch.doctor_workspace(data, WORKSPACE)["backfill"] == "complete"


def test_switch_refuses_online_workspace(workspace):
    data, root, _ = workspace
    with data_lock(data):
        with pytest.raises(BackupError, match="in use"):
            switch.switch_workspace(data, WORKSPACE, "compact")
    assert workspace_path(data, WORKSPACE) == root


def test_lock_blocks_writes_during_backfill_and_releases_after(workspace):
    data, root, thread = workspace
    seen = []

    def checkpoint(step):
        if step in ("staging", "palace", "verified"):
            with pytest.raises(BackupError, match="in use"):
                with data_lock(data):
                    raise AssertionError("A writer acquired the offline workspace")
            if step == "palace":
                import subprocess

                code = """
import sys
from mempalace.palace import mine_palace_lock, MineAlreadyRunning
try:
    with mine_palace_lock(sys.argv[1]):
        raise AssertionError('Writer lease was not held')
except MineAlreadyRunning:
    pass
"""
                result = subprocess.run(
                    [sys.executable, "-c", code, str(root / "palace")],
                    capture_output=True,
                    env=dict(
                        __import__("os").environ,
                        HOME=str(root / "home"),
                        USERPROFILE=str(root / "home"),
                    ),
                    timeout=20,
                )
                assert result.returncode == 0, result.stderr.decode()
            seen.append(step)

    switch.switch_workspace(data, WORKSPACE, "compact", checkpoint=checkpoint)
    assert seen == ["staging", "palace", "verified"]
    with data_lock(data), closing(open_threads(workspace_path(data, WORKSPACE))) as store:
        assert store.get(thread.id) is not None


def test_retention_never_deletes_before_fourteen_days(workspace):
    data, root, _ = workspace
    switch.switch_workspace(data, WORKSPACE, "compact")
    retired_at = read_json(root / POINTER)["retained"][0]["retired_at"]
    switch.prune_generations(data, WORKSPACE, now=retired_at + switch.RETENTION_SECONDS - 1)
    assert (root / "threads.sqlite3").exists()
    switch.prune_generations(data, WORKSPACE, now=retired_at + switch.RETENTION_SECONDS)
    assert not (root / "threads.sqlite3").exists()
    assert workspace_path(data, WORKSPACE) != root
    assert thread_path(workspace_path(data, WORKSPACE)).exists()


def test_workspace_size_limit_setting(workspace):
    data, root, _ = workspace
    backend = ChromaBackend()
    try:
        open_collection(backend, root / "palace", "mempalace_drawers", create=True).add(
            ids=["b"],
            documents=["another"],
            metadatas=[{"last_modified": "2026-01-01"}],
            embeddings=[[1.0, 1.0, 1.0]],
        )
    finally:
        backend.close()
    (root / SETTINGS).write_text('{"compact_max_collection_records":1}')
    with pytest.raises(ValueError, match="size limit"):
        switch.switch_workspace(data, WORKSPACE, "compact")
    assert workspace_path(data, WORKSPACE) == root
    (root / SETTINGS).write_text('{"compact_max_collection_records":2}')
    switch.switch_workspace(data, WORKSPACE, "compact")


def test_surrogate_rollback_refuses_and_keeps_every_byte(workspace):
    data, root, _ = workspace
    switch.switch_workspace(data, WORKSPACE, "compact")
    active = workspace_path(data, WORKSPACE)
    with closing(open_threads(active)) as store:
        thread = store.store(
            WORKSPACE,
            "surrogate",
            [ThreadMessage(0, "user", "message", "lone \ud800", "raw \ud800")],
            actor="actor",
        )
    with pytest.raises(ValueError, match="1 threads.*cannot be represented by v1"):
        switch.switch_workspace(data, WORKSPACE, "v1")
    assert workspace_path(data, WORKSPACE) == active
    with closing(open_threads(active)) as store:
        assert store.messages(thread.id)[0].text == "lone \ud800"


@pytest.mark.parametrize("available", [True, False])
def test_keyword_only_rollback_uses_configured_embedder(workspace, monkeypatch, caplog, available):
    from mempalace.backends.compact import KEYWORD_ONLY

    data, root, _ = workspace
    switch.switch_workspace(data, WORKSPACE, "compact")
    active = workspace_path(data, WORKSPACE)
    backend = CompactBackend()
    try:
        open_collection(backend, active / "palace", "mempalace_drawers", create=True).add(
            ids=["keyword"],
            documents=["keyword-only exact text"],
            metadatas=[{KEYWORD_ONLY: True, "last_modified": "2026-01-01"}],
        )
    finally:
        backend.close()

    def factory(directory):
        assert directory == active

        def embed(documents, metadatas, **kwargs):
            assert documents == ["keyword-only exact text"]
            if not available:
                raise ValueError("unavailable")
            return [[1.0, 0.1, 0.3]]

        return embed

    monkeypatch.setattr(switch, "_embedder", factory)
    with caplog.at_level(logging.WARNING):
        if available:
            switch.switch_workspace(data, WORKSPACE, "v1")
        else:
            with pytest.raises(ValueError, match="embedder unavailable"):
                switch.switch_workspace(data, WORKSPACE, "v1")
            assert workspace_path(data, WORKSPACE) == active
    assert "count=1" in caplog.text and "estimated_seconds=" in caplog.text
    assert "keyword-only exact text" not in caplog.text
    if available:
        backend = ChromaBackend()
        try:
            row = open_collection(
                backend, workspace_path(data, WORKSPACE) / "palace", "mempalace_drawers"
            ).get(ids=["keyword"], include=["documents", "metadatas", "embeddings"])
            assert row.documents == ["keyword-only exact text"]
            assert row.embeddings[0] is not None
            assert KEYWORD_ONLY not in row.metadatas[0]
        finally:
            backend.close()


@pytest.mark.parametrize("transition", [1, 2, 3])
@pytest.mark.parametrize(
    "step",
    [
        "backup",
        "staging",
        "files",
        "palace",
        "threads",
        "copied",
        "rehearsed",
        "verified",
        "pointer",
        "publish_state",
        "pointer_mirrored",
        "published",
        "complete",
    ],
)
def test_process_crash_at_each_boundary_is_restartable(workspace, step, transition):
    import subprocess

    data, root, thread = workspace
    for number in range(1, transition):
        switch.switch_workspace(data, WORKSPACE, "compact" if number % 2 else "v1")
    source = workspace_path(data, WORKSPACE)
    target = "compact" if transition % 2 else "v1"
    code = """
import os,sys
from pathlib import Path
from commonplace import store_switch as switch
switch._verify_offline = lambda path: None
switch._embedder = lambda directory: lambda documents,metadatas,**kwargs: [[1.,.3,-.1] for _ in documents]
def checkpoint(step):
    if step == sys.argv[3]:
        os._exit(91)
switch.switch_workspace(Path(sys.argv[1]),sys.argv[2],sys.argv[4],checkpoint=checkpoint)
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(data), WORKSPACE, step, target],
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 91, result.stderr.decode()
    committed = step in ("pointer", "publish_state", "pointer_mirrored", "published", "complete")
    assert (workspace_path(data, WORKSPACE) != source) == committed
    with data_lock(data), closing(open_threads(workspace_path(data, WORKSPACE))) as store:
        assert store.get(thread.id) is not None
        assert store.messages(thread.id)[1].text == "second"
    switch.switch_workspace(data, WORKSPACE, target)
    assert switch.doctor_workspace(data, WORKSPACE)["store"] == target
    assert switch.doctor_workspace(data, WORKSPACE)["backfill"] == "complete"
    assert not any(
        path.name.endswith((".source-palace", ".probe", ".rollback-probe"))
        for path in root.parent.iterdir()
    )
    assert switch.prune_generations(data, WORKSPACE, report_only=True)["findings"] == []


def test_all_resolvers_follow_generation_pointer(workspace, monkeypatch):
    import argparse
    from contextlib import ExitStack
    from types import SimpleNamespace

    from commonplace.cli import _stores
    from commonplace.handoff.wiring import HandoffContext
    from commonplace.workers import WorkerSupervisor
    from commonplace.workspace_store import worker_config

    data, root, thread = workspace
    switch.switch_workspace(data, WORKSPACE, "compact")
    active = workspace_path(data, WORKSPACE)
    supervisor = WorkerSupervisor(str(data))
    assert supervisor.workspace_dir(WORKSPACE) == active
    context = HandoffContext(gateway=None, control_db=None, data_dir=data)
    try:
        assert context.has_threads(WORKSPACE)
        assert context.store_for(WORKSPACE).get(thread.id)
        assert (
            context.store_for(WORKSPACE)._conn.execute("PRAGMA application_id").fetchone()[0] != 0
        )
    finally:
        context.close()
    with ExitStack() as stack:
        cli_store = _stores(argparse.Namespace(data=str(data)), stack)(WORKSPACE)
        assert cli_store.get(thread.id)
        assert cli_store._conn.execute("PRAGMA application_id").fetchone()[0] != 0
    seen = []
    monkeypatch.setattr(
        switch.subprocess,
        "run",
        lambda *a, **kw: (
            seen.append(kw["env"]["COMMONPLACE_WORKSPACE_DIR"])
            or SimpleNamespace(
                returncode=0,
                stderr="",
                stdout=json.dumps(
                    {
                        "completed_at": __import__("datetime")
                        .datetime.now(__import__("datetime").timezone.utc)
                        .isoformat(),
                        "off_host": True,
                        "nonce": kw["env"]["COMMONPLACE_BACKUP_NONCE"],
                    }
                ),
            )
        ),
    )
    switch.backup_workspace(root, active, WORKSPACE)
    assert seen == [str(active)]
    monkeypatch.setenv("MEMPALACE_BACKEND", "chroma")
    assert worker_config(active)["backend"] == "compact"
    monkeypatch.setenv("MEMPALACE_CONFIG_DIR", str(active / "config"))
    from mempalace.config import MempalaceConfig

    assert MempalaceConfig().backend == "compact"
    assert switch.doctor_workspace(data, WORKSPACE)["thread_index_version"] == 0


def test_fixed_workspace_thread_paths_are_only_built_by_resolver():
    import ast
    from pathlib import Path

    package = Path(__file__).resolve().parents[1] / "commonplace"
    for path in package.rglob("*.py"):
        if path.name in ("workspace_store.py", "store_switch.py"):
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
                assert not (
                    isinstance(node.right, ast.Constant)
                    and node.right.value in ("threads.sqlite3", "threads.v2.sqlite3")
                ), str(path.relative_to(package))


def test_embedding_process_uses_workspace_config_and_excludes_operator_secrets(
    workspace, monkeypatch
):
    from types import SimpleNamespace

    data, root, _ = workspace
    monkeypatch.setenv("OPENAI_API_KEY", "operator-secret")
    monkeypatch.setenv("MEMPALACE_EMBEDDING_API_URL", "operator-endpoint")
    seen = []

    def run(argv, **kwargs):
        assert argv[-2:] == ["-m", "commonplace.store_embed"]
        assert kwargs["env"]["MEMPALACE_CONFIG_DIR"] == str(root / "config")
        assert "OPENAI_API_KEY" not in kwargs["env"]
        assert "MEMPALACE_EMBEDDING_API_URL" not in kwargs["env"]
        seen.append(json.loads(kwargs["input"]))
        return SimpleNamespace(returncode=0, stdout="[[1,0]]")

    monkeypatch.setattr(switch.subprocess, "run", run)
    assert REAL_EMBEDDER(root)(["text"], [{}]) == [[1, 0]]
    assert seen == [{"documents": ["text"], "metadatas": [{}], "identity": None, "dimension": None}]


def test_cli_store_doctor_and_rollback(workspace, monkeypatch, capsys):
    from commonplace.cli import main
    from commonplace.control_db import ControlDB

    data, root, _ = workspace
    with closing(ControlDB(str(data / "control.sqlite3"))) as db:
        db.create_user("owner@example.test", is_admin=True)
        wid = db.create_workspace("fixture", "owner@example.test")
    root.rename(root.with_name(wid))
    args = ["--data", str(data)]
    assert main(args + ["workspace", "store", "fixture", "compact"]) == 0
    capsys.readouterr()
    assert main(args + ["doctor", "--workspace", "fixture"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["store"] == "compact" and report["thread_index_version"] == 0
    assert main(args + ["workspace", "store", "fixture", "v1"]) == 0
    assert main(args + ["workspace", "prune-stores", "fixture"]) == 0
    capsys.readouterr()
    stable = root.with_name(wid)
    (stable / POINTER_PREVIOUS).write_text("{}")
    assert switch.doctor_workspace(data, wid)["previous_pointer_difference"]
    assert read_json(stable / POINTER_PREVIOUS) == {}
    assert main(args + ["doctor", "--workspace", "fixture"]) == 3
    difference = json.loads(capsys.readouterr().out)
    assert "restore .store-pointer.prev.json" in difference["repair"]
    assert main(args + ["workspace", "prune-stores", "fixture", "--report-only"]) == 3
    assert "previous_pointer_differs" in json.loads(capsys.readouterr().out)["findings"]
    assert read_json(stable / POINTER_PREVIOUS) == {}
    assert main(args + ["workspace", "prune-stores", "fixture"]) == 2
    refusal = capsys.readouterr().err
    assert (
        "restore .store-pointer.prev.json" in refusal
        and "or delete .store-pointer.prev.json" in refusal
    )
    assert read_json(stable / POINTER_PREVIOUS) == {}
    (stable / POINTER_PREVIOUS).unlink()
    assert main(args + ["workspace", "prune-stores", "fixture"]) == 0
    assert read_json(stable / POINTER_PREVIOUS) == read_json(stable / POINTER)
    assert main(args + ["workspace", "prune-stores", "fixture", "--report-only"]) == 0
    capsys.readouterr()
    (stable / switch.STATE).write_text("{")
    assert main(args + ["doctor", "--workspace", "fixture"]) == 0
    corrupt = json.loads(capsys.readouterr().out)
    assert corrupt["backfill"] == "corrupt" and "repair .store-backfill.json" in corrupt["repair"]
    assert main(args + ["workspace", "prune-stores", "fixture", "--report-only"]) == 3


def test_failed_switch_removes_all_staging_and_unchanged_cleans_orphans(workspace):
    data, root, _ = workspace

    def fail(step):
        if step == "palace":
            raise ValueError("injected failure")

    with pytest.raises(ValueError, match="injected"):
        switch.switch_workspace(data, WORKSPACE, "compact", checkpoint=fail)
    assert list(root.parent.glob(root.name + ".generation-*")) == []
    orphan = root.with_name(root.name + ".generation-" + "a" * 24)
    (orphan / "payload").mkdir(parents=True)
    switch.switch_workspace(data, WORKSPACE, "v1")
    assert not orphan.exists()
    orphan.mkdir()
    switch.prune_generations(data, WORKSPACE)
    assert not orphan.exists()


def test_disk_space_gate_precedes_backup(workspace, monkeypatch):
    from types import SimpleNamespace

    data, root, _ = workspace
    monkeypatch.setattr(switch.shutil, "disk_usage", lambda path: SimpleNamespace(free=0))
    monkeypatch.setattr(
        switch, "backup_workspace", lambda *args: pytest.fail("Backup ran before disk gate")
    )
    with pytest.raises(ValueError, match="free space"):
        switch.switch_workspace(data, WORKSPACE, "compact")
    assert workspace_path(data, WORKSPACE) == root


def test_prune_refuses_writes_in_retired_generation(workspace):
    import os

    data, root, _ = workspace
    switch.switch_workspace(data, WORKSPACE, "compact")
    assert (root / "threads.sqlite3").stat().st_mode & 0o222 == 0
    # Simulate an older binary restoring write permissions and adding content.
    (root / "late-record").write_text("new verbatim data")
    retired_at = read_json(root / POINTER)["retained"][0]["retired_at"]
    stamp = int((retired_at + 1) * 1_000_000_000)
    os.utime(root / "late-record", ns=(stamp, stamp))
    with pytest.raises(ValueError, match="newer writes"):
        switch.prune_generations(data, WORKSPACE, now=retired_at + switch.RETENTION_SECONDS + 1)
    assert (root / "late-record").read_text() == "new verbatim data"
    assert switch.doctor_workspace(data, WORKSPACE)["retained_generations"][0]["newer_writes"]


@pytest.mark.parametrize("damage", ["missing", "missing_active", "nonexistent"])
@pytest.mark.parametrize("operation", ["prune", "compact", "v1"])
def test_invalid_pointer_preserves_active_generation_and_post_switch_writes(
    workspace, damage, operation
):
    data, root, _ = workspace
    switch.switch_workspace(data, WORKSPACE, "compact")
    active = workspace_path(data, WORKSPACE)
    with closing(open_threads(active)) as store:
        added = store.store(
            WORKSPACE,
            "after-switch",
            [ThreadMessage(0, "user", "message", "post-switch verbatim", "raw")],
            actor="actor",
        )
    pointer = read_json(root / POINTER)
    if damage == "missing":
        (root / POINTER).unlink()
    else:
        if damage == "missing_active":
            pointer.pop("active")
        else:
            pointer["active"] = root.name + ".generation-" + "f" * 24
        (root / POINTER).write_text(json.dumps(pointer))
    with pytest.raises(ValueError, match="generation pointer"):
        if operation == "prune":
            switch.prune_generations(data, WORKSPACE)
        else:
            switch.switch_workspace(data, WORKSPACE, operation)
    assert active.is_dir()
    with read_store(active / "threads.v2.sqlite3", compact=True) as store:
        assert store.messages(added.id)[0].text == "post-switch verbatim"


def test_missing_pointer_requires_explicit_restore_before_publication_recovery(workspace):
    data, root, _ = workspace
    switch.switch_workspace(data, WORKSPACE, "compact")
    active = workspace_path(data, WORKSPACE)
    (root / switch.STATE).write_text(
        json.dumps({"state": "copying", "staging": active.name, "store": "compact"})
    )
    (root / POINTER).unlink()
    before = _generation_bytes(root, active)
    with pytest.raises(ValueError, match="restore .store-pointer.prev.json"):
        switch.prune_generations(data, WORKSPACE)
    assert _generation_bytes(root, active) == before
    (root / POINTER).write_bytes((root / POINTER_PREVIOUS).read_bytes())
    switch.prune_generations(data, WORKSPACE)
    assert workspace_path(data, WORKSPACE) == active
    assert read_json(root / switch.STATE)["state"] == "complete"
    assert (active / "threads.v2.sqlite3").exists()


def test_invalid_pointer_only_cleans_recorded_unpublished_staging(workspace):
    data, root, _ = workspace
    staged = root.with_name(root.name + ".generation-" + "b" * 24)
    protected = root.with_name(root.name + ".generation-" + "c" * 24)
    protected.mkdir()
    (protected / "post-switch-data").write_text("preserve")
    for path in switch._staging_paths(staged):
        path.mkdir()
        (path / "payload").write_text("unfinished")
    (root / switch.STATE).write_text(json.dumps({"state": "copying", "staging": staged.name}))
    (root / POINTER).write_text("{}")
    with pytest.raises(ValueError, match="generation pointer"):
        switch.prune_generations(data, WORKSPACE)
    assert all(not path.exists() for path in switch._staging_paths(staged))
    assert (protected / "post-switch-data").read_text() == "preserve"
    assert read_json(root / switch.STATE)["state"] == "failed"


def test_doctor_and_unexpired_prune_do_not_hash_retained_data(workspace, monkeypatch):
    data, root, _ = workspace
    switch.switch_workspace(data, WORKSPACE, "compact")
    monkeypatch.setattr(
        switch, "_retirement_digest", lambda *args: pytest.fail("Hashed unexpired generation")
    )
    assert not switch.doctor_workspace(data, WORKSPACE)["retained_generations"][0]["newer_writes"]
    retired_at = read_json(root / POINTER)["retained"][0]["retired_at"]
    switch.prune_generations(data, WORKSPACE, now=retired_at + switch.RETENTION_SECONDS - 1)
    assert (root / "threads.sqlite3").exists()


def test_prune_drops_missing_generation_and_resets_crash_state(workspace):
    import shutil

    data, root, _ = workspace
    switch.switch_workspace(data, WORKSPACE, "compact")
    compact = workspace_path(data, WORKSPACE)
    switch.switch_workspace(data, WORKSPACE, "v1")
    shutil.rmtree(compact)
    staged = root.with_name(root.name + ".generation-" + "d" * 24)
    staged.mkdir()
    (root / switch.STATE).write_text(json.dumps({"state": "copying", "staging": staged.name}))
    switch.prune_generations(data, WORKSPACE)
    assert compact.name not in [item["name"] for item in read_json(root / POINTER)["retained"]]
    assert not staged.exists()
    assert switch.doctor_workspace(data, WORKSPACE)["backfill"] == "failed"


@pytest.mark.parametrize("edit", ["minimal", "partial"])
@pytest.mark.parametrize("operation", ["prune", "switch", "unchanged"])
def test_pointer_edits_refuse_before_switch_or_prune_and_preserve_all_history(
    workspace, monkeypatch, edit, operation
):
    data, root, _ = workspace
    switch.switch_workspace(data, WORKSPACE, "compact")
    retained = workspace_path(data, WORKSPACE)
    switch.switch_workspace(data, WORKSPACE, "v1")
    active = workspace_path(data, WORKSPACE)
    expected = read_json(root / POINTER)
    assert read_json(root / POINTER_PREVIOUS) == expected
    changed = {"active": active.name}
    if edit == "partial":
        changed["retained"] = [expected["retained"][0]]
    (root / POINTER).write_text(json.dumps(changed))
    before = _generation_bytes(root, retained, active)
    monkeypatch.setattr(
        switch, "backup_workspace", lambda *args: pytest.fail("Backup preceded pointer refusal")
    )
    with pytest.raises(ValueError, match="Pointer differs from the last-good prev copy") as error:
        if operation == "prune":
            switch.prune_generations(data, WORKSPACE)
        else:
            switch.switch_workspace(data, WORKSPACE, "compact" if operation == "switch" else "v1")
    assert "restore .store-pointer.prev.json" in str(error.value)
    assert "or delete .store-pointer.prev.json" in str(error.value)
    assert _generation_bytes(root, retained, active) == before
    assert read_json(root / POINTER_PREVIOUS) == expected
    difference = switch.doctor_workspace(data, WORKSPACE)["previous_pointer_difference"]
    assert difference["message"] == "pointer differs from prev copy"
    assert difference["entries"] == sorted(
        [root.name, retained.name] if edit == "minimal" else [retained.name]
    )
    report = switch.prune_generations(data, WORKSPACE, report_only=True)
    assert "previous_pointer_differs" in report["findings"]
    assert "restore .store-pointer.prev.json" in report["repair"]


def _write_after_publication(directory):
    import os

    with closing(open_threads(directory)) as store:
        added = store.store(
            WORKSPACE,
            "after-publication",
            [
                ThreadMessage(
                    0, "user", "message", "post-publication verbatim", "post-publication raw"
                )
            ],
            actor="actor",
        )
    stamp = (directory / ".store-comparison.json").stat().st_mtime_ns + 1_000_000_000
    os.utime(directory / "threads.v2.sqlite3", ns=(stamp, stamp))
    return added


def _generation_bytes(*directories):
    return {
        (directory.name, path.relative_to(directory).as_posix()): path.read_bytes()
        for directory in directories
        for path in directory.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize("transition,phase", [(2, "copy"), (3, "copy"), (3, "rehearsal")])
def test_mid_copy_and_rehearsal_crashes_remove_marked_scratch(workspace, transition, phase):
    import subprocess

    data, root, thread = workspace
    for number in range(1, transition):
        switch.switch_workspace(data, WORKSPACE, "compact" if number % 2 else "v1")
    target = "compact" if transition % 2 else "v1"
    code = """
import os,sys
from pathlib import Path
from commonplace import store_switch as switch
switch._verify_offline = lambda path: None
switch._embedder = lambda directory: lambda documents,metadatas,**kwargs: [[1.,.3,-.1] for _ in documents]
copy_tree = switch._copy_tree
def crash_copy(source, destination, **kwargs):
    copy_tree(source, destination, **kwargs)
    name = Path(destination).name
    if sys.argv[4] == 'copy':
        kill = name.endswith('.source-palace') and '.rollback-probe' not in name
    else:
        kill = name.endswith('.rollback-probe.source-palace')
    if kill:
        os._exit(93)
switch._copy_tree = crash_copy
switch.switch_workspace(Path(sys.argv[1]), sys.argv[2], sys.argv[3])
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(data), WORKSPACE, target, phase],
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 93, result.stderr.decode()
    scratch = [path for path in root.parent.glob(root.name + ".generation-*source-palace")]
    assert scratch and all((path / ".store-comparison.json").exists() for path in scratch)
    report = switch.prune_generations(data, WORKSPACE, report_only=True)
    assert set(path.name for path in scratch) <= set(report["leftover_generation_directories"])
    assert "generation_leftovers" in report["findings"]
    switch.switch_workspace(data, WORKSPACE, target)
    assert all(not path.exists() for path in scratch)
    assert switch.prune_generations(data, WORKSPACE, report_only=True)["findings"] == []
    with closing(open_threads(workspace_path(data, WORKSPACE))) as store:
        assert store.messages(thread.id)[1].text == "second"


def test_report_only_cli_exits_three_for_leftover_scratch(workspace, capsys):
    from commonplace.cli import main
    from commonplace.control_db import ControlDB

    data, root, _ = workspace
    with closing(ControlDB(str(data / "control.sqlite3"))) as db:
        db.create_user("owner@example.test", is_admin=True)
        wid = db.create_workspace("fixture", "owner@example.test")
    root.rename(root.with_name(wid))
    stable = root.with_name(wid)
    switch.switch_workspace(data, wid, "compact")
    scratch = stable.with_name(stable.name + ".generation-" + "e" * 24 + ".source-palace")
    scratch.mkdir()
    (scratch / ".store-comparison.json").write_text("{}")
    assert main(["--data", str(data), "workspace", "prune-stores", "fixture", "--report-only"]) == 3
    report = json.loads(capsys.readouterr().out)
    assert scratch.name in report["leftover_generation_directories"]
    assert scratch.is_dir()


def test_previous_pointer_refresh_allows_only_retained_entries_that_are_gone(workspace):
    import shutil

    data, root, _ = workspace
    switch.switch_workspace(data, WORKSPACE, "compact")
    retired = workspace_path(data, WORKSPACE)
    switch.switch_workspace(data, WORKSPACE, "v1")
    shutil.rmtree(retired)
    pointer = read_json(root / POINTER)
    pointer["retained"] = [entry for entry in pointer["retained"] if entry["name"] != retired.name]
    (root / POINTER).write_text(json.dumps(pointer))
    switch.prune_generations(data, WORKSPACE)
    assert read_json(root / POINTER_PREVIOUS) == pointer


def test_missing_active_directory_does_not_make_prev_disposable(workspace):
    data, root, _ = workspace
    switch.switch_workspace(data, WORKSPACE, "compact")
    first = workspace_path(data, WORKSPACE)
    switch.switch_workspace(data, WORKSPACE, "v1")
    second = workspace_path(data, WORKSPACE)
    previous = (root / POINTER_PREVIOUS).read_bytes()
    moved = second.with_name("moved-active-generation")
    second.rename(moved)
    (root / POINTER).write_text(json.dumps({"active": first.name}))
    before = _generation_bytes(root, first, moved)
    with pytest.raises(ValueError, match="Pointer differs from the last-good prev copy") as error:
        switch.prune_generations(data, WORKSPACE)
    assert second.name in str(error.value) and first.name in str(error.value)
    assert _generation_bytes(root, first, moved) == before
    assert (root / POINTER_PREVIOUS).read_bytes() == previous
    assert workspace_path(data, WORKSPACE) == first


@pytest.mark.parametrize("operation", ["prune", "switch"])
@pytest.mark.parametrize("state", ["copying", "published"])
def test_pending_publication_cannot_mirror_a_partial_pointer_over_prev(workspace, operation, state):
    data, root, _ = workspace
    switch.switch_workspace(data, WORKSPACE, "compact")
    first = workspace_path(data, WORKSPACE)
    previous = (root / POINTER_PREVIOUS).read_bytes()
    switch.switch_workspace(data, WORKSPACE, "v1")
    second = workspace_path(data, WORKSPACE)
    recorded = {"state": state, "staging": second.name}
    if state == "published":
        recorded["pointer"] = read_json(root / POINTER)
    (root / POINTER_PREVIOUS).write_bytes(previous)
    (root / POINTER).write_text(json.dumps({"active": second.name}))
    (root / switch.STATE).write_text(json.dumps(recorded))
    before = _generation_bytes(root, first, second)
    with pytest.raises(ValueError, match="Pointer differs from the last-good prev copy"):
        if operation == "prune":
            switch.prune_generations(data, WORKSPACE)
        else:
            switch.switch_workspace(data, WORKSPACE, "compact")
    assert _generation_bytes(root, first, second) == before
    assert (root / POINTER_PREVIOUS).read_bytes() == previous


@pytest.mark.parametrize(
    "contents,parsed",
    [
        ("{", False),
        ("[]", True),
        ("null", True),
        ('{"active":"ws_missing"}', True),
    ],
)
def test_prev_parseability_is_separate_from_pointer_validity(workspace, contents, parsed):
    data, root, _ = workspace
    pointer = {"active": root.name, "retained": []}
    switch._write_pointer(root, pointer)
    (root / POINTER_PREVIOUS).write_text(contents)
    before = _generation_bytes(root)
    if parsed:
        with pytest.raises(ValueError, match="Pointer differs from the last-good prev copy"):
            switch.prune_generations(data, WORKSPACE)
        assert _generation_bytes(root) == before
    else:
        switch.prune_generations(data, WORKSPACE)
        assert read_json(root / POINTER_PREVIOUS) == pointer


@pytest.mark.parametrize("minimal", [POINTER, POINTER_PREVIOUS])
def test_missing_retained_is_equivalent_to_an_empty_list(workspace, minimal):
    data, root, _ = workspace
    switch._write_pointer(root, {"active": root.name, "retained": []})
    (root / minimal).write_text(json.dumps({"active": root.name}))
    before = [(root / name).read_bytes() for name in (POINTER, POINTER_PREVIOUS)]
    assert switch.doctor_workspace(data, WORKSPACE)["previous_pointer_difference"] is None
    assert switch.prune_generations(data, WORKSPACE, report_only=True)["findings"] == []
    switch.prune_generations(data, WORKSPACE)
    assert switch.switch_workspace(data, WORKSPACE, "v1")["state"] == "unchanged"
    assert [(root / name).read_bytes() for name in (POINTER, POINTER_PREVIOUS)] == before
    assert switch.switch_workspace(data, WORKSPACE, "compact")["state"] == "complete"
    assert read_json(root / POINTER)["retained"][0]["name"] == root.name


@pytest.mark.parametrize("operation", ["resolve", "switch", "prune"])
@pytest.mark.parametrize("contents", ['{"active":"ws_fixture"}', "{}", "{"])
def test_prev_without_main_refuses_even_without_generation_directories(
    workspace, monkeypatch, operation, contents
):
    data, root, _ = workspace
    (root / POINTER_PREVIOUS).write_text(contents)
    assert not list(root.parent.glob(root.name + ".generation-*"))
    before = _generation_bytes(root)
    monkeypatch.setattr(
        switch, "backup_workspace", lambda *args: pytest.fail("Backup preceded pointer refusal")
    )
    with pytest.raises(ValueError, match="Missing workspace generation pointer") as error:
        if operation == "resolve":
            workspace_path(data, WORKSPACE)
        elif operation == "switch":
            switch.switch_workspace(data, WORKSPACE, "compact")
        else:
            switch.prune_generations(data, WORKSPACE)
    if contents == '{"active":"ws_fixture"}':
        assert "restore .store-pointer.prev.json" in str(error.value)
    assert "or delete .store-pointer.prev.json" in str(error.value)
    assert _generation_bytes(root) == before


@pytest.mark.parametrize("pending", ["complete", "copying", "published", "published_stale_prev"])
@pytest.mark.parametrize("remedy", ["restore", "delete"])
def test_missing_main_hint_offers_only_working_recovery_choices(
    workspace, monkeypatch, pending, remedy
):
    import subprocess

    data, root, original = workspace
    if pending == "copying":
        code = """
import os,sys
from pathlib import Path
from commonplace import store_switch as switch
switch._verify_offline = lambda path: None
switch._embedder = lambda directory: lambda documents,metadatas,**kwargs: [[1.,.3,-.1] for _ in documents]
def checkpoint(step):
    if step == 'pointer': os._exit(96)
switch.switch_workspace(Path(sys.argv[1]),sys.argv[2],'compact',checkpoint=checkpoint)
"""
        result = subprocess.run(
            [sys.executable, "-c", code, str(data), WORKSPACE], capture_output=True, timeout=60
        )
        assert result.returncode == 96, result.stderr.decode()
    else:
        switch.switch_workspace(data, WORKSPACE, "compact")
        if pending.startswith("published"):
            switch._record_published(root, read_json(root / POINTER), "compact")
            if pending == "published_stale_prev":
                (root / POINTER_PREVIOUS).write_text(
                    json.dumps({"active": root.name, "retained": []})
                )
    active = workspace_path(data, WORKSPACE)
    (root / POINTER).unlink()
    before = _generation_bytes(root, active)
    with pytest.raises(ValueError, match="Missing workspace generation pointer") as error:
        switch.prune_generations(data, WORKSPACE)
    hint = str(error.value)
    if pending == "published_stale_prev":
        assert "restore .store-pointer.prev.json" not in hint
    else:
        assert "restore .store-pointer.prev.json as .store-pointer.json" in hint
    assert _generation_bytes(root, active) == before
    if pending.startswith("published"):
        assert f"republish the recorded generation {active.name}" in hint
        assert "accept the original workspace root" not in hint
        if pending == "published_stale_prev" and remedy == "restore":
            return
    else:
        assert "delete .store-pointer.prev.json" not in hint
        if remedy == "delete":
            return
    if remedy == "restore":
        (root / POINTER).write_bytes((root / POINTER_PREVIOUS).read_bytes())
    else:
        (root / POINTER_PREVIOUS).unlink()
    switch.prune_generations(data, WORKSPACE)
    resolved = workspace_path(data, WORKSPACE)
    with closing(open_threads(resolved)) as store:
        assert store.messages(original.id)[1].text == "second"
    if pending.startswith("published"):
        assert resolved == active


def test_missing_main_without_generations_can_accept_original_root(workspace):
    data, root, _ = workspace
    switch._write_pointer(root, {"active": root.name})
    (root / POINTER).unlink()
    with pytest.raises(ValueError, match="delete .store-pointer.prev.json to accept the original"):
        switch.prune_generations(data, WORKSPACE)
    (root / POINTER_PREVIOUS).unlink()
    switch.prune_generations(data, WORKSPACE)
    assert switch.switch_workspace(data, WORKSPACE, "v1")["state"] == "unchanged"


@pytest.mark.parametrize("condition", ["missing_prev", "conflict", "conflict_with_difference"])
def test_doctor_and_report_only_agree_on_repair_hints_and_exit_codes(workspace, capsys, condition):
    from commonplace.cli import main
    from commonplace.control_db import ControlDB

    data, root, _ = workspace
    with closing(ControlDB(str(data / "control.sqlite3"))) as db:
        db.create_user("owner@example.test", is_admin=True)
        wid = db.create_workspace("fixture", "owner@example.test")
    root.rename(root.with_name(wid))
    root = root.with_name(wid)
    switch.switch_workspace(data, wid, "compact")
    active = workspace_path(data, wid)
    if condition == "missing_prev":
        (root / POINTER_PREVIOUS).unlink()
    else:
        published = read_json(root / POINTER)
        switch._record_published(root, published, "compact")
        manual = {"active": root.name, "retained": []}
        if condition == "conflict":
            switch._write_pointer(root, manual)
        else:
            (root / POINTER).write_text(json.dumps(manual))
    before = _generation_bytes(root, active)
    args = ["--data", str(data)]
    assert main(args + ["doctor", "--workspace", "fixture"]) == 3
    doctor = json.loads(capsys.readouterr().out)
    assert main(args + ["workspace", "prune-stores", "fixture", "--report-only"]) == 3
    report = json.loads(capsys.readouterr().out)
    assert doctor["repair"] == report["repair"]
    if condition == "missing_prev":
        assert "refresh the prev copy" in report["repair"]
    else:
        assert "recovery conflicts with the valid main pointer" in report["repair"]
        assert "recovery_conflict" in report["findings"]
        if condition == "conflict":
            assert doctor["previous_pointer_difference"] is None
    assert _generation_bytes(root, active) == before


@pytest.mark.parametrize("restore_previous", [False, True])
def test_prune_pointer_write_crash_heals_an_emptied_root(workspace, restore_previous):
    import subprocess

    data, root, _ = workspace
    switch.switch_workspace(data, WORKSPACE, "compact")
    active = workspace_path(data, WORKSPACE)
    retired_at = read_json(root / POINTER)["retained"][0]["retired_at"]
    code = """
import os,sys
from pathlib import Path
from commonplace import store_switch as switch
switch._verify_offline = lambda path: None
write_json = switch.atomic_write_json
def crash_write(path, data, **kwargs):
    write_json(path, data, **kwargs)
    if Path(path).name == switch.POINTER and not data.get('retained'):
        os._exit(94)
switch.atomic_write_json = crash_write
switch.prune_generations(Path(sys.argv[1]),sys.argv[2],now=float(sys.argv[3]))
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(data), WORKSPACE, str(retired_at + 40 * 24 * 60 * 60)],
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 94, result.stderr.decode()
    assert read_json(root / POINTER)["retained"] == []
    assert read_json(root / POINTER_PREVIOUS)["retained"][0]["name"] == root.name
    assert root.is_dir() and not (root / "threads.sqlite3").exists()
    if restore_previous:
        (root / POINTER).write_bytes((root / POINTER_PREVIOUS).read_bytes())
        doctor = switch.doctor_workspace(data, WORKSPACE)
        report = switch.prune_generations(data, WORKSPACE, report_only=True)
        assert doctor["emptied_retained_root"] == report["emptied_retained_root"] == root.name
        assert "emptied_retained_root" in report["findings"]
        assert "prune-stores finishes it" in doctor["repair"] == report["repair"]
    before = _generation_bytes(active)
    switch.prune_generations(data, WORKSPACE)
    assert _generation_bytes(active) == before
    assert read_json(root / POINTER_PREVIOUS) == read_json(root / POINTER)
    assert read_json(root / POINTER)["retained"] == []
    assert switch.prune_generations(data, WORKSPACE, report_only=True)["findings"] == []


@pytest.mark.parametrize("target", ["root", "generation"])
def test_prune_resumes_a_recorded_partial_deletion_after_a_hard_kill(workspace, target):
    import subprocess

    data, root, original = workspace
    switch.switch_workspace(data, WORKSPACE, "compact")
    retired = root
    if target == "generation":
        retired = workspace_path(data, WORKSPACE)
        switch.switch_workspace(data, WORKSPACE, "v1")
    active = workspace_path(data, WORKSPACE)
    now = max(item["retired_at"] for item in read_json(root / POINTER)["retained"]) + 40 * 86400
    code = """
import os,sys
from pathlib import Path
from commonplace import store_switch as switch
switch._verify_offline = lambda path: None
remove = switch._remove_retired_directory
def crash_remove(root, directory):
    if directory.name == sys.argv[3]:
        (directory / ('threads.sqlite3' if directory == root else 'threads.v2.sqlite3')).unlink()
        os._exit(97)
    remove(root, directory)
switch._remove_retired_directory = crash_remove
switch.prune_generations(Path(sys.argv[1]),sys.argv[2],now=float(sys.argv[4]))
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(data), WORKSPACE, retired.name, str(now)],
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 97, result.stderr.decode()
    assert retired.exists() and list((retired / "palace").iterdir())
    assert read_json(root / switch.STATE)["state"] == "deleting"
    doctor = switch.doctor_workspace(data, WORKSPACE)
    report = switch.prune_generations(data, WORKSPACE, report_only=True)
    assert doctor["interrupted_deletions"] == report["interrupted_deletions"]
    assert report["interrupted_deletions"][0]["name"] == retired.name
    assert "the next workspace prune-stores finishes it" in doctor["repair"] == report["repair"]
    with pytest.raises(ValueError, match="Interrupted deletion"):
        switch.switch_workspace(data, WORKSPACE, "compact")
    switch.prune_generations(data, WORKSPACE, now=now)
    assert switch._generation_gone(root, retired)
    assert read_json(root / switch.STATE)["state"] == "complete"
    assert workspace_path(data, WORKSPACE) == active
    with closing(open_threads(active)) as store:
        assert store.messages(original.id)[1].text == "second"


@pytest.mark.parametrize("mutation", ["add", "change"])
def test_recorded_deletion_refuses_changed_files_during_downtime(workspace, monkeypatch, mutation):
    import os

    data, root, _ = workspace
    switch.switch_workspace(data, WORKSPACE, "compact")
    active = workspace_path(data, WORKSPACE)
    retired_at = read_json(root / POINTER)["retained"][0]["retired_at"]
    remove = switch._remove_retired_directory

    def interrupt(*args):
        raise RuntimeError("interrupted")

    monkeypatch.setattr(switch, "_remove_retired_directory", interrupt)
    with pytest.raises(RuntimeError, match="interrupted"):
        switch.prune_generations(data, WORKSPACE, now=retired_at + 40 * 86400)
    relative = "post-crash-write" if mutation == "add" else "palace/custom-sidecar"
    changed = root / relative
    if changed.exists():
        stamp = changed.stat().st_mtime_ns
        changed.chmod(0o600)
        changed.write_text("verbatim new data")
        os.utime(changed, ns=(stamp, stamp))
    else:
        changed.write_text("verbatim new data")
    monkeypatch.setattr(switch, "_remove_retired_directory", remove)
    before = _generation_bytes(root, active)
    with pytest.raises(ValueError, match="remaining files changed or were added") as error:
        switch.prune_generations(data, WORKSPACE, now=retired_at + 40 * 86400)
    assert relative in str(error.value)
    after = _generation_bytes(root, active)
    assert {key: value for key, value in after.items() if key[1] != switch.STATE} == {
        key: value for key, value in before.items() if key[1] != switch.STATE
    }
    assert read_json(root / switch.STATE)["prune_findings"][0]["kind"] == "changed since retirement"


@pytest.mark.parametrize(
    "missing,legacy",
    [("threads.sqlite3", False), ("boards.sqlite3", False), ("palace/chroma.sqlite3", True)],
)
def test_unrecorded_partial_deletion_is_named_and_saved_for_stat_only_doctor(
    workspace, monkeypatch, missing, legacy
):
    data, root, _ = workspace
    switch.switch_workspace(data, WORKSPACE, "compact")
    if legacy:
        pointer = read_json(root / POINTER)
        pointer["retained"][0].pop("inventory")
        switch._write_pointer(root, pointer)
    (root / missing).unlink()
    retired_at = read_json(root / POINTER)["retained"][0]["retired_at"]
    now = retired_at + 40 * 86400
    with monkeypatch.context() as context:
        context.setattr(
            switch,
            "_retirement_digest",
            lambda *args, **kwargs: pytest.fail("Doctor hashed retired data"),
        )
        doctor = switch.doctor_workspace(data, WORKSPACE)
    report = switch.prune_generations(data, WORKSPACE, now=now, report_only=True)
    assert (
        doctor["retired_findings"][0]["kind"]
        == report["retired_findings"][0]["kind"]
        == "incomplete (interrupted deletion)"
    )
    assert missing in report["retired_findings"][0]["missing_files"]
    assert "manual removal" in report["repair"]
    with pytest.raises(ValueError, match=r"incomplete \(interrupted deletion\)") as error:
        switch.prune_generations(data, WORKSPACE, now=now)
    assert "newer writes" not in str(error.value)
    saved = read_json(root / switch.STATE)["prune_findings"]
    assert set(saved[0]) == {"generation", "kind", "at"}
    assert saved[0]["generation"] == root.name
    with monkeypatch.context() as context:
        context.setattr(
            switch,
            "_retirement_digest",
            lambda *args, **kwargs: pytest.fail("Doctor hashed retired data"),
        )
        assert switch.doctor_workspace(data, WORKSPACE)["last_prune_findings"] == saved


@pytest.mark.parametrize(
    "mutation,kind",
    [
        ("backdated", "changed since retirement"),
        ("newer", "newer writes"),
        ("mtime_only", "changed since retirement"),
    ],
)
def test_prune_classifies_changes_and_doctor_uses_saved_findings(
    workspace, monkeypatch, mutation, kind
):
    import os

    data, root, _ = workspace
    switch.switch_workspace(data, WORKSPACE, "compact")
    retired_at = read_json(root / POINTER)["retained"][0]["retired_at"]
    path = root / "palace" / "custom-sidecar"
    old = path.stat().st_mtime_ns
    if mutation != "mtime_only":
        path.chmod(0o600)
        path.write_bytes(b"changed verbatim fixture")
    stamp = old if mutation == "backdated" else int((retired_at + 1) * 1_000_000_000)
    os.utime(path, ns=(stamp, stamp))
    now = retired_at + 40 * 86400
    report = switch.prune_generations(data, WORKSPACE, now=now, report_only=True)
    assert report["retired_findings"][0]["kind"] == kind
    with pytest.raises(ValueError, match=kind):
        switch.prune_generations(data, WORKSPACE, now=now)
    with monkeypatch.context() as context:
        context.setattr(
            switch,
            "_retirement_digest",
            lambda *args, **kwargs: pytest.fail("Doctor hashed retired data"),
        )
        assert switch.doctor_workspace(data, WORKSPACE)["retired_findings"][0]["kind"] == kind


@pytest.mark.parametrize("layout", ["palace_only", "threads_only", "unknown"])
@pytest.mark.parametrize("old_finding", [False, True])
def test_legacy_optional_layouts_are_healthy_and_prunable(
    workspace, monkeypatch, capsys, layout, old_finding
):
    from commonplace.cli import main
    from commonplace.control_db import ControlDB

    data, root, _ = workspace
    with closing(ControlDB(str(data / "control.sqlite3"))) as db:
        db.create_user("owner@example.test", is_admin=True)
        wid = db.create_workspace("fixture", "owner@example.test")
    root.rename(root.with_name(wid))
    root = root.with_name(wid)
    switch.switch_workspace(data, wid, "compact")
    active = workspace_path(data, wid)
    if layout != "threads_only":
        (root / "threads.sqlite3").unlink()
    else:
        import shutil

        shutil.rmtree(root / "palace")
    if layout == "unknown":
        (root / "palace" / "chroma.sqlite3").unlink()
    pointer = read_json(root / POINTER)
    entry = pointer["retained"][0]
    entry.pop("inventory")
    entry["digest"] = switch._retirement_digest(root)
    switch._write_pointer(root, pointer)
    if old_finding:
        switch._save_prune_findings(
            root,
            [
                {
                    "generation": root.name,
                    "kind": "incomplete (interrupted deletion)",
                    "at": entry["retired_at"],
                }
            ],
        )
    with monkeypatch.context() as context:
        context.setattr(
            switch,
            "_retirement_digest",
            lambda *args, **kwargs: pytest.fail("Doctor hashed legacy data"),
        )
        assert main(["--data", str(data), "doctor", "--workspace", "fixture"]) == 0
        doctor = json.loads(capsys.readouterr().out)
    assert doctor["retired_findings"] == []
    verification = doctor["retained_generations"][0]["verification"]
    assert verification == (
        None if layout == "palace_only" else "unverified (legacy entry; prune checks it)"
    )
    now = entry["retired_at"] + 40 * 86400
    assert switch.prune_generations(data, wid, now=now, report_only=True)["findings"] == []
    switch.prune_generations(data, wid, now=now)
    assert switch._generation_gone(root, root)
    assert workspace_path(data, wid) == active
    assert read_json(root / POINTER)["retained"] == []


def test_report_only_hashes_expired_generations_and_doctor_never_hashes(workspace, monkeypatch):
    data, root, _ = workspace
    switch.switch_workspace(data, WORKSPACE, "compact")
    retired_at = read_json(root / POINTER)["retained"][0]["retired_at"]
    digest = switch._retirement_digest
    calls = []

    def observe(directory, **kwargs):
        calls.append(directory)
        return digest(directory, **kwargs)

    monkeypatch.setattr(switch, "_retirement_digest", observe)
    switch.prune_generations(
        data, WORKSPACE, now=retired_at + switch.RETENTION_SECONDS - 1, report_only=True
    )
    assert calls == []
    switch.prune_generations(
        data, WORKSPACE, now=retired_at + switch.RETENTION_SECONDS, report_only=True
    )
    assert calls == [root]
    monkeypatch.setattr(switch.time, "time", lambda: retired_at + 40 * 86400)
    switch.doctor_workspace(data, WORKSPACE)
    assert calls == [root]


@pytest.mark.parametrize("matches_retirement", [True, False])
def test_legacy_compact_threads_with_empty_palace_are_unverified(
    workspace, monkeypatch, capsys, matches_retirement
):
    from commonplace.cli import main
    from commonplace.control_db import ControlDB

    data, root, _ = workspace
    with closing(ControlDB(str(data / "control.sqlite3"))) as db:
        db.create_user("owner@example.test")
        wid = db.create_workspace("fixture", "owner@example.test")
    root.rename(root.with_name(wid))
    root = root.with_name(wid)
    switch.switch_workspace(data, wid, "compact")
    compact = workspace_path(data, wid)
    switch.switch_workspace(data, wid, "v1")
    (compact / "palace" / "compact.sqlite3").unlink()
    pointer = read_json(root / POINTER)
    entry = pointer["retained"][-1]
    entry.pop("inventory")
    if matches_retirement:
        entry["digest"] = switch._retirement_digest(compact)
    switch._write_pointer(root, pointer)
    with monkeypatch.context() as context:
        context.setattr(
            switch, "_retirement_digest", lambda *a, **kw: pytest.fail("Doctor hashed data")
        )
        assert main(["--data", str(data), "doctor", "--workspace", "fixture"]) == 0
    doctor = json.loads(capsys.readouterr().out)
    assert doctor["retired_findings"] == []
    assert doctor["retained_generations"][-1]["verification"] == (
        "unverified (legacy entry; prune checks it)"
    )
    now = entry["retired_at"] + 40 * 86400
    if matches_retirement:
        switch.prune_generations(data, wid, now=now)
        assert not compact.exists()
    else:
        with pytest.raises(ValueError, match="changed since retirement|newer writes"):
            switch.prune_generations(data, wid, now=now)
        assert compact.exists()


@pytest.mark.parametrize("digest_state", ["missing", None, ""])
@pytest.mark.parametrize("legacy", [False, True])
def test_prune_refuses_retired_generation_without_fingerprint(workspace, digest_state, legacy):
    data, root, _ = workspace
    switch.switch_workspace(data, WORKSPACE, "compact")
    active = workspace_path(data, WORKSPACE)
    pointer = read_json(root / POINTER)
    item = pointer["retained"][-1]
    retained = switch.generation_path(root, item["name"])
    before = switch._retirement_digest(retained)
    if digest_state == "missing":
        item.pop("digest")
    else:
        item["digest"] = digest_state
    if legacy:
        item.pop("inventory")
    switch._write_pointer(root, pointer)
    assert read_json(root / switch.STATE)["sealing"] == "complete"
    with pytest.raises(ValueError, match="changed since retirement"):
        switch.prune_generations(data, WORKSPACE, now=item["retired_at"] + 40 * 86400)
    assert retained.is_dir() and active.is_dir()
    assert switch._retirement_digest(retained) == before
    assert read_json(root / POINTER)["retained"] == pointer["retained"]
    assert read_json(root / switch.STATE)["state"] != "deleting"


def test_expiring_prune_hashes_each_generation_once(workspace, monkeypatch):
    data, root, _ = workspace
    switch.switch_workspace(data, WORKSPACE, "compact")
    compact = workspace_path(data, WORKSPACE)
    switch.switch_workspace(data, WORKSPACE, "v1")
    active = workspace_path(data, WORKSPACE)
    calls = []
    digest = switch._retirement_digest

    def observe(directory, **kwargs):
        calls.append(directory)
        return digest(directory, **kwargs)

    monkeypatch.setattr(switch, "_retirement_digest", observe)
    now = read_json(root / POINTER)["retained"][-1]["retired_at"] + 40 * 86400
    switch.prune_generations(data, WORKSPACE, now=now)
    assert calls == [root, compact]
    assert workspace_path(data, WORKSPACE) == active


def test_retirement_inventory_stays_off_runtime_path(workspace, monkeypatch):
    from commonplace import workspace_store as ws

    data, root, _ = workspace
    switch.switch_workspace(data, WORKSPACE, "compact")
    pointer = read_json(root / POINTER)
    item = pointer["retained"][0]
    expected = ws.retired_files(root, item)
    assert expected and "expected_files" not in item
    assert read_json(root / POINTER_PREVIOUS) == pointer
    reads = []
    read = ws.read_json

    def observe(path, *args, **kwargs):
        reads.append(path.name)
        return read(path, *args, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(ws, "read_json", observe)
        context.setattr(
            ws, "evidence_digest", lambda *args: pytest.fail("Resolver hashed evidence")
        )
        for _ in range(100):
            assert workspace_path(data, WORKSPACE).name == pointer["active"]
    assert reads == [POINTER] * 100


def test_existing_inline_inventory_pointer_remains_prunable(workspace):
    from commonplace.workspace_store import retired_files

    data, root, _ = workspace
    switch.switch_workspace(data, WORKSPACE, "compact")
    active = workspace_path(data, WORKSPACE)
    pointer = read_json(root / POINTER)
    item = pointer["retained"][0]
    item["expected_files"] = retired_files(root, item)
    item.pop("inventory")
    switch._write_pointer(root, pointer)
    (root / switch.INVENTORIES).unlink()
    assert switch.doctor_workspace(data, WORKSPACE)["retired_findings"] == []
    switch.switch_workspace(data, WORKSPACE, "v1")
    active = workspace_path(data, WORKSPACE)
    now = read_json(root / POINTER)["retained"][-1]["retired_at"] + 40 * 86400
    switch.prune_generations(data, WORKSPACE, now=now)
    assert workspace_path(data, WORKSPACE) == active
    assert switch._generation_gone(root, root)


@pytest.mark.parametrize("damage", ["missing", "invalid_json", "missing_entry", "changed_files"])
def test_corrupt_inventory_refuses_prune_without_deleting_data(workspace, damage):
    data, root, _ = workspace
    switch.switch_workspace(data, WORKSPACE, "compact")
    active = workspace_path(data, WORKSPACE)
    item = read_json(root / POINTER)["retained"][0]
    path = root / switch.INVENTORIES
    if damage == "missing":
        path.unlink()
    elif damage == "invalid_json":
        path.write_text("{")
    elif damage == "missing_entry":
        path.write_text("{}")
    else:
        path.write_text(json.dumps({item["inventory"]: []}))
    before = _generation_bytes(root, active)
    with pytest.raises(ValueError, match="retired generation inventory"):
        switch.prune_generations(data, WORKSPACE, now=item["retired_at"] + 40 * 86400)
    with pytest.raises(ValueError, match="retired generation inventory"):
        switch.switch_workspace(data, WORKSPACE, "v1")
    assert _generation_bytes(root, active) == before
    assert workspace_path(data, WORKSPACE) == active


@pytest.mark.parametrize("failure", ["inventory", "pointer"])
def test_inventory_publication_failure_preserves_current_writes(workspace, monkeypatch, failure):
    data, root, _ = workspace
    switch.switch_workspace(data, WORKSPACE, "compact")
    active = workspace_path(data, WORKSPACE)
    added = _write_after_publication(active)
    os.utime(active / "threads.v2.sqlite3", None)
    pointer = read_json(root / POINTER)
    write = switch.atomic_write_json

    def interrupt(path, value, **kwargs):
        if path.name == switch.INVENTORIES or (
            failure == "pointer" and path.name == POINTER and value["active"] != active.name
        ):
            if failure == "inventory":
                raise OSError("injected inventory failure")
            if path.name == POINTER:
                raise OSError("injected pointer failure")
        return write(path, value, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(switch, "atomic_write_json", interrupt)
        with pytest.raises(OSError, match="injected"):
            switch.switch_workspace(data, WORKSPACE, "v1")
    assert read_json(root / POINTER) == read_json(root / POINTER_PREVIOUS) == pointer
    assert workspace_path(data, WORKSPACE) == active
    with closing(open_threads(active)) as store:
        assert store.messages(added.id)[0].text == "post-publication verbatim"
    switch.switch_workspace(data, WORKSPACE, "v1")
    with closing(open_threads(workspace_path(data, WORKSPACE))) as store:
        assert store.messages(added.id)[0].text == "post-publication verbatim"


def test_publication_receipt_failure_preserves_committed_generation(workspace, monkeypatch):
    data, root, _ = workspace
    write = switch.atomic_write_json

    def interrupt(path, value, **kwargs):
        if path.name == switch.PUBLICATION:
            raise OSError("injected receipt failure")
        return write(path, value, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(switch, "atomic_write_json", interrupt)
        with pytest.raises(OSError, match="receipt failure"):
            switch.switch_workspace(data, WORKSPACE, "compact")
    active = workspace_path(data, WORKSPACE)
    added = _write_after_publication(active)
    assert read_json(root / switch.STATE)["state"] == "copying"
    switch.prune_generations(data, WORKSPACE)
    assert workspace_path(data, WORKSPACE) == active
    assert read_json(root / switch.PUBLICATION)["pointer"] == read_json(root / POINTER)
    with closing(open_threads(active)) as store:
        assert store.messages(added.id)[0].text == "post-publication verbatim"


@pytest.mark.parametrize(
    "damage", [None, "receipt", "missing_receipt", "history", "marker", "inventory", "state"]
)
def test_pointer_loss_offers_adoption_only_with_intact_evidence(workspace, damage):
    data, root, original = workspace

    def interrupt(step):
        if step == "pointer":
            raise RuntimeError("interrupted after pointer")

    with pytest.raises(RuntimeError, match="after pointer"):
        switch.switch_workspace(data, WORKSPACE, "compact", checkpoint=interrupt)
    active = workspace_path(data, WORKSPACE)
    added = _write_after_publication(active)
    if damage is None:
        os.utime(active / "threads.v2.sqlite3", None)
        backend = CompactBackend()
        try:
            open_collection(backend, active / "palace", "mempalace_drawers", create=True).add(
                ids=["post-publication"],
                documents=["verbatim palace write after publication"],
                metadatas=[{"wing": "test"}],
                embeddings=[[0.5, 1.0, 0.7]],
            )
        finally:
            backend.close()
        with closing(sqlite3.connect(active / "boards.sqlite3")) as conn, conn:
            conn.execute("INSERT INTO fixture VALUES ('later','board write after publication')")
    receipt = read_json(root / switch.PUBLICATION)
    (root / POINTER).unlink()
    if damage == "receipt":
        receipt["pointer"]["retained"] = []
        (root / switch.PUBLICATION).write_text(json.dumps(receipt))
    elif damage == "history":
        (root / POINTER_PREVIOUS).write_text(json.dumps({"active": active.name}))
    elif damage == "missing_receipt":
        (root / switch.PUBLICATION).unlink()
    elif damage == "marker":
        (active / ".store-comparison.json").write_text("{}")
    elif damage == "inventory":
        (root / switch.INVENTORIES).unlink()
    elif damage == "state":
        (root / switch.STATE).write_text("{")
    before = _generation_bytes(root, active)
    with pytest.raises(ValueError) as error:
        workspace_path(data, WORKSPACE)
    hint = str(error.value)
    assert (f"adopt {active.name}" in hint) == (damage is None)
    assert _generation_bytes(root, active) == before
    if damage is not None:
        return
    assert f"restore {POINTER_PREVIOUS} as {POINTER}" in hint
    switch.atomic_write_json(root / POINTER, receipt["pointer"], follow_symlinks=False)
    switch.prune_generations(data, WORKSPACE)
    assert workspace_path(data, WORKSPACE) == active
    switch.switch_workspace(data, WORKSPACE, "v1")
    with closing(open_threads(workspace_path(data, WORKSPACE))) as store:
        assert store.messages(original.id)[1].text == "second"
        assert store.messages(added.id)[0].text == "post-publication verbatim"
    restored = workspace_path(data, WORKSPACE)
    backend = ChromaBackend()
    try:
        result = open_collection(backend, restored / "palace", "mempalace_drawers").get(
            ids=["post-publication"], include=["documents"]
        )
        assert result.documents == ["verbatim palace write after publication"]
    finally:
        backend.close()
    with closing(sqlite3.connect(restored / "boards.sqlite3")) as conn:
        assert conn.execute("SELECT content FROM fixture WHERE id='later'").fetchone() == (
            "board write after publication",
        )


def test_last_prune_findings_survive_a_store_switch(workspace, monkeypatch):
    import os

    data, root, _ = workspace
    switch.switch_workspace(data, WORKSPACE, "compact")
    (root / switch.STATE).unlink()
    path = root / "palace" / "custom-sidecar"
    stamp = path.stat().st_mtime_ns
    path.chmod(0o600)
    path.write_bytes(b"changed original generation")
    os.utime(path, ns=(stamp, stamp))
    retired_at = read_json(root / POINTER)["retained"][0]["retired_at"]
    with pytest.raises(ValueError, match="changed since retirement"):
        switch.prune_generations(data, WORKSPACE, now=retired_at + 40 * 86400)
    saved = read_json(root / switch.STATE)["prune_findings"]
    switch.switch_workspace(data, WORKSPACE, "v1")
    monkeypatch.setattr(
        switch,
        "_retirement_digest",
        lambda *args, **kwargs: pytest.fail("Doctor hashed retained data"),
    )
    assert switch.doctor_workspace(data, WORKSPACE)["last_prune_findings"] == saved


def test_old_root_drop_crash_has_a_rerun_hint(workspace):
    data, root, _ = workspace
    switch.switch_workspace(data, WORKSPACE, "compact")
    previous = (root / POINTER_PREVIOUS).read_bytes()
    retired_at = read_json(root / POINTER)["retained"][0]["retired_at"]
    switch.prune_generations(data, WORKSPACE, now=retired_at + 40 * 86400)
    (root / POINTER_PREVIOUS).write_bytes(previous)
    doctor = switch.doctor_workspace(data, WORKSPACE)
    report = switch.prune_generations(data, WORKSPACE, report_only=True)
    assert "Rerun workspace prune-stores" in doctor["repair"] == report["repair"]
    switch.prune_generations(data, WORKSPACE)
    assert read_json(root / POINTER) == read_json(root / POINTER_PREVIOUS)


@pytest.mark.parametrize("restore", ["stale_previous", "handwritten_root"])
def test_seal_error_refuses_a_conflicting_manual_rollback_without_changes(
    workspace, monkeypatch, restore
):
    data, root, original_thread = workspace
    seal = switch._seal_retired

    def fail_seal(stable, pointer):
        if pointer.get("active") and pointer["active"] != root.name:
            raise OSError("injected seal failure")
        seal(stable, pointer)

    monkeypatch.setattr(switch, "_seal_retired", fail_seal)
    with pytest.raises(OSError, match="injected seal failure"):
        switch.switch_workspace(data, WORKSPACE, "compact")
    active = workspace_path(data, WORKSPACE)
    state = read_json(root / switch.STATE)
    assert state["state"] == "published" and state["sealing"] == "failed"
    doctor = switch.doctor_workspace(data, WORKSPACE)
    assert doctor["backfill"] == "published" and doctor["sealing"] == "failed"
    added = _write_after_publication(active)
    stale = {"active": root.name, "retained": []}
    if restore == "stale_previous":
        (root / POINTER_PREVIOUS).write_text(json.dumps(stale))
        (root / POINTER).write_bytes((root / POINTER_PREVIOUS).read_bytes())
    else:
        (root / POINTER).write_text(json.dumps({"active": root.name}))
    with closing(open_threads(root)) as store:
        manual = store.store(
            WORKSPACE,
            "manual-rollback-write",
            [ThreadMessage(0, "user", "message", "root write after rollback", "root raw")],
            actor="actor",
        )
    monkeypatch.setattr(
        switch, "_seal_retired", lambda *args: pytest.fail("Sealed before refusing conflict")
    )
    before = _generation_bytes(root, active)
    retired_at = state["pointer"]["retained"][-1]["retired_at"]
    with pytest.raises(ValueError, match="recovery conflicts with the valid main pointer"):
        switch.prune_generations(data, WORKSPACE, now=retired_at + 40 * 24 * 60 * 60)
    assert _generation_bytes(root, active) == before
    assert workspace_path(data, WORKSPACE) == root
    with read_store(root / "threads.sqlite3") as store:
        assert store.messages(manual.id)[0].text == "root write after rollback"
    with read_store(active / "threads.v2.sqlite3", compact=True) as store:
        assert store.messages(added.id)[0].text == "post-publication verbatim"
        assert store.messages(original_thread.id)[1].text == "second"
    assert switch.doctor_workspace(data, WORKSPACE)["recovery_conflict"]
    assert (
        "recovery_conflict"
        in switch.prune_generations(data, WORKSPACE, report_only=True)["findings"]
    )


def test_crash_after_pointer_then_stale_restore_preserves_written_staging_at_forty_days(workspace):
    import subprocess
    import time

    data, root, original_thread = workspace
    code = """
import os,sys
from pathlib import Path
from commonplace import store_switch as switch
switch._verify_offline = lambda path: None
switch._embedder = lambda directory: lambda documents,metadatas,**kwargs: [[1.,.3,-.1] for _ in documents]
def checkpoint(step):
    if step == 'pointer':
        os._exit(92)
switch.switch_workspace(Path(sys.argv[1]),sys.argv[2],'compact',checkpoint=checkpoint)
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(data), WORKSPACE], capture_output=True, timeout=60
    )
    assert result.returncode == 92, result.stderr.decode()
    active = workspace_path(data, WORKSPACE)
    assert read_json(root / switch.STATE)["state"] == "copying"
    assert read_json(root / POINTER_PREVIOUS)["active"] == root.name
    added = _write_after_publication(active)
    (root / POINTER).write_bytes((root / POINTER_PREVIOUS).read_bytes())
    switch.prune_generations(data, WORKSPACE, now=time.time() + 40 * 24 * 60 * 60)
    assert active.is_dir()
    with read_store(active / "threads.v2.sqlite3", compact=True) as store:
        assert store.messages(added.id)[0].text == "post-publication verbatim"
        assert store.messages(original_thread.id)[1].text == "second"
    expected = [{"name": active.name, "writes_after_markers": True}]
    assert switch.doctor_workspace(data, WORKSPACE)["preserved_generations"] == expected
    report = switch.prune_generations(data, WORKSPACE, report_only=True)
    assert report["preserved_generations"] == expected
    assert "unlisted_published_generations" in report["findings"]


@pytest.mark.parametrize("evidence", ["pointer", "previous"])
def test_copying_state_finishes_publication_from_either_pointer(workspace, evidence):
    data, root, _ = workspace
    switch.switch_workspace(data, WORKSPACE, "compact")
    active = workspace_path(data, WORKSPACE)
    published = read_json(root / POINTER)
    (root / switch.STATE).write_text(json.dumps({"state": "copying", "staging": active.name}))
    filename = POINTER_PREVIOUS if evidence == "pointer" else POINTER
    (root / filename).write_text(json.dumps({"active": root.name}))
    if evidence == "previous":
        before = _generation_bytes(root, active)
        with pytest.raises(ValueError, match="recovery conflicts with the valid main pointer"):
            switch.prune_generations(data, WORKSPACE)
        assert _generation_bytes(root, active) == before
        assert workspace_path(data, WORKSPACE) == root
        return
    switch.prune_generations(data, WORKSPACE)
    assert read_json(root / POINTER) == published
    assert read_json(root / POINTER_PREVIOUS) == published
    assert read_json(root / switch.STATE)["state"] == "complete"


def test_returned_retired_generation_is_preserved_after_entry_was_dropped(workspace, caplog):
    data, root, _ = workspace
    switch.switch_workspace(data, WORKSPACE, "compact")
    retained = workspace_path(data, WORKSPACE)
    switch.switch_workspace(data, WORKSPACE, "v1")
    moved = retained.with_name("temporarily-moved-generation")
    retained.rename(moved)
    switch.prune_generations(data, WORKSPACE)
    assert retained.name not in [item["name"] for item in read_json(root / POINTER)["retained"]]
    moved.rename(retained)
    before = switch._retirement_digest(retained)
    switch.prune_generations(data, WORKSPACE)
    assert switch._retirement_digest(retained) == before
    assert "Preserving unlisted published generation " + retained.name in caplog.text


@pytest.mark.parametrize("damage", ["missing", "invalid_json", "missing_active", "nonexistent"])
def test_all_resolvers_refuse_lost_pointer_even_after_original_root_is_pruned(workspace, damage):
    import argparse
    from contextlib import ExitStack
    from types import SimpleNamespace

    from commonplace.cli import _stores
    from commonplace.extensions import ToolContext
    from commonplace.gateway import Gateway
    from commonplace.handoff.wiring import HandoffContext
    from commonplace.workers import WorkerSupervisor

    data, root, _ = workspace
    switch.switch_workspace(data, WORKSPACE, "compact")
    active = workspace_path(data, WORKSPACE)
    retired_at = read_json(root / POINTER)["retained"][0]["retired_at"]
    switch.prune_generations(data, WORKSPACE, now=retired_at + switch.RETENTION_SECONDS)
    assert not (root / "threads.sqlite3").exists()
    if damage == "missing":
        (root / POINTER).unlink()
    elif damage == "invalid_json":
        (root / POINTER).write_text("{")
    elif damage == "missing_active":
        (root / POINTER).write_text("{}")
    else:
        (root / POINTER).write_text(json.dumps({"active": root.name + ".generation-" + "f" * 24}))
    # Every entry point ultimately uses the same resolver; none may recreate v1.
    with pytest.raises(ValueError, match=r"generation pointer.*\.store-pointer.prev.json"):
        workspace_path(data, WORKSPACE)
    with pytest.raises(ValueError, match="generation pointer"):
        switch.doctor_workspace(data, WORKSPACE)
    supervisor = WorkerSupervisor(str(data))
    gateway = Gateway.__new__(Gateway)
    gateway.supervisor = supervisor
    tool = ToolContext(principal=SimpleNamespace(workspace_id=WORKSPACE), gateway=gateway)
    context = HandoffContext(gateway=None, control_db=None, data_dir=data)
    for resolve in (
        lambda: supervisor.workspace_dir(WORKSPACE),
        lambda: gateway.workspace_dir(WORKSPACE),
        tool.workspace_dir,
        lambda: context.store_for(WORKSPACE),
    ):
        with pytest.raises(ValueError, match="generation pointer"):
            resolve()
    with ExitStack() as stack, pytest.raises(ValueError, match="generation pointer"):
        _stores(argparse.Namespace(data=str(data)), stack)(WORKSPACE)
    context.close()
    assert active.is_dir() and not (root / "threads.sqlite3").exists()


@pytest.mark.parametrize(
    "damage",
    [
        "{",
        "[]",
        '{"state":"unknown"}',
        '{"state":"copying","staging":7}',
        '{"state":"copying","staging":"../other"}',
        '{"state":"deleting","deletion":{}}',
        '{"state":"complete","prune_findings":[{}]}',
    ],
)
def test_corrupt_state_prune_reports_without_modifying_any_generation(workspace, caplog, damage):
    data, root, _ = workspace
    switch.switch_workspace(data, WORKSPACE, "compact")
    active = workspace_path(data, WORKSPACE)
    staged = root.with_name(root.name + ".generation-" + "e" * 24)
    staged.mkdir()
    (staged / "unfinished").write_text("staging must remain untouched")
    (root / switch.STATE).write_text(damage)
    pointer = (root / POINTER).read_bytes()
    recovery = (root / POINTER_PREVIOUS).read_bytes()
    before = {path.name: switch._retirement_digest(path) for path in (root, active, staged)}
    report = switch.prune_generations(data, WORKSPACE)
    assert report["state"] == "report_only"
    assert report["generations"] == sorted([root.name, active.name, staged.name])
    assert "repair .store-backfill.json" in report["repair"]
    assert "read-only report" in caplog.text
    doctor = switch.doctor_workspace(data, WORKSPACE)
    assert doctor["backfill"] == "corrupt"
    assert "repair .store-backfill.json" in doctor["repair"]
    with pytest.raises(ValueError, match="Corrupt workspace backfill state"):
        switch.switch_workspace(data, WORKSPACE, "v1")
    assert (root / POINTER).read_bytes() == pointer
    assert (root / POINTER_PREVIOUS).read_bytes() == recovery
    assert before == {path.name: switch._retirement_digest(path) for path in (root, active, staged)}


def test_corrupt_retained_thread_database_does_not_block_live_store(workspace):
    data, root, thread = workspace
    switch.switch_workspace(data, WORKSPACE, "compact")
    (root / "threads.sqlite3").chmod(0o600)
    (root / "threads.sqlite3").write_bytes(b"corrupt retired database")
    with closing(open_threads(workspace_path(data, WORKSPACE))) as store:
        assert store.get(thread.id)


def test_prune_cli_authorizes_inside_exclusive_lock(workspace, monkeypatch, capsys):
    import commonplace.cli as cli

    data, _, _ = workspace
    original = cli._db
    opened = []

    def locked_db(args):
        with pytest.raises(BackupError):
            with data_lock(data):
                pytest.fail("Control database opened outside exclusive lock")
        opened.append(True)
        return original(args)

    monkeypatch.setattr(cli, "_db", locked_db)
    assert cli.main(["--data", str(data), "workspace", "prune-stores", "missing"]) == 2
    assert opened and "no such workspace" in capsys.readouterr().err


def test_palace_dual_read_skips_chroma_without_touching_retired_files(workspace):
    import hashlib
    from types import SimpleNamespace

    from mempalace.dual_read import observe_collection

    data, root, _ = workspace
    switch.switch_workspace(data, WORKSPACE, "compact")
    active = workspace_path(data, WORKSPACE)
    before = {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }
    current = SimpleNamespace(get=lambda: "live")
    assert observe_collection(current, str(active / "palace"), "mempalace_drawers") is current
    after = {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }
    assert before == after


def test_thread_dual_read_and_close_from_another_thread(workspace, caplog):
    from concurrent.futures import ThreadPoolExecutor

    from commonplace.handoff.wiring import HandoffContext

    data, root, thread = workspace
    switch.switch_workspace(data, WORKSPACE, "compact")
    context = HandoffContext(gateway=None, control_db=None, data_dir=data)
    store = context.store_for(WORKSPACE)
    store._sample_every = 1
    with (
        caplog.at_level(logging.INFO, logger="mempalace.dual_read"),
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        assert pool.submit(store.messages, thread.id).result()[0].text == "first"
        pool.submit(context.close).result()
    assert "match=True" in caplog.text
    assert "comparison_unavailable" not in caplog.text


def test_rehearsal_probe_does_not_collide_with_existing_collection(workspace):
    data, root, _ = workspace
    backend = ChromaBackend()
    try:
        open_collection(backend, root / "palace", "rollback_verification", create=True).add(
            ids=["probe"],
            documents=["original"],
            metadatas=[{"last_modified": "2026-01-01"}],
            embeddings=[[1.0, 0.0, 0.0, 0.0]],
        )
    finally:
        backend.close()
    switch.switch_workspace(data, WORKSPACE, "compact")


def test_unexpected_source_backend_is_refused_before_backup(workspace, monkeypatch):
    import shutil

    from mempalace.backends import SQLiteExactBackend

    data, root, _ = workspace
    shutil.rmtree(root / "palace")
    backend = SQLiteExactBackend()
    try:
        open_collection(backend, root / "palace", "mempalace_drawers", create=True).add(
            ids=["original"], documents=["verbatim"], embeddings=[[1.0, 2.0]]
        )
    finally:
        backend.close()
    monkeypatch.setattr(
        switch, "backup_workspace", lambda *args: pytest.fail("Backup ran for unsupported backend")
    )
    with pytest.raises(ValueError, match="backend disagrees"):
        switch.switch_workspace(data, WORKSPACE, "compact")
    assert workspace_path(data, WORKSPACE) == root
    assert (root / "palace" / "sqlite_exact.sqlite3").exists()


def test_blank_backup_command_is_a_clear_refusal(workspace, monkeypatch):
    data, root, _ = workspace
    monkeypatch.setenv("COMMONPLACE_BACKUP_CMD", "   ")
    with pytest.raises(BackupError, match="COMMONPLACE_BACKUP_CMD"):
        switch.switch_workspace(data, WORKSPACE, "compact")
    assert workspace_path(data, WORKSPACE) == root


@pytest.mark.parametrize(
    "payload", ["boards.sqlite3", "rules.sqlite3", "config/extra.json", "home/verbatim"]
)
def test_final_verification_checks_all_workspace_files(workspace, payload):
    data, root, _ = workspace
    target = root / payload
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_text("original")

    def corrupt(step):
        if step == "files":
            stage = root.parent / read_json(root / switch.STATE)["staging"]
            (stage / payload).write_bytes(b"changed")

    with pytest.raises((ValueError, sqlite3.Error, OSError)):
        switch.switch_workspace(data, WORKSPACE, "compact", checkpoint=corrupt)
    assert workspace_path(data, WORKSPACE) == root
    assert not list(root.parent.glob(root.name + ".generation-*"))


def test_stale_nonce_receipt_and_backup_diagnostics(workspace, monkeypatch):
    data, root, _ = workspace
    code = "import datetime,json,sys; print('backup diagnostic',file=sys.stderr); print(json.dumps({'completed_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'off_host':True,'nonce':'other-run'}))"
    monkeypatch.setenv("COMMONPLACE_BACKUP_CMD", shlex.join([sys.executable, "-c", code]))
    with pytest.raises(BackupError, match="nonce"):
        switch.switch_workspace(data, WORKSPACE, "compact")
    assert (root / switch.BACKUP_STDERR).read_text() == "backup diagnostic\n"


def test_runtime_ignores_migration_limit_typo_and_refuses_missing_v1_settings(workspace):
    data, root, thread = workspace
    (root / SETTINGS).write_text('{"compact_max_collection_records":"typo"}')
    with closing(open_threads(root)) as store:
        assert store.get(thread.id)
    with pytest.raises(ValueError, match="positive integer"):
        switch.switch_workspace(data, WORKSPACE, "compact")
    (root / SETTINGS).unlink()
    switch.switch_workspace(data, WORKSPACE, "compact")
    active = workspace_path(data, WORKSPACE)
    (active / SETTINGS).unlink()
    with pytest.raises(ValueError, match="disagrees with settings"):
        open_threads(active)
    assert not (active / "threads.sqlite3").exists()


def test_rollback_preflight_refuses_surrogates_before_backup(workspace, monkeypatch):
    data, root, _ = workspace
    switch.switch_workspace(data, WORKSPACE, "compact")
    with closing(open_threads(workspace_path(data, WORKSPACE))) as store:
        store.store(
            WORKSPACE,
            "unrepresentable",
            [ThreadMessage(0, "user", "message", "\ud800", "\ud800")],
            actor="actor",
        )
    monkeypatch.setattr(
        switch, "backup_workspace", lambda *args: pytest.fail("Backup ran before surrogate gate")
    )
    with pytest.raises(ValueError, match="cannot be represented"):
        switch.switch_workspace(data, WORKSPACE, "v1")


@pytest.mark.parametrize(
    "identity,dimension",
    [
        ({"model_name": "other-model", "dimension": 3}, 3),
        ({"model_name": "fixture-model", "dimension": 4}, 4),
    ],
)
def test_store_embed_refuses_wrong_model_or_dimension_before_embedding(
    monkeypatch, identity, dimension
):
    import io

    import mempalace.backends.embedding_wrapper as wrapper
    import mempalace.embedding as embedding
    from commonplace.store_embed import main
    from mempalace.backends.base import EmbedderIdentity

    monkeypatch.setattr(
        embedding, "get_embedder_identity", lambda: EmbedderIdentity("fixture-model", 3)
    )
    monkeypatch.setattr(
        wrapper, "_embed_texts", lambda *a, **kw: pytest.fail("Embedded before identity check")
    )
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "documents": ["text"],
                    "metadatas": [{}],
                    "identity": identity,
                    "dimension": dimension,
                }
            )
        ),
    )
    with pytest.raises(ValueError, match="does not match stored"):
        main()


def test_store_embed_checks_identity_and_embeds_keyword_probe(monkeypatch, capsys):
    import io

    import mempalace.backends.embedding_wrapper as wrapper
    import mempalace.embedding as embedding
    from commonplace.store_embed import main
    from mempalace.backends.base import EmbedderIdentity
    from mempalace.backends.compact import KEYWORD_ONLY

    seen = []
    monkeypatch.setattr(
        embedding, "get_embedder_identity", lambda: EmbedderIdentity("fixture-model", 3)
    )
    monkeypatch.setattr(
        wrapper, "_embed_texts", lambda documents, **kw: seen.extend(documents) or [[1.0, 0.0, 0.0]]
    )
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "documents": ["keyword probe"],
                    "metadatas": [{KEYWORD_ONLY: True}],
                    "identity": {"model_name": "fixture-model", "dimension": 3},
                    "dimension": 3,
                }
            )
        ),
    )
    main()
    assert seen == ["keyword probe"]
    assert json.loads(capsys.readouterr().out) == [[1.0, 0.0, 0.0]]


def test_keyword_estimate_precedes_backup_and_rehearsal_exercises_it(
    workspace, monkeypatch, caplog
):
    from mempalace.backends.compact import KEYWORD_ONLY

    data, root, _ = workspace
    calls = []

    def factory(directory):
        def embed(documents, metadatas, *, identity=None, dimension=None):
            assert dimension == 3
            assert all(meta.get(KEYWORD_ONLY) is True for meta in metadatas)
            calls.extend(documents)
            return [[1.0, 0.0, 0.0] for _ in documents]

        return embed

    monkeypatch.setattr(switch, "_embedder", factory)
    switch.switch_workspace(data, WORKSPACE, "compact")
    assert "rollback keyword verification" in calls
    active = workspace_path(data, WORKSPACE)
    backend = CompactBackend()
    try:
        open_collection(backend, active / "palace", "mempalace_drawers", create=True).add(
            ids=["keyword"], documents=["verbatim"], metadatas=[{KEYWORD_ONLY: True}]
        )
    finally:
        backend.close()

    def before_backup(*args):
        assert "Rollback re-embedding count=1" in caplog.text
        raise BackupError("stopped before backup")

    caplog.clear()
    monkeypatch.setattr(switch, "backup_workspace", before_backup)
    with caplog.at_level(logging.WARNING), pytest.raises(BackupError, match="before backup"):
        switch.switch_workspace(data, WORKSPACE, "v1")


def test_palace_lock_namespace_does_not_share_reentrant_credit(tmp_path):
    import os
    import subprocess

    from mempalace.palace import mine_palace_lock

    palace = str(tmp_path / "palace")
    other = str(tmp_path / "other-locks")
    with mine_palace_lock(palace), mine_palace_lock(palace, lock_dir=other):
        code = """
import sys
from mempalace.palace import mine_palace_lock, MineAlreadyRunning
try:
    with mine_palace_lock(sys.argv[1], lock_dir=sys.argv[2]):
        raise AssertionError('Namespaced lease was bypassed')
except MineAlreadyRunning:
    pass
"""
        result = subprocess.run(
            [sys.executable, "-c", code, palace, other],
            env=dict(os.environ),
            capture_output=True,
            timeout=20,
        )
        assert result.returncode == 0, result.stderr.decode()


def test_unknown_workspace_cli_refuses_without_traceback(workspace, capsys):
    from commonplace.cli import main

    data, _, _ = workspace
    assert main(["--data", str(data), "workspace", "store", "missing", "compact"]) == 2
    error = capsys.readouterr().err
    assert "no such workspace" in error and "Traceback" not in error


def test_backup_completion_must_follow_this_command_start(workspace, monkeypatch):
    import os
    import time
    from datetime import datetime, timezone
    from types import SimpleNamespace

    data, root, _ = workspace
    for path in root.rglob("*"):
        if path.is_file():
            os.utime(path, (time.time() - 100, time.time() - 100))

    def completed_before_start(*args, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stderr="",
            stdout=json.dumps(
                {
                    "completed_at": datetime.fromtimestamp(
                        time.time() - 1, timezone.utc
                    ).isoformat(),
                    "off_host": True,
                    "nonce": kwargs["env"]["COMMONPLACE_BACKUP_NONCE"],
                }
            ),
        )

    monkeypatch.setattr(switch.subprocess, "run", completed_before_start)
    with pytest.raises(BackupError, match="after its start"):
        switch.backup_workspace(root, root, WORKSPACE)


def test_size_gate_runs_before_backup(workspace, monkeypatch):
    data, root, _ = workspace
    (root / SETTINGS).write_text('{"compact_max_collection_records":0}')
    monkeypatch.setattr(
        switch, "backup_workspace", lambda *args: pytest.fail("Backup ran before size gate")
    )
    with pytest.raises(ValueError, match="positive integer"):
        switch.switch_workspace(data, WORKSPACE, "compact")


def test_mirror_progress_survives_switch_rollback_and_resume(workspace, monkeypatch):
    from types import SimpleNamespace

    from commonplace.handoff.palace_mirror import mirror_pending
    from mempalace import mcp_server as server
    from mempalace.backends.embedding_wrapper import EmbeddingCollection

    data, root, thread = workspace
    messages = [
        ThreadMessage(0, "user", "message", "first", "raw first"),
        ThreadMessage(1, "assistant", "message", "second", "raw second"),
    ]
    monkeypatch.setattr(
        "mempalace.backends.embedding_wrapper._embed_texts",
        lambda texts, **kwargs: [[1.0, 0.3, -0.1] for _ in texts],
    )
    monkeypatch.setattr(server, "_wal_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "_wal_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "_invalidate_overview_caches", lambda: None)
    monkeypatch.setattr(server, "_is_chroma_backend", lambda: False)
    monkeypatch.setattr(server, "_palace_known_wings", lambda: frozenset({"private_threads"}))

    def resume(directory, kind, extra=None):
        backend = CompactBackend() if kind == "compact" else ChromaBackend()
        try:
            col = EmbeddingCollection(
                open_collection(backend, directory / "palace", "mempalace_drawers", create=True)
            )
            monkeypatch.setattr(server, "_get_collection", lambda **kwargs: col)
            monkeypatch.setattr(
                server,
                "_config",
                SimpleNamespace(
                    palace_path=str(directory / "palace"),
                    collection_name="mempalace_drawers",
                    chunk_size=800,
                ),
            )

            def call(principal, name, arguments):
                return (
                    server.write_mirror_units(**arguments)
                    if name == "write_mirror_units"
                    else server.TOOLS[name]["handler"](**arguments)
                )

            with closing(open_threads(directory)) as store:
                if extra is not None:
                    messages.append(extra)
                    store.append(thread.id, messages)
                ids = mirror_pending(call, None, store.get(thread.id), store)
                assert store.mirror_progress(thread.id) == (len(messages) - 1, len(messages) - 1)
                assert mirror_pending(call, None, store.get(thread.id), store) == []
                return ids, col.get(include=["documents", "metadatas"])
        finally:
            backend.close()

    old_ids, old = resume(root, "v1")
    assert len(old_ids) == 1
    switch.switch_workspace(data, WORKSPACE, "compact")
    active = workspace_path(data, WORKSPACE)
    with closing(open_threads(active)) as store:
        assert store.mirror_progress(thread.id) == (1, 1)
    compact_ids, compact = resume(
        active, "compact", ThreadMessage(2, "tool", "tool_result", "Tool output", "raw output")
    )
    assert len(compact_ids) == 1 and set(old.ids) <= set(compact.ids)
    switch.switch_workspace(data, WORKSPACE, "v1")
    restored = workspace_path(data, WORKSPACE)
    with closing(open_threads(restored)) as store:
        assert store.mirror_progress(thread.id) == (2, 2)
    new_ids, final = resume(
        restored,
        "v1",
        ThreadMessage(3, "assistant", "message", "After rollback", "raw after rollback"),
    )
    assert len(new_ids) == 1
    assert set(final.ids) == set(old.ids) | set(compact_ids) | set(new_ids)
    assert len(set(old_ids + compact_ids + new_ids)) == 3
