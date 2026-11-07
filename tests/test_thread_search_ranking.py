"""Rank-before-fetch contract, deterministic ties and full result equivalence."""

import random
import re
import sqlite3

import pytest
from _thread_search_oracle import old_search

from commonplace.handoff.thread_store import ThreadStore


@pytest.fixture
def ranked_store(tmp_path):
    store = ThreadStore(str(tmp_path / "threads.sqlite3"))
    try:
        ids = ["thread-z", "thread-m", "thread-a", "thread-q", "thread-b"]
        with store._transaction():
            # Reverse both logical keys relative to rowid, including ties at LIMIT.
            for thread_id in ids:
                store._conn.execute(
                    "INSERT INTO threads VALUES (?, 'synthetic', ?, '', 12, 12, '', '', '', '')",
                    (thread_id, thread_id),
                )
                rows = [
                    (thread_id, idx, idx, idx, "common rare" if idx % 3 == 0 else "common filler")
                    for idx in reversed(range(12))
                ]
                store._conn.executemany("INSERT INTO chunks VALUES (?,?,?,?,?)", rows)
            store._repair_fts()
        yield store, ids
    finally:
        store.close()


@pytest.mark.parametrize("query", ["common", "rare"])
def test_fts_ranks_before_chunk_lookup(ranked_store, query):
    store, ids = ranked_store
    statements = []
    store._conn.set_trace_callback(statements.append)
    try:
        assert store.search(ids, query, 4) == old_search(store, ids, query, 4)
    finally:
        store._conn.set_trace_callback(None)
    ranking = [
        sql.split("SELECT c.thread_id", 1)[0] for sql in statements if sql.startswith("WITH ")
    ]
    assert ranking, "expected a materialized ranking query"
    assert all(not re.search(r"\bchunks\b", sql) for sql in ranking), (
        "ranking must limit FTS identities before looking up canonical chunks"
    )


@pytest.mark.parametrize("batched", [False, True])
@pytest.mark.parametrize("query,indices", [("common", [0, 1, 2, 3]), ("rare", [0, 3, 6, 9])])
def test_fts_ties_use_thread_id_then_chunk_index_before_limit(
    ranked_store, batched, query, indices
):
    store, ids = ranked_store
    if batched:
        if not hasattr(store._conn, "setlimit"):
            pytest.skip("Python 3.11+ limit API")
        store._conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 4)
    hits = store.search(ids, query, 4)
    assert len({hit.score for hit in hits}) == 1
    assert [(hit.stored_thread_id, hit.chunk_index) for hit in hits] == [
        (min(ids), idx) for idx in indices
    ]
    assert hits == old_search(store, ids, query, 4)


def test_fts_full_lists_equal_original_query_for_random_scopes(ranked_store):
    store, ids = ranked_store
    rng = random.Random(36)
    for i in range(200):
        if hasattr(store._conn, "setlimit"):
            store._conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 4 if i % 2 else 999)
        scope = rng.sample(ids, rng.randint(1, len(ids)))
        scope += [scope[0], "missing-thread"]
        query = rng.choice(["common", "rare", "filler", "rare filler", "absent", '" OR common'])
        limit = rng.choice([1, 4, 8, 19, 100])
        assert store.search(scope, query, limit) == old_search(store, scope, query, limit)
