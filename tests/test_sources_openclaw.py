"""Tests for the OpenClaw source adapter (RFC 002).

Covers:
    * Adapter class identity (capabilities, modes, declared transformations).
    * RFC 002 conformance — declared-transformation round-trip, schema
      conformance, stable ``source_file`` shape.
    * Unit tests for JSONL extraction, runtime-context strip, metadata-preamble
      strip, exchange formatting, and chunked-exchange emit via
      ``convo_miner.chunk_exchanges``.
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
    OpenClawSourceAdapter,
    _build_canonical_source_bytes,
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
        'Conversation info (untrusted metadata):\n'
        '```json\n'
        '{\n'
        '  "chat_id": "user:UTEST123",\n'
        '  "message_id": "1234567890.123456",\n'
        '  "sender_id": "UTEST123",\n'
        '  "sender": "Test User"\n'
        '}\n'
        '```\n'
        '\n' + user_request
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
                ["Use the useQuery hook with a queryFn. For example: useQuery({ queryKey: ['todos'], queryFn: fetchTodos })"],
            ),
            (
                "How do I invalidate the cache after a mutation?",
                ["Call queryClient.invalidateQueries({ queryKey: ['todos'] }) inside the onSuccess callback of useMutation."],
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
                ["That error means you're calling .split() on a None value. Check where the variable is assigned."],
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
                ["async/await は非同期処理のための構文です。asyncキーワードをコルーチン関数の前に付けます。"],
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
    """Every declared transformation MUST resolve to a callable on transforms (RFC 002 §7.3)."""
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
    sf = session_source_file("/home/user/.openclaw/agents/main/sessions/abc.trajectory.jsonl", "abc")
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


def test_scan_trajectory_file_returns_none_for_empty_file(tmp_path):
    empty = tmp_path / "empty.trajectory.jsonl"
    empty.write_text("")
    assert _scan_trajectory_file(empty) is None


def test_scan_trajectory_file_version_is_last_seq(sessions_dir):
    """Version must be the seq of the last event (append-only growth check)."""
    tfile = sessions_dir / f"{_SESSION_SIMPLE}.trajectory.jsonl"
    meta = _scan_trajectory_file(tfile)
    # Read the last non-empty line to confirm the seq matches
    events = [json.loads(ln) for ln in tfile.read_text().splitlines() if ln.strip()]
    last_seq = str(events[-1]["seq"])
    assert meta["version"] == last_seq


# ===========================================================================
# Ingest shape
# ===========================================================================


def test_ingest_yields_metadata_then_drawers(adapter, palace_ctx, sessions_dir):
    results = list(adapter.ingest(source=SourceRef(local_path=str(sessions_dir)), palace=palace_ctx))
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
    results = list(adapter.ingest(source=SourceRef(local_path=str(sessions_dir)), palace=palace_ctx))
    drawers = [r for r in results if isinstance(r, DrawerRecord)]
    src_files_with_drawers = {d.source_file for d in drawers}
    assert all(_SESSION_SINGLE_TURN not in sf for sf in src_files_with_drawers), (
        "session with no exchanges must produce no drawers"
    )


def test_drawer_metadata_has_all_universal_and_schema_fields(adapter, palace_ctx, sessions_dir):
    results = list(adapter.ingest(source=SourceRef(local_path=str(sessions_dir)), palace=palace_ctx))
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
    results = list(adapter.ingest(source=SourceRef(local_path=str(sessions_dir)), palace=palace_ctx))
    for r in results:
        if isinstance(r, DrawerRecord):
            assert r.metadata["extract_mode"] == "exchange"


def test_drawer_route_hint_carries_wing_and_room(adapter, palace_ctx, sessions_dir):
    results = list(adapter.ingest(source=SourceRef(local_path=str(sessions_dir)), palace=palace_ctx))
    drawers = [r for r in results if isinstance(r, DrawerRecord)]
    for d in drawers:
        assert d.route_hint is not None
        assert d.route_hint.wing, "wing must not be empty"
        assert d.route_hint.room, "room must not be empty"


# ===========================================================================
# Wing routing
# ===========================================================================


def test_wing_derived_from_workspace_dir_basename(adapter, palace_ctx, sessions_dir):
    results = list(adapter.ingest(source=SourceRef(local_path=str(sessions_dir)), palace=palace_ctx))
    drawers = [r for r in results if isinstance(r, DrawerRecord)]
    # _SESSION_SIMPLE has workspace_dir="/home/user/projects/myproject" → wing="myproject"
    myproject_drawers = [
        d for d in drawers if _SESSION_SIMPLE in d.source_file
    ]
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
    assert adapter.is_current(item=item, existing_metadata={"openclaw_session_version": "42"}) is True
    assert adapter.is_current(item=item, existing_metadata={"openclaw_session_version": "99"}) is False


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
        {"type": "model.completed", "data": {"assistantTexts": ["First answer.", "Second answer."]}},
    ]
    raw = "\n".join(json.dumps(e) for e in events)
    result = src_transforms.openclaw_extract_turns(raw)
    lines = result.split("\n")
    assistant_body = json.loads(lines[1].partition("\t")[2])
    assert "First answer." in assistant_body
    assert "Second answer." in assistant_body


def test_openclaw_strip_runtime_context_removes_block():
    """Runtime context block must be removed from user turns."""
    raw_prompt = (
        "<<<OpenClaw runtime context\nSome internal info.\nEND_OPENCLAW_INTERNAL_CONTEXT>>>\n\nActual question"
    )
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
        'Conversation info (untrusted metadata):\n'
        '```json\n'
        '{"chat_id": "user:U123", "sender_id": "U123"}\n'
        '```\n'
        '\nReal user request here'
    )
    role_tab = f"user\t{json.dumps(preamble_prompt)}"
    result = src_transforms.openclaw_strip_metadata_preamble(role_tab)
    body = json.loads(result.partition("\t")[2])
    assert "chat_id" not in body
    assert "Real user request here" in body


def test_openclaw_strip_metadata_preamble_removes_label_lines():
    prompt_with_label = (
        "Conversation info (untrusted metadata):\n"
        "Real content below"
    )
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
# Declared-transformation round-trip (RFC 002 §7.3)
# ===========================================================================


def test_declared_transformation_round_trip_reproduces_drawer_content(
    adapter, palace_ctx, sessions_dir
):
    """Applying the declared transforms in order to canonical source bytes
    then chunking MUST reproduce the drawer content."""
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
    results = list(adapter.ingest(source=SourceRef(local_path=str(sessions_dir)), palace=palace_ctx))
    drawers = [
        d for d in results
        if isinstance(d, DrawerRecord) and _SESSION_UNICODE in d.source_file
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
    results = list(adapter.ingest(source=SourceRef(local_path=str(sessions_dir)), palace=palace_ctx))
    drawers = [
        d for d in results
        if isinstance(d, DrawerRecord) and _SESSION_METADATA in d.source_file
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
        {"type": "session.started", "data": {}, "sessionId": "sess-mal",
         "sessionKey": "k", "workspaceDir": "/tmp", "modelId": "m",
         "ts": "2026-01-01T00:00:00.000Z", "seq": 1},
        "THIS IS NOT JSON",
        {"type": "prompt.submitted", "data": {"prompt": "Valid question that is long enough to pass chunking threshold"},
         "sessionId": "sess-mal", "sessionKey": "k", "workspaceDir": "/tmp",
         "modelId": "m", "ts": "2026-01-01T00:00:01.000Z", "seq": 2},
        "ANOTHER GARBAGE LINE",
        {"type": "model.completed", "data": {"assistantTexts": ["Valid answer that is also long enough to make a proper chunk"]},
         "sessionId": "sess-mal", "sessionKey": "k", "workspaceDir": "/tmp",
         "modelId": "m", "ts": "2026-01-01T00:00:02.000Z", "seq": 3},
    ]
    with tfile.open("w") as fh:
        for e in events:
            fh.write((json.dumps(e) if isinstance(e, dict) else e) + "\n")

    results = list(adapter.ingest(source=SourceRef(local_path=str(sdir)), palace=palace_ctx))
    drawers = [r for r in results if isinstance(r, DrawerRecord)]
    assert len(drawers) >= 1, "malformed lines should be skipped, not crash"
