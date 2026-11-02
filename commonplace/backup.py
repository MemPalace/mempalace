"""Offline, verified backups of a Commonplace data directory.

CLI database users hold ``data_lock`` for their whole lifetime. Backups take
its exclusive form before inspecting or opening any database. Linux /proc is
also checked for workers left behind by a stopped gateway. Other platforms
refuse backup until equivalent worker verification is available.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

LOCK_NAME = ".commonplace.lock"
_SQLITE_HEADER = b"SQLite format 3\x00"
_PROC_ROOT = Path("/proc")


class BackupError(ValueError):
    """A backup cannot safely be completed."""


@contextmanager
def data_lock(data_dir, *, exclusive=False):
    """Hold a nonblocking data-directory lock; never remove the lock file.

    Serve and CLI commands use the default shared lock. Windows uses an
    exclusive byte lock for both modes, safely serializing CLI commands.
    """
    root = Path(data_dir).expanduser()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(root / LOCK_NAME, flags, 0o600)
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt

            # A byte beyond EOF may be locked without modifying the file.
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            fcntl.flock(fd, mode | fcntl.LOCK_NB)
        acquired = True
        yield
    except OSError as exc:
        if not acquired and exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
            raise BackupError("data directory is in use; stop Commonplace before backup") from exc
        raise
    finally:
        if acquired:
            if os.name == "nt":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _names_worker(arg: bytes) -> bool:
    """Match worker names without treating multiword wrapper commands as workers.

    Keep absolute script paths with spaces as candidates. Single-word wrapper
    commands remain candidates too, conservatively failing closed.
    """
    return (b"mempalace" in arg or b"commonplace" in arg) and (
        arg.startswith(b"/") or len(arg.split()) == 1
    )


def _process_path(value: str, proc: Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = proc / "cwd" / path
    return path.resolve()


def _verify_offline(root: Path) -> None:
    """Reject live processes naming this data tree, including orphan workers."""
    proc_root = _PROC_ROOT
    if not sys.platform.startswith("linux") or not (proc_root / "self" / "environ").exists():
        raise BackupError("offline worker verification requires Linux /proc")
    # This process and its ancestors (a shell, sudo, docker-init under
    # ``init: true``) carry our command line and environment but write nothing.
    own = _ancestors(proc_root)
    for proc in proc_root.iterdir():
        if not proc.name.isdigit() or int(proc.name) in own:
            continue
        try:
            # Workers inherit the gateway's uid. Other users' private process
            # environments are not inspectable on an ordinary Linux host.
            if proc.stat().st_uid != os.getuid():
                continue
            args = (proc / "cmdline").read_bytes().split(b"\x00")
            executable = Path(os.fsdecode(args[0])).name.lower() if args[0] else ""
            named_worker = _names_worker(os.fsencode(executable)) or any(
                _names_worker(arg) for arg in args
            )
            if not executable.startswith(("python", "pypy")) and not named_worker:
                # Services such as systemd --user can deny environment access
                # even to their own uid; they cannot be our Python workers.
                continue
            env = dict(
                entry.split(b"=", 1)
                for entry in (proc / "environ").read_bytes().split(b"\x00")
                if b"=" in entry
            )
            for name in (
                b"MEMPALACE_PALACE_PATH",
                b"MEMPALACE_PALACE",
                b"MEMPAL_PALACE_PATH",
                b"MEMPALACE_CONFIG_DIR",
                b"COMMONPLACE_DATA",
            ):
                value = env.get(name)
                if value and _inside(_process_path(os.fsdecode(value), proc), root):
                    raise BackupError(f"data directory is in use by worker process {proc.name}")
            if named_worker:
                for index, arg in enumerate(args):
                    value = None
                    if arg in (b"--palace", b"--data") and index + 1 < len(args):
                        value = args[index + 1]
                    elif arg.startswith((b"--palace=", b"--data=")):
                        value = arg.split(b"=", 1)[1]
                    if value and _inside(_process_path(os.fsdecode(value), proc), root):
                        raise BackupError(f"data directory is in use by process {proc.name}")
        except (FileNotFoundError, ProcessLookupError):
            continue  # Process exited during the check.
        except PermissionError as exc:
            raise BackupError(f"cannot verify whether process {proc.name} uses this data") from exc


def _inventory(root: Path) -> tuple[list[Path], list[Path]]:
    files, dirs = [], []

    def visit(directory: Path):
        for path in sorted(directory.iterdir()):
            relative = path.relative_to(root)
            if relative in (Path(LOCK_NAME), Path("manifest.json")):
                # A restored backup can itself be backed up; its previous
                # manifest is replaced, never listed as a user-data file.
                continue
            # SubprocessLauncher shares the operator's downloaded model cache
            # here by symlink. It is disposable and never contains user memory.
            if (
                len(relative.parts) == 5
                and relative.parts[0] == "workspaces"
                and relative.parts[2:] == ("home", ".cache", "chroma")
            ):
                continue
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise BackupError(f"refusing symlink in data directory: {relative}")
            if stat.S_ISDIR(mode):
                dirs.append(path)
                visit(path)
            elif stat.S_ISREG(mode):
                files.append(path)
            else:
                raise BackupError(f"refusing special file in data directory: {relative}")

    visit(root)
    return files, dirs


def _is_sqlite(path: Path) -> bool:
    with path.open("rb") as stream:
        return stream.read(16) == _SQLITE_HEADER


def _snapshot(source: Path, target: Path) -> None:
    deadline = time.monotonic() + 30

    def progress(_status, _remaining, _total):
        if time.monotonic() > deadline:
            raise BackupError(f"database backup timed out: {source.name}")

    src = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=1)
    try:
        dst = sqlite3.connect(target)
        try:
            src.backup(dst, pages=256, progress=progress, sleep=0.01)
            dst.execute("PRAGMA journal_mode = DELETE")
            if dst.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise BackupError(f"database integrity check failed: {source.name}")
        finally:
            dst.close()
    finally:
        src.close()
    target.chmod(0o600)


def _file_entry(path: Path, root: Path) -> dict:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return {
        "path": path.relative_to(root).as_posix(),
        "size": path.stat().st_size,
        "sha256": digest.hexdigest(),
    }


def verify_backup(backup_dir) -> dict:
    """Check a snapshot's complete file inventory, hashes and SQLite integrity.

    The manifest detects damage, not malicious replacement: trust the encrypted
    repository and its access controls. No database is opened for writing.
    """
    root = Path(backup_dir).expanduser().absolute()
    if root.is_symlink() or not root.is_dir():
        raise BackupError("backup must be an existing directory, without a symlink")
    root = root.resolve()
    manifest_path = root / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise BackupError("backup has no regular manifest.json")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise BackupError("invalid backup manifest") from exc
    if not isinstance(manifest, dict) or manifest.get("version") != 1:
        raise BackupError("unsupported backup manifest")
    entries = manifest.get("files")
    if not isinstance(entries, list):
        raise BackupError("invalid backup file inventory")
    expected = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise BackupError("invalid backup file entry")
        name = entry.get("path")
        if (
            not isinstance(name, str)
            or not name
            or "\\" in name
            or Path(name).is_absolute()
            or any(part in ("", ".", "..") for part in name.split("/"))
            or name in expected
            or type(entry.get("size")) is not int
            or entry["size"] < 0
            or not isinstance(entry.get("sha256"), str)
        ):
            raise BackupError("invalid backup file entry")
        expected[name] = entry
    files, _ = _inventory(root)
    actual = {path.relative_to(root).as_posix(): path for path in files}
    if expected.keys() != actual.keys() or "control.sqlite3" not in actual:
        raise BackupError("backup file inventory does not match manifest")
    for name, path in actual.items():
        if _file_entry(path, root) != expected[name]:
            raise BackupError(f"backup size or hash mismatch: {name}")
        if _is_sqlite(path):
            if any(name + suffix in actual for suffix in ("-wal", "-shm", "-journal")):
                raise BackupError(f"backup contains SQLite sidecars: {name}")
            # Published snapshots have no WAL; immutable avoids creating SHM
            # files or otherwise modifying a snapshot being checked.
            conn = sqlite3.connect(path.as_uri() + "?immutable=1", uri=True)
            try:
                if conn.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                    raise BackupError(f"database integrity check failed: {name}")
            finally:
                conn.close()
    if not _is_sqlite(actual["control.sqlite3"]):
        raise BackupError("backup has no valid control.sqlite3")
    return manifest


def restore(backup_dir, dest_dir) -> dict:
    """Verify and atomically restore into a new, offline scratch directory.

    Existing data is never overwritten. The returned duration covers validation,
    copying and validation of the restored files, not off-host retrieval.
    """
    started = time.monotonic()
    source = Path(backup_dir).expanduser().absolute()
    manifest = verify_backup(source)
    source = source.resolve()
    destination = Path(dest_dir).expanduser().absolute()
    if destination.is_symlink() or destination.exists():
        raise BackupError("restore destination already exists")
    destination = destination.resolve()
    if _inside(destination, source) or _inside(source, destination):
        raise BackupError("restore destination must be outside the backup directory")
    if not destination.parent.is_dir():
        raise BackupError("restore destination parent must exist")
    partial = Path(tempfile.mkdtemp(prefix=f".{destination.name}.partial-", dir=destination.parent))
    try:
        files, directories = _inventory(source)
        for directory in directories:
            (partial / directory.relative_to(source)).mkdir(mode=0o700)
        for path in [*files, source / "manifest.json"]:
            target = partial / path.relative_to(source)
            shutil.copyfile(path, target)
            target.chmod(0o600)
        verify_backup(partial)
        if destination.exists() or destination.is_symlink():
            raise BackupError("restore destination already exists")
        partial.rename(destination)
    finally:
        if partial.exists():
            shutil.rmtree(partial)
    return {
        "files": len(manifest["files"]),
        "bytes": sum(entry["size"] for entry in manifest["files"]),
        "elapsed_seconds": round(time.monotonic() - started, 6),
    }


def backup(data_dir, dest_dir) -> dict:
    """Publish a complete backup at a new destination and return its manifest.

    Every SQLite file (including Chroma and historical workspace state) uses
    SQLite's backup API, which includes committed WAL rows. WAL/SHM/journal
    sidecars are omitted so they cannot overwrite a restored snapshot. The
    entire remaining tree is copied verbatim, excluding the shared model cache.
    ``manifest.json`` inventories every other output file with size and SHA256.
    """
    source = Path(data_dir).expanduser().absolute()
    destination = Path(dest_dir).expanduser().absolute()
    if source.is_symlink() or not source.is_dir():
        raise BackupError("data directory must be an existing directory, without a symlink")
    source = source.resolve()
    destination = destination.resolve()
    if _inside(destination, source) or _inside(source, destination):
        raise BackupError("backup destination must be outside the data directory")
    if destination.exists():
        raise BackupError("backup destination already exists")
    if not destination.parent.is_dir():
        raise BackupError("backup destination parent must exist")
    partial = None
    with data_lock(source, exclusive=True):
        try:
            _verify_offline(source)
            files, directories = _inventory(source)
            control = source / "control.sqlite3"
            databases = {path for path in files if _is_sqlite(path)}
            if control not in databases:
                raise BackupError("data directory has no valid control.sqlite3")
            sidecars = {
                Path(str(path) + suffix)
                for path in databases
                for suffix in ("-wal", "-shm", "-journal")
            }
            partial = Path(
                tempfile.mkdtemp(prefix=f".{destination.name}.partial-", dir=destination.parent)
            )
            partial.chmod(0o700)
            for directory in directories:
                (partial / directory.relative_to(source)).mkdir(mode=0o700)
            entries = []
            for path in files:
                if path in sidecars:
                    continue
                relative = path.relative_to(source)
                target = partial / relative
                if path in databases:
                    _snapshot(path, target)
                else:
                    shutil.copyfile(path, target)
                    target.chmod(0o600)
                entries.append(_file_entry(target, partial))
            _verify_offline(source)
            manifest = {
                "version": 1,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "files": entries,
            }
            manifest_path = partial / "manifest.json"
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            manifest_path.chmod(0o600)
            if destination.exists():
                raise BackupError("backup destination already exists")
            partial.rename(destination)
            partial = None
            return manifest
        finally:
            if partial is not None:
                shutil.rmtree(partial)


def _ancestors(proc_root) -> set:
    """This process's pid and every parent pid up to init."""
    pids = set()
    pid = os.getpid()
    while pid > 0 and pid not in pids:
        pids.add(pid)
        try:
            stat = (proc_root / str(pid) / "stat").read_text()
            pid = int(stat.rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
    return pids
