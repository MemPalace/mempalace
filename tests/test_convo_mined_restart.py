"""Mine restart decisions must not trust torn conversation completion markers."""

import pytest

from mempalace.palace import (
    CONVO_CHUNKER_VERSION,
    NORMALIZE_VERSION,
    file_already_mined,
    prefetch_mined_set,
)

_SOURCE = "/sessions/current.jsonl"


def _meta(index=0, total=3, *, filed="A", mode="exchange", source=_SOURCE):
    return {
        "source_file": source,
        "source_mtime": 123.0,
        "filed_at": filed,
        "ingest_mode": "convos",
        "extract_mode": mode,
        "normalize_version": NORMALIZE_VERSION,
        "convo_chunker_version": CONVO_CHUNKER_VERSION,
        "chunk_index": index,
        "chunk_total": total,
    }


class MetadataCollection:
    def __init__(self, rows):
        self.rows = rows

    def count(self):
        return len(self.rows)

    def get(self, where=None, limit=None, offset=0, include=None):
        rows = self.rows
        if where and "source_file" in where:
            source = where["source_file"]
            wanted = set(source["$in"]) if isinstance(source, dict) else {source}
            rows = [(row_id, meta) for row_id, meta in rows if meta["source_file"] in wanted]
        end = None if limit is None else offset + limit
        rows = rows[offset:end]
        return {
            "ids": [row_id for row_id, _ in rows],
            "metadatas": [meta for _, meta in rows],
        }


def _assert_restart_status(rows, expected, *, mode="exchange"):
    col = MetadataCollection(rows)
    assert file_already_mined(col, _SOURCE, extract_mode=mode) is expected
    for kwargs in ({}, {"source_files": [_SOURCE]}):
        known = prefetch_mined_set(col, extract_mode=mode, **kwargs)
        assert (_SOURCE in known) is expected
        if expected:
            assert known[_SOURCE] == 123.0


@pytest.mark.parametrize(
    ("indices", "totals", "complete"),
    [
        ((0, 1, 2), (3, 3, 3), True),
        ((0, 0, 2), (3, 3, 3), False),
        ((0, 1, 3), (3, 3, 3), False),
        ((0, 1, 2), (3, 3, 4), False),
    ],
)
def test_stamped_generation_requires_unique_contiguous_indices(indices, totals, complete):
    rows = [
        (f"chunk-{n}", _meta(index=index, total=total))
        for n, (index, total) in enumerate(zip(indices, totals))
    ]
    _assert_restart_status(rows, complete)


def test_complete_a_cannot_mask_torn_b_with_identical_source_mtime():
    rows = [(f"a-{i}", _meta(i, 2)) for i in range(2)]
    rows += [("b-0", _meta(0, 3, filed="B"))]
    _assert_restart_status(rows, False)


def test_other_source_and_mode_do_not_hide_complete_exchange():
    rows = [(f"a-{i}", _meta(i, 2)) for i in range(2)]
    rows += [("incomplete-general", _meta(0, 3, filed="B", mode="general"))]
    rows += [("different-source", _meta(0, 3, filed="B", source="/other.jsonl"))]
    _assert_restart_status(rows, True)


def test_legacy_unstamped_conversation_remains_skippable():
    meta = _meta()
    meta.pop("chunk_total")
    meta.pop("chunk_index")
    _assert_restart_status([("legacy", meta)], True)
