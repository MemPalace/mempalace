"""Rank-first equivalence, access control and derived-index migration."""

import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from _thread_search_oracle import assert_alignment, old_search
from hypothesis import given, settings, strategies as st

from commonplace.handoff.models import ThreadMessage
from commonplace.handoff.thread_store import ThreadStore


@settings(max_examples=35, deadline=None)
@given(
    corpora=st.lists(
        st.lists(
            st.sampled_from(["alpha", "beta", "alpha beta", "gamma", "alpha alpha"]),
            min_size=1,
            max_size=5,
        ),
        min_size=1,
        max_size=7,
    ),
    data=st.data(),
    query=st.lists(
        st.sampled_from(["alpha", "beta", "gamma", "absent", '"alpha"']), min_size=1, max_size=3
    ).map(" ".join),
)
def test_rank_first_matches_original(corpora, query, data):
    tokens = iter(data.draw(st.permutations([f"{i:016x}" for i in range(len(corpora))])))
    with patch(
        "commonplace.handoff.thread_store.secrets.token_hex",
        side_effect=lambda _: next(tokens),
    ):
        directory = tempfile.TemporaryDirectory()
        path = str(Path(directory.name) / "threads.sqlite3")
        store = ThreadStore(path)
        ids = []
        messages = []
        for i, texts in enumerate(corpora):
            # Pad messages to make multiple chunks; repeated texts force ties.
            items = [
                ThreadMessage(j, "user", "message", text + " filler" * 300, raw="")
                for j, text in enumerate(texts)
            ]
            messages.append(items)
            ids.append(store.store("test", str(i), items, actor="test").id)
    try:
        drawn_scope = data.draw(st.lists(st.sampled_from(ids + ["missing"]), max_size=12))
        for appended in (False, True):
            if appended:
                store.append(
                    ids[0],
                    messages[0]
                    + [ThreadMessage(len(messages[0]), "user", "message", "alpha beta", raw="")],
                )
            expected = old_search(store, ids, query, 1000)
            assert_alignment(store)
            store.close()
            store = ThreadStore(path)
            assert_alignment(store)
            assert store.search(ids, query, 1000) == expected
            for scope in ([], ids[:1], ids[-1:], ids, ids[::2] + ["missing", ids[0]], drawn_scope):
                for limit in (0, 1, 3, 1000):
                    assert store.search(scope, query, limit) == old_search(
                        store, scope, query, limit
                    )
            # Force several stored-id batches without changing the corpus scores.
            if hasattr(store._conn, "setlimit"):
                previous = store._conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 4)
                try:
                    for limit in (1, 1000):
                        assert store.search(ids, query, limit) == old_search(
                            store, ids, query, limit
                        )
                finally:
                    store._conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, previous)
    finally:
        store.close()
        directory.cleanup()


def test_legacy_rowids_migrate_and_reopen_idempotently(tmp_path):
    path = str(tmp_path / "threads.sqlite3")
    store = ThreadStore(path)
    ids = [
        store.store(
            "test", str(i), [ThreadMessage(0, "user", "message", text, raw="")], actor="test"
        ).id
        for i, text in enumerate(["alpha beta", "alpha", "beta"])
    ]
    expected = old_search(store, ids, "alpha beta")
    # Simulate a legacy file with arbitrary FTS rowids, plus an unindexed chunk.
    store._conn.execute("UPDATE chunks_fts SET rowid=rowid+100 WHERE rowid=1")
    store._conn.execute("DELETE FROM chunks_fts WHERE rowid=2")
    store._conn.execute("PRAGMA user_version=0")
    original = [tuple(row) for row in store._conn.execute("SELECT * FROM chunks")]
    store.close()
    for _ in range(2):
        store = ThreadStore(path)
        try:
            assert store.search(ids, "alpha beta") == expected
            assert store.search(ids, "alpha beta") == old_search(store, ids, "alpha beta")
            assert_alignment(store)
            assert [tuple(row) for row in store._conn.execute("SELECT * FROM chunks")] == original
            assert store._conn.execute("SELECT count(*) FROM chunks_fts").fetchone()[0] == 3
            assert (
                store._conn.execute(
                    "SELECT count(*) FROM chunks_fts f JOIN chunks c ON f.rowid=c.rowid "
                    "WHERE f.thread_id=c.thread_id AND f.idx=c.idx AND f.text=c.text"
                ).fetchone()[0]
                == 3
            )
        finally:
            store.close()


def test_scope_excludes_global_top_hits():
    store = ThreadStore(":memory:")
    try:
        denied = store.store(
            "other",
            "denied",
            [ThreadMessage(0, "user", "message", "alpha " * 50, raw="")],
            actor="test",
        )
        allowed = store.store(
            "test",
            "allowed",
            [ThreadMessage(0, "user", "message", "alpha " + "filler " * 500, raw="")],
            actor="test",
        )
        assert store.search([denied.id, allowed.id], "alpha", 1)[0].stored_thread_id == denied.id
        assert store.search([allowed.id], "alpha", 1) == old_search(store, [allowed.id], "alpha", 1)
        assert store.search([allowed.id], "alpha", 1)[0].stored_thread_id == allowed.id
    finally:
        store.close()


@pytest.mark.skipif(not hasattr(sqlite3.Connection, "setlimit"), reason="Python 3.11+ API")
def test_ties_use_thread_and_chunk_order_across_id_batches():
    store = ThreadStore(":memory:")
    try:
        with patch(
            "commonplace.handoff.thread_store.secrets.token_hex",
            side_effect=[f"{i:016x}" for i in (7, 1, 5, 0, 6, 3, 2, 4)],
        ):
            ids = [
                store.store(
                    "test",
                    str(i),
                    [ThreadMessage(j, "user", "message", "alpha " * 600, raw="") for j in range(3)],
                    actor="test",
                ).id
                for i in range(8)
            ]
        ids.sort(reverse=True)
        expected = old_search(store, ids, "alpha", 100)
        assert len({hit.score for hit in expected}) == 1
        assert [(hit.stored_thread_id, hit.chunk_index) for hit in expected] == sorted(
            (hit.stored_thread_id, hit.chunk_index) for hit in expected
        )
        previous = store._conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 4)
        try:
            for limit in (1, 2, 7, 100):
                assert store.search(ids, "alpha", limit) == expected[:limit]
        finally:
            store._conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, previous)
    finally:
        store.close()


def test_aligned_legacy_migration_and_reopen_do_not_rewrite_index(tmp_path):
    path = str(tmp_path / "threads.sqlite3")
    store = ThreadStore(path)
    stored = store.store(
        "test", "original", [ThreadMessage(0, "user", "message", "alpha", raw="")], actor="test"
    )
    store._conn.execute("PRAGMA user_version=0")
    store.close()
    for _ in range(2):
        store = ThreadStore(path)
        try:
            assert store._conn.execute("PRAGMA user_version").fetchone()[0] == 1
            assert store._conn.total_changes == 0
            assert_alignment(store)
            assert store.search([stored.id], "alpha") == old_search(store, [stored.id], "alpha")
        finally:
            store.close()


def test_old_writer_after_fallback_repairs_alignment_even_at_version_one(tmp_path):
    path = str(tmp_path / "threads.sqlite3")
    original = ThreadStore(path)
    original.store(
        "test", "first", [ThreadMessage(0, "user", "message", "alpha", raw="")], actor="test"
    )
    # Model an old FTS writer already open while another writer lacks FTS5.
    fallback = ThreadStore(path)
    fallback._fts_enabled = False
    denied = fallback.store(
        "other", "denied", [ThreadMessage(0, "user", "message", "topsecret", raw="")], actor="test"
    )
    fallback.close()
    original._fts_enabled = False
    allowed = original.store(
        "test", "allowed", [ThreadMessage(0, "user", "message", "public", raw="")], actor="test"
    )
    # The old store path assigns its own FTS rowid, filling the fallback gap.
    original._conn.execute(
        "INSERT INTO chunks_fts(thread_id, idx, text) "
        "SELECT thread_id, idx, text FROM chunks WHERE thread_id=?",
        (allowed.id,),
    )
    original.close()
    # The old initializer backfills by identity and ignores user_version.
    with sqlite3.connect(path) as legacy:
        legacy.execute(
            "INSERT INTO chunks_fts(thread_id, idx, text) "
            "SELECT thread_id, idx, text FROM chunks WHERE (thread_id, idx) NOT IN "
            "(SELECT thread_id, idx FROM chunks_fts)"
        )
        assert legacy.execute("PRAGMA user_version").fetchone()[0] == 1
        assert (
            legacy.execute(
                "SELECT c.thread_id FROM chunks_fts f JOIN chunks c ON c.rowid=f.rowid "
                "WHERE f.text LIKE '%topsecret%'"
            ).fetchone()[0]
            == allowed.id
        )
    reopened = ThreadStore(path)
    try:
        assert_alignment(reopened)
        for scope in ([allowed.id], [denied.id], [allowed.id, denied.id]):
            for query in ("topsecret", "public", "alpha public"):
                assert reopened.search(scope, query) == old_search(reopened, scope, query)
        assert reopened.search([allowed.id], "topsecret") == []
    finally:
        reopened.close()


@pytest.mark.parametrize("query", ["alpha", "rare"])
def test_fts_identity_ranking_matches_original(query):
    store = ThreadStore(":memory:")
    try:
        ids = [
            store.store(
                "test",
                str(i),
                [
                    ThreadMessage(
                        0, "user", "message", "alpha rare" if i == 0 else "alpha filler", raw=""
                    )
                ],
                actor="test",
            ).id
            for i in range(10)
        ]
        statements = []
        store._conn.set_trace_callback(statements.append)
        hits = store.search(ids, query, 1)
        store._conn.set_trace_callback(None)
        assert hits == old_search(store, ids, query, 1)
        assert any(sql.startswith("WITH ranked AS MATERIALIZED") for sql in statements)
        assert_alignment(store)
    finally:
        store.close()


@pytest.mark.parametrize("first_search", ["false-positive", "false-negative"])
def test_gateway_stays_open_across_legacy_and_no_fts_writes(tmp_path, first_search):
    path = str(tmp_path / "threads.sqlite3")
    gateway = ThreadStore(path)
    fallback = ThreadStore(path)
    try:
        gateway.store(
            "test", "first", [ThreadMessage(0, "user", "message", "alpha", "raw")], actor="test"
        )
        fallback._fts_enabled = False
        allowed = fallback.store(
            "test", "allowed", [ThreadMessage(0, "user", "message", "public", "raw")], actor="test"
        )
        denied = fallback.store(
            "other", "denied", [ThreadMessage(0, "user", "message", "secret", "raw")], actor="test"
        )
        with sqlite3.connect(path) as legacy:
            legacy.execute(
                "INSERT INTO chunks_fts(thread_id, idx, text) "
                "SELECT thread_id, idx, text FROM chunks WHERE thread_id=?",
                (denied.id,),
            )
        gateway.store(
            "test", "neutral", [ThreadMessage(0, "user", "message", "neutral", "raw")], actor="test"
        )
        with sqlite3.connect(path) as legacy:
            legacy.execute(
                "INSERT INTO chunks_fts(thread_id, idx, text) "
                "SELECT thread_id, idx, text FROM chunks WHERE (thread_id, idx) NOT IN "
                "(SELECT thread_id, idx FROM chunks_fts)"
            )
        # A foreign logical identity currently indexes the allowed chunk rowid.
        assert (
            gateway._conn.execute("SELECT thread_id FROM chunks_fts WHERE rowid=2").fetchone()[0]
            == denied.id
        )
        with patch.object(gateway, "_repair_fts", wraps=gateway._repair_fts) as repair:
            if first_search == "false-positive":
                assert gateway.search([allowed.id], "secret") == []
            else:
                assert gateway.search([denied.id], "secret")
            assert repair.call_count == 1
            for scope in ([allowed.id], [denied.id], [allowed.id, denied.id]):
                for query in ("public", "secret", "alpha public"):
                    assert gateway.search(scope, query) == old_search(gateway, scope, query)
            assert repair.call_count == 1
        assert gateway.search([allowed.id], "public")
        assert gateway.search([denied.id], "secret")
        assert_alignment(gateway)
        # A later no-FTS commit must recover even with zero selected candidates.
        missing = fallback.store(
            "test",
            "missing",
            [ThreadMessage(0, "user", "message", "zeroentry", "raw")],
            actor="test",
        )
        assert old_search(gateway, [missing.id], "zeroentry") == []
        assert gateway.search([missing.id], "zeroentry") == old_search(
            gateway, [missing.id], "zeroentry"
        )
        assert gateway.search([missing.id], "zeroentry")
        assert_alignment(gateway)
    finally:
        fallback.close()
        gateway.close()


@pytest.mark.parametrize("readonly", [False, True])
def test_identity_fallback_reruns_full_scope_before_limit(tmp_path, readonly):
    from commonplace.handoff.thread_store_migrate import read_store

    path = tmp_path / "threads.sqlite3"
    store = ThreadStore(str(path))
    tokens = ["z", "a", "b", "c"]
    with patch("commonplace.handoff.thread_store.secrets.token_hex", side_effect=tokens):
        ids = [
            store.store(
                "test", str(i), [ThreadMessage(0, "user", "message", text, "raw")], actor="test"
            ).id
            for i, text in enumerate(["secret", "public", "secret", "filler " * 1500])
        ]
    # Same-connection changes do not change data_version: selected validation
    # must still reject the foreign identity, then rerank the entire scope.
    store._conn.execute("UPDATE chunks_fts SET rowid=100 WHERE rowid=1")
    store._conn.execute("UPDATE chunks_fts SET rowid=1 WHERE rowid=2")
    store._conn.execute("UPDATE chunks_fts SET rowid=2 WHERE rowid=100")
    scope = [ids[0], ids[2]]
    expected = old_search(store, scope, "secret", 2)
    assert len(expected) == 2 and expected[0].stored_thread_id == ids[2]
    if readonly:
        store.close()
        before = path.read_bytes()
        with read_store(path) as reader:
            statements = []
            reader._conn.set_trace_callback(statements.append)
            assert reader.search(scope, "secret", 2) == expected
            assert reader.search([ids[1]], "secret") == []
            assert reader._conn.total_changes == 0
            assert not hasattr(reader, "_fts_verified_version")
            assert not any(sql.startswith(("INSERT", "DELETE", "UPDATE")) for sql in statements)
        assert path.read_bytes() == before
    else:
        try:
            statements = []
            store._conn.set_trace_callback(statements.append)
            assert store.search(scope, "secret", 2) == expected
            assert any(sql.startswith("WITH ranked AS MATERIALIZED") for sql in statements)
            assert store._fts_verified_version is None
        finally:
            store.close()


@pytest.mark.skipif(not hasattr(sqlite3.Connection, "setlimit"), reason="Python 3.11+ API")
@pytest.mark.parametrize("query", ["alpha", "rare", "absent"])
def test_ranking_batches_need_no_global_match_probe(query):
    store = ThreadStore(":memory:")
    try:
        ids = []
        with patch(
            "commonplace.handoff.thread_store.secrets.token_hex", side_effect=["d", "b", "a", "c"]
        ):
            for i, count in enumerate([1, 3, 1, 3]):
                items = [
                    ThreadMessage(
                        j,
                        "user",
                        "message",
                        "alpha " * 400 + ("rare" if j == 0 and i != 2 else "filler"),
                        "raw",
                    )
                    for j in range(count)
                ]
                ids.append(store.store("test", str(i), items, actor="test").id)
        # Counts 2, 6, 0, independently permuted from insertion and lexical order.
        scope = [ids[2], ids[0], ids[3], ids[1], "missing"]
        store._conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 4)
        statements = []
        store._conn.set_trace_callback(statements.append)
        hits = store.search(scope, query, 3)
        store._conn.set_trace_callback(None)
        assert hits == old_search(store, scope, query, 3)
        probes = [sql for sql in statements if sql.startswith("SELECT count(*) FROM (SELECT 1")]
        assert probes == []
        ranked = [sql for sql in statements if sql.startswith("WITH ranked AS MATERIALIZED")]
        assert bool(ranked) == (query != "absent")
        statements.clear()
        store._conn.set_trace_callback(statements.append)
        assert store.search(["missing"], query) == []
        assert store.search([], query) == []
        store._conn.set_trace_callback(None)
        assert not any(sql.startswith("SELECT count(*) FROM (SELECT 1") for sql in statements)
    finally:
        store.close()
