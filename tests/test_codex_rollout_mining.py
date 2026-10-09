"""Codex rollouts file into their project's wing, and a Codex parser revision
re-mines rollouts already filed by an older parser (#2470)."""

import json
import os

import chromadb

from mempalace.convo_miner import _codex_project_wing, mine_convos
from mempalace.palace import (
    CODEX_NORMALIZE_VERSION,
    _meta_is_current,
    is_codex_rollout_source,
)

ROLLOUT = "rollout-2026-01-15T09-30-00-0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0.jsonl"


def _write_rollout(path, cwd, turns=(("What is memory?", "Memory is persistence."),)):
    rows = [
        {
            "timestamp": "2026-01-15T09:30:00.000Z",
            "type": "session_meta",
            "payload": {"id": "synthetic-session", "cwd": cwd},
        }
    ]
    for user, agent in turns:
        rows.append({"type": "event_msg", "payload": {"type": "user_message", "message": user}})
        rows.append({"type": "event_msg", "payload": {"type": "agent_message", "message": agent}})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    return path


def _sessions_dir(tmp_path):
    return tmp_path / ".codex" / "sessions" / "2026" / "01" / "15"


def _rows(palace_path):
    col = chromadb.PersistentClient(path=palace_path).get_collection("mempalace_drawers")
    got = col.get(include=["metadatas"])
    return [m for m in got["metadatas"] if m.get("ingest_mode") == "convos"]


# ── is_codex_rollout_source ──────────────────────────────────────────────────


def test_rollout_source_matches_basename_on_any_path():
    assert is_codex_rollout_source(f"/home/u/.codex/sessions/2026/01/15/{ROLLOUT}")
    assert is_codex_rollout_source(f"C:\\Users\\u\\.codex\\archived_sessions\\{ROLLOUT}")
    assert is_codex_rollout_source(f"/backup/{ROLLOUT}")


def test_rollout_source_rejects_other_transcripts():
    assert not is_codex_rollout_source("/home/u/.claude/projects/-x/abc.jsonl")
    assert not is_codex_rollout_source("/home/u/.codex/session_index.jsonl")
    assert not is_codex_rollout_source("/home/u/rollout-notes.md")
    assert not is_codex_rollout_source(None)


# ── _meta_is_current ─────────────────────────────────────────────────────────


def _current_meta(source_file, **extra):
    from mempalace.palace import CONVO_CHUNKER_VERSION, NORMALIZE_VERSION

    meta = {
        "source_file": source_file,
        "normalize_version": NORMALIZE_VERSION,
        "convo_chunker_version": CONVO_CHUNKER_VERSION,
    }
    meta.update(extra)
    return meta


def test_unstamped_codex_row_is_stale_in_convo_scopes():
    meta = _current_meta(f"/x/{ROLLOUT}")
    assert not _meta_is_current(meta, "exchange")
    assert not _meta_is_current(meta, "general")
    stamped = _current_meta(f"/x/{ROLLOUT}", codex_normalize_version=CODEX_NORMALIZE_VERSION)
    assert _meta_is_current(stamped, "exchange")


def test_codex_revision_does_not_touch_other_sources_or_project_scope():
    assert _meta_is_current(_current_meta("/x/.claude/projects/-a/s.jsonl"), "exchange")
    # Project mining (extract_mode None) never consults the Codex revision.
    assert _meta_is_current(_current_meta(f"/x/{ROLLOUT}"), None)


# ── _codex_project_wing ──────────────────────────────────────────────────────


def test_project_wing_from_session_meta_cwd(tmp_path):
    f = _write_rollout(_sessions_dir(tmp_path) / ROLLOUT, "/home/u/dev/acme-site")
    assert _codex_project_wing(f, set()) == "acme_site"


def test_project_wing_handles_windows_cwd(tmp_path):
    f = _write_rollout(_sessions_dir(tmp_path) / ROLLOUT, "C:\\Users\\u\\Projects\\example.io")
    assert _codex_project_wing(f, set()) == "example.io"


def test_project_wing_reuses_existing_wing_like_wings_split(tmp_path):
    f = _write_rollout(_sessions_dir(tmp_path) / ROLLOUT, "/home/u/dev/acme-portal")
    assert _codex_project_wing(f, {"portal"}) == "portal"


def test_project_wing_none_for_home_dir_and_missing_cwd(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    f = _write_rollout(_sessions_dir(tmp_path) / ROLLOUT, str(tmp_path))
    assert _codex_project_wing(f, set()) is None
    g = _sessions_dir(tmp_path) / ROLLOUT.replace("0f1e", "ffff")
    g.write_text(json.dumps({"type": "session_meta", "payload": {"id": "x"}}), encoding="utf-8")
    assert _codex_project_wing(g, set()) is None


def test_project_wing_none_for_codex_desktop_scratch_folder(tmp_path):
    """A Codex Desktop chat without a project runs in Documents/Codex/<date>/<slug>."""
    f = _write_rollout(
        _sessions_dir(tmp_path) / ROLLOUT, "C:\\Users\\u\\Documents\\Codex\\2026-01-15\\new-chat"
    )
    assert _codex_project_wing(f, set()) is None


def test_wings_split_agrees_on_scratch_folder(tmp_path):
    from mempalace.wing_split import project_key

    f = _write_rollout(_sessions_dir(tmp_path) / ROLLOUT, "/Users/u/Documents/Codex/2026-01-15/x")
    assert project_key(str(f)) is None
    g = _write_rollout(_sessions_dir(tmp_path) / "rollout-b.jsonl", "/Users/u/dev/acme-app")
    assert project_key(str(g)) == "acme-app"


def test_project_wing_none_for_non_rollout(tmp_path):
    f = _write_rollout(tmp_path / "chat.jsonl", "/home/u/dev/proj")
    assert _codex_project_wing(f, set()) is None


# ── mine_convos ──────────────────────────────────────────────────────────────


def test_mine_files_codex_rollouts_per_project(tmp_path):
    day = _sessions_dir(tmp_path)
    _write_rollout(day / ROLLOUT, "/home/u/dev/alpha")
    _write_rollout(day / ROLLOUT.replace("0f1e", "1a2b"), "/home/u/dev/beta")
    palace_path = str(tmp_path / "palace")

    mine_convos(str(tmp_path / ".codex"), palace_path)

    rows = _rows(palace_path)
    assert {m["wing"] for m in rows} == {"alpha", "beta"}
    assert all(m["codex_normalize_version"] == CODEX_NORMALIZE_VERSION for m in rows)


def test_mine_reuses_wing_already_in_palace(tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "chat.txt").write_text(
        "> What is the portal?\nThe customer portal.\n\n> Who owns it?\nThe web team.\n",
        encoding="utf-8",
    )
    palace_path = str(tmp_path / "palace")
    mine_convos(str(notes), palace_path, wing="portal")
    _write_rollout(_sessions_dir(tmp_path) / ROLLOUT, "/home/u/dev/acme-portal")

    mine_convos(str(tmp_path / ".codex"), palace_path)

    codex_rows = [m for m in _rows(palace_path) if ROLLOUT in m["source_file"]]
    assert codex_rows and {m["wing"] for m in codex_rows} == {"portal"}


def test_dry_run_routes_without_creating_palace(tmp_path, capsys):
    _write_rollout(_sessions_dir(tmp_path) / ROLLOUT, "/home/u/dev/alpha")
    palace_path = tmp_path / "palace"

    mine_convos(str(tmp_path / ".codex"), str(palace_path), dry_run=True)

    assert not palace_path.exists()
    assert "drawers)  -> alpha" in capsys.readouterr().out


def test_explicit_wing_overrides_codex_project_routing(tmp_path):
    _write_rollout(_sessions_dir(tmp_path) / ROLLOUT, "/home/u/dev/alpha")
    palace_path = str(tmp_path / "palace")

    mine_convos(str(tmp_path / ".codex"), palace_path, wing="codex_all")

    assert {m["wing"] for m in _rows(palace_path)} == {"codex_all"}


def test_rollout_without_cwd_falls_back_to_wing_api(tmp_path):
    f = _sessions_dir(tmp_path) / ROLLOUT
    _write_rollout(f, "/ignored")
    lines = f.read_text(encoding="utf-8").splitlines()
    lines[0] = json.dumps({"type": "session_meta", "payload": {"id": "no-cwd"}})
    f.write_text("\n".join(lines), encoding="utf-8")
    palace_path = str(tmp_path / "palace")

    mine_convos(str(tmp_path / ".codex"), palace_path)

    assert {m["wing"] for m in _rows(palace_path)} == {"wing_api"}


def _strip_codex_revision(palace_path):
    """Make every convo row look like it was filed before the revision existed."""
    col = chromadb.PersistentClient(path=palace_path).get_collection("mempalace_drawers")
    got = col.get(include=["metadatas", "documents", "embeddings"])
    metas = []
    for meta in got["metadatas"]:
        meta = dict(meta)
        meta.pop("codex_normalize_version", None)
        meta["wing"] = "wing_api"
        metas.append(meta)
    # ``update`` merges metadata, so dropping a key needs delete + add.
    col.delete(ids=got["ids"])
    col.add(
        ids=got["ids"],
        documents=got["documents"],
        embeddings=got["embeddings"],
        metadatas=metas,
    )


def test_unchanged_rollout_from_older_parser_is_remined_once(tmp_path):
    f = _write_rollout(_sessions_dir(tmp_path) / ROLLOUT, "/home/u/dev/alpha")
    palace_path = str(tmp_path / "palace")
    mine_convos(str(tmp_path / ".codex"), palace_path)
    before = len(_rows(palace_path))
    mtime = os.path.getmtime(f)

    # Simulate a palace filed by the previous release: same file, no stamp,
    # everything in wing_api.
    _strip_codex_revision(palace_path)

    mine_convos(str(tmp_path / ".codex"), palace_path)
    assert os.path.getmtime(f) == mtime  # repair never touches the source
    rows = _rows(palace_path)
    assert len(rows) == before  # old rows replaced, not duplicated
    assert {m["wing"] for m in rows} == {"alpha"}
    assert all(m["codex_normalize_version"] == CODEX_NORMALIZE_VERSION for m in rows)

    # The next ordinary mine does no work.
    snapshot = sorted((m["chunk_index"], m["filed_at"]) for m in rows)
    mine_convos(str(tmp_path / ".codex"), palace_path)
    assert sorted((m["chunk_index"], m["filed_at"]) for m in _rows(palace_path)) == snapshot


def test_unparseable_rollout_keeps_old_rows_and_stays_retryable(tmp_path):
    f = _write_rollout(_sessions_dir(tmp_path) / ROLLOUT, "/home/u/dev/alpha")
    palace_path = str(tmp_path / "palace")
    mine_convos(str(tmp_path / ".codex"), palace_path)
    _strip_codex_revision(palace_path)
    old = _rows(palace_path)

    # The rollout changes to a shape this parser can't read (a future schema).
    stat = os.stat(f)
    f.write_text(
        "\n".join(
            json.dumps(r)
            for r in (
                {"type": "session_meta", "payload": {"id": "s", "cwd": "/home/u/dev/alpha"}},
                {"type": "event_msg", "payload": {"type": "future_conversation_format"}},
            )
        ),
        encoding="utf-8",
    )
    os.utime(f, (stat.st_atime, stat.st_mtime))

    mine_convos(str(tmp_path / ".codex"), palace_path)

    rows = _rows(palace_path)
    assert len(rows) == len(old)  # nothing purged
    assert all("codex_normalize_version" not in m for m in rows)  # still stale → retried
