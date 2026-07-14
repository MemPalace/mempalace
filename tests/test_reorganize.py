"""Tests for read-only palace reorganization planning."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import stat
from datetime import date

from mempalace.reorganize import (
    InventoryRecord,
    collect_duplicate_evidence,
    exact_hash,
    inventory_palace,
    plan_actions,
    prove_worktree_duplicate,
    write_manifest,
)


def _record(
    drawer_id: str,
    origin: str,
    *,
    content: str,
    relative_identity: str | None = None,
    authored_at: str | None = None,
    pinned: bool = False,
    chunk_index: int | None = 0,
    source_sha256: str | None = None,
) -> InventoryRecord:
    metadata = {"wing": "se"}
    if chunk_index is not None:
        metadata["chunk_index"] = chunk_index
    if source_sha256 is not None:
        metadata["source_sha256"] = source_sha256
    if authored_at is not None:
        metadata["authored_at"] = authored_at
    if pinned:
        metadata["pinned"] = True
    return InventoryRecord(
        drawer_id=drawer_id,
        content=content,
        metadata=metadata,
        origin=origin,
        relative_identity=relative_identity,
        source_path=None,
    )


def _seed_palace(palace, rows):
    db_path = palace / "chroma.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE collections (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE segments (id INTEGER PRIMARY KEY, collection INTEGER NOT NULL);
            CREATE TABLE embeddings (
                id INTEGER PRIMARY KEY,
                segment_id INTEGER NOT NULL,
                embedding_id TEXT,
                created_at TEXT
            );
            CREATE TABLE embedding_metadata (
                id INTEGER,
                key TEXT,
                string_value TEXT,
                int_value INTEGER,
                float_value REAL,
                bool_value INTEGER
            );
            INSERT INTO collections(id, name) VALUES (1, 'mempalace_drawers');
            INSERT INTO segments(id, collection) VALUES (1, 1);
            """
        )
        for row_id, (drawer_id, content, metadata) in enumerate(rows, start=1):
            conn.execute(
                "INSERT INTO embeddings(id, segment_id, embedding_id, created_at) "
                "VALUES (?, 1, ?, '2026-07-14T00:00:00Z')",
                (row_id, drawer_id),
            )
            conn.execute(
                "INSERT INTO embedding_metadata(id, key, string_value) VALUES (?, ?, ?)",
                (row_id, "chroma:document", content),
            )
            for key, value in metadata.items():
                if isinstance(value, bool):
                    column = "bool_value"
                    stored = int(value)
                elif isinstance(value, int):
                    column = "int_value"
                    stored = value
                elif isinstance(value, float):
                    column = "float_value"
                    stored = value
                else:
                    column = "string_value"
                    stored = str(value)
                conn.execute(
                    f"INSERT INTO embedding_metadata(id, key, {column}) VALUES (?, ?, ?)",
                    (row_id, key, stored),
                )
    return db_path


def test_exact_hash_hashes_verbatim_utf8_content():
    content = "decision\nwith trailing space "
    assert exact_hash(content) == hashlib.sha256(content.encode("utf-8")).hexdigest()


def test_plan_classifies_canonical_sessions_and_worktree_candidates():
    actions = plan_actions(
        [
            _record(
                "canonical",
                "canonical",
                content="A",
                relative_identity="src/a.ts",
            ),
            _record(
                "worktree",
                "worktree",
                content="A",
                relative_identity="src/a.ts",
            ),
            _record(
                "session",
                "session",
                content="talk",
                authored_at="2026-01-01",
            ),
        ],
        hot_days=90,
        now=date(2026, 7, 14),
    )

    by_id = {action.drawer_id: action for action in actions}
    assert by_id["canonical"].destination_wing == "se-code"
    assert by_id["worktree"].action == "duplicate_candidate"
    assert by_id["session"].metadata["memory_tier"] == "cold"


def test_pinned_old_session_stays_hot():
    actions = plan_actions(
        [
            _record(
                "session",
                "session",
                content="talk",
                authored_at="2020-01-01",
                pinned=True,
            )
        ],
        hot_days=90,
        now=date(2026, 7, 14),
    )

    assert actions[0].metadata["memory_tier"] == "hot"


def test_inventory_reads_chroma_sqlite_without_changing_it(tmp_path):
    palace = tmp_path / "palace"
    canonical = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    sessions = tmp_path / "sessions"
    for path in (palace, canonical, worktree, sessions):
        path.mkdir()

    db_path = _seed_palace(
        palace,
        [
            (
                "z-worktree",
                "same",
                {
                    "wing": "se",
                    "source_file": str(worktree / "src/a.ts"),
                    "chunk_index": 0,
                },
            ),
            (
                "a-canonical",
                "same",
                {
                    "wing": "se",
                    "source_file": str(canonical / "src/a.ts"),
                    "chunk_index": 0,
                },
            ),
            (
                "m-session",
                "talk",
                {
                    "wing": "se",
                    "source_file": str(sessions / "one.jsonl"),
                    "authored_at": "2026-01-01",
                },
            ),
        ],
    )
    before = hashlib.sha256(db_path.read_bytes()).hexdigest()

    records = inventory_palace(
        palace,
        canonical_root=canonical,
        worktree_roots=[worktree],
        session_roots=[sessions],
    )

    assert [record.drawer_id for record in records] == [
        "a-canonical",
        "m-session",
        "z-worktree",
    ]
    assert {record.drawer_id: record.origin for record in records} == {
        "a-canonical": "canonical",
        "m-session": "session",
        "z-worktree": "worktree",
    }
    assert records[0].relative_identity == "src/a.ts"
    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == before


def test_duplicate_requires_same_relative_identity_chunk_and_content_hash():
    canonical = _record(
        "canonical",
        "canonical",
        content="A",
        relative_identity="src/a.ts",
        source_sha256="source-hash",
    )

    assert (
        prove_worktree_duplicate(
            _record(
                "different-content",
                "worktree",
                content="B",
                relative_identity="src/a.ts",
                source_sha256="source-hash",
            ),
            canonical,
        )
        is None
    )
    assert (
        prove_worktree_duplicate(
            _record(
                "different-chunk",
                "worktree",
                content="A",
                relative_identity="src/a.ts",
                chunk_index=1,
                source_sha256="source-hash",
            ),
            canonical,
        )
        is None
    )
    assert (
        prove_worktree_duplicate(
            _record(
                "different-source",
                "worktree",
                content="A",
                relative_identity="src/a.ts",
                source_sha256="other-source-hash",
            ),
            canonical,
        )
        is None
    )
    assert (
        prove_worktree_duplicate(
            _record(
                "missing-identity",
                "worktree",
                content="A",
                relative_identity=None,
                source_sha256="source-hash",
            ),
            canonical,
        )
        is None
    )


def test_duplicate_proof_allows_legacy_pair_with_no_source_hash():
    canonical = _record(
        "canonical",
        "canonical",
        content="A",
        relative_identity="src/a.ts",
    )
    worktree = _record(
        "worktree",
        "worktree",
        content="A",
        relative_identity="src/a.ts",
    )

    evidence = prove_worktree_duplicate(worktree, canonical)

    assert evidence is not None
    assert evidence.worktree_drawer_id == "worktree"
    assert evidence.canonical_drawer_id == "canonical"
    assert evidence.content_sha256 == exact_hash("A")


def test_duplicate_with_only_one_source_hash_is_preserved_as_uncertain():
    records = [
        _record(
            "canonical",
            "canonical",
            content="A",
            relative_identity="src/a.ts",
            source_sha256="known",
        ),
        _record(
            "worktree",
            "worktree",
            content="A",
            relative_identity="src/a.ts",
        ),
    ]

    actions = plan_actions(records)

    assert {action.drawer_id: action.action for action in actions}["worktree"] == (
        "preserve_uncertain"
    )
    assert collect_duplicate_evidence(records, actions) == []


def test_manifest_is_owner_only_deterministic_and_excludes_content(tmp_path):
    records = [
        _record(
            "canonical",
            "canonical",
            content="SECRET DRAWER TEXT",
            relative_identity="src/a.ts",
        ),
        _record(
            "worktree",
            "worktree",
            content="SECRET DRAWER TEXT",
            relative_identity="src/a.ts",
        ),
    ]
    actions = plan_actions(records)
    evidence = collect_duplicate_evidence(records, actions)
    path = tmp_path / "manifest.json"

    write_manifest(
        path,
        records,
        actions,
        evidence,
        palace_path=tmp_path / "palace",
        sqlite_snapshot={"size": 10, "mtime_ns": 20, "sha256": "db-hash"},
    )
    first = path.read_bytes()
    write_manifest(
        path,
        list(reversed(records)),
        list(reversed(actions)),
        list(reversed(evidence)),
        palace_path=tmp_path / "palace",
        sqlite_snapshot={"size": 10, "mtime_ns": 20, "sha256": "db-hash"},
    )

    assert path.read_bytes() == first
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert b"SECRET DRAWER TEXT" not in first
    payload = json.loads(first)
    assert payload["version"] == 1
    assert payload["counts"]["inventory_total"] == 2
    assert payload["counts"]["verified_duplicate_candidates"] == 1
    assert payload["sqlite_snapshot"]["sha256"] == "db-hash"
