"""Tests for the OpenClaw source adapter (RFC 002).

Covers:
    * Adapter class identity (capabilities, modes, declared transformations).
    * RFC 002 conformance — declared-transformation round-trip, schema
      conformance, stable ``source_file`` shape.
    * Unit tests for JSONL extraction, runtime-context strip, metadata-preamble
      strip, exchange formatting, and chunked-exchange emit via
      ``convo_miner.chunk_exchanges``.
    * Second-pass hardening (improvement 1-4):
      - Schema-marker validation (valid, wrong traceSchema, unknown schemaVersion).
      - Discovery across OPENCLAW_TRAJECTORY_DIR, pointer files, and default.
      - Truncation tolerance (partial final line, truncation-marker event).
      - TrajectoryEventSource / JsonlTrajectoryEventSource abstraction seam.
    * Edge cases: empty file, file with no exchange pairs, single-turn
      sessions, unicode content, explicit wing override.

No real recorded ``*.trajectory.jsonl`` files are used. All trajectory
content is built synthetically by :func:`build_trajectory_fixture` so
nothing from a live session is committed to the repository.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List

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
from mempalace.sources.openclaw import (
    JsonlTrajectoryEventSource,
    OpenClawSourceAdapter,
    TrajectoryEventSource,
    _build_canonical_source_bytes,
    _read_pointer_file,
    _scan_trajectory_file,
    session_source_file,
)


# ===========================================================================
# Synthetic fixture builder
# ===========================================================================


def _make_event(
    event_type: str,
    session_id: str,
    seq: int,
    *,
    session_key: str = "agent:main:slack:direct:UTEST",
    workspace_dir: str = "/home/user/workspace",
    model_id: str = "anthropic/claude-test-model",
    ts_base: str = "2026-01-01T10:00:00.000Z",
    data: dict | None = None,
) -> dict:
    """Build one synthetic trajectory event object."""
    return {
        "traceSchema": "openclaw-trajectory",
        "schemaVersion": 1,
        "traceId": session_id,
        "source": "runtime",
        "type": event_type,
        "ts": ts_base,
        "seq": seq,
        "sourceSeq": seq + 100,
        "sessionId": session_id,
        "sessionKey": session_key,
        "runId": f"run-{seq:04d}",
        "workspaceDir": workspace_dir,
        "provider": "anthropic",
        "modelId": model_id,
        "modelApi": "messages",
        "data": data or {},
    }


def _make_exchange(
    session_id: str,
    seq_start: int,
    user_text: str,
    assistant_texts: List[str],
    **event_kwargs,
) -> List[dict]:
    """Build a synthetic prompt.submitted + model.completed pair."""
    events = []
    events.append(
        _make_event(
            "context.compiled",
            session_id,
            seq_start,
            **event_kwargs,
            data={},
        )
    )
    events.append(
        _make_event(
            "prompt.submitted",
            session_id,
            seq_start + 1,
            **event_kwargs,
            data={"prompt": user_text, "systemPrompt": "", "messages": [], "imagesCount": 0},
        )
    )
    events.append(
        _make_event(
            "model.completed",
            session_id,
            seq_start + 2,
            **event_kwargs,
            data={
                "assistantTexts": assistant_texts,
                "aborted": False,
                "usage": {"input_tokens": 10, "output_tokens": 20},
            },
        )
    )
    events.append(
        _make_event(
            "trace.artifacts",
            session_id,
            seq_start + 3,
            **event_kwargs,
            data={},
        )
    )
    events.append(
        _make_event(
            "session.ended",
            session_id,
            seq_start + 4,
            **event_kwargs,
            data={},
        )
    )
    return events


def build_trajectory_fixture(
    path: Path,
    session_id: str,
    exchanges: List[tuple],
    *,
    session_key: str = "agent:main:slack:direct:UTEST",
    workspace_dir: str = "/home/user/workspace",
    model_id: str = "anthropic/claude-test-model",
    ts_base: str = "2026-01-01T10:00:00.000Z",
) -> str:
    """Write a synthetic trajectory JSONL file.

    Args:
        path: Destination file path.
        session_id: UUID-style session identifier.
        exchanges: List of ``(user_text, [assistant_text, ...])`` tuples.
        session_key: OpenClaw session routing key.
        workspace_dir: Workspace directory for all events.
        model_id: Model identifier for all events.
        ts_base: ISO-8601 timestamp for all events.

    Returns:
        The path as a string (for chaining with ``SourceRef``).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    all_events: List[dict] = []

    # Leading session.started event
    all_events.append(
        _make_event(
            "session.started",
            session_id,
            seq=1,
            session_key=session_key,
            workspace_dir=workspace_dir,
            model_id=model_id,
            ts_base=ts_base,
            data={},
        )
    )

    seq = 2
    kwargs = dict(
        session_key=session_key,
        workspace_dir=workspace_dir,
        model_id=model_id,
        ts_base=ts_base,
    )
    for user_text, assistant_texts in exchanges:
        all_events.extend(_make_exchange(session_id, seq, user_text, assistant_texts, **kwargs))
        seq += 10  # leave room between exchange groups

    with path.open("w", encoding="utf-8") as fh:
        for event in all_events:
            fh.write(json.dumps(event) + "\n")

    return str(path)


# ---------------------------------------------------------------------------
# Canonical fixture sessions
# ---------------------------------------------------------------------------

_SESSION_SIMPLE = "sess-aa11bb22-0000-0000-0000-000000000001"
_SESSION_METADATA = "sess-aa11bb22-0000-0000-0000-000000000002"
_SESSION_UNICODE = "sess-aa11bb22-0000-0000-0000-000000000003"
_SESSION_SINGLE_TURN = "sess-aa11bb22-0000-0000-0000-000000000004"


def _make_metadata_prompt(user_request: str) -> str:
    """Wrap a user request with the OpenClaw metadata preamble."""
    return (
        "Conversation info (untrusted metadata):\n"
        "```json\n"
        "{\n"
        '  "chat_id": "user:UTEST123",\n'
        '  "message_id": "1234567890.123456",\n'
        '  "sender_id": "UTEST123",\n'
        '  "sender": "Test User"\n'
        "}\n"
        "```\n"
        "\n" + user_request
    )


def _make_runtime_context_prompt(user_request: str) -> str:
    """Wrap a user request with an OpenClaw runtime context block."""
    return (
        "<<<OpenClaw runtime context\n"
        "Some internal plumbing text that should be stripped.\n"
        "END_OPENCLAW_INTERNAL_CONTEXT>>>\n"
        "\n" + user_request
    )


# ===========================================================================
# pytest fixtures
# ===========================================================================


@pytest.fixture
def adapter() -> OpenClawSourceAdapter:
    return OpenClawSourceAdapter()


@pytest.fixture
def sessions_dir(tmp_path) -> Path:
    """Return a temp sessions directory with canonical synthetic files."""
    sdir = tmp_path / "sessions"
    sdir.mkdir()

    # Session 1: simple two-exchange session in workspace "myproject"
    build_trajectory_fixture(
        sdir / f"{_SESSION_SIMPLE}.trajectory.jsonl",
        _SESSION_SIMPLE,
        [
            (
                "How do I use TanStack Query for data fetching?",
                [
                    "Use the useQuery hook with a queryFn. For example: useQuery({ queryKey: ['todos'], queryFn: fetchTodos })"
                ],
            ),
            (
                "How do I invalidate the cache after a mutation?",
                [
                    "Call queryClient.invalidateQueries({ queryKey: ['todos'] }) inside the onSuccess callback of useMutation."
                ],
            ),
        ],
        workspace_dir="/home/user/projects/myproject",
    )

    # Session 2: session with metadata preamble that should be stripped
    build_trajectory_fixture(
        sdir / f"{_SESSION_METADATA}.trajectory.jsonl",
        _SESSION_METADATA,
        [
            (
                _make_metadata_prompt("Can you help me debug this Python error?"),
                ["Sure! Share the traceback and I'll help you debug it."],
            ),
            (
                "Here is the traceback: AttributeError: NoneType has no attribute 'split'",
                [
                    "That error means you're calling .split() on a None value. Check where the variable is assigned."
                ],
            ),
        ],
        workspace_dir="/home/user/projects/backend",
    )

    # Session 3: unicode content
    build_trajectory_fixture(
        sdir / f"{_SESSION_UNICODE}.trajectory.jsonl",
        _SESSION_UNICODE,
        [
            (
                "日本語で説明してください: how does async/await work in Python?",
                [
                    "async/await は非同期処理のための構文です。asyncキーワードをコルーチン関数の前に付けます。"
                ],
            ),
        ],
        workspace_dir="/home/user/projects/docs",
    )

    # Session 4: only one user turn, no assistant reply → should produce no drawers
    build_trajectory_fixture(
        sdir / f"{_SESSION_SINGLE_TURN}.trajectory.jsonl",
        _SESSION_SINGLE_TURN,
        exchanges=[],  # no exchanges at all — empty transcript
        workspace_dir="/home/user/projects/empty",
    )

    return sdir


@pytest.fixture
def palace_ctx() -> PalaceContext:
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

    return PalaceContext(
        drawer_collection=_FakeCollection(),
        knowledge_graph=_FakeKG(),
        palace_path="/tmp/palace",
        adapter_name="openclaw",
        adapter_version="0.1.0",
    )


# ===========================================================================
# Class identity
# ===========================================================================


def test_adapter_identity():
    assert OpenClawSourceAdapter.name == "openclaw"
    assert OpenClawSourceAdapter.spec_version == "1.0"
    assert OpenClawSourceAdapter.adapter_version == "0.1.0"
    assert "chunked_content" in OpenClawSourceAdapter.supported_modes
    assert "supports_incremental" in OpenClawSourceAdapter.capabilities
    assert "adapter_owns_routing" in OpenClawSourceAdapter.capabilities
    assert OpenClawSourceAdapter.default_privacy_class == "pii_potential"


def test_declared_transformations_have_reference_impls():
    """Every declared transformation MUST resolve to a callable on transforms (RFC 002 section 7.3)."""
    for name in OpenClawSourceAdapter.declared_transformations:
        impl = getattr(src_transforms, name, None)
        assert callable(impl), (
            f"declared transformation {name!r} has no callable reference impl "
            f"on mempalace.sources.transforms"
        )


def test_declared_transformation_order_matches_set():
    """DECLARED_TRANSFORMATION_ORDER must be a permutation of declared_transformations."""
    assert set(OpenClawSourceAdapter.DECLARED_TRANSFORMATION_ORDER) == (
        OpenClawSourceAdapter.declared_transformations
    )


def test_describe_schema_returns_adapter_schema(adapter):
    schema = adapter.describe_schema()
    assert isinstance(schema, AdapterSchema)
    assert schema.version == "1.0"
    required = {k for k, v in schema.fields.items() if v.required}
    assert {"session_id", "session_created_at", "message_count", "extract_mode"}.issubset(required)
    assert isinstance(schema.fields["session_id"], FieldSpec)
    assert schema.fields["session_id"].indexed is True


# ===========================================================================
# source_file identity
# ===========================================================================


def test_session_source_file_is_stable():
    s1 = session_source_file("/tmp/foo.trajectory.jsonl", "sess-abc123")
    s2 = session_source_file("/tmp/foo.trajectory.jsonl", "sess-abc123")
    s3 = session_source_file("/tmp/foo.trajectory.jsonl", "sess-def456")
    assert s1 == s2
    assert s1 != s3
    assert s1 == "openclaw:///tmp/foo.trajectory.jsonl#session=sess-abc123"


def test_session_source_file_has_expected_shape():
    sf = session_source_file(
        "/home/user/.openclaw/agents/main/sessions/abc.trajectory.jsonl", "abc"
    )
    assert sf.startswith("openclaw://")
    assert "#session=" in sf


# ===========================================================================
# Discovery / errors
# ===========================================================================


def test_ingest_raises_source_not_found_for_missing_path(adapter, palace_ctx, tmp_path):
    missing = tmp_path / "nosuchdir"
    ref = SourceRef(local_path=str(missing))
    with pytest.raises(SourceNotFoundError):
        list(adapter.ingest(source=ref, palace=palace_ctx))


def test_ingest_accepts_empty_directory(adapter, palace_ctx, tmp_path):
    empty_dir = tmp_path / "sessions"
    empty_dir.mkdir()
    results = list(adapter.ingest(source=SourceRef(local_path=str(empty_dir)), palace=palace_ctx))
    assert results == []


def test_close_then_ingest_raises_adapter_closed(adapter, palace_ctx, sessions_dir):
    adapter.close()
    ref = SourceRef(local_path=str(sessions_dir))
    with pytest.raises(AdapterClosedError):
        list(adapter.ingest(source=ref, palace=palace_ctx))


# ===========================================================================
# source_summary
# ===========================================================================


def test_source_summary_counts_trajectory_files(adapter, sessions_dir):
    summary = adapter.source_summary(source=SourceRef(local_path=str(sessions_dir)))
    # 4 synthetic files in sessions_dir
    assert summary.item_count == 4
    assert "OpenClaw sessions at" in summary.description


def test_source_summary_for_missing_dir(adapter, tmp_path):
    summary = adapter.source_summary(source=SourceRef(local_path=str(tmp_path / "nodir")))
    assert summary.item_count == 0
    assert "not found" in summary.description.lower()


# ===========================================================================
# Scan / metadata extraction
# ===========================================================================


def test_scan_trajectory_file_extracts_metadata(sessions_dir):
    tfile = sessions_dir / f"{_SESSION_SIMPLE}.trajectory.jsonl"
    meta = _scan_trajectory_file(tfile)
    assert meta is not None
    assert meta["session_id"] == _SESSION_SIMPLE
    assert meta["session_key"] == "agent:main:slack:direct:UTEST"
    assert meta["workspace_dir"] == "/home/user/projects/myproject"
    assert meta["model_id"] == "anthropic/claude-test-model"
    assert meta["session_created_at"]  # non-empty ISO timestamp
    assert meta["version"]  # non-empty version string
    # Defect 1 fix: message_count must be an integer, not absent or None.
    assert isinstance(meta["message_count"], int)
    assert meta["message_count"] == 2  # _SESSION_SIMPLE has exactly 2 exchanges


def test_scan_trajectory_file_message_count_empty_session(sessions_dir):
    """A session with zero exchange pairs must report message_count == 0."""
    tfile = sessions_dir / f"{_SESSION_SINGLE_TURN}.trajectory.jsonl"
    meta = _scan_trajectory_file(tfile)
    assert meta is not None
    assert isinstance(meta["message_count"], int)
    assert meta["message_count"] == 0


def test_scan_trajectory_file_returns_none_for_empty_file(tmp_path):
    empty = tmp_path / "empty.trajectory.jsonl"
    empty.write_text("")
    assert _scan_trajectory_file(empty) is None


def test_scan_trajectory_file_version_is_size_based(sessions_dir):
    """Version must be the file byte-size (append-only monotonic — M2 fix)."""
    tfile = sessions_dir / f"{_SESSION_SIMPLE}.trajectory.jsonl"
    meta = _scan_trajectory_file(tfile)
    assert meta is not None
    assert meta["version"] == str(tfile.stat().st_size)


def test_version_is_stable_when_file_unchanged(sessions_dir):
    """Re-scanning the same unchanged file must return the same version."""
    tfile = sessions_dir / f"{_SESSION_SIMPLE}.trajectory.jsonl"
    meta1 = _scan_trajectory_file(tfile)
    meta2 = _scan_trajectory_file(tfile)
    assert meta1 is not None
    assert meta2 is not None
    assert meta1["version"] == meta2["version"]


def test_version_grows_on_append(tmp_path):
    """Appending bytes to a file must increase its version (M2: size-based)."""
    tfile = tmp_path / "sess-grow.trajectory.jsonl"
    build_trajectory_fixture(
        tfile,
        "sess-grow",
        [("Initial question that forms a complete chunk.", ["Initial answer."])],
        workspace_dir="/tmp/test",
    )
    meta1 = _scan_trajectory_file(tfile)
    assert meta1 is not None

    # Append a benign non-event line (version check only cares about byte count).
    extra = {
        "type": "trace.ping",
        "seq": 99,
        "sessionId": "sess-grow",
        "traceSchema": "openclaw-trajectory",
        "schemaVersion": 1,
        "ts": "2026-01-02T00:00:00.000Z",
        "data": {},
    }
    with tfile.open("a") as fh:
        fh.write(json.dumps(extra) + "\n")

    meta2 = _scan_trajectory_file(tfile)
    assert meta2 is not None
    assert int(meta2["version"]) > int(meta1["version"]), (
        "appending to a file must increase the size-based version"
    )


def test_per_run_seq_no_longer_used_as_version(tmp_path):
    """Confirm version is NOT the last event's seq (which would break M2)."""
    tfile = tmp_path / "sess-seq-check.trajectory.jsonl"
    # Write a tiny file where last seq is 7 (mirrors the real-data finding).
    events = [
        {
            "traceSchema": "openclaw-trajectory",
            "schemaVersion": 1,
            "type": "session.started",
            "seq": 1,
            "sessionId": "sess-seq-check",
            "sessionKey": "k",
            "workspaceDir": "/tmp",
            "modelId": "m",
            "ts": "2026-01-01T00:00:00.000Z",
            "data": {},
        },
        {
            "traceSchema": "openclaw-trajectory",
            "schemaVersion": 1,
            "type": "model.completed",
            "seq": 7,  # seq resets per-run
            "sessionId": "sess-seq-check",
            "sessionKey": "k",
            "workspaceDir": "/tmp",
            "modelId": "m",
            "ts": "2026-01-01T00:00:01.000Z",
            "data": {"assistantTexts": ["answer"]},
        },
    ]
    with tfile.open("w") as fh:
        for e in events:
            fh.write(json.dumps(e) + "\n")
    meta = _scan_trajectory_file(tfile)
    assert meta is not None
    assert meta["version"] != "7", "version must not be last_event.seq (per-run, not monotonic)"
    assert meta["version"] == str(tfile.stat().st_size)


# ===========================================================================
# Ingest shape
# ===========================================================================


def test_ingest_yields_metadata_then_drawers(adapter, palace_ctx, sessions_dir):
    results = list(
        adapter.ingest(source=SourceRef(local_path=str(sessions_dir)), palace=palace_ctx)
    )
    metas = [r for r in results if isinstance(r, SourceItemMetadata)]
    drawers = [r for r in results if isinstance(r, DrawerRecord)]
    # 4 files → 4 SourceItemMetadata
    assert len(metas) == 4
    # At least the 2-exchange sessions should produce drawers
    assert len(drawers) >= 1
    # All source_files carry the openclaw:// prefix
    for m in metas:
        assert m.source_file.startswith("openclaw://")
        assert "#session=" in m.source_file


def test_ingest_empty_session_produces_no_drawers(adapter, palace_ctx, sessions_dir):
    """The single-turn (0-exchange) session should yield metadata but no drawers."""
    results = list(
        adapter.ingest(source=SourceRef(local_path=str(sessions_dir)), palace=palace_ctx)
    )
    drawers = [r for r in results if isinstance(r, DrawerRecord)]
    src_files_with_drawers = {d.source_file for d in drawers}
    assert all(_SESSION_SINGLE_TURN not in sf for sf in src_files_with_drawers), (
        "session with no exchanges must produce no drawers"
    )


def test_drawer_metadata_has_all_universal_and_schema_fields(adapter, palace_ctx, sessions_dir):
    results = list(
        adapter.ingest(source=SourceRef(local_path=str(sessions_dir)), palace=palace_ctx)
    )
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
        assert universal_keys.issubset(meta.keys()), (
            f"missing universal keys: {universal_keys - meta.keys()}"
        )
        assert schema_keys.issubset(meta.keys()), (
            f"missing schema keys: {schema_keys - meta.keys()}"
        )
        # Flat-scalar invariant — chroma constraint.
        for k, v in meta.items():
            assert isinstance(v, (str, int, float, bool)), (
                f"metadata[{k}]={v!r} (type {type(v).__name__}) is not flat-scalar"
            )


def test_drawer_extract_mode_is_exchange(adapter, palace_ctx, sessions_dir):
    results = list(
        adapter.ingest(source=SourceRef(local_path=str(sessions_dir)), palace=palace_ctx)
    )
    for r in results:
        if isinstance(r, DrawerRecord):
            assert r.metadata["extract_mode"] == "exchange"


def test_drawer_route_hint_carries_wing_and_room(adapter, palace_ctx, sessions_dir):
    results = list(
        adapter.ingest(source=SourceRef(local_path=str(sessions_dir)), palace=palace_ctx)
    )
    drawers = [r for r in results if isinstance(r, DrawerRecord)]
    for d in drawers:
        assert d.route_hint is not None
        assert d.route_hint.wing, "wing must not be empty"
        assert d.route_hint.room, "room must not be empty"


# ===========================================================================
# Wing routing
# ===========================================================================


def test_wing_derived_from_workspace_dir_basename(adapter, palace_ctx, sessions_dir):
    results = list(
        adapter.ingest(source=SourceRef(local_path=str(sessions_dir)), palace=palace_ctx)
    )
    drawers = [r for r in results if isinstance(r, DrawerRecord)]
    # _SESSION_SIMPLE has workspace_dir="/home/user/projects/myproject" → wing="myproject"
    myproject_drawers = [d for d in drawers if _SESSION_SIMPLE in d.source_file]
    assert myproject_drawers, "expected drawers for simple session"
    assert all(d.metadata["wing"] == "myproject" for d in myproject_drawers)


def test_explicit_wing_option_overrides_workspace_dir(adapter, palace_ctx, sessions_dir):
    ref = SourceRef(local_path=str(sessions_dir), options={"wing": "Custom Wing"})
    results = list(adapter.ingest(source=ref, palace=palace_ctx))
    drawers = [r for r in results if isinstance(r, DrawerRecord)]
    assert drawers, "expected drawers with custom wing"
    wings = {d.metadata["wing"] for d in drawers}
    assert wings == {"custom_wing"}, f"unexpected wings: {wings}"


# ===========================================================================
# Skip / incremental behavior
# ===========================================================================


def test_skip_current_item_short_circuits_drawer_emit(adapter, palace_ctx, sessions_dir):
    """After skip_current_item(), no drawers should emerge for that item."""
    ref = SourceRef(local_path=str(sessions_dir))
    gen = adapter.ingest(source=ref, palace=palace_ctx)
    drawers_seen = 0
    for result in gen:
        if isinstance(result, SourceItemMetadata):
            palace_ctx.skip_current_item()
        elif isinstance(result, DrawerRecord):
            drawers_seen += 1
    assert drawers_seen == 0, f"expected 0 drawers but got {drawers_seen}"


def test_is_current_returns_false_when_no_existing_metadata(adapter):
    item = SourceItemMetadata(
        source_file="openclaw:///tmp/x.trajectory.jsonl#session=sess-abc",
        version="42",
    )
    assert adapter.is_current(item=item, existing_metadata=None) is False
    assert adapter.is_current(item=item, existing_metadata={}) is False


def test_is_current_uses_stored_version_when_present(adapter):
    item = SourceItemMetadata(
        source_file="openclaw:///tmp/x.trajectory.jsonl#session=sess-abc",
        version="42",
    )
    assert (
        adapter.is_current(item=item, existing_metadata={"openclaw_session_version": "42"}) is True
    )
    assert (
        adapter.is_current(item=item, existing_metadata={"openclaw_session_version": "99"}) is False
    )


def test_is_current_falls_back_to_presence_when_version_missing(adapter):
    """Drawers without openclaw_session_version → assume current (safer)."""
    item = SourceItemMetadata(
        source_file="openclaw:///tmp/x.trajectory.jsonl#session=sess-abc",
        version="42",
    )
    assert (
        adapter.is_current(item=item, existing_metadata={"session_id": "sess-abc", "wing": "x"})
        is True
    )


# ===========================================================================
# M1: No-double-parse — runtime path identical to conformance path
# ===========================================================================


def test_extract_turns_from_events_matches_openclaw_extract_turns(sessions_dir):
    """_extract_turns_from_events must produce identical output to openclaw_extract_turns."""
    from mempalace.sources.openclaw import (
        JsonlTrajectoryEventSource,
        _build_canonical_source_bytes_from_events,
        _extract_turns_from_events,
    )

    tfile = sessions_dir / f"{_SESSION_SIMPLE}.trajectory.jsonl"
    src = JsonlTrajectoryEventSource(tfile)
    events = list(src.iter_events())

    # Runtime path (single parse).
    runtime_turns = _extract_turns_from_events(events)

    # Conformance path (re-serialise events, then run declared extract transform).
    canon_bytes = _build_canonical_source_bytes_from_events(events)
    conformance_turns = src_transforms.openclaw_extract_turns(canon_bytes)

    assert runtime_turns == conformance_turns, (
        "_extract_turns_from_events must produce identical output to "
        "openclaw_extract_turns applied to serialised events"
    )


def test_extract_turns_and_metadata_matches_split_helpers(sessions_dir):
    """S5: the fused single-pass runtime path must be byte-identical to the two
    original helpers it replaces (so it can't silently drift from the RFC
    conformance reference impls)."""
    from mempalace.sources.openclaw import (
        JsonlTrajectoryEventSource,
        _aggregate_session_metadata,
        _extract_turns_and_metadata,
        _extract_turns_from_events,
    )

    tfile = sessions_dir / f"{_SESSION_SIMPLE}.trajectory.jsonl"
    events = list(JsonlTrajectoryEventSource(tfile).iter_events())

    turns_split = _extract_turns_from_events(events)
    meta_split = _aggregate_session_metadata(tfile.name, iter(events), fallback_stem=tfile.stem)

    # Fused pass consumes a fresh generator (never materializes the list).
    turns_fused, meta_fused = _extract_turns_and_metadata(
        JsonlTrajectoryEventSource(tfile).iter_events(),
        fallback_stem=tfile.stem,
    )

    assert turns_fused == turns_split
    assert meta_fused == meta_split


def test_extract_turns_and_metadata_empty_returns_none():
    """S5: no events → (None, None), matching the split helpers' empty contract."""
    from mempalace.sources.openclaw import _extract_turns_and_metadata

    turns, meta = _extract_turns_and_metadata(iter([]), fallback_stem="stem")
    assert turns is None and meta is None


def test_build_canonical_source_bytes_from_events_round_trips(sessions_dir):
    """Serialise → extract should reproduce the same turns as direct extract."""
    from mempalace.sources.openclaw import (
        JsonlTrajectoryEventSource,
        _build_canonical_source_bytes_from_events,
    )

    tfile = sessions_dir / f"{_SESSION_METADATA}.trajectory.jsonl"
    src = JsonlTrajectoryEventSource(tfile)
    events = list(src.iter_events())

    canon = _build_canonical_source_bytes_from_events(events)
    # Every line must be valid JSON.
    for i, line in enumerate(canon.split("\n")):
        if line.strip():
            parsed = json.loads(line)
            assert isinstance(parsed, dict), f"line {i} not a dict after round-trip"


# ===========================================================================
# Transform unit tests
# ===========================================================================


def test_openclaw_extract_turns_basic():
    """prompt.submitted and model.completed events become user/assistant lines."""
    events = [
        {"type": "session.started", "data": {}},
        {"type": "prompt.submitted", "data": {"prompt": "Hello, how are you?"}},
        {"type": "model.completed", "data": {"assistantTexts": ["I'm fine, thanks!"]}},
    ]
    raw = "\n".join(json.dumps(e) for e in events)
    result = src_transforms.openclaw_extract_turns(raw)
    lines = result.split("\n")
    assert len(lines) == 2
    role0, _, body0 = lines[0].partition("\t")
    role1, _, body1 = lines[1].partition("\t")
    assert role0 == "user"
    assert role1 == "assistant"
    assert json.loads(body0) == "Hello, how are you?"
    assert json.loads(body1) == "I'm fine, thanks!"


def test_openclaw_extract_turns_skips_non_exchange_events():
    """context.compiled, trace.*, session.ended etc. must not appear in output."""
    events = [
        {"type": "context.compiled", "data": {}},
        {"type": "session.started", "data": {}},
        {"type": "trace.metadata", "data": {}},
        {"type": "prompt.submitted", "data": {"prompt": "What is 2+2?"}},
        {"type": "trace.artifacts", "data": {}},
        {"type": "model.completed", "data": {"assistantTexts": ["4"]}},
        {"type": "session.ended", "data": {}},
    ]
    raw = "\n".join(json.dumps(e) for e in events)
    result = src_transforms.openclaw_extract_turns(raw)
    # Only user + assistant lines
    assert result.count("\n") == 1
    assert result.startswith("user\t")
    assert "assistant\t" in result


def test_openclaw_extract_turns_multiple_assistant_texts():
    """Multiple assistantTexts entries are joined with newline."""
    events = [
        {"type": "prompt.submitted", "data": {"prompt": "Give me two answers."}},
        {
            "type": "model.completed",
            "data": {"assistantTexts": ["First answer.", "Second answer."]},
        },
    ]
    raw = "\n".join(json.dumps(e) for e in events)
    result = src_transforms.openclaw_extract_turns(raw)
    lines = result.split("\n")
    assistant_body = json.loads(lines[1].partition("\t")[2])
    assert "First answer." in assistant_body
    assert "Second answer." in assistant_body


def test_openclaw_strip_runtime_context_removes_block():
    """Runtime context block must be removed from user turns."""
    raw_prompt = "<<<OpenClaw runtime context\nSome internal info.\nEND_OPENCLAW_INTERNAL_CONTEXT>>>\n\nActual question"
    role_tab = f"user\t{json.dumps(raw_prompt)}"
    result = src_transforms.openclaw_strip_runtime_context(role_tab)
    body = json.loads(result.partition("\t")[2])
    assert "OpenClaw runtime context" not in body
    assert "END_OPENCLAW_INTERNAL_CONTEXT" not in body
    assert "Actual question" in body


def test_openclaw_strip_runtime_context_preserves_assistant_turns():
    """Assistant turns must not be modified by the strip transform."""
    raw = f"assistant\t{json.dumps('This is the answer.')}"
    result = src_transforms.openclaw_strip_runtime_context(raw)
    assert result == raw


def test_openclaw_strip_metadata_preamble_removes_fence():
    """Metadata JSON fences must be stripped from user prompts."""
    preamble_prompt = (
        "Conversation info (untrusted metadata):\n"
        "```json\n"
        '{"chat_id": "user:U123", "sender_id": "U123"}\n'
        "```\n"
        "\nReal user request here"
    )
    role_tab = f"user\t{json.dumps(preamble_prompt)}"
    result = src_transforms.openclaw_strip_metadata_preamble(role_tab)
    body = json.loads(result.partition("\t")[2])
    assert "chat_id" not in body
    assert "Real user request here" in body


def test_openclaw_strip_metadata_preamble_removes_label_lines():
    prompt_with_label = "Conversation info (untrusted metadata):\nReal content below"
    role_tab = f"user\t{json.dumps(prompt_with_label)}"
    result = src_transforms.openclaw_strip_metadata_preamble(role_tab)
    body = json.loads(result.partition("\t")[2])
    assert "Conversation info (untrusted metadata)" not in body
    assert "Real content below" in body


def test_openclaw_format_exchange_produces_quote_blocks():
    """User turns become '> text'; assistant turns become plain paragraphs."""
    role_tab = f"user\t{json.dumps('What is Python?')}\nassistant\t{json.dumps('Python is a programming language.')}"
    result = src_transforms.openclaw_format_exchange(role_tab)
    assert result.startswith("> What is Python?")
    assert "Python is a programming language." in result


def test_openclaw_format_exchange_handles_multiline_user():
    """Multi-line user text is quoted line-by-line."""
    multiline = "Line one\nLine two"
    role_tab = f"user\t{json.dumps(multiline)}\nassistant\t{json.dumps('Answer.')}"
    result = src_transforms.openclaw_format_exchange(role_tab)
    assert "> Line one" in result
    assert "> Line two" in result


# ===========================================================================
# Declared-transformation round-trip (RFC 002 section 7.3)
# ===========================================================================


def test_declared_transformation_round_trip_reproduces_drawer_content(
    adapter, palace_ctx, sessions_dir
):
    """Applying declared transforms in order to canonical source bytes then
    chunking MUST reproduce the drawer content (RFC 002 section 7.3 conformance).

    This test verifies both:
    (a) The conformance path: raw bytes → full DECLARED_TRANSFORMATION_ORDER → chunks.
    (b) Implicitly, the runtime path (_extract_turns_from_events +
        _apply_post_extract_pipeline) produces identical results since both
        paths converge on the same transcript text.
    """
    results = list(
        adapter.ingest(source=SourceRef(local_path=str(sessions_dir)), palace=palace_ctx)
    )
    drawers = [r for r in results if isinstance(r, DrawerRecord)]
    if not drawers:
        pytest.skip("no drawers produced — nothing to verify")

    by_src: dict[str, list[DrawerRecord]] = {}
    for d in drawers:
        by_src.setdefault(d.source_file, []).append(d)

    from mempalace.convo_miner import chunk_exchanges

    for src_file, ds in by_src.items():
        # Resolve the trajectory file path from source_file URI
        # shape: openclaw://<absolute-path>#session=<sid>
        file_path = src_file.split("openclaw://", 1)[1].split("#session=", 1)[0]
        tfile = Path(file_path)
        assert tfile.exists(), f"trajectory file not found: {tfile}"

        raw = _build_canonical_source_bytes(tfile)
        transformed = raw
        for name in adapter.DECLARED_TRANSFORMATION_ORDER:
            fn = getattr(src_transforms, name)
            transformed = fn(transformed)

        expected_chunks = chunk_exchanges(transformed)
        ds_sorted = sorted(ds, key=lambda d: d.chunk_index)
        assert len(expected_chunks) == len(ds_sorted), (
            f"chunk count mismatch for {src_file}: "
            f"{len(expected_chunks)} expected, {len(ds_sorted)} produced"
        )
        for c, d in zip(expected_chunks, ds_sorted):
            assert c["content"] == d.content, (
                f"chunk content mismatch at chunk_index={d.chunk_index} for {src_file}"
            )


# ===========================================================================
# Realistic Slack + media input — strips all envelope cruft
# ===========================================================================

_SESSION_SLACK_REALISTIC = "sess-aa11bb22-0000-0000-0000-000000000099"


def _make_slack_user_prompt(core_message: str) -> str:
    """Build a realistic Slack-delivered prompt as the runtime injects it.

    Includes every pattern the Defect 2 fix must strip:
    * Two OpenClaw metadata fences (Conversation info + Sender).
    * A ``[media attached: ...]`` annotation line.
    * A Slack routing header prefix ``[Slack <name> ...] <name>:``.
    * A ``[Slack file: ...]`` file provenance line.
    * A ``[slack message id: ...]`` message provenance line.
    * A ``<file ...>...</file>`` wrapper block containing
      ``<<<EXTERNAL_UNTRUSTED_CONTENT>>>`` scaffolding.
    """
    return (
        "Conversation info (untrusted metadata):\n"
        "```json\n"
        "{\n"
        '  "chat_id": "user:UREALISTIC",\n'
        '  "message_id": "1783366572.599909",\n'
        '  "sender_id": "UREALISTIC",\n'
        '  "sender": "Grace",\n'
        '  "inbound_event_kind": "user_request"\n'
        "}\n"
        "```\n"
        "\n"
        "Sender (untrusted metadata):\n"
        "```json\n"
        "{\n"
        '  "label": "Grace (UREALISTIC)",\n'
        '  "id": "UREALISTIC",\n'
        '  "name": "Grace"\n'
        "}\n"
        "```\n"
        "\n"
        "[media attached: media://inbound/abc-def-123 (application/force-download)]\n"
        "[Slack Grace +1m Mon 2026-07-06 19:36:12 UTC] Grace: " + core_message + "\n"
        "[Slack file: notes.txt (fileId: F0BGBN9QG9E)]\n"
        "[slack message id: 1783366572.599909 channel: D0AM196D2P6]\n"
        "\n"
        '<file name="abc-def-123" mime="text/plain">\n'
        "\n"
        '<<<EXTERNAL_UNTRUSTED_CONTENT id="deadbeef0001">\n'
        "Source: External\n"
        "---\n"
        "This is injected file content that must be stripped entirely.\n"
        '<<<END_EXTERNAL_UNTRUSTED_CONTENT id="deadbeef0001">\n'
        "</file>\n"
    )


def test_realistic_slack_media_cruft_stripped(adapter, palace_ctx, tmp_path):
    """Realistic Slack-delivered input: all envelope annotations must be stripped.

    Asserts (per task spec):
    (a) All envelope cruft is absent from drawer content.
    (b) message_count is an int and equals the number of exchange pairs.
    (c) All describe_schema fields appear in every emitted DrawerRecord.metadata.
    """
    sdir = tmp_path / "sessions"
    sdir.mkdir()

    core_msg_1 = (
        "Can you summarize the attached Slack transcript? "
        "It is long enough to form a complete chunk."
    )
    core_msg_2 = "Thanks! Now list the three main decisions."

    build_trajectory_fixture(
        sdir / f"{_SESSION_SLACK_REALISTIC}.trajectory.jsonl",
        _SESSION_SLACK_REALISTIC,
        [
            (
                _make_slack_user_prompt(core_msg_1),
                [
                    "Sure — the Slack transcript covers three topics: "
                    "memory routing, the RFC 002 adapter spike, and palace cleanup. "
                    "I'll summarize each in turn."
                ],
            ),
            (
                _make_slack_user_prompt(core_msg_2),
                [
                    "The three main decisions were: "
                    "(1) use declared-transformation round-trip tests, "
                    "(2) adopt the wing/room/hall routing hierarchy, "
                    "(3) keep ChromaDB as the vector backend for v4."
                ],
            ),
        ],
        workspace_dir="/home/user/projects/slack-test",
    )

    results = list(adapter.ingest(source=SourceRef(local_path=str(sdir)), palace=palace_ctx))
    drawers = [r for r in results if isinstance(r, DrawerRecord)]
    assert drawers, "expected at least one drawer from the realistic session"

    joined = "\n".join(d.content for d in drawers)

    # (a) All Slack/media envelope patterns must be gone from drawer content.
    assert "media attached:" not in joined, "[media attached:] survived transform"
    assert "[Slack Grace" not in joined, "Slack header prefix survived transform"
    assert "[Slack file:" not in joined, "[Slack file:] survived transform"
    assert "slack message id:" not in joined.lower(), "[slack message id:] survived"
    assert "<file name=" not in joined, "<file> wrapper survived transform"
    assert "EXTERNAL_UNTRUSTED_CONTENT" not in joined, "file content survived transform"
    assert "chat_id" not in joined, "metadata fence content survived transform"
    assert "sender_id" not in joined, "sender fence content survived transform"

    # Actual user content must survive.
    assert "summarize" in joined or "decisions" in joined, (
        "core user message was stripped alongside the cruft"
    )

    # (b) message_count must be a populated int equal to the pair count.
    meta_values = [d.metadata.get("message_count") for d in drawers]
    assert all(isinstance(v, int) for v in meta_values), (
        f"message_count is not int in some drawers: {meta_values!r}"
    )
    # All chunks of the same session share the same message_count.
    assert all(v == 2 for v in meta_values), (
        f"expected message_count == 2 (2 exchanges), got: {set(meta_values)}"
    )

    # (c) All describe_schema fields must appear in each DrawerRecord.metadata.
    schema_keys = set(adapter.describe_schema().fields.keys())
    for drawer in drawers:
        missing = schema_keys - set(drawer.metadata.keys())
        assert not missing, (
            f"drawer metadata missing schema fields: {missing}; source_file={drawer.source_file!r}"
        )


# ===========================================================================
# Edge cases
# ===========================================================================


def test_ingest_single_file_via_local_path(adapter, palace_ctx, sessions_dir):
    """Passing a path directly to a single .trajectory.jsonl file works."""
    tfile = sessions_dir / f"{_SESSION_SIMPLE}.trajectory.jsonl"
    results = list(adapter.ingest(source=SourceRef(local_path=str(tfile)), palace=palace_ctx))
    metas = [r for r in results if isinstance(r, SourceItemMetadata)]
    drawers = [r for r in results if isinstance(r, DrawerRecord)]
    assert len(metas) == 1
    assert len(drawers) >= 1


def test_unicode_content_preserved_end_to_end(adapter, palace_ctx, sessions_dir):
    """BMP + emoji Unicode must survive the full transform pipeline to drawer.content."""
    results = list(
        adapter.ingest(source=SourceRef(local_path=str(sessions_dir)), palace=palace_ctx)
    )
    drawers = [
        d for d in results if isinstance(d, DrawerRecord) and _SESSION_UNICODE in d.source_file
    ]
    assert drawers, "expected at least one drawer for the unicode session"
    joined = "\n".join(d.content for d in drawers)
    # The Japanese text in the user prompt must appear somewhere
    assert "日本語" in joined


def test_runtime_context_stripped_from_drawers(adapter, palace_ctx, tmp_path):
    """Runtime context blocks must not appear in drawer content."""
    sdir = tmp_path / "sessions"
    sdir.mkdir()
    build_trajectory_fixture(
        sdir / "rtx.trajectory.jsonl",
        "sess-rtx-0001",
        [
            (
                _make_runtime_context_prompt(
                    "How do I center a div in CSS? This question is long enough to pass chunking."
                ),
                [
                    "Use flexbox: display: flex; justify-content: center; align-items: center; on the parent container."
                ],
            ),
        ],
        workspace_dir="/home/user/projects/web",
    )
    results = list(adapter.ingest(source=SourceRef(local_path=str(sdir)), palace=palace_ctx))
    drawers = [r for r in results if isinstance(r, DrawerRecord)]
    joined = "\n".join(d.content for d in drawers)
    assert "END_OPENCLAW_INTERNAL_CONTEXT" not in joined
    assert "Some internal plumbing text" not in joined
    assert "center a div" in joined


def test_metadata_preamble_stripped_from_drawers(adapter, palace_ctx, sessions_dir):
    """Metadata JSON fences must not appear in drawer content."""
    results = list(
        adapter.ingest(source=SourceRef(local_path=str(sessions_dir)), palace=palace_ctx)
    )
    drawers = [
        d for d in results if isinstance(d, DrawerRecord) and _SESSION_METADATA in d.source_file
    ]
    assert drawers, "expected drawers for metadata-preamble session"
    joined = "\n".join(d.content for d in drawers)
    assert "chat_id" not in joined
    assert "sender_id" not in joined
    # But the actual user content must still be present
    assert "Python error" in joined or "debug" in joined


def test_malformed_jsonl_lines_are_skipped(adapter, palace_ctx, tmp_path):
    """Non-JSON lines in a trajectory file must not cause crashes."""
    sdir = tmp_path / "sessions"
    sdir.mkdir()
    tfile = sdir / "malformed.trajectory.jsonl"
    events = [
        {
            "type": "session.started",
            "data": {},
            "sessionId": "sess-mal",
            "sessionKey": "k",
            "workspaceDir": "/tmp",
            "modelId": "m",
            "ts": "2026-01-01T00:00:00.000Z",
            "seq": 1,
        },
        "THIS IS NOT JSON",
        {
            "type": "prompt.submitted",
            "data": {"prompt": "Valid question that is long enough to pass chunking threshold"},
            "sessionId": "sess-mal",
            "sessionKey": "k",
            "workspaceDir": "/tmp",
            "modelId": "m",
            "ts": "2026-01-01T00:00:01.000Z",
            "seq": 2,
        },
        "ANOTHER GARBAGE LINE",
        {
            "type": "model.completed",
            "data": {
                "assistantTexts": ["Valid answer that is also long enough to make a proper chunk"]
            },
            "sessionId": "sess-mal",
            "sessionKey": "k",
            "workspaceDir": "/tmp",
            "modelId": "m",
            "ts": "2026-01-01T00:00:02.000Z",
            "seq": 3,
        },
    ]
    with tfile.open("w") as fh:
        for e in events:
            fh.write((json.dumps(e) if isinstance(e, dict) else e) + "\n")

    results = list(adapter.ingest(source=SourceRef(local_path=str(sdir)), palace=palace_ctx))
    drawers = [r for r in results if isinstance(r, DrawerRecord)]
    assert len(drawers) >= 1, "malformed lines should be skipped, not crash"


# ===========================================================================
# Second-pass hardening: 1 — Schema-marker validation
# ===========================================================================

# ---------------------------------------------------------------------------
# Helpers for schema-marker tests
# ---------------------------------------------------------------------------


def _write_events(path: Path, events: list) -> None:
    """Write a list of event dicts (or raw strings) as JSONL."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for e in events:
            fh.write((json.dumps(e) if isinstance(e, dict) else str(e)) + "\n")


def _make_minimal_event(
    event_type: str,
    seq: int,
    *,
    trace_schema: str = "openclaw-trajectory",
    schema_version: int = 1,
    session_id: str = "sess-schema-test",
) -> dict:
    """Build a minimal event with explicit schema marker fields."""
    return {
        "traceSchema": trace_schema,
        "schemaVersion": schema_version,
        "type": event_type,
        "ts": "2026-01-01T00:00:00.000Z",
        "seq": seq,
        "sessionId": session_id,
        "sessionKey": "agent:main:slack:direct:UTEST",
        "workspaceDir": "/home/user/projects/schematest",
        "modelId": "anthropic/claude-test",
        "data": {},
    }


def _make_exchange_events(
    session_id: str,
    seq_start: int,
    user_text: str,
    assistant_text: str,
    *,
    trace_schema: str = "openclaw-trajectory",
    schema_version: int = 1,
) -> list:
    """Build prompt.submitted + model.completed events with explicit schema fields."""
    kwargs = dict(
        trace_schema=trace_schema,
        schema_version=schema_version,
        session_id=session_id,
    )
    return [
        _make_minimal_event("session.started", seq_start, **kwargs),
        {
            **_make_minimal_event("prompt.submitted", seq_start + 1, **kwargs),
            "data": {"prompt": user_text},
        },
        {
            **_make_minimal_event("model.completed", seq_start + 2, **kwargs),
            "data": {"assistantTexts": [assistant_text]},
        },
    ]


# ---------------------------------------------------------------------------
# Test: valid traceSchema parses normally
# ---------------------------------------------------------------------------


def test_valid_trace_schema_parses_normally(adapter, palace_ctx, tmp_path):
    """A file with the correct traceSchema and schemaVersion 1 ingests cleanly."""
    sdir = tmp_path / "sessions"
    sdir.mkdir()
    tfile = sdir / "sess-valid-schema.trajectory.jsonl"
    events = _make_exchange_events(
        "sess-valid-schema",
        1,
        "What is the capital of France? This question is long enough to chunk.",
        "The capital of France is Paris, a major European city with rich culture.",
    )
    _write_events(tfile, events)

    results = list(adapter.ingest(source=SourceRef(local_path=str(sdir)), palace=palace_ctx))
    drawers = [r for r in results if isinstance(r, DrawerRecord)]
    metas = [r for r in results if isinstance(r, SourceItemMetadata)]
    assert len(metas) == 1, "expected one SourceItemMetadata for valid schema"
    assert drawers, "valid traceSchema should produce drawers"


# ---------------------------------------------------------------------------
# Test: wrong traceSchema causes file to be skipped
# ---------------------------------------------------------------------------


def test_wrong_trace_schema_causes_file_to_be_skipped(adapter, palace_ctx, tmp_path):
    """A file with traceSchema != 'openclaw-trajectory' must be silently skipped."""
    sdir = tmp_path / "sessions"
    sdir.mkdir()
    tfile = sdir / "sess-wrong-schema.trajectory.jsonl"
    events = _make_exchange_events(
        "sess-wrong-schema",
        1,
        "Ignored user text because the schema is wrong.",
        "Ignored assistant text.",
        trace_schema="some-other-format",
    )
    _write_events(tfile, events)

    results = list(adapter.ingest(source=SourceRef(local_path=str(sdir)), palace=palace_ctx))
    # Wrong schema → _scan_trajectory_file returns None → file skipped entirely
    metas = [r for r in results if isinstance(r, SourceItemMetadata)]
    drawers = [r for r in results if isinstance(r, DrawerRecord)]
    assert len(metas) == 0, (
        "wrong traceSchema must suppress SourceItemMetadata; adapter should skip the file entirely"
    )
    assert len(drawers) == 0, "wrong traceSchema must suppress all drawers"


def test_wrong_trace_schema_logs_warning(tmp_path, caplog):
    """Wrong traceSchema must emit a WARNING log."""
    import logging

    tfile = tmp_path / "sess-warn.trajectory.jsonl"
    events = _make_exchange_events(
        "sess-warn", 1, "user text", "assistant text", trace_schema="bad-schema"
    )
    _write_events(tfile, events)

    src = JsonlTrajectoryEventSource(tfile)
    with caplog.at_level(logging.WARNING, logger="mempalace.sources.openclaw"):
        result = src.session_metadata()

    assert result is None, "wrong traceSchema should return None metadata"
    assert any("traceSchema" in rec.message for rec in caplog.records), (
        "expected a WARNING mentioning traceSchema"
    )


def test_missing_trace_schema_is_tolerated(adapter, palace_ctx, tmp_path):
    """Events without any traceSchema marker (legacy files) must be parsed normally."""
    sdir = tmp_path / "sessions"
    sdir.mkdir()
    tfile = sdir / "sess-no-schema.trajectory.jsonl"

    # Build events without schema marker fields at all.
    events = [
        {
            "type": "session.started",
            "ts": "2026-01-01T00:00:00.000Z",
            "seq": 1,
            "sessionId": "sess-no-schema",
            "sessionKey": "k",
            "workspaceDir": "/home/user/projects/noschema",
            "modelId": "m",
            "data": {},
        },
        {
            "type": "prompt.submitted",
            "ts": "2026-01-01T00:00:01.000Z",
            "seq": 2,
            "sessionId": "sess-no-schema",
            "sessionKey": "k",
            "workspaceDir": "/home/user/projects/noschema",
            "modelId": "m",
            "data": {
                "prompt": (
                    "How do I use context managers in Python? "
                    "This is long enough to form a complete chunk in the test."
                )
            },
        },
        {
            "type": "model.completed",
            "ts": "2026-01-01T00:00:02.000Z",
            "seq": 3,
            "sessionId": "sess-no-schema",
            "sessionKey": "k",
            "workspaceDir": "/home/user/projects/noschema",
            "modelId": "m",
            "data": {
                "assistantTexts": [
                    "Use the 'with' statement. It calls __enter__ and __exit__ "
                    "automatically, ensuring cleanup even if an exception occurs."
                ]
            },
        },
    ]
    _write_events(tfile, events)

    results = list(adapter.ingest(source=SourceRef(local_path=str(sdir)), palace=palace_ctx))
    drawers = [r for r in results if isinstance(r, DrawerRecord)]
    assert drawers, "events without schema marker (legacy) must parse normally"


# ---------------------------------------------------------------------------
# Test: unknown schemaVersion warns but still parses
# ---------------------------------------------------------------------------


def test_unknown_schema_version_warns_and_still_parses(adapter, palace_ctx, tmp_path, caplog):
    """An unknown schemaVersion must emit a WARNING but still produce drawers."""
    import logging

    sdir = tmp_path / "sessions"
    sdir.mkdir()
    tfile = sdir / "sess-future-schema.trajectory.jsonl"
    events = _make_exchange_events(
        "sess-future-schema",
        1,
        (
            "How does the new v99 trajectory format work? "
            "This question is long enough to produce a chunk."
        ),
        (
            "The v99 format is hypothetical. Best-effort parse should still "
            "extract user and assistant turns from known fields."
        ),
        schema_version=99,  # future/unknown version
    )
    _write_events(tfile, events)

    with caplog.at_level(logging.WARNING, logger="mempalace.sources.openclaw"):
        results = list(adapter.ingest(source=SourceRef(local_path=str(sdir)), palace=palace_ctx))

    drawers = [r for r in results if isinstance(r, DrawerRecord)]
    assert drawers, "unknown schemaVersion should still produce drawers (best-effort parse)"
    assert any("schemaVersion" in rec.message for rec in caplog.records), (
        "expected a WARNING mentioning schemaVersion"
    )


def test_jsonl_source_scan_unknown_schema_version_not_none(tmp_path):
    """JsonlTrajectoryEventSource must return non-None metadata for future schemaVersion."""
    tfile = tmp_path / "future.trajectory.jsonl"
    events = _make_exchange_events(
        "sess-future",
        1,
        "user text for future schema",
        "assistant text for future schema",
        schema_version=42,
    )
    _write_events(tfile, events)

    src = JsonlTrajectoryEventSource(tfile)
    meta = src.session_metadata()
    assert meta is not None, "best-effort parse of future schemaVersion must return metadata"
    assert meta["message_count"] == 1


# ===========================================================================
# Second-pass hardening: 2 — Discovery across all documented capture locations
# ===========================================================================


def test_discovery_finds_files_in_openclaw_trajectory_dir(
    adapter, palace_ctx, tmp_path, monkeypatch
):
    """OPENCLAW_TRAJECTORY_DIR env var must be scanned for trajectory files."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    traj_dir = tmp_path / "trajectories"
    traj_dir.mkdir()

    session_id = "sess-traj-dir-0001"
    build_trajectory_fixture(
        traj_dir / f"{session_id}.trajectory.jsonl",
        session_id,
        [
            (
                "Question in the trajectory dir — long enough to chunk properly.",
                ["Answer from the trajectory dir — also long enough for chunking."],
            )
        ],
        workspace_dir="/home/user/projects/trajdir",
    )

    monkeypatch.setenv("OPENCLAW_TRAJECTORY_DIR", str(traj_dir))

    results = list(
        adapter.ingest(source=SourceRef(local_path=str(sessions_dir)), palace=palace_ctx)
    )
    metas = [r for r in results if isinstance(r, SourceItemMetadata)]
    # The file is only in traj_dir, not in sessions_dir — must still be found.
    assert len(metas) == 1, f"expected 1 file from OPENCLAW_TRAJECTORY_DIR, got {len(metas)}"
    assert any(session_id in m.source_file for m in metas)


def test_discovery_follows_pointer_files(adapter, palace_ctx, tmp_path):
    """Pointer files (*.trajectory-path.json) beside sessions must be followed."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    relocated_dir = tmp_path / "relocated"
    relocated_dir.mkdir()

    session_id = "sess-pointer-0001"
    tfile = relocated_dir / f"{session_id}.trajectory.jsonl"
    build_trajectory_fixture(
        tfile,
        session_id,
        [
            (
                "Pointer-relocated question — long enough to form a chunk.",
                ["Pointer-relocated answer — long enough to form a chunk."],
            )
        ],
        workspace_dir="/home/user/projects/pointed",
    )

    # Write pointer file beside a (non-existent) session file in sessions_dir.
    pointer_path = sessions_dir / f"{session_id}.trajectory-path.json"
    pointer_path.write_text(json.dumps({"path": str(tfile)}), encoding="utf-8")

    results = list(
        adapter.ingest(source=SourceRef(local_path=str(sessions_dir)), palace=palace_ctx)
    )
    metas = [r for r in results if isinstance(r, SourceItemMetadata)]
    assert len(metas) == 1, f"pointer file must resolve to 1 trajectory file; got {len(metas)}"
    assert any(session_id in m.source_file for m in metas)


def test_discovery_deduplicates_when_pointer_and_direct(adapter, palace_ctx, tmp_path, monkeypatch):
    """If a file is reachable via both direct sidecar AND pointer, count it once."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    traj_dir = tmp_path / "trajectories"
    traj_dir.mkdir()

    session_id = "sess-dedup-0001"
    tfile = sessions_dir / f"{session_id}.trajectory.jsonl"
    build_trajectory_fixture(
        tfile,
        session_id,
        [
            (
                "Deduplicated question — must appear exactly once.",
                ["Deduplicated answer — must appear exactly once."],
            )
        ],
        workspace_dir="/home/user/projects/dedup",
    )

    # Pointer points to the SAME file that already exists as a sidecar.
    pointer_path = sessions_dir / f"{session_id}.trajectory-path.json"
    pointer_path.write_text(json.dumps({"path": str(tfile)}), encoding="utf-8")

    # OPENCLAW_TRAJECTORY_DIR also has a symlink to the same file (extra dup).
    import shutil

    shutil.copy(str(tfile), str(traj_dir / f"{session_id}.trajectory.jsonl"))

    monkeypatch.setenv("OPENCLAW_TRAJECTORY_DIR", str(traj_dir))

    results = list(
        adapter.ingest(source=SourceRef(local_path=str(sessions_dir)), palace=palace_ctx)
    )
    metas = [r for r in results if isinstance(r, SourceItemMetadata)]
    # All three sources point to files with the same resolved content/session;
    # the traj_dir copy is a different inode so may produce 2 metas — but
    # the sidecar + pointer MUST deduplicate to 1.
    # traj_dir copy has same session_id embedded in source_file but different
    # file path, so they are distinct source_files. What we verify is that the
    # sessions_dir sidecar + pointer do NOT duplicate.
    assert len(metas) <= 2, (
        "sidecar + pointer referencing the same file must deduplicate; "
        f"got {len(metas)} SourceItemMetadata records"
    )


def test_discovery_invalid_openclaw_trajectory_dir_logs_warning(tmp_path, caplog, monkeypatch):
    """OPENCLAW_TRAJECTORY_DIR pointing at a non-dir emits a WARNING, no crash."""
    import logging

    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    nonexistent = tmp_path / "nosuchdir"

    monkeypatch.setenv("OPENCLAW_TRAJECTORY_DIR", str(nonexistent))

    from mempalace.sources.openclaw import _discover_from_dir

    with caplog.at_level(logging.WARNING, logger="mempalace.sources.openclaw"):
        result = _discover_from_dir(sessions_dir)

    assert isinstance(result, list), "must return a list even on bad env var"
    assert any("OPENCLAW_TRAJECTORY_DIR" in rec.message for rec in caplog.records), (
        "expected a WARNING about OPENCLAW_TRAJECTORY_DIR"
    )


def test_read_pointer_file_returns_path_from_valid_json(tmp_path):
    """_read_pointer_file must return the target Path for a valid JSON pointer."""
    pointer = tmp_path / "sess.trajectory-path.json"
    target_path = "/abs/path/to/sess.trajectory.jsonl"
    pointer.write_text(json.dumps({"path": target_path}), encoding="utf-8")

    result = _read_pointer_file(pointer)
    assert result is not None
    # Compare as Path objects, not strings: str(Path("/abs/path")) renders with
    # backslashes on Windows, so a hard-coded POSIX string comparison is not
    # portable. Path equality normalizes per-platform.
    assert result == Path(target_path)


def test_read_pointer_file_returns_none_for_corrupt_json(tmp_path):
    """_read_pointer_file must return None for corrupt / unreadable pointer files."""
    pointer = tmp_path / "sess.trajectory-path.json"
    pointer.write_text("THIS IS NOT JSON", encoding="utf-8")

    result = _read_pointer_file(pointer)
    assert result is None


def test_read_pointer_file_returns_none_for_missing_key(tmp_path):
    """_read_pointer_file must return None if no recognised path key is present."""
    pointer = tmp_path / "sess.trajectory-path.json"
    pointer.write_text(json.dumps({"unknown_key": "/some/path"}), encoding="utf-8")

    result = _read_pointer_file(pointer)
    assert result is None


# ===========================================================================
# Second-pass hardening: 3 — Truncation tolerance
# ===========================================================================


def test_partial_final_line_does_not_crash(adapter, palace_ctx, tmp_path):
    """A file ending with a partial (un-terminated) JSON line must not crash."""
    sdir = tmp_path / "sessions"
    sdir.mkdir()
    tfile = sdir / "sess-partial.trajectory.jsonl"

    # Write a valid session with two events, then append a partial line (no newline).
    events = _make_exchange_events(
        "sess-partial",
        1,
        "Question before truncation — long enough to form a proper chunk.",
        "Answer before truncation — long enough to form a proper chunk here.",
    )
    lines = [json.dumps(e) + "\n" for e in events]
    lines.append('{"type": "model.completed", "seq": 99, "data": {"assistantTe')  # partial!

    with tfile.open("w", encoding="utf-8") as fh:
        fh.writelines(lines)

    # Must not raise; should produce the drawer from the complete exchange.
    results = list(adapter.ingest(source=SourceRef(local_path=str(sdir)), palace=palace_ctx))
    metas = [r for r in results if isinstance(r, SourceItemMetadata)]
    drawers = [r for r in results if isinstance(r, DrawerRecord)]
    assert len(metas) == 1, "partial final line must not suppress SourceItemMetadata"
    assert len(drawers) >= 1, "complete exchange before partial line must produce a drawer"


def test_partial_final_line_scan_does_not_crash(tmp_path):
    """_scan_trajectory_file must not crash on a partial final line."""
    tfile = tmp_path / "sess-partial-scan.trajectory.jsonl"
    events = _make_exchange_events(
        "sess-partial-scan",
        1,
        "Question before truncation.",
        "Answer before truncation.",
    )
    lines = [json.dumps(e) + "\n" for e in events]
    lines.append('{"type": "broken_json": ')  # partial line, also invalid JSON

    with tfile.open("w", encoding="utf-8") as fh:
        fh.writelines(lines)

    # Must not raise; should return valid metadata for the events that parsed.
    meta = _scan_trajectory_file(tfile)
    assert meta is not None, "partial final line must not prevent metadata extraction"
    assert isinstance(meta["message_count"], int)
    assert meta["message_count"] == 1


def test_truncation_marker_event_is_skipped_and_data_preserved(adapter, palace_ctx, tmp_path):
    """A truncation-marker event must be skipped; data before it must survive."""
    sdir = tmp_path / "sessions"
    sdir.mkdir()
    tfile = sdir / "sess-truncated.trajectory.jsonl"

    # Build a normal exchange followed by the truncation marker.
    events = _make_exchange_events(
        "sess-truncated",
        1,
        "Question captured before the 10 MiB cap fired — long enough to chunk.",
        "Answer captured before the 10 MiB cap fired — long enough to chunk.",
    )
    truncation_marker = {
        "type": "trace.truncated",
        "ts": "2026-01-01T00:00:10.000Z",
        "seq": 20,
        "sessionId": "sess-truncated",
        "traceSchema": "openclaw-trajectory",
        "schemaVersion": 1,
        "data": {"reason": "size_cap", "bytesWritten": 10485760},
    }
    _write_events(tfile, events + [truncation_marker])

    results = list(adapter.ingest(source=SourceRef(local_path=str(sdir)), palace=palace_ctx))
    drawers = [r for r in results if isinstance(r, DrawerRecord)]
    metas = [r for r in results if isinstance(r, SourceItemMetadata)]

    # The truncation marker must not become a drawer.
    assert len(metas) == 1, "expected one SourceItemMetadata"
    assert len(drawers) >= 1, "data before truncation marker must still produce drawers"
    # The truncation marker event must not appear in any drawer content.
    joined = "\n".join(d.content for d in drawers)
    assert "size_cap" not in joined, "truncation marker data must not appear in drawer content"
    assert "trace.truncated" not in joined


def test_truncation_marker_not_counted_in_message_count(tmp_path):
    """Truncation-marker events must not be counted in message_count."""
    tfile = tmp_path / "sess-tc-count.trajectory.jsonl"
    events = _make_exchange_events(
        "sess-tc-count",
        1,
        "Real exchange before truncation.",
        "Real answer before truncation.",
    )
    truncation = {
        "type": "trace.truncated",
        "seq": 99,
        "sessionId": "sess-tc-count",
        "traceSchema": "openclaw-trajectory",
        "schemaVersion": 1,
        "ts": "2026-01-01T00:00:10.000Z",
        "data": {},
    }
    _write_events(tfile, events + [truncation])

    src = JsonlTrajectoryEventSource(tfile)
    meta = src.session_metadata()
    assert meta is not None
    assert meta["message_count"] == 1, "truncation marker must not increment message_count"


def test_file_all_corrupt_returns_none_scan(tmp_path):
    """A file containing only unparseable lines must return None from scan."""
    tfile = tmp_path / "corrupt.trajectory.jsonl"
    tfile.write_text("NOT JSON\nALSO NOT JSON\n{broken\n", encoding="utf-8")
    assert _scan_trajectory_file(tfile) is None


def test_jsonl_source_iter_events_empty_lines_skipped(tmp_path):
    """Blank lines in a JSONL file must be silently skipped."""
    tfile = tmp_path / "blanks.trajectory.jsonl"
    events = _make_exchange_events("sess-blanks", 1, "user text", "assistant text")
    # Interleave blank lines.
    lines = []
    for e in events:
        lines.append(json.dumps(e))
        lines.append("")  # blank
        lines.append("   ")  # whitespace-only
    tfile.write_text("\n".join(lines), encoding="utf-8")

    src = JsonlTrajectoryEventSource(tfile)
    collected = list(src.iter_events())
    assert len(collected) == len(events), "blank lines must be skipped without altering event count"


# ===========================================================================
# Second-pass hardening: 4 — Reader abstraction seam
# ===========================================================================


def test_jsonl_source_is_trajectory_event_source(tmp_path):
    """JsonlTrajectoryEventSource must satisfy the TrajectoryEventSource Protocol."""
    tfile = tmp_path / "proto-check.trajectory.jsonl"
    tfile.write_text("")
    src = JsonlTrajectoryEventSource(tfile)
    assert isinstance(src, TrajectoryEventSource), (
        "JsonlTrajectoryEventSource must pass isinstance check against "
        "TrajectoryEventSource Protocol"
    )


def test_trajectory_event_source_protocol_has_required_methods():
    """TrajectoryEventSource must declare iter_events and session_metadata."""
    # Protocol attributes are accessible via its __protocol_attrs__ in Python 3.12+
    # or via dir() for all versions.
    attrs = set(dir(TrajectoryEventSource))
    assert "iter_events" in attrs
    assert "session_metadata" in attrs


def test_jsonl_source_iter_events_yields_dicts(sessions_dir):
    """iter_events must yield dicts with expected event fields."""
    tfile = sessions_dir / f"{_SESSION_SIMPLE}.trajectory.jsonl"
    src = JsonlTrajectoryEventSource(tfile)
    events = list(src.iter_events())
    assert events, "expected at least one event from a non-empty fixture"
    for event in events:
        assert isinstance(event, dict), f"iter_events must yield dicts; got {type(event)}"
    # At least one prompt.submitted and one model.completed.
    types = {e.get("type") for e in events}
    assert "prompt.submitted" in types
    assert "model.completed" in types


def test_jsonl_source_session_metadata_is_consistent_with_scan(sessions_dir):
    """JsonlTrajectoryEventSource.session_metadata must match _scan_trajectory_file.

    _scan_trajectory_file adds 'version' (size-based) on top of the base metadata;
    all other keys must agree.
    """
    for sid in (_SESSION_SIMPLE, _SESSION_METADATA, _SESSION_UNICODE):
        tfile = sessions_dir / f"{sid}.trajectory.jsonl"
        src = JsonlTrajectoryEventSource(tfile)
        meta_src = src.session_metadata()
        meta_scan = _scan_trajectory_file(tfile)
        assert meta_src is not None
        assert meta_scan is not None
        # scan adds 'version'; strip it before comparing.
        scan_without_version = {k: v for k, v in meta_scan.items() if k != "version"}
        assert meta_src == scan_without_version, (
            f"session_metadata() and _scan_trajectory_file() (minus 'version') must agree for {sid}"
        )
        # version must be the file size.
        assert meta_scan["version"] == str(tfile.stat().st_size)


def test_jsonl_source_metadata_cache_is_stable(sessions_dir):
    """Calling session_metadata() twice must return the same dict (caching)."""
    tfile = sessions_dir / f"{_SESSION_SIMPLE}.trajectory.jsonl"
    src = JsonlTrajectoryEventSource(tfile)
    first = src.session_metadata()
    second = src.session_metadata()
    assert first is second, "session_metadata must return the same cached object"


def test_reader_abstraction_seam_docstring_present():
    """TrajectoryEventSource must have a docstring describing the SQLite seam."""
    doc = TrajectoryEventSource.__doc__ or ""
    assert "trajectory_runtime_events" in doc, (
        "TrajectoryEventSource docstring must describe the SQLite seam "
        "with the trajectory_runtime_events table name"
    )
    assert "SqliteTrajectoryEventSource" in doc, (
        "TrajectoryEventSource docstring must name the expected SQLite implementation"
    )


# ===========================================================================
# M3: Secret redaction — transform unit tests + end-to-end
# ===========================================================================

# Fake (non-real) secret values used exclusively in test fixtures.
# These are intentionally synthetic — they follow the prefix format but are
# not (and have never been) real credentials.
_FAKE_AWS_KEY = "AKIAFAKEKEY1234ABCDE"
_FAKE_GH_TOKEN = "ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
_FAKE_LIN_API = "lin_api_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
_FAKE_BEARER = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.AAAAAAAAAAAAAAAAAA"
_FAKE_ATATT = "ATATT3xFfGF0AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
_FAKE_CLH = "clh_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
_FAKE_PEM = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "MIIEowIBAAKCAQEA0000000000000000000000000000000000000000000000\n"
    "-----END RSA PRIVATE KEY-----"
)


def test_openclaw_redact_secrets_removes_aws_key():
    """AWS access key IDs must be redacted."""
    role_tab = f"user\t{json.dumps(f'My key is {_FAKE_AWS_KEY} thanks')}"
    result = src_transforms.openclaw_redact_secrets(role_tab)
    body = json.loads(result.partition("\t")[2])
    assert _FAKE_AWS_KEY not in body
    assert "[REDACTED:aws_key]" in body


def test_openclaw_redact_secrets_removes_github_token():
    """GitHub personal access tokens (ghp_...) must be redacted."""
    role_tab = f"assistant\t{json.dumps(f'Token: {_FAKE_GH_TOKEN}')}"
    result = src_transforms.openclaw_redact_secrets(role_tab)
    body = json.loads(result.partition("\t")[2])
    assert _FAKE_GH_TOKEN not in body
    assert "[REDACTED:api_token]" in body


def test_openclaw_redact_secrets_removes_linear_api_key():
    """Linear API keys (lin_api_...) must be redacted."""
    role_tab = f"user\t{json.dumps(f'API key: {_FAKE_LIN_API}')}"
    result = src_transforms.openclaw_redact_secrets(role_tab)
    body = json.loads(result.partition("\t")[2])
    assert _FAKE_LIN_API not in body
    assert "[REDACTED:api_token]" in body


def test_openclaw_redact_secrets_removes_bearer_token():
    """Bearer tokens in Authorization headers must be redacted."""
    role_tab = f"user\t{json.dumps(f'Authorization: Bearer {_FAKE_BEARER}')}"
    result = src_transforms.openclaw_redact_secrets(role_tab)
    body = json.loads(result.partition("\t")[2])
    assert _FAKE_BEARER not in body
    assert "Bearer [REDACTED:bearer_token]" in body


def test_openclaw_redact_secrets_removes_kv_password():
    """key=value password patterns must be redacted."""
    role_tab = f"user\t{json.dumps('password=SuperSecretPass123!')}"
    result = src_transforms.openclaw_redact_secrets(role_tab)
    body = json.loads(result.partition("\t")[2])
    assert "SuperSecretPass123!" not in body
    assert "[REDACTED:secret_value]" in body


def test_openclaw_redact_secrets_removes_pem_private_key():
    """PEM private key blocks must be redacted."""
    role_tab = f"user\t{json.dumps(f'Key: {_FAKE_PEM}')}"
    result = src_transforms.openclaw_redact_secrets(role_tab)
    body = json.loads(result.partition("\t")[2])
    assert "-----BEGIN RSA PRIVATE KEY-----" not in body
    assert "[REDACTED:pem_private_key]" in body


def test_openclaw_redact_secrets_is_noop_for_clean_content():
    """Clean content (no secrets) must pass through unchanged."""
    clean = "How do I use Python decorators effectively in production code?"
    role_tab = f"user\t{json.dumps(clean)}"
    result = src_transforms.openclaw_redact_secrets(role_tab)
    body = json.loads(result.partition("\t")[2])
    assert body == clean


def test_openclaw_redact_secrets_redacts_assistant_turns():
    """Redaction must apply to assistant turns as well as user turns."""
    role_tab = f"assistant\t{json.dumps(f'Here is your token: {_FAKE_GH_TOKEN}')}"
    result = src_transforms.openclaw_redact_secrets(role_tab)
    body = json.loads(result.partition("\t")[2])
    assert _FAKE_GH_TOKEN not in body


def test_redaction_end_to_end_drawer_content(adapter, palace_ctx, tmp_path):
    """Seeded fake secrets must NOT appear in emitted drawer content (M3).

    Uses only synthetic/fake credentials — no real tokens.
    """
    sdir = tmp_path / "sessions"
    sdir.mkdir()

    # Embed fake credentials into the user prompts.
    user_text_with_secrets = (
        f"My AWS key is {_FAKE_AWS_KEY} and my GitHub token is {_FAKE_GH_TOKEN}. "
        f"Also the Linear key is {_FAKE_LIN_API} and Atlassian is {_FAKE_ATATT}. "
        f"Please help me set up access. This text is long enough to form a chunk."
    )
    assistant_text = (
        "I can help you set up access. Please rotate those credentials first — "
        "sharing them in chat is insecure. This answer is long enough to chunk."
    )

    build_trajectory_fixture(
        sdir / "sess-redact-e2e.trajectory.jsonl",
        "sess-redact-e2e",
        [(user_text_with_secrets, [assistant_text])],
        workspace_dir="/home/user/projects/redact-test",
    )

    results = list(adapter.ingest(source=SourceRef(local_path=str(sdir)), palace=palace_ctx))
    drawers = [r for r in results if isinstance(r, DrawerRecord)]
    assert drawers, "expected at least one drawer"

    joined = "\n".join(d.content for d in drawers)

    # None of the fake secrets should appear in drawer content.
    assert _FAKE_AWS_KEY not in joined, "AWS key survived redaction"
    assert _FAKE_GH_TOKEN not in joined, "GitHub token survived redaction"
    assert _FAKE_LIN_API not in joined, "Linear API key survived redaction"
    assert _FAKE_ATATT not in joined, "Atlassian token survived redaction"

    # Legitimate content must still be present.
    assert "set up access" in joined or "rotate" in joined, (
        "legitimate content was stripped by over-aggressive redaction"
    )


def test_redaction_round_trip_still_matches_declared_transforms(adapter, palace_ctx, tmp_path):
    """Conformance: declared-transform round-trip must match runtime even with redaction.

    Seeded secrets are redacted in BOTH the conformance path and the runtime path,
    so the outputs must remain identical.
    """
    from mempalace.convo_miner import chunk_exchanges

    sdir = tmp_path / "sessions"
    sdir.mkdir()

    user_text = (
        f"Token for CI: {_FAKE_GH_TOKEN}. "
        f"Please confirm the deployment pipeline is set up. "
        f"This message is intentionally long enough to form a chunk for testing."
    )
    assistant_text = (
        "Deployment confirmed. The CI pipeline is configured. "
        "This response is also long enough to be chunked properly."
    )

    tfile = sdir / "sess-redact-roundtrip.trajectory.jsonl"
    build_trajectory_fixture(
        tfile,
        "sess-redact-roundtrip",
        [(user_text, [assistant_text])],
        workspace_dir="/home/user/projects/roundtrip",
    )

    results = list(adapter.ingest(source=SourceRef(local_path=str(sdir)), palace=palace_ctx))
    drawers = [r for r in results if isinstance(r, DrawerRecord)]
    if not drawers:
        pytest.skip("no drawers — nothing to verify")

    # Apply conformance path.
    raw = _build_canonical_source_bytes(tfile)
    transformed = raw
    for name in adapter.DECLARED_TRANSFORMATION_ORDER:
        fn = getattr(src_transforms, name)
        transformed = fn(transformed)
    expected_chunks = chunk_exchanges(transformed)

    ds_sorted = sorted(drawers, key=lambda d: d.chunk_index)
    assert len(expected_chunks) == len(ds_sorted)
    for c, d in zip(expected_chunks, ds_sorted):
        assert c["content"] == d.content, (
            f"round-trip mismatch at chunk_index={d.chunk_index} after redaction"
        )


# ===========================================================================
# S3: message_count definition — drawer count vs exchange-pair count
# ===========================================================================


def test_message_count_reflects_exchange_pairs_not_drawer_count(adapter, palace_ctx, tmp_path):
    """message_count must equal exchange pairs, NOT drawer count (S3 fix).

    The schema description now explicitly states this — verify the field value
    is the exchange pair count for a session where chunking might produce a
    different number of drawers.
    """
    sdir = tmp_path / "sessions"
    sdir.mkdir()

    # Build a 3-exchange session.
    exchanges = [
        (
            f"Question {i}: explain concept {i} in enough detail to fill a chunk properly.",
            [f"Answer {i}: here is a thorough explanation of concept {i} that fills a chunk."],
        )
        for i in range(3)
    ]
    build_trajectory_fixture(
        sdir / "sess-msgcount.trajectory.jsonl",
        "sess-msgcount",
        exchanges,
        workspace_dir="/home/user/projects/msgcount",
    )

    results = list(adapter.ingest(source=SourceRef(local_path=str(sdir)), palace=palace_ctx))
    drawers = [r for r in results if isinstance(r, DrawerRecord)]
    assert drawers

    # All drawers from one session share the same message_count.
    mc_values = {d.metadata["message_count"] for d in drawers}
    assert len(mc_values) == 1, "all drawers from one session must have the same message_count"
    mc = mc_values.pop()

    # message_count must equal the number of exchange pairs (3), not drawer count.
    assert mc == 3, f"expected message_count == 3 (exchange pairs), got {mc}"

    # Document that it may differ from drawer count (S3 contract).
    # (We don't assert equality — the spec says they CAN differ.)
    drawer_count = len(drawers)
    _ = drawer_count  # number of drawers is chunk-dependent; not required to equal mc


def test_schema_description_documents_message_count_definition(adapter):
    """describe_schema message_count description must mention 'drawer' or 'chunking'."""
    schema = adapter.describe_schema()
    desc = schema.fields["message_count"].description
    assert any(word in desc.lower() for word in ("drawer", "chunk", "emitted", "differ")), (
        "message_count schema description must clarify it counts exchange pairs, "
        f"not drawers (got: {desc!r})"
    )


# ===========================================================================
# R1: Broader token redaction — sk-proj-*, sk-svcacct-*, github_pat_, gho_, etc.
# ===========================================================================

# Fake tokens in the new formats — never real credentials.
_FAKE_SK_PROJ = "sk-proj-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
_FAKE_SK_SVCACCT = "sk-svcacct-BBBBBBBBBBBBBBBBBBBBBB"
_FAKE_GITHUB_PAT = "github_pat_CCCCCCCCCCCCCCCCCCCCCC"
_FAKE_GHO = "gho_DDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD"
_FAKE_GHU = "ghu_EEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEE"
_FAKE_GHR = "ghr_FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF"


def test_r1_sk_proj_token_is_redacted():
    """R1: sk-proj-... tokens must be redacted (dashes in tail broke old pattern)."""
    role_tab = f"user\t{json.dumps(f'Token: {_FAKE_SK_PROJ}')}"
    result = src_transforms.openclaw_redact_secrets(role_tab)
    body = json.loads(result.partition("\t")[2])
    assert _FAKE_SK_PROJ not in body, "sk-proj-... token survived redaction"
    assert "[REDACTED:api_token]" in body


def test_r1_sk_svcacct_token_is_redacted():
    """R1: sk-svcacct-... tokens must be redacted."""
    role_tab = f"user\t{json.dumps(f'Key: {_FAKE_SK_SVCACCT}')}"
    result = src_transforms.openclaw_redact_secrets(role_tab)
    body = json.loads(result.partition("\t")[2])
    assert _FAKE_SK_SVCACCT not in body, "sk-svcacct-... token survived redaction"
    assert "[REDACTED:api_token]" in body


def test_r1_github_pat_is_redacted():
    """R1: github_pat_... fine-grained PATs must be redacted."""
    role_tab = f"user\t{json.dumps(f'PAT: {_FAKE_GITHUB_PAT}')}"
    result = src_transforms.openclaw_redact_secrets(role_tab)
    body = json.loads(result.partition("\t")[2])
    assert _FAKE_GITHUB_PAT not in body, "github_pat_... token survived redaction"
    assert "[REDACTED:api_token]" in body


def test_r1_gho_token_is_redacted():
    """R1: gho_... GitHub OAuth tokens must be redacted."""
    role_tab = f"user\t{json.dumps(f'OAuth token: {_FAKE_GHO}')}"
    result = src_transforms.openclaw_redact_secrets(role_tab)
    body = json.loads(result.partition("\t")[2])
    assert _FAKE_GHO not in body, "gho_... token survived redaction"
    assert "[REDACTED:api_token]" in body


def test_r1_ghu_ghr_tokens_are_redacted():
    """R1: ghu_... and ghr_... GitHub tokens must be redacted."""
    for tok in (_FAKE_GHU, _FAKE_GHR):
        role_tab = f"user\t{json.dumps(f'Token {tok}')}"
        result = src_transforms.openclaw_redact_secrets(role_tab)
        body = json.loads(result.partition("\t")[2])
        assert tok not in body, f"{tok[:10]}... token survived redaction"
        assert "[REDACTED:api_token]" in body


def test_r1_new_tokens_redacted_end_to_end(adapter, palace_ctx, tmp_path):
    """R1: sk-proj- and github_pat_ tokens must not appear in drawer content."""
    sdir = tmp_path / "sessions"
    sdir.mkdir()

    user_text = (
        f"Here is my OpenAI key: {_FAKE_SK_PROJ}. "
        f"And my GitHub PAT: {_FAKE_GITHUB_PAT}. "
        f"Please help me configure the CI. This message is long enough to chunk."
    )
    assistant_text = (
        "Please rotate those credentials immediately — they are now compromised. "
        "This answer is also long enough to form a complete chunk for testing."
    )
    build_trajectory_fixture(
        sdir / "sess-r1-tokens.trajectory.jsonl",
        "sess-r1-tokens",
        [(user_text, [assistant_text])],
        workspace_dir="/home/user/projects/r1-test",
    )

    results = list(adapter.ingest(source=SourceRef(local_path=str(sdir)), palace=palace_ctx))
    drawers = [r for r in results if isinstance(r, DrawerRecord)]
    assert drawers, "expected at least one drawer"

    joined = "\n".join(d.content for d in drawers)
    assert _FAKE_SK_PROJ not in joined, "sk-proj- token survived end-to-end redaction"
    assert _FAKE_GITHUB_PAT not in joined, "github_pat_ token survived end-to-end redaction"
    assert "configure the CI" in joined or "rotate" in joined, (
        "legitimate content was stripped by over-aggressive redaction"
    )


def test_r1_existing_prefixes_still_redacted():
    """R1: existing ghp_/ghs_/lin_api_ patterns must still be redacted (no regression)."""
    for fake in (_FAKE_GH_TOKEN, _FAKE_LIN_API, _FAKE_CLH, _FAKE_ATATT):
        role_tab = f"user\t{json.dumps(f'Token: {fake}')}"
        result = src_transforms.openclaw_redact_secrets(role_tab)
        body = json.loads(result.partition("\t")[2])
        assert fake not in body, f"existing token {fake[:12]}... regressed after R1 changes"


# ===========================================================================
# R3: Envelope-only user turns — assistant content must not be lost
# ===========================================================================


def _build_envelope_only_trajectory(path: Path, session_id: str) -> None:
    """Write a trajectory where the first user turn is pure Slack envelope (no real text).

    After strip transforms, the user body is empty.  The assistant reply must
    still survive into a drawer via the > [non-text user turn] placeholder.
    """
    # User text that reduces to empty after metadata strip.
    envelope_only = (
        "Conversation info (untrusted metadata):\n"
        "```json\n"
        '{"chat_id": "user:UTEST", "sender_id": "UTEST", "inbound_event_kind": "ping"}\n'
        "```\n"
        "\n"
        "Sender (untrusted metadata):\n"
        "```json\n"
        '{"label": "Bot", "id": "UTEST"}\n'
        "```\n"
    )
    assistant_reply = (
        "I received your ping. The bot is online and ready for commands. "
        "This response is long enough to be stored as a drawer."
    )
    build_trajectory_fixture(
        path,
        session_id,
        [(envelope_only, [assistant_reply])],
        workspace_dir="/home/user/projects/r3-test",
    )


def test_r3_envelope_only_user_turn_produces_placeholder(adapter, palace_ctx, tmp_path):
    """R3: assistant reply must survive when the leading user turn is envelope-only.

    Before the fix, an empty user body caused openclaw_format_exchange to
    ``continue`` (skip the turn), leaving the assistant reply as a leading
    orphan that _chunk_by_exchange silently discarded.  After the fix, the
    placeholder ``> [non-text user turn]`` keeps the pair intact.
    """
    sdir = tmp_path / "sessions"
    sdir.mkdir()
    session_id = "sess-r3-envelope-0001"
    _build_envelope_only_trajectory(sdir / f"{session_id}.trajectory.jsonl", session_id)

    results = list(adapter.ingest(source=SourceRef(local_path=str(sdir)), palace=palace_ctx))
    drawers = [r for r in results if isinstance(r, DrawerRecord)]

    assert drawers, (
        "R3 regression: envelope-only user + assistant reply produced no drawers; "
        "the assistant content was silently dropped"
    )
    joined = "\n".join(d.content for d in drawers)
    assert "online and ready" in joined, (
        "R3 regression: assistant reply text missing from drawer content"
    )


def test_r3_placeholder_appears_in_format_exchange_output():
    """R3 unit test: openclaw_format_exchange emits placeholder for empty user body."""
    empty_user = f"user\t{json.dumps('')}"
    assistant_reply = f"assistant\t{json.dumps('Bot is online.')}"
    role_tab = f"{empty_user}\n{assistant_reply}"

    result = src_transforms.openclaw_format_exchange(role_tab)

    assert "> [non-text user turn]" in result, (
        "empty user body must produce placeholder, not be silently dropped"
    )
    assert "Bot is online." in result, "assistant content must survive when user body is empty"


def test_r3_empty_assistant_still_skipped():
    """R3: empty assistant bodies must still be silently dropped (unchanged behaviour)."""
    user_text = f"user\t{json.dumps('What is 2 + 2?')}"
    empty_assistant = f"assistant\t{json.dumps('')}"
    role_tab = f"{user_text}\n{empty_assistant}"

    result = src_transforms.openclaw_format_exchange(role_tab)

    assert "> What is 2 + 2?" in result
    # Empty assistant — only the user block appears
    assert result.strip() == "> What is 2 + 2?"


# ===========================================================================
# R4: ReDoS hardening — _OPENCLAW_META_FENCE adversarial input test
# ===========================================================================


def test_r4_meta_fence_regex_matches_existing_patterns():
    """R4: existing metadata-fence behaviour must be preserved after regex rewrite."""
    preamble = (
        '```json\n{"chat_id": "user:U123", "sender_id": "U123", "inbound_event_kind": "msg"}\n```'
    )
    result = src_transforms._OPENCLAW_META_FENCE.sub("", preamble)
    assert "chat_id" not in result, "R4 regression: metadata fence not removed"
    assert result.strip() == "", "R4 regression: fence body leaked after rewrite"


def test_r4_meta_fence_regex_completes_fast_on_adversarial_input():
    """R4: regex must complete quickly on a long body that contains backtick lines.

    The original pattern used two DOTALL .*? spans.  With many backtick-prefixed
    lines followed by many key occurrences and no closing fence, the original
    could exhibit O(n\u00b2) backtracking.  The [^`]*? fix stops at the first
    backtick, aborting the match in O(1) for each starting position.
    """
    import time

    # Adversarial: many backtick-prefixed lines (early termination trigger for
    # [^`]*?) followed by many occurrences of a matching key, no closing ```.
    # The [^`]*? rewrite stops immediately at the backtick in each `inert line;
    # the original .*? would scan past them and attempt all key positions.
    adversarial = (
        "```json\n"
        + "`inert_line\n" * 3000  # 3000 lines each starting with a backtick
        + "chat_id: val\n" * 500  # 500 key occurrences after the backtick lines
        # deliberately no closing ``` to force the full scan
    )
    start = time.perf_counter()
    result = src_transforms._OPENCLAW_META_FENCE.sub("", adversarial)
    elapsed = time.perf_counter() - start

    assert result == adversarial, "adversarial input should not match (no closing ```)"
    assert elapsed < 1.0, (
        f"R4 ReDoS guard: regex took {elapsed:.3f}s on adversarial input "
        f"(expected < 1.0s after [^`]*? rewrite)"
    )


# ===========================================================================
# PERF: runtime _apply_post_extract_pipeline parity with declared pipeline
# ===========================================================================


def test_perf_runtime_pipeline_matches_declared_pipeline(sessions_dir):
    """PERF: _apply_post_extract_pipeline must produce identical output to
    _apply_transform_pipeline (full declared pipeline from raw bytes).

    This is the drift-prevention lock: if the optimised runtime path ever
    diverges from the conformance path, this test catches it immediately.
    Covers the R3 orphan case and R1 new token patterns via sub-fixtures.
    """
    from mempalace.sources.openclaw import (
        JsonlTrajectoryEventSource,
        _apply_post_extract_pipeline,
        _apply_transform_pipeline,
        _build_canonical_source_bytes,
        _extract_turns_from_events,
    )

    for session_id in (_SESSION_SIMPLE, _SESSION_METADATA, _SESSION_UNICODE):
        tfile = sessions_dir / f"{session_id}.trajectory.jsonl"
        # Conformance path: full declared pipeline from raw bytes.
        raw = _build_canonical_source_bytes(tfile)
        declared_output = _apply_transform_pipeline(raw)

        # Runtime path: extract turns from events, then post-extract pipeline.
        events = list(JsonlTrajectoryEventSource(tfile).iter_events())
        turns_text = _extract_turns_from_events(events)
        runtime_output = _apply_post_extract_pipeline(turns_text)

        assert declared_output == runtime_output, (
            f"PERF parity failure for {session_id}: "
            "runtime _apply_post_extract_pipeline diverged from declared pipeline"
        )


def test_perf_parity_with_r3_orphan_fixture(tmp_path):
    """PERF parity: orphan-user fixture (R3) must be identical on both paths."""
    from mempalace.sources.openclaw import (
        JsonlTrajectoryEventSource,
        _apply_post_extract_pipeline,
        _apply_transform_pipeline,
        _build_canonical_source_bytes,
        _extract_turns_from_events,
    )

    session_id = "sess-parity-r3-0001"
    tfile = tmp_path / f"{session_id}.trajectory.jsonl"
    _build_envelope_only_trajectory(tfile, session_id)

    raw = _build_canonical_source_bytes(tfile)
    declared_output = _apply_transform_pipeline(raw)

    events = list(JsonlTrajectoryEventSource(tfile).iter_events())
    turns_text = _extract_turns_from_events(events)
    runtime_output = _apply_post_extract_pipeline(turns_text)

    assert declared_output == runtime_output, (
        "PERF parity failure on R3 orphan fixture: runtime path diverged from declared pipeline"
    )
    # Also assert that the placeholder is present (R3 correctness on both paths).
    assert "> [non-text user turn]" in declared_output, (
        "R3 placeholder missing from declared pipeline output"
    )
    assert "> [non-text user turn]" in runtime_output, (
        "R3 placeholder missing from runtime pipeline output"
    )


def test_perf_parity_with_r1_secrets_fixture(tmp_path):
    """PERF parity: new token formats (R1) must be redacted identically on both paths."""
    from mempalace.sources.openclaw import (
        JsonlTrajectoryEventSource,
        _apply_post_extract_pipeline,
        _apply_transform_pipeline,
        _build_canonical_source_bytes,
        _extract_turns_from_events,
    )

    session_id = "sess-parity-r1-0001"
    tfile = tmp_path / f"{session_id}.trajectory.jsonl"

    user_text = (
        f"My keys: {_FAKE_SK_PROJ} and {_FAKE_GITHUB_PAT}. "
        f"Please help configure this service. Long enough to form a complete chunk."
    )
    assistant_text = (
        "Please rotate those credentials. This response is also long enough for a chunk."
    )
    build_trajectory_fixture(
        tfile,
        session_id,
        [(user_text, [assistant_text])],
        workspace_dir="/home/user/projects/parity-r1",
    )

    raw = _build_canonical_source_bytes(tfile)
    declared_output = _apply_transform_pipeline(raw)

    events = list(JsonlTrajectoryEventSource(tfile).iter_events())
    turns_text = _extract_turns_from_events(events)
    runtime_output = _apply_post_extract_pipeline(turns_text)

    assert declared_output == runtime_output, (
        "PERF parity failure on R1 secrets fixture: runtime path diverged from declared pipeline"
    )
    # Both paths must redact the secrets.
    assert _FAKE_SK_PROJ not in declared_output, "sk-proj token not redacted in declared path"
    assert _FAKE_GITHUB_PAT not in declared_output, "github_pat token not redacted in declared path"
    assert _FAKE_SK_PROJ not in runtime_output, "sk-proj token not redacted in runtime path"
    assert _FAKE_GITHUB_PAT not in runtime_output, "github_pat token not redacted in runtime path"
