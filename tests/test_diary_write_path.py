"""What a diary re-ingest leaves in a real palace.

A diary file is rebuilt in place whenever its text changes. These pin the
drawers that rebuild leaves behind, including when a write or the stale-drawer
prune fails part way.
"""

import pytest

from mempalace.diary_ingest import _diary_drawer_id_entry, ingest_diaries
from mempalace.palace import get_collection

_DATE = "2026-04-13"


def _entry(n, body=None):
    return (
        f"## 10:{n:02d} — Entry {n}\n\n{body or f'Entry {n} body with enough words to keep it.'}\n"
    )


def _write(diary_dir, *entries):
    path = diary_dir / f"{_DATE}.md"
    path.write_text(f"# {_DATE}\n\n" + "\n".join(entries), encoding="utf-8")
    return path


def _rows(palace, path):
    got = get_collection(palace).get(
        where={"source_file": str(path)}, include=["documents", "metadatas"]
    )
    return sorted(
        (meta["entry_index"], meta["entry_chunk_index"], meta["chunk_index"], doc)
        for doc, meta in zip(got["documents"], got["metadatas"])
    )


@pytest.fixture
def diary(tmp_path):
    diary_dir = tmp_path / "diaries"
    diary_dir.mkdir()
    return diary_dir, str(tmp_path / "palace")


def test_a_shorter_rewrite_leaves_only_the_current_entries(diary):
    diary_dir, palace = diary
    path = _write(diary_dir, _entry(1), _entry(2), _entry(3))
    ingest_diaries(str(diary_dir), palace)
    _write(diary_dir, _entry(1, "Entry 1 rewritten with the day's real outcome."))
    ingest_diaries(str(diary_dir), palace)

    rows = _rows(palace, path)
    assert [(e, c) for e, c, _, _ in rows] == [(0, 0)]
    assert "real outcome" in rows[0][3]


def test_a_longer_rewrite_adds_the_new_entries(diary):
    diary_dir, palace = diary
    path = _write(diary_dir, _entry(1))
    ingest_diaries(str(diary_dir), palace)
    _write(diary_dir, _entry(1), _entry(2), _entry(3))
    ingest_diaries(str(diary_dir), palace)

    assert [(e, c) for e, c, _, _ in _rows(palace, path)] == [(0, 0), (1, 0), (2, 0)]


def test_a_long_entry_is_chunked_and_an_earlier_growth_shifts_chunk_index(diary):
    diary_dir, palace = diary
    long_body = " ".join(f"word{i}" for i in range(700))
    path = _write(diary_dir, _entry(1), _entry(2, long_body))
    ingest_diaries(str(diary_dir), palace)
    first = _rows(palace, path)
    assert [e for e, _, _, _ in first].count(1) > 1
    assert [ci for _, _, ci, _ in first] == list(range(len(first)))

    # Entry 0 now spans several chunks too, so entry 1's drawers keep their
    # ids but move to later chunk_index values.
    _write(diary_dir, _entry(1, long_body), _entry(2, long_body))
    ingest_diaries(str(diary_dir), palace)
    second = _rows(palace, path)
    assert [ci for _, _, ci, _ in second] == list(range(len(second)))
    assert {e for e, _, _, _ in second} == {0, 1}
    ids = set(get_collection(palace).get(where={"source_file": str(path)})["ids"])
    assert _diary_drawer_id_entry("diary", _DATE, 1, 0) in ids


def test_a_failed_prune_keeps_text_readable_and_the_next_ingest_converges(diary, monkeypatch):
    diary_dir, palace = diary
    path = _write(diary_dir, _entry(1), _entry(2), _entry(3))
    ingest_diaries(str(diary_dir), palace)
    _write(diary_dir, _entry(1, "Entry 1 rewritten after the meeting."))

    col_type = type(get_collection(palace))
    real_delete = col_type.delete

    def crash(self, *args, **kwargs):
        raise RuntimeError("killed before the stale prune")

    monkeypatch.setattr(col_type, "delete", crash)
    ingest_diaries(str(diary_dir), palace)
    assert _rows(palace, path), "the day's text is gone"

    monkeypatch.setattr(col_type, "delete", real_delete)
    ingest_diaries(str(diary_dir), palace)
    rows = _rows(palace, path)
    assert [(e, c) for e, c, _, _ in rows] == [(0, 0)]
    assert "after the meeting" in rows[0][3]


def test_a_failed_write_keeps_the_previous_entries(diary, monkeypatch):
    diary_dir, palace = diary
    path = _write(diary_dir, _entry(1), _entry(2))
    ingest_diaries(str(diary_dir), palace)
    before = _rows(palace, path)
    _write(diary_dir, _entry(1, "Entry 1 rewritten."), _entry(2))

    col_type = type(get_collection(palace))

    def crash(self, *args, **kwargs):
        raise RuntimeError("killed during the write")

    monkeypatch.setattr(col_type, "upsert", crash)
    with pytest.raises(RuntimeError):
        ingest_diaries(str(diary_dir), palace)
    monkeypatch.undo()

    assert _rows(palace, path) == before
    ingest_diaries(str(diary_dir), palace)
    assert "rewritten" in _rows(palace, path)[0][3]


def test_two_diary_files_sharing_a_date_refuse_to_overwrite_each_other(diary):
    from mempalace.collision_scan import CollisionError

    diary_dir, palace = diary
    first = _write(diary_dir, _entry(1))
    ingest_diaries(str(diary_dir), palace)
    second = diary_dir / f"{_DATE}-standup.md"
    second.write_text(
        f"# {_DATE}\n\n" + _entry(1, "Standup notes, not the entry."), encoding="utf-8"
    )

    with pytest.raises(CollisionError) as exc:
        ingest_diaries(str(diary_dir), palace)
    assert first.name in str(exc.value) and second.name in str(exc.value)
    assert "Entry 1 body" in _rows(palace, first)[0][3]
