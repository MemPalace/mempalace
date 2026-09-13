"""A failed re-mine must leave one complete generation readable."""

import contextlib
import copy

import pytest

from mempalace import convo_miner, mcp_server, searcher
from mempalace.ids import make_convo_commit_id, make_convo_drawer_id


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
