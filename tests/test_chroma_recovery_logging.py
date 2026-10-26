"""Expected recovery fallback stays quiet; real scan failures stay visible."""

import logging
import sqlite3
from types import SimpleNamespace

import pytest
from test_chroma_metadata_contracts import synthetic_db

from mempalace import hallways, palace as mined
from mempalace.backends import chroma as cb
from mempalace.backends._inproc_sqlite import release
from mempalace.backends.base import GetResult


@pytest.mark.parametrize("caller", ["scan", "prefetch", "hallways"])
@pytest.mark.parametrize("pending", [True, False])
def test_recovery_fallback_logging(tmp_path, monkeypatch, caplog, caller, pending):
    db = synthetic_db(tmp_path)
    with sqlite3.connect(db) as conn:
        if pending:
            conn.execute("INSERT INTO embeddings_queue VALUES(2, 'persistent/default/col')")
        else:
            conn.execute("DROP TABLE max_seq_id")
    col = cb.ChromaCollection(
        SimpleNamespace(name="independent_repairs"), palace_path=str(tmp_path)
    )
    monkeypatch.setattr(col, "count", lambda: 1)
    monkeypatch.setattr(
        col,
        "get",
        lambda **kw: GetResult(ids=["doc"], documents=[], metadatas=[{"wing": "original"}]),
    )
    caplog.set_level(logging.DEBUG)
    try:
        if caller == "scan":
            absorbed = []
            mined._scan_collection_metadata(col, ["wing"], absorbed.append, absorbed.clear)
            assert absorbed == [{"wing": "original"}]
        elif caller == "prefetch":
            assert mined.prefetch_complete_mtimes(col, ["file"]) is None
        else:
            assert hallways._wing_metadata_from_sqlite(col, "original") is None
        messages = [r for r in caplog.records if r.name.startswith("mempalace")]
        if pending:
            assert any("recovery pending" in r.message for r in messages)
            assert not any(r.levelno >= logging.WARNING for r in messages)
        else:
            assert any(r.levelno >= logging.WARNING and r.exc_info for r in messages)
    finally:
        release(str(db))
