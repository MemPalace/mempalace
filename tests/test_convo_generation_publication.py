"""A failed re-mine must leave one complete generation readable."""

import contextlib
import copy

import pytest

from mempalace import convo_miner, mcp_server
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

    def mine(collection, documents):
        chunks = [{"content": text, "chunk_index": index} for index, text in enumerate(documents)]
        return convo_miner._file_chunks_locked(
            collection, source, chunks, "wing", "general", "agent", "exchange"
        )

    def visible(collection):
        result = []
        for index in range(3):
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
