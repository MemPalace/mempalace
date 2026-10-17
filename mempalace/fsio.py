"""Durable replacement of palace state files.

``tempfile.mkstemp`` is not used. On Windows it treats ``PermissionError`` as
a name collision and retries ``TMP_MAX`` times (hundreds of thousands, and on
older Pythons ``os.TMP_MAX``), which reads as a hang. Callers that need the
permission error — a directory that will take no new name — never see it.
The opener below retries only ``FileExistsError``, and only
``TEMP_NAME_ATTEMPTS`` times. (#2530, #2531)

A publish writes a temporary file in the destination directory, fsyncs it,
then ``os.replace``s it into place and fsyncs the directory. An exception
before the replace leaves the previous file untouched and removes the
temporary file.
"""

import errno
import json
import os
import stat
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional, Union

PathLike = Union[str, os.PathLike]

# Only a name already taken is retried. Every other OSError reaches the caller.
TEMP_NAME_ATTEMPTS = 8

_IN_PLACE_ERRNOS = (errno.EACCES, errno.EPERM, errno.EROFS)

# A filesystem without fsync answers with one of these; the write still lands.
_NO_FSYNC_ERRNOS = tuple(
    code for code in (errno.EINVAL, getattr(errno, "ENOTSUP", None), errno.EOPNOTSUPP) if code
)

# Windows refuses to replace a file another process holds open (access denied,
# sharing violation). Readers let go quickly, so the rename is retried.
_SHARING_WINERRORS = (5, 32)
_REPLACE_BACKOFF = (0.01, 0.05, 0.1, 0.25, 0.5)


def unlink_quietly(path: PathLike) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def fsync_directory(directory: PathLike) -> None:
    """Make a rename in ``directory`` durable.

    On ext4 the kernel can acknowledge a rename and, after a crash, come
    back with the temporary file still present and the target holding the
    old bytes. Windows cannot open a directory this way; answering nothing
    there matches a filesystem that does not implement directory fsync.
    """
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _fsync_file(fd: int) -> None:
    try:
        os.fsync(fd)
    except OSError as exc:
        if exc.errno not in _NO_FSYNC_ERRNOS:
            raise


def write_target(path: PathLike) -> Path:
    """The file a write should replace, following a symlink to it.

    Replacing the link itself would leave the real file unchanged. A link
    this call cannot ``lstat`` is raised rather than treated as a non-link:
    that reading would put a regular file where the link was.
    """
    path = Path(path)
    try:
        is_link = stat.S_ISLNK(os.lstat(str(path)).st_mode)
    except FileNotFoundError:
        return path
    if is_link:
        return Path(os.path.realpath(str(path)))
    return path


def open_new_temp_file(directory: PathLike, prefix: str, suffix: str = ""):
    """Create one new file under ``directory`` and return ``(fd, path)``.

    Only ``FileExistsError`` is retried, and only ``TEMP_NAME_ATTEMPTS``
    times. ``EPERM``, ``EACCES`` and ``EROFS`` propagate so the caller can
    fall back. Exhausting the attempts raises ``FileExistsError``.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    directory = str(directory)
    for _ in range(TEMP_NAME_ATTEMPTS):
        candidate = os.path.join(directory, f"{prefix}{os.urandom(6).hex()}{suffix}")
        try:
            return os.open(candidate, flags, 0o600), candidate
        except FileExistsError:
            continue
    raise FileExistsError(
        errno.EEXIST,
        f"no usable temporary file name found in {directory} after {TEMP_NAME_ATTEMPTS} attempts",
    )


def _chmod_quiet(path: PathLike, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except (OSError, NotImplementedError):
        pass


def write_in_place(path: PathLike, write_body: Callable[[Any], None]) -> None:
    """Write without a rename, for a directory that refuses a new name.

    This truncates ``path`` before the new bytes are durable. The publish
    uses it only when the directory will not take a temporary file.
    """
    with open(path, "w", encoding="utf-8") as handle:
        write_body(handle)
        handle.flush()
        _fsync_file(handle.fileno())
    _chmod_quiet(path, 0o600)


def _fill_temp(fd: int, tmp: str, write_body: Callable[[Any], None]) -> None:
    """Serialize into ``fd`` and fsync before anything is published.

    The mode is set before the write. Under a umask that clears the owner's
    write bit, ``O_CREAT`` leaves the file at 0400, and one left behind by a
    killed run is then a name its owner cannot open.
    """
    try:
        _chmod_quiet(tmp, 0o600)
        os.ftruncate(fd, 0)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            write_body(handle)
            handle.flush()
            _fsync_file(handle.fileno())
    except BaseException:
        unlink_quietly(tmp)
        raise


def _open_publish_temp(path: Path):
    """Return ``(fd, tmp)`` for a publish into ``path``.

    The pid-named file is tried first. ``O_NOFOLLOW`` keeps a symlink at that
    name from being written through. A hard link is not a symlink, so a
    second link is rejected before the truncate: truncating it would empty a
    file nobody named here. Anything else in the way of the pid name asks
    the directory for a name of its own.
    """
    tmp = str(path.with_name(f"{path.name}.tmp-{os.getpid()}"))
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(tmp, flags, 0o600)
        try:
            linked = os.fstat(fd).st_nlink > 1
        except BaseException:
            os.close(fd)
            raise
        if linked:
            os.close(fd)
            raise OSError(errno.EMLINK, "temporary name has another link", tmp)
    except OSError:
        fd, tmp = open_new_temp_file(path.parent, prefix=f".{path.name}.", suffix=".tmp")
    return fd, tmp


def _replace(tmp: str, path: Path) -> None:
    for delay in _REPLACE_BACKOFF:
        try:
            os.replace(tmp, str(path))
            return
        except OSError as exc:
            if getattr(exc, "winerror", None) not in _SHARING_WINERRORS:
                raise
        time.sleep(delay)
    os.replace(tmp, str(path))


def _warn_in_place(path: Path, *, why: str) -> None:
    print(
        f"  ! {path.parent} would not take {why}, so {path.name} was "
        "written in place and an interrupted write can truncate it",
        file=sys.stderr,
    )


def _finish_failed_replace(
    exc: BaseException, tmp: str, path: Path, write_body: Callable[[Any], None]
) -> None:
    """Publish in place when the directory itself refused the rename.

    That happens on POSIX when an orphan at the pid name was reused, so the
    directory first answers at the rename. A Windows error is a file another
    process still holds after the retries; writing through it would let that
    reader see a torn file, so it is raised. So is anything not an
    ``OSError`` (a signal between the write and the rename) and any errno the
    fallback is not for. The temporary file holds a finished, fsynced copy:
    it is removed only after the in-place write returns.
    """
    if (
        not isinstance(exc, OSError)
        or exc.errno not in _IN_PLACE_ERRNOS
        or getattr(exc, "winerror", None) is not None
    ):
        unlink_quietly(tmp)
        raise exc
    try:
        write_in_place(path, write_body)
    except BaseException:
        print(
            f"  ! {path.name} was not written and may have been truncated; "
            f"{tmp} holds what this call was asked to save",
            file=sys.stderr,
        )
        raise
    unlink_quietly(tmp)
    if os.path.exists(tmp):
        print(
            f"  ! {tmp} holds a copy of what was written and could not be "
            "removed; nothing here removes it later either",
            file=sys.stderr,
        )
    _warn_in_place(path, why="the rename")


def atomic_publish(path: PathLike, write_body: Callable[[Any], None]) -> None:
    """Replace ``path`` with what ``write_body`` writes, or leave it untouched.

    The write falls back to ``write_in_place`` only when the directory
    refuses a new name (``EACCES``, ``EPERM``, ``EROFS``) while the
    destination itself may still be writable. Every other failure propagates
    and does not truncate ``path``.
    """
    path = write_target(path)
    try:
        fd, tmp = _open_publish_temp(path)
    except OSError as exc:
        if exc.errno not in _IN_PLACE_ERRNOS:
            raise
        write_in_place(path, write_body)
        _warn_in_place(path, why="a temporary file")
        return

    _fill_temp(fd, tmp, write_body)
    try:
        _replace(tmp, path)
    except BaseException as exc:
        _finish_failed_replace(exc, tmp, path, write_body)
        return
    fsync_directory(path.parent)


def atomic_write_json(path: PathLike, payload: Any) -> None:
    """Serialize ``payload`` (indented, UTF-8, non-ASCII kept) into ``path``."""
    atomic_publish(path, lambda handle: json.dump(payload, handle, indent=2, ensure_ascii=False))


def atomic_write_text(path: PathLike, text: str) -> None:
    """Replace ``path`` with ``text``. Same crash rules as ``atomic_write_json``."""
    atomic_publish(path, lambda handle: handle.write(text))


class CorruptStateError(OSError):
    """A state file did not parse and must not be overwritten.

    ``kept_at`` is where the previous bytes were moved, when the directory
    allowed the rename. The original path is not replaced with an empty
    or partial document.
    """

    def __init__(self, path: PathLike, reason: str, kept_at: Optional[PathLike] = None):
        self.path = os.fspath(path)
        self.reason = reason
        self.kept_at = None if kept_at is None else os.fspath(kept_at)
        message = f"{self.path} {reason}"
        if self.kept_at:
            message += f"; kept the previous bytes at {self.kept_at}"
        message += "; refusing to overwrite it"
        super().__init__(message)


def refuse_unparsed(path: PathLike, reason: str) -> None:
    """Move ``path`` aside and raise. Never returns.

    A failed rename leaves the file where it was and still raises, so the
    caller cannot write a fresh document over bytes it did not understand.
    """
    try:
        kept = move_aside(path, label="corrupt")
    except OSError as exc:
        raise CorruptStateError(path, f"{reason} and could not be moved aside ({exc})") from exc
    raise CorruptStateError(path, reason, kept_at=kept)


def read_json_for_update(path: PathLike, *, expect: Optional[type] = None):
    """Parse JSON that a later save is allowed to replace.

    A missing file returns ``None`` (the caller may create one). A file
    that cannot be read raises :class:`CorruptStateError` and stays put.
    Bytes that are not JSON, or JSON of the wrong top-level type, are
    renamed to ``<name>.corrupt-<stamp>-<random>`` and then refused.
    """
    path = Path(path)
    if not os.path.lexists(path):
        return None
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise CorruptStateError(path, f"could not be read ({exc})") from exc
    try:
        loaded = json.loads(raw.decode("utf-8-sig"))
    except ValueError:
        refuse_unparsed(path, "does not parse")
    if expect is not None and not isinstance(loaded, expect):
        refuse_unparsed(path, f"is not a JSON {expect.__name__}")
    return loaded


def move_aside(path: PathLike, *, label: str) -> Optional[str]:
    """Rename ``path`` to ``<name>.<label>-<stamp>-<random>``, keeping its bytes.

    Returns the new path, or ``None`` when nothing was there. A failure
    other than the file already being gone leaves it where it was and
    raises: a file that could not be moved is not one to write over.
    """
    path = write_target(path)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    fd, target = open_new_temp_file(path.parent, prefix=f"{path.name}.{label}-{stamp}-")
    os.close(fd)
    try:
        os.replace(str(path), target)
    except FileNotFoundError:
        unlink_quietly(target)
        return None
    except OSError:
        unlink_quietly(target)
        raise
    return target
