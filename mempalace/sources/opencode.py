"""OpenCode source adapter (RFC 002).

Ingests OpenCode AI-coding-CLI session transcripts out of OpenCode's local
SQLite store (default ``~/.local/share/opencode/opencode.db``) into the
palace as :class:`DrawerRecord` instances.

Each OpenCode session becomes one ``source_file`` of the shape
``opencode://<absolute-db-path>#session=<sid>``. The drawers under that
``source_file`` are exchange-pair chunks of the session transcript,
formatted to match the existing ``convo_miner`` shape so downstream
ranking, search, and closet-building behave identically.

Reverse-engineering credit: the SQLite schema, ``json_extract`` paths,
tool-echo / file-injection skip filters, and same-role merge originated in
@JakobSachs's PR #23 (``feat: add OpenCode SQLite session database
support``). This adapter rebuilds those same primitives on the RFC 002
contract so it can ship as a registered adapter rather than a normalize.py
branch.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

from ..convo_miner import chunk_exchanges, detect_convo_room
from ..config import normalize_wing_name
from . import transforms as _transforms
from .base import (
    AdapterClosedError,
    AdapterSchema,
    AuthRequiredError,  # noqa: F401  (re-exported error type used in docstrings)
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


# Default lookup order for the OpenCode SQLite store. Verified 2026-05-12 on
# opencode-ai 1.14.39 (Linux XDG path); the ``~/.opencode`` legacy location
# is kept for older macOS installs (see PR #23 thread).
_DEFAULT_DB_PATHS: Tuple[str, ...] = (
    "~/.local/share/opencode/opencode.db",
    "~/.opencode/opencode.db",
)


# Hall-detection helper: defer to convo_miner's cached lookup so an adapter
# instance never re-reads the palace config per drawer. Imported lazily because
# convo_miner module-load imports chromadb on some paths.
def _detect_hall(content: str) -> str:
    from ..convo_miner import _detect_hall_cached

    return _detect_hall_cached(content)


def _resolve_db(local_path: Optional[str] = None) -> str:
    """Resolve a concrete SQLite path for the OpenCode store.

    Order:
        1. ``local_path`` if it points at an existing file (caller chose).
        2. Each entry of :data:`_DEFAULT_DB_PATHS` in declaration order.

    Raises :class:`SourceNotFoundError` if no candidate resolves.
    """
    candidates: List[str] = []
    if local_path:
        candidates.append(local_path)
    candidates.extend(_DEFAULT_DB_PATHS)
    for raw in candidates:
        p = Path(raw).expanduser()
        if p.is_file():
            return str(p.resolve())
    raise SourceNotFoundError(
        f"No OpenCode SQLite database found (searched {candidates}). "
        f"Pass SourceRef(local_path=<path>) or place the file at one of the "
        f"default paths."
    )


def _build_source_bytes_per_session(
    conn: sqlite3.Connection, session_id: str
) -> str:
    """Return the canonical ``source bytes`` for one OpenCode session.

    The bytes are role-prefixed ``part.data`` JSON values, one per line, in
    ``(message.time_created, part.time_created)`` order. This is the shape
    the declared OpenCode transformations (``opencode_extract_text_parts``
    onward) consume; the conformance suite uses the same shape so the
    declared-transformation round-trip is exact.
    """
    rows = conn.execute(
        """
        SELECT
            json_extract(m.data, '$.role')  AS role,
            p.data                           AS part_data
        FROM message m
        JOIN part p ON p.message_id = m.id
        WHERE m.session_id = ?
        ORDER BY m.time_created, p.time_created
        """,
        (session_id,),
    ).fetchall()
    return "\n".join(f"{role or ''}\t{part}" for role, part in rows)


def _extract_session_messages(
    conn: sqlite3.Connection, session_id: str
) -> List[Tuple[str, str]]:
    """Walk ``part.data`` for one session and return merged ``(role, text)`` pairs.

    Applies the OpenCode-specific transformations in declaration order using
    the reference implementations in :mod:`mempalace.sources.transforms`.
    Returns the resulting list of ``(role, body)`` tuples ready for the
    exchange-format emit step.
    """
    raw = _build_source_bytes_per_session(conn, session_id)
    if not raw:
        return []
    pipeline = [
        _transforms.opencode_extract_text_parts,
        _transforms.opencode_skip_tool_echo,
        _transforms.opencode_skip_file_injection,
        _transforms.opencode_role_coerce,
        _transforms.opencode_same_role_merge,
    ]
    text = raw
    for step in pipeline:
        text = step(text)
    # Final state is ``role\tbody`` lines; split back into tuples.
    pairs: List[Tuple[str, str]] = []
    for line in text.split("\n"):
        role, sep, body = line.partition("\t")
        if not sep:
            continue
        pairs.append((role, body))
    return pairs


def _session_transcript(messages: List[Tuple[str, str]]) -> str:
    """Render merged ``(role, text)`` pairs as exchange-pair markdown.

    Uses the declared ``opencode_format_exchange`` transformation; emits
    ``> user-text`` blocks alternating with assistant blocks.
    """
    role_lines = "\n".join(f"{r}\t{b}" for r, b in messages)
    formatted = _transforms.opencode_format_exchange(role_lines)
    formatted = _transforms.newline_normalize(formatted)
    return _transforms.whitespace_trim(formatted)


def _utc_iso(ms: int) -> str:
    """Convert OpenCode millisecond-epoch to ISO-8601 UTC string."""
    return (
        datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def session_source_file(db_path: str, session_id: str) -> str:
    """Construct the stable per-session ``source_file`` identifier.

    Shape: ``opencode://<absolute-db-path>#session=<sid>``. Stable across
    re-ingests, used as the ChromaDB ``where={"source_file": ...}`` key and
    by ``is_current`` to look up existing drawers.
    """
    return f"opencode://{db_path}#session={session_id}"


class OpenCodeSourceAdapter(BaseSourceAdapter):
    """Mine OpenCode AI-coding-CLI sessions into the palace (RFC 002 §1)."""

    name = "opencode"
    adapter_version = "0.1.0"
    capabilities = frozenset(
        {
            "supports_incremental",
            "supports_structured_metadata",
            "requires_local_tool",  # SQLite is python-stdlib but the .db is opencode's
            "adapter_owns_routing",
        }
    )
    supported_modes = frozenset({"chunked_content"})
    declared_transformations = frozenset(
        {
            "opencode_extract_text_parts",
            "opencode_skip_tool_echo",
            "opencode_skip_file_injection",
            "opencode_role_coerce",
            "opencode_same_role_merge",
            "opencode_format_exchange",
            "newline_normalize",
            "whitespace_trim",
        }
    )
    default_privacy_class = "pii_potential"

    # Order of declared transformations as applied by the adapter. The
    # conformance suite walks this list in order, so it MUST mirror the
    # actual pipeline in ``_extract_session_messages`` + ``_session_transcript``.
    DECLARED_TRANSFORMATION_ORDER: Tuple[str, ...] = (
        "opencode_extract_text_parts",
        "opencode_skip_tool_echo",
        "opencode_skip_file_injection",
        "opencode_role_coerce",
        "opencode_same_role_merge",
        "opencode_format_exchange",
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
                    description="OpenCode session id (e.g. ses_a1b2c3...)",
                    indexed=True,
                ),
                "session_title": FieldSpec(
                    type="string",
                    required=False,
                    description="Session title as recorded by OpenCode",
                ),
                "project_dir": FieldSpec(
                    type="string",
                    required=True,
                    description="Absolute filesystem path where the session was started",
                    indexed=True,
                ),
                "session_created_at": FieldSpec(
                    type="string",
                    required=True,
                    description="ISO-8601 UTC of session creation (from time_created ms)",
                ),
                "message_count": FieldSpec(
                    type="int",
                    required=True,
                    description="Number of merged (role, text) exchange parts in the session",
                ),
                "extract_mode": FieldSpec(
                    type="string",
                    required=True,
                    description="Always 'exchange' for the OpenCode adapter in v0.1",
                ),
                "opencode_db_path": FieldSpec(
                    type="string",
                    required=True,
                    description="Absolute path of the OpenCode SQLite database the drawer was extracted from",
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
            raise AdapterClosedError("OpenCodeSourceAdapter is closed")
        db_path = _resolve_db(source.local_path)
        conn = sqlite3.connect(db_path)
        try:
            self._verify_schema(conn, db_path)
            sessions = conn.execute(
                """
                SELECT id, title, directory, time_created, time_updated
                FROM session
                ORDER BY time_created
                """
            ).fetchall()
            for sid, title, directory, time_created, time_updated in sessions:
                src_file = session_source_file(db_path, sid)
                # Yield the lazy-fetch metadata so core can short-circuit when
                # the session has not changed since the previous ingest.
                yield SourceItemMetadata(
                    source_file=src_file,
                    version=str(time_updated or time_created or 0),
                    size_hint=None,
                    route_hint=self._route_hint_for(directory, ""),
                )
                if palace.is_skip_requested():
                    continue

                messages = _extract_session_messages(conn, sid)
                if len(messages) < 2:
                    # Skip cancelled / single-turn sessions — matches PR #23
                    # behavior and convo_miner's general "skip too-small files"
                    # heuristic.
                    logger.debug(
                        "opencode adapter: skipping session %s (%d messages)",
                        sid,
                        len(messages),
                    )
                    continue

                transcript = _session_transcript(messages)
                if not transcript:
                    continue

                chunks = chunk_exchanges(transcript)
                if not chunks:
                    continue

                wing = self._wing_for(source, directory)
                room = detect_convo_room(transcript)
                created_iso = _utc_iso(time_created or 0)
                # Pre-compute once per session so every chunk of the same
                # session shares the same filed_at timestamp.
                filed_at = (
                    datetime.now(timezone.utc)
                    .replace(microsecond=0)
                    .isoformat()
                    .replace("+00:00", "Z")
                )
                session_version = str(time_updated or time_created or 0)
                for chunk in chunks:
                    content = chunk["content"]
                    chunk_index = int(chunk["chunk_index"])
                    metadata = {
                        # Universal §5.1 fields
                        "source_file": src_file,
                        "chunk_index": chunk_index,
                        "filed_at": filed_at,
                        "added_by": "opencode-adapter",
                        "wing": wing,
                        "room": room,
                        "hall": _detect_hall(content),
                        "ingest_mode": "chunked_content",
                        "extract_mode": "exchange",
                        "privacy_class": self.default_privacy_class,
                        # Adapter-declared fields (§5.2)
                        "session_id": sid,
                        "session_title": title or "",
                        "project_dir": directory or "",
                        "session_created_at": created_iso,
                        "message_count": len(messages),
                        "opencode_db_path": db_path,
                        # Required by is_current() for incremental ingest;
                        # mirrors the SourceItemMetadata.version yielded above.
                        "opencode_session_version": session_version,
                    }
                    yield DrawerRecord(
                        content=content,
                        source_file=src_file,
                        chunk_index=chunk_index,
                        metadata=metadata,
                        route_hint=RouteHint(wing=wing, room=room, hall=metadata["hall"]),
                    )
        finally:
            conn.close()

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
        # Existing palace drawers expose either ``session_created_at`` (ISO)
        # or an older opaque metadata blob. The cheapest stable comparison is
        # the raw ``time_updated`` ms-epoch we encoded into ``version`` — but
        # the palace stores ISO. Honor both: if a ``opencode_session_version``
        # field is present (future-proof), use it; otherwise fall back to the
        # ISO-vs-ISO comparison against ``session_created_at``.
        stored_version = existing_metadata.get("opencode_session_version")
        if stored_version is not None:
            return str(stored_version) == item.version
        # Fall back to "we have drawers for this source_file" → assume current.
        # Safer than a default of "always re-extract" because OpenCode session
        # rows are append-only: an existing drawer for a session_id means we
        # already mined the messages that existed at last extraction.
        return True

    def source_summary(self, *, source: SourceRef) -> SourceSummary:
        try:
            db_path = _resolve_db(source.local_path)
        except SourceNotFoundError:
            return SourceSummary(description="OpenCode database not found", item_count=0)
        conn = sqlite3.connect(db_path)
        try:
            self._verify_schema(conn, db_path)
            (count,) = conn.execute("SELECT COUNT(*) FROM session").fetchone()
        finally:
            conn.close()
        return SourceSummary(
            description=f"OpenCode database at {db_path}",
            item_count=int(count),
        )

    def close(self) -> None:
        self._closed = True

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _verify_schema(conn: sqlite3.Connection, db_path: str) -> None:
        """Confirm the SQLite has the tables the adapter relies on.

        ``json_extract`` is a SQLite JSON1 feature — usually built into
        modern Python/SQLite shipments but we sanity-check it once so we
        raise a clear error instead of an opaque OperationalError mid-fetch.
        """
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        required = {"session", "message", "part"}
        missing = required - tables
        if missing:
            raise SourceNotFoundError(
                f"OpenCode database at {db_path} is missing tables: {sorted(missing)}"
            )
        try:
            conn.execute("SELECT json_extract('{}', '$')").fetchone()
        except sqlite3.OperationalError as e:
            raise SourceNotFoundError(
                f"SQLite at {db_path} lacks JSON1 (json_extract) — upgrade SQLite/Python: {e}"
            ) from e

    def _wing_for(self, source: SourceRef, directory: Optional[str]) -> str:
        """Resolve the wing for a session, RFC 002 §2.5 precedence:

        1. Explicit ``options["wing"]`` from the SourceRef
        2. Project directory basename (the session's ``directory`` column)
        3. Adapter fallback: ``"opencode_general"``
        """
        explicit = (source.options or {}).get("wing")
        if explicit:
            return normalize_wing_name(str(explicit))
        if directory and directory != "/":
            base = Path(directory).name
            if base:
                return normalize_wing_name(base)
        return "opencode_general"

    def _route_hint_for(
        self, directory: Optional[str], content: str
    ) -> Optional[RouteHint]:
        wing = (
            normalize_wing_name(Path(directory).name)
            if directory and Path(directory).name
            else "opencode_general"
        )
        # Room is content-dependent so we leave it None at the lazy-fetch stage;
        # the eager DrawerRecord emit fills it in per chunk.
        return RouteHint(wing=wing, room=None, hall=None)


__all__ = [
    "OpenCodeSourceAdapter",
    "session_source_file",
]
