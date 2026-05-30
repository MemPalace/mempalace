import os
import tempfile
import shutil
from pathlib import Path

import chromadb
import pytest

from mempalace.convo_miner import (
    _is_ai_tool_path,
    _resolve_wing,
    mine_convos,
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


# ── _resolve_wing — wing_aliases.json collapsing on the CLI miner ──────
#
# When the user has ``~/.mempalace/wing_aliases.json`` configured and runs
# ``mempalace mine --mode convos ~/.claude/projects/-Users-me-joy-web-3/``
# without an explicit ``--wing``, the miner now routes through the same
# alias map the Stop/PreCompact hooks use. The basename comes from each
# session's authoritative ``cwd``, so working-copy clones (``joy-web``,
# ``joy-web-1``, ..., ``joy-web-5``) all collapse onto one wing — without
# the user having to hand-pass ``--wing`` per directory in a wrapper.
#
# Behavior contract:
#  - Alias hit → use the aliased wing.
#  - Alias miss → fall through to the existing ``wing_api`` default
#    (no surprise per-project wing splits for users without alias config).
#  - No transcript / no cwd / I/O error → fall through to ``wing_api``.


import json as _json  # noqa: E402

import mempalace.hooks_cli as _hooks_cli_mod  # noqa: E402


def _write_transcript_with_cwd(jsonl_path: Path, cwd: str) -> None:
    """Write a minimal Claude Code-shaped transcript containing one cwd record."""
    jsonl_path.write_text(
        _json.dumps({"type": "queue-operation", "operation": "enqueue"})
        + "\n"
        + _json.dumps({"type": "user", "cwd": cwd, "content": "hi"})
        + "\n",
        encoding="utf-8",
    )


def _install_aliases(tmp_path, monkeypatch, aliases_dict: dict) -> Path:
    """Wire up an isolated ``wing_aliases.json`` for one test."""
    palace_root = tmp_path / ".mempalace"
    palace_root.mkdir(exist_ok=True)
    (palace_root / "wing_aliases.json").write_text(_json.dumps({"aliases": aliases_dict}))
    monkeypatch.setattr(_hooks_cli_mod, "PALACE_ROOT", palace_root)
    monkeypatch.setattr(_hooks_cli_mod, "_WING_ALIASES_CACHE", {})
    return palace_root


def test_resolve_wing_alias_hit_collapses_clones(tmp_path, monkeypatch):
    """A ``joy-web*`` prefix alias collapses every clone onto one wing."""
    _install_aliases(tmp_path, monkeypatch, {"joy-web*": "wing_joy_web"})

    target = tmp_path / ".claude" / "projects" / "-Users-me-Claude-joy-web-3"
    target.mkdir(parents=True)
    _write_transcript_with_cwd(target / "s.jsonl", "/Users/me/Claude/joy-web-3")
    assert _resolve_wing(target, wing=None) == "wing_joy_web"

    target2 = tmp_path / ".claude" / "projects" / "-Users-me-Claude-joy-web"
    target2.mkdir(parents=True)
    _write_transcript_with_cwd(target2 / "s.jsonl", "/Users/me/Claude/joy-web")
    assert _resolve_wing(target2, wing=None) == "wing_joy_web"


def test_resolve_wing_alias_miss_falls_through_to_wing_api(tmp_path, monkeypatch):
    """No alias for this basename → keep the existing wing_api default.

    Critical: users without ``wing_aliases.json`` (or with one that doesn't
    name this project) MUST NOT see a behavior change. The cwd-derived
    basename here would slug to ``wing_random_project`` if we honored it
    unconditionally — and that would silently splinter their AI corpus
    across many wings.
    """
    _install_aliases(tmp_path, monkeypatch, {"joy-web*": "wing_joy_web"})

    target = tmp_path / ".claude" / "projects" / "-Users-me-Claude-random-project"
    target.mkdir(parents=True)
    _write_transcript_with_cwd(target / "s.jsonl", "/Users/me/Claude/random-project")
    assert _resolve_wing(target, wing=None) == "wing_api"


def test_resolve_wing_alias_exact_match(tmp_path, monkeypatch):
    """Exact (non-``*``) aliases also apply on the CLI miner path."""
    _install_aliases(tmp_path, monkeypatch, {"mempalace": "wing_mempalace"})

    target = tmp_path / ".claude" / "projects" / "-Users-me-Claude-mempalace"
    target.mkdir(parents=True)
    _write_transcript_with_cwd(target / "s.jsonl", "/Users/me/Claude/mempalace")
    assert _resolve_wing(target, wing=None) == "wing_mempalace"


def test_resolve_wing_explicit_wing_still_wins_over_alias(tmp_path, monkeypatch):
    """Step 1 of the precedence chain is still ``--wing`` — aliases never
    override an explicit user choice, even when both would match."""
    _install_aliases(tmp_path, monkeypatch, {"joy-web*": "wing_joy_web"})

    target = tmp_path / ".claude" / "projects" / "-Users-me-Claude-joy-web-3"
    target.mkdir(parents=True)
    _write_transcript_with_cwd(target / "s.jsonl", "/Users/me/Claude/joy-web-3")
    assert _resolve_wing(target, wing="user_chose_this") == "user_chose_this"


def test_resolve_wing_no_transcripts_keeps_wing_api(tmp_path, monkeypatch):
    """AI-tool path with no jsonl files yet → cannot derive cwd → wing_api."""
    _install_aliases(tmp_path, monkeypatch, {"joy-web*": "wing_joy_web"})

    target = tmp_path / ".claude" / "projects" / "-Users-me-Claude-joy-web-3"
    target.mkdir(parents=True)
    # No .jsonl files inside.
    assert _resolve_wing(target, wing=None) == "wing_api"


def test_resolve_wing_transcript_without_cwd_keeps_wing_api(tmp_path, monkeypatch):
    """A transcript that records no ``cwd`` (queue-only file, old format) →
    falls through cleanly to ``wing_api`` rather than crashing or guessing."""
    _install_aliases(tmp_path, monkeypatch, {"joy-web*": "wing_joy_web"})

    target = tmp_path / ".claude" / "projects" / "-Users-me-Claude-joy-web-3"
    target.mkdir(parents=True)
    (target / "s.jsonl").write_text(
        _json.dumps({"type": "queue-operation"})
        + "\n"
        + _json.dumps({"type": "ai-title", "aiTitle": "untitled"})
        + "\n",
        encoding="utf-8",
    )
    assert _resolve_wing(target, wing=None) == "wing_api"


def test_resolve_wing_malformed_transcript_keeps_wing_api(tmp_path, monkeypatch):
    """A jsonl with garbage lines must not crash wing resolution."""
    _install_aliases(tmp_path, monkeypatch, {"joy-web*": "wing_joy_web"})

    target = tmp_path / ".claude" / "projects" / "-Users-me-Claude-anywhere"
    target.mkdir(parents=True)
    (target / "s.jsonl").write_text("not json at all\n{broken,\n", encoding="utf-8")
    assert _resolve_wing(target, wing=None) == "wing_api"


def test_resolve_wing_alias_does_not_affect_non_ai_paths(tmp_path, monkeypatch):
    """Non-AI-tool corpora keep the existing basename behavior — aliases
    only intercept on AI-tool paths because that's where ``cwd`` reading
    makes sense (the path encoding is lossy on AI-tool dirs and lossless
    on regular dirs)."""
    _install_aliases(tmp_path, monkeypatch, {"joy-web*": "wing_joy_web"})

    target = tmp_path / "Documents" / "joy-web-3"
    target.mkdir(parents=True)
    _write_transcript_with_cwd(target / "s.jsonl", "/Users/me/Claude/joy-web-3")
    # Non-AI-tool path → step 4 (basename normalize), not alias-routed.
    assert _resolve_wing(target, wing=None) == "joy_web_3"
