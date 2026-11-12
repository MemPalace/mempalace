"""Recall access, chains, ranking and verbatim range reads without a worker."""

import builtins
import unicodedata
from dataclasses import replace

import pytest

from commonplace.control_db import Principal
from commonplace.handoff import palace_mirror
from commonplace.handoff.models import RecallHit, StoredThread, ThreadMessage
from commonplace.handoff.recall import MAX_RANGE, Recall, ThreadNotFound
from commonplace.handoff.thread_store import room_match_value, source_match_key, thread_id_error
from commonplace.policy import Policy


class MemoryStore:
    def __init__(self):
        self.threads = {}
        self.parents = {}
        self.rows = {}
        self.hits = []
        self.calls = []

    def get(self, stored_id):
        self.calls.append(("get", stored_id))
        return self.threads.get(stored_id)

    def parent_of(self, stored_id):
        self.calls.append(("parent_of", stored_id))
        return self.parents.get(stored_id)

    def messages(self, stored_id, start=0, end=None):
        self.calls.append(("messages", stored_id, start, end))
        return [
            row
            for row in self.rows.get(stored_id, [])
            if start <= row.seq and (end is None or row.seq <= end)
        ]

    def search(self, stored_ids, query, limit=8):
        self.calls.append(("search", stored_ids, query, limit))
        return self.hits[:limit]

    # The source-key lookups real thread stores index; not recorded in calls.
    def by_source_value(self, workspace_id, value):
        key = unicodedata.normalize("NFKC", value)
        return [
            thread
            for thread in self.threads.values()
            if thread.workspace_id == workspace_id
            and unicodedata.normalize("NFKC", thread.source_thread_id) == key
        ]

    def by_source_prefix(self, workspace_id, value):
        target = room_match_value(value)
        return [
            thread
            for thread in self.threads.values()
            if thread.workspace_id == workspace_id
            and target.startswith("mcp:")
            and target.startswith(source_match_key(thread.source_thread_id))
        ]

    def legacy_sources(self, workspace_id):
        return [
            (thread.id, thread.source_thread_id)
            for thread in self.threads.values()
            if thread.workspace_id == workspace_id and thread_id_error(thread.source_thread_id)
        ]


def reader(*, scopes=("threads:read", "memory:read"), wings=("threads",)):
    return Principal(
        user_id="user_one",
        user_email="reader@example.test",
        workspace_id="workspace_one",
        connection_id="connection_one",
        connection="reader",
        client="test",
        auth="token",
        role="member",
        policy=Policy.build(scopes=scopes, read_wings=wings, trust="standard"),
    )


def stored(number=1, **changes):
    return replace(
        StoredThread(
            id=f"thr_{number:016x}",
            workspace_id="workspace_one",
            source_thread_id=f"source_{number}",
            message_count=1,
            chunk_count=1,
            sha256="digest",
            created_at="2026-10-30T00:00:00Z",
        ),
        **changes,
    )


def hit(thread, index=0, score=1.0, text=None):
    return RecallHit(
        stored_thread_id=thread.id,
        chunk_index=index,
        seq_start=index * 2,
        seq_end=index * 2 + 1,
        score=score,
        text=text if text is not None else f"verbatim {thread.id} #{index}",
    )


def invoke(recall, action, principal, stored_id):
    if action == "recall":
        return recall.recall(principal, stored_id, "a fact")
    return getattr(recall, action)(principal, stored_id)


@pytest.fixture
def memory():
    store = MemoryStore()
    workspaces = []

    def store_for(workspace_id):
        workspaces.append(workspace_id)
        return store

    return store, Recall(store_for), workspaces


@pytest.mark.parametrize("action", ["thread", "chain", "recall", "get"])
def test_missing_thread_is_not_found(memory, action):
    _, recall, _ = memory
    with pytest.raises(ThreadNotFound, match="^thread not found$"):
        invoke(recall, action, reader(), stored().id)


@pytest.mark.parametrize("action", ["thread", "chain", "recall", "get"])
@pytest.mark.parametrize(
    ("principal", "changes"),
    [
        (reader(scopes=("memory:read",)), {}),
        (reader(scopes=("threads:read",)), {}),
        (reader(wings=()), {}),
        (reader(wings=("other",)), {}),
        (reader(), {"workspace_id": "workspace_two"}),
        (reader(wings=("*",)), {"wing": "private.notes"}),
        (reader(), {"wing": "other"}),
        (reader(), {"wing": ""}),
        (reader(wings=("linux_workstation",)), {"wing": "linux_workstation"}),
    ],
)
def test_denied_thread_is_indistinguishable_from_missing(memory, action, principal, changes):
    store, recall, _ = memory
    present = stored(**changes)
    with pytest.raises(ThreadNotFound) as missing:
        invoke(recall, action, principal, present.id)
    store.threads[present.id] = present
    with pytest.raises(ThreadNotFound) as denied:
        invoke(recall, action, principal, present.id)
    assert type(denied.value) is type(missing.value)
    assert str(denied.value) == str(missing.value) == "thread not found"
    assert not any(call[0] in ("parent_of", "messages", "search") for call in store.calls)


@pytest.mark.parametrize("action", ["thread", "chain", "recall", "get"])
@pytest.mark.parametrize(
    "stored_id", ["", "thr_", "thr_xyz", "THR_ab", "thr_ab\n", "thr_../ab", " thr_ab", None, 7]
)
def test_malformed_id_never_touches_store(memory, action, stored_id):
    store, recall, workspaces = memory
    with pytest.raises(ThreadNotFound, match="^thread not found$"):
        invoke(recall, action, reader(), stored_id)
    assert store.calls == []
    assert workspaces == []


def test_missing_scope_never_opens_store(memory):
    store, recall, workspaces = memory
    with pytest.raises(ThreadNotFound):
        recall.thread(reader(scopes=()), stored().id)
    assert store.calls == []
    assert workspaces == []


@pytest.mark.parametrize(
    ("principal", "wing"),
    [
        (reader(), "threads"),
        (reader(), "wing_threads"),
        (reader(wings=("research",)), "research"),
        (
            reader(scopes=("memory:read", "threads:read", "private:read"), wings=("*",)),
            "private.notes",
        ),
    ],
)
def test_thread_uses_real_policy_and_principal_workspace(memory, principal, wing):
    store, recall, workspaces = memory
    present = stored(id="thr_A1b2", wing=wing)
    store.threads[present.id] = present
    assert recall.thread(principal, present.id) is present
    assert workspaces == [principal.workspace_id]


def test_chain_is_newest_first_and_cycle_safe(memory):
    store, recall, _ = memory
    newest, older, oldest = stored(3), stored(2), stored(1)
    store.threads = {thread.id: thread for thread in (newest, older, oldest)}
    store.parents = {newest.id: older.id, older.id: oldest.id, oldest.id: newest.id}
    assert recall.chain(reader(), newest.id) == [newest, older, oldest]
    assert [call for call in store.calls if call[0] == "get"] == [
        ("get", thread.id) for thread in (newest, older, oldest)
    ]


def test_chain_self_cycle(memory):
    store, recall, _ = memory
    present = stored()
    store.threads[present.id] = present
    store.parents[present.id] = present.id
    assert recall.chain(reader(), present.id) == [present]
    assert store.calls == [("get", present.id), ("parent_of", present.id)]


@pytest.mark.parametrize("parent_kind", ["missing", "malformed", "workspace", "wing", "private"])
def test_chain_stops_at_first_unreadable_parent(memory, parent_kind):
    store, recall, _ = memory
    newest, parent, oldest = stored(3), stored(2), stored(1)
    if parent_kind == "malformed":
        parent = replace(parent, id="source_thread")
    elif parent_kind == "workspace":
        parent = replace(parent, workspace_id="workspace_two")
    elif parent_kind == "wing":
        parent = replace(parent, wing="other")
    elif parent_kind == "private":
        parent = replace(parent, wing="private.notes")
    store.threads = {thread.id: thread for thread in (newest, parent, oldest)}
    if parent_kind == "missing":
        del store.threads[parent.id]
    store.parents = {newest.id: parent.id, parent.id: oldest.id}
    assert recall.chain(reader(), newest.id) == [newest]
    assert ("parent_of", parent.id) not in store.calls
    assert ("get", oldest.id) not in store.calls
    if parent_kind == "malformed":
        assert ("get", parent.id) not in store.calls


def test_chain_is_bounded_to_fifty_threads(memory):
    store, recall, _ = memory
    threads = [stored(number) for number in range(1, 53)]
    store.threads = {thread.id: thread for thread in threads}
    store.parents = {current.id: parent.id for current, parent in zip(threads, threads[1:])}
    assert recall.chain(reader(), threads[0].id) == threads[:50]
    assert ("parent_of", threads[49].id) not in store.calls
    assert ("get", threads[50].id) not in store.calls


@pytest.mark.parametrize("query", ["", " ", "\n\t\r", "\u2003"])
def test_empty_query_has_no_search_or_mirror_calls(memory, query, monkeypatch):
    store, _, _ = memory
    present = stored()
    store.threads[present.id] = present

    def unexpected(*args, **kwargs):
        pytest.fail("empty recall touched a source")

    monkeypatch.setattr(palace_mirror, "palace_search", unexpected)
    recall = Recall(lambda _: store, call_tool=unexpected)
    assert recall.recall(reader(), present.id, query) == []
    assert store.calls == [("get", present.id)]


@pytest.mark.parametrize("access", ["malformed", "missing", "scope", "workspace", "wing"])
def test_empty_query_still_checks_thread_access(memory, access):
    store, recall, workspaces = memory
    present = stored()
    principal = reader()
    if access == "malformed":
        present = replace(present, id="bad_id")
    elif access == "scope":
        principal = reader(scopes=())
    elif access == "workspace":
        present = replace(present, workspace_id="workspace_two")
    elif access == "wing":
        present = replace(present, wing="other")
    if access != "missing":
        store.threads[present.id] = present
    with pytest.raises(ThreadNotFound, match="^thread not found$"):
        recall.recall(principal, present.id, "  \n")
    assert not any(call[0] in ("parent_of", "messages", "search") for call in store.calls)
    if access in ("malformed", "scope"):
        assert workspaces == []


def test_fts_only_preserves_text_and_ranges(memory, monkeypatch):
    store, recall, _ = memory
    present = stored()
    store.threads[present.id] = present
    original = hit(present, score=-5.0, text='  Exact\r\n言葉\t"words"\n')
    store.hits = [original]

    def unexpected(*args, **kwargs):
        pytest.fail("mirror called without call_tool")

    monkeypatch.setattr(palace_mirror, "palace_search", unexpected)
    assert recall.recall(reader(), present.id, "  Exact\n") == [replace(original, score=1.0)]
    assert store.calls[-1] == ("search", [present.id], "Exact", 8)
    assert store.hits == [original]


def test_merge_normalises_sources_dedupes_and_orders(memory, monkeypatch):
    store, _, workspaces = memory
    newest = stored(2, created_at="2026-10-30T01:00:00Z")
    older = stored(1, created_at="2026-10-29T01:00:00Z")
    store.threads = {newest.id: newest, older.id: older}
    store.parents[newest.id] = older.id
    store.hits = [
        hit(older, 0, 100),
        hit(newest, 0, 40, "FTS duplicate"),
        hit(newest, 0, 25, "lower FTS duplicate"),
        hit(newest, 1, 0),
    ]
    semantic = [
        hit(newest, 2, 0.9),
        hit(newest, 0, 0.6, "semantic duplicate wins"),
        hit(older, 0, 0.5, "lower semantic duplicate"),
        hit(older, 1, 0.3),
    ]
    calls = []
    principal = reader()
    call_tool = object()

    def mirror_search(*args):
        calls.append(args)
        return semantic

    monkeypatch.setattr(palace_mirror, "palace_search", mirror_search)
    recall = Recall(
        lambda workspace_id: workspaces.append(workspace_id) or store, call_tool=call_tool
    )
    result = recall.recall(principal, newest.id, "  planted fact  ", limit=8)
    assert [(item.stored_thread_id, item.chunk_index) for item in result] == [
        (newest.id, 2),
        (older.id, 0),
        (newest.id, 0),
        (newest.id, 1),
        (older.id, 1),
    ]
    assert [item.score for item in result] == pytest.approx([1, 1, 0.5, 0, 0])
    assert result[2].text == "semantic duplicate wins"
    assert calls == [(call_tool, principal, [newest.id, older.id], "planted fact", 8)]
    assert store.calls[-1] == ("search", [newest.id, older.id], "planted fact", 8)


def test_equal_duplicate_score_prefers_fts_text(memory, monkeypatch):
    store, _, _ = memory
    present = stored()
    store.threads[present.id] = present
    original = hit(present, score=300, text="exact stored text")
    store.hits = [original]
    monkeypatch.setattr(palace_mirror, "palace_search", lambda *args: [hit(present, text="mirror")])
    recall = Recall(lambda _: store, call_tool=object())
    assert recall.recall(reader(), present.id, "query") == [replace(original, score=1.0)]


def test_empty_fts_still_uses_mirror(memory, monkeypatch):
    store, _, _ = memory
    present = stored()
    store.threads[present.id] = present
    semantic = hit(present, score=0.25)
    monkeypatch.setattr(palace_mirror, "palace_search", lambda *args: [semantic])
    recall = Recall(lambda _: store, call_tool=object())
    assert recall.recall(reader(), present.id, "fact") == [replace(semantic, score=1.0)]


def test_both_sources_empty(memory, monkeypatch):
    store, _, _ = memory
    present = stored()
    store.threads[present.id] = present
    monkeypatch.setattr(palace_mirror, "palace_search", lambda *args: [])
    recall = Recall(lambda _: store, call_tool=object())
    assert recall.recall(reader(), present.id, "fact") == []


@pytest.mark.parametrize("follow_chain", [True, False])
def test_searches_only_readable_selected_threads(memory, monkeypatch, follow_chain):
    store, _, _ = memory
    newest, older, hidden, oldest = stored(4), stored(3), stored(2, wing="other"), stored(1)
    store.threads = {thread.id: thread for thread in (newest, older, hidden, oldest)}
    store.parents = {newest.id: older.id, older.id: hidden.id, hidden.id: oldest.id}
    store.hits = [hit(newest), hit(older), hit(hidden, score=999), hit(oldest, score=999)]
    calls = []

    def mirror_search(_tool, _principal, ids, query, limit):
        calls.append((ids, query, limit))
        return [hit(newest, 1), hit(older, 1), hit(hidden, 1, 999), hit(oldest, 1, 999)]

    monkeypatch.setattr(palace_mirror, "palace_search", mirror_search)
    recall = Recall(lambda _: store, call_tool=object())
    ids = [newest.id, older.id] if follow_chain else [newest.id]
    result = recall.recall(reader(), newest.id, "query", follow_chain=follow_chain)
    assert {item.stored_thread_id for item in result} == set(ids)
    assert all(item.score == 1 for item in result)
    assert store.calls[-1] == ("search", ids, "query", 8)
    assert calls == [(ids, "query", 8)]
    if not follow_chain:
        assert not any(call[0] == "parent_of" for call in store.calls)


@pytest.mark.parametrize("failure", [RuntimeError("worker failed"), NotImplementedError("stub")])
def test_mirror_failure_logs_and_falls_back_to_fts(memory, monkeypatch, caplog, failure):
    store, _, _ = memory
    present = stored()
    store.threads[present.id] = present
    original = hit(present)
    store.hits = [original]

    def broken(*args):
        raise failure

    monkeypatch.setattr(palace_mirror, "palace_search", broken)
    recall = Recall(lambda _: store, call_tool=object())
    assert recall.recall(reader(), present.id, "sensitive query") == [original]
    assert "Memory search unavailable during handoff recall" in caplog.text
    assert "sensitive query" not in caplog.text


def test_invalid_mirror_result_falls_back_to_fts(memory, monkeypatch, caplog):
    store, _, _ = memory
    present = stored()
    store.threads[present.id] = present
    store.hits = [hit(present)]
    monkeypatch.setattr(palace_mirror, "palace_search", lambda *args: None)
    recall = Recall(lambda _: store, call_tool=object())
    assert recall.recall(reader(), present.id, "query") == store.hits
    assert "Memory search unavailable" in caplog.text


@pytest.mark.parametrize("use_mirror", [False, True])
def test_mirror_import_is_lazy_and_import_failure_is_best_effort(
    memory, monkeypatch, caplog, use_mirror
):
    store, _, _ = memory
    present = stored()
    store.threads[present.id] = present
    store.hits = [hit(present)]
    original_import = builtins.__import__
    imports = []

    def unavailable(name, *args, **kwargs):
        if name == "palace_mirror":
            imports.append(name)
            raise ImportError("mirror unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", unavailable)
    recall = Recall(lambda _: store, call_tool=object() if use_mirror else None)
    assert recall.recall(reader(), present.id, "query") == store.hits
    assert imports == (["palace_mirror"] if use_mirror else [])
    assert bool(caplog.records) is use_mirror


@pytest.mark.parametrize(
    ("limit", "bounded"), [(-10, 1), (0, 1), (1, 1), (7, 7), (50, 50), (500, 50)]
)
def test_recall_limit_is_clamped_for_both_sources_and_output(memory, monkeypatch, limit, bounded):
    store, _, _ = memory
    present = stored()
    store.threads[present.id] = present
    store.hits = [hit(present, index, 100 - index) for index in range(60)]
    calls = []

    def mirror_search(_tool, _principal, ids, query, source_limit):
        calls.append(source_limit)
        return [hit(present, index + 100, 100 - index) for index in range(source_limit)]

    monkeypatch.setattr(palace_mirror, "palace_search", mirror_search)
    recall = Recall(lambda _: store, call_tool=object())
    result = recall.recall(reader(), present.id, "query", limit=limit)
    assert len(result) == bounded
    assert calls == [bounded]
    assert store.calls[-1][-1] == bounded
    assert all(0 <= item.score <= 1 for item in result)


@pytest.mark.parametrize(
    ("scores", "expected"),
    [
        ([3, 2, 1], [1, 0.5, 0]),
        ([-1, -2, -3], [1, 0.5, 0]),
        ([0, 0], [1, 1]),
        ([-7, -7], [1, 1]),
        ([1e308, 0, -1e308], [1, 0.5, 0]),
        ([5e-324, 0], [1, 0]),
        ([float("nan"), float("inf"), -float("inf")], []),
        ([3, float("nan"), 1], [1, 0]),
    ],
)
def test_normalised_scores_are_finite_and_bounded(memory, scores, expected):
    store, recall, _ = memory
    present = stored()
    store.threads[present.id] = present
    store.hits = [hit(present, index, score) for index, score in enumerate(scores)]
    result = recall.recall(reader(), present.id, "query")
    assert [item.score for item in result] == pytest.approx(expected)


@pytest.mark.parametrize(
    ("newer_at", "older_at"),
    [
        ("2026-10-30T00:00:00.1Z", "2026-10-30T00:00:00Z"),
        ("2026-10-30T00:00:01+00:00", "2026-10-30T00:00:00Z"),
        ("2026-10-30T00:00:01", "2026-10-30T00:00:00"),
        ("2026-10-30T00:00:01Z", ""),
        ("2026-10-30T00:00:01Z", "invalid"),
    ],
)
def test_ties_use_creation_time_even_when_parent_is_newer(memory, newer_at, older_at):
    store, recall, _ = memory
    child = stored(2, created_at=older_at)
    parent = stored(1, created_at=newer_at)
    store.threads = {child.id: child, parent.id: parent}
    store.parents[child.id] = parent.id
    store.hits = [hit(child), hit(parent)]
    assert [item.stored_thread_id for item in recall.recall(reader(), child.id, "query")] == [
        parent.id,
        child.id,
    ]


def test_equal_creation_times_sort_by_chain_then_chunk(memory):
    store, recall, _ = memory
    newest, older = stored(2, created_at=""), stored(1, created_at="")
    store.threads = {newest.id: newest, older.id: older}
    store.parents[newest.id] = older.id
    store.hits = [hit(older, 2), hit(newest, 3), hit(newest, 1), hit(older, 0)]
    result = recall.recall(reader(), newest.id, "query")
    assert [(item.stored_thread_id, item.chunk_index) for item in result] == [
        (newest.id, 1),
        (newest.id, 3),
        (older.id, 0),
        (older.id, 2),
    ]


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (0, None, (0, MAX_RANGE - 1)),
        (-10, None, (0, MAX_RANGE - 1)),
        (-10, 2, (0, 2)),
        (10, None, (10, 10 + MAX_RANGE - 1)),
        (10, 1000, (10, 10 + MAX_RANGE - 1)),
        (10, 12, (10, 12)),
        (10, 10, (10, 10)),
        (1000, 2000, (1000, 1000 + MAX_RANGE - 1)),
    ],
)
def test_get_clamps_inclusive_range_and_preserves_messages(memory, start, end, expected):
    store, recall, _ = memory
    present = stored(message_count=300)
    store.threads[present.id] = present
    store.rows[present.id] = [
        ThreadMessage(
            seq=index,
            role="tool",
            kind="tool_result",
            text=f"  Verbatim {index}\r\n日本語\t  ",
            raw=f'{{"result": "exact {index}"}}\n',
            name="read_file",
            call_id=f"call_{index}",
            at="2026-10-30T00:00:00Z",
        )
        for index in range(300)
    ]
    result = recall.get(reader(), present.id, start, end)
    first, last = expected
    assert result == store.rows[present.id][first : last + 1]
    assert len(result) <= MAX_RANGE
    assert store.calls[-1] == ("messages", present.id, first, last)


@pytest.mark.parametrize(("start", "end"), [(5, 4), (0, -1), (-5, -1)])
def test_get_inverted_range_returns_empty_after_access_check(memory, start, end):
    store, recall, _ = memory
    present = stored()
    store.threads[present.id] = present
    assert recall.get(reader(), present.id, start, end) == []
    assert store.calls == [("get", present.id)]
    with pytest.raises(ThreadNotFound, match="^thread not found$"):
        recall.get(reader(wings=()), present.id, start, end)


def test_fts_failure_is_not_silently_treated_as_no_results(memory, monkeypatch):
    store, recall, _ = memory
    present = stored()
    store.threads[present.id] = present

    def broken(*args, **kwargs):
        raise RuntimeError("index unavailable")

    monkeypatch.setattr(store, "search", broken)
    with pytest.raises(RuntimeError, match="index unavailable"):
        recall.recall(reader(), present.id, "query")
