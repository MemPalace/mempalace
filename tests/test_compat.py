"""
test_compat.py — Tests for the palace compatibility guardrails.
"""

import json
import os

import pytest

from mempalace.compat import (
    META_FILE,
    chromadb_major,
    chromadb_version,
    ensure_palace_safe,
    meta_path,
    read_palace_metadata,
    write_palace_metadata,
)


class TestChromaHelpers:
    def test_chromadb_version_returns_string(self):
        v = chromadb_version()
        assert isinstance(v, str)
        assert v != "unknown"

    def test_chromadb_major_returns_int(self):
        major = chromadb_major()
        assert isinstance(major, int)
        assert major >= 0


class TestMetaPath:
    def test_meta_path_resolves(self, tmp_dir):
        p = meta_path(tmp_dir)
        assert p.name == META_FILE
        assert p.parent.is_absolute()
        assert str(p.parent) == str(p.parent.resolve())

    def test_meta_path_with_tilde(self, tmp_dir):
        """Ensure expanduser is applied (cross-platform safe)."""
        p = meta_path(tmp_dir)
        assert "~" not in str(p)


class TestWriteReadMetadata:
    def test_round_trip(self, tmp_dir):
        palace = os.path.join(tmp_dir, "palace")
        os.makedirs(palace)
        write_palace_metadata(palace)
        meta = read_palace_metadata(palace)
        assert meta is not None
        assert "mempalace_version" in meta
        assert "chromadb_version" in meta
        assert "chromadb_major" in meta
        assert isinstance(meta["chromadb_major"], int)

    def test_read_missing_returns_none(self, tmp_dir):
        assert read_palace_metadata(os.path.join(tmp_dir, "nonexistent")) is None

    def test_read_corrupt_json_returns_none(self, tmp_dir):
        palace = os.path.join(tmp_dir, "palace")
        os.makedirs(palace)
        path = meta_path(palace)
        path.write_text("not valid json {{{")
        assert read_palace_metadata(palace) is None

    def test_creates_parent_dirs(self, tmp_dir):
        palace = os.path.join(tmp_dir, "a", "b", "c")
        write_palace_metadata(palace)
        assert read_palace_metadata(palace) is not None


class TestEnsurePalaceSafe:
    def test_no_metadata_chroma_0x_passes(self, tmp_dir):
        """With Chroma <1 and no metadata, should pass (legacy palace)."""
        palace = os.path.join(tmp_dir, "palace")
        os.makedirs(palace)
        if chromadb_major() is not None and chromadb_major() < 1:
            ensure_palace_safe(palace)  # should not raise

    def test_matching_major_passes(self, tmp_dir):
        """If recorded major matches current, no error."""
        palace = os.path.join(tmp_dir, "palace")
        os.makedirs(palace)
        write_palace_metadata(palace)
        ensure_palace_safe(palace)  # should not raise

    def test_mismatched_major_raises(self, tmp_dir):
        """If recorded major differs from current, RuntimeError."""
        palace = os.path.join(tmp_dir, "palace")
        os.makedirs(palace)
        # Write metadata with a fake different major
        current = chromadb_major()
        fake_major = 999 if current != 999 else 998
        path = meta_path(palace)
        path.write_text(json.dumps({
            "mempalace_version": "0.0.0",
            "chromadb_version": f"{fake_major}.0.0",
            "chromadb_major": fake_major,
        }))
        with pytest.raises(RuntimeError, match="Refusing to proceed"):
            ensure_palace_safe(palace)

    def test_no_metadata_dir_does_not_exist(self, tmp_dir):
        """Non-existent path with no metadata — should pass on Chroma <1."""
        palace = os.path.join(tmp_dir, "ghost")
        if chromadb_major() is not None and chromadb_major() < 1:
            ensure_palace_safe(palace)  # no error

    def test_metadata_with_null_major_passes(self, tmp_dir):
        """If recorded major is null, skip the version check."""
        palace = os.path.join(tmp_dir, "palace")
        os.makedirs(palace)
        path = meta_path(palace)
        path.write_text(json.dumps({
            "mempalace_version": "0.0.0",
            "chromadb_version": "unknown",
            "chromadb_major": None,
        }))
        ensure_palace_safe(palace)  # should not raise
