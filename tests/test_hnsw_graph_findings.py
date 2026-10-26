"""hnsw_graph_findings: structural check of a persisted HNSW graph from its files alone.

The failure it guards against: an element on level >= 1 lists a neighbour that only exists on
level 0. A level-1 search reaches a node without a link list and chromadb dies with an access
violation, while ``repair-status`` counts and file sizes all look healthy.
"""

import sqlite3
import struct
from unittest.mock import patch

import pytest

from mempalace import repair
from mempalace.backends.chroma import hnsw_graph_findings

DIM, MAX_M, MAX_M0 = 2, 2, 4
OFF_DATA = 4 + MAX_M0 * 4
LABEL_OFF = OFF_DATA + DIM * 4
PER = LABEL_OFF + 8
REC = MAX_M * 4 + 4  # size_links_per_element


def write_segment(seg, levels, upper, deleted=(), version=1, max_elements=16):
    """Write a chroma-hnswlib segment byte by byte.

    ``levels[i]`` is element i's level; ``upper[(i, level)]`` the neighbour ids of its list.
    """
    seg.mkdir(parents=True, exist_ok=True)
    cur = len(levels)
    header = struct.pack(
        "<i6QiI3QdQ",
        version,
        0,
        max_elements,
        cur,
        PER,
        LABEL_OFF,
        OFF_DATA,
        max(levels),
        0,
        MAX_M,
        MAX_M0,
        MAX_M,
        1.0,
        10,
    )
    assert len(header) == 100
    (seg / "header.bin").write_bytes(header)
    data = bytearray()
    for i in range(cur):
        data += struct.pack("<HBB", 0, 1 if i in deleted else 0, 0) + b"\0" * (MAX_M0 * 4)
        data += struct.pack("<2f", 0.1 * i, 1.0) + struct.pack("<Q", i + 1)
    (seg / "data_level0.bin").write_bytes(bytes(data))
    (seg / "length.bin").write_bytes(b"\0" * 4 * cur)
    links = bytearray()
    for i in range(cur):
        links += struct.pack("<I", levels[i] * REC)
        for lvl in range(1, levels[i] + 1):
            ids = upper.get((i, lvl), [])
            links += struct.pack("<HH", len(ids), 0)
            links += b"".join(struct.pack("<I", x) for x in ids + [0] * (MAX_M - len(ids)))
    (seg / "link_lists.bin").write_bytes(bytes(links))
    return seg


def test_consistent_graph_has_no_dangling_links(tmp_path):
    seg = write_segment(tmp_path / "s", [1, 1, 0, 0], {(0, 1): [1], (1, 1): [0]}, deleted={3})
    assert hnsw_graph_findings(str(seg)) == {
        "elements": 4,
        "upper_links": 2,
        "dangling_links": 0,
        "dangling_nodes": 0,
    }


def test_link_to_a_node_that_only_exists_on_level_0_is_dangling(tmp_path):
    seg = write_segment(tmp_path / "s", [1, 1, 0, 0], {(0, 1): [1, 2], (1, 1): [0]})
    found = hnsw_graph_findings(str(seg))
    assert (found["dangling_links"], found["dangling_nodes"], found["upper_links"]) == (1, 1, 3)


def test_link_to_an_id_beyond_the_element_count_is_dangling(tmp_path):
    seg = write_segment(tmp_path / "s", [1, 1, 0, 0], {(0, 1): [1], (1, 1): [99]})
    assert hnsw_graph_findings(str(seg))["dangling_links"] == 1


def test_a_level_2_list_needs_level_2_neighbours(tmp_path):
    seg = write_segment(tmp_path / "s", [2, 1, 0, 0], {(0, 1): [1], (0, 2): [1], (1, 1): [0]})
    assert hnsw_graph_findings(str(seg))["dangling_links"] == 1


@pytest.mark.parametrize(
    "damage", ["missing_links", "empty_links", "truncated", "trailing", "version", "short_header"]
)
def test_a_segment_that_cannot_be_judged_returns_none_and_never_raises(tmp_path, damage):
    seg = write_segment(tmp_path / "s", [1, 1, 0, 0], {(0, 1): [1], (1, 1): [0]})
    links = seg / "link_lists.bin"
    if damage == "missing_links":
        links.unlink()
    elif damage == "empty_links":
        links.write_bytes(b"")
    elif damage == "truncated":
        links.write_bytes(links.read_bytes()[:-6])
    elif damage == "trailing":
        links.write_bytes(links.read_bytes() + b"\0" * 8)
    elif damage == "version":
        write_segment(tmp_path / "s", [1, 1, 0, 0], {(0, 1): [1], (1, 1): [0]}, version=2)
    else:
        (seg / "header.bin").write_bytes(b"\0" * 10)
    assert hnsw_graph_findings(str(seg)) is None


def test_missing_directory_returns_none(tmp_path):
    assert hnsw_graph_findings(str(tmp_path / "nope")) is None


def test_matches_what_chromadb_actually_persists(tmp_path):
    """Pins the on-disk format to the real writer so a chromadb upgrade that changes it fails here
    instead of silently turning the check into a permanent 'ok' (or a permanent None)."""
    import chromadb

    from mempalace.backends.chroma import _vector_segment_id

    client = chromadb.PersistentClient(path=str(tmp_path))
    col = client.get_or_create_collection(
        "mempalace_drawers",
        metadata={"hnsw:space": "cosine", "hnsw:sync_threshold": 10, "hnsw:batch_size": 10},
    )
    n = 200
    col.upsert(
        ids=[f"d{i}" for i in range(n)],
        embeddings=[
            [((i * 7 + j * 13) % 17) / 17.0 + 0.01 * j for j in range(8)] for i in range(n)
        ],
        documents=["x"] * n,
    )
    del col, client
    seg_id = _vector_segment_id(str(tmp_path), "mempalace_drawers")
    found = hnsw_graph_findings(str(tmp_path / seg_id))
    assert found is not None, (
        "chromadb's persisted format is no longer what hnsw_graph_findings reads"
    )
    assert found["elements"] >= 190  # persisted at the sync threshold
    assert found["dangling_links"] == 0


def _status_with_segment(tmp_path, levels, upper, capsys):
    palace = tmp_path / "palace"
    palace.mkdir()
    sqlite3.connect(palace / "chroma.sqlite3").close()
    write_segment(palace / "seg-1", levels, upper)
    info = {
        "segment_id": "seg-1",
        "sqlite_count": 4,
        "hnsw_count": 4,
        "divergence": 0,
        "diverged": False,
        "status": "ok",
        "message": "",
    }
    with (
        patch("mempalace.repair.sqlite_drawer_count", return_value=4),
        patch(
            "mempalace.repair.hnsw_capacity_status",
            side_effect=[dict(info), dict(info, segment_id=None)],
        ),
    ):
        result = repair.status(palace_path=str(palace))
    return result, capsys.readouterr().out


def test_status_reports_a_dangling_graph_and_recommends_a_rebuild(tmp_path, capsys):
    result, out = _status_with_segment(
        tmp_path, [1, 1, 0, 0], {(0, 1): [1, 2], (1, 1): [0]}, capsys
    )
    assert "graph:          DANGLING (1 upper-level links from 1 nodes" in out
    assert "mempalace repair --mode from-sqlite --archive-existing" in out
    assert result["drawers"]["graph"]["dangling_links"] == 1
    assert result["drawers"]["status"] == "ok"  # the capacity verdict is untouched
    assert result["closets"]["graph"] is None  # no segment id: skipped, not an error


def test_status_reports_a_healthy_graph_without_a_recommendation(tmp_path, capsys):
    _result, out = _status_with_segment(tmp_path, [1, 1, 0, 0], {(0, 1): [1], (1, 1): [0]}, capsys)
    assert "graph:          ok (2 upper-level links)" in out
    assert "DANGLING" not in out
    assert "internally inconsistent" not in out
