"""Synthetic adapter contracts; these do not exercise remote services."""

import base64
import random
import sys
from types import SimpleNamespace

import pytest
from test_qdrant_backend import fake_qdrant as fake_qdrant

from mempalace.backends import BackendError, PalaceRef
from mempalace.backends.milvus import MilvusBackend
from mempalace.backends.qdrant import QdrantBackend
from mempalace.lexical import bm25_scores, tokenize
from mempalace.stored_text import IndexSource, index_text, prepare_stored_write


class _Schema:
    def __init__(self):
        self.fields = {}
        self.functions = []

    def add_field(self, *, field_name, **kwargs):
        self.fields[field_name] = {"name": field_name, **kwargs}

    def add_function(self, function):
        self.functions.append(vars(function))


class _IndexParams:
    def __init__(self):
        self.indexes = []

    def add_index(self, **kwargs):
        self.indexes.append(kwargs)


class _Iterator:
    def __init__(self, rows):
        self.rows = rows
        self.closed = False

    def next(self):
        rows, self.rows = self.rows, []
        return rows

    def close(self):
        self.closed = True


class _FakeMilvusClient:
    def __init__(self, **kwargs):
        self.collections = {}
        self.search_calls = []
        self.iterators = []
        self.created = []

    def has_collection(self, collection):
        return collection in self.collections

    def create_schema(self, **kwargs):
        assert kwargs == {"auto_id": False, "enable_dynamic_field": True}
        return _Schema()

    def prepare_index_params(self):
        return _IndexParams()

    def create_collection(self, *, collection_name, schema, **kwargs):
        self.created.append({"schema": schema, **kwargs})
        self.collections[collection_name] = {"schema": schema, "rows": {}}

    def describe_collection(self, collection):
        schema = self.collections[collection]["schema"]
        return {"fields": list(schema.fields.values()), "functions": schema.functions}

    def load_collection(self, collection):
        assert self.has_collection(collection)

    def upsert(self, *, collection_name, data):
        collection = self.collections[collection_name]
        for row in data:
            if "index_text" in collection["schema"].fields:
                assert "index_text" in row
            collection["rows"][row["id"]] = dict(row)

    def insert(self, *, collection_name, data):
        assert all(row["id"] not in self.collections[collection_name]["rows"] for row in data)
        self.upsert(collection_name=collection_name, data=data)

    def get(self, *, collection_name, ids, output_fields, **kwargs):
        rows = self.collections[collection_name]["rows"]
        return [{key: rows[id][key] for key in output_fields} for id in ids if id in rows]

    def query_iterator(self, *, collection_name, output_fields, filter="", **kwargs):
        rows = self._filtered_rows(collection_name, filter)
        iterator = _Iterator([{key: row[key] for key in output_fields} for row in rows])
        self.iterators.append(iterator)
        return iterator

    def _filtered_rows(self, collection_name, filter):
        rows = list(self.collections[collection_name]["rows"].values())
        if filter:
            assert filter == 'wing == "keep"'
            rows = [row for row in rows if row["metadata"].get("wing") == "keep"]
        return rows

    def search(self, **kwargs):
        self.search_calls.append(kwargs)
        rows = self._filtered_rows(kwargs["collection_name"], kwargs.get("filter", ""))
        if kwargs["anns_field"] == "sparse":
            scores = bm25_scores(kwargs["data"][0], [row["index_text"] for row in rows])
        else:
            scores = [1.0] * len(rows)
        hits = [
            {
                "id": row["id"],
                "distance": score,
                "entity": {key: row[key] for key in kwargs["output_fields"]},
            }
            for row, score in zip(rows, scores)
            if score > 0
        ]
        return [sorted(hits, key=lambda hit: hit["distance"], reverse=True)[: kwargs["limit"]]]


@pytest.fixture
def fake_milvus(monkeypatch):
    client = _FakeMilvusClient()
    module = SimpleNamespace(
        MilvusClient=lambda **kwargs: client,
        DataType=SimpleNamespace(VARCHAR=21, JSON=23, FLOAT_VECTOR=101, SPARSE_FLOAT_VECTOR=104),
        Function=SimpleNamespace,
        FunctionType=SimpleNamespace(BM25=1),
    )
    monkeypatch.setitem(sys.modules, "pymilvus", module)
    for name in ("URI", "TOKEN", "DB_NAME", "NAMESPACE", "CONSISTENCY_LEVEL"):
        monkeypatch.delenv(f"MEMPALACE_MILVUS_{name}", raising=False)
    return client


@pytest.fixture(params=["qdrant", "milvus"])
def remote(request, tmp_path, fake_qdrant, fake_milvus):
    backend = QdrantBackend() if request.param == "qdrant" else MilvusBackend()
    palace = PalaceRef(id=str(tmp_path), local_path=str(tmp_path))
    col = backend.get_collection(palace=palace, collection_name="drawers", create=True)
    return SimpleNamespace(
        kind=request.param, backend=backend, palace=palace, col=col, client=col._client
    )


def _blob():
    return base64.b64encode(random.Random(44).randbytes(300)).decode("ascii")


def _stored_rows(remote):
    collection = remote.client.collections[remote.col._remote_collection]
    if remote.kind == "qdrant":
        return [point["payload"] for point in collection["points"].values()]
    return list(collection["rows"].values())


def _reopen(remote):
    remote.col = remote.backend.get_collection(
        palace=remote.palace, collection_name="drawers", create=False
    )
    return remote.col


def _make_legacy(remote):
    """Model persisted collections created by the previous adapter schema."""
    if remote.kind == "qdrant":
        remote.client.created_indexes = [(remote.col._remote_collection, "document", "text")]
    else:
        schema = remote.client.collections[remote.col._remote_collection]["schema"]
        del schema.fields["index_text"]
        schema.fields["document"].update(enable_analyzer=True, enable_match=True)
        schema.functions[0]["input_field_names"] = ["document"]
    for row in _stored_rows(remote):
        del row["index_text"]
    return _reopen(remote)


@pytest.mark.parametrize("method", ["add", "upsert"])
def test_remote_projects_index_and_preserves_original_and_vectors(remote, method):
    original = f"needle 漢字\x00\udcff\n{_blob()}\nend"
    metadata = {"wing": "keep", "title": "escaped\x00title"}
    getattr(remote.col, method)(
        ids=["one"], documents=[original], metadatas=[metadata], embeddings=[[3, -4]]
    )
    [stored] = _stored_rows(remote)
    docs, _, _ = prepare_stored_write([original], ["one"], [metadata])
    assert stored["document"] == docs[0]
    assert stored["index_text"] == index_text(original, metadata, stored_form=False)
    assert "[base64 binary 300 B]" in stored["index_text"]
    assert _blob() not in stored["index_text"]

    got = remote.col.get(ids=["one"], include=["documents", "metadatas", "embeddings"])
    assert got.documents == [original]
    assert got.metadatas == [metadata]
    assert got.embeddings == [[3.0, -4.0]]
    result = remote.col.query(
        query_embeddings=[[1, 0]], include=["documents", "metadatas", "embeddings"]
    )
    assert result.documents == [[original]]
    assert result.embeddings == [[[3.0, -4.0]]]
    hits = remote.col.lexical_search(query="needle").hits
    assert [(hit.id, hit.document, hit.metadata) for hit in hits] == [("one", original, metadata)]


def test_remote_new_schema_and_reconnect_use_projected_lexical_index(remote):
    remote.col.upsert(ids=["one"], documents=[f"needle\n{_blob()}"], embeddings=[[1, 0]])
    if remote.kind == "qdrant":
        assert remote.client.created_indexes == [
            (remote.col._remote_collection, "index_text", "text")
        ]
        remote.client.scroll_calls.clear()
    else:
        created = remote.client.created[0]
        fields = created["schema"].fields
        assert not fields["document"].get("enable_analyzer")
        assert not fields["document"].get("enable_match")
        assert fields["index_text"]["enable_analyzer"]
        assert fields["index_text"]["enable_match"]
        assert fields["index_text"]["max_length"] == fields["document"]["max_length"]
        [function] = created["schema"].functions
        assert function["input_field_names"] == ["index_text"]
        assert function["output_field_names"] == ["sparse"]
        assert function["function_type"] == 1
        assert {index["field_name"] for index in created["index_params"].indexes} == {
            "vector",
            "sparse",
        }

    col = _reopen(remote)
    assert [hit.id for hit in col.lexical_search(query="needle").hits] == ["one"]
    assert col.lexical_search(query=max(tokenize(_blob()), key=len)).hits == []
    if remote.kind == "qdrant":
        assert remote.client.scroll_calls[0] == {
            "must": [{"key": "index_text", "match": {"text_any": "needle"}}]
        }
        assert len(remote.client.scroll_calls) == 2  # Empty matches do not trigger a scan.
    else:
        assert remote.client.search_calls[0]["anns_field"] == "sparse"
        assert remote.client.search_calls[0]["data"] == ["needle"]
        assert not remote.client.iterators


def test_remote_legacy_scans_projection_without_rebuilding_or_losing_old_rows(remote):
    original = f"needle old\x00\n{_blob()}"
    remote.col.upsert(
        ids=["old", "other"],
        documents=[original, "needle needle outside"],
        metadatas=[{"wing": "keep"}, {"wing": "other"}],
        embeddings=[[3, -4], [0, 1]],
    )
    col = _make_legacy(remote)
    hits = col.lexical_search(query="needle", where={"wing": "keep"}, n_results=1).hits
    assert [(hit.id, hit.document) for hit in hits] == [("old", original)]
    assert col.lexical_search(query=max(tokenize(_blob()), key=len)).hits == []
    col.upsert(
        ids=["new"], documents=["needle new"], metadatas=[{"wing": "keep"}], embeddings=[[1, 0]]
    )
    assert {hit.id for hit in col.lexical_search(query="needle", where={"wing": "keep"}).hits} == {
        "old",
        "new",
    }
    got = col.get(ids=["old"], include=["documents", "embeddings"])
    assert got.documents == [original]
    assert got.embeddings == [[3.0, -4.0]]
    if remote.kind == "qdrant":
        assert remote.client.created_indexes == [(col._remote_collection, "document", "text")]
        assert all("text_any" not in str(call) for call in remote.client.scroll_calls)
    else:
        assert len(remote.client.created) == 1
        assert "index_text" not in remote.client.created[0]["schema"].fields
        assert "index_text" not in _stored_rows(remote)[-1]
        assert not remote.client.search_calls
        assert remote.client.iterators and all(it.closed for it in remote.client.iterators)


@pytest.mark.parametrize("legacy", [False, True])
def test_remote_honors_continuation_span_hints(remote, legacy):
    source = _blob()
    fragment = source[150:250]
    metadata = IndexSource(source).metadata(150, 250)
    assert index_text(fragment, metadata, stored_form=False) == ""
    remote.col.upsert(
        ids=["fragment"], documents=[fragment], metadatas=[metadata], embeddings=[[1, 0]]
    )
    if legacy:
        _make_legacy(remote)
    else:
        assert _stored_rows(remote)[0]["index_text"] == ""
    assert remote.col.lexical_search(query=max(tokenize(fragment), key=len)).hits == []
    assert remote.col.get(ids=["fragment"]).documents == [fragment]


def test_remote_schema_inspection_failure_uses_sanitized_scan(remote, monkeypatch):
    remote.col.upsert(ids=["one"], documents=[f"needle\n{_blob()}"], embeddings=[[1, 0]])
    col = _reopen(remote)

    def fail(*args, **kwargs):
        raise BackendError("synthetic inspection error")

    method = "get_collection_info" if remote.kind == "qdrant" else "describe_collection"
    monkeypatch.setattr(remote.client, method, fail)
    assert [hit.id for hit in col.lexical_search(query="needle").hits] == ["one"]
    assert col.lexical_search(query=max(tokenize(_blob()), key=len)).hits == []


def test_remote_still_requires_explicit_embeddings(remote):
    with pytest.raises(ValueError, match="requires explicit embeddings"):
        remote.col.upsert(ids=["one"], documents=["needle"])
    with pytest.raises(ValueError, match="requires query_embeddings"):
        remote.col.query(query_texts=["needle"])
    assert remote.client.collections == {}


@pytest.mark.parametrize("remote", ["milvus"], indirect=True)
def test_milvus_old_bm25_binding_does_not_enable_projected_native_search(remote):
    remote.col.upsert(ids=["one"], documents=["needle"], embeddings=[[1, 0]])
    schema = remote.client.collections[remote.col._remote_collection]["schema"]
    schema.functions[0]["input_field_names"] = ["document"]
    col = _reopen(remote)
    assert not col._has_native_lexical()
    assert [hit.id for hit in col.lexical_search(query="needle").hits] == ["one"]
    assert not remote.client.search_calls
    col.upsert(ids=["two"], documents=["needle again"], embeddings=[[0, 1]])
    assert _stored_rows(remote)[-1]["index_text"] == "needle again"


@pytest.mark.parametrize("remote", ["qdrant"], indirect=True)
def test_qdrant_text_filter_failure_uses_sanitized_scan(remote, monkeypatch):
    remote.col.upsert(ids=["one"], documents=[f"needle\n{_blob()}"], embeddings=[[1, 0]])
    original_scroll = remote.client.scroll_points

    def scroll(*args, **kwargs):
        if "text_any" in str(kwargs.get("qdrant_filter")):
            raise BackendError("synthetic text-filter error")
        return original_scroll(*args, **kwargs)

    monkeypatch.setattr(remote.client, "scroll_points", scroll)
    assert [hit.id for hit in remote.col.lexical_search(query="needle").hits] == ["one"]
    assert remote.col.lexical_search(query=max(tokenize(_blob()), key=len)).hits == []
