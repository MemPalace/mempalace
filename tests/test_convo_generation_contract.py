"""Executable reproductions for the conversation generation publication contract.

The current `write_file_drawers` write-then-prune path stamps an incomplete
chunk_total, but an upsert into a deterministic positional ID can already
replace the previous verbatim text before the next batch fails. These expected
failures are deliberately isolated until immutable physical generations and
commit-marker visibility are implemented together.

Do not remove xfail merely to make CI green: make each assertion pass, then
remove its marker and validate get/list/search/SQLite visibility separately.
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
            f"> {label} question {index} " + "asked " * 30 + "\n"
            + f"{label} answer {index} " + "verbatim " * 45
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
    if not before.issubset(after):
        pytest.xfail(
            "P1 reproduced: the first changed batch overwrote verbatim text from "
            "the last published generation"
        )
    assert before.issubset(after)


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
    if not previous.issubset(after):
        pytest.xfail(
            "Issue #1593 reproduced: successful re-mine pruned/overwrote "
            "the previous authored conversation"
        )
    assert previous.issubset(after)
