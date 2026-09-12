# tests/test_prefetch_content_hashes_fast_path.py
"""
Tests for ``prefetch_content_hashes()`` on the ``get_all_metadata()`` fast
path -- the same #1796 O(n^2) class of bug already fixed for the MCP server
(#1832) and the CLI status path (#2154), at the convo-miner's own call site.

``prefetch_content_hashes`` paged the collection with
``get(limit=1000, offset=N)``. On a backend whose ``get()`` materializes the
full result set and Python-slices it afterwards -- exactly what Qdrant's
``_rows() -> _scroll_all()`` does -- every page re-walked the whole
collection to discard all but its own slice.

Covers:
  1. The fast path is actually taken: one ``get_all_metadata()`` call, and
     no ``get()``/``count()`` paging at all.
  2. Filtering/aggregation semantics are byte-for-byte what the offset loop
     produced (extract_mode scope, normalize_version floor, comma-joined
     hashes, first-source-wins, wing-keyed).
  3. A backend blowing up still yields the documented partial result rather
     than propagating.
"""

from mempalace.palace import NORMALIZE_VERSION, prefetch_content_hashes


class FakeCollection:
    """Records which access pattern the caller used."""

    def __init__(self, metadatas, raises=False):
        self._metadatas = metadatas
        self._raises = raises
        self.get_all_metadata_calls = 0
        self.get_calls = 0
        self.count_calls = 0

    def get_all_metadata(self, where=None):
        self.get_all_metadata_calls += 1
        if self._raises:
            raise RuntimeError("backend down")
        return list(self._metadatas)

    # Present so a regression back to the offset loop is observable rather
    # than an AttributeError that could be mistaken for an unrelated break.
    def get(self, **kwargs):
        self.get_calls += 1
        return {"ids": [], "metadatas": []}

    def count(self):
        self.count_calls += 1
        return len(self._metadatas)


def _meta(**kw):
    base = {
        "content_hash": "h1",
        "source_file": "/tmp/a.jsonl",
        "wing": "sessions",
        "extract_mode": "convos",
        "normalize_version": NORMALIZE_VERSION,
    }
    base.update(kw)
    return base


def test_uses_get_all_metadata_and_never_pages():
    col = FakeCollection([_meta()])

    out = prefetch_content_hashes(col, extract_mode="convos")

    assert out == {("sessions", "h1"): "/tmp/a.jsonl"}
    assert col.get_all_metadata_calls == 1
    # The whole point of the fix: no offset loop, so no per-page get() and no
    # count() to size one.
    assert col.get_calls == 0
    assert col.count_calls == 0


def test_filtering_semantics_match_the_offset_loop():
    col = FakeCollection(
        [
            _meta(content_hash="keep", source_file="/first.jsonl"),
            # Same (wing, hash) from a later row: first source_file wins.
            _meta(content_hash="keep", source_file="/second.jsonl"),
            # Same hash, different wing: a deliberate re-file, kept separately.
            _meta(content_hash="keep", wing="other", source_file="/third.jsonl"),
            # Comma-joined bundle hash: every component is indexed.
            _meta(content_hash="b1,b2", source_file="/bundle.jsonl"),
            # Dropped: incomplete rows.
            _meta(content_hash=None),
            _meta(source_file=None),
            _meta(wing=None),
            # Dropped: pre-normalization row.
            _meta(content_hash="stale", normalize_version=NORMALIZE_VERSION - 1),
            # Dropped: a different extraction scope.
            _meta(content_hash="wrongmode", extract_mode="exchange"),
            # Dropped: not convo_miner's row at all (#104).
            _meta(content_hash="sweep", extract_mode=None, ingest_mode="sweep"),
            None,
        ]
    )

    out = prefetch_content_hashes(col, extract_mode="convos")

    assert out == {
        ("sessions", "keep"): "/first.jsonl",
        ("other", "keep"): "/third.jsonl",
        ("sessions", "b1"): "/bundle.jsonl",
        ("sessions", "b2"): "/bundle.jsonl",
    }


def test_extract_mode_none_matches_every_scope():
    col = FakeCollection(
        [
            _meta(content_hash="a", extract_mode="convos"),
            _meta(content_hash="b", extract_mode="exchange"),
        ]
    )

    out = prefetch_content_hashes(col, extract_mode=None)

    assert set(out) == {("sessions", "a"), ("sessions", "b")}


def test_backend_failure_returns_partial_instead_of_raising():
    col = FakeCollection([_meta()], raises=True)

    assert prefetch_content_hashes(col, extract_mode="convos") == {}
    assert col.get_all_metadata_calls == 1
