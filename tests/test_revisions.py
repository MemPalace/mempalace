import threading
from pathlib import Path

from mempalace.revisions import (
    append_drawer_revision,
    load_drawer_revisions,
    purge_drawer_revisions,
    revisions_path,
)


def test_append_creates_a_missing_palace_dir_and_writes_lf(tmp_path):
    palace = tmp_path / "remote-palace"

    rid = append_drawer_revision(str(palace), "d1", "line one\r\nline two", {"wing": "w"})

    raw = revisions_path(str(palace)).read_bytes()
    assert raw.endswith(b"}\n") and b"}\r\n" not in raw
    [row] = load_drawer_revisions(str(palace), "d1")
    assert row["revision_id"] == rid
    assert row["content"] == "line one\r\nline two"


def test_load_skips_a_torn_line(tmp_path):
    append_drawer_revision(str(tmp_path), "d1", "first")
    with revisions_path(str(tmp_path)).open("ab") as handle:
        handle.write(b'{"revision_id": "torn", "drawer_')
    append_drawer_revision(str(tmp_path), "d1", "second")

    assert [r["content"] for r in load_drawer_revisions(str(tmp_path), "d1")] == [
        "first",
        "second",
    ]


def test_purge_drops_matching_drawers_and_torn_lines(tmp_path):
    palace = str(tmp_path)
    append_drawer_revision(palace, "d1", "gone", {"source_file": "a.md"})
    with revisions_path(palace).open("ab") as handle:
        handle.write(b'{"revision_id": "torn", "drawer_')
    append_drawer_revision(palace, "d2", "kept", {"source_file": "b.md"})
    append_drawer_revision(palace, "d3", "gone by source", {"source_file": "c.md"})

    assert purge_drawer_revisions(palace, ["d1"], source_file="c.md") == 2

    raw = revisions_path(palace).read_bytes()
    assert b"torn" not in raw and b"\r" not in raw
    assert [r["content"] for r in load_drawer_revisions(palace)] == ["kept"]
    assert list(tmp_path.iterdir()) == [revisions_path(palace)]


def test_purge_without_a_match_leaves_the_file_alone(tmp_path):
    palace = str(tmp_path)
    append_drawer_revision(palace, "d1", "kept")
    before = revisions_path(palace).read_bytes()

    assert purge_drawer_revisions(palace, ["other"]) == 0
    assert revisions_path(palace).read_bytes() == before
    assert purge_drawer_revisions(str(tmp_path / "missing"), ["d1"]) == 0


def test_an_append_during_a_purge_survives(tmp_path, monkeypatch):
    """An update that appends while a purge is rewriting must not be lost."""
    palace = str(tmp_path)
    append_drawer_revision(palace, "gone", "old")
    append_drawer_revision(palace, "kept", "kept")
    path = revisions_path(palace)
    real_read = Path.read_bytes
    racer = {}

    def read_then_race(self):
        data = real_read(self)
        if self == path and "thread" not in racer:
            racer["thread"] = threading.Thread(
                target=append_drawer_revision, args=(palace, "late", "appended mid-purge")
            )
            racer["thread"].start()
            racer["thread"].join(timeout=0.5)  # the purge holds the lock, so it waits
        return data

    monkeypatch.setattr(Path, "read_bytes", read_then_race)

    assert purge_drawer_revisions(palace, ["gone"]) == 1
    racer["thread"].join(timeout=15)
    assert not racer["thread"].is_alive()
    assert [r["drawer_id"] for r in load_drawer_revisions(palace)] == ["kept", "late"]


def test_purge_removes_the_file_when_nothing_is_left(tmp_path):
    palace = str(tmp_path)
    append_drawer_revision(palace, "d1", "only")

    assert purge_drawer_revisions(palace, ["d1"]) == 1
    assert not revisions_path(palace).exists()


class _SharingViolation(PermissionError):
    winerror = 32


def test_purge_retries_a_rewrite_a_reader_blocks(tmp_path, monkeypatch):
    """Windows refuses to replace the file while a reader has it open; the
    purge retries instead of failing the delete it follows."""
    import errno
    import os

    import mempalace.fsio as fsio

    palace = str(tmp_path)
    append_drawer_revision(palace, "d1", "gone")
    append_drawer_revision(palace, "d2", "kept")
    real_replace = os.replace
    refusals = []

    def held_twice(src, dst):
        if len(refusals) < 2:
            refusals.append(dst)
            raise _SharingViolation(errno.EACCES, "being used by another process")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", held_twice)
    monkeypatch.setattr(fsio.time, "sleep", lambda s: None)

    assert purge_drawer_revisions(palace, ["d1"]) == 1

    assert len(refusals) == 2
    raw = revisions_path(palace).read_bytes()
    assert b"\r" not in raw
    assert [r["content"] for r in load_drawer_revisions(palace)] == ["kept"]
    assert list(tmp_path.iterdir()) == [revisions_path(palace)]
