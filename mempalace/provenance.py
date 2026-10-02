"""Expose record filing provenance without guessing authorship or changing text."""

from typing import Mapping, Optional


_ORIGINS = frozenset({"project_file", "conversation", "agent_note", "diary", "unknown"})


def _writer(value: object) -> Optional[str]:
    # Keep the caller's label verbatim in machine-readable results. Display
    # surfaces can bound/escape it separately; this is not an authenticated ID.
    return value if isinstance(value, str) and value.strip() else None


def memory_provenance(metadata: Optional[Mapping] = None) -> dict:
    """Return ``added_by`` and ``origin`` for a matched drawer.

    New writers stamp ``origin`` explicitly. Older records are classified only
    from known producer markers: filenames and configurable writer names cannot
    distinguish mined files from agent notes. Ambiguous records stay unknown.
    This reads metadata only and does not backfill or mutate stored records.
    """
    meta = metadata or {}
    stamped = meta.get("origin")
    mode = meta.get("ingest_mode")
    extraction = meta.get("extract_mode")
    source = meta.get("source_file")
    has_source = isinstance(source, str) and bool(source.strip())
    version = meta.get("normalize_version")

    if isinstance(stamped, str) and stamped in _ORIGINS:
        origin = stamped
    elif "origin" in meta:
        # An unrecognized explicit producer must not acquire an origin from a
        # partial resemblance to legacy metadata.
        origin = "unknown"
    elif meta.get("type") == "diary_entry" or meta.get("source_session") == "daily_diary":
        origin = "diary"
    elif mode == "convos" or mode == "sweep":
        origin = "conversation"
    elif mode == "extract" and extraction == "format" and has_source:
        origin = "project_file"
    elif (
        not mode
        and not extraction
        and not meta.get("convo_chunker_version")
        and meta.get("room") != "_registry"
        and has_source
        and isinstance(version, int)
        and not isinstance(version, bool)
        and version > 0
    ):
        origin = "project_file"
    else:
        origin = "unknown"

    writer = _writer(meta.get("added_by"))
    if writer is None and origin == "diary":
        writer = _writer(meta.get("agent"))
    return {"added_by": writer, "origin": origin}
