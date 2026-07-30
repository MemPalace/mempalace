"""Validated, verbatim conversation exports produced by trusted operators.

The format is deliberately opt-in. A transcript is treated as normalized only
when an adjacent ``.meta.json`` sidecar exists and both files satisfy this
module's closed schema. Generic conversation imports never pass through here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA = "mempalace-normalized-conversation/v1"
SIDECAR_SUFFIX = ".meta.json"
MAX_SIDECAR_BYTES = 64 * 1024
MAX_TRANSCRIPT_BYTES = 128 * 1024 * 1024
MAX_ENVELOPE_BYTES = 16 * 1024
MAX_MESSAGES_PER_EXCHANGE = 64

_SIDECAR_FIELDS = {
    "schema",
    "room",
    "authored_from",
    "authored_to",
    "source_fingerprint",
    "transformations",
    "exporter_version",
    "hermes_profile",
    "hermes_session_id",
    "hermes_source",
}
_HALL_CATEGORY_ROOMS = {
    "advice",
    "decisions",
    "discoveries",
    "emotional_context",
    "events",
    "facts",
    "milestones",
    "preferences",
    "problems",
}
_ROOM_RE = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$")
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:[-_.][a-z0-9]+)*$")
_VERSION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+){1,3}(?:[-+][A-Za-z0-9.-]+)?$")
_FINGERPRINT_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_TRANSFORMATIONS_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*(?:;[a-z0-9]+(?:-[a-z0-9]+)*)*$")
_EXCHANGE_PREFIX = "<!-- mempalace-exchange "
_COVERAGE_SCHEMA = "mempalace-applied-coverage/v1"
_COVERAGE_FILENAME = "normalized-conversation-coverage.json"
_COVERAGE_FIELDS = {
    "schema",
    "wing",
    "applied_snapshot_at",
    "covered_sources",
    "authored_from",
    "authored_to",
    "accepted_session_count",
    "quarantine_count",
    "verified_profile_receipt",
    "source_version",
}


def coverage_receipt_json_schema() -> dict[str, Any]:
    """Return the MCP-compatible schema for one applied coverage receipt."""

    properties = {field: {"type": "string"} for field in sorted(_COVERAGE_FIELDS)}
    properties.update(
        {
            "schema": {"type": "string", "const": _COVERAGE_SCHEMA},
            "wing": {"type": "string", "pattern": r"^wing_[a-z0-9]+$"},
            "applied_snapshot_at": {"type": "string", "format": "date-time"},
            "authored_from": {"type": "string", "format": "date-time"},
            "authored_to": {"type": "string", "format": "date-time"},
            "covered_sources": {
                "type": "string",
                "pattern": (
                    r"^[a-z0-9]+(?:[-_.][a-z0-9]+)*"
                    r"(?:;[a-z0-9]+(?:[-_.][a-z0-9]+)*)*$"
                ),
            },
            "verified_profile_receipt": {
                "type": "string",
                "pattern": r"^sha256:[0-9a-f]{64}$",
            },
            "source_version": {
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": r"^[^/\\\u0000]+$",
            },
        }
    )
    for field in ("accepted_session_count", "quarantine_count"):
        properties[field] = {"type": "integer", "minimum": 0}
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": sorted(_COVERAGE_FIELDS),
    }


class NormalizedConversationError(ValueError):
    """Raised when an opt-in normalized conversation fails validation."""


@dataclass(frozen=True)
class NormalizedExchange:
    start: int
    stop: int
    authored_from: str
    authored_to: str
    message_from: str
    message_to: str
    message_ids: str
    message_count: int


@dataclass(frozen=True)
class NormalizedMetadata:
    schema: str
    room: str
    authored_from: str
    authored_to: str
    source_fingerprint: str
    transformations: str
    exporter_version: str
    hermes_profile: str
    hermes_session_id: str
    hermes_source: str


@dataclass(frozen=True)
class NormalizedConversation:
    transcript_path: Path
    sidecar_path: Path
    transcript: str
    metadata: NormalizedMetadata
    exchanges: tuple[NormalizedExchange, ...]
    source_version: str


@dataclass(frozen=True)
class NormalizedConversationProbe:
    """Validated metadata and composite identity without transcript parsing."""

    transcript_path: Path
    sidecar_path: Path
    metadata: NormalizedMetadata
    source_version: str


def sidecar_path_for(transcript_path: Path) -> Path:
    """Return the only sidecar path recognized for ``transcript_path``."""

    return transcript_path.with_name(transcript_path.name + SIDECAR_SUFFIX)


def has_normalized_sidecar(transcript_path: Path) -> bool:
    """Return whether the adjacent opt-in sidecar path exists.

    A symlink still returns true so the loader can reject it explicitly rather
    than silently falling back to generic parsing.
    """

    return os.path.lexists(sidecar_path_for(Path(transcript_path)))


def _parse_iso(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise NormalizedConversationError(f"{field} must be a non-empty ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise NormalizedConversationError(f"{field} must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise NormalizedConversationError(f"{field} must include a timezone")
    return parsed


def _safe_read(path: Path, root: Path, *, max_bytes: int, label: str) -> bytes:
    try:
        if path.is_symlink():
            raise NormalizedConversationError(f"{label} must not be a symlink")
        resolved_root = root.expanduser().resolve(strict=True)
        resolved_path = path.expanduser().resolve(strict=True)
        resolved_path.relative_to(resolved_root)
    except NormalizedConversationError:
        raise
    except (OSError, ValueError) as exc:
        raise NormalizedConversationError(f"{label} path must stay within source root") from exc

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise NormalizedConversationError(f"unable to open {label}") from exc
    try:
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise NormalizedConversationError(f"{label} must be a regular file")
        if file_stat.st_size > max_bytes:
            raise NormalizedConversationError(f"{label} exceeds maximum size")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise NormalizedConversationError(f"{label} exceeds maximum size")
        return raw
    finally:
        os.close(fd)


def _load_sidecar(sidecar_path: Path, root: Path) -> NormalizedMetadata:
    raw = _safe_read(sidecar_path, root, max_bytes=MAX_SIDECAR_BYTES, label="sidecar")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NormalizedConversationError("sidecar must be valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise NormalizedConversationError("sidecar must be a JSON object")
    if set(payload) != _SIDECAR_FIELDS:
        missing = sorted(_SIDECAR_FIELDS - set(payload))
        unknown = sorted(set(payload) - _SIDECAR_FIELDS)
        raise NormalizedConversationError(
            f"sidecar field mismatch; missing={missing}, unknown={unknown}"
        )
    if any(not isinstance(value, str) for value in payload.values()):
        raise NormalizedConversationError("sidecar fields must all be flat strings")
    if payload["schema"] != SCHEMA:
        raise NormalizedConversationError("unsupported normalized conversation schema")

    room = payload["room"]
    if not _ROOM_RE.fullmatch(room) or room in _HALL_CATEGORY_ROOMS:
        raise NormalizedConversationError("room must be a reviewed subject slug")
    authored_from = _parse_iso(payload["authored_from"], "authored_from")
    authored_to = _parse_iso(payload["authored_to"], "authored_to")
    if authored_from > authored_to:
        raise NormalizedConversationError("authored_from must not exceed authored_to")
    if not _FINGERPRINT_RE.fullmatch(payload["source_fingerprint"]):
        raise NormalizedConversationError("source_fingerprint must be sha256:<hex>")
    if not _TRANSFORMATIONS_RE.fullmatch(payload["transformations"]):
        raise NormalizedConversationError("transformations must be semicolon-delimited slugs")
    if not _VERSION_RE.fullmatch(payload["exporter_version"]):
        raise NormalizedConversationError("exporter_version must be a version string")
    for field in ("hermes_profile", "hermes_source"):
        if not _SLUG_RE.fullmatch(payload[field]):
            raise NormalizedConversationError(f"{field} must be a safe slug")
    session_id = payload["hermes_session_id"]
    if (
        not session_id
        or len(session_id) > 256
        or any(char in session_id for char in ("/", "\\", "\x00"))
        or ".." in session_id
    ):
        raise NormalizedConversationError("hermes_session_id is invalid")
    return NormalizedMetadata(**payload)


def _validate_messages(payload: Any) -> tuple[list[dict[str, str]], list[datetime]]:
    if not isinstance(payload, dict) or set(payload) != {"messages"}:
        raise NormalizedConversationError("exchange envelope must contain only messages")
    messages = payload["messages"]
    if not isinstance(messages, list) or not 2 <= len(messages) <= MAX_MESSAGES_PER_EXCHANGE:
        raise NormalizedConversationError(
            f"exchange messages must contain 2-{MAX_MESSAGES_PER_EXCHANGE} entries"
        )
    timestamps: list[datetime] = []
    identifiers: set[str] = set()
    for index, message in enumerate(messages):
        if not isinstance(message, dict) or set(message) != {"id", "role", "timestamp"}:
            raise NormalizedConversationError("each message provenance entry has invalid fields")
        if any(not isinstance(value, str) or not value for value in message.values()):
            raise NormalizedConversationError("message provenance values must be strings")
        identifier = message["id"]
        if (
            len(identifier) > 256
            or any(char in identifier for char in ("/", "\\", "\x00"))
            or ".." in identifier
        ):
            raise NormalizedConversationError("message id is invalid")
        if identifier in identifiers:
            raise NormalizedConversationError("message ids must be unique within an exchange")
        identifiers.add(identifier)
        role = message["role"]
        if role not in {"user", "assistant"}:
            raise NormalizedConversationError("message role must be user or assistant")
        if index == 0 and role != "user":
            raise NormalizedConversationError("first message in an exchange must be user")
        if index > 0 and role != "assistant":
            raise NormalizedConversationError("only the first exchange message may be user")
        timestamps.append(_parse_iso(message["timestamp"], "message timestamp"))
    return list(messages), timestamps


def _parse_exchanges(transcript: str) -> tuple[NormalizedExchange, ...]:
    starts = [match.start() for match in re.finditer(r"(?m)^<!-- mempalace-exchange ", transcript)]
    if not starts or starts[0] != 0:
        raise NormalizedConversationError("transcript must begin with an exchange envelope")

    exchanges: list[NormalizedExchange] = []
    transcript_message_ids: set[str] = set()
    for position, start in enumerate(starts):
        stop = starts[position + 1] if position + 1 < len(starts) else len(transcript)
        newline = transcript.find("\n", start, stop)
        envelope_stop = stop if newline == -1 else newline
        envelope_line = transcript[start:envelope_stop].rstrip("\r")
        if len(envelope_line.encode("utf-8")) > MAX_ENVELOPE_BYTES:
            raise NormalizedConversationError("exchange envelope is too large")
        if not envelope_line.startswith(_EXCHANGE_PREFIX) or not envelope_line.endswith(" -->"):
            raise NormalizedConversationError("exchange envelope is malformed")
        try:
            envelope = json.loads(envelope_line[len(_EXCHANGE_PREFIX) : -4])
        except json.JSONDecodeError as exc:
            raise NormalizedConversationError("exchange envelope JSON is invalid") from exc
        messages, timestamps = _validate_messages(envelope)
        exchange_message_ids = {message["id"] for message in messages}
        if transcript_message_ids & exchange_message_ids:
            raise NormalizedConversationError("message ids must be unique across the transcript")
        transcript_message_ids.update(exchange_message_ids)

        body_start = envelope_stop + (1 if newline != -1 else 0)
        body_line_stop = transcript.find("\n", body_start, stop)
        first_body_line = transcript[
            body_start : stop if body_line_stop == -1 else body_line_stop
        ].rstrip("\r")
        if body_start >= stop or not first_body_line.startswith(">"):
            raise NormalizedConversationError(
                "each exchange must have exactly one leading user marker"
            )

        minimum_timestamp = min(timestamps)
        maximum_timestamp = max(timestamps)
        authored_from = minimum_timestamp.isoformat()
        authored_to = maximum_timestamp.isoformat()
        # Keep the export's canonical spelling when the bound matches an
        # original timestamp, including its chosen UTC ``Z`` representation.
        for message, parsed in zip(messages, timestamps):
            if parsed == minimum_timestamp:
                authored_from = message["timestamp"]
                break
        for message, parsed in reversed(list(zip(messages, timestamps))):
            if parsed == maximum_timestamp:
                authored_to = message["timestamp"]
                break
        message_ids = [message["id"] for message in messages]
        exchanges.append(
            NormalizedExchange(
                start=start,
                stop=stop,
                authored_from=authored_from,
                authored_to=authored_to,
                message_from=message_ids[0],
                message_to=message_ids[-1],
                message_ids=";".join(message_ids),
                message_count=len(message_ids),
            )
        )
    return tuple(exchanges)


def _canonical_sidecar(metadata: NormalizedMetadata) -> bytes:
    return json.dumps(asdict(metadata), sort_keys=True, separators=(",", ":")).encode()


def _source_version(transcript_bytes: bytes, metadata: NormalizedMetadata) -> str:
    digest = hashlib.sha256(
        b"mempalace-normalized-conversation-source/v1\0"
        + transcript_bytes
        + b"\0"
        + _canonical_sidecar(metadata)
    ).hexdigest()
    return f"sha256:{digest}"


def probe_normalized_conversation(
    transcript_path: Path, source_root: Path, *, extract_mode: str
) -> NormalizedConversationProbe:
    """Validate a pair and compute identity without parsing its exchanges."""

    transcript_path = Path(transcript_path)
    source_root = Path(source_root)
    sidecar_path = sidecar_path_for(transcript_path)
    if extract_mode != "exchange":
        raise NormalizedConversationError(
            "normalized Hermes conversations require extract_mode=exchange; "
            "extract_mode=general is refused"
        )
    transcript_bytes = _safe_read(
        transcript_path,
        source_root,
        max_bytes=MAX_TRANSCRIPT_BYTES,
        label="transcript",
    )
    metadata = _load_sidecar(sidecar_path, source_root)
    return NormalizedConversationProbe(
        transcript_path=transcript_path.resolve(),
        sidecar_path=sidecar_path.resolve(),
        metadata=metadata,
        source_version=_source_version(transcript_bytes, metadata),
    )


def load_normalized_conversation(
    transcript_path: Path, source_root: Path, *, extract_mode: str
) -> NormalizedConversation:
    """Load and validate one normalized transcript/sidecar pair."""

    transcript_path = Path(transcript_path)
    source_root = Path(source_root)
    sidecar_path = sidecar_path_for(transcript_path)
    if extract_mode != "exchange":
        raise NormalizedConversationError(
            "normalized Hermes conversations require extract_mode=exchange; "
            "extract_mode=general is refused"
        )
    transcript_bytes = _safe_read(
        transcript_path,
        source_root,
        max_bytes=MAX_TRANSCRIPT_BYTES,
        label="transcript",
    )
    try:
        transcript = transcript_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise NormalizedConversationError("transcript must be valid UTF-8") from exc
    sidecar = _load_sidecar(sidecar_path, source_root)
    exchanges = _parse_exchanges(transcript)

    exchange_from = min(
        _parse_iso(item.authored_from, "exchange authored_from") for item in exchanges
    )
    exchange_to = max(_parse_iso(item.authored_to, "exchange authored_to") for item in exchanges)
    if exchange_from < _parse_iso(sidecar.authored_from, "authored_from"):
        raise NormalizedConversationError("exchange authored range precedes sidecar range")
    if exchange_to > _parse_iso(sidecar.authored_to, "authored_to"):
        raise NormalizedConversationError("exchange authored range exceeds sidecar range")

    return NormalizedConversation(
        transcript_path=transcript_path.resolve(),
        sidecar_path=sidecar_path.resolve(),
        transcript=transcript,
        metadata=sidecar,
        exchanges=exchanges,
        source_version=_source_version(transcript_bytes, sidecar),
    )


def iter_normalized_conversation_chunks(
    conversation: NormalizedConversation, *, chunk_size: int
) -> Iterator[dict[str, Any]]:
    """Yield bounded chunks without retaining copied transcript text."""

    if not isinstance(chunk_size, int) or isinstance(chunk_size, bool) or chunk_size <= 0:
        raise ValueError("chunk_size must be a positive integer")
    chunk_index = 0
    for exchange in conversation.exchanges:
        for offset in range(exchange.start, exchange.stop, chunk_size):
            yield {
                "content": conversation.transcript[
                    offset : min(offset + chunk_size, exchange.stop)
                ],
                "chunk_index": chunk_index,
                "authored_from": exchange.authored_from,
                "authored_to": exchange.authored_to,
                "message_from": exchange.message_from,
                "message_to": exchange.message_to,
                "message_ids": exchange.message_ids,
                "message_count": exchange.message_count,
            }
            chunk_index += 1


def chunk_normalized_conversation(
    conversation: NormalizedConversation, *, chunk_size: int
) -> list[dict[str, Any]]:
    """Return bounded chunks without changing normalized transcript bytes."""

    return list(iter_normalized_conversation_chunks(conversation, chunk_size=chunk_size))


def count_normalized_conversation_chunks(
    conversation: NormalizedConversation, *, chunk_size: int
) -> int:
    """Count the chunks the miner will emit without copying their text."""

    if not isinstance(chunk_size, int) or isinstance(chunk_size, bool) or chunk_size <= 0:
        raise ValueError("chunk_size must be a positive integer")
    return sum(
        (exchange.stop - exchange.start + chunk_size - 1) // chunk_size
        for exchange in conversation.exchanges
    )


def _validate_coverage_receipt(receipt: Any) -> dict[str, Any]:
    if not isinstance(receipt, dict) or set(receipt) != _COVERAGE_FIELDS:
        raise NormalizedConversationError("coverage receipt field mismatch")
    if receipt.get("schema") != _COVERAGE_SCHEMA:
        raise NormalizedConversationError("unsupported coverage receipt schema")
    wing = receipt.get("wing")
    if not isinstance(wing, str) or not re.fullmatch(r"wing_[a-z0-9]+", wing):
        raise NormalizedConversationError("coverage wing must be a safe wing slug")
    applied = _parse_iso(receipt.get("applied_snapshot_at"), "applied_snapshot_at")
    authored_from = _parse_iso(receipt.get("authored_from"), "authored_from")
    authored_to = _parse_iso(receipt.get("authored_to"), "authored_to")
    if authored_from > authored_to or applied < authored_to:
        raise NormalizedConversationError("coverage timestamp range is invalid")
    sources = receipt.get("covered_sources")
    if (
        not isinstance(sources, str)
        or not sources
        or any(not _SLUG_RE.fullmatch(source) for source in sources.split(";"))
        or len(set(sources.split(";"))) != len(sources.split(";"))
    ):
        raise NormalizedConversationError("covered_sources must be unique safe slugs")
    for field in ("accepted_session_count", "quarantine_count"):
        value = receipt.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise NormalizedConversationError(f"{field} must be a non-negative integer")
    verified = receipt.get("verified_profile_receipt")
    if not isinstance(verified, str) or not _FINGERPRINT_RE.fullmatch(verified):
        raise NormalizedConversationError("verified_profile_receipt must be sha256:<hex>")
    source_version = receipt.get("source_version")
    if (
        not isinstance(source_version, str)
        or not source_version
        or len(source_version) > 128
        or any(char in source_version for char in ("/", "\\", "\x00"))
    ):
        raise NormalizedConversationError("source_version is invalid")
    return dict(receipt)


def _coverage_path(palace_path: Path) -> Path:
    return Path(palace_path).expanduser().resolve() / _COVERAGE_FILENAME


def read_applied_coverage(palace_path: Path) -> dict[str, dict[str, Any]]:
    """Read the content-free applied coverage registry without changing it."""

    path = _coverage_path(palace_path)
    if not path.exists():
        return {}
    raw = _safe_read(
        path,
        path.parent,
        max_bytes=256 * 1024,
        label="coverage registry",
    )
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NormalizedConversationError("coverage registry is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise NormalizedConversationError("coverage registry must be an object")
    validated: dict[str, dict[str, Any]] = {}
    for wing, receipt in payload.items():
        checked = _validate_coverage_receipt(receipt)
        if wing != checked["wing"]:
            raise NormalizedConversationError("coverage registry wing key mismatch")
        validated[wing] = checked
    return validated


def commit_applied_coverage(
    palace_path: Path,
    receipt: dict[str, Any],
) -> dict[str, Any]:
    """Atomically advance one wing after an operator verified its full apply."""

    from .palace import mine_palace_lock

    palace = Path(palace_path).expanduser().resolve()
    if not palace.is_dir():
        raise NormalizedConversationError("palace path must already exist")
    checked = _validate_coverage_receipt(receipt)
    with mine_palace_lock(str(palace)):
        current = read_applied_coverage(palace)
        previous = current.get(checked["wing"])
        if previous == checked:
            return checked
        if previous is not None:
            previous_snapshot = _parse_iso(
                previous["applied_snapshot_at"], "previous applied_snapshot_at"
            )
            next_snapshot = _parse_iso(checked["applied_snapshot_at"], "applied_snapshot_at")
            if next_snapshot < previous_snapshot:
                raise NormalizedConversationError("coverage receipt must not move backward")
            if next_snapshot == previous_snapshot:
                raise NormalizedConversationError(
                    "coverage receipt conflicts with the existing snapshot"
                )
            if _parse_iso(checked["authored_to"], "authored_to") < _parse_iso(
                previous["authored_to"], "previous authored_to"
            ):
                raise NormalizedConversationError(
                    "coverage authored watermark must not move backward"
                )
        current[checked["wing"]] = checked
        encoded = (json.dumps(current, sort_keys=True, indent=2) + "\n").encode("utf-8")
        destination = _coverage_path(palace)

        fd, temporary_name = tempfile.mkstemp(
            prefix=".normalized-conversation-coverage.",
            suffix=".tmp",
            dir=palace,
        )
        temporary = Path(temporary_name)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb", closefd=False) as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.close(fd)
            fd = -1
            os.replace(temporary, destination)
            try:
                directory_fd = os.open(palace, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                # Windows and some filesystems reject directory descriptors.
                pass
        finally:
            if fd != -1:
                os.close(fd)
            temporary.unlink(missing_ok=True)
    return checked
