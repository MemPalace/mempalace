"""Tests for the #1222 HNSW capacity probe and BM25-only fallback.

The probe and fallback never load chromadb's HNSW segment, so all of
these tests synthesize the on-disk shape directly: a chroma.sqlite3 with
the relevant schema rows and an ``index_metadata.pickle`` matching what
chromadb 1.5.x writes (``{"id_to_label": {...}, ...}``).
"""

from __future__ import annotations

import os
import pickle
import sqlite3

import pytest

from mempalace.backends.chroma import (
    _hnsw_element_count,
    _vector_segment_id,
    hnsw_capacity_status,
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


def test_capacity_status_flags_unflushed_with_large_sqlite(tmp_path):
    """No pickle + many sqlite rows is its own divergence signal."""
    seg = "seg-noflush"
    _seed_chroma_db(str(tmp_path), sqlite_count=10_000, segment_id=seg)
    info = hnsw_capacity_status(str(tmp_path), COLLECTION)
    assert info["diverged"] is True
    assert info["hnsw_count"] is None
    assert "never flushed" in info["message"]


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
    """The status command prints DIVERGED and recommends rebuild."""
    from mempalace.repair import status as repair_status

    seg = "seg-status"
    _seed_chroma_db(str(tmp_path), sqlite_count=20_000, segment_id=seg)
    _write_pickle(str(tmp_path), seg, hnsw_count=2_000)
    out = repair_status(palace_path=str(tmp_path))
    captured = capsys.readouterr().out
    assert "DIVERGED" in captured
    assert "mempalace repair`" in captured
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
