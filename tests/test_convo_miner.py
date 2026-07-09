import os
import tempfile
import shutil
import time
from pathlib import Path

import chromadb
import pytest

from mempalace.convo_miner import (
    _is_ai_tool_path,
    _register_file,
    _resolve_wing,
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
