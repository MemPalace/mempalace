from pathlib import Path

import pytest

from mempalace.changed_set import normalize_changed_set, sync_changed_sources


def test_normalize_changed_set_rejects_escape_and_overlap(tmp_path):
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="escapes project root"):
        normalize_changed_set(tmp_path, ["../outside.py"], [])
    with pytest.raises(ValueError, match="both changed and deleted"):
        normalize_changed_set(tmp_path, ["a.py"], ["a.py"])


def test_dry_run_does_not_open_palace(tmp_path):
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    report = sync_changed_sources(
        palace_path="/not/opened",
        project_root=tmp_path,
        changed=["a.py"],
        deleted=["old.py"],
    )
    assert report == {
        "changed": 1,
        "deleted": 1,
        "ignored": 0,
        "reindexed": 0,
        "drawers_added": 0,
        "dry_run": True,
    }


def test_apply_purges_affected_sources_and_indexes_only_changed(monkeypatch, tmp_path):
    from mempalace import changed_set

    changed = tmp_path / "a.py"
    changed.write_text("print('new')", encoding="utf-8")
    calls = {"deleted": [], "closets": [], "processed": []}

    class FakeCollection:
        def delete(self, *, where):
            calls["deleted"].append(where)

    class Lock:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(changed_set, "mine_palace_lock", lambda _path: Lock())
    monkeypatch.setattr(changed_set, "get_collection", lambda *_a, **_k: FakeCollection())
    monkeypatch.setattr(changed_set, "get_closets_collection", lambda *_a, **_k: object())

    class FakeClosets:
        def delete(self, *, where):
            calls["closets"].append(where)

    monkeypatch.setattr(changed_set, "get_closets_collection", lambda *_a, **_k: FakeClosets())
    monkeypatch.setattr(
        changed_set,
        "load_config",
        lambda _root: {"wing": "project", "rooms": [{"name": "general"}]},
    )

    def fake_process(path: Path, *_args, **_kwargs):
        calls["processed"].append(path)
        return 2, "general", None

    monkeypatch.setattr(changed_set, "process_file", fake_process)
    report = sync_changed_sources(
        palace_path="/palace",
        project_root=tmp_path,
        changed=["a.py"],
        deleted=["old.py"],
        dry_run=False,
    )

    assert calls["processed"] == [changed]
    assert len(calls["deleted"]) == 1
    assert len(calls["closets"]) == 1
    assert report["drawers_added"] == 2
    assert report["reindexed"] == 1


def test_explicit_changed_set_purges_gitignored_sources_without_reindex(monkeypatch, tmp_path):
    from mempalace import changed_set

    generated = tmp_path / "generated" / "bundle.md"
    generated.parent.mkdir()
    generated.write_text("duplicated aggregate", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("generated/\n", encoding="utf-8")
    calls = {"drawers": [], "closets": [], "processed": []}

    class FakeCollection:
        def __init__(self, key):
            self.key = key

        def delete(self, *, where):
            calls[self.key].append(where)

    class Lock:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(changed_set, "mine_palace_lock", lambda _path: Lock())
    monkeypatch.setattr(changed_set, "get_collection", lambda *_a, **_k: FakeCollection("drawers"))
    monkeypatch.setattr(
        changed_set,
        "get_closets_collection",
        lambda *_a, **_k: FakeCollection("closets"),
    )
    monkeypatch.setattr(
        changed_set,
        "load_config",
        lambda _root: {"wing": "project", "rooms": [{"name": "general"}]},
    )
    monkeypatch.setattr(
        changed_set,
        "process_file",
        lambda path, *_a, **_k: calls["processed"].append(path),
    )

    report = sync_changed_sources(
        palace_path="/palace",
        project_root=tmp_path,
        changed=["generated/bundle.md"],
        deleted=[],
        dry_run=False,
    )

    assert report["ignored"] == 1
    assert report["reindexed"] == 0
    assert calls["processed"] == []
    assert len(calls["drawers"]) == 1
    assert len(calls["closets"]) == 1


@pytest.mark.parametrize("field", ["changed", "deleted"])
def test_normalize_changed_set_rejects_non_array_manifests(tmp_path, field):
    values = {"changed": [], "deleted": []}
    values[field] = "a.py"
    with pytest.raises(ValueError, match=f"{field} must be an array of strings"):
        normalize_changed_set(tmp_path, values["changed"], values["deleted"])
