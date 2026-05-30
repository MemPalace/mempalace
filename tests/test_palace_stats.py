"""Read-path aggregation seam tests (#1657).

The correctness oracle the issue mandates: the SQL ``GROUP BY`` fast paths on
``ChromaCollection`` must produce *identical* counts to the inherited Python
full-scan default on the same palace. We assert parity directly by comparing
``ChromaCollection.count_by`` (SQL) against ``BaseCollection.count_by`` (scan)
on the same adapter, plus the labeled shapes ``palace_stats`` derives for the
CLI and MCP read tools.
"""

from mempalace import palace_stats, source_file_access
from mempalace.backends.base import BaseCollection
from mempalace.backends.chroma import ChromaCollection


def _adapter(collection, palace_path):
    return ChromaCollection(collection, palace_path=palace_path)


# --------------------------------------------------------------------------
# SQL fast path == Python full-scan default (parity oracle)
# --------------------------------------------------------------------------


def test_count_by_sql_matches_scan(seeded_collection, collection, palace_path):
    col = _adapter(collection, palace_path)
    sql = col.count_by("wing")  # ChromaCollection SQL GROUP BY
    scan = BaseCollection.count_by(col, "wing")  # inherited full-scan
    assert sql == scan == {"project": 3, "notes": 1}


def test_crosstab_sql_matches_scan(seeded_collection, collection, palace_path):
    col = _adapter(collection, palace_path)
    sql = col.crosstab("wing", "room")
    scan = BaseCollection.crosstab(col, "wing", "room")
    assert sql == scan
    assert sql == {"project": {"backend": 2, "frontend": 1}, "notes": {"planning": 1}}


def test_count_matching_sql_matches_scan(seeded_collection, collection, palace_path):
    col = _adapter(collection, palace_path)
    assert col.count_matching({"source_file": "auth.py"}) == 1
    assert BaseCollection.count_matching(col, {"source_file": "auth.py"}) == 1
    assert col.count_matching({"source_file": "does-not-exist.py"}) == 0


def test_grouped_counts_sum_to_collection_count(seeded_collection, collection, palace_path):
    col = _adapter(collection, palace_path)
    total = col.count()
    assert sum(col.count_by("wing").values()) == total
    assert (
        sum(n for rooms in col.crosstab("wing", "room").values() for n in rooms.values()) == total
    )


# --------------------------------------------------------------------------
# Missing-key parity: absent metadata groups under None on both paths
# --------------------------------------------------------------------------


def test_missing_key_parity_and_relabeling(collection, palace_path):
    # One drawer with wing/room, one with neither.
    collection.add(
        ids=["with_meta", "no_meta"],
        documents=["alpha", "beta"],
        metadatas=[{"wing": "w1", "room": "r1"}, {"source_file": "z.py"}],
    )
    col = _adapter(collection, palace_path)

    sql = col.count_by("wing")
    scan = BaseCollection.count_by(col, "wing")
    assert sql == scan
    assert sql == {"w1": 1, None: 1}

    # palace_stats relabels the None bucket for the caller (MCP: "unknown").
    assert palace_stats.wing_counts(col) == {"w1": 1, "unknown": 1}
    # CLI uses "?" as the missing label.
    assert palace_stats.taxonomy(col, missing="?") == {"w1": {"r1": 1}, "?": {"?": 1}}


# --------------------------------------------------------------------------
# Derived shapes used by the read tools
# --------------------------------------------------------------------------


def test_palace_stats_shapes(seeded_collection, collection, palace_path):
    col = _adapter(collection, palace_path)
    assert palace_stats.taxonomy(col) == {
        "project": {"backend": 2, "frontend": 1},
        "notes": {"planning": 1},
    }
    assert palace_stats.wing_counts(col) == {"project": 3, "notes": 1}
    assert palace_stats.room_counts(col) == {"backend": 2, "frontend": 1, "planning": 1}
    assert palace_stats.room_counts(col, wing="project") == {"backend": 2, "frontend": 1}
    assert palace_stats.room_counts(col, wing="absent") == {}

    wings, rooms = palace_stats.wing_room_summary(col)
    assert wings == {"project": 3, "notes": 1}
    assert rooms == {"backend": 2, "frontend": 1, "planning": 1}


# --------------------------------------------------------------------------
# Bounded source-file access (#1657 candidate 3)
# --------------------------------------------------------------------------


def test_source_file_access_counts_and_bounds(collection, palace_path):
    collection.add(
        ids=["big_0", "big_1", "big_2", "other"],
        documents=["one", "two", "three", "elsewhere"],
        metadatas=[
            {"wing": "w", "room": "r", "source_file": "big.md", "chunk_index": 0},
            {"wing": "w", "room": "r", "source_file": "big.md", "chunk_index": 1},
            {"wing": "w", "room": "r", "source_file": "big.md", "chunk_index": 2},
            {"wing": "w", "room": "r", "source_file": "small.md", "chunk_index": 0},
        ],
    )
    col = _adapter(collection, palace_path)

    # COUNT(*) without materializing documents.
    assert source_file_access.count_drawers(col, "big.md") == 3
    assert source_file_access.count_drawers(col, "small.md") == 1

    # Unbounded fetch returns all; explicit limit bounds the materialization.
    assert len(source_file_access.fetch_drawers(col, "big.md").ids) == 3
    assert len(source_file_access.fetch_drawers(col, "big.md", limit=2).ids) == 2


# --------------------------------------------------------------------------
# Full-scan default works for a backend without the SQL fast path
# --------------------------------------------------------------------------


def test_fallback_default_when_no_palace_path(seeded_collection, collection):
    # palace_path=None disables the SQL fast path -> base scan must still work.
    col = ChromaCollection(collection, palace_path=None)
    assert col.count_by("wing") == {"project": 3, "notes": 1}
    assert col.count_matching({"source_file": "auth.py"}) == 1


# --------------------------------------------------------------------------
# Typed-metadata parity (#1657 review finding): the SQL fast path must group
# on the same typed value the Python scan sees, not string_value alone — else
# int/float/bool-valued keys collapse into the missing-key (None) bucket.
# --------------------------------------------------------------------------


def test_count_by_int_metadata_matches_scan(collection, palace_path):
    collection.add(
        ids=["c0", "c1", "c2"],
        documents=["a", "b", "c"],
        # chunk_index is an int -> stored in int_value, string_value is NULL.
        metadatas=[
            {"wing": "w", "chunk_index": 0},
            {"wing": "w", "chunk_index": 1},
            {"wing": "w", "chunk_index": 1},
        ],
    )
    col = _adapter(collection, palace_path)
    sql = col.count_by("chunk_index")
    scan = BaseCollection.count_by(col, "chunk_index")
    assert sql == scan == {0: 1, 1: 2}  # NOT {None: 3}


def test_crosstab_typed_dimension_matches_scan(collection, palace_path):
    collection.add(
        ids=["c0", "c1", "c2"],
        documents=["a", "b", "c"],
        metadatas=[
            {"wing": "w", "chunk_index": 0},
            {"wing": "w", "chunk_index": 1},
            {"wing": "x", "chunk_index": 0},
        ],
    )
    col = _adapter(collection, palace_path)
    sql = col.crosstab("wing", "chunk_index")
    scan = BaseCollection.crosstab(col, "wing", "chunk_index")
    assert sql == scan
    assert sql == {"w": {0: 1, 1: 1}, "x": {0: 1}}
