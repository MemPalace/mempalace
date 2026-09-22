"""Tests for Strands Agents MemPalace memory tools.

Verifies:
  - Verbatim storage (byte-for-byte content preservation)
  - Empty/missing user_id handling (fallback to \"default\")
  - Cross-user isolation (user A cannot recall user B's memories)
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fixtures / Mocks
# ---------------------------------------------------------------------------


class MockToolContext:
    """Minimal ToolContext mock with invocation_state."""

    def __init__(self, user_id: str | None = None):
        self.invocation_state = {}
        if user_id is not None:
            self.invocation_state["user_id"] = user_id


class MockCollection:
    """In-memory ChromaDB collection mock for testing."""

    def __init__(self):
        self.documents: list[str] = []
        self.ids: list[str] = []
        self.metadatas: list[dict] = []

    def add(self, ids, documents, metadatas):
        self.ids.extend(ids)
        self.documents.extend(documents)
        self.metadatas.extend(metadatas)

    def get(self, where=None, **kwargs):
        """Filter by metadata where clause."""
        results = {"ids": [], "documents": [], "metadatas": []}
        for i, meta in enumerate(self.metadatas):
            if where is None or all(meta.get(k) == v for k, v in where.items()):
                results["ids"].append(self.ids[i])
                results["documents"].append(self.documents[i])
                results["metadatas"].append(self.metadatas[i])
        return results


@pytest.fixture
def mock_collection():
    return MockCollection()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestVerbatimStorage:
    """Stored drawer content must be byte-for-byte identical to input."""

    def test_simple_text_stored_verbatim(self, mock_collection):
        """Plain text is stored without any transformation."""
        import sys
        sys.path.insert(0, ".")
        from memory_tools import mp_memory_store

        content = "User prefers dark mode with 80% opacity"
        ctx = MockToolContext(user_id="test-user")

        with patch("memory_tools._get_collection", return_value=mock_collection):
            with patch("memory_tools.mine_lock", side_effect=lambda _: __import__("contextlib").nullcontext()):
                result = mp_memory_store(content=content, tool_context=ctx)

        assert mock_collection.documents[0] == content
        assert "Stored verbatim" in result

    def test_multiline_content_preserved(self, mock_collection):
        """Multiline text, whitespace, special chars \u2014 all preserved."""
        import sys
        sys.path.insert(0, ".")
        from memory_tools import mp_memory_store

        content = "Line 1: important\n  Line 2: indented\n\nLine 4: after blank"
        ctx = MockToolContext(user_id="test-user")

        with patch("memory_tools._get_collection", return_value=mock_collection):
            with patch("memory_tools.mine_lock", side_effect=lambda _: __import__("contextlib").nullcontext()):
                mp_memory_store(content=content, tool_context=ctx)

        assert mock_collection.documents[0] == content

    def test_unicode_preserved(self, mock_collection):
        """Unicode (Cyrillic, emoji, CJK) stored verbatim."""
        import sys
        sys.path.insert(0, ".")
        from memory_tools import mp_memory_store

        content = "\u041f\u0440\u0438\u0432\u0435\u0442 \u043c\u0438\u0440! \ud83c\udf2a\ufe0f \u65e5\u672c\u8a9e\u30c6\u30b9\u30c8"
        ctx = MockToolContext(user_id="test-user")

        with patch("memory_tools._get_collection", return_value=mock_collection):
            with patch("memory_tools.mine_lock", side_effect=lambda _: __import__("contextlib").nullcontext()):
                mp_memory_store(content=content, tool_context=ctx)

        assert mock_collection.documents[0] == content

    def test_normalize_version_in_metadata(self, mock_collection):
        """Metadata includes normalize_version for MemPalace compatibility."""
        import sys
        sys.path.insert(0, ".")
        from memory_tools import mp_memory_store

        content = "test"
        ctx = MockToolContext(user_id="test-user")

        with patch("memory_tools._get_collection", return_value=mock_collection):
            with patch("memory_tools.mine_lock", side_effect=lambda _: __import__("contextlib").nullcontext()):
                mp_memory_store(content=content, tool_context=ctx)

        meta = mock_collection.metadatas[0]
        assert "normalize_version" in meta
        assert meta["normalize_version"] >= 2


class TestUserIdHandling:
    """Empty or missing user_id must fall back to 'default' room."""

    def test_missing_user_id_uses_default(self, mock_collection):
        """No user_id in invocation_state \u2192 room = 'user-default'."""
        import sys
        sys.path.insert(0, ".")
        from memory_tools import mp_memory_store

        ctx = MockToolContext(user_id=None)

        with patch("memory_tools._get_collection", return_value=mock_collection):
            with patch("memory_tools.mine_lock", side_effect=lambda _: __import__("contextlib").nullcontext()):
                mp_memory_store(content="test", tool_context=ctx)

        assert mock_collection.metadatas[0]["room"] == "user-default"

    def test_empty_string_user_id_uses_default(self, mock_collection):
        """Empty string user_id \u2192 room = 'user-default'."""
        import sys
        sys.path.insert(0, ".")
        from memory_tools import mp_memory_store

        ctx = MockToolContext(user_id="")

        with patch("memory_tools._get_collection", return_value=mock_collection):
            with patch("memory_tools.mine_lock", side_effect=lambda _: __import__("contextlib").nullcontext()):
                mp_memory_store(content="test", tool_context=ctx)

        assert mock_collection.metadatas[0]["room"] == "user-default"

    def test_valid_user_id_scoped(self, mock_collection):
        """Valid user_id \u2192 room = 'user-{user_id}'."""
        import sys
        sys.path.insert(0, ".")
        from memory_tools import mp_memory_store

        ctx = MockToolContext(user_id="alice")

        with patch("memory_tools._get_collection", return_value=mock_collection):
            with patch("memory_tools.mine_lock", side_effect=lambda _: __import__("contextlib").nullcontext()):
                mp_memory_store(content="test", tool_context=ctx)

        assert mock_collection.metadatas[0]["room"] == "user-alice"


class TestCrossUserIsolation:
    """User A cannot access User B's memories through recall."""

    def test_recall_scoped_to_user(self):
        """mp_memory_recall filters by user's room \u2014 cannot see other users."""
        import sys
        sys.path.insert(0, ".")
        from memory_tools import mp_memory_recall

        mock_searcher = MagicMock()
        mock_searcher.return_value = {"results": [{"text": "secret"}]}

        with patch("memory_tools._get_searcher", return_value=mock_searcher):
            ctx_alice = MockToolContext(user_id="alice")
            mp_memory_recall(query="preferences", tool_context=ctx_alice)

            # Verify searcher was called with alice's room
            call_kwargs = mock_searcher.call_args[1]
            assert call_kwargs["room"] == "user-alice"

            ctx_bob = MockToolContext(user_id="bob")
            mp_memory_recall(query="preferences", tool_context=ctx_bob)

            # Verify searcher was called with bob's room (not alice's)
            call_kwargs = mock_searcher.call_args[1]
            assert call_kwargs["room"] == "user-bob"
