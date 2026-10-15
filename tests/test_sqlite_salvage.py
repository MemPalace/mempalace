"""Offline salvage must preserve exact recoverable text and original files."""

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time

import pytest

from mempalace import sqlite_salvage as salvage


def _palace(path, texts=("Exact\noriginal words", "日本語 — ação 🚀")):
    path.mkdir()
    with closing(sqlite3.connect(path / "chroma.sqlite3")) as conn:
        conn.executescript(
            """
            CREATE TABLE collections(id TEXT PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE segments(id TEXT PRIMARY KEY, collection TEXT NOT NULL, scope TEXT);
            CREATE TABLE embeddings(id INTEGER PRIMARY KEY, segment_id TEXT, embedding_id TEXT);
            CREATE TABLE embedding_metadata(id INTEGER, key TEXT, string_value TEXT,
              int_value INTEGER, float_value REAL, bool_value INTEGER, PRIMARY KEY(id,key));
            CREATE TABLE embeddings_queue(seq_id INTEGER PRIMARY KEY, document TEXT);
            INSERT INTO collections VALUES('collection', 'mempalace_drawers');
            INSERT INTO segments VALUES('segment', 'collection', 'METADATA');
            """
        )
        for i, text in enumerate(texts, 1):
            conn.execute("INSERT INTO embeddings VALUES(?, 'segment', ?)", (i, f"drawer-{i}"))
            conn.execute(
                "INSERT INTO embedding_metadata(id,key,string_value) VALUES(?, 'chroma:document', ?)",
                (i, text),
            )
            conn.execute(
                "INSERT INTO embedding_metadata(id,key,string_value) VALUES(?, 'wing', 'original')",
                (i,),
            )
        conn.commit()
    (path / "index.bin").write_bytes(b"untouched HNSW bytes")
    return path


def _damage_header(source):
    db = source / "chroma.sqlite3"
    with open(db, "r+b") as file:
        file.seek(5)
        file.write(b"\x17\x03\x03\x00\x13\x83\x1e\xdar\xfb\xce")


def _hashes(source):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in source.iterdir()}


def _skip_or_fail(reason):
    # CI installs a recover-capable sqlite3 and sets this, so the real-CLI
    # tests cannot silently skip there.
    if os.environ.get("MEMPALACE_REQUIRE_SQLITE_RECOVER"):
        pytest.fail(f"MEMPALACE_REQUIRE_SQLITE_RECOVER is set but {reason}")
    pytest.skip(reason)


def _require_recover():
    binary = shutil.which("sqlite3")
    if binary is None:
        _skip_or_fail("local sqlite3 CLI not installed")
    probe = subprocess.run(
        [
            binary,
            "-safe",
            "-batch",
            "-bail",
            "-init",
            os.devnull,
            ":memory:",
            ".recover --ignore-freelist",
        ],
        capture_output=True,
        timeout=10,
    )
    # Some distro CLIs advertise .recover in help but omit sqlite_dbpage from
    # their linked SQLite library. Exercise recovery, rather than its help text.
    if probe.returncode:
        _skip_or_fail("local sqlite3 CLI has no working .recover and -safe support")


def test_real_recovery_gate_checks_execution_not_advertised_help(monkeypatch):
    monkeypatch.delenv("MEMPALACE_REQUIRE_SQLITE_RECOVER", raising=False)
    monkeypatch.setattr(shutil, "which", lambda _: "sqlite3")
    commands = []

    def missing_dbpage(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(
            command, 1, stdout=b"", stderr=b"sql error: no such table: sqlite_dbpage"
        )

    monkeypatch.setattr(subprocess, "run", missing_dbpage)
    with pytest.raises(pytest.skip.Exception, match="no working .recover"):
        _require_recover()
    assert commands[0][-1] == ".recover --ignore-freelist"


def test_real_corrupt_header_recovers_exact_text_without_modifying_source(tmp_path, monkeypatch):
    _require_recover()
    texts = ("exact original\nwith indentation\n  preserved", "中文, português, 🚀", "")
    source = _palace(tmp_path / "source", texts)
    _damage_header(source)
    before = _hashes(source)
    dest = tmp_path / "recovered"
    # Opening Chroma or resolving an embedder here would touch corrupt data or
    # make offline recovery depend on unrelated model configuration.
    import chromadb
    import mempalace.embedding

    monkeypatch.setattr(chromadb, "PersistentClient", lambda *a, **k: pytest.fail("opened Chroma"))
    monkeypatch.setattr(
        mempalace.embedding, "get_embedding_function", lambda *a, **k: pytest.fail("embedded")
    )
    manifest = salvage.salvage_sqlite(str(source), str(dest), progress=lambda _: None)
    assert _hashes(source) == before
    assert manifest["census"]["document_rows"] == 3
    assert manifest["status"] == "salvaged-requires-content-review"
    assert not (dest / "index.bin").exists()
    assert json.loads((dest / "salvage-manifest.json").read_text()) == manifest
    assert manifest["source_files"]["chroma.sqlite3"]["sha256"] == before["chroma.sqlite3"]
    assert (dest / "source-snapshot" / "chroma.sqlite3").read_bytes() == (
        source / "chroma.sqlite3"
    ).read_bytes()
    with closing(sqlite3.connect(dest / "chroma.sqlite3")) as conn:
        recovered = conn.execute(
            "SELECT e.embedding_id, m.string_value FROM embeddings e "
            "JOIN embedding_metadata m ON m.id=e.id WHERE m.key='chroma:document' ORDER BY e.id"
        ).fetchall()
        assert recovered == [(f"drawer-{i}", text) for i, text in enumerate(texts, 1)]
        assert (
            conn.execute("SELECT string_value FROM embedding_metadata WHERE key='wing'").fetchall()
            == [("original",)] * 3
        )


def test_dry_run_never_writes_or_launches_subprocess(tmp_path, monkeypatch):
    source = _palace(tmp_path / "source")
    before = _hashes(source)
    monkeypatch.setattr(salvage.subprocess, "run", lambda *a, **k: pytest.fail("subprocess"))
    monkeypatch.setattr(salvage.tempfile, "mkdtemp", lambda *a, **k: pytest.fail("staging"))
    result = salvage.salvage_sqlite(str(source), str(tmp_path / "dest"), dry_run=True)
    assert result["dry_run"]
    assert _hashes(source) == before
    assert not (tmp_path / "dest").exists()


@pytest.mark.parametrize("target", ["same", "nested", "ancestor", "existing", "missing-parent"])
def test_destination_refused_before_copy(tmp_path, target):
    source = _palace(tmp_path / "source")
    before = _hashes(source)
    targets = {
        "same": source,
        "nested": source / "salvage",
        "ancestor": tmp_path,
        "existing": tmp_path / "exists",
        "missing-parent": tmp_path / "absent" / "dest",
    }
    targets["existing"].mkdir()
    with pytest.raises(salvage.SQLiteSalvageError):
        salvage.salvage_sqlite(str(source), str(targets[target]))
    assert _hashes(source) == before
    assert not list(tmp_path.glob(".*.salvage-*"))


@pytest.mark.parametrize(
    "entry", ["source-dir", "destination", "chroma.sqlite3", "chroma.sqlite3-wal"]
)
def test_symlink_input_or_destination_is_refused(tmp_path, entry):
    source = _palace(tmp_path / "source")
    dest = tmp_path / "dest"
    src_arg = source
    try:
        if entry == "source-dir":
            src_arg = tmp_path / "linked-source"
            src_arg.symlink_to(source, target_is_directory=True)
        elif entry == "destination":
            dest.symlink_to(tmp_path / "absent", target_is_directory=True)
        else:
            f = source / entry
            if f.exists():
                f.rename(tmp_path / "original.sqlite3")
            f.symlink_to(tmp_path / "original.sqlite3")
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(salvage.SQLiteSalvageError, match="symlink|regular file"):
        salvage.salvage_sqlite(str(src_arg), str(dest), dry_run=True)


@pytest.mark.parametrize("source_arg,dest_arg", [(None, "dest"), ("source", None)])
def test_explicit_source_and_destination_required(tmp_path, source_arg, dest_arg):
    with pytest.raises(salvage.SQLiteSalvageError, match="explicit"):
        salvage.salvage_sqlite(source_arg, dest_arg, dry_run=True)


def test_missing_cli_creates_no_output(tmp_path, monkeypatch):
    source = _palace(tmp_path / "source")
    before = _hashes(source)
    monkeypatch.setattr(salvage.shutil, "which", lambda _: None)
    with pytest.raises(salvage.SQLiteSalvageError, match="CLI was not found"):
        salvage.salvage_sqlite(str(source), str(tmp_path / "dest"))
    assert _hashes(source) == before
    assert not list(tmp_path.glob(".*.salvage-*"))


def test_live_writer_refuses_snapshot(tmp_path, monkeypatch):
    from mempalace import palace

    source = _palace(tmp_path / "source")
    before = _hashes(source)
    monkeypatch.setattr(salvage.shutil, "which", lambda _: "sqlite3")

    def locked(_):
        raise palace.MineAlreadyRunning("held by live owner")

    monkeypatch.setattr(palace, "mine_palace_lock", locked)
    with pytest.raises(salvage.SQLiteSalvageError, match="live writer"):
        salvage.salvage_sqlite(str(source), str(tmp_path / "dest"))
    assert _hashes(source) == before
    assert not list(tmp_path.glob(".*.salvage-*"))


def test_snapshot_detects_uncoordinated_write_and_preserves_evidence(tmp_path, monkeypatch):
    source = _palace(tmp_path / "source")
    injected = (source / "chroma.sqlite3").read_bytes() + b"external writer changed size"
    fsync = salvage.os.fsync
    mutated = False

    def mutate(fd):
        nonlocal mutated
        fsync(fd)
        if not mutated:
            mutated = True
            (source / "chroma.sqlite3").write_bytes(injected)

    monkeypatch.setattr(salvage.os, "fsync", mutate)
    monkeypatch.setattr(salvage.shutil, "which", lambda _: "sqlite3")
    with pytest.raises(salvage.SQLiteSalvageError, match="changed during.*Forensic files retained"):
        salvage.salvage_sqlite(str(source), str(tmp_path / "dest"))
    assert (source / "chroma.sqlite3").read_bytes() == injected
    assert not (tmp_path / "dest").exists()
    assert len(list(tmp_path.glob(".dest.salvage-*"))) == 1


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), True, "300"])
def test_invalid_timeout_refused_before_copy(tmp_path, timeout):
    source = _palace(tmp_path / "source")
    with pytest.raises(salvage.SQLiteSalvageError, match="positive finite"):
        salvage.salvage_sqlite(str(source), str(tmp_path / "dest"), timeout=timeout)
    assert not list(tmp_path.glob(".*.salvage-*"))


def test_timed_out_subprocess_retains_snapshot(tmp_path, monkeypatch):
    source = _palace(tmp_path / "source")
    before = _hashes(source)
    monkeypatch.setattr(salvage.shutil, "which", lambda _: "sqlite3")

    def hang(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(salvage.subprocess, "run", hang)
    with pytest.raises(salvage.SQLiteSalvageError, match="timed out.*Forensic files retained"):
        salvage.salvage_sqlite(str(source), str(tmp_path / "dest"))
    assert _hashes(source) == before
    assert not (tmp_path / "dest").exists()
    stage = next(tmp_path.glob(".dest.salvage-*"))
    assert (stage / "source-snapshot" / "chroma.sqlite3").read_bytes() == (
        source / "chroma.sqlite3"
    ).read_bytes()


def test_failed_cli_diagnostic_reads_bounded_prefix(tmp_path, monkeypatch):
    error = tmp_path / "error.txt"

    def fail(command, **kwargs):
        kwargs["stderr"].write(b"unsupported .recover\n" + b"x" * (2 * 1024 * 1024))
        return subprocess.CompletedProcess(command, 1)

    monkeypatch.setattr(salvage.subprocess, "run", fail)
    monkeypatch.setattr(Path, "read_text", lambda *a, **k: pytest.fail("read entire diagnostic"))
    with pytest.raises(salvage.SQLiteSalvageError, match="recover and -safe support") as err:
        salvage._run_sqlite(["sqlite3"], deadline=time.monotonic() + 10, error_file=error)
    assert len(str(err.value)) < 1700
    assert error.stat().st_size > 2 * 1024 * 1024


def test_missing_dbpage_recovery_fails_without_publishing_or_changing_source(tmp_path, monkeypatch):
    source = _palace(tmp_path / "source")
    before = _hashes(source)
    monkeypatch.setattr(salvage.shutil, "which", lambda _: "sqlite3")

    def missing_dbpage(command, **kwargs):
        if command[-1] == ".recover --ignore-freelist":
            kwargs["stderr"].write(b"sql error: no such table: sqlite_dbpage (1)")
            return subprocess.CompletedProcess(command, 1)
        kwargs["stdout"].write(b"SQLite test version")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(salvage.subprocess, "run", missing_dbpage)
    with pytest.raises(salvage.SQLiteSalvageError, match="sqlite_dbpage.*Forensic files retained"):
        salvage.salvage_sqlite(str(source), str(tmp_path / "dest"))
    assert _hashes(source) == before
    assert not (tmp_path / "dest").exists()
    stage = next(tmp_path.glob(".dest.salvage-*"))
    assert _hashes(stage / "source-snapshot") == {"chroma.sqlite3": before["chroma.sqlite3"]}


def test_census_requires_chroma_schema_and_stored_document_text(tmp_path):
    db = tmp_path / "unrelated.sqlite3"
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("CREATE TABLE words(text TEXT)")
    with pytest.raises(salvage.SQLiteSalvageError, match="lacks Chroma table"):
        salvage._census(db, deadline=time.monotonic() + 10)
    source = _palace(tmp_path / "source", texts=())
    with pytest.raises(salvage.SQLiteSalvageError, match="No stored Chroma document text"):
        salvage._census(source / "chroma.sqlite3", deadline=time.monotonic() + 10)


def test_census_reports_pending_and_orphan_records(tmp_path):
    source = _palace(tmp_path / "source")
    with closing(sqlite3.connect(source / "chroma.sqlite3")) as conn:
        conn.executescript(
            """
            INSERT INTO embeddings VALUES(99, 'missing', 'unassigned');
            INSERT INTO embedding_metadata(id,key,string_value) VALUES(999,'wing','missing');
            INSERT INTO embeddings_queue VALUES(1, 'pending exact text');
            CREATE TABLE lost_and_found(rootpgno, pgno, nfield, c0);
            INSERT INTO lost_and_found VALUES(1,2,1,'unassigned exact text');
            """
        )
    census = salvage._census(source / "chroma.sqlite3", deadline=time.monotonic() + 10)
    assert census["document_rows"] == 2
    assert census["pending_queue_rows"] == 1
    assert census["orphan_embedding_rows"] == 1
    assert census["orphan_metadata_rows"] == 1
    assert census["lost_and_found_rows"] == 1


def test_cli_salvage_bypasses_normal_repair_and_bad_embedding_config(tmp_path, monkeypatch):
    import mempalace.cli as cli

    source = _palace(tmp_path / "source")
    monkeypatch.setattr(cli, "_cmd_regular_repair", lambda _: pytest.fail("normal repair"))
    monkeypatch.setattr(cli, "_exit_on_misspelled_embedding_model", lambda: pytest.fail("model"))
    monkeypatch.setenv("MEMPALACE_EMBEDDING_MODEL", "typo-does-not-matter-for-salvage")
    cli.cmd_repair(
        argparse.Namespace(
            mode="salvage-sqlite", source=str(source), palace=str(tmp_path / "dest"), dry_run=True
        )
    )
    assert not (tmp_path / "dest").exists()


@pytest.mark.parametrize("incompatible", ["repair_action", "archive_existing"])
def test_cli_salvage_rejects_incompatible_actions_before_output(tmp_path, incompatible):
    import mempalace.cli as cli

    args = argparse.Namespace(mode="salvage-sqlite", source=None, palace=None)
    setattr(args, incompatible, "rebuild-index" if incompatible == "repair_action" else True)
    with pytest.raises(SystemExit) as error:
        cli.cmd_repair(args)
    assert error.value.code == 2


def _holder(source, script):
    import sys

    process = subprocess.Popen(
        [sys.executable, "-u", "-c", script, str(source)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    ready = process.stdout.readline().strip()
    if ready != "READY":
        process.kill()
        _, error = process.communicate(timeout=10)
        pytest.fail(f"scratch holder did not start: {ready} {error}")
    return process


def _release_holder(process):
    try:
        process.communicate(input="stop\n", timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)


def test_real_other_process_writer_lease_refuses_before_copy(tmp_path):
    _require_recover()
    source = _palace(tmp_path / "source")
    before = _hashes(source)
    holder = _holder(
        source,
        "import sys\nfrom mempalace.palace import mine_palace_lock\n"
        "with mine_palace_lock(sys.argv[1]):\n print('READY',flush=True)\n sys.stdin.readline()\n",
    )
    try:
        with pytest.raises(salvage.SQLiteSalvageError, match="live writer"):
            salvage.salvage_sqlite(str(source), str(tmp_path / "dest"))
        assert _hashes(source) == before
        assert not list(tmp_path.glob(".dest.salvage-*"))
    finally:
        _release_holder(holder)


def test_real_wal_snapshot_preserves_db_wal_shm_hashes_and_uncheckpointed_text(tmp_path):
    _require_recover()
    source = _palace(tmp_path / "source")
    holder = _holder(
        source,
        "import sys,sqlite3\nfrom pathlib import Path\n"
        "c=sqlite3.connect(Path(sys.argv[1])/'chroma.sqlite3')\n"
        "c.execute('PRAGMA journal_mode=WAL')\n"
        "c.execute('PRAGMA wal_autocheckpoint=0')\n"
        "c.execute(\"INSERT INTO embeddings VALUES(3,'segment','wal-drawer')\")\n"
        'c.execute("INSERT INTO embedding_metadata(id,key,string_value) '
        "VALUES(3,'chroma:document','exact uncheckpointed WAL text')\")\n"
        "c.commit()\nprint('READY',flush=True)\nsys.stdin.readline()\nc.close()\n",
    )
    try:
        before = _hashes(source)
        assert {"chroma.sqlite3", "chroma.sqlite3-wal", "chroma.sqlite3-shm"} <= before.keys()
        dest = tmp_path / "dest"
        manifest = salvage.salvage_sqlite(str(source), str(dest), progress=lambda _: None)
        assert _hashes(source) == before
        for name in ("chroma.sqlite3", "chroma.sqlite3-wal", "chroma.sqlite3-shm"):
            assert manifest["source_files"][name]["sha256"] == before[name]
        with closing(sqlite3.connect(dest / "chroma.sqlite3")) as conn:
            assert conn.execute(
                "SELECT string_value FROM embedding_metadata WHERE id=3 AND key='chroma:document'"
            ).fetchone() == ("exact uncheckpointed WAL text",)
        assert manifest["census"]["document_rows"] == 3
    finally:
        _release_holder(holder)


def test_missing_chroma_column_and_integrity_fault_are_not_success(tmp_path):
    source = _palace(tmp_path / "source")
    db = source / "chroma.sqlite3"
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("ALTER TABLE embedding_metadata RENAME COLUMN string_value TO missing_value")
    with pytest.raises(salvage.SQLiteSalvageError, match="lacks columns.*string_value"):
        salvage._census(db, deadline=time.monotonic() + 10)
    _damage_header(source)
    with pytest.raises(sqlite3.DatabaseError):
        salvage._census(db, deadline=time.monotonic() + 10)


@pytest.mark.parametrize("palace_first", [False, True])
def test_cli_parser_preserves_explicit_salvage_destination_in_both_positions(
    tmp_path, monkeypatch, palace_first
):
    import mempalace.cli as cli

    source = _palace(tmp_path / "source")
    destination = str(tmp_path / "dest")
    argv = ["repair", "--mode", "salvage-sqlite", "--source", str(source), "--dry-run"]
    if palace_first:
        argv = ["--palace", destination, *argv]
    else:
        argv += ["--palace", destination]
    captured = []
    monkeypatch.setattr(cli, "_cmd_sqlite_salvage", lambda args: captured.append(args))
    import sys

    monkeypatch.setattr(sys, "argv", ["mempalace", *argv])
    monkeypatch.setenv("MEMPALACE_PALACE_PATH", os.environ.get("MEMPALACE_PALACE_PATH", ""))
    monkeypatch.setenv("PYTHONPATH", os.environ.get("PYTHONPATH", ""))
    cli.main()
    assert len(captured) == 1
    assert captured[0].palace == destination
    assert captured[0].source == str(source)
    assert not (tmp_path / "dest").exists()


def test_destination_created_during_recovery_is_not_overwritten(tmp_path, monkeypatch):
    _require_recover()
    source = _palace(tmp_path / "source")
    dest = tmp_path / "dest"
    real_census = salvage._census

    def occupied(db, **kwargs):
        result = real_census(db, **kwargs)
        dest.mkdir()
        (dest / "owned-by-another-operation").write_bytes(b"preserve this")
        return result

    monkeypatch.setattr(salvage, "_census", occupied)
    before = _hashes(source)
    with pytest.raises(salvage.SQLiteSalvageError, match="Forensic files retained"):
        salvage.salvage_sqlite(str(source), str(dest))
    assert _hashes(dest) == {
        "owned-by-another-operation": hashlib.sha256(b"preserve this").hexdigest()
    }
    assert _hashes(source) == before
    assert len(list(tmp_path.glob(".dest.salvage-*"))) == 1


def test_real_sqliterc_is_not_loaded(tmp_path, monkeypatch):
    _require_recover()
    source = _palace(tmp_path / "source")
    init_home = tmp_path / "user-home"
    init_home.mkdir()
    marker = tmp_path / "unexpected-sqlite-output"
    (init_home / ".sqliterc").write_text(f'.output "{marker}"\n', encoding="utf-8")
    monkeypatch.setenv("HOME", str(init_home))
    monkeypatch.setenv("USERPROFILE", str(init_home))
    manifest = salvage.salvage_sqlite(str(source), str(tmp_path / "dest"), progress=lambda _: None)
    assert manifest["census"]["document_rows"] == 2
    assert not marker.exists()


def test_real_cli_recovers_damaged_header_with_misspelled_embedding_model(tmp_path):
    import sys

    _require_recover()
    source = _palace(tmp_path / "source")
    _damage_header(source)
    before = _hashes(source)
    dest = tmp_path / "dest"
    env = os.environ.copy()
    env["MEMPALACE_EMBEDDING_MODEL"] = "typo-not-needed-for-page-salvage"
    env["HF_HUB_OFFLINE"] = "1"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mempalace",
            "repair",
            "--mode",
            "salvage-sqlite",
            "--source",
            str(source),
            "--palace",
            str(dest),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Recovered 2 stored document row(s)" in result.stdout
    assert "completeness is not proven" in result.stdout
    assert "--mode from-sqlite" in result.stdout
    assert _hashes(source) == before


def test_snapshot_identity_check_tolerates_windows_fstat_fields(monkeypatch):
    # Windows lstat and fstat report different st_dev/st_ino/st_ctime for the
    # same untouched file; only size and mtime may be compared across them.
    path_sig = (1, 2, 100, 5_000, 7)
    fd_sig = (0, 9, 100, 5_000, 8)
    monkeypatch.setattr(salvage.os, "name", "nt")
    assert salvage._same_file_content(path_sig, fd_sig)
    assert not salvage._same_file_content(path_sig, (0, 9, 101, 5_000, 8))
    assert not salvage._same_file_content(path_sig, (0, 9, 100, 6_000, 8))
    monkeypatch.setattr(salvage.os, "name", "posix")
    assert not salvage._same_file_content(path_sig, fd_sig)
    assert salvage._same_file_content(path_sig, path_sig)


def _chromadb_palace(path, texts):
    """A palace written by chromadb itself, with its FTS5 virtual table."""
    import chromadb

    client = chromadb.PersistentClient(path=str(path))
    col = client.get_or_create_collection("mempalace_drawers")
    col.add(
        ids=[f"drawer-{i}" for i in range(1, len(texts) + 1)],
        documents=list(texts),
        embeddings=[[float(i), 1.0, 0.0] for i in range(1, len(texts) + 1)],
        metadatas=[{"wing": "original"} for _ in texts],
    )
    if hasattr(client, "close"):
        client.close()
    return path


def test_real_chromadb_palace_with_fts5_recovers_under_safe_apply(tmp_path):
    # .recover restores Chroma's FTS5 table through a sqlite_schema INSERT,
    # which `sqlite3 -safe` refused, so every real palace failed to apply.
    _require_recover()
    texts = ("the lighthouse keeper's log", "second drawer, tide tables", "third: fog horn")
    source = _chromadb_palace(tmp_path / "source", texts)
    with closing(sqlite3.connect(source / "chroma.sqlite3")) as conn:
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='embedding_fulltext_search'"
        ).fetchone()
    _damage_header(source)
    before = hashlib.sha256((source / "chroma.sqlite3").read_bytes()).hexdigest()
    dest = tmp_path / "recovered"
    manifest = salvage.salvage_sqlite(str(source), str(dest), progress=lambda _: None)
    assert hashlib.sha256((source / "chroma.sqlite3").read_bytes()).hexdigest() == before
    assert manifest["census"]["document_rows"] == len(texts)
    with closing(sqlite3.connect(dest / "chroma.sqlite3")) as conn:
        docs = sorted(
            row[0]
            for row in conn.execute(
                "SELECT string_value FROM embedding_metadata WHERE key='chroma:document'"
            )
        )
        assert docs == sorted(texts)
        assert conn.execute(
            "SELECT count(*) FROM embedding_fulltext_search WHERE string_value MATCH 'lighthouse'"
        ).fetchone() == (1,)


@pytest.mark.parametrize(
    "statement",
    [
        "ATTACH DATABASE 'outside.db' AS o;",
        "PRAGMA journal_mode=WAL;",
        "SELECT load_extension('x');",
    ],
)
def test_recovery_apply_refuses_statements_outside_the_stage(tmp_path, statement):
    sql = tmp_path / "recovery.sql"
    sql.write_text("BEGIN;\nCREATE TABLE t(x);\n" + statement + "\nCOMMIT;\n")
    target = tmp_path / "stage.sqlite3"
    target.touch()
    with pytest.raises(salvage.SQLiteSalvageError, match="Applying the recovered SQL failed"):
        salvage._apply_recovery_sql(
            sql, target, deadline=time.monotonic() + 30, error_file=tmp_path / "err.txt"
        )
    assert not (tmp_path / "outside.db").exists()
