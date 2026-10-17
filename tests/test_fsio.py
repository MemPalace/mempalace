"""The shared publisher must not hang on Windows, and must not truncate.

``tempfile.mkstemp`` treats ``PermissionError`` as a name collision when
``os.name == "nt"`` and the directory is writable, and retries ``TMP_MAX``
times. ``config`` used to call it. A crash while the new bytes are still in
the temporary file must leave the destination byte-for-byte as it was.
"""

import errno
import json
import os
import re
import stat
import subprocess
import sys
import time

import pytest

from mempalace import fsio


def _forbid_mkstemp(*args, **kwargs):
    raise AssertionError("tempfile.mkstemp is the Windows name sweep")


def test_open_new_temp_file_does_not_retry_permission_error(tmp_path, monkeypatch):
    """A permission error is the directory answering, not a taken name."""
    attempts = {"n": 0}

    def not_permitted(path, flags, *args, **kwargs):
        attempts["n"] += 1
        raise PermissionError(errno.EACCES, "Access is denied")

    monkeypatch.setattr(os, "open", not_permitted)

    with pytest.raises(PermissionError):
        fsio.open_new_temp_file(tmp_path, prefix=".state.")

    assert attempts["n"] == 1


def test_open_new_temp_file_retries_only_collisions(tmp_path, monkeypatch):
    real_open = os.open
    calls = {"n": 0}

    def always_exists(path, flags, *args, **kwargs):
        if os.path.basename(str(path)).startswith("probe."):
            calls["n"] += 1
            raise FileExistsError(errno.EEXIST, "File exists")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", always_exists)

    with pytest.raises(FileExistsError):
        fsio.open_new_temp_file(tmp_path, prefix="probe.")

    assert calls["n"] == fsio.TEMP_NAME_ATTEMPTS


def test_windows_mkstemp_would_sweep_but_config_publish_does_not(tmp_path, monkeypatch, capsys):
    """The old config publisher called ``mkstemp`` after the pid name failed.

    On Windows that call treats ``PermissionError`` as a taken name and
    retries ``TMP_MAX`` times. Publishing must not enter ``mkstemp`` at all,
    and a permission error on the names the directory picks is one answer,
    not a sweep.
    """
    dest = tmp_path / "config.json"
    dest.write_bytes(b'{"palace_path": "/keep"}\n')
    real_open = os.open
    attempts = []

    def not_permitted(path, flags, *args, **kwargs):
        name = os.path.basename(str(path))
        if name.startswith("config.json.") or name.startswith(".config.json."):
            attempts.append(name)
            if len(attempts) > fsio.TEMP_NAME_ATTEMPTS + 1:
                raise AssertionError(f"temporary-name sweep reached {len(attempts)}")
            raise PermissionError(errno.EACCES, "Access is denied")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", not_permitted)
    monkeypatch.setattr("tempfile.mkstemp", _forbid_mkstemp)

    fsio.atomic_write_json(dest, {"backend": "chroma"})

    assert len(attempts) <= fsio.TEMP_NAME_ATTEMPTS + 1
    assert json.loads(dest.read_text()) == {"backend": "chroma"}
    assert "written in place" in capsys.readouterr().err


def test_quarantine_does_not_sweep_or_call_mkstemp(tmp_path, monkeypatch):
    dest = tmp_path / "config.json"
    original = b'{"palace_path": "/mnt'
    dest.write_bytes(original)
    real_open = os.open
    attempts = []

    def not_permitted(path, flags, *args, **kwargs):
        name = os.path.basename(str(path))
        if ".unreadable-" in name:
            attempts.append(name)
            if len(attempts) > 1:
                raise AssertionError(f"quarantine swept {len(attempts)} names")
            raise PermissionError(errno.EACCES, "Access is denied")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", not_permitted)
    monkeypatch.setattr("tempfile.mkstemp", _forbid_mkstemp)

    with pytest.raises(PermissionError):
        fsio.move_aside(dest, label="unreadable")

    assert attempts == attempts[:1]
    assert len(attempts) == 1
    assert dest.read_bytes() == original


def test_crash_during_json_serialize_does_not_truncate(tmp_path, monkeypatch):
    dest = tmp_path / "state.json"
    original = b'{"keep": "every byte"}\n'
    dest.write_bytes(original)

    def crash(payload, handle, **kwargs):
        handle.write("{")
        handle.flush()
        raise RuntimeError("killed mid-write")

    monkeypatch.setattr(json, "dump", crash)

    with pytest.raises(RuntimeError, match="killed mid-write"):
        fsio.atomic_write_json(dest, {"new": True})

    assert dest.read_bytes() == original
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_crash_before_replace_does_not_truncate(tmp_path, monkeypatch):
    """The new bytes are fsynced in the temporary file before ``os.replace``.

    Dying there — the crash a torn ``open(path, "w")`` would turn into a
    truncated destination — leaves the previous file in place.
    """
    dest = tmp_path / "marker.json"
    original = b'{"backend": "pgvector", "created_at": "2020-01-01T00:00:00Z"}\n'
    dest.write_bytes(original)

    def killed(fd):
        raise RuntimeError("killed before replace")

    monkeypatch.setattr(os, "fsync", killed)

    with pytest.raises(RuntimeError, match="killed before replace"):
        fsio.atomic_write_text(dest, '{"backend": "pgvector", "created_at": "later"}')

    assert dest.read_bytes() == original
    assert [p.name for p in tmp_path.iterdir()] == ["marker.json"]


def test_a_publish_killed_before_the_rename_leaves_a_temp_file_the_sweep_removes(tmp_path):
    """A writer killed between filling its temporary file and the rename runs no
    cleanup, so the file stays, and no later publish reuses the pid-named name
    (#2756). Once nothing has modified it for ``older_than`` seconds the sweep
    removes it; before that it may be a live writer's, so it stays."""
    dest = tmp_path / "state.json"
    dest.write_text('{"old": 1}', encoding="utf-8")
    killed_mid_publish = (
        "import os, sys\n"
        "from mempalace import fsio\n"
        "fsio._replace = lambda tmp, path: os._exit(9)\n"
        "fsio.atomic_write_json(sys.argv[1], {'new': ['x'] * 1000})\n"
    )
    child = subprocess.run([sys.executable, "-c", killed_mid_publish, str(dest)])
    assert child.returncode == 9
    (leftover,) = [p for p in tmp_path.iterdir() if p.name != "state.json"]
    assert leftover.name.startswith("state.json.tmp-")

    assert fsio.remove_abandoned_publish_temps(dest, older_than=600) == 0
    assert leftover.exists()

    an_hour_ago = time.time() - 3600
    os.utime(leftover, (an_hour_ago, an_hour_ago))
    assert fsio.remove_abandoned_publish_temps(dest, older_than=600) == 1
    assert not leftover.exists()
    assert json.loads(dest.read_text(encoding="utf-8")) == {"old": 1}


def test_abandoned_temp_sweep_touches_only_this_files_temporaries(tmp_path):
    dest = tmp_path / "state.json"
    an_hour_ago = time.time() - 3600
    removed = [".state.json.0123456789ab.tmp", "state.json.tmp-77", ".state-abc.tmp"]
    kept = [
        "state.json.tmp-backup",
        "state.json.tmp-",
        "state.json.tmp-77.bak",
        ".state.json.xyz.tmp",
        "other.json.tmp-77",
        "notes.tmp",
        "state.json",
    ]
    for name in removed + kept:
        (tmp_path / name).write_text("x", encoding="utf-8")
        os.utime(tmp_path / name, (an_hour_ago, an_hour_ago))
    target = tmp_path / "kept-target"
    target.write_text("x", encoding="utf-8")
    link = tmp_path / "state.json.tmp-78"
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pass  # no symlink privilege (Windows)
    else:
        kept.append(link.name)
        # Age the link itself where the platform can (not on Windows); a
        # fresh link is kept by the age check alone.
        if os.utime in os.supports_follow_symlinks:
            os.utime(link, (an_hour_ago, an_hour_ago), follow_symlinks=False)

    legacy = re.compile(r"\.state-[a-z]{3}\.tmp")
    assert fsio.remove_abandoned_publish_temps(dest, older_than=600, legacy=legacy) == 3

    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(kept + ["kept-target"])


def test_abandoned_temp_sweep_follows_a_symlinked_destination(tmp_path):
    """``atomic_publish`` writes next to the file a symlink points at, so that is
    where its temporaries are left and where the sweep must look."""
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    (real_dir / "state.json").write_text("{}", encoding="utf-8")
    link = tmp_path / "state.json"
    try:
        os.symlink(real_dir / "state.json", link)
    except (OSError, NotImplementedError):
        pytest.skip("no symlink privilege")
    leftover = real_dir / "state.json.tmp-77"
    leftover.write_text("{", encoding="utf-8")
    an_hour_ago = time.time() - 3600
    os.utime(leftover, (an_hour_ago, an_hour_ago))

    assert fsio.remove_abandoned_publish_temps(link, older_than=600) == 1
    assert not leftover.exists()


def test_successful_json_replace_is_complete_and_private(tmp_path):
    dest = tmp_path / "state.json"
    dest.write_text('{"old": 1}', encoding="utf-8")

    fsio.atomic_write_json(dest, {"new": "ok", "n": 2})

    assert json.loads(dest.read_text(encoding="utf-8")) == {"new": "ok", "n": 2}
    if os.name != "nt":
        assert stat.S_IMODE(dest.stat().st_mode) == 0o600


def test_config_publish_does_not_call_mkstemp(tmp_path, monkeypatch):
    dest = tmp_path / "config.json"
    dest.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("tempfile.mkstemp", _forbid_mkstemp)

    fsio.atomic_write_json(dest, {"backend": "sqlite_exact"})

    assert json.loads(dest.read_text(encoding="utf-8")) == {"backend": "sqlite_exact"}


def test_failed_replace_leaves_the_previous_bytes(tmp_path, monkeypatch):
    dest = tmp_path / "state.json"
    original = b'{"keep": true}'
    dest.write_bytes(original)

    def boom(*args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(os, "replace", boom)

    with pytest.raises(OSError):
        fsio.atomic_write_json(dest, {"new": True})

    assert dest.read_bytes() == original
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


class _SharingViolation(PermissionError):
    winerror = 32


def test_a_rename_blocked_by_a_reader_is_retried(tmp_path, monkeypatch, capsys):
    """Windows refuses to replace a file another process has open. The reader
    lets go, so the rename is retried rather than written in place."""
    dest = tmp_path / "config.json"
    dest.write_text('{"old": 1}', encoding="utf-8")
    real_replace = os.replace
    refusals = []

    def held_twice(src, dst):
        if len(refusals) < 2:
            refusals.append(dst)
            raise _SharingViolation(errno.EACCES, "being used by another process")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", held_twice)
    monkeypatch.setattr(fsio.time, "sleep", lambda s: None)

    fsio.atomic_write_json(dest, {"new": 2})

    assert len(refusals) == 2
    assert json.loads(dest.read_text(encoding="utf-8")) == {"new": 2}
    assert "written in place" not in capsys.readouterr().err
    assert [p.name for p in tmp_path.iterdir()] == ["config.json"]


def test_a_file_held_open_is_not_written_in_place(tmp_path, monkeypatch, capsys):
    """Writing through a file another process is reading lets it see a torn
    file, so a rename that stays refused is raised and nothing is truncated."""
    dest = tmp_path / "config.json"
    original = b'{"keep": true}'
    dest.write_bytes(original)

    def held(src, dst):
        raise _SharingViolation(errno.EACCES, "being used by another process")

    monkeypatch.setattr(os, "replace", held)
    monkeypatch.setattr(fsio.time, "sleep", lambda s: None)

    with pytest.raises(PermissionError):
        fsio.atomic_write_json(dest, {"new": True})

    assert dest.read_bytes() == original
    assert "written in place" not in capsys.readouterr().err
    assert [p.name for p in tmp_path.iterdir()] == ["config.json"]


def test_a_filesystem_without_fsync_still_publishes(tmp_path, monkeypatch):
    dest = tmp_path / "state.json"

    def unsupported(fd):
        raise OSError(errno.EINVAL, "Invalid argument")

    monkeypatch.setattr(os, "fsync", unsupported)

    fsio.atomic_write_json(dest, {"new": True})

    assert json.loads(dest.read_text(encoding="utf-8")) == {"new": True}


def test_an_fsync_io_error_is_not_swallowed(tmp_path, monkeypatch):
    dest = tmp_path / "state.json"
    original = b'{"keep": true}'
    dest.write_bytes(original)

    def broken(fd):
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(os, "fsync", broken)

    with pytest.raises(OSError, match="Input/output"):
        fsio.atomic_write_json(dest, {"new": True})

    assert dest.read_bytes() == original
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_move_aside_retries_a_rename_a_reader_blocks(tmp_path, monkeypatch):
    """Windows refuses to rename a file another process has open; the reader
    lets go, so moving the file aside is retried rather than raised."""
    path = tmp_path / "registry.json"
    path.write_bytes(b"{not json")
    real_replace = os.replace
    refusals = []

    def held_twice(src, dst):
        if len(refusals) < 2:
            refusals.append(dst)
            raise _SharingViolation(errno.EACCES, "being used by another process")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", held_twice)
    monkeypatch.setattr(fsio.time, "sleep", lambda s: None)

    kept = fsio.move_aside(path, label="corrupt")

    assert len(refusals) == 2
    assert not path.exists()
    assert (tmp_path / os.path.basename(kept)).read_bytes() == b"{not json"


def test_atomic_write_bytes_keeps_the_bytes_exactly(tmp_path):
    dest = tmp_path / "log.jsonl"
    dest.write_bytes(b"old\n")

    fsio.atomic_write_bytes(dest, b"a\nb\r\nc\n")

    assert dest.read_bytes() == b"a\nb\r\nc\n"
    assert [p.name for p in tmp_path.iterdir()] == ["log.jsonl"]
