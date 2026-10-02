"""Hallway persistence keeps old state intact when atomic publication fails."""

import errno
import json
import os
import stat
import tempfile
from pathlib import Path

import pytest

from mempalace import hallways


@pytest.fixture
def hallway_file(tmp_path, monkeypatch):
    path = tmp_path / "hallways.json"
    path.write_bytes(b'{"hallways": [{"id": "old"}]}\n')
    monkeypatch.setattr(hallways, "_get_hallway_file", lambda config=None: str(path))
    return path


@pytest.mark.parametrize("error_number", [errno.EPERM, errno.EACCES, errno.EROFS])
def test_allocation_permission_failure_is_immediate_and_preserves_old_bytes(
    hallway_file, monkeypatch, error_number
):
    old_bytes = hallway_file.read_bytes()
    real_open = os.open
    attempts = []
    failure = OSError(error_number, "temporary file creation refused")

    def refuse_adjacent_temporary_file(path, *args, **kwargs):
        if Path(path).parent == hallway_file.parent and Path(path).name.startswith(".hallways-"):
            attempts.append(path)
            raise failure
        return real_open(path, *args, **kwargs)

    # Keep the original Windows mkstemp path safe during the RED reproduction.
    # Its permission retry budget can otherwise be billions of attempts.
    monkeypatch.setattr(tempfile, "TMP_MAX", 8)
    monkeypatch.setattr(os, "open", refuse_adjacent_temporary_file)

    with pytest.raises(OSError) as raised:
        hallways._save_hallways([{"id": "new"}])

    assert raised.value is failure
    assert len(attempts) == 1
    assert hallway_file.read_bytes() == old_bytes
    assert sorted(path.name for path in hallway_file.parent.iterdir()) == ["hallways.json"]


def _track_allocated_file(monkeypatch):
    real_create = hallways.create_temp_file
    allocated = []

    def track(*args, **kwargs):
        fd, path = real_create(*args, **kwargs)
        allocated.append((fd, Path(path)))
        return fd, path

    monkeypatch.setattr(hallways, "create_temp_file", track)
    return allocated


def _assert_closed_and_removed(allocated):
    assert len(allocated) == 1
    fd, temporary_path = allocated[0]
    with pytest.raises(OSError) as raised:
        os.fstat(fd)
    assert raised.value.errno == errno.EBADF
    assert not temporary_path.exists()


def test_first_name_collision_retries_without_touching_occupied_file(tmp_path, monkeypatch):
    from mempalace import _atomic_file

    occupied = tmp_path / ".hallways-occupied.tmp"
    occupied.write_bytes(b"belongs to another writer")
    tokens = iter(["occupied", "available"])
    monkeypatch.setattr(_atomic_file.secrets, "token_hex", lambda size: next(tokens))

    fd, path = _atomic_file.create_temp_file(str(tmp_path), prefix=".hallways-", suffix=".tmp")
    try:
        os.write(fd, b"this writer")
        assert Path(path).read_bytes() == b"this writer"
        assert occupied.read_bytes() == b"belongs to another writer"
        assert Path(path) == tmp_path / ".hallways-available.tmp"
    finally:
        os.close(fd)
        os.unlink(path)


def test_collision_exhaustion_is_bounded_and_preserves_foreign_file(tmp_path, monkeypatch):
    from mempalace import _atomic_file

    occupied = tmp_path / ".hallways-occupied.tmp"
    occupied.write_bytes(b"belongs to another writer")
    real_open = os.open
    attempts = []

    def count_open(path, *args, **kwargs):
        attempts.append(path)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(_atomic_file.secrets, "token_hex", lambda size: "occupied")
    monkeypatch.setattr(os, "open", count_open)

    with pytest.raises(FileExistsError) as raised:
        _atomic_file.create_temp_file(str(tmp_path), prefix=".hallways-", suffix=".tmp")

    assert raised.value.errno == errno.EEXIST
    assert len(attempts) == 8
    assert occupied.read_bytes() == b"belongs to another writer"
    assert list(tmp_path.iterdir()) == [occupied]


def test_temporary_file_is_created_exclusively_with_restrictive_mode(tmp_path, monkeypatch):
    from mempalace import _atomic_file

    real_open = os.open
    calls = []

    def track_open(path, flags, mode):
        calls.append((flags, mode))
        return real_open(path, flags, mode)

    monkeypatch.setattr(os, "open", track_open)
    fd, path = _atomic_file.create_temp_file(str(tmp_path), prefix=".hallways-", suffix=".tmp")
    try:
        flags, mode = calls[0]
        assert flags & os.O_CREAT
        assert flags & os.O_EXCL
        assert flags & os.O_WRONLY
        assert mode == 0o600
        if hasattr(os, "O_NOFOLLOW"):
            assert flags & os.O_NOFOLLOW
        if hasattr(os, "O_BINARY"):
            assert flags & os.O_BINARY
    finally:
        os.close(fd)
        os.unlink(path)


def test_success_publishes_complete_unicode_json_atomically(hallway_file, monkeypatch):
    old_bytes = hallway_file.read_bytes()
    real_replace = os.replace
    published = []
    records = [{"id": "new", "entity_a": "Árvore", "entity_b": "東京", "label": "🙂 ↔ Aya"}]

    def check_complete_before_replace(source, destination):
        assert Path(destination) == hallway_file
        assert Path(source).parent == hallway_file.parent
        assert hallway_file.read_bytes() == old_bytes
        assert json.loads(Path(source).read_text(encoding="utf-8")) == {
            "schema_version": 1,
            "hallways": records,
        }
        assert "東京".encode("utf-8") in Path(source).read_bytes()
        published.append(source)
        return real_replace(source, destination)

    monkeypatch.setattr(os, "replace", check_complete_before_replace)

    hallways._save_hallways(records)

    assert len(published) == 1
    assert hallways._load_hallways() == records
    assert sorted(path.name for path in hallway_file.parent.iterdir()) == ["hallways.json"]


@pytest.mark.skipif(os.name != "posix", reason="Windows chmod does not provide POSIX mode bits")
def test_temporary_and_published_files_have_restrictive_posix_mode(hallway_file, monkeypatch):
    real_replace = os.replace

    def check_mode_before_publish(source, destination):
        assert stat.S_IMODE(os.stat(source).st_mode) == 0o600
        return real_replace(source, destination)

    monkeypatch.setattr(os, "replace", check_mode_before_publish)

    hallways._save_hallways([{"id": "new"}])

    assert stat.S_IMODE(hallway_file.stat().st_mode) == 0o600


@pytest.mark.parametrize("failure", [RuntimeError("serialization failed"), KeyboardInterrupt()])
def test_interrupted_serialization_closes_and_removes_owned_file(
    hallway_file, monkeypatch, failure
):
    old_bytes = hallway_file.read_bytes()
    allocated = _track_allocated_file(monkeypatch)

    def interrupted_dump(payload, stream, *args, **kwargs):
        stream.write('{"hallways": [')
        raise failure

    monkeypatch.setattr(hallways.json, "dump", interrupted_dump)

    with pytest.raises(type(failure)) as raised:
        hallways._save_hallways([{"id": "new"}])

    assert raised.value is failure
    assert hallway_file.read_bytes() == old_bytes
    _assert_closed_and_removed(allocated)


def test_fdopen_failure_closes_raw_descriptor_and_removes_owned_file(hallway_file, monkeypatch):
    old_bytes = hallway_file.read_bytes()
    allocated = _track_allocated_file(monkeypatch)
    failure = RuntimeError("could not wrap descriptor")

    def refuse_fdopen(*args, **kwargs):
        raise failure

    monkeypatch.setattr(os, "fdopen", refuse_fdopen)

    with pytest.raises(RuntimeError) as raised:
        hallways._save_hallways([{"id": "new"}])

    assert raised.value is failure
    assert hallway_file.read_bytes() == old_bytes
    _assert_closed_and_removed(allocated)


@pytest.mark.parametrize("failure", [PermissionError(errno.EACCES, "rename refused"), SystemExit()])
def test_replace_failure_closes_and_removes_owned_file(hallway_file, monkeypatch, failure):
    old_bytes = hallway_file.read_bytes()
    allocated = _track_allocated_file(monkeypatch)

    def refuse_replace(source, destination):
        assert hallway_file.read_bytes() == old_bytes
        raise failure

    monkeypatch.setattr(os, "replace", refuse_replace)

    with pytest.raises(type(failure)) as raised:
        hallways._save_hallways([{"id": "new"}])

    assert raised.value is failure
    assert hallway_file.read_bytes() == old_bytes
    _assert_closed_and_removed(allocated)


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission errors must prevent publication")
def test_posix_chmod_failure_preserves_old_file_and_removes_owned_file(hallway_file, monkeypatch):
    old_bytes = hallway_file.read_bytes()
    allocated = _track_allocated_file(monkeypatch)
    failure = PermissionError(errno.EPERM, "chmod refused")

    def refuse_chmod(*args, **kwargs):
        raise failure

    monkeypatch.setattr(os, "chmod", refuse_chmod)

    with pytest.raises(PermissionError) as raised:
        hallways._save_hallways([{"id": "new"}])

    assert raised.value is failure
    assert hallway_file.read_bytes() == old_bytes
    _assert_closed_and_removed(allocated)


@pytest.mark.skipif(os.name != "nt", reason="Windows chmod compatibility behavior")
def test_windows_chmod_failure_still_publishes_complete_json(hallway_file, monkeypatch):
    chmod_calls = []

    def refuse_chmod(path, mode):
        chmod_calls.append((Path(path), mode))
        raise OSError(errno.EPERM, "chmod unsupported")

    monkeypatch.setattr(os, "chmod", refuse_chmod)

    hallways._save_hallways([{"id": "new"}])

    assert len(chmod_calls) == 1
    assert chmod_calls[0][1] == 0o600
    assert hallways._load_hallways() == [{"id": "new"}]
    assert sorted(path.name for path in hallway_file.parent.iterdir()) == ["hallways.json"]


def test_failure_after_collision_removes_only_owned_temporary_file(hallway_file, monkeypatch):
    from mempalace import _atomic_file

    old_bytes = hallway_file.read_bytes()
    foreign = hallway_file.parent / ".hallways-occupied.tmp"
    foreign.write_bytes(b"foreign temporary contents")
    tokens = iter(["occupied", "available"])
    monkeypatch.setattr(_atomic_file.secrets, "token_hex", lambda size: next(tokens))
    failure = OSError(errno.EACCES, "replace refused")

    def refuse_replace(*args, **kwargs):
        raise failure

    monkeypatch.setattr(os, "replace", refuse_replace)

    with pytest.raises(OSError) as raised:
        hallways._save_hallways([{"id": "new"}])

    assert raised.value is failure
    assert hallway_file.read_bytes() == old_bytes
    assert foreign.read_bytes() == b"foreign temporary contents"
    assert sorted(path.name for path in hallway_file.parent.iterdir()) == [
        ".hallways-occupied.tmp",
        "hallways.json",
    ]


def test_cleanup_failure_does_not_replace_the_original_write_error(hallway_file, monkeypatch):
    old_bytes = hallway_file.read_bytes()
    allocated = _track_allocated_file(monkeypatch)
    failure = RuntimeError("original write error")
    real_unlink = os.unlink

    def refuse_dump(*args, **kwargs):
        raise failure

    def refuse_owned_unlink(path, *args, **kwargs):
        if Path(path).name.startswith(".hallways-"):
            raise OSError(errno.EACCES, "cleanup refused")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(hallways.json, "dump", refuse_dump)
    monkeypatch.setattr(os, "unlink", refuse_owned_unlink)

    with pytest.raises(RuntimeError) as raised:
        hallways._save_hallways([{"id": "new"}])

    assert raised.value is failure
    assert hallway_file.read_bytes() == old_bytes
    fd, temporary_path = allocated[0]
    with pytest.raises(OSError) as closed:
        os.fstat(fd)
    assert closed.value.errno == errno.EBADF
    assert temporary_path.exists()
    real_unlink(temporary_path)
