"""Tests for the lexical row cache of the Qdrant backend (``_qdrant_lexical_cache``).

Runs the real ``QdrantCollection.lexical_search``: with ``MEMPALACE_QDRANT_LEXICAL_CACHE_TTL_S``
unset or ``0`` (the default, the cache is opt-in) it takes the original full-scroll path, with a
positive value the cached path -- both against the same in-memory Qdrant, so equivalence is
checked on the package's own code.
"""

import random
from types import SimpleNamespace

import pytest

from mempalace.backends import _qdrant_lexical_cache as lc, qdrant as q

WORDS = (
    "irion tesla laden zuhause work seo keywords schlaf energie ziele kunde termin größe".split()
)
TTL = "MEMPALACE_QDRANT_LEXICAL_CACHE_TTL_S"
CACHE_ON = "300"
URL = "http://fake:6333"
REMOTE = "fake_closets"


def _matches(point, flt):
    for cond in (flt or {}).get("must", []):
        if "must" in cond:
            if not _matches(point, cond):
                return False
        elif "has_id" in cond:
            if point["id"] not in cond["has_id"]:
                return False
        elif "text_any" in cond["match"]:
            doc = point["payload"][q._PAYLOAD_DOCUMENT]
            if not set(cond["match"]["text_any"].split()) & set(q.tokenize(doc)):
                return False
        else:
            meta = point["payload"][q._PAYLOAD_METADATA]
            if meta.get(cond["key"].split(".")[-1]) != cond["match"]["value"]:
                return False
    return True


class FakeQdrant(q._QdrantRESTClient):
    """In-memory points; ``scrolls`` records every scroll body."""

    def __init__(self):
        super().__init__(q._QdrantConfig(url=URL))
        self.points: dict[str, dict] = {}
        self.scrolls: list[dict] = []

    def request(self, method, path, *, body=None, query=None):
        body = body or {}
        if method == "GET":
            return {}
        if method == "DELETE":
            self.points.clear()
        elif path.endswith("/points"):  # upsert
            for p in body["points"]:
                self.points[p["id"]] = p
        elif path.endswith("/points/delete"):
            if "points" in body:
                for pid in body["points"]:
                    self.points.pop(pid, None)
            else:
                for pid in [i for i, p in self.points.items() if _matches(p, body["filter"])]:
                    del self.points[pid]
        elif path.endswith("/points/scroll"):
            self.scrolls.append(body)
            hits = [p for _, p in sorted(self.points.items()) if _matches(p, body.get("filter"))]
            start = int(body.get("offset") or 0)
            end = start + body["limit"]
            page = [
                {"id": p["id"], **({"payload": p["payload"]} if body["with_payload"] else {})}
                for p in hits[start:end]
            ]
            nxt = end if end < len(hits) else None
            return {"result": {"points": page, "next_page_offset": nxt}}
        return {}


@pytest.fixture
def col(monkeypatch):
    monkeypatch.setenv(TTL, CACHE_ON)  # the cache is opt-in; these tests opt in
    client = FakeQdrant()
    collection = q.QdrantCollection(
        backend=SimpleNamespace(_closed=False),
        client=client,
        config=client._config,
        palace=None,
        collection_name="closets",
        remote_collection=REMOTE,
    )
    lc.invalidate(URL, REMOTE)
    return collection


def _put(col, doc_id, text, meta=None):
    col._client.upsert_points(
        REMOTE,
        [
            {
                "id": q._point_id(doc_id),
                "vector": [0.0],
                "payload": {
                    q._PAYLOAD_ID: doc_id,
                    q._PAYLOAD_DOCUMENT: text,
                    q._PAYLOAD_METADATA: meta or {},
                },
            }
        ],
    )


def _corpus(col, n=80, seed=1):
    rnd = random.Random(seed)
    for i in range(n):
        text = " ".join(rnd.choices(WORDS, k=rnd.randint(0, 12)))
        _put(col, f"c{i}", text, {"wing": rnd.choice("ab")})


def _sig(result):
    return [(h.id, h.document, h.metadata, h.score) for h in result.hits]


def _cached_rows():
    return lc._cache_for(URL, REMOTE).rows


QUERIES = ["irion", "irion tesla laden", "schlaf energie ziele", "größe termin", "nope", "x"]


@pytest.mark.parametrize("where", [None, {"wing": "a"}])
@pytest.mark.parametrize("query", QUERIES)
def test_hits_are_bit_identical_to_the_scan(col, monkeypatch, query, where):
    _corpus(col)
    kw = {"query": query, "n_results": 16, "where": where}
    monkeypatch.delenv(TTL)  # default = cache off: original path, full scroll with payload
    want = col.lexical_search(**kw)
    assert not _cached_rows()
    monkeypatch.setenv(TTL, CACHE_ON)
    assert _sig(col.lexical_search(**kw)) == _sig(want)
    assert _sig(col.lexical_search(**kw)) == _sig(want)  # second call served from the cache


def test_second_search_fetches_no_payload(col):
    _corpus(col)
    col.lexical_search(query="irion tesla")
    col._client.scrolls.clear()
    col.lexical_search(query="irion tesla")
    assert [s["with_payload"] for s in col._client.scrolls] == [False]


def test_writes_of_other_processes_show_up_for_new_and_deleted_points(col):
    _corpus(col)
    col.lexical_search(query="irion")
    pid = q._point_id("fremd")
    payload = {q._PAYLOAD_ID: "fremd", q._PAYLOAD_DOCUMENT: "irion irion", q._PAYLOAD_METADATA: {}}
    col._client.points[pid] = {"id": pid, "payload": payload}  # behind the cache's back
    assert "fremd" in [h.id for h in col.lexical_search(query="irion").hits]
    del col._client.points[pid]
    assert "fremd" not in [h.id for h in col.lexical_search(query="irion").hits]


def test_own_upsert_delete_and_delete_collection_invalidate(col):
    _put(col, "a", "irion")
    assert [h.document for h in col.lexical_search(query="irion").hits] == ["irion"]
    _put(col, "a", "tesla")
    assert col.lexical_search(query="irion").hits == []
    assert [h.document for h in col.lexical_search(query="tesla").hits] == ["tesla"]
    col.delete(ids=["a"])
    assert col.lexical_search(query="tesla").hits == []
    _put(col, "b", "tesla")
    col.lexical_search(query="tesla")
    assert _cached_rows()
    col._client.delete_collection(REMOTE)
    assert not _cached_rows()


def test_delete_by_filter_clears_the_cache(col):
    _corpus(col, n=10)
    col.lexical_search(query="irion")
    assert _cached_rows()
    col.delete(where={"wing": "a"})
    assert not _cached_rows()


def test_rewrite_by_another_process_is_seen_after_the_ttl(col):
    _put(col, "a", "irion")
    col.lexical_search(query="irion")
    col._client.points[q._point_id("a")]["payload"][q._PAYLOAD_DOCUMENT] = "irion tesla"
    # Membership is live, the row text is cached: stale, but only until the epoch expires.
    assert [h.document for h in col.lexical_search(query="irion").hits] == ["irion"]
    lc._cache_for(URL, REMOTE).epoch_start -= lc.ttl_seconds() + 1
    assert [h.document for h in col.lexical_search(query="irion").hits] == ["irion tesla"]


def test_a_write_during_a_load_is_not_cached(col):
    _put(col, "a", "irion")

    def load_then_write(missing):
        fresh = {
            pid: lc.Row({"id": "a", "document": "irion", "metadata": {}}, ["irion"])
            for pid in missing
        }
        _put(col, "a", "tesla")  # a write lands between the read and the store
        return fresh

    lc.rows_for(URL, REMOTE, [q._point_id("a")], load_then_write)
    assert not _cached_rows()


def test_cache_is_off_by_default(col, monkeypatch):
    _corpus(col, n=10)
    monkeypatch.delenv(TTL)
    assert lc.ttl_seconds() == 0.0
    col._client.scrolls.clear()
    assert col.lexical_search(query="irion").hits
    assert not _cached_rows()
    assert [s["with_payload"] for s in col._client.scrolls] == [True]  # the original scan


@pytest.mark.parametrize("value", ["0", "0.0", "-5", "", "soon", "nan"])
def test_ttl_that_is_not_positive_keeps_the_cache_off(col, monkeypatch, value):
    _corpus(col, n=10)
    monkeypatch.setenv(TTL, value)
    assert lc.ttl_seconds() == 0.0
    col.lexical_search(query="irion")
    assert not _cached_rows()


def test_a_positive_ttl_turns_the_cache_on(col, monkeypatch):
    _corpus(col, n=10)
    monkeypatch.setenv(TTL, "1.5")
    assert lc.ttl_seconds() == 1.5
    col.lexical_search(query="irion")
    assert _cached_rows()


def test_cap_answers_without_storing(col, monkeypatch):
    _corpus(col, n=20)
    monkeypatch.setattr(lc, "MAX_ROWS", 5)
    assert col.lexical_search(query="irion tesla").hits
    assert not _cached_rows()
