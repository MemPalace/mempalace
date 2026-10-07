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


def _meta_is_current(meta: dict, extract_mode: Optional[str]) -> bool:
    """True when a drawer was filed by the current pipeline for its scope.

    Pre-v2 drawers have no ``normalize_version`` and count as stale. In the
    exchange scope a missing ``convo_chunker_version`` is stale too, so a
    chunker fix re-mines conversation files without touching project files.
    """
    if meta.get("normalize_version", 1) < NORMALIZE_VERSION:
        return False
    if extract_mode == "exchange":
        return meta.get("convo_chunker_version", 1) >= CONVO_CHUNKER_VERSION
    return True


# Every metadata key the mined-set checks below read.
_MINED_SCAN_KEYS = (
    "source_file",
    "source_mtime",
    "source_fingerprint",
    "chunk_total",
    "extract_mode",
    "ingest_mode",
    "normalize_version",
    "convo_chunker_version",
    "content_hash",
    "wing",
)


class MinedSetUnavailable(RuntimeError):
    """The palace could not be read to learn which sources are already filed.

    Raised instead of returning a partial registry: an incomplete one reads as
    "not mined yet" and "no duplicate", which cannot be told apart from a
    complete answer, so a mine would re-file sources or write duplicates blind.
    """


def _fast_collection_metadata(collection, keys, require_key=None, equals=None):
    """Chroma's one-pass sqlite metadata stream, or ``None`` for other backends.

    Paging ``get(limit, offset)`` costs a SQL ``OFFSET`` per page, which
    re-walks every skipped row, and ``count()`` on a fresh client loads the
    whole HNSW index first: on a 360k-drawer palace one such scan took 138 s,
    and it grows with the square of the palace.
    """
    from ..backends.chroma import ChromaCollection

    inner = collection._inner if isinstance(collection, EmbeddingCollection) else collection
    if isinstance(inner, ChromaCollection):
        if equals is None:
            return inner.iter_metadata(keys, require_key=require_key)
        return inner.iter_metadata(keys, require_key=require_key, equals=equals)
    return None


def _scan_collection_metadata(collection, keys, absorb, reset, require_key=None) -> None:
    """Feed every drawer's metadata to ``absorb``: completely, or raise.

    The fast sqlite read goes first. If it fails partway, what it gathered is
    discarded through ``reset`` and the collection is paged instead, where
    ``require_key`` is only a hint the caller re-checks. If paging fails too,
    :class:`MinedSetUnavailable` stops the caller.
    """
    fast = _fast_collection_metadata(collection, keys, require_key)
    if fast is not None:
        try:
            for meta in fast:
                absorb(meta)
            return
        except Exception:
            logger.warning(
                "sqlite metadata scan failed; paging through the collection instead",
                exc_info=True,
            )
            reset()
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
    check_source_fingerprint: bool = False,
) -> bool:
    """Check if a file has already been filed in the palace.

    Returns False (so the file gets re-mined) when:
      - no drawers exist for this source_file
      - the stored `normalize_version` is missing or older than the current
        schema (triggers silent rebuild after a normalization upgrade), or,
        for extract_mode="exchange", the stored `convo_chunker_version` is
        missing or older than CONVO_CHUNKER_VERSION
      - `check_mtime=True` and the file's mtime differs from the stored one

    With check_source_fingerprint=True, the currentness check requires an
    exact source fingerprint instead of the legacy mtime tolerance. Rows
    without it are re-mined once to record a verified source snapshot.

    The project miner uses check_mtime=True, skipping this call only when
    prefetch_complete_mtimes() proves it would return True for the file.
    The conversation miner uses check_source_fingerprint=True for this
    lock-held recheck and requests the same fingerprints from
    prefetch_mined_set() for its bulk skip-check.

    When extract_mode is set (used by convo miner), idempotency is scoped to
    that extraction mode so exchange-mode and general-mode drawers can coexist
    for the same source transcript. Legacy drawers without extract_mode are
    treated as exchange-mode drawers.

    A drawer whose metadata carries ``chunk_total`` (see #21) is only
    counted toward a match once its source-state group has accumulated at
    least that many drawers -- guarding against a mid-file crash between
    upsert batches, where the surviving drawers share the current source state
    (the file itself was never touched) but are short of the full set. A
    drawer with no ``chunk_total`` (legacy rows, or a single-shot
    ``add_drawer()`` call with no partial-batch risk) is trusted on its own,
    exactly as before.
    """
    try:
        # Under the additive-mining model, a single ``source_file`` can have
        # multiple ``parent_drawer_id`` groups in the palace — one per
        # mining pass — each with its own stored source state and
        # ``normalize_version``. The function must return True if ANY stored
        # group is current (matching version + matching source state when checked),
        # because ChromaDB's ``get(..., limit=1)`` has undefined ordering
        # across multiple matching rows: a ``limit=1`` shortcut picks
        # whichever row ChromaDB orders first and only checks that one,
        # causing spurious re-mines whenever the stale group is returned.
        # Iterating via the same paginated pattern used in the
        # extract_mode-is-set branch lets the function short-circuit on the
        # first matching group regardless of ordering.
        check_mtime = check_mtime or check_source_fingerprint
        current_mtime = (
            os.path.getmtime(source_file) if check_mtime and not check_source_fingerprint else None
        )
        current_fingerprint = (
            source_fingerprint(os.stat(source_file))
            if check_mtime and check_source_fingerprint
            else None
        )
        offset = 0
        # Tracks, per matching source-state group, how many drawers have
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
                # extract_mode scoping (was the existing ``else`` branch):
                if extract_mode is not None and not _metadata_matches_extract_mode(
                    meta, extract_mode
                ):
                    continue
                if not _meta_is_current(meta, extract_mode):
                    continue
                if not check_mtime:
                    return True
                stored_mtime = meta.get("source_mtime")
                if check_source_fingerprint:
                    if meta.get("source_fingerprint") != current_fingerprint:
                        continue
                else:
                    if stored_mtime is None:
                        continue
                    if abs(float(stored_mtime) - current_mtime) >= 0.001:
                        continue
                chunk_total = meta.get("chunk_total")
                if chunk_total is None:
                    # No completion marker on this drawer — can't verify
                    # completeness for its group, trust the match as before.
                    return True
                group_key = (
                    meta.get("source_fingerprint") if check_source_fingerprint else stored_mtime
                )
                seen = group_counts.get(group_key, 0) + 1
                group_counts[group_key] = seen
                if seen >= chunk_total:
                    return True
            if not ids:
                break
            offset += len(ids)
        return False
    except Exception:
        return False


# Above this many candidate files, one filtered get costs more round trips
# than a full paginated scan, so the scoped path below stops paying off.
_PREFETCH_SCOPE_THRESHOLD = 50


def prefetch_mined_set(
    collection,
    extract_mode: Optional[str] = None,
    source_files: Optional[list] = None,
    *,
    source_fingerprints: bool = False,
) -> dict[str, Optional[Union[float, str]]]:
    """Pre-fetch source_file -> stored source state for files already mined
    at the current NORMALIZE_VERSION, in one bulk pass instead of one
    ChromaDB query per file.

    The default values are stored mtimes. Callers compare each value with
    os.path.getmtime(src); membership alone does not establish freshness.
    None means no verified state was stored and must be treated as stale.

    With source_fingerprints=True, values and completion groups use exact
    source fingerprints instead. Missing fingerprints return None so callers
    re-mine legacy files rather than trusting a possibly stale mtime.

    When extract_mode is set, mirrors file_already_mined(..., extract_mode=...)
    so conversation mines skip per extraction mode rather than per source file.

    Completeness mirrors :func:`file_already_mined`'s ``chunk_total`` rule
    (#2183): a source that only has a mid-file partial (surviving drawers
    share the current source state but are short of ``chunk_total``) is **omitted**
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
    # Per source_file: per source-state group → count + optional chunk_total.
    # A source is only "mined" once some group is complete.
    groups: dict[str, dict] = {}

    def _absorb(meta):
        meta = meta or {}
        src = meta.get("source_file")
        if not src:
            return
        if not _metadata_matches_extract_mode(meta, extract_mode):
            return
        if not _meta_is_current(meta, extract_mode):
            return
        if source_fingerprints:
            state_key = meta.get("source_fingerprint")
        else:
            stored_mtime = meta.get("source_mtime")
            state_key = float(stored_mtime) if stored_mtime is not None else None
        entry = groups.setdefault(src, {}).setdefault(state_key, {"count": 0, "chunk_total": None})
        entry["count"] += 1
        chunk_total = meta.get("chunk_total")
        if chunk_total is not None:
            try:
                entry["chunk_total"] = int(chunk_total)
            except (TypeError, ValueError):
                pass

    def _scan_all():
        _scan_collection_metadata(collection, _MINED_SCAN_KEYS, _absorb, groups.clear)

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
                groups.clear()
                _scan_all()
    else:
        _scan_all()

    mined: dict[str, Optional[Union[float, str]]] = {}
    for src, by_state in groups.items():
        for state_key, entry in by_state.items():
            chunk_total = entry["chunk_total"]
            if chunk_total is None:
                # Legacy / registry: no completion marker — trust membership.
                mined[src] = state_key
                break
            if entry["count"] >= chunk_total:
                mined[src] = state_key
                break
    return mined


# The keys file_already_mined() reads when called without an extract_mode.
_PROJECT_MINED_KEYS = ("source_file", "source_mtime", "chunk_total", "normalize_version")


def _is_plain_number(value) -> bool:
    # NaN compares unequal to itself and would never prove a match.
    return isinstance(value, (int, float)) and value == value


def prefetch_complete_mtimes(collection, source_files) -> Optional[dict[str, tuple]]:
    """``source_file -> stored mtimes of its complete drawer groups``, in one
    sqlite pass, for the project miner's skip check (#2684).

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
    rows = _fast_collection_metadata(collection, _PROJECT_MINED_KEYS, require_key="source_file")
    if rows is None:
        return None
    wanted = set(source_files)
    # Per source_file: stored mtime -> [drawer count, largest chunk_total,
    # whether some drawer has no chunk_total].
    groups: dict[str, dict] = {}
    unprovable: set = set()
    try:
        for meta in rows:
            meta = meta or {}
            src = meta.get("source_file")
            if src not in wanted or src in unprovable:
                continue
            version = meta.get("normalize_version", 1)
            stored_mtime = meta.get("source_mtime")
            chunk_total = meta.get("chunk_total")
            if not (
                _is_plain_number(version)
                and (stored_mtime is None or _is_plain_number(stored_mtime))
                and (chunk_total is None or _is_plain_number(chunk_total))
            ):
                unprovable.add(src)
                groups.pop(src, None)
                continue
            if version < NORMALIZE_VERSION or stored_mtime is None:
                # file_already_mined() skips this drawer without counting it.
                continue
            entry = groups.setdefault(src, {}).setdefault(stored_mtime, [0, 0, False])
            entry[0] += 1
            if chunk_total is None:
                entry[2] = True
            else:
                entry[1] = max(entry[1], chunk_total)
    except Exception:
        logger.warning("sqlite metadata scan failed; checking each file on its own", exc_info=True)
        return None
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

    Deliberately NOT scoped by candidate source_files the way
    prefetch_mined_set is: a stored `content_hash` can hold several
    comma-joined hashes (one privacy-export bundle drawer covers several
    conversations), so a `where`-filtered query can only match a document
    whose entire stored value equals one candidate hash and would silently
    miss any bundle row. An incomplete result here is worse than none,
    because a caller reading the returned dict has no way to tell "no
    duplicate" from "the query couldn't see this row". Narrowing this scan
    correctly needs either a schema change (one hash per metadata field
    instead of a joined list) or a second index; out of scope for the fix
    that scoped prefetch_mined_set. See test_mine_convos_skips_same_content_
    under_new_filename and test_mine_convos_skips_same_conversation_within_
    re_exported_bundle, which a source_files-scoped or skipped version of
    this function broke.
    """
    hashes: dict[tuple[str, str], str] = {}

    def _absorb(meta):
        meta = meta or {}
        content_hash_field = meta.get("content_hash")
        src = meta.get("source_file")
        wing = meta.get("wing")
        if not content_hash_field or not src or not wing:
            return
        if not _metadata_matches_extract_mode(meta, extract_mode):
            return
        if not _meta_is_current(meta, extract_mode):
            return
        for content_hash in content_hash_field.split(","):
            key = (wing, content_hash)
            if content_hash and key not in hashes:
                hashes[key] = src

    # Only drawers carrying a content_hash can match, and on Chroma the
    # (key, string_value) index finds them without reading the rest.
    _scan_collection_metadata(
        collection, _MINED_SCAN_KEYS, _absorb, hashes.clear, require_key="content_hash"
    )
    return hashes
