"""Explicit client opt-in, durable reconnect and sync event caching."""

import json

import pytest

from tests.commonplace_sync_fixtures import (
    accepted,
    actors,
    counts,
    home_tree,
    hub as hub,
    item,
    pending,
    raw,
    reached,
    records,
    reset_reached as reset_reached,
    state_rows,
)


def test_cli_install_inert_then_enable_import_live(hub):
    h = hub()
    p, token, grant = actors(h)
    root = h.home.parent / "native"
    root.mkdir()
    F, G = records("S"), records("S", 1, "g")
    (root / "session.jsonl").write_bytes(raw(F + G))
    E = h.env_file(token, grant)
    before = home_tree(h.home)
    rc = h.cli(["sync", "install", "--source", f"claude-code:{root}", "--hub", h.origin])
    assert rc == 0  # COUNTED:L13
    reached("L13")
    assert home_tree(h.home) == before
    assert (
        h.cli(
            [
                "sync",
                "install",
                "--enable",
                "--no-service",
                "--source",
                f"claude-code:{root}",
                "--env-file",
                E,
                "--hub",
                h.origin,
            ]
        )
        == 0
    )
    config = h.home / ".commonplace/sync/config.json"
    assert config.stat().st_mode & 0o777 == 0o600
    assert token.encode() not in config.read_bytes() and grant.encode() not in config.read_bytes()
    accepted(h.imp("claude-code", [item("S", raw(F))], token=grant))
    before_live = h.oracle(p, F + G)
    h.at(1)
    assert h.cli(["sync", "run", "--once"]) == 0
    assert h.oracle(p, F + G) == before_live
    assert pending(h.home)
    assert state_rows(h.home, "cadence")
    h.at(901)
    assert h.cli(["sync", "run", "--once"]) == 0
    assert counts(h.oracle(p, F + G), F + G) == [1] * len(F + G)
    assert not pending(h.home)


@pytest.mark.parametrize("plan", ["free", "off"], ids=["free", "off"])
def test_daemon_offline_three_writes_reconnect_exactly_once(hub, plan):
    h = hub(plans="on" if plan == "free" else "off")
    p, token, grant = actors(h)
    root = h.home.parent / "native"
    root.mkdir()
    F = records("S")
    R = [records("S", 1, f"r{i}")[0] for i in range(1, 5)]
    path = root / "session.jsonl"
    path.write_bytes(raw(F))
    E = h.env_file(token, grant)
    rc = h.cli(
        [
            "sync",
            "install",
            "--enable",
            "--no-service",
            "--source",
            f"claude-code:{root}",
            "--env-file",
            E,
            "--hub",
            h.origin,
        ]
    )
    assert rc == 0  # COUNTED:L14
    reached("L14")
    assert h.cli(["sync", "run", "--once"]) == 0
    h.stop()
    path.write_bytes(raw(F + R[:1]))
    assert h.cli(["sync", "run", "--once"]) == 1
    assert len(pending(h.home)) == 1
    first_pending = pending(h.home)[0]
    path.write_bytes(raw(F + R[:3]))
    assert h.cli(["sync", "run", "--once"]) == 1
    assert len(pending(h.home)) == 1
    still_pending = pending(h.home)[0]
    assert still_pending["op_id"] == first_pending["op_id"]
    assert still_pending["slice_sha256"] == first_pending["slice_sha256"]
    assert still_pending["length"] == first_pending["length"]
    h.restart()
    if plan == "free":
        h.at(901)
        assert h.cli(["sync", "run", "--once", "--hub", h.origin]) == 0
        assert len(pending(h.home)) == 1
        h.at(1802)
        assert h.cli(["sync", "run", "--once", "--hub", h.origin]) == 0
    else:
        assert h.cli(["sync", "run", "--once", "--hub", h.origin]) == 0
    assert counts(h.oracle(p, R[:3]), R[:3]) == [1, 1, 1]
    assert not pending(h.home)
    path.write_bytes(raw(F + R))
    h.at(2703)
    state_path = h.home / ".commonplace/sync/state.db"

    def reinstall(destination):
        before = state_path.read_bytes()
        assert (
            h.cli(
                [
                    "sync",
                    "install",
                    "--enable",
                    "--no-service",
                    "--source",
                    f"claude-code:{root}",
                    "--env-file",
                    E,
                    "--hub",
                    destination,
                ]
            )
            == 0
        )
        assert state_path.read_bytes() == before

    with h.drop_proxy() as proxy:
        reinstall(proxy)
        assert h.cli(["sync", "run", "--once", "--hub", proxy]) == 1
    reinstall(h.origin)
    assert counts(h.oracle(p, R[-1:]), R[-1:]) == [1]
    assert h.cli(["sync", "run", "--once", "--hub", h.origin]) == 0
    assert counts(h.oracle(p, R[-1:]), R[-1:]) == [1]
    assert not pending(h.home)


def test_daemon_event_cache_persists_and_resumes(hub, capsys):
    h = hub(plans="off")
    p, token, grant = actors(h)
    root = h.home.parent / "native"
    root.mkdir()
    F = records("S")
    (root / "session.jsonl").write_bytes(raw(F))
    E = h.env_file(token, grant)
    rc = h.cli(
        [
            "sync",
            "install",
            "--enable",
            "--no-service",
            "--source",
            f"claude-code:{root}",
            "--env-file",
            E,
            "--hub",
            h.origin,
        ]
    )
    assert rc == 0  # COUNTED:L19
    reached("L19")
    assert h.cli(["sync", "run", "--once"]) == 0
    cursors = state_rows(h.home, "feed_cursor")
    assert len(cursors) == 1
    s0 = cursors[0]["seq"]
    state = h.home / ".commonplace/sync/state.db"
    assert state.exists()
    for path in state.parent.glob("state.db*"):
        assert path.stat().st_mode & 0o777 == 0o600
        data = path.read_bytes()
        assert all(
            value not in data for value in (token.encode(), grant.encode(), F[0].key.encode())
        )
    assert h.set_capture(p, False).status == 200
    assert h.set_capture(p, True).status == 200
    assert h.sync_now(p).status == 200
    h.restart()
    assert h.cli(["sync", "run", "--once", "--hub", h.origin]) == 0
    events = state_rows(h.home, "events")
    assert [r["seq"] for r in events if r["seq"] > s0] == list(range(s0 + 1, s0 + 4))
    assert state_rows(h.home, "feed_cursor")[0]["seq"] == s0 + 3
    assert h.cli(["sync", "run", "--once", "--hub", h.origin]) == 0
    assert state_rows(h.home, "events") == events
    capsys.readouterr()
    assert h.cli(["sync", "status"]) == 0
    output = capsys.readouterr().out
    assert str(s0 + 3) in output
    assert token not in output and grant not in output
    assert F[0].key not in json.dumps(state_rows(h.home, "outbox"))


def test_free_daemon_never_faster_than_900(hub):
    h = hub()
    p, token, grant = actors(h)
    root = h.home.parent / "native"
    root.mkdir()
    histories = {sid: records(sid) for sid in ("A", "B")}
    for sid, recs in histories.items():
        (root / f"{sid}.jsonl").write_bytes(raw(recs))
    E = h.env_file(token, grant)
    rc = h.cli(
        [
            "sync",
            "install",
            "--enable",
            "--no-service",
            "--source",
            f"claude-code:{root}",
            "--env-file",
            E,
            "--hub",
            h.origin,
        ]
    )
    assert rc == 0  # COUNTED:L22
    reached("L22")
    observed = []
    # The hub's retry hint at t=10 is 891 seconds, so the durable deadline is t=901.
    for seconds in (0, 10, 450, 899, 900, 901, 1810):
        h.at(seconds)
        if seconds:
            for sid in histories:
                histories[sid] += records(sid, 1, f"t{seconds}")
                (root / f"{sid}.jsonl").write_bytes(raw(histories[sid]))
        assert h.cli(["sync", "run", "--once"]) == 0
        writes = []
        store = h.store(p)
        for thread in h.stored_threads(p):
            writes.extend(
                (thread.source_thread_id, m.at)
                for m in store.messages(thread.id)
                if m.name == "capture.raw"
            )
        observed.append((seconds, set(writes)))
    assert all(writes == observed[0][1] for seconds, writes in observed if seconds < 901)
    times = {}
    for stream, at in observed[-1][1]:
        times.setdefault(stream, []).append(at)
    from datetime import datetime

    starts = set()
    for entries in times.values():
        stamps = sorted(datetime.fromisoformat(at) for at in entries)
        assert all((b - a).total_seconds() >= 900 for a, b in zip(stamps, stamps[1:]))
        starts.update(stamps)
    assert sorted((at - h.now).total_seconds() + 1810 for at in starts) == [0, 901, 1810]
