from datetime import datetime, timedelta

from mempalace import file_state


class FakeCollection:
    def __init__(self, ids=None, metadatas=None):
        self.ids = list(ids or [])
        self.metadatas = list(metadatas or [])
        self.updated = None
        self.deleted = None

    def get(self, where=None, include=None, ids=None):
        if ids is not None:
            indexes = [self.ids.index(item) for item in ids if item in self.ids]
            return {
                "ids": [self.ids[index] for index in indexes],
                "metadatas": [self.metadatas[index] for index in indexes],
            }
        return {"ids": self.ids, "metadatas": self.metadatas}

    def update(self, ids, metadatas):
        self.updated = (ids, metadatas)

    def delete(self, ids):
        self.deleted = ids


def test_new_file_is_mined_and_stamped(tmp_path):
    source = tmp_path / "note.md"
    source.write_text("new material", encoding="utf-8")

    action, content_hash, mtime = file_state.decide(FakeCollection(), str(source))

    assert action == file_state.MINE
    assert content_hash == file_state.hash_file(source)
    assert mtime == file_state.file_mtime(source)


def test_unchanged_mtime_skips_without_rehashing(tmp_path, monkeypatch):
    source = tmp_path / "note.md"
    source.write_text("same material", encoding="utf-8")
    mtime = file_state.file_mtime(source)
    collection = FakeCollection(
        ["drawer-1"],
        [{"content_hash": "stored", "source_mtime": mtime}],
    )
    monkeypatch.setattr(file_state, "hash_file", lambda _path: (_ for _ in ()).throw(AssertionError()))

    action, content_hash, current_mtime = file_state.decide(collection, str(source))

    assert (action, content_hash, current_mtime) == (file_state.SKIP, "stored", mtime)
    assert collection.updated is None


def test_touched_but_identical_file_restamps_and_skips(tmp_path):
    source = tmp_path / "note.md"
    source.write_text("same material", encoding="utf-8")
    content_hash = file_state.hash_file(source)
    collection = FakeCollection(
        ["drawer-1"],
        [{"content_hash": content_hash, "source_mtime": 1.0, "room": "general"}],
    )

    action, current_hash, mtime = file_state.decide(collection, str(source))

    assert (action, current_hash) == (file_state.SKIP, content_hash)
    assert collection.updated == (
        ["drawer-1"],
        [{
            "content_hash": content_hash,
            "source_mtime": mtime,
            "room": "general",
        }],
    )


def test_changed_content_is_remined(tmp_path):
    source = tmp_path / "note.md"
    source.write_text("changed material", encoding="utf-8")
    collection = FakeCollection(
        ["drawer-1"],
        [{"content_hash": "old-hash", "source_mtime": 1.0}],
    )

    action, content_hash, _mtime = file_state.decide(collection, str(source))

    assert action == file_state.REMINE
    assert content_hash == file_state.hash_file(source)


def test_legacy_drawer_older_than_filed_time_is_backfilled(tmp_path):
    source = tmp_path / "note.md"
    source.write_text("legacy material", encoding="utf-8")
    filed_at = datetime.now() + timedelta(seconds=5)
    collection = FakeCollection(["drawer-1"], [{"filed_at": filed_at.isoformat()}])

    action, content_hash, mtime = file_state.decide(collection, str(source))

    assert action == file_state.SKIP
    assert collection.updated == (
        ["drawer-1"],
        [{
            "content_hash": content_hash,
            "filed_at": filed_at.isoformat(),
            "source_mtime": mtime,
        }],
    )


def test_drop_drawers_deletes_only_matching_ids():
    collection = FakeCollection(["drawer-1", "drawer-2"], [{}, {}])

    assert file_state.drop_drawers(collection, "note.md") == 2
    assert collection.deleted == ["drawer-1", "drawer-2"]
