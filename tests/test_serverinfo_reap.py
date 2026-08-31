"""Tests for server_registry.reap_stale_serverinfo — stale hub-record reaping.

``mempalace serve`` records ``{pid, host, port, ...}`` per palace under
``~/.mempalace/server/<key>/serverinfo.json`` and deletes it via
``clear_serverinfo`` atexit. A hub that crashes or is killed never runs that
cleanup, so records with a dead PID accumulate. The reaper must remove only
records whose pid is a positive int that is provably dead — live hubs,
malformed records, and unparseable JSON stay untouched.
"""

from __future__ import annotations

import json
import os


from mempalace import server_registry
from mempalace.server_registry import (
    reap_stale_serverinfo,
    read_live_serverinfo,
    serverinfo_path,
    server_state_dir,
    write_serverinfo,
)


def _isolate_home(monkeypatch, tmp_path):
    """Point ``~`` at ``tmp_path`` on both POSIX (HOME) and Windows (USERPROFILE)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))


def _write_raw(palace_path, payload):
    """Write an arbitrary serverinfo.json body for a palace key dir."""
    path = serverinfo_path(palace_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_sweep_removes_stale_keeps_live_and_malformed(tmp_path, monkeypatch):
    _isolate_home(monkeypatch, tmp_path)
    stale_palace = os.path.join(str(tmp_path), "stale_palace")
    dead_pid = 999999999  # platform-independently dead
    stale_path = _write_raw(
        stale_palace,
        {
            "pid": dead_pid,
            "host": "127.0.0.1",
            "port": 8123,
            "scheme": "http",
            "read_only": False,
            "palace_path": os.path.abspath(stale_palace),
        },
    )
    assert server_registry._pid_alive(dead_pid) is False
    assert read_live_serverinfo(stale_palace) is None

    live_palace = os.path.join(str(tmp_path), "live_palace")
    live_path = write_serverinfo(
        live_palace, host="127.0.0.1", port=8100, scheme="http", read_only=False
    )
    assert read_live_serverinfo(live_palace) is not None

    malformed_palace = os.path.join(str(tmp_path), "malformed_palace")
    for payload in (
        {"pid": 0, "port": 1},
        {"pid": -5, "port": 1},
        {"pid": "abc", "port": 1},
        {"port": 1},
    ):
        _write_raw(malformed_palace, payload)

    removed = reap_stale_serverinfo()

    assert removed == 1
    assert not stale_path.exists(), "stale record with dead PID must be removed"
    assert live_path.exists(), "live hub record must never be removed"
    assert read_live_serverinfo(live_palace) is not None
    # Only the last malformed payload survives per key dir; assert the dir
    # level: malformed records are conservatively left in place.
    malformed_path = serverinfo_path(malformed_palace)
    assert malformed_path.exists(), "malformed record must be left alone"


def test_per_palace_reap_only_touches_that_palace(tmp_path, monkeypatch):
    _isolate_home(monkeypatch, tmp_path)
    a = os.path.join(str(tmp_path), "palace_a")
    b = os.path.join(str(tmp_path), "palace_b")
    path_a = _write_raw(
        a,
        {
            "pid": 999999999,
            "host": "h",
            "port": 1,
            "scheme": "http",
            "read_only": False,
            "palace_path": os.path.abspath(a),
        },
    )
    path_b = _write_raw(
        b,
        {
            "pid": 999999999,
            "host": "h",
            "port": 1,
            "scheme": "http",
            "read_only": False,
            "palace_path": os.path.abspath(b),
        },
    )

    removed = reap_stale_serverinfo(palace_path=a)

    assert removed == 1
    assert not path_a.exists()
    assert path_b.exists(), "per-palace reap must not touch other palaces"


def test_unparseable_json_left_alone(tmp_path, monkeypatch):
    _isolate_home(monkeypatch, tmp_path)
    palace = os.path.join(str(tmp_path), "garbage_palace")
    path = serverinfo_path(palace)
    path.parent.mkdir(parents=True, exist_ok=True)
    for content in ('{"not json', "", "null"):
        path.write_text(content, encoding="utf-8")
        assert reap_stale_serverinfo() == 0
        assert path.exists(), "unparseable serverinfo must not be removed"
        assert read_live_serverinfo(palace) is None


def test_missing_server_root_is_noop(tmp_path, monkeypatch):
    _isolate_home(monkeypatch, tmp_path)
    assert not server_state_dir(os.path.join(str(tmp_path), "x")).parent.exists()
    assert reap_stale_serverinfo() == 0


def test_live_record_survives_reap_and_clear_removes_it(tmp_path, monkeypatch):
    _isolate_home(monkeypatch, tmp_path)
    palace = os.path.join(str(tmp_path), "hub_palace")
    write_serverinfo(palace, host="127.0.0.1", port=8100, scheme="http", read_only=False)
    assert reap_stale_serverinfo() == 0, "our own live record must survive"
    assert read_live_serverinfo(palace) is not None
    server_registry.clear_serverinfo(palace)
    assert not serverinfo_path(palace).exists()
