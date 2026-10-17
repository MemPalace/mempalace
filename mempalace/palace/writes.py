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


def _clear_dropped_keys(collection, batch: list) -> list:
    """Send a key a re-filed drawer no longer has as ``None``, which removes it.

    Chroma keeps the metadata keys an upsert omits, so without this a drawer
    re-mined over its old id would keep, say, entities its new text dropped.
    Backends that replace the metadata need nothing.
    """
    if getattr(collection, "upsert_merges_metadata", False) is not True:
        return batch
    existing = collection.get(ids=[drawer_id for drawer_id, _, _ in batch], include=["metadatas"])
    old = dict(zip(existing.get("ids") or [], existing.get("metadatas") or []))
    return [
        (drawer_id, doc, {**dict.fromkeys(set(old.get(drawer_id) or {}) - set(meta)), **meta})
        for drawer_id, doc, meta in batch
    ]


def write_file_drawers(
    collection,
    source_file: str,
    drawers: list,
    *,
    extract_mode: Optional[str] = None,
    prune: bool = True,
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
    A pruning re-file deletes the file's summaries there, since they quote
    text the file no longer has.

    Returns the number of drawers written. Raises
    :class:`StaleDrawerPurgeFailed` when the stale drawers could not be
    removed; an error in :data:`MINE_ABORTS` (a dead backend) propagates as is.
    """
    if prune:
        total = len(drawers)
        drawers = [(i, doc, {**meta, "chunk_total": total + 1}) for i, doc, meta in drawers]
    written = []
    for start in range(0, len(drawers), DRAWER_UPSERT_BATCH_SIZE):
        batch = drawers[start : start + DRAWER_UPSERT_BATCH_SIZE]
        assert_no_collisions([(drawer_id, meta) for drawer_id, _, meta in batch], collection)
        batch = _clear_dropped_keys(collection, batch)
        collection.upsert(
            ids=[drawer_id for drawer_id, _, _ in batch],
            documents=[doc for _, doc, _ in batch],
            metadatas=[meta for _, _, meta in batch],
        )
        written += batch
    if not prune:
        return len(written)

    fresh = {drawer_id for drawer_id, _, _ in drawers}
    try:
        stale = [
            drawer_id
            for drawer_id in _source_file_delete_ids(collection, source_file, extract_mode)
            if drawer_id not in fresh
        ]
        if stale:
            collection.delete(ids=stale)
    except MINE_ABORTS:
        raise
    except Exception as exc:
        raise StaleDrawerPurgeFailed(
            f"stale-drawer purge failed for {source_file!r} ({exc!r}); the new drawers "
            "stay marked unfinished and the next mine retries"
        ) from exc
    if aaak_col is not None:
        try:
            purge_aaak_rows(aaak_col, source_file=source_file)
        except MINE_ABORTS:
            raise
        except Exception:
            logger.warning("AAAK purge failed for %s", source_file, exc_info=True)
    for start in range(0, len(written), DRAWER_UPSERT_BATCH_SIZE):
        batch = written[start : start + DRAWER_UPSERT_BATCH_SIZE]
        collection.update(
            ids=[drawer_id for drawer_id, _, _ in batch],
            metadatas=[{**meta, "chunk_total": total} for _, _, meta in batch],
        )
    return len(written)
