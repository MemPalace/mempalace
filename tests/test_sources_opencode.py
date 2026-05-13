"""Tests for the OpenCode source adapter (RFC 002).

Covers:
    * Adapter class identity (capabilities, modes, declared transformations).
    * Conformance against the RFC 002 spec — declared-transformation
      round-trip, schema-conformance, stable source_file shape.
    * Unit tests for the SQLite walk, tool-echo/file-injection skip,
      same-role merge, role coerce, transcript formatting, and the chunked
      exchange emit through ``convo_miner.chunk_exchanges``.
    * Edge cases: empty session, single-message session, sessions with
      mixed text + tool parts, multi-session DB across projects.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from mempalace.sources import transforms as src_transforms
from mempalace.sources.base import (
    AdapterClosedError,
    AdapterSchema,
    DrawerRecord,
    FieldSpec,
    SourceItemMetadata,
    SourceNotFoundError,
    SourceRef,
)
from mempalace.sources.context import PalaceContext
from mempalace.sources.opencode import (
    OpenCodeSourceAdapter,
    _build_source_bytes_per_session,
    session_source_file,
)

# Fixture builder lives next to the fixture data on purpose so it is
# discoverable by anyone investigating the .db format. Loaded via
# importlib so we don't mutate sys.path at module scope (which would
# also trip ruff E402 on the import-not-at-top check).
FIXTURE_DIR = Path(__file__).parent / "fixtures" / "opencode" / "sample_session_2026_05_12"
_fixture_spec = importlib.util.spec_from_file_location(
    "build_fixture", FIXTURE_DIR / "build_fixture.py"
)
build_fixture = importlib.util.module_from_spec(_fixture_spec)
# Register in sys.modules so dataclass / typing introspection inside
# build_fixture can resolve back to the module via cls.__module__.
sys.modules["build_fixture"] = build_fixture
_fixture_spec.loader.exec_module(build_fixture)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def canonical_db(tmp_path):
    """Build a SQLite fixture from the canonical synthetic sessions."""
    path = tmp_path / "opencode.db"
    build_fixture.build_fixture(str(path), sessions=build_fixture.CANONICAL_SESSIONS)
    return str(path)


@pytest.fixture
def adapter():
    return OpenCodeSourceAdapter()


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
    def add_triple(self, *args, **kwargs):
        pass


@pytest.fixture
def palace_ctx():
    return PalaceContext(
        drawer_collection=_FakeCollection(),
        knowledge_graph=_FakeKG(),
        palace_path="/tmp/palace",
        adapter_name="opencode",
        adapter_version="0.1.0",
    )


# ---------------------------------------------------------------------------
# Class identity
# ---------------------------------------------------------------------------


def test_adapter_identity():
    assert OpenCodeSourceAdapter.name == "opencode"
    assert OpenCodeSourceAdapter.spec_version == "1.0"
    assert OpenCodeSourceAdapter.adapter_version == "0.1.0"
    assert "chunked_content" in OpenCodeSourceAdapter.supported_modes
    assert "supports_incremental" in OpenCodeSourceAdapter.capabilities
    assert "adapter_owns_routing" in OpenCodeSourceAdapter.capabilities
    assert OpenCodeSourceAdapter.default_privacy_class == "pii_potential"


def test_declared_transformations_have_reference_impls():
    """Every declared transformation MUST resolve to an attribute on
    mempalace.sources.transforms (RFC 002 §7.3)."""
    for name in OpenCodeSourceAdapter.declared_transformations:
        impl = getattr(src_transforms, name, None)
        assert callable(impl), f"declared transformation {name!r} has no callable reference impl"


def test_describe_schema_returns_adapter_schema(adapter):
    schema = adapter.describe_schema()
    assert isinstance(schema, AdapterSchema)
    assert schema.version == "1.0"
    required = {k for k, v in schema.fields.items() if v.required}
    assert {
        "session_id",
        "project_dir",
        "session_created_at",
        "message_count",
        "extract_mode",
        "opencode_db_path",
    }.issubset(required)
    assert isinstance(schema.fields["session_id"], FieldSpec)
    assert schema.fields["session_id"].indexed is True
    assert schema.fields["project_dir"].indexed is True


# ---------------------------------------------------------------------------
# Source-file identity
# ---------------------------------------------------------------------------


def test_session_source_file_shape_is_stable():
    s1 = session_source_file("/tmp/x.db", "ses_abc")
    s2 = session_source_file("/tmp/x.db", "ses_abc")
    s3 = session_source_file("/tmp/x.db", "ses_def")
    assert s1 == s2
    assert s1 != s3
    assert s1 == "opencode:///tmp/x.db#session=ses_abc"


# ---------------------------------------------------------------------------
# DB resolution / errors
# ---------------------------------------------------------------------------


def test_ingest_raises_source_not_found_for_missing_db(adapter, palace_ctx, tmp_path):
    missing = tmp_path / "nope.db"
    ref = SourceRef(local_path=str(missing))
    with pytest.raises(SourceNotFoundError):
        list(adapter.ingest(source=ref, palace=palace_ctx))


def test_ingest_raises_source_not_found_for_db_without_expected_tables(
    adapter, palace_ctx, tmp_path
):
    path = tmp_path / "bad.db"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE other (id INTEGER)")
    conn.commit()
    conn.close()
    ref = SourceRef(local_path=str(path))
    with pytest.raises(SourceNotFoundError):
        list(adapter.ingest(source=ref, palace=palace_ctx))


def test_close_then_ingest_raises_adapter_closed(adapter, palace_ctx, canonical_db):
    adapter.close()
    ref = SourceRef(local_path=canonical_db)
    with pytest.raises(AdapterClosedError):
        list(adapter.ingest(source=ref, palace=palace_ctx))


# ---------------------------------------------------------------------------
# Source-summary
# ---------------------------------------------------------------------------


def test_source_summary_counts_sessions(adapter, canonical_db):
    summary = adapter.source_summary(source=SourceRef(local_path=canonical_db))
    # 4 sessions in the canonical fixture; the <2-message one is still counted
    # at the summary stage (it's a session, just skipped on ingest).
    assert summary.item_count == 4
    assert "OpenCode database at" in summary.description


def test_source_summary_for_missing_db(adapter, tmp_path):
    summary = adapter.source_summary(source=SourceRef(local_path=str(tmp_path / "absent.db")))
    assert summary.item_count == 0
    assert "not found" in summary.description.lower()


# ---------------------------------------------------------------------------
# Ingest shape
# ---------------------------------------------------------------------------


def test_ingest_yields_metadata_then_drawers_per_session(adapter, palace_ctx, canonical_db):
    results = list(adapter.ingest(source=SourceRef(local_path=canonical_db), palace=palace_ctx))
    metas = [r for r in results if isinstance(r, SourceItemMetadata)]
    drawers = [r for r in results if isinstance(r, DrawerRecord)]
    # 4 sessions in the fixture, each gets a SourceItemMetadata
    assert len(metas) == 4
    # 3 sessions have >=2 real text exchanges; the 4th (ses_ddd444) is skipped
    src_files = {d.source_file for d in drawers}
    assert len(src_files) == 3
    # Source files all carry the opencode:// prefix
    for sf in src_files:
        assert sf.startswith("opencode://")
        assert "#session=" in sf


def test_ingest_skips_cancelled_session_with_too_few_turns(adapter, palace_ctx, canonical_db):
    results = list(adapter.ingest(source=SourceRef(local_path=canonical_db), palace=palace_ctx))
    drawers = [r for r in results if isinstance(r, DrawerRecord)]
    src_files = {d.source_file for d in drawers}
    assert all(
        "ses_ddd444" not in sf for sf in src_files
    ), "single-message cancelled session must be skipped"


def test_drawer_metadata_carries_universal_and_schema_fields(adapter, palace_ctx, canonical_db):
    results = list(adapter.ingest(source=SourceRef(local_path=canonical_db), palace=palace_ctx))
    drawers = [r for r in results if isinstance(r, DrawerRecord)]
    assert drawers, "expected at least one drawer"
    schema_keys = set(adapter.describe_schema().fields.keys())
    universal_keys = {
        "source_file",
        "chunk_index",
        "filed_at",
        "added_by",
        "wing",
        "room",
        "hall",
        "ingest_mode",
        "extract_mode",
        "privacy_class",
    }
    for drawer in drawers:
        meta = drawer.metadata
        assert universal_keys.issubset(
            meta.keys()
        ), f"missing universal keys: {universal_keys - meta.keys()}"
        assert schema_keys.issubset(
            meta.keys()
        ), f"missing schema keys: {schema_keys - meta.keys()}"
        # Flat-scalar invariant — chroma constraint.
        for k, v in meta.items():
            assert isinstance(
                v, (str, int, float, bool)
            ), f"metadata[{k}]={v!r} of type {type(v).__name__} is not flat-scalar"


def test_drawer_route_hint_carries_wing(adapter, palace_ctx, canonical_db):
    results = list(adapter.ingest(source=SourceRef(local_path=canonical_db), palace=palace_ctx))
    drawers = [r for r in results if isinstance(r, DrawerRecord)]
    for d in drawers:
        assert d.route_hint is not None
        assert d.route_hint.wing  # never empty
        # OpenCode adapter populates room from convo_miner.detect_convo_room.
        assert d.route_hint.room


def test_wing_routing_groups_by_session_directory(adapter, palace_ctx, canonical_db):
    """Two frontend sessions and one backend session should produce two wings."""
    results = list(adapter.ingest(source=SourceRef(local_path=canonical_db), palace=palace_ctx))
    drawers = [r for r in results if isinstance(r, DrawerRecord)]
    wings = {d.metadata["wing"] for d in drawers}
    assert "frontend" in wings
    assert "backend" in wings


def test_explicit_wing_option_wins_over_directory(adapter, palace_ctx, canonical_db):
    ref = SourceRef(local_path=canonical_db, options={"wing": "Custom Wing"})
    results = list(adapter.ingest(source=ref, palace=palace_ctx))
    drawers = [r for r in results if isinstance(r, DrawerRecord)]
    wings = {d.metadata["wing"] for d in drawers}
    assert wings == {"custom_wing"}  # normalize_wing_name lower+underscored


# ---------------------------------------------------------------------------
# Skip / incremental behavior
# ---------------------------------------------------------------------------


def test_skip_current_item_short_circuits_drawer_emit(adapter, palace_ctx, canonical_db):
    """When core calls palace.skip_current_item() after a metadata item, the
    adapter MUST stop emitting drawers for that item and move on."""
    ref = SourceRef(local_path=canonical_db)
    gen = adapter.ingest(source=ref, palace=palace_ctx)
    drawers_seen = 0
    for result in gen:
        if isinstance(result, SourceItemMetadata):
            palace_ctx.skip_current_item()
        elif isinstance(result, DrawerRecord):
            drawers_seen += 1
    # Every session was skipped after metadata, so zero drawers should emerge.
    assert drawers_seen == 0


def test_is_current_uses_version_when_present(adapter):
    item = SourceItemMetadata(source_file="opencode:///x#session=ses_a", version="123")
    assert adapter.is_current(item=item, existing_metadata=None) is False
    assert (
        adapter.is_current(item=item, existing_metadata={"opencode_session_version": "123"}) is True
    )
    assert (
        adapter.is_current(item=item, existing_metadata={"opencode_session_version": "999"})
        is False
    )


def test_is_current_falls_back_to_presence_when_version_missing(adapter):
    """Older drawers may not carry opencode_session_version; presence implies current."""
    item = SourceItemMetadata(source_file="opencode:///x#session=ses_a", version="123")
    # any other-metadata-present scenario should return True (we assume we
    # already mined this session) — this is safer than always re-extracting.
    assert (
        adapter.is_current(item=item, existing_metadata={"session_id": "ses_a", "wing": "x"})
        is True
    )


# ---------------------------------------------------------------------------
# Tool/file part skipping
# ---------------------------------------------------------------------------


def test_tool_input_and_tool_output_parts_are_skipped(adapter, palace_ctx, canonical_db):
    """ses_aaa111 has tool-input + tool-output parts; their content must not
    appear in any drawer."""
    results = list(adapter.ingest(source=SourceRef(local_path=canonical_db), palace=palace_ctx))
    drawers = [
        r
        for r in results
        if isinstance(r, DrawerRecord) and r.source_file.endswith("#session=ses_aaa111")
    ]
    joined = "\n".join(d.content for d in drawers)
    # Sentinels we wrote into tool-input/tool-output parts in the fixture
    assert "src/api.ts" not in joined
    assert "file edited" not in joined


def test_tool_echo_lines_are_stripped():
    """A user turn echoing a tool invocation should be dropped before the
    transcript is chunked."""
    raw = "user\t" + json.dumps(
        {"type": "text", "text": "Called the read tool with the following input"}
    )
    raw += "\nuser\t" + json.dumps({"type": "text", "text": "Actually, what's the answer?"})
    out = src_transforms.opencode_extract_text_parts(raw)
    out = src_transforms.opencode_skip_tool_echo(out)
    assert "Called the read tool" not in out
    assert "Actually, what's the answer?" in out


def test_file_injection_lines_are_stripped():
    raw = "user\t" + json.dumps({"type": "text", "text": "<path>foo.py</path><type>file</type>"})
    raw += "\nuser\t" + json.dumps({"type": "text", "text": "Real question follows."})
    out = src_transforms.opencode_extract_text_parts(raw)
    out = src_transforms.opencode_skip_file_injection(out)
    assert "<path>foo.py</path>" not in out
    assert "Real question follows." in out


# ---------------------------------------------------------------------------
# Build-source-bytes / declared-transformation round-trip
# ---------------------------------------------------------------------------


def test_build_source_bytes_returns_role_tab_part_lines(canonical_db):
    conn = sqlite3.connect(canonical_db)
    try:
        raw = _build_source_bytes_per_session(conn, "ses_aaa111")
    finally:
        conn.close()
    lines = raw.split("\n")
    # Every line is "<role>\t<json>"
    for line in lines:
        role, sep, body = line.partition("\t")
        assert sep == "\t"
        assert role in {"user", "assistant", ""}
        obj = json.loads(body)
        assert "type" in obj


def test_declared_transformation_round_trip_reproduces_drawer_content(
    adapter, palace_ctx, canonical_db
):
    """RFC 002 §7.3: applying the declared transformations to canonical source
    bytes, in the adapter's declared order, MUST reproduce the chunk content
    (modulo chunk_exchanges' chunking step which is applied on top)."""
    results = list(adapter.ingest(source=SourceRef(local_path=canonical_db), palace=palace_ctx))
    drawers = [r for r in results if isinstance(r, DrawerRecord)]
    # Group drawers by source_file, then verify the transcript reproduces from
    # canonical source bytes via the declared pipeline.
    by_src: dict[str, list[DrawerRecord]] = {}
    for d in drawers:
        by_src.setdefault(d.source_file, []).append(d)

    conn = sqlite3.connect(canonical_db)
    try:
        for src_file, ds in by_src.items():
            # Extract session_id from source_file shape opencode://<path>#session=<sid>
            sid = src_file.split("#session=", 1)[1]
            raw = _build_source_bytes_per_session(conn, sid)
            transformed = raw
            for name in adapter.DECLARED_TRANSFORMATION_ORDER:
                fn = getattr(src_transforms, name)
                transformed = fn(transformed)
            # `transformed` is the pre-chunking transcript; chunk_exchanges then
            # produces the same per-drawer content list. We import locally so
            # the test mirrors what the adapter does.
            from mempalace.convo_miner import chunk_exchanges

            expected_chunks = chunk_exchanges(transformed)
            assert len(expected_chunks) == len(ds), (
                f"chunk count mismatch for {src_file}: "
                f"{len(expected_chunks)} expected, {len(ds)} produced"
            )
            ds_sorted = sorted(ds, key=lambda d: d.chunk_index)
            for c, d in zip(expected_chunks, ds_sorted):
                assert c["content"] == d.content
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Empty / edge cases
# ---------------------------------------------------------------------------


def test_empty_db_yields_nothing(adapter, palace_ctx, tmp_path):
    """A schema-valid but empty OpenCode DB ingests cleanly with zero records."""
    path = tmp_path / "empty.db"
    build_fixture.build_fixture(str(path), sessions=[])
    results = list(adapter.ingest(source=SourceRef(local_path=str(path)), palace=palace_ctx))
    assert results == []


def test_session_with_only_user_turn_is_skipped(adapter, palace_ctx, tmp_path):
    """A single-turn session (just one user message) cannot form an exchange
    pair, so the adapter skips it and emits no drawer (only the metadata)."""
    only_user = build_fixture.SyntheticSession(
        session_id="ses_only_user",
        project_id="p",
        directory="/tmp/p",
        title="Only user",
        messages=[
            build_fixture.SyntheticMessage(
                role="user", parts=[build_fixture.SyntheticPart(text="hi")]
            )
        ],
    )
    path = tmp_path / "only_user.db"
    build_fixture.build_fixture(str(path), sessions=[only_user])
    results = list(adapter.ingest(source=SourceRef(local_path=str(path)), palace=palace_ctx))
    metas = [r for r in results if isinstance(r, SourceItemMetadata)]
    drawers = [r for r in results if isinstance(r, DrawerRecord)]
    assert len(metas) == 1
    assert drawers == []


def test_unicode_content_preserved_end_to_end(adapter, palace_ctx, tmp_path):
    """Unicode (BMP and non-BMP) in user/assistant text MUST survive to
    drawer.content unchanged."""
    # Use larger Unicode bodies so chunk_exchanges' MIN_CHUNK_SIZE (30 chars)
    # doesn't elide the only exchange in the session.
    sess = build_fixture.SyntheticSession(
        session_id="ses_uni",
        project_id="p",
        directory="/tmp/p",
        title="Unicode session",
        messages=[
            build_fixture.SyntheticMessage(
                role="user",
                parts=[
                    build_fixture.SyntheticPart(
                        text=(
                            "日本語テスト 🎯 кириллица — how do I handle UTF-8 in "
                            "the database column when storing user names with emoji?"
                        )
                    )
                ],
            ),
            build_fixture.SyntheticMessage(
                role="assistant",
                parts=[
                    build_fixture.SyntheticPart(
                        text=(
                            "हिन्दी और عربى — emoji 🚀 ok. Use utf8mb4 in MySQL, "
                            "TEXT in Postgres (already 4-byte UTF-8), and make sure "
                            "your connection charset is utf8mb4 too."
                        )
                    )
                ],
            ),
        ],
    )
    path = tmp_path / "uni.db"
    build_fixture.build_fixture(str(path), sessions=[sess])
    results = list(adapter.ingest(source=SourceRef(local_path=str(path)), palace=palace_ctx))
    drawers = [r for r in results if isinstance(r, DrawerRecord)]
    assert drawers
    combined = "\n".join(d.content for d in drawers)
    for needle in ("日本語テスト", "🎯", "кириллица", "हिन्दी", "عربى", "🚀"):
        assert needle in combined, f"unicode {needle!r} did not survive transcript"


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------


def test_registry_can_resolve_opencode_when_registered_explicitly():
    """Even without entry-point discovery (pip install -e .) the registry
    SHOULD admit explicit registration."""
    from mempalace.sources.registry import (
        available_adapters,
        get_adapter,
        register,
        unregister,
    )

    register("opencode", OpenCodeSourceAdapter)
    try:
        assert "opencode" in available_adapters()
        inst = get_adapter("opencode")
        assert isinstance(inst, OpenCodeSourceAdapter)
    finally:
        unregister("opencode")


def test_capability_byte_preserving_is_NOT_advertised():
    """Sanity check: the OpenCode adapter is declared-lossy (transforms
    declared but non-empty), so it MUST NOT advertise byte_preserving."""
    assert "byte_preserving" not in OpenCodeSourceAdapter.capabilities
    assert len(OpenCodeSourceAdapter.declared_transformations) > 0
