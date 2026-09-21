import json
import os
import json
import sqlite3
import tempfile
import shutil
import time
from pathlib import Path

import chromadb
import pytest

from mempalace.convo_miner import (
    _ai_tool_default_wing,
    chunk_exchanges,
    _is_ai_tool_path,
    _register_file,
    _resolve_wing,
    mine_convos,
    scan_convos,
)
from mempalace.palace import (
    NORMALIZE_VERSION,
    MineAlreadyRunning,
    file_already_mined,
    prefetch_mined_set,
)


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


def test_mine_convos_skips_same_content_under_new_filename(capsys):
    """Re-exporting the same conversation from Claude/ChatGPT under a new
    filename (fresh export bundle, regenerated slug, etc.) must not create
    a duplicate set of drawers -- only the exact-new content should file."""
    tmpdir = tempfile.mkdtemp()
    try:
        transcript = (
            "> What is the plan?\nStart with the schema, then the API.\n\n"
            "> Any risks?\nMigration ordering is the main one.\n"
        )
        (Path(tmpdir) / "export_2026-01-01.txt").write_text(transcript)
        palace_path = os.path.join(tmpdir, "palace")
        mine_convos(tmpdir, palace_path, wing="test")

        client = chromadb.PersistentClient(path=palace_path)
        col = client.get_collection("mempalace_drawers")
        count_after_first = col.count()
        assert count_after_first >= 2

        # Simulate a later export: the same conversation lands under a new
        # filename, alongside one genuinely new conversation.
        (Path(tmpdir) / "export_2026-02-01.txt").write_text(transcript)
        (Path(tmpdir) / "export_2026-02-01_new.txt").write_text(
            "> What's next?\nUNIQUE_SECOND_EXPORT_MARKER covers the new work.\n"
        )
        mine_convos(tmpdir, palace_path, wing="test")
        out = capsys.readouterr().out
        assert "duplicate of export_2026-01-01.txt" in out

        col = client.get_collection("mempalace_drawers")
        docs = col.get(include=["documents"])["documents"]
        dup_hits = sum(1 for d in docs if "Migration ordering is the main one" in d)
        assert dup_hits == 1, f"duplicate transcript re-filed: {dup_hits} copies"
        assert any("UNIQUE_SECOND_EXPORT_MARKER" in d for d in docs)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _privacy_export_bundle(conversations):
    """Build a Claude.ai privacy-export-shaped JSON payload: an array of
    conversation objects, each with its own chat_messages list."""
    return [
        {
            "chat_messages": [
                {"sender": "human", "text": turn}
                if i % 2 == 0
                else {"sender": "assistant", "text": turn}
                for i, turn in enumerate(turns)
            ]
        }
        for turns in conversations
    ]


def test_mine_convos_skips_same_conversation_within_re_exported_bundle(capsys):
    """A Claude.ai privacy export bundles every conversation into one JSON
    file. Re-exporting that bundle under a new filename with one additional
    conversation must not re-file the conversations that didn't change --
    hashing the whole bundle would change the file-level hash the moment
    any conversation is added, hiding the ones that are still duplicates.
    """
    tmpdir = tempfile.mkdtemp()
    try:
        convo_a = ["What is the plan?", "CONVO_A_MARKER: start with the schema."]
        convo_b = ["Any risks?", "CONVO_B_MARKER: migration ordering is the main one."]
        convo_c = ["What's next?", "CONVO_C_MARKER: covers the new work."]

        bundle1 = _privacy_export_bundle([convo_a, convo_b])
        (Path(tmpdir) / "export_2026-01-01.json").write_text(json.dumps(bundle1))

        palace_path = os.path.join(tmpdir, "palace")
        mine_convos(tmpdir, palace_path, wing="test")

        client = chromadb.PersistentClient(path=palace_path)
        col = client.get_collection("mempalace_drawers")
        docs_after_first = col.get(include=["documents"])["documents"]
        assert any("CONVO_A_MARKER" in d for d in docs_after_first)
        assert any("CONVO_B_MARKER" in d for d in docs_after_first)

        # Re-export: same two conversations plus one genuinely new one, all
        # under a fresh filename (as a real re-export from Claude would do).
        bundle2 = _privacy_export_bundle([convo_a, convo_b, convo_c])
        (Path(tmpdir) / "export_2026-02-01.json").write_text(json.dumps(bundle2))

        mine_convos(tmpdir, palace_path, wing="test")

        col = client.get_collection("mempalace_drawers")
        docs = col.get(include=["documents"])["documents"]
        a_hits = sum(1 for d in docs if "CONVO_A_MARKER" in d)
        b_hits = sum(1 for d in docs if "CONVO_B_MARKER" in d)
        c_hits = sum(1 for d in docs if "CONVO_C_MARKER" in d)
        assert a_hits == 1, f"conversation A re-filed from the updated bundle: {a_hits} copies"
        assert b_hits == 1, f"conversation B re-filed from the updated bundle: {b_hits} copies"
        assert c_hits >= 1, "new conversation C was not filed at all"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_content_dedup_is_scoped_per_wing():
    """Mining the same transcript content into a second wing must file real
    drawers there, not just the registry sentinel -- the content-hash map
    is a dedup signal within a wing, not a cross-wing "already have this
    content anywhere" gate.
    """
    tmpdir = tempfile.mkdtemp()
    try:
        transcript = (
            "> What is the plan?\nStart with the schema, then the API.\n\n"
            "> Any risks?\nMigration ordering is the main one.\n"
        )
        dir_a = Path(tmpdir) / "wing_a_src"
        dir_b = Path(tmpdir) / "wing_b_src"
        dir_a.mkdir()
        dir_b.mkdir()
        (dir_a / "session.txt").write_text(transcript)
        (dir_b / "session.txt").write_text(transcript)

        palace_path = os.path.join(tmpdir, "palace")
        mine_convos(str(dir_a), palace_path, wing="wing_a")
        mine_convos(str(dir_b), palace_path, wing="wing_b")

        client = chromadb.PersistentClient(path=palace_path)
        col = client.get_collection("mempalace_drawers")
        wing_b_docs = col.get(where={"wing": "wing_b"}, include=["documents", "metadatas"])
        real_drawers = [
            d
            for d, m in zip(wing_b_docs["documents"], wing_b_docs["metadatas"])
            if m.get("room") != "_registry"
        ]
        assert real_drawers, (
            "wing_b holds only the registry sentinel -- content dedup leaked across wings"
        )
        assert any("Migration ordering is the main one" in d for d in real_drawers)
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


def test_prefetch_mined_set_omits_incomplete_chunk_total_group():
    """Mid-file partials with chunk_total must not bulk-skip the source (#2183)."""
    tmpdir = tempfile.mkdtemp()
    try:
        palace_path = os.path.join(tmpdir, "palace")
        client = chromadb.PersistentClient(path=palace_path)
        col = client.get_or_create_collection("mempalace_drawers")
        mtime = 1_700_000_000.0
        source = "/fake/session.jsonl"
        # Only 2 of 3 expected chunks landed before a crash.
        col.upsert(
            ids=["d0", "d1"],
            documents=["chunk 0", "chunk 1"],
            metadatas=[
                {
                    "wing": "test",
                    "room": "general",
                    "source_file": source,
                    "chunk_index": 0,
                    "extract_mode": "exchange",
                    "normalize_version": NORMALIZE_VERSION,
                    "source_mtime": mtime,
                    "chunk_total": 3,
                },
                {
                    "wing": "test",
                    "room": "general",
                    "source_file": source,
                    "chunk_index": 1,
                    "extract_mode": "exchange",
                    "normalize_version": NORMALIZE_VERSION,
                    "source_mtime": mtime,
                    "chunk_total": 3,
                },
            ],
        )
        mined = prefetch_mined_set(col, extract_mode="exchange")
        assert source not in mined, (
            "prefetch_mined_set treated 2/3 chunks as fully filed — the bulk "
            "skip path would permanently strand the missing exchange (#2183)"
        )

        col.upsert(
            ids=["d2"],
            documents=["chunk 2"],
            metadatas=[
                {
                    "wing": "test",
                    "room": "general",
                    "source_file": source,
                    "chunk_index": 2,
                    "extract_mode": "exchange",
                    "normalize_version": NORMALIZE_VERSION,
                    "source_mtime": mtime,
                    "chunk_total": 3,
                }
            ],
        )
        mined = prefetch_mined_set(col, extract_mode="exchange")
        assert source in mined
        assert abs(mined[source] - mtime) < 0.001
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


# ---------------------------------------------------------------------------
# file_conversation_exchange — canonical single-exchange write path
# ---------------------------------------------------------------------------


class _RecordingCollection:
    """Captures upsert kwargs without a real ChromaDB behind it."""

    def __init__(self):
        self.upserts = []

    def upsert(self, *, ids, documents, metadatas):
        self.upserts.append({"ids": ids, "documents": documents, "metadatas": metadatas})


def _exchange_kwargs(**overrides):
    kwargs = {
        "wing": "wing_dev",
        "room": "conversations",
        "text": "User: hi\n\nAssistant: hello",
        "source_file": "hermes-session:s1",
        "agent": "hermes",
    }
    kwargs.update(overrides)
    return kwargs


def test_file_conversation_exchange_extra_metadata_cannot_clobber_canonical():
    """The docstring promises extras are append-only — colliding keys lose.

    PR #1915 review: ``metadata.update(extra_metadata)`` let a caller
    silently overwrite ``wing`` / ``filed_at`` / etc.
    """
    from mempalace.convo_miner import file_conversation_exchange

    col = _RecordingCollection()
    file_conversation_exchange(
        col,
        **_exchange_kwargs(),
        extra_metadata={"wing": "wing_evil", "filed_at": "1970-01-01", "source": "hermes"},
    )
    meta = col.upserts[0]["metadatas"][0]
    assert meta["wing"] == "wing_dev"
    assert meta["filed_at"] != "1970-01-01"
    # Non-colliding extras still land.
    assert meta["source"] == "hermes"


def test_file_conversation_exchange_invalid_wing_falls_back_to_wing_general():
    """A bad configured wing must not drop the turn — verbatim first.

    Same validation the MCP write tools apply (sanitize_name), but with a
    wing_general fallback instead of an error: live filing losing turns
    over a config typo would violate the 100%-recall promise.
    """
    from mempalace.convo_miner import file_conversation_exchange

    col = _RecordingCollection()
    file_conversation_exchange(col, **_exchange_kwargs(wing="../escape"))
    meta = col.upserts[0]["metadatas"][0]
    assert meta["wing"] == "wing_general"
    assert col.upserts[0]["documents"] == ["User: hi\n\nAssistant: hello"]


def test_file_conversation_exchange_invalid_room_falls_back_to_conversations():
    from mempalace.convo_miner import file_conversation_exchange

    col = _RecordingCollection()
    file_conversation_exchange(col, **_exchange_kwargs(room="a/b"))
    meta = col.upserts[0]["metadatas"][0]
    assert meta["room"] == "conversations"


def _write_dry_run_transcript(path: Path) -> None:
    path.write_text(
        "> What is the plan?\n"
        "Start with the schema, then the API.\n\n"
        "> Are there any risks?\n"
        "Migration ordering is the main one.\n\n"
        "> What comes next?\n"
        "Run focused tests before the full suite.\n",
        encoding="utf-8",
    )


def test_mine_convos_dry_run_skips_unchanged_mined_file(
    tmp_path,
    capsys,
    monkeypatch,
):
    monkeypatch.setenv("HOME", str(tmp_path))

    convo_dir = tmp_path / "convos"
    convo_dir.mkdir()
    transcript = convo_dir / "session.txt"
    _write_dry_run_transcript(transcript)
    palace_path = str(tmp_path / "palace")

    mine_convos(
        str(convo_dir),
        palace_path,
        wing="original",
    )
    capsys.readouterr()

    mine_convos(
        str(convo_dir),
        palace_path,
        wing="target",
        dry_run=True,
    )
    output = capsys.readouterr().out

    assert "[DRY RUN] session.txt" not in output
    assert "Files processed: 0" in output
    assert "Files skipped (already filed): 1" in output
    assert "Drawers filed: 0" in output


def test_mine_convos_dry_run_keeps_modified_file_as_work(
    tmp_path,
    capsys,
    monkeypatch,
):
    monkeypatch.setenv("HOME", str(tmp_path))

    convo_dir = tmp_path / "convos"
    convo_dir.mkdir()
    transcript = convo_dir / "session.txt"
    _write_dry_run_transcript(transcript)
    palace_path = str(tmp_path / "palace")

    mine_convos(
        str(convo_dir),
        palace_path,
        wing="original",
    )
    capsys.readouterr()

    transcript.write_text(
        transcript.read_text(encoding="utf-8")
        + "\n> Did the plan change?\n"
        + "Yes, add a migration rollback test.\n",
        encoding="utf-8",
    )

    future = time.time() + 60
    os.utime(transcript, (future, future))

    mine_convos(
        str(convo_dir),
        palace_path,
        wing="target",
        dry_run=True,
    )
    output = capsys.readouterr().out

    assert "[DRY RUN] session.txt" in output
    assert "Files processed: 1" in output
    assert "Files skipped (already filed): 0" in output


def test_mine_convos_dry_run_missing_palace_does_not_create_it(
    tmp_path,
    capsys,
    monkeypatch,
):
    monkeypatch.setenv("HOME", str(tmp_path))

    convo_dir = tmp_path / "convos"
    convo_dir.mkdir()
    transcript = convo_dir / "session.txt"
    _write_dry_run_transcript(transcript)
    palace_path = tmp_path / "palace"

    mine_convos(
        str(convo_dir),
        str(palace_path),
        wing="target",
        dry_run=True,
    )
    output = capsys.readouterr().out

    assert "[DRY RUN] session.txt" in output
    assert "Files processed: 1" in output
    assert "Files skipped (already filed): 0" in output
    assert not palace_path.exists()


def test_mine_convos_dry_run_single_file_does_not_scan_siblings(
    tmp_path,
    capsys,
):
    selected = tmp_path / "selected.txt"
    sibling = tmp_path / "sibling.txt"

    selected.write_text(
        "> Which transcript should be mined?\n"
        "SELECTED_ONLY_MARKER belongs to the active transcript.\n\n"
        "> Should sibling files be included?\n"
        "No. Only the selected transcript should be scanned.\n",
        encoding="utf-8",
    )
    sibling.write_text(
        "> Should this sibling be mined?\n"
        "SIBLING_SHOULD_NOT_BE_MINED by the single-file invocation.\n\n"
        "> Is that important?\n"
        "Yes. It keeps hook-triggered mining narrowly scoped.\n",
        encoding="utf-8",
    )

    palace_path = tmp_path / "palace"

    mine_convos(
        str(selected),
        str(palace_path),
        wing="sessions",
        dry_run=True,
    )
    output = capsys.readouterr().out

    assert "Files:   1" in output
    assert "[DRY RUN] selected.txt" in output
    assert "sibling.txt" not in output
    assert not palace_path.exists()
