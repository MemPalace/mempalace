from __future__ import annotations

import struct
from pathlib import Path

import pytest

import mempalace.backends.chroma as chroma


pytestmark = pytest.mark.skipif(
    struct.calcsize("P") != 8,
    reason="chroma-hnswlib v1 header counts use 64-bit size_t fields",
)


def _write_header(
    segment_dir: Path,
    *,
    persistence_version: int = 1,
    offset_level0: int = 0,
    max_elements: int = 1_000,
    cur_element_count: int = 250,
    trailing: bytes = b"\0" * 72,
) -> Path:
    segment_dir.mkdir(
        parents=True,
        exist_ok=True,
    )
    header_path = segment_dir / "header.bin"
    header_path.write_bytes(
        struct.pack(
            "=iQQQ",
            persistence_version,
            offset_level0,
            max_elements,
            cur_element_count,
        )
        + trailing
    )
    return header_path


def test_reads_current_v1_split_header_layout(tmp_path):
    _write_header(
        tmp_path,
        offset_level0=7,
        max_elements=1_000,
        cur_element_count=250,
    )

    result = chroma._read_hnsw_binary_header(str(tmp_path))

    assert result == {
        "persistence_version": 1,
        "offset_level0": 7,
        "max_elements": 1_000,
        "cur_element_count": 250,
    }
    assert not (chroma._hnsw_binary_header_has_impossible_counts(result))


def test_missing_and_truncated_header_are_unknown(tmp_path):
    assert chroma._read_hnsw_binary_header(str(tmp_path)) is None

    (tmp_path / "header.bin").write_bytes(b"short")

    assert chroma._read_hnsw_binary_header(str(tmp_path)) is None


def test_astronomical_hnsw_counts_are_rejected(tmp_path):
    observed_corrupt_count = 7_198_365_188_096

    _write_header(
        tmp_path,
        max_elements=observed_corrupt_count,
        cur_element_count=observed_corrupt_count,
    )

    result = chroma._read_hnsw_binary_header(str(tmp_path))

    assert result is not None
    assert chroma._hnsw_binary_header_has_impossible_counts(result)


def test_current_count_above_declared_capacity_is_rejected(
    tmp_path,
):
    _write_header(
        tmp_path,
        max_elements=100,
        cur_element_count=101,
    )

    result = chroma._read_hnsw_binary_header(str(tmp_path))

    assert result is not None
    assert chroma._hnsw_binary_header_has_impossible_counts(result)


def test_unknown_persistence_version_is_not_interpreted_as_v1(
    tmp_path,
):
    _write_header(
        tmp_path,
        persistence_version=2,
        max_elements=(chroma._HNSW_SANE_ELEMENT_CAP + 1),
        cur_element_count=(chroma._HNSW_SANE_ELEMENT_CAP + 1),
    )

    result = chroma._read_hnsw_binary_header(str(tmp_path))

    assert result is not None
    assert not (chroma._hnsw_binary_header_has_impossible_counts(result))


def test_segment_health_accepts_sane_header_and_rejects_corrupt_header(
    tmp_path,
):
    sane = tmp_path / "sane"
    corrupt = tmp_path / "corrupt"

    _write_header(
        sane,
        max_elements=100,
        cur_element_count=10,
    )
    _write_header(
        corrupt,
        max_elements=(chroma._HNSW_SANE_ELEMENT_CAP + 1),
        cur_element_count=10,
    )

    assert chroma._segment_appears_healthy(str(sane))
    assert not chroma._segment_appears_healthy(str(corrupt))


def test_quarantine_catches_fresh_corrupt_header(
    tmp_path,
):
    palace = tmp_path / "palace"
    palace.mkdir()

    (palace / "chroma.sqlite3").write_bytes(b"sqlite")

    segment = palace / "11111111-2222-3333-4444-555555555555"
    segment.mkdir()

    (segment / "data_level0.bin").write_bytes(b"data")

    _write_header(
        segment,
        max_elements=(chroma._HNSW_SANE_ELEMENT_CAP + 1),
        cur_element_count=10,
    )

    moved = chroma.quarantine_stale_hnsw(
        str(palace),
        stale_seconds=1_000_000,
    )

    assert len(moved) == 1
    assert not segment.exists()
    assert Path(moved[0]).is_dir()
    assert ".drift-" in Path(moved[0]).name


def test_capacity_status_surfaces_corrupt_header_before_pickle(
    tmp_path,
    monkeypatch,
):
    segment_id = "segment-1595"
    segment = tmp_path / segment_id

    _write_header(
        segment,
        max_elements=(chroma._HNSW_SANE_ELEMENT_CAP + 1),
        cur_element_count=10,
    )

    monkeypatch.setattr(
        chroma,
        "_vector_segment_id",
        lambda *_args, **_kwargs: segment_id,
    )
    monkeypatch.setattr(
        chroma,
        "_sqlite_embedding_count",
        lambda *_args, **_kwargs: 10,
    )

    def fail_if_pickle_is_read(
        *_args,
        **_kwargs,
    ):
        raise AssertionError("corrupt header must stop the probe before pickle deserialization")

    monkeypatch.setattr(
        chroma,
        "_hnsw_element_count",
        fail_if_pickle_is_read,
    )

    result = chroma._hnsw_capacity_status_uncached(str(tmp_path))

    assert result["status"] == "diverged"
    assert result["diverged"] is True
    assert result["sqlite_count"] == 10
    assert result["hnsw_binary_max_elements"] == chroma._HNSW_SANE_ELEMENT_CAP + 1
    assert result["hnsw_binary_cur_element_count"] == 10
    assert "impossible element counts" in result["message"]
    assert "repair" in result["message"]


def test_capacity_cache_invalidates_when_header_changes(
    tmp_path,
    monkeypatch,
):
    segment_id = "segment-cache"
    segment = tmp_path / segment_id

    header_path = _write_header(
        segment,
        max_elements=100,
        cur_element_count=10,
    )

    calls = {
        "hnsw": 0,
    }

    monkeypatch.setattr(
        chroma,
        "_vector_segment_id",
        lambda *_args, **_kwargs: segment_id,
    )
    monkeypatch.setattr(
        chroma,
        "_sqlite_embedding_count",
        lambda *_args, **_kwargs: 10,
    )

    def count_hnsw(
        *_args,
        **_kwargs,
    ):
        calls["hnsw"] += 1
        return 10

    monkeypatch.setattr(
        chroma,
        "_hnsw_element_count",
        count_hnsw,
    )
    monkeypatch.setattr(
        chroma,
        "_read_sync_threshold",
        lambda *_args, **_kwargs: 1_000,
    )
    monkeypatch.setattr(
        chroma,
        "_collection_has_sync_threshold_metadata",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        chroma,
        "_hnsw_metadata_age_seconds",
        lambda *_args, **_kwargs: 0.0,
    )

    chroma.reset_hnsw_capacity_cache()

    try:
        first = chroma.hnsw_capacity_status(str(tmp_path))
        cached = chroma.hnsw_capacity_status(str(tmp_path))

        assert first["status"] == "ok"
        assert cached["status"] == "ok"
        assert calls["hnsw"] == 1

        _write_header(
            segment,
            max_elements=(chroma._HNSW_SANE_ELEMENT_CAP + 1),
            cur_element_count=10,
            trailing=b"\0" * 73,
        )

        assert header_path.stat().st_size == 101

        changed = chroma.hnsw_capacity_status(str(tmp_path))

        assert changed["status"] == "diverged"
        assert changed["diverged"] is True

        # The corrupt-header gate runs before the pickle
        # count, proving that the cached healthy verdict
        # was invalidated by header.bin.
        assert calls["hnsw"] == 1
    finally:
        chroma.reset_hnsw_capacity_cache()
