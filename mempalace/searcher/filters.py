# Loaded into mempalace.searcher via exec (see __init__.py). Not a standalone module.
if __name__ != "mempalace.searcher":
    raise ImportError(f"{__name__} is an implementation fragment; import mempalace.searcher")


def _scoped_source_filter(source_file: str, parent_drawer_id=None) -> dict:
    """Build a Chroma ``where`` clause that scopes a query to ``source_file``,
    additionally constrained by ``parent_drawer_id`` when one is supplied.

    Two unrelated oversized ``tool_add_drawer`` writes (the chunked path)
    can pass the same ``source_file`` (e.g. two pastes tagged
    ``"chat.log"``); each call stores its own ``parent_drawer_id`` group
    of chunks but the bare ``source_file`` filter pulls chunks from both
    groups as if they were siblings. When the matched chunk
    carries a ``parent_drawer_id`` the filter narrows to that logical
    group. Otherwise (drawers without ``parent_drawer_id``, single-chunk writes, and
    ``diary_ingest`` drawers grouped by real file path) the original
    file-global shape is preserved. Mirrors the conditional-``$and``
    precedent in ``build_where_filter``.
    """
    if parent_drawer_id:
        return {
            "$and": [
                {"source_file": source_file},
                {"parent_drawer_id": parent_drawer_id},
            ]
        }
    return {"source_file": source_file}


def _warn_if_legacy_metric(col) -> None:
    """Print a one-line notice if the palace was created without
    ``hnsw:space=cosine``.

    ChromaDB's default is L2 (Euclidean), under which cosine-based
    similarity interpretation falls apart — distances routinely exceed
    1.0 and the display ``max(0, 1 - dist)`` floors every result to 0.
    Legacy palaces (mined before this metadata was consistently set)
    need ``mempalace repair`` to rebuild with the correct metric.

    The warning fires only for palaces that clearly have the wrong
    metric; palaces with no metadata table at all (empty dict) also
    fall under this check since that is the signal of a pre-metadata
    palace.
    """
    try:
        meta = getattr(col, "metadata", None)
    except Exception:
        return
    if not isinstance(meta, dict):
        return
    space = meta.get("hnsw:space")
    if space == "cosine":
        return
    # Either missing or set to something else — both are suspect.
    import sys as _sys

    detail = f"hnsw:space={space!r}" if space else "no hnsw:space metadata"
    print(
        f"\n  NOTICE: this palace was created without cosine distance ({detail}).\n"
        "          Semantic similarity scores will not be meaningful.\n"
        "          Run `mempalace repair` to rebuild the index with the correct metric.",
        file=_sys.stderr,
    )


def _hnsw_capacity_diverged(palace_path: str) -> bool:
    """Return True if HNSW divergence is severe enough to crash ChromaDB.

    Thin, exception-safe wrapper around
    :func:`mempalace.backends.chroma.hnsw_capacity_status`. Used by the
    CLI search path to short-circuit to the BM25-only fallback before
    opening a Chroma client. Client construction and collection identity
    checks can themselves touch the damaged index, so guarding only
    ``col.query()`` is too late (the MCP path is covered by the module-level
    ``_vector_disabled`` flag; this covers the CLI path).

    A probe that raises falls through to ``False`` so the caller proceeds
    to the normal vector path — the underlying query then either succeeds
    (probe was a false negative) or raises its own diagnostic error. The
    probe itself must never be the thing that crashes search.
    """
    try:
        from ..backends.chroma import hnsw_capacity_status
        from ..config import get_configured_collection_name

        info = hnsw_capacity_status(palace_path, get_configured_collection_name())
        return bool(info.get("diverged"))
    except Exception:
        logger.debug("HNSW capacity probe raised; proceeding to vector path", exc_info=True)
        return False
