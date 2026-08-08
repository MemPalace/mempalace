"""Tests for the first-party ``ndjson`` source adapter (RFC 002).

A pipeline that pre-extracts records writes a JSON-Lines spool and runs
``mempalace mine --source ndjson <spool>``; this adapter reads it back into
verbatim drawers.
"""

from __future__ import annotations

import json
import os
from argparse import Namespace

import pytest

from mempalace.sources import available_adapters
from mempalace.sources.base import SourceAdapterError, SourceNotFoundError, SourceRef
from mempalace.sources.ndjson import NdjsonSourceAdapter


def _write_spool(tmp_dir, records) -> str:
    path = os.path.join(tmp_dir, "batch-0.ndjson")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(json.dumps(r) for r in records) + "\n")
    return path


def test_ndjson_is_registered_first_party():
    # Registered on import of mempalace.sources — no install step needed.
    assert "ndjson" in available_adapters()


def test_ingest_reads_spool_verbatim(tmp_dir):
    spool = _write_spool(
        tmp_dir,
        [
            {
                "content": "the record stored exactly as written",
                "source_file": "feed://abc/item/1",
                "metadata": {"opaque_field": "kept", "wing": "wing_a"},
            }
        ],
    )
    adapter = NdjsonSourceAdapter()
    out = list(adapter.ingest(source=SourceRef(local_path=spool), palace=None))

    assert len(out) == 1
    drawer = out[0]
    assert drawer.content == "the record stored exactly as written"  # verbatim
    assert drawer.source_file == "feed://abc/item/1"
    assert drawer.chunk_index == 0
    assert drawer.metadata["opaque_field"] == "kept"  # producer metadata flows through
    assert drawer.metadata["wing"] == "wing_a"


def test_absent_chunk_index_gets_per_source_ordinal(tmp_dir):
    # Two records sharing a source_file with no chunk_index must NOT collide on
    # the deterministic drawer id (sha256(source_file)_chunk).
    spool = _write_spool(
        tmp_dir,
        [
            {"content": "first", "source_file": "feed://x"},
            {"content": "second", "source_file": "feed://x"},
            {"content": "other", "source_file": "feed://y"},
        ],
    )
    out = list(NdjsonSourceAdapter().ingest(source=SourceRef(local_path=spool), palace=None))

    triples = [(d.source_file, d.chunk_index, d.content) for d in out]
    assert triples == [
        ("feed://x", 0, "first"),
        ("feed://x", 1, "second"),
        ("feed://y", 0, "other"),
    ]


def test_provided_chunk_index_passes_through(tmp_dir):
    spool = _write_spool(
        tmp_dir,
        [{"content": "c", "source_file": "feed://x", "chunk_index": 7}],
    )
    out = list(NdjsonSourceAdapter().ingest(source=SourceRef(local_path=spool), palace=None))
    assert out[0].chunk_index == 7


def test_wing_option_fills_only_when_absent(tmp_dir):
    spool = _write_spool(
        tmp_dir,
        [
            {"content": "a", "source_file": "s://1"},
            {"content": "b", "source_file": "s://2", "metadata": {"wing": "wing_own"}},
        ],
    )
    out = list(
        NdjsonSourceAdapter().ingest(
            source=SourceRef(local_path=spool, options={"wing": "wing_flag"}), palace=None
        )
    )
    assert out[0].metadata["wing"] == "wing_flag"  # filled from the flag
    assert out[1].metadata["wing"] == "wing_own"  # the record's own wing wins


def test_blank_lines_skipped(tmp_dir):
    path = os.path.join(tmp_dir, "batch.ndjson")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write('\n{"content": "c", "source_file": "s"}\n\n')
    out = list(NdjsonSourceAdapter().ingest(source=SourceRef(local_path=path), palace=None))
    assert len(out) == 1


def test_malformed_json_raises(tmp_dir):
    path = os.path.join(tmp_dir, "bad.ndjson")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{not json}\n")
    with pytest.raises(SourceAdapterError):
        list(NdjsonSourceAdapter().ingest(source=SourceRef(local_path=path), palace=None))


def test_missing_required_fields_raises(tmp_dir):
    spool = _write_spool(tmp_dir, [{"content": "no source_file"}])
    with pytest.raises(SourceAdapterError):
        list(NdjsonSourceAdapter().ingest(source=SourceRef(local_path=spool), palace=None))


def test_missing_spool_raises_not_found(tmp_dir):
    with pytest.raises(SourceNotFoundError):
        list(
            NdjsonSourceAdapter().ingest(
                source=SourceRef(local_path=os.path.join(tmp_dir, "nope.ndjson")), palace=None
            )
        )


def test_end_to_end_mine_source_ndjson_files_drawers(tmp_dir, palace_path):
    from mempalace.cli import _mine_via_source_adapter
    from mempalace.palace import get_collection

    spool = _write_spool(
        tmp_dir,
        [
            {
                "content": "pre-extracted record line",
                "source_file": "feed://e2e/1",
                "metadata": {"opaque_field": "kept"},
            }
        ],
    )
    args = Namespace(source="ndjson", dir=spool, wing=None, dry_run=False)
    _mine_via_source_adapter(args, palace_path)

    col = get_collection(palace_path, create=True)
    got = col.get(where={"source_file": "feed://e2e/1"})
    assert got["documents"] == ["pre-extracted record line"]
    meta = got["metadatas"][0]
    assert meta["opaque_field"] == "kept"
    assert meta["adapter_name"] == "ndjson"  # stamped by PalaceContext, not by the adapter
    assert meta["adapter_version"] == "0.1.0"


def test_end_to_end_dry_run_files_nothing(tmp_dir, palace_path):
    from mempalace.cli import _mine_via_source_adapter
    from mempalace.palace import get_collection

    spool = _write_spool(tmp_dir, [{"content": "c", "source_file": "feed://dry/1"}])
    args = Namespace(source="ndjson", dir=spool, wing=None, dry_run=True)
    _mine_via_source_adapter(args, palace_path)

    col = get_collection(palace_path, create=True)
    assert col.get(where={"source_file": "feed://dry/1"})["ids"] == []
