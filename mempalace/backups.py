"""Writing and pruning palace backups.

``mempalace migrate`` and ``mempalace repair max-seq-id`` each write a fresh,
timestamped backup every time they run and historically never deleted the old
ones. On a machine that mines or repairs on a schedule those full-size copies
accumulate silently — a real palace was found with hundreds of gigabytes of
backups sitting beside only a few hundred megabytes of live data, nearly
filling the disk. This module prunes the backup set down to a bounded count
after each new backup is written.

The retention count comes from ``MempalaceConfig.max_backups`` (default 10).

``copy_palace_dir`` is the other half: the whole-directory copy that
``mempalace repair`` in its default mode and ``mempalace migrate`` take before
they overwrite a live palace. The other backup paths copy a single file and do
not use it.
"""

import glob
import os
import shutil
import stat


# Names for the file types a backup copy deliberately leaves behind. A socket
# or a named pipe makes ``shutil.copytree`` record an error for that entry and
# raise at the end. Device nodes do not, but dereferencing one copies the
# DEVICE instead of palace data, and a link to ``/dev/zero`` is copied without
# bound, so they are left out for the opposite reason: the copy would succeed
# at the wrong thing.
_UNCOPYABLE_FILE_TYPES = (
    (stat.S_ISSOCK, "socket"),
    (stat.S_ISFIFO, "named pipe"),
    (stat.S_ISCHR, "character device"),
    (stat.S_ISBLK, "block device"),
)

_UNNAMED_FILE_TYPE = "not a regular file or directory"


def _file_type_label(mode):
    """Name the file type in ``mode``, falling back to a generic phrase.

    The fallback covers a file type this table has no name for. Nothing on
    Linux reaches it, because ``stat`` there reports ``S_IFDOOR``,
    ``S_IFPORT`` and ``S_IFWHT`` as 0. macOS does define ``S_IFWHT``, a
    union-mount whiteout, which is precisely what the fallback exists for:
    an entry no copy can carry is skipped with an honest reason rather than
    read as copyable.
    """
    for is_type, label in _UNCOPYABLE_FILE_TYPES:
        if is_type(mode):
            return label
    return _UNNAMED_FILE_TYPE


def _uncopyable_reason(path, *, follow_symlinks):
    """Name why ``path`` cannot be copied into a backup, or return ``None``.

    This is an allowlist, matching how the rest of the package treats
    directory entries: only regular files and directories are copyable, and
    everything else is named and left out.

    Args:
        path: The directory entry to classify.
        follow_symlinks: Mirrors the copy's own link handling. ``True`` when
            the copy dereferences links, so the TARGET's file type decides;
            ``False`` when it recreates them as links, in which case any
            symlink is fine and its target is never read.

    Returns:
        A short human-readable reason, or ``None`` both when the entry is
        copyable and when its type could not be established.

    Those two ``None`` cases are deliberately the same answer: only a file
    type this could read names a reason. An entry whose type it could not
    read is left for the copy to attempt and, if it really is broken, to
    fail on.

    A failed ``stat`` therefore never becomes a reason to skip. It says the
    entry cannot be resolved right now, which is not the same as holding no
    data, and no errno separates the two: a symlink into a volume that is
    not mounted fails exactly like one whose target was deleted, and on
    Windows an unmapped drive letter and an unreachable network share both
    arrive as ``ENOENT`` as well.
    """
    try:
        st = os.lstat(path)
    except OSError:
        return None

    if stat.S_ISLNK(st.st_mode):
        if not follow_symlinks:
            return None
        try:
            st = os.stat(path)
        except OSError:
            return None

    if stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode):
        return None
    return _file_type_label(st.st_mode)


def _report_skipped(skipped, src, log):
    """Tell the operator what the copy left out.

    Every step that can fail is guarded on its own, because this also runs
    after the copy has failed: both callers pass ``print``, so one line the
    terminal will not take must cost neither the copy's own outcome nor the
    lines after it.
    """
    noun = "entry" if len(skipped) == 1 else "entries"
    lines = [f"  Backup: skipped {len(skipped)} {noun} that cannot be copied:"]
    for path, reason in skipped:
        try:
            name = os.path.relpath(path, src)
        except (OSError, ValueError):
            # On Windows ``relpath`` rejects paths on different drives; for a
            # relative ``src`` it also reaches ``os.getcwd()``, which can fail
            # on a deleted directory. Neither costs us the entry's name.
            name = path
        lines.append(f"    {name} ({reason})")

    for line in lines:
        try:
            log(line)
        except UnicodeError:
            # An stdout that cannot encode this entry's name. Dropping the
            # line would leave a header whose count disagrees with the list
            # under it, so the name is escaped and retried before it is
            # given up on.
            try:
                log(line.encode("ascii", "backslashreplace").decode("ascii"))
            except (OSError, UnicodeError):
                continue
        except OSError:
            # A write that the device refused. A short report is buffered
            # whole, so this arrives only once the report is long enough to
            # flush partway through, and then the header is already out.
            # Anything else is a caller passing something that is not a
            # working ``log``, which should surface rather than be swallowed.
            continue


def copy_palace_dir(src, dst, *, symlinks=False, log=None):
    """Copy a palace directory to ``dst``, skipping entries no copy can carry.

    ``shutil.copytree`` finishes the copy but raises ``shutil.Error`` at the
    end when a directory entry is not something it knows how to duplicate: a
    Unix domain socket, or a named pipe. The caller has to treat that as a
    failed backup, so the command died before the rebuild the backup was
    guarding (#2207). Such entries are runtime artifacts of whatever process
    created them and never hold palace data, so a backup without them is
    still a complete backup of the palace.

    Args:
        src: Palace directory to copy.
        dst: Destination path. Must not already exist.
        symlinks: Passed to ``shutil.copytree``. ``True`` recreates symlinks
            as symlinks, ``False`` copies what they point at.
        log: Optional callable (e.g. ``print``) for human-readable progress.

    Returns:
        The list of ``(path, reason)`` pairs that were skipped: sorted within
        each directory, in the order the copy visited the directories. Like
        ``prune_backups``, this both returns what it did and logs it.

    Every other copy failure still raises out of ``shutil.copytree``. A caller
    about to overwrite the live palace must still stop when its safety copy
    did not come out whole, so this narrows what the copy attempts rather
    than swallowing what it reports.

    ``shutil.copytree`` hands the callback names rather than the ``os.scandir``
    entries it already holds, so classifying costs one extra ``os.lstat`` per
    directory entry, and it classifies a whole directory before copying any of
    it. An entry whose type changes inside that window is handled by the
    earlier reading, which cuts both ways: one that became a socket still
    aborts the copy, and one that was a socket and became a regular file is
    skipped with its contents. Nothing in MemPalace replaces an entry that
    way, and the skipped name is printed either way.
    """
    skipped = []
    # ``shutil.copytree`` classifies the top directory before it creates the
    # destination, so a copy that never starts still produces a skip list.
    # Reporting one would tell the operator what a nonexistent backup is
    # missing, so both ways that happens are excluded below: a destination
    # already occupied (this snapshot), and a destination that could not be
    # created at all.
    dst_existed = os.path.lexists(dst)

    def _ignore(directory, names):
        ignored = set()
        found = []
        for name in names:
            path = os.path.join(directory, name)
            reason = _uncopyable_reason(path, follow_symlinks=not symlinks)
            if reason is not None:
                ignored.add(name)
                found.append((path, reason))
        skipped.extend(sorted(found))
        return ignored

    def _report():
        if skipped and log and not dst_existed and os.path.isdir(dst):
            _report_skipped(skipped, src, log)

    try:
        shutil.copytree(src, dst, symlinks=symlinks, ignore=_ignore)
    except BaseException:
        # Reported for a failed copy too, including an interrupted one: a
        # half-written backup is exactly when the operator needs to know what
        # was left out of it. But the copy's own failure is what the caller
        # has to diagnose, so a report that fails on top of it is dropped
        # rather than raised in its place.
        try:
            _report()
        except Exception:
            pass
        raise

    # Not suppressed here: with the backup complete and nothing destructive
    # done yet, a ``log`` that cannot be called at all is a caller's bug worth
    # hearing about. The terminal failures inside ``_report_skipped`` are
    # absorbed on both paths.
    _report()
    return skipped


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
