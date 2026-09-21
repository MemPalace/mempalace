"""Tests for writing and pruning palace backups (mempalace.backups).

``prune_backups`` guards the fix for unbounded backup growth: ``mempalace
migrate`` and ``mempalace repair max-seq-id`` each drop a fresh full-size,
timestamped copy every run, and used to never delete the old ones. A palace
was found with hundreds of GB of stale backups beside a few hundred MB of
live data.

``copy_palace_dir`` guards #2207: the whole-directory copy that ``repair``
and ``migrate`` take before overwriting a live palace used to abort the whole
command when the palace held a directory entry ``shutil.copytree`` cannot
duplicate.
"""

import errno
import os
import shutil
import socket
import stat

import pytest

from mempalace.backups import (
    _file_type_label,
    _uncopyable_reason,
    copy_palace_dir,
    prune_backups,
)

needs_unix_socket = pytest.mark.skipif(
    os.name == "nt" or not hasattr(socket, "AF_UNIX"),
    reason="Unix domain socket files are POSIX-only",
)
needs_fifo = pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="named pipes are POSIX-only")
needs_unprivileged_posix = pytest.mark.skipif(
    os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="directory permission bits gate neither root nor Windows",
)
needs_char_device = pytest.mark.skipif(
    os.name == "nt" or not os.path.exists("/dev/null"),
    reason="device nodes are POSIX-only",
)


def _symlink_or_skip(link, target):
    """Create ``link`` pointing at ``target``, or skip if the platform refuses.

    Windows without ``SeCreateSymbolicLinkPrivilege`` raises ``OSError``
    before any product code runs. Per PR #1555 review (Igor), symlink tests
    skip cleanly there rather than fail spuriously. Trying the syscall keeps
    the symlink half of this module under test wherever it does work, which a
    blanket ``os.name == "nt"`` skip would give up on.

    ``NotImplementedError`` covers the restricted sandboxes ``test_exporter``
    already documents. Otherwise only a permission refusal is turned into a
    skip: ``EEXIST`` from a test that left the name behind, ``ENOENT`` from a
    missing parent and ``ENOSPC`` are bugs in the test, and swallowing them
    would delete this module's symlink coverage from a run that still
    reported success.
    """
    try:
        link.symlink_to(target)
    except NotImplementedError as exc:
        pytest.skip(f"symlinks are unavailable here: {exc}")
    except OSError as exc:
        if os.name != "nt" and exc.errno not in (errno.EPERM, errno.EACCES):
            raise
        pytest.skip(f"symlink creation not permitted for this user: {exc}")


def _make_palace(parent, name="palace"):
    """A palace directory holding the one file every caller cares about."""
    path = parent / name
    path.mkdir()
    (path / "chroma.sqlite3").write_bytes(b"SQLite format 3\x00")
    segment = path / "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    segment.mkdir()
    (segment / "data_level0.bin").write_bytes(b"\x00" * 32)
    return path


def _bind_socket(directory, name, monkeypatch):
    """Bind a Unix socket inside ``directory`` and return its path.

    Bound by relative name from inside the directory: an absolute pytest tmp
    path can exceed the ~100-byte ``sun_path`` limit, which is tight on
    macOS. ``monkeypatch.chdir`` restores the working directory afterwards.
    """
    monkeypatch.chdir(directory)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(name)
    finally:
        # The socket FILE outlives the process that bound it, which is
        # exactly the state #2207 was reported in.
        sock.close()
    return os.path.join(str(directory), name)


def _make_backup_dir(parent, name, mtime):
    """Create a directory backup with a fixed mtime."""
    path = parent / name
    path.mkdir()
    (path / "chroma.sqlite3").write_text("db")
    os.utime(path, (mtime, mtime))
    return path


def _make_backup_file(parent, name, mtime):
    """Create a file backup with a fixed mtime."""
    path = parent / name
    path.write_text("db")
    os.utime(path, (mtime, mtime))
    return path


def test_prune_keeps_newest_and_removes_oldest(tmp_path):
    # 5 backups, mtimes 100..500; keep 2 newest (400, 500).
    paths = [_make_backup_file(tmp_path, f"b.{i}", mtime=i * 100) for i in range(1, 6)]

    removed = prune_backups(str(tmp_path / "b.*"), max_backups=2)

    surviving = {p.name for p in tmp_path.iterdir()}
    assert surviving == {"b.4", "b.5"}
    assert set(removed) == {str(paths[0]), str(paths[1]), str(paths[2])}


def test_prune_removes_directory_backups(tmp_path):
    """migrate writes directory backups (full copytree) — must rmtree them."""
    _make_backup_dir(tmp_path, "palace.pre-migrate.1", mtime=100)
    _make_backup_dir(tmp_path, "palace.pre-migrate.2", mtime=200)
    keep = _make_backup_dir(tmp_path, "palace.pre-migrate.3", mtime=300)

    removed = prune_backups(str(tmp_path / "palace.pre-migrate.*"), max_backups=1)

    assert keep.is_dir()
    assert len(removed) == 2
    assert not (tmp_path / "palace.pre-migrate.1").exists()
    assert not (tmp_path / "palace.pre-migrate.2").exists()


def test_prune_noop_when_under_limit(tmp_path):
    _make_backup_file(tmp_path, "b.1", mtime=100)
    _make_backup_file(tmp_path, "b.2", mtime=200)

    removed = prune_backups(str(tmp_path / "b.*"), max_backups=10)

    assert removed == []
    assert len(list(tmp_path.iterdir())) == 2


def test_prune_noop_when_exactly_at_limit(tmp_path):
    _make_backup_file(tmp_path, "b.1", mtime=100)
    _make_backup_file(tmp_path, "b.2", mtime=200)

    removed = prune_backups(str(tmp_path / "b.*"), max_backups=2)

    assert removed == []


@pytest.mark.parametrize("disabled", [0, -1, None])
def test_prune_disabled_keeps_everything(tmp_path, disabled):
    for i in range(1, 6):
        _make_backup_file(tmp_path, f"b.{i}", mtime=i * 100)

    removed = prune_backups(str(tmp_path / "b.*"), max_backups=disabled)

    assert removed == []
    assert len(list(tmp_path.iterdir())) == 5


def test_prune_no_matches(tmp_path):
    assert prune_backups(str(tmp_path / "nope.*"), max_backups=3) == []


def test_prune_only_touches_matching_pattern(tmp_path):
    """Live data and unrelated files must never be swept up by a backup glob."""
    _make_backup_file(tmp_path, "chroma.sqlite3.max-seq-id-backup-1", mtime=100)
    _make_backup_file(tmp_path, "chroma.sqlite3.max-seq-id-backup-2", mtime=200)
    _make_backup_file(tmp_path, "chroma.sqlite3.max-seq-id-backup-3", mtime=300)
    # The live database and an unrelated file — must survive.
    live = _make_backup_file(tmp_path, "chroma.sqlite3", mtime=400)
    other = _make_backup_file(tmp_path, "tunnels.json", mtime=400)

    prune_backups(
        str(tmp_path / "chroma.sqlite3.max-seq-id-backup-*"),
        max_backups=1,
    )

    assert live.exists()
    assert other.exists()
    assert (tmp_path / "chroma.sqlite3.max-seq-id-backup-3").exists()
    assert not (tmp_path / "chroma.sqlite3.max-seq-id-backup-1").exists()
    assert not (tmp_path / "chroma.sqlite3.max-seq-id-backup-2").exists()


def test_prune_respects_glob_escape_for_metacharacter_paths(tmp_path):
    """Palace paths can contain glob metacharacters like ``[``.

    Without ``glob.escape`` the pattern would silently match nothing (the
    bracket is read as a character class), leaving backups unpruned. Callers
    escape the literal prefix; this confirms the helper prunes correctly once
    they do.
    """
    import glob

    weird = tmp_path / "weird[name]"
    weird.mkdir()
    for i in range(1, 4):
        _make_backup_file(weird, f"chroma.sqlite3.max-seq-id-backup-{i}", mtime=i * 100)

    pattern = os.path.join(glob.escape(str(weird)), "chroma.sqlite3.max-seq-id-backup-*")
    removed = prune_backups(pattern, max_backups=1)

    assert len(removed) == 2
    assert (weird / "chroma.sqlite3.max-seq-id-backup-3").exists()


def test_prune_is_best_effort_on_delete_failure(tmp_path, monkeypatch):
    """A failed deletion is logged and skipped, never raised — pruning must
    not undo a migrate/repair that already succeeded."""
    for i in range(1, 5):
        _make_backup_file(tmp_path, f"b.{i}", mtime=i * 100)

    real_remove = os.remove

    def flaky_remove(path):
        if path.endswith("b.1"):
            raise OSError("permission denied")
        return real_remove(path)

    monkeypatch.setattr(os, "remove", flaky_remove)

    logs = []
    removed = prune_backups(str(tmp_path / "b.*"), max_backups=2, log=logs.append)

    # b.1 and b.2 were over the limit; b.1 failed, b.2 succeeded.
    assert str(tmp_path / "b.2") in removed
    assert str(tmp_path / "b.1") not in removed
    assert (tmp_path / "b.1").exists()
    assert any("could not remove" in line for line in logs)


# ---------------------------------------------------------------------------
# copy_palace_dir (#2207)
# ---------------------------------------------------------------------------


def test_copy_palace_dir_copies_an_ordinary_palace(tmp_path):
    palace = _make_palace(tmp_path)
    dest = tmp_path / "palace.backup"

    skipped = copy_palace_dir(str(palace), str(dest))

    assert skipped == []
    assert (dest / "chroma.sqlite3").read_bytes() == b"SQLite format 3\x00"
    assert (dest / "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee" / "data_level0.bin").is_file()


def test_copy_palace_dir_logs_nothing_when_every_entry_copies(tmp_path):
    palace = _make_palace(tmp_path)
    logs = []

    copy_palace_dir(str(palace), str(tmp_path / "palace.backup"), log=logs.append)

    assert logs == []


@needs_unix_socket
def test_copy_palace_dir_skips_a_socket_and_still_copies_the_database(tmp_path, monkeypatch):
    """The reported #2207 case: a leftover daemon socket beside chroma.sqlite3."""
    palace = _make_palace(tmp_path)
    sock_path = _bind_socket(palace, "mcp.sock", monkeypatch)
    dest = tmp_path / "palace.backup"
    logs = []

    skipped = copy_palace_dir(str(palace), str(dest), log=logs.append)

    assert skipped == [(sock_path, "socket")]
    assert (dest / "chroma.sqlite3").read_bytes() == b"SQLite format 3\x00"
    assert not os.path.lexists(dest / "mcp.sock")
    # Compared line for line, and with one entry, so the singular header and
    # the socket detail line are both pinned. A substring assertion accepts a
    # plural that disagrees with the count and a detail line with anything
    # appended to it.
    assert logs == [
        "  Backup: skipped 1 entry that cannot be copied:",
        "    mcp.sock (socket)",
    ]


@needs_fifo
def test_copy_palace_dir_skips_a_named_pipe(tmp_path):
    palace = _make_palace(tmp_path)
    fifo = palace / "mempalace.fifo"
    os.mkfifo(fifo)
    dest = tmp_path / "palace.backup"

    skipped = copy_palace_dir(str(palace), str(dest))

    assert skipped == [(str(fifo), "named pipe")]
    assert (dest / "chroma.sqlite3").is_file()


def test_copy_palace_dir_still_raises_on_a_symlink_whose_target_is_missing(tmp_path):
    """A link that does not resolve is NOT proof that it carried no data.

    A palace segment can be a link onto another volume, and a volume that is
    not mounted right now fails ``os.stat`` with the same ``ENOENT`` as a
    target that was deleted. On Windows an unmapped drive letter and an
    unreachable network share arrive as ``ENOENT`` too. Skipping on that
    would drop a whole subtree from the safety copy and let the rebuild run
    over the live palace anyway, so the copy has to fail the way it does on
    ``develop``.
    """
    palace = _make_palace(tmp_path)
    link = palace / "chroma.sqlite3.prev"
    _symlink_or_skip(link, palace / "gone-away.sqlite3")
    dest = tmp_path / "palace.backup"

    assert _uncopyable_reason(str(link), follow_symlinks=True) is None
    with pytest.raises(shutil.Error):
        copy_palace_dir(str(palace), str(dest))
    assert not os.path.lexists(dest / "chroma.sqlite3.prev")


@needs_unix_socket
def test_copy_palace_dir_skips_a_symlink_pointing_at_a_socket(tmp_path, monkeypatch):
    """The copy dereferences links by default, so the TARGET's type decides."""
    palace = _make_palace(tmp_path)
    sock_path = _bind_socket(palace, "daemon.sock", monkeypatch)
    link = palace / "mcp.sock"
    _symlink_or_skip(link, palace / "daemon.sock")
    dest = tmp_path / "palace.backup"

    skipped = copy_palace_dir(str(palace), str(dest))

    # Sorted within the directory: "daemon.sock" before "mcp.sock".
    assert skipped == [(sock_path, "socket"), (str(link), "socket")]
    assert (dest / "chroma.sqlite3").is_file()


def test_copy_palace_dir_follows_a_symlink_to_a_real_file(tmp_path):
    """Link handling is unchanged from the plain ``copytree`` this replaced.

    A resolvable link is dereferenced and its content lands in the backup,
    which is what the bare call did. Pinned so the refactor cannot quietly
    alter it, not as an argument that dereferencing is the right default.
    """
    palace = _make_palace(tmp_path)
    (tmp_path / "outside.json").write_text("payload", encoding="utf-8")
    _symlink_or_skip(palace / "tunnels.json", tmp_path / "outside.json")
    dest = tmp_path / "palace.backup"

    skipped = copy_palace_dir(str(palace), str(dest))

    assert skipped == []
    assert (dest / "tunnels.json").read_text(encoding="utf-8") == "payload"


def test_copy_palace_dir_keeps_a_broken_symlink_when_symlinks_are_preserved(tmp_path):
    """``migrate`` copies with ``symlinks=True``, so links are recreated as
    links and their targets are never read."""
    palace = _make_palace(tmp_path)
    _symlink_or_skip(palace / "chroma.sqlite3.prev", palace / "gone-away.sqlite3")
    dest = tmp_path / "palace.pre-migrate.1"

    skipped = copy_palace_dir(str(palace), str(dest), symlinks=True)

    assert skipped == []
    assert (dest / "chroma.sqlite3.prev").is_symlink()


@needs_unix_socket
def test_copy_palace_dir_skips_a_socket_even_when_symlinks_are_preserved(tmp_path, monkeypatch):
    palace = _make_palace(tmp_path)
    sock_path = _bind_socket(palace, "mcp.sock", monkeypatch)
    dest = tmp_path / "palace.pre-migrate.1"

    skipped = copy_palace_dir(str(palace), str(dest), symlinks=True)

    assert skipped == [(sock_path, "socket")]
    assert (dest / "chroma.sqlite3").is_file()


@needs_unix_socket
def test_copy_palace_dir_keeps_a_link_to_a_socket_when_symlinks_are_preserved(
    tmp_path, monkeypatch
):
    """The link's own type decides when the copy recreates links as links.

    ``migrate`` never reads through a link, so a link that happens to point
    at a socket is a link like any other and belongs in the backup. Judging
    it by its target instead would drop it.
    """
    palace = _make_palace(tmp_path)
    _bind_socket(palace, "daemon.sock", monkeypatch)
    _symlink_or_skip(palace / "mcp.sock", palace / "daemon.sock")
    dest = tmp_path / "palace.pre-migrate.1"

    skipped = copy_palace_dir(str(palace), str(dest), symlinks=True)

    assert [reason for _, reason in skipped] == ["socket"]
    assert (dest / "mcp.sock").is_symlink()


@needs_unprivileged_posix
def test_copy_palace_dir_still_raises_on_a_copy_failure_it_cannot_classify(tmp_path):
    """Only provably uncopyable entries are skipped.

    Anything else must still abort the caller: the backup is the safety net
    for a rebuild that is about to overwrite the live palace, so a copy that
    did not come out whole has to be loud.
    """
    palace = _make_palace(tmp_path)
    locked = palace / "locked-segment"
    locked.mkdir()
    (locked / "data_level0.bin").write_bytes(b"\x00")
    locked.chmod(0o000)
    try:
        with pytest.raises(shutil.Error):
            copy_palace_dir(str(palace), str(tmp_path / "palace.backup"))
    finally:
        locked.chmod(0o700)


@needs_unix_socket
def test_uncopyable_reason_names_a_socket(tmp_path, monkeypatch):
    sock_path = _bind_socket(tmp_path, "mcp.sock", monkeypatch)
    assert _uncopyable_reason(sock_path, follow_symlinks=True) == "socket"
    assert _uncopyable_reason(sock_path, follow_symlinks=False) == "socket"


def test_uncopyable_reason_passes_regular_files_and_directories(tmp_path):
    regular = tmp_path / "chroma.sqlite3"
    regular.write_bytes(b"SQLite format 3\x00")
    directory = tmp_path / "segment"
    directory.mkdir()

    for path in (regular, directory):
        assert _uncopyable_reason(str(path), follow_symlinks=True) is None
        assert _uncopyable_reason(str(path), follow_symlinks=False) is None


@needs_unix_socket
def test_uncopyable_reason_judges_a_symlink_by_how_the_copy_treats_it(tmp_path, monkeypatch):
    _bind_socket(tmp_path, "mcp.sock", monkeypatch)
    link = tmp_path / "link-to-sock"
    _symlink_or_skip(link, tmp_path / "mcp.sock")

    # Dereferenced by the copy: the target's type decides.
    assert _uncopyable_reason(str(link), follow_symlinks=True) == "socket"
    # Recreated as a link by the copy: the target is never read.
    assert _uncopyable_reason(str(link), follow_symlinks=False) is None


def test_uncopyable_reason_never_skips_on_a_failed_stat(tmp_path):
    """An entry whose type could not be read is left for the copy to attempt.

    Naming a reason here would skip it silently. A failed ``stat`` says the
    entry cannot be resolved right now, not that it holds no data, and no
    errno separates a deleted target from one on a volume that is not
    mounted, so ``None`` is the only answer that cannot lose data.
    """
    missing = tmp_path / "not-there"
    assert _uncopyable_reason(str(missing), follow_symlinks=True) is None

    link = tmp_path / "onto-a-missing-volume"
    _symlink_or_skip(link, missing)
    assert _uncopyable_reason(str(link), follow_symlinks=True) is None


def test_file_type_label_names_what_it_knows_and_refuses_the_rest():
    """The allowlist needs an answer for a type its table has no name for.

    Nothing on Linux reaches the fallback: ``stat.S_IFDOOR`` and its siblings
    read as ``0`` there. macOS does define ``S_IFWHT``, so a mode carrying it
    would land here, though producing one needs a union mount that no CI
    runner has. The label is therefore exercised directly, rather than left
    unpinned behind a state no platform here can create.
    """
    assert _file_type_label(stat.S_IFSOCK) == "socket"
    assert _file_type_label(stat.S_IFIFO) == "named pipe"
    assert _file_type_label(stat.S_IFCHR) == "character device"
    assert _file_type_label(stat.S_IFBLK) == "block device"
    assert _file_type_label(stat.S_IFLNK) == "not a regular file or directory"


@needs_unprivileged_posix
def test_copy_palace_dir_still_raises_when_a_symlink_target_cannot_be_inspected(tmp_path):
    """The target may be real palace data behind a directory we cannot enter.

    Skipping it would drop live data from the safety copy and let the rebuild
    proceed over the live palace, so the copy has to fail the way it does on
    ``develop``.
    """
    palace = _make_palace(tmp_path)
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "shard.sqlite3").write_text("REAL PALACE DATA", encoding="utf-8")
    _symlink_or_skip(palace / "shard.sqlite3", vault / "shard.sqlite3")
    vault.chmod(0o000)
    dest = tmp_path / "palace.backup"
    try:
        with pytest.raises(shutil.Error):
            copy_palace_dir(str(palace), str(dest))
    finally:
        vault.chmod(0o700)
    assert not (dest / "shard.sqlite3").exists()


def test_copy_palace_dir_still_raises_on_a_symlink_cycle(tmp_path):
    """A cycle resolves to nothing, and that is still not a reason to skip."""
    palace = _make_palace(tmp_path)
    _symlink_or_skip(palace / "loop_a", palace / "loop_b")
    _symlink_or_skip(palace / "loop_b", palace / "loop_a")

    with pytest.raises(shutil.Error):
        copy_palace_dir(str(palace), str(tmp_path / "palace.backup"))


def test_copy_palace_dir_still_raises_on_a_symlink_to_its_own_parent(tmp_path):
    """The directory-cycle form, which bloats the copy rather than failing fast."""
    palace = _make_palace(tmp_path)
    _symlink_or_skip(palace / "self", palace)

    with pytest.raises(shutil.Error):
        copy_palace_dir(str(palace), str(tmp_path / "palace.backup"))


@needs_char_device
def test_copy_palace_dir_skips_a_symlink_to_a_character_device(tmp_path):
    """Device nodes do not make the copy raise; they make it copy the wrong thing.

    Dereferencing a link to ``/dev/null`` writes an empty regular file, and a
    link to ``/dev/zero`` is copied without bound, so both are left out. The
    entry is a link rather than a real node because creating one needs root.
    """
    palace = _make_palace(tmp_path)
    link = palace / "devnull"
    _symlink_or_skip(link, "/dev/null")
    dest = tmp_path / "palace.backup"

    skipped = copy_palace_dir(str(palace), str(dest))

    assert skipped == [(str(link), "character device")]
    assert not os.path.lexists(dest / "devnull")
    assert (dest / "chroma.sqlite3").is_file()


@needs_fifo
@needs_unprivileged_posix
def test_copy_palace_dir_reports_skips_even_when_the_copy_fails(tmp_path):
    """The skip list is what the operator needs most when the copy died.

    The copy is made to fail partway through rather than before it starts, so
    this really is the half-written-backup case and does not lean on the
    order in which ``shutil`` happens to call the ignore callback.
    """
    palace = _make_palace(tmp_path)
    os.mkfifo(palace / "aaa.fifo")
    locked = palace / "locked-segment"
    locked.mkdir()
    (locked / "data_level0.bin").write_bytes(b"\x00")
    locked.chmod(0o000)
    dest = tmp_path / "palace.backup"
    logs = []

    try:
        with pytest.raises(shutil.Error):
            copy_palace_dir(str(palace), str(dest), log=logs.append)
    finally:
        locked.chmod(0o700)

    assert any("aaa.fifo (named pipe)" in line for line in logs)
    assert (dest / "chroma.sqlite3").is_file()


@needs_fifo
@needs_unprivileged_posix
def test_copy_palace_dir_report_never_replaces_the_copys_own_failure(tmp_path):
    """A failing ``log`` must not become the error the caller diagnoses.

    Both callers pass ``print``, so the report can fail on its own: a closed
    pipe (``mempalace repair | head``) or an stdout that cannot encode a
    skipped entry's name. Letting that out of the failure path would hand the
    caller that error instead of the reason the backup did not come out whole.
    """
    palace = _make_palace(tmp_path)
    os.mkfifo(palace / "leftover.fifo")
    locked = palace / "locked-segment"
    locked.mkdir()
    (locked / "data_level0.bin").write_bytes(b"\x00")
    locked.chmod(0o000)
    dest = tmp_path / "palace.backup"

    def log_that_dies(_line):
        # Not one of the terminal failures the per-line guard absorbs, so
        # this is the report escaping as far as it ever can.
        raise RuntimeError("logging is broken")

    try:
        with pytest.raises(shutil.Error):
            copy_palace_dir(str(palace), str(dest), log=log_that_dies)
    finally:
        locked.chmod(0o700)


@needs_fifo
@needs_unprivileged_posix
def test_copy_palace_dir_says_nothing_when_the_destination_cannot_be_created(tmp_path):
    """The other way the copy never starts: the destination's parent is closed.

    ``os.path.lexists`` reads False there as well, so the before-and-after
    snapshot alone would report a skip list for a backup that does not exist.
    """
    palace = _make_palace(tmp_path)
    os.mkfifo(palace / "leftover.fifo")
    closed = tmp_path / "closed"
    closed.mkdir()
    closed.chmod(0o500)  # readable, not writable
    logs = []

    try:
        with pytest.raises(PermissionError):
            copy_palace_dir(str(palace), str(closed / "palace.backup"), log=logs.append)
    finally:
        closed.chmod(0o700)

    assert logs == []


@needs_fifo
def test_copy_palace_dir_surfaces_an_unusable_log_when_the_copy_succeeded(tmp_path):
    """Best-effort covers the terminal, not a caller who passed no logger.

    Suppressing this too would hide an integration bug behind a backup that
    reports success. The backup is already complete when it surfaces, and
    nothing destructive has run yet.
    """
    palace = _make_palace(tmp_path)
    os.mkfifo(palace / "leftover.fifo")

    with pytest.raises(TypeError):
        copy_palace_dir(str(palace), str(tmp_path / "palace.backup"), log="not-a-callable")


@needs_fifo
@pytest.mark.parametrize("shape", ["directory", "regular file", "dangling symlink"])
def test_copy_palace_dir_says_nothing_about_a_backup_it_never_started(tmp_path, shape):
    """``copytree`` classifies the top directory before it creates the
    destination, so a destination it refuses still yields a skip list. It
    describes a backup that does not exist, whatever is sitting in the way."""
    palace = _make_palace(tmp_path)
    os.mkfifo(palace / "leftover.fifo")
    dest = tmp_path / "palace.backup"
    if shape == "directory":
        dest.mkdir()
    elif shape == "regular file":
        dest.write_text("in the way", encoding="utf-8")
    else:
        _symlink_or_skip(dest, tmp_path / "gone-away")
    logs = []

    with pytest.raises(FileExistsError):
        copy_palace_dir(str(palace), str(dest), log=logs.append)

    assert logs == []


@needs_fifo
def test_copy_palace_dir_survives_a_failing_report_on_a_good_copy(tmp_path):
    """The report is best-effort, like ``prune_backups``' own cleanup."""
    palace = _make_palace(tmp_path)
    os.mkfifo(palace / "leftover.fifo")
    dest = tmp_path / "palace.backup"

    def log_that_dies(_line):
        raise BrokenPipeError(32, "Broken pipe")

    skipped = copy_palace_dir(str(palace), str(dest), log=log_that_dies)

    assert [reason for _, reason in skipped] == ["named pipe"]
    assert (dest / "chroma.sqlite3").is_file()


@needs_fifo
def test_copy_palace_dir_reports_skips_as_palace_relative_lines(tmp_path):
    """The whole report is pinned, not just fragments of it.

    Substring assertions accept an absolute path, a wrong plural and a lost
    indent alike, so the rendering is compared line for line instead.
    """
    palace = _make_palace(tmp_path)
    os.mkfifo(palace / "aaa.fifo")
    os.mkfifo(palace / "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee" / "bbb.fifo")
    logs = []

    copy_palace_dir(str(palace), str(tmp_path / "palace.backup"), log=logs.append)

    assert logs == [
        "  Backup: skipped 2 entries that cannot be copied:",
        "    aaa.fifo (named pipe)",
        f"    aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee{os.sep}bbb.fifo (named pipe)",
    ]


@needs_fifo
def test_copy_palace_dir_report_does_not_swallow_an_interrupt(tmp_path):
    """Best-effort covers failures, not the operator pressing Ctrl-C."""
    palace = _make_palace(tmp_path)
    os.mkfifo(palace / "leftover.fifo")

    def log_that_aborts(_line):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        copy_palace_dir(str(palace), str(tmp_path / "palace.backup"), log=log_that_aborts)


@needs_fifo
def test_copy_palace_dir_reports_skips_in_a_stable_order(tmp_path):
    """Within one directory the order is sorted, not whatever ``scandir`` gave.

    Across directories it stays the order the copy visited them in, which is
    filesystem-dependent; only the within-a-directory half is a promise.
    """
    palace = _make_palace(tmp_path)
    for name in ("zzz.fifo", "mmm.fifo", "aaa.fifo"):
        os.mkfifo(palace / name)

    skipped = copy_palace_dir(str(palace), str(tmp_path / "palace.backup"))

    names = [os.path.basename(path) for path, _ in skipped]
    assert names == ["aaa.fifo", "mmm.fifo", "zzz.fifo"]


@needs_fifo
def test_copy_palace_dir_keeps_directories_in_visit_order(tmp_path):
    """Directories stay in the order the copy walked them, not sorted globally.

    The nested entry's full path sorts before the top-level one, so sorting
    the whole list at the end would swap them and break the promise the
    docstring makes.
    """
    palace = _make_palace(tmp_path)
    os.mkfifo(palace / "zzz.fifo")
    os.mkfifo(palace / "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee" / "bbb.fifo")
    logs = []

    copy_palace_dir(str(palace), str(tmp_path / "palace.backup"), log=logs.append)

    assert logs == [
        "  Backup: skipped 2 entries that cannot be copied:",
        "    zzz.fifo (named pipe)",
        f"    aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee{os.sep}bbb.fifo (named pipe)",
    ]


@needs_fifo
def test_copy_palace_dir_reports_skips_when_the_copy_is_interrupted(tmp_path, monkeypatch):
    """Ctrl-C during a long copy still tells the operator what was left out.

    That is why the copy is wrapped in ``except BaseException`` rather than
    ``except Exception``: the half-written backup is real, and what it lacks
    is what the operator has to know before deciding what to do with it.
    """
    palace = _make_palace(tmp_path)
    os.mkfifo(palace / "leftover.fifo")
    logs = []

    def interrupt(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr("mempalace.backups.shutil.copystat", interrupt)

    with pytest.raises(KeyboardInterrupt):
        copy_palace_dir(str(palace), str(tmp_path / "palace.backup"), log=logs.append)

    assert logs == [
        "  Backup: skipped 1 entry that cannot be copied:",
        "    leftover.fifo (named pipe)",
    ]


@needs_fifo
def test_copy_palace_dir_names_an_entry_it_cannot_make_relative(tmp_path, monkeypatch):
    """``os.path.relpath`` calls ``os.getcwd()`` and rejects other drives.

    Neither is a reason to drop the entry from the report, so the absolute
    path stands in for the palace-relative one.
    """
    palace = _make_palace(tmp_path)
    fifo = palace / "leftover.fifo"
    os.mkfifo(fifo)
    logs = []

    def no_relpath(*_args, **_kwargs):
        raise ValueError("path is on mount 'C:', start on mount 'D:'")

    monkeypatch.setattr("mempalace.backups.os.path.relpath", no_relpath)

    copy_palace_dir(str(palace), str(tmp_path / "palace.backup"), log=logs.append)

    assert logs == [
        "  Backup: skipped 1 entry that cannot be copied:",
        f"    {fifo} (named pipe)",
    ]


@needs_fifo
def test_copy_palace_dir_escapes_a_name_the_terminal_cannot_encode(tmp_path):
    """A name stdout cannot encode is escaped, not dropped.

    The count in the header comes from the copy, so dropping the line would
    leave the operator reading "skipped 3" above a list of two. Retried in
    ASCII, the entry is still named.
    """
    palace = _make_palace(tmp_path)
    for name in ("aaa.fifo", "mü.fifo", "zzz.fifo"):
        os.mkfifo(palace / name)
    logs = []

    def log_that_cannot_encode_one(line):
        # Stands in for an stdout whose encoding cannot carry the name.
        if not line.isascii():
            raise UnicodeEncodeError("ascii", line, 0, 1, "cannot encode")
        logs.append(line)

    copy_palace_dir(str(palace), str(tmp_path / "palace.backup"), log=log_that_cannot_encode_one)

    assert logs == [
        "  Backup: skipped 3 entries that cannot be copied:",
        "    aaa.fifo (named pipe)",
        "    m\\xfc.fifo (named pipe)",
        "    zzz.fifo (named pipe)",
    ]


@needs_fifo
def test_copy_palace_dir_report_survives_a_line_that_cannot_be_written(tmp_path):
    """A per-line write failure must not cost the operator the other lines."""
    palace = _make_palace(tmp_path)
    for name in ("aaa.fifo", "mmm.fifo", "zzz.fifo"):
        os.mkfifo(palace / name)
    logs = []

    def log_that_dies_once(line):
        if "mmm.fifo" in line:
            raise BrokenPipeError(32, "Broken pipe")
        logs.append(line)

    copy_palace_dir(str(palace), str(tmp_path / "palace.backup"), log=log_that_dies_once)

    assert logs == [
        "  Backup: skipped 3 entries that cannot be copied:",
        "    aaa.fifo (named pipe)",
        "    zzz.fifo (named pipe)",
    ]
