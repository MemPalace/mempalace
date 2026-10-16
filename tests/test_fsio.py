"""The shared publisher must not hang on Windows, and must not truncate.

``tempfile.mkstemp`` treats ``PermissionError`` as a name collision when
``os.name == "nt"`` and the directory is writable, and retries ``TMP_MAX``
times. ``config`` used to call it. A crash while the new bytes are still in
the temporary file must leave the destination byte-for-byte as it was.
"""

import errno
import json
import os
import stat

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
