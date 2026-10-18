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


def _symlink_or_skip(link, target):
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable here: {exc}")


def test_a_followed_link_writes_its_target(tmp_path):
    target = tmp_path / "real.txt"
    target.write_text("old", encoding="utf-8")
    link = tmp_path / "link.txt"
    _symlink_or_skip(link, target)

    fsio.atomic_write_text(link, "new")

    assert os.path.islink(link)
    assert target.read_text(encoding="utf-8") == "new"


def test_an_unfollowed_link_is_replaced_and_its_target_left_alone(tmp_path):
    target = tmp_path / "victim.txt"
    target.write_text("untouched", encoding="utf-8")
    link = tmp_path / "corrupt_ids.txt"
    _symlink_or_skip(link, target)

    fsio.atomic_write_text(link, "drawer_bad\n", follow_symlinks=False)

    assert not os.path.islink(link)
    assert link.read_text(encoding="utf-8") == "drawer_bad\n"
    assert target.read_text(encoding="utf-8") == "untouched"


def test_an_unfollowed_link_is_not_written_in_place_when_the_rename_fails(tmp_path, monkeypatch):
    """Writing in place would follow the link, so the refusal is raised."""
    victim = tmp_path / "victim.txt"
    victim.write_text("untouched", encoding="utf-8")
    link = tmp_path / "corrupt_ids.txt"
    _symlink_or_skip(link, victim)
    real_replace = os.replace

    def refused(src, dst, *args, **kwargs):
        if os.path.basename(str(dst)) == link.name:
            raise PermissionError(errno.EPERM, "Operation not permitted")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", refused)

    with pytest.raises(PermissionError):
        fsio.atomic_write_text(link, "drawer_bad\n", follow_symlinks=False)

    assert os.path.islink(link)
    assert victim.read_text(encoding="utf-8") == "untouched"
    assert sorted(p.name for p in tmp_path.iterdir()) == [link.name, victim.name]


def test_an_unfollowed_link_is_not_written_in_place_without_a_temp_file(tmp_path, monkeypatch):
    victim = tmp_path / "victim.txt"
    victim.write_text("untouched", encoding="utf-8")
    link = tmp_path / "corrupt_ids.txt"
    _symlink_or_skip(link, victim)

    def refused(*args, **kwargs):
        raise PermissionError(errno.EACCES, "Access is denied")

    monkeypatch.setattr(fsio, "open_new_temp_file", refused)

    with pytest.raises(PermissionError):
        fsio.atomic_write_text(link, "drawer_bad\n", follow_symlinks=False)

    assert victim.read_text(encoding="utf-8") == "untouched"


def test_a_file_at_the_old_pid_temp_name_is_left_alone(tmp_path):
    dest = tmp_path / "state.json"
    stale = tmp_path / f"state.json.tmp-{os.getpid()}"
    stale.write_text("someone else's", encoding="utf-8")

    fsio.atomic_write_text(dest, "new")

    assert dest.read_text(encoding="utf-8") == "new"
    assert stale.read_text(encoding="utf-8") == "someone else's"
    assert sorted(p.name for p in tmp_path.iterdir()) == [dest.name, stale.name]


def test_a_link_at_the_next_temp_name_is_skipped_not_written_through(tmp_path, monkeypatch):
    """Temporary files are created ``O_EXCL``: a link planted at the name the
    publisher picks costs one attempt, never a write through it."""
    victim = tmp_path / "victim.txt"
    victim.write_text("untouched", encoding="utf-8")
    dest = tmp_path / "state.json"
    names = iter([b"\x00" * 6, b"\x01" * 6])
    monkeypatch.setattr(os, "urandom", lambda n: next(names))
    planted = tmp_path / ".state.json.000000000000.tmp"
    _symlink_or_skip(planted, victim)

    fsio.atomic_write_text(dest, "new")

    assert dest.read_text(encoding="utf-8") == "new"
    assert victim.read_text(encoding="utf-8") == "untouched"
    assert os.path.islink(planted)


def test_a_read_racing_a_publish_is_retried_on_windows(tmp_path, monkeypatch):
    """Opening a file while os.replace renames over it gives EACCES on Windows."""
    dest = tmp_path / "updates-cache.json"
    dest.write_bytes(b'{"latest": "3.9.0"}')
    real_read = fsio.Path.read_bytes
    failures = []

    def read_bytes(self):
        if len(failures) < 2:
            failures.append(self)
            raise PermissionError(errno.EACCES, "Permission denied", str(self))
        return real_read(self)

    monkeypatch.setattr(fsio, "_WINDOWS", True)
    monkeypatch.setattr(fsio, "_REPLACE_BACKOFF", (0, 0, 0))
    monkeypatch.setattr(fsio.Path, "read_bytes", read_bytes)

    assert fsio.read_published_bytes(dest) == b'{"latest": "3.9.0"}'
    assert len(failures) == 2


def test_a_denied_read_is_not_retried_off_windows(tmp_path, monkeypatch):
    dest = tmp_path / "state.json"
    dest.write_bytes(b"{}")
    calls = []

    def read_bytes(self):
        calls.append(self)
        raise PermissionError(errno.EACCES, "Permission denied", str(self))

    monkeypatch.setattr(fsio, "_WINDOWS", False)
    monkeypatch.setattr(fsio.Path, "read_bytes", read_bytes)

    with pytest.raises(PermissionError):
        fsio.read_published_bytes(dest)
    assert len(calls) == 1


def test_a_link_planted_at_the_old_pid_temp_name_is_never_followed(tmp_path):
    """The temporary file used to be ``<name>.tmp-<pid>`` opened without
    ``O_EXCL``; on Windows, which has no ``O_NOFOLLOW``, a link planted there
    sent the write to its target and the rename left a link in place."""
    victim = tmp_path / "victim.txt"
    victim.write_text("untouched", encoding="utf-8")
    dest = tmp_path / "corrupt_ids.txt"
    _symlink_or_skip(tmp_path / f"corrupt_ids.txt.tmp-{os.getpid()}", victim)

    fsio.atomic_write_text(dest, "drawer_bad\n", follow_symlinks=False)

    assert not os.path.islink(dest)
    assert dest.read_text(encoding="utf-8") == "drawer_bad\n"
    assert victim.read_text(encoding="utf-8") == "untouched"
