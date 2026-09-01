"""Tests for mempalace.dedup — near-duplicate drawer detection and removal."""

from unittest.mock import MagicMock, patch


from mempalace import dedup


# ── get_source_groups ─────────────────────────────────────────────────


def test_get_source_groups_aborts_on_hnsw_divergence():
    """count() on a diverged HNSW segment can hard-crash the process
    (#1222); get_source_groups must not call it when hnsw_capacity_status
    reports divergence for the caller-supplied palace_path (#92). Omitting
    palace_path (as every existing test above does) skips the check
    entirely -- this only fires when a real caller passes one."""
    col = MagicMock()
    with patch(
        "mempalace.backends.chroma.hnsw_capacity_status",
        return_value={"diverged": True, "message": "test divergence"},
    ):
        groups = dedup.get_source_groups(col, palace_path="/fake/palace")
    assert groups == {}
    col.count.assert_not_called()


def test_get_source_groups_basic():
    col = MagicMock()
    col.count.return_value = 5
    col.get.side_effect = [
        {
            "ids": ["d1", "d2", "d3", "d4", "d5"],
            "metadatas": [
                {"source_file": "a.txt"},
                {"source_file": "a.txt"},
                {"source_file": "a.txt"},
                {"source_file": "a.txt"},
                {"source_file": "a.txt"},
            ],
        },
        {"ids": []},
    ]
    groups = dedup.get_source_groups(col, min_count=5)
    assert "a.txt" in groups
    assert len(groups["a.txt"]) == 5


def test_get_source_groups_below_min():
    col = MagicMock()
    col.count.return_value = 2
    col.get.side_effect = [
        {
            "ids": ["d1", "d2"],
            "metadatas": [
                {"source_file": "a.txt"},
                {"source_file": "a.txt"},
            ],
        },
        {"ids": []},
    ]
    groups = dedup.get_source_groups(col, min_count=5)
    assert len(groups) == 0


def test_get_source_groups_source_filter():
    col = MagicMock()
    col.count.return_value = 6
    col.get.side_effect = [
        {
            "ids": ["d1", "d2", "d3", "d4", "d5", "d6"],
            "metadatas": [
                {"source_file": "project_a.txt"},
                {"source_file": "project_a.txt"},
                {"source_file": "project_a.txt"},
                {"source_file": "project_a.txt"},
                {"source_file": "project_a.txt"},
                {"source_file": "other.txt"},
            ],
        },
        {"ids": []},
    ]
    groups = dedup.get_source_groups(col, min_count=5, source_pattern="project_a")
    assert "project_a.txt" in groups
    assert "other.txt" not in groups


def test_get_source_groups_wing_filter():
    col = MagicMock()
    col.count.return_value = 5
    col.get.side_effect = [
        {
            "ids": ["d1", "d2", "d3", "d4", "d5"],
            "metadatas": [
                {"source_file": "a.txt"},
                {"source_file": "a.txt"},
                {"source_file": "a.txt"},
                {"source_file": "a.txt"},
                {"source_file": "a.txt"},
            ],
        },
        {"ids": []},
    ]
    dedup.get_source_groups(col, min_count=5, wing="my_wing")
    # Verify where filter was passed
    first_call = col.get.call_args_list[0]
    assert first_call.kwargs.get("where") == {"wing": "my_wing"}


def test_get_source_groups_missing_source_file():
    col = MagicMock()
    col.count.return_value = 5
    col.get.side_effect = [
        {
            "ids": ["d1", "d2", "d3", "d4", "d5"],
            "metadatas": [{}, {}, {}, {}, {}],
        },
        {"ids": []},
    ]
    groups = dedup.get_source_groups(col, min_count=5)
    assert "unknown" in groups


# ── dedup_source_group ────────────────────────────────────────────────


def test_dedup_source_group_all_unique():
    col = MagicMock()
    col.get.return_value = {
        "ids": ["d1", "d2"],
        "documents": ["long document one content here", "different document two here"],
        "metadatas": [{"wing": "a"}, {"wing": "a"}],
    }
    col.query.return_value = {
        "ids": [["d1"]],
        "distances": [[0.8]],  # far apart = unique
    }
    kept, deleted = dedup.dedup_source_group(col, ["d1", "d2"], threshold=0.15, dry_run=True)
    assert len(kept) == 2
    assert len(deleted) == 0


def test_dedup_source_group_with_duplicate():
    col = MagicMock()
    col.get.return_value = {
        "ids": ["d1", "d2"],
        "documents": [
            "long document content that is fairly long",
            "long document content that is fairly long",
        ],
        "metadatas": [{"wing": "a"}, {"wing": "a"}],
    }
    col.query.return_value = {
        "ids": [["d1"]],
        "distances": [[0.05]],  # very close = duplicate
    }
    kept, deleted = dedup.dedup_source_group(col, ["d1", "d2"], threshold=0.15, dry_run=True)
    assert len(kept) == 1
    assert len(deleted) == 1


def test_dedup_source_group_short_docs_deleted():
    col = MagicMock()
    col.get.return_value = {
        "ids": ["d1", "d2"],
        "documents": ["long enough document to keep in the palace", "tiny"],
        "metadatas": [{"wing": "a"}, {"wing": "a"}],
    }
    kept, deleted = dedup.dedup_source_group(col, ["d1", "d2"], threshold=0.15, dry_run=True)
    assert "d2" in deleted  # too short


def test_dedup_source_group_empty_doc_deleted():
    col = MagicMock()
    col.get.return_value = {
        "ids": ["d1", "d2"],
        "documents": ["real document content here that is long enough", None],
        "metadatas": [{"wing": "a"}, {"wing": "a"}],
    }
    kept, deleted = dedup.dedup_source_group(col, ["d1", "d2"], threshold=0.15, dry_run=True)
    assert "d2" in deleted


def test_dedup_source_group_live_deletes():
    col = MagicMock()
    col.get.return_value = {
        "ids": ["d1", "d2"],
        "documents": ["long document content here enough", "long document content here enough"],
        "metadatas": [{"wing": "a"}, {"wing": "a"}],
    }
    col.query.return_value = {
        "ids": [["d1"]],
        "distances": [[0.05]],
    }
    kept, deleted = dedup.dedup_source_group(col, ["d1", "d2"], threshold=0.15, dry_run=False)
    col.delete.assert_called_once()


def test_dedup_source_group_query_failure_keeps():
    col = MagicMock()
    col.get.return_value = {
        "ids": ["d1", "d2"],
        "documents": [
            "long document one content here enough",
            "long document two content here enough",
        ],
        "metadatas": [{"wing": "a"}, {"wing": "a"}],
    }
    col.query.side_effect = Exception("query failed")
    kept, deleted = dedup.dedup_source_group(col, ["d1", "d2"], threshold=0.15, dry_run=True)
    assert len(kept) == 2  # both kept on error


# ── show_stats ────────────────────────────────────────────────────────


def _install_mock_collection(mock_get_collection, collection):
    mock_get_collection.return_value = collection
    return collection


@patch("mempalace.dedup.get_collection")
def test_show_stats(mock_get_collection, tmp_path):
    mock_col = MagicMock()
    mock_col.count.return_value = 5
    mock_col.get.side_effect = [
        {
            "ids": ["d1", "d2", "d3", "d4", "d5"],
            "metadatas": [
                {"source_file": "a.txt"},
                {"source_file": "a.txt"},
                {"source_file": "a.txt"},
                {"source_file": "a.txt"},
                {"source_file": "a.txt"},
            ],
        },
        {"ids": []},
    ]
    _install_mock_collection(mock_get_collection, mock_col)

    dedup.show_stats(palace_path=str(tmp_path))  # should not raise


# ── dedup_palace ──────────────────────────────────────────────────────


@patch("mempalace.dedup.dedup_source_group")
@patch("mempalace.dedup.get_source_groups")
@patch("mempalace.dedup.get_collection")
def test_dedup_palace_dry_run(mock_get_collection, mock_groups, mock_dedup_group, tmp_path):
    mock_col = MagicMock()
    mock_col.count.return_value = 10
    _install_mock_collection(mock_get_collection, mock_col)

    mock_groups.return_value = {"a.txt": ["d1", "d2", "d3", "d4", "d5"]}
    mock_dedup_group.return_value = (["d1", "d2", "d3"], ["d4", "d5"])

    dedup.dedup_palace(palace_path=str(tmp_path), dry_run=True)
    mock_dedup_group.assert_called_once()


@patch("mempalace.dedup.get_source_groups")
@patch("mempalace.dedup.get_collection")
def test_dedup_palace_aborts_on_hnsw_divergence(mock_get_collection, mock_groups, tmp_path):
    """dedup_palace's OWN count() print (a few lines before it calls
    get_source_groups) is a separate call site from #92 -- count() on a
    diverged segment can hard-crash the process, so this print must never
    be reached either when hnsw_capacity_status reports divergence."""
    mock_col = MagicMock()
    mock_col.count.side_effect = AssertionError("count() must not be called when diverged")
    _install_mock_collection(mock_get_collection, mock_col)

    with patch(
        "mempalace.backends.chroma.hnsw_capacity_status",
        return_value={"diverged": True, "message": "test divergence"},
    ):
        dedup.dedup_palace(palace_path=str(tmp_path), dry_run=True)

    mock_groups.assert_not_called()


@patch("mempalace.dedup.dedup_source_group")
@patch("mempalace.dedup.get_source_groups")
@patch("mempalace.dedup.get_collection")
def test_dedup_palace_with_wing(mock_get_collection, mock_groups, mock_dedup_group, tmp_path):
    mock_col = MagicMock()
    mock_col.count.return_value = 10
    _install_mock_collection(mock_get_collection, mock_col)

    mock_groups.return_value = {}
    dedup.dedup_palace(palace_path=str(tmp_path), wing="test_wing", dry_run=True)
    mock_groups.assert_called_once_with(
        mock_col, 5, None, wing="test_wing", palace_path=str(tmp_path)
    )


@patch("mempalace.dedup.dedup_source_group")
@patch("mempalace.dedup.get_source_groups")
@patch("mempalace.dedup.get_collection")
def test_dedup_palace_no_groups(mock_get_collection, mock_groups, mock_dedup_group, tmp_path):
    mock_col = MagicMock()
    mock_col.count.return_value = 3
    _install_mock_collection(mock_get_collection, mock_col)

    mock_groups.return_value = {}
    dedup.dedup_palace(palace_path=str(tmp_path), dry_run=True)
    mock_dedup_group.assert_not_called()


# ── dedup_source_group — group scoping (the under-deletion bug) ────────


class _ScopedFakeCol:
    """Fake collection with a global corpus: query() honors the `where`
    source_file filter the way ChromaDB does. Models the failure mode on a
    large palace: the globally nearest neighbors of a doc are near-dups from
    OTHER sources, so an unscoped top-5 never surfaces the in-group twin.
    """

    def __init__(self):
        # d1/d2 are twins in a.txt; x1..x5 are foreign near-dups in other files.
        self.docs = {
            "d1": ("aaa bbb ccc ddd eee fff ggg hhh", "a.txt"),
            "d2": ("aaa bbb ccc ddd eee fff ggg hhh", "a.txt"),
            **{f"x{i}": ("aaa bbb ccc ddd eee fff ggg hhh", f"other{i}.txt") for i in range(1, 6)},
        }
        self.query_calls = []

    def get(self, ids=None, include=None, **kw):
        return {
            "ids": list(ids),
            "documents": [self.docs[i][0] for i in ids],
            "metadatas": [{"source_file": self.docs[i][1]} for i in ids],
        }

    def query(
        self, query_texts=None, query_embeddings=None, n_results=5, where=None, include=None, **kw
    ):
        self.query_calls.append({"where": where, "n_results": n_results})
        pool = [i for i, (_, src) in self.docs.items()]
        if where and "source_file" in where:
            pool = [i for i in pool if self.docs[i][1] == where["source_file"]]
        # Everything is a near-identical twin: distance 0.01 for all.
        hits = pool[:n_results]
        return {"ids": [hits], "distances": [[0.01] * len(hits)]}

    def delete(self, ids=None, **kw):
        pass


def test_dedup_source_group_scopes_query_to_source_file():
    """The in-group twin must be found even when 5+ foreign near-dups are
    globally closer — the query must carry where={'source_file': ...}."""
    col = _ScopedFakeCol()
    kept, deleted = dedup.dedup_source_group(col, ["d1", "d2"], threshold=0.15, dry_run=True)
    assert deleted == ["d2"]
    assert len(kept) == 1
    assert all(c["where"] == {"source_file": "a.txt"} for c in col.query_calls)


def test_dedup_source_group_unscoped_without_source_metadata():
    """Legacy drawers without source_file metadata keep the unscoped query."""
    col = MagicMock()
    col.get.return_value = {
        "ids": ["d1", "d2"],
        "documents": ["long document one content here", "different document two here"],
        "metadatas": [{}, {}],
    }
    col.query.return_value = {"ids": [["d1"]], "distances": [[0.8]]}
    kept, deleted = dedup.dedup_source_group(col, ["d1", "d2"], threshold=0.15, dry_run=True)
    assert len(kept) == 2
    _, kwargs = col.query.call_args
    assert kwargs.get("where") is None


def test_dedup_source_group_self_match_does_not_consume_slots():
    """The candidate itself is in the collection and comes back at distance
    ~0. It must be skipped (not treated as a kept-twin) and must not consume
    one of the neighbor slots that could have held the real kept twin."""
    col = MagicMock()
    col.get.return_value = {
        "ids": ["d1", "d2"],
        "documents": [
            "long document content that is fairly long",
            "long document content that is fairly lonG",
        ],
        "metadatas": [{"source_file": "a.txt"}, {"source_file": "a.txt"}],
    }
    # Query for d2 returns d2 itself first, then the kept twin d1.
    col.query.return_value = {"ids": [["d2", "d1"]], "distances": [[0.0, 0.05]]}
    kept, deleted = dedup.dedup_source_group(col, ["d1", "d2"], threshold=0.15, dry_run=True)
    assert deleted == ["d2"]
    _, kwargs = col.query.call_args
    # One extra slot requested to absorb the self-match.
    assert kwargs.get("n_results") == 2


def test_dedup_source_group_reuses_stored_embeddings():
    """When the initial get() returns embeddings, query with query_embeddings
    (no re-embedding of every candidate doc)."""
    col = MagicMock()
    col.get.return_value = {
        "ids": ["d1", "d2"],
        "documents": ["long document one content here", "different document two here"],
        "metadatas": [{"source_file": "a.txt"}, {"source_file": "a.txt"}],
        "embeddings": [[0.1, 0.2], [0.3, 0.4]],
    }
    col.query.return_value = {"ids": [["d1"]], "distances": [[0.8]]}
    kept, deleted = dedup.dedup_source_group(col, ["d1", "d2"], threshold=0.15, dry_run=True)
    assert len(kept) == 2
    _, kwargs = col.query.call_args
    assert kwargs.get("query_embeddings") is not None
    assert kwargs.get("query_texts") is None
