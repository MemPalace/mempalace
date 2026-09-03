"""Tests for ``mempalace repair --mode reconcile``.

Reconcile re-indexes ONLY the drawers missing from the HNSW index by diffing
the sqlite embedding IDs against the HNSW ``id_to_label`` keys and re-upserting
just the gap — the fast fix for a small flush-lag divergence, versus a full
``--mode from-sqlite`` re-embed of the whole palace.

Two layers here:

* Unit tests synthesize the on-disk shape directly (a ``chroma.sqlite3`` plus an
  ``index_metadata.pickle`` matching chromadb 1.5.x's ``{"id_to_label": {...}}``)
  and never open a chromadb client — the diff/extract functions read sqlite and
  the pickle only.
* Orchestration tests patch ``mempalace.repair.ChromaBackend`` so the live
  ``upsert`` path is a mock (same approach ``test_repair.py`` uses for the
  rebuild path) while the diff, cap, and fetch logic run for real against the
  synthetic palace. The real end-to-end "divergence closes" proof runs against
  a live palace, not CI.
* Writer-lease tests hold or probe the real ``mine_palace_lock`` file through
  an independent file handle (byte-range locks on Windows and BSD ``flock``
  are per-handle, so a second handle conflicts even from the same process) —
  proving no concurrent writer can interleave at any phase boundary of the
  reconcile transaction.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import pickle
import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from mempalace import repair
from mempalace.backends.chroma import _hnsw_element_count, _hnsw_id_to_label_keys
from mempalace.palace import MineAlreadyRunning
from mempalace.repair import (
    ReconcileNotSafe,
    _sqlite_embedding_ids,
    compute_missing_ids,
    extract_via_sqlite,
    reconcile_index,
)

COLLECTION = "mempalace_drawers"
VECTOR_SEG = "seg-vec"
META_SEG = "seg-meta"


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _lock_dir_in_tmp(tmp_path, monkeypatch):
    """Keep ``mine_palace_lock``'s ``~/.mempalace/locks`` inside the test tmp
    dir. ``os.path.expanduser("~")`` reads HOME on POSIX but USERPROFILE on
    Windows — set both (same pattern as ``test_palace_locks.py``)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))


def _seed(
    palace: str,
    sqlite_ids: list[str],
    hnsw_ids: "list[str] | None",
    *,
    sync_threshold: "int | None" = 2,
    docs: "dict[str, str] | None" = None,
) -> None:
    """Seed a synthetic palace.

    ``sqlite_ids`` become ``embeddings`` rows under the METADATA segment (each
    with a ``chroma:document`` + wing/room, so ``extract_via_sqlite`` returns
    them). ``hnsw_ids`` — when not None — become the ``id_to_label`` keys of a
    chromadb-1.5.x-shaped ``index_metadata.pickle`` in the VECTOR segment dir;
    None models a never-flushed segment (no pickle).
    """
    db_path = os.path.join(palace, "chroma.sqlite3")
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE collections (id TEXT PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE collection_metadata (
                collection_id TEXT REFERENCES collections(id) ON DELETE CASCADE,
                key TEXT NOT NULL, str_value TEXT, int_value INTEGER,
                float_value REAL, bool_value INTEGER,
                PRIMARY KEY (collection_id, key)
            );
            CREATE TABLE segments (
                id TEXT PRIMARY KEY, collection TEXT NOT NULL, scope TEXT NOT NULL
            );
            CREATE TABLE embeddings (
                id INTEGER PRIMARY KEY, segment_id TEXT NOT NULL,
                embedding_id TEXT NOT NULL, seq_id BLOB NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE embedding_metadata (
                id INTEGER REFERENCES embeddings(id), key TEXT NOT NULL,
                string_value TEXT, int_value INTEGER, float_value REAL,
                bool_value INTEGER, PRIMARY KEY (id, key)
            );
            """
        )
        col_id = "col-test"
        conn.execute("INSERT INTO collections (id, name) VALUES (?, ?)", (col_id, COLLECTION))
        if sync_threshold is not None:
            conn.execute(
                """INSERT INTO collection_metadata (collection_id, key, int_value)
                   VALUES (?, 'hnsw:sync_threshold', ?)""",
                (col_id, sync_threshold),
            )
        conn.execute(
            "INSERT INTO segments (id, collection, scope) VALUES (?, ?, 'VECTOR')",
            (VECTOR_SEG, col_id),
        )
        conn.execute(
            "INSERT INTO segments (id, collection, scope) VALUES (?, ?, 'METADATA')",
            (META_SEG, col_id),
        )
        for i, eid in enumerate(sqlite_ids, start=1):
            conn.execute(
                """INSERT INTO embeddings (id, segment_id, embedding_id, seq_id)
                   VALUES (?, ?, ?, ?)""",
                (i, META_SEG, eid, b"\x00" * 8),
            )
            doc = (docs or {}).get(eid, f"doc for {eid}")
            conn.execute(
                """INSERT INTO embedding_metadata (id, key, string_value)
                   VALUES (?, 'chroma:document', ?)""",
                (i, doc),
            )
            conn.execute(
                """INSERT INTO embedding_metadata (id, key, string_value)
                   VALUES (?, 'wing', 'w')""",
                (i,),
            )
            conn.execute(
                """INSERT INTO embedding_metadata (id, key, string_value)
                   VALUES (?, 'room', 'r')""",
                (i,),
            )
        conn.commit()
    finally:
        conn.close()

    if hnsw_ids is not None:
        seg_dir = os.path.join(palace, VECTOR_SEG)
        os.makedirs(seg_dir, exist_ok=True)
        state = {
            "dimensionality": 384,
            "total_elements_added": len(hnsw_ids),
            "max_seq_id": None,
            "id_to_label": {eid: i for i, eid in enumerate(hnsw_ids)},
            "label_to_id": {i: eid for i, eid in enumerate(hnsw_ids)},
            "id_to_seq_id": {},
        }
        with open(os.path.join(seg_dir, "index_metadata.pickle"), "wb") as f:
            pickle.dump(state, f, pickle.HIGHEST_PROTOCOL)


def _ids(n: int, start: int = 0) -> list[str]:
    return [f"d-{i}" for i in range(start, start + n)]


def _install_mock_backend(mock_backend_cls, collection):
    mock_backend = MagicMock()
    mock_backend.get_collection.return_value = collection
    mock_backend_cls.return_value = mock_backend
    return mock_backend


# ── _hnsw_id_to_label_keys / _hnsw_element_count ──────────────────────


def test_hnsw_id_to_label_keys_returns_keys(tmp_path):
    _seed(str(tmp_path), _ids(5), hnsw_ids=_ids(3))
    keys = _hnsw_id_to_label_keys(str(tmp_path), VECTOR_SEG)
    assert keys == {"d-0", "d-1", "d-2"}


def test_hnsw_id_to_label_keys_missing_pickle_is_none(tmp_path):
    _seed(str(tmp_path), _ids(5), hnsw_ids=None)
    assert _hnsw_id_to_label_keys(str(tmp_path), VECTOR_SEG) is None


def test_hnsw_id_to_label_keys_rejects_tampered_pickle(tmp_path):
    """The allowlist unpickler must reject a GLOBAL opcode → returns None."""
    seg_dir = tmp_path / VECTOR_SEG
    seg_dir.mkdir()
    (seg_dir / "index_metadata.pickle").write_bytes(b"c" + b"os\nsystem\n" + pickle.STOP)
    assert _hnsw_id_to_label_keys(str(tmp_path), VECTOR_SEG) is None


def test_hnsw_element_count_wraps_keys(tmp_path):
    """Regression: the refactored count is still len(id_to_label)."""
    _seed(str(tmp_path), _ids(10), hnsw_ids=_ids(7))
    assert _hnsw_element_count(str(tmp_path), VECTOR_SEG) == 7


def test_hnsw_element_count_none_without_pickle(tmp_path):
    _seed(str(tmp_path), _ids(10), hnsw_ids=None)
    assert _hnsw_element_count(str(tmp_path), VECTOR_SEG) is None


# ── _sqlite_embedding_ids ─────────────────────────────────────────────


def test_sqlite_embedding_ids_returns_all(tmp_path):
    _seed(str(tmp_path), _ids(6), hnsw_ids=_ids(2))
    assert _sqlite_embedding_ids(str(tmp_path), COLLECTION) == set(_ids(6))


def test_sqlite_embedding_ids_none_without_db(tmp_path):
    # None (not empty set) so an unreadable palace is never mistaken for one
    # with zero drawers — the difference between "can't read" and "nothing".
    assert _sqlite_embedding_ids(str(tmp_path), COLLECTION) is None


# ── compute_missing_ids ───────────────────────────────────────────────


def test_compute_missing_ids_finds_the_gap(tmp_path):
    _seed(str(tmp_path), _ids(10), hnsw_ids=_ids(7))
    missing, status = compute_missing_ids(str(tmp_path), COLLECTION)
    assert missing == {"d-7", "d-8", "d-9"}
    assert status["sqlite_count"] == 10
    assert status["hnsw_count"] == 7


def test_compute_missing_ids_empty_when_indexed(tmp_path):
    _seed(str(tmp_path), _ids(5), hnsw_ids=_ids(5))
    missing, _ = compute_missing_ids(str(tmp_path), COLLECTION)
    assert missing == set()


def test_compute_missing_ids_none_when_pickle_absent(tmp_path):
    """A never-flushed pickle is "unknown", never "nothing missing"."""
    _seed(str(tmp_path), _ids(5), hnsw_ids=None)
    missing, _ = compute_missing_ids(str(tmp_path), COLLECTION)
    assert missing is None


def test_compute_missing_ids_none_when_sqlite_unreadable(tmp_path, monkeypatch):
    """A sqlite read failure (e.g. sustained lock) must surface as "unknown",
    never as an empty gap that reconcile would treat as a clean no-op."""
    _seed(str(tmp_path), _ids(5), hnsw_ids=_ids(3))
    monkeypatch.setattr(repair, "_sqlite_embedding_ids", lambda *a, **k: None)
    missing, _ = compute_missing_ids(str(tmp_path), COLLECTION)
    assert missing is None


def test_compute_missing_ids_negative_divergence_is_noop(tmp_path):
    """HNSW holding more than sqlite yields an empty set — never a delete."""
    _seed(str(tmp_path), _ids(5), hnsw_ids=_ids(10))
    missing, status = compute_missing_ids(str(tmp_path), COLLECTION)
    assert missing == set()
    assert status["divergence"] == -5


# ── extract_via_sqlite(only_ids=...) ──────────────────────────────────


def test_extract_only_ids_filters(tmp_path):
    _seed(str(tmp_path), _ids(6), hnsw_ids=_ids(2))
    got = dict(
        (eid, (doc, meta))
        for eid, doc, meta in extract_via_sqlite(str(tmp_path), COLLECTION, only_ids={"d-2", "d-4"})
    )
    assert set(got) == {"d-2", "d-4"}
    assert got["d-2"][0] == "doc for d-2"
    assert got["d-2"][1] == {"wing": "w", "room": "r"}


def test_extract_only_ids_preserves_sparse_metadata_rows(tmp_path):
    _seed(str(tmp_path), ["d-0", "d-1"], hnsw_ids=["d-0"])
    conn = sqlite3.connect(tmp_path / "chroma.sqlite3")
    try:
        row = conn.execute("SELECT id FROM embeddings WHERE embedding_id = ?", ("d-1",)).fetchone()
        assert row is not None
        conn.execute("DELETE FROM embedding_metadata WHERE id = ?", (row[0],))
        conn.commit()
    finally:
        conn.close()

    assert list(extract_via_sqlite(str(tmp_path), COLLECTION, only_ids={"d-1"})) == [
        ("d-1", "", {})
    ]


def test_extract_only_ids_empty_yields_nothing(tmp_path):
    _seed(str(tmp_path), _ids(6), hnsw_ids=_ids(2))
    assert list(extract_via_sqlite(str(tmp_path), COLLECTION, only_ids=set())) == []


def test_extract_only_ids_chunks_over_sqlite_param_limit(tmp_path):
    """More than _EXTRACT_ID_CHUNK ids must be fetched across chunks, not one
    oversized IN() that would blow SQLite's host-parameter limit."""
    n = repair._EXTRACT_ID_CHUNK * 2 + 7  # spans three chunks
    _seed(str(tmp_path), _ids(n), hnsw_ids=[])
    want = set(_ids(n))
    got = {eid for eid, _, _ in extract_via_sqlite(str(tmp_path), COLLECTION, only_ids=want)}
    assert got == want


def test_extract_full_scan_unchanged(tmp_path):
    """only_ids=None keeps the original whole-segment behavior."""
    _seed(str(tmp_path), _ids(4), hnsw_ids=_ids(1))
    got = {eid for eid, _, _ in extract_via_sqlite(str(tmp_path), COLLECTION)}
    assert got == set(_ids(4))


# ── reconcile_index orchestration (mocked backend) ────────────────────


# The 2% fraction cap is tuned for large palaces (the real case is 472/292_052
# = 0.16%). On a tiny test palace any gap is a large fraction, so the happy-path
# tests lift the fraction cap to isolate the diff→fetch→upsert mechanic; the cap
# itself has dedicated tests below.


@patch("mempalace.repair.ChromaBackend")
def test_reconcile_closes_gap_upserts_only_missing(mock_backend_cls, tmp_path):
    _seed(str(tmp_path), _ids(10), hnsw_ids=_ids(7))
    mock_col = MagicMock()
    mock_backend = _install_mock_backend(mock_backend_cls, mock_col)

    with patch.object(repair, "RECONCILE_MAX_MISSING_FRACTION", 1.0):
        result = reconcile_index(str(tmp_path), COLLECTION)

    assert result["missing"] == 3
    assert result["reconciled"] == 3
    assert result["fetched"] == 3
    mock_backend.get_collection.assert_called_once()
    # Exactly the missing ids were upserted (batched here into one call).
    upserted = set()
    for call in mock_col.upsert.call_args_list:
        upserted.update(call.kwargs["ids"])
    assert upserted == {"d-7", "d-8", "d-9"}


@patch("mempalace.repair.ChromaBackend")
def test_reconcile_batches_large_gap(mock_backend_cls, tmp_path):
    _seed(str(tmp_path), _ids(12), hnsw_ids=[])
    # 12 missing, batch_size 5 → 3 upsert calls (5 + 5 + 2).
    mock_col = MagicMock()
    _install_mock_backend(mock_backend_cls, mock_col)

    with patch.object(repair, "RECONCILE_MAX_MISSING_FRACTION", 1.0):
        result = reconcile_index(str(tmp_path), COLLECTION, batch_size=5)

    assert result["reconciled"] == 12
    assert mock_col.upsert.call_count == 3


@patch("mempalace.repair.ChromaBackend")
def test_reconcile_no_gap_is_noop(mock_backend_cls, tmp_path):
    _seed(str(tmp_path), _ids(5), hnsw_ids=_ids(5))
    result = reconcile_index(str(tmp_path), COLLECTION)
    assert result["missing"] == 0
    assert result["reconciled"] == 0
    mock_backend_cls.assert_not_called()  # never opened the live segment


@patch("mempalace.repair.ChromaBackend")
def test_reconcile_bails_when_pickle_absent(mock_backend_cls, tmp_path):
    _seed(str(tmp_path), _ids(5), hnsw_ids=None)
    result = reconcile_index(str(tmp_path), COLLECTION)
    assert result["reconciled"] == 0
    mock_backend_cls.assert_not_called()


@patch("mempalace.repair.ChromaBackend")
def test_reconcile_cap_trips_on_fraction(mock_backend_cls, tmp_path):
    # 10 missing of 100 = 10% > 2% fraction cap → refuse before opening.
    _seed(str(tmp_path), _ids(100), hnsw_ids=_ids(90))
    with pytest.raises(ReconcileNotSafe) as exc:
        reconcile_index(str(tmp_path), COLLECTION)
    assert exc.value.n_missing == 10
    assert exc.value.sqlite_count == 100
    mock_backend_cls.assert_not_called()


@patch("mempalace.repair.ChromaBackend")
def test_reconcile_cap_trips_on_absolute(mock_backend_cls, tmp_path):
    _seed(str(tmp_path), _ids(100), hnsw_ids=_ids(90))
    # Isolate the absolute cap: fraction lifted, absolute lowered to 3 < 10.
    with (
        patch.object(repair, "RECONCILE_MAX_MISSING_FRACTION", 1.0),
        patch.object(repair, "RECONCILE_MAX_MISSING", 3),
    ):
        with pytest.raises(ReconcileNotSafe):
            reconcile_index(str(tmp_path), COLLECTION)
    mock_backend_cls.assert_not_called()


@patch("mempalace.repair.ChromaBackend")
def test_reconcile_dry_run_makes_no_changes(mock_backend_cls, tmp_path):
    _seed(str(tmp_path), _ids(10), hnsw_ids=_ids(7))
    with patch.object(repair, "RECONCILE_MAX_MISSING_FRACTION", 1.0):
        result = reconcile_index(str(tmp_path), COLLECTION, dry_run=True)
    assert result["dry_run"] is True
    assert result["missing"] == 3
    assert result["reconciled"] == 0
    mock_backend_cls.assert_not_called()


@patch("mempalace.repair.ChromaBackend")
def test_reconcile_propagates_mine_already_running(mock_backend_cls, tmp_path):
    _seed(str(tmp_path), _ids(10), hnsw_ids=_ids(7))
    mock_col = MagicMock()
    mock_col.upsert.side_effect = MineAlreadyRunning(str(tmp_path))
    _install_mock_backend(mock_backend_cls, mock_col)

    with patch.object(repair, "RECONCILE_MAX_MISSING_FRACTION", 1.0):
        with pytest.raises(MineAlreadyRunning):
            reconcile_index(str(tmp_path), COLLECTION)


# ── writer lease across the reconcile transaction ─────────────────────
#
# Reconcile must hold ONE palace writer lease for the complete transaction —
# integrity preflight, missing-ID diff, sqlite extraction, every upsert batch,
# and the final verification. Any phase running outside the lease lets a
# concurrent writer move sqlite/HNSW between phases, so the reconcile set can
# describe an older generation than the state being modified.
#
# The foreign writer is simulated with an INDEPENDENT file handle on the lock
# file: Windows byte-range locks and POSIX flock are per-handle / per-open-
# file-description, so a second handle conflicts even from the same process —
# no subprocess needed.


def _palace_lock_path(palace: str) -> str:
    """Mirror mine_palace_lock's lock-file derivation for the test's palace."""
    resolved = os.path.realpath(os.path.expanduser(palace))
    key = hashlib.sha256(os.path.normcase(resolved).encode()).hexdigest()[:16]
    return os.path.join(os.path.expanduser("~"), ".mempalace", "locks", f"mine_palace_{key}.lock")


def _lock_byte0(lf) -> bool:
    """Try a non-blocking byte-0 lock on handle ``lf``; True when acquired."""
    lf.seek(0)
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(lf.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _unlock_byte0(lf) -> None:
    lf.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(lf.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(lf, fcntl.LOCK_UN)


def _open_lock_handle(lock_path):
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    if not os.path.exists(lock_path):
        fd = os.open(lock_path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
    return open(lock_path, "r+b")


@contextlib.contextmanager
def _foreign_writer_holding(lock_path):
    """Hold the palace lock the way another writer process would."""
    lf = _open_lock_handle(lock_path)
    try:
        assert _lock_byte0(lf), "test setup: foreign writer could not acquire"
        try:
            yield
        finally:
            _unlock_byte0(lf)
    finally:
        lf.close()


def _foreign_writer_refused(lock_path) -> bool:
    """True when a simulated foreign writer CANNOT acquire the palace lock."""
    if not os.path.exists(lock_path):
        return False  # no lock file → nothing is holding the palace
    lf = _open_lock_handle(lock_path)
    try:
        if _lock_byte0(lf):
            _unlock_byte0(lf)
            return False
        return True
    finally:
        lf.close()


def _spy_phases(monkeypatch, lock_path, seen):
    """Wrap each reconcile phase with a pass-through spy recording — at the
    moment the phase runs — whether a foreign writer is refused the lock."""
    real_integrity = repair.sqlite_integrity_errors

    def spy_integrity(*a, **k):
        seen.append(("preflight", _foreign_writer_refused(lock_path)))
        return real_integrity(*a, **k)

    monkeypatch.setattr(repair, "sqlite_integrity_errors", spy_integrity)

    real_diff = repair.compute_missing_ids

    def spy_diff(*a, **k):
        seen.append(("diff", _foreign_writer_refused(lock_path)))
        return real_diff(*a, **k)

    monkeypatch.setattr(repair, "compute_missing_ids", spy_diff)

    real_extract = repair.extract_via_sqlite

    def spy_extract(*a, **k):
        seen.append(("extract", _foreign_writer_refused(lock_path)))
        yield from real_extract(*a, **k)

    monkeypatch.setattr(repair, "extract_via_sqlite", spy_extract)

    real_verify = repair.hnsw_capacity_status

    def spy_verify(*a, **k):
        # Fires twice: once nested inside compute_missing_ids (which calls
        # hnsw_capacity_status as part of the diff) and once as the real
        # post-upsert verification. Both must run under the lease, so the
        # extra checkpoint only adds proof-points.
        seen.append(("verify", _foreign_writer_refused(lock_path)))
        return real_verify(*a, **k)

    monkeypatch.setattr(repair, "hnsw_capacity_status", spy_verify)


@patch("mempalace.repair.ChromaBackend")
def test_reconcile_holds_one_writer_lease_across_all_phases(
    mock_backend_cls, tmp_path, monkeypatch
):
    """Interleaving-writer regression: at EVERY phase boundary a concurrent
    writer must be refused the palace lock, and the lease must be free again
    once the transaction is over."""
    _seed(str(tmp_path), _ids(10), hnsw_ids=_ids(7))
    lock_path = _palace_lock_path(str(tmp_path))
    seen: list = []
    _spy_phases(monkeypatch, lock_path, seen)

    mock_col = MagicMock()
    mock_col.upsert.side_effect = lambda **kw: seen.append(
        ("upsert", _foreign_writer_refused(lock_path))
    )
    _install_mock_backend(mock_backend_cls, mock_col)

    with patch.object(repair, "RECONCILE_MAX_MISSING_FRACTION", 1.0):
        result = reconcile_index(str(tmp_path), COLLECTION)

    assert result["reconciled"] == 3
    assert {p for p, _ in seen} >= {"preflight", "diff", "extract", "upsert", "verify"}
    assert all(refused for _, refused in seen), f"writer interleaved at: {seen}"
    # Transaction over → the lease is released, a writer may acquire again.
    assert not _foreign_writer_refused(lock_path)


@patch("mempalace.repair.ChromaBackend")
def test_reconcile_nested_real_write_lock_reenters_under_the_lease(mock_backend_cls, tmp_path):
    """Exercise the REAL ``ChromaCollection._write_lock`` → ``mine_palace_lock``
    nesting under reconcile's outer lease: the nested acquisition must pass
    through re-entrantly (no self-deadlock, no ``MineAlreadyRunning``) while a
    foreign writer stays refused for the duration of the upsert. Only the
    innermost chromadb collection is mocked — the adapter's locking runs."""
    from mempalace.backends.chroma import ChromaCollection

    _seed(str(tmp_path), _ids(10), hnsw_ids=_ids(7))
    lock_path = _palace_lock_path(str(tmp_path))
    seen: list = []

    inner = MagicMock()  # the raw chromadb collection beneath the adapter
    inner.upsert.side_effect = lambda **kw: seen.append(
        ("nested-upsert", _foreign_writer_refused(lock_path))
    )
    real_col = ChromaCollection(inner, palace_path=str(tmp_path))
    _install_mock_backend(mock_backend_cls, real_col)

    with patch.object(repair, "RECONCILE_MAX_MISSING_FRACTION", 1.0):
        result = reconcile_index(str(tmp_path), COLLECTION)

    assert result["reconciled"] == 3
    assert seen == [("nested-upsert", True)]
    assert not _foreign_writer_refused(lock_path)


@patch("mempalace.repair.ChromaBackend")
def test_reconcile_dry_run_diff_runs_under_the_lease(mock_backend_cls, tmp_path, monkeypatch):
    """The dry-run report must come from a lease-consistent snapshot too."""
    _seed(str(tmp_path), _ids(10), hnsw_ids=_ids(7))
    lock_path = _palace_lock_path(str(tmp_path))
    seen: list = []
    _spy_phases(monkeypatch, lock_path, seen)

    with patch.object(repair, "RECONCILE_MAX_MISSING_FRACTION", 1.0):
        result = reconcile_index(str(tmp_path), COLLECTION, dry_run=True)

    assert result["dry_run"] is True
    assert ("diff", True) in seen
    assert not _foreign_writer_refused(lock_path)
    mock_backend_cls.assert_not_called()


@patch("mempalace.repair.ChromaBackend")
def test_reconcile_refuses_at_entry_when_a_writer_holds_the_palace(
    mock_backend_cls, tmp_path, monkeypatch
):
    """With a foreign writer holding the palace, reconcile must refuse BEFORE
    the preflight — no phase may read a palace another writer is moving."""
    _seed(str(tmp_path), _ids(10), hnsw_ids=_ids(7))
    called: list = []
    monkeypatch.setattr(
        repair, "sqlite_integrity_errors", lambda *a, **k: called.append("preflight") or []
    )

    with _foreign_writer_holding(_palace_lock_path(str(tmp_path))):
        with pytest.raises(MineAlreadyRunning):
            reconcile_index(str(tmp_path), COLLECTION)

    assert called == []
    mock_backend_cls.assert_not_called()


@patch("mempalace.repair.ChromaBackend")
def test_reconcile_releases_lease_when_a_phase_raises(mock_backend_cls, tmp_path):
    """A failing phase must not strand the palace lease."""
    _seed(str(tmp_path), _ids(10), hnsw_ids=_ids(7))
    mock_col = MagicMock()
    mock_col.upsert.side_effect = RuntimeError("upsert exploded")
    _install_mock_backend(mock_backend_cls, mock_col)

    with patch.object(repair, "RECONCILE_MAX_MISSING_FRACTION", 1.0):
        with pytest.raises(RuntimeError):
            reconcile_index(str(tmp_path), COLLECTION)

    lock_path = _palace_lock_path(str(tmp_path))
    # Non-vacuous: the lease WAS taken (lock file exists) and is free again.
    assert os.path.exists(lock_path)
    assert not _foreign_writer_refused(lock_path)
