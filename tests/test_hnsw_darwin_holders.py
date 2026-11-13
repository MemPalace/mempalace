"""Portable coverage for Darwin's quarantine ownership check."""

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
def test_darwin_quarantine_retains_holder_and_retries_after_release(
    tmp_path, monkeypatch, quarantine
):
    segment = _invalid_segment(tmp_path)
    monkeypatch.setattr(chroma, "sys", SimpleNamespace(platform="darwin"), raising=False)
    results = iter(
        [
            subprocess.CompletedProcess([], 0, "p123\n", ""),
            subprocess.CompletedProcess([], 1, "", ""),
        ]
    )
    calls = []

    def lsof(arguments, **kwargs):
        calls.append((arguments, kwargs))
        return next(results)

    monkeypatch.setattr(subprocess, "run", lsof)

    assert quarantine(str(tmp_path)) == []
    assert segment.is_dir()
    moved = quarantine(str(tmp_path))
    assert len(moved) == 1
    assert not segment.exists()
    assert calls[0][0] == ["/usr/sbin/lsof", "-nP", "-Fp", "+d", str(segment.resolve())]
    assert calls[0][1]["timeout"] == 5


@pytest.mark.parametrize("held", [True, False])
def test_pre_open_refuses_invalid_segment_until_holder_probe_is_clear(tmp_path, monkeypatch, held):
    segment = _invalid_segment(tmp_path)
    monkeypatch.setattr(chroma.ChromaBackend, "_quarantined_paths", set())
    monkeypatch.setattr(chroma, "_fix_blob_seq_ids", lambda _path: None)
    monkeypatch.setattr(chroma, "_fix_missing_collection_type", lambda _path: None)
    monkeypatch.setattr(chroma, "sys", SimpleNamespace(platform="darwin"))

    def lsof(*args, **kwargs):
        if not held:
            raise FileNotFoundError("lsof unavailable")
        return subprocess.CompletedProcess([], 0, "p123\n", "")

    monkeypatch.setattr(subprocess, "run", lsof)
    opened = []
    monkeypatch.setattr(chroma.chromadb, "PersistentClient", lambda **kw: opened.append(kw))

    with pytest.raises(chroma.HNSWQuarantineDeferredError, match="Cannot safely open HNSW"):
        chroma.ChromaBackend.make_client(str(tmp_path))
    assert segment.is_dir()
    assert opened == []
    assert str(tmp_path) not in chroma.ChromaBackend._quarantined_paths

    monkeypatch.setattr(
        subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess([], 1, "", "")
    )
    chroma.ChromaBackend.make_client(str(tmp_path))
    assert not segment.exists()
    assert len(opened) == 1
    assert str(tmp_path) in chroma.ChromaBackend._quarantined_paths


@pytest.mark.skipif(sys.platform != "darwin", reason="requires native macOS holder inspection")
@pytest.mark.parametrize("mode", ["open", "mmap"])
def test_native_darwin_quarantine_preserves_open_and_mapped_segment(tmp_path, mode):
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
            "print('READY', flush=True); sys.stdin.readline()",
            str(held_file),
            mode,
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "READY"
        if mode == "mmap":
            descriptors = subprocess.run(
                ["/usr/sbin/lsof", "-nP", "-a", "-p", str(child.pid), "-Ff", "+d", str(segment)],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
            assert not any(
                field.startswith("f") and field[1:].isdigit()
                for field in descriptors.stdout.splitlines()
            )
        assert chroma._segment_holder_pid(str(segment)) == child.pid
        assert chroma.quarantine_invalid_hnsw_metadata(str(tmp_path)) == []
        with pytest.raises(chroma.HNSWQuarantineDeferredError):
            chroma.quarantine_invalid_hnsw_metadata(str(tmp_path), fail_if_held=True)
        assert segment.is_dir()
    finally:
        try:
            child.communicate("stop\n", timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.communicate(timeout=10)
    assert child.returncode == 0
    assert len(chroma.quarantine_invalid_hnsw_metadata(str(tmp_path))) == 1
    assert not segment.exists()


@pytest.mark.parametrize(
    "failure",
    [
        FileNotFoundError("lsof unavailable"),
        subprocess.TimeoutExpired("lsof", 5),
        subprocess.CompletedProcess([], 1, "", "permission denied"),
        subprocess.CompletedProcess([], 2, "", ""),
        subprocess.CompletedProcess([], 0, "unexpected output", ""),
    ],
)
def test_darwin_quarantine_preserves_segment_when_probe_is_inconclusive(
    tmp_path, monkeypatch, failure
):
    segment = _invalid_segment(tmp_path)
    monkeypatch.setattr(chroma, "sys", SimpleNamespace(platform="darwin"), raising=False)

    def lsof(*args, **kwargs):
        if isinstance(failure, Exception):
            raise failure
        return failure

    monkeypatch.setattr(subprocess, "run", lsof)

    assert chroma.quarantine_invalid_hnsw_metadata(str(tmp_path)) == []
    assert segment.is_dir()
