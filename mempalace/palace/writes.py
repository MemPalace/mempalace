# Loaded into mempalace.palace via exec (see __init__.py). Not a standalone module.
if __name__ != "mempalace.palace":
    raise ImportError(f"{__name__} is an implementation fragment; import mempalace.palace")


# Drawers per upsert: most of the embedding batch speedup without one huge
# Chroma/SQLite request for a large file.
DRAWER_UPSERT_BATCH_SIZE = 1000


class StaleDrawerPurgeFailed(RuntimeError):
    """A file's new drawers are written but its stale ones could not be removed.

    The new drawers still carry their unfinished marker, so the file reads as
    not mined and the next mine finishes the job.
    """


def _source_file_delete_ids(
    collection,
    source_file: str,
    extract_mode: Optional[str],
    pass_contents: Optional[set] = None,
) -> list:
    """Collect drawer IDs for one source file in one extraction mode's scope.

    ``extract_mode=None`` is every drawer for the path. Legacy conversation
    drawers did not carry extract_mode; treat those as exchange-mode rows so
    schema rebuilds can still clean them up without deleting newer
    general-mode drawers for the same transcript.

    When ``pass_contents`` is a non-empty set of drawer text bodies produced
    by the current mine pass, the function applies additive-history filtering:
    a drawer is kept (excluded from the delete list) only if (a) its metadata
    matches the current schema version (_meta_is_current) AND (b) its document
    text is a non-empty string that is NOT in ``pass_contents``.  Drawers that
    fail either check are included in the list and will be deleted — these are
    the stale-schema drawers, re-derived drawers, and blank orphans the PR
    intends to remove.  When ``pass_contents`` is None, the function falls
    back to the original blanket behavior (every in-scope drawer is a delete
    candidate), preserving backward compatibility for all existing callers.
    """
    ids: list = []
    offset = 0
    include = ["metadatas", "documents"] if pass_contents else ["metadatas"]
    while True:
        batch = collection.get(
            where={"source_file": source_file},
            limit=1000,
            offset=offset,
            include=include,
        )
        batch_ids = batch.get("ids") or []
        metadatas = batch.get("metadatas") or []
        documents = batch.get("documents") or [None] * len(batch_ids)
        for drawer_id, meta, content in zip(batch_ids, metadatas, documents):
            if not _metadata_matches_extract_mode(meta or {}, extract_mode):
                continue
            if pass_contents is not None:
                # Additive-history rules:
                #   Rule 1 KEEP  : current schema + content not in pass_contents
                #   Rule 2 DEL   : older schema (normalize_version < current)
                #   Rule 3 DEL   : current schema + content IS in pass_contents
                #   Rule 4 DEL   : blank / empty document text
                if content and content in pass_contents:
                    ids.append(drawer_id)  # Rule 3: redundant, upsert replaces
                elif not content:
                    ids.append(drawer_id)  # Rule 4: blank orphan
                elif not _meta_is_current(meta or {}, extract_mode):
                    ids.append(drawer_id)  # Rule 2: stale schema
                # else Rule 1: keep — not added to ids
            else:
                ids.append(drawer_id)
        if not batch_ids:
            break
        offset += len(batch_ids)
    return ids


class FiledDrawers(int):
    """How many drawers :func:`write_file_drawers` wrote.

    ``unchanged`` is true for a pruning re-file that stored every drawer
    under its old id with its old text and removed nothing, so the file's
    closets and AAAK summaries still describe it.
    """

    unchanged = False


def _reusable_vector(vector, dimension: int) -> bool:
    return vector is not None and dimension > 0 and len(vector) == dimension


def _split_unchanged(collection, batch: list) -> tuple:
    """Split ``batch`` into rows to embed and rows whose stored vector still fits.

    A drawer stored under the same id with the same text and the same
    embedding title keeps its vector, so re-filing text that did not change
    embeds nothing. The vector must also have the current embedder's
    dimension: Milvus returns an empty one for a row without a vector, and
    ``set-embedder --force`` can leave vectors of another size behind.
    Returns ``(to_embed, reuse, reuse_vectors)``.

    Chroma keeps the metadata keys an upsert omits, so there a key the
    re-filed drawer no longer has is sent as ``None``, which removes it;
    without that a drawer re-mined over its old id would keep, say, entities
    its new text dropped. Backends that replace the metadata need nothing.

    Vectors are read only for drawers whose text is unchanged, so a first
    mine never reads one. Chroma persists HNSW in the background, and a
    vector read that lands mid-persist on a young segment fails with
    "Nothing found on disk"; reuse is only a saving, so such a failure
    re-embeds those drawers instead of failing the mine.
    """
    from ..backends.embedding_wrapper import _metadata_title

    existing = collection.get(
        ids=[drawer_id for drawer_id, _, _ in batch],
        include=["documents", "metadatas"],
    )
    ids = existing.get("ids") or []
    documents = existing.get("documents") or [None] * len(ids)
    metadatas = existing.get("metadatas") or [None] * len(ids)
    stored = {
        drawer_id: (doc, meta or {}) for drawer_id, doc, meta in zip(ids, documents, metadatas)
    }
    merges = getattr(collection, "upsert_merges_metadata", False) is True
    rows, unchanged = [], []
    for drawer_id, doc, meta in batch:
        old_doc, old_meta = stored.get(drawer_id, (None, {}))
        if merges:
            meta = {**dict.fromkeys(set(old_meta) - set(meta)), **meta}
        same = old_doc == doc and _metadata_title(old_meta) == _metadata_title(meta)
        rows.append((drawer_id, doc, meta, same))
        if same:
            unchanged.append(drawer_id)
    vectors = _stored_vectors(collection, unchanged)
    dimension = 0
    if vectors:
        from ..embedding import probe_dimension

        dimension = probe_dimension()
    to_embed, reuse, reuse_vectors = [], [], []
    for drawer_id, doc, meta, same in rows:
        vector = vectors.get(drawer_id)
        if same and _reusable_vector(vector, dimension):
            reuse.append((drawer_id, doc, meta))
            reuse_vectors.append(vector)
        else:
            to_embed.append((drawer_id, doc, meta))
    return to_embed, reuse, reuse_vectors


def _stored_vectors(collection, ids: list) -> dict:
    if not ids:
        return {}
    try:
        got = collection.get(ids=ids, include=["embeddings"])
    except Exception:
        logger.warning(
            "Could not read stored vectors; re-embedding %d drawers", len(ids), exc_info=True
        )
        return {}
    vectors = got.get("embeddings")
    if vectors is None:
        return {}
    return dict(zip(got.get("ids") or [], vectors))


def _upsert(collection, rows: list, embeddings=None) -> None:
    if not rows:
        return
    kwargs = {} if embeddings is None else {"embeddings": embeddings}
    collection.upsert(
        ids=[drawer_id for drawer_id, _, _ in rows],
        documents=[doc for _, doc, _ in rows],
        metadatas=[meta for _, _, meta in rows],
        **kwargs,
    )


def write_file_drawers(
    collection,
    source_file: str,
    drawers: list,
    *,
    extract_mode: Optional[str] = None,
    prune: bool = True,
    pass_contents: Optional[set] = None,
    aaak_col=None,
) -> int:
    """File one source file's drawers: write the new ones, then drop the stale.

    ``drawers`` is a list of ``(drawer_id, document, metadata)``. The caller
    holds ``mine_lock(source_file)`` and has already decided the file needs
    filing.

    With ``prune``, every new drawer is written with ``chunk_total`` one
    higher than the count, so ``file_already_mined`` reads the set as
    unfinished. The file's other drawers in the ``extract_mode`` scope (see
    :func:`_source_file_delete_ids`) are then deleted, and only after that is
    ``chunk_total`` set to the real count, as a metadata-only update. A crash
    anywhere leaves the old drawers or the new ones readable and the file
    marked unfinished, so the next mine redoes it.

    Without ``prune`` the drawers are written as given and nothing is removed:
    a skip marker that must not take the last good text with it.

    ``aaak_col`` is the palace's AAAK collection, if ``compress`` made one.
    A pruning re-file deletes the summaries of drawers it rewrote or removed,
    since they quote text the file no longer has; drawers whose text did not
    change keep theirs.

    Returns a :class:`FiledDrawers` count of the drawers written. Raises
    :class:`StaleDrawerPurgeFailed` when the stale drawers could not be
    removed; an error in :data:`MINE_ABORTS` (a dead backend) propagates as is.
    """
    if prune:
        total = len(drawers)
        drawers = [(i, doc, {**meta, "chunk_total": total + 1}) for i, doc, meta in drawers]
    written = []
    rewritten = []
    for start in range(0, len(drawers), DRAWER_UPSERT_BATCH_SIZE):
        batch = drawers[start : start + DRAWER_UPSERT_BATCH_SIZE]
        assert_no_collisions([(drawer_id, meta) for drawer_id, _, meta in batch], collection)
        to_embed, reuse, reuse_vectors = _split_unchanged(collection, batch)
        _upsert(collection, reuse, embeddings=reuse_vectors)
        _upsert(collection, to_embed)
        written += reuse + to_embed
        rewritten += [drawer_id for drawer_id, _, _ in to_embed]
    if not prune:
        return FiledDrawers(len(written))

    fresh = {drawer_id for drawer_id, _, _ in drawers}
    try:
        delete_candidates = _source_file_delete_ids(
            collection, source_file, extract_mode, pass_contents=pass_contents
        )
        stale = [drawer_id for drawer_id in delete_candidates if drawer_id not in fresh]
        if stale:
            collection.delete(ids=stale)
    except MINE_ABORTS:
        raise
    except Exception as exc:
        raise StaleDrawerPurgeFailed(
            f"stale-drawer purge failed for {source_file!r} ({exc!r}); the new drawers "
            "stay marked unfinished and the next mine retries"
        ) from exc
    if rewritten or stale:
        drawers_removed(None, aaak_ids=rewritten + stale, aaak_col=aaak_col, reraise=MINE_ABORTS)
    for start in range(0, len(written), DRAWER_UPSERT_BATCH_SIZE):
        batch = written[start : start + DRAWER_UPSERT_BATCH_SIZE]
        collection.update(
            ids=[drawer_id for drawer_id, _, _ in batch],
            metadatas=[{**meta, "chunk_total": total} for _, _, meta in batch],
        )
    filed = FiledDrawers(len(written))
    filed.unchanged = bool(written) and not rewritten and not stale
    return filed
