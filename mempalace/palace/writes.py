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


class PriorTextArchiveFailed(StaleDrawerPurgeFailed):
    """Conversation or diary text could not be journaled, so it was not replaced.

    ``write_file_drawers`` records the text a preserving re-file is about to
    overwrite or remove before it writes; when that append fails, nothing it
    would have destroyed is touched. Callers count it like a failed purge: the
    file stays unfinished and the next mine retries.
    """


def _source_file_delete_ids(collection, source_file: str, extract_mode: Optional[str]) -> list:
    """Collect drawer IDs for one source file in one extraction mode's scope.

    ``extract_mode=None`` is every drawer for the path. Legacy conversation
    drawers did not carry extract_mode; treat those as exchange-mode rows so
    schema rebuilds can still clean them up without deleting newer
    general-mode drawers for the same transcript.
    """
    ids: list = []
    offset = 0
    while True:
        batch = collection.get(
            where={"source_file": source_file},
            limit=1000,
            offset=offset,
            include=["metadatas"],
        )
        batch_ids = batch.get("ids") or []
        metadatas = batch.get("metadatas") or []
        for drawer_id, meta in zip(batch_ids, metadatas):
            if _metadata_matches_extract_mode(meta or {}, extract_mode):
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


def _split_unchanged(collection, batch: list, prior: Optional[dict] = None) -> tuple:
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

    ``prior``, when given, receives ``{drawer_id: (document, metadata)}`` for
    every drawer of the batch already stored, as read before any write.
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
    if prior is not None:
        prior.update(stored)
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


def _journal_rows(rows: list) -> list:
    """The ``(drawer_id, document, metadata)`` rows that hold text worth journaling."""
    from ..metadata_types import is_bookkeeping

    return [
        (drawer_id, doc, meta)
        for drawer_id, doc, meta in rows
        if isinstance(doc, str) and not is_bookkeeping(meta, drawer_id)
    ]


def _archive_prior_text(palace_path, source_file: str, rows: list, reason: str) -> None:
    """Journal the text a preserving re-file is about to overwrite or remove.

    Raises :class:`PriorTextArchiveFailed` when the journal cannot take it, so
    the caller writes nothing that would destroy that text.
    """
    rows = _journal_rows(rows)
    if not rows:
        return
    from ..revisions import append_drawer_revisions

    try:
        append_drawer_revisions(
            palace_path,
            [
                (drawer_id, doc, {**meta, "revision_reason": reason})
                for drawer_id, doc, meta in rows
            ],
        )
    except OSError as exc:
        raise PriorTextArchiveFailed(
            f"could not journal the prior text of {len(rows)} drawers of {source_file!r} "
            f"({exc!r}); nothing was replaced and the next mine retries"
        ) from exc


def _owned_rows(collection, ids: list, owner: str) -> tuple:
    """Split ``ids`` into the rows ``owner`` may remove and a count of the rest.

    Returns ``(owned, kept)``: ``owned`` is ``(drawer_id, document, metadata)``
    for each row :func:`mempalace.retention.owns` gives ``owner``; ``kept``
    counts the rows another producer filed under the same path.
    """
    from ..retention import owns

    owned, kept = [], 0
    for start in range(0, len(ids), DRAWER_UPSERT_BATCH_SIZE):
        got = collection.get(
            ids=ids[start : start + DRAWER_UPSERT_BATCH_SIZE],
            include=["documents", "metadatas"],
        )
        got_ids = got.get("ids") or []
        documents = got.get("documents") or [None] * len(got_ids)
        metadatas = got.get("metadatas") or [None] * len(got_ids)
        for drawer_id, doc, meta in zip(got_ids, documents, metadatas):
            if owns(owner, meta or {}):
                owned.append((drawer_id, doc, meta or {}))
            else:
                kept += 1
    return owned, kept


def write_file_drawers(
    collection,
    source_file: str,
    drawers: list,
    *,
    extract_mode: Optional[str] = None,
    prune: bool = True,
    aaak_col=None,
    owner: Optional[str] = None,
    palace_path: Optional[str] = None,
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

    ``owner`` names the miner filing (``project``, ``format``, ``convo`` or
    ``diary``; see :mod:`mempalace.retention`). A path is not an ownership
    scope: the prune removes only the rows that miner filed, and keeps a
    conversation, diary, manual or other producer's drawer stored under the
    same ``source_file``. A ``convo`` or ``diary`` re-file preserves memory:
    before it overwrites a drawer's text or removes a drawer, the prior text
    and metadata are journaled to the palace's ``drawer_revisions.jsonl``
    (``palace_path``), and if that append fails nothing is overwritten or
    removed. Without ``owner`` the prune takes every row in the scope and
    nothing is journaled.

    ``aaak_col`` is the palace's AAAK collection, if ``compress`` made one.
    A pruning re-file deletes the summaries of drawers it rewrote or removed,
    since they quote text the file no longer has; drawers whose text did not
    change keep theirs.

    Returns a :class:`FiledDrawers` count of the drawers written. Raises
    :class:`StaleDrawerPurgeFailed` when the stale drawers could not be
    removed and :class:`PriorTextArchiveFailed` when prior text could not be
    journaled; an error in :data:`MINE_ABORTS` (a dead backend) propagates
    as is.
    """
    from ..retention import OWNERS, PRESERVING_OWNERS

    if owner is not None and owner not in OWNERS:
        raise ValueError(f"unknown drawer owner {owner!r}")
    preserving = owner in PRESERVING_OWNERS
    if prune:
        total = len(drawers)
        drawers = [(i, doc, {**meta, "chunk_total": total + 1}) for i, doc, meta in drawers]
    written = []
    rewritten = []
    for start in range(0, len(drawers), DRAWER_UPSERT_BATCH_SIZE):
        batch = drawers[start : start + DRAWER_UPSERT_BATCH_SIZE]
        assert_no_collisions([(drawer_id, meta) for drawer_id, _, meta in batch], collection)
        prior: dict = {}
        to_embed, reuse, reuse_vectors = _split_unchanged(collection, batch, prior)
        if preserving:
            _archive_prior_text(
                palace_path,
                source_file,
                [
                    (drawer_id, *prior[drawer_id])
                    for drawer_id, doc, _ in to_embed
                    if drawer_id in prior and prior[drawer_id][0] != doc
                ],
                "remine_overwrite",
            )
        _upsert(collection, reuse, embeddings=reuse_vectors)
        _upsert(collection, to_embed)
        written += reuse + to_embed
        rewritten += [drawer_id for drawer_id, _, _ in to_embed]
    if not prune:
        return FiledDrawers(len(written))

    fresh = {drawer_id for drawer_id, _, _ in drawers}
    kept = 0
    try:
        stale = [
            drawer_id
            for drawer_id in _source_file_delete_ids(collection, source_file, extract_mode)
            if drawer_id not in fresh
        ]
        if stale and owner is not None:
            owned, kept = _owned_rows(collection, stale, owner)
            stale = [drawer_id for drawer_id, _, _ in owned]
            if preserving:
                _archive_prior_text(palace_path, source_file, owned, "remine_prune")
        if stale:
            collection.delete(ids=stale)
    except MINE_ABORTS:
        raise
    except PriorTextArchiveFailed:
        raise
    except Exception as exc:
        raise StaleDrawerPurgeFailed(
            f"stale-drawer purge failed for {source_file!r} ({exc!r}); the new drawers "
            "stay marked unfinished and the next mine retries"
        ) from exc
    if kept:
        logger.info(
            "kept %d drawers another producer filed under %r while re-filing it as %s",
            kept,
            source_file,
            owner,
        )
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
