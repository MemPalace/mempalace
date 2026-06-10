"""Tests for lone-surrogate sanitisation (issue #1235).

Covers:
- Unit: ``strip_lone_surrogates()`` edge cases (lone surrogates, real emoji,
  empty input, hashing).
- Integration: every MCP tool that takes a user string and reaches ChromaDB
  must survive lone surrogates in its payload.

MCP clients (Claude Desktop, WorkBuddy) occasionally relay lone UTF-16
surrogates (U+D800–U+DFFF) when proxying binary-in-Unicode or corrupted
clipboard input. Python's ``str.encode('utf-8')`` raises on these, which
crashes ChromaDB add/upsert with -32000 Internal Error.

Note on surrogate pairs in Python source: writing ``"\\ud83d\\ude00"`` in
Python creates a string with *two* lone surrogates, not the astral emoji
U+1F600. Python 3 does not silently merge them. Use ``"\\U0001f600"`` to
embed a real emoji as a single code point.
"""

import hashlib

from mempalace.config import strip_lone_surrogates


# ── Unit tests ─────────────────────────────────────────────────────────────


class TestStripLoneSurrogates:
    def test_passthrough_normal(self):
        assert strip_lone_surrogates("hello world") == "hello world"
        assert strip_lone_surrogates("你好世界") == "你好世界"
        assert strip_lone_surrogates("mixed 中 English 文") == "mixed 中 English 文"

    def test_empty_string(self):
        assert strip_lone_surrogates("") == ""

    def test_replaces_high_surrogate(self):
        assert strip_lone_surrogates("hello\udc95world") == "hello�world"
        assert strip_lone_surrogates("\udcff\udc00\udcaf") == "�" * 3

    def test_replaces_low_surrogate(self):
        assert strip_lone_surrogates("test\ud800more") == "test�more"
        assert strip_lone_surrogates("\ud800\udbff") == "�" * 2

    def test_replaces_multiple_at_different_positions(self):
        assert strip_lone_surrogates("a\udca1b\udcffc") == "a�b�c"

    def test_preserves_real_emoji(self):
        """Astral code points written with \\U have no surrogates and pass through."""
        assert strip_lone_surrogates("\U0001f600") == "\U0001f600"
        assert strip_lone_surrogates("\U0001f680") == "\U0001f680"
        assert strip_lone_surrogates("hello \U0001f600 world") == "hello \U0001f600 world"

    def test_real_emoji_with_adjacent_lone_surrogate(self):
        assert strip_lone_surrogates("\U0001f600\udc95") == "\U0001f600�"
        assert strip_lone_surrogates("\udc95\U0001f600") == "�\U0001f600"

    def test_only_surrogates(self):
        assert strip_lone_surrogates("\udc95\udcff") == "��"
        assert strip_lone_surrogates("\ud800" + "\udbff" + "\udc00" + "\udfff") == "�" * 4

    def test_cleaned_string_is_utf8_encodable(self):
        """The actual crash path: encode/hash must not raise after cleaning."""
        dirty = "content\udc95with\ud800surrogate"
        clean = strip_lone_surrogates(dirty)
        h = hashlib.sha256(clean.encode("utf-8")).hexdigest()
        assert len(h) == 64

    def test_workbuddy_injected_surrogate(self):
        """The specific surrogate observed in WorkBuddy production logs."""
        result = strip_lone_surrogates("2026-04-27\udcadworkBuddy relay")
        assert result == "2026-04-27�workBuddy relay"
        # Must not raise — was the original failure mode.
        hashlib.sha256(result.encode("utf-8")).hexdigest()


# ── Sanitizer integration ──────────────────────────────────────────────────


class TestSanitizersStripSurrogates:
    """sanitize_content and sanitize_kg_value clean inline — verifies the
    architectural fix so all MCP tools route through and inherit it for free."""

    def test_sanitize_content_strips_surrogates(self):
        from mempalace.config import sanitize_content

        assert sanitize_content("hello\udc95world") == "hello�world"

    def test_sanitize_kg_value_strips_surrogates(self):
        from mempalace.config import sanitize_kg_value

        assert sanitize_kg_value("Alice\udc95") == "Alice�"

    def test_sanitize_query_strips_surrogates(self):
        from mempalace.query_sanitizer import sanitize_query

        result = sanitize_query("search\udc95term")
        assert "\udc95" not in result["clean_query"]
        assert "�" in result["clean_query"]


# ── End-to-end tool integration ────────────────────────────────────────────


def _patch_mcp_server(monkeypatch, config, kg):
    from mempalace import mcp_server

    monkeypatch.setattr(mcp_server, "_config", config)
    monkeypatch.setattr(mcp_server, "_get_kg", lambda: kg)


class TestToolsAcceptSurrogates:
    """End-to-end: every MCP tool that takes user text must not crash on
    lone surrogates. Failures here surface as ChromaDB UnicodeEncodeError."""

    def test_add_drawer_content(self, monkeypatch, collection, config, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_add_drawer

        result = tool_add_drawer(
            wing="test",
            room="surrogate",
            content="drawer content with \udc95 surrogate",
        )
        assert result["success"] is True
        assert result["drawer_id"].startswith("drawer_test_surrogate_")

    def test_add_drawer_metadata(self, monkeypatch, collection, config, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_add_drawer

        result = tool_add_drawer(
            wing="test",
            room="meta",
            content="content here",
            source_file="path/to/\udcadfile.txt",
            added_by="user\udc95agent",
        )
        assert result["success"] is True

    def test_check_duplicate(self, monkeypatch, collection, config, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_check_duplicate

        result = tool_check_duplicate(content="exact\udc95match")
        assert isinstance(result, dict)
        assert "is_duplicate" in result

    def test_search_query(self, monkeypatch, collection, config, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_search

        result = tool_search(query="search\udc95term")
        assert isinstance(result, dict)
        assert "error" not in result or "results" in result

    def test_update_drawer(self, monkeypatch, collection, config, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_add_drawer, tool_update_drawer

        add_result = tool_add_drawer(wing="test", room="update", content="original content")
        assert add_result["success"] is True

        update_result = tool_update_drawer(
            drawer_id=add_result["drawer_id"],
            content="updated\udc95content",
        )
        assert update_result["success"] is True

    def test_diary_write(self, monkeypatch, collection, config, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_diary_write

        result = tool_diary_write(
            agent_name="数数",
            entry="今日工作\udc95完成了，修复了Chromadb crash",
            topic="log",
        )
        assert result["success"] is True


# ── Backend chokepoint (bulk ingest paths) ──────────────────────────────────


class _CapturingCollection:
    """Minimal chromadb.Collection stand-in that records the kwargs it receives,
    so we can assert what actually reaches the chromadb client."""

    def __init__(self):
        self.calls = []

    def add(self, **kwargs):
        self.calls.append(("add", kwargs))

    def upsert(self, **kwargs):
        self.calls.append(("upsert", kwargs))

    def update(self, **kwargs):
        self.calls.append(("update", kwargs))


class TestBackendChokepointStripsDocuments:
    """#1235 sanitised the MCP write tools, but the bulk ingest paths
    (miner, convo_miner, sweeper, diary_ingest) build documents without routing
    through ``sanitize_content`` and reach ``ChromaCollection`` directly. A lone
    surrogate in *document* text crashes the whole add/upsert batch (-32000),
    silently dropping the other rows. The backend chokepoint must strip
    documents, mirroring ``_sanitize_metadatas_for_chromadb``."""

    @staticmethod
    def _collection():
        from mempalace.backends.chroma import ChromaCollection

        fake = _CapturingCollection()
        return fake, ChromaCollection(fake)

    def test_add_strips_lone_surrogate_in_document(self):
        fake, col = self._collection()
        col.add(documents=["clean\udc95doc"], ids=["1"])
        _, kwargs = fake.calls[0]
        assert kwargs["documents"] == ["clean�doc"]
        kwargs["documents"][0].encode("utf-8")  # the crash path must not raise

    def test_upsert_strips_lone_surrogate_in_document(self):
        fake, col = self._collection()
        col.upsert(documents=["a\ud800b"], ids=["1"], metadatas=[{"wing": "w"}])
        _, kwargs = fake.calls[0]
        assert kwargs["documents"] == ["a�b"]

    def test_update_strips_lone_surrogate_in_document(self):
        fake, col = self._collection()
        col.update(ids=["1"], documents=["x\udcffy"])
        _, kwargs = fake.calls[0]
        assert kwargs["documents"] == ["x�y"]

    def test_one_poison_message_does_not_drop_the_batch(self):
        """A single bad row used to abort the entire batch, silently dropping
        the others. After sanitising, every row survives."""
        fake, col = self._collection()
        col.upsert(
            documents=["ok one", "poison\udc95row", "ok three"],
            ids=["1", "2", "3"],
        )
        _, kwargs = fake.calls[0]
        assert kwargs["documents"] == ["ok one", "poison�row", "ok three"]
        assert len(kwargs["ids"]) == 3
        for d in kwargs["documents"]:
            d.encode("utf-8")  # must not raise

    def test_real_emoji_and_none_metadata_preserved(self):
        fake, col = self._collection()
        col.add(documents=["ship \U0001f680"], ids=["1"])
        _, kwargs = fake.calls[0]
        assert kwargs["documents"] == ["ship \U0001f680"]

    def test_single_string_document_is_not_split_into_chars(self):
        """chromadb accepts a bare str as one document (OneOrMany[Document]).
        The sanitiser must keep it whole and clean, not split it into
        per-character documents."""
        fake, col = self._collection()
        col.upsert(documents="one\udc95document", ids=["1"])
        _, kwargs = fake.calls[0]
        assert kwargs["documents"] == "one�document"
        assert isinstance(kwargs["documents"], str)
