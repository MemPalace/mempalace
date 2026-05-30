"""Bounded access to the drawers of a single source file (#1657).

Per-query search enrichment used to materialize *every* drawer of a source
file — once just to ``len()`` for a count, once to pick the best chunk for
neighbor hydration — with no limit, so cost scaled with the largest file in the
palace. This module owns source-file access behind two small functions: a count
that never pulls documents into memory, and a fetch with an explicit cap.
"""

from __future__ import annotations

# Upper bound on drawers materialized for one source-file hydration. Files
# larger than this still hydrate (from the first ``FETCH_LIMIT`` drawers); the
# cap only stops a pathologically large source from dragging unbounded content
# into memory on every query.
FETCH_LIMIT = 500


def count_drawers(col, source_file: str) -> int:
    """Return how many drawers share ``source_file`` without materializing them.

    Backed by ``COUNT(*)`` on the ChromaDB backend; falls back to a metadata-
    only scan on backends without the fast path.
    """
    return col.count_matching({"source_file": source_file})


def fetch_drawers(col, source_file: str, *, limit: int = FETCH_LIMIT):
    """Return up to ``limit`` drawers (documents + metadatas) for ``source_file``.

    Returns the backend's ``GetResult``. The bound is the point: callers that
    only need a best chunk plus neighbors must not pay for an unbounded file.
    """
    return col.get(
        where={"source_file": source_file},
        include=["documents", "metadatas"],
        limit=limit,
    )
