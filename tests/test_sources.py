"""Tests for the RFC 002 source-adapter scaffolding."""

import pytest

from mempalace.sources import (
    AdapterSchema,
    BaseSourceAdapter,
    DrawerRecord,
    FieldSpec,
    PalaceContext,
    RouteHint,
    SourceItemMetadata,
    SourceRef,
    SourceSummary,
    available_adapters,
    get_adapter,
    get_adapter_class,
    register,
    reset_adapters,
    resolve_adapter_for_source,
    unregister,
)
from mempalace.sources.transforms import (
    RESERVED_TRANSFORMATIONS,
    blank_line_drop,
    get_transformation,
    line_join_spaces,
    line_trim,
    newline_normalize,
    utf8_replace_invalid,
    whitespace_collapse_internal,
    whitespace_trim,
)


# ---------------------------------------------------------------------------
# Minimal conforming adapter used as a fixture across tests
# ---------------------------------------------------------------------------


class _TrivialAdapter(BaseSourceAdapter):
    name = "_trivial"
    adapter_version = "0.1.0"
    capabilities = frozenset({"byte_preserving"})
    supported_modes = frozenset({"whole_record"})
    declared_transformations = frozenset()
    default_privacy_class = "public"

    def ingest(self, *, source, palace):
        yield SourceItemMetadata(source_file=source.uri or "x", version="v1")
        yield DrawerRecord(content="hello", source_file=source.uri or "x", chunk_index=0)

    def describe_schema(self):
        return AdapterSchema(
            version="1.0",
            fields={"example": FieldSpec(type="string", required=False, description="x")},
        )


@pytest.fixture(autouse=True)
def _isolate_registry():
    yield
    reset_adapters()
    for name in list(available_adapters()):
        unregister(name)


# ---------------------------------------------------------------------------
# base.py — ABC + typed records
# ---------------------------------------------------------------------------


def test_base_adapter_is_abstract_without_required_methods():
    with pytest.raises(TypeError):

        class Incomplete(BaseSourceAdapter):
            name = "incomplete"

        Incomplete()


def test_conforming_adapter_instantiates_and_yields_typed_records():
    adapter = _TrivialAdapter()
    results = list(adapter.ingest(source=SourceRef(uri="foo"), palace=None))
    assert len(results) == 2
    assert isinstance(results[0], SourceItemMetadata)
    assert isinstance(results[1], DrawerRecord)
    assert results[1].content == "hello"


def test_is_current_default_is_false_always_reextracts():
    adapter = _TrivialAdapter()
    item = SourceItemMetadata(source_file="f", version="v1")
    assert adapter.is_current(item=item, existing_metadata=None) is False
    assert adapter.is_current(item=item, existing_metadata={"version": "v1"}) is False


def test_source_summary_default_uses_adapter_name():
    adapter = _TrivialAdapter()
    summary = adapter.source_summary(source=SourceRef(uri="x"))
    assert isinstance(summary, SourceSummary)
    assert summary.description == "_trivial"


def test_source_ref_options_default_is_empty_dict():
    # Frozen dataclass must not share a default_factory=dict instance across instances.
    a = SourceRef(uri="a")
    b = SourceRef(uri="b")
    a.options["touched"] = True
    assert "touched" not in b.options


# ---------------------------------------------------------------------------
# transforms.py
# ---------------------------------------------------------------------------


def test_reserved_transformations_registry_has_all_13():
    expected = {
        "utf8_replace_invalid",
        "newline_normalize",
        "whitespace_trim",
        "whitespace_collapse_internal",
        "line_trim",
        "line_join_spaces",
        "blank_line_drop",
        "strip_tool_chrome",
        "tool_result_truncate",
        "tool_result_omitted",
        "spellcheck_user",
        "synthesized_marker",
        "speaker_role_assignment",
    }
    assert set(RESERVED_TRANSFORMATIONS) == expected


def test_utf8_replace_invalid_handles_bad_bytes():
    # A lone 0xff byte is never valid UTF-8; U+FFFD should replace it.
    assert utf8_replace_invalid(b"ok \xff end") == "ok \ufffd end"


def test_newline_normalize_converts_crlf_and_cr():
    assert newline_normalize("a\r\nb\rc\nd") == "a\nb\nc\nd"


def test_whitespace_trim_strips_boundaries():
    assert whitespace_trim("  hello\n\n") == "hello"


def test_whitespace_collapse_internal_caps_at_two_blanks():
    # Five blanks collapses to exactly three newlines (two blank lines).
    text = "a\n\n\n\n\nb"
    assert whitespace_collapse_internal(text) == "a\n\n\nb"


def test_line_trim_strips_each_line():
    assert line_trim("  a  \n\t b \n c") == "a\nb\nc"


def test_line_join_spaces_preserves_paragraph_breaks():
    text = "foo\nbar\nbaz\n\nqux\nquux"
    assert line_join_spaces(text) == "foo bar baz\n\nqux quux"


def test_blank_line_drop_removes_blanks_only():
    assert blank_line_drop("a\n\nb\n\n\nc") == "a\nb\nc"


def test_get_transformation_resolves_reserved_and_rejects_unknown():
    assert get_transformation("newline_normalize") is newline_normalize
    with pytest.raises(KeyError):
        get_transformation("not_a_real_transformation")


# ---------------------------------------------------------------------------
# registry.py
# ---------------------------------------------------------------------------


def test_register_and_get_adapter_roundtrip():
    register("_trivial", _TrivialAdapter)
    assert "_trivial" in available_adapters()
    inst = get_adapter("_trivial")
    assert isinstance(inst, _TrivialAdapter)
    # Cached: repeated calls return the same instance.
    assert get_adapter("_trivial") is inst


def test_get_adapter_class_returns_class_not_instance():
    register("_trivial", _TrivialAdapter)
    assert get_adapter_class("_trivial") is _TrivialAdapter


def test_get_adapter_unknown_raises_key_error():
    with pytest.raises(KeyError):
        get_adapter("does-not-exist")


def test_unregister_drops_registration_and_cached_instance():
    register("_trivial", _TrivialAdapter)
    get_adapter("_trivial")
    unregister("_trivial")
    assert "_trivial" not in available_adapters()
    with pytest.raises(KeyError):
        get_adapter("_trivial")


def test_resolve_adapter_priority_order():
    # Explicit wins over everything.
    assert resolve_adapter_for_source(explicit="cursor", config_value="git") == "cursor"
    # Config wins over default.
    assert resolve_adapter_for_source(config_value="git") == "git"
    # Default is filesystem (preserves existing ``mempalace mine <path>`` behavior).
    assert resolve_adapter_for_source() == "filesystem"


# ---------------------------------------------------------------------------
# PalaceContext
# ---------------------------------------------------------------------------


class _FakeCollection:
    def __init__(self):
        self.upserts = []

    def add(self, **kwargs):
        pass

    def upsert(self, **kwargs):
        self.upserts.append(kwargs)

    def query(self, **kwargs):
        return {}

    def get(self, **kwargs):
        return {}

    def delete(self, **kwargs):
        pass

    def count(self):
        return 0


class _FakeKG:
    def __init__(self):
        self.triples = []

    def add_triple(self, subject, predicate, obj, **kwargs):
        self.triples.append((subject, predicate, obj, kwargs))


def test_palace_context_upsert_drawer_stamps_adapter_metadata():
    drawers = _FakeCollection()
    kg = _FakeKG()
    ctx = PalaceContext(
        drawer_collection=drawers,
        knowledge_graph=kg,
        palace_path="/tmp/palace",
        adapter_name="test-adapter",
        adapter_version="0.1.0",
    )
    record = DrawerRecord(
        content="hello",
        source_file="/abs/path/file.txt",
        chunk_index=2,
        metadata={"wing": "proj"},
    )
    ctx.upsert_drawer(record)

    assert len(drawers.upserts) == 1
    kwargs = drawers.upserts[0]
    assert kwargs["documents"] == ["hello"]
    assert len(kwargs["ids"]) == 1
    meta = kwargs["metadatas"][0]
    assert meta["wing"] == "proj"
    assert meta["adapter_name"] == "test-adapter"
    assert meta["adapter_version"] == "0.1.0"
    assert meta["source_file"] == "/abs/path/file.txt"
    assert meta["chunk_index"] == 2


def test_palace_context_drawer_id_is_sha256_prefix_not_sha1():
    """Guards against the pre-review sha1[:16]=64-bit id scheme.

    64-bit ids sit close to the birthday bound for palace-sized corpora.
    The helper uses sha256[:24]=96 bits so collision risk stays negligible.
    """
    import hashlib

    from mempalace.sources.context import _build_drawer_id

    src = "/an/absolute/path/to/a/file.txt"
    record = DrawerRecord(content="x", source_file=src, chunk_index=3)
    drawer_id = _build_drawer_id(record)

    expected_prefix = hashlib.sha256(src.encode("utf-8")).hexdigest()[:24]
    assert drawer_id == f"{expected_prefix}_3"
    # Negative: the old sha1 scheme MUST NOT produce the same id.
    sha1_prefix = hashlib.sha1(src.encode("utf-8")).hexdigest()[:16]
    assert drawer_id != f"{sha1_prefix}_3"


def test_palace_context_skip_current_item_sets_flag():
    ctx = PalaceContext(
        drawer_collection=_FakeCollection(),
        knowledge_graph=_FakeKG(),
        palace_path="/tmp/p",
    )
    assert ctx._skip_requested is False
    ctx.skip_current_item()
    assert ctx._skip_requested is True


def test_palace_context_emit_dispatches_to_hooks_and_swallows_errors():
    calls = []
    err_calls = []

    def good_hook(event, **details):
        calls.append((event, details))

    def bad_hook(event, **details):
        err_calls.append(event)
        raise RuntimeError("hook exploded")

    ctx = PalaceContext(
        drawer_collection=_FakeCollection(),
        knowledge_graph=_FakeKG(),
        palace_path="/tmp/p",
        progress_hooks=[good_hook, bad_hook],
    )
    ctx.emit("mined_file", path="a.txt", bytes=42)
    assert calls == [("mined_file", {"path": "a.txt", "bytes": 42})]
    assert err_calls == ["mined_file"]  # was invoked; error was swallowed


def test_palace_context_uses_route_hint_when_present():
    # Route hints are frozen dataclasses the adapter passes through.
    hint = RouteHint(wing="proj", room="backend", hall="general")
    assert hint.wing == "proj"
    assert hint.room == "backend"


# ---------------------------------------------------------------------------
# KnowledgeGraph new provenance params (RFC 002 §5.5)
# ---------------------------------------------------------------------------


def test_knowledge_graph_add_triple_accepts_source_drawer_id_and_adapter_name(tmp_path):
    from mempalace.knowledge_graph import KnowledgeGraph

    kg = KnowledgeGraph(db_path=str(tmp_path / "kg.sqlite3"))
    try:
        triple_id = kg.add_triple(
            "Ben",
            "committed",
            "PR-567",
            valid_from="2026-03-12",
            source_file="github.com/org/repo#pr=567",
            source_drawer_id="abc123_0",
            adapter_name="git",
        )
        assert triple_id is not None

        import sqlite3

        conn = sqlite3.connect(str(tmp_path / "kg.sqlite3"))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT source_drawer_id, adapter_name FROM triples WHERE id=?", (triple_id,)
        ).fetchone()
        assert row["source_drawer_id"] == "abc123_0"
        assert row["adapter_name"] == "git"
        conn.close()
    finally:
        kg.close()


def test_knowledge_graph_fresh_schema_includes_new_columns(tmp_path):
    """Brand-new palaces should get source_drawer_id / adapter_name directly
    from CREATE TABLE, not via a post-hoc ALTER. _migrate_schema exists only
    for legacy palaces."""
    import sqlite3

    from mempalace.knowledge_graph import KnowledgeGraph

    kg = KnowledgeGraph(db_path=str(tmp_path / "fresh.sqlite3"))
    try:
        conn = sqlite3.connect(str(tmp_path / "fresh.sqlite3"))
        cols = {row[1] for row in conn.execute("PRAGMA table_info(triples)")}
        conn.close()
        assert "source_drawer_id" in cols
        assert "adapter_name" in cols
    finally:
        kg.close()


def test_knowledge_graph_migration_adds_missing_columns_to_old_schema(tmp_path):
    """An old-schema triples table (pre-RFC 002) should auto-migrate on open."""
    import sqlite3

    db_path = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE entities (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            type TEXT DEFAULT 'unknown',
            properties TEXT DEFAULT '{}',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE triples (
            id TEXT PRIMARY KEY,
            subject TEXT NOT NULL,
            predicate TEXT NOT NULL,
            object TEXT NOT NULL,
            valid_from TEXT,
            valid_to TEXT,
            confidence REAL DEFAULT 1.0,
            source_closet TEXT,
            source_file TEXT,
            extracted_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.commit()
    conn.close()

    from mempalace.knowledge_graph import KnowledgeGraph

    kg = KnowledgeGraph(db_path=str(db_path))
    try:
        # New columns must be present after _init_db runs the migration.
        conn = sqlite3.connect(str(db_path))
        cols = {row[1] for row in conn.execute("PRAGMA table_info(triples)")}
        conn.close()
        assert "source_drawer_id" in cols
        assert "adapter_name" in cols

        # New-column insert works.
        kg.add_triple("a", "rel", "b", source_drawer_id="d0", adapter_name="x")
    finally:
        kg.close()


def test_knowledge_graph_add_triple_backwards_compatible_without_new_kwargs(tmp_path):
    """Existing callers that omit the RFC 002 kwargs keep working unchanged."""
    from mempalace.knowledge_graph import KnowledgeGraph

    kg = KnowledgeGraph(db_path=str(tmp_path / "kg.sqlite3"))
    try:
        triple_id = kg.add_triple("Max", "likes", "trains")
        assert triple_id is not None
    finally:
        kg.close()


# ---------------------------------------------------------------------------
# pyproject entry-point group is discoverable even when empty
# ---------------------------------------------------------------------------


def test_entry_point_group_exists_and_returns_zero_or_more_adapters():
    # No in-tree first-party adapters yet (miners migrate in a follow-up PR),
    # but the ``mempalace.sources`` entry-point group is declared so third-
    # party packages can register. ``available_adapters`` MUST NOT raise.
    adapters = available_adapters()
    assert isinstance(adapters, list)
