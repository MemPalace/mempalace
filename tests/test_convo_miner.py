import os
import tempfile
import shutil
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import chromadb
import pytest

from mempalace.config import MempalaceConfig
from mempalace.convo_miner import (
    _compute_convo_cursor,
    _extract_cwd_from_codex_session,
    _fetch_stored_cursor,
    _flag_or_drop_duplicates,
    _incremental_reparse,
    _is_ai_tool_path,
    _is_claude_code_projects_path,
    _is_codex_path,
    _register_file,
    _resolve_wing,
    _resolve_wing_for_file,
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


# ── _is_claude_code_projects_path / _is_codex_path — narrow format gates ─
#
# Narrower than _is_ai_tool_path: used by _resolve_wing_for_file to decide
# which per-file cwd-extraction strategy (if any) applies to one specific
# conversation file.


def test_is_claude_code_projects_path_true(tmp_path):
    target = tmp_path / ".claude" / "projects" / "-Users-x" / "session.jsonl"
    target.parent.mkdir(parents=True)
    assert _is_claude_code_projects_path(target) is True


def test_is_claude_code_projects_path_false_for_codex(tmp_path):
    target = tmp_path / ".codex" / "sessions" / "session.jsonl"
    target.parent.mkdir(parents=True)
    assert _is_claude_code_projects_path(target) is False


def test_is_codex_path_true(tmp_path):
    target = tmp_path / ".codex" / "sessions" / "2026" / "session.jsonl"
    target.parent.mkdir(parents=True)
    assert _is_codex_path(target) is True


def test_is_codex_path_false_for_claude_code(tmp_path):
    target = tmp_path / ".claude" / "projects" / "-Users-x" / "session.jsonl"
    target.parent.mkdir(parents=True)
    assert _is_codex_path(target) is False


# ── _extract_cwd_from_codex_session ──────────────────────────────────────
#
# Codex CLI nests cwd under payload, on session_meta/turn_context records
# -- confirmed against real ~/.codex/sessions/**/*.jsonl files, a
# structurally different location than Claude Code's top-level cwd field.


def test_extract_cwd_from_codex_session_meta_record(tmp_path):
    import json as jsonlib

    f = tmp_path / "session.jsonl"
    f.write_text(
        jsonlib.dumps(
            {
                "timestamp": "2026-07-05T15:59:41Z",
                "type": "session_meta",
                "payload": {"cwd": "/Users/jrmurray/Code/forktail/forktail-app"},
            }
        )
        + "\n"
    )
    assert _extract_cwd_from_codex_session(f) == "/Users/jrmurray/Code/forktail/forktail-app"


def test_extract_cwd_from_codex_turn_context_record(tmp_path):
    import json as jsonlib

    f = tmp_path / "session.jsonl"
    f.write_text(
        jsonlib.dumps(
            {
                "timestamp": "2026-07-05T15:59:42Z",
                "type": "turn_context",
                "payload": {"cwd": "/Users/x/Code/y", "workspace_roots": ["/Users/x/Code/y"]},
            }
        )
        + "\n"
    )
    assert _extract_cwd_from_codex_session(f) == "/Users/x/Code/y"


def test_extract_cwd_from_codex_session_ignores_other_record_types(tmp_path):
    import json as jsonlib

    f = tmp_path / "session.jsonl"
    lines = [
        jsonlib.dumps({"type": "response_item", "payload": {"cwd": "/should/not/match"}}),
        jsonlib.dumps({"type": "session_meta", "payload": {"cwd": "/real/project"}}),
    ]
    f.write_text("\n".join(lines) + "\n")
    assert _extract_cwd_from_codex_session(f) == "/real/project"


def test_extract_cwd_from_codex_session_none_if_top_level_cwd(tmp_path):
    """Claude-Code-shaped top-level cwd (not nested under payload) must
    NOT match -- confirms the two formats' extractors stay independent."""
    import json as jsonlib

    f = tmp_path / "session.jsonl"
    f.write_text(jsonlib.dumps({"type": "session_meta", "cwd": "/should/not/match"}) + "\n")
    assert _extract_cwd_from_codex_session(f) is None


def test_extract_cwd_from_codex_session_none_if_malformed(tmp_path):
    f = tmp_path / "session.jsonl"
    f.write_text("not valid json at all\n")
    assert _extract_cwd_from_codex_session(f) is None


def test_extract_cwd_from_codex_session_none_if_file_missing(tmp_path):
    assert _extract_cwd_from_codex_session(tmp_path / "missing.jsonl") is None


# ── _resolve_wing_for_file — per-file wing resolution ────────────────────


def test_resolve_wing_for_file_explicit_wing_always_wins(tmp_path):
    """The exact bug this must never repeat: an explicit --wing wing_api
    must be indistinguishable from any other explicit choice, not
    silently overridden by per-file detection just because it happens to
    match the AI-tool-path sentinel default (MemPalace/mempalace#1757)."""
    import json as jsonlib

    session = tmp_path / ".claude" / "projects" / "-Users-x" / "session.jsonl"
    session.parent.mkdir(parents=True)
    session.write_text(jsonlib.dumps({"type": "user", "cwd": "/Users/x/some-real-project"}) + "\n")

    assert _resolve_wing_for_file(session, "wing_api", "wing_api") == "wing_api"
    assert _resolve_wing_for_file(session, "my_custom_wing", "wing_api") == "my_custom_wing"


def test_resolve_wing_for_file_claude_code_resolves_from_cwd(tmp_path):
    import json as jsonlib

    session = tmp_path / ".claude" / "projects" / "-Users-x-forktail" / "session.jsonl"
    session.parent.mkdir(parents=True)
    session.write_text(
        jsonlib.dumps({"type": "user", "cwd": "/Users/x/Code/forktail/forktail-app"}) + "\n"
    )
    assert _resolve_wing_for_file(session, None, "wing_api") == "forktail_app"


def test_resolve_wing_for_file_claude_code_collapses_worktree(tmp_path):
    import json as jsonlib

    session = tmp_path / ".claude" / "projects" / "-x" / "session.jsonl"
    session.parent.mkdir(parents=True)
    cwd = "/Users/x/Code/forktail/forktail-app/.claude/worktrees/silly-mcnulty-d987f4"
    session.write_text(jsonlib.dumps({"type": "user", "cwd": cwd}) + "\n")
    assert _resolve_wing_for_file(session, None, "wing_api") == "forktail_app"


def test_resolve_wing_for_file_codex_resolves_from_nested_cwd(tmp_path):
    import json as jsonlib

    session = tmp_path / ".codex" / "sessions" / "2026" / "session.jsonl"
    session.parent.mkdir(parents=True)
    session.write_text(
        jsonlib.dumps(
            {"type": "session_meta", "payload": {"cwd": "/Users/x/Code/forktail/forktail-app"}}
        )
        + "\n"
    )
    assert _resolve_wing_for_file(session, None, "wing_api") == "forktail_app"


def test_resolve_wing_for_file_falls_back_when_cwd_unreadable(tmp_path):
    """Malformed/empty session with no readable cwd -> falls back to
    default_wing, not a crash."""
    session = tmp_path / ".claude" / "projects" / "-x" / "empty.jsonl"
    session.parent.mkdir(parents=True)
    session.write_text("not valid json\n")
    assert _resolve_wing_for_file(session, None, "wing_api") == "wing_api"


def test_resolve_wing_for_file_gemini_not_attempted_falls_back(tmp_path):
    """No confirmed cwd-equivalent field for Gemini (no real session data,
    no upstream reference) -- stays on the existing coarse fallback rather
    than risk a silent misresolution against an unverified schema."""
    import json as jsonlib

    session = tmp_path / ".gemini" / "tmp" / "abc" / "chats" / "session.jsonl"
    session.parent.mkdir(parents=True)
    session.write_text(jsonlib.dumps({"type": "user", "cwd": "/Users/x/some-project"}) + "\n")
    assert _resolve_wing_for_file(session, None, "wing_api") == "wing_api"


def test_resolve_wing_for_file_non_ai_tool_path_falls_back(tmp_path):
    session = tmp_path / "Documents" / "myproject" / "chat.txt"
    session.parent.mkdir(parents=True)
    session.write_text("> hi\nhello\n")
    assert _resolve_wing_for_file(session, None, "myproject") == "myproject"


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


# ── min_wing_resolution_version — one-time per-project-wing backfill ────
#
# file_already_mined/prefetch_mined_set's min_wing_resolution_version
# parameter is None by default (used by no caller except the convo
# miner's per-project wing-resolution backfill) -- passing it excludes a
# file whose stored wing_resolution_version is missing or older than it,
# forcing a full re-mine that resolves the file's correct per-project
# wing exactly once. Mirrors normalize_version's existing stale-schema
# handling, scoped separately so it never affects the project/format
# miners (which never pass this parameter).


def test_prefetch_mined_set_excludes_stale_wing_resolution_version():
    tmpdir = tempfile.mkdtemp()
    try:
        palace_path = os.path.join(tmpdir, "palace")
        client = chromadb.PersistentClient(path=palace_path)
        col = client.get_or_create_collection("mempalace_drawers")
        col.upsert(
            ids=["drawer_1"],
            documents=["content mined before wing_resolution_version existed"],
            metadatas=[
                {
                    "wing": "wing_api",
                    "room": "general",
                    "source_file": "/fake/pre-wing-feature/file.txt",
                    "chunk_index": 0,
                    "extract_mode": "exchange",
                    "normalize_version": 999,
                    "source_mtime": 123.456,
                    # no wing_resolution_version field at all
                }
            ],
        )
        without_check = prefetch_mined_set(col, extract_mode="exchange")
        assert "/fake/pre-wing-feature/file.txt" in without_check

        with_check = prefetch_mined_set(col, extract_mode="exchange", min_wing_resolution_version=1)
        assert "/fake/pre-wing-feature/file.txt" not in with_check
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_prefetch_mined_set_includes_current_wing_resolution_version():
    tmpdir = tempfile.mkdtemp()
    try:
        palace_path = os.path.join(tmpdir, "palace")
        client = chromadb.PersistentClient(path=palace_path)
        col = client.get_or_create_collection("mempalace_drawers")
        col.upsert(
            ids=["drawer_1"],
            documents=["content mined under the current wing-resolution version"],
            metadatas=[
                {
                    "wing": "forktail_app",
                    "room": "general",
                    "source_file": "/fake/current/file.txt",
                    "chunk_index": 0,
                    "extract_mode": "exchange",
                    "normalize_version": 999,
                    "source_mtime": 123.456,
                    "wing_resolution_version": 1,
                }
            ],
        )
        mined = prefetch_mined_set(col, extract_mode="exchange", min_wing_resolution_version=1)
        assert "/fake/current/file.txt" in mined
        assert mined["/fake/current/file.txt"] == 123.456
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_file_already_mined_false_for_stale_wing_resolution_version():
    tmpdir = tempfile.mkdtemp()
    try:
        palace_path = os.path.join(tmpdir, "palace")
        client = chromadb.PersistentClient(path=palace_path)
        col = client.get_or_create_collection("mempalace_drawers")
        source_file = str(Path(tmpdir) / "session.txt")
        Path(source_file).write_text("hi")
        col.upsert(
            ids=["drawer_1"],
            documents=["content"],
            metadatas=[
                {
                    "wing": "wing_api",
                    "room": "general",
                    "source_file": source_file,
                    "chunk_index": 0,
                    "extract_mode": "exchange",
                    "normalize_version": 999,
                    "source_mtime": os.path.getmtime(source_file),
                }
            ],
        )
        assert file_already_mined(col, source_file, extract_mode="exchange") is True
        assert (
            file_already_mined(
                col, source_file, extract_mode="exchange", min_wing_resolution_version=1
            )
            is False
        )
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


class TestFlagOrDropDuplicates:
    @staticmethod
    def _call(batch_docs, batch_metas, mock_col=None, batch_ids=None, batch_rooms=None):
        mock_col = mock_col if mock_col is not None else MagicMock()
        batch_ids = (
            batch_ids if batch_ids is not None else [f"id{i}" for i in range(len(batch_docs))]
        )
        batch_rooms = batch_rooms if batch_rooms is not None else ["technical"] * len(batch_docs)
        dropped = _flag_or_drop_duplicates(
            mock_col, "a.txt", batch_docs, batch_ids, batch_metas, batch_rooms
        )
        return dropped, batch_ids, batch_rooms

    def test_noop_when_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("MEMPALACE_DUPLICATE_DETECTION", raising=False)
        batch_docs = ["some content"]
        batch_metas = [{"source_file": "a.txt"}]
        with patch("mempalace.searcher.find_near_duplicates") as mock_find:
            dropped, *_ = self._call(batch_docs, batch_metas)
        mock_find.assert_not_called()
        assert "possible_duplicate_of" not in batch_metas[0]
        assert dropped == 0

    def test_attaches_metadata_when_enabled_and_match_found(self, monkeypatch):
        monkeypatch.setenv("MEMPALACE_DUPLICATE_DETECTION", "true")
        batch_docs = ["near-duplicate content"]
        batch_metas = [{"source_file": "a.txt"}]
        with patch(
            "mempalace.searcher.find_near_duplicates",
            return_value=[("drawer_existing_1", 0.94)],
        ) as mock_find:
            dropped, *_ = self._call(batch_docs, batch_metas)
        mock_find.assert_called_once()
        assert batch_metas[0]["possible_duplicate_of"] == "drawer_existing_1"
        assert batch_metas[0]["duplicate_similarity"] == 0.94
        assert dropped == 0  # drop threshold (0.97) not cleared, and drop_enabled is off anyway

    def test_no_metadata_added_when_enabled_but_no_match(self, monkeypatch):
        monkeypatch.setenv("MEMPALACE_DUPLICATE_DETECTION", "true")
        batch_docs = ["genuinely new content"]
        batch_metas = [{"source_file": "a.txt"}]
        with patch("mempalace.searcher.find_near_duplicates", return_value=[None]):
            self._call(batch_docs, batch_metas)
        assert "possible_duplicate_of" not in batch_metas[0]

    def test_never_drops_when_drop_disabled(self, monkeypatch):
        """Flagging is metadata-only by default -- batch_docs/batch_ids
        (what actually gets upserted) must be untouched even for a
        near-perfect match, unless duplicate_drop_enabled is separately
        turned on."""
        monkeypatch.setenv("MEMPALACE_DUPLICATE_DETECTION", "true")
        monkeypatch.delenv("MEMPALACE_DUPLICATE_DROP", raising=False)
        batch_docs = ["a", "b", "c"]
        batch_metas = [{"i": 0}, {"i": 1}, {"i": 2}]
        with patch(
            "mempalace.searcher.find_near_duplicates",
            return_value=[("d1", 0.99), ("d2", 0.95), None],
        ):
            dropped, batch_ids, _ = self._call(batch_docs, batch_metas)
        assert dropped == 0
        assert batch_docs == ["a", "b", "c"]  # untouched
        assert len(batch_metas) == 3  # nothing removed
        assert len(batch_ids) == 3
        assert batch_metas[0]["possible_duplicate_of"] == "d1"
        assert batch_metas[1]["possible_duplicate_of"] == "d2"
        assert "possible_duplicate_of" not in batch_metas[2]

    def test_drops_when_drop_enabled_and_above_drop_threshold(self, monkeypatch):
        monkeypatch.setenv("MEMPALACE_DUPLICATE_DETECTION", "true")
        monkeypatch.setenv("MEMPALACE_DUPLICATE_DROP", "true")
        batch_docs = ["exact-ish duplicate"]
        batch_metas = [{"i": 0}]
        with patch(
            "mempalace.searcher.find_near_duplicates",
            return_value=[("d1", 0.99)],  # clears the default 0.97 drop threshold
        ):
            dropped, batch_ids, batch_rooms = self._call(batch_docs, batch_metas)
        assert dropped == 1
        assert batch_docs == []
        assert batch_ids == []
        assert batch_metas == []
        assert batch_rooms == []

    def test_flags_but_does_not_drop_between_flag_and_drop_threshold(self, monkeypatch):
        monkeypatch.setenv("MEMPALACE_DUPLICATE_DETECTION", "true")
        monkeypatch.setenv("MEMPALACE_DUPLICATE_DROP", "true")
        batch_docs = ["similar but not certain"]
        batch_metas = [{"i": 0}]
        with patch(
            "mempalace.searcher.find_near_duplicates",
            # Clears the 0.9 flag threshold but not the stricter 0.97
            # default drop threshold.
            return_value=[("d1", 0.93)],
        ):
            dropped, batch_ids, _ = self._call(batch_docs, batch_metas)
        assert dropped == 0
        assert batch_docs == ["similar but not certain"]
        assert batch_ids == ["id0"]
        assert batch_metas[0]["possible_duplicate_of"] == "d1"
        assert batch_metas[0]["duplicate_similarity"] == 0.93

    def test_mixed_batch_drops_only_matching_indices_consistently(self, monkeypatch):
        """batch_docs/batch_ids/batch_metas/batch_rooms must all shrink
        to the SAME surviving indices, in the same order -- a dropped
        chunk in the middle of a batch must not misalign the others."""
        monkeypatch.setenv("MEMPALACE_DUPLICATE_DETECTION", "true")
        monkeypatch.setenv("MEMPALACE_DUPLICATE_DROP", "true")
        batch_docs = ["dropped", "flagged", "clean"]
        batch_metas = [{"i": 0}, {"i": 1}, {"i": 2}]
        batch_ids = ["id0", "id1", "id2"]
        batch_rooms = ["technical", "problems", "planning"]
        with patch(
            "mempalace.searcher.find_near_duplicates",
            return_value=[("d0", 0.99), ("d1", 0.93), None],
        ):
            dropped = _flag_or_drop_duplicates(
                MagicMock(), "a.txt", batch_docs, batch_ids, batch_metas, batch_rooms
            )
        assert dropped == 1
        assert batch_docs == ["flagged", "clean"]
        assert batch_ids == ["id1", "id2"]
        assert batch_rooms == ["problems", "planning"]
        assert batch_metas[0]["possible_duplicate_of"] == "d1"
        assert "possible_duplicate_of" not in batch_metas[1]

    def test_drop_threshold_clamped_to_at_least_flag_threshold(self, monkeypatch):
        """A misconfigured drop_threshold BELOW the flag threshold must
        not make dropping easier to trigger than flagging --
        find_near_duplicates only ever returns matches at or above the
        flag threshold, so an unclamped lower drop_threshold would drop
        everything that gets flagged. The clamp guarantees dropping
        stays at least as strict as flagging even then."""
        monkeypatch.setenv("MEMPALACE_DUPLICATE_DETECTION", "true")
        monkeypatch.setenv("MEMPALACE_DUPLICATE_DROP", "true")
        monkeypatch.setattr(MempalaceConfig, "duplicate_drop_threshold", 0.5)
        batch_docs = ["borderline match"]
        batch_metas = [{"i": 0}]
        with patch(
            "mempalace.searcher.find_near_duplicates",
            # Exactly at the (default 0.9) flag threshold -- the lowest
            # similarity find_near_duplicates would ever return a match
            # for.
            return_value=[("d1", 0.9)],
        ):
            dropped, *_ = self._call(batch_docs, batch_metas)
        assert dropped == 1


class TestDuplicateDropWiredIn:
    """End-to-end through the real mine_convos()/palace path, using real
    embeddings (not mocked) -- two files with identical exchange text
    should embed to the same vector and clear the default 0.97 drop
    threshold, proving the wiring works with the actual similarity
    backend, not just the mocked unit tests above."""

    _SHARED_TEXT = (
        "> What is the deployment process?\n"
        "We deploy via a blue-green rollout with automated health checks "
        "before traffic cuts over to the new version.\n"
    )

    def test_drop_enabled_removes_near_certain_duplicate(self, monkeypatch, capsys):
        monkeypatch.setenv("MEMPALACE_DUPLICATE_DETECTION", "true")
        monkeypatch.setenv("MEMPALACE_DUPLICATE_DROP", "true")
        tmpdir = tempfile.mkdtemp()
        try:
            (Path(tmpdir) / "a.txt").write_text(self._SHARED_TEXT)
            (Path(tmpdir) / "b.txt").write_text(self._SHARED_TEXT)
            palace_path = os.path.join(tmpdir, "palace")

            mine_convos(tmpdir, palace_path, wing="test")
            out = capsys.readouterr().out

            client = chromadb.PersistentClient(path=palace_path)
            col = client.get_collection("mempalace_drawers")
            result = col.get(include=["metadatas"])
            source_files = {m.get("source_file") for m in result["metadatas"] if m}
            # Only ONE of the two identical files' drawers actually got
            # filed -- whichever was mined second had its near-certain
            # duplicate dropped against the first.
            assert len(source_files) == 1
            assert "Possible duplicates skipped: 1" in out
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_flag_only_keeps_both_when_drop_disabled(self, monkeypatch, capsys):
        """Same identical-content setup, but with drop NOT enabled --
        both files' drawers must survive (flagging never removes
        content), confirming the drop behavior is gated by its own
        opt-in and not an accidental side effect of flagging alone."""
        monkeypatch.setenv("MEMPALACE_DUPLICATE_DETECTION", "true")
        monkeypatch.delenv("MEMPALACE_DUPLICATE_DROP", raising=False)
        tmpdir = tempfile.mkdtemp()
        try:
            (Path(tmpdir) / "a.txt").write_text(self._SHARED_TEXT)
            (Path(tmpdir) / "b.txt").write_text(self._SHARED_TEXT)
            palace_path = os.path.join(tmpdir, "palace")

            mine_convos(tmpdir, palace_path, wing="test")
            out = capsys.readouterr().out

            client = chromadb.PersistentClient(path=palace_path)
            col = client.get_collection("mempalace_drawers")
            result = col.get(include=["metadatas"])
            source_files = {m.get("source_file") for m in result["metadatas"] if m}
            assert len(source_files) == 2
            assert "Possible duplicates skipped: 0" in out
            flagged = [m for m in result["metadatas"] if m and m.get("possible_duplicate_of")]
            assert len(flagged) == 1
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


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


# ── per-project wing segregation for multi-project sweeps ───────────────
#
# Mining a directory containing sessions from more than one underlying
# project (most commonly the whole ~/.claude/projects) used to put every
# session into the same wing_api bucket regardless of which project it
# actually came from. _resolve_wing_for_file resolves each Claude Code /
# Codex session's real project identity from its own cwd, splitting a
# multi-project sweep into one wing per project instead.


def _write_claude_code_session(path: Path, cwd: str, topic: str = "the deployment plan") -> None:
    import json as jsonlib

    lines = [
        jsonlib.dumps({"type": "human", "cwd": cwd, "message": {"content": f"What is {topic}?"}}),
        jsonlib.dumps(
            {
                "type": "assistant",
                "cwd": cwd,
                "message": {
                    "content": f"Here is a detailed explanation of {topic} with enough words to clear the minimum chunk size floor comfortably."
                },
            }
        ),
    ]
    path.write_text("\n".join(lines) + "\n")


class TestMultiProjectWingSegregation:
    def test_segregates_by_project_with_no_explicit_wing(self, capsys):
        tmpdir = tempfile.mkdtemp()
        try:
            projects_root = Path(tmpdir) / ".claude" / "projects"
            proj_a = projects_root / "-Users-x-Code-alpha-repo"
            proj_b = projects_root / "-Users-x-Code-beta-repo"
            proj_a.mkdir(parents=True)
            proj_b.mkdir(parents=True)
            _write_claude_code_session(
                proj_a / "session.jsonl",
                "/Users/x/Code/alpha-repo",
                topic="alpha's release process",
            )
            _write_claude_code_session(
                proj_b / "session.jsonl", "/Users/x/Code/beta-repo", topic="beta's test suite"
            )
            palace_path = os.path.join(tmpdir, "palace")

            mine_convos(str(projects_root), palace_path)
            out = capsys.readouterr().out
            assert "alpha_repo" in out
            assert "beta_repo" in out

            client = chromadb.PersistentClient(path=palace_path)
            col = client.get_collection("mempalace_drawers")
            result = col.get(include=["metadatas"])
            wings_by_source = {
                m["source_file"]: m["wing"]
                for m in result["metadatas"]
                if m and m.get("room") != "_registry"
            }
            assert wings_by_source[str((proj_a / "session.jsonl").resolve())] == "alpha_repo"
            assert wings_by_source[str((proj_b / "session.jsonl").resolve())] == "beta_repo"
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_worktree_sessions_of_same_project_share_one_wing(self, capsys):
        """Two sessions from different worktrees of the SAME repo must
        land in the SAME wing, not be split by the ephemeral worktree
        name -- the whole point of _project_name_from_cwd's
        worktree-collapsing."""
        tmpdir = tempfile.mkdtemp()
        try:
            projects_root = Path(tmpdir) / ".claude" / "projects"
            proj_main = projects_root / "-Users-x-Code-gamma-repo"
            proj_wt = projects_root / "-Users-x-Code-gamma-repo--claude-worktrees-some-wt"
            proj_main.mkdir(parents=True)
            proj_wt.mkdir(parents=True)
            _write_claude_code_session(
                proj_main / "session.jsonl",
                "/Users/x/Code/gamma-repo",
                topic="the main branch work",
            )
            _write_claude_code_session(
                proj_wt / "session.jsonl",
                "/Users/x/Code/gamma-repo/.claude/worktrees/some-wt",
                topic="the worktree branch work",
            )
            palace_path = os.path.join(tmpdir, "palace")

            mine_convos(str(projects_root), palace_path)

            client = chromadb.PersistentClient(path=palace_path)
            col = client.get_collection("mempalace_drawers")
            result = col.get(include=["metadatas"])
            wings = {m["wing"] for m in result["metadatas"] if m and m.get("room") != "_registry"}
            assert wings == {"gamma_repo"}
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_malformed_session_falls_back_to_wing_api(self):
        """A session with no readable cwd (malformed/empty JSONL) must
        fall back to wing_api, not crash the whole sweep."""
        tmpdir = tempfile.mkdtemp()
        try:
            projects_root = Path(tmpdir) / ".claude" / "projects"
            proj = projects_root / "-Users-x-Code-bad-session"
            proj.mkdir(parents=True)
            (proj / "broken.jsonl").write_text("not valid json at all\nmore garbage\n")
            palace_path = os.path.join(tmpdir, "palace")

            # Must not raise.
            mine_convos(str(projects_root), palace_path)

            client = chromadb.PersistentClient(path=palace_path)
            col = client.get_collection("mempalace_drawers")
            result = col.get(include=["metadatas"])
            assert all(m.get("wing") == "wing_api" for m in result["metadatas"] if m)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_explicit_wing_overrides_per_file_detection_for_every_file(self, capsys):
        """Explicit --wing (even literally "wing_api") must win for every
        file, unconditionally -- must never be silently replaced by
        per-file detection just because it matches the AI-tool-path
        sentinel default (the exact bug in the abandoned upstream attempt
        at this feature, MemPalace/mempalace#1757)."""
        tmpdir = tempfile.mkdtemp()
        try:
            projects_root = Path(tmpdir) / ".claude" / "projects"
            proj_a = projects_root / "-Users-x-Code-alpha-repo"
            proj_b = projects_root / "-Users-x-Code-beta-repo"
            proj_a.mkdir(parents=True)
            proj_b.mkdir(parents=True)
            _write_claude_code_session(proj_a / "session.jsonl", "/Users/x/Code/alpha-repo")
            _write_claude_code_session(proj_b / "session.jsonl", "/Users/x/Code/beta-repo")
            palace_path = os.path.join(tmpdir, "palace")

            mine_convos(str(projects_root), palace_path, wing="wing_api")

            client = chromadb.PersistentClient(path=palace_path)
            col = client.get_collection("mempalace_drawers")
            result = col.get(include=["metadatas"])
            wings = {m["wing"] for m in result["metadatas"] if m and m.get("room") != "_registry"}
            assert wings == {"wing_api"}
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_one_time_backfill_reclassifies_already_mined_content(self):
        """Content mined before this feature existed (stamped without
        wing_resolution_version, everything collapsed under wing_api)
        must get reclassified into its correct per-project wing on the
        VERY NEXT mine -- even though the underlying file's content and
        mtime are completely unchanged. This is the one-time backfill:
        prefetch_mined_set's min_wing_resolution_version check excludes
        such a file from the "already mined" set, forcing a full re-mine
        that resolves and stamps its correct wing."""
        tmpdir = tempfile.mkdtemp()
        try:
            projects_root = Path(tmpdir) / ".claude" / "projects"
            proj = projects_root / "-Users-x-Code-delta-repo"
            proj.mkdir(parents=True)
            session = proj / "session.jsonl"
            _write_claude_code_session(session, "/Users/x/Code/delta-repo")
            palace_path = os.path.join(tmpdir, "palace")

            # Simulate a PRE-EXISTING mine from before this feature: mine
            # normally, then strip wing_resolution_version from every
            # drawer and force the wing back to wing_api, as if it had
            # been filed under the old single-wing-per-sweep behavior.
            mine_convos(str(projects_root), palace_path)

            client = chromadb.PersistentClient(path=palace_path)
            col = client.get_collection("mempalace_drawers")
            existing = col.get(include=["metadatas", "documents"])
            downgraded_metas = []
            for meta in existing["metadatas"]:
                meta = dict(meta or {})
                meta.pop("wing_resolution_version", None)
                meta["wing"] = "wing_api"
                downgraded_metas.append(meta)
            # col.update() merges metadata rather than replacing it --
            # a field simply absent from the new dict silently keeps its
            # OLD stored value, which would defeat this exact simulation
            # (removing wing_resolution_version). delete + fresh upsert
            # is the only way to actually drop a field.
            col.delete(ids=existing["ids"])
            col.upsert(
                ids=existing["ids"],
                documents=existing["documents"],
                metadatas=downgraded_metas,
            )
            del col, client

            # Re-mine the SAME, unchanged directory -- content and mtime
            # are identical to the first pass.
            mine_convos(str(projects_root), palace_path)

            client2 = chromadb.PersistentClient(path=palace_path)
            col2 = client2.get_collection("mempalace_drawers")
            result = col2.get(include=["metadatas"])
            wings = {m["wing"] for m in result["metadatas"] if m and m.get("room") != "_registry"}
            assert wings == {"delta_repo"}, (
                f"expected the backfill to reclassify into delta_repo, got {wings}"
            )
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
