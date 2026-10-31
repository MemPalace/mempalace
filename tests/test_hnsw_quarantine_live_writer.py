"""HNSW quarantine preserves segments held by a process or palace writer."""

import mmap
import multiprocessing
import os
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from mempalace.backends.chroma import (
    _CORRUPT_SUFFIX,
    _DRIFT_SUFFIX,
    _HNSW_LINK_TO_DATA_MAX_RATIO,
    ChromaBackend,
    quarantine_invalid_hnsw_metadata,
    quarantine_stale_hnsw,
)
from mempalace.palace import mine_palace_lock

DATA_BYTES = 100
BLOATED_LINK_BYTES = int(DATA_BYTES * (_HNSW_LINK_TO_DATA_MAX_RATIO + 1))
HELD_SEGMENT_NAME = "seg-held"
HOLDER_TIMEOUT_SECONDS = 10

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="holder detection reads /proc"
)


def _close_segment_descriptors(held_file: Path) -> None:
    # mmap duplicates its descriptor; leave only the mapping alive.
    segment_path = str(held_file)
    for descriptor in os.listdir("/proc/self/fd"):
        try:
            open_file = os.readlink(f"/proc/self/fd/{descriptor}")
        except OSError:
            continue

        if open_file == segment_path:
            os.close(int(descriptor))


def _open_or_map_segment_file(held_file: Path, mode: str):
    if mode == "open":
        return held_file.open("rb")

    with held_file.open("rb") as segment_file:
        mapping = mmap.mmap(segment_file.fileno(), 0, access=mmap.ACCESS_READ)

    _close_segment_descriptors(held_file)
    return mapping


def _hold_segment_file(held_file, mode, ready, release):
    with _open_or_map_segment_file(held_file, mode):
        ready.set()
        release.wait()


def _release_holder(holder, release):
    release.set()
    holder.join(timeout=HOLDER_TIMEOUT_SECONDS)

    if holder.is_alive():
        holder.terminate()
        holder.join(timeout=HOLDER_TIMEOUT_SECONDS)

    assert not holder.is_alive()


@contextmanager
def _holding_in_child(target, *arguments):
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    holder = context.Process(target=target, args=(*arguments, ready, release))
    holder.start()

    try:
        assert ready.wait(timeout=HOLDER_TIMEOUT_SECONDS), "The child did not acquire its holder."
        yield holder
    finally:
        _release_holder(holder, release)

    assert holder.exitcode == 0


def _assert_file_holding_mode(holder, held_file, mode):
    descriptor_directory = Path(f"/proc/{holder.pid}/fd")
    open_files = []
    for descriptor_path in descriptor_directory.iterdir():
        open_files.append(descriptor_path.readlink())

    if mode == "mmap":
        assert held_file not in open_files
    else:
        assert held_file in open_files


def _assert_segment_retained(held_segment, moved_dirs):
    assert moved_dirs == []
    assert held_segment.is_dir()


def _assert_segment_quarantined(held_segment, moved_dirs, quarantine_suffix):
    assert len(moved_dirs) == 1
    assert not held_segment.exists()

    quarantined_segment = Path(moved_dirs[0])
    assert quarantined_segment.is_dir()
    assert quarantined_segment.name.startswith(f"{held_segment.name}{quarantine_suffix}")


def _write_bloated_segment(palace: Path) -> Path:
    palace.mkdir()
    (palace / "chroma.sqlite3").write_text("sqlite placeholder")

    held_segment = palace / HELD_SEGMENT_NAME
    held_segment.mkdir()
    (held_segment / "data_level0.bin").write_bytes(b"\0" * DATA_BYTES)
    (held_segment / "link_lists.bin").write_bytes(b"\0" * BLOATED_LINK_BYTES)
    (held_segment / "index_metadata.pickle").write_bytes(b"\x80" + b"x" * 16 + b"\x2e")

    return held_segment


def _write_invalid_segment(palace):
    held_segment = _write_bloated_segment(palace)
    (held_segment / "index_metadata.pickle").write_bytes(b"not a pickle")

    return held_segment


@pytest.mark.parametrize("mode", ("open", "mmap"))
def test_stale_scan_skips_held(tmp_path, mode):
    """Stale quarantine preserves held segments and retries after release."""
    palace = tmp_path / "palace"
    held_segment = _write_bloated_segment(palace)
    held_file = held_segment / "data_level0.bin"

    with _holding_in_child(_hold_segment_file, held_file, mode) as holder:
        _assert_file_holding_mode(holder, held_file, mode)
        moved_dirs = quarantine_stale_hnsw(palace)
        _assert_segment_retained(held_segment, moved_dirs)

    moved_dirs = quarantine_stale_hnsw(palace)
    _assert_segment_quarantined(held_segment, moved_dirs, _DRIFT_SUFFIX)


@pytest.mark.parametrize("mode", ("open", "mmap"))
def test_metadata_scan_skips_held(tmp_path, mode):
    """Metadata quarantine preserves held segments and retries after release."""
    palace = tmp_path / "palace"
    held_segment = _write_invalid_segment(palace)
    held_file = held_segment / "link_lists.bin"

    with _holding_in_child(_hold_segment_file, held_file, mode) as holder:
        _assert_file_holding_mode(holder, held_file, mode)
        moved_dirs = quarantine_invalid_hnsw_metadata(palace)
        _assert_segment_retained(held_segment, moved_dirs)

    moved_dirs = quarantine_invalid_hnsw_metadata(palace)
    _assert_segment_quarantined(held_segment, moved_dirs, _CORRUPT_SUFFIX)


def _hold_palace_lock(palace_path, ready, release):
    with mine_palace_lock(palace_path):
        ready.set()
        release.wait()


def _assert_quarantine_deferred(palace_path, held_segment):
    ChromaBackend._prepare_palace_for_open(palace_path)

    assert held_segment.is_dir()
    assert palace_path not in ChromaBackend._quarantined_paths


def _assert_quarantine_completed(palace_path, held_segment):
    ChromaBackend._prepare_palace_for_open(palace_path)

    assert not held_segment.exists()
    assert palace_path in ChromaBackend._quarantined_paths


def test_pre_open_waits_for_lock(tmp_path, monkeypatch):
    """Pre-open quarantine retries after another process releases the lock."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(ChromaBackend, "_quarantined_paths", set())
    palace = tmp_path / "palace"
    palace_path = str(palace)
    held_segment = _write_bloated_segment(palace)

    with _holding_in_child(_hold_palace_lock, palace_path):
        _assert_quarantine_deferred(palace_path, held_segment)

    _assert_quarantine_completed(palace_path, held_segment)
