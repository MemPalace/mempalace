"""macOS preserves unsafe indexes when holder absence cannot be established."""

import subprocess
import sys
from types import SimpleNamespace

import pytest

from mempalace.backends import chroma


def _invalid_segment(tmp_path):
    segment = tmp_path / "seg-held"
    segment.mkdir()
    (tmp_path / "chroma.sqlite3").write_bytes(b"placeholder")
    (segment / "data_level0.bin").write_bytes(b"\0" * 100)
    (segment / "link_lists.bin").write_bytes(b"\0" * 1101)
    (segment / "index_metadata.pickle").write_bytes(b"not a pickle")
    return segment


@pytest.mark.parametrize(
    "quarantine", [chroma.quarantine_invalid_hnsw_metadata, chroma.quarantine_stale_hnsw]
)
def test_darwin_preserves_unsafe_segment_without_holder_absence_proof(
    tmp_path, monkeypatch, quarantine
):
    segment = _invalid_segment(tmp_path)
    monkeypatch.setattr(chroma, "sys", SimpleNamespace(platform="darwin"))
    # Even a no-holder result cannot account for invisible descriptor-free mappings.
    monkeypatch.setattr(chroma, "_segment_holder_pid", lambda _segment: None)

    assert quarantine(str(tmp_path)) == []
    assert segment.is_dir()
    with pytest.raises(chroma.HNSWQuarantineDeferredError, match="separate palace"):
        quarantine(str(tmp_path), fail_if_held=True)
    assert segment.is_dir()


def test_pre_open_refuses_unsafe_darwin_segment_before_loading_native_client(tmp_path, monkeypatch):
    segment = _invalid_segment(tmp_path)
    monkeypatch.setattr(chroma.ChromaBackend, "_quarantined_paths", set())
    monkeypatch.setattr(chroma, "_fix_blob_seq_ids", lambda _path: None)
    monkeypatch.setattr(chroma, "_fix_missing_collection_type", lambda _path: None)
    monkeypatch.setattr(chroma, "sys", SimpleNamespace(platform="darwin"))
    opened = []
    monkeypatch.setattr(chroma.chromadb, "PersistentClient", lambda **kw: opened.append(kw))

    for _ in range(2):
        with pytest.raises(
            chroma.HNSWQuarantineDeferredError, match="Original files were preserved"
        ):
            chroma.ChromaBackend.make_client(str(tmp_path))
    assert segment.is_dir()
    assert opened == []
    assert str(tmp_path) not in chroma.ChromaBackend._quarantined_paths


def test_healthy_darwin_palace_does_not_require_quarantine(tmp_path, monkeypatch):
    monkeypatch.setattr(chroma.ChromaBackend, "_quarantined_paths", set())
    monkeypatch.setattr(chroma, "_fix_blob_seq_ids", lambda _path: None)
    monkeypatch.setattr(chroma, "_fix_missing_collection_type", lambda _path: None)
    monkeypatch.setattr(chroma, "sys", SimpleNamespace(platform="darwin"))
    opened = []
    monkeypatch.setattr(chroma.chromadb, "PersistentClient", lambda **kw: opened.append(kw))

    chroma.ChromaBackend.make_client(str(tmp_path))
    assert len(opened) == 1
    assert str(tmp_path) in chroma.ChromaBackend._quarantined_paths


@pytest.mark.parametrize("unsafe", [True, False])
def test_darwin_safety_check_cannot_be_bypassed_by_unavailable_writer_lock(
    tmp_path, monkeypatch, unsafe
):
    from mempalace import palace

    segment = _invalid_segment(tmp_path) if unsafe else None
    monkeypatch.setattr(chroma.ChromaBackend, "_quarantined_paths", set())
    monkeypatch.setattr(chroma, "_fix_blob_seq_ids", lambda _path: None)
    monkeypatch.setattr(chroma, "_fix_missing_collection_type", lambda _path: None)
    monkeypatch.setattr(chroma, "sys", SimpleNamespace(platform="darwin"))

    def locked(*args, **kwargs):
        raise palace.MineAlreadyRunning("synthetic writer lease held")

    monkeypatch.setattr(palace, "mine_palace_lock", locked)
    opened = []
    monkeypatch.setattr(chroma.chromadb, "PersistentClient", lambda **kw: opened.append(kw))
    if unsafe:
        with pytest.raises(chroma.HNSWQuarantineDeferredError):
            chroma.ChromaBackend.make_client(str(tmp_path))
        assert segment.is_dir()
        assert opened == []
    else:
        chroma.ChromaBackend.make_client(str(tmp_path))
        assert len(opened) == 1


@pytest.mark.skipif(sys.platform != "darwin", reason="requires native macOS mappings")
@pytest.mark.parametrize("mode", ["open", "mmap"])
def test_native_darwin_preserves_open_and_descriptor_free_mapped_segment(tmp_path, mode):
    if mode == "mmap" and sys.version_info < (3, 13):
        pytest.skip("descriptor-free mmap requires Python 3.13's trackfd option")
    segment = _invalid_segment(tmp_path)
    held_file = segment / "data_level0.bin"
    child = subprocess.Popen(
        [
            sys.executable,
            "-u",
            "-c",
            "import mmap,sys; f=open(sys.argv[1], 'rb'); "
            "m=mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ, trackfd=False) "
            "if sys.argv[2]=='mmap' else None; "
            "f.close() if m is not None else None; "
            "print('MAPPED_CLOSED' if m is not None and f.closed else 'OPEN', flush=True); "
            "sys.stdin.readline(); "
            "print(m[0] if m is not None else f.read(1).hex(), flush=True)",
            str(held_file),
            mode,
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == ("MAPPED_CLOSED" if mode == "mmap" else "OPEN")
        assert chroma.quarantine_invalid_hnsw_metadata(str(tmp_path)) == []
        assert chroma.quarantine_stale_hnsw(str(tmp_path)) == []
        with pytest.raises(chroma.HNSWQuarantineDeferredError):
            chroma.quarantine_invalid_hnsw_metadata(str(tmp_path), fail_if_held=True)
        assert segment.is_dir()
        stdout, stderr = child.communicate("stop\n", timeout=10)
        assert child.returncode == 0, stderr
        assert stdout.strip() == ("0" if mode == "mmap" else "00")
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=10)
    # Exiting the child still cannot prove that all other possible holders are gone.
    assert chroma.quarantine_invalid_hnsw_metadata(str(tmp_path)) == []
    assert segment.is_dir()
