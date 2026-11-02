"""Lossless storage, literal search, atomic retries and concurrent access."""

import hashlib
import json
import os
import re
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from _thread_search_oracle import assert_alignment, old_search

from commonplace.handoff import thread_store as store_module
from commonplace.handoff.models import CHUNK_MAX_CHARS, THREADS_WING, RecallHit, ThreadMessage
from commonplace.handoff.thread_store import (
    Chunk,
    ThreadConflict,
    ThreadStore,
    canonical_sha256,
    chunk_messages,
)


def message(text="original words", *, seq=0, **fields):
    return ThreadMessage(seq, "user", "message", text, raw=text, **fields)


@pytest.fixture
def messages():
    return [
        ThreadMessage(0, "system", "system", "\x01Rules\x1f\t\r\n", ' { "system": true }\n'),
        ThreadMessage(
            1,
            "user",
            "message",
            "  Café e\u0301 🧠 中文\r\n\tverbatim  ",
            '{"content":"  Café é 🧠 中文\\r\\n\\tverbatim  "}',
            at="2026-10-30T03:00:00+00:00",
        ),
        ThreadMessage(
            2,
            "assistant",
            "tool_call",
            '{ "query": "violet" }',
            '{"tool": "find", "arguments": { "query": "violet" }}',
            name="find",
            call_id="call_🧠",
            at="2026-10-30T03:01:00Z",
        ),
        ThreadMessage(
            3,
            "tool",
            "tool_result",
            "found\n\x02violet\x7f",
            '{"result": "found\\n\\u0002violet\\u007f"}',
            name="find",
            call_id="call_🧠",
        ),
        ThreadMessage(4, "assistant", "other", "", '{"unknown": [1, 2, 3]}'),
    ]


@pytest.fixture
def make_store(tmp_path):
    opened = []

    def make(name="threads.sqlite3"):
        value = ThreadStore(str(tmp_path / name))
        opened.append(value)
        return value

    yield make
    for value in reversed(opened):
        value.close()


@pytest.fixture
def store(make_store):
    return make_store()


class NoFtsConnection(sqlite3.Connection):
    def execute(self, sql, parameters=()):
        if sql.startswith("CREATE VIRTUAL TABLE"):
            raise sqlite3.OperationalError("no such module: fts5")
        return super().execute(sql, parameters)


def use_connection(monkeypatch, connection_type):
    original = sqlite3.connect

    def connect(*args, **kwargs):
        return original(*args, factory=connection_type, **kwargs)

    monkeypatch.setattr(store_module.sqlite3, "connect", connect)


@pytest.fixture(params=[True, False], ids=["fts", "like"])
def searchable(request, monkeypatch, make_store):
    if not request.param:
        use_connection(monkeypatch, NoFtsConnection)
    value = make_store()
    assert value._fts_enabled is request.param
    return value


def test_chunk_empty():
    assert chunk_messages([]) == []


@pytest.mark.parametrize("size", [0, -1, -100])
def test_chunk_invalid_size(size):
    with pytest.raises(ValueError, match="positive"):
        chunk_messages([], max_chars=size)


@pytest.mark.parametrize("text", ["", "a"])
def test_chunk_prefix_cannot_fit(text):
    with pytest.raises(ValueError, match="prefix"):
        chunk_messages([message(text)], max_chars=1)


def test_chunk_prefix_fits_exactly_for_empty_text():
    line = "[0] user message: "
    assert chunk_messages([message("")], len(line)) == [Chunk(0, 0, 0, line)]
    with pytest.raises(ValueError, match="prefix"):
        chunk_messages([message("a")], len(line))


def test_chunk_line_format_and_grouping(messages):
    chunks = chunk_messages(messages)
    assert chunks == [
        Chunk(
            0,
            0,
            4,
            "[0] system system: \x01Rules\x1f\t\r\n\n"
            "[1] user message:   Café e\u0301 🧠 中文\r\n\tverbatim  \n"
            '[2] assistant tool_call find: { "query": "violet" }\n'
            "[3] tool tool_result find: found\n\x02violet\x7f\n"
            "[4] assistant other: ",
        )
    ]


def test_chunk_exact_group_boundary():
    first = "[0] user message: a"
    second = "[1] user message: b"
    limit = len(first) + 1 + len(second)
    items = [message(text, seq=seq) for seq, text in enumerate("abc")]
    assert chunk_messages(items, limit) == [
        Chunk(0, 0, 1, first + "\n" + second),
        Chunk(1, 2, 2, "[2] user message: c"),
    ]
    assert chunk_messages([items[0]], len(first)) == [Chunk(0, 0, 0, first)]


@pytest.mark.parametrize("limit", [32, 49, 64, 100])
def test_chunk_split_retains_every_character_at_boundaries(limit):
    prefix = "[7] user message: "
    capacity = limit - len(prefix)
    alphabet = "a🧠\n \t\r\x01中e\u0301"
    for size in [0, 1, capacity - 1, capacity, capacity + 1, capacity * 2, capacity * 2 + 1]:
        text = (alphabet * (size // len(alphabet) + 1))[:size]
        original = message(text, seq=7)
        chunks = chunk_messages([original], limit)
        assert [chunk.index for chunk in chunks] == list(range(len(chunks)))
        assert all(chunk.seq_start == chunk.seq_end == 7 for chunk in chunks)
        assert all(len(chunk.text) <= limit for chunk in chunks)
        assert "".join(chunk.text.removeprefix(prefix) for chunk in chunks) == text
        assert sum(len(chunk.text) - len(prefix) for chunk in chunks) == size
        assert original.text == text


def test_chunk_long_message_stays_isolated():
    text = " leading \r\n🧠 [1] user message: \ttrailing  " * 15
    items = [message("before"), message(text, seq=1), message("after", seq=2)]
    chunks = chunk_messages(items, 64)
    assert chunks[0] == Chunk(0, 0, 0, "[0] user message: before")
    assert chunks[-1] == Chunk(len(chunks) - 1, 2, 2, "[2] user message: after")
    pieces = chunks[1:-1]
    assert all(chunk.seq_start == chunk.seq_end == 1 for chunk in pieces)
    assert "".join(chunk.text.removeprefix("[1] user message: ") for chunk in pieces) == text
    assert all(len(chunk.text) <= 64 for chunk in chunks)


def test_canonical_hash_is_deterministic_and_unambiguous(messages):
    values = [(m.seq, m.role, m.kind, m.name, m.call_id, m.text, m.raw) for m in messages]
    expected = hashlib.sha256(
        json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert canonical_sha256(messages) == expected
    assert canonical_sha256(list(messages)) == expected
    assert canonical_sha256(list(reversed(messages))) != expected
    assert canonical_sha256([replace(messages[0], role="a", kind="bc")]) != canonical_sha256(
        [replace(messages[0], role="ab", kind="c")]
    )
    assert canonical_sha256([]) == hashlib.sha256(b"[]").hexdigest()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("seq", 1),
        ("role", "assistant"),
        ("kind", "other"),
        ("name", "find"),
        ("call_id", "call_one"),
        ("text", "changed"),
        ("raw", '{"changed":true}'),
    ],
)
def test_canonical_hash_includes_each_contracted_field(field, value):
    original = message()
    assert canonical_sha256([original]) != canonical_sha256([replace(original, **{field: value})])


def test_canonical_hash_excludes_timestamp_per_contract():
    original = message(at="2026-10-30T00:00:00Z")
    assert canonical_sha256([original]) == canonical_sha256([replace(original, at="changed")])


def test_store_round_trip_all_fields_and_reopen(make_store, messages):
    value = make_store("nested/threads.sqlite3")
    stored = value.store("workspace-a", "source-a", messages, actor="actor-a")
    assert re.fullmatch(r"thr_[0-9a-f]{16}", stored.id)
    assert stored.workspace_id == "workspace-a"
    assert stored.source_thread_id == "source-a"
    assert stored.message_count == len(messages)
    assert stored.chunk_count == len(chunk_messages(messages))
    assert stored.sha256 == canonical_sha256(messages)
    assert stored.wing == THREADS_WING
    assert stored.created_by == "actor-a"
    assert datetime.fromisoformat(stored.created_at).tzinfo == timezone.utc
    assert value.get(stored.id) == stored
    assert value.by_source("workspace-a", "source-a") == stored
    assert value.messages(stored.id) == messages
    assert value.chunks(stored.id) == chunk_messages(messages)
    value.close()
    reopened = make_store("nested/threads.sqlite3")
    assert reopened.get(stored.id) == stored
    assert reopened.messages(stored.id) == messages
    assert reopened.chunks(stored.id) == chunk_messages(messages)
    assert reopened.search([stored.id], "violet")[0].text == reopened.chunks(stored.id)[0].text


def test_one_megabyte_message_round_trip_and_chunk_reconstruction(store):
    # One MiB of UTF-8, with Unicode and NUL-free control characters.
    unit = "🧠中e\u0301\r\n\t\x01\x1f\x7f"
    unit_size = len(unit.encode("utf-8"))
    text = unit * (1_048_576 // unit_size) + "a" * (1_048_576 % unit_size)
    assert len(text.encode("utf-8")) == 1_048_576
    original = message(text, at="2026-10-30T00:00:00Z")
    stored = store.store("workspace-a", "large", [original], actor="actor-a")
    assert store.messages(stored.id) == [original]
    chunks = store.chunks(stored.id)
    assert stored.chunk_count == len(chunks) > 1
    assert "".join(chunk.text.removeprefix("[0] user message: ") for chunk in chunks) == text
    assert all(len(chunk.text) <= CHUNK_MAX_CHARS for chunk in chunks)
    assert all(chunk.seq_start == chunk.seq_end == 0 for chunk in chunks)


def test_store_empty_rejected_without_writes(store):
    before = store._conn.total_changes
    with pytest.raises(ValueError, match="at least one"):
        store.store("workspace-a", "empty", [], actor="actor-a")
    assert store._conn.total_changes == before
    assert store.by_source("workspace-a", "empty") is None


@pytest.mark.parametrize("sequences", [[1], [-1], [0, 0], [0, 2], [1, 0]])
def test_store_rejects_non_dense_sequence(store, sequences):
    with pytest.raises(ValueError, match="sequence"):
        store.store(
            "workspace-a", "invalid", [message(seq=seq) for seq in sequences], actor="actor-a"
        )
    assert store._conn.execute("SELECT count(*) FROM threads").fetchone()[0] == 0


def test_store_retry_writes_nothing_and_preserves_original_metadata(store, messages):
    stored = store.store("workspace-a", "source-a", messages, actor="first")
    before = store._conn.total_changes
    statements = []
    store._conn.set_trace_callback(statements.append)
    retry = store.store(
        "workspace-a", "source-a", messages, actor="second", parent_stored_id="ignored-on-retry"
    )
    assert retry == stored
    assert store._conn.total_changes == before
    assert not any(sql.lstrip().startswith(("INSERT", "UPDATE", "DELETE")) for sql in statements)
    assert store.parent_of(stored.id) is None
    assert store.messages(stored.id) == messages


@pytest.mark.parametrize("field", ["text", "raw", "name", "call_id", "role", "kind"])
def test_conflict_keeps_original_and_connection_usable(store, field):
    original = message()
    stored = store.store("workspace-a", "source-a", [original], actor="actor-a")
    before = store._conn.total_changes
    with pytest.raises(ThreadConflict, match="different"):
        store.store(
            "workspace-a", "source-a", [replace(original, **{field: "changed"})], actor="actor-b"
        )
    assert store._conn.total_changes == before
    assert store.get(stored.id) == stored
    assert store.messages(stored.id) == [original]
    assert store.store("workspace-a", "source-a", [original], actor="actor-a") == stored
    assert not store._conn.in_transaction


def test_store_uses_exactly_one_immediate_transaction(store, messages):
    statements = []
    store._conn.set_trace_callback(statements.append)
    store.store("workspace-a", "source-a", messages, actor="actor-a")
    assert statements.count("BEGIN IMMEDIATE") == 1
    assert statements.count("COMMIT") == 1
    assert "ROLLBACK" not in statements
    assert not store._conn.in_transaction


def test_source_ids_are_scoped_to_workspace(store):
    first = store.store("workspace-a", "same-source", [message("first")], actor="actor-a")
    second = store.store("workspace-b", "same-source", [message("second")], actor="actor-b")
    assert first.id != second.id
    assert store.by_source("workspace-a", "same-source") == first
    assert store.by_source("workspace-b", "same-source") == second
    assert store.by_source("workspace-c", "same-source") is None


def test_parent_links(store):
    root = store.store("workspace-a", "root", [message()], actor="actor-a")
    child = store.store(
        "workspace-a", "child", [message()], actor="actor-a", parent_stored_id=root.id
    )
    successor = store.store(
        "workspace-a", "successor", [message()], actor="actor-a", parent_stored_id=child.id
    )
    assert store.parent_of(successor.id) == child.id
    assert store.parent_of(child.id) == root.id
    assert store.parent_of(root.id) is None
    assert store.parent_of("missing") is None


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (0, None, slice(None)),
        (0, 0, slice(0, 1)),
        (1, 3, slice(1, 4)),
        (2, None, slice(2, None)),
        (4, 99, slice(4, None)),
        (100, None, slice(0, 0)),
        (-1, None, slice(0, 0)),
        (0, -1, slice(0, 0)),
        (3, 2, slice(0, 0)),
    ],
)
def test_message_ranges_are_inclusive(store, messages, start, end, expected):
    stored = store.store("workspace-a", "ranges", messages, actor="actor-a")
    assert store.messages(stored.id, start, end) == messages[expected]


def test_missing_lookups_and_literal_ids(store):
    missing = "' OR 1=1 --"
    assert store.get(missing) is None
    assert store.by_source(missing, missing) is None
    assert store.messages(missing) == []
    assert store.chunks(missing) == []
    assert store.parent_of(missing) is None
    stored = store.store(missing, missing, [message()], actor="actor-a")
    assert store.get(stored.id) == stored
    assert store.by_source(missing, missing) == stored


@pytest.mark.skipif(os.name == "nt", reason="POSIX file permissions")
def test_file_and_wal_are_private_and_existing_permissions_are_tightened(tmp_path, make_store):
    path = tmp_path / "threads.sqlite3"
    path.touch(mode=0o666)
    path.chmod(0o666)
    value = make_store()
    value.store("workspace-a", "source-a", [message()], actor="actor-a")
    assert path.stat().st_mode & 0o777 == 0o600
    for suffix in ["-wal", "-shm"]:
        assert path.with_name(path.name + suffix).stat().st_mode & 0o777 == 0o600
    assert value._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert value._conn.execute("PRAGMA synchronous").fetchone()[0] == 2
    assert value._conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_memory_store_and_idempotent_close():
    value = ThreadStore(":memory:")
    stored = value.store("workspace-a", "source-a", [message()], actor="actor-a")
    assert value.messages(stored.id) == [message()]
    value.close()
    value.close()
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        value.get(stored.id)


@pytest.mark.parametrize(
    "query",
    [
        '"',
        "AND",
        "*",
        "NEAR(",
        "🧠",
        "",
        " \t\r\n ",
        "OR NOT",
        '"AND" * NEAR(',
        'user:violet OR text:"secret"',
        '"; DROP TABLE threads; --',
        "a\x00b",
        '""""',
        "-+(){}[]^:$%_\\",
    ],
)
def test_search_untrusted_queries_never_raise(searchable, query):
    stored = searchable.store(
        "workspace-a", "source-a", [message('AND violet 🧠 NEAR( "quoted"')], actor="actor-a"
    )
    hits = searchable.search([stored.id], query)
    assert all(isinstance(hit, RecallHit) and hit.stored_thread_id == stored.id for hit in hits)
    if not query.strip():
        assert hits == []
    assert searchable.get(stored.id) == stored


def test_search_returns_verbatim_chunks_and_only_allowed_ids(searchable):
    allowed = searchable.store(
        "workspace-a", "allowed", [message("needle\n🧠 \x01 trailing  ")], actor="actor-a"
    )
    denied = searchable.store("workspace-b", "denied", [message("needle private")], actor="actor-b")
    chunk = searchable.chunks(allowed.id)[0]
    hits = searchable.search([allowed.id, allowed.id, "' OR 1=1 --"], "needle")
    assert len(hits) == 1
    assert hits[0].stored_thread_id == allowed.id
    assert hits[0].chunk_index == chunk.index
    assert hits[0].seq_start == chunk.seq_start
    assert hits[0].seq_end == chunk.seq_end
    assert hits[0].text == chunk.text
    assert searchable.search([allowed.id], "private") == []
    assert searchable.search(["missing"], "needle") == []
    assert searchable.search([denied.id], "private")[0].stored_thread_id == denied.id


def test_search_or_semantics_and_limit(searchable):
    first = searchable.store("workspace-a", "first", [message("alpine")], actor="actor-a")
    second = searchable.store("workspace-a", "second", [message("violet")], actor="actor-a")
    third = searchable.store("workspace-a", "third", [message("unrelated")], actor="actor-a")
    ids = [first.id, second.id, third.id]
    hits = searchable.search(ids, "alpine violet")
    assert {hit.stored_thread_id for hit in hits} == {first.id, second.id}
    assert searchable.search(ids, "alpine violet", 1) == hits[:1]
    assert searchable.search(ids, "alpine violet", 0) == []
    assert searchable.search(ids, "alpine violet", -1) == []
    assert searchable.search([], "alpine") == []


def test_search_nul_queries_match_the_complete_literal(searchable):
    exact = searchable.store("workspace-a", "exact", [message("foo\x00bar")], actor="actor-a")
    different = searchable.store(
        "workspace-a", "different", [message("foo\x00baz")], actor="actor-a"
    )
    plain = searchable.store("workspace-a", "plain", [message("foo")], actor="actor-a")
    ids = [different.id, plain.id, exact.id]
    hits = searchable.search(ids, "foo\x00bar")
    assert [hit.stored_thread_id for hit in hits] == [exact.id]
    assert hits[0].text == "[0] user message: foo\x00bar"
    assert searchable.search(ids, "FOO\x00BAR") == hits
    assert searchable.search(ids, "foo\x00qux") == []
    assert searchable.messages(exact.id) == [message("foo\x00bar")]


def test_search_long_queries_keep_all_tokens_and_respect_limit(searchable):
    words = [f"absent{index}" for index in range(1_500)] + ["needle"]
    ids = [
        searchable.store("workspace-a", word, [message(word)], actor="actor-a").id
        for word in (words[0], words[749], words[-1])
    ]
    query = " ".join(words)
    hits = searchable.search(ids, query)
    assert {hit.stored_thread_id for hit in hits} == set(ids)
    assert searchable.search(ids, query, limit=1) == hits[:1]


def test_search_token_longer_than_like_pattern_limit(searchable):
    stored = searchable.store("workspace-a", "plain", [message("foo")], actor="actor-a")
    assert searchable.search([stored.id], "x" * 60_000) == []


@pytest.mark.skipif(not hasattr(sqlite3.Connection, "setlimit"), reason="Python 3.11+ API")
@pytest.mark.parametrize("category", ["SQLITE_LIMIT_VARIABLE_NUMBER", "SQLITE_LIMIT_EXPR_DEPTH"])
def test_search_respects_sqlite_query_limits(searchable, category):
    stored = searchable.store("workspace-a", "found", [message("needle")], actor="actor-a")
    previous = searchable._conn.setlimit(getattr(sqlite3, category), 32)
    try:
        ids = [f"missing{index}" for index in range(80)] + [stored.id]
        query = " ".join([f"absent{index}" for index in range(80)] + ["needle"])
        hits = searchable.search(ids, query)
        assert [hit.stored_thread_id for hit in hits] == [stored.id]
    finally:
        searchable._conn.setlimit(getattr(sqlite3, category), previous)


@pytest.mark.skipif(not hasattr(sqlite3.Connection, "setlimit"), reason="Python 3.11+ API")
def test_fts_id_batches_preserve_global_scores_and_limits(store):
    weak = store.store(
        "workspace-a", "weak", [message("needle " + "filler " * 100)], actor="actor-a"
    )
    strong = store.store("workspace-a", "strong", [message("needle " * 20)], actor="actor-a")
    ids = [weak.id] + [f"missing{index}" for index in range(40)] + [strong.id]
    expected = store.search(ids, "needle")
    assert [hit.stored_thread_id for hit in expected] == [strong.id, weak.id]
    previous = store._conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 8)
    try:
        assert store.search(ids, "needle") == expected
        assert store.search(ids, "needle", limit=1) == expected[:1]
    finally:
        store._conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, previous)


@pytest.mark.skipif(not hasattr(sqlite3.Connection, "setlimit"), reason="Python 3.11+ API")
def test_search_matches_when_token_exceeds_configured_pattern_limit(searchable):
    stored = searchable.store("workspace-a", "found", [message("needle")], actor="actor-a")
    previous = searchable._conn.setlimit(sqlite3.SQLITE_LIMIT_LIKE_PATTERN_LENGTH, 4)
    try:
        hits = searchable.search([stored.id], "NEEDLE")
        assert [hit.stored_thread_id for hit in hits] == [stored.id]
    finally:
        searchable._conn.setlimit(sqlite3.SQLITE_LIMIT_LIKE_PATTERN_LENGTH, previous)


@pytest.mark.parametrize("use_fts", [True, False], ids=["fts", "like"])
def test_search_without_runtime_limit_api(monkeypatch, make_store, use_fts):
    base = sqlite3.Connection if use_fts else NoFtsConnection

    class LegacyConnection(base):
        getlimit = None

    use_connection(monkeypatch, LegacyConnection)
    value = make_store()
    assert value._fts_enabled is use_fts
    stored = value.store("workspace-a", "found", [message("needle")], actor="actor-a")
    query = " ".join([f"absent{index}" for index in range(1_500)] + ["needle"])
    assert [hit.stored_thread_id for hit in value.search([stored.id], query)] == [stored.id]


def test_fts_bm25_best_first(store):
    strong = store.store("workspace-a", "strong", [message("needle " * 20)], actor="actor-a")
    weak = store.store(
        "workspace-a", "weak", [message("needle " + "filler " * 100)], actor="actor-a"
    )
    hits = store.search([weak.id, strong.id], "needle")
    assert [hit.stored_thread_id for hit in hits] == [strong.id, weak.id]
    assert hits[0].score > hits[1].score > 0


def test_search_limit_across_multiple_chunks(searchable):
    text = "needle " * 2_000
    stored = searchable.store("workspace-a", "large", [message(text)], actor="actor-a")
    hits = searchable.search([stored.id], "needle", 2)
    assert len(hits) == 2
    chunks = searchable.chunks(stored.id)
    assert len(chunks) > 2
    assert len({hit.chunk_index for hit in hits}) == 2
    assert all(hit.text == chunks[hit.chunk_index].text for hit in hits)
    assert hits == searchable.search([stored.id], "needle", 2)


@pytest.mark.parametrize("literal", ["%", "_", "\\"])
def test_like_fallback_escapes_wildcards(monkeypatch, make_store, literal):
    use_connection(monkeypatch, NoFtsConnection)
    value = make_store()
    found = value.store("workspace-a", "literal", [message("has " + literal)], actor="actor-a")
    other = value.store("workspace-a", "other", [message("unrelated")], actor="actor-a")
    hits = value.search([other.id, found.id], literal)
    assert [hit.stored_thread_id for hit in hits] == [found.id]


@pytest.mark.parametrize("has_initial_index", [False, True])
def test_fts_backfills_after_unavailable_extension(monkeypatch, make_store, has_initial_index):
    ids = []
    if has_initial_index:
        initial = make_store()
        ids.append(initial.store("workspace-a", "first", [message("needle")], actor="actor-a").id)
        initial.close()
    with monkeypatch.context() as patch:
        use_connection(patch, NoFtsConnection)
        fallback = make_store()
        stored = fallback.store("workspace-a", "second", [message("needle")], actor="actor-a")
        ids.append(stored.id)
        assert fallback.search(ids, "needle")
        fallback.close()
    reopened = make_store()
    assert len(reopened.search(ids, "needle")) == len(ids)
    assert reopened.search(ids, "needle") == old_search(reopened, ids, "needle")
    assert_alignment(reopened)
    reopened.close()
    reopened = make_store()
    assert len(reopened.search(ids, "needle")) == len(ids)
    assert reopened.search(ids, "needle") == old_search(reopened, ids, "needle")
    assert_alignment(reopened)
    assert reopened._conn.execute("SELECT count(*) FROM chunks_fts").fetchone()[0] == len(ids)


@pytest.mark.parametrize("statement", ["PRAGMA journal_mode=WAL", "CREATE VIRTUAL TABLE"])
def test_initialization_errors_are_not_hidden_and_connection_is_closed(
    monkeypatch, tmp_path, statement
):
    closed = []

    class BrokenConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if sql.startswith(statement):
                raise sqlite3.OperationalError("disk I/O error")
            return super().execute(sql, parameters)

        def close(self):
            closed.append(True)
            return super().close()

    use_connection(monkeypatch, BrokenConnection)
    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        ThreadStore(str(tmp_path / "broken.sqlite3"))
    assert closed == [True]


class InterruptedConnection(sqlite3.Connection):
    fail_table = ""

    def execute(self, sql, parameters=()):
        result = super().execute(sql, parameters)
        if self.fail_table and sql.lstrip().startswith("INSERT INTO " + self.fail_table + "\n"):
            self.fail_table = ""
            raise RuntimeError("simulated interruption")
        return result

    def executemany(self, sql, parameters):
        if self.fail_table and sql.lstrip().startswith("INSERT INTO " + self.fail_table):
            super().execute(sql, next(iter(parameters)))
            self.fail_table = ""
            raise RuntimeError("simulated interruption")
        return super().executemany(sql, parameters)


@pytest.mark.parametrize("table", ["threads", "messages", "chunks", "chunks_fts"])
def test_mid_insert_failure_rolls_back_everything_and_retry_succeeds(
    monkeypatch, make_store, table
):
    use_connection(monkeypatch, InterruptedConnection)
    value = make_store()
    old = value.store("workspace-a", "old", [message("needle original")], actor="actor-a")
    tables = ["threads", "messages", "chunks", "chunks_fts"]
    before = {
        name: value._conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0] for name in tables
    }
    value._conn.fail_table = table
    items = [message("needle " * 1_000), message("second", seq=1)]
    with pytest.raises(RuntimeError, match="simulated interruption"):
        value.store("workspace-a", "interrupted", items, actor="actor-a")
    assert not value._conn.in_transaction
    assert value.by_source("workspace-a", "interrupted") is None
    assert value.messages(old.id) == [message("needle original")]
    assert {
        name: value._conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0] for name in tables
    } == before
    value.close()
    reopened = make_store()
    assert reopened.by_source("workspace-a", "interrupted") is None
    stored = reopened.store("workspace-a", "interrupted", items, actor="actor-a")
    assert reopened.messages(stored.id) == items
    assert reopened.search([stored.id], "needle")
    assert reopened.store("workspace-a", "interrupted", items, actor="actor-a") == stored


@pytest.mark.parametrize("separate_connections", [False, True])
def test_concurrent_same_source_stores_return_one_record(make_store, separate_connections):
    first = make_store()
    second = make_store() if separate_connections else first
    barrier = threading.Barrier(2, timeout=5)
    items = [message("needle " * 1_000)]

    def save(value, actor):
        barrier.wait()
        stored = value.store("workspace-a", "same-source", items, actor=actor)
        assert value.messages(stored.id) == items
        assert value.get(stored.id) == stored
        assert value.search([stored.id], "needle")
        return stored

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(save, first, "first"), pool.submit(save, second, "second")]
        results = [future.result(timeout=10) for future in futures]
    assert results[0] == results[1]
    assert first._conn.execute("SELECT count(*) FROM threads").fetchone()[0] == 1
    assert first._conn.execute("SELECT count(*) FROM messages").fetchone()[0] == len(items)
    assert (
        first._conn.execute("SELECT count(*) FROM chunks").fetchone()[0] == results[0].chunk_count
    )
    assert (
        first._conn.execute("SELECT count(*) FROM chunks_fts").fetchone()[0]
        == results[0].chunk_count
    )


def test_concurrent_different_content_has_one_winner(make_store):
    first = make_store()
    second = make_store()
    barrier = threading.Barrier(2, timeout=5)

    def save(value, text):
        barrier.wait()
        try:
            return value.store("workspace-a", "same-source", [message(text)], actor="actor-a")
        except ThreadConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(save, first, "first"), pool.submit(save, second, "second")]
        results = [future.result(timeout=10) for future in futures]
    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert first.get(winners[0].id) == winners[0]
    assert first._conn.execute("SELECT count(*) FROM threads").fetchone()[0] == 1


class FTSFailureConnection(sqlite3.Connection):
    failure = None
    abort_on_failure = False
    fts_attempts = 0
    partial_counts = None

    def executemany(self, sql, parameters):
        if sql.startswith("INSERT INTO chunks_fts"):
            self.fts_attempts += 1
            if self.failure:
                error, self.failure = self.failure, None
                super().execute(sql, next(iter(parameters)))
                if self.abort_on_failure:
                    self.rollback()
                raise error
            try:
                return super().executemany(sql, parameters)
            except sqlite3.IntegrityError:
                self.partial_counts = (
                    self.execute("SELECT count(*) FROM chunks").fetchone()[0],
                    self.execute("SELECT count(*) FROM chunks_fts").fetchone()[0],
                )
                raise
        return super().executemany(sql, parameters)


def _write_state(store):
    return {
        table: list(map(tuple, store._conn.execute(f"SELECT * FROM {table} ORDER BY rowid")))
        for table in ("threads", "messages", "chunks", "chunks_fts", "handoff_docs")
    }


def _orphan_after_next_chunk(store):
    rowid = store._conn.execute("SELECT max(rowid)+2 FROM chunks").fetchone()[0]
    store._conn.execute(
        "INSERT INTO chunks_fts(rowid, thread_id, idx, text) VALUES (?, 'orphan', 0, 'stale')",
        (rowid,),
    )


def _collision_operation(store, operation, old, items):
    if operation == "store":
        return store.store("test", "new", items, actor="test")
    return store.append(old.id, store.messages(old.id) + items)


@pytest.mark.parametrize("operation", ["store", "append"])
def test_live_fts_collision_rolls_back_partial_batch_then_retries_once(
    monkeypatch, make_store, operation
):
    use_connection(monkeypatch, FTSFailureConnection)
    store = make_store()
    old = store.store("test", "old", [message("old boundary")], actor="test")
    before_chunks = store.chunks(old.id)
    _orphan_after_next_chunk(store)
    count = store._conn.fts_attempts
    offset = int(operation == "append")
    items = [message(" café\r\n\t" * 1000, seq=offset), message("tail\x00", seq=offset + 1)]
    expected = ([message("old boundary")] if offset else []) + items
    with patch.object(store, "_repair_fts", wraps=store._repair_fts) as repair:
        result = _collision_operation(store, operation, old, items)
    assert repair.call_count == 1
    assert store._conn.fts_attempts == count + 2
    # At the collision, the first item in the attempted batch was indexed.
    assert store._conn.partial_counts[0] > 2
    assert store._conn.partial_counts[1] == 3
    assert store.messages(result.id) == expected
    assert result.sha256 == canonical_sha256(expected)
    assert result.message_count == len(expected)
    assert result.chunk_count == len(store.chunks(result.id))
    assert store.chunks(old.id)[: len(before_chunks)] == before_chunks
    assert_alignment(store)
    changes = store._conn.total_changes
    if operation == "store":
        assert store.store("test", "new", items, actor="other") == result
    else:
        assert store.append(old.id, expected) == result
    assert store._conn.total_changes == changes


@pytest.mark.parametrize("operation", ["store", "append"])
@pytest.mark.parametrize(
    "error",
    [
        sqlite3.IntegrityError("unrelated constraint"),
        sqlite3.IntegrityError("constraint failed"),
        sqlite3.OperationalError("disk I/O error"),
        RuntimeError("interrupted"),
    ],
    ids=["integrity", "unverified-constraint", "io", "interruption"],
)
def test_unrelated_fts_failure_preserves_caller_writes_and_does_not_repair(
    monkeypatch, make_store, operation, error
):
    use_connection(monkeypatch, FTSFailureConnection)
    store = make_store()
    old = store.store("test", "old", [message("old")], actor="test")
    _orphan_after_next_chunk(store)
    with store._transaction():
        store.put_doc("caller", "earlier caller write")
        before = _write_state(store)
        store._conn.failure = error
        with patch.object(store, "_repair_fts", wraps=store._repair_fts) as repair:
            with pytest.raises(type(error), match=str(error)):
                _collision_operation(
                    store, operation, old, [message("new", seq=int(operation == "append"))]
                )
        assert repair.call_count == 0
        assert store._conn.in_transaction
        assert _write_state(store) == before
    assert store.doc("caller") == "earlier caller write"


@pytest.mark.parametrize("operation", ["store", "append"])
@pytest.mark.parametrize("failure", ["repair", "second-retry", "quota"])
def test_collision_recovery_failure_is_atomic(monkeypatch, make_store, operation, failure):
    use_connection(monkeypatch, FTSFailureConnection)
    store = make_store()
    old = store.store("test", "old", [message("old")], actor="test")
    _orphan_after_next_chunk(store)
    before = _write_state(store)
    repair = store._repair_fts

    def fail_repair():
        repair()
        if failure == "repair":
            raise sqlite3.OperationalError("repair failed")
        if failure == "second-retry":
            _orphan_after_next_chunk(store)

    monkeypatch.setattr(store, "_repair_fts", fail_repair)
    if failure == "quota":
        check = store._check_storage_budget

        def quota(maximum, messages=()):
            check(maximum, messages)
            if not messages:
                raise store_module.ThreadStorageLimit("quota failed")

        monkeypatch.setattr(store, "_check_storage_budget", quota)
    error_type = {
        "repair": sqlite3.OperationalError,
        "second-retry": sqlite3.IntegrityError,
        "quota": store_module.ThreadStorageLimit,
    }[failure]
    count = store._conn.fts_attempts
    with pytest.raises(error_type):
        _collision_operation(
            store,
            operation,
            old,
            [message("needle " * 1200, seq=int(operation == "append"))],
        )
    assert store._conn.fts_attempts == count + (1 if failure == "repair" else 2)
    assert _write_state(store) == before
    assert store._fts_verified_version is None
    assert not store._conn.in_transaction


@pytest.mark.parametrize("operation", ["store", "append"])
def test_collision_repair_and_ingest_follow_outer_rollback(store, operation):
    old = store.store("test", "old", [message("old")], actor="test")
    _orphan_after_next_chunk(store)
    before = _write_state(store)
    with pytest.raises(RuntimeError, match="outer rollback"):
        with store._transaction():
            _collision_operation(
                store,
                operation,
                old,
                [message("needle " * 1200, seq=int(operation == "append"))],
            )
            assert_alignment(store)
            assert store._conn.in_transaction
            raise RuntimeError("outer rollback")
    assert _write_state(store) == before
    assert store._fts_verified_version is None


@pytest.mark.parametrize("operation", ["store", "append"])
def test_database_aborted_ingest_preserves_original_failure(monkeypatch, make_store, operation):
    use_connection(monkeypatch, FTSFailureConnection)
    store = make_store()
    old = store.store("test", "old", [message("old")], actor="test")
    before = _write_state(store)
    error = sqlite3.OperationalError("disk I/O error")
    store._conn.failure = error
    store._conn.abort_on_failure = True
    with pytest.raises(sqlite3.OperationalError) as caught:
        _collision_operation(
            store, operation, old, [message("new", seq=int(operation == "append"))]
        )
    assert caught.value is error
    assert not store._conn.in_transaction
    assert _write_state(store) == before
