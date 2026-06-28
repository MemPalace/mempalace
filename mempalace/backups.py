"""Retention pruning for timestamped palace backups.

``mempalace migrate`` and ``mempalace repair max-seq-id`` each write a fresh,
timestamped backup every time they run and historically never deleted the old
ones. On a machine that mines or repairs on a schedule those full-size copies
accumulate silently — a real palace was found with hundreds of gigabytes of
backups sitting beside only a few hundred megabytes of live data, nearly
filling the disk. This module prunes the backup set down to a bounded count
after each new backup is written.

The retention count comes from ``MempalaceConfig.max_backups`` (default 10).
"""

import glob
import os
import shutil


def prune_backups(pattern, max_backups, *, log=None):
    """Delete the oldest backups matching ``pattern`` so at most ``max_backups`` remain.

    Args:
        pattern: A glob pattern matching the backup paths (files or
            directories). The caller is responsible for ``glob.escape``-ing
            any literal, non-wildcard portion that can contain glob
            metacharacters — palace paths sometimes do (e.g. a ``[``).
        max_backups: Number of most-recent backups to keep. ``None`` or any
            value ``<= 0`` disables pruning and returns immediately, so a
            backup set is never touched when the user has opted out.
        log: Optional callable (e.g. ``print``) for human-readable progress.

    Returns:
        The list of paths that were successfully removed.

    Recency is determined by filesystem mtime rather than by parsing the
    timestamp out of the name, so it stays correct even when two backup
    producers use different timestamp formats. Deletion failures are logged
    and skipped: pruning is best-effort cleanup and must never abort the
    migrate/repair operation that just completed successfully.
    """
    if max_backups is None or max_backups <= 0:
        return []

    scored = []
    for path in glob.glob(pattern):
        try:
            scored.append((os.path.getmtime(path), path))
        except OSError:
            # Vanished between glob and stat (concurrent prune / cleanup);
            # nothing for us to remove.
            continue

    if len(scored) <= max_backups:
        return []

    # Newest first; the path breaks mtime ties so ordering is deterministic.
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)

    removed = []
    for _mtime, path in scored[max_backups:]:
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
        except OSError as exc:
            if log:
                log(f"  Backup prune: could not remove {path}: {exc}")
            continue
        removed.append(path)
        if log:
            log(f"  Backup prune: removed old backup {path}")

    return removed
