"""Live-substrate conformance run for the pgvector backend (RFC 001).

Mirrors the fake-client arms of ``test_pgvector_backend.py`` against a real
PostgreSQL + pgvector server, plus live-only arms the in-memory fake cannot
exercise: the real ``<=>`` operator class, JSONB pushdown vs local-fallback
equivalence, multi-connection concurrent writers, and the advisory-lock
serialization of ``run_maintenance("reindex")``.

Gate: ``MEMPALACE_PGVECTOR_LIVE_DSN`` (a scratch database — every test creates
its own namespaced tables; never point this at a production palace).
"""

import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from _backend_conformance import assert_partition_isolation

from mempalace.backends import (
    BackendError,
    BackendMismatchError,
    CollectionNotInitializedError,
    DimensionMismatchError,
    PalaceRef,
)
from mempalace.backends.pgvector import PgVectorBackend

LIVE_DSN = os.environ.get("MEMPALACE_PGVECTOR_LIVE_DSN")

pytestmark = pytest.mark.skipif(
    not LIVE_DSN, reason="set MEMPALACE_PGVECTOR_LIVE_DSN (scratch DB) to run"
)


@pytest.fixture
def live(request, tmp_path):
    """Backend + collection on the live server, namespaced per test."""
    namespace = "conf_" + request.node.name.replace("[", "_").replace("]", "")[:40]
    backend = PgVectorBackend()
    created = []
    created_lock = threading.Lock()

    def make(path, name="drawers", create=True, ns=namespace, dsn=LIVE_DSN, backend_=None):
        b = backend_ or backend
        ref = PalaceRef(id=str(path), local_path=str(path), namespace=ns)
        col = b.get_collection(
            palace=ref, collection_name=name, create=create, options={"dsn": dsn, "namespace": ns}
        )
        # The concurrent tests call make() from worker threads; plain list
        # append is not guaranteed safe on every Python build.
        with created_lock:
            created.append(col)
        return col

    yield backend, make, namespace
    for col in created:
        try:
            col._client.drop_table(col._table)
        except Exception:
            pass
    backend.close()


def _seed(col):
    col.add(
        ids=["a", "b", "c"],
        documents=[
            "alpha backend note",
            "rareterm pgvector backend note",
            "frontend design note",
        ],
        metadatas=[
            {"wing": "project", "room": "backend", "rank": 1},
            {"wing": "project", "room": "backend", "rank": 3},
            {"wing": "project", "room": "frontend", "rank": 2},
        ],
        embeddings=[[1, 0], [0.9, 0.1], [0, 1]],
    )


def test_live_add_query_filters_lexical_and_marker(live, tmp_path):
    backend, make, _ns = live
    col = make(tmp_path)
    assert not os.path.isfile(tmp_path / "pgvector_backend.json")
    _seed(col)

    assert PgVectorBackend.detect(str(tmp_path))
    assert os.path.isfile(tmp_path / "pgvector_backend.json")
    assert col.count() == 3

    result = col.query(
        query_embeddings=[[1, 0]],
        n_results=3,
        where={"wing": "project"},
        include=["documents", "metadatas", "distances", "embeddings"],
    )
    # ORDER BY distance ASC is part of the query contract — assert the
    # exact ranking, not just membership.
    assert result.ids[0] == ["a", "b", "c"]
    assert result.embeddings[0][0] == pytest.approx([1.0, 0.0])

    hits = col.lexical_search(query="rareterm backend", n_results=2, where={"wing": "project"}).hits
    assert [hit.id for hit in hits] == ["b", "a"]


def test_live_requires_explicit_embeddings(live, tmp_path):
    _backend, make, _ns = live
    col = make(tmp_path)
    with pytest.raises(ValueError, match="explicit embeddings"):
        col.add(ids=["a"], documents=["no vector"], metadatas=[{}])


def test_live_dimension_mismatch(live, tmp_path):
    _backend, make, _ns = live
    col = make(tmp_path)
    col.upsert(ids=["a"], documents=["one"], metadatas=[{}], embeddings=[[1, 0]])
    with pytest.raises(DimensionMismatchError):
        col.upsert(ids=["b"], documents=["two"], metadatas=[{}], embeddings=[[1, 0, 0]])


def test_live_duplicate_ids_in_batch_rejected(live, tmp_path):
    _backend, make, _ns = live
    col = make(tmp_path)
    with pytest.raises(ValueError, match="unique"):
        col.add(
            ids=["a", "a"], documents=["x", "y"], metadatas=[{}, {}], embeddings=[[1, 0], [0, 1]]
        )


def test_live_complex_filters_pushdown_vs_local_fallback(live, tmp_path):
    """$or / $contains route to local fallback, equality/$gte push down to
    JSONB SQL — on the live server both paths must agree with the fake."""
    _backend, make, _ns = live
    col = make(tmp_path)
    col.add(
        ids=["a", "b", "c"],
        documents=["alpha", "beta", "gamma"],
        metadatas=[
            {"wing": "x", "rank": 1, "tags": "core,vector"},
            {"wing": "y", "rank": 3, "tags": "sqlite,exact"},
            {"wing": "z", "rank": 2, "tags": "old"},
        ],
        embeddings=[[1, 0], [0.9, 0.1], [0, 1]],
    )

    or_hits = col.get(where={"$or": [{"wing": "x"}, {"wing": "z"}]})
    assert set(or_hits.ids) == {"a", "c"}

    contains = col.get(where={"tags": {"$contains": "sqlite"}})
    assert contains.ids == ["b"]

    ranked = col.query(query_embeddings=[[1, 0]], n_results=3, where={"rank": {"$gte": 2}})
    assert ranked.ids[0] == ["b", "c"]

    eq_pushdown = col.get(where={"wing": "y"})
    assert eq_pushdown.ids == ["b"]


def test_live_marker_rejects_target_change(live, tmp_path):
    _backend, make, _ns = live
    col = make(tmp_path)
    col.upsert(ids=["a"], documents=["one"], metadatas=[{}], embeddings=[[1, 0]])

    backend2 = PgVectorBackend()
    palace = PalaceRef(id=str(tmp_path), local_path=str(tmp_path))
    try:
        with pytest.raises(BackendMismatchError):
            backend2.get_collection(
                palace=palace,
                collection_name="drawers",
                create=True,
                options={"dsn": "postgresql://other-host:5432/other"},
            )
    finally:
        backend2.close()


def test_live_marker_backend_mismatch(live, tmp_path):
    from mempalace.palace import resolve_backend_name

    _backend, make, _ns = live
    col = make(tmp_path)
    col.upsert(ids=["a"], documents=["one"], metadatas=[{}], embeddings=[[1, 0]])

    assert resolve_backend_name(str(tmp_path)) == "pgvector"
    with pytest.raises(BackendMismatchError):
        resolve_backend_name(str(tmp_path), explicit="qdrant")


def test_live_rejects_pure_remote_palace(live):
    backend = PgVectorBackend()
    palace = PalaceRef(id="tenant-remote", local_path=None, namespace="tenant-remote")
    try:
        with pytest.raises(BackendError, match="local palace path"):
            backend.get_collection(
                palace=palace, collection_name="drawers", create=True, options={"dsn": LIVE_DSN}
            )
    finally:
        backend.close()


def test_live_missing_table_after_marker_is_not_initialized(live, tmp_path):
    _backend, make, _ns = live
    col = make(tmp_path)
    col.upsert(ids=["a"], documents=["one"], metadatas=[{}], embeddings=[[1, 0]])
    col._client.drop_table(col._table)

    assert col.health().ok is False
    with pytest.raises(CollectionNotInitializedError):
        col.count()


def test_live_cross_palace_isolation_conformance(live, tmp_path):
    backend, make, _ns = live
    cols = [make(tmp_path / label) for label in ("alpha", "beta")]
    assert cols[0]._table != cols[1]._table
    assert_partition_isolation(backend, cols[0], cols[1], embedding=[1.0, 0.0])


def test_live_cross_namespace_isolation_conformance(live, tmp_path):
    """The cschnatz arm: same DSN, two namespaces, no leakage either way."""
    assert "supports_namespace_isolation" in PgVectorBackend.capabilities
    backend, make, ns = live
    col_a = make(tmp_path / "tenant-a", ns=f"{ns}_a")
    col_b = make(tmp_path / "tenant-b", ns=f"{ns}_b")
    assert col_a._table != col_b._table
    assert_partition_isolation(backend, col_a, col_b, embedding=[1.0, 0.0])


def test_live_cosine_operator_ranking_ground_truth(live, tmp_path):
    """The real ``<=>`` operator class must rank by cosine distance exactly
    as the fake's local math claims (our #1679 Q2-adjacent point: distance
    semantics should be a contract fact; here we verify the live operator)."""
    _backend, make, _ns = live
    col = make(tmp_path)
    col.add(
        ids=["same", "close", "orthogonal", "opposite"],
        documents=["d1", "d2", "d3", "d4"],
        metadatas=[{}, {}, {}, {}],
        embeddings=[[1, 0], [0.9, 0.1], [0, 1], [-1, 0]],
    )
    result = col.query(query_embeddings=[[1, 0]], n_results=4, include=["distances"])
    assert result.ids[0] == ["same", "close", "orthogonal", "opposite"]
    distances = result.distances[0]
    assert distances[0] == pytest.approx(0.0, abs=1e-6)
    assert distances[2] == pytest.approx(1.0, abs=1e-6)
    assert distances[3] == pytest.approx(2.0, abs=1e-6)


def test_live_concurrent_writers_distinct_connections(live, tmp_path):
    """8 backends (8 connections) upserting distinct rows into the same
    table concurrently — the multi-daemon-writer shape from production."""
    _backend, make, ns = live
    seed_col = make(tmp_path)
    seed_col.upsert(ids=["seed"], documents=["seed"], metadatas=[{}], embeddings=[[1, 0]])

    errors = []

    def writer(worker):
        backend = PgVectorBackend()
        # The marker file is already written by the seed step. upsert()
        # rewrites it on every call with a plain open("w"), so 8 backends
        # sharing one local_path would race on the same file — a test-design
        # artifact (and a known sharing-violation hazard on Windows), not
        # the contract under test here. Stub it for the concurrent phase.
        backend._write_marker = lambda *args, **kwargs: None
        try:
            col = make(tmp_path, backend_=backend)
            for i in range(25):
                col.upsert(
                    ids=[f"w{worker}-r{i}"],
                    documents=[f"row {i} from worker {worker}"],
                    metadatas=[{"worker": worker}],
                    embeddings=[[1.0, float(i) / 100]],
                )
        except Exception as exc:  # noqa: BLE001 - collected for the report
            errors.append(repr(exc))
        finally:
            backend.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(writer, range(8)))

    assert errors == [], f"concurrent writers raised: {errors[:3]}"
    assert seed_col.count() == 1 + 8 * 25


def test_live_reindex_advisory_lock_race(live, tmp_path):
    """Two connections racing run_maintenance('reindex') — the #1732
    advisory-lock behavior: exactly one 'ran', the loser learns
    'already_running' (or 'noop' after the winner finishes), nobody stacks
    a second ACCESS EXCLUSIVE build and nobody raises."""
    _backend, make, ns = live
    col = make(tmp_path)
    col.add(
        ids=[f"r{i}" for i in range(50)],
        documents=[f"doc {i}" for i in range(50)],
        metadatas=[{} for _ in range(50)],
        embeddings=[[1.0, float(i)] for i in range(50)],
    )
    assert col.maintenance_state()["vector_index"] is None

    barrier = threading.Barrier(2)
    statuses, errors = [], []

    def race():
        backend = PgVectorBackend()
        try:
            racer = make(tmp_path, backend_=backend)
            barrier.wait(timeout=10)
            result = racer.run_maintenance("reindex")
            statuses.append(result.status)
        except Exception as exc:  # noqa: BLE001 - collected for the report
            errors.append(repr(exc))
        finally:
            backend.close()

    threads = [threading.Thread(target=race) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert errors == [], f"reindex race raised: {errors}"
    # The index does not exist beforehand, so exactly one racer must win
    # the advisory lock and build it.
    assert statuses.count("ran") == 1
    assert all(s in {"ran", "already_running", "noop"} for s in statuses), statuses
    state = col.maintenance_state()
    assert state["vector_index"] == "hnsw"
    assert state["index_build_complete"] is True


def test_live_analyze_maintenance(live, tmp_path):
    _backend, make, _ns = live
    col = make(tmp_path)
    col.upsert(ids=["a"], documents=["one"], metadatas=[{}], embeddings=[[1, 0]])
    result = col.run_maintenance("analyze")
    assert result.status == "ran"
