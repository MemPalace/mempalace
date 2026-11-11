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
