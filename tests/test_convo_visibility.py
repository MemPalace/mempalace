"""The metadata-only listing guard must not confuse source or extraction modes."""

from mempalace.convo_visibility import visible_conversation_rows


def _meta(src="session.txt", mode="exchange", filed="A", index=0, total=3, history=False):
    return {
        "ingest_mode": "convos",
        "source_file": src,
        "extract_mode": mode,
        "source_mtime": 1.0,
        "filed_at": filed,
        "chunk_index": index,
        "chunk_total": total,
        "historical_version": history,
    }


def _visible(rows):
    ids, metadata = zip(*rows)
    return {row_id for row_id, _ in visible_conversation_rows(ids, metadata)}


def test_interrupted_replacement_exposes_only_complete_history():
    rows = [(f"a_history_{i}", _meta(index=i, history=True)) for i in range(3)]
    rows += [("b_partial", _meta(filed="B", total=4))]
    rows += [(f"a_old_{i}", _meta(index=i)) for i in (1, 2)]
    assert _visible(rows) == {f"a_history_{i}" for i in range(3)}


def test_completed_replacement_and_unrelated_source_and_mode_remain_visible():
    rows = [(f"a_history_{i}", _meta(index=i, history=True)) for i in range(3)]
    rows += [(f"b_{i}", _meta(filed="B", index=i)) for i in range(3)]
    rows += [(f"g_{i}", _meta(mode="general", index=i)) for i in range(3)]
    rows += [("other", _meta(src="other.txt", total=1))]
    assert _visible(rows) == {row_id for row_id, _ in rows}


def test_finalization_of_only_some_rows_is_not_publication():
    rows = [(f"b_{i}", _meta(filed="B", index=i, total=3 if i < 2 else 4)) for i in range(3)]
    assert not _visible(rows)


def test_missing_chunk_or_duplicate_index_cannot_fake_completion():
    rows = [(f"a_{i}", _meta(index=index)) for i, index in enumerate((0, 0, 2))]
    assert not _visible(rows)


def test_incomplete_historical_backup_does_not_hide_complete_current():
    rows = [(f"a_{i}", _meta(index=i)) for i in range(3)]
    rows += [("one_history", _meta(index=0, history=True))]
    assert _visible(rows) == {f"a_{i}" for i in range(3)}


def test_unstamped_legacy_and_non_conversation_rows_keep_previous_visibility():
    legacy = _meta()
    del legacy["chunk_total"]
    rows = [("legacy", legacy), ("project", {"ingest_mode": "project"})]
    assert _visible(rows) == {"legacy", "project"}
