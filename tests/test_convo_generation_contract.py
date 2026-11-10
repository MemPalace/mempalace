"""Executable reproductions for the conversation generation publication contract.

A new per-source history backup protects prior conversation text even if a
later positional upsert fails. These assertions exercise the real content
preservation gate, not merely stable IDs or drawer counts. Generation-wide
atomic visibility remains a separate, unsolved contract.

The tests must fail normally on content loss; future staging and commit-marker
filters require their own read-visibility regressions.
"""

import json
import os
import subprocess
import sys

import pytest

from mempalace.convo_miner import mine_convos
from mempalace.palace import MineFileErrors, get_collection


def _write_session(path, label, count=3):
    """Write exchanges long enough to create one drawer per conversation turn."""
    turns = []
    for index in range(count):
        turns.append(
            f"> {label} question {index} "
            + "asked " * 30
            + "\n"
            + f"{label} answer {index} "
            + "verbatim " * 45
        )
    path.write_text("\n\n".join(turns) + "\n", encoding="utf-8")


def _touch_after_write(path):
    stat = path.stat()
    os.utime(path, (stat.st_atime, stat.st_mtime + 10))


def _documents(palace_path, source):
    col = get_collection(palace_path)
    rows = col.get(
        where={"source_file": str(source)},
        include=["documents", "metadatas"],
    )
    return {
        text
        for text, metadata in zip(rows.get("documents") or [], rows.get("metadatas") or [])
        if (metadata or {}).get("extract_mode") == "exchange"
        and (metadata or {}).get("ingest_mode") == "convos"
    }


def _mine(source_dir, palace_path):
    mine_convos(str(source_dir), palace_path, wing="sessions", extract_mode="exchange")


def test_second_batch_failure_preserves_every_previous_document(tmp_path, monkeypatch):
    """Crash after one successful changed-chunk upsert must not destroy history."""
    source_dir = tmp_path / "sessions"
    source_dir.mkdir()
    session = source_dir / "session.txt"
    palace_path = str(tmp_path / "palace")
    _write_session(session, "original")
    _mine(source_dir, palace_path)
    before = _documents(palace_path, session)
    assert len(before) >= 3

    _write_session(session, "replacement")
    _touch_after_write(session)

    import mempalace.palace as palace

    monkeypatch.setattr(palace, "DRAWER_UPSERT_BATCH_SIZE", 1)
    original_upsert = type(get_collection(palace_path)).upsert
    writes = 0

    def interrupt_second_upsert(collection, *args, **kwargs):
        nonlocal writes
        # History backups are expected before the current write; inject the
        # failure *after* the first actual replacement has been persisted.
        if all("_hist_" in drawer_id for drawer_id in kwargs.get("ids", [])):
            return original_upsert(collection, *args, **kwargs)
        writes += 1
        if writes == 2:
            raise RuntimeError("injected second-batch write failure")
        return original_upsert(collection, *args, **kwargs)

    monkeypatch.setattr(type(get_collection(palace_path)), "upsert", interrupt_second_upsert)
    with pytest.raises(MineFileErrors):
        _mine(source_dir, palace_path)
    assert writes >= 2, "test did not reach the intended failure window"

    # IDs/counts alone are insufficient: compare the actual original text.
    after = _documents(palace_path, session)
    assert before.issubset(after), "old verbatim text was lost after batch failure"

    # A separate process is a fresh SQLite reader, not the in-process Chroma
    # cache. Verify the *entire* old snapshot was durably filed before the
    # interrupted positional overwrite. This does not establish atomic PUBLIC
    # visibility: raw SQLite can still contain unpublished B rows.
    probe = (
        "import json, sqlite3, sys\n"
        "with sqlite3.connect(sys.argv[1]) as db:\n"
        "    rows = db.execute(\n"
        '        "SELECT doc.string_value FROM embedding_metadata AS doc "\n'
        '        "JOIN embedding_metadata AS src ON src.id = doc.id "\n'
        "        \"AND src.key = 'source_file' \"\n"
        '        "JOIN embedding_metadata AS mode ON mode.id = doc.id "\n'
        "        \"AND mode.key = 'extract_mode' \"\n"
        "        \"WHERE doc.key = 'chroma:document' \"\n"
        "        \"AND src.string_value = ? AND mode.string_value = 'exchange'\",\n"
        "        (sys.argv[2],),\n"
        "    ).fetchall()\n"
        "print(json.dumps([doc for (doc,) in rows]))\n"
    )
    read_back = subprocess.run(
        [sys.executable, "-c", probe, os.path.join(palace_path, "chroma.sqlite3"), str(session)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert read_back.returncode == 0, read_back.stderr
    assert before.issubset(set(json.loads(read_back.stdout))), (
        "a fresh reader process cannot recover all pre-failure A contents"
    )


def test_successful_rewrite_is_additive_in_verbatim_conversation_history(tmp_path):
    """A successful transcript rewrite must preserve prior authored content."""
    source_dir = tmp_path / "sessions"
    source_dir.mkdir()
    session = source_dir / "session.txt"
    palace_path = str(tmp_path / "palace")
    _write_session(session, "original")
    _mine(source_dir, palace_path)
    previous = _documents(palace_path, session)
    assert len(previous) >= 3

    _write_session(session, "replacement")
    _touch_after_write(session)
    _mine(source_dir, palace_path)
    after = _documents(palace_path, session)
    assert any("replacement question" in text for text in after)
    assert previous.issubset(after), "successful re-mine destroyed prior history"


def test_history_rewrite_a_to_b_to_a_never_duplicates_archived_versions(tmp_path):
    """A reversion is not a new historical version each time it is revisited."""
    source_dir = tmp_path / "sessions"
    source_dir.mkdir()
    session = source_dir / "session.txt"
    palace_path = str(tmp_path / "palace")

    _write_session(session, "A", count=2)
    _mine(source_dir, palace_path)
    first = _documents(palace_path, session)
    assert len(first) >= 2

    _write_session(session, "B", count=2)
    _touch_after_write(session)
    _mine(source_dir, palace_path)
    second = _documents(palace_path, session)
    assert first.issubset(second)

    _write_session(session, "A", count=2)
    _touch_after_write(session)
    _mine(source_dir, palace_path)
    third = _documents(palace_path, session)
    assert first.issubset(third)
    assert second.issubset(third)

    def history_ids():
        rows = get_collection(palace_path).get(
            where={"source_file": str(session)}, include=["metadatas"]
        )
        return {
            drawer_id
            for drawer_id, meta in zip(rows["ids"], rows["metadatas"])
            if (meta or {}).get("historical_version") is True
        }

    stable = history_ids()
    assert len(stable) >= 4

    _write_session(session, "B", count=2)
    _touch_after_write(session)
    _mine(source_dir, palace_path)
    assert history_ids() == stable, "A/B history rows multiplied on reversion"
    assert first.union(second).issubset(_documents(palace_path, session))


def test_failed_current_write_retry_does_not_clone_history_twice(tmp_path, monkeypatch):
    """Backups written before a failed new pass remain idempotent on retry."""
    source_dir = tmp_path / "sessions"
    source_dir.mkdir()
    session = source_dir / "session.txt"
    palace_path = str(tmp_path / "palace")
    _write_session(session, "A", count=2)
    _mine(source_dir, palace_path)
    _write_session(session, "B", count=2)
    _touch_after_write(session)

    collection = get_collection(palace_path)
    old_upsert = type(collection).upsert
    failed = False

    def fail_current_upsert(self, *args, **kwargs):
        nonlocal failed
        if not failed and any("_hist_" not in i for i in kwargs.get("ids", [])):
            failed = True
            raise RuntimeError("injected before first current replacement")
        return old_upsert(self, *args, **kwargs)

    monkeypatch.setattr(type(collection), "upsert", fail_current_upsert)
    with pytest.raises(MineFileErrors):
        _mine(source_dir, palace_path)
    assert failed, "injection did not reach a current-version write"
    before = get_collection(palace_path).get(
        where={"source_file": str(session)}, include=["metadatas"]
    )
    hist_before = {
        row_id
        for row_id, meta in zip(before["ids"], before["metadatas"])
        if (meta or {}).get("historical_version") is True
    }
    assert hist_before

    monkeypatch.setattr(type(collection), "upsert", old_upsert)
    _mine(source_dir, palace_path)
    after = get_collection(palace_path).get(
        where={"source_file": str(session)}, include=["metadatas"]
    )
    hist_after = {
        row_id
        for row_id, meta in zip(after["ids"], after["metadatas"])
        if (meta or {}).get("historical_version") is True
    }
    assert hist_before == hist_after, "retry duplicated immutable backups"
    assert any("B question" in text for text in _documents(palace_path, session))


def test_uncommitted_current_rows_must_not_displace_previous_snapshot(tmp_path, monkeypatch):
    """Future generation gate: an interrupted mine must not expose mixed rows."""
    source_dir = tmp_path / "sessions"
    source_dir.mkdir()
    session = source_dir / "session.txt"
    palace_path = str(tmp_path / "palace")
    _write_session(session, "A", count=3)
    _mine(source_dir, palace_path)

    # Exercise the public MCP API before the injected failure as well: the
    # tool deliberately exposes the source basename instead of the full
    # local path (see _response_safe_meta). A full-path comparison silently
    # selects zero rows and never reaches the generation-visibility check.
    from mempalace import mcp_server
    from mempalace.config import MempalaceConfig

    monkeypatch.setattr(mcp_server, "_config", MempalaceConfig(palace_path=palace_path))
    monkeypatch.setattr(
        mcp_server, "_get_collection", lambda *args, **kwargs: get_collection(palace_path)
    )

    def public_session_previews():
        listing = mcp_server.tool_list_drawers(limit=100)
        assert "error" not in listing, listing
        return [
            row.get("content_preview") or ""
            for row in listing["drawers"]
            if (row.get("metadata") or {}).get("source_file") == session.name
        ]

    initial_public = public_session_previews()
    initial_snapshot = set(initial_public)
    assert len(initial_snapshot) >= 3, "public listing did not expose the initial snapshot"
    assert all("A question" in text for text in initial_snapshot)

    _write_session(session, "B", count=3)
    _touch_after_write(session)

    import mempalace.palace as palace

    monkeypatch.setattr(palace, "DRAWER_UPSERT_BATCH_SIZE", 1)
    collection = get_collection(palace_path)
    original_upsert = type(collection).upsert
    replacement_batches = 0

    def fail_second_current(self, *args, **kwargs):
        nonlocal replacement_batches
        if all("_hist_" in i for i in kwargs.get("ids", [])):
            return original_upsert(self, *args, **kwargs)
        replacement_batches += 1
        if replacement_batches == 2:
            raise RuntimeError("injected before second current replacement")
        return original_upsert(self, *args, **kwargs)

    monkeypatch.setattr(type(collection), "upsert", fail_second_current)
    with pytest.raises(MineFileErrors):
        _mine(source_dir, palace_path)
    assert replacement_batches == 2

    # Raw Chroma rows are allowed to contain unpublished staging records.
    # The user-visible contract is the MCP listing, not the storage internals.
    public = public_session_previews()
    assert initial_snapshot.issubset(set(public)), (
        "interrupted generation hid part of the previously published A snapshot"
    )
    if any("B question" in text for text in public):
        pytest.xfail(
            "MCP listing leaks an incomplete replacement after the first "
            "new batch; requires committed-generation filtering before limits"
        )
    assert all("A question" in text for text in public)
