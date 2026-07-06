"""OpenClaw source adapter (RFC 002).

Ingests OpenClaw AI-agent session transcripts from ``*.trajectory.jsonl``
files into the palace as :class:`DrawerRecord` instances.

By default the adapter discovers trajectory files under
``~/.openclaw/agents/main/sessions/``. Pass
``SourceRef(local_path="<directory>")`` to point at an alternate sessions
directory, or ``SourceRef(local_path="<file>.trajectory.jsonl")`` to ingest
a single file.

Each trajectory file produces one ``source_file`` identifier of the shape
``openclaw://<absolute-path>#session=<session-id>``. Drawers are
exchange-pair chunks of the session transcript (user prompt + assistant
response), formatted to match the ``convo_miner`` shape so downstream
ranking, search, and closet-building behave identically.

The extraction pipeline mirrors ``convo-spike/convert_trajectories.py``:

* ``prompt.submitted`` events → user turns (``data.prompt``).
* ``model.completed`` events → assistant turns (``data.assistantTexts``
  joined with ``\\n``).
* ``context.compiled``, ``session.started``, ``session.ended``,
  ``trace.*`` etc. are skipped.
* OpenClaw runtime-context injection blocks and metadata-preamble fences
  are stripped from user text before chunking.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Protocol, Tuple, runtime_checkable

from ..convo_miner import chunk_exchanges, detect_convo_room
from ..config import normalize_wing_name
from . import transforms as _transforms
from .base import (
    AdapterClosedError,
    AdapterSchema,
    BaseSourceAdapter,
    DrawerRecord,
    FieldSpec,
    RouteHint,
    SourceItemMetadata,
    SourceNotFoundError,
    SourceRef,
    SourceSummary,
)
from .context import PalaceContext

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default sessions-directory search order.
_DEFAULT_SESSIONS_DIRS: Tuple[str, ...] = ("~/.openclaw/agents/main/sessions",)

# Filename suffix that identifies trajectory files.
_TRAJECTORY_SUFFIX = ".trajectory.jsonl"

# Pointer file suffix: written by OpenClaw beside the session file when the
# trajectory sidecar was relocated (e.g. via ``OPENCLAW_TRAJECTORY_DIR``).
# Content: JSON object with a "path" key pointing to the relocated sidecar.
_POINTER_SUFFIX = ".trajectory-path.json"

# Expected schema marker value (docs/tools/trajectory.md).
# Events whose ``traceSchema`` differs from this value indicate a different
# format — the file is skipped to avoid corrupt ingest.
_TRACE_SCHEMA_VALUE = "openclaw-trajectory"

# Schema versions this adapter knows how to handle. A file with a
# ``schemaVersion`` outside this set triggers a WARNING, but the adapter
# proceeds with best-effort parsing (newer minor versions are likely
# backward-compatible for the fields we consume).
_KNOWN_SCHEMA_VERSIONS: frozenset = frozenset({1})

# Truncation-marker event types emitted when the 10 MiB live-capture cap or
# the 200,000-event cap fires. These events are recognised and skipped
# without being counted as data events. The partial data before them is
# still parsed.
#
# The canonical type is "trace.truncated" (confirmed from OpenClaw source:
# the sessions-tail renderer maps `case "trace.truncated": return "trajectory
# truncated"`). Kept as a set so additional truncation markers can be added.
_TRUNCATION_EVENT_TYPES: frozenset = frozenset({"trace.truncated"})


# ---------------------------------------------------------------------------
# Reader abstraction (RFC 002 extension seam for database-first migration)
# ---------------------------------------------------------------------------


@runtime_checkable
class TrajectoryEventSource(Protocol):
    """Source-agnostic protocol for iterating OpenClaw trajectory events.

    The current production implementation is :class:`JsonlTrajectoryEventSource`
    which reads ``*.trajectory.jsonl`` sidecars from disk.  Code that performs
    extraction, transform, and chunking works against this protocol; it does
    **not** need to change when the storage backend changes.

    .. rubric:: SQLite extension seam (docs/refactor/database-first.md)

    Per ``database-first.md``, trajectory runtime events are migrating from
    ``*.trajectory.jsonl`` sidecars to a per-agent SQLite table in
    ``agents/<agentId>/agent/openclaw-agent.sqlite``.  The planned schema::

        CREATE TABLE trajectory_runtime_events (
            session_id  TEXT    NOT NULL,
            run_id      TEXT    NOT NULL,
            seq         INTEGER NOT NULL,
            event_json  TEXT    NOT NULL,   -- full event serialised as JSON
            created_at  TEXT    NOT NULL    -- ISO-8601 UTC
        );

    To implement ``SqliteTrajectoryEventSource``:

    1. ``class SqliteTrajectoryEventSource:``
       ``    def __init__(self, agent_id: str, session_id: str, db_path: Path)``
    2. ``iter_events()``: open the agent db with ``sqlite3``, execute::

           SELECT event_json FROM trajectory_runtime_events
            WHERE session_id = ?
            ORDER BY seq

       and ``yield json.loads(row[0])`` for each row.  Apply the same
       truncation-marker and error-tolerance logic as
       :class:`JsonlTrajectoryEventSource`.
    3. ``session_metadata()``: call
       :func:`_aggregate_session_metadata` with ``self.iter_events()`` as
       the event iterator.
    4. Pass the new instance wherever :class:`JsonlTrajectoryEventSource`
       is used today — no changes to extraction, transform, or chunking
       required.

    JSONL sidecars remain valid as legacy/doctor-import inputs via
    :class:`JsonlTrajectoryEventSource`; runtime ingest switches to the
    SQLite source once the migration is complete.

    .. note::
        Transcript content transformation (the JSONL text → ruff pipeline)
        is separate from event iteration.  For a SQLite source, synthesise
        JSONL text from ``iter_events()`` output before passing it to
        :func:`_build_canonical_source_bytes_from_events` (a helper to be
        added when the SQLite backend lands).  The transform/chunk steps are
        unchanged.
    """

    def iter_events(self) -> Iterator[Dict[str, Any]]:
        """Yield parsed event dicts in ascending sequence order.

        Implementation contract:

        * Silently skip lines / rows that cannot be parsed as JSON objects.
        * Validate the schema marker on the first parseable event; skip the
          whole stream (return early) if ``traceSchema`` is wrong; log a
          WARNING and continue if ``schemaVersion`` is unknown.
        * Silently skip truncation-marker events
          (see :data:`_TRUNCATION_EVENT_TYPES`).
        * Tolerate a partial final line written when the 10 MiB cap fires
          before a newline is flushed — such a line will fail ``json.loads``
          and must be skipped without raising.
        """
        ...

    def session_metadata(self) -> Optional[Dict[str, Any]]:
        """Return aggregated session-level metadata, or ``None`` if no events.

        Returned keys when not ``None``:
        ``session_id``, ``session_key``, ``workspace_dir``, ``model_id``,
        ``session_created_at``, ``version``, ``message_count``.
        """
        ...


# ---------------------------------------------------------------------------
# Schema-marker helpers
# ---------------------------------------------------------------------------


def _validate_schema_marker(
    event: Dict[str, Any],
    source_name: str,
) -> bool:
    """Validate the schema marker on the first parseable event.

    Args:
        event: First parsed event dict.
        source_name: Human-readable label for log messages (e.g. file basename).

    Returns:
        ``True`` — proceed with parsing (valid marker, or no marker present).
        ``False`` — skip the whole file (wrong ``traceSchema``).

    Side effects:
        Logs a WARNING for an unknown ``schemaVersion`` (best-effort parse
        continues).  Logs a WARNING and returns ``False`` for a wrong
        ``traceSchema``.
    """
    trace_schema = event.get("traceSchema")
    schema_version = event.get("schemaVersion")

    if trace_schema is not None and trace_schema != _TRACE_SCHEMA_VALUE:
        logger.warning(
            "openclaw adapter: %s — unexpected traceSchema %r (expected %r); "
            "skipping file to avoid corrupt ingest",
            source_name,
            trace_schema,
            _TRACE_SCHEMA_VALUE,
        )
        return False  # wrong format — skip file

    if schema_version is not None and schema_version not in _KNOWN_SCHEMA_VERSIONS:
        logger.warning(
            "openclaw adapter: %s — unknown schemaVersion %r "
            "(known: %r); attempting best-effort parse — "
            "some fields may be missing or misinterpreted",
            source_name,
            schema_version,
            sorted(_KNOWN_SCHEMA_VERSIONS),
        )
        # Continue parsing — do NOT return False.

    return True


# ---------------------------------------------------------------------------
# Source-agnostic metadata aggregation
# ---------------------------------------------------------------------------


def _aggregate_session_metadata(
    source_name: str,
    events: Iterator[Dict[str, Any]],
    *,
    fallback_stem: str = "",
) -> Optional[Dict[str, Any]]:
    """Aggregate session metadata from any event iterator.

    This helper is **source-agnostic**: it consumes whatever
    :meth:`TrajectoryEventSource.iter_events` yields, whether from JSONL on
    disk or (in future) from the SQLite ``trajectory_runtime_events`` table.

    Args:
        source_name: Human-readable label for debug messages.
        events: Iterator of parsed event dicts.
        fallback_stem: Fallback ``session_id`` when the first event has no
            ``sessionId`` field (e.g. stem of the source filename).

    Returns:
        Metadata dict with keys ``session_id``, ``session_key``,
        ``workspace_dir``, ``model_id``, ``session_created_at``,
        ``version``, ``message_count``; or ``None`` if no events were found.
    """
    first_event: Optional[Dict[str, Any]] = None
    last_event: Optional[Dict[str, Any]] = None
    message_count = 0
    pending_user = False

    for event in events:
        if first_event is None:
            first_event = event
        last_event = event
        etype = event.get("type")
        if etype == "prompt.submitted":
            pending_user = True
        elif etype == "model.completed" and pending_user:
            message_count += 1
            pending_user = False

    if first_event is None:
        return None

    return {
        "session_id": first_event.get("sessionId") or fallback_stem,
        "session_key": first_event.get("sessionKey") or "",
        "workspace_dir": first_event.get("workspaceDir") or "",
        "model_id": first_event.get("modelId") or "",
        "session_created_at": first_event.get("ts") or "",
        "version": str(last_event.get("seq", 0)) if last_event else "0",
        "message_count": message_count,
    }


# ---------------------------------------------------------------------------
# JSONL-on-disk implementation
# ---------------------------------------------------------------------------


class JsonlTrajectoryEventSource:
    """JSONL-on-disk implementation of :class:`TrajectoryEventSource`.

    Reads ``*.trajectory.jsonl`` files line-by-line with:

    * **Schema-marker validation** on the first parseable event
      (:func:`_validate_schema_marker`).
    * **Partial-line tolerance** — a line that fails ``json.loads``
      (e.g. the last line was written mid-stream when the 10 MiB cap fired
      before the newline was flushed, or an individual line exceeded the
      256 KiB per-line cap) is silently skipped.
    * **Truncation-marker recognition** — events whose ``type`` is in
      :data:`_TRUNCATION_EVENT_TYPES` are silently skipped and not counted
      as data events; the partial data before them is still parsed.
    * **Line-level error recovery** — any non-JSON line is silently skipped.

    Args:
        path: Absolute or relative path to the ``*.trajectory.jsonl`` file.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._meta_cache: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------
    # TrajectoryEventSource implementation
    # ------------------------------------------------------------------

    def iter_events(self) -> Iterator[Dict[str, Any]]:
        """Yield parsed event dicts with full schema validation and truncation tolerance."""
        schema_validated = False
        try:
            fh = self._path.open(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("openclaw adapter: cannot open %s: %s", self._path, exc)
            return

        try:
            for raw_line in fh:
                raw = raw_line.strip()
                if not raw:
                    continue

                try:
                    event = json.loads(raw)
                except (ValueError, TypeError):
                    # Partial or corrupt line — covers truncation at the
                    # 10 MiB or 256 KiB per-line cap.  Skip silently.
                    continue

                if not isinstance(event, dict):
                    continue

                # Validate schema marker on the FIRST parseable event only.
                if not schema_validated:
                    schema_validated = True
                    if not _validate_schema_marker(event, self._path.name):
                        return  # wrong traceSchema — skip whole file

                # Skip truncation-marker events cleanly; still yield data
                # events that appear before them.
                if event.get("type") in _TRUNCATION_EVENT_TYPES:
                    logger.debug(
                        "openclaw adapter: truncation marker in %s — "
                        "file was capped at 10 MiB or 200k events; "
                        "partial ingest follows",
                        self._path.name,
                    )
                    continue

                yield event
        finally:
            fh.close()

    def session_metadata(self) -> Optional[Dict[str, Any]]:
        """Return cached session metadata computed from :meth:`iter_events`."""
        if self._meta_cache is None:
            self._meta_cache = _aggregate_session_metadata(
                self._path.name,
                self.iter_events(),
                fallback_stem=self._path.stem,
            )
        return self._meta_cache


# ---------------------------------------------------------------------------
# Legacy scan shim (backward-compatible; delegates to JsonlTrajectoryEventSource)
# ---------------------------------------------------------------------------


def _scan_trajectory_file(path: Path) -> Optional[dict]:
    """Single-pass scan of a trajectory file for session metadata + version.

    Returns a dict with:

    * ``session_id`` — from ``sessionId`` field of first event.
    * ``session_key`` — from ``sessionKey`` field.
    * ``workspace_dir`` — from ``workspaceDir`` field.
    * ``model_id`` — from ``modelId`` field.
    * ``session_created_at`` — ISO-8601 ``ts`` of the first event.
    * ``version`` — ``str(seq)`` of the last event.
    * ``message_count`` — number of complete exchange pairs found.

    Returns ``None`` if no parseable events are found or if the file has a
    wrong ``traceSchema`` (see :func:`_validate_schema_marker`).

    .. note::
        This function is a thin shim over
        :meth:`JsonlTrajectoryEventSource.session_metadata`; it exists for
        backward compatibility and is exported in :data:`__all__`.
    """
    return JsonlTrajectoryEventSource(path).session_metadata()


# ---------------------------------------------------------------------------
# Pointer file reader
# ---------------------------------------------------------------------------


def _read_pointer_file(pointer_path: Path) -> Optional[Path]:
    """Read a ``.trajectory-path.json`` pointer and return the target :class:`Path`.

    OpenClaw writes a best-effort pointer file beside the session file when
    the trajectory sidecar was relocated via ``OPENCLAW_TRAJECTORY_DIR``.
    Expected JSON shape::

        {"path": "/abs/path/to/<session-id>.trajectory.jsonl"}

    Alternative key names tried in order (for forward-compat):
    ``path``, ``trajectoryPath``, ``trajectoryFilePath``.

    Returns ``None`` on any I/O or parse error (pointer lookup is best-effort).
    """
    try:
        data = json.loads(pointer_path.read_text(encoding="utf-8", errors="replace"))
        for key in ("path", "trajectoryPath", "trajectoryFilePath"):
            val = data.get(key)
            if val:
                return Path(str(val)).expanduser()
    except Exception as exc:
        logger.debug(
            "openclaw adapter: failed to read pointer file %s: %s",
            pointer_path,
            exc,
        )
    return None


# ---------------------------------------------------------------------------
# Discovery helpers
# ---------------------------------------------------------------------------


def _discover_from_dir(sessions_dir: Path) -> List[Path]:
    """Discover trajectory files from a sessions directory.

    Searches in three ways, de-duplicating by resolved path:

    1. **Default sidecars** — ``<sessions_dir>/*.trajectory.jsonl`` files
       written beside session files (default OpenClaw capture location).
    2. **Pointer files** — ``<sessions_dir>/*.trajectory-path.json`` files
       written by OpenClaw when ``OPENCLAW_TRAJECTORY_DIR`` was set; each
       pointer contains the path to the relocated sidecar.
    3. **OPENCLAW_TRAJECTORY_DIR** — when this env var is set, all
       ``*.trajectory.jsonl`` files in that dedicated directory are included.

    The ``OPENCLAW_TRAJECTORY_DIR`` path is read from the environment on
    every call so tests can use ``monkeypatch.setenv`` without reloading.

    Args:
        sessions_dir: Resolved absolute path to the sessions directory.

    Returns:
        Sorted, de-duplicated list of resolved :class:`Path` objects.
    """
    found: set = set()

    # 1. Default sidecars (beside session files).
    for f in sessions_dir.glob(f"*{_TRAJECTORY_SUFFIX}"):
        if f.is_file():
            found.add(f.resolve())

    # 2. Pointer files (best-effort; missing/corrupt pointers are skipped).
    for pf in sessions_dir.glob(f"*{_POINTER_SUFFIX}"):
        target = _read_pointer_file(pf)
        if target is not None:
            resolved = target.resolve()
            if resolved.is_file():
                found.add(resolved)
            else:
                logger.debug(
                    "openclaw adapter: pointer %s → %s does not exist; skipping",
                    pf.name,
                    resolved,
                )

    # 3. OPENCLAW_TRAJECTORY_DIR dedicated directory.
    traj_dir_env = os.environ.get("OPENCLAW_TRAJECTORY_DIR")
    if traj_dir_env:
        traj_dir = Path(traj_dir_env).expanduser()
        if traj_dir.is_dir():
            for f in traj_dir.glob(f"*{_TRAJECTORY_SUFFIX}"):
                if f.is_file():
                    found.add(f.resolve())
        else:
            logger.warning(
                "openclaw adapter: OPENCLAW_TRAJECTORY_DIR=%r is not a directory; "
                "skipping dedicated trajectory directory",
                traj_dir_env,
            )

    return sorted(found)


def _resolve_sessions_dir(local_path: Optional[str] = None) -> Optional[Path]:
    """Resolve a sessions directory from an optional caller-supplied path.

    Returns ``None`` when ``local_path`` points at a single trajectory file
    (caller will handle enumeration themselves). Returns a :class:`Path` to
    the directory to enumerate otherwise.

    Raises :class:`SourceNotFoundError` when a non-``None`` ``local_path``
    resolves to neither a file nor a directory.
    """
    if local_path:
        p = Path(local_path).expanduser()
        if p.is_file():
            return None  # single-file mode; caller enumerates
        if p.is_dir():
            return p.resolve()
        raise SourceNotFoundError(
            f"SourceRef.local_path {local_path!r} is neither a file nor a directory."
        )
    # No explicit path: try defaults.
    for raw in _DEFAULT_SESSIONS_DIRS:
        p = Path(raw).expanduser()
        if p.is_dir():
            return p.resolve()
    raise SourceNotFoundError(
        f"No OpenClaw sessions directory found (searched {list(_DEFAULT_SESSIONS_DIRS)}). "
        f"Pass SourceRef(local_path=<sessions-dir>) or create one of the default paths."
    )


def _enumerate_trajectory_files(source: SourceRef) -> List[Path]:
    """Return sorted, de-duplicated trajectory files for a :class:`SourceRef`.

    Handles three shapes:

    * ``local_path`` pointing at a single ``*.trajectory.jsonl`` file → return
      that one file (env var and pointer lookup are skipped for single-file mode).
    * ``local_path`` pointing at a directory → call :func:`_discover_from_dir`.
    * ``local_path`` is ``None`` → try :data:`_DEFAULT_SESSIONS_DIRS` in order
      and call :func:`_discover_from_dir` on the first existing one.

    In directory mode, :func:`_discover_from_dir` additionally checks pointer
    files and ``OPENCLAW_TRAJECTORY_DIR`` so all three documented capture
    locations are covered.
    """
    if source.local_path:
        p = Path(source.local_path).expanduser()
        if p.is_file():
            return [p.resolve()]
        if p.is_dir():
            return _discover_from_dir(p.resolve())
        raise SourceNotFoundError(
            f"SourceRef.local_path {source.local_path!r} is neither a file nor a directory."
        )
    # Default: scan the first existing default directory.
    for raw in _DEFAULT_SESSIONS_DIRS:
        d = Path(raw).expanduser()
        if d.is_dir():
            return _discover_from_dir(d.resolve())
    raise SourceNotFoundError(
        f"No OpenClaw sessions directory found (searched {list(_DEFAULT_SESSIONS_DIRS)}). "
        f"Pass SourceRef(local_path=<sessions-dir>) or create one of the default paths."
    )


# ---------------------------------------------------------------------------
# Source file URI and transform helpers
# ---------------------------------------------------------------------------


def session_source_file(file_path: str, session_id: str) -> str:
    """Construct the stable per-session ``source_file`` identifier.

    Shape: ``openclaw://<absolute-path>#session=<session-id>``.
    Stable across re-ingests; used as the ChromaDB
    ``where={"source_file": …}`` key and by :meth:`is_current`.
    """
    return f"openclaw://{file_path}#session={session_id}"


def _build_canonical_source_bytes(path: Path) -> str:
    """Return the canonical source bytes for a trajectory file.

    The canonical form is the raw UTF-8 text of the ``.trajectory.jsonl``
    file (UTF-8 decode with replacement for invalid bytes). This is the
    input the declared transformation chain consumes; the conformance
    suite uses the same shape to verify the declared-transformation
    round-trip is exact.

    .. note:: SQLite seam — future work
        For a SQLite-backed :class:`TrajectoryEventSource`, synthesise JSONL
        text from the event rows before passing to this pipeline::

            raw = "\\n".join(json.dumps(e) for e in source.iter_events())

        This keeps the transform/chunk pipeline source-agnostic.  The helper
        ``_build_canonical_source_bytes_from_events`` will be added when the
        SQLite backend lands.
    """
    return path.read_text(encoding="utf-8", errors="replace")


def _apply_transform_pipeline(raw_text: str) -> str:
    """Apply the declared OpenClaw transform pipeline in declaration order.

    Returns the pre-chunking exchange-pair transcript; pass the result to
    ``convo_miner.chunk_exchanges`` to produce drawer chunks.
    """
    pipeline = [
        _transforms.openclaw_extract_turns,
        _transforms.openclaw_strip_runtime_context,
        _transforms.openclaw_strip_metadata_preamble,
        _transforms.openclaw_format_exchange,
        _transforms.newline_normalize,
        _transforms.whitespace_trim,
    ]
    text = raw_text
    for step in pipeline:
        text = step(text)
    return text


def _now_utc_iso() -> str:
    """Return current UTC time as ISO-8601 with trailing ``Z``."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _detect_hall(content: str) -> str:
    """Hall-detection helper — defers to convo_miner's cached lookup."""
    from ..convo_miner import _detect_hall_cached

    return _detect_hall_cached(content)


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class OpenClawSourceAdapter(BaseSourceAdapter):
    """Mine OpenClaw AI-agent session transcripts into the palace (RFC 002 §1)."""

    name = "openclaw"
    adapter_version = "0.1.0"
    capabilities = frozenset(
        {
            "supports_incremental",
            "supports_structured_metadata",
            "adapter_owns_routing",
        }
    )
    supported_modes = frozenset({"chunked_content"})
    declared_transformations = frozenset(
        {
            "openclaw_extract_turns",
            "openclaw_strip_runtime_context",
            "openclaw_strip_metadata_preamble",
            "openclaw_format_exchange",
            "newline_normalize",
            "whitespace_trim",
        }
    )
    default_privacy_class = "pii_potential"

    # Order of declared transformations as applied by the adapter. The
    # conformance suite walks this list in order, so it MUST mirror the
    # actual pipeline in :func:`_apply_transform_pipeline`.
    DECLARED_TRANSFORMATION_ORDER: Tuple[str, ...] = (
        "openclaw_extract_turns",
        "openclaw_strip_runtime_context",
        "openclaw_strip_metadata_preamble",
        "openclaw_format_exchange",
        "newline_normalize",
        "whitespace_trim",
    )

    def __init__(self) -> None:
        self._closed = False

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def describe_schema(self) -> AdapterSchema:
        return AdapterSchema(
            version="1.0",
            fields={
                "session_id": FieldSpec(
                    type="string",
                    required=True,
                    description=(
                        "OpenClaw session UUID (e.g. 00852ef4-fb01-415b-8335-9e5d559c8754)"
                    ),
                    indexed=True,
                ),
                "session_key": FieldSpec(
                    type="string",
                    required=False,
                    description=(
                        "OpenClaw session routing key (e.g. agent:main:slack:direct:<user-id>)"
                    ),
                    indexed=True,
                ),
                "workspace_dir": FieldSpec(
                    type="string",
                    required=False,
                    description=(
                        "Absolute filesystem path of the agent's workspace "
                        "when the session was recorded"
                    ),
                    indexed=True,
                ),
                "model_id": FieldSpec(
                    type="string",
                    required=False,
                    description=(
                        "Model identifier used for the session (e.g. anthropic/claude-sonnet-4-6)"
                    ),
                ),
                "session_created_at": FieldSpec(
                    type="string",
                    required=True,
                    description=("ISO-8601 UTC timestamp of the first event in the trajectory"),
                ),
                "message_count": FieldSpec(
                    type="int",
                    required=True,
                    description=(
                        "Number of exchange pairs (user+assistant turns) extracted from the session"
                    ),
                ),
                "extract_mode": FieldSpec(
                    type="string",
                    required=True,
                    description="Always 'exchange' for the OpenClaw adapter in v0.1",
                ),
            },
        )

    # ------------------------------------------------------------------
    # Ingest
    # ------------------------------------------------------------------

    def ingest(
        self,
        *,
        source: SourceRef,
        palace: PalaceContext,
    ) -> Iterator[object]:
        if self._closed:
            raise AdapterClosedError("OpenClawSourceAdapter is closed")
        trajectory_files = _enumerate_trajectory_files(source)
        for tfile in trajectory_files:
            meta = _scan_trajectory_file(tfile)
            if meta is None:
                logger.debug("openclaw adapter: skipping %s (no parseable events)", tfile)
                continue

            file_path = str(tfile)
            session_id = meta["session_id"]
            src_file = session_source_file(file_path, session_id)
            version = meta["version"]

            # Yield lazy-fetch metadata so core can short-circuit when
            # the trajectory file hasn't grown since the last ingest.
            yield SourceItemMetadata(
                source_file=src_file,
                version=version,
                size_hint=tfile.stat().st_size,
                route_hint=self._route_hint_for(source, meta["workspace_dir"]),
            )
            # is_skip_requested() is the public getter added in PR #1484 (not yet
            # on develop); fall back to the underlying flag until it merges.
            skip = (
                palace.is_skip_requested()
                if hasattr(palace, "is_skip_requested")
                else palace._skip_requested
            )
            if skip:
                continue

            raw_text = _build_canonical_source_bytes(tfile)
            transcript = _apply_transform_pipeline(raw_text)
            if not transcript:
                logger.debug(
                    "openclaw adapter: skipping %s — empty transcript after transforms",
                    tfile.name,
                )
                continue

            chunks = chunk_exchanges(transcript)
            if not chunks:
                logger.debug(
                    "openclaw adapter: skipping %s — chunk_exchanges produced nothing",
                    tfile.name,
                )
                continue

            # message_count is computed during scan: number of matched
            # prompt.submitted → model.completed pairs in the raw events.
            message_count = meta["message_count"]

            wing = self._wing_for(source, meta["workspace_dir"])
            room = detect_convo_room(transcript)
            filed_at = _now_utc_iso()

            for chunk in chunks:
                content = chunk["content"]
                chunk_index = int(chunk["chunk_index"])
                metadata = {
                    # Universal §5.1 fields
                    "source_file": src_file,
                    "chunk_index": chunk_index,
                    "filed_at": filed_at,
                    "added_by": "openclaw-adapter",
                    "wing": wing,
                    "room": room,
                    "hall": _detect_hall(content),
                    "ingest_mode": "chunked_content",
                    "extract_mode": "exchange",
                    "privacy_class": self.default_privacy_class,
                    # Adapter-declared fields (§5.2)
                    "session_id": session_id,
                    "session_key": meta["session_key"],
                    "workspace_dir": meta["workspace_dir"],
                    "model_id": meta["model_id"],
                    "session_created_at": meta["session_created_at"],
                    "message_count": message_count,
                    # Required by is_current() for incremental ingest;
                    # mirrors the SourceItemMetadata.version yielded above.
                    "openclaw_session_version": version,
                }
                yield DrawerRecord(
                    content=content,
                    source_file=src_file,
                    chunk_index=chunk_index,
                    metadata=metadata,
                    route_hint=RouteHint(wing=wing, room=room, hall=metadata["hall"]),
                )

    # ------------------------------------------------------------------
    # Incremental ingest
    # ------------------------------------------------------------------

    def is_current(
        self,
        *,
        item: SourceItemMetadata,
        existing_metadata: Optional[dict],
    ) -> bool:
        if not existing_metadata:
            return False
        # Prefer the stored ``openclaw_session_version`` field (set in v0.1+).
        stored_version = existing_metadata.get("openclaw_session_version")
        if stored_version is not None:
            return str(stored_version) == item.version
        # Fall back to "we have drawers for this source_file" → assume current.
        # Trajectory files are append-only; existing drawers mean we already
        # extracted whatever was in the file at last ingest time. Only
        # re-extract if the version field was recorded (see above) and changed.
        return True

    def source_summary(self, *, source: SourceRef) -> SourceSummary:
        try:
            files = _enumerate_trajectory_files(source)
        except SourceNotFoundError:
            return SourceSummary(description="OpenClaw sessions directory not found", item_count=0)
        count = len(files)
        try:
            if source.local_path:
                desc_path = str(Path(source.local_path).expanduser().resolve())
            else:
                desc_path = str(Path(_DEFAULT_SESSIONS_DIRS[0]).expanduser())
        except Exception:
            desc_path = source.local_path or _DEFAULT_SESSIONS_DIRS[0]
        return SourceSummary(
            description=f"OpenClaw sessions at {desc_path}",
            item_count=count,
        )

    def close(self) -> None:
        self._closed = True

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _wing_for(self, source: SourceRef, workspace_dir: Optional[str]) -> str:
        """Resolve the wing for a session, RFC 002 §2.5 precedence:

        1. Explicit ``options["wing"]`` from the SourceRef.
        2. Basename of ``workspaceDir`` from the trajectory.
        3. Adapter fallback: ``"openclaw_general"``.
        """
        explicit = (source.options or {}).get("wing")
        if explicit:
            return normalize_wing_name(str(explicit))
        if workspace_dir and workspace_dir != "/":
            base = Path(workspace_dir).name
            if base:
                return normalize_wing_name(base)
        return "openclaw_general"

    def _route_hint_for(
        self, source: SourceRef, workspace_dir: Optional[str]
    ) -> Optional[RouteHint]:
        """Wing-only route hint for the lazy-fetch :class:`SourceItemMetadata`.

        Room is content-dependent, so it is left ``None`` at the lazy-fetch
        stage; the eager :class:`DrawerRecord` emit fills it in per chunk.
        This mirrors the OpenCode adapter's §2.5 pattern so core makes
        consistent skip/routing decisions when ``options["wing"]`` is set.
        """
        wing = self._wing_for(source, workspace_dir)
        return RouteHint(wing=wing, room=None, hall=None)


__all__ = [
    "OpenClawSourceAdapter",
    "JsonlTrajectoryEventSource",
    "TrajectoryEventSource",
    "session_source_file",
    "_build_canonical_source_bytes",
    "_scan_trajectory_file",
]
