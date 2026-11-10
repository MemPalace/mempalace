"""Executable reproductions for the conversation generation publication contract.

A new per-source history backup protects prior conversation text even if a
later positional upsert fails. These assertions exercise the real content
preservation gate, not merely stable IDs or drawer counts. Generation-wide
atomic visibility remains a separate, unsolved contract.

The tests must fail normally on content loss; future staging and commit-marker
filters require their own read-visibility regressions.
"""

import os

import pytest

from mempalace.convo_miner import mine_convos
from mempalace.palace import MineFileErrors, get_collection


def _write_session(path, label, count=3):
    """Write exchanges long enough to create one drawer per conversation turn."""
    turns = []
    for index in range(count):
        turns.append(
            f"> {label} question {index} "
            + "asked " * 30
            + "\n"
            + f"{label} answer {index} "
            + "verbatim " * 45
        )
    path.write_text("\n\n".join(turns) + "\n", encoding="utf-8")


def _touch_after_write(path):
    stat = path.stat()
    os.utime(path, (stat.st_atime, stat.st_mtime + 10))


def _documents(palace_path, source):
    col = get_collection(palace_path)
    rows = col.get(
        where={"source_file": str(source)},
        include=["documents", "metadatas"],
    )
    return {
        text
        for text, metadata in zip(rows.get("documents") or [], rows.get("metadatas") or [])
        if (metadata or {}).get("extract_mode") == "exchange"
        and (metadata or {}).get("ingest_mode") == "convos"
    }


def _mine(source_dir, palace_path):
    mine_convos(str(source_dir), palace_path, wing="sessions", extract_mode="exchange")


def test_second_batch_failure_preserves_every_previous_document(tmp_path, monkeypatch):
    """Crash after one successful changed-chunk upsert must not destroy history."""
    source_dir = tmp_path / "sessions"
    source_dir.mkdir()
    session = source_dir / "session.txt"
    palace_path = str(tmp_path / "palace")
    _write_session(session, "original")
    _mine(source_dir, palace_path)
    before = _documents(palace_path, session)
    assert len(before) >= 3

    _write_session(session, "replacement")
    _touch_after_write(session)

    import mempalace.palace as palace

    monkeypatch.setattr(palace, "DRAWER_UPSERT_BATCH_SIZE", 1)
    original_upsert = type(get_collection(palace_path)).upsert
    writes = 0

    def interrupt_second_upsert(collection, *args, **kwargs):
        nonlocal writes
        # History backups are expected before the current write; inject the
        # failure *after* the first actual replacement has been persisted.
        if all("_hist_" in drawer_id for drawer_id in kwargs.get("ids", [])):
            return original_upsert(collection, *args, **kwargs)
        writes += 1
        if writes == 2:
            raise RuntimeError("injected second-batch write failure")
        return original_upsert(collection, *args, **kwargs)

    monkeypatch.setattr(type(get_collection(palace_path)), "upsert", interrupt_second_upsert)
    with pytest.raises(MineFileErrors):
        _mine(source_dir, palace_path)
    assert writes >= 2, "test did not reach the intended failure window"

    # IDs/counts alone are insufficient: compare the actual original text.
    after = _documents(palace_path, session)
    assert before.issubset(after), "old verbatim text was lost after batch failure"


def test_successful_rewrite_is_additive_in_verbatim_conversation_history(tmp_path):
    """A successful transcript rewrite must preserve prior authored content."""
    source_dir = tmp_path / "sessions"
    source_dir.mkdir()
    session = source_dir / "session.txt"
    palace_path = str(tmp_path / "palace")
    _write_session(session, "original")
    _mine(source_dir, palace_path)
    previous = _documents(palace_path, session)
    assert len(previous) >= 3

    _write_session(session, "replacement")
    _touch_after_write(session)
    _mine(source_dir, palace_path)
    after = _documents(palace_path, session)
    assert any("replacement question" in text for text in after)
    assert previous.issubset(after), "successful re-mine destroyed prior history"
