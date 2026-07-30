"""Tests for the #1222 HNSW capacity probe and BM25-only fallback.

The probe and fallback never load chromadb's HNSW segment, so all of
these tests synthesize the on-disk shape directly: a chroma.sqlite3 with
the relevant schema rows and an ``index_metadata.pickle`` matching what
chromadb 1.5.x writes (``{"id_to_label": {...}, ...}``).
"""

from __future__ import annotations

import os
import pickle
import shutil
import sqlite3
import time

import pytest

from mempalace.backends.chroma import (
    _hnsw_element_count,
    _vector_segment_id,
    hnsw_capacity_status,
    reset_hnsw_capacity_cache,
)
from mempalace.searcher import _bm25_only_via_sqlite


COLLECTION = "mempalace_drawers"


# ── Fixtures ──────────────────────────────────────────────────────────


def _seed_chroma_db(
    palace: str,
    sqlite_count: int,
    segment_id: str,
    sync_threshold: int | None = None,
) -> None:
    """Create a minimal chroma.sqlite3 with one collection + VECTOR segment.

    Mirrors the columns the probe queries: ``segments``, ``collections``,
    ``collection_metadata``, ``embeddings``, ``embedding_metadata``.
    Schema matches chromadb 1.5.x; column types are kept loose because
    we read with COUNT(*) and SELECT key, *_value rather than driver-
    specific casts.

    When ``sync_threshold`` is supplied, an ``hnsw:sync_threshold`` row
    is added to ``collection_metadata`` so the divergence floor scales
    accordingly. Omit to model an older palace that pre-dates PR #1191.
    """
    db_path = os.path.join(palace, "chroma.sqlite3")
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE collections (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL
            );
            CREATE TABLE collection_metadata (
                collection_id TEXT REFERENCES collections(id) ON DELETE CASCADE,
                key TEXT NOT NULL,
                str_value TEXT,
                int_value INTEGER,
                float_value REAL,
                bool_value INTEGER,
                PRIMARY KEY (collection_id, key)
            );
            CREATE TABLE segments (
                id TEXT PRIMARY KEY,
                collection TEXT NOT NULL,
                scope TEXT NOT NULL
            );
            CREATE TABLE embeddings (
                id INTEGER PRIMARY KEY,
                segment_id TEXT NOT NULL,
                embedding_id TEXT NOT NULL,
                seq_id BLOB NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE embedding_metadata (
                id INTEGER REFERENCES embeddings(id),
                key TEXT NOT NULL,
                string_value TEXT,
                int_value INTEGER,
                float_value REAL,
                bool_value INTEGER,
                PRIMARY KEY (id, key)
            );
            CREATE VIRTUAL TABLE embedding_fulltext_search
                USING fts5(string_value, tokenize='trigram');
            """
        )
        col_id = "col-test"
        meta_seg = "seg-meta"
        conn.execute("INSERT INTO collections (id, name) VALUES (?, ?)", (col_id, COLLECTION))
        if sync_threshold is not None:
            conn.execute(
                """INSERT INTO collection_metadata (collection_id, key, int_value)
                   VALUES (?, 'hnsw:sync_threshold', ?)""",
                (col_id, sync_threshold),
            )
        conn.execute(
            "INSERT INTO segments (id, collection, scope) VALUES (?, ?, 'VECTOR')",
            (segment_id, col_id),
        )
        conn.execute(
            "INSERT INTO segments (id, collection, scope) VALUES (?, ?, 'METADATA')",
            (meta_seg, col_id),
        )
        for i in range(sqlite_count):
            conn.execute(
                """INSERT INTO embeddings (id, segment_id, embedding_id, seq_id)
                   VALUES (?, ?, ?, ?)""",
                (i + 1, segment_id, f"d-{i}", b"\x00\x00\x00\x00\x00\x00\x00\x01"),
            )
        conn.commit()
    finally:
        conn.close()


def _write_pickle(palace: str, segment_id: str, hnsw_count: int) -> None:
    """Write an index_metadata.pickle matching chromadb 1.5.x's shape.

    1.5.x ``__reduce_ex__`` serializes the PersistentData instance as a
    plain dict; we replicate that so the safe unpickler in
    ``_hnsw_element_count`` reads the same bytes shape it would in
    production.
    """
    seg_dir = os.path.join(palace, segment_id)
    os.makedirs(seg_dir, exist_ok=True)
    pickle_path = os.path.join(seg_dir, "index_metadata.pickle")
    state = {
        "dimensionality": 384,
        "total_elements_added": hnsw_count,
        "max_seq_id": None,
        "id_to_label": {f"d-{i}": i for i in range(hnsw_count)},
        "label_to_id": {i: f"d-{i}" for i in range(hnsw_count)},
        "id_to_seq_id": {},
    }
    with open(pickle_path, "wb") as f:
        pickle.dump(state, f, pickle.HIGHEST_PROTOCOL)


# ── _vector_segment_id ────────────────────────────────────────────────


def test_vector_segment_id_returns_uuid(tmp_path):
    seg = "11111111-2222-3333-4444-555555555555"
    _seed_chroma_db(str(tmp_path), sqlite_count=10, segment_id=seg)
    assert _vector_segment_id(str(tmp_path), COLLECTION) == seg


def test_vector_segment_id_no_palace(tmp_path):
    assert _vector_segment_id(str(tmp_path), COLLECTION) is None


def test_vector_segment_id_unknown_collection(tmp_path):
    seg = "11111111-2222-3333-4444-555555555555"
    _seed_chroma_db(str(tmp_path), sqlite_count=10, segment_id=seg)
    assert _vector_segment_id(str(tmp_path), "nope") is None


# ── _hnsw_element_count ───────────────────────────────────────────────


def test_hnsw_element_count_reads_pickle(tmp_path):
    seg = "seg-001"
    _seed_chroma_db(str(tmp_path), sqlite_count=100, segment_id=seg)
    _write_pickle(str(tmp_path), seg, hnsw_count=42)
    assert _hnsw_element_count(str(tmp_path), seg) == 42


def test_hnsw_element_count_missing_pickle(tmp_path):
    seg = "seg-001"
    _seed_chroma_db(str(tmp_path), sqlite_count=100, segment_id=seg)
    # Segment dir doesn't even exist — no flush ever happened.
    assert _hnsw_element_count(str(tmp_path), seg) is None


def test_hnsw_element_count_rejects_arbitrary_class(tmp_path):
    """Pickled references to unallowed classes must not deserialize.

    Guards against a tampered ``index_metadata.pickle`` triggering code
    execution. The unpickler allowlist is the only protection between
    the file and arbitrary import-time side effects. We hand-craft the
    pickle bytes (rather than ``pickle.dump`` a local class) because
    pickle can't serialize locally-defined classes — but the bytes form
    that names an arbitrary stdlib class is a faithful proxy for the
    tampered-file threat we want to test.
    """
    import pickle as _pickle

    seg = "seg-evil"
    seg_dir = tmp_path / seg
    seg_dir.mkdir()
    pickle_path = seg_dir / "index_metadata.pickle"
    # GLOBAL opcode pointing at os.system, then STOP. If the unpickler
    # didn't enforce the allowlist, find_class would resolve os.system
    # and pickle would set up the call. The allowlist must reject it
    # before find_class returns anything.
    payload = b"c" + b"os\nsystem\n" + _pickle.STOP
    pickle_path.write_bytes(payload)
    assert _hnsw_element_count(str(tmp_path), seg) is None


# ── hnsw_capacity_status ──────────────────────────────────────────────


def test_capacity_status_ok_when_balanced(tmp_path):
    seg = "seg-001"
    _seed_chroma_db(str(tmp_path), sqlite_count=1000, segment_id=seg)
    _write_pickle(str(tmp_path), seg, hnsw_count=950)
    info = hnsw_capacity_status(str(tmp_path), COLLECTION)
    assert info["status"] == "ok"
    assert info["diverged"] is False
    assert info["sqlite_count"] == 1000
    assert info["hnsw_count"] == 950


def test_capacity_status_flags_severe_divergence(tmp_path):
    """Reproduces #1222: sqlite has 192k, HNSW frozen at ~16k."""
    seg = "seg-1222"
    _seed_chroma_db(str(tmp_path), sqlite_count=20_000, segment_id=seg)
    _write_pickle(str(tmp_path), seg, hnsw_count=2_000)
    info = hnsw_capacity_status(str(tmp_path), COLLECTION)
    assert info["status"] == "diverged"
    assert info["diverged"] is True
    assert info["divergence"] == 18_000
    assert "repair" in info["message"].lower()


def test_capacity_status_tolerates_flush_lag(tmp_path):
    """A few hundred entries behind sqlite is normal post-mine state."""
    seg = "seg-lag"
    _seed_chroma_db(str(tmp_path), sqlite_count=5_000, segment_id=seg)
    _write_pickle(str(tmp_path), seg, hnsw_count=4_500)
    info = hnsw_capacity_status(str(tmp_path), COLLECTION)
    assert info["diverged"] is False
    assert info["status"] == "ok"


def test_capacity_status_does_not_flag_unflushed_with_large_sqlite(tmp_path):
    """No pickle + many sqlite rows is inconclusive, not divergence."""
    seg = "seg-noflush"
    _seed_chroma_db(str(tmp_path), sqlite_count=10_000, segment_id=seg)
    info = hnsw_capacity_status(str(tmp_path), COLLECTION)
    assert info["diverged"] is False
    assert info["status"] == "unknown"
    assert info["divergence"] is None
    assert info["hnsw_count"] is None
    assert "capacity unavailable" in info["message"]
    assert "leaving vector search enabled" in info["message"]


def test_mcp_probe_does_not_disable_vectors_for_unflushed_metadata(tmp_path, monkeypatch):
    """The MCP preflight must not route all searches to BM25 on this signal."""
    from mempalace import mcp_server

    seg = "seg-mcp-noflush"
    _seed_chroma_db(str(tmp_path), sqlite_count=10_000, segment_id=seg)

    class _Cfg:
        palace_path = str(tmp_path)
        collection_name = "mempalace_drawers"

    monkeypatch.setattr(mcp_server, "_config", _Cfg())
    monkeypatch.setattr(mcp_server, "_vector_disabled", True)
    monkeypatch.setattr(mcp_server, "_vector_disabled_reason", "old divergence")

    mcp_server._refresh_vector_disabled_flag()

    assert mcp_server._vector_disabled is False
    assert mcp_server._vector_disabled_reason == ""
    assert mcp_server._vector_capacity_status["status"] == "unknown"
    assert "leaving vector search enabled" in mcp_server._vector_capacity_status["message"]


def test_capacity_status_quiet_for_empty_palace(tmp_path):
    info = hnsw_capacity_status(str(tmp_path), COLLECTION)
    assert info["diverged"] is False
    assert info["status"] == "unknown"


# ── Divergence threshold scales with hnsw:sync_threshold ───────────────


def test_capacity_status_tolerates_lag_under_large_sync_threshold(tmp_path):
    """Regression for the PR #1191 / PR #1227 conflict.

    Palaces created via mempalace's _HNSW_BLOAT_GUARD (sync_threshold=
    50_000) naturally accumulate up to ~50K queued entries between
    flushes. The pickle-vs-sqlite probe must scale its tolerance to
    ``2 × sync_threshold`` so this expected lag is not flagged as
    corruption — otherwise vector search disables for ~80% of the
    write cycle on any actively-mined ≥100K palace.
    """
    seg = "seg-bloat-guard"
    _seed_chroma_db(str(tmp_path), sqlite_count=100_000, segment_id=seg, sync_threshold=50_000)
    _write_pickle(str(tmp_path), seg, hnsw_count=50_000)
    info = hnsw_capacity_status(str(tmp_path), COLLECTION)
    # 50K divergence is exactly one flush window — well within 2× = 100K.
    assert info["diverged"] is False, info["message"]
    assert info["status"] == "ok"
    assert info["divergence"] == 50_000


def test_capacity_status_still_flags_real_corruption_under_large_sync(tmp_path):
    """The dynamic floor must still catch genuine #1222-style corruption.

    sqlite at 200K with HNSW frozen at 16K is the original #1222 shape —
    any reasonable threshold should flag it, regardless of whether the
    collection was created with sync_threshold=1000 or 50_000.
    """
    seg = "seg-1222-with-bloat-guard"
    _seed_chroma_db(str(tmp_path), sqlite_count=200_000, segment_id=seg, sync_threshold=50_000)
    _write_pickle(str(tmp_path), seg, hnsw_count=16_384)
    info = hnsw_capacity_status(str(tmp_path), COLLECTION)
    # 183,616 missing — far past 2 × 50K = 100K floor and 10% of 200K = 20K.
    assert info["diverged"] is True
    assert info["status"] == "diverged"
    assert info["divergence"] == 183_616


def test_capacity_status_default_threshold_when_no_sync_metadata(tmp_path):
    """Older palaces without ``hnsw:sync_threshold`` fall back to 2000 floor.

    Pre-PR-#1191 collections only carry ``hnsw:space``. The probe must
    use chromadb's own default sync_threshold of 1000 → floor of 2000,
    matching pre-fix behavior.
    """
    seg = "seg-legacy"
    # No sync_threshold supplied — collection_metadata stays empty.
    _seed_chroma_db(str(tmp_path), sqlite_count=10_000, segment_id=seg)
    _write_pickle(str(tmp_path), seg, hnsw_count=7_500)
    info = hnsw_capacity_status(str(tmp_path), COLLECTION)
    # 2,500 divergence > max(2000 floor, 10% of 10K = 1000) → DIVERGED
    assert info["diverged"] is True
    assert info["divergence"] == 2_500


def test_unflushed_path_also_uses_dynamic_floor(tmp_path):
    """The never-flushed branch must scale with sync_threshold too.

    A 30K-drawer collection under sync_threshold=50_000 hasn't reached
    its first flush yet — pickle is absent. Pre-fix this would flag
    DIVERGED (30K > fixed 2000 floor); post-fix the 30K stays under
    the dynamic 100K floor.
    """
    seg = "seg-preflush-large"
    _seed_chroma_db(str(tmp_path), sqlite_count=30_000, segment_id=seg, sync_threshold=50_000)
    info = hnsw_capacity_status(str(tmp_path), COLLECTION)
    assert info["hnsw_count"] is None
    assert info["diverged"] is False, info["message"]


# ── BM25-only sqlite fallback ─────────────────────────────────────────


def _seed_drawers(palace: str, segment_id: str, drawers: list[tuple[str, dict, str]]) -> None:
    """Insert (text, metadata, embedding_id) tuples into a seeded palace.

    Replaces the bare ``embeddings`` rows from ``_seed_chroma_db`` so the
    sqlite count matches what we insert here.
    """
    db_path = os.path.join(palace, "chroma.sqlite3")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DELETE FROM embeddings")
        for i, (text, meta, eid) in enumerate(drawers, start=1):
            conn.execute(
                """INSERT INTO embeddings (id, segment_id, embedding_id, seq_id)
                   VALUES (?, ?, ?, ?)""",
                (i, segment_id, eid, b"\x00" * 8),
            )
            conn.execute(
                """INSERT INTO embedding_metadata (id, key, string_value)
                   VALUES (?, 'chroma:document', ?)""",
                (i, text),
            )
            conn.execute(
                "INSERT INTO embedding_fulltext_search (rowid, string_value) VALUES (?, ?)",
                (i, text),
            )
            for k, v in meta.items():
                if isinstance(v, int):
                    conn.execute(
                        """INSERT INTO embedding_metadata (id, key, int_value)
                           VALUES (?, ?, ?)""",
                        (i, k, v),
                    )
                else:
                    conn.execute(
                        """INSERT INTO embedding_metadata (id, key, string_value)
                           VALUES (?, ?, ?)""",
                        (i, k, str(v)),
                    )
        conn.commit()
    finally:
        conn.close()


def _set_drawer_created_at(palace: str, timestamps: dict[int, str]) -> None:
    db_path = os.path.join(palace, "chroma.sqlite3")
    conn = sqlite3.connect(db_path)
    try:
        for emb_id, created_at in timestamps.items():
            conn.execute("UPDATE embeddings SET created_at = ? WHERE id = ?", (created_at, emb_id))
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def palace_with_drawers(tmp_path):
    seg = "seg-bm25"
    _seed_chroma_db(str(tmp_path), sqlite_count=0, segment_id=seg)
    drawers = [
        (
            "ChromaDB segfault on every tool call after HNSW divergence",
            {"wing": "ops", "room": "incidents", "source_file": "/x/incident.md"},
            "d-1",
        ),
        (
            "Memory palace technique using rooms and drawers for recall",
            {"wing": "design", "room": "metaphor", "source_file": "/x/design.md"},
            "d-2",
        ),
        (
            "Repair rebuild backs up only the sqlite database",
            {"wing": "ops", "room": "runbook", "source_file": "/x/repair.md"},
            "d-3",
        ),
    ]
    _seed_drawers(str(tmp_path), seg, drawers)
    return tmp_path


def test_bm25_fallback_returns_matches(palace_with_drawers):
    out = _bm25_only_via_sqlite("segfault chromadb", str(palace_with_drawers), n_results=5)
    assert out["fallback"] == "bm25_only_via_sqlite"
    assert len(out["results"]) >= 1
    top = out["results"][0]
    # The incident drawer is the closest BM25 match for these terms.
    assert "segfault" in top["text"].lower()
    assert top["matched_via"] == "bm25_sqlite"
    # Vector fields are intentionally absent in fallback mode.
    assert top["similarity"] is None
    assert top["distance"] is None


def test_bm25_fallback_filters_by_wing(palace_with_drawers):
    out = _bm25_only_via_sqlite(
        "memory palace recall", str(palace_with_drawers), wing="design", n_results=5
    )
    assert all(r["wing"] == "design" for r in out["results"])


def test_bm25_fallback_applies_wing_before_fts_candidate_limit(tmp_path):
    seg = "seg-bm25-fts-limit"
    _seed_chroma_db(str(tmp_path), sqlite_count=0, segment_id=seg)
    _seed_drawers(
        str(tmp_path),
        seg,
        [
            (
                "shared token outside target wing",
                {"wing": "ops", "room": "incidents", "source_file": "/x/ops.md"},
                "d-1",
            ),
            (
                "shared token inside target wing",
                {"wing": "project", "room": "diary", "source_file": "/x/project.md"},
                "d-2",
            ),
        ],
    )

    out = _bm25_only_via_sqlite("shared token", str(tmp_path), wing="project", max_candidates=1)

    assert out["total_before_filter"] == 1
    assert len(out["results"]) == 1
    assert out["results"][0]["wing"] == "project"


def test_bm25_fallback_applies_room_before_fts_candidate_limit(tmp_path):
    seg = "seg-bm25-room-limit"
    _seed_chroma_db(str(tmp_path), sqlite_count=0, segment_id=seg)
    _seed_drawers(
        str(tmp_path),
        seg,
        [
            (
                "shared token wrong room",
                {"wing": "project", "room": "scratch", "source_file": "/x/scratch.md"},
                "d-1",
            ),
            (
                "shared token right room",
                {"wing": "project", "room": "diary", "source_file": "/x/diary.md"},
                "d-2",
            ),
        ],
    )

    out = _bm25_only_via_sqlite(
        "shared token",
        str(tmp_path),
        wing="project",
        room="diary",
        max_candidates=1,
    )

    assert out["total_before_filter"] == 1
    assert len(out["results"]) == 1
    assert out["results"][0]["wing"] == "project"
    assert out["results"][0]["room"] == "diary"


def test_bm25_fallback_applies_wing_before_recency_candidate_limit(tmp_path):
    seg = "seg-bm25-recency-limit"
    _seed_chroma_db(str(tmp_path), sqlite_count=0, segment_id=seg)
    _seed_drawers(
        str(tmp_path),
        seg,
        [
            (
                "target drawer for short query",
                {"wing": "project", "room": "diary", "source_file": "/x/project.md"},
                "d-1",
            ),
            (
                "newer drawer outside target wing",
                {"wing": "ops", "room": "incidents", "source_file": "/x/ops.md"},
                "d-2",
            ),
        ],
    )
    _set_drawer_created_at(
        str(tmp_path),
        {
            1: "2026-01-01 00:00:00",
            2: "2026-02-01 00:00:00",
        },
    )

    out = _bm25_only_via_sqlite("a", str(tmp_path), wing="project", max_candidates=1)

    assert out["total_before_filter"] == 1
    assert len(out["results"]) == 1
    assert out["results"][0]["wing"] == "project"


def test_bm25_fallback_returns_empty_when_filtered_wing_has_no_candidates(tmp_path):
    seg = "seg-bm25-empty-filter"
    _seed_chroma_db(str(tmp_path), sqlite_count=0, segment_id=seg)
    _seed_drawers(
        str(tmp_path),
        seg,
        [
            (
                "shared token outside target wing",
                {"wing": "ops", "room": "incidents", "source_file": "/x/ops.md"},
                "d-1",
            ),
        ],
    )

    out = _bm25_only_via_sqlite("shared token", str(tmp_path), wing="project", max_candidates=1)

    assert out["total_before_filter"] == 0
    assert out["results"] == []


def test_bm25_fallback_no_palace(tmp_path):
    out = _bm25_only_via_sqlite("anything", str(tmp_path))
    assert "error" in out


def test_bm25_fallback_handles_short_query(palace_with_drawers):
    """Single-character tokens are unmatchable in trigram FTS5 — must
    not crash, must fall back to the recency window."""
    out = _bm25_only_via_sqlite("a", str(palace_with_drawers), n_results=5)
    # Falls back to recency window; returns whatever it can rank.
    assert out["fallback"] == "bm25_only_via_sqlite"
    assert isinstance(out["results"], list)


# ── repair.status CLI command ─────────────────────────────────────────


def test_repair_status_reports_diverged(tmp_path, capsys):
    """The status command prints DIVERGED and recommends the from-sqlite
    rebuild (not a re-mine), since a diverged index means the rows are
    intact in sqlite but the HNSW segment is out of sync (#1843)."""
    from mempalace.repair import status as repair_status

    seg = "seg-status"
    _seed_chroma_db(str(tmp_path), sqlite_count=20_000, segment_id=seg)
    _write_pickle(str(tmp_path), seg, hnsw_count=2_000)
    out = repair_status(palace_path=str(tmp_path))
    captured = capsys.readouterr().out
    assert "DIVERGED" in captured
    assert "mempalace repair --mode from-sqlite --archive-existing" in captured
    assert "Do not re-mine" in captured
    assert out["drawers"]["diverged"] is True


def test_repair_status_quiet_on_healthy_palace(tmp_path, capsys):
    from mempalace.repair import status as repair_status

    seg = "seg-status-ok"
    _seed_chroma_db(str(tmp_path), sqlite_count=500, segment_id=seg)
    _write_pickle(str(tmp_path), seg, hnsw_count=480)
    repair_status(palace_path=str(tmp_path))
    captured = capsys.readouterr().out
    assert "DIVERGED" not in captured
    assert "Recommended" not in captured


# ── tool_status sqlite fallback (#1222 short-circuit) ─────────────────


def test_tool_status_via_sqlite_returns_breakdown(palace_with_drawers, monkeypatch):
    """When _vector_disabled is set, tool_status reads counts from sqlite
    instead of opening a chromadb client."""
    from mempalace import mcp_server

    # _config.palace_path is a read-only property; swap the whole object
    # for a tiny stand-in so we don't have to monkey with the real
    # MempalaceConfig.
    class _Cfg:
        palace_path = str(palace_with_drawers)
        collection_name = "mempalace_drawers"

    monkeypatch.setattr(mcp_server, "_config", _Cfg())
    monkeypatch.setattr(mcp_server, "_vector_disabled", True)
    monkeypatch.setattr(mcp_server, "_vector_disabled_reason", "test divergence")

    out = mcp_server._tool_status_via_sqlite()
    assert out["vector_disabled"] is True
    assert out["vector_disabled_reason"] == "test divergence"
    assert out["total_drawers"] == 3
    # Wing breakdown comes from the seeded palace_with_drawers fixture:
    # ops×2 (incident + repair runbook), design×1 (metaphor).
    assert out["wings"].get("ops") == 2
    assert out["wings"].get("design") == 1


def test_capacity_status_flags_small_gap_with_explicit_low_sync_threshold(tmp_path):
    """New palaces use a low explicit sync threshold, so 57 missing rows is unsafe."""
    seg = "seg-1816-explicit-low-sync"
    _seed_chroma_db(str(tmp_path), sqlite_count=1768, segment_id=seg, sync_threshold=2)
    _write_pickle(str(tmp_path), seg, hnsw_count=1711)

    info = hnsw_capacity_status(str(tmp_path), COLLECTION)

    assert info["divergence"] == 57
    assert info["threshold"] == 4
    assert info["status"] == "diverged"
    assert info["diverged"] is True
    assert "repair" in info["message"].lower()


def test_capacity_status_flags_stale_below_floor_divergence(tmp_path):
    """A persistent below-floor sqlite>HNSW gap must not be treated as fresh lag."""
    from mempalace.backends import chroma

    seg = "seg-1816-stale-below-floor"
    _seed_chroma_db(str(tmp_path), sqlite_count=1768, segment_id=seg)
    _write_pickle(str(tmp_path), seg, hnsw_count=1711)

    pickle_path = tmp_path / seg / "index_metadata.pickle"
    old = time.time() - chroma._HNSW_PERSISTENT_DIVERGENCE_GRACE_SECONDS - 10
    os.utime(pickle_path, (old, old))

    info = hnsw_capacity_status(str(tmp_path), COLLECTION)

    assert info["divergence"] == 57
    assert info["threshold"] >= 2000
    assert info["status"] == "diverged"
    assert info["diverged"] is True
    assert "persisted below" in info["message"]


def test_capacity_status_ok_with_stale_metadata_under_explicit_threshold(tmp_path):
    """An idle database with an explicit sync threshold and a gap within tolerance must remain OK."""
    seg = "seg-1816-stale-ok"
    _seed_chroma_db(str(tmp_path), sqlite_count=1712, segment_id=seg, sync_threshold=2)
    _write_pickle(str(tmp_path), seg, hnsw_count=1711)
    pickle_path = tmp_path / seg / "index_metadata.pickle"
    old = time.time() - 400.0
    os.utime(pickle_path, (old, old))
    info = hnsw_capacity_status(str(tmp_path), COLLECTION)
    assert info["divergence"] == 1
    assert info["threshold"] == 4
    assert info["status"] == "ok"
    assert info["diverged"] is False


# ── Probe result cache (#1471) ────────────────────────────────────────


class TestCapacityProbeCache:
    """The probe is re-used while the files it reads are untouched.

    Every search, duplicate check and status call runs the probe, and each
    run costs four sqlite connections plus a full unpickle of the segment
    metadata. Caching it is only safe if any on-disk change re-probes
    immediately — a stale "ok" would walk a diverged segment straight into
    the #1222 segfault the probe exists to prevent.
    """

    # Cache isolation comes from conftest's suite-wide ``_reset_mcp_cache``,
    # which clears every module-level palace cache between tests — the same
    # place ChromaBackend._quarantined_paths and the MCP client cache are reset.

    @pytest.fixture
    def probe_runs(self, monkeypatch):
        """Record every call that reaches the uncached probe."""
        from mempalace.backends import chroma as chroma_mod

        calls: list[tuple[str, str]] = []
        real = chroma_mod._hnsw_capacity_status_uncached

        def counting(palace_path, collection_name="mempalace_drawers"):
            calls.append((palace_path, collection_name))
            return real(palace_path, collection_name)

        monkeypatch.setattr(chroma_mod, "_hnsw_capacity_status_uncached", counting)
        return calls

    @staticmethod
    def _balanced_palace(tmp_path, seg="seg-cache"):
        _seed_chroma_db(str(tmp_path), sqlite_count=20_000, segment_id=seg)
        _write_pickle(str(tmp_path), seg, hnsw_count=19_900)
        return seg

    def test_unchanged_palace_probes_once(self, tmp_path, probe_runs):
        self._balanced_palace(tmp_path)
        first = hnsw_capacity_status(str(tmp_path), COLLECTION)
        second = hnsw_capacity_status(str(tmp_path), COLLECTION)
        third = hnsw_capacity_status(str(tmp_path), COLLECTION)
        assert len(probe_runs) == 1
        assert first == second == third
        assert first["status"] == "ok"

    def test_pickle_rewrite_reprobes_and_reports_divergence(self, tmp_path, probe_runs):
        """The #1222 guard must not go blind behind the cache."""
        seg = self._balanced_palace(tmp_path)
        assert hnsw_capacity_status(str(tmp_path), COLLECTION)["diverged"] is False

        # The segment loses almost every element — exactly the state that
        # segfaults chromadb once a query touches it.
        _write_pickle(str(tmp_path), seg, hnsw_count=2_000)

        after = hnsw_capacity_status(str(tmp_path), COLLECTION)
        assert len(probe_runs) == 2, "a rewritten pickle must re-run the probe"
        assert after["diverged"] is True
        assert after["hnsw_count"] == 2_000

    def test_sqlite_write_reprobes(self, tmp_path, probe_runs):
        seg = self._balanced_palace(tmp_path)
        hnsw_capacity_status(str(tmp_path), COLLECTION)

        db_path = os.path.join(str(tmp_path), "chroma.sqlite3")
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                """INSERT INTO embeddings (id, segment_id, embedding_id, seq_id)
                   VALUES (?, ?, ?, ?)""",
                (999_999, seg, "d-extra", b"\x00\x00\x00\x00\x00\x00\x00\x01"),
            )
            conn.commit()
        finally:
            conn.close()

        after = hnsw_capacity_status(str(tmp_path), COLLECTION)
        assert len(probe_runs) == 2
        assert after["sqlite_count"] == 20_001

    def test_wal_sidecar_write_reprobes(self, tmp_path, probe_runs):
        """Under WAL a writer leaves chroma.sqlite3's own mtime untouched.

        chromadb 1.5.x uses ``journal_mode=delete``, so this models the palace
        being opened in WAL mode by something else rather than today's default.
        """
        self._balanced_palace(tmp_path)
        hnsw_capacity_status(str(tmp_path), COLLECTION)

        db_path = os.path.join(str(tmp_path), "chroma.sqlite3")
        before = os.stat(db_path)
        with open(db_path + "-wal", "wb") as f:
            f.write(b"\x00" * 4096)
        assert os.stat(db_path).st_mtime_ns == before.st_mtime_ns, (
            "precondition: the main DB file must be untouched by this write"
        )

        hnsw_capacity_status(str(tmp_path), COLLECTION)
        assert len(probe_runs) == 2, "a WAL-only write must still invalidate"

    def test_cache_is_keyed_by_collection(self, tmp_path, probe_runs):
        """repair.status probes drawers and closets against the same palace."""
        self._balanced_palace(tmp_path)
        drawers = hnsw_capacity_status(str(tmp_path), COLLECTION)
        closets = hnsw_capacity_status(str(tmp_path), "mempalace_closets")

        assert len(probe_runs) == 2
        assert drawers["sqlite_count"] == 20_000
        # The closets collection does not exist in this palace, so its verdict
        # must not be the drawers verdict served from a palace-only key.
        assert closets["sqlite_count"] != 20_000

    def test_reset_forces_a_reprobe(self, tmp_path, probe_runs):
        self._balanced_palace(tmp_path)
        hnsw_capacity_status(str(tmp_path), COLLECTION)
        hnsw_capacity_status(str(tmp_path), COLLECTION)
        assert len(probe_runs) == 1

        reset_hnsw_capacity_cache()
        hnsw_capacity_status(str(tmp_path), COLLECTION)
        assert len(probe_runs) == 2

    def test_caller_mutation_does_not_corrupt_the_cache(self, tmp_path, probe_runs):
        """mcp_server parks the verdict in a module global; keep them isolated.

        Both directions matter: the dict handed to the caller that ran the
        probe must not be the stored one, and neither must the dict handed to
        every later caller served from the cache.
        """
        self._balanced_palace(tmp_path)
        first = hnsw_capacity_status(str(tmp_path), COLLECTION)
        first["diverged"] = "tampered-by-prober"
        first["message"] = "tampered-by-prober"

        second = hnsw_capacity_status(str(tmp_path), COLLECTION)
        assert second["diverged"] is False
        assert second["message"] != "tampered-by-prober"

        # A cache hit must hand out a copy too, or this caller poisons the
        # verdict every later reader sees.
        second["diverged"] = "tampered-by-reader"
        second["message"] = "tampered-by-reader"

        third = hnsw_capacity_status(str(tmp_path), COLLECTION)
        assert len(probe_runs) == 1
        assert third["diverged"] is False
        assert third["message"] != "tampered-by-reader"

    def test_write_during_the_probe_is_not_cached(self, tmp_path, monkeypatch):
        """A verdict that pins to neither disk state must not be reused."""
        from mempalace.backends import chroma as chroma_mod

        self._balanced_palace(tmp_path)
        db_path = os.path.join(str(tmp_path), "chroma.sqlite3")
        real = chroma_mod._hnsw_capacity_status_uncached
        runs: list[int] = []

        def racing(palace_path, collection_name="mempalace_drawers"):
            result = real(palace_path, collection_name)
            runs.append(1)
            if len(runs) == 1:
                # Land a write after the probe read, before it returns.
                with open(db_path + "-wal", "wb") as f:
                    f.write(b"\x01" * 2048)
            return result

        monkeypatch.setattr(chroma_mod, "_hnsw_capacity_status_uncached", racing)

        hnsw_capacity_status(str(tmp_path), COLLECTION)
        hnsw_capacity_status(str(tmp_path), COLLECTION)
        assert len(runs) == 2, "a palace written mid-probe must not be cached"

        # The second probe ran without a racing write, so caching resumes.
        hnsw_capacity_status(str(tmp_path), COLLECTION)
        assert len(runs) == 2, "a quiet palace must be cached again afterwards"

    def test_verdict_is_not_reused_past_the_age_ceiling(self, tmp_path, probe_runs, monkeypatch):
        """Backstop for filesystems whose timestamps are too coarse to notice."""
        from mempalace.backends import chroma as chroma_mod

        self._balanced_palace(tmp_path)
        clock = [1_000.0]
        monkeypatch.setattr(chroma_mod.time, "monotonic", lambda: clock[0])

        hnsw_capacity_status(str(tmp_path), COLLECTION)
        clock[0] += chroma_mod._CAPACITY_CACHE_MAX_AGE_SECONDS / 2
        hnsw_capacity_status(str(tmp_path), COLLECTION)
        assert len(probe_runs) == 1, "still inside the ceiling: serve the cached verdict"

        clock[0] += chroma_mod._CAPACITY_CACHE_MAX_AGE_SECONDS
        hnsw_capacity_status(str(tmp_path), COLLECTION)
        assert len(probe_runs) == 2, "past the ceiling: re-probe even with identical files"

    def test_probe_still_never_raises_on_an_unstattable_path(self):
        """The probe's stated contract is "never raises"; caching must keep it.

        A palace path with an embedded null byte makes ``os.stat`` raise
        ``ValueError`` rather than ``OSError``, which a signature helper that
        only caught ``OSError`` would let escape.
        """
        result = hnsw_capacity_status("\x00bad", COLLECTION)
        assert result["status"] == "unknown"
        assert result["diverged"] is False

    def test_first_probe_runs_even_when_process_uptime_is_tiny(
        self, tmp_path, probe_runs, monkeypatch
    ):
        """The withdrawn PR #1756 skipped its very first probe.

        That TTL compared ``time.monotonic()`` against a timestamp initialised
        to ``0.0``, so on a process younger than the TTL the comparison said
        "probed recently" before any probe had run. Here the age check is
        reachable only through an existing cache entry, so a cold cache always
        probes no matter what the clock reads.
        """
        from mempalace.backends import chroma as chroma_mod

        self._balanced_palace(tmp_path)
        monkeypatch.setattr(chroma_mod.time, "monotonic", lambda: 0.5)

        info = hnsw_capacity_status(str(tmp_path), COLLECTION)
        assert len(probe_runs) == 1
        assert info["status"] == "ok"
        assert info["sqlite_count"] == 20_000

    def test_first_flush_after_an_unknown_verdict_is_noticed(self, tmp_path, probe_runs):
        """The common lifecycle: a fresh palace has no pickle until it flushes."""
        seg = "seg-firstflush"
        _seed_chroma_db(str(tmp_path), sqlite_count=20_000, segment_id=seg)

        unflushed = hnsw_capacity_status(str(tmp_path), COLLECTION)
        assert unflushed["status"] == "unknown"
        assert unflushed["hnsw_count"] is None

        # The first flush writes a brand-new file in a sibling segment dir and
        # never touches chroma.sqlite3's own mtime.
        _write_pickle(str(tmp_path), seg, hnsw_count=2_000)

        after = hnsw_capacity_status(str(tmp_path), COLLECTION)
        assert len(probe_runs) == 2, "the first flush must invalidate an 'unknown' verdict"
        assert after["hnsw_count"] == 2_000
        assert after["diverged"] is True

    def test_segment_replacement_is_noticed(self, tmp_path, probe_runs):
        """A dropped-and-recreated collection lands on a new VECTOR segment."""
        old_seg = self._balanced_palace(tmp_path, seg="seg-old")
        assert hnsw_capacity_status(str(tmp_path), COLLECTION)["segment_id"] == old_seg

        new_seg = "seg-new"
        db_path = os.path.join(str(tmp_path), "chroma.sqlite3")
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("UPDATE segments SET id = ? WHERE scope = 'VECTOR'", (new_seg,))
            conn.execute("UPDATE embeddings SET segment_id = ?", (new_seg,))
            conn.commit()
        finally:
            conn.close()
        _write_pickle(str(tmp_path), new_seg, hnsw_count=19_900)

        moved = hnsw_capacity_status(str(tmp_path), COLLECTION)
        assert moved["segment_id"] == new_seg

        # The verdict must now track the NEW segment's pickle, not the old one.
        _write_pickle(str(tmp_path), new_seg, hnsw_count=2_000)
        runs_before = len(probe_runs)
        diverged = hnsw_capacity_status(str(tmp_path), COLLECTION)
        assert len(probe_runs) == runs_before + 1
        assert diverged["diverged"] is True

    def test_palace_removal_is_noticed(self, tmp_path, probe_runs):
        """A palace deleted underneath a live server must not stay "ok"."""
        palace = tmp_path / "palace"
        palace.mkdir()
        _seed_chroma_db(str(palace), sqlite_count=20_000, segment_id="seg-gone")
        _write_pickle(str(palace), "seg-gone", hnsw_count=19_900)
        assert hnsw_capacity_status(str(palace), COLLECTION)["status"] == "ok"

        shutil.rmtree(palace)

        gone = hnsw_capacity_status(str(palace), COLLECTION)
        assert len(probe_runs) == 2
        assert gone["status"] == "unknown"
        assert gone["diverged"] is False

    def test_cache_never_exceeds_the_bound_at_any_point(self, tmp_path):
        """Check the peak, not just the size left over at the end.

        The final size after the clear-and-refill cycle is small whatever the
        threshold comparison is, so asserting on it alone would pass with an
        off-by-one bound.
        """
        from mempalace.backends import chroma as chroma_mod

        peak = 0
        for i in range(chroma_mod._CAPACITY_CACHE_MAX_ENTRIES + 5):
            palace = tmp_path / f"palace-{i}"
            palace.mkdir()
            _seed_chroma_db(str(palace), sqlite_count=10, segment_id=f"seg-{i}")
            hnsw_capacity_status(str(palace), COLLECTION)
            peak = max(peak, len(chroma_mod._capacity_cache))

        assert peak <= chroma_mod._CAPACITY_CACHE_MAX_ENTRIES

    def test_concurrent_probes_are_safe_and_agree(self, tmp_path):
        """The cache is deliberately lock-free; a racing miss must stay benign."""
        import concurrent.futures

        self._balanced_palace(tmp_path)

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            verdicts = list(
                pool.map(lambda _: hnsw_capacity_status(str(tmp_path), COLLECTION), range(32))
            )

        assert all(v["status"] == "ok" for v in verdicts)
        assert {v["sqlite_count"] for v in verdicts} == {20_000}

    def test_wal_mode_palace_still_gets_cache_hits(self, tmp_path, probe_runs):
        """A WAL-mode palace must not invalidate its own cache on every call.

        sqlite restamps the ``-shm`` WAL index every time a connection opens
        the database, read-only included. A signature covering ``-shm`` would
        therefore change under the probe's own reads, so the cache would miss
        100% of the time on exactly the palaces this fix is meant to speed up.
        """
        self._balanced_palace(tmp_path)
        db_path = os.path.join(str(tmp_path), "chroma.sqlite3")
        conn = sqlite3.connect(db_path)
        try:
            mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        finally:
            conn.close()
        assert mode == "wal", "precondition: the palace must really be in WAL mode"

        # The first reads settle the WAL file itself, which legitimately moves
        # the signature; what matters is that it then stops moving.
        for _ in range(2):
            hnsw_capacity_status(str(tmp_path), COLLECTION)
        settled = len(probe_runs)

        for _ in range(4):
            assert hnsw_capacity_status(str(tmp_path), COLLECTION)["status"] == "ok"

        assert len(probe_runs) == settled, (
            "reads alone must not keep invalidating the verdict on a WAL palace"
        )

    def test_pickle_rewrite_during_probe_is_not_cached(self, tmp_path):
        """A pickle rewrite landing mid-probe must not be pinned as fresh.

        The probe reads ``index_metadata.pickle`` partway through, then makes
        two more sqlite calls. If the fingerprint were snapshotted only after
        the probe returned, a rewrite that lands in that window would be
        recorded as "unchanged" while the verdict still reflected the pre-write
        file — the exact #1222 blindness the probe exists to prevent. We drive
        the writer from ``_read_sync_threshold``, which the probe calls strictly
        after the pickle read and strictly before it returns.
        """
        from mempalace.backends import chroma as chroma_mod

        seg = "seg-race"
        _seed_chroma_db(str(tmp_path), sqlite_count=20_000, segment_id=seg)
        _write_pickle(str(tmp_path), seg, hnsw_count=19_900)  # healthy

        real_rst = chroma_mod._read_sync_threshold
        fired = []

        def racing(pp, cn):
            if not fired:
                fired.append(1)
                _write_pickle(str(tmp_path), seg, hnsw_count=2_000)  # collapse
            return real_rst(pp, cn)

        try:
            chroma_mod._read_sync_threshold = racing
            hnsw_capacity_status(str(tmp_path), COLLECTION)  # races, must not cache
        finally:
            chroma_mod._read_sync_threshold = real_rst

        # No further writes: a later caller must see the collapsed truth, not a
        # cached "ok" from the raced probe.
        served = hnsw_capacity_status(str(tmp_path), COLLECTION)
        assert served["hnsw_count"] == 2_000
        assert served["diverged"] is True

    def test_locked_database_verdict_is_not_cached(self, tmp_path, probe_runs):
        """A probe that could not read sqlite must not pin a false 'unknown'."""
        from mempalace.backends import chroma as chroma_mod

        self._balanced_palace(tmp_path)

        real_count = chroma_mod._sqlite_embedding_count
        locked = []

        def flaky_count(pp, cn):
            if not locked:
                locked.append(1)
                return None  # simulate "database is locked" swallowed to None
            return real_count(pp, cn)

        try:
            chroma_mod._sqlite_embedding_count = flaky_count
            first = hnsw_capacity_status(str(tmp_path), COLLECTION)
        finally:
            chroma_mod._sqlite_embedding_count = real_count

        assert first["sqlite_count"] is None  # the locked read
        # The lock cleared; the next call must re-probe rather than serve the
        # cached "could not read" verdict.
        second = hnsw_capacity_status(str(tmp_path), COLLECTION)
        assert len(probe_runs) == 2
        assert second["sqlite_count"] == 20_000

    def test_reset_during_probe_is_not_resurrected(self, tmp_path, probe_runs):
        """A reset landing while a probe runs must win — the entry stays gone.

        ``tool_reconnect`` clears the cache to force a fresh read; a probe that
        started earlier must not repopulate the very entry the reset dropped.
        """
        from mempalace.backends import chroma as chroma_mod

        self._balanced_palace(tmp_path)
        real = chroma_mod._hnsw_capacity_status_uncached

        def reset_midway(pp, cn="mempalace_drawers"):
            result = real(pp, cn)
            reset_hnsw_capacity_cache()  # a concurrent tool_reconnect
            return result

        try:
            chroma_mod._hnsw_capacity_status_uncached = reset_midway
            hnsw_capacity_status(str(tmp_path), COLLECTION)
        finally:
            chroma_mod._hnsw_capacity_status_uncached = real

        assert chroma_mod._capacity_cache == {}, "the in-flight probe resurrected a reset entry"
