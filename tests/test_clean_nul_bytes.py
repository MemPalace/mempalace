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
file covers the chokepoint that closes that gap: the NUL is stored as a
reversible escape, so a NUL-containing chunk neither aborts nor corrupts an
otherwise-good mine, and reads return it unchanged.
"""

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
        import pytest

        from mempalace.config import sanitize_content

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


def _stored_docs(kwargs):
    """Documents as a reader gets them back from what reached chromadb."""
    from mempalace.stored_text import restore_rows

    docs = kwargs["documents"]
    single = isinstance(docs, str)
    docs, _ = restore_rows([docs] if single else docs, kwargs["metadatas"])
    return docs[0] if single else docs


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
        stored = kwargs["documents"][0]
        assert "\x00" not in stored
        stored.encode("utf-8")
        assert _stored_docs(kwargs)[0] == "clean\x00doc"

    def test_upsert_strips_nul_in_document(self):
        fake, col = self._collection()
        col.upsert(documents=["a\x00b"], ids=["1"], metadatas=[{"wing": "w"}])
        _, kwargs = fake.calls[0]
        stored = kwargs["documents"][0]
        assert "\x00" not in stored
        assert _stored_docs(kwargs)[0] == "a\x00b"
        from mempalace.stored_text import FLAG

        assert kwargs["metadatas"][0][FLAG] == 1

    def test_update_strips_nul_in_document(self):
        fake, col = self._collection()
        col.update(ids=["1"], documents=["x\x00y"])
        _, kwargs = fake.calls[0]
        stored = kwargs["documents"][0]
        assert "\x00" not in stored
        assert _stored_docs(kwargs)[0] == "x\x00y"

    def test_one_nul_message_does_not_corrupt_the_batch(self):
        """The reporter's shape: a single NUL-heavy row alongside ordinary
        ones in the same batch. All rows survive, sanitised."""
        fake, col = self._collection()
        col.upsert(
            documents=["ok one", "poison\x00row", "ok three"],
            ids=["1", "2", "3"],
        )
        _, kwargs = fake.calls[0]
        assert _stored_docs(kwargs) == [
            "ok one",
            "poison\x00row",
            "ok three",
        ]
        assert all("\x00" not in doc for doc in kwargs["documents"])
        assert len(kwargs["ids"]) == 3

    def test_both_surrogates_and_nuls_in_same_document(self):
        """Both sanitizers apply to the same document — order doesn't
        leave either kind of bad character behind."""
        fake, col = self._collection()
        original = "a\udc95b\x00c"
        col.upsert(documents=[original], ids=["1"])
        _, kwargs = fake.calls[0]
        stored = kwargs["documents"][0]
        stored.encode("utf-8")
        assert "\x00" not in stored
        assert "\udc95" not in stored
        assert _stored_docs(kwargs)[0] == original

    def test_single_string_document_is_not_split_into_chars(self):
        """chromadb accepts a bare str as one document (OneOrMany[Document]).
        The sanitiser must keep it whole and clean, not split it into
        per-character documents."""
        fake, col = self._collection()
        col.upsert(documents="one\x00document", ids=["1"])
        _, kwargs = fake.calls[0]
        assert isinstance(kwargs["documents"], str)
        assert "\x00" not in kwargs["documents"]
        assert _stored_docs(kwargs) == "one\x00document"
