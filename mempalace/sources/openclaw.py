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
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

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


# Default sessions-directory search order.
_DEFAULT_SESSIONS_DIRS: Tuple[str, ...] = (
    "~/.openclaw/agents/main/sessions",
)

# Filename suffix that identifies trajectory files.
_TRAJECTORY_SUFFIX = ".trajectory.jsonl"


def _detect_hall(content: str) -> str:
    """Hall-detection helper — defers to convo_miner's cached lookup."""
    from ..convo_miner import _detect_hall_cached

    return _detect_hall_cached(content)


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
    """Return sorted list of trajectory files for a :class:`SourceRef`.

    Handles three shapes:

    * ``local_path`` pointing at a directory → enumerate ``*.trajectory.jsonl``
      inside it.
    * ``local_path`` pointing at a single ``*.trajectory.jsonl`` file → return
      that one file.
    * ``local_path`` is ``None`` → use :data:`_DEFAULT_SESSIONS_DIRS`.
    """
    if source.local_path:
        p = Path(source.local_path).expanduser()
        if p.is_file():
            return [p.resolve()]
        if p.is_dir():
            return sorted(p.resolve().glob(f"*{_TRAJECTORY_SUFFIX}"))
        raise SourceNotFoundError(
            f"SourceRef.local_path {source.local_path!r} is neither a file nor a directory."
        )
    # Default: scan the first existing default directory.
    for raw in _DEFAULT_SESSIONS_DIRS:
        d = Path(raw).expanduser()
        if d.is_dir():
            return sorted(d.resolve().glob(f"*{_TRAJECTORY_SUFFIX}"))
    raise SourceNotFoundError(
        f"No OpenClaw sessions directory found (searched {list(_DEFAULT_SESSIONS_DIRS)}). "
        f"Pass SourceRef(local_path=<sessions-dir>)."
    )


def session_source_file(file_path: str, session_id: str) -> str:
    """Construct the stable per-session ``source_file`` identifier.

    Shape: ``openclaw://<absolute-path>#session=<session-id>``.
    Stable across re-ingests; used as the ChromaDB
    ``where={"source_file": …}`` key and by :meth:`is_current`.
    """
    return f"openclaw://{file_path}#session={session_id}"


def _scan_trajectory_file(path: Path) -> Optional[dict]:
    """Single-pass scan of a trajectory file for session metadata + version.

    Returns a dict with:

    * ``session_id`` — from ``sessionId`` field of first event.
    * ``session_key`` — from ``sessionKey`` field (e.g. ``agent:main:slack:…``).
    * ``workspace_dir`` — from ``workspaceDir`` field.
    * ``model_id`` — from ``modelId`` field.
    * ``session_created_at`` — ISO-8601 ``ts`` of the first event.
    * ``version`` — ``str(seq)`` of the last event (used by
      :meth:`is_current`; trajectory files are append-only so the last
      ``seq`` advances whenever new events are appended).

    Returns ``None`` if no parseable events are found.
    """
    first_event: Optional[dict] = None
    last_event: Optional[dict] = None
    for raw in path.open(encoding="utf-8", errors="replace"):
        raw = raw.strip()
        if not raw:
            continue
        try:
            event = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if first_event is None:
            first_event = event
        last_event = event

    if first_event is None:
        return None

    return {
        "session_id": first_event.get("sessionId") or path.stem,
        "session_key": first_event.get("sessionKey") or "",
        "workspace_dir": first_event.get("workspaceDir") or "",
        "model_id": first_event.get("modelId") or "",
        "session_created_at": first_event.get("ts") or "",
        "version": str(last_event.get("seq", 0)) if last_event else "0",
    }


def _build_canonical_source_bytes(path: Path) -> str:
    """Return the canonical source bytes for a trajectory file.

    The canonical form is the raw UTF-8 text of the ``.trajectory.jsonl``
    file (UTF-8 decode with replacement for invalid bytes). This is the
    input the declared transformation chain consumes; the conformance
    suite uses the same shape to verify the declared-transformation
    round-trip is exact.
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
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


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
                        "OpenClaw session routing key "
                        "(e.g. agent:main:slack:direct:<user-id>)"
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
                        "Model identifier used for the session "
                        "(e.g. anthropic/claude-sonnet-4-6)"
                    ),
                ),
                "session_created_at": FieldSpec(
                    type="string",
                    required=True,
                    description="ISO-8601 UTC timestamp of the first event in the trajectory",
                ),
                "message_count": FieldSpec(
                    type="int",
                    required=True,
                    description=(
                        "Number of exchange pairs (user+assistant turns) extracted "
                        "from the session"
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
                logger.debug("openclaw adapter: skipping empty file %s", tfile)
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

            # Count exchange pairs for metadata (rough: count "> " lines).
            message_count = sum(1 for ln in transcript.split("\n") if ln.strip().startswith(">"))

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
    "session_source_file",
    "_build_canonical_source_bytes",
    "_scan_trajectory_file",
]
