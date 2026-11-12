"""Conservative metadata-only visibility guard for conversation re-mines.

This guards listings against torn positional writes, not physical generation
staging, atomic publication, vector search, MCP get, or lexical/SQLite/BM25.
"""

from collections import defaultdict


def visible_conversation_rows(ids, metadatas):
    """Exclude incomplete per-source/mode conversation batches before pagination.

    A pruning re-mine stamps all new rows with ``chunk_total=N+1`` and
    only changes it to ``N`` after the write and prune succeed. During an
    interrupted positional rewrite, complete copy-on-write history rows are
    available, while mixed current rows must stay out of public listings.

    This is not an atomic publication protocol: only metadata-only listings
    using a consistent source snapshot are guarded. Older rows without the
    completion stamp retain their previous visibility.
    """
    rows = list(zip(ids, metadatas))
    groups = defaultdict(list)
    for index, (_, metadata) in enumerate(rows):
        meta = metadata or {}
        if meta.get("ingest_mode") != "convos" or meta.get("extract_mode") not in (
            "exchange",
            "general",
        ):
            continue
        if "chunk_total" not in meta:
            continue  # pre-completion-marker legacy records
        scope = (
            meta.get("source_file"),
            meta.get("extract_mode"),
            meta.get("source_mtime"),
            meta.get("filed_at"),
            meta.get("historical_version") is True,
        )
        groups[scope].append((index, meta))

    hidden = set()
    for group in groups.values():
        totals = {meta.get("chunk_total") for _, meta in group}
        indices = {meta.get("chunk_index") for _, meta in group}
        expected = next(iter(totals)) if len(totals) == 1 else None
        complete = (
            type(expected) is int
            and expected > 0
            and len(group) == expected
            and len(indices) == expected
            and all(type(i) is int and 0 <= i < expected for i in indices)
        )
        if not complete:
            hidden.update(index for index, _ in group)
    return [row for index, row in enumerate(rows) if index not in hidden]


def visible_conversation_candidate_ids(palace_path, collection_name, candidates):
    """Exclude unfinished stamped vector candidates using a source-wide snapshot.

    The entire source/mode is inspected, not just the top-K vector winners:
    otherwise a partly overwritten B generation could masquerade as complete.
    Unavailable/lagging metadata fails closed only for stamped conversation
    candidates. This does not constitute an atomic vector/SQLite read.
    """
    from .backends.chroma_sqlite import sqlite_list_id_metadata, sqlite_metadata_snapshot
    from .config import get_configured_collection_name

    pairs = [(row_id, meta or {}) for row_id, meta in candidates]
    stamped = [
        (row_id, meta)
        for row_id, meta in pairs
        if meta.get("ingest_mode") == "convos"
        and meta.get("extract_mode") in ("exchange", "general")
        and "chunk_total" in meta
    ]
    accepted = {row_id for row_id, _ in pairs} - {row_id for row_id, _ in stamped}
    if not stamped:
        return accepted

    sources = sorted(
        {
            meta.get("source_file")
            for _, meta in stamped
            if isinstance(meta.get("source_file"), str) and meta.get("source_file")
        }
    )
    if not sources:
        return accepted
    name = collection_name or get_configured_collection_name()
    with sqlite_metadata_snapshot(palace_path, name) as connection:
        if connection is None:
            return accepted
        snapshot = sqlite_list_id_metadata(
            palace_path, name, where={"source_file": {"$in": sources}}, _connection=connection
        )
        if snapshot is None:
            return accepted
        ids, metadatas = snapshot
        visible_ids = {row_id for row_id, _ in visible_conversation_rows(ids, metadatas)}
        current = dict(zip(ids, metadatas))
        generation_fields = (
            "ingest_mode",
            "source_file",
            "extract_mode",
            "source_mtime",
            "filed_at",
            "historical_version",
            "chunk_total",
            "chunk_index",
            # A concurrent same-ID metadata rewrite must not borrow an
            # unrelated wing/room scope from the validated SQLite row.
            "wing",
            "room",
        )
        for row_id, meta in stamped:
            persisted = current.get(row_id)
            if (
                row_id in visible_ids
                and persisted is not None
                and all(persisted.get(key) == meta.get(key) for key in generation_fields)
            ):
                accepted.add(row_id)
    return accepted
