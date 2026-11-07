"""Read selected native sources verbatim; persist only operation metadata."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path

from .capture_client import CHUNK_BYTES, MAX_RECORD_BYTES, CaptureError, files
from .sync_protocol import (
    CURSOR_MESSAGE,
    MEMORY_FORMAT,
    MEMORY_IMPORT_SOURCE,
    PROBE_KEY_ALLOWLIST,
    TRANSCRIPT_SOURCES,
    native_identity,
    native_source,
)
from .sync_state import Op, body_digest, eligible

MEMORY_BYTES = 400_000


class SourceError(ValueError):
    """Source diagnostics never include paths or user content."""


class SourceTooLarge(SourceError):
    """A record exceeds the bounded upload size."""


class SliceConflict(SourceError):
    """An immutable pending operation no longer matches its source bytes."""


@dataclass(frozen=True)
class Source:
    source: str
    path: str
    native_id: str
    size: int


def _open(path):
    path = Path(path)
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise SourceError("Symlinked source refused")
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    )
    stream = os.fdopen(descriptor, "rb")
    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
        stream.close()
        raise SourceError("Source must be a regular file")
    return stream


def discover(config) -> list[Source]:
    """Enumerate capture identities so a caller can fetch missing server cursors."""
    result, seen = [], set()
    home = Path(config.get("home", Path.home()))
    defaults = {"claude-code": home / ".claude/projects", "codex": home / ".codex/sessions"}
    for selected in config.get("sources", []):
        source = selected["source"]
        if source == "cursor":
            raise SourceError(CURSOR_MESSAGE)
        if source not in TRANSCRIPT_SOURCES:
            raise SourceError("Unsupported transcript source")
        root = Path(selected.get("root") or defaults.get(source, "")).expanduser().absolute()
        if source == "t3" and not selected.get("root"):
            raise SourceError("Select an explicit native transcript directory")
        state = config.get("_state")
        try:
            paths = list(files(root))
        except (CaptureError, OSError):
            if state:
                state.set_status(source, root, "", "unreadable")
            continue
        for path in paths:
            try:
                info = path.stat()
                cached = state.cached_file(source, path, info) if state else None
                if cached is None:
                    with _open(path) as stream:
                        raw = stream.read(MAX_RECORD_BYTES)
                        info = os.fstat(stream.fileno())
                    underlying = native_source(source, "auto", raw.decode("utf-8", errors="ignore"))
                    identity = native_identity(raw, underlying)
                    if state:
                        state.cache_file(source, path, info, [underlying, identity])
                else:
                    underlying, identity = cached
                if identity is not None and (underlying, identity) not in seen:
                    seen.add((underlying, identity))
                    result.append(Source(underlying, str(path), identity, info.st_size))
            except (OSError, SourceError):
                if state:
                    state.set_status(source, path, "", "unreadable")
    return result


def _hash_prefix(stream, length):
    digest = hashlib.sha256()
    while length:
        raw = stream.read(min(length, CHUNK_BYTES))
        if not raw:
            raise SliceConflict("Source prefix changed; saved cursor kept")
        digest.update(raw)
        length -= len(raw)
    return digest.hexdigest()


def _capture_body(source, native_id, offset, prefix, raw, op_id):
    return {
        "source": source,
        "thread_id": native_id,
        "offset": offset,
        "prefix_sha256": prefix,
        "transcript": raw.decode("utf-8"),
        "client_op_id": op_id,
    }


def _memory_body(native_id, raw):
    return {
        "source": MEMORY_IMPORT_SOURCE,
        "threads": [
            {"thread_id": native_id, "transcript": raw.decode("utf-8"), "format": MEMORY_FORMAT}
        ],
    }


def body(op):
    """Reconstruct the exact request; appends after its immutable slice are allowed."""
    try:
        with _open(op.path) as stream:
            prefix = _hash_prefix(stream, op.offset)
            raw = stream.read(op.length)
        if len(raw) != op.length or hashlib.sha256(raw).hexdigest() != op.slice_sha256:
            raise SliceConflict("Pending source slice changed; operation kept")
        request = (
            _capture_body(op.source, op.native_id, op.offset, prefix, raw, op.op_id)
            if op.kind == "capture"
            else _memory_body(op.native_id, raw)
        )
        if body_digest(request) != op.body_sha256:
            raise SliceConflict("Pending request changed; operation kept")
        return request
    except (UnicodeError, SourceError) as error:
        if isinstance(error, SliceConflict):
            raise
        raise SliceConflict("Pending source unavailable; operation kept") from None


def _op(kind, source, path, native_id, offset, raw, prefix=None):
    op_id = uuid.uuid4().hex
    request = (
        _capture_body(source, native_id, offset, prefix, raw, op_id)
        if kind == "capture"
        else _memory_body(native_id, raw)
    )
    return Op(
        op_id,
        kind,
        source,
        str(path),
        native_id,
        offset,
        len(raw),
        hashlib.sha256(raw).hexdigest(),
        body_digest(request),
    )


def _capture_op(selected, state, target):
    cursor = state.cursor(selected.source, selected.path, selected.native_id)
    if cursor is None:
        return None
    with _open(selected.path) as stream:
        before = os.fstat(stream.fileno())
        offset = cursor["offset"]
        if offset > target or before.st_size < target:
            raise SliceConflict("Source shrank; saved cursor kept")
        if target == offset:
            return None
        prefix = _hash_prefix(stream, offset)
        if prefix != cursor["prefix_sha256"]:
            raise SliceConflict("Source prefix changed; saved cursor kept")
        raw = stream.read(min(target - offset, CHUNK_BYTES + MAX_RECORD_BYTES + 1))
        after = os.fstat(stream.fileno())
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        return None
    # A request ends on a complete physical record. Keep an unflushed UTF-8 tail local.
    end = raw.rfind(b"\n", 0, CHUNK_BYTES + MAX_RECORD_BYTES + 1) + 1
    if end == 0:
        if len(raw) > MAX_RECORD_BYTES:
            raise SourceTooLarge("Native record too large; use an explicit import")
        return None
    if any(len(line) > MAX_RECORD_BYTES for line in raw[:end].splitlines(keepends=True)):
        raise SourceTooLarge("Native record too large; use an explicit import")
    # Bound ordinary chunks, extending through at most one whole record.
    if len(raw) > CHUNK_BYTES:
        boundary = raw.find(b"\n", CHUNK_BYTES)
        if boundary != -1:
            end = boundary + 1
    return _op(
        "capture", selected.source, selected.path, selected.native_id, offset, raw[:end], prefix
    )


def _manifest_hashes(home, state=None):
    manifest = home / ".commonplace/onboarding.json"
    if not manifest.exists():
        return set()
    try:
        if any(p.is_symlink() for p in (manifest, *manifest.parents)):
            raise ValueError
        info = manifest.stat()
        cached = state.cached_file("manifest", manifest, info) if state else None
        if cached is not None:
            return set(cached)
        with _open(manifest) as stream:
            raw = stream.read(2 * 1024 * 1024 + 1)
            after = os.fstat(stream.fileno())
        if (info.st_size, info.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError
        entries = json.loads(raw)["files"]
        hashes = {row["original_sha256"] for row in entries.values() if "original_sha256" in row}
        if state:
            state.cache_file("manifest", manifest, info, sorted(hashes))
        return hashes
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise SourceError("Onboarding manifest unavailable; memory scan refused") from None


def _memory_ops(config, state, now, targets):
    """Legacy memory discovery is disabled before any filesystem access."""
    return iter(())


def scan(config, state, *, now, targets=None) -> list[Op]:
    """Queue immutable metadata, captures first, bounded by the cycle's file ends.

    Unknown cursors are skipped. Call discover(), fetch GET /api/capture for each
    State.cursor(...) == None, and store_cursor() before scanning. targets maps
    absolute path strings to byte lengths; config['targets'] is also supported.
    """
    targets = targets if targets is not None else config.get("targets")
    occupied = {(op.source, op.native_id) for op in state.outbox_all()}
    for selected in _discovered(config):
        if (selected.source, selected.native_id) in occupied:
            continue
        target = selected.size if targets is None else targets.get(selected.path)
        if target is None:
            continue
        try:
            op = _capture_op(selected, state, target)
        except (OSError, UnicodeError, SourceError) as error:
            status = (
                "conflict"
                if isinstance(error, SliceConflict)
                else ("too_large" if isinstance(error, SourceTooLarge) else "unreadable")
            )
            state.set_status(selected.source, selected.path, selected.native_id, status)
            continue
        if op:
            state.outbox_add(op)
            occupied.add((op.source, op.native_id))
    for op in _memory_ops(config, state, now, targets):
        if (op.source, op.native_id) not in occupied:
            state.outbox_add(op)
            occupied.add((op.source, op.native_id))
    return [op for op in state.outbox_pending() if eligible(op)]


def _discovered(config):
    return config["_discovered"] if "_discovered" in config else discover(config)


def snapshot_targets(config):
    """Freeze consented transcript byte ends without inspecting memory roots."""
    return {source.path: source.size for source in _discovered(config)}


def probe(path) -> dict:
    """Report JSON structure without values, filenames or unknown key names."""
    with _open(path) as stream:
        raw = stream.read(MAX_RECORD_BYTES + 1)
    if len(raw) > MAX_RECORD_BYTES:
        raise SourceError("Probe input too large")
    try:
        documents = [json.loads(raw)]
    except (ValueError, UnicodeError, RecursionError):
        try:
            documents = [json.loads(line) for line in raw.splitlines() if line.strip()]
        except (ValueError, UnicodeError, RecursionError):
            raise SourceError("Probe requires JSON or JSONL") from None
    keys, types = set(), set()
    pending = list(documents)
    while pending:
        value = pending.pop()
        types.add(type(value).__name__)
        if isinstance(value, dict):
            keys.update(
                key if key in PROBE_KEY_ALLOWLIST else hashlib.sha256(key.encode()).hexdigest()[:12]
                for key in value
            )
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    return {"records": len(documents), "keys": sorted(keys), "types": sorted(types)}
