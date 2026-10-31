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
    scope and silently deletes them on the very next re-mine.
    """
    if extract_mode is None:
        return True
    stored_mode = meta.get("extract_mode")
    if stored_mode == extract_mode:
        return True
    if stored_mode is not None:
        return False
    return extract_mode == "exchange" and meta.get("ingest_mode") in (None, "convos")


def _meta_is_current(meta: dict, extract_mode: Optional[str]) -> bool:
    """True when a drawer was filed by the current pipeline for its scope.

    Pre-v2 drawers have no ``normalize_version`` and count as stale. In a
    convo scope with a ``CHUNKER_VERSIONS`` entry a missing or older stamp
    is stale too, so a chunker fix re-mines conversation files without
    touching project files.
    """
    # An explicit null (or any non-number) is stale, never a TypeError that
    # aborts the whole prefetch: file_already_mined treats it as stale too.
    version = meta.get("normalize_version", 1)
    if not _is_plain_number(version) or version < NORMALIZE_VERSION:
        return False
    if extract_mode in CHUNKER_VERSIONS:
        key, current = CHUNKER_VERSIONS[extract_mode]
        chunker = meta.get(key, 1)
        return _is_plain_number(chunker) and chunker >= current
    return True


def _is_plain_number(value) -> bool:
    # NaN compares unequal to itself and would never prove a match.
    return isinstance(value, (int, float)) and value == value


def _is_comparable(meta: dict, extract_mode: Optional[str] = None) -> bool:
    """True when every value the mined-set checks compare is a plain number.

    A source with any other value is "unprovable": every check answers "not
    mined" for it, whatever order its rows come back in.
    """
    keys = ["normalize_version", "source_mtime", "chunk_total"]
    if extract_mode in CHUNKER_VERSIONS:
        keys.append(CHUNKER_VERSIONS[extract_mode][0])
    return all(meta.get(key) is None or _is_plain_number(meta[key]) for key in keys)


# Every metadata key the mined-set checks below read.
_MINED_SCAN_KEYS = (
    "source_file",
    "source_mtime",
    "chunk_total",
    "extract_mode",
    "ingest_mode",
    "normalize_version",
    "convo_chunker_version",
    "general_extractor_version",
    "wing",
)


class MinedSetUnavailable(RuntimeError):
    """The palace could not be read to learn which sources are already filed.

    Raised instead of returning a partial registry: an incomplete one reads as
    "not mined yet" and "no duplicate", which cannot be told apart from a
    complete answer, so a mine would re-file sources or write duplicates blind.
    """


# A file that fails is recorded and the mine moves on; these mean the palace
# or the run is unusable, so every miner aborts on them instead.
MINE_ABORTS = (BackendError, CollisionError, MineAlreadyRunning, MinedSetUnavailable)


def _fast_collection_metadata(collection, keys, require_key=None, equals=None):
    """One-pass metadata stream from backends that implement ``iter_metadata``.

    Paging ``get(limit, offset)`` costs a SQL ``OFFSET`` (or repeated scroll
    from offset 0) per page, which re-walks every skipped row, and ``count()``
    on a fresh Chroma client loads the whole HNSW index first: on a 360k-drawer
    palace one such scan took 138 s, and it grows with the square of the palace.
    """
    from ..backends.base import BaseCollection

    inner = collection._inner if isinstance(collection, EmbeddingCollection) else collection
    if isinstance(inner, BaseCollection) and hasattr(inner, "iter_metadata"):
        if equals is None:
            return inner.iter_metadata(keys, require_key=require_key)
        return inner.iter_metadata(keys, require_key=require_key, equals=equals)
    return None


def _scan_collection_metadata(collection, keys, absorb, reset) -> None:
    """Feed every drawer's metadata to ``absorb``: completely, or raise.

    The fast sqlite read goes first. If it fails partway, what it gathered is
    discarded through ``reset`` and the collection is paged instead. If paging
    fails too, :class:`MinedSetUnavailable` stops the caller.
    """
    from ..backends.chroma_sqlite import MetadataRecoveryPending

    fast = _fast_collection_metadata(collection, keys)
    if fast is not None:
        try:
            for meta in fast:
                absorb(meta)
            return
        except MetadataRecoveryPending:
            logger.debug("metadata recovery pending; paging through the collection instead")
            reset()
        except Exception:
            logger.warning(
                "sqlite metadata scan failed; paging through the collection instead",
                exc_info=True,
            )
            reset()
        finally:
            close = getattr(fast, "close", None)
            if close is not None:
                close()
    try:
        for meta in _paged_metadata(collection):
            absorb(meta)
    except Exception as exc:
        raise MinedSetUnavailable(
            "could not read which sources are already mined; stopping rather than "
            f"mining against an incomplete registry ({exc!r})"
        ) from exc


def _paged_metadata(collection):
    total = collection.count()
    offset = 0
    while offset < total:
        batch = collection.get(limit=1000, offset=offset, include=["metadatas"])
        yield from batch["metadatas"]
        if not batch["ids"]:
            break
        offset += len(batch["ids"])


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
        schema (triggers silent rebuild after a normalization upgrade), or,
        for a convo extract_mode in CHUNKER_VERSIONS, its chunker stamp is
        missing or older than the current revision
      - `check_mtime=True` and the file's mtime differs from the stored one

    When check_mtime=True (used by the project miner, and by the convo
    miner's in-lock recheck), also re-mines on content change. Conversation
    transcripts are NOT assumed immutable: a Claude Code session keeps
    appending to its own file while active, and /compact or /clear can
    rewrite one in place. The convo miner's bulk skip-check uses
    prefetch_mined_set()'s stored mtimes instead of calling this function
    per file (same mtime-aware decision, without the O(n) per-file query
    cost); this function's check_mtime=True path remains its per-file,
    lock-held race-condition recheck. The project miner skips a file
    without calling this only when prefetch_complete_mtimes() proves this
    would return True.

    When extract_mode is set (used by convo miner), idempotency is scoped to
    that extraction mode so exchange-mode and general-mode drawers can coexist
    for the same source transcript. Legacy drawers without extract_mode are
    treated as exchange-mode drawers.

    A drawer whose metadata carries ``chunk_total`` is only
    counted toward a match once its stored_mtime group has accumulated at
    least that many drawers -- guarding against a mid-file crash between
    upsert batches, where the surviving drawers share the current mtime
    (the file itself was never touched) but are short of the full set. A
    drawer with no ``chunk_total`` (legacy rows, or a single-shot
    ``add_drawer()`` call with no partial-batch risk) is trusted on its own,
    exactly as before. A group is complete once it holds the largest
    ``chunk_total`` any of its drawers names, the rule
    :func:`prefetch_mined_set` and :func:`prefetch_complete_mtimes` use too.

    Raises :class:`MinedSetUnavailable` when the palace cannot be read: a
    read error that answered False would re-file the source as unmined.
    """
    current_mtime = None
    if check_mtime:
        try:
            current_mtime = os.path.getmtime(source_file)
        except OSError:
            return False
    # A source can hold several stored groups, one per mining pass, so every
    # matching drawer is walked and the answer is decided after the walk:
    # get() ordering is undefined. Per stored_mtime group: [drawers seen,
    # largest chunk_total, whether some drawer has no chunk_total].
    groups: dict = {}
    unprovable = False
    offset = 0
    while True:
        try:
            results = collection.get(
                where={"source_file": source_file},
                limit=1000,
                offset=offset,
                include=["metadatas"],
            )
        except Exception as exc:
            raise MinedSetUnavailable(
                f"could not read whether {source_file} is already mined; stopping "
                f"rather than filing it again ({exc!r})"
            ) from exc
        ids = results.get("ids") or []
        for meta in results.get("metadatas") or []:
            meta = coerce_metadata_types(meta or {})
            if extract_mode is not None and not _metadata_matches_extract_mode(meta, extract_mode):
                continue
            if not _is_comparable(meta, extract_mode):
                unprovable = True
                continue
            if not _meta_is_current(meta, extract_mode):
                continue
            stored_mtime = meta.get("source_mtime")
            if check_mtime and (stored_mtime is None or abs(stored_mtime - current_mtime) >= 0.001):
                continue
            chunk_total = meta.get("chunk_total")
            entry = groups.setdefault(stored_mtime, [0, 0, False])
            entry[0] += 1
            if chunk_total is None:
                # No completion marker (legacy rows, sentinels): trusted.
                entry[2] = True
            else:
                entry[1] = max(entry[1], chunk_total)
        if not ids:
            break
        offset += len(ids)
    if unprovable:
        return False
    return any(untracked or seen >= largest for seen, largest, untracked in groups.values())


# Above this many candidate files, one filtered get costs more round trips
# than a full paginated scan, so the scoped path below stops paying off.
_PREFETCH_SCOPE_THRESHOLD = 50


def prefetch_mined_set(
    collection, extract_mode: Optional[str] = None, source_files: Optional[list] = None
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

    Completeness mirrors :func:`file_already_mined`'s ``chunk_total`` rule:
    a source that only has a mid-file partial (surviving drawers
    share the current mtime but are short of ``chunk_total``) is **omitted**
    from the result so the bulk skip path re-mines instead of permanently
    stranding the missing exchanges. Drawers with no ``chunk_total``
    (legacy rows, registry sentinels) are trusted on their own, as before.

    The convo miner walks thousands of transcript files; per-file
    `collection.get(where={"source_file": X})` costs ~2s on a 150k-drawer
    palace, making a 2000-file sweep take >1h of pure skip-checking. This
    helper drops that to a single paginated scan plus O(1) lookups.

    When `source_files` is given and holds at most `_PREFETCH_SCOPE_THRESHOLD`
    paths, the caller already knows the only source_file values that could
    possibly match, so a paged `where={"source_file": {"$in": ...}}` get()
    replaces the full-collection scan. On a one-file mine of a 660k-drawer
    palace that mtime scan was about 42% of the mine; the content-hash scan
    was the rest, and it stays unscoped because a stored hash can be several
    comma-joined values. Above the threshold, or when source_files is
    omitted, the behaviour is unchanged: a bulk sweep still benefits more
    from one full scan than from many filtered queries.
    """
    # Per source_file: per stored_mtime group -> [count, largest chunk_total,
    # whether some drawer has no chunk_total]. A source is only "mined" once
    # some group is complete, and never while any of its rows is unprovable.
    groups: dict[str, dict] = {}
    unprovable: set = set()

    def _reset():
        groups.clear()
        unprovable.clear()

    def _absorb(meta):
        meta = coerce_metadata_types(meta or {})
        src = meta.get("source_file")
        if not src:
            return
        if not _metadata_matches_extract_mode(meta, extract_mode):
            return
        if not _is_comparable(meta, extract_mode):
            unprovable.add(src)
            return
        if not _meta_is_current(meta, extract_mode):
            return
        stored_mtime = meta.get("source_mtime")
        mtime_key = float(stored_mtime) if stored_mtime is not None else None
        entry = groups.setdefault(src, {}).setdefault(mtime_key, [0, 0, False])
        entry[0] += 1
        chunk_total = meta.get("chunk_total")
        if chunk_total is None:
            entry[2] = True
        else:
            entry[1] = max(entry[1], chunk_total)

    def _scan_all():
        _scan_collection_metadata(collection, _MINED_SCAN_KEYS, _absorb, _reset)

    if source_files is not None and len(source_files) <= _PREFETCH_SCOPE_THRESHOLD:
        if source_files:
            try:
                offset = 0
                while True:
                    scoped = collection.get(
                        where={"source_file": {"$in": list(source_files)}},
                        include=["metadatas"],
                        limit=1000,
                        offset=offset,
                    )
                    ids = scoped.get("ids") or []
                    for meta in scoped.get("metadatas") or []:
                        _absorb(meta)
                    if len(ids) < 1000:
                        break
                    offset += len(ids)
            except Exception:
                logger.warning("prefetch_mined_set: scoped fetch failed; scanning")
                _reset()
                _scan_all()
    else:
        _scan_all()

    mined: dict[str, Optional[float]] = {}
    for src, by_mtime in groups.items():
        if src in unprovable:
            continue
        for mtime_key, (count, largest, untracked) in by_mtime.items():
            # Legacy / registry rows carry no completion marker: trusted.
            if untracked or count >= largest:
                mined[src] = mtime_key
                break
    return mined


# The keys file_already_mined() reads when called without an extract_mode.
_PROJECT_MINED_KEYS = ("source_file", "source_mtime", "chunk_total", "normalize_version")


def prefetch_complete_mtimes(collection, source_files) -> Optional[dict[str, tuple]]:
    """``source_file -> stored mtimes of its complete drawer groups``, in one
    sqlite pass, for the project miner's skip check.

    ``file_already_mined(collection, src, check_mtime=True)`` costs one
    ``get(where={"source_file": src})`` per file, ~0.1 s on a 1M-drawer
    palace, which made a no-op mine of a 1.4k-file project take minutes.
    A source is listed here only when its stored drawers prove that call
    would return True for a matching on-disk mtime: some ``source_mtime``
    group at the current ``normalize_version`` either has a drawer with no
    ``chunk_total`` or holds at least the largest ``chunk_total`` any of
    its drawers names. A source with a value that check could not compare
    (a non-numeric ``normalize_version``, ``source_mtime`` or
    ``chunk_total``) is left out, since there it returns False. A source
    missing from the result is not "not mined": the caller asks
    ``file_already_mined()`` as before.

    Only ``source_files`` are tracked. Returns ``None`` on backends without
    Chroma's sqlite stream, or when the stream fails, so the caller keeps
    the per-file check for every file.
    """
    wanted = set(source_files)
    from ..backends.sqlite_exact import SQLiteExactCollection

    inner = collection._inner if isinstance(collection, EmbeddingCollection) else collection
    scope = (
        {"source_file": wanted}
        if isinstance(inner, SQLiteExactCollection)
        and 1 <= len(wanted) <= 5000
        and all(isinstance(x, str) for x in wanted)
        else None
    )
    rows = _fast_collection_metadata(
        collection, _PROJECT_MINED_KEYS, require_key="source_file", equals=scope
    )
    if rows is None:
        return None
    # Per source_file: stored mtime -> [drawer count, largest chunk_total,
    # whether some drawer has no chunk_total].
    groups: dict[str, dict] = {}
    unprovable: set = set()
    from ..backends.chroma_sqlite import MetadataRecoveryPending

    try:
        for meta in rows:
            meta = coerce_metadata_types(meta or {})
            src = meta.get("source_file")
            if src not in wanted or src in unprovable:
                continue
            if not _is_comparable(meta) or not _is_plain_number(meta.get("normalize_version", 1)):
                unprovable.add(src)
                groups.pop(src, None)
                continue
            version = meta.get("normalize_version", 1)
            stored_mtime = meta.get("source_mtime")
            chunk_total = meta.get("chunk_total")
            if version < NORMALIZE_VERSION or stored_mtime is None:
                # file_already_mined() skips this drawer without counting it.
                continue
            entry = groups.setdefault(src, {}).setdefault(stored_mtime, [0, 0, False])
            entry[0] += 1
            if chunk_total is None:
                entry[2] = True
            else:
                entry[1] = max(entry[1], chunk_total)
    except MetadataRecoveryPending:
        logger.debug("metadata recovery pending; checking each file on its own")
        return None
    except Exception:
        logger.warning("sqlite metadata scan failed; checking each file on its own", exc_info=True)
        return None
    finally:
        close = getattr(rows, "close", None)
        if close is not None:
            close()
    complete: dict[str, tuple] = {}
    for src, by_mtime in groups.items():
        mtimes = tuple(
            float(mtime)
            for mtime, (count, largest, untracked) in by_mtime.items()
            if untracked or count >= largest
        )
        if mtimes:
            complete[src] = mtimes
    return complete
