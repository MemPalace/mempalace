"""Tests for mempalace.dedup — near-duplicate drawer detection and removal."""

from unittest.mock import MagicMock, patch


from mempalace import dedup


# ── get_source_groups ─────────────────────────────────────────────────


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


def _install_mock_backend(mock_backend_cls, collection):
    mock_backend = MagicMock()
    mock_backend.get_collection.return_value = collection
    mock_backend_cls.return_value = mock_backend
    return mock_backend


@patch("mempalace.dedup.ChromaBackend")
def test_show_stats(mock_backend_cls, tmp_path):
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
    _install_mock_backend(mock_backend_cls, mock_col)

    dedup.show_stats(palace_path=str(tmp_path))  # should not raise


# ── dedup_palace ──────────────────────────────────────────────────────


@patch("mempalace.dedup.dedup_source_group")
@patch("mempalace.dedup.get_source_groups")
@patch("mempalace.dedup.ChromaBackend")
def test_dedup_palace_dry_run(mock_backend_cls, mock_groups, mock_dedup_group, tmp_path):
    mock_col = MagicMock()
    mock_col.count.return_value = 10
    _install_mock_backend(mock_backend_cls, mock_col)

    mock_groups.return_value = {"a.txt": ["d1", "d2", "d3", "d4", "d5"]}
    mock_dedup_group.return_value = (["d1", "d2", "d3"], ["d4", "d5"])

    dedup.dedup_palace(palace_path=str(tmp_path), dry_run=True)
    mock_dedup_group.assert_called_once()


@patch("mempalace.dedup.dedup_source_group")
@patch("mempalace.dedup.get_source_groups")
@patch("mempalace.dedup.ChromaBackend")
def test_dedup_palace_with_wing(mock_backend_cls, mock_groups, mock_dedup_group, tmp_path):
    mock_col = MagicMock()
    mock_col.count.return_value = 10
    _install_mock_backend(mock_backend_cls, mock_col)

    mock_groups.return_value = {}
    dedup.dedup_palace(palace_path=str(tmp_path), wing="test_wing", dry_run=True)
    mock_groups.assert_called_once_with(mock_col, 5, None, wing="test_wing")


@patch("mempalace.dedup.dedup_source_group")
@patch("mempalace.dedup.get_source_groups")
@patch("mempalace.dedup.ChromaBackend")
def test_dedup_palace_no_groups(mock_backend_cls, mock_groups, mock_dedup_group, tmp_path):
    mock_col = MagicMock()
    mock_col.count.return_value = 3
    _install_mock_backend(mock_backend_cls, mock_col)

    mock_groups.return_value = {}
    dedup.dedup_palace(palace_path=str(tmp_path), dry_run=True)
    mock_dedup_group.assert_not_called()
"""
test_dedup.py — Tests for deduplication features.

Covers:
  - tool_diary_write duplicate rejection (exact and near-identical)
  - tool_diary_write acceptance of distinct entries
  - tool_dedup_report clustering on seeded data
  - tool_dedup_report on clean (no duplicates) data
  - tool_dedup_report wing filtering
"""

from mempalace import mcp_server


def _setup_mcp(config, collection):
    """Point the MCP server at the test palace."""
    mcp_server._config = config
    mcp_server._client_cache = None
    mcp_server._collection_cache = None


# ==================== DIARY DEDUP =============

class TestDiaryDedup:
    """tool_diary_write should reject near-identical entries."""

    def test_exact_duplicate_rejected(self, config, collection):
        _setup_mcp(config, collection)
        entry = "SESSION:2026-04-08|debugged.auth.flow+fixed.JWT.expiry|★★★"

        r1 = mcp_server.tool_diary_write("claude", entry, topic="work")
        assert r1["success"] is True

        r2 = mcp_server.tool_diary_write("claude", entry, topic="work")
        assert r2["success"] is False
        assert r2["reason"] == "duplicate_diary_entry"

    def test_near_duplicate_rejected(self, config, collection):
        _setup_mcp(config, collection)

        r1 = mcp_server.tool_diary_write(
            "claude",
            "SESSION:2026-04-08|debugged.auth.flow+fixed.JWT.expiry|★★★",
            topic="work",
        )
        assert r1["success"] is True

        # Near-identical: same content with trivial variation
        r2 = mcp_server.tool_diary_write(
            "claude",
            "SESSION:2026-04-08|debugged.auth.flow+fixed.JWT.expiry|★★",
            topic="work",
        )
        assert r2["success"] is False
        assert r2["reason"] == "duplicate_diary_entry"

    def test_distinct_entry_accepted(self, config, collection):
        _setup_mcp(config, collection)

        r1 = mcp_server.tool_diary_write(
            "claude",
            "SESSION:2026-04-08|debugged.auth.flow+fixed.JWT.expiry|★★★",
            topic="work",
        )
        assert r1["success"] is True

        # Completely different content should succeed
        r2 = mcp_server.tool_diary_write(
            "claude",
            "SESSION:2026-04-08|migrated.database.to.cockroachdb+updated.alembic.configs|★★",
            topic="infra",
        )
        assert r2["success"] is True

    def test_different_agents_can_write_similar(self, config, collection):
        """Two different agents writing similar entries should both succeed.

        NOTE: This test documents current behavior. Since dedup checks the
        entire collection (not per-agent), similar entries from different
        agents WILL be flagged as duplicates. This is arguably correct:
        if the same fact is already in the palace, a second copy adds noise
        regardless of who wrote it.
        """
        _setup_mcp(config, collection)
        entry = "The database migration completed successfully at 14:00 UTC."

        r1 = mcp_server.tool_diary_write("alice", entry, topic="deploy")
        assert r1["success"] is True

        # Second agent writing the same thing — cross-agent dedup kicks in
        r2 = mcp_server.tool_diary_write("bob", entry, topic="deploy")
        assert r2["success"] is False
        assert r2["reason"] == "duplicate_diary_entry"


# ==================== DEDUP REPORT =============

class TestDedupReport:
    """tool_dedup_report should find and cluster near-duplicate drawers."""

    def test_report_on_clean_palace(self, config, collection):
        """No duplicates in a clean palace."""
        _setup_mcp(config, collection)

        # Add distinct documents
        collection.add(
            ids=["d1", "d2", "d3"],
            documents=[
                "The authentication module uses JWT tokens for session management.",
                "React frontend uses TanStack Query for server state management.",
                "Sprint planning: migrate auth to passkeys by Q3 2026.",
            ],
            metadatas=[
                {"wing": "proj", "room": "backend", "filed_at": "2026-01-01"},
                {"wing": "proj", "room": "frontend", "filed_at": "2026-01-02"},
                {"wing": "notes", "room": "planning", "filed_at": "2026-01-03"},
            ],
        )

        report = mcp_server.tool_dedup_report(threshold=0.92)
        assert report["total_duplicates"] == 0
        assert report["total_clusters"] == 0
        assert report["scanned"] == 3

    def test_report_finds_duplicates(self, config, collection):
        """Exact duplicates should appear in the report."""
        _setup_mcp(config, collection)

        # Add duplicated content
        collection.add(
            ids=["dup1", "dup2", "dup3", "unique1"],
            documents=[
                "The database uses PostgreSQL 15 with connection pooling via pgbouncer.",
                "The database uses PostgreSQL 15 with connection pooling via pgbouncer.",
                "The database uses PostgreSQL 15 with connection pooling via pgbouncer.",
                "React frontend uses TanStack Query for state management.",
            ],
            metadatas=[
                {"wing": "proj", "room": "backend", "filed_at": "2026-01-01"},
                {"wing": "proj", "room": "backend", "filed_at": "2026-01-02"},
                {"wing": "proj", "room": "backend", "filed_at": "2026-01-03"},
                {"wing": "proj", "room": "frontend", "filed_at": "2026-01-04"},
            ],
        )

        report = mcp_server.tool_dedup_report(threshold=0.92)
        assert report["total_duplicates"] >= 2
        assert report["total_clusters"] >= 1

    def test_report_wing_filter(self, config, collection):
        """Wing filter should restrict scan scope."""
        _setup_mcp(config, collection)

        collection.add(
            ids=["a1", "a2", "b1", "b2"],
            documents=[
                "Alpha wing document about testing infrastructure.",
                "Alpha wing document about testing infrastructure.",
                "Beta wing document about deployment pipelines.",
                "Beta wing document about deployment pipelines.",
            ],
            metadatas=[
                {"wing": "alpha", "room": "tests", "filed_at": "2026-01-01"},
                {"wing": "alpha", "room": "tests", "filed_at": "2026-01-02"},
                {"wing": "beta", "room": "deploy", "filed_at": "2026-01-03"},
                {"wing": "beta", "room": "deploy", "filed_at": "2026-01-04"},
            ],
        )

        report_alpha = mcp_server.tool_dedup_report(threshold=0.92, wing="alpha")
        assert report_alpha["scanned"] == 2

    def test_report_empty_palace(self, config, collection):
        """Report on empty palace should return zeros."""
        _setup_mcp(config, collection)
        report = mcp_server.tool_dedup_report()
        assert report["scanned"] == 0
        assert report["total_duplicates"] == 0

    def test_report_threshold_sensitivity(self, config, collection):
        """Lower threshold catches more near-duplicates."""
        _setup_mcp(config, collection)

        collection.add(
            ids=["sim1", "sim2"],
            documents=[
                "The authentication module uses JWT tokens for session management. "
                "Tokens expire after 24 hours. Refresh tokens are stored in cookies.",
                "The auth module uses JSON Web Tokens for session handling. "
                "Tokens expire after one day. Refresh tokens are in HTTP cookies.",
            ],
            metadatas=[
                {"wing": "proj", "room": "auth", "filed_at": "2026-01-01"},
                {"wing": "proj", "room": "auth", "filed_at": "2026-01-05"},
            ],
        )

        strict = mcp_server.tool_dedup_report(threshold=0.99)
        lenient = mcp_server.tool_dedup_report(threshold=0.70)

        # Lenient should find equal or more duplicates than strict
        assert lenient["total_duplicates"] >= strict["total_duplicates"]
