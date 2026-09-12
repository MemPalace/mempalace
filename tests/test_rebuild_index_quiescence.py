"""Regression test: rebuild_index must acquire the palace writer lease
before opening ChromaDB or entering the snapshot/rebuild path.
"""

import pytest

from mempalace import repair
from mempalace import palace


def test_rebuild_index_refuses_before_backend_open_when_writer_lease_unavailable(
    tmp_path, monkeypatch
):
    palace_path = tmp_path / "palace"
    palace_path.mkdir()

    # Bypass unrelated preflights. This test isolates one invariant:
    # no Chroma/backend access before the whole-operation writer lease.
    monkeypatch.setattr(repair, "sqlite_integrity_errors", lambda *_a, **_k: [])
    monkeypatch.setattr(
        repair,
        "maybe_repair_poisoned_max_seq_id_before_rebuild",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        repair,
        "hnsw_capacity_status",
        lambda *_a, **_k: {"diverged": False},
    )

    def refuse_writer_lease(_path):
        raise palace.MineAlreadyRunning("held by test writer")

    monkeypatch.setattr(palace, "mine_palace_lock", refuse_writer_lease)

    class BackendMustNotOpen:
        def __init__(self):
            raise AssertionError("ChromaBackend opened before rebuild_index acquired writer lease")

    monkeypatch.setattr(repair, "ChromaBackend", BackendMustNotOpen)

    with pytest.raises(palace.MineAlreadyRunning):
        repair.rebuild_index(
            palace_path=str(palace_path),
            progress=lambda *_: None,
        )


def test_rebuild_index_keeps_writer_lease_held_for_rebuild_body(tmp_path, monkeypatch):
    from contextlib import contextmanager

    palace_path = tmp_path / "palace"
    palace_path.mkdir()

    monkeypatch.setattr(repair, "sqlite_integrity_errors", lambda *_a, **_k: [])
    monkeypatch.setattr(
        repair,
        "maybe_repair_poisoned_max_seq_id_before_rebuild",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        repair,
        "hnsw_capacity_status",
        lambda *_a, **_k: {"diverged": False},
    )

    lease_active = {"value": False}
    helper_called = {"value": False}

    @contextmanager
    def tracking_writer_lease(path):
        assert path == str(palace_path)
        assert lease_active["value"] is False
        lease_active["value"] = True
        try:
            yield
        finally:
            lease_active["value"] = False

    monkeypatch.setattr(palace, "mine_palace_lock", tracking_writer_lease)

    class DummyBackend:
        pass

    monkeypatch.setattr(repair, "ChromaBackend", DummyBackend)

    def guarded_rebuild_body(**kwargs):
        assert lease_active["value"] is True, "rebuild body executed outside palace writer lease"
        assert isinstance(kwargs["backend"], DummyBackend)
        helper_called["value"] = True

    monkeypatch.setattr(
        repair,
        "_rebuild_index_under_lease",
        guarded_rebuild_body,
    )

    repair.rebuild_index(
        palace_path=str(palace_path),
        progress=lambda *_: None,
    )

    assert helper_called["value"] is True
    assert lease_active["value"] is False
