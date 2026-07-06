import os
import json
import sqlite3
import tempfile
import shutil
from pathlib import Path

import chromadb
import pytest

from mempalace.convo_miner import (
    _ai_tool_default_wing,
    chunk_exchanges,
    _is_ai_tool_path,
    _project_wing_from_jsonl_cwd,
    _opencode_source_file,
    _resolve_opencode_db_path,
    _resolve_wing_for_file,
    _resolve_wing,
    mine_convos,
    scan_convos,
)
from mempalace.palace import MineAlreadyRunning, file_already_mined


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
        # v4 content-pure ids: wing/room live in metadata now, not the id.
        assert rows["ids"] and all(
            len(did.removeprefix("drawer_")) == 32
            and did.startswith("drawer_")
            and all(c in "0123456789abcdef" for c in did.removeprefix("drawer_"))
            for did in rows["ids"]
        )
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


def test_chunk_exchanges_prefers_word_boundaries_for_long_responses():
    content = "> What happened?\n" + ("alpha beta gamma delta epsilon\n" * 20)
    chunks = chunk_exchanges(content, chunk_size=80, min_chunk_size=0)

    assert len(chunks) > 1
    assert "".join(chunk["content"] for chunk in chunks) == content.rstrip("\n")
    assert all(
        not (chunk["content"][0].isalnum() and chunks[index - 1]["content"][-1].isalnum())
        for index, chunk in enumerate(chunks)
        if index > 0
    )


def test_chunk_exchanges_preserves_unbroken_long_tokens():
    long_token = "x" * 120
    content = f"> Token?\n{long_token}"
    chunks = chunk_exchanges(content, chunk_size=50, min_chunk_size=0)

    assert "".join(chunk["content"] for chunk in chunks) == content


def test_chunk_exchanges_prefers_punctuation_boundaries_for_long_paths():
    path_like = "/Users/me/.local/share/zsh/site-functions:/usr/share/zsh/functions:" * 4
    content = f"> Path?\n{path_like}"
    chunks = chunk_exchanges(content, chunk_size=80, min_chunk_size=0)

    assert len(chunks) > 1
    assert "".join(chunk["content"] for chunk in chunks) == content
    assert all(
        not (chunk["content"][0].isalnum() and chunks[index - 1]["content"][-1].isalnum())
        for index, chunk in enumerate(chunks)
        if index > 0
    )


def test_chunk_exchanges_paragraph_fallback_preserves_separators():
    content = (
        "First paragraph has enough content to spill over.\n\n"
        "Second paragraph must keep the blank line before it.\n"
        "- and lists\n"
        "- should retain their line breaks too\n"
    )
    chunks = chunk_exchanges(content, chunk_size=70, min_chunk_size=0)

    assert len(chunks) > 1
    assert "".join(chunk["content"] for chunk in chunks) == content.rstrip("\n")


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


def test_scan_convos_generic_export_keeps_text_markdown_and_json(tmp_path):
    for name in ("chat.txt", "chat.md", "chat.json", "chat.jsonl"):
        (tmp_path / name).write_text("> hello\nthere\n> again\nthere\n")

    found = {path.name for path in scan_convos(str(tmp_path))}

    assert found == {"chat.txt", "chat.md", "chat.json", "chat.jsonl"}


# ── _is_ai_tool_path / _resolve_wing — AI transcript auto-routing ──────
#
# When a user runs `mempalace mine --mode convos` against a directory
# inside a known AI-tool storage path (Claude Code's
# ~/.claude/projects/, OpenAI Codex's ~/.codex/, Google Gemini CLI's
# ~/.gemini/), the wing auto-defaults to a harness-specific conversations
# wing rather than the directory basename. This keeps backfilled agent
# histories easy to scan and avoids collapsing every tool into wing_api.
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


def test_scan_convos_claude_projects_excludes_sidecars(tmp_path):
    target = tmp_path / ".claude" / "projects" / "-Users-test-myapp"
    (target / "memory").mkdir(parents=True)
    (target / "tool-results").mkdir()
    (target / "workflows").mkdir()
    (target / "subagents" / "workflows" / "wf_123").mkdir(parents=True)
    (target / "session.jsonl").write_text("{}\n")
    (target / "memory" / "MEMORY.md").write_text("not a transcript")
    (target / "tool-results" / "tool.txt").write_text("tool output")
    (target / "workflows" / "wf.json").write_text("{}")
    (target / "subagents" / "workflows" / "wf_123" / "journal.jsonl").write_text("{}\n")

    found = [path.relative_to(target).as_posix() for path in scan_convos(str(target))]

    assert found == ["session.jsonl"]


def test_scan_convos_codex_sessions_only_accepts_jsonl(tmp_path):
    target = tmp_path / ".codex" / "sessions" / "2026" / "06" / "30"
    target.mkdir(parents=True)
    (target / "rollout.jsonl").write_text("{}\n")
    (target / "notes.md").write_text("sidecar")
    (target / "tool.txt").write_text("sidecar")
    (target / "config.json").write_text("{}")

    found = [path.name for path in scan_convos(str(target))]

    assert found == ["rollout.jsonl"]


def test_scan_convos_gemini_keeps_json_session_formats(tmp_path):
    target = tmp_path / ".gemini" / "sessions"
    target.mkdir(parents=True)
    (target / "session.jsonl").write_text("{}\n")
    (target / "session.json").write_text("{}")
    (target / "notes.md").write_text("sidecar")

    found = {path.name for path in scan_convos(str(target))}

    assert found == {"session.jsonl", "session.json"}


def test_resolve_wing_explicit_wins_over_auto_detection(tmp_path):
    """User-passed --wing always wins, even on an AI tool path."""
    target = tmp_path / ".claude" / "projects" / "-Users-x"
    target.mkdir(parents=True)
    assert _resolve_wing(target, wing="my_custom_wing") == "my_custom_wing"


def test_ai_tool_default_wing_routes_by_harness(tmp_path):
    claude = tmp_path / ".claude" / "projects" / "-Users-test-myapp"
    codex = tmp_path / ".codex" / "sessions" / "2026"
    gemini = tmp_path / ".gemini" / "tmp" / "abc" / "chats"
    unrelated = tmp_path / "Documents" / "myproject"
    for path in (claude, codex, gemini, unrelated):
        path.mkdir(parents=True)

    assert _ai_tool_default_wing(claude) == "claude_conversations"
    assert _ai_tool_default_wing(codex) == "codex_conversations"
    assert _ai_tool_default_wing(gemini) == "gemini_conversations"
    assert _ai_tool_default_wing(unrelated) is None


def test_resolve_wing_claude_projects_auto_routes_to_claude_conversations(tmp_path):
    target = tmp_path / ".claude" / "projects" / "-Users-test-myapp"
    target.mkdir(parents=True)
    assert _resolve_wing(target, wing=None) == "claude_conversations"


def test_resolve_wing_codex_auto_routes_to_codex_conversations(tmp_path):
    target = tmp_path / ".codex" / "sessions" / "2026"
    target.mkdir(parents=True)
    assert _resolve_wing(target, wing=None) == "codex_conversations"


def test_resolve_wing_gemini_auto_routes_to_gemini_conversations(tmp_path):
    target = tmp_path / ".gemini" / "tmp" / "abc" / "chats"
    target.mkdir(parents=True)
    assert _resolve_wing(target, wing=None) == "gemini_conversations"


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
    assert _resolve_wing(target, wing="") == "gemini_conversations"


def test_project_wing_from_jsonl_cwd_uses_project_name(tmp_path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        '{"type":"user","cwd":"/Users/me/dev/wormdb","content":"hello"}\n',
        encoding="utf-8",
    )

    assert _project_wing_from_jsonl_cwd(transcript) == "wormdb"


def _create_opencode_db(db_path: Path):
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            create table session (
                id text primary key,
                directory text not null,
                title text not null,
                version text not null,
                agent text,
                model text,
                time_created integer not null,
                time_updated integer not null
            );
            create table message (
                id text primary key,
                session_id text not null,
                time_created integer not null,
                time_updated integer not null,
                data text not null
            );
            create table part (
                id text primary key,
                message_id text not null,
                session_id text not null,
                time_created integer not null,
                time_updated integer not null,
                data text not null
            );
            """
        )
        conn.execute(
            """
            insert into session
                (id, directory, title, version, agent, model, time_created, time_updated)
            values (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "ses_test",
                "/Users/me/dev/WormDB",
                "OpenCode memory test",
                "1.17.12",
                "build",
                '{"id":"local"}',
                1782864000000,
                1782864004000,
            ),
        )
        rows = [
            (
                "msg_user",
                "ses_test",
                1782864001000,
                1782864001000,
                json.dumps({"role": "user"}),
            ),
            (
                "msg_assistant",
                "ses_test",
                1782864002000,
                1782864004000,
                json.dumps({"role": "assistant"}),
            ),
        ]
        conn.executemany(
            """
            insert into message (id, session_id, time_created, time_updated, data)
            values (?, ?, ?, ?, ?)
            """,
            rows,
        )
        parts = [
            (
                "prt_user",
                "msg_user",
                "ses_test",
                1782864001001,
                1782864001001,
                json.dumps(
                    {
                        "type": "text",
                        "text": "Remember the opencode comet canary for this project.",
                    }
                ),
            ),
            (
                "prt_reasoning",
                "msg_assistant",
                "ses_test",
                1782864002001,
                1782864002001,
                json.dumps(
                    {
                        "type": "reasoning",
                        "text": "SHOULD_NOT_APPEAR_REASONING",
                    }
                ),
            ),
            (
                "prt_tool",
                "msg_assistant",
                "ses_test",
                1782864003000,
                1782864003000,
                json.dumps(
                    {
                        "type": "tool",
                        "tool": "mempalace_mempalace_search",
                        "state": {"output": "SHOULD_NOT_APPEAR_TOOL_OUTPUT"},
                    }
                ),
            ),
            (
                "prt_assistant",
                "msg_assistant",
                "ses_test",
                1782864004000,
                1782864004000,
                json.dumps(
                    {
                        "type": "text",
                        "text": "The opencode comet canary is stored for WormDB.",
                    }
                ),
            ),
        ]
        conn.executemany(
            """
            insert into part (id, message_id, session_id, time_created, time_updated, data)
            values (?, ?, ?, ?, ?, ?)
            """,
            parts,
        )
        conn.commit()
    finally:
        conn.close()


def test_resolve_opencode_db_path_accepts_file_and_data_dir(tmp_path):
    data_dir = tmp_path / "opencode"
    data_dir.mkdir()
    db_path = data_dir / "opencode.db"
    _create_opencode_db(db_path)

    assert _resolve_opencode_db_path(db_path) == db_path
    assert _resolve_opencode_db_path(data_dir) == db_path


def test_mine_convos_opencode_db_uses_project_wing_and_skips_operational_parts(tmp_path, capsys):
    data_dir = tmp_path / "opencode"
    data_dir.mkdir()
    db_path = data_dir / "opencode.db"
    _create_opencode_db(db_path)
    palace_path = str(tmp_path / "palace")

    mine_convos(str(db_path), palace_path)
    out = capsys.readouterr().out
    assert "MemPalace Mine — OpenCode Conversations" in out

    client = chromadb.PersistentClient(path=palace_path)
    col = client.get_collection("mempalace_drawers")
    rows = col.get(where={"source_file": _opencode_source_file("ses_test")})
    assert rows["ids"], "OpenCode session should be filed under a synthetic session source"
    doc = "\n".join(rows["documents"])
    assert "Remember the opencode comet canary for this project." in doc
    assert "The opencode comet canary is stored for WormDB." in doc
    assert "SHOULD_NOT_APPEAR_REASONING" not in doc
    assert "SHOULD_NOT_APPEAR_TOOL_OUTPUT" not in doc
    assert all(meta["wing"] == "wormdb" for meta in rows["metadatas"])
    assert all(
        meta["source_file"] == _opencode_source_file("ses_test") for meta in rows["metadatas"]
    )
    assert all(meta["authored_at"] == "2026-07-01T00:00:04.000Z" for meta in rows["metadatas"])

    mine_convos(str(data_dir), palace_path)
    out2 = capsys.readouterr().out
    assert "Sessions skipped (already filed): 1" in out2
    del col, client


def test_resolve_wing_for_file_prefers_project_cwd_over_harness_wing(tmp_path):
    transcript = tmp_path / ".codex" / "sessions" / "2026" / "session.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        '{"type":"user","cwd":"/Users/me/dev/MemPalace/mempalace","content":"hello"}\n',
        encoding="utf-8",
    )

    assert _resolve_wing_for_file(transcript, "codex_conversations", None) == "mempalace"


def test_resolve_wing_for_file_explicit_wing_still_wins(tmp_path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        '{"type":"user","cwd":"/Users/me/dev/wormdb","content":"hello"}\n',
        encoding="utf-8",
    )

    assert _resolve_wing_for_file(transcript, "codex_conversations", "custom") == "custom"


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
