"""Every durable writer goes through ``fsio`` and keeps the bytes it wrote before.

Each case writes through the real module function and compares the file with
the serialization that writer has always produced. Private files stay 0600;
the others keep the umask's mode, as a plain ``open()`` gave them.
"""

import json
import os
import pathlib
import stat
import threading
from types import SimpleNamespace

import pytest

from mempalace import (
    daemon,
    kg_normalize,
    logstream,
    replica,
    rooms,
    server_registry,
    tunnels_tool,
    wing_split,
)

posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows sharing violations")


def _expect(path, text):
    """The file holds ``text``, newlines translated as text mode always did."""
    assert path.read_bytes() == text.replace("\n", os.linesep).encode("utf-8")


def _no_leftovers(directory, *keep):
    leftovers = sorted(p.name for p in directory.iterdir() if p.name not in keep)
    assert leftovers == [], leftovers


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setenv(daemon.STATE_ROOT_ENV, str(tmp_path / "daemon"))
    return tmp_path / "home"


def _plans(tmp_path):
    config = SimpleNamespace(palace_path=str(tmp_path / "palace"))
    plan = {"wing": "wing_a", "projects": {"p": {"target": "wing_b"}}, "note": "é"}
    return config, plan


def test_plan_files_keep_their_indented_json(tmp_path):
    config, plan = _plans(tmp_path)
    expected = json.dumps(plan, indent=2, ensure_ascii=False)

    for path in (
        kg_normalize.save_normalize_plan(config, plan),
        tunnels_tool.save_proposal(config, plan),
        wing_split.save_split_plan(config, plan),
    ):
        _expect(pathlib.Path(path), expected)
        _no_leftovers(pathlib.Path(path).parent, pathlib.Path(path).name)


def test_room_files_keep_their_indented_json(tmp_path):
    config, _ = _plans(tmp_path)
    room_set = rooms.RoomSet(wing="wing_a", rooms=[])
    path = pathlib.Path(rooms.save_room_set(config, room_set))
    _expect(path, json.dumps(room_set.to_dict(), indent=2, ensure_ascii=False))

    pending = pathlib.Path(rooms.save_pending_apply(config, "wing_a", {}, 0))
    assert json.loads(pending.read_text(encoding="utf-8"))["wing"] == "wing_a"
    _no_leftovers(path.parent, path.name, pending.name)


def test_split_pending_ends_with_a_newline(tmp_path):
    config, plan = _plans(tmp_path)
    path = pathlib.Path(wing_split.write_split_pending(config, "wing_a", plan))
    text = path.read_text(encoding="utf-8")
    assert text.endswith("}\n")
    assert json.loads(text)["plan_sha256"] == wing_split.split_plan_fingerprint(plan)


def test_watch_cursor_stays_compact(tmp_path):
    path = tmp_path / "state" / "watch.json"
    logstream.write_watch_cursor(str(path), "42", agent="a")
    payload = json.loads(path.read_text(encoding="utf-8"))
    _expect(path, json.dumps(payload))
    assert payload["cursor"] == "42"
    _no_leftovers(path.parent, path.name)


def test_replica_identity_keeps_its_format(tmp_path):
    replica_id = replica.get_replica_id(str(tmp_path))
    path = tmp_path / replica.REPLICA_FILENAME
    note = {"replica_id": replica_id, "minted_at_note": "RFC 004 step 0"}
    _expect(path, json.dumps(note, indent=2) + "\n")
    assert replica.get_replica_id(str(tmp_path)) == replica_id


def test_hub_records_keep_their_format(tmp_path, home):
    palace = str(tmp_path / "palace")
    info = server_registry.write_serverinfo(
        palace, host="127.0.0.1", port=1, scheme="http", read_only=False
    )
    assert json.loads(info.read_text(encoding="utf-8"))["port"] == 1
    assert info.read_text(encoding="utf-8").endswith("}\n")

    mesh = server_registry.write_mesh_state(palace, peers={"p": 1}, profiles={})
    _expect(mesh, json.dumps(json.loads(mesh.read_text(encoding="utf-8"))) + "\n")

    token = daemon.ensure_token(palace)
    assert daemon.read_token(palace) == token
    _no_leftovers(mesh.parent, info.name, mesh.name)


@posix_only
def test_private_files_stay_0600_and_the_rest_follow_the_umask(tmp_path, home):
    old = os.umask(0o022)
    try:
        config, plan = _plans(tmp_path)
        plan_path = kg_normalize.save_normalize_plan(config, plan)
        mesh = server_registry.write_mesh_state(str(tmp_path / "p"), peers={}, profiles={})
    finally:
        os.umask(old)
    assert stat.S_IMODE(os.stat(plan_path).st_mode) == 0o644
    assert stat.S_IMODE(os.stat(mesh).st_mode) == 0o600


def _write_while_a_reader_holds(path, write):
    """Hold ``path`` open for reading, release it shortly, and write meanwhile."""
    handle = open(path, encoding="utf-8")
    timer = threading.Timer(0.2, handle.close)
    timer.start()
    try:
        write()
    finally:
        timer.join()
        handle.close()


@windows_only
def test_a_reader_holding_a_room_file_does_not_fail_the_save(tmp_path):
    config, _ = _plans(tmp_path)
    first = rooms.RoomSet(wing="wing_a", rooms=[], proposed_by="first")
    path = pathlib.Path(rooms.save_room_set(config, first))
    second = rooms.RoomSet(wing="wing_a", rooms=[], proposed_by="second")

    _write_while_a_reader_holds(path, lambda: rooms.save_room_set(config, second))

    assert json.loads(path.read_text(encoding="utf-8"))["proposed_by"] == "second"


@windows_only
def test_a_reader_holding_the_mesh_state_does_not_fail_the_round(tmp_path, home):
    palace = str(tmp_path / "palace")
    path = server_registry.write_mesh_state(palace, peers={"old": 1}, profiles={})

    _write_while_a_reader_holds(
        path, lambda: server_registry.write_mesh_state(palace, peers={"new": 1}, profiles={})
    )

    assert json.loads(path.read_text(encoding="utf-8"))["peers"] == {"new": 1}


def test_a_link_at_a_room_file_is_replaced_not_written_through(tmp_path):
    """These state files were written by temp file and rename, which replaces a
    link at the name; following it would rewrite whatever it points at."""
    config, _ = _plans(tmp_path)
    room_set = rooms.RoomSet(wing="wing_a", rooms=[])
    path = pathlib.Path(rooms.room_set_path(config, "wing_a"))
    path.parent.mkdir(parents=True)
    victim = tmp_path / "victim.json"
    victim.write_text("untouched", encoding="utf-8")
    try:
        os.symlink(victim, path)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable here: {exc}")

    rooms.save_room_set(config, room_set)

    assert not os.path.islink(path)
    assert json.loads(path.read_text(encoding="utf-8"))["wing"] == "wing_a"
    assert victim.read_text(encoding="utf-8") == "untouched"
