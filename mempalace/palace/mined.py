# Loaded into mempalace.palace via exec (see __init__.py). Not a standalone module.
if __name__ != "mempalace.palace":
    raise ImportError(f"{__name__} is an implementation fragment; import mempalace.palace")


def _metadata_matches_extract_mode(meta: dict, extract_mode: Optional[str]) -> bool:
    """Scope a drawer to a convo-miner extraction mode.

    A missing ``extract_mode`` is treated as a legacy exchange-mode row
    ONLY when the drawer is otherwise convo_miner's own (no ``ingest_mode``
    at all -- pre-``ingest_mode``-schema convo drawers -- or the convo
    miner's own ``"convos"`` tag). A drawer from a different producer that
    never set ``extract_mode`` because it was never meant to carry one --
    e.g. the sweeper's ``ingest_mode="sweep"`` rows -- must not match: the
    legacy-compat rule otherwise scoops every sweeper drawer for a shared
    transcript into convo_miner's default "exchange" purge/idempotency
    scope and silently deletes them on the very next re-mine (#104).
    """
    if extract_mode is None:
        return True
    stored_mode = meta.get("extract_mode")
    if stored_mode == extract_mode:
        return True
    if stored_mode is not None:
        return False
    return extract_mode == "exchange" and meta.get("ingest_mode") in (None, "convos")


def _source_mode_commit_key(meta: dict) -> Optional[tuple]:
    """``(source_file, extract_mode)`` for generation-commit filtering.

    Tokenless leftover rows are predecessors only for the same source and
    extraction mode that now has a committed generation token. Legacy convo
    drawers without ``extract_mode`` are exchange-mode, matching
    :func:`_metadata_matches_extract_mode`.
    """
    src = meta.get("source_file")
    if not src:
        return None
    mode = meta.get("extract_mode")
    if mode is None and meta.get("ingest_mode") in (None, "convos"):
        mode = "exchange"
    return (src, mode)


def file_already_mined(
    collection,
    source_file: str,
    check_mtime: bool = False,
    extract_mode: Optional[str] = None,
) -> bool:
    """Check if a file has already been filed in the palace.

    Returns False (so the file gets re-mined) when:
      - no drawers exist for this source_file
      - the stored `normalize_version` is missing or older than the current
        schema (triggers silent rebuild after a normalization upgrade)
      - `check_mtime=True` and the file's mtime differs from the stored one

    When check_mtime=True (used by the project miner, and by the convo
    miner's in-lock recheck), also re-mines on content change. Conversation
    transcripts are NOT assumed immutable: a Claude Code session keeps
    appending to its own file while active, and /compact or /clear can
    rewrite one in place. The convo miner's bulk skip-check uses
    prefetch_mined_set()'s stored mtimes instead of calling this function
    per file (same mtime-aware decision, without the O(n) per-file query
    cost); this function's check_mtime=True path remains its per-file,
    lock-held race-condition recheck.

    When extract_mode is set (used by convo miner), idempotency is scoped to
    that extraction mode so exchange-mode and general-mode drawers can coexist
    for the same source transcript. Legacy drawers without extract_mode are
    treated as exchange-mode drawers.

    A drawer whose metadata carries ``chunk_total`` (see #21) is only
    counted toward a match once its stored_mtime group has accumulated at
    least that many drawers -- guarding against a mid-file crash between
    upsert batches, where the surviving drawers share the current mtime
    (the file itself was never touched) but are short of the full set. A
    drawer with no ``chunk_total`` (legacy rows, or a single-shot
    ``add_drawer()`` call with no partial-batch risk) is trusted on its own,
    exactly as before.
    """
    try:
        if extract_mode is not None:
            from ..ids import make_convo_commit_id

            commit_marker = collection.get(
                ids=[make_convo_commit_id(source_file, extract_mode)],
                include=["metadatas"],
            )
            if any(
                (meta or {}).get("mine_cleanup_pending") is True
                for meta in (commit_marker.get("metadatas") or [])
            ):
                return False
        # Under the additive-mining model, a single ``source_file`` can have
        # multiple ``parent_drawer_id`` groups in the palace — one per
        # mining pass — each with its own stored ``source_mtime`` and
        # ``normalize_version``. The function must return True if ANY stored
        # group is current (matching version + matching mtime when checked),
        # because ChromaDB's ``get(..., limit=1)`` has undefined ordering
        # across multiple matching rows: a ``limit=1`` shortcut picks
        # whichever row ChromaDB orders first and only checks that one,
        # causing spurious re-mines whenever the stale group is returned.
        # Iterating via the same paginated pattern used in the
        # extract_mode-is-set branch lets the function short-circuit on the
        # first matching group regardless of ordering.
        current_mtime = os.path.getmtime(source_file) if check_mtime else None
        offset = 0
        # Tracks, per matching stored_mtime group, how many drawers have
        # been seen so far toward that group's own chunk_total (#21).
        group_counts: dict = {}
        while True:
            results = collection.get(
                where={"source_file": source_file},
                limit=1000,
                offset=offset,
                include=["metadatas"],
            )
            ids = results.get("ids") or []
            metadatas = results.get("metadatas") or []
            for meta in metadatas:
                meta = meta or {}
                if meta.get("mine_staged") is True:
                    continue
                # extract_mode scoping (was the existing ``else`` branch):
                if extract_mode is not None and not _metadata_matches_extract_mode(
                    meta, extract_mode
                ):
                    continue
                # Pre-v2 drawers have no version field — treat them as stale.
                stored_version = meta.get("normalize_version", 1)
                if stored_version < NORMALIZE_VERSION:
                    continue
                if not check_mtime:
                    return True
                stored_mtime = meta.get("source_mtime")
                if stored_mtime is None:
                    continue
                if abs(float(stored_mtime) - current_mtime) >= 0.001:
                    continue
                chunk_total = meta.get("chunk_total")
                if chunk_total is None:
                    # No completion marker on this drawer — can't verify
                    # completeness for its group, trust the match as before.
                    return True
                seen = group_counts.get(stored_mtime, 0) + 1
                group_counts[stored_mtime] = seen
                if seen >= chunk_total:
                    return True
            if not ids:
                break
            offset += len(ids)
        return False
    except Exception:
        return False


def prefetch_mined_set(
    collection, extract_mode: Optional[str] = None
) -> dict[str, Optional[float]]:
    """Pre-fetch source_file -> stored source_mtime for files already mined
    at the current NORMALIZE_VERSION, in one bulk pass instead of one
    ChromaDB query per file.

    Return type is a dict rather than a bare set so callers get mtime
    awareness "for free": conversation transcripts are not immutable once
    mined (a Claude Code session keeps appending to the same file while
    active, and /compact or /clear can rewrite one in place), so "we've
    seen this source_file before" is not sufficient to skip it -- the caller
    must also confirm its current on-disk mtime still matches what was
    stored. `if src in mined_set` still means the same thing as the old
    set-based return (dict `in` checks keys); a caller that wants staleness
    detection reads `mined_set[src]` and compares against
    os.path.getmtime(src) itself. `None` means either no mtime was ever
    stored (drawers written before this field existed) or getmtime failed
    when the drawer was written -- both should be treated as stale.

    When extract_mode is set, mirrors file_already_mined(..., extract_mode=...)
    so conversation mines skip per extraction mode rather than per source file.

    Completeness mirrors :func:`file_already_mined`'s ``chunk_total`` rule
    (#2183): a source that only has a mid-file partial (surviving drawers
    share the current mtime but are short of ``chunk_total``) is **omitted**
    from the result so the bulk skip path re-mines instead of permanently
    stranding the missing exchanges. Drawers with no ``chunk_total``
    (legacy rows, registry sentinels) are trusted on their own, as before.

    The convo miner walks thousands of transcript files; per-file
    `collection.get(where={"source_file": X})` costs ~2s on a 150k-drawer
    palace, making a 2000-file sweep take >1h of pure skip-checking. This
    helper drops that to a single paginated scan plus O(1) lookups.
    """
    # Per source_file: per stored_mtime group → count + optional chunk_total.
    # A source is only "mined" once some group is complete.
    groups: dict[str, dict] = {}
    pending_sources: set[str] = set()
    try:
        total = collection.count()
        offset = 0
        while offset < total:
            batch = collection.get(limit=1000, offset=offset, include=["metadatas"])
            for meta in batch["metadatas"]:
                meta = meta or {}
                if meta.get("mine_commit_marker") is True:
                    if (
                        meta.get("mine_cleanup_pending") is True
                        and meta.get("source_file")
                        and _metadata_matches_extract_mode(meta, extract_mode)
                    ):
                        pending_sources.add(meta["source_file"])
                    continue
                if meta.get("mine_staged") is True:
                    continue
                src = meta.get("source_file")
                if not src:
                    continue
                if not _metadata_matches_extract_mode(meta, extract_mode):
                    continue
                # Same default as file_already_mined: missing version == 1
                version = meta.get("normalize_version", 1)
                if version < NORMALIZE_VERSION:
                    continue
                stored_mtime = meta.get("source_mtime")
                mtime_key = float(stored_mtime) if stored_mtime is not None else None
                entry = groups.setdefault(src, {}).setdefault(
                    mtime_key, {"count": 0, "chunk_total": None}
                )
                entry["count"] += 1
                chunk_total = meta.get("chunk_total")
                if chunk_total is not None:
                    try:
                        entry["chunk_total"] = int(chunk_total)
                    except (TypeError, ValueError):
                        pass
            if not batch["ids"]:
                break
            offset += len(batch["ids"])
    except Exception:
        logger.warning("prefetch_mined_set: partial fetch, %d source groups loaded", len(groups))

    mined: dict[str, Optional[float]] = {}
    for src, by_mtime in groups.items():
        if src in pending_sources:
            continue
        for mtime_key, entry in by_mtime.items():
            chunk_total = entry["chunk_total"]
            if chunk_total is None:
                # Legacy / registry: no completion marker — trust membership.
                mined[src] = mtime_key
                break
            if entry["count"] >= chunk_total:
                mined[src] = mtime_key
                break
    return mined


def prefetch_content_hashes(
    collection, extract_mode: Optional[str] = None
) -> dict[tuple[str, str], str]:
    """Pre-fetch (wing, content_hash) -> source_file for drawers already
    filed at the current NORMALIZE_VERSION, in one bulk pass.

    Repeated exports from Claude/ChatGPT land under a new filename each run
    (timestamped bundle, regenerated slug, etc.) even when the conversation
    itself hasn't changed. `prefetch_mined_set` only recognizes a file as
    already-mined by its exact path, so the same conversation re-exported
    under a new path always looked "new" and got re-mined as a duplicate
    drawer. This does the same bulk scan but keyed on the SHA-256 of the
    normalized transcript text, so the convo miner can recognize "this exact
    conversation is already filed under a different path" and skip it.

    Keyed by (wing, content_hash) rather than content_hash alone — mining
    the same transcript into a second wing is a deliberate re-file, not a
    duplicate, and should produce real drawers in that wing rather than
    just the registry sentinel.

    A drawer's ``content_hash`` metadata may hold several comma-joined
    SHA-256 hashes: a privacy-export bundle normalizes to one conversation
    per drawer set, but the hash is computed per conversation so that a
    re-export with one new conversation added doesn't change the hash of
    the ones that didn't. Only the first source_file seen for a given
    (wing, hash) pair is kept — good enough to detect and skip a repeat,
    the point is not to track every alias.

    Commit markers are loaded first as a small set so unpublished
    generations can be filtered while drawer metadata is scanned in
    bounded pages. Pages are released after each batch; the full palace
    metadata is never retained.

    A source that later publishes a tokened generation can leave tokenless
    predecessor rows behind when stale-row deletion fails. Those leftovers
    are not visible once the source/mode commit marker names a token, so
    their hashes must not suppress a later file of the same transcript.
    """
    hashes: dict[tuple[str, str], str] = {}
    committed_tokens = set()
    tokened_source_modes = set()

    def _consider(meta):
        meta = meta or {}
        generation_token = meta.get("mine_generation_token")
        if meta.get("mine_staged") is True and generation_token not in committed_tokens:
            return
        if generation_token and generation_token not in committed_tokens:
            return
        content_hash_field = meta.get("content_hash")
        src = meta.get("source_file")
        wing = meta.get("wing")
        if not content_hash_field or not src or not wing:
            return
        if not generation_token:
            source_mode = _source_mode_commit_key(meta)
            if source_mode is not None and source_mode in tokened_source_modes:
                return
        if not _metadata_matches_extract_mode(meta, extract_mode):
            return
        if meta.get("normalize_version", 1) < NORMALIZE_VERSION:
            return
        for content_hash in content_hash_field.split(","):
            key = (wing, content_hash)
            if content_hash and key not in hashes:
                hashes[key] = src

    try:
        marker_offset = 0
        while True:
            marker_batch = collection.get(
                where={"mine_commit_marker": True},
                limit=1000,
                offset=marker_offset,
                include=["metadatas"],
            )
            marker_ids = marker_batch.get("ids") or []
            for meta in marker_batch.get("metadatas") or []:
                meta = meta or {}
                token = meta.get("mine_generation_commit")
                if meta.get("mine_commit_marker") is True and token:
                    committed_tokens.add(token)
                    source_mode = _source_mode_commit_key(meta)
                    if source_mode is not None:
                        tokened_source_modes.add(source_mode)
            del marker_batch
            if not marker_ids:
                break
            marker_offset += len(marker_ids)
            del marker_ids
    except Exception:
        logger.warning(
            "prefetch_content_hashes: marker fetch failed, %d commit tokens loaded",
            len(committed_tokens),
        )

    try:
        total = collection.count()
        offset = 0
        while offset < total:
            batch = collection.get(limit=1000, offset=offset, include=["metadatas"])
            ids = batch.get("ids") or []
            for meta in batch.get("metadatas") or []:
                _consider(meta)
            page_len = len(ids)
            del ids, batch
            if not page_len:
                break
            offset += page_len
    except Exception:
        logger.warning("prefetch_content_hashes: partial fetch, %d hashes loaded", len(hashes))
    return hashes
