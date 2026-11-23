"""Source growth must not bypass the shared reader's byte limit."""

import tracemalloc

import pytest

from mempalace import fsio


@pytest.mark.parametrize("track_snapshot", [False, True])
def test_growth_after_open_cannot_bypass_byte_limit(tmp_path, monkeypatch, track_snapshot):
    source = tmp_path / "growing.txt"
    source.write_bytes(b"a" * 8)
    original_open = fsio.open_source_file
    metadata = {} if track_snapshot else None

    def open_then_append(path):
        fd, before = original_open(path)
        with source.open("ab") as writer:
            writer.write(b"b" * 32)
        return fd, before

    monkeypatch.setattr(fsio, "open_source_file", open_then_append)
    with pytest.raises(OSError, match="file too large"):
        fsio.read_source_text(source, max_bytes=16, source_metadata=metadata)
    assert metadata in (None, {})


@pytest.mark.parametrize("size", [0, 15, 16, 17])
def test_source_byte_limit_boundary(tmp_path, size):
    source = tmp_path / "boundary.txt"
    source.write_bytes(b"x" * size)
    if size > 16:
        with pytest.raises(OSError, match="file too large"):
            fsio.read_source_text(source, max_bytes=16)
    else:
        text, encoding, _ = fsio.read_source_text(source, max_bytes=16)
        assert text == "x" * size
        assert encoding == "utf-8"


@pytest.mark.parametrize("track_snapshot", [False, True])
def test_small_source_does_not_allocate_the_maximum_size(tmp_path, track_snapshot):
    source = tmp_path / "tiny.txt"
    source.write_bytes(b"small source\r\n")
    metadata = {} if track_snapshot else None
    tracemalloc.start()
    try:
        text, _, _ = fsio.read_source_text(
            source, max_bytes=500 * 1024 * 1024, source_metadata=metadata
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert text == "small source\n"
    assert peak < 1024 * 1024, f"reading a tiny source allocated {peak} bytes"
    if track_snapshot:
        assert metadata["source_fingerprint"]


@pytest.mark.parametrize("size", [65535, 65536, 65537, 131073])
def test_multichunk_source_at_exact_limit_is_complete(tmp_path, size):
    source = tmp_path / "chunks.txt"
    content = (b"line\r\n" * (size // 6 + 1))[:size]
    source.write_bytes(content)
    metadata = {}
    text, _, _ = fsio.read_source_text(source, max_bytes=size, source_metadata=metadata)
    assert text == content.decode().replace("\r\n", "\n").replace("\r", "\n")
    assert metadata["source_fingerprint"]


def test_growth_between_read_chunks_cannot_bypass_limit(tmp_path, monkeypatch):
    source = tmp_path / "growing.txt"
    source.write_bytes(b"a" * 65536)
    original_fdopen = fsio.os.fdopen

    class AppendingReader:
        def __init__(self, stream):
            self.stream = stream
            self.appended = False

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def read(self, size=-1):
            result = self.stream.read(size)
            if not self.appended:
                with source.open("ab") as writer:
                    writer.write(b"b" * 65537)
                self.appended = True
            return result

    monkeypatch.setattr(
        fsio.os, "fdopen", lambda fd, *a, **kw: AppendingReader(original_fdopen(fd, *a, **kw))
    )
    metadata = {"previous": "snapshot"}
    with pytest.raises(OSError, match="file too large"):
        fsio.read_source_text(source, max_bytes=131072, source_metadata=metadata)
    assert metadata == {"previous": "snapshot"}
