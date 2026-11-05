"""Compact's trusted mirror policy keeps verbatim tool text without vectors."""

from types import SimpleNamespace

import pytest
from hypothesis import given, settings, strategies as st

from mempalace.backends.embedding_wrapper import EmbeddingCollection
from mempalace.stored_text import KEYWORD_ONLY, user_tool_arguments
from tests._commonplace_helpers import hub_fixture  # noqa: F401


@pytest.fixture(params=["compact", "sqlite_exact", "chroma"])
def writer(request, tmp_path, monkeypatch):
    from mempalace import mcp_server as server
    from mempalace.backends.chroma import ChromaBackend
    from mempalace.backends.compact import CompactBackend
    from mempalace.backends.sqlite_exact import SQLiteExactBackend

    backend = {
        "compact": CompactBackend,
        "sqlite_exact": SQLiteExactBackend,
        "chroma": ChromaBackend,
    }[request.param]()
    col = EmbeddingCollection(backend.create_collection(str(tmp_path), "drawers"))
    embedded = []

    def embed(texts, **kwargs):
        embedded.extend(texts)
        return [[1.0, 0.0] for _ in texts]

    monkeypatch.setattr("mempalace.backends.embedding_wrapper._embed_texts", embed)
    monkeypatch.setattr(
        server,
        "_config",
        SimpleNamespace(chunk_size=800, palace_path=str(tmp_path), collection_name="drawers"),
    )
    monkeypatch.setattr(server, "_is_chroma_backend", lambda: False)
    monkeypatch.setattr(server, "_palace_known_wings", lambda: frozenset({"private_threads"}))
    monkeypatch.setattr(server, "_get_collection", lambda **kwargs: col)
    monkeypatch.setattr(server, "_wal_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "_wal_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "_invalidate_overview_caches", lambda: None)
    try:
        yield server, col, embedded, request.param
    finally:
        backend.close()


def test_lexical_only_writer_preserves_splits_and_does_not_retrofit(writer):
    server, col, embedded, backend = writer
    text = "ZXQ739251 exact tool output\n" * 70
    units = [
        {"seq_start": 0, "seq_end": 0, "message_class": "tool_output", "part": 0, "text": text}
    ]
    result = server.write_mirror_units("logs", units)
    assert result["success"]
    records = col.get(include=["documents", "metadatas", "embeddings"])
    assert len(records.ids) > 1
    assert "".join(records.documents) == text
    if backend == "compact":
        assert embedded == []
        assert all(meta[KEYWORD_ONLY] is True for meta in records.metadatas)
        assert all(vector is None for vector in records.embeddings)
    else:
        assert len(embedded) == len(records.ids)
        assert all(KEYWORD_ONLY not in meta for meta in records.metadatas)
        assert all(vector is not None for vector in records.embeddings)

    normal = server.tool_add_drawer("chat", "logs", "Existing conversation")
    count = len(embedded)
    retry = server.tool_add_drawer(
        **user_tool_arguments(
            {
                "wing": "chat",
                "room": "logs",
                "content": "Existing conversation",
                "lexical_only": True,
            },
            tool_name="mempalace_add_drawer",
        )
    )
    assert retry["drawer_id"] == normal["drawer_id"]
    assert retry["reason"] == "already_exists" and len(embedded) == count
    old = col.get(ids=[normal["drawer_id"]], include=["metadatas", "embeddings"])
    assert KEYWORD_ONLY not in old.metadatas[0]
    assert old.embeddings[0] is not None


def test_untrusted_drawer_arguments_cannot_choose_lexical_only():
    arguments = {"wing": "chat", "room": "logs", "content": "text", "lexical_only": True}
    assert user_tool_arguments(arguments, tool_name="mempalace_add_drawer") == {
        "wing": "chat",
        "room": "logs",
        "content": "text",
    }
    assert arguments["lexical_only"] is True


def test_keyword_only_rank_uses_bm25_without_changing_outside_pool_hits():
    from mempalace.searcher import _hybrid_rank

    hits = [
        {"drawer_id": "vector", "text": "unrelated prose", "distance": 0.0},
        {"drawer_id": "outside", "text": "ZXQ739251", "distance": None},
        {
            "drawer_id": "keyword",
            "text": "ZXQ739251",
            "distance": None,
            "_index_metadata": {KEYWORD_ONLY: True},
        },
    ]
    _hybrid_rank(hits, "ZXQ739251")
    assert [hit["drawer_id"] for hit in hits] == ["keyword", "vector", "outside"]
    assert [hit["score"] for hit in hits] == [1.0, 0.6, 0.4]
    assert hits[0]["rank_components"]["bm25_weight"] == 1.0
    assert hits[0]["rank_components"]["vector_weight"] == 0.0


def test_class_units_group_consecutive_classes_and_keep_exact_ranges():
    from commonplace.handoff.models import ThreadMessage
    from commonplace.handoff.palace_mirror import class_units

    messages = [
        ThreadMessage(i, role, kind, f"verbatim {i}", f"raw {i}")
        for i, (role, kind) in enumerate(
            [
                ("user", "message"),
                ("assistant", "message"),
                ("assistant", "other"),
                ("assistant", "tool_call"),
                ("assistant", "tool_call"),
                ("tool", "tool_result"),
                ("system", "system"),
                ("user", "message"),
            ]
        )
    ]
    units = class_units(messages)
    assert [(u.message_class, u.seq_start, u.seq_end) for u in units] == [
        ("conversation", 0, 2),
        ("tool_call", 3, 4),
        ("tool_output", 5, 5),
        ("system", 6, 6),
        ("conversation", 7, 7),
    ]
    assert all(len(u.text) <= 4000 for u in units)
    assert units[0].text == "\n".join(
        f"[{m.seq}] {m.role} {m.kind}: {m.text}" for m in messages[:3]
    )


@pytest.mark.parametrize("payload_bytes", [300, 1950, 4500])
def test_whole_message_hints_cross_mirror_and_physical_boundaries(writer, payload_bytes):
    import base64
    import random

    from commonplace.handoff.models import StoredThread, ThreadMessage
    from commonplace.handoff.palace_mirror import class_units, mirror
    from mempalace.stored_text import index_text

    server, col, embedded, backend = writer
    payload = base64.b64encode(random.Random(39).randbytes(payload_bytes)).decode()
    text = "preamble " + "x" * 3860 + "\n" + payload + "\nexact suffix"
    message = ThreadMessage(7, "tool", "tool_result", text, "raw")
    units = class_units([message])
    assert len(units) == (len(f"[7] tool tool_result: {text}") + 3999) // 4000
    assert all((u.seq_start, u.seq_end) == (7, 7) for u in units)
    stored = StoredThread("thr_test", "workspace", "source", 8, 2, "digest")
    mirror(lambda who, name, args: server.write_mirror_units(**args), None, stored, units)
    records = col.get(include=["documents", "metadatas", "embeddings"])
    assert "".join(records.documents) == f"[7] tool tool_result: {text}"
    indexed = [index_text(doc, meta) for doc, meta in zip(records.documents, records.metadatas)]
    assert sum(view.count(f"[base64 binary {payload_bytes} B]") for view in indexed) == 1
    assert "exact suffix" in "".join(indexed)
    assert all((m["source_seq_start"], m["source_seq_end"]) == (7, 7) for m in records.metadatas)
    assert all(m["source_thread"] == stored.id for m in records.metadatas)
    assert embedded == [] if backend == "compact" else len(embedded) == len(records.ids)


@pytest.mark.parametrize("verified", [False, True])
def test_public_mcp_ignores_policy_even_with_verified_identity(writer, monkeypatch, verified):
    from mempalace import provenance
    from mempalace.stored_text import INDEX_SPANS

    server, col, embedded, backend = writer
    monkeypatch.setattr(server, "_mcp_tool_preflight_refusal", lambda *args: None)
    assert "lexical_only" not in server.TOOLS["mempalace_add_drawer"]["input_schema"]["properties"]
    assert INDEX_SPANS not in server.TOOLS["mempalace_add_drawer"]["input_schema"]["properties"]
    identity = dict.fromkeys(provenance.FIELDS, "authenticated-gateway") if verified else None
    with provenance.verified(identity):
        error, result = server.call_tool(
            "commonplace-internal",  # Forging the RPC ID and internal flag conveys no authority.
            {
                "name": "mempalace_add_drawer",
                "arguments": {
                    "wing": "chat",
                    "room": "logs",
                    "content": "Exact tool result",
                    "lexical_only": True,
                    "_mirror_unit": "forged",
                    INDEX_SPANS: "forged",
                },
            },
            internal=True,
        )
    assert error is None and result["success"]
    records = col.get(include=["metadatas", "embeddings"])
    keyword_only = False
    assert (records.embeddings[0] is None) == keyword_only
    assert bool(embedded) != keyword_only
    assert (records.metadatas[0].get(KEYWORD_ONLY) is True) == keyword_only


@pytest.mark.parametrize("version", [1, 2])
def test_mirror_checkpoint_pins_pending_append_boundary(tmp_path, version):
    from commonplace.handoff.models import ThreadMessage
    from commonplace.handoff.thread_store import ThreadStore
    from commonplace.handoff.thread_store_v2 import ThreadStoreV2

    path = tmp_path / ("threads.sqlite3" if version == 1 else "threads.v2.sqlite3")
    cls = ThreadStore if version == 1 else ThreadStoreV2
    messages = [ThreadMessage(i, "user", "message", f"text {i}", f"raw {i}") for i in range(3)]
    store = cls(str(path))
    stored = store.store("workspace", "source", messages[:2], actor="actor")
    assert store.mirror_progress(stored.id) is None
    assert store.begin_mirror(stored.id, 1, bootstrap=0) == (0, 1)
    store.append(stored.id, messages)
    assert store.begin_mirror(stored.id, 2) == (0, 1)
    store.close()
    store = cls(str(path))
    try:
        assert store.mirror_progress(stored.id) == (0, 1)
        store.finish_mirror(stored.id, 2)  # A different snapshot cannot acknowledge the batch.
        assert store.mirror_progress(stored.id) == (0, 1)
        store.finish_mirror(stored.id, 1)
        assert store.begin_mirror(stored.id, 2) == (1, 2)
        store.finish_mirror(stored.id, 2)
        assert store.mirror_progress(stored.id) == (2, 2)
        assert store._conn.execute("SELECT count(*) FROM mirror_progress").fetchone()[0] == 1
    finally:
        store.close()


def test_v2_upgrades_progress_table_without_rewriting_threads(tmp_path):
    from commonplace.handoff.models import ThreadMessage
    from commonplace.handoff.thread_store_v2 import ThreadStoreV2

    path = tmp_path / "threads.v2.sqlite3"
    store = ThreadStoreV2(str(path))
    stored = store.store(
        "workspace", "source", [ThreadMessage(0, "user", "message", "text", "raw")], actor="actor"
    )
    tables = [
        row[0]
        for row in store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name!='mirror_progress'"
        )
    ]
    before = {
        name: [tuple(row) for row in store._conn.execute(f'SELECT * FROM "{name}"')]
        for name in tables
    }
    store._conn.execute("DROP TABLE mirror_progress")
    store._conn.execute("PRAGMA user_version=2")
    store.close()
    store = ThreadStoreV2(str(path))
    try:
        assert store._conn.execute("PRAGMA user_version").fetchone()[0] == 3
        assert store.mirror_progress(stored.id) is None
        for name in tables:
            assert [tuple(row) for row in store._conn.execute(f'SELECT * FROM "{name}"')] == before[
                name
            ]
    finally:
        store.close()


@pytest.mark.parametrize("version", [1, 2])
def test_legacy_ranges_stay_untouched_new_classes_append_once(
    writer, tmp_path, monkeypatch, version
):
    from commonplace.handoff.models import ThreadMessage
    from commonplace.handoff.palace_mirror import mirror, mirror_pending
    from commonplace.handoff.thread_store import ThreadStore, chunk_messages
    from commonplace.handoff.thread_store_v2 import ThreadStoreV2
    from mempalace import provenance

    server, col, _, backend = writer
    cls = ThreadStore if version == 1 else ThreadStoreV2
    store = cls(str(tmp_path / ("threads.sqlite3" if version == 1 else "threads.v2.sqlite3")))
    messages = [
        ThreadMessage(i, role, kind, text, text)
        for i, (role, kind, text) in enumerate(
            [
                ("user", "message", "Old request"),
                ("tool", "tool_result", "Old mixed output"),
                ("tool", "tool_result", "ZXQ739251 exact output"),
                ("system", "system", "System record"),
                ("assistant", "message", "New conversation"),
            ]
        )
    ]
    identity = dict.fromkeys(provenance.FIELDS, "gateway")
    calls = []

    def call(principal, name, args):
        calls.append((name, args.copy()))
        with provenance.verified(identity):
            return (
                server.write_mirror_units(**args)
                if name == "write_mirror_units"
                else server.TOOLS[name]["handler"](**args)
            )

    try:
        stored = store.store("workspace", "source", messages[:2], actor="actor")
        old_ids = mirror(call, None, stored, chunk_messages(messages[:2]))
        old = col.get(include=["documents", "metadatas"])
        stored = store.append(stored.id, messages)
        new_ids = mirror_pending(call, None, stored, store)
        assert len(new_ids) == 3
        assert store.mirror_progress(stored.id) == (4, 4)
        assert col.get(ids=old.ids, include=["documents", "metadatas"]) == old
        assert old_ids[0] not in new_ids
        new = col.get(ids=new_ids, include=["metadatas", "embeddings"])
        assert [meta["source_seq_start"] for meta in new.metadatas] == [2, 3, 4]
        assert all(
            meta["source_file"].startswith(f"handoff:{stored.id}#v2:") for meta in new.metadatas
        )
        assert [vector is None for vector in new.embeddings] == (
            [True, True, False] if backend == "compact" else [False, False, False]
        )
        count = len(calls)
        assert mirror_pending(call, None, stored, store) == []
        assert len(calls) == count
        assert col.count() == len(old.ids) + len(new_ids)
        from commonplace.handoff.palace_mirror import palace_search
        from commonplace.handoff.recall import Recall
        from mempalace import searcher

        monkeypatch.setattr(searcher, "get_collection", lambda *args, **kwargs: col)
        monkeypatch.setattr(searcher, "get_closets_collection", lambda *args, **kwargs: None)
        principal = SimpleNamespace(
            workspace_id="workspace",
            policy=SimpleNamespace(
                has_scope=lambda scope: True,
                can_read_wing=lambda wing: True,
            ),
        )
        hits = palace_search(call, principal, [stored.id], "ZXQ739251", 10)
        assert (hits[0].seq_start, hits[0].seq_end) == (2, 2)
        recall = Recall(lambda workspace: store, call_tool=call)
        merged = recall.recall(principal, stored.id, "ZXQ739251", limit=10)
        assert any(hit.seq_start <= 2 <= hit.seq_end for hit in merged)
        assert all(
            a.seq_end < b.seq_start or b.seq_end < a.seq_start
            for i, a in enumerate(merged)
            for b in merged[i + 1 :]
        )
        assert recall.get(principal, stored.id, 2, 2) == [messages[2]]
    finally:
        store.close()


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("append", [False, True], ids=["no-append", "append"])
def test_legacy_partial_message_replays_its_missing_tail(
    writer, tmp_path, monkeypatch, version, append
):
    from commonplace.handoff.models import ThreadMessage
    from commonplace.handoff.palace_mirror import mirror, mirror_pending, palace_search
    from commonplace.handoff.recall import Recall
    from commonplace.handoff.thread_store import ThreadStore, chunk_messages
    from commonplace.handoff.thread_store_v2 import ThreadStoreV2
    from mempalace import provenance, searcher

    server, col, _, _ = writer
    cls = ThreadStore if version == 1 else ThreadStoreV2
    path = tmp_path / ("threads.sqlite3" if version == 1 else "threads.v2.sqlite3")
    store = cls(str(path))
    messages = [
        ThreadMessage(0, "user", "message", "Complete earlier request", "raw"),
        ThreadMessage(1, "tool", "tool_result", "routine output\n" * 700 + "TAIL_ZXQ739251", "raw"),
        ThreadMessage(2, "user", "message", "Appended request", "raw"),
    ]

    def call(principal, name, args):
        with provenance.verified(dict.fromkeys(provenance.FIELDS, "gateway")):
            return (
                server.write_mirror_units(**args)
                if name == "write_mirror_units"
                else server.TOOLS[name]["handler"](**args)
            )

    try:
        stored = store.store("workspace", "source", messages[:2], actor="actor")
        legacy = chunk_messages(messages[:2])
        assert len(legacy) > 2
        mirror(call, None, stored, legacy[:2])
        old = col.get(include=["documents", "metadatas"])
        assert all("TAIL_ZXQ739251" not in text for text in old.documents)
        if append:
            stored = store.append(stored.id, messages)
        ids = mirror_pending(call, None, stored, store)
        end = stored.message_count - 1
        assert store.mirror_progress(stored.id) == (end, end)
        assert col.get(ids=old.ids, include=["documents", "metadatas"]) == old
        new = col.get(include=["metadatas"])
        assert all(
            meta["source_seq_start"] >= 1 for meta in new.metadatas if "#v2:" in meta["source_file"]
        )
        monkeypatch.setattr(searcher, "get_collection", lambda *args, **kwargs: col)
        monkeypatch.setattr(searcher, "get_closets_collection", lambda *args, **kwargs: None)
        hits = palace_search(call, None, [stored.id], "TAIL_ZXQ739251", 10)
        assert hits and "TAIL_ZXQ739251" in hits[0].text
        assert (hits[0].seq_start, hits[0].seq_end) == (1, 1)
        earlier = palace_search(call, None, [stored.id], "earlier", 10)
        assert any(
            (hit.seq_start, hit.seq_end, hit.text) == (0, 0, legacy[0].text) for hit in earlier
        )
        principal = SimpleNamespace(
            workspace_id="workspace",
            policy=SimpleNamespace(has_scope=lambda scope: True, can_read_wing=lambda wing: True),
        )
        recall = Recall(lambda workspace: store, call_tool=call)
        hits = recall.recall(principal, stored.id, "TAIL_ZXQ739251", limit=10)
        assert len([hit for hit in hits if hit.seq_start <= 1 <= hit.seq_end]) == 1
        assert recall.get(principal, stored.id, 1, 1) == [messages[1]]
        count = col.count()
        assert ids and mirror_pending(call, None, stored, store) == []
        assert col.count() == count
    finally:
        store.close()


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize(
    "append,physical_rows,source",
    [
        pytest.param(False, False, "palace", id="no-append"),
        pytest.param(True, False, "palace", id="append-palace"),
        pytest.param(True, False, "recall", id="append-recall"),
        pytest.param(False, True, "palace", id="physical-rows"),
    ],
)
def test_legacy_unsplit_final_keeps_earlier_matching_messages(
    writer, tmp_path, monkeypatch, version, append, physical_rows, source
):
    from commonplace.handoff.models import ThreadMessage
    from commonplace.handoff.palace_mirror import mirror, mirror_pending, palace_search
    from commonplace.handoff.recall import Recall
    from commonplace.handoff.thread_store import ThreadStore, chunk_messages
    from commonplace.handoff.thread_store_v2 import ThreadStoreV2
    from mempalace import provenance, searcher

    server, col, _, _ = writer
    cls = ThreadStore if version == 1 else ThreadStoreV2
    path = tmp_path / ("threads.sqlite3" if version == 1 else "threads.v2.sqlite3")
    store = cls(str(path))
    final = (
        "zephyr " + "filler " * 350 + "TAIL_ZXQ739251" if physical_rows else "zephyr zephyr gamma"
    )
    messages = [
        ThreadMessage(i, "user", "message", text, text)
        for i, text in enumerate(["alpha zephyr note", "beta plain message", final])
    ]

    def call(principal, name, args):
        with provenance.verified(dict.fromkeys(provenance.FIELDS, "gateway")):
            return (
                server.write_mirror_units(**args)
                if name == "write_mirror_units"
                else server.TOOLS[name]["handler"](**args)
            )

    monkeypatch.setattr(searcher, "get_collection", lambda *args, **kwargs: col)
    monkeypatch.setattr(searcher, "get_closets_collection", lambda *args, **kwargs: None)
    principal = SimpleNamespace(
        workspace_id="workspace",
        policy=SimpleNamespace(has_scope=lambda scope: True, can_read_wing=lambda wing: True),
    )
    try:
        initial = messages[:2] if append else messages
        stored = store.store("workspace", "source", initial, actor="actor")
        legacy = chunk_messages(initial)
        assert len(legacy) == 1
        mirror(call, principal, stored, legacy)
        old = col.get(include=["documents", "metadatas"])
        if physical_rows:
            assert len(final) > 3 * server._config.chunk_size
            assert len(old.ids) == (len(legacy[0].text) + 799) // 800 >= 4
        if append:
            stored = store.append(stored.id, messages)
        ids = mirror_pending(call, principal, stored, store)
        assert store.mirror_progress(stored.id) == (2, 2)
        assert col.get(ids=old.ids, include=["documents", "metadatas"]) == old
        recall = Recall(lambda workspace: store, call_tool=call)
        query = "TAIL_ZXQ739251" if physical_rows else "zephyr"
        hits = (
            recall.recall(principal, stored.id, query, limit=10)
            if source == "recall"
            else palace_search(call, principal, [stored.id], query, 10)
        )
        expected = {(0, 1), (2, 2)} if append else {(0, 2)}
        assert {(hit.seq_start, hit.seq_end) for hit in hits} == expected
        assert any(hit.text == legacy[0].text for hit in hits)
        assert recall.get(principal, stored.id, 0, 2) == messages
        if append:
            assert len(ids) == 1
        else:
            assert ids == []
        assert col.count() == len(old.ids) + len(ids)
        assert mirror_pending(call, principal, stored, store) == []
    finally:
        store.close()


@pytest.mark.parametrize(
    "case,end,expected",
    [
        ("fresh", -1, -1),
        ("unsplit", 2, 2),
        ("split", 2, 1),
        ("split", 0, -1),
        ("missing", 2, 1),
        ("wrong-sequence", 2, 1),
        ("multiple", 2, 1),
    ],
)
def test_bootstrap_seq_reads_only_the_final_legacy_message(case, end, expected):
    from unittest.mock import Mock

    from commonplace.handoff.models import ThreadMessage
    from commonplace.handoff.palace_mirror import _bootstrap_seq

    message = ThreadMessage(
        end + (case == "wrong-sequence"),
        "user",
        "message",
        "x" * (4001 if case == "split" else 2500),
        "raw",
    )
    found = [] if case == "missing" else [message] * (2 if case == "multiple" else 1)
    store = SimpleNamespace(messages=Mock(return_value=found))
    stored = SimpleNamespace(id="thr_legacy")
    assert _bootstrap_seq(store, stored, end) == expected
    if end < 0:
        store.messages.assert_not_called()
    else:
        store.messages.assert_called_once_with(stored.id, end, end)


@settings(max_examples=100, deadline=None, derandomize=True)
@given(
    specifications=st.lists(
        st.tuples(
            st.sampled_from([("user", "message"), ("tool", "tool_result"), ("system", "system")]),
            st.text(alphabet="abc", max_size=20),
            st.one_of(
                st.integers(min_value=0, max_value=12000),
                st.sampled_from([0, 1, 3970, 3980, 3999, 4000, 4001, 8000]),
            ),
        ),
        min_size=1,
        max_size=6,
    ),
    first=st.integers(min_value=0, max_value=1000),
)
def test_legacy_message_splits_alone_iff_it_spans_multiple_batch_chunks(specifications, first):
    from commonplace.handoff.models import CHUNK_MAX_CHARS, ThreadMessage
    from commonplace.handoff.thread_store import chunk_messages

    assert CHUNK_MAX_CHARS == 4000
    messages = [
        ThreadMessage(first + i, role, kind, ("x \n" * ((size + 2) // 3))[:size], "raw", name=name)
        for i, ((role, kind), name, size) in enumerate(specifications)
    ]
    chunks = chunk_messages(messages)
    for message in messages:
        appearances = sum(chunk.seq_start <= message.seq <= chunk.seq_end for chunk in chunks)
        assert (len(chunk_messages([message])) > 1) == (appearances >= 2)


def test_internal_writer_deduplicates_parts_but_keeps_distinct_ranges(writer, monkeypatch):
    server, col, embedded, _ = writer
    units = [
        {
            "seq_start": 2,
            "seq_end": 2,
            "message_class": "tool_output",
            "part": part,
            "text": "identical continuation",
        }
        for part in range(2)
    ]
    units.append({**units[0], "seq_start": 3, "seq_end": 3})
    writes = []
    original = server._write_drawer

    def write(**arguments):
        writes.append(arguments["drawer_id"])
        return original(**arguments)

    monkeypatch.setattr(server, "_write_drawer", write)
    first = server.write_mirror_units("thr_parts", units)
    assert first["success"] and len(set(first["drawer_ids"])) == 2
    assert first["drawer_ids"][0] == first["drawer_ids"][1]
    assert first["drawer_ids"][0] != first["drawer_ids"][2]
    assert writes == [first["drawer_ids"][0], first["drawer_ids"][2]]
    count = len(embedded)
    assert server.write_mirror_units("thr_parts", units) == first
    assert len(embedded) == count
    assert col.count() == 2


@pytest.mark.parametrize("size_mib,old_count,new_count", [(1, 7, 11), (20, 8, 15)])
def test_repeated_message_deduplicates_before_transport_and_embedding(
    writer, size_mib, old_count, new_count
):
    from commonplace.handoff.models import StoredThread, ThreadMessage
    from commonplace.handoff.palace_mirror import class_units, mirror
    from commonplace.handoff.thread_store import chunk_messages

    server, col, embedded, _ = writer
    message = ThreadMessage(0, "user", "message", "x" * (size_mib * 1024 * 1024), "raw")

    def legacy_call(principal, name, arguments):
        return server.TOOLS[name]["handler"](**arguments)

    old = StoredThread("thr_old", "workspace", "source", 1, 1, "digest")
    mirror(legacy_call, None, old, chunk_messages([message]))
    assert col.count() == len(embedded) == old_count
    count = len(embedded)
    transported = []

    def call(principal, name, arguments):
        assert name == "write_mirror_units"
        transported.extend(arguments["units"])
        return server.write_mirror_units(**arguments)

    stored = StoredThread("thr_new", "workspace", "source", 1, 1, "digest")
    units = class_units([message])
    ids = mirror(call, None, stored, units)
    assert len(ids) == len(units)
    assert len(set(ids)) == len(transported) == 3
    assert col.count() - old_count == len(embedded) - count == new_count
    assert len({u["text"] for u in transported}) == 3
    count = len(embedded)
    assert mirror(call, None, stored, units) == ids
    assert len(embedded) == count and col.count() == old_count + new_count


@pytest.mark.parametrize("verified", [False, True])
def test_http_internal_operation_requires_real_verified_channel(writer, monkeypatch, verified):
    import json

    from mempalace import provenance

    server, col, embedded, backend = writer
    monkeypatch.setattr(server, "_mcp_tool_preflight_refusal", lambda *args: None)
    assert "write_mirror_units" not in server.TOOLS
    identity = dict.fromkeys(provenance.FIELDS, "authenticated-gateway") if verified else None
    with provenance.verified(identity):
        result = server._http_dispatch(
            {
                "jsonrpc": "2.0",
                "id": "commonplace-internal",
                "method": "internal/write_mirror_units",
                "params": {
                    "thread_id": "thr_parts",
                    "units": [
                        {
                            "seq_start": 0,
                            "seq_end": 0,
                            "message_class": "tool_output",
                            "part": 0,
                            "text": "Exact tool output",
                        }
                    ],
                },
            }
        )
    if not verified:
        assert result["error"]["code"] == -32601
        assert col.count() == 0 and embedded == []
    else:
        assert json.loads(result["result"]["content"][0]["text"])["success"]
        records = col.get(include=["embeddings"])
        assert (records.embeddings[0] is None) == (backend == "compact")


@pytest.mark.parametrize("version", [1, 2])
def test_crash_after_first_long_message_unit_resumes_without_regrouping(
    writer, tmp_path, monkeypatch, version
):
    from commonplace.handoff.models import ThreadMessage
    from commonplace.handoff.palace_mirror import MirrorError, class_units, mirror_pending
    from commonplace.handoff.thread_store import ThreadStore
    from commonplace.handoff.thread_store_v2 import ThreadStoreV2

    server, col, _, _ = writer
    cls = ThreadStore if version == 1 else ThreadStoreV2
    path = tmp_path / ("threads.sqlite3" if version == 1 else "threads.v2.sqlite3")
    store = cls(str(path))
    messages = [ThreadMessage(0, "user", "message", "x" * 12000, "raw")]
    stored = store.store("workspace", "source", messages, actor="actor")

    def call(principal, name, args):
        return (
            server.write_mirror_units(**args)
            if name == "write_mirror_units"
            else server.TOOLS[name]["handler"](**args)
        )

    original = server._write_drawer

    def lost_response(**kwargs):
        original(**kwargs)
        raise RuntimeError("connection interrupted after commit")

    with monkeypatch.context() as patch:
        patch.setattr(server, "_write_drawer", lost_response)
        with pytest.raises(MirrorError):
            mirror_pending(call, None, stored, store)
    first = col.get(include=["documents", "metadatas"])
    assert len(first.ids) == 5
    assert store.mirror_progress(stored.id) == (-1, 0)
    store.close()
    store = cls(str(path))
    try:
        messages.append(
            ThreadMessage(1, "user", "message", "Appended after the crash", "raw append")
        )
        stored = store.append(stored.id, messages)
        completed = []
        finish = store.finish_mirror

        def record(stored_id, through):
            finish(stored_id, through)
            completed.append(through)

        monkeypatch.setattr(store, "finish_mirror", record)
        ids = mirror_pending(call, None, stored, store)
        expected = class_units(messages[:1]) + class_units(messages[1:])
        assert len(ids) == len(expected) == 5
        distinct = {(u.seq_start, u.seq_end, u.message_class, u.text): u for u in expected}
        assert len(set(ids)) == len(distinct) == 4
        assert completed == [0, 1]
        assert store.mirror_progress(stored.id) == (1, 1)
        assert col.get(ids=first.ids, include=["documents", "metadatas"]) == first
        assert col.count() == sum((len(unit.text) + 799) // 800 for unit in distinct.values())
        count = col.count()
        assert mirror_pending(call, None, stored, store) == []
        assert col.count() == count and completed == [0, 1]
    finally:
        store.close()


def test_real_worker_authentication_separates_internal_writes_from_tokens(writer, hub, monkeypatch):
    from mempalace.stored_text import INDEX_SPANS

    server, col, _, backend = writer
    monkeypatch.setattr(server, "_get_collection", lambda **kwargs: col)
    monkeypatch.setattr(server, "_mcp_tool_preflight_refusal", lambda *args: None)
    token = hub.token(
        "writer", profile="slim", scopes=["memory:write"], write_wing="shared", trust="untrusted"
    )
    public, response = hub.call(
        token,
        "mempalace_add_drawer",
        wing="shared",
        room="logs",
        content="Public conversation",
        lexical_only=True,
        _mirror_unit="forged",
        **{INDEX_SPANS: "forged"},
    )
    assert public["success"], response
    assert col.get(ids=[public["drawer_id"]], include=["embeddings"]).embeddings[0] is not None
    arguments = {
        "thread_id": "thr_internal",
        "units": [
            {
                "seq_start": 0,
                "seq_end": 0,
                "message_class": "tool_output",
                "part": 0,
                "text": "Internal tool output",
            }
        ],
    }
    blocked = hub.rpc(token, "internal/write_mirror_units", arguments)
    assert blocked["error"]["code"] == -32601
    assert col.count() == 1
    principal = hub.db.authenticate_token(token)
    result = hub.gateway.mediator.workers.call_tool(principal, "write_mirror_units", arguments)
    assert result["success"]
    records = col.get(ids=result["drawer_ids"], include=["embeddings", "metadatas"])
    assert (records.embeddings[0] is None) == (backend == "compact")
    assert records.metadatas[0]["hub_connection"] == "writer"
    assert col.count() == 2


@pytest.mark.parametrize("backend", ["compact", "sqlite_exact", "chroma"])
def test_first_mirror_creates_absent_palace_and_default_search_finds_it(hub, monkeypatch, backend):
    from commonplace.handoff.models import ThreadMessage
    from commonplace.handoff.palace_mirror import mirror_pending
    from mempalace import mcp_server as server

    monkeypatch.setenv("MEMPALACE_BACKEND_EXPLICIT", backend)
    monkeypatch.setattr(server, "_args", SimpleNamespace(transport="http"))
    assert server._get_collection() is None
    principal = hub.db.authenticate_token(hub.owner_token())
    ctx = hub.gateway.handoff
    store = ctx.store_for(hub.workspace)
    text = "ZXQ739251 exact first tool output"
    stored = store.store(
        hub.workspace,
        "first-mirror",
        [ThreadMessage(0, "tool", "tool_result", text, "raw")],
        actor=principal.actor,
    )
    ids = mirror_pending(ctx.call_tool, principal, stored, store)
    assert len(ids) == 1
    assert store.mirror_progress(stored.id) == (0, 0)
    assert mirror_pending(ctx.call_tool, principal, stored, store) == []
    result = ctx.call_tool(
        principal,
        "mempalace_search",
        {
            "query": "ZXQ739251",
            "wing": "private_threads",
            "room": stored.id,
            "limit": 10,
            "include_facts": False,
        },
    )
    assert ids[0] in [hit["drawer_id"] for hit in result["results"]]
    records = server._get_collection().get(ids=ids, include=["embeddings", "metadatas"])
    assert len(records.ids) == 1
    assert (records.embeddings[0] is None) == (backend == "compact")
    ctx.close()


@pytest.mark.parametrize(
    "response,accepted",
    [
        ({"error": "No palace found"}, True),
        ({"backend": "chroma", "error": "Chroma database missing"}, True),
        ({"backend": "compact", "error": "Chroma database missing"}, False),
        ({"backend": "chroma", "error": "Backend open failed"}, False),
        ({"backend": "compact", "error": "Backend mismatch"}, False),
        ({"error": "permission denied"}, False),
    ],
)
def test_bootstrap_only_treats_known_missing_palace_errors_as_empty(response, accepted):
    from commonplace.handoff.palace_mirror import MirrorError, _legacy_mirrored_seq

    stored = SimpleNamespace(id="thr_new", message_count=1)

    def read(*_args):
        return response

    if accepted:
        assert _legacy_mirrored_seq(read, None, stored) == -1
    else:
        with pytest.raises(MirrorError):
            _legacy_mirrored_seq(read, None, stored)


def test_similarity_duplicate_query_excludes_mirrors_before_limit(writer, monkeypatch):
    server, col, embedded, backend = writer
    monkeypatch.setattr(server, "_vector_disabled", False)
    # More mirror rows than n_results: post-filtering would hide the public match.
    for index in range(6):
        server.write_mirror_units(
            f"thr_{index:02x}",
            [
                {
                    "seq_start": 0,
                    "seq_end": 0,
                    "message_class": "conversation",
                    "part": 0,
                    "text": "Exact duplicate document",
                }
            ],
        )
    hidden = server.tool_check_duplicate("Exact duplicate document")
    assert hidden == {"is_duplicate": False, "matches": []}
    public = server.tool_add_drawer("shared", "public", "Exact duplicate document")
    assert public["success"]
    visible = server.tool_check_duplicate("Exact duplicate document")
    assert visible["is_duplicate"]
    assert {m["id"] for m in visible["matches"]} == {public["drawer_id"]}
    assert all(m["wing"] == "shared" for m in visible["matches"])


def test_checkpoint_files_document_present_only_in_mirror(writer, monkeypatch):
    server, col, embedded, backend = writer
    monkeypatch.setattr(server, "_vector_disabled", False)
    server.write_mirror_units(
        "thr_ab",
        [
            {
                "seq_start": 0,
                "seq_end": 0,
                "message_class": "conversation",
                "part": 0,
                "text": "Exact checkpoint document",
            }
        ],
    )
    result = server.tool_checkpoint(
        items=[
            {
                "wing": "shared",
                "room": "saved",
                "content": "Exact checkpoint document",
            }
        ]
    )
    assert result["added"] and not result["duplicates"] and not result["errors"]
