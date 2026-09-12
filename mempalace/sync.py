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

    Three limits of that reading are worth stating. A directory the palace
    knows no *surviving* file in cannot corroborate anything, so a file
    deleted on its own from a one-file directory is kept and reported, and
    so is a whole directory's worth of files deleted together. A mount
    point that also holds a mined file of its own does corroborate, since
    that file survives the unmount: separating those needs the identity of
    the filesystem each source was mined from, which is not recorded. And a
    volume that leaves and returns between two adjacent ``stat`` calls is
    not covered, because nothing spans two syscalls.

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
                not_there.append((drawer_id, source_file))
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
        for drawer_id, source_file in not_there:
            # ``absent`` is only reachable through a source_file that is a
            # non-empty absolute path, so nothing here guards against a
            # missing one, the same way the removal below does not.
            if source_file not in settled:
                witnesses = live_dirs.get(os.path.dirname(source_file), ())
                settled[source_file] = (
                    any(_is_a_present_file(Path(w)) for w in witnesses)
                    and _source_state(Path(source_file)) == _STATE_NOT_THERE
                )
            established = settled[source_file]
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

        closets_col = None
        try:
            closets_col = get_closets_collection(palace_path, create=False)
        except Exception as exc:
            logger.warning("Closet purge skipped (collection unavailable): %s", exc)

        closets_removed = 0
        if closets_col is not None and removable_sources:
            closet_ids = (
                closets_col.get(
                    where={"source_file": {"$in": list(removable_sources)}},
                    include=[],
                ).get("ids")
                or []
            )
            if closet_ids:
                closets_col.delete(ids=closet_ids)
                closets_removed = len(closet_ids)
        report["removed_closets"] = closets_removed
    return report


__all__ = [
    "MineAlreadyRunning",
    "SyncReport",
    "sync_palace",
]
