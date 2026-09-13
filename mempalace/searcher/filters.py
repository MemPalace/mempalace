# Loaded into mempalace.searcher via exec (see __init__.py). Not a standalone module.
if __name__ != "mempalace.searcher":
    raise ImportError(f"{__name__} is an implementation fragment; import mempalace.searcher")


def build_where_filter(wing: str = None, room: str = None, source_file: str = None) -> dict:
    """Build a ChromaDB where filter from optional wing/room/source_file.

    ChromaDB needs a ``$and`` only when ≥2 clauses are present; a single
    clause is returned bare and zero clauses yield an empty filter (#1815).
    """
    clauses = []
    if wing:
        clauses.append({"wing": wing})
    if room:
        clauses.append({"room": room})
    if source_file:
        clauses.append({"source_file": source_file})
    if not clauses:
        return {}
    if len(clauses) == 1:
        return clauses[0]
    return {"$and": clauses}


def _is_staged_metadata(metadata, committed_tokens=frozenset()) -> bool:
    """True for an unpublished conversation-mine physical generation."""
    if not isinstance(metadata, dict):
        return False
    value = metadata.get("mine_staged")
    # Chroma returns bools; the sqlite-only fallback reconstructs them from
    # ``int_value`` as 0/1.
    if not (value is True or value == 1):
        return False
    token = metadata.get("mine_generation_token")
    return not token or token not in committed_tokens


def _committed_generation_tokens(collection) -> frozenset[str]:
    """Read crash-atomic conversation generation markers from the drawer store."""
    try:
        result = collection.get(
            where={"mine_commit_marker": True},
            include=["metadatas"],
        )
        return frozenset(
            meta.get("mine_generation_commit")
            for meta in (result.get("metadatas") or [])
            if isinstance(meta, dict) and meta.get("mine_generation_commit")
        )
    except Exception:
        logger.warning("Could not read conversation generation commit markers", exc_info=True)
        return frozenset()


def _visible_drawer_where(where: dict, committed_tokens=frozenset()) -> dict:
    """Exclude staged rows in the backend before its top-K limit is applied."""
    committed = {"mine_staged": {"$ne": True}}
    visibility = committed
    if committed_tokens:
        visibility = {
            "$or": [
                committed,
                {"mine_generation_token": {"$in": sorted(committed_tokens)}},
            ]
        }
    if not where:
        return visibility
    clauses = where.get("$and") if isinstance(where, dict) else None
    if isinstance(clauses, list):
        return {"$and": [*clauses, visibility]}
    return {"$and": [where, visibility]}


def _extract_drawer_ids_from_closet(closet_doc: str) -> list:
    """Parse all `→drawer_id_a,drawer_id_b` pointers out of a closet document.

    Preserves order and dedupes.
    """
    seen: dict = {}
    for match in _CLOSET_DRAWER_REF_RE.findall(closet_doc):
        for did in match.split(","):
            did = did.strip()
            if did and did not in seen:
                seen[did] = None
    return list(seen.keys())


def _scoped_source_filter(source_file: str, parent_drawer_id=None) -> dict:
    """Build a Chroma ``where`` clause that scopes a query to ``source_file``,
    additionally constrained by ``parent_drawer_id`` when one is supplied.

    Two unrelated oversized ``tool_add_drawer`` writes (chunked path from
    #1539) can pass the same ``source_file`` (e.g. two pastes tagged
    ``"chat.log"``); each call stores its own ``parent_drawer_id`` group
    of chunks but the bare ``source_file`` filter pulls chunks from both
    groups as if they were siblings (#1580). When the matched chunk
    carries a ``parent_drawer_id`` the filter narrows to that logical
    group. Otherwise (pre-#1539 drawers, single-chunk writes, and
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


def _expand_with_neighbors(drawers_col, matched_doc: str, matched_meta: dict, radius: int = 1):
    """Expand a matched drawer with its ±radius sibling chunks in the same source file.

    Motivation — "drawer-grep context" feature: a closet hit returns one
    drawer, but the chunk boundary may clip mid-thought (e.g., the matched
    chunk says "here's a breakdown:" and the actual breakdown lives in the
    next chunk). Fetching the small neighborhood around the match gives
    callers enough context without forcing a follow-up ``get_drawer`` call.

    Returns a dict with:
        ``text``            combined chunks in chunk_index order
        ``drawer_index``    the matched chunk's index in the source file
        ``total_drawers``   total drawer count for the source file (or None)

    On any ChromaDB failure or missing metadata, falls back to returning the
    matched drawer alone so search never breaks because neighbor expansion
    failed.
    """
    src = matched_meta.get("source_file")
    chunk_idx = matched_meta.get("chunk_index")
    if not src or not isinstance(chunk_idx, int):
        return {"text": matched_doc, "drawer_index": chunk_idx, "total_drawers": None}

    # Narrow by ``parent_drawer_id`` when present so chunks from unrelated
    # logical drawers sharing ``source_file`` do not stitch (#1580). See
    # ``_scoped_source_filter`` for the contract.
    parent_id = matched_meta.get("parent_drawer_id")
    target_indexes = [chunk_idx + offset for offset in range(-radius, radius + 1)]
    neighbor_clauses = [
        {"source_file": src},
        {"chunk_index": {"$in": target_indexes}},
    ]
    if parent_id:
        neighbor_clauses.append({"parent_drawer_id": parent_id})
    try:
        neighbors = drawers_col.get(
            where={"$and": neighbor_clauses},
            include=["documents", "metadatas"],
        )
    except Exception:
        return {"text": matched_doc, "drawer_index": chunk_idx, "total_drawers": None}

    indexed_docs = []
    for doc, meta in zip(neighbors.documents, neighbors.metadatas):
        ci = meta.get("chunk_index")
        if isinstance(ci, int):
            indexed_docs.append((ci, doc))
    indexed_docs.sort(key=lambda pair: pair[0])

    if not indexed_docs:
        combined_text = matched_doc
    else:
        combined_text = "\n\n".join(doc for _, doc in indexed_docs)

    # Cheap total_drawers lookup. When ``parent_drawer_id`` is present the
    # count is scoped to that group so the returned number matches the
    # text the caller gets back. Without a parent id, the legacy
    # file-global count is preserved.
    total_drawers = None
    try:
        all_meta = drawers_col.get(
            where=_scoped_source_filter(src, parent_id),
            include=["metadatas"],
        )
        total_drawers = len(all_meta.ids) if all_meta.ids else None
    except Exception:
        logger.debug("total_drawers lookup failed for %s", src, exc_info=True)

    return {
        "text": combined_text,
        "drawer_index": chunk_idx,
        "total_drawers": total_drawers,
    }


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
    ``col.query()`` is too late (#1222 covers the MCP path via the module-level
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
