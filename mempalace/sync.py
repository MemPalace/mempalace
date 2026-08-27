"""
sync.py — Gitignore-aware drawer prune (#1252).

Removes drawers whose source files are now gitignored, deleted, or moved
out of the project. Reuses the same GitignoreMatcher infrastructure that
the miner uses on the way in, so the same rules that block ingest also
drive the corresponding cleanup.

Usage:
    from mempalace.sync import sync_palace
    report = sync_palace(palace_path, project_dirs=["/repo"], dry_run=True)
"""

import logging
import os
import stat as stat_module
from collections import defaultdict
from pathlib import Path
from typing import Callable, Final, Literal, Optional, TypedDict

from .miner import is_gitignored, load_gitignore_matcher
from .palace import (
    MineAlreadyRunning,
    get_closets_collection,
    get_collection,
    mine_palace_lock,
)
from .source_identity import directory_identity


logger = logging.getLogger(__name__)
_BATCH = 1000


class SyncReport(TypedDict):
    scanned: int
    kept: int
    gitignored: int
    missing: int
    unresolved: int
    no_source: int
    out_of_scope: int
    removed_drawers: int
    removed_closets: int
    dry_run: bool
    by_source: dict[str, int]
    unresolved_by_source: dict[str, int]


def _resolve_project_root(source_file: Path, project_roots: list) -> Optional[Path]:
    """Return the longest project_root that source_file lives under.

    Assumes ``project_roots`` is sorted by path-length descending so the
    first match is the longest (deepest) prefix.
    """
    for root in project_roots:
        try:
            source_file.relative_to(root)
            return root
        except ValueError:
            continue
    return None


def _ancestor_matchers(source_file: Path, root: Path, matcher_cache: dict) -> list:
    """Build the ancestor-chain matcher list, root → file's parent.

    Callers are expected to invoke this only after `_resolve_project_root`
    confirms `source_file` lives under `root`. The defensive try/except
    keeps the function safe if a future caller skips that check.
    """
    matchers: list = []
    try:
        parts = source_file.relative_to(root).parts
    except ValueError:
        return matchers
    cursor = root
    matcher = load_gitignore_matcher(cursor, matcher_cache)
    if matcher is not None:
        matchers.append(matcher)
    for part in parts[:-1]:
        cursor = cursor / part
        matcher = load_gitignore_matcher(cursor, matcher_cache)
        if matcher is not None:
            matchers.append(matcher)
    return matchers


def _is_registry_row(meta: dict, drawer_id: str) -> bool:
    """Convo miner sentinels track 'have I seen this transcript' — preserve them.

    Deleting a `_reg_*` sentinel makes the next mine pass re-chunk and re-embed
    the entire transcript even though its content has not changed.
    """
    if (meta or {}).get("room") == "_registry":
        return True
    if (meta or {}).get("ingest_mode") == "registry":
        return True
    if drawer_id and drawer_id.startswith("_reg_"):
        return True
    return False


# ``Final`` keeps these literal types rather than widening them to ``str``,
# which the annotated return of ``_source_state`` needs.
_STATE_PRESENT: Final = "present"
_STATE_NOT_THERE: Final = "not_there"
_STATE_UNKNOWN: Final = "unknown"

# How many rows one source's survivor probe reads before it stops concluding
# anything. That probe is the fallback a batched read drops to, so this bound
# is what the pass degrades to rather than what it normally does. A source's
# registry sentinels are one per extract mode and there are two, so it is
# headroom rather than a guess, and a read that fills it is treated as "a
# drawer may sit past this" and keeps the closets. It also caps what one probe
# can bind: asking for the metadata of a matching row binds a variable for
# that row too, so an unbounded read of a source with many drawers reaches the
# backend's limit and answers with an error instead. Measured against the
# backend this ships with: one source of 40,000 drawers answers at
# ``limit=32766`` and raises ``too many SQL variables`` at 32,767.
_SURVIVOR_PROBE_LIMIT: Final = 8

# What one ``$in`` may carry. Each entry binds a variable, and the backend
# refuses past a ceiling of its own: measured, a list of 32,762 answers and
# 32,763 raises ``too many SQL variables``. That ceiling is the backend's,
# not the one the SQLite reachable from Python reports, which is 250,000.
# ``closet_llm`` works around the same number from the other side,
# paginating a fetch whose returned rows would bind past it (#802, #850,
# #1073). 500 sits sixty-five times under it, which leaves room for the
# matching rows a survivor probe also asks the metadata of, since those bind
# a variable each as well; a batch the backend still refuses is halved rather
# than assumed to fit.
_IN_CLAUSE_LIMIT: Final = 500

# What one batched probe may read back. The rows a batch can legitimately
# answer with are bookkeeping ones, and a full batch of sources carrying the
# per-source headroom each is what this allows: 500 sources binding a variable
# apiece plus these is 4,500, which is seven times under the ceiling the
# backend refuses at. It is not scaled to the batch, because a source's
# bookkeeping rows have no ceiling of their own and a bound that tightens
# with a smaller batch would turn a source carrying a pile of them into one
# whose closets are never purged. What it is there to stop is the read
# growing with what one source still holds: a wing-scoped pass leaves that
# source's drawers in the wings it did not scan, and an unbounded read brings
# every one of them back to answer yes or no.
_BATCH_PROBE_LIMIT: Final = _SURVIVOR_PROBE_LIMIT * _IN_CLAUSE_LIMIT


def _source_state(src: Path) -> Literal["present", "not_there", "unknown"]:
    """Report what one ``stat`` of ``src`` established, and nothing beyond it.

    ``not_there`` means the path answered ``ENOENT``. On its own that is not
    a reason to delete anything, because no errno separates "nothing is
    here" from "this cannot be reached right now".  ``_uncopyable_reason``
    in ``backups.py`` states the same rule for an operation that only leaves
    a file out of a backup copy, and records that on Windows an unmapped
    drive letter and an unreachable share both arrive as ``ENOENT`` as well.
    Turning ``not_there`` into a removal is ``sync_palace``'s job, and it
    needs a second reading to do it.

    ``unknown`` is every other failure: a path that could not be walked at
    all, which says nothing whatever about the leaf.
    """
    try:
        os.stat(src)
    except FileNotFoundError:
        return _STATE_NOT_THERE
    except (OSError, ValueError):
        # ENOTDIR, ELOOP, EACCES, a share that stopped answering. ValueError
        # is a path the platform cannot encode; the caller's ``resolve``
        # raises on those first, so it should not arrive, and catching it
        # costs nothing next to letting one drawer end the run.
        return _STATE_UNKNOWN
    return _STATE_PRESENT


def _is_a_present_file(src: Path) -> bool:
    """Whether ``src`` is a regular file that answers right now.

    A witness has to be a file *in* the directory it speaks for, and
    ``os.path.dirname`` alone does not establish that. A ``source_file``
    whose last component is empty, ``.`` or ``..`` keys to that directory
    while naming the directory itself, or its parent, and a directory
    outlives the unmount that takes its contents away. ``tool_add_drawer``
    stores whatever string its caller passed, so those spellings reach a
    real palace without anything being corrupt.

    Three answers collapse to ``False`` here on purpose: not a file, not
    there, and could not be read. Only the first of them corroborates a
    removal, so anything else has to keep the drawer.
    """
    try:
        return stat_module.S_ISREG(os.stat(src).st_mode)
    except (OSError, ValueError):
        return False


def _as_inode(value: object) -> Optional[int]:
    """``value`` as the number an inode is, or ``None`` when it is not one.

    A drawer holds whatever its backend gave back: the miner writes a string,
    and one that round-trips metadata through JSON may answer with a number.
    Reading both spellings as the same number keeps a match from reading as a
    mismatch, which would leave the drawer unremovable for good.

    Zero is a number and is returned as one; that it is not an identity is the
    caller's reading, since ``source_identity`` refuses to record it. Anything
    that is not an integer at all, a string that does not spell one included,
    is not an identity either. ``bool`` is an integer to Python and is refused
    here anyway: metadata carrying ``True`` would otherwise read as inode 1,
    which is the root of every tmpfs and a directory some drawer really was
    mined from.

    A float is read where it stands for a whole number, since a backend that
    keeps metadata numbers as doubles hands one back for what the miner wrote
    as a string. Refusing it would not be conservative: an unreadable identity
    is treated as no identity at all, which puts the drawer back where it was
    before any of this and lets corroboration alone remove it.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _mined_directory_still_answers(recorded: object, answering: Optional[str]) -> bool:
    """Whether the directory answering for a source is the one it was mined from.

    A drawer filed before ``source_identity`` existed, or by a path that does
    not record one, or from a directory that could not be stat'ed, carries no
    identity, and this answers ``True``: those drawers are decided by
    corroboration alone, exactly as before. So does a drawer carrying a zero,
    which is what a filesystem with no inode of its own reports and not an
    identity anything can match.

    ``recorded`` is typed as it arrives rather than as it was written: the
    miner writes a string, and a backend that round-trips metadata through
    JSON may hand back a number.

    Where an identity was recorded, anything other than that same inode
    answering right now means the directory the witness lives in is not the
    one the missing file was mined from. That covers a mount point whose
    lower layer holds a mined file of its own, a volume mounted over a
    directory the palace already knows a file in, a bind mount of another
    directory over it, and a volume that is simply away, since an unmounted
    mount point is the directory underneath and answers with its own number.

    What it does not cover is one volume swapped for another at the same
    path, where both answer the same number. A filesystem's root always
    does: inode 2 on ext4, 1 on tmpfs, whatever volume of that type is
    mounted. Below the root it depends on what the filesystem has allocated
    since. tmpfs hands its inodes out in creation order, so the first thing
    made on a fresh one takes 2 and the next 3. On ext4 a directory deleted
    and made again takes the same number back only while nothing was
    allocated in between: measured, ten rounds of ``rm -rf`` and ``mkdir``
    answered with one number every time, and a single directory made in
    between was enough to move it. Where the numbers do repeat, those
    drawers are decided by corroboration alone, exactly as on ``develop``.

    Both arguments belong to different things: ``recorded`` is this drawer's,
    ``answering`` is the directory's right now, read once per source.
    """
    # Both sides are read as the number an inode is before either is judged.
    # Judging the value as it arrived would split one identity in two: a zero
    # written as ``0`` reads as no identity, while the same zero written as
    # ``"0"`` reads as one nothing can ever match, and that drawer stops being
    # removable for good.
    recorded_ino = _as_inode(recorded)
    if not recorded_ino:
        return True
    # The directory answering with nothing is not the number this drawer was
    # filed against.
    return _as_inode(answering) == recorded_ino


def _classify_drawer(
    meta: dict, matcher_cache: dict, project_roots: list, drawer_id: str = ""
) -> str:
    """Classify a drawer by its source_file metadata.

    Returns one of: kept, gitignored, absent, unresolved, no_source,
    out_of_scope.

    ``absent`` is provisional and never reaches a ``SyncReport``. It says
    the file is not at its path, which is not yet a reason to remove the
    drawer; ``sync_palace`` decides that, and settles every ``absent`` into
    ``missing`` or ``unresolved`` once the whole pass is done.
    """
    # Defensive: main loop filters registry rows; this guards direct callers.
    if _is_registry_row(meta, drawer_id):
        return "kept"

    source_file = (meta or {}).get("source_file")
    if not source_file:
        return "no_source"

    src = Path(source_file)
    if not src.is_absolute():
        return "no_source"
    try:
        src = src.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        # A symlink loop: up to 3.12 pathlib turns ELOOP into RuntimeError
        # here, and 3.13 stopped raising and leaves it to the stat below. A
        # path the platform cannot encode raises ValueError here instead,
        # before any probe runs at all. Either way this drawer is one the
        # run must survive, not end on.
        return "unresolved"

    root = _resolve_project_root(src, project_roots)
    if root is None:
        return "out_of_scope"

    state = _source_state(src)
    if state == _STATE_UNKNOWN:
        return "unresolved"
    if state == _STATE_NOT_THERE:
        return "absent"

    matchers = _ancestor_matchers(src, root, matcher_cache)
    if matchers and is_gitignored(src, matchers, is_dir=False):
        return "gitignored"

    return "kept"


def _iter_drawer_metadata(col, wing: Optional[str]):
    """Yield (id, metadata) tuples from the drawers collection in batches."""
    offset = 0
    where = {"wing": wing} if wing else None
    while True:
        kwargs = {"include": ["metadatas"], "limit": _BATCH, "offset": offset}
        if where:
            kwargs["where"] = where
        batch = col.get(**kwargs)
        ids = batch.get("ids") or []
        metas = batch.get("metadatas") or []
        if not ids:
            return
        for drawer_id, meta in zip(ids, metas):
            yield drawer_id, meta
        if len(ids) < _BATCH:
            return
        offset += len(ids)


def _auto_detect_project_roots(col, wing: Optional[str]) -> list:
    """Walk drawer metadata once collecting candidate project roots.

    A path is a project root if any ancestor up to filesystem root holds
    a `.git` directory or a `.gitignore` file. The deepest such ancestor
    wins, so nested-but-still-tracked subprojects are honoured.
    `Path.parents` iterates deepest-first, so the first hit IS deepest.

    Dedupes on ``source_file`` string so a 200-chunk file costs one disk
    walk, not 200.
    """
    roots: set = set()
    seen_sources: set = set()
    for _, meta in _iter_drawer_metadata(col, wing):
        source_file = (meta or {}).get("source_file")
        if not source_file or source_file in seen_sources:
            continue
        seen_sources.add(source_file)
        src = Path(source_file)
        if not src.is_absolute():
            continue
        for parent in src.parents:
            if not _has_project_marker(parent):
                continue
            try:
                roots.add(parent.resolve(strict=False))
            except (OSError, RuntimeError, ValueError):
                # The marker is here but this path will not resolve. Stop
                # rather than let the climb register a higher ancestor as
                # the root, which would widen what the run treats as in
                # scope instead of narrowing it. No POSIX input reaches
                # this: a marker that stats means its own path resolved.
                # It is kept because the cost of being wrong about that on
                # another platform is the whole run, not one drawer.
                pass
            break
    return sorted(roots, key=lambda p: (-len(str(p)), str(p)))


def _has_project_marker(directory: Path) -> bool:
    """Whether ``directory`` looks like a project root, without ever raising.

    Each marker is probed on its own. An ancestor this process cannot walk
    holds no marker it could have read anyway, and an unreadable ``.git``
    must not hide a ``.gitignore`` beside it. A root that goes undetected
    leaves its drawers ``out_of_scope``, which is the side that keeps them,
    while a probe that escaped would end the run before a single drawer was
    classified.
    """
    try:
        if (directory / ".git").exists():
            return True
    except (OSError, RuntimeError, ValueError):
        pass
    try:
        return (directory / ".gitignore").is_file()
    except (OSError, RuntimeError, ValueError):
        return False


def _normalize_project_dirs(project_dirs) -> list:
    """Resolve and sort project dirs so deepest-prefix wins on first match."""
    resolved = [Path(p).resolve(strict=False) for p in project_dirs]
    return sorted(resolved, key=lambda p: (-len(str(p)), str(p)))


def _delete_in_batches(col, ids: list, batch_size: int, wal_log: Optional[Callable]):
    """Delete drawer IDs in batches, optionally logging each batch to WAL."""
    deleted = 0
    for i in range(0, len(ids), batch_size):
        chunk = ids[i : i + batch_size]
        col.delete(ids=chunk)
        deleted += len(chunk)
        if wal_log is not None:
            wal_log(
                "sync_prune",
                {"first_id": chunk[0]},
                {"removed_count": len(chunk)},
            )
    return deleted


def _is_bookkeeping_row(meta: dict, drawer_id: str) -> bool:
    """Whether a row tracks a source rather than holding any of its content.

    Both sentinel writers put one of these beside a file's drawers: the convo
    miner's registry row, which ``_is_registry_row`` names, and the format
    miner's, which files a source that extracted to nothing under
    ``room="documents"`` with an id of its own. Neither is ever indexed by a
    closet, so neither is evidence that the lines in one still point at
    anything.

    Kept apart from ``_is_registry_row`` because that predicate decides which
    drawers a pass may remove, and the format sentinel is removable: it names
    a real file and goes when the file does. Widening it would keep those
    sentinels for good instead.
    """
    if _is_registry_row(meta, drawer_id):
        return True
    if (meta or {}).get("is_sentinel"):
        return True
    return bool(drawer_id) and drawer_id.startswith("sentinel_")


def _source_holds_nothing(col, source) -> bool:
    """Whether ``source`` has no drawer left, asked one source at a time.

    The fallback the batch below drops to, and bounded because a source with
    more drawers than the backend takes variables for answers with an error
    rather than a result: measured, one source of 40,000 drawers answers at
    ``limit=32766`` and raises ``too many SQL variables`` at 32,767.

    A read that filled that bound without finding a drawer says one may sit
    past it, which is not an established absence, so it answers ``False``.
    So does a read that raised, and one that came back with fewer metadata
    rows than ids: a row that did not come back cannot be told from a
    survivor, and ``zip`` would drop it without a word.
    """
    # Only the read is guarded. Reading the answer under the same handler
    # would report a fault in this function as a backend that would not
    # answer, and keep the closets of every source while it did.
    try:
        answer = col.get(
            where={"source_file": source},
            limit=_SURVIVOR_PROBE_LIMIT,
            include=["metadatas"],
        )
    except Exception as exc:
        logger.warning(
            "Closets kept for %s and not asked about again (survivor probe failed): %s",
            source,
            exc,
            exc_info=True,
        )
        return False
    ids = answer.get("ids") or []
    metas = answer.get("metadatas") or []
    if len(metas) != len(ids):
        logger.warning(
            "Closets kept for %s (survivor probe returned %d ids and %d metadata rows)",
            source,
            len(ids),
            len(metas),
        )
        return False
    survivors = any(
        not _is_bookkeeping_row(meta or {}, drawer_id) for drawer_id, meta in zip(ids, metas)
    )
    return not survivors and len(ids) < _SURVIVOR_PROBE_LIMIT


def _sources_holding_nothing(col, batch: list) -> list:
    """Which of ``batch`` the palace has no drawer of left, in one read.

    A probe is a scan of the metadata, so it costs the collection's size and
    not the number of rows it matches, and asking per source pays that scan
    again for every one. Measured on 2,000 emptied sources, both forms
    reaching the same verdict: 3.2 seconds against 0.013 over a palace of
    8,000 rows, 12.0 to 14.8 against 0.04 over one of 80,000.

    Each entry binds a variable and so does each matching row, so a batch
    the backend will not take answers with an error. That is what the
    halving is for: a batch that raises is split until it answers or is one
    source, which is then read the bounded way. Reaching the same verdict
    down that path is what makes the split safe rather than a second rule.

    The read is bounded by ``_BATCH_PROBE_LIMIT`` and not by what its sources
    hold. A wing-scoped pass leaves a source's drawers in the wings it did not
    scan, which is the case this question exists for, and an unbounded read
    brings every one of them back to answer yes or no: measured, one source of
    50,000 such rows in a batch of 500 makes the read bind past what the
    backend takes, and the halving that follows costs 38 seconds and a peak
    RSS of 1.8 GiB, where reading that batch one source at a time costs 3.2
    seconds and 159 MiB and bounded costs 3.5 and 236.

    Three answers are not answers to this question, and all drop to reading
    the batch one source at a time. Fewer metadata rows than ids is one. A row
    whose metadata carries no ``source_file`` is another: the grouping below
    has nothing to file it under, and a row that cannot be attributed would
    leave the source it belongs to looking empty. The third is an answer that
    filled the bound, which says nothing about what sits past it: a source the
    answer does not name is not thereby empty.
    """
    try:
        answer = col.get(
            where={"source_file": {"$in": batch}},
            limit=_BATCH_PROBE_LIMIT,
            include=["metadatas"],
        )
    except Exception as exc:
        if len(batch) > 1:
            half = len(batch) // 2
            return _sources_holding_nothing(col, batch[:half]) + _sources_holding_nothing(
                col, batch[half:]
            )
        logger.warning(
            "Survivor probe for %s reading one source at a time (batch failed): %s",
            batch[0],
            exc,
        )
        return [batch[0]] if _source_holds_nothing(col, batch[0]) else []
    ids = answer.get("ids") or []
    metas = answer.get("metadatas") or []
    if len(metas) != len(ids):
        logger.warning(
            "Survivor probe for %d sources returned %d ids and %d metadata rows;"
            " reading them one at a time",
            len(batch),
            len(ids),
            len(metas),
        )
        return [source for source in batch if _source_holds_nothing(col, source)]
    if len(ids) >= _BATCH_PROBE_LIMIT:
        logger.warning(
            "Survivor probe for %d sources filled its bound of %d rows; reading them one at a time",
            len(batch),
            _BATCH_PROBE_LIMIT,
        )
        return [source for source in batch if _source_holds_nothing(col, source)]
    holding = set()
    for drawer_id, meta in zip(ids, metas):
        named = (meta or {}).get("source_file")
        if not named:
            logger.warning(
                "Survivor probe for %d sources answered with a row naming none of them;"
                " reading them one at a time",
                len(batch),
            )
            return [source for source in batch if _source_holds_nothing(col, source)]
        if not _is_bookkeeping_row(meta or {}, drawer_id):
            holding.add(named)
    return [source for source in batch if source not in holding]


def _purge_emptied_closets(col, closets_col, removable_sources) -> int:
    """Delete the closets of every source this pass left holding no drawer.

    The verdict is per drawer, so one source can have a drawer removed and
    another kept. Purging by source would strand that survivor without the
    closet lines that index it, and a re-mine does not rebuild them:
    ``file_already_mined`` skips a file whose mtime has not moved, so the loss
    would outlive the volume's return. Which sources are really empty is
    therefore asked of the palace once the drawers are gone, rather than
    counted from this pass: a wing-scoped run reads one wing's drawers, and a
    count taken from it would call a source empty while its drawers in another
    wing are untouched.

    A registry sentinel names the same source as the drawers it tracks and is
    never removed, so it is not evidence that any of the file is still filed.
    Reading the rows rather than counting them tells the two apart, and a
    source whose sentinel is all that is left has closets that point at
    nothing.

    Whatever that question could not settle leaves the closets in place and
    is reported as closets that were not removed, which reads the same as a
    source that had none. No later pass revisits such a source: its drawers
    are already gone, so nothing puts it in front of this question again.
    """
    ordered = sorted(removable_sources)
    emptied: list = []
    for start in range(0, len(ordered), _IN_CLAUSE_LIMIT):
        emptied.extend(_sources_holding_nothing(col, ordered[start : start + _IN_CLAUSE_LIMIT]))
    if not emptied:
        return 0

    # Batched because the list itself binds one variable per entry, so a pass
    # that emptied enough sources would end in an error where it should have
    # purged their closets. Nothing but ids is asked for, so a matching row
    # binds nothing of its own here, and this read is left unguarded the way
    # ``develop`` leaves it: a fault here ends the call, where swallowing it
    # would report a purge that did not happen.
    closet_ids: list = []
    for start in range(0, len(emptied), _IN_CLAUSE_LIMIT):
        batch = emptied[start : start + _IN_CLAUSE_LIMIT]
        closet_ids.extend(
            closets_col.get(where={"source_file": {"$in": batch}}, include=[]).get("ids") or []
        )
    if not closet_ids:
        return 0
    closets_col.delete(ids=closet_ids)
    return len(closet_ids)


def sync_palace(
    palace_path: str,
    project_dirs: Optional[list] = None,
    wing: Optional[str] = None,
    dry_run: bool = True,
    batch_size: int = _BATCH,
    wal_log: Optional[Callable] = None,
) -> SyncReport:
    """Prune drawers whose source files are gitignored, missing, or moved.

    Returns a SyncReport with bucket counts. Dry-run by default; pass
    dry_run=False to actually delete drawers and matching closets.

    Only ``gitignored`` and ``missing`` are removed. A source file this
    could not establish as deleted lands in ``unresolved`` and is counted,
    printed and kept: an unmounted volume must not be read as a deletion.

    A file that is not at its path reaches ``missing`` only when the palace
    can still see a source file of its own in that same directory. A
    deletion leaves the file's neighbours where they were; a volume that is
    not mounted takes every one of them away at once, and there is no call
    that tells those two apart from the file alone. Both halves of that are
    read again when the verdict is formed rather than trusted from earlier
    in the pass, since a volume can leave inside one pass and can come back
    inside one.

    A witness proves the directory is reachable, not that it is the
    directory the missing file was mined from, so the corroboration is read
    together with the inode ``source_identity`` recorded at mine time. A
    mount point whose lower layer holds a mined file of its own, a volume
    mounted over a directory the palace already knows a file in, and a bind
    mount of another directory over it all answer with an inode other than
    the drawer's, and the drawer is kept. A drawer filed before that existed,
    or from a directory that could not be stat'ed, carries no identity and is
    decided by corroboration alone.

    Three limits of that reading are worth stating. A directory the palace
    knows no *surviving* file in cannot corroborate anything, so a file
    deleted on its own from a one-file directory is kept and reported, and
    so is a whole directory's worth of files deleted together. A volume
    that leaves and returns between two adjacent ``stat`` calls is not
    covered, because nothing spans two syscalls. And a directory that is
    deleted and recreated may answer with a different inode than the one its
    drawers carry, which reads exactly like a volume swapped in: the drawers
    of files that really went are then kept and reported as ``unresolved``
    for good, since the files they name are gone and cannot be mined again.
    ``mempalace_delete_by_source`` removes them once the report names them,
    and ``mempalace_delete_drawer`` takes one at a time; there is no bulk way
    out of that state on purpose, since a stranded drawer and a drawer a
    volume is holding are the same reading, and nothing here can prune one
    kind without pruning the other.

    ``wing`` scopes the corroboration as well as the scan, since only that
    wing's drawers are read. A wing-scoped run therefore keeps what a run
    over the whole palace would prune, and every candidate the wider run
    has is a candidate it has too.

    Holds ``mine_palace_lock`` for the whole call so the classify pass and
    the apply branch see the same drawer snapshot. Raises
    ``MineAlreadyRunning`` if another mine is in progress on this palace.

    On apply (``dry_run=False``), at least one of ``wing`` or
    ``project_dirs`` must be set so a caller cannot accidentally prune
    every wing in a multi-project palace via auto-detected roots.
    """
    if not dry_run and not wing and not project_dirs:
        raise ValueError(
            "sync apply requires explicit wing= or project_dirs= so it cannot "
            "auto-prune every wing in a multi-project palace; pass --wing or "
            "a project directory"
        )
    if project_dirs is not None and not project_dirs:
        raise ValueError(
            "project_dirs was provided but is empty; pass at least one project "
            "root or pass project_dirs=None to auto-detect from drawer metadata"
        )

    counts = {
        "scanned": 0,
        "kept": 0,
        "gitignored": 0,
        "missing": 0,
        "unresolved": 0,
        "no_source": 0,
        "out_of_scope": 0,
    }
    by_source: dict = defaultdict(int)
    unresolved_by_source: dict = defaultdict(int)
    removable_ids: list = []
    removable_sources: set = set()

    with mine_palace_lock(palace_path):
        col = get_collection(palace_path, create=False)

        if project_dirs is not None:
            roots = _normalize_project_dirs(project_dirs)
        else:
            roots = _auto_detect_project_roots(col, wing)

        matcher_cache: dict = {}
        # Same source_file → same verdict holds because mine_palace_lock
        # blocks concurrent writers and the loop is synchronous.
        classification_cache: dict = {}

        # Candidate witnesses per directory, and the drawers whose source
        # file is not at its path. The second list waits for the first to be
        # complete: one drawer cannot say whether its directory lost one
        # file or all of them. Every candidate is kept rather than the first
        # one met, so the verdict does not turn on which drawer the pass
        # happened to reach first.
        live_dirs: dict = {}
        not_there: list = []

        for drawer_id, meta in _iter_drawer_metadata(col, wing):
            counts["scanned"] += 1
            meta = meta or {}
            source_file = meta.get("source_file")

            registry_row = _is_registry_row(meta, drawer_id)
            if registry_row:
                bucket = "kept"
            elif source_file and source_file in classification_cache:
                bucket = classification_cache[source_file]
            else:
                bucket = _classify_drawer(meta, matcher_cache, roots, drawer_id)
                if source_file:
                    classification_cache[source_file] = bucket

            if bucket == "absent":
                not_there.append((drawer_id, source_file, meta.get("source_dir_ino")))
                continue

            # A registry row is kept without the file being looked at, so it
            # is not evidence that anything is on disk. The inner mapping is
            # a set that keeps its insertion order: a file arrives once per
            # chunk it was split into, and re-reading the same path a
            # hundred times would be the whole cost of this pass.
            if source_file and not registry_row and bucket in ("kept", "gitignored"):
                live_dirs.setdefault(os.path.dirname(source_file), {})[source_file] = None

            counts[bucket] += 1
            if bucket == "unresolved":
                # Reachable only through a source_file that is a non-empty
                # absolute path, like the removal below.
                unresolved_by_source[source_file] += 1
            if bucket == "gitignored":
                removable_ids.append(drawer_id)
                if source_file:
                    removable_sources.add(source_file)
                    by_source[source_file] += 1

        # The directory keys are the metadata strings as written, while the
        # classification above works on the resolved path, so two spellings
        # of one directory need not meet: ``os.path.dirname`` leaves a
        # ``/./`` segment alone, and a path through a symlink keeps the
        # link's spelling. Not unifying them errs towards keeping. It cannot
        # err the other way as long as a candidate is a file in the
        # directory it is filed under, which is what ``_is_a_present_file``
        # is there to require: a path whose last component is empty, ``.``
        # or ``..`` is filed under the directory it names, or under that
        # directory's parent, and neither is a file in it.
        #
        # Both halves of the verdict are read again here rather than trusted
        # from earlier in the pass, and back to back rather than one of them
        # early: a pass over a large palace runs for minutes, so a volume can
        # leave or return inside one. Re-reading only the neighbour would let
        # a volume that came back condemn every drawer read while it was
        # away, exactly as re-reading neither would let one that left condemn
        # every drawer read after it. ``any`` stops at the candidate that
        # answered, so that reading is the one immediately before the
        # source's own. Two syscalls are still two syscalls, and a volume
        # that flaps between them is not covered by anything here.
        settled: dict = {}
        identity_now: dict = {}
        for drawer_id, source_file, source_dir_ino in not_there:
            # ``absent`` is only reachable through a source_file that is a
            # non-empty absolute path, so nothing here guards against a
            # missing one, the same way the removal below does not.
            if source_file not in settled:
                directory = os.path.dirname(source_file)
                witnesses = live_dirs.get(directory, ())
                # The identity is read on both sides of the corroboration
                # rather than once before it. A mount that arrives, or one
                # that leaves, while the witnesses are being stat'ed is the
                # event this rule is here for, and one reading cannot see it:
                # taken apart, the readings describe two different
                # directories, and a verdict built from them is a verdict
                # about neither. Two transitions inside the same window are
                # not covered, because the readings then agree about a
                # directory that was gone between them; nothing that samples
                # a state twice can cover that, the same way the drawer's own
                # two ``stat`` calls do not.
                identity_before = directory_identity(directory)
                settled[source_file] = (
                    any(_is_a_present_file(Path(w)) for w in witnesses)
                    and _source_state(Path(source_file)) == _STATE_NOT_THERE
                )
                identity_now[source_file] = directory_identity(directory)
                if identity_before != identity_now[source_file]:
                    settled[source_file] = False
            # The corroboration belongs to the source; the recorded identity
            # belongs to this drawer. One source's drawers need not agree about
            # it: they are filed at different times and by different writers,
            # so a sweep leaves none where a mine left one, and a re-mine after
            # the directory was replaced leaves a third answer. Applying one
            # drawer's identity to all of them would make the verdict turn on
            # which drawer the pass reached first.
            established = settled[source_file] and _mined_directory_still_answers(
                source_dir_ino, identity_now[source_file]
            )
            counts["missing" if established else "unresolved"] += 1
            if established:
                removable_ids.append(drawer_id)
                removable_sources.add(source_file)
                by_source[source_file] += 1
            else:
                unresolved_by_source[source_file] += 1

        report: SyncReport = {
            **counts,
            "removed_drawers": 0,
            "removed_closets": 0,
            "dry_run": dry_run,
            "by_source": dict(by_source),
            "unresolved_by_source": dict(unresolved_by_source),
        }

        if dry_run or not removable_ids:
            return report

        report["removed_drawers"] = _delete_in_batches(col, removable_ids, batch_size, wal_log)

        closets_removed = 0
        try:
            closets_col = get_closets_collection(palace_path, create=False)
        except Exception as exc:
            # No collection to purge from, so the probe below would be asking
            # a question whose answer nothing could use.
            logger.warning("Closet purge skipped (collection unavailable): %s", exc)
        else:
            closets_removed = _purge_emptied_closets(col, closets_col, removable_sources)
        report["removed_closets"] = closets_removed
    return report


__all__ = [
    "MineAlreadyRunning",
    "SyncReport",
    "sync_palace",
]
