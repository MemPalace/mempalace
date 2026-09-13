"""A failed re-mine must leave one complete generation readable."""

import contextlib
import copy

import pytest

from mempalace import convo_miner, mcp_server, searcher
from mempalace.ids import make_convo_commit_id, make_convo_drawer_id, make_convo_generation_id


class InjectedWriteFailure(RuntimeError):
    pass


class GenerationCollection:
    """In-memory rows with Chroma-style metadata merges and retained vectors."""

    def __init__(self):
        self.rows = {}
        self.write_count = 0
        self.fail_at = None
        self.fail_before = False
        self.observe = lambda: None
        self.embedded_documents = []

    def get(self, ids=None, where=None, include=None, offset=0, limit=None):
        rows = [
            (key, row)
            for key, row in self.rows.items()
            if (ids is None or key in ids)
            and (where is None or self._matches(row["metadata"], where))
        ]
        rows = rows[offset : offset + limit if limit is not None else None]
        result = {"ids": [key for key, _ in rows]}
        for field, stored in (
            ("documents", "document"),
            ("metadatas", "metadata"),
            ("embeddings", "embedding"),
        ):
            if include is None or field in include:
                result[field] = [copy.deepcopy(row[stored]) for _, row in rows]
        return result

    @classmethod
    def _matches(cls, metadata, where):
        for key, value in where.items():
            if key == "$and":
                return all(cls._matches(metadata, part) for part in value)
            if key == "$or":
                return any(cls._matches(metadata, part) for part in value)
            actual = metadata.get(key)
            if isinstance(value, dict):
                if "$in" in value and actual not in value["$in"]:
                    return False
                if "$ne" in value and actual == value["$ne"]:
                    return False
            elif actual != value:
                return False
        return True

    def _before_write(self):
        self.write_count += 1
        if self.fail_before and self.write_count == self.fail_at:
            raise InjectedWriteFailure("before write")

    def _after_write(self):
        self.observe()
        if not self.fail_before and self.write_count == self.fail_at:
            raise InjectedWriteFailure("after write")

    def upsert(self, ids, documents, metadatas, embeddings=None):
        self._before_write()
        for index, (key, document, metadata) in enumerate(zip(ids, documents, metadatas)):
            if embeddings is None and not metadata.get("mine_commit_marker"):
                self.embedded_documents.append(document)
            embedding = embeddings[index] if embeddings is not None else [float(len(document)), 1.0]
            self.rows[key] = {
                "document": document,
                "metadata": copy.deepcopy(metadata),
                "embedding": list(embedding),
            }
        self._after_write()

    def update(self, ids, metadatas):
        self._before_write()
        for key, metadata in zip(ids, metadatas):
            if key in self.rows:
                self.rows[key]["metadata"].update(copy.deepcopy(metadata))
        self._after_write()

    def delete(self, ids):
        self._before_write()
        for key in ids:
            self.rows.pop(key, None)
        self._after_write()


@pytest.fixture
def publication_case(tmp_path, monkeypatch):
    source = str(tmp_path / "session.txt")
    (tmp_path / "session.txt").write_text("transcript", encoding="utf-8")
    monkeypatch.setattr(convo_miner, "mine_lock", lambda *_: contextlib.nullcontext())
    monkeypatch.setattr(convo_miner, "file_already_mined", lambda *_a, **_k: False)
    monkeypatch.setattr(convo_miner, "_detect_hall_cached", lambda *_: "conversations")
    monkeypatch.setattr(convo_miner, "DRAWER_UPSERT_BATCH_SIZE", 1)

    def mine(collection, documents, access_gate=None):
        chunks = [{"content": text, "chunk_index": index} for index, text in enumerate(documents)]
        return convo_miner._file_chunks_locked(
            collection,
            source,
            chunks,
            "wing",
            "general",
            "agent",
            "exchange",
            access_gate=access_gate,
        )

    def visible(collection, count=3):
        result = []
        for index in range(count):
            logical_id = make_convo_drawer_id("wing", "general", source, "exchange", index)
            record = mcp_server._logical_generation_record(collection, logical_id)
            result.append(record["content"] if record else None)
        return result

    collection = GenerationCollection()
    initial = ["original first chunk", "unchanged middle chunk", "unchanged last chunk"]
    previous = ["first revision", *initial[1:]]
    assert mine(collection, initial)[2] is False
    assert mine(collection, previous)[2] is False
    assert visible(collection) == previous
    collection.write_count = 0
    collection.embedded_documents.clear()
    return collection, mine, visible, make_convo_commit_id(source, "exchange"), previous


def test_each_publication_write_leaves_a_complete_readable_generation(publication_case):
    collection, mine, visible, commit_id, previous = publication_case
    next_documents = ["second revision", *previous[1:]]
    old_token = collection.rows[commit_id]["metadata"]["mine_generation_commit"]
    snapshots = []

    def observe():
        token = collection.rows[commit_id]["metadata"]["mine_generation_commit"]
        snapshots.append((token, visible(collection)))

    collection.observe = observe
    assert mine(collection, next_documents)[2] is False
    assert snapshots
    for token, documents in snapshots:
        assert documents == (previous if token == old_token else next_documents)
    assert collection.embedded_documents == [next_documents[0]]


@pytest.mark.parametrize("fail_before", [True, False])
def test_interrupted_publication_and_retry_keep_complete_content(publication_case, fail_before):
    original, mine, visible, commit_id, previous = publication_case
    next_documents = ["second revision", *previous[1:]]
    old_token = original.rows[commit_id]["metadata"]["mine_generation_commit"]
    completed = copy.deepcopy(original)
    assert mine(completed, next_documents)[2] is False

    for write_index in range(1, completed.write_count + 1):
        collection = copy.deepcopy(original)
        collection.fail_before = fail_before
        collection.fail_at = write_index
        try:
            mine(collection, next_documents)
        except InjectedWriteFailure:
            pass
        token = collection.rows[commit_id]["metadata"]["mine_generation_commit"]
        assert visible(collection) == (previous if token == old_token else next_documents), (
            fail_before,
            write_index,
        )
        collection.fail_at = None
        assert mine(collection, next_documents)[2] is False
        assert visible(collection) == next_documents
        assert mine(collection, previous)[2] is False
        assert visible(collection) == previous


def _committed_tokens(collection):
    return searcher._committed_generation_tokens(collection)


def _sqlite_visible_rows(collection):
    """Python equivalent of the sqlite BM25 generation visibility clauses."""
    tokens = _committed_tokens(collection)
    visible = []
    for key, row in collection.rows.items():
        meta = row["metadata"]
        if meta.get("mine_commit_marker") is True:
            continue
        if searcher._is_staged_metadata(meta, tokens):
            continue
        token = meta.get("mine_generation_token")
        if token and token not in tokens:
            continue
        visible.append((key, row["document"], meta))
    return visible


def _list_visible_drawer_ids(collection):
    tokens = _committed_tokens(collection)
    ids, documents, metadatas = [], [], []
    for key, row in collection.rows.items():
        meta = row["metadata"]
        if meta.get("mine_commit_marker") is True:
            continue
        if meta.get("mine_staged") is True and meta.get("mine_generation_token") not in tokens:
            continue
        if meta.get("mine_generation_token") and meta.get("mine_generation_token") not in tokens:
            continue
        ids.append(key)
        documents.append(row["document"])
        metadatas.append(meta)
    return {
        drawer["drawer_id"]
        for drawer in mcp_server._collapse_drawer_rows(ids, documents, metadatas, tokens)
    }


def _search_visible_contents(collection):
    tokens = _committed_tokens(collection)
    rows = [
        (key, row["document"], row["metadata"])
        for key, row in collection.rows.items()
        if row["metadata"].get("mine_commit_marker") is not True
    ]
    collapsed = searcher._collapse_physical_generation_rows(rows, tokens)
    by_logical = {}
    for _physical_id, document, meta in collapsed:
        logical_id = meta.get("logical_drawer_id") or _physical_id
        by_logical[logical_id] = document
    return by_logical


def _assert_all_read_paths(collection, source, expected):
    get_contents = []
    visible_ids = []
    hidden_ids = []
    for index, content in enumerate(expected):
        logical_id = make_convo_drawer_id("wing", "general", source, "exchange", index)
        record = mcp_server._logical_generation_record(collection, logical_id)
        actual = record["content"] if record else None
        get_contents.append(actual)
        if content is None:
            hidden_ids.append(logical_id)
            assert actual is None
        else:
            visible_ids.append(logical_id)
            assert actual == content
    assert get_contents == expected

    search_by_logical = _search_visible_contents(collection)
    sqlite_by_logical = {
        meta.get("logical_drawer_id") or key: document
        for key, document, meta in searcher._collapse_physical_generation_rows(
            _sqlite_visible_rows(collection), _committed_tokens(collection)
        )
    }
    listed = _list_visible_drawer_ids(collection)
    assert listed == set(visible_ids)
    for logical_id, content in zip(visible_ids, [item for item in expected if item is not None]):
        assert search_by_logical.get(logical_id) == content
        assert sqlite_by_logical.get(logical_id) == content
    for logical_id in hidden_ids:
        assert logical_id not in listed
        assert logical_id not in search_by_logical
        assert logical_id not in sqlite_by_logical


class RecordingAccessGate:
    """Exclusive write bursts that hand off to a reader on every release."""

    def __init__(self, on_release=None):
        self.depth = 0
        self.write_sessions = 0
        self.ops_in_session = 0
        self.ids_in_session = 0
        self.max_ops_in_session = 0
        self.max_ids_in_session = 0
        self.reader_runs = 0
        self.on_release = on_release

    def note_op(self, n_ids):
        assert self.depth == 1
        self.ops_in_session += 1
        self.ids_in_session += n_ids

    @contextlib.contextmanager
    def read_lock(self):
        assert self.depth == 0
        self.reader_runs += 1
        yield

    @contextlib.contextmanager
    def write_lock(self):
        assert self.depth == 0
        self.depth = 1
        self.write_sessions += 1
        self.ops_in_session = 0
        self.ids_in_session = 0
        try:
            yield
        finally:
            self.max_ops_in_session = max(self.max_ops_in_session, self.ops_in_session)
            self.max_ids_in_session = max(self.max_ids_in_session, self.ids_in_session)
            self.depth = 0
            if self.on_release is not None:
                with self.read_lock():
                    self.on_release()


class GatedGenerationCollection(GenerationCollection):
    def __init__(self, gate):
        super().__init__()
        self.gate = gate

    def upsert(self, ids, documents, metadatas, embeddings=None):
        self.gate.note_op(len(ids))
        super().upsert(ids, documents, metadatas, embeddings)

    def update(self, ids, metadatas):
        self.gate.note_op(len(ids))
        super().update(ids, metadatas)

    def delete(self, ids):
        self.gate.note_op(len(ids))
        super().delete(ids)


def test_grown_rows_stay_hidden_when_later_shrink_delete_fails(tmp_path, monkeypatch):
    source = str(tmp_path / "session.txt")
    (tmp_path / "session.txt").write_text("transcript", encoding="utf-8")
    monkeypatch.setattr(convo_miner, "mine_lock", lambda *_: contextlib.nullcontext())
    monkeypatch.setattr(convo_miner, "file_already_mined", lambda *_a, **_k: False)
    monkeypatch.setattr(convo_miner, "_detect_hall_cached", lambda *_: "conversations")
    monkeypatch.setattr(convo_miner, "DRAWER_UPSERT_BATCH_SIZE", 1)

    def mine(collection, documents):
        chunks = [{"content": text, "chunk_index": index} for index, text in enumerate(documents)]
        return convo_miner._file_chunks_locked(
            collection, source, chunks, "wing", "general", "agent", "exchange"
        )

    class DeleteFailingCollection(GenerationCollection):
        def __init__(self):
            super().__init__()
            self.fail_delete = False

        def delete(self, ids):
            if self.fail_delete:
                raise InjectedWriteFailure("stale delete failed")
            super().delete(ids)

    initial = ["original first chunk", "unchanged middle chunk", "unchanged last chunk"]
    grown = [*initial, "appended fourth chunk", "appended fifth chunk"]
    hidden_after_shrink = [*initial, None, None]
    collection = DeleteFailingCollection()
    assert mine(collection, initial)[2] is False
    _assert_all_read_paths(collection, source, initial)
    assert all(
        not row["metadata"].get("mine_generation_token")
        for row in collection.rows.values()
        if row["metadata"].get("mine_commit_marker") is not True
    )

    collection.embedded_documents.clear()
    collection.write_count = 0
    assert mine(collection, grown)[2] is False
    _assert_all_read_paths(collection, source, grown)
    assert collection.embedded_documents == grown[3:]
    appended_logical = [
        make_convo_drawer_id("wing", "general", source, "exchange", index) for index in (3, 4)
    ]
    commit_id = make_convo_commit_id(source, "exchange")
    grow_token = collection.rows[commit_id]["metadata"]["mine_generation_commit"]
    appended_physical = [
        key
        for key, row in collection.rows.items()
        if row["metadata"].get("logical_drawer_id") in appended_logical
    ]
    assert appended_physical
    assert all(
        collection.rows[key]["metadata"].get("mine_generation_token") == grow_token
        for key in appended_physical
    )

    collection.embedded_documents.clear()
    collection.write_count = 0
    collection.fail_delete = True
    skipped = mine(collection, initial)[2]
    assert skipped is True
    assert all(key in collection.rows for key in appended_physical)
    _assert_all_read_paths(collection, source, hidden_after_shrink)
    assert collection.embedded_documents == []

    collection.fail_delete = False
    assert mine(collection, initial)[2] is False
    _assert_all_read_paths(collection, source, hidden_after_shrink)
    assert collection.embedded_documents == []


def test_publication_releases_access_gate_between_bounded_batches(monkeypatch):
    monkeypatch.setattr(convo_miner, "DRAWER_UPSERT_BATCH_SIZE", 2)
    snapshots = []
    gate = RecordingAccessGate(on_release=lambda: snapshots.append(True))
    collection = GatedGenerationCollection(gate)
    for index in range(5):
        collection.rows[f"keep-{index}"] = {
            "document": f"keep {index}",
            "metadata": {
                "logical_drawer_id": f"keep-{index}",
                "mine_staged": True,
                "mine_generation_token": "new-token",
            },
            "embedding": [1.0],
        }
        collection.rows[f"stale-{index}"] = {
            "document": f"stale {index}",
            "metadata": {
                "logical_drawer_id": f"stale-{index}",
                "mine_generation_token": "old-token",
            },
            "embedding": [1.0],
        }
    collection.rows["commit"] = {
        "document": "[commit]",
        "metadata": {
            "mine_staged": True,
            "mine_commit_marker": True,
            "mine_generation_commit": "old-token",
            "mine_cleanup_pending": True,
        },
        "embedding": [0.0],
    }
    marker = {
        "mine_staged": True,
        "mine_commit_marker": True,
        "mine_generation_commit": "new-token",
        "mine_cleanup_pending": True,
    }
    final_metadata = [
        (
            f"keep-{index}",
            {
                "logical_drawer_id": f"keep-{index}",
                "mine_staged": False,
                "mine_generation_token": "new-token",
            },
        )
        for index in range(5)
    ]
    stale_ids = [f"stale-{index}" for index in range(5)]
    collection.write_count = 0
    assert (
        convo_miner._publish_changed_generations(
            collection,
            final_metadata=final_metadata,
            stale_ids=stale_ids,
            commit_id="commit",
            commit_metadata=marker,
            source_file="chat.jsonl",
            access_gate=gate,
        )
        is True
    )
    # marker upsert + 3 metadata batches + 3 stale-delete batches + completion
    assert gate.write_sessions == 8
    assert gate.reader_runs == 8
    assert gate.max_ops_in_session == 1
    assert gate.max_ids_in_session <= 2
    assert snapshots == [True] * 8
    assert collection.rows["commit"]["metadata"]["mine_cleanup_pending"] is False
    assert "stale-0" not in collection.rows
    assert "stale-4" not in collection.rows


def test_reader_sees_complete_generation_between_publication_write_bursts(publication_case):
    collection, mine, visible, commit_id, previous = publication_case
    next_documents = ["second revision", *previous[1:]]
    old_token = collection.rows[commit_id]["metadata"]["mine_generation_commit"]
    snapshots = []
    gate = RecordingAccessGate(
        on_release=lambda: snapshots.append(
            (
                collection.rows[commit_id]["metadata"]["mine_generation_commit"],
                visible(collection),
            )
        )
    )
    gated = GatedGenerationCollection(gate)
    gated.rows = copy.deepcopy(collection.rows)
    assert mine(gated, next_documents, access_gate=gate)[2] is False
    assert gate.write_sessions > 1
    assert gate.max_ops_in_session == 1
    assert gate.max_ids_in_session <= 1
    assert snapshots
    for token, documents in snapshots:
        assert documents == (previous if token == old_token else next_documents)
    assert gated.embedded_documents == [next_documents[0]]


def _non_marker_rows(collection):
    return {
        key: row
        for key, row in collection.rows.items()
        if row["metadata"].get("mine_commit_marker") is not True
    }


def _snapshot_active_rows(collection):
    return {
        key: (list(row["embedding"]), row["document"], row["metadata"].get("mine_generation_token"))
        for key, row in _non_marker_rows(collection).items()
    }


def _snapshot_token_rows(collection, token):
    return {
        key: value for key, value in _snapshot_active_rows(collection).items() if value[2] == token
    }


def _plan_chunk(logical_id, chunk_hash, content, index, stored=None, rewritten=False):
    candidate = (logical_id, stored) if stored is not None else None
    if rewritten:
        candidates = [candidate] if candidate is not None else []
        matches = []
    elif candidate is not None:
        candidates = [candidate]
        matches = [candidate]
    else:
        candidates = []
        matches = []
    return {
        "chunk": {"content": content, "chunk_index": index},
        "chunk_room": "general",
        "logical_drawer_id": logical_id,
        "chunk_hash": chunk_hash,
        "candidates": candidates,
        "matches": matches,
    }


def _plan_writes(planned, existing, pending_cleanup=False, active_token=None):
    return convo_miner._plan_convo_generation_writes(
        planned,
        existing=existing,
        pending_cleanup=pending_cleanup,
        wing="wing",
        source_file="session.txt",
        agent="agent",
        filed_at="2026-09-13T00:00:00",
        authored_at=None,
        extract_mode="exchange",
        chunk_total=len(planned),
        source_mtime=1.0,
        content_hash=None,
        active_token=active_token,
    )


def test_plan_reuses_active_token_for_append_only_growth(monkeypatch):
    monkeypatch.setattr(convo_miner, "_detect_hall_cached", lambda *_: "conversations")
    active = "active-token"
    existing = {
        "drawer-a": {
            "logical_drawer_id": "drawer-a",
            "chunk_hash": "hash-a",
            "mine_generation_token": active,
            "mine_staged": False,
            "chunk_index": 0,
        },
        "drawer-b": {
            "logical_drawer_id": "drawer-b",
            "chunk_hash": "hash-b",
            "mine_generation_token": active,
            "mine_staged": False,
            "chunk_index": 1,
        },
    }
    planned = [
        _plan_chunk("drawer-a", "hash-a", "A", 0, existing["drawer-a"]),
        _plan_chunk("drawer-b", "hash-b", "B", 1, existing["drawer-b"]),
        _plan_chunk("drawer-c", "hash-c", "C", 2),
    ]
    to_upsert, to_touch, to_copy, new_ids, token, will_publish = _plan_writes(planned, existing)

    assert token == active
    assert will_publish is True
    assert to_copy == []
    assert {row_id for row_id, _ in to_touch} == {"drawer-a", "drawer-b"}
    assert [row_id for row_id, _, _ in to_upsert] == ["drawer-c"]
    assert new_ids == {"drawer-a", "drawer-b", "drawer-c"}

    content_set_token = convo_miner._content_set_generation_token(
        [(item["logical_drawer_id"], item["chunk_hash"]) for item in planned]
    )
    assert content_set_token != active


def test_plan_does_not_reuse_token_when_append_matches_retired_tokenless(monkeypatch):
    """Failed A→B cleanup leaves tokenless A beside committed B.

    A later B→A reversion plus append must not treat leftover A as an
    append-only match; that would retag A with B's token.
    """
    monkeypatch.setattr(convo_miner, "_detect_hall_cached", lambda *_: "conversations")
    active = "b-token"
    leftover_a = {
        "logical_drawer_id": "drawer-a",
        "chunk_hash": "hash-a",
        "mine_staged": False,
        "chunk_index": 0,
    }
    committed_b = {
        "logical_drawer_id": "drawer-a",
        "chunk_hash": "hash-b",
        "mine_generation_token": active,
        "mine_staged": False,
        "chunk_index": 0,
    }
    existing = {"drawer-a": leftover_a, "drawer-a-b": committed_b}
    planned = [
        {
            "chunk": {"content": "A", "chunk_index": 0},
            "chunk_room": "general",
            "logical_drawer_id": "drawer-a",
            "chunk_hash": "hash-a",
            "candidates": [("drawer-a", leftover_a), ("drawer-a-b", committed_b)],
            "matches": [("drawer-a", leftover_a)],
        },
        _plan_chunk("drawer-c", "hash-c", "C", 1),
    ]
    assert convo_miner._is_append_only_growth(planned, existing, active) is False
    to_upsert, to_touch, to_copy, new_ids, token, will_publish = _plan_writes(
        planned, existing, pending_cleanup=True
    )

    assert token != active
    assert will_publish is True
    assert "drawer-a-b" not in new_ids
    assert [row_id for row_id, _, _ in to_upsert] == ["drawer-c"]
    assert {row_id for row_id, _ in to_touch} == {"drawer-a"}
    assert to_copy == []
    content_set_token = convo_miner._content_set_generation_token(
        [(item["logical_drawer_id"], item["chunk_hash"]) for item in planned]
    )
    assert token == content_set_token


def test_plan_reuses_active_token_when_leftover_tokenless_is_not_the_match(monkeypatch):
    """True B appends stay incremental even if tokenless A was left behind."""
    monkeypatch.setattr(convo_miner, "_detect_hall_cached", lambda *_: "conversations")
    active = "b-token"
    leftover_a = {
        "logical_drawer_id": "drawer-a",
        "chunk_hash": "hash-a",
        "mine_staged": False,
        "chunk_index": 0,
    }
    committed_b = {
        "logical_drawer_id": "drawer-a",
        "chunk_hash": "hash-b",
        "mine_generation_token": active,
        "mine_staged": False,
        "chunk_index": 0,
    }
    existing = {"drawer-a": leftover_a, "drawer-a-b": committed_b}
    planned = [
        {
            "chunk": {"content": "B", "chunk_index": 0},
            "chunk_room": "general",
            "logical_drawer_id": "drawer-a",
            "chunk_hash": "hash-b",
            "candidates": [("drawer-a", leftover_a), ("drawer-a-b", committed_b)],
            "matches": [("drawer-a-b", committed_b)],
        },
        _plan_chunk("drawer-c", "hash-c", "C", 1),
    ]
    assert convo_miner._is_append_only_growth(planned, existing, active) is True
    to_upsert, to_touch, to_copy, new_ids, token, will_publish = _plan_writes(planned, existing)

    assert token == active
    assert will_publish is True
    assert to_copy == []
    assert {row_id for row_id, _ in to_touch} == {"drawer-a-b"}
    assert [row_id for row_id, _, _ in to_upsert] == ["drawer-c"]
    assert new_ids == {"drawer-a-b", "drawer-c"}
    assert "drawer-a" not in new_ids


def test_plan_reuses_active_token_when_pending_append_rows_are_still_staged(monkeypatch):
    """A crash after staging the tail must still reuse the active token."""
    monkeypatch.setattr(convo_miner, "_detect_hall_cached", lambda *_: "conversations")
    active = "active-token"
    existing = {
        "drawer-a": {
            "logical_drawer_id": "drawer-a",
            "chunk_hash": "hash-a",
            "mine_generation_token": active,
            "mine_staged": False,
        },
        "drawer-b": {
            "logical_drawer_id": "drawer-b",
            "chunk_hash": "hash-b",
            "mine_generation_token": active,
            "mine_staged": True,
        },
    }
    planned = [
        _plan_chunk("drawer-a", "hash-a", "A", 0, existing["drawer-a"]),
        _plan_chunk("drawer-b", "hash-b", "B", 1, existing["drawer-b"]),
        _plan_chunk("drawer-c", "hash-c", "C", 2),
    ]
    assert convo_miner._is_append_only_growth(planned, existing, active) is True
    to_upsert, to_touch, to_copy, new_ids, token, will_publish = _plan_writes(
        planned, existing, pending_cleanup=True
    )

    assert token == active
    assert will_publish is True
    assert to_copy == []
    assert {row_id for row_id, _ in to_touch} == {"drawer-a", "drawer-b"}
    assert [row_id for row_id, _, _ in to_upsert] == ["drawer-c"]
    assert new_ids == {"drawer-a", "drawer-b", "drawer-c"}


def test_plan_reuses_active_token_when_append_retry_has_pending_cleanup(monkeypatch):
    monkeypatch.setattr(convo_miner, "_detect_hall_cached", lambda *_: "conversations")
    active = "active-token"
    existing = {
        "drawer-a": {
            "logical_drawer_id": "drawer-a",
            "chunk_hash": "hash-a",
            "mine_generation_token": active,
            "mine_staged": False,
        }
    }
    planned = [
        _plan_chunk("drawer-a", "hash-a", "A", 0, existing["drawer-a"]),
        _plan_chunk("drawer-b", "hash-b", "B", 1),
    ]
    to_upsert, to_touch, to_copy, new_ids, token, will_publish = _plan_writes(
        planned, existing, pending_cleanup=True
    )

    assert token == active
    assert will_publish is True
    assert to_copy == []
    assert {row_id for row_id, _ in to_touch} == {"drawer-a"}
    assert [row_id for row_id, _, _ in to_upsert] == ["drawer-b"]
    assert new_ids == {"drawer-a", "drawer-b"}


def test_plan_reuses_marker_token_when_residual_rows_have_two_tokens(monkeypatch):
    """Failed T1→T2 cleanup leaves both tokens; the marker still names T2."""
    monkeypatch.setattr(convo_miner, "_detect_hall_cached", lambda *_: "conversations")
    t1, t2 = "t1-token", "t2-token"
    leftover_a = {
        "logical_drawer_id": "drawer-a",
        "chunk_hash": "hash-a1",
        "mine_generation_token": t1,
        "mine_staged": False,
        "chunk_index": 0,
    }
    leftover_b = {
        "logical_drawer_id": "drawer-b",
        "chunk_hash": "hash-b",
        "mine_generation_token": t1,
        "mine_staged": False,
        "chunk_index": 1,
    }
    active_a = {
        "logical_drawer_id": "drawer-a",
        "chunk_hash": "hash-a2",
        "mine_generation_token": t2,
        "mine_staged": False,
        "chunk_index": 0,
    }
    active_b = {
        "logical_drawer_id": "drawer-b",
        "chunk_hash": "hash-b",
        "mine_generation_token": t2,
        "mine_staged": False,
        "chunk_index": 1,
    }
    existing = {
        "drawer-a-t1": leftover_a,
        "drawer-b-t1": leftover_b,
        "drawer-a-t2": active_a,
        "drawer-b-t2": active_b,
    }
    planned = [
        {
            "chunk": {"content": "A2", "chunk_index": 0},
            "chunk_room": "general",
            "logical_drawer_id": "drawer-a",
            "chunk_hash": "hash-a2",
            "candidates": [("drawer-a-t1", leftover_a), ("drawer-a-t2", active_a)],
            "matches": [("drawer-a-t2", active_a)],
        },
        {
            "chunk": {"content": "B", "chunk_index": 1},
            "chunk_room": "general",
            "logical_drawer_id": "drawer-b",
            "chunk_hash": "hash-b",
            "candidates": [("drawer-b-t1", leftover_b), ("drawer-b-t2", active_b)],
            "matches": [("drawer-b-t1", leftover_b), ("drawer-b-t2", active_b)],
        },
        _plan_chunk("drawer-c", "hash-c", "C", 2),
    ]
    assert convo_miner._active_drawer_generation_token(existing) is None
    assert convo_miner._is_append_only_growth(planned, existing, t2) is True
    assert convo_miner._is_append_only_growth(planned, existing, t1) is False

    to_upsert, to_touch, to_copy, new_ids, token, will_publish = _plan_writes(
        planned, existing, pending_cleanup=True, active_token=t2
    )
    assert token == t2
    assert will_publish is True
    assert to_copy == []
    assert {row_id for row_id, _ in to_touch} == {"drawer-a-t2", "drawer-b-t2"}
    assert [row_id for row_id, _, _ in to_upsert] == ["drawer-c"]
    assert new_ids == {"drawer-a-t2", "drawer-b-t2", "drawer-c"}
    assert "drawer-a-t1" not in new_ids
    assert "drawer-b-t1" not in new_ids

    _, _, inferred_copies, inferred_ids, inferred_token, _ = _plan_writes(
        planned, existing, pending_cleanup=True
    )
    assert inferred_token != t2
    assert inferred_copies
    assert "drawer-a-t1" not in inferred_ids
    assert "drawer-b-t1" not in inferred_ids

    same_set = planned[:2]
    assert convo_miner._is_append_only_growth(same_set, existing, t2) is True
    retry_upsert, retry_touch, retry_copy, retry_ids, retry_token, _ = _plan_writes(
        same_set, existing, pending_cleanup=True, active_token=t2
    )
    assert retry_token == t2
    assert retry_copy == []
    assert retry_upsert == []
    assert {row_id for row_id, _ in retry_touch} == {"drawer-a-t2", "drawer-b-t2"}
    assert retry_ids == {"drawer-a-t2", "drawer-b-t2"}


def test_plan_reuses_marker_token_when_shrink_left_retired_logical_ids(monkeypatch):
    """Failed T1→T2 shrink cleanup leaves a logical id T2 dropped.

    The append-only subset check must ignore that retired id. A later
    append that reuses the dropped index with new content must also ignore
    the leftover candidate, or every append would clone unchanged T2 rows.
    """
    monkeypatch.setattr(convo_miner, "_detect_hall_cached", lambda *_: "conversations")
    t1, t2 = "t1-token", "t2-token"
    leftover_a = {
        "logical_drawer_id": "drawer-a",
        "chunk_hash": "hash-a1",
        "mine_generation_token": t1,
        "mine_staged": False,
        "chunk_index": 0,
    }
    leftover_b = {
        "logical_drawer_id": "drawer-b",
        "chunk_hash": "hash-b",
        "mine_generation_token": t1,
        "mine_staged": False,
        "chunk_index": 1,
    }
    leftover_c = {
        "logical_drawer_id": "drawer-c",
        "chunk_hash": "hash-c1",
        "mine_generation_token": t1,
        "mine_staged": False,
        "chunk_index": 2,
    }
    active_a = {
        "logical_drawer_id": "drawer-a",
        "chunk_hash": "hash-a2",
        "mine_generation_token": t2,
        "mine_staged": False,
        "chunk_index": 0,
    }
    active_b = {
        "logical_drawer_id": "drawer-b",
        "chunk_hash": "hash-b",
        "mine_generation_token": t2,
        "mine_staged": False,
        "chunk_index": 1,
    }
    existing = {
        "drawer-a-t1": leftover_a,
        "drawer-b-t1": leftover_b,
        "drawer-c-t1": leftover_c,
        "drawer-a-t2": active_a,
        "drawer-b-t2": active_b,
    }
    planned_a = {
        "chunk": {"content": "A2", "chunk_index": 0},
        "chunk_room": "general",
        "logical_drawer_id": "drawer-a",
        "chunk_hash": "hash-a2",
        "candidates": [("drawer-a-t1", leftover_a), ("drawer-a-t2", active_a)],
        "matches": [("drawer-a-t2", active_a)],
    }
    planned_b = {
        "chunk": {"content": "B", "chunk_index": 1},
        "chunk_room": "general",
        "logical_drawer_id": "drawer-b",
        "chunk_hash": "hash-b",
        "candidates": [("drawer-b-t1", leftover_b), ("drawer-b-t2", active_b)],
        "matches": [("drawer-b-t1", leftover_b), ("drawer-b-t2", active_b)],
    }

    planned_partial = [planned_a, planned_b, _plan_chunk("drawer-d", "hash-d", "D", 2)]
    assert convo_miner._active_drawer_generation_token(existing) is None
    assert convo_miner._is_append_only_growth(planned_partial, existing, t2) is True
    assert convo_miner._is_append_only_growth(planned_partial, existing, t1) is False

    to_upsert, to_touch, to_copy, new_ids, token, will_publish = _plan_writes(
        planned_partial, existing, pending_cleanup=True, active_token=t2
    )
    assert token == t2
    assert will_publish is True
    assert to_copy == []
    assert {row_id for row_id, _ in to_touch} == {"drawer-a-t2", "drawer-b-t2"}
    assert [row_id for row_id, _, _ in to_upsert] == ["drawer-d"]
    assert new_ids == {"drawer-a-t2", "drawer-b-t2", "drawer-d"}
    assert "drawer-c-t1" not in new_ids
    assert "drawer-a-t1" not in new_ids
    assert "drawer-b-t1" not in new_ids

    planned_reused_index = [
        planned_a,
        planned_b,
        {
            "chunk": {"content": "C2", "chunk_index": 2},
            "chunk_room": "general",
            "logical_drawer_id": "drawer-c",
            "chunk_hash": "hash-c2",
            "candidates": [("drawer-c-t1", leftover_c)],
            "matches": [],
        },
    ]
    assert convo_miner._is_append_only_growth(planned_reused_index, existing, t2) is True
    assert convo_miner._is_append_only_growth(planned_reused_index, existing, t1) is False
    reused_upsert, reused_touch, reused_copy, reused_ids, reused_token, _ = _plan_writes(
        planned_reused_index, existing, pending_cleanup=True, active_token=t2
    )
    assert reused_token == t2
    assert reused_copy == []
    assert {row_id for row_id, _ in reused_touch} == {"drawer-a-t2", "drawer-b-t2"}
    assert [row_id for row_id, _, _ in reused_upsert] == [
        make_convo_generation_id("drawer-c", "hash-c2")
    ]
    assert "drawer-c-t1" not in reused_ids
    assert "drawer-a-t2" in reused_ids
    assert "drawer-b-t2" in reused_ids


def test_plan_clones_reused_rows_when_shrinking(monkeypatch):
    monkeypatch.setattr(convo_miner, "_detect_hall_cached", lambda *_: "conversations")
    active = "active-token"
    existing = {
        "drawer-a": {
            "logical_drawer_id": "drawer-a",
            "chunk_hash": "hash-a",
            "mine_generation_token": active,
            "mine_staged": False,
        },
        "drawer-b": {
            "logical_drawer_id": "drawer-b",
            "chunk_hash": "hash-b",
            "mine_generation_token": active,
            "mine_staged": False,
        },
        "drawer-c": {
            "logical_drawer_id": "drawer-c",
            "chunk_hash": "hash-c",
            "mine_generation_token": active,
            "mine_staged": False,
        },
    }
    planned = [
        _plan_chunk("drawer-a", "hash-a", "A", 0, existing["drawer-a"]),
        _plan_chunk("drawer-b", "hash-b", "B", 1, existing["drawer-b"]),
    ]
    to_upsert, to_touch, to_copy, new_ids, token, will_publish = _plan_writes(planned, existing)

    assert will_publish is True
    assert token != active
    assert to_upsert == []
    assert to_touch == []
    assert {row_id for row_id, _, _, _ in to_copy} == {
        convo_miner._reused_generation_copy_id("drawer-a", token),
        convo_miner._reused_generation_copy_id("drawer-b", token),
    }
    assert "drawer-c" not in new_ids
    assert "drawer-a" not in new_ids
    assert "drawer-b" not in new_ids


def test_plan_clones_unchanged_rows_when_rewriting(monkeypatch):
    monkeypatch.setattr(convo_miner, "_detect_hall_cached", lambda *_: "conversations")
    active = "active-token"
    existing = {
        "drawer-a": {
            "logical_drawer_id": "drawer-a",
            "chunk_hash": "hash-a",
            "mine_generation_token": active,
            "mine_staged": False,
        },
        "drawer-b": {
            "logical_drawer_id": "drawer-b",
            "chunk_hash": "hash-b",
            "mine_generation_token": active,
            "mine_staged": False,
        },
    }
    planned = [
        _plan_chunk("drawer-a", "hash-a2", "A2", 0, existing["drawer-a"], rewritten=True),
        _plan_chunk("drawer-b", "hash-b", "B", 1, existing["drawer-b"]),
    ]
    to_upsert, to_touch, to_copy, new_ids, token, will_publish = _plan_writes(planned, existing)

    assert will_publish is True
    assert token != active
    assert [row_id for row_id, _, _ in to_upsert] == [
        make_convo_generation_id("drawer-a", "hash-a2")
    ]
    assert to_touch == []
    assert [row_id for row_id, _, _, _ in to_copy] == [
        convo_miner._reused_generation_copy_id("drawer-b", token)
    ]
    assert "drawer-a" not in new_ids
    assert "drawer-b" not in new_ids


def test_repeated_appends_keep_active_rows_in_place_and_hide_on_failed_shrink(
    tmp_path, monkeypatch
):
    source = str(tmp_path / "session.txt")
    (tmp_path / "session.txt").write_text("transcript", encoding="utf-8")
    monkeypatch.setattr(convo_miner, "mine_lock", lambda *_: contextlib.nullcontext())
    monkeypatch.setattr(convo_miner, "file_already_mined", lambda *_a, **_k: False)
    monkeypatch.setattr(convo_miner, "_detect_hall_cached", lambda *_: "conversations")
    monkeypatch.setattr(convo_miner, "DRAWER_UPSERT_BATCH_SIZE", 1)

    def mine(collection, documents):
        chunks = [{"content": text, "chunk_index": index} for index, text in enumerate(documents)]
        return convo_miner._file_chunks_locked(
            collection, source, chunks, "wing", "general", "agent", "exchange"
        )

    class TrackingCollection(GenerationCollection):
        def __init__(self):
            super().__init__()
            self.upserted_ids = []
            self.deleted_ids = []
            self.fail_delete = False

        def upsert(self, ids, documents, metadatas, embeddings=None):
            self.upserted_ids.extend(ids)
            super().upsert(ids, documents, metadatas, embeddings)

        def delete(self, ids):
            self.deleted_ids.extend(ids)
            if self.fail_delete:
                raise InjectedWriteFailure("stale delete failed")
            super().delete(ids)

    initial = ["original first chunk", "unchanged middle chunk", "unchanged last chunk"]
    fourth_fifth = ["appended fourth chunk", "appended fifth chunk"]
    sixth_seventh = ["appended sixth", "appended seventh"]
    eighth_ninth = ["appended eighth", "appended ninth"]
    grown = [
        [*initial, *fourth_fifth],
        [*initial, *fourth_fifth, *sixth_seventh],
        [*initial, *fourth_fifth, *sixth_seventh, *eighth_ninth],
    ]
    collection = TrackingCollection()
    assert mine(collection, initial)[2] is False
    _assert_all_read_paths(collection, source, initial)

    previous_active = None
    grow_token = None
    for documents in grown:
        if previous_active is not None:
            collection.upserted_ids.clear()
            collection.deleted_ids.clear()
            collection.embedded_documents.clear()
            collection.write_count = 0
        assert mine(collection, documents)[2] is False
        _assert_all_read_paths(collection, source, documents)
        current_active = _snapshot_active_rows(collection)
        if previous_active is not None:
            for key, (embedding, document, token) in previous_active.items():
                assert key in current_active
                assert current_active[key][0] == embedding
                assert current_active[key][1] == document
                assert current_active[key][2] == token
                assert key not in collection.upserted_ids
                assert key not in collection.deleted_ids
            assert collection.deleted_ids == []
            assert collection.embedded_documents == documents[len(previous_active) :]
        commit_id = make_convo_commit_id(source, "exchange")
        grow_token = collection.rows[commit_id]["metadata"]["mine_generation_commit"]
        appended = [
            key
            for key, row in _non_marker_rows(collection).items()
            if row["metadata"].get("logical_drawer_id")
            not in {
                make_convo_drawer_id("wing", "general", source, "exchange", index)
                for index in range(3)
            }
        ]
        assert appended
        assert all(
            collection.rows[key]["metadata"].get("mine_generation_token") == grow_token
            for key in appended
        )
        previous_active = current_active

    appended_physical = [
        key
        for key, row in _non_marker_rows(collection).items()
        if row["metadata"].get("logical_drawer_id")
        not in {
            make_convo_drawer_id("wing", "general", source, "exchange", index) for index in range(3)
        }
    ]
    hidden_after_shrink = [*initial, *[None] * (len(grown[-1]) - 3)]
    collection.embedded_documents.clear()
    collection.upserted_ids.clear()
    collection.deleted_ids.clear()
    collection.fail_delete = True
    skipped = mine(collection, initial)[2]
    assert skipped is True
    assert all(key in collection.rows for key in appended_physical)
    _assert_all_read_paths(collection, source, hidden_after_shrink)
    assert collection.embedded_documents == []

    collection.fail_delete = False
    assert mine(collection, initial)[2] is False
    _assert_all_read_paths(collection, source, hidden_after_shrink)
    assert collection.embedded_documents == []


def test_failed_rewrite_cleanup_then_revert_append_exposes_newest_text(tmp_path, monkeypatch):
    """Legacy A → B cleanup fail, then A+append cleanup fail, must show A+append.

    If append-only reuse retags tokenless A with B's token and the B delete
    fails again, logical reads would prefer newer-filed B and expose obsolete
    text. Newest wanted text must be visible immediately and after retry.
    """
    source = str(tmp_path / "session.txt")
    (tmp_path / "session.txt").write_text("transcript", encoding="utf-8")
    monkeypatch.setattr(convo_miner, "mine_lock", lambda *_: contextlib.nullcontext())
    monkeypatch.setattr(convo_miner, "file_already_mined", lambda *_a, **_k: False)
    monkeypatch.setattr(convo_miner, "_detect_hall_cached", lambda *_: "conversations")
    monkeypatch.setattr(convo_miner, "DRAWER_UPSERT_BATCH_SIZE", 1)

    def mine(collection, documents):
        chunks = [{"content": text, "chunk_index": index} for index, text in enumerate(documents)]
        return convo_miner._file_chunks_locked(
            collection, source, chunks, "wing", "general", "agent", "exchange"
        )

    class DeleteFailingCollection(GenerationCollection):
        def __init__(self):
            super().__init__()
            self.fail_delete = False

        def delete(self, ids):
            if self.fail_delete:
                raise InjectedWriteFailure("stale delete failed")
            super().delete(ids)

    legacy_a = ["alpha first chunk", "alpha second chunk", "alpha third chunk"]
    rewritten_b = ["beta first chunk", "beta second chunk", "beta third chunk"]
    wanted = [*legacy_a, "alpha appended chunk"]
    collection = DeleteFailingCollection()
    assert mine(collection, legacy_a)[2] is False
    _assert_all_read_paths(collection, source, legacy_a)
    tokenless_a_ids = [
        key
        for key, row in _non_marker_rows(collection).items()
        if not row["metadata"].get("mine_generation_token")
    ]
    assert tokenless_a_ids

    collection.fail_delete = True
    skipped = mine(collection, rewritten_b)[2]
    assert skipped is True
    assert all(key in collection.rows for key in tokenless_a_ids)
    _assert_all_read_paths(collection, source, rewritten_b)
    commit_id = make_convo_commit_id(source, "exchange")
    b_token = collection.rows[commit_id]["metadata"]["mine_generation_commit"]
    assert b_token
    assert all(
        not collection.rows[key]["metadata"].get("mine_generation_token") for key in tokenless_a_ids
    )
    b_ids = [
        key
        for key, row in _non_marker_rows(collection).items()
        if row["metadata"].get("mine_generation_token") == b_token
    ]
    assert b_ids

    collection.fail_delete = True
    skipped = mine(collection, wanted)[2]
    assert skipped is True
    assert all(key in collection.rows for key in b_ids)
    for key in tokenless_a_ids:
        assert collection.rows[key]["metadata"].get("mine_generation_token") != b_token
    _assert_all_read_paths(collection, source, wanted)

    collection.fail_delete = False
    assert mine(collection, wanted)[2] is False
    _assert_all_read_paths(collection, source, wanted)
    assert all(key not in collection.rows for key in b_ids)


def test_failed_tokened_rewrite_cleanup_then_repeated_appends_keep_active_rows(
    tmp_path, monkeypatch
):
    """Tokened T1→T2 rewrite delete failure must not re-clone on later appends.

    Residual rows then carry both tokens, so inferring the active token from
    them returns None even though the commit marker names T2. Subsequent
    appends must keep T2's physical ids/embeddings and only insert new tails
    until cleanup retry finally deletes T1.
    """
    source = str(tmp_path / "session.txt")
    (tmp_path / "session.txt").write_text("transcript", encoding="utf-8")
    monkeypatch.setattr(convo_miner, "mine_lock", lambda *_: contextlib.nullcontext())
    monkeypatch.setattr(convo_miner, "file_already_mined", lambda *_a, **_k: False)
    monkeypatch.setattr(convo_miner, "_detect_hall_cached", lambda *_: "conversations")
    monkeypatch.setattr(convo_miner, "DRAWER_UPSERT_BATCH_SIZE", 1)

    def mine(collection, documents):
        chunks = [{"content": text, "chunk_index": index} for index, text in enumerate(documents)]
        return convo_miner._file_chunks_locked(
            collection, source, chunks, "wing", "general", "agent", "exchange"
        )

    class TrackingCollection(GenerationCollection):
        def __init__(self):
            super().__init__()
            self.upserted_ids = []
            self.deleted_ids = []
            self.fail_delete = False

        def upsert(self, ids, documents, metadatas, embeddings=None):
            self.upserted_ids.extend(ids)
            super().upsert(ids, documents, metadatas, embeddings)

        def delete(self, ids):
            self.deleted_ids.extend(ids)
            if self.fail_delete:
                raise InjectedWriteFailure("stale delete failed")
            super().delete(ids)

    initial = ["original first chunk", "unchanged middle chunk", "unchanged last chunk"]
    t1_docs = ["first revision", *initial[1:]]
    t2_docs = ["second revision", *initial[1:]]
    grown = [
        [*t2_docs, "appended fourth chunk", "appended fifth chunk"],
        [
            *t2_docs,
            "appended fourth chunk",
            "appended fifth chunk",
            "appended sixth",
            "appended seventh",
        ],
        [
            *t2_docs,
            "appended fourth chunk",
            "appended fifth chunk",
            "appended sixth",
            "appended seventh",
            "appended eighth",
            "appended ninth",
        ],
    ]
    collection = TrackingCollection()
    assert mine(collection, initial)[2] is False
    _assert_all_read_paths(collection, source, initial)
    assert mine(collection, t1_docs)[2] is False
    _assert_all_read_paths(collection, source, t1_docs)
    commit_id = make_convo_commit_id(source, "exchange")
    t1 = collection.rows[commit_id]["metadata"]["mine_generation_commit"]
    t1_ids = set(_snapshot_token_rows(collection, t1))
    assert t1_ids

    collection.embedded_documents.clear()
    collection.upserted_ids.clear()
    collection.deleted_ids.clear()
    collection.fail_delete = True
    skipped = mine(collection, t2_docs)[2]
    assert skipped is True
    assert all(key in collection.rows for key in t1_ids)
    _assert_all_read_paths(collection, source, t2_docs)
    t2 = collection.rows[commit_id]["metadata"]["mine_generation_commit"]
    assert t2
    assert t2 != t1
    residual = {key: row["metadata"] for key, row in _non_marker_rows(collection).items()}
    assert convo_miner._active_drawer_generation_token(residual) is None
    assert (
        convo_miner._committed_marker_generation_token([collection.rows[commit_id]["metadata"]])
        == t2
    )
    t2_active = _snapshot_token_rows(collection, t2)
    assert t2_active
    assert t1_ids.isdisjoint(t2_active)

    previous_active = t2_active
    for documents in grown:
        collection.upserted_ids.clear()
        collection.deleted_ids.clear()
        collection.embedded_documents.clear()
        skipped = mine(collection, documents)[2]
        assert skipped is True
        _assert_all_read_paths(collection, source, documents)
        assert collection.rows[commit_id]["metadata"]["mine_generation_commit"] == t2
        assert all(key in collection.rows for key in t1_ids)
        current_active = _snapshot_token_rows(collection, t2)
        for key, (embedding, document, token) in previous_active.items():
            assert key in current_active
            assert current_active[key][0] == embedding
            assert current_active[key][1] == document
            assert current_active[key][2] == token
            assert key not in collection.upserted_ids
            assert key not in collection.deleted_ids
        assert collection.embedded_documents == documents[len(previous_active) :]
        previous_active = current_active

    collection.fail_delete = False
    collection.embedded_documents.clear()
    collection.upserted_ids.clear()
    collection.deleted_ids.clear()
    assert mine(collection, grown[-1])[2] is False
    _assert_all_read_paths(collection, source, grown[-1])
    assert all(key not in collection.rows for key in t1_ids)
    for key, (embedding, document, token) in t2_active.items():
        row = collection.rows[key]
        assert list(row["embedding"]) == embedding
        assert row["document"] == document
        assert row["metadata"].get("mine_generation_token") == token
    assert collection.embedded_documents == []
    assert collection.rows[commit_id]["metadata"]["mine_generation_commit"] == t2


def test_failed_shrink_cleanup_then_repeated_appends_keep_active_rows(tmp_path, monkeypatch):
    """Failed T1→T2 shrink cleanup leaves extra retired logical ids.

    Later appends to T2 must not clone or delete unchanged T2 rows while
    cleanup keeps failing. Visible text stays on T2 plus the new tails,
    including when an append reuses a dropped T1 index with new content.
    A later cleanup retry finally removes T1.
    """
    source = str(tmp_path / "session.txt")
    (tmp_path / "session.txt").write_text("transcript", encoding="utf-8")
    monkeypatch.setattr(convo_miner, "mine_lock", lambda *_: contextlib.nullcontext())
    monkeypatch.setattr(convo_miner, "file_already_mined", lambda *_a, **_k: False)
    monkeypatch.setattr(convo_miner, "_detect_hall_cached", lambda *_: "conversations")
    monkeypatch.setattr(convo_miner, "DRAWER_UPSERT_BATCH_SIZE", 1)

    def mine(collection, documents):
        chunks = [{"content": text, "chunk_index": index} for index, text in enumerate(documents)]
        return convo_miner._file_chunks_locked(
            collection, source, chunks, "wing", "general", "agent", "exchange"
        )

    class TrackingCollection(GenerationCollection):
        def __init__(self):
            super().__init__()
            self.upserted_ids = []
            self.deleted_ids = []
            self.fail_delete = False

        def upsert(self, ids, documents, metadatas, embeddings=None):
            self.upserted_ids.extend(ids)
            super().upsert(ids, documents, metadatas, embeddings)

        def delete(self, ids):
            self.deleted_ids.extend(ids)
            if self.fail_delete:
                raise InjectedWriteFailure("stale delete failed")
            super().delete(ids)

    initial = ["original first chunk", "unchanged middle chunk", "unchanged last chunk"]
    t1_docs = [*initial, "t1 extra fourth chunk", "t1 extra fifth chunk"]
    t2_docs = ["second revision", *initial[1:]]
    grown = [
        [*t2_docs, "appended fourth chunk"],
        [*t2_docs, "appended fourth chunk", "appended fifth chunk"],
        [
            *t2_docs,
            "appended fourth chunk",
            "appended fifth chunk",
            "appended sixth",
        ],
    ]
    collection = TrackingCollection()
    assert mine(collection, initial)[2] is False
    _assert_all_read_paths(collection, source, initial)
    assert mine(collection, t1_docs)[2] is False
    _assert_all_read_paths(collection, source, t1_docs)
    commit_id = make_convo_commit_id(source, "exchange")
    t1 = collection.rows[commit_id]["metadata"]["mine_generation_commit"]
    t1_ids = set(_snapshot_token_rows(collection, t1))
    assert t1_ids
    t1_extra_logical = [
        make_convo_drawer_id("wing", "general", source, "exchange", index) for index in (3, 4)
    ]
    t1_extra_ids = [
        key
        for key, row in _non_marker_rows(collection).items()
        if row["metadata"].get("logical_drawer_id") in t1_extra_logical
    ]
    assert len(t1_extra_ids) == 2

    collection.embedded_documents.clear()
    collection.upserted_ids.clear()
    collection.deleted_ids.clear()
    collection.fail_delete = True
    skipped = mine(collection, t2_docs)[2]
    assert skipped is True
    assert all(key in collection.rows for key in t1_ids)
    assert all(key in collection.rows for key in t1_extra_ids)
    _assert_all_read_paths(collection, source, [*t2_docs, None, None])
    t2 = collection.rows[commit_id]["metadata"]["mine_generation_commit"]
    assert t2
    assert t2 != t1
    residual = {key: row["metadata"] for key, row in _non_marker_rows(collection).items()}
    assert convo_miner._active_drawer_generation_token(residual) is None
    assert (
        convo_miner._committed_marker_generation_token([collection.rows[commit_id]["metadata"]])
        == t2
    )
    t2_active = _snapshot_token_rows(collection, t2)
    assert t2_active
    assert t1_ids.isdisjoint(t2_active)
    assert all(
        collection.rows[key]["metadata"].get("mine_generation_token") == t1 for key in t1_extra_ids
    )

    previous_active = t2_active
    for documents in grown:
        collection.upserted_ids.clear()
        collection.deleted_ids.clear()
        collection.embedded_documents.clear()
        skipped = mine(collection, documents)[2]
        assert skipped is True
        hidden = [None] * max(0, len(t1_docs) - len(documents))
        _assert_all_read_paths(collection, source, [*documents, *hidden])
        assert collection.rows[commit_id]["metadata"]["mine_generation_commit"] == t2
        assert all(key in collection.rows for key in t1_ids)
        current_active = _snapshot_token_rows(collection, t2)
        for key, (embedding, document, token) in previous_active.items():
            assert key in current_active
            assert current_active[key][0] == embedding
            assert current_active[key][1] == document
            assert current_active[key][2] == token
            assert key not in collection.upserted_ids
            assert key not in collection.deleted_ids
        assert collection.embedded_documents == documents[len(previous_active) :]
        previous_active = current_active

    collection.fail_delete = False
    collection.embedded_documents.clear()
    collection.upserted_ids.clear()
    collection.deleted_ids.clear()
    assert mine(collection, grown[-1])[2] is False
    _assert_all_read_paths(collection, source, grown[-1])
    assert all(key not in collection.rows for key in t1_ids)
    for key, (embedding, document, token) in t2_active.items():
        row = collection.rows[key]
        assert list(row["embedding"]) == embedding
        assert row["document"] == document
        assert row["metadata"].get("mine_generation_token") == token
    assert collection.embedded_documents == []
    assert collection.rows[commit_id]["metadata"]["mine_generation_commit"] == t2
    assert collection.deleted_ids
    assert all(key not in collection.deleted_ids for key in t2_active)
