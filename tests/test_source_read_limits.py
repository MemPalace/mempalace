"""Source growth must not bypass the shared reader's byte limit."""

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
