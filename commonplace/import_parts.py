"""Opt-in native import parts using the common append-only thread transaction.

Prefix reconstruction intentionally reads accumulated history. This bounds HTTP
bodies, not server memory or total work for a growing session.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from io import StringIO

from .handoff.models import ThreadMessage
from .handoff.thread_store import ThreadConflict
from .handoff.transcripts import _record_format, parse

PART_KEYS = {
    "version",
    "session_bytes",
    "session_sha256",
    "index",
    "offset",
    "prefix_sha256",
    "sha256",
    "final",
}
_HASH = re.compile(r"[0-9a-f]{64}\Z")


def validate_part(part, raw):
    if not isinstance(part, dict) or part.keys() != PART_KEYS:
        raise ValueError("Invalid part descriptor")
    for key in ("version", "session_bytes", "index", "offset"):
        if type(part[key]) is not int or not 0 <= part[key] <= 2**63 - 1:
            raise ValueError("Invalid part counter")
    if part["version"] != 1 or part["session_bytes"] == 0:
        raise ValueError("Unsupported part version or size")
    for key in ("session_sha256", "prefix_sha256", "sha256"):
        if not isinstance(part[key], str) or not _HASH.fullmatch(part[key]):
            raise ValueError("Invalid part digest")
    end = part["offset"] + len(raw)
    if (
        not raw
        or end > part["session_bytes"]
        or type(part["final"]) is not bool
        or part["final"] != (end == part["session_bytes"])
        or hashlib.sha256(raw).hexdigest() != part["sha256"]
    ):
        raise ValueError("Invalid part bytes or finality")


def native_records(text, fmt, final):
    """Validate *all* physical records, without the snapshot live-tail tolerance."""
    if not final and not text.endswith(("\r", "\n")):
        raise ValueError("Part must end at a record boundary")
    recognized = False
    for raw in StringIO(text, newline=""):
        if raw.strip():
            recognized |= _record_format(json.loads(raw)) == fmt
    return recognized


def _marker(message, name):
    return (
        message.role == "system"
        and message.kind == "other"
        and not message.text
        and message.name == name
        and not message.call_id
        and not message.at
    )


def _state(previous, identity):
    """Verify the saved layout and exact raw prefix; legacy archives conflict."""
    digest = hashlib.sha256()
    offset = 0
    recognized = False
    parts = []
    position = 0
    complete = False
    while position < len(previous):
        if complete or position + 1 >= len(previous):
            raise ThreadConflict()
        archive, metadata = previous[position : position + 2]
        if not _marker(archive, "import.raw") or not _marker(metadata, "import.part"):
            raise ThreadConflict()
        meta = json.loads(metadata.raw)
        if meta.get("identity") != identity:
            raise ThreadConflict()
        part = meta["part"]
        raw = archive.raw.encode("utf-8")
        validate_part(part, raw)
        if (
            part["index"] != len(parts)
            or part["offset"] != offset
            or part["prefix_sha256"] != digest.hexdigest()
            or parts
            and parts[-1][1].endswith("\r")
            and archive.raw.startswith("\n")
        ):
            raise ThreadConflict()
        recognized |= native_records(archive.raw, identity["format"], part["final"])
        digest.update(raw)
        offset += len(raw)
        complete = part["final"]
        count = meta["message_count"]
        if (
            type(count) is not int
            or count < 0
            or position + 2 + count > len(previous)
            or meta["recognized"] is not recognized
            or meta["session_state"] != ("complete" if complete else "incomplete")
            or complete
            and (not recognized or digest.hexdigest() != identity["session_sha256"])
        ):
            raise ThreadConflict()
        parts.append((part, archive.raw))
        position += 2 + count
    return digest, offset, recognized, parts, complete


def append_part(store, workspace, source, thread_id, fmt, text, part, *, actor, maximum):
    if source not in ("claude-code", "codex", "t3") or fmt not in ("claude-code", "codex"):
        raise ValueError("Parts require an explicit native format")
    if source != "t3" and fmt != source:
        raise ValueError("Native format differs from source")
    if not isinstance(text, str):
        raise ValueError("Part must contain text")
    raw = text.encode("utf-8")
    validate_part(part, raw)
    recognized = native_records(text, fmt, part["final"])
    parsed = parse(text, fmt, require_recognized=False)
    identity = {
        "source": source,
        "thread_id": thread_id,
        "format": fmt,
        "version": part["version"],
        "session_bytes": part["session_bytes"],
        "session_sha256": part["session_sha256"],
    }
    with store._transaction():
        stored = store.by_source(workspace, f"import:{source}:{thread_id}")
        previous = store.messages(stored.id) if stored else []
        digest, offset, seen, saved, complete = _state(previous, identity)
        work = {"previous_rows": len(previous), "previous_source_bytes": offset, "readback_rows": 0}
        duplicate = part["index"] < len(saved)
        if duplicate:
            if saved[part["index"]] != (part, text):
                raise ThreadConflict()
        else:
            if (
                complete
                or part["index"] != len(saved)
                or part["offset"] != offset
                or part["prefix_sha256"] != digest.hexdigest()
                or saved
                and saved[-1][1].endswith("\r")
                and text.startswith("\n")
            ):
                raise ThreadConflict()
            digest.update(raw)
            offset += len(raw)
            seen |= recognized
            complete = part["final"]
            # Finality is validated before any write or cursor advancement.
            if complete and (not seen or digest.hexdigest() != part["session_sha256"]):
                raise ThreadConflict()
            metadata = json.dumps(
                {
                    "identity": identity,
                    "part": part,
                    "recognized": seen,
                    "session_state": "complete" if complete else "incomplete",
                    "message_count": len(parsed),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            start = len(previous)
            combined = previous + [
                ThreadMessage(start, "system", "other", "", text, name="import.raw"),
                ThreadMessage(start + 1, "system", "other", "", metadata, name="import.part"),
                *(replace(m, seq=start + 2 + i) for i, m in enumerate(parsed)),
            ]
            if stored:
                stored = store.append(stored.id, combined, max_storage_bytes=maximum)
            else:
                stored = store.store(
                    workspace,
                    f"import:{source}:{thread_id}",
                    combined,
                    actor=actor,
                    max_storage_bytes=maximum,
                )
            # Both formats must reconstruct the exact common prefix before the
            # outer transaction commits. Readback failure rolls back every row.
            readback = store.messages(stored.id)
            work["readback_rows"] = len(readback)
            if readback != combined:
                raise ValueError("Import readback failed")
        return {
            "status": "duplicate" if duplicate else "imported",
            "unit": "part",
            "stored_thread_id": stored.id,
            "format": fmt,
            "part": part,
            "source_bytes": len(raw),
            "messages": len(parsed),
            "offset": offset,
            "prefix_sha256": digest.hexdigest(),
            "next_index": len(saved) + (not duplicate),
            "session_state": "complete" if complete else "incomplete",
            "prefix_work": work,
        }
