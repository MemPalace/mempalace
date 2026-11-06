"""Pending room-apply closet snapshot contract (Astra second review findings 2–4)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

import pytest

from mempalace.backends.sqlite_exact import SQLiteExactBackend
from mempalace.config import MempalaceConfig
from mempalace.rooms import (
    GENERIC_ROOMS,
    RoomSet,
    RoomSpec,
    apply_inputs,
    load_pending_apply,
    save_pending_apply,
    save_room_set,
)


def _seed(tmp_path):
    backend = SQLiteExactBackend()
    drawers = backend.get_collection(str(tmp_path), "mempalace_drawers", create=True)
    closets = backend.get_collection(str(tmp_path), "mempalace_closets", create=True)
    for collection in (drawers, closets):
        collection.add(
            ids=["a", "b"],
            documents=["original a", "original b"],
            metadatas=[
                {"wing": "w", "source_file": "source", "room": "technical"},
                {
                    "wing": "w",
                    "source_file": "source",
                    "room": "releases" if collection is drawers else "technical",
                },
            ],
            embeddings=[[1.0, 0.0], [0.0, 1.0]],
        )
    closets.add(
        ids=["other-wing"],
        documents=["unrelated source"],
        metadatas=[{"wing": "other", "source_file": "unrelated", "room": "general"}],
        embeddings=[[1.0, 0.0]],
    )
    cfg = MempalaceConfig(palace_path=str(tmp_path))
    save_room_set(
        cfg,
        RoomSet(
            "w",
            [
                RoomSpec("technical", "Technical", exemplars=["a"]),
                RoomSpec("releases", "Release", exemplars=["b"]),
            ],
        ),
    )
    marker = Path(
        save_pending_apply(
            cfg,
            "w",
            {
                ("source", "general"): "technical",
                ("source", "technical"): "releases",
            },
            0,
            apply_inputs(cfg, "w", 0.75, GENERIC_ROOMS),
            moves={
                "a": ["source", "general", "technical"],
                "b": ["source", "technical", "releases"],
            },
        )
    )
    backend.close()
    return marker


def _resume(tmp_path):
    """Fresh-process CLI resume against disposable SQLiteExact palace; no models."""
    code = r"""
import contextlib, io, json, sys
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch
import mempalace.cli as cli
from mempalace.backends.sqlite_exact import SQLiteExactBackend

path = sys.argv[1]
backend = SQLiteExactBackend()
drawers = backend.get_collection(path, "mempalace_drawers")
closets = backend.get_collection(path, "mempalace_closets")

def snapshot():
    result = {}
    for name, collection in (("drawers", drawers), ("closets", closets)):
        r = collection.get(include=["documents", "metadatas", "embeddings"])
        result[name] = {
            i: {"document": d, "metadata": m, "embedding": e}
            for i, d, m, e in zip(r.ids, r.documents, r.metadatas, r.embeddings)
        }
    return result

def no_model(*a, **k):
    raise AssertionError("No models allowed; every approved room has a stored exemplar")

marker = Path(path) / "rooms/w.apply-pending.json"
before = snapshot()
marker_before = marker.read_text()
error = None
stdout = io.StringIO()
with patch("mempalace.palace.get_collection", lambda *a, **kw: drawers), \
     patch("mempalace.palace.get_closets_collection", lambda *a, **kw: closets), \
     patch("mempalace.embedding.get_embedding_function", lambda: no_model), \
     contextlib.redirect_stdout(stdout):
    try:
        cli.cmd_rooms(Namespace(rooms_action="apply", palace=path, wing="w", threshold=.75, yes=True))
    except BaseException as exc:
        error = {"type": type(exc).__name__, "message": str(exc)}
backend.close()
backend = SQLiteExactBackend()
drawers = backend.get_collection(path, "mempalace_drawers")
closets = backend.get_collection(path, "mempalace_closets")
after = snapshot()
backend.close()
print(json.dumps({
    "before": before, "after": after, "error": error, "stdout": stdout.getvalue(),
    "marker_before": marker_before,
    "marker_after": marker.read_text() if marker.exists() else None,
}, sort_keys=True))
"""
    env = {k: v for k, v in os.environ.items() if not k.startswith("MEMPALACE_")}
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    result = subprocess.run(
        [sys.executable, "-B", "-c", code, str(tmp_path)],
        env=env,
        cwd=env["PYTHONPATH"],
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    return json.loads(result.stdout)


@pytest.mark.parametrize(
    "label,moves",
    [
        ("null", None),
        ("mapping_as_listish", []),  # wrong: empty list is not the dict container
        ("noise", "asdf"),
        ("malformed_row", {"a": ["source"]}),
        ("partial_row", {"a": ["source", "general", "technical"], "b": ["source"]}),
        (
            "duplicate_conflict",
            {
                # dict cannot hold duplicate keys; encode conflict vs targets instead
                "a": ["source", "general", "releases"],
                "b": ["source", "technical", "releases"],
            },
        ),
    ],
)
def test_malformed_marker_must_fail_before_any_write(tmp_path, label, moves):
    marker = _seed(tmp_path)
    data = json.loads(marker.read_text())
    data["closet_moves"] = moves
    marker.write_text(json.dumps(data))
    # load-time rejection for most; resume catches the rest
    if label == "duplicate_conflict":
        with pytest.raises(ValueError, match="conflict with room apply targets"):
            load_pending_apply(MempalaceConfig(palace_path=str(tmp_path)), "w")
        assert marker.is_file()
        return
    with pytest.raises(ValueError, match="invalid closet moves"):
        load_pending_apply(MempalaceConfig(palace_path=str(tmp_path)), "w")
    assert marker.is_file()


@pytest.mark.parametrize(
    "label,moves",
    [
        (
            "unapproved_destination",
            {
                "a": ["source", "general", "unapproved"],
                "b": ["source", "technical", "releases"],
            },
        ),
        ("wrong_wing", {"other-wing": ["unrelated", "general", "releases"]}),
    ],
)
def test_inconsistent_marker_must_fail_before_any_write(tmp_path, label, moves):
    marker = _seed(tmp_path)
    data = json.loads(marker.read_text())
    data["closet_moves"] = moves
    marker.write_text(json.dumps(data))
    if label == "unapproved_destination":
        with pytest.raises(ValueError, match="conflict with room apply targets"):
            load_pending_apply(MempalaceConfig(palace_path=str(tmp_path)), "w")
        assert marker.is_file()
        return
    # wrong_wing: targets correspondence may pass if we craft matching closets entry;
    # require closets decision alignment: add matching target row so load accepts,
    # then replay must refuse ownership.
    data["closets"] = data["closets"] + [["unrelated", "general", "releases"]]
    marker.write_text(json.dumps(data))
    outcome = _resume(tmp_path)
    assert outcome["error"] is not None, outcome
    assert outcome["marker_after"] == outcome["marker_before"]
    assert outcome["after"] == outcome["before"]


def test_valid_empty_snapshot_must_not_recompute_from_current_rooms(tmp_path):
    marker = _seed(tmp_path)
    data = json.loads(marker.read_text())
    data["closet_moves"] = {}
    marker.write_text(json.dumps(data))
    outcome = _resume(tmp_path)
    assert outcome["error"] is None, outcome
    assert outcome["after"] == outcome["before"], "an empty saved snapshot must remain empty"


def test_cli_generated_empty_snapshot_stays_empty_after_later_closet_creation(
    tmp_path, monkeypatch
):
    import mempalace.cli as cli

    marker = _seed(tmp_path)
    marker.unlink()
    backend = SQLiteExactBackend()
    drawers = backend.get_collection(str(tmp_path), "mempalace_drawers")
    closets = backend.get_collection(str(tmp_path), "mempalace_closets")
    closets.delete(ids=["a", "b", "other-wing"])
    drawers.update(ids=["a", "b"], metadatas=[{"room": "general"}, {"room": "technical"}])
    monkeypatch.setattr("mempalace.palace.get_collection", lambda *a, **kw: drawers)

    def no_model(*a, **kw):
        raise AssertionError("No model calls permitted")

    monkeypatch.setattr("mempalace.embedding.get_embedding_function", lambda: no_model)
    calls = 0

    def open_closets(*a, **kw):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic closet-open failure after drawers finished")
        return closets

    monkeypatch.setattr("mempalace.palace.get_closets_collection", open_closets)
    with pytest.raises(OSError, match="drawers finished"):
        cli.cmd_rooms(
            Namespace(
                rooms_action="apply",
                palace=str(tmp_path),
                wing="w",
                threshold=0.75,
                yes=True,
            )
        )
    data = json.loads(marker.read_text())
    assert data["closet_moves"] == {}
    assert data["closets"] == [
        ["source", "general", "technical"],
        ["source", "technical", "releases"],
    ]
    closets.add(
        ids=["new-a", "new-b"],
        documents=["new source a", "new source b"],
        metadatas=[
            {"wing": "w", "source_file": "source", "room": "technical"},
            {"wing": "w", "source_file": "source", "room": "releases"},
        ],
        embeddings=[[1.0, 0.0], [0.0, 1.0]],
    )
    backend.close()
    outcome = _resume(tmp_path)
    assert outcome["error"] is None, outcome
    assert outcome["after"] == outcome["before"], (
        "new-a was created after the empty snapshot and must not be chained into releases"
    )


def test_valid_snapshot_must_not_move_a_closet_now_owned_by_another_wing(tmp_path):
    _seed(tmp_path)
    backend = SQLiteExactBackend()
    closets = backend.get_collection(str(tmp_path), "mempalace_closets")
    closets.update(ids=["b"], metadatas=[{"wing": "other", "room": "general"}])
    backend.close()
    outcome = _resume(tmp_path)
    assert outcome["error"] is not None, outcome
    assert outcome["after"]["closets"]["b"] == outcome["before"]["closets"]["b"]
    assert outcome["marker_after"] == outcome["marker_before"]


def test_changed_closet_source_must_not_be_overwritten_on_retry(tmp_path):
    _seed(tmp_path)
    backend = SQLiteExactBackend()
    collection = backend.get_collection(str(tmp_path), "mempalace_closets")
    collection.update(
        ids=["b"], metadatas=[{"source_file": "replaced-source", "room": "other-purpose"}]
    )
    backend.close()
    outcome = _resume(tmp_path)
    assert outcome["error"] is not None, outcome
    assert outcome["marker_after"] == outcome["marker_before"]
    assert outcome["after"] == outcome["before"]


@pytest.mark.parametrize("dest", ["", "../outside", "room/child", " technical "])
def test_invalid_destination_is_rejected_before_write_control(tmp_path, dest):
    marker = _seed(tmp_path)
    data = json.loads(marker.read_text())
    data["closet_moves"] = {
        "a": ["source", "general", dest],
        "b": ["source", "technical", "releases"],
    }
    marker.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        load_pending_apply(MempalaceConfig(palace_path=str(tmp_path)), "w")
    assert marker.is_file()


def test_valid_snapshot_replays_only_remaining_stable_id_control(tmp_path):
    _seed(tmp_path)
    outcome = _resume(tmp_path)
    assert outcome["error"] is None, outcome
    assert outcome["marker_after"] is None
    assert outcome["after"]["closets"]["a"] == outcome["before"]["closets"]["a"]
    assert outcome["after"]["closets"]["b"]["metadata"]["room"] == "releases"
    assert outcome["after"]["closets"]["other-wing"] == outcome["before"]["closets"]["other-wing"]


def test_legacy_list_closet_moves_are_rejected(tmp_path):
    """Pre-contract markers that only stored [id, dest] lack ownership fields."""
    marker = _seed(tmp_path)
    data = json.loads(marker.read_text())
    data["closet_moves"] = [["a", "technical"], ["b", "releases"]]
    marker.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="invalid closet moves"):
        load_pending_apply(MempalaceConfig(palace_path=str(tmp_path)), "w")
