"""Legacy client safety stop, using synthetic sources and durable local state."""

import argparse
import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from commonplace import sync_cli, sync_daemon, sync_sources
from commonplace.sync_protocol import TOKEN_ENV, WRITE_BUDGET
from commonplace.sync_state import open_state

NOW = 2_000_000_000
HUB = "https://sync.example"
EMPTY = hashlib.sha256(b"").hexdigest()
VARIANTS = [("import", "claude-code"), ("capture", "native-memory"), ("import", "native-memory")]


def args(**kw):
    values = dict(
        sync_command="install",
        source=[],
        memory=[],
        hub=HUB,
        env_file=None,
        enable=False,
        no_service=True,
        no_activate=True,
        service="systemd",
        once=True,
    )
    values.update(kw)
    return argparse.Namespace(**values)


def record():
    return '{"type":"user","sessionId":"safe","message":{"role":"user","content":" café  \\r\\n"},"unknown":42}\r\n'.encode()


@pytest.fixture
def local(tmp_path, monkeypatch):
    for key in (*TOKEN_ENV.values(), "COMMONPLACE_URL"):
        monkeypatch.delenv(key, raising=False)
    home = tmp_path / "home"
    root = home / "transcripts"
    root.mkdir(parents=True)
    path = root / "safe.jsonl"
    path.write_bytes(record())
    config = dict(
        version=1,
        home=str(home),
        hub=HUB,
        env_file=None,
        consent={"version": 1, "hub": HUB},
        memories=[],
        sources=[{"source": "claude-code", "root": str(root)}],
        tokens={env: "synthetic" for env in TOKEN_ENV.values()},
    )
    with open_state(home / ".commonplace/sync/state.db") as state:
        yield home, root, config, state


class Hub:
    def __init__(self):
        self.posts = []
        self.raw = b""

    def __call__(self, target, token, method, route, body=None):
        if route.startswith("/api/events"):
            return 200, dict(
                feed="sync",
                reset=False,
                waited=False,
                seq=0,
                events=[],
                more=False,
                state=dict(capture_enabled=True, interval_seconds=900, next_window_at=None),
            )
        if method == "GET":
            return 200, dict(offset=0, prefix_sha256=EMPTY)
        self.posts.append((route, body))
        if route == "/api/import":
            return 200, dict(accepted=1, errors=0)
        self.raw += body["transcript"].encode()
        return 200, dict(offset=len(self.raw), prefix_sha256=hashlib.sha256(self.raw).hexdigest())


def run(config, state, hub):
    return sync_daemon.run(
        config, state, clock=lambda: NOW, sleep=lambda _: None, transport=hub, once=True
    )


def seed(state, root, kind, source, status="pending", suffix=""):
    path = root / ("legacy" + status + suffix + ".md")
    raw = b"synthetic legacy words\n"
    path.write_bytes(raw)
    os.utime(path, (NOW - 120, NOW - 120))
    op = sync_sources._op(
        kind,
        source,
        path,
        "notes:" + status + suffix + ".md:" + hashlib.sha256(raw).hexdigest(),
        0,
        raw,
        EMPTY,
    )
    state.outbox_add(op)
    if status != "pending":
        state.outbox_mark(op.op_id, status)
    state.set_status(source, path, op.native_id, status)
    return replace(op, state=status)


def snapshot(state, ops):
    ids = {op.op_id for op in ops}
    identities = {(op.source, op.path, op.native_id) for op in ops}
    return (
        [op for op in state.outbox_all() if op.op_id in ids],
        [
            dict(row)
            for row in state.statuses()
            if (row["source"], row["path"], row["native_id"]) in identities
        ],
        [tuple(row) for row in state.conn.execute("SELECT * FROM memory_seen")],
        [tuple(row) for row in state.conn.execute("SELECT * FROM cadence WHERE key LIKE '%:op:%'")],
    )


@pytest.mark.parametrize("enable", [False, True], ids=["preview", "enable"])
@pytest.mark.parametrize("flavour", ["normal", "secret", "future"])
def test_install_memory_refused(local, monkeypatch, enable, flavour, capsys):
    home, root, _, _ = local
    calls = []
    original = sync_cli._selections
    monkeypatch.setattr(
        sync_cli, "_selections", lambda *a: (calls.append("selection"), original(*a))[1]
    )
    monkeypatch.setattr(sync_cli, "_environment", lambda *a: (calls.append("env"), {})[1])
    from commonplace import sync_service

    monkeypatch.setattr(sync_service, "install", lambda *a, **k: calls.append("service"))
    before = sorted(str(p) for p in home.rglob("*"))
    rc = sync_cli.main(args(home=home, memory=[f"notes={root}:{flavour}*.md"], enable=enable))
    assert rc == 2
    assert calls == []
    assert sorted(str(p) for p in home.rglob("*")) == before
    assert (
        "Legacy memory sync is disabled; memory-once is not available yet."
        in capsys.readouterr().out
    )


@pytest.mark.parametrize("entry", ["cli", "direct"])
@pytest.mark.parametrize("reason", ["memories", "absent", "invalid", "changed", "override"])
def test_run_refuses_before_any_state_or_io(local, monkeypatch, entry, reason):
    home, root, config, state = local
    if reason == "memories":
        config["memories"] = [{"root": str(root), "label": "notes"}]
    elif reason == "absent":
        config.pop("consent")
    elif reason == "invalid":
        config["consent"] = {"version": True, "hub": HUB}
    elif reason == "changed":
        config["hub"] = "https://other.example"
    elif entry == "direct":
        config["hub"] = "https://other.example"
    calls = []
    monkeypatch.setattr(state, "retry_rejected", lambda *a, **k: calls.append("retry"))
    monkeypatch.setattr(sync_daemon, "_Session", lambda *a, **k: safe_session(calls))
    monkeypatch.setattr(sync_cli, "_environment", lambda *a: (calls.append("env"), {})[1])
    from commonplace import sync_state

    original = sync_state.open_state
    monkeypatch.setattr(
        sync_state, "open_state", lambda *a: (calls.append("state"), original(*a))[1]
    )
    if entry == "cli":
        monkeypatch.setattr(sync_cli, "_load", lambda *a: config)
        rc = sync_cli.main(
            args(
                home=home,
                sync_command="run",
                hub="https://other.example" if reason == "override" else None,
            )
        )
        refused = rc == 2
    else:
        try:
            run(config, state, Hub())
        except sync_daemon.ConfigError:
            refused = True
        else:
            refused = False
    assert refused
    assert calls == []


def safe_session(calls):
    calls.append("session")

    class Session:
        refresh = False

        def feed(self):
            return 0

        def cycle(self, **kw):
            return 0

    return Session()


@pytest.mark.parametrize("entry", ["run", "rearm"])
@pytest.mark.parametrize("kind,source", VARIANTS)
def test_retained_legacy_rows_never_read_or_rewritten(local, monkeypatch, kind, source, entry):
    _, root, config, state = local
    ops = [
        seed(state, root, kind, source, status)
        for status in ("conflict", "revoked", "failed", "unreadable")
    ]
    before = snapshot(state, ops)
    reads = []
    original = sync_sources.body

    def body(op):
        if op.op_id in {item.op_id for item in ops}:
            reads.append(op.op_id)
        return original(op)

    monkeypatch.setattr(sync_sources, "body", body)
    hub = Hub()
    if entry == "rearm":
        active = sync_daemon._Session(config, state, lambda: NOW, hub)
        active._rearm()
        assert snapshot(state, ops) == before
        assert reads == [] and hub.posts == []
        return
    run(config, state, hub)
    legacy_after = snapshot(state, ops)
    assert legacy_after == before
    assert reads == []
    assert hub.raw == record()
    cursor = state.cursor("claude-code", root / "safe.jsonl", "safe")
    assert cursor["offset"] == len(record())
    assert cursor["prefix_sha256"] == hashlib.sha256(record()).hexdigest()
    session = sync_daemon._Session(config, state, lambda: NOW, hub)
    session._rearm()
    assert snapshot(state, ops) == before


@pytest.mark.parametrize("flavour", ["normal", "secret", "absent", "future"])
def test_scanner_and_snapshot_ignore_memory_roots(local, monkeypatch, flavour):
    home, root, config, state = local
    memory = home / "private-memory"
    if flavour != "absent":
        memory.mkdir()
        note = memory / "secret.md"
        note.write_text("synthetic secret example" if flavour == "secret" else "exact words")
        os.utime(note, (NOW - 120, NOW - 120))
    config["memories"] = [
        {
            "label": "notes",
            "root": str(memory),
            "glob": "future*.md" if flavour == "future" else "*.md",
        }
    ]
    events = []
    for name in ("glob", "stat", "lstat", "open"):
        original = getattr(Path, name)

        def observed(path, *a, _name=name, _original=original, **k):
            if path == memory or memory in path.parents:
                events.append(_name)
            return _original(path, *a, **k)

        monkeypatch.setattr(Path, name, observed)
    discovered = sync_sources.discover(config)
    for s in discovered:
        state.store_cursor(s.source, s.path, s.native_id, 0, EMPTY)
    targets = sync_sources.snapshot_targets(config)
    ops = sync_sources.scan(config, state, now=NOW, targets=targets)
    assert events == []
    assert len(ops) == 1 and ops[0].kind == "capture"
    assert not any(row["source"] == "native-memory" for row in state.statuses())


@pytest.mark.parametrize("kind,source", VARIANTS)
def test_queued_import_attempt_is_inert(local, monkeypatch, kind, source):
    _, root, config, state = local
    op = seed(state, root, kind, source)
    before = snapshot(state, [op])
    hub = Hub()
    session = sync_daemon._Session(config, state, lambda: NOW, hub)
    calls = []
    monkeypatch.setattr(session, "_budget", lambda *a, **kw: (calls.append("budget"), True)[1])
    monkeypatch.setattr(
        sync_sources,
        "body",
        lambda op: (calls.append("body"), {"thread_id": "legacy", "transcript": "words"})[1],
    )
    session._attempt(op, TOKEN_ENV[source], "F")
    assert hub.posts == []
    assert calls == []
    assert snapshot(state, [op]) == before


@pytest.mark.parametrize("injected", [False, True])
def test_legacy_pending_does_not_hold_backfill(local, monkeypatch, injected, capsys):
    home, root, config, state = local
    op = seed(state, root, "import", "native-memory")
    before = snapshot(state, [op])
    state.store_cursor(
        "claude-code",
        root / "safe.jsonl",
        "safe",
        len(record()),
        hashlib.sha256(record()).hexdigest(),
    )
    active = sync_daemon._Session(config, state, lambda: NOW, Hub())
    state.defer(active.bkey, 1)
    state.defer(active._token_key(TOKEN_ENV["native-memory"]), NOW + 100)
    if injected:
        monkeypatch.setattr(sync_sources, "scan", lambda *a, **k: [op])
    active.cycle(once=False)
    assert active.backfill is False
    assert state.next_due(active.bkey + "-done") == NOW
    assert snapshot(state, [op]) == before
    if not injected:
        assert sync_sources.scan(config, state, now=NOW) == []
    for i in range(WRITE_BUDGET[0]):
        state.defer(active.key + ":writes:" + TOKEN_ENV["claude-code"] + f":{i}", NOW + 100)
    assert active._budget(TOKEN_ENV["claude-code"]) is False
    sync_cli._save(home / ".commonplace/sync", config)
    sync_cli._status(home / ".commonplace/sync", home)
    output = capsys.readouterr().out
    assert "Pending operations: 0" in output
    assert "Backfill: done" in output and "Backfill: active" not in output


@pytest.mark.parametrize(
    "status", ["pending", "conflict", "revoked", "failed", "unreadable", "rejected"]
)
def test_status_excludes_legacy_pending_and_ready(local, status, capsys):
    home, root, config, state = local
    op = seed(state, root, "import", "native-memory", status)
    state.set_status(op.source, op.path, op.native_id, "ready")
    before = snapshot(state, [op])
    sync_cli._save(home / ".commonplace/sync", config)
    sync_cli._status(home / ".commonplace/sync", home)
    output = capsys.readouterr().out
    assert "Pending operations: 0" in output
    assert "Disabled legacy operations: 1" in output
    assert "disabled; coverage unverified" in output and "ready" not in output
    assert "Parked operations:" not in output
    assert snapshot(state, [op]) == before


@pytest.mark.parametrize("destination", [None, "", "bound"])
def test_destination_required_and_bound(local, monkeypatch, destination):
    home, root, config, state = local
    directory = home / ".commonplace/sync"
    if destination != "bound":
        missing_rc = sync_cli.main(
            args(home=home, source=[f"claude-code:{root}"], hub=destination, enable=True)
        )
        assert missing_rc == 2
        assert not (directory / "config.json").exists()
    changed = dict(config, hub="https://changed.example")
    try:
        run(changed, state, Hub())
    except sync_daemon.ConfigError:
        changed_refused = True
    else:
        changed_refused = False
    assert changed_refused
    for env_route in (False, True):
        monkeypatch.setenv("COMMONPLACE_URL", HUB if env_route else "")
        assert (
            sync_cli.main(
                args(
                    home=home,
                    source=[f"claude-code:{root}"],
                    hub=None if env_route else HUB,
                    enable=True,
                )
            )
            == 0
        )
        saved = sync_cli._load(directory, home)
        assert saved["consent"] == {"version": 1, "hub": HUB}
        assert "synthetic" not in json.dumps(saved["consent"])
    monkeypatch.setenv(TOKEN_ENV["claude-code"], "synthetic")
    monkeypatch.setattr(sync_daemon, "request", Hub())
    assert sync_cli.main(args(home=home, sync_command="run", hub=HUB)) == 0
    assert sync_cli.main(args(home=home, sync_command="run", hub=HUB + "/")) == 2
    saved.pop("consent")
    sync_cli._save(directory, saved)
    assert sync_cli.main(args(home=home, sync_command="run", hub=None)) == 2


def test_preview_contains_no_private_paths(local, capsys):
    home, root, _, _ = local
    env = home / "private.env"
    env.write_text("COMMONPLACE_CAPTURE_CLAUDE_CODE_TOKEN=synthetic-private-value\n")
    env.chmod(0o600)
    assert sync_cli.main(args(home=home, source=[f"claude-code:{root}"], env_file=env)) == 0
    output = capsys.readouterr().out
    assert str(root) not in output
    for private in (str(home), str(env), "synthetic-private-value", "café", HUB):
        assert private not in output
    assert "claude-code" in output and "1" in output
    assert "Repeat your command with --enable" in output


def test_install_check_order(local, monkeypatch, capsys):
    home, root, _, _ = local
    bad_env = home / "bad.env"
    bad_env.write_text("COMMONPLACE_URL=https://sync.example\n")
    bad_env.chmod(0o644)
    calls = []
    for name in ("_selections", "_environment"):
        original = getattr(sync_cli, name)

        def observed(*a, _name=name, _original=original):
            calls.append(_name)
            return _original(*a)

        monkeypatch.setattr(sync_cli, name, observed)
    memory_rc = sync_cli.main(
        args(home=home, source=["cursor"], memory=["notes=missing"], env_file=bad_env)
    )
    assert memory_rc == 2
    assert calls == []
    assert "Legacy memory sync is disabled" in capsys.readouterr().out
    assert sync_cli.main(args(home=home, source=["cursor"], hub=None, env_file=bad_env)) == 1
    assert calls == ["_selections"]
    calls.clear()
    assert (
        sync_cli.main(args(home=home, source=[f"claude-code:{root}"], hub=None, enable=True)) == 2
    )
    assert calls == ["_selections", "_environment"]
    calls.clear()
    assert sync_cli.main(args(home=home, source=[f"claude-code:{root}"], env_file=bad_env)) == 2
    assert calls == ["_selections", "_environment"]
    assert sync_cli.main(args(home=home, source=[f"claude-code:{root}"], no_service=False)) == 0
    assert not (home / ".commonplace/sync/config.json").exists()
