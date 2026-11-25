"""Scoped post-mine integrity check, maintenance full check, child isolation (#2684)."""

from __future__ import annotations

import pytest

from mempalace.backends.chroma import ChromaBackend, chroma_written_ids_since
from mempalace.palace import palace_write_serial


def _collection(backend, palace):
    return backend.create_collection(str(palace), "mempalace_drawers")


# ── 1. Write journal ────────────────────────────────────────────────


def test_journal_returns_ids_written_since_a_serial(tmp_path):
    palace = tmp_path / "palace"
    palace.mkdir()
    backend = ChromaBackend()
    try:
        col = _collection(backend, palace)
        col.upsert(ids=["old"], documents=["before"], metadatas=[{"wing": "w"}])
        since = palace_write_serial()
        col.add(ids=["a"], documents=["one"], metadatas=[{"wing": "w"}])
        col.upsert(ids=["b", "c"], documents=["two", "three"], metadatas=[{"wing": "w"}] * 2)
        col.update(ids=["a"], metadatas=[{"wing": "w", "room": "r"}])
        col.delete(ids=["c"])
        assert chroma_written_ids_since(since) == {"a", "b", "c"}
        assert chroma_written_ids_since(palace_write_serial()) == set()
    finally:
        backend.close()


def test_journal_reports_incomplete_once_entries_are_evicted(tmp_path, monkeypatch):
    from mempalace.backends import chroma as chroma_mod

    monkeypatch.setattr(chroma_mod, "_WRITTEN_IDS_CAP", 3)
    palace = tmp_path / "palace"
    palace.mkdir()
    backend = ChromaBackend()
    try:
        col = _collection(backend, palace)
        since = palace_write_serial()
        col.upsert(ids=["a", "b"], documents=["1", "2"], metadatas=[{"wing": "w"}] * 2)
        recent = palace_write_serial()
        col.upsert(ids=["c", "d"], documents=["3", "4"], metadatas=[{"wing": "w"}] * 2)
        assert chroma_written_ids_since(since) is None, "evicted ids must not pass as complete"
        assert chroma_written_ids_since(recent) == {"c", "d"}
    finally:
        backend.close()


def test_journal_marks_a_write_without_declared_ids_incomplete():
    from mempalace.backends import chroma as chroma_mod

    since = palace_write_serial()
    chroma_mod._count_write(None)
    assert chroma_written_ids_since(since) is None


# ── 2. Probe functions (run in the child; tested here in-process) ───


def _palace_with(tmp_path, records):
    """A closed Chroma palace holding ``records`` = [(id, document), ...]."""
    palace = tmp_path / "palace"
    palace.mkdir()
    backend = ChromaBackend()
    try:
        col = _collection(backend, palace)
        col.upsert(
            ids=[r[0] for r in records],
            documents=[r[1] for r in records],
            metadatas=[{"wing": "w", "room": "r"} for _ in records],
        )
    finally:
        backend.close()
    return palace


def _raw_sql(palace, sql, params=()):
    """Write straight through sqlite, bypassing the stored-text chokepoint."""
    import sqlite3
    from contextlib import closing

    with closing(sqlite3.connect(str(palace / "chroma.sqlite3"))) as conn:
        conn.execute(sql, params)
        conn.commit()


def _rowid(palace, drawer_id):
    import sqlite3
    from contextlib import closing

    with closing(sqlite3.connect(str(palace / "chroma.sqlite3"))) as conn:
        return conn.execute(
            "SELECT id FROM embeddings WHERE embedding_id = ?", (drawer_id,)
        ).fetchone()[0]


def test_scoped_check_is_clean_on_a_clean_palace(tmp_path):
    from mempalace.integrity_check import scoped_check_errors

    palace = _palace_with(tmp_path, [("a", "hello world"), ("b", "second")])
    assert scoped_check_errors(str(palace / "chroma.sqlite3"), {"a", "b", "gone"}) == []


def test_scoped_check_finds_a_raw_nul_in_a_written_document(tmp_path):
    """The #1537 precursor: a NUL that got past the stored-text chokepoint."""
    from mempalace.integrity_check import scoped_check_errors

    palace = _palace_with(tmp_path, [("a", "hello world"), ("b", "second")])
    _raw_sql(
        palace,
        "UPDATE embedding_metadata SET string_value = ? WHERE id = ? AND key = 'chroma:document'",
        ("hel\x00lo", _rowid(palace, "a")),
    )
    db = str(palace / "chroma.sqlite3")

    errors = scoped_check_errors(db, {"a"})
    assert errors and all("NUL" in e for e in errors)
    assert any("'a'" in e and "embedding_metadata" in e for e in errors)
    # Scoped means scoped: a row this mine did not write is not scanned.
    assert scoped_check_errors(db, {"b"}) == []


def test_scoped_check_finds_nul_and_bad_utf8_in_the_fts_content_and_metadata(tmp_path):
    from mempalace.integrity_check import scoped_check_errors

    palace = _palace_with(tmp_path, [("a", "hello world")])
    rowid = _rowid(palace, "a")
    _raw_sql(
        palace,
        "UPDATE embedding_fulltext_search_content SET c0 = ? WHERE id = ?",
        ("he\x00llo world", rowid),
    )
    _raw_sql(
        palace,
        "UPDATE embedding_metadata SET string_value = CAST(? AS TEXT) WHERE id = ? AND key = 'room'",
        (b"r\xed\xa0\x80", rowid),  # an encoded lone surrogate
    )
    errors = scoped_check_errors(str(palace / "chroma.sqlite3"), {"a"})
    joined = "\n".join(errors)
    assert "embedding_fulltext_search_content" in joined and "NUL" in joined
    assert "invalid UTF-8" in joined and "room" in joined


def test_full_check_reports_page_corruption(tmp_path):
    from mempalace.integrity_check import full_check_errors

    palace = _palace_with(tmp_path, [(f"d{i}", "lorem ipsum " * 50) for i in range(40)])
    db = palace / "chroma.sqlite3"
    assert full_check_errors(str(db)) == []
    with open(db, "r+b") as handle:
        handle.seek(8192)
        handle.write(b"\xde\xad\xbe\xef" * 4096)
    assert full_check_errors(str(db))


# ── 3. When the full check is due, and its cache ─────────────────────

HOUR = 3600.0


def _reason(palace, *, mode="scoped", interval=24.0, now, ids_complete=True):
    from mempalace.integrity_check import full_check_reason

    return full_check_reason(
        str(palace), mode=mode, interval_hours=interval, now=now, ids_complete=ids_complete
    )


def _touch_db(palace):
    """Change the database the way a write does (size and mtime)."""
    with open(palace / "chroma.sqlite3", "ab") as handle:
        handle.write(b"\0" * 4096)


def test_first_check_is_full_then_cached_until_the_palace_changes(tmp_path):
    from mempalace.integrity_check import record_clean_full_check

    palace = _palace_with(tmp_path, [("a", "hello")])
    assert "no full check" in _reason(palace, now=1000.0)

    record_clean_full_check(str(palace), now=1000.0)
    assert _reason(palace, now=1000.0 + 25 * HOUR) is None, "unchanged palace: cached"

    _touch_db(palace)
    assert _reason(palace, now=1000.0 + 1 * HOUR) is None, "changed but within the interval"
    assert "older than 24" in _reason(palace, now=1000.0 + 24 * HOUR)


def test_interval_and_mode_are_configurable(tmp_path):
    from mempalace.integrity_check import record_clean_full_check

    palace = _palace_with(tmp_path, [("a", "hello")])
    record_clean_full_check(str(palace), now=0.0)
    _touch_db(palace)
    assert _reason(palace, interval=2.0, now=1.5 * HOUR) is None
    assert _reason(palace, interval=2.0, now=2.5 * HOUR) is not None
    assert _reason(palace, interval=0, now=1000 * HOUR) is None, "0 disables the interval"
    assert "full" in _reason(palace, mode="full", now=1.0)
    assert "journal" in _reason(palace, now=1.0, ids_complete=False)


def test_a_writer_that_died_mid_write_forces_the_next_full_check(tmp_path):
    from mempalace import integrity_check as ic

    palace = _palace_with(tmp_path, [("a", "hello")])
    ic.record_clean_full_check(str(palace), now=0.0)
    _touch_db(palace)
    assert _reason(palace, now=1.0) is None

    # A write in flight in this process is not a crash.
    ic.note_write_started(str(palace))
    assert _reason(palace, now=1.0) is None
    ic.note_write_finished(str(palace))
    assert _reason(palace, now=1.0) is None

    # A marker nobody in this process owns was left by a writer that died.
    marker = palace / ".mempalace" / "write-in-flight"
    marker.write_text("12345")
    assert "mid-write" in _reason(palace, now=1.0)
    # The next write must not erase the evidence before the mine's check reads it.
    ic.note_write_started(str(palace))
    ic.note_write_finished(str(palace))
    assert "mid-write" in _reason(palace, now=1.0)

    ic.record_clean_full_check(str(palace), now=2.0)
    assert _reason(palace, now=3.0) is None


def test_chroma_writes_hold_the_in_flight_marker_only_while_writing(tmp_path, monkeypatch):
    palace = _palace_with(tmp_path, [("a", "hello")])
    marker = palace / ".mempalace" / "write-in-flight"
    assert not marker.exists()
    assert not (palace / ".mempalace" / "integrity-unclean").exists()

    seen = []
    backend = ChromaBackend()
    try:
        col = backend.get_collection(str(palace), "mempalace_drawers")
        real_upsert = col._collection.upsert

        def _spy(**kwargs):
            seen.append(marker.exists())
            return real_upsert(**kwargs)

        monkeypatch.setattr(col._collection, "upsert", _spy)
        col.upsert(ids=["b"], documents=["two"], metadatas=[{"wing": "w"}])
    finally:
        backend.close()
    assert seen == [True]
    assert not marker.exists()


# ── 4. The post-mine validator, with a real child process ───────────


def _mine_like_write(palace, ids, documents):
    """Write as a mine does and return the serial read before it."""
    backend = ChromaBackend()
    try:
        col = backend.get_or_create_collection(str(palace), "mempalace_drawers")
        before = palace_write_serial()
        col.upsert(ids=ids, documents=documents, metadatas=[{"wing": "w"} for _ in ids])
    finally:
        backend.close()
    return before


def _record_probes(monkeypatch):
    from mempalace import integrity_check as ic

    calls = []
    real = ic.run_probe

    def _spy(db_path, *, ids, full, scan_all=False):
        calls.append({"ids": None if ids is None else set(ids), "full": full})
        return real(db_path, ids=ids, full=full, scan_all=scan_all)

    monkeypatch.setattr(ic, "run_probe", _spy)
    return calls


def test_parent_never_opens_the_palace_with_python_sqlite(tmp_path, monkeypatch):
    """The second SQLite copy stays out of the writer's process (#2684)."""
    import sqlite3

    from mempalace.palace import _validate_palace_fts5_after_mine

    palace = _palace_with(tmp_path, [("a", "hello")])
    before = _mine_like_write(palace, ["b"], ["fresh words"])
    # Backend detection's lock-safe immutable=1 magic probe predates this check
    # and is cached per inode; a mine has run it long before its end.
    from mempalace.palace import resolve_backend_name

    assert resolve_backend_name(str(palace)) == "chroma"
    opened = []
    real_connect = sqlite3.connect

    def _spy(database, *args, **kwargs):
        opened.append(str(database))
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", _spy)
    probes = _record_probes(monkeypatch)

    _validate_palace_fts5_after_mine(str(palace), writes_since=before)

    assert [p for p in opened if "chroma.sqlite3" in p] == []
    assert probes and probes[0]["ids"] == {"b"}


def test_validator_scans_only_the_mines_rows_once_a_full_check_is_on_record(tmp_path, monkeypatch):
    from mempalace.integrity_check import record_clean_full_check
    from mempalace.palace import _validate_palace_fts5_after_mine

    palace = _palace_with(tmp_path, [("a", "hello")])
    record_clean_full_check(str(palace))
    before = _mine_like_write(palace, ["b", "c"], ["two", "three"])
    probes = _record_probes(monkeypatch)

    _validate_palace_fts5_after_mine(str(palace), writes_since=before)

    assert probes == [{"ids": {"b", "c"}, "full": False}]


def test_validator_catches_a_nul_written_past_the_chokepoint(tmp_path):
    """#1537 class: a raw NUL in a row this mine wrote fails the mine."""
    from mempalace.integrity_check import record_clean_full_check
    from mempalace.palace import MineValidationError, _validate_palace_fts5_after_mine

    palace = _palace_with(tmp_path, [("a", "hello")])
    record_clean_full_check(str(palace))
    before = _mine_like_write(palace, ["b"], ["fresh words"])
    _raw_sql(
        palace,
        "UPDATE embedding_metadata SET string_value = ? WHERE id = ? AND key = 'chroma:document'",
        ("fresh\x00words", _rowid(palace, "b")),
    )

    record = palace / ".mempalace" / "integrity.json"
    recorded = record.read_text()
    with pytest.raises(MineValidationError) as exc_info:
        _validate_palace_fts5_after_mine(str(palace), writes_since=before)
    assert any("raw NUL" in e and "'b'" in e for e in exc_info.value.errors)
    # The escalated full check passes (quick_check cannot see a stored NUL);
    # it must not certify the palace that just failed.
    assert record.read_text() == recorded


def test_fts_index_damage_is_left_to_the_maintenance_check(tmp_path):
    """What the scoped check does not catch, and who does.

    Damage to the FTS5 inverted index passes the scoped check by design (it
    would cost a full check to see). The unclean-exit trigger, the usual
    source of such damage, sends the mine to the full check, which finds it
    and auto-heals it.
    """
    import sqlite3
    from contextlib import closing

    from test_miner_fts5_validation import _corrupt_fts5_segment

    from mempalace.integrity_check import full_check_errors, record_clean_full_check
    from mempalace.palace import _validate_palace_fts5_after_mine

    palace = _palace_with(tmp_path, [(f"d{i}", f"memorable phrase {i} " * 20) for i in range(30)])
    record_clean_full_check(str(palace))
    before = _mine_like_write(palace, ["new"], ["another memorable phrase"])
    db = palace / "chroma.sqlite3"
    _corrupt_fts5_segment(db)
    assert full_check_errors(str(db)), "fixture must produce FTS5 damage the full check sees"

    # The last full check (before the damage) is fresh, so this mine scopes.
    _validate_palace_fts5_after_mine(str(palace), writes_since=before)  # scoped: passes
    assert full_check_errors(str(db)), "scoped check must not have touched the index"

    (palace / ".mempalace" / "write-in-flight").write_text("99999")  # a writer died
    _validate_palace_fts5_after_mine(str(palace), writes_since=before)  # full: heals
    with closing(sqlite3.connect(str(db))) as conn:
        assert conn.execute("PRAGMA quick_check").fetchall() == [("ok",)]
    assert not (palace / ".mempalace" / "write-in-flight").exists()


def test_config_full_mode_restores_the_full_check_on_every_writing_mine(tmp_path, monkeypatch):
    from mempalace.integrity_check import record_clean_full_check
    from mempalace.palace import _validate_palace_fts5_after_mine

    monkeypatch.setenv("MEMPALACE_POST_MINE_INTEGRITY_CHECK", "full")
    palace = _palace_with(tmp_path, [("a", "hello")])
    record_clean_full_check(str(palace))
    before = _mine_like_write(palace, ["b"], ["two"])
    probes = _record_probes(monkeypatch)

    _validate_palace_fts5_after_mine(str(palace), writes_since=before)

    assert probes == [{"ids": {"b"}, "full": True}]


def test_interval_trigger_reads_the_configured_hours(tmp_path, monkeypatch):
    from mempalace.integrity_check import record_clean_full_check
    from mempalace.palace import _validate_palace_fts5_after_mine

    palace = _palace_with(tmp_path, [("a", "hello")])
    record_clean_full_check(str(palace), now=0.0)  # a check long ago
    before = _mine_like_write(palace, ["b"], ["two"])
    probes = _record_probes(monkeypatch)

    monkeypatch.setenv("MEMPALACE_INTEGRITY_FULL_CHECK_INTERVAL_HOURS", "0")
    _validate_palace_fts5_after_mine(str(palace), writes_since=before)
    monkeypatch.delenv("MEMPALACE_INTEGRITY_FULL_CHECK_INTERVAL_HOURS")
    _validate_palace_fts5_after_mine(str(palace), writes_since=before)
    _validate_palace_fts5_after_mine(str(palace), writes_since=before)

    assert [p["full"] for p in probes] == [False, True, False], "full once, then fresh again"


def test_a_child_that_cannot_answer_fails_the_check(tmp_path, monkeypatch):
    from mempalace import integrity_check as ic

    palace = _palace_with(tmp_path, [("a", "hello")])
    monkeypatch.setattr(ic, "_CHILD_CODE", "import sys; sys.exit(3)")
    result = ic.run_probe(str(palace / "chroma.sqlite3"), ids={"a"}, full=True)
    assert result.scoped and "exit status 3" in result.scoped[0]
    assert result.scoped[0].startswith(ic.PROBE_FAILED_PREFIX)
    assert result.full == result.scoped


def test_config_values_fall_back_on_garbage(monkeypatch):
    from mempalace.config import MempalaceConfig

    monkeypatch.setenv("MEMPALACE_POST_MINE_INTEGRITY_CHECK", "sometimes")
    monkeypatch.setenv("MEMPALACE_INTEGRITY_FULL_CHECK_INTERVAL_HOURS", "-3")
    cfg = MempalaceConfig()
    assert cfg.post_mine_integrity_check == "scoped"
    assert cfg.integrity_full_check_interval_hours == 24.0
    monkeypatch.setenv("MEMPALACE_INTEGRITY_FULL_CHECK_INTERVAL_HOURS", "1.5")
    assert MempalaceConfig().integrity_full_check_interval_hours == 1.5


# ── 5. On demand: mempalace integrity-check ─────────────────────────


def _integrity_check_cli(palace, *, force=False):
    import argparse

    from mempalace.cli import cmd_integrity_check

    cmd_integrity_check(argparse.Namespace(palace=str(palace), force=force))


def test_integrity_check_command_runs_then_reuses_a_clean_result(tmp_path, capsys, monkeypatch):
    palace = _palace_with(tmp_path, [("a", "hello")])
    probes = _record_probes(monkeypatch)

    _integrity_check_cli(palace)
    assert "clean" in capsys.readouterr().out
    _integrity_check_cli(palace)
    assert "unchanged since" in capsys.readouterr().out
    _integrity_check_cli(palace, force=True)
    assert [p["full"] for p in probes] == [True, True], "the cached run probes nothing"

    _mine_like_write(palace, ["b"], ["a change since the last check"])
    _integrity_check_cli(palace)
    assert "found no issues" in capsys.readouterr().out
    assert len(probes) == 3, "a changed palace must be checked again"


def test_integrity_check_command_leaves_another_writers_marker_alone(tmp_path, capsys):
    """It takes no writer lock, so a marker may belong to a live mine elsewhere."""
    palace = _palace_with(tmp_path, [("a", "hello")])
    marker = palace / ".mempalace" / "write-in-flight"
    marker.parent.mkdir(exist_ok=True)
    marker.write_text("4242")

    _integrity_check_cli(palace)

    assert marker.exists()


def test_integrity_check_command_exits_nonzero_on_corruption(tmp_path, capsys):
    palace = _palace_with(tmp_path, [(f"d{i}", "lorem ipsum " * 50) for i in range(40)])
    with open(palace / "chroma.sqlite3", "r+b") as handle:
        handle.seek(8192)
        handle.write(b"\xde\xad\xbe\xef" * 4096)

    with pytest.raises(SystemExit) as exc_info:
        _integrity_check_cli(palace)
    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "mempalace repair" in out
    assert not (palace / ".mempalace" / "integrity.json").exists()


# ── 6. A check that could not run is not reported as corruption ─────


def test_a_child_that_cannot_run_fails_the_mine_without_the_corruption_banner(
    tmp_path, monkeypatch, capsys
):
    from types import SimpleNamespace

    from mempalace import cli, integrity_check as ic, miner, repair as repair_mod
    from mempalace.palace import MineValidationError, _validate_palace_fts5_after_mine

    palace = _palace_with(tmp_path, [("a", "hello")])
    before = _mine_like_write(palace, ["b"], ["two"])
    monkeypatch.setattr(ic, "_CHILD_CODE", "import sys; sys.exit(3)")
    healed = []
    monkeypatch.setattr(
        repair_mod, "maybe_autoheal_fts5_index", lambda p, e, **_k: healed.append(e) or e
    )

    with pytest.raises(MineValidationError) as exc_info:
        _validate_palace_fts5_after_mine(str(palace), writes_since=before)
    exc = exc_info.value
    assert exc.unverified and "could not run" in str(exc)
    assert healed == [], "no verdict, nothing to heal"
    assert not (palace / ".mempalace" / "integrity.json").exists()

    def _raise(*_a, **_k):
        raise exc

    monkeypatch.setattr(miner, "mine", _raise)
    src = tmp_path / "src"
    src.mkdir()
    args = SimpleNamespace(
        palace=str(palace),
        dir=str(src),
        mode="project",
        wing=None,
        agent="mempalace",
        limit=0,
        dry_run=False,
        no_gitignore=False,
        include_ignored=None,
        extract="exchange",
        redetect_origin=False,
    )
    with pytest.raises(SystemExit) as exit_info:
        cli.cmd_mine(args)
    assert exit_info.value.code == 1
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "could not run" in combined
    assert "corruption" not in combined.lower()


def test_full_mode_checks_even_when_the_cache_matches(tmp_path, monkeypatch):
    """A same-size in-place update under a coarse-mtime filesystem can keep the
    fingerprint; ``full`` must not trust it."""
    from mempalace.integrity_check import record_clean_full_check
    from mempalace.palace import _validate_palace_fts5_after_mine

    palace = _palace_with(tmp_path, [("a", "hello")])
    before = _mine_like_write(palace, ["b"], ["two"])
    record_clean_full_check(str(palace))  # cache matches the database exactly
    monkeypatch.setenv("MEMPALACE_POST_MINE_INTEGRITY_CHECK", "full")
    probes = _record_probes(monkeypatch)

    _validate_palace_fts5_after_mine(str(palace), writes_since=before)

    assert probes == [{"ids": {"b"}, "full": True}]


@pytest.mark.parametrize("baseline", ["no_writes", "unknown"])
def test_required_full_probe_survives_unchanged_fingerprint(tmp_path, monkeypatch, baseline):
    from mempalace.integrity_check import record_clean_full_check
    from mempalace.palace import _validate_palace_fts5_after_mine

    palace = _palace_with(tmp_path, [("a", "hello")])
    record_clean_full_check(str(palace))
    before = None
    if baseline == "no_writes":
        import subprocess
        import sys

        # Crash after acquiring the writer lock and marking the write, before
        # SQLite changes the fingerprint. A no-op successor still owes a check.
        script = (
            "import os, sys\n"
            "from mempalace.palace import mine_palace_lock\n"
            "from mempalace.integrity_check import note_write_started\n"
            "with mine_palace_lock(sys.argv[1]):\n"
            "    note_write_started(sys.argv[1])\n"
            "    os._exit(17)\n"
        )
        crash = subprocess.run([sys.executable, "-c", script, str(palace)], timeout=60)
        assert crash.returncode == 17
        before = palace_write_serial()

    probes = _record_probes(monkeypatch)
    _validate_palace_fts5_after_mine(str(palace), writes_since=before)
    assert [p["full"] for p in probes] == [True]


def test_write_invalidates_maintenance_cache_even_with_unchanged_stat(tmp_path):
    import os

    from mempalace.integrity_check import run_full_check

    palace = _palace_with(tmp_path, [("a", "one")])
    assert not run_full_check(str(palace)).cached
    db = palace / "chroma.sqlite3"
    before = db.stat()
    _mine_like_write(palace, ["a"], ["two"])
    assert db.stat().st_size == before.st_size
    os.utime(db, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert not run_full_check(str(palace)).cached


def test_scoped_scan_checks_keys_of_numeric_metadata(tmp_path):
    from mempalace.integrity_check import scoped_check_errors

    palace = _palace_with(tmp_path, [("a", "hello")])
    _raw_sql(
        palace,
        "INSERT INTO embedding_metadata(id, key, int_value) VALUES(?, ?, 1)",
        (_rowid(palace, "a"), "bad\x00key"),
    )
    errors = scoped_check_errors(str(palace / "chroma.sqlite3"), {"a"})
    assert any("raw NUL" in message and "key" in message for message in errors)


def test_missing_requested_child_verdict_fails_closed(tmp_path, monkeypatch):
    from mempalace import integrity_check as ic

    palace = _palace_with(tmp_path, [("a", "hello")])
    monkeypatch.setattr(ic, "_CHILD_CODE", """print('{"full": null, "scoped": null}')""")
    outcome = ic.run_full_check(str(palace))
    assert ic.all_unverified(outcome.errors)
    assert not (palace / ".mempalace" / "integrity.json").exists()


def test_maintenance_command_reports_unverified_child_without_recovery(
    tmp_path, monkeypatch, capsys
):
    from mempalace import integrity_check as ic

    palace = _palace_with(tmp_path, [("a", "hello")])
    monkeypatch.setattr(ic, "_CHILD_CODE", "raise SystemExit(3)")
    with pytest.raises(SystemExit) as exc_info:
        _integrity_check_cli(palace)
    assert exc_info.value.code == 1
    output = capsys.readouterr().out
    assert "could not run" in output
    assert "mempalace repair" not in output


def test_evicted_write_cannot_hide_a_stored_nul(tmp_path, monkeypatch):
    from mempalace.backends import chroma
    from mempalace.palace import MineValidationError, _validate_palace_fts5_after_mine

    palace = _palace_with(tmp_path, [("a", "before")])
    monkeypatch.setattr(chroma, "_WRITTEN_IDS_CAP", 1)
    since = _mine_like_write(palace, ["a", "b"], ["first", "second"])
    assert chroma_written_ids_since(since) is None
    _raw_sql(
        palace,
        "UPDATE embedding_metadata SET string_value=? WHERE id=? AND key='chroma:document'",
        ("bad\x00row", _rowid(palace, "a")),
    )
    with pytest.raises(MineValidationError) as error:
        _validate_palace_fts5_after_mine(str(palace), writes_since=since)
    assert any("raw NUL" in message for message in error.value.errors)


# ── Heal child: parent-death watchdog ───────────────────────────────


_WATCHDOG_PARENT = """
import site, sys
site.ENABLE_USER_SITE = True
site.getusersitepackages = lambda: sys.argv[1]
from mempalace.integrity_check import _run_heal_child
_run_heal_child(sys.argv[2], ["x"], print)
"""


def test_heal_child_exits_when_its_parent_is_killed(tmp_path):
    """The heal child must not outlive a parent killed mid-heal (it would
    keep the recovery transaction open with nobody holding the lease).

    A ``.pth`` in the user site the parent hands over parks the child after
    its watchdog started, standing in for a slow heal. Killing the parent
    (TerminateProcess on Windows, SIGKILL elsewhere) must end the child within
    seconds: the child shares the parent's stderr pipe, so EOF on it means
    both are gone. On Windows the watchdog used to read stdin, which also
    deadlocked the heal's own DLL imports.
    """
    import os
    import signal
    import subprocess
    import sys
    import threading
    import time

    import mempalace

    slow = tmp_path / "slow-site"
    slow.mkdir()
    pid_file = tmp_path / "child.pid"
    (slow / "slow_heal.pth").write_text(
        f"import os, time; open({str(pid_file)!r}, 'w').write(str(os.getpid())); time.sleep(120)\n"
    )
    env = dict(os.environ)
    package_parent = os.path.dirname(os.path.dirname(os.path.abspath(mempalace.__file__)))
    env["PYTHONPATH"] = os.pathsep.join(p for p in (package_parent, env.get("PYTHONPATH")) if p)
    parent = subprocess.Popen(
        [sys.executable, "-c", _WATCHDOG_PARENT, str(slow), str(tmp_path / "palace")],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env=env,
    )
    child_pid = None
    try:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline and parent.poll() is None:
            if pid_file.exists() and pid_file.read_text().strip():
                child_pid = int(pid_file.read_text())
                break
            time.sleep(0.1)
        assert child_pid, f"heal child never started (parent exit {parent.poll()})"

        parent.kill()
        parent.wait(timeout=30)
        drained = threading.Event()

        def drain():
            parent.stderr.read()
            drained.set()

        threading.Thread(target=drain, daemon=True).start()
        assert drained.wait(15), "heal child outlived its killed parent"
        child_pid = None
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait()
        if child_pid:
            kill = getattr(signal, "SIGKILL", signal.SIGTERM)
            try:
                os.kill(child_pid, kill)
            except OSError:
                pass
