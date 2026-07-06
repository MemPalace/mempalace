import os
import tempfile
import shutil
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import chromadb
import pytest

from mempalace.convo_miner import (
    _compute_convo_cursor,
    _fetch_stored_cursor,
    _flag_possible_duplicates,
    _incremental_reparse,
    _is_ai_tool_path,
    _register_file,
    _resolve_wing,
    chunk_exchanges,
    mine_convos,
)
from mempalace.palace import MineAlreadyRunning, file_already_mined, prefetch_mined_set


def test_convo_mining():
    tmpdir = tempfile.mkdtemp()
    with open(os.path.join(tmpdir, "chat.txt"), "w") as f:
        f.write(
            "> What is memory?\nMemory is persistence.\n\n> Why does it matter?\nIt enables continuity.\n\n> How do we build it?\nWith structured storage.\n"
        )

    palace_path = os.path.join(tmpdir, "palace")
    mine_convos(tmpdir, palace_path, wing="test_convos")

    client = chromadb.PersistentClient(path=palace_path)
    col = client.get_collection("mempalace_drawers")
    assert col.count() >= 2

    # Verify search works
    results = col.query(query_texts=["memory persistence"], n_results=1)
    assert len(results["documents"][0]) > 0

    shutil.rmtree(tmpdir, ignore_errors=True)


def test_mine_convos_does_not_reprocess_short_files(capsys):
    """Files below MIN_CHUNK_SIZE get a sentinel so they are skipped on re-run."""
    tmpdir = tempfile.mkdtemp()
    try:
        # A file too short to produce any chunks
        with open(os.path.join(tmpdir, "tiny.txt"), "w") as f:
            f.write("hi")

        palace_path = os.path.join(tmpdir, "palace")

        # First run -- file is processed (sentinel written)
        mine_convos(tmpdir, palace_path, wing="test")
        capsys.readouterr()  # drain output

        # Verify sentinel was written (resolve path -- macOS /var -> /private/var)
        resolved_file = str(Path(tmpdir).resolve() / "tiny.txt")
        client = chromadb.PersistentClient(path=palace_path)
        col = client.get_collection("mempalace_drawers")
        assert file_already_mined(col, resolved_file)

        # Second run -- file should be skipped
        mine_convos(tmpdir, palace_path, wing="test")
        out2 = capsys.readouterr().out
        assert "Files skipped (already filed): 1" in out2
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_mine_convos_does_not_reprocess_empty_chunk_files(capsys):
    """Files that normalize but produce 0 exchange chunks get a sentinel."""
    tmpdir = tempfile.mkdtemp()
    try:
        # Content long enough to pass MIN_CHUNK_SIZE but with no exchange markers
        # (no "> " lines), so chunk_exchanges returns []
        with open(os.path.join(tmpdir, "no_exchanges.txt"), "w") as f:
            f.write("This is a plain paragraph without any exchange markers. " * 5)

        palace_path = os.path.join(tmpdir, "palace")

        mine_convos(tmpdir, palace_path, wing="test")
        mine_convos(tmpdir, palace_path, wing="test")
        out2 = capsys.readouterr().out
        assert "Files skipped (already filed): 1" in out2
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_mine_convos_allows_general_after_exchange(capsys):
    """A transcript mined as exchange can later be mined as general memories."""
    tmpdir = tempfile.mkdtemp()
    try:
        convo_path = Path(tmpdir) / "chat.txt"
        convo_path.write_text(
            "> What did we decide?\n"
            "We decided to use SQLite because it keeps the local setup simple.\n\n"
            "> What broke?\n"
            "The search failed because the old index was stale, and the fix was rebuild.\n"
        )
        palace_path = os.path.join(tmpdir, "palace")

        mine_convos(tmpdir, palace_path, wing="test", extract_mode="exchange")
        capsys.readouterr()
        mine_convos(tmpdir, palace_path, wing="test", extract_mode="general")
        out = capsys.readouterr().out

        assert "Files skipped (already filed): 0" in out

        client = chromadb.PersistentClient(path=palace_path)
        col = client.get_collection("mempalace_drawers")
        resolved = str(Path(tmpdir).resolve() / "chat.txt")
        rows = col.get(where={"source_file": resolved}, include=["metadatas"])
        modes = {meta.get("extract_mode") for meta in rows["metadatas"]}
        assert {"exchange", "general"} <= modes
        assert any(drawer_id.startswith("drawer_test_decision_") for drawer_id in rows["ids"])
        del col, client
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_mine_convos_rebuilds_stale_drawers_after_schema_bump(capsys):
    """When stored drawers have an older normalize_version, the next mine
    silently purges them and refiles — no manual erase required.

    This is what makes the strip_noise upgrade apply to existing corpora:
    users just run `mempalace mine` again and old noise-filled drawers get
    replaced with clean ones."""
    from mempalace.palace import NORMALIZE_VERSION

    tmpdir = tempfile.mkdtemp()
    try:
        convo_path = Path(tmpdir) / "chat.txt"
        convo_path.write_text(
            "> What is memory?\nMemory is persistence.\n\n"
            "> Why does it matter?\nIt enables continuity.\n\n"
            "> How do we build it?\nWith structured storage.\n"
        )
        palace_path = os.path.join(tmpdir, "palace")

        # First mine — stamps drawers with NORMALIZE_VERSION
        mine_convos(tmpdir, palace_path, wing="test")
        capsys.readouterr()

        client = chromadb.PersistentClient(path=palace_path)
        col = client.get_collection("mempalace_drawers")
        resolved = str(Path(tmpdir).resolve() / "chat.txt")
        first_pass = col.get(where={"source_file": resolved})
        first_ids = set(first_pass["ids"])
        assert first_ids, "first mine should produce drawers"
        for meta in first_pass["metadatas"]:
            assert meta.get("normalize_version") == NORMALIZE_VERSION

        # Simulate pre-v2 drawers: rewrite metadata to an older version,
        # and replace content with "noise" so we can see it get cleaned up.
        stale_metas = []
        for meta in first_pass["metadatas"]:
            stale = dict(meta)
            stale["normalize_version"] = 1
            stale_metas.append(stale)
        col.update(
            ids=list(first_pass["ids"]),
            documents=["STALE NOISE"] * len(first_pass["ids"]),
            metadatas=stale_metas,
        )
        # Add an extra orphan drawer that should also be purged.
        col.add(
            ids=["orphan_drawer"],
            documents=["OLD ORPHAN"],
            metadatas=[
                {
                    "wing": "test",
                    "room": "default",
                    "source_file": resolved,
                    "chunk_index": 999,
                    "normalize_version": 1,
                }
            ],
        )
        del col, client

        # Second mine — version gate should trigger rebuild
        mine_convos(tmpdir, palace_path, wing="test")
        out = capsys.readouterr().out
        assert "Files skipped (already filed): 0" in out, (
            "stale drawers should force a rebuild, not a skip"
        )

        client = chromadb.PersistentClient(path=palace_path)
        col = client.get_collection("mempalace_drawers")
        rebuilt = col.get(where={"source_file": resolved})
        # Orphan is gone
        assert "orphan_drawer" not in rebuilt["ids"]
        # No stale content survived
        assert all("STALE NOISE" not in d for d in rebuilt["documents"])
        assert all("OLD ORPHAN" not in d for d in rebuilt["documents"])
        # All rebuilt drawers carry the current version
        for meta in rebuilt["metadatas"]:
            assert meta.get("normalize_version") == NORMALIZE_VERSION
        del col, client
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _hold_palace_lock_in_child(palace_path, ready_flag, release_flag):
    """Acquire mine_palace_lock in a child process and hold until signalled.

    Cannot use threads because mine_palace_lock is intentionally re-entrant
    within a single thread (so ChromaCollection write methods can compose
    with miner.mine() without self-deadlock). The convos concurrency
    guarantee is across processes / threads, so the test has to mirror that.
    """
    import os as _os
    import time as _time

    from mempalace.palace import mine_palace_lock as _mpl

    with _mpl(palace_path):
        open(ready_flag, "w").close()
        for _ in range(500):
            if _os.path.exists(release_flag):
                return
            _time.sleep(0.01)


def test_mine_convos_refuses_concurrent_run_against_same_palace(tmp_path, monkeypatch):
    """A second `mine_convos` against a palace currently being mined must
    raise MineAlreadyRunning, not stack up as a waiter that drives parallel
    ChromaDB writes. Mirrors the guarantee already given by `miner.mine`
    (see test_palace_locks.py) for the convos code path.
    """
    import multiprocessing
    import time

    monkeypatch.setenv("HOME", str(tmp_path))
    convo_dir = tmp_path / "convos"
    convo_dir.mkdir()
    (convo_dir / "chat.txt").write_text("> q1\nshort answer.\n\n> q2\nanother short answer.\n")
    palace_path = str(tmp_path / "palace")
    ready_flag = str(tmp_path / "ready")
    release_flag = str(tmp_path / "release")

    ctx = multiprocessing.get_context("spawn")
    holder = ctx.Process(
        target=_hold_palace_lock_in_child,
        args=(palace_path, ready_flag, release_flag),
    )
    holder.start()
    try:
        # Wait for the child to actually hold the lock before we attempt
        # to acquire from this process.
        for _ in range(500):
            if os.path.exists(ready_flag):
                break
            time.sleep(0.01)
        assert os.path.exists(ready_flag), "child never acquired palace lock"

        with pytest.raises(MineAlreadyRunning):
            mine_convos(str(convo_dir), palace_path, wing="test")
    finally:
        open(release_flag, "w").close()
        holder.join(timeout=10)
        if holder.is_alive():
            holder.terminate()
            holder.join(timeout=5)


def test_mine_convos_dry_run_bypasses_palace_lock(tmp_path, monkeypatch):
    """Dry-run never writes to the palace, so it must coexist with a live
    mine instead of being blocked by the per-palace flock.
    """
    import multiprocessing
    import time

    monkeypatch.setenv("HOME", str(tmp_path))
    convo_dir = tmp_path / "convos"
    convo_dir.mkdir()
    (convo_dir / "chat.txt").write_text("> q1\nshort answer.\n\n> q2\nanother short answer.\n")
    palace_path = str(tmp_path / "palace")
    ready_flag = str(tmp_path / "ready_dry")
    release_flag = str(tmp_path / "release_dry")

    ctx = multiprocessing.get_context("spawn")
    holder = ctx.Process(
        target=_hold_palace_lock_in_child,
        args=(palace_path, ready_flag, release_flag),
    )
    holder.start()
    try:
        for _ in range(500):
            if os.path.exists(ready_flag):
                break
            time.sleep(0.01)
        assert os.path.exists(ready_flag), "child never acquired palace lock"

        # Must not raise — dry-run skips the lock entirely.
        mine_convos(str(convo_dir), palace_path, wing="test", dry_run=True)
    finally:
        open(release_flag, "w").close()
        holder.join(timeout=10)
        if holder.is_alive():
            holder.terminate()
            holder.join(timeout=5)


# ── _is_ai_tool_path / _resolve_wing — wing_api auto-routing ───────────
#
# When a user runs `mempalace mine --mode convos` against a directory
# inside a known AI-tool storage path (Claude Code's
# ~/.claude/projects/, OpenAI Codex's ~/.codex/, Google Gemini CLI's
# ~/.gemini/), the wing auto-defaults to "wing_api" rather than the
# directory basename. This keeps API-sourced conversations grouped
# under a single dedicated wing for visibility and privacy isolation.
#
# Explicit user-passed --wing always wins. Unrelated directories use
# the existing basename fallback unchanged.


def test_is_ai_tool_path_claude_projects_subdir(tmp_path):
    """A subdirectory inside ~/.claude/projects/ is an AI tool path."""
    target = tmp_path / ".claude" / "projects" / "-Users-test-myapp"
    target.mkdir(parents=True)
    assert _is_ai_tool_path(target) is True


def test_is_ai_tool_path_claude_projects_root(tmp_path):
    """The ~/.claude/projects/ directory itself is an AI tool path."""
    target = tmp_path / ".claude" / "projects"
    target.mkdir(parents=True)
    assert _is_ai_tool_path(target) is True


def test_is_ai_tool_path_codex_root(tmp_path):
    target = tmp_path / ".codex"
    target.mkdir()
    assert _is_ai_tool_path(target) is True


def test_is_ai_tool_path_codex_sessions(tmp_path):
    """Codex stores sessions under ~/.codex/sessions/YYYY/MM/DD/."""
    target = tmp_path / ".codex" / "sessions" / "2026" / "04" / "26"
    target.mkdir(parents=True)
    assert _is_ai_tool_path(target) is True


def test_is_ai_tool_path_gemini_root(tmp_path):
    target = tmp_path / ".gemini"
    target.mkdir()
    assert _is_ai_tool_path(target) is True


def test_is_ai_tool_path_gemini_chats(tmp_path):
    """Gemini stores sessions under ~/.gemini/tmp/<hash>/chats/."""
    target = tmp_path / ".gemini" / "tmp" / "abc123" / "chats"
    target.mkdir(parents=True)
    assert _is_ai_tool_path(target) is True


def test_is_ai_tool_path_dotclaude_without_projects_not_matched(tmp_path):
    """`.claude/` alone (without `/projects`) is the settings dir, not a
    conversation source — it MUST NOT auto-route to wing_api."""
    target = tmp_path / ".claude"
    target.mkdir()
    assert _is_ai_tool_path(target) is False


def test_is_ai_tool_path_unrelated_directory(tmp_path):
    target = tmp_path / "Documents" / "myproject"
    target.mkdir(parents=True)
    assert _is_ai_tool_path(target) is False


def test_is_ai_tool_path_substring_no_false_positive(tmp_path):
    """A directory NAMED like `.gemini-backup` or `.codex-archive` is NOT
    a real AI tool path. We use exact-segment match, not substring."""
    a = tmp_path / ".gemini-backup"
    a.mkdir()
    b = tmp_path / ".codex-archive"
    b.mkdir()
    assert _is_ai_tool_path(a) is False
    assert _is_ai_tool_path(b) is False


def test_resolve_wing_explicit_wins_over_auto_detection(tmp_path):
    """User-passed --wing always wins, even on an AI tool path."""
    target = tmp_path / ".claude" / "projects" / "-Users-x"
    target.mkdir(parents=True)
    assert _resolve_wing(target, wing="my_custom_wing") == "my_custom_wing"


def test_resolve_wing_claude_projects_auto_routes_to_wing_api(tmp_path):
    target = tmp_path / ".claude" / "projects" / "-Users-test-myapp"
    target.mkdir(parents=True)
    assert _resolve_wing(target, wing=None) == "wing_api"


def test_resolve_wing_codex_auto_routes_to_wing_api(tmp_path):
    target = tmp_path / ".codex" / "sessions" / "2026"
    target.mkdir(parents=True)
    assert _resolve_wing(target, wing=None) == "wing_api"


def test_resolve_wing_gemini_auto_routes_to_wing_api(tmp_path):
    target = tmp_path / ".gemini" / "tmp" / "abc" / "chats"
    target.mkdir(parents=True)
    assert _resolve_wing(target, wing=None) == "wing_api"


def test_resolve_wing_unrelated_dir_uses_basename_fallback(tmp_path):
    """Existing behavior preserved: arbitrary directories use the
    sanitized basename as the wing."""
    target = tmp_path / "MyProject Folder"
    target.mkdir()
    # Spaces become underscores, hyphens become underscores, lowercased.
    assert _resolve_wing(target, wing=None) == "myproject_folder"


def test_resolve_wing_empty_string_treated_as_no_wing(tmp_path):
    """An empty string for wing should behave like None — fall through to
    auto-detection / basename. Mirrors the original `if not wing:` guard."""
    target = tmp_path / ".gemini" / "tmp"
    target.mkdir(parents=True)
    assert _resolve_wing(target, wing="") == "wing_api"


def test_mine_convos_limit_skips_already_mined(capsys):
    """--limit N counts only new work, not already-mined skips (#1535)."""
    tmpdir = tempfile.mkdtemp()
    try:
        convo_text = (
            "> What is topic {i}?\n"
            "Topic {i} is about something important and interesting enough "
            "to produce at least one exchange chunk for the test.\n\n"
            "> Tell me more about topic {i}.\n"
            "Sure, topic {i} has many facets worth exploring in detail.\n"
        )
        for i in range(4):
            with open(os.path.join(tmpdir, f"chat_{i}.txt"), "w") as f:
                f.write(convo_text.format(i=i))

        palace_path = os.path.join(tmpdir, "palace")

        mine_convos(tmpdir, palace_path, wing="test")
        capsys.readouterr()

        for i in range(4, 7):
            with open(os.path.join(tmpdir, f"chat_{i}.txt"), "w") as f:
                f.write(convo_text.format(i=i))

        mine_convos(tmpdir, palace_path, wing="test", limit=2)
        out = capsys.readouterr().out

        assert "Files processed: 2" in out
        assert "Drawers filed:" in out
        for line in out.split("\n"):
            if "Drawers filed:" in line:
                filed = int(line.split(":")[1].strip())
                assert filed > 0, f"limit=2 should mine new files, got {filed}"
                break
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ── mtime-aware re-mining ────────────────────────────────────────────
#
# Conversation transcripts are NOT immutable: a Claude Code session keeps
# appending to its own file while active, and /compact or /clear can
# rewrite one in place. These tests cover the fix -- convo mining used to
# treat "we've seen this source_file before" as sufficient to skip it
# forever (transcripts were assumed immutable), silently missing content
# appended after the first mine.


def test_mine_convos_reprocesses_when_file_grows(capsys):
    """A session file that grows after being mined must be picked up on
    the next mine, not skipped forever."""
    tmpdir = tempfile.mkdtemp()
    try:
        convo_path = Path(tmpdir) / "session.txt"
        convo_path.write_text(
            "> What is the plan?\nStart with the schema, then the API.\n\n"
            "> Any risks?\nMigration ordering is the main one.\n"
        )
        palace_path = os.path.join(tmpdir, "palace")

        mine_convos(tmpdir, palace_path, wing="test")
        capsys.readouterr()

        # Simulate the session being extended: real content added, mtime
        # bumped forward (avoids same-second mtime resolution flakiness).
        convo_path.write_text(
            "> What is the plan?\nStart with the schema, then the API.\n\n"
            "> Any risks?\nMigration ordering is the main one.\n\n"
            "> UNIQUE_GROWN_SESSION_MARKER, did we resolve it?\n"
            "Yes, resolved by locking the migration order explicitly.\n"
        )
        future = time.time() + 60
        os.utime(convo_path, (future, future))

        mine_convos(tmpdir, palace_path, wing="test")
        out = capsys.readouterr().out
        assert "Files skipped (already filed): 1" not in out

        client = chromadb.PersistentClient(path=palace_path)
        col = client.get_collection("mempalace_drawers")
        docs = col.get(include=["documents"])["documents"]
        assert any("UNIQUE_GROWN_SESSION_MARKER" in d for d in docs), (
            "grown session content was not picked up on re-mine"
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_mine_convos_unchanged_file_still_skipped(capsys):
    """A file whose content and mtime are unchanged must still be skipped
    -- the mtime check must not defeat the existing skip-on-unchanged
    optimization."""
    tmpdir = tempfile.mkdtemp()
    try:
        convo_path = Path(tmpdir) / "session.txt"
        convo_path.write_text(
            "> What is the plan?\nStart with the schema, then the API.\n\n"
            "> Any risks?\nMigration ordering is the main one.\n"
        )
        palace_path = os.path.join(tmpdir, "palace")

        mine_convos(tmpdir, palace_path, wing="test")
        capsys.readouterr()

        mine_convos(tmpdir, palace_path, wing="test")
        out = capsys.readouterr().out
        assert "Files skipped (already filed): 1" in out
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_mine_convos_grown_file_purges_stale_drawers_not_additive(capsys):
    """Re-mining a grown file must not leave duplicate/stale drawers behind
    -- purge-then-insert, not additive accumulation. Checks content
    directly (a drawer count comparison is fragile: ChromaDB collections
    can carry non-drawer bookkeeping rows unrelated to this behavior)."""
    tmpdir = tempfile.mkdtemp()
    try:
        convo_path = Path(tmpdir) / "session.txt"
        convo_path.write_text(
            "> What is the plan?\nUNIQUE_ORIGINAL_EXCHANGE_MARKER here.\n\n"
            "> Any risks?\nMigration ordering is the main one.\n"
        )
        palace_path = os.path.join(tmpdir, "palace")

        mine_convos(tmpdir, palace_path, wing="test")
        capsys.readouterr()

        convo_path.write_text(
            "> What is the plan?\nUNIQUE_ORIGINAL_EXCHANGE_MARKER here.\n\n"
            "> Any risks?\nMigration ordering is the main one.\n\n"
            "> One more exchange?\nUNIQUE_NEW_EXCHANGE_MARKER here.\n"
        )
        future = time.time() + 60
        os.utime(convo_path, (future, future))
        mine_convos(tmpdir, palace_path, wing="test")
        capsys.readouterr()

        client = chromadb.PersistentClient(path=palace_path)
        col = client.get_collection("mempalace_drawers")
        docs = col.get(include=["documents"])["documents"]

        original_hits = sum(1 for d in docs if "UNIQUE_ORIGINAL_EXCHANGE_MARKER" in d)
        new_hits = sum(1 for d in docs if "UNIQUE_NEW_EXCHANGE_MARKER" in d)
        assert original_hits == 1, (
            f"original exchange duplicated across re-mine: {original_hits} copies"
        )
        assert new_hits == 1, f"new exchange should appear exactly once, got {new_hits}"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_prefetch_mined_set_returns_stored_mtime():
    """prefetch_mined_set's dict carries each source_file's stored mtime,
    not just membership."""
    tmpdir = tempfile.mkdtemp()
    try:
        convo_path = Path(tmpdir) / "session.txt"
        convo_path.write_text(
            "> What is the plan?\nStart with the schema, then the API.\n\n"
            "> Any risks?\nMigration ordering is the main one.\n"
        )
        palace_path = os.path.join(tmpdir, "palace")
        mine_convos(tmpdir, palace_path, wing="test")

        resolved_file = str(convo_path.resolve())
        actual_mtime = os.path.getmtime(resolved_file)

        client = chromadb.PersistentClient(path=palace_path)
        col = client.get_collection("mempalace_drawers")
        mined = prefetch_mined_set(col, extract_mode="exchange")

        assert resolved_file in mined
        assert mined[resolved_file] is not None
        assert abs(mined[resolved_file] - actual_mtime) < 0.001
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_prefetch_mined_set_none_for_drawer_without_stored_mtime():
    """A drawer written before source_mtime existed (or with getmtime
    failure at write time) must surface as None, not be silently absent --
    None must be treated as stale by callers, not as 'unknown, assume ok'."""
    tmpdir = tempfile.mkdtemp()
    try:
        palace_path = os.path.join(tmpdir, "palace")
        client = chromadb.PersistentClient(path=palace_path)
        col = client.get_or_create_collection("mempalace_drawers")
        col.upsert(
            ids=["drawer_legacy_1"],
            documents=["legacy content with no source_mtime field"],
            metadatas=[
                {
                    "wing": "test",
                    "room": "general",
                    "source_file": "/fake/legacy/file.txt",
                    "chunk_index": 0,
                    "extract_mode": "exchange",
                    "normalize_version": 999,  # force >= current version
                }
            ],
        )
        mined = prefetch_mined_set(col, extract_mode="exchange")
        assert "/fake/legacy/file.txt" in mined
        assert mined["/fake/legacy/file.txt"] is None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_mine_convos_reprocesses_legacy_drawer_without_stored_mtime(capsys):
    """A file mined before source_mtime was tracked (simulated: drawer
    written directly, no source_mtime field) must be re-mined on the next
    run, not skipped forever -- this is the one-time backfill behavior."""
    tmpdir = tempfile.mkdtemp()
    try:
        convo_path = Path(tmpdir) / "session.txt"
        convo_path.write_text(
            "> What is the plan?\nUNIQUE_LEGACY_BACKFILL_MARKER here.\n\n"
            "> Any risks?\nMigration ordering is the main one.\n"
        )
        resolved_file = str(convo_path.resolve())
        palace_path = os.path.join(tmpdir, "palace")

        # Simulate a pre-existing drawer from before source_mtime existed.
        client = chromadb.PersistentClient(path=palace_path)
        col = client.get_or_create_collection("mempalace_drawers")
        from mempalace.palace import NORMALIZE_VERSION

        col.upsert(
            ids=["drawer_legacy_session_1"],
            documents=["stale legacy content, no mtime field"],
            metadatas=[
                {
                    "wing": "test",
                    "room": "general",
                    "source_file": resolved_file,
                    "chunk_index": 0,
                    "extract_mode": "exchange",
                    "normalize_version": NORMALIZE_VERSION,
                }
            ],
        )
        del col, client

        mine_convos(tmpdir, palace_path, wing="test")
        out = capsys.readouterr().out
        assert "Files skipped (already filed): 1" not in out

        client = chromadb.PersistentClient(path=palace_path)
        col = client.get_collection("mempalace_drawers")
        docs = col.get(include=["documents"])["documents"]
        assert any("UNIQUE_LEGACY_BACKFILL_MARKER" in d for d in docs)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_register_file_sentinel_includes_source_mtime():
    """The 0-chunk sentinel must stamp source_mtime too, so a file that
    later grows past the min-chunk-size floor is detected as changed
    instead of being skipped forever by the sentinel."""
    tmpdir = tempfile.mkdtemp()
    try:
        tiny_file = Path(tmpdir) / "tiny.txt"
        tiny_file.write_text("hi")
        palace_path = os.path.join(tmpdir, "palace")
        client = chromadb.PersistentClient(path=palace_path)
        col = client.get_or_create_collection("mempalace_drawers")

        _register_file(col, str(tiny_file), "test", "mempalace", "exchange")

        mined = prefetch_mined_set(col, extract_mode="exchange")
        assert str(tiny_file) in mined
        assert mined[str(tiny_file)] is not None
        assert abs(mined[str(tiny_file)] - os.path.getmtime(tiny_file)) < 0.001
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ── bulk-mining duplicate detection (opt-in, off by default) ───────────
#
# On a match, the new chunk is flagged (possible_duplicate_of /
# duplicate_similarity metadata) and still stored -- never skipped, per
# the "verbatim is sacred" policy this codebase already applies elsewhere.
# find_near_duplicates itself is unit-tested in test_searcher.py; these
# tests cover the config gate and the metadata-attachment contract.


class TestFlagPossibleDuplicates:
    def test_noop_when_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("MEMPALACE_DUPLICATE_DETECTION", raising=False)
        mock_col = MagicMock()
        batch_docs = ["some content"]
        batch_metas = [{"source_file": "a.txt"}]
        with patch("mempalace.searcher.find_near_duplicates") as mock_find:
            _flag_possible_duplicates(mock_col, batch_docs, batch_metas)
        mock_find.assert_not_called()
        assert "possible_duplicate_of" not in batch_metas[0]

    def test_attaches_metadata_when_enabled_and_match_found(self, monkeypatch):
        monkeypatch.setenv("MEMPALACE_DUPLICATE_DETECTION", "true")
        mock_col = MagicMock()
        batch_docs = ["near-duplicate content"]
        batch_metas = [{"source_file": "a.txt"}]
        with patch(
            "mempalace.searcher.find_near_duplicates",
            return_value=[("drawer_existing_1", 0.94)],
        ) as mock_find:
            _flag_possible_duplicates(mock_col, batch_docs, batch_metas)
        mock_find.assert_called_once()
        assert batch_metas[0]["possible_duplicate_of"] == "drawer_existing_1"
        assert batch_metas[0]["duplicate_similarity"] == 0.94

    def test_no_metadata_added_when_enabled_but_no_match(self, monkeypatch):
        monkeypatch.setenv("MEMPALACE_DUPLICATE_DETECTION", "true")
        mock_col = MagicMock()
        batch_docs = ["genuinely new content"]
        batch_metas = [{"source_file": "a.txt"}]
        with patch("mempalace.searcher.find_near_duplicates", return_value=[None]):
            _flag_possible_duplicates(mock_col, batch_docs, batch_metas)
        assert "possible_duplicate_of" not in batch_metas[0]

    def test_never_skips_the_insert_even_when_flagged(self, monkeypatch):
        """The flag is metadata-only -- batch_docs/batch_ids (what actually
        gets upserted) must be untouched; content is never dropped based
        on a similarity match."""
        monkeypatch.setenv("MEMPALACE_DUPLICATE_DETECTION", "true")
        mock_col = MagicMock()
        batch_docs = ["a", "b", "c"]
        batch_metas = [{"i": 0}, {"i": 1}, {"i": 2}]
        with patch(
            "mempalace.searcher.find_near_duplicates",
            return_value=[("d1", 0.99), ("d2", 0.95), None],
        ):
            _flag_possible_duplicates(mock_col, batch_docs, batch_metas)
        assert batch_docs == ["a", "b", "c"]  # untouched
        assert len(batch_metas) == 3  # nothing removed
        assert batch_metas[0]["possible_duplicate_of"] == "d1"
        assert batch_metas[1]["possible_duplicate_of"] == "d2"
        assert "possible_duplicate_of" not in batch_metas[2]


# ── incremental-mining cursor (compute + store only, nothing reads it yet) ──
#
# Nothing reads this cursor yet -- these tests cover that it gets computed
# correctly and stored on exactly the right drawer, not that mining
# behavior changes (it doesn't, yet).


class TestComputeConvoCursor:
    """_compute_convo_cursor takes the RAW content the caller already read
    for normalize()/chunk_exchanges -- not a file path -- specifically so
    it can never re-read a different state of a file that's actively
    being appended to (see the docstring for the full rationale)."""

    def test_zero_chunks_returns_none(self):
        content = '{"type":"user","message":{"content":"hi"}}\n'
        assert _compute_convo_cursor(content, num_chunks=0) is None

    def test_non_claude_code_content_returns_none(self):
        """Content that doesn't parse as Claude Code JSONL at all (e.g.
        plain text) gets no cursor -- the safe "no incremental path for
        this format" default."""
        content = "just some plain text, not JSON at all\nsecond line\n"
        assert _compute_convo_cursor(content, num_chunks=2) is None

    def test_below_exchange_chunking_threshold_returns_none(self):
        """The bug this test guards against: chunk_exchanges falls back to
        paragraph/character-offset chunking when the transcript has fewer
        than 3 quoted lines (chunk_exchanges' own quote_lines >= 3 gate).
        In that mode "chunk N" has no correspondence to "the Nth user
        turn" at all -- a cursor computed here would silently attach
        wrong position data to an unrelated chunk. Must mirror that gate
        exactly and return None rather than compute a misleading cursor.
        A single exchange produces only 2 quoted lines' worth of
        structure (one user turn) -- below the threshold.
        """
        import json as jsonlib

        content = "\n".join(
            [
                jsonlib.dumps({"type": "human", "message": {"content": "Q1"}}),
                jsonlib.dumps({"type": "assistant", "message": {"content": "A1"}}),
            ]
        )
        assert _compute_convo_cursor(content, num_chunks=2) is None

    def test_real_claude_code_jsonl_computes_correct_cursor(self):
        import json as jsonlib

        lines = [
            jsonlib.dumps({"type": "human", "message": {"content": "Q1"}}),  # line 0
            jsonlib.dumps({"type": "assistant", "message": {"content": "A1"}}),  # line 1
            jsonlib.dumps({"type": "human", "message": {"content": "Q2"}}),  # line 2
            jsonlib.dumps({"type": "assistant", "message": {"content": "A2"}}),  # line 3
            jsonlib.dumps({"type": "human", "message": {"content": "Q3"}}),  # line 4
            jsonlib.dumps({"type": "assistant", "message": {"content": "A3"}}),  # line 5
        ]
        content = "\n".join(lines) + "\n"

        # min_chunk_size=0: these synthetic exchanges are a handful of
        # characters each, well under the module's real 30-char floor --
        # irrelevant to what this test is checking (cursor arithmetic),
        # but the floor is applied for real when this function re-derives
        # the trailing exchange's own chunk count, so it must be
        # explicitly disabled here or the trailing exchange would be
        # dropped as noise before it's ever counted.
        cursor = _compute_convo_cursor(content, num_chunks=3, min_chunk_size=0)
        assert cursor is not None
        assert cursor["cursor_line"] == 4  # the third (last) user turn
        assert cursor["cursor_chunk_index"] == 2  # 0-indexed, last of 3 chunks
        assert cursor["cursor_format"] == "claude_code_jsonl"
        expected_hash = __import__("hashlib").sha256(lines[4].encode("utf-8")).hexdigest()
        assert cursor["cursor_anchor_hash"] == expected_hash

    def test_multi_chunk_trailing_exchange_points_to_first_chunk(self):
        """The bug this guards against: a trailing exchange whose own AI
        response is long enough to split across multiple physical chunks
        (via chunk_exchanges' chunk_size splitting) must get a cursor
        pointing at the FIRST of those chunks, not the last -- otherwise
        a future incremental re-mine would treat the earlier ones as part
        of the stable, unaffected prefix, when they actually belong to
        the same (about-to-be-regenerated) trailing exchange."""
        import json as jsonlib

        from mempalace.normalize import normalize

        long_response = "A" * 100
        lines = [
            jsonlib.dumps({"type": "human", "message": {"content": "Q1"}}),
            jsonlib.dumps({"type": "assistant", "message": {"content": "A1"}}),
            jsonlib.dumps({"type": "human", "message": {"content": "Q2"}}),
            jsonlib.dumps({"type": "assistant", "message": {"content": "A2"}}),
            jsonlib.dumps({"type": "human", "message": {"content": "Q3"}}),
            jsonlib.dumps({"type": "assistant", "message": {"content": long_response}}),
        ]
        content = "\n".join(lines) + "\n"

        normalized = normalize("session.jsonl", content=content)
        chunks = chunk_exchanges(normalized, chunk_size=20, min_chunk_size=0)
        # Confirms this test actually exercises a split trailing exchange
        # rather than accidentally testing the single-chunk case.
        assert len(chunks) > 3

        cursor = _compute_convo_cursor(
            content, num_chunks=len(chunks), chunk_size=20, min_chunk_size=0
        )
        assert cursor is not None

        trailing_from_full = chunks[cursor["cursor_chunk_index"] :]
        assert len(trailing_from_full) > 1  # really did land on a multi-chunk span
        # Every chunk here is either the exchange's opening slice (has
        # "Q3") or a pure slice of the long "A"-only response -- nothing
        # from an earlier, unrelated exchange leaked in.
        assert all("Q3" in c["content"] or set(c["content"]) <= {"A"} for c in trailing_from_full)
        # The chunk immediately before the cursor must belong to the
        # PRIOR exchange -- confirms the cursor doesn't point too early.
        if cursor["cursor_chunk_index"] > 0:
            assert "Q3" not in chunks[cursor["cursor_chunk_index"] - 1]["content"]


class TestIncrementalReparse:
    """_incremental_reparse takes a stored cursor and the CURRENT
    (possibly grown) raw content and must produce output that, combined
    with the stable prefix of a prior mine's chunks, is identical to a
    full re-chunk of the grown file -- without re-chunking the whole
    thing. Nothing calls this yet; these are equivalence tests against
    the real chunk_exchanges/normalize pipeline, not a hand-derived
    oracle, since that pipeline's exact chunking behavior is what an
    incremental re-mine must match byte-for-byte.
    """

    @staticmethod
    def _build_jsonl(num_exchanges: int, trailing_response: str = None) -> str:
        import json as jsonlib

        lines = []
        for i in range(1, num_exchanges + 1):
            lines.append(jsonlib.dumps({"type": "human", "message": {"content": f"Q{i}"}}))
            response = (
                f"A{i}" if not (i == num_exchanges and trailing_response) else trailing_response
            )
            lines.append(jsonlib.dumps({"type": "assistant", "message": {"content": response}}))
        return "\n".join(lines) + "\n"

    @staticmethod
    def _append_exchanges(raw: str, start: int, count: int) -> str:
        new_lines = []
        for i in range(start, start + count):
            new_lines.append(f'{{"type":"human","message":{{"content":"Q{i}"}}}}')
            new_lines.append(f'{{"type":"assistant","message":{{"content":"A{i}"}}}}')
        return raw.rstrip("\n") + "\n" + "\n".join(new_lines) + "\n"

    def _assert_equivalent_to_full_remine(
        self, raw_v1: str, raw_v2: str, chunk_size: int = 800, min_chunk_size: int = 0
    ) -> None:
        from mempalace.normalize import normalize

        chunks_v1 = chunk_exchanges(
            normalize("session.jsonl", content=raw_v1),
            chunk_size=chunk_size,
            min_chunk_size=min_chunk_size,
        )
        cursor = _compute_convo_cursor(
            raw_v1, len(chunks_v1), chunk_size=chunk_size, min_chunk_size=min_chunk_size
        )
        assert cursor is not None, "test setup must produce a real cursor"

        tail_chunks = _incremental_reparse(
            cursor, raw_v2, chunk_size=chunk_size, min_chunk_size=min_chunk_size
        )
        assert tail_chunks is not None

        stable_prefix = chunks_v1[: cursor["cursor_chunk_index"]]
        combined = stable_prefix + tail_chunks

        chunks_v2_full = chunk_exchanges(
            normalize("session.jsonl", content=raw_v2),
            chunk_size=chunk_size,
            min_chunk_size=min_chunk_size,
        )

        assert [c["content"] for c in combined] == [c["content"] for c in chunks_v2_full]
        assert [c["chunk_index"] for c in combined] == [c["chunk_index"] for c in chunks_v2_full]

    def test_equivalence_small_increment_below_exchange_threshold(self):
        """The common case: growing a session by one more exchange. The
        TAIL slice alone has only 2 quoted lines (the trailing exchange
        from v1 plus the one new exchange) -- below chunk_exchanges' own
        3-line exchange-vs-paragraph gate, proving _incremental_reparse
        must call _chunk_by_exchange directly rather than through that
        public dispatcher, or this would wrongly fall back to paragraph
        chunking for just the tail while the full document still uses
        exchange-pair chunking."""
        raw_v1 = self._build_jsonl(3)
        raw_v2 = self._append_exchanges(raw_v1, start=4, count=1)
        self._assert_equivalent_to_full_remine(raw_v1, raw_v2)

    def test_equivalence_many_new_exchanges(self):
        """Growing by several exchanges at once (e.g. a long working
        session since the last mine)."""
        raw_v1 = self._build_jsonl(3)
        raw_v2 = self._append_exchanges(raw_v1, start=4, count=5)
        self._assert_equivalent_to_full_remine(raw_v1, raw_v2)

    def test_equivalence_multi_chunk_trailing_exchange(self):
        """The trailing exchange at v1 time is itself long enough to span
        multiple physical chunks under a small chunk_size -- confirms
        the fix to _compute_convo_cursor's cursor_chunk_index (first
        chunk of the trailing exchange, not the last) combines correctly
        with _incremental_reparse's output rather than duplicating or
        dropping part of that exchange."""
        raw_v1 = self._build_jsonl(3, trailing_response="A" * 100)
        raw_v2 = self._append_exchanges(raw_v1, start=4, count=1)
        self._assert_equivalent_to_full_remine(raw_v1, raw_v2, chunk_size=20)

    def test_anchor_hash_mismatch_returns_none(self):
        """The anchor line itself was edited (not purely appended after)
        -- the append-only assumption this whole feature depends on was
        violated for this file. Must fall back to a full re-mine rather
        than trust a cursor that no longer describes reality."""
        from mempalace.normalize import normalize

        raw_v1 = self._build_jsonl(3)
        chunks_v1 = chunk_exchanges(
            normalize("session.jsonl", content=raw_v1),
            min_chunk_size=0,
        )
        cursor = _compute_convo_cursor(raw_v1, len(chunks_v1), min_chunk_size=0)
        assert cursor is not None

        raw_lines = raw_v1.strip().split("\n")
        raw_lines[cursor["cursor_line"]] = raw_lines[cursor["cursor_line"]].replace(
            "Q3", "Q3-edited"
        )
        tampered = "\n".join(raw_lines) + "\n"

        assert _incremental_reparse(cursor, tampered) is None

    def test_file_shrank_below_anchor_returns_none(self):
        """The file is now SHORTER than the stored anchor line -- it was
        truncated or rewritten, not grown. Never safe to trust."""
        cursor = {
            "cursor_line": 100,
            "cursor_chunk_index": 2,
            "cursor_anchor_hash": "deadbeef",
            "cursor_format": "claude_code_jsonl",
        }
        assert _incremental_reparse(cursor, "short content\nonly a few lines\n") is None

    def test_non_claude_code_cursor_format_returns_none(self):
        """A cursor for a format other than claude_code_jsonl (none exist
        yet, but the gate must hold for whatever comes next) is never
        acted on here."""
        cursor = {
            "cursor_line": 0,
            "cursor_chunk_index": 0,
            "cursor_anchor_hash": "deadbeef",
            "cursor_format": "some_future_format",
        }
        assert _incremental_reparse(cursor, "anything\n") is None

    def test_lone_trailing_user_message_returns_none(self):
        """The tail, parsed on its own, is just a single trailing user
        turn with no response yet (e.g. re-mining while the assistant is
        still mid-response). _try_claude_code_jsonl requires at least 2
        messages to recognize content as this format at all -- correctly
        falls back to a full re-mine rather than guess at incomplete
        data."""
        raw_v1 = self._build_jsonl(3)
        from mempalace.normalize import normalize

        chunks_v1 = chunk_exchanges(normalize("session.jsonl", content=raw_v1), min_chunk_size=0)
        cursor = _compute_convo_cursor(raw_v1, len(chunks_v1), min_chunk_size=0)
        assert cursor is not None

        # No new content appended -- the tail is exactly the same lone
        # trailing exchange as before, unchanged. Sanity check that this
        # normally resolves fine (min_chunk_size=0: these synthetic
        # exchanges are a handful of characters, under the module's real
        # 30-char floor, which is irrelevant to what this test checks).
        assert _incremental_reparse(cursor, raw_v1, min_chunk_size=0) is not None

        # Truncate raw_v2 to end right after the anchor line's OWN user
        # turn, before its assistant response -- simulates re-mining
        # mid-write.
        raw_lines = raw_v1.strip().split("\n")
        raw_v2 = "\n".join(raw_lines[: cursor["cursor_line"] + 1]) + "\n"
        assert _incremental_reparse(cursor, raw_v2, min_chunk_size=0) is None

    def test_equivalence_tool_round_in_original_trailing_exchange(self):
        """A tool_use/tool_result round entirely within the ORIGINAL
        trailing exchange (resolved before v1 was mined), with a brand
        new exchange appended after it in v2. Confirms re-parsing the
        tail in isolation correctly rebuilds tool_use_map from scratch
        for that exchange rather than needing anything from before the
        cursor."""
        import json as jsonlib

        lines_v1 = [
            jsonlib.dumps({"type": "human", "message": {"content": "Q1"}}),
            jsonlib.dumps({"type": "assistant", "message": {"content": "A1"}}),
            jsonlib.dumps({"type": "human", "message": {"content": "Q2"}}),
            jsonlib.dumps({"type": "assistant", "message": {"content": "A2"}}),
            jsonlib.dumps({"type": "human", "message": {"content": "Q3"}}),
            jsonlib.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "text", "text": "Let me check."},
                            {
                                "type": "tool_use",
                                "id": "tool1",
                                "name": "Bash",
                                "input": {"command": "ls"},
                            },
                        ]
                    },
                }
            ),
            jsonlib.dumps(
                {
                    "type": "human",
                    "message": {
                        "content": [
                            {"type": "tool_result", "tool_use_id": "tool1", "content": "file1.txt"}
                        ]
                    },
                }
            ),
            jsonlib.dumps({"type": "assistant", "message": {"content": "Found it."}}),
        ]
        raw_v1 = "\n".join(lines_v1) + "\n"
        raw_v2 = self._append_exchanges(raw_v1, start=4, count=1)
        self._assert_equivalent_to_full_remine(raw_v1, raw_v2)

    def test_equivalence_multi_round_tool_loop_in_new_trailing_exchange(self):
        """The NEW exchange added since v1 (not the original trailing
        one) itself contains a multi-round tool loop -- two separate
        tool_use/tool_result rounds folding into one assistant message
        -- before a final plain-text assistant reply. Confirms the
        assistant-message-merge logic in _try_claude_code_jsonl behaves
        identically whether it's parsing the tail in isolation or as
        part of a full-document parse."""
        import json as jsonlib

        raw_v1 = self._build_jsonl(3)
        new_lines = [
            jsonlib.dumps({"type": "human", "message": {"content": "Q4"}}),
            jsonlib.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "text", "text": "Checking..."},
                            {
                                "type": "tool_use",
                                "id": "tool1",
                                "name": "Bash",
                                "input": {"command": "ls"},
                            },
                        ]
                    },
                }
            ),
            jsonlib.dumps(
                {
                    "type": "human",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "tool1",
                                "content": "a.txt b.txt",
                            }
                        ]
                    },
                }
            ),
            jsonlib.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "tool2",
                                "name": "Read",
                                "input": {"file_path": "a.txt", "offset": 1, "limit": 10},
                            }
                        ]
                    },
                }
            ),
            jsonlib.dumps(
                {
                    "type": "human",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "tool2",
                                "content": "contents of a.txt",
                            }
                        ]
                    },
                }
            ),
            jsonlib.dumps(
                {"type": "assistant", "message": {"content": "Done, here's what I found."}}
            ),
        ]
        raw_v2 = raw_v1.rstrip("\n") + "\n" + "\n".join(new_lines) + "\n"
        self._assert_equivalent_to_full_remine(raw_v1, raw_v2)

    def test_malformed_cursor_returns_none_instead_of_raising(self):
        """A cursor dict missing required keys, or not a dict at all --
        e.g. stored drawer metadata that predates this field, or was
        hand-edited -- is just another way the append-only precondition
        can't be confirmed. Must return None like every other
        precondition failure, not raise."""
        assert (
            _incremental_reparse(
                {"cursor_format": "claude_code_jsonl", "cursor_anchor_hash": "x"}, "a\nb\n"
            )
            is None
        )
        assert _incremental_reparse(None, "a\nb\n") is None
        assert _incremental_reparse("not a dict", "a\nb\n") is None
        assert (
            _incremental_reparse(
                {
                    "cursor_format": "claude_code_jsonl",
                    "cursor_line": 0,
                    "cursor_chunk_index": 0,
                    "cursor_anchor_hash": "x",
                },
                None,
            )
            is None
        )
        assert (
            _incremental_reparse(
                {
                    "cursor_format": "claude_code_jsonl",
                    "cursor_line": "not an int",
                    "cursor_chunk_index": 0,
                    "cursor_anchor_hash": "x",
                },
                "a\nb\n",
            )
            is None
        )

    def test_non_positive_chunk_size_raises_value_error(self):
        """_compute_convo_cursor and _incremental_reparse both call
        _chunk_by_exchange directly, bypassing chunk_exchanges' own
        upfront chunk_size validation -- they must reproduce that
        validation themselves rather than let a bad value fail deep
        inside _emit_bounded with a confusing error."""
        cursor = {
            "cursor_format": "claude_code_jsonl",
            "cursor_line": 0,
            "cursor_chunk_index": 0,
            "cursor_anchor_hash": "x",
        }
        with pytest.raises(ValueError):
            _incremental_reparse(cursor, "a\nb\n", chunk_size=0)
        with pytest.raises(ValueError):
            _incremental_reparse(cursor, "a\nb\n", min_chunk_size=-1)
        with pytest.raises(ValueError):
            _compute_convo_cursor("a\nb\n", 3, chunk_size=0)


class TestConvoCursorStorage:
    """End-to-end: a real mine stores the cursor on exactly the last
    drawer, nowhere else, and general mode gets no cursor at all."""

    def test_cursor_stored_only_on_last_drawer(self):
        tmpdir = tempfile.mkdtemp()
        try:
            convo_path = Path(tmpdir) / "session.jsonl"
            # At least 3 exchanges: chunk_exchanges only takes the real
            # exchange-pair path (which a cursor can meaningfully anchor
            # on) once the transcript has >= 3 quoted lines; fewer than
            # that falls back to paragraph chunking, where no cursor is
            # computed at all (see TestComputeConvoCursor).
            entries = [
                '{"type":"human","message":{"content":"What is the plan?"}}',
                '{"type":"assistant","message":{"content":"Start with the schema."}}',
                '{"type":"human","message":{"content":"Any risks?"}}',
                '{"type":"assistant","message":{"content":"Migration ordering is the main one."}}',
                '{"type":"human","message":{"content":"What about rollback?"}}',
                '{"type":"assistant","message":{"content":"We snapshot before every migration."}}',
            ]
            convo_path.write_text("\n".join(entries) + "\n")
            palace_path = os.path.join(tmpdir, "palace")

            mine_convos(tmpdir, palace_path, wing="test")

            client = chromadb.PersistentClient(path=palace_path)
            col = client.get_collection("mempalace_drawers")
            result = col.get(include=["metadatas"])
            with_cursor = [m for m in result["metadatas"] if m and m.get("cursor_format")]
            assert len(with_cursor) == 1, (
                f"expected exactly one drawer with a cursor, got {len(with_cursor)}"
            )
            assert with_cursor[0]["cursor_chunk_index"] == max(
                m["chunk_index"] for m in result["metadatas"] if m
            )
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_general_mode_gets_no_cursor(self):
        tmpdir = tempfile.mkdtemp()
        try:
            convo_path = Path(tmpdir) / "session.jsonl"
            entries = [
                '{"type":"human","message":{"content":"What did we decide about the API design?"}}',
                '{"type":"assistant","message":{"content":"We decided to use REST because it keeps things simple and well understood by the team."}}',
            ]
            convo_path.write_text("\n".join(entries) + "\n")
            palace_path = os.path.join(tmpdir, "palace")

            mine_convos(tmpdir, palace_path, wing="test", extract_mode="general")

            client = chromadb.PersistentClient(path=palace_path)
            col = client.get_collection("mempalace_drawers")
            result = col.get(include=["metadatas"])
            with_cursor = [m for m in result["metadatas"] if m and m.get("cursor_format")]
            assert with_cursor == []
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestFetchStoredCursor:
    """_fetch_stored_cursor scans one source file's own drawers for the
    cursor a prior mine stored, applying the same schema-version and
    extract_mode scoping other lookups in this module use -- verified
    directly here rather than relying only on end-to-end coverage, since
    an incorrect cursor returned here would silently drive an
    incremental mine off stale or wrong data."""

    @staticmethod
    def _make_collection(metadatas: list) -> MagicMock:
        ids = [f"d{i}" for i in range(len(metadatas))]
        mock_col = MagicMock()
        # _fetch_stored_cursor loops until a batch comes back with no ids
        # (matching the paginated-scan pattern used elsewhere in this
        # module) -- the first call returns the real data, the second
        # terminates the loop.
        mock_col.get.side_effect = [
            {"ids": ids, "metadatas": metadatas},
            {"ids": [], "metadatas": []},
        ]
        return mock_col

    def test_returns_none_when_no_cursor_bearing_drawer(self):
        col = self._make_collection([{"source_file": "a.jsonl", "chunk_index": 0}])
        assert _fetch_stored_cursor(col, "a.jsonl", "exchange") is None

    def test_returns_cursor_and_room_when_found(self):
        from mempalace.palace import NORMALIZE_VERSION

        meta = {
            "source_file": "a.jsonl",
            "chunk_index": 2,
            "cursor_line": 10,
            "cursor_chunk_index": 2,
            "cursor_anchor_hash": "abc123",
            "cursor_format": "claude_code_jsonl",
            "room": "technical",
            "normalize_version": NORMALIZE_VERSION,
        }
        col = self._make_collection([meta])
        result = _fetch_stored_cursor(col, "a.jsonl", "exchange")
        assert result == {
            "cursor_line": 10,
            "cursor_chunk_index": 2,
            "cursor_anchor_hash": "abc123",
            "cursor_format": "claude_code_jsonl",
            "room": "technical",
        }

    def test_returns_none_when_cursor_drawer_has_stale_normalize_version(self):
        """A cursor-bearing drawer stamped with an older schema version
        must not drive an incremental mine -- the stored chunk/cursor
        shape may not match what the current pipeline would produce.
        Enforced directly here, not just relied on via a caller's own
        mined_mtimes gate."""
        from mempalace.palace import NORMALIZE_VERSION

        meta = {
            "source_file": "a.jsonl",
            "chunk_index": 2,
            "cursor_line": 10,
            "cursor_chunk_index": 2,
            "cursor_anchor_hash": "abc123",
            "cursor_format": "claude_code_jsonl",
            "room": "technical",
            "normalize_version": NORMALIZE_VERSION - 1,
        }
        col = self._make_collection([meta])
        assert _fetch_stored_cursor(col, "a.jsonl", "exchange") is None

    def test_returns_none_when_more_than_one_cursor_bearing_drawer(self):
        """A data inconsistency (shouldn't happen under normal operation,
        since only one chunk per file is ever stamped) -- safer to fall
        back to a full re-mine than guess which one is current."""
        from mempalace.palace import NORMALIZE_VERSION

        base = {
            "source_file": "a.jsonl",
            "cursor_format": "claude_code_jsonl",
            "cursor_line": 4,
            "cursor_anchor_hash": "x",
            "room": "technical",
            "normalize_version": NORMALIZE_VERSION,
        }
        meta1 = {**base, "chunk_index": 1, "cursor_chunk_index": 1}
        meta2 = {**base, "chunk_index": 3, "cursor_chunk_index": 3}
        col = self._make_collection([meta1, meta2])
        assert _fetch_stored_cursor(col, "a.jsonl", "exchange") is None

    def test_ignores_drawers_from_a_different_extract_mode(self):
        from mempalace.palace import NORMALIZE_VERSION

        meta = {
            "source_file": "a.jsonl",
            "chunk_index": 0,
            "extract_mode": "general",
            "cursor_format": "claude_code_jsonl",
            "cursor_line": 0,
            "cursor_chunk_index": 0,
            "cursor_anchor_hash": "x",
            "room": "technical",
            "normalize_version": NORMALIZE_VERSION,
        }
        col = self._make_collection([meta])
        assert _fetch_stored_cursor(col, "a.jsonl", "exchange") is None


# ── incremental mining wired in (on by default, opt-out) ─────────────────


class TestIncrementalMiningWiredIn:
    """End-to-end through the real mine_convos()/palace path: when
    incremental_mining_enabled applies (the default, after a real-corpus
    benchmark -- see MempalaceConfig's docstring), growing a previously-
    mined transcript re-chunks only the trailing exchange onward instead
    of purging and rebuilding the whole file -- proven by the STABLE
    prefix's drawers surviving the second mine completely untouched
    (same filed_at, not just same content -- filed_at is stamped fresh
    on every upsert, so an unchanged filed_at proves no re-upsert
    happened). Explicitly disabling it (MEMPALACE_INCREMENTAL_MINING=0)
    still produces the old full-remine behavior exactly."""

    @staticmethod
    def _write_jsonl(path: Path, num_exchanges: int) -> None:
        import json as jsonlib

        lines = []
        for i in range(1, num_exchanges + 1):
            lines.append(
                jsonlib.dumps(
                    {"type": "human", "message": {"content": f"Question number {i} about the plan"}}
                )
            )
            lines.append(
                jsonlib.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "content": f"Answer number {i} explaining the approach in detail"
                        },
                    }
                )
            )
        path.write_text("\n".join(lines) + "\n")

    def test_explicitly_disabled_full_remine_on_growth(self, monkeypatch, capsys):
        """Opting back out (MEMPALACE_INCREMENTAL_MINING=false) must still
        produce the old full-remine behavior, with no "(incremental)"
        marker -- the flag has to work in both directions, not just
        toward the new default."""
        monkeypatch.setenv("MEMPALACE_INCREMENTAL_MINING", "false")
        tmpdir = tempfile.mkdtemp()
        try:
            convo_path = Path(tmpdir) / "session.jsonl"
            self._write_jsonl(convo_path, 3)
            palace_path = os.path.join(tmpdir, "palace")

            mine_convos(tmpdir, palace_path, wing="test")
            capsys.readouterr()

            time.sleep(0.05)  # ensure a distinct mtime
            self._write_jsonl(convo_path, 4)
            mine_convos(tmpdir, palace_path, wing="test")
            out = capsys.readouterr().out
            assert "(incremental)" not in out
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_default_takes_incremental_path_with_no_override(self, monkeypatch, capsys):
        """incremental_mining_enabled defaults to True (see
        MempalaceConfig's docstring for the benchmark backing this) --
        growing a previously-mined file with NO env var or config.json
        override at all must take the incremental path out of the box."""
        monkeypatch.delenv("MEMPALACE_INCREMENTAL_MINING", raising=False)
        tmpdir = tempfile.mkdtemp()
        try:
            convo_path = Path(tmpdir) / "session.jsonl"
            self._write_jsonl(convo_path, 3)
            palace_path = os.path.join(tmpdir, "palace")

            mine_convos(tmpdir, palace_path, wing="test")
            capsys.readouterr()

            time.sleep(0.05)
            self._write_jsonl(convo_path, 4)
            mine_convos(tmpdir, palace_path, wing="test")
            out = capsys.readouterr().out
            assert "(incremental)" in out
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_enabled_reuses_stable_prefix_without_reupserting(self, monkeypatch, capsys):
        monkeypatch.setenv("MEMPALACE_INCREMENTAL_MINING", "true")
        tmpdir = tempfile.mkdtemp()
        try:
            convo_path = Path(tmpdir) / "session.jsonl"
            self._write_jsonl(convo_path, 3)
            palace_path = os.path.join(tmpdir, "palace")

            mine_convos(tmpdir, palace_path, wing="test")
            capsys.readouterr()

            client = chromadb.PersistentClient(path=palace_path)
            col = client.get_collection("mempalace_drawers")
            first = col.get(include=["metadatas", "documents"])
            first_by_id = dict(zip(first["ids"], zip(first["documents"], first["metadatas"])))
            del col, client

            time.sleep(0.05)
            self._write_jsonl(convo_path, 5)  # grow by 2 more exchanges

            mine_convos(tmpdir, palace_path, wing="test")
            out = capsys.readouterr().out
            assert "(incremental)" in out

            client = chromadb.PersistentClient(path=palace_path)
            col = client.get_collection("mempalace_drawers")
            second = col.get(include=["metadatas", "documents"])
            second_by_id = dict(zip(second["ids"], zip(second["documents"], second["metadatas"])))

            # Every drawer except the one that carried the cursor (the
            # old trailing exchange, superseded by this pass) must
            # survive completely untouched.
            first_cursor_id = next(
                did for did, (_, meta) in first_by_id.items() if meta.get("cursor_format")
            )
            stable_ids = set(first_by_id) - {first_cursor_id}
            assert stable_ids, "test setup should produce more than one drawer on the first mine"
            for did in stable_ids:
                assert did in second_by_id, f"{did} missing after incremental mine"
                assert second_by_id[did][0] == first_by_id[did][0], f"{did} content changed"
                assert second_by_id[did][1].get("filed_at") == first_by_id[did][1].get(
                    "filed_at"
                ), f"{did}'s filed_at changed -- it was re-upserted, not left untouched"

            # The end result must still equal a full re-mine of the same
            # final content -- the equivalence claim, now exercised
            # end-to-end through the real palace.
            del col, client
            palace_path_full = os.path.join(tmpdir, "palace_full_control")
            mine_convos(tmpdir, palace_path_full, wing="test")
            client2 = chromadb.PersistentClient(path=palace_path_full)
            col2 = client2.get_collection("mempalace_drawers")
            full = col2.get(include=["documents"])
            assert sorted(second["documents"]) == sorted(full["documents"])
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_falls_back_to_full_remine_when_no_stored_cursor(self, monkeypatch, capsys):
        """A file with nothing to resume from (mined before any cursor
        was ever computed for it -- e.g. it was below the exchange-
        chunking threshold at the time) still mines correctly via the
        existing full path, even with incremental mining enabled."""
        monkeypatch.setenv("MEMPALACE_INCREMENTAL_MINING", "true")
        tmpdir = tempfile.mkdtemp()
        try:
            convo_path = Path(tmpdir) / "session.jsonl"
            entries = [
                '{"type":"human","message":{"content":"A short question long enough to clear the min chunk size floor"}}',
                '{"type":"assistant","message":{"content":"A short answer long enough to clear the min chunk size floor too"}}',
            ]
            convo_path.write_text("\n".join(entries) + "\n")
            palace_path = os.path.join(tmpdir, "palace")
            mine_convos(tmpdir, palace_path, wing="test")
            capsys.readouterr()

            time.sleep(0.05)
            self._write_jsonl(convo_path, 3)  # now clears the threshold

            mine_convos(tmpdir, palace_path, wing="test")
            out = capsys.readouterr().out
            assert "(incremental)" not in out
            assert "Files skipped (already filed): 0" in out
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
