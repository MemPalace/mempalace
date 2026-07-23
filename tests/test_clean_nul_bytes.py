"""Tests for embedded-NUL sanitisation in the bulk-ingest write path.

Mirrors ``test_clean_lone_surrogates.py`` (issue #1235) for a sibling class of
bad input: a document containing an embedded NUL character (U+0000).

Unlike a lone surrogate, a NUL-containing string is well-formed UTF-8 — it
doesn't raise on ``.encode()``. But handing it to ChromaDB's SQLite/FTS5
layer can corrupt the FTS5 inverted index for the *whole* collection
(``PRAGMA quick_check`` reports "malformed inverted index for FTS5 table"),
not just fail to store that one document. This is routine in mined Bash
tool output — e.g. a reader racing a background writer, or genuine
NUL-delimited/binary command output captured verbatim.

``sanitize_content`` (the MCP write-tool path) already rejects NUL bytes
outright (``ValueError``) — that behaviour is unchanged and still tested
here for completeness. The bulk ingest paths (miner, convo_miner, sweeper,
diary_ingest) build documents without routing through that helper and reach
``ChromaCollection`` directly, exactly as #1235 found for surrogates. This
file covers the chokepoint fix that closes that gap: replace, not reject, so
a NUL-containing chunk doesn't abort or corrupt an otherwise-good mine.
"""

from mempalace.config import strip_nul_bytes


# ── Unit tests ─────────────────────────────────────────────────────────────


class TestStripNulBytes:
    def test_passthrough_normal(self):
        assert strip_nul_bytes("hello world") == "hello world"
        assert strip_nul_bytes("你好世界") == "你好世界"

    def test_empty_string(self):
        assert strip_nul_bytes("") == ""

    def test_replaces_single_nul(self):
        assert strip_nul_bytes("hello\x00world") == "hello�world"

    def test_replaces_multiple_nuls(self):
        assert strip_nul_bytes("\x00\x00\x00") == "���"
        assert strip_nul_bytes("a\x00b\x00c") == "a�b�c"

    def test_replaces_large_nul_run(self):
        """The reporter-shaped case: a large contiguous NUL run inside
        otherwise-ordinary captured tool output."""
        dirty = "before-nuls " + ("\x00" * 10291) + " after-nuls"
        clean = strip_nul_bytes(dirty)
        assert "\x00" not in clean
        assert clean.startswith("before-nuls ")
        assert clean.endswith(" after-nuls")
        assert clean.count("�") == 10291

    def test_preserves_real_content_around_nuls(self):
        assert (
            strip_nul_bytes("ordinary log line one\x00\x00\x00ordinary log line two")
            == "ordinary log line one���ordinary log line two"
        )


# ── Existing sanitizer behavior (unchanged) ─────────────────────────────────


class TestSanitizeContentStillRejectsNuls:
    """sanitize_content (the MCP write-tool path) already guards against NUL
    bytes by rejecting them outright — that's a reasonable choice for an
    interactive tool call and is untouched by this fix. Verifies it still
    holds now that a sibling path (the backend chokepoint) takes the
    replace-not-reject approach instead, for the bulk-ingest paths where
    rejecting one bad chunk would otherwise abort or corrupt a whole mine.
    """

    def test_sanitize_content_rejects_nul(self):
        from mempalace.config import sanitize_content

        import pytest

        with pytest.raises(ValueError, match="null bytes"):
            sanitize_content("hello\x00world")


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


class TestBackendChokepointStripsNulBytes:
    """The bulk ingest paths (miner, convo_miner, sweeper, diary_ingest) build
    documents without routing through ``sanitize_content`` and reach
    ``ChromaCollection`` directly — the same gap #1235 found for surrogates.
    A NUL byte in *document* text can corrupt the FTS5 inverted index for the
    whole collection. The backend chokepoint must strip it, mirroring the
    existing surrogate handling in the same method."""

    @staticmethod
    def _collection():
        from mempalace.backends.chroma import ChromaCollection

        fake = _CapturingCollection()
        return fake, ChromaCollection(fake)

    def test_add_strips_nul_in_document(self):
        fake, col = self._collection()
        col.add(documents=["clean\x00doc"], ids=["1"])
        _, kwargs = fake.calls[0]
        assert kwargs["documents"] == ["clean�doc"]

    def test_upsert_strips_nul_in_document(self):
        fake, col = self._collection()
        col.upsert(documents=["a\x00b"], ids=["1"], metadatas=[{"wing": "w"}])
        _, kwargs = fake.calls[0]
        assert kwargs["documents"] == ["a�b"]

    def test_update_strips_nul_in_document(self):
        fake, col = self._collection()
        col.update(ids=["1"], documents=["x\x00y"])
        _, kwargs = fake.calls[0]
        assert kwargs["documents"] == ["x�y"]

    def test_one_nul_message_does_not_corrupt_the_batch(self):
        """The reporter's shape: a single NUL-heavy row alongside ordinary
        ones in the same batch. All rows survive, sanitised."""
        fake, col = self._collection()
        col.upsert(
            documents=["ok one", "poison\x00row", "ok three"],
            ids=["1", "2", "3"],
        )
        _, kwargs = fake.calls[0]
        assert kwargs["documents"] == ["ok one", "poison�row", "ok three"]
        assert len(kwargs["ids"]) == 3

    def test_both_surrogates_and_nuls_in_same_document(self):
        """Both sanitizers apply to the same document — order doesn't
        leave either kind of bad character behind."""
        fake, col = self._collection()
        col.upsert(documents=["a\udc95b\x00c"], ids=["1"])
        _, kwargs = fake.calls[0]
        assert kwargs["documents"] == ["a�b�c"]

    def test_single_string_document_is_not_split_into_chars(self):
        """chromadb accepts a bare str as one document (OneOrMany[Document]).
        The sanitiser must keep it whole and clean, not split it into
        per-character documents."""
        fake, col = self._collection()
        col.upsert(documents="one\x00document", ids=["1"])
        _, kwargs = fake.calls[0]
        assert kwargs["documents"] == "one�document"
        assert isinstance(kwargs["documents"], str)
