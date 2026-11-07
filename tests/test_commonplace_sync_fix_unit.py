"""Regression checks for source isolation, bounded idle work and CLI failures."""

import hashlib
import json
import os
import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest

from commonplace import cli, sync_cli, sync_daemon, sync_service, sync_sources
from commonplace.sync_protocol import TOKEN_ENV
from commonplace.sync_state import open_state

NOW = 2_000_000_000


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def record(sid, text="verbatim café"):
    return (
        json.dumps({"type": "user", "sessionId": sid, "message": {"role": "user", "content": text}})
        + "\n"
    ).encode()


class Hub:
    def __init__(self, *, interval=0, reject=None, window=None):
        self.interval, self.reject, self.window = interval, reject, window
        self.posts, self.stored, self.feeds = [], {}, 0

    def __call__(self, target, token, method, route, body=None):
        if route.startswith("/api/events?"):
            self.feeds += 1
            return 200, {
                "feed": "sync",
                "seq": 0,
                "events": [],
                "more": False,
                "reset": False,
                "waited": False,
                "state": {
                    "capture_enabled": True,
                    "interval_seconds": self.interval,
                    "next_window_at": self.window,
                },
            }
        if method == "GET":
            return 200, {"offset": 0, "prefix_sha256": sha(b"")}
        self.posts.append(body)
        if body.get("thread_id") == "bad" and self.reject:
            return self.reject, {}
        if route == "/api/import":
            return 200, {"accepted": 1, "errors": 0}
        sid = body["thread_id"]
        self.stored[sid] = self.stored.get(sid, b"") + body["transcript"].encode()
        return 200, {"offset": len(self.stored[sid]), "prefix_sha256": sha(self.stored[sid])}


@pytest.fixture
def local(tmp_path):
    home = tmp_path / "home"
    root = home / ".claude/projects"
    root.mkdir(parents=True)
    config = {
        "home": str(home),
        "hub": "https://sync.example",
        "consent": {"version": 1, "hub": "https://sync.example"},
        "memories": [],
        "sources": [{"source": "claude-code", "root": str(root)}],
        "tokens": {TOKEN_ENV["claude-code"]: "synthetic", TOKEN_ENV["native-memory"]: "synthetic"},
    }
    with open_state(home / ".commonplace/sync/state.db") as state:
        yield config, state, root


def retained_import(state, path, label="notes"):
    from commonplace.sync_protocol import memory_thread_id

    raw = path.read_bytes()
    op = sync_sources._op(
        "import", "native-memory", path, memory_thread_id(label, path.name, sha(raw)), 0, raw
    )
    state.outbox_add(op)
    return op


def run(local, hub, *, continuous=False):
    config, state, _ = local
    sleeps = []
    return sync_daemon.run(
        config,
        state,
        clock=lambda: NOW,
        sleep=sleeps.append,
        transport=hub,
        once=not continuous,
        stop=lambda: bool(sleeps),
    )


@pytest.mark.parametrize("once", [False, True], ids=["continuous", "once"])
def test_exhausted_token_does_not_block_import_or_codex(local, once):
    config, state, root = local
    for sid in ("a-blocked", "b-blocked"):
        (root / f"{sid}.jsonl").write_bytes(record(sid))
    codex = root.parent / "codex"
    codex.mkdir()
    (codex / "session.jsonl").write_text(
        json.dumps({"type": "session_meta", "payload": {"id": "codex-session"}}) + "\n"
    )
    config["sources"].append({"source": "codex", "root": str(codex)})
    notes = root.parent / "notes"
    notes.mkdir()
    note = notes / "memory.md"
    note.write_text("Synthetic verbatim memory.\n")
    os.utime(note, (NOW - 120, NOW - 120))
    legacy = retained_import(state, note)
    config["tokens"] = {env: "synthetic-" + source for source, env in TOKEN_ENV.items()}
    hub = Hub()

    def transport(target, token, method, route, body=None):
        if method == "POST":
            assert token == config["tokens"][TOKEN_ENV[body["source"]]]
        return hub(target, token, method, route, body)

    active = sync_daemon._Session(config, state, lambda: NOW, transport)
    # Exercise steady-state exhaustion, without the first-sync backfill allowance.
    state.defer(active.bkey + "-done", NOW)
    for _ in range(sync_daemon.WRITE_BUDGET[0]):
        assert active._budget(TOKEN_ENV["claude-code"])
    assert active.cycle(once=once) == 0
    assert [body["source"] for body in hub.posts] == ["codex"]  # RED
    assert legacy in state.outbox_all()
    pending = [op for op in state.outbox_pending() if op.kind != "import"]
    assert [op.native_id for op in pending] == ["a-blocked", "b-blocked"]
    assert all(op.attempts == 0 for op in pending)
    assert state.next_due(active.key) == 0

    # Reopening the session preserves the budget and the order within its token.
    state.defer(active.key + ":writes:" + TOKEN_ENV["claude-code"] + ":0", NOW)
    active = sync_daemon._Session(config, state, lambda: NOW, transport)
    assert active.cycle(once=once) == 0
    assert hub.posts[-1]["thread_id"] == "a-blocked"
    assert [op.native_id for op in state.outbox_pending() if op.kind != "import"] == ["b-blocked"]
    assert legacy in state.outbox_all()


def test_fix1_default_install_syncs_available_source(tmp_path, monkeypatch):
    home = tmp_path / "home"
    root = home / ".claude/projects"
    root.mkdir(parents=True)
    raw = record("good")
    (root / "good.jsonl").write_bytes(raw)
    env = tmp_path / "client.env"
    env.write_text(
        "COMMONPLACE_URL=https://sync.example\nCOMMONPLACE_CAPTURE_TOKEN_CLAUDE_CODE=synthetic\n"
    )
    env.chmod(0o600)
    hub = Hub()
    monkeypatch.setattr(sync_daemon, "request", hub)
    assert (
        cli.main(
            [
                "sync",
                "install",
                "--enable",
                "--no-service",
                "--env-file",
                str(env),
                "--home",
                str(home),
            ]
        )
        == 0
    )
    assert cli.main(["sync", "run", "--once", "--home", str(home)]) == 0
    assert hub.stored == {"good": raw}


def test_fix1_explicit_missing_root_fails_before_config(tmp_path, monkeypatch):
    from commonplace import sync_cli

    calls = []
    original = sync_cli._selections

    def observed(*a):
        calls.append(1)
        return original(*a)

    monkeypatch.setattr(sync_cli, "_selections", observed)
    home = tmp_path / "home"
    assert (
        cli.main(
            [
                "sync",
                "install",
                "--source",
                "codex:" + str(tmp_path / "absent"),
                "--hub",
                "https://sync.example",
                "--enable",
                "--no-service",
                "--home",
                str(home),
            ]
        )
        == 2
    )
    assert calls == [1]
    assert not (home / ".commonplace/sync/config.json").exists()


def test_fix1_failed_root_does_not_hide_other_roots(local):
    config, _, root = local
    (root / "good.jsonl").write_bytes(record("good"))
    config["sources"].insert(0, {"source": "codex", "root": str(root / "missing")})
    hub = Hub()
    run(local, hub)
    assert "good" in hub.stored


@pytest.mark.parametrize("continuous", [False, True])
@pytest.mark.parametrize("fault", ["oversize", "encoding", "conflict", "unreadable"])
def test_fix2_bad_source_does_not_block_good_source(local, continuous, fault, monkeypatch):
    config, state, root = local
    (root / "z-good.jsonl").write_bytes(record("good"))
    bad = root / "a-bad.jsonl"
    if fault == "encoding":
        bad = root / "a-bad.jsonl"
        bad.write_bytes(record("bad") + b"\xff\n")
        state.store_cursor("claude-code", bad, "bad", 0, sha(b""))
        os.utime(bad, (NOW - 120, NOW - 120))
    else:
        bad.write_bytes(
            record("bad") + (record("bad", "x" * 1_700_000) if fault == "oversize" else b"")
        )
        if fault == "conflict":
            state.store_cursor("claude-code", str(bad), "bad", 1, sha(b"wrong"))
        if fault == "unreadable":
            original = sync_sources._open

            def unavailable(path):
                if Path(path) == bad:
                    raise PermissionError()
                return original(path)

            monkeypatch.setattr(sync_sources, "_open", unavailable)
    hub = Hub()
    run(local, hub, continuous=continuous)
    assert hub.stored.get("good") == record("good")
    assert any(row["status"] in {"conflict", "too_large", "unreadable"} for row in state.statuses())


@pytest.mark.parametrize("status", [400, 401, 403, 404, 413])
def test_fix3_rejected_op_yields_to_next_and_retries_after_restart_or_backoff(local, status):
    _, state, root = local
    (root / "a-bad.jsonl").write_bytes(record("bad"))
    (root / "z-good.jsonl").write_bytes(record("good"))
    hub = Hub(reject=status)
    assert run(local, hub) == 1
    assert (hub.stored.get("good") == record("good")) == (status in (401, 403))
    assert state.cursor("claude-code", root / "a-bad.jsonl", "bad")["offset"] == 0
    assert bool(state.outbox_pending()) == (status not in (401, 403))
    hub.reject = None
    config, _, _ = local
    assert (
        sync_daemon.run(
            config, state, clock=lambda: NOW + 60, sleep=lambda _: None, transport=hub, once=True
        )
        == 0
    )
    assert hub.stored["bad"] == record("bad")


def test_fix4_idle_cycles_read_no_source_or_memory_content(local, monkeypatch):
    config, state, root = local
    (root / "session.jsonl").write_bytes(record("good"))
    note = root / "note.md"
    note.write_bytes(b"verbatim memory")
    os.utime(note, (NOW - 120, NOW - 120))
    legacy = retained_import(state, note)
    from commonplace.onboarding import POINTER

    (root / "pointer.md").write_bytes(POINTER.encode())
    (root / "empty.md").write_bytes(b"")
    original_note = root / "original.md"
    original_note.write_bytes(b"previously onboarded memory")
    for path in (root / "pointer.md", root / "empty.md", original_note):
        os.utime(path, (NOW - 120, NOW - 120))
    manifest = Path(config["home"]) / ".commonplace/onboarding.json"
    manifest.write_text(
        json.dumps({"files": {"original": {"original_sha256": sha(original_note.read_bytes())}}})
    )
    hub = Hub()
    assert run(local, hub) == 0
    read_bytes = []
    discoveries = []
    discover = sync_sources.discover

    def discover_once(config):
        discoveries.append(1)
        return discover(config)

    monkeypatch.setattr(sync_sources, "discover", discover_once)
    original = sync_sources._open

    @contextmanager
    def counted(path):
        with original(path) as stream:

            class Reader:
                def read(self, *args):
                    raw = stream.read(*args)
                    read_bytes.append(len(raw))
                    return raw

                def fileno(self):
                    return stream.fileno()

            yield Reader()

    monkeypatch.setattr(sync_sources, "_open", counted)
    for _ in range(10):
        assert run(local, hub) == 0
    assert sum(read_bytes) == 0
    assert len(discoveries) == 10
    # Disabled versions never trigger reads or advance retained history.
    note.write_bytes(b"new verbatim memory")
    os.utime(note, (NOW - 120, NOW - 120))
    assert run(local, hub) == 0
    assert legacy in state.outbox_all()
    assert sum(read_bytes) == 0
    assert all(body["source"] != "native-memory" for body in hub.posts)


def test_fix7_free_feed_waits_until_next_window(local):
    now = [NOW]
    hub = Hub(interval=900, window="2033-05-18T03:46:40Z")  # NOW + 800
    config, state, _ = local

    def advance(seconds):
        now[0] += seconds

    assert (
        sync_daemon.run(
            config,
            state,
            clock=lambda: now[0],
            sleep=advance,
            transport=hub,
            stop=lambda: now[0] >= NOW + 300,
        )
        == 0
    )
    assert hub.feeds <= 1


def test_fix8_service_failure_is_generic_exit_two(tmp_path, monkeypatch, capsys):
    calls = []

    def failed(*args, **kwargs):
        calls.append(1)
        raise subprocess.CalledProcessError(1, ["synthetic-manager"])

    monkeypatch.setattr(sync_service, "install", failed)
    env = tmp_path / "client.env"
    env.write_text("COMMONPLACE_CAPTURE_TOKEN_CLAUDE_CODE=synthetic\n")
    env.chmod(0o600)
    try:
        result = cli.main(
            [
                "sync",
                "install",
                "--source",
                "claude-code",
                "--enable",
                "--no-activate",
                "--hub",
                "https://sync.example",
                "--env-file",
                str(env),
                "--home",
                str(tmp_path / "home"),
            ]
        )
    except subprocess.CalledProcessError:
        result = None
    assert result == 2
    assert calls == [1]
    assert (
        capsys.readouterr().err
        == "Sync failed; check configuration, credentials and private file permissions.\n"
    )


@pytest.mark.parametrize("selection", ["default-parent", "default-root", "empty"])
def test_fix2_install_refuses_symlinked_or_empty_transcript_selections(
    tmp_path, monkeypatch, selection
):
    home = tmp_path / "home"
    home.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    args = [
        "sync",
        "install",
        "--enable",
        "--no-service",
        "--home",
        str(home),
        "--hub",
        "https://sync.example",
    ]
    if selection.startswith("default"):
        if selection == "default-parent":
            (target / "projects").mkdir()
            (home / ".claude").symlink_to(target, target_is_directory=True)
        else:
            (home / ".claude").mkdir()
            (home / ".claude/projects").symlink_to(target, target_is_directory=True)
    errors = []
    original = sync_cli._selections

    def selections(*args):
        try:
            return original(*args)
        except ValueError as error:
            errors.append(str(error))
            raise

    monkeypatch.setattr(sync_cli, "_selections", selections)
    assert cli.main(args) == 2
    assert errors == [
        "Select at least one source"
        if selection == "empty"
        else "Symlinked transcript directory refused"
    ]
    assert not (home / ".commonplace/sync/config.json").exists()


@pytest.mark.parametrize("selection", ["memory-parent", "memory-root"])
def test_fix2_memory_install_refused_before_selection(tmp_path, monkeypatch, capsys, selection):
    home = tmp_path / "home"
    home.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    link = home / "notes"
    link.symlink_to(target, target_is_directory=True)
    root = link
    if selection == "memory-parent":
        (target / "nested").mkdir()
        root = link / "nested"
    calls = []
    monkeypatch.setattr(sync_cli, "_selections", lambda *args: (calls.append(1), ([], []))[1])
    assert (
        cli.main(
            [
                "sync",
                "install",
                "--enable",
                "--no-service",
                "--home",
                str(home),
                "--memory",
                "notes=" + str(root),
                "--hub",
                "https://sync.example",
            ]
        )
        == 2
    )
    assert capsys.readouterr().out == (
        "Legacy memory sync is disabled; memory-once is not available yet.\n"
    )
    assert calls == []
    assert not (home / ".commonplace").exists()


def test_fix2_free_sleep_handles_expired_deadlines(local, monkeypatch):
    now, slept = [NOW], []
    config, state, _ = local
    hub = Hub(interval=900, window="2033-05-18T03:33:21Z")  # NOW + 1

    def cycle(session, **kwargs):
        state.defer(session.key, NOW + 1)
        now[0] = NOW + 2  # Work finishes after both previously future deadlines.
        return 0

    monkeypatch.setattr(sync_daemon._Session, "cycle", cycle)
    try:
        result = sync_daemon.run(
            config,
            state,
            clock=lambda: now[0],
            sleep=slept.append,
            transport=hub,
            stop=lambda: bool(slept),
        )
    except ValueError as exc:
        raise AssertionError("expired deadlines must still produce a bounded sleep") from exc
    assert result == 0 and slept == [2]


@pytest.mark.parametrize("earlier", ["rejected", "conflict"])
def test_fix2_pause_exits_successfully_after_prior_failure(local, earlier):
    _, _, root = local
    (root / "a.jsonl").write_bytes(record("bad"))
    (root / "b.jsonl").write_bytes(record("good"))
    hub = Hub()

    def transport(target, token, method, route, body=None):
        if method == "POST":
            if body["thread_id"] == "bad":
                return (403, {}) if earlier == "rejected" else (409, {"code": "prefix_conflict"})
            return 409, {"error": "Workspace capture is off"}
        return hub(target, token, method, route, body)

    assert run(local, transport) == 0


@pytest.mark.parametrize("status", [408, 425])
def test_fix2_temporary_rejection_remains_pending(local, status):
    config, state, root = local
    (root / "bad.jsonl").write_bytes(record("bad"))
    assert run(local, Hub(reject=status)) == 1
    pending = state.outbox_pending()
    assert len(pending) == 1 and pending[0].native_id == "bad"
    # Retry after the durable backoff does not require restart/requeue.
    session = sync_daemon._Session(config, state, lambda: NOW + 60, Hub())
    assert session.cycle(once=True) == 0
    assert not state.outbox_pending()


@pytest.mark.parametrize("failure", ["symlink", "regular-file"])
def test_fix2_source_error_status_matches_actual_kind(local, monkeypatch, failure):
    config, state, root = local
    path = root / "bad.jsonl"
    path.write_bytes(record("bad"))
    source = sync_sources.Source("claude-code", str(path), "bad", path.stat().st_size)
    state.store_cursor("claude-code", path, "bad", 0, sha(b""))
    config["_discovered"] = [source]

    def refused(path):
        raise sync_sources.SourceError(
            "Symlinked source refused" if failure == "symlink" else "Source must be a regular file"
        )

    monkeypatch.setattr(sync_sources, "_open", refused)
    assert sync_sources.scan(config, state, now=NOW) == []
    assert state.statuses()[0]["status"] == "unreadable"


def parking_sources(local):
    config, _, root = local
    (root / "a-bad.jsonl").write_bytes(record("bad"))
    (root / "b-good.jsonl").write_bytes(record("good"))
    codex = root.parent / "codex"
    codex.mkdir()
    (codex / "session.jsonl").write_text(
        json.dumps({"type": "session_meta", "payload": {"id": "codex-session"}}) + "\n"
    )
    config["sources"].append({"source": "codex", "root": str(codex)})
    config["tokens"] = {env: "synthetic-" + source for source, env in TOKEN_ENV.items()}


@pytest.mark.parametrize(
    "stage,once",
    [("get", False), ("get", True), ("post", False), ("post", True), ("legacy", False)],
)
def test_card223_f1_capture_cadence_is_stream_local(local, stage, once):
    parking_sources(local)
    config, state, root = local
    hub, now, attempts = Hub(), [NOW], []

    def transport(target, token, method, route, body=None):
        limited = (stage == "get" and method == "GET" and "thread_id=bad" in route) or (
            stage == "post" and method == "POST" and body.get("thread_id") == "bad"
        )
        if limited:
            attempts.append(now[0])
            if now[0] < NOW + 7:
                return 429, {"error": "Capture not due; retry in 7 seconds"}
        return hub(target, token, method, route, body)

    active = sync_daemon._Session(config, state, lambda: now[0], transport)
    if stage == "legacy":
        state.defer(active.key, NOW + 900)
    assert active.cycle(once=once) == 0
    assert hub.stored.get("good") == record("good")  # RED
    assert {body["source"] for body in hub.posts} == {"claude-code", "codex"}
    if stage != "legacy":
        assert state.next_due(active.key) == 0  # RED
        assert active.retry_due == NOW + 7
        assert "bad" not in hub.stored
        if stage == "get":
            assert state.cursor("claude-code", root / "a-bad.jsonl", "bad") is None
        else:
            assert len(state.outbox_pending()) == 1
        # A fresh session honors the durable deadline even in --once mode.
        active = sync_daemon._Session(config, state, lambda: now[0], transport)
        assert active.cycle(once=once) == 0
        assert attempts == [NOW]
        now[0] += 7
        assert active.cycle(once=once) == 0
        assert hub.stored["bad"] == record("bad")
        assert attempts == [NOW, NOW + 7]
    assert not state.outbox_pending()


def test_card223_old_verified_conflict_is_requeued_but_changed_slice_stays_parked(local):
    parking_sources(local)
    config, state, root = local
    hub = Hub()
    active = sync_daemon._Session(config, state, lambda: NOW, hub)
    assert active.initialize() == 0
    sync_sources.scan(config, state, now=NOW)
    for op in state.outbox_pending():
        if op.native_id in ("bad", "good"):
            state.outbox_mark(op.op_id, "conflict")
    (root / "b-good.jsonl").write_bytes(record("good", "changed words"))
    assert run(local, hub) == 0
    assert hub.stored.get("bad") == record("bad")  # RED
    assert "good" not in hub.stored
    assert [(op.native_id, op.state) for op in state.outbox_all()] == [("good", "conflict")]


def test_card223_guard_s3_import_account_cadence_limits_fanout(local):
    config, state, root = local
    notes = root.parent / "notes"
    notes.mkdir()
    for i in range(36):
        note = notes / f"m{i:02d}.md"
        note.write_text(f"Synthetic verbatim memory {i}.\n")
        os.utime(note, (NOW - 120, NOW - 120))
    legacy = [retained_import(state, note) for note in sorted(notes.glob("*.md"))]
    now, posts, admitted, hub = [NOW], [], [], Hub()

    def transport(target, token, method, route, body=None):
        if method == "POST" and route == "/api/import":
            posts.append(now[0])
            if admitted and now[0] - admitted[-1] < 900:
                return 429, {"error": "Import not due; retry in 900 seconds"}
            admitted.append(now[0])
        return hub(target, token, method, route, body)

    active = sync_daemon._Session(config, state, lambda: now[0], transport)
    state.defer(active.bkey + "-done", NOW)
    for step in range(0, 3600 + 1, 60):
        now[0] = NOW + step
        assert active.cycle() == 0
    assert admitted == posts == []
    assert state.outbox_all() == legacy


def test_card223_guard_slow_down_stops_get_pass(local):
    config, state, root = local
    for sid in ("a", "b", "c"):
        (root / f"{sid}.jsonl").write_bytes(record(sid))
    gets = []

    def transport(target, token, method, route, body=None):
        gets.append(route)
        return 429, {"error": "slow down"}

    active = sync_daemon._Session(config, state, lambda: NOW, transport)
    assert active.cycle() == 0
    assert len(gets) == 1


def test_card223_guard_s5b_run_loop_wakes_at_900_seconds(local):
    config, state, root = local
    (root / "bad.jsonl").write_bytes(record("bad"))
    now, attempts, sleeps, hub = [NOW], [], [], Hub(interval=900)

    def transport(target, token, method, route, body=None):
        if method == "POST":
            attempts.append(now[0] - NOW)
            if now[0] < NOW + 900:
                return 429, {"error": "Capture not due; retry in 900 seconds"}
        return hub(target, token, method, route, body)

    def advance(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    assert (
        sync_daemon.run(
            config,
            state,
            clock=lambda: now[0],
            sleep=advance,
            transport=transport,
            stop=lambda: now[0] >= NOW + 1800,
        )
        == 0
    )
    assert attempts == [0, 900]
    assert sleeps == [900, 900]


def test_card223_account_capture_wait_defers_whole_token(local):
    parking_sources(local)
    config, state, _ = local
    hub, posts = Hub(), []

    def transport(target, token, method, route, body=None):
        if method == "POST" and body["source"] == "claude-code":
            posts.append(body)
            return 429, {"error": "Account capture not due; retry in 900 seconds"}
        return hub(target, token, method, route, body)

    active = sync_daemon._Session(config, state, lambda: NOW, transport)
    assert active.cycle() == 0
    assert any(body["source"] == "codex" for body in hub.posts)
    assert all(body["source"] != "native-memory" for body in hub.posts)  # RED
    assert len(posts) == 1
    assert state.next_due(active._token_key(TOKEN_ENV["claude-code"])) == NOW + 900


def test_card223_exhausted_budget_does_not_reread_body(local, monkeypatch):
    config, state, root = local
    (root / "bad.jsonl").write_bytes(record("bad"))
    active = sync_daemon._Session(config, state, lambda: NOW, Hub())
    state.defer(active.bkey + "-done", NOW)
    assert active.initialize() == 0
    sync_sources.scan(config, state, now=NOW)
    for _ in range(sync_daemon.WRITE_BUDGET[0]):
        assert active._budget(TOKEN_ENV["claude-code"])
    reads = []
    body = sync_sources.body

    def counted(op):
        reads.append(op.op_id)
        return body(op)

    monkeypatch.setattr(sync_sources, "body", counted)
    assert active.cycle() == 0
    assert reads == []  # RED


def test_card223_initialize_backoff_doubles_and_caps_at_1800(local):
    config, state, root = local
    (root / "bad.jsonl").write_bytes(record("bad"))
    now, gets = [NOW], []

    def transport(target, token, method, route, body=None):
        gets.append(now[0])
        return 429, {"error": "slow down"}

    active = sync_daemon._Session(config, state, lambda: now[0], transport)
    assert active.cycle() == 0
    assert state.next_due(active.key) == 0  # RED
    key = active._token_key(TOKEN_ENV["claude-code"])
    for delay in (60, 120, 240, 480, 960, 1800, 1800):
        assert state.next_due(key) == now[0] + delay
        assert active.cycle() == 0
        before = len(gets)
        now[0] += delay
        assert active.cycle() == 0
        assert len(gets) == before + 1


@pytest.mark.parametrize(
    "first,second,scope",
    [
        (
            {"error": "slow down", "_retry_after": "10000000"},
            {"error": "slow down", "_retry_after": "0"},
            "token",
        ),
        (
            {"error": "slow down", "_retry_after": "Fri, 01 Jan 2100 00:00:00 GMT"},
            {"error": "slow down", "_retry_after": "Wed, 01 Jan 2020 00:00:00 GMT"},
            "token",
        ),
        (
            {"error": "Account capture not due; retry in 10000000 seconds"},
            {"error": "Account capture not due; retry in 0 seconds"},
            "token",
        ),
        (
            {"error": "Capture not due; retry in 10000000 seconds"},
            {"error": "Capture not due; retry in 0 seconds"},
            "stream",
        ),
    ],
    ids=["seconds", "http-date", "account-message", "stream-message"],
)
def test_card223_server_wait_is_clamped_and_invalid_wait_uses_floor(local, first, second, scope):
    config, state, root = local
    path = root / "bad.jsonl"
    path.write_bytes(record("bad"))
    state.store_cursor("claude-code", path, "bad", 0, sha(b""))
    now, posts = [NOW], []

    def transport(target, token, method, route, body=None):
        posts.append(now[0])
        return 429, dict(first if len(posts) == 1 else second)

    active = sync_daemon._Session(config, state, lambda: now[0], transport)
    assert active.cycle() == 0
    # Include the legacy shared key so the base comparison fails by assertion.
    due = max(
        state.next_due(active.key),
        *(
            state.next_due(key)
            for key in [
                active.key + ":token:" + TOKEN_ENV["claude-code"],
                active._stream_key("claude-code", "bad")
                if hasattr(active, "_stream_key")
                else active.key,
            ]
        ),
    )
    assert due == NOW + 3600  # RED
    now[0] += 3600
    assert active.cycle() == 0
    key = (
        active._token_key(TOKEN_ENV["claude-code"])
        if scope == "token"
        else active._stream_key("claude-code", "bad")
    )
    assert state.next_due(key) == now[0] + (120 if scope == "token" else 60)
    now[0] += 1
    assert active.cycle() == 0
    assert len(posts) == 2


def test_card223_server_409_stays_rejected_across_restart(local):
    config, state, root = local
    (root / "bad.jsonl").write_bytes(record("bad"))
    hub, posts = Hub(), []

    def transport(target, token, method, route, body=None):
        if method == "POST":
            posts.append(body.copy())
            return 409, {"code": "prefix_conflict"}
        return hub(target, token, method, route, body)

    assert run(local, transport) == 1
    assert [op.state for op in state.outbox_all()] == ["rejected"]  # RED
    assert run(local, transport) == 0
    assert len(posts) == 1
    assert any(row["status"] == "rejected" for row in state.statuses())


def test_card223_guard_get_outage_never_parks_pending_operation(local):
    config, state, root = local
    pending_path = root / "pending.jsonl"
    pending_path.write_bytes(record("pending"))
    state.store_cursor("claude-code", pending_path, "pending", 0, sha(b""))
    (root / "a-unknown.jsonl").write_bytes(record("unknown"))
    sync_sources.scan(config, state, now=NOW)
    now, gets, hub = [NOW], [], Hub()

    def transport(target, token, method, route, body=None):
        if method == "GET":
            gets.append(now[0])
        if now[0] < NOW + 2400:
            return 500, {}
        return hub(target, token, method, route, body)

    active = sync_daemon._Session(config, state, lambda: now[0], transport)
    state.defer(active.bkey + "-done", NOW)
    for step in range(0, 2400, 60):
        now[0] = NOW + step
        active.cycle()
        assert all(op.state == "pending" for op in state.outbox_all())
    for step in range(2400, 3 * 3600 + 1, 60):
        now[0] = NOW + step
        active.cycle()
    assert hub.stored.get("pending") == record("pending")
    assert hub.stored.get("unknown") == record("unknown")
    assert not state.outbox_all()


def test_card223_guard_successful_get_does_not_reset_post_streak(local):
    config, state, root = local
    (root / "new.jsonl").write_bytes(record("new"))
    active = sync_daemon._Session(config, state, lambda: NOW, Hub())
    key = active.key + ":token:" + TOKEN_ENV["claude-code"]
    state.defer(key + ":failures", 3)
    assert active.initialize() == 0
    assert state.next_due(key + ":failures") == 3


def seeded(local, sids):
    config, state, root = local
    for index, sid in enumerate(sids):
        path = root / f"{index:04d}-{sid}.jsonl"
        path.write_bytes(record(sid))
        state.store_cursor("claude-code", path, sid, 0, sha(b""))
    sync_sources.scan(config, state, now=NOW)
    for op in state.outbox_pending():
        state.set_status(op.source, op.path, op.native_id, "pending")
    return {op.native_id: op for op in state.outbox_pending()}


def op_key(active, op):
    return active.key + ":op:" + op.op_id


def token_key(active, source="claude-code"):
    return active.key + ":token:" + TOKEN_ENV[source]


def active_session(local, now, transport):
    config, state, _ = local
    active = sync_daemon._Session(config, state, lambda: now[0], transport)
    state.defer(active.bkey + "-done", NOW)
    return active


def attempt_times(posts, sid):
    return [step for step, name in posts if name == sid]


def synthetic_failure(fault):
    if fault in ("sent_reset", "sent_timeout", "html502"):
        # Old code has no SentError: preserve its post-send OSError behavior.
        raise getattr(sync_daemon, "SentError", OSError)("Synthetic sent failure")
    if fault == "offset":
        return 200, {"offset": 123456, "prefix_sha256": sha(b"")}
    if fault == "errors_invalid":
        return 200, {"accepted": 0, "errors": 1, "results": [{"code": "invalid_transcript"}]}
    if fault == "state_error":
        return 200, {"offset": len(record("bad")), "prefix_sha256": sha(record("bad"))}
    return int(str(fault).removeprefix("s")), {}


def test_card223_t01_connect_error_ends_cycle(local):
    parking_sources(local)
    config, state, _ = local
    hub, posts = Hub(), []

    def transport(target, token, method, route, body=None):
        if method == "POST":
            posts.append(body)
            raise OSError("Synthetic connection refusal")
        return hub(target, token, method, route, body)

    active = active_session(local, [NOW], transport)
    assert active.cycle() == 1
    assert len(posts) == 1 and posts[0]["thread_id"] == "bad"
    assert not state.conn.execute("SELECT 1 FROM cadence WHERE key LIKE '%:op:%'").fetchall()


@pytest.mark.parametrize(
    "fault",
    [
        500,
        503,
        408,
        425,
        "sent_reset",
        "sent_timeout",
        "html502",
        "s302",
        "offset",
        "errors_invalid",
        "state_error",
        "s405",
        "s413",
        "s422",
    ],
)
def test_card223_t02_failure_ends_only_this_token(local, monkeypatch, fault):
    from commonplace.sync_state import StateError

    parking_sources(local)
    config, state, _ = local
    now, hub, posts = [NOW], Hub(), []
    ack = state.outbox_ack

    def acknowledge(oid, payload):
        if (
            fault == "state_error"
            and next(op for op in state.outbox_all() if op.op_id == oid).native_id == "bad"
        ):
            raise StateError("Synthetic acknowledgement failure")
        return ack(oid, payload)

    monkeypatch.setattr(state, "outbox_ack", acknowledge)

    def transport(target, token, method, route, body=None):
        if method == "POST":
            posts.append((now[0] - NOW, body.get("thread_id", "import")))
            if body.get("thread_id") == "bad":
                return synthetic_failure(fault)
        return hub(target, token, method, route, body)

    active = active_session(local, now, transport)
    raised = None
    try:
        result = active.cycle()
    except StateError as exc:
        raised, result = type(exc).__name__, None
    assert (raised, result, "codex-session" in hub.stored, "good" in hub.stored) == (  # RED
        None,
        1,
        True,
        False,
    )
    assert attempt_times(posts, "bad") == [0]
    assert any(body["source"] == "codex" for body in hub.posts)
    assert all(body["source"] != "native-memory" for body in hub.posts)
    bad = next(op for op in state.outbox_all() if op.native_id == "bad")
    key = op_key(active, bad)
    assert [state.next_due(key + suffix) for suffix in (":fails", ":strikes", ":last", "")] == [
        1,
        0,
        NOW,
        NOW + 60,
    ]
    now[0] += 60
    assert active.cycle() == 1
    assert posts[-2:] == [(60, "bad"), (60, "good")]
    assert hub.stored["good"] == record("good")


def test_card223_t02_strike_needs_ack_after_previous_failure(local):
    _, state, root = local
    ops = seeded(local, ["p"])
    now, posts, hub = [NOW], [], Hub()

    def transport(target, token, method, route, body=None):
        if method == "POST" and body["thread_id"] == "p":
            posts.append(now[0] - NOW)
            return 500, {}
        return hub(target, token, method, route, body)

    active = active_session(local, now, transport)
    key = op_key(active, ops["p"])
    for step in (0, 60, 100, 180):
        now[0] = NOW + step
        if step == 100:
            (root / "h.jsonl").write_bytes(record("h"))
        active.cycle()
    assert (posts, state.next_due(key + ":strikes")) == ([0, 60, 180], 1)  # RED
    assert state.next_due(key + ":acks_at_fail") == 1


def test_card223_t02_retry_after_on_5xx(local):
    seeded(local, ["p"])
    now, posts = [NOW], []

    def transport(target, token, method, route, body=None):
        posts.append(now[0] - NOW)
        return 503, {"_retry_after": "3600"}

    active = active_session(local, now, transport)
    for step in range(0, 3601, 60):
        now[0] = NOW + step
        active.cycle()
    assert posts == [0, 3600]  # RED


@pytest.mark.parametrize(
    "ack_first", [False, True], ids=["legacy_resend_once_per_attempt", "no_resend_after_op_id_ack"]
)
def test_card223_t04_legacy_resend(local, ack_first):
    _, state, _ = local
    seeded(local, ["a-h"] if ack_first else [])
    now, posts, hub = [NOW], [], Hub()
    active = active_session(local, now, hub)
    if ack_first:
        active.cycle()
    seeded(local, ["p"])

    def transport(target, token, method, route, body=None):
        if method == "POST":
            posts.append((now[0] - NOW, "client_op_id" in body))
            return 400, {}
        return hub(target, token, method, route, body)

    active.transport = transport
    active.cycle()
    now[0] += 60
    active.cycle()
    expected = (
        [(0, True), (60, True)] if ack_first else [(0, True), (0, False), (60, True), (60, False)]
    )
    assert posts == expected and state.outbox_all()[0].state == "pending"  # RED
    assert state.next_due(op_key(active, state.outbox_all()[0]) + ":fails") == 2
    assert state.next_due(token_key(active) + ":op_ids") == int(ack_first)


def test_card223_f4_legacy_without_retry_slot_yields_once(local, monkeypatch):
    parking_sources(local)
    _, state, _ = local
    now, posts, hub = [NOW], [], Hub()
    budget = sync_daemon._Session._budget

    def no_retry(self, env_name, retry=False, **kwargs):
        return False if retry else budget(self, env_name, **kwargs)

    monkeypatch.setattr(sync_daemon._Session, "_budget", no_retry)

    def transport(target, token, method, route, body=None):
        if method == "POST" and body.get("thread_id") == "bad":
            posts.append(now[0] - NOW)
            return 400, {}
        return hub(target, token, method, route, body)

    active = active_session(local, now, transport)
    result = active.cycle()
    assert (result, posts, "codex-session" in hub.stored, "good" in hub.stored) == (  # RED
        1,
        [0],
        True,
        False,
    )
    assert active.cycle() == 0 and posts == [0]
    now[0] += 60
    assert active.cycle() == 1 and posts == [0, 60]
    assert "good" in hub.stored
    assert state.next_due(op_key(active, state.outbox_all()[0]) + ":fails") == 2


@pytest.mark.parametrize("status", [401, 403])
def test_card223_t06_post_401_403_revoke_and_continue(local, status):
    _, state, _ = local
    seeded(local, ["a-bad", "b-good"])
    now, posts, hub = [NOW], [], Hub()

    def transport(target, token, method, route, body=None):
        if method == "POST" and body["thread_id"] == "a-bad":
            posts.append(now[0] - NOW)
            return status, {}
        return hub(target, token, method, route, body)

    active = active_session(local, now, transport)
    assert active.cycle() == 1
    assert state.outbox_all()[0].state == "revoked"
    assert "b-good" in hub.stored and active.refresh == (status == 401)
    assert run(local, transport) == 1
    assert posts == [0, 0]


@pytest.mark.parametrize("off", [True, False], ids=["off", "offset_message"])
def test_card223_t07_capture_off_exact_text(local, off):
    _, state, _ = local
    seeded(local, ["p"])
    active = active_session(
        local,
        [NOW],
        lambda *args: (
            409,
            {"error": "Workspace capture is off" if off else "Saved offset differs"},
        ),
    )
    result = active.cycle()
    assert (result, active.enabled, state.statuses()[0]["status"]) == (  # RED
        (0, False, "paused") if off else (1, True, "rejected")
    )


def imports_seeded(local, count=3):
    config, state, root = local
    notes = root.parent / "notes"
    notes.mkdir()
    ops = []
    for index in range(1, count + 1):
        note = notes / f"i{index}.md"
        note.write_text(f"Synthetic memory {index}.\n")
        os.utime(note, (NOW - 120, NOW - 120))
        ops.append(retained_import(state, note))
    config["tokens"][TOKEN_ENV["native-memory"]] = "synthetic-import"
    return ops


@pytest.mark.parametrize(
    "code", ["conflict", "storage_limit", "import_unavailable", "invalid_transcript"]
)
def test_card223_import_row_classification(local, code):
    _, state, _ = local
    ops = imports_seeded(local)
    active = active_session(local, [NOW], Hub())
    body = {
        "source": "native-memory",
        "threads": [
            {"thread_id": ops[0].native_id, "format": "file", "transcript": "Synthetic memory 1.\n"}
        ],
    }
    result = active._post_result(
        ops[0],
        TOKEN_ENV["native-memory"],
        body,
        200,
        {"accepted": 0, "errors": 1, "results": [{"code": code}]},
        None,
    )
    bad = next(op for op in state.outbox_all() if op.op_id == ops[0].op_id)
    key = op_key(active, bad)
    expected = (
        ("rejected", 0, 0)
        if code == "conflict"
        else ("pending", 0, NOW + 60)
        if code == "storage_limit"
        else ("pending", 1, 0)
    )
    assert (
        bad.state,
        state.next_due(key + ":fails"),
        state.next_due(token_key(active, "native-memory")),
    ) == expected
    assert result == (
        "terminal" if code == "conflict" else "token" if code == "storage_limit" else "failure"
    )
    if code not in ("conflict", "storage_limit"):
        assert state.next_due(key) == NOW + 60
    assert all(op.attempts == 0 for op in state.outbox_all())


@pytest.mark.parametrize("status", [507, 404, 421])
def test_card223_t12_token_wide_replies(local, status):
    parking_sources(local)
    _, state, _ = local
    now, hub, posts = [NOW], Hub(), []

    def transport(target, token, method, route, body=None):
        if method == "POST" and body.get("thread_id") == "bad":
            posts.append(body)
            return status, {}
        return hub(target, token, method, route, body)

    active = active_session(local, now, transport)
    result = active.cycle()
    assert (result, len(posts), "codex-session" in hub.stored, "good" in hub.stored) == (  # RED
        1,
        1,
        True,
        False,
    )
    assert state.next_due(token_key(active)) == NOW + 60
    assert state.next_due(token_key(active) + ":failures") == 1
    assert not state.conn.execute("SELECT 1 FROM cadence WHERE key LIKE '%:op:%'").fetchall()


@pytest.mark.parametrize(
    "kind",
    ["import", "slow_down"],
    ids=["t11_import_wait_defers_only_import_token", "t11_post_slow_down_defers_token"],
)
def test_card223_t11_token_wait(local, kind):
    parking_sources(local)
    _, state, _ = local
    legacy = imports_seeded(local, 1) if kind == "import" else []
    hub, posts = Hub(), []

    def transport(target, token, method, route, body=None):
        limited = (
            route == "/api/import" if kind == "import" else body and body.get("thread_id") == "bad"
        )
        if method == "POST" and limited:
            posts.append(body)
            return (
                (429, {"error": "Import not due; retry in 900 seconds"})
                if kind == "import"
                else (429, {"error": "slow down", "_retry_after": "5"})
            )
        return hub(target, token, method, route, body)

    active = active_session(local, [NOW], transport)
    active.cycle()
    key = token_key(active, "native-memory" if kind == "import" else "claude-code")
    assert state.next_due(key) == (0 if kind == "import" else NOW + 5)  # RED
    assert "codex-session" in hub.stored and len(posts) == (0 if kind == "import" else 1)
    assert all(op in state.outbox_all() for op in legacy)
    assert ("good" in hub.stored) == (kind == "import")
    assert not state.conn.execute("SELECT 1 FROM cadence WHERE key LIKE '%:op:%'").fetchall()


@pytest.mark.parametrize(
    "fault",
    ["sent_timeout", "invalid", "state_error", "s302", 500, 503, 400, 401, 403, 404, 410, 421],
)
def test_card223_t15_t16_get_failures_use_stream_ladder(local, monkeypatch, fault):
    from commonplace.sync_state import StateError

    config, state, root = local
    seeded(local, ["z"])
    (root / "a-unknown.jsonl").write_bytes(record("unknown"))
    now, gets, hub = [NOW], [], Hub()
    store = state.store_cursor

    def store_cursor(source, path, native_id, offset, prefix):
        if fault == "state_error" and native_id == "unknown":
            raise StateError("Synthetic cursor failure")
        return store(source, path, native_id, offset, prefix)

    monkeypatch.setattr(state, "store_cursor", store_cursor)

    def transport(target, token, method, route, body=None):
        if method == "GET" and "thread_id=unknown" in route:
            gets.append(now[0] - NOW)
            if fault == "state_error":
                return 200, {"offset": 0, "prefix_sha256": sha(b"")}
            if fault == "invalid":
                return 200, {"offset": -1}
            return synthetic_failure(fault)
        return hub(target, token, method, route, body)

    active = active_session(local, now, transport)
    raised, results = None, []
    try:
        for step in range(0, 601, 60):
            now[0] = NOW + step
            results.append(active.cycle())
    except StateError as exc:
        raised = type(exc).__name__
    assert (raised, gets, hub.stored.get("z")) == (None, [0, 60, 180, 420], record("z"))  # RED
    revoked = isinstance(fault, int) and fault < 500
    if revoked:
        assert results == [1] * 11
        assert any(
            row["native_id"] == "unknown" and row["status"] == "revoked" for row in state.statuses()
        )
    assert state.next_due(token_key(active) + ":failures") == 0
    key = active._stream_key("claude-code", "unknown")
    assert state.next_due(key + ":fails") == 4


@pytest.mark.parametrize("status", [408, 425, 500, 503, 429])
def test_card223_initialize_token_failure_skips_same_token_but_sends_others(local, status):
    parking_sources(local)
    config, state, _ = local
    now, gets, hub = [NOW], [], Hub()

    def transport(target, token, method, route, body=None):
        if method == "GET" and token == config["tokens"][TOKEN_ENV["claude-code"]]:
            gets.append(route)
            return status, {"error": "slow down", "_retry_after": "37"}
        return hub(target, token, method, route, body)

    active = active_session(local, now, transport)
    result = active.cycle()
    assert (len(gets), "codex-session" in hub.stored) == (1, True)  # RED
    assert any(body["source"] == "codex" for body in hub.posts)
    assert all(body["source"] != "native-memory" for body in hub.posts)
    assert result == (0 if status == 429 else 1)
    key = token_key(active) if status == 429 else active._stream_key("claude-code", "bad")
    assert state.next_due(key) == NOW + 37
    assert state.next_due(token_key(active)) == (NOW + 37 if status == 429 else 0)


@pytest.mark.parametrize("status", [429, 408, 425, 500, 503])
def test_card223_token_retry_after_exponential_backoff_and_success_reset(local, status):
    parking_sources(local)
    config, state, root = local
    for name, sid in (("a-bad", "bad"), ("b-good", "good")):
        state.store_cursor("claude-code", root / f"{name}.jsonl", sid, 0, sha(b""))
    now, posts, hub, broken = [NOW], [], Hub(), [True]

    def transport(target, token, method, route, body=None):
        if method == "POST" and body.get("thread_id") == "bad" and broken[0]:
            posts.append(now[0] - NOW)
            return status, {
                "error": "slow down",
                **({"_retry_after": "37"} if len(posts) == 1 else {}),
            }
        return hub(target, token, method, route, body)

    active = active_session(local, now, transport)
    result = active.cycle()
    assert "codex-session" in hub.stored and result == (0 if status == 429 else 1)  # RED
    bad = next(op for op in state.outbox_all() if op.native_id == "bad")
    key = token_key(active) if status == 429 else op_key(active, bad)
    assert state.next_due(key) == NOW + 37
    now[0] += 36
    assert active.cycle() == 0 and posts == [0]
    now[0] += 1
    active.cycle()
    assert posts == [0, 37]
    assert state.next_due(key) == NOW + 157
    assert ("good" in hub.stored) == (status != 429)
    now[0] = NOW + 157
    broken[0] = False
    assert active.cycle() == 0
    assert not state.outbox_all()
    assert state.next_due(token_key(active) + ":failures") == 0
    assert not state.conn.execute("SELECT 1 FROM cadence WHERE key LIKE '%:op:%'").fetchall()


@pytest.mark.parametrize("status", [302, 408, 425, 500, 503, 200])
def test_card223_f3_failed_head_yields_then_retries_same_op(local, status):
    parking_sources(local)
    _, state, _ = local
    now, attempts, hub = [NOW], [], Hub()

    def transport(target, token, method, route, body=None):
        if method == "POST" and body.get("thread_id") == "bad":
            attempts.append(body.copy())
            if now[0] < NOW + 180:
                return status, {}
        return hub(target, token, method, route, body)

    active = active_session(local, now, transport)
    active.cycle()
    assert "codex-session" in hub.stored  # RED
    assert "good" not in hub.stored
    now[0] += 60
    active.cycle()
    assert hub.stored["good"] == record("good")
    assert len(attempts) == 2 and attempts[0] == attempts[1]
    now[0] += 120
    assert active.cycle() == 0
    assert hub.stored["bad"] == record("bad")
    assert attempts == [attempts[0]] * 3 and not state.outbox_all()


@pytest.mark.parametrize("count", [1, 2])
def test_card223_s1_poison_does_not_starve_healthy(local, count):
    _, state, root = local
    ops = seeded(local, [f"p{i}" for i in range(count)])
    now, posts, stored_at, hub = [NOW], [], [], Hub()

    def transport(target, token, method, route, body=None):
        if method == "POST":
            posts.append((now[0] - NOW, body["thread_id"]))
            if body["thread_id"].startswith("p"):
                return 500, {}
            stored_at.append(now[0] - NOW)
        return hub(target, token, method, route, body)

    active = active_session(local, now, transport)
    for step in range(0, 10861, 60):
        now[0] = NOW + step
        if step == 3660:
            (root / "healthy.jsonl").write_bytes(record("healthy"))
        active.cycle()
    expected = (
        [[0, 60, 180, 420, 900, 1860, 3660, 5460, 7260, 9060, 10860]]
        if count == 1
        else [
            [0, 60, 180, 420, 1140, 3660, 5460, 7260, 10020],
            [60, 120, 240, 660, 2100, 3720, 5700, 8220],
        ]
    )
    assert (stored_at, [attempt_times(posts, f"p{i}") for i in range(count)]) == (  # RED
        [3660],
        expected,
    )
    assert [state.next_due(op_key(active, ops[f"p{i}"]) + ":strikes") for i in range(count)] == [
        1
    ] * count
    lane_s = [step for sid in ops for step in attempt_times(posts, sid)[2:]]
    assert max(sum(t <= step < t + 3600 for step in lane_s) for t in lane_s if t >= 3600) <= 6


def test_card223_s1b_known_healthy_after_two_poisons(local):
    _, state, _ = local
    ops = seeded(local, ["a0", "a1", "z"])
    now, posts, gets, hub = [NOW], [], [], Hub()

    def transport(target, token, method, route, body=None):
        if method == "GET":
            gets.append(route)
        if method == "POST":
            posts.append((now[0] - NOW, body["thread_id"]))
            if body["thread_id"].startswith("a"):
                return 500, {}
        return hub(target, token, method, route, body)

    active = active_session(local, now, transport)
    for step in (0, 60, 120):
        now[0] = NOW + step
        active.cycle()
    assert posts == [(0, "a0"), (60, "a0"), (60, "a1"), (120, "a1"), (120, "z")]  # RED
    assert hub.stored["z"] == record("z") and gets == []
    assert [state.next_due(op_key(active, ops[sid]) + ":strikes") for sid in ("a0", "a1")] == [0, 0]


@pytest.mark.parametrize("post_only", [False, True], ids=["hub-wide", "post-only"])
def test_card223_guard_s2_short_outage_preserves_budget(local, post_only):
    seeded(local, [f"s{i:03d}" for i in range(130)])
    now, posts, hub = [NOW], [], Hub()

    def transport(target, token, method, route, body=None):
        if method == "POST":
            posts.append((now[0] - NOW, body["thread_id"]))
        if now[0] == NOW and (method == "POST" or not post_only):
            return 500, {}
        return hub(target, token, method, route, body)

    active = active_session(local, now, transport)
    counts = []
    for step in (0, 60, 120, 3600, 3660):
        now[0] = NOW + step
        active.cycle()
        counts.append(len(hub.stored))
    assert counts == [0, 119, 119, 120, 130]
    assert sum(step == 0 for step, _ in posts) == 1
    # Base has the same totals but retries s000 before the rest at 60 as well.
    assert attempt_times(posts, "s000") == [0, 60]


def test_card223_guard_backfill_post_only_outage(local):
    _, state, _ = local
    seeded(local, [f"s{i:03d}" for i in range(130)])
    now, posts, hub = [NOW], [], Hub()

    def transport(target, token, method, route, body=None):
        if method == "POST":
            posts.append((now[0] - NOW, body["thread_id"]))
            if now[0] == NOW:
                return 500, {}
        return hub(target, token, method, route, body)

    config = local[0]
    active = sync_daemon._Session(config, state, lambda: now[0], transport)
    counts = []
    for step in range(0, 661, 60):
        now[0] = NOW + step
        active.cycle()
        if step in (0, 60, 120, 600, 660):
            counts.append(len(hub.stored))
    assert counts == [0, 120, 121, 129, 130]
    assert state.next_due(active.bkey) == 11
    assert attempt_times(posts, "s000") == [0, 60]
    assert sum(step == 0 for step, _ in posts) == 1


def test_card223_guard_run_backfill_spacing(local):
    config, state, _ = local
    seeded(local, [f"s{i:03d}" for i in range(130)])
    now, posts, sleeps, hub = [NOW], [], [], Hub(interval=900)

    def transport(target, token, method, route, body=None):
        if method == "POST":
            posts.append(now[0] - NOW)
        return hub(target, token, method, route, body)

    def advance(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    assert (
        sync_daemon.run(
            config,
            state,
            clock=lambda: now[0],
            sleep=advance,
            transport=transport,
            stop=lambda: len(hub.stored) == 130,
        )
        == 0
    )
    assert posts == [0] * 121 + list(range(2, 19, 2))
    assert sleeps == [2] * 9 + [882]
    active = active_session(local, now, hub)
    assert state.next_due(active.bkey) == 10


@pytest.mark.parametrize("post_only", [False, True], ids=["hub-wide", "post-only"])
def test_card223_s5a_run_loop_one_failing_capture(local, post_only):
    config, state, _ = local
    ops = seeded(local, ["p"])
    now, posts, sleeps, hub = [NOW], [], [], Hub(interval=900)

    def transport(target, token, method, route, body=None):
        if method == "POST":
            posts.append(now[0] - NOW)
        if route.startswith("/api/events?"):
            return hub(target, token, method, route, body)
        if method == "POST" or not post_only:
            return 500, {}
        return hub(target, token, method, route, body)

    def advance(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    sync_daemon.run(
        config,
        state,
        clock=lambda: now[0],
        sleep=advance,
        transport=transport,
        stop=lambda: now[0] >= NOW + 3600,
    )
    assert posts == [0, 60, 180, 420, 900, 1860]  # RED
    assert len(sleeps) <= 16
    active = active_session(local, now, hub)
    assert state.next_due(op_key(active, ops["p"]) + ":strikes") == 0


@pytest.mark.parametrize("fault", [500, 408, 425, "html502"])
@pytest.mark.parametrize("post_only", [False, True], ids=["hub-wide", "post-only"])
def test_card223_e1_outage_recovers_without_restart_or_park(local, fault, post_only):
    _, state, _ = local
    ops = seeded(local, ["p"])
    now, posts, stored_at, hub = [NOW], [], [], Hub()

    def transport(target, token, method, route, body=None):
        if method == "POST":
            posts.append(now[0] - NOW)
        if now[0] < NOW + 2400 and (method == "POST" or not post_only):
            return synthetic_failure(fault)
        if method == "POST":
            stored_at.append(now[0] - NOW)
        return hub(target, token, method, route, body)

    active = active_session(local, now, transport)
    strikes = []
    for step in range(0, 3661, 60):
        now[0] = NOW + step
        active.cycle()
        strikes.append(state.next_due(op_key(active, ops["p"]) + ":strikes"))
    assert (posts, stored_at) == ([0, 60, 180, 420, 900, 1860, 3660], [3660])  # RED
    assert set(strikes) == {0} and not state.outbox_all()
    assert hub.stored["p"] == record("p")


def poison_transport(now, posts, hub, *, status=500, healthy_at=None):
    def transport(target, token, method, route, body=None):
        if method == "POST":
            sid = body["thread_id"]
            posts.append((now[0] - NOW, sid))
            if (
                sid.startswith("p")
                or healthy_at is not None
                and sid == "h"
                and now[0] - NOW < healthy_at
            ):
                return status, {}
        return hub(target, token, method, route, body)

    return transport


def test_card223_n60_poisons_leave_budget_for_healthy(local):
    _, state, root = local
    ops = seeded(local, [f"p{i:02d}" for i in range(60)])
    now, posts, hub = [NOW], [], Hub()
    active = active_session(local, now, poison_transport(now, posts, hub))
    latencies = []
    for step in range(0, 28800, 60):
        now[0] = NOW + step
        if 4200 <= step <= 24600 and step % 600 == 0:
            sid = f"h{step}"
            (root / f"{sid}.jsonl").write_bytes(record(sid))
        active.cycle()
        if 4200 <= step <= 24600 and step % 600 == 0:
            latencies.append(0 if f"h{step}" in hub.stored else None)
    lane_s = []
    for sid in ops:
        times = attempt_times(posts, sid)
        lane_s.extend(times[2:])
    lane_s.sort()
    expected = [3600, 3660, 3780, 4020, 4200, 4260, 4380, 4620, 4800, 4860, 4980, 5220]
    expected += [
        start + offset + delta
        for start in range(7200, 21601, 3600)
        for offset in (0, 600, 1200)
        for delta in (0, 60, 180, 420)
    ]
    expected += [25200, 25260, 25380, 25620, 26100, 27060]
    assert (latencies, lane_s) == ([0] * 35, expected)  # RED
    assert len(lane_s) == 78
    assert len([sid for _, sid in posts if sid.startswith("p")]) == 198
    assert all(sum(t <= step < t + 3600 for step in lane_s) <= 12 for t in lane_s)
    assert all(op.state == "pending" for op in state.outbox_all())
    assert all(attempt_times(posts, f"p{i:02d}")[:2] == [60 * i, 60 * (i + 1)] for i in range(60))


def test_card223_n60_400_poisons_cost_one_pass_each(local):
    _, state, root = local
    seeded(local, [f"p{i:02d}" for i in range(60)])
    now, posts, hub = [NOW], [], Hub()
    active = active_session(local, now, poison_transport(now, posts, hub, status=400))
    state.defer(token_key(active) + ":op_ids", 1)
    before = None
    for step in range(0, 3601, 60):
        now[0] = NOW + step
        if step == 600:
            (root / "h.jsonl").write_bytes(record("h"))
        active.cycle()
        if step == 3540:
            before = len(posts)
    assert (before, attempt_times(posts, "h")) == (119, [3600])  # RED
    assert hub.stored["h"] == record("h")


@pytest.mark.parametrize(
    "cohort",
    ["fresh", "persistent-idle", "persistent-traffic", "newer", "ahead"],
    ids=[
        "once_failed_healthy_behind_fresh_poisons",
        "once_failed_healthy_goes_before_persistent-idle",
        "once_failed_healthy_goes_before_persistent-traffic",
        "h_newer_than_16_op_cohort",
        "lane_r_cohort_ahead_of_h",
    ],
)
def test_card223_once_failed_healthy_cohorts(local, cohort):
    _, state, root = local
    ops = seeded(local, [f"p{i:02d}" for i in range(16)] + ["h"])
    now, posts, hub = [NOW], [], Hub()
    active = active_session(
        local,
        now,
        poison_transport(now, posts, hub, healthy_at=1020 if cohort == "newer" else None),
    )
    hkey = op_key(active, ops["h"])
    if cohort != "newer":
        state.defer(hkey + ":fails", 1)
        state.defer(hkey + ":last", NOW if cohort == "ahead" else NOW - 60)
        state.defer(hkey, NOW + 60 if cohort == "ahead" else NOW)
    for index in range(16):
        key = op_key(active, ops[f"p{index:02d}"])
        if cohort.startswith("persistent") or cohort == "ahead":
            state.defer(key + ":fails", 1 if cohort == "ahead" else 6)
            state.defer(
                key + ":last", NOW - 960 + index * 60 if cohort == "ahead" else NOW + index * 60
            )
    for step in range(0, 1081, 60):
        now[0] = NOW + step
        if cohort == "persistent-traffic":
            sid = f"z{step}"
            (root / f"{sid}.jsonl").write_bytes(record(sid))
        active.cycle()
    expected = [960] if cohort == "ahead" else [960, 1020] if cohort == "newer" else [0]
    assert attempt_times(posts, "h") == expected and hub.stored.get("h") == record("h")  # RED
    if cohort == "ahead":
        lane_s = sorted(
            step
            for sid in ops
            if sid.startswith("p")
            for step in attempt_times(posts, sid)[1:]
            if step < 960
        )
        assert lane_s == [120, 180, 300, 540]
    if cohort == "newer":
        assert (
            sorted(
                step for sid in ops if sid.startswith("p") for step in attempt_times(posts, sid)[2:]
            )[0]
            == 1020
        )


@pytest.mark.parametrize("older", [True, False], ids=["older", "newer"])
def test_card223_older_lane_f_behind_newer_poisons(local, older):
    _, state, _ = local
    seeded(local, (["h"] if older else []) + [f"p{i}" for i in range(6)] + ([] if older else ["h"]))
    now, posts, hub = [NOW], [], Hub()
    active = active_session(local, now, poison_transport(now, posts, hub))
    for step in range(0, 361, 60):
        now[0] = NOW + step
        active.cycle()
    assert attempt_times(posts, "h") == ([0] if older else [360])  # RED
    assert state.next_due(token_key(active) + ":failures") == 0


def test_card223_ceiling_ignores_acks(local):
    _, state, root = local
    ops = seeded(local, [f"p{i:02d}" for i in range(60)])
    now, posts, hub = [NOW], [], Hub()
    active = active_session(local, now, poison_transport(now, posts, hub))
    for op in ops.values():
        state.defer(op_key(active, op) + ":fails", 6)
    healthy = []
    for step in range(0, 7200, 60):
        now[0] = NOW + step
        sid = f"h{step}"
        (root / f"{sid}.jsonl").write_bytes(record(sid))
        active.cycle()
        healthy.append(sid in hub.stored)
    suspects = [step for step, sid in posts if sid.startswith("p")]
    assert healthy == [True] * 120 and len(suspects) == 24  # RED
    assert [sum(start <= stamp < start + 3600 for stamp in suspects) for start in (0, 3600)] == [
        12,
        12,
    ]
    assert all(sum(t <= stamp < t + 3600 for stamp in suspects) <= 12 for t in suspects)


def test_card223_live_mode_ceiling(local):
    config, state, root = local
    ops = seeded(local, [f"p{i:02d}" for i in range(60)])
    now, posts, hub = [NOW], [], Hub(interval=0)
    transport = poison_transport(now, posts, hub, status=400)
    active = active_session(local, now, transport)
    state.conn.execute("DELETE FROM cadence WHERE key=?", (active.bkey + "-done",))
    state.defer(token_key(active) + ":op_ids", 1)
    for op in ops.values():
        state.defer(op_key(active, op) + ":fails", 6)
    (root / "h.jsonl").write_bytes(record("h"))
    next_append = [10]

    def advance(seconds):
        now[0] += seconds
        while next_append[0] <= now[0] - NOW and next_append[0] < 3600:
            with (root / "h.jsonl").open("ab") as stream:
                stream.write(record("h", str(next_append[0])))
            next_append[0] += 10

    sync_daemon.run(
        config,
        state,
        clock=lambda: now[0],
        sleep=advance,
        transport=transport,
        stop=lambda: now[0] >= NOW + 3600,
    )
    suspects = [step for step, sid in posts if sid.startswith("p")]
    healthy = attempt_times(posts, "h")
    assert len(suspects) <= 12 and len(healthy) >= 300  # RED
    assert all((b - a) >= 2 for a, b in zip(healthy, healthy[1:]))


def test_card223_lone_failure_never_parks_without_acks(local):
    _, state, _ = local
    ops = seeded(local, ["p"])
    now, posts, hub = [NOW], [], Hub()
    active = active_session(local, now, poison_transport(now, posts, hub))
    for step in range(0, 26 * 3600 + 1, 60):
        now[0] = NOW + step
        active.cycle()
    expected = [0, 60, 180, 420, 900, 1860] + list(range(3660, 26 * 3600 + 1, 1800))
    assert attempt_times(posts, "p") == expected  # RED
    assert state.outbox_all()[0].state == "pending"
    assert state.next_due(op_key(active, ops["p"]) + ":strikes") == 0


def test_card223_park_survives_restart_and_rearms_after_24h(local):
    config, state, root = local
    ops = seeded(local, ["a-h", "p"])
    now, posts, hub, observed = [NOW], [], Hub(interval=900), []
    wrapped = poison_transport(now, posts, hub, status=400)

    def transport(target, token, method, route, body=None):
        if method == "POST" and body["thread_id"] == "p":
            observed.append(
                next(row["status"] for row in state.statuses() if row["native_id"] == "p")
            )
        return wrapped(target, token, method, route, body)

    parked_at = []
    rearm = []
    for step in range(0, 178201, 900):
        now[0] = NOW + step
        if step:
            with Path(ops["a-h"].path).open("ab") as stream:
                stream.write(record("a-h", str(step)))
        sync_daemon.run(
            config,
            state,
            clock=lambda: now[0],
            sleep=lambda _: None,
            transport=transport,
            once=True,
        )
        p = next(op for op in state.outbox_all() if op.native_id == "p")
        if p.state == "failed":
            parked_at.append(step)
        if step == 91800:
            active = active_session(local, now, hub)
            rearm.append(state.next_due(op_key(active, ops["p"])))
    expected = [0, 900, 1800, 2700, 3600, 5400] + list(range(7200, 91801, 1800)) + [178200]
    assert attempt_times(posts, "p") == expected and parked_at[0] == 91800  # RED
    assert len(expected) == 55 and rearm == [NOW + 178200]
    assert observed[-1] == "pending"
    assert p.state == "failed"
    assert state.next_due(op_key(active, ops["p"]) + ":strikes") == 54


def test_card223_t03_read_error_parks_unreadable(local, monkeypatch):
    _, state, root = local
    ops = seeded(local, ["a-h", "p"])
    now, posts, reads, observed, hub = [NOW], [], [], [], Hub()
    actual_open = sync_sources._open
    actual_body = sync_sources.body
    reading_body = [False]

    def body(op):
        reading_body[0] = True
        try:
            return actual_body(op)
        finally:
            reading_body[0] = False

    @contextmanager
    def unavailable(path):
        if reading_body[0] and Path(path) == Path(ops["p"].path):
            reads.append(now[0] - NOW)
            observed.append(
                next(row["status"] for row in state.statuses() if row["native_id"] == "p")
            )
            raise OSError("Synthetic missing file")
        with actual_open(path) as stream:
            yield stream

    monkeypatch.setattr(sync_sources, "body", body)
    monkeypatch.setattr(sync_sources, "_open", unavailable)
    active = active_session(local, now, poison_transport(now, posts, hub))
    parks = []
    for step in range(0, 174661, 60):
        now[0] = NOW + step
        if step:
            with Path(ops["a-h"].path).open("ab") as stream:
                stream.write(record("a-h", str(step)))
        active.cycle()
        if next(op for op in state.outbox_all() if op.native_id == "p").state == "unreadable":
            parks.append(step)
    expected = [0, 60, 180, 420, 900, 1860] + list(range(3660, 88261, 1800)) + [174660]
    assert reads == expected and parks[0] == 88260  # RED
    assert len(reads) == 55 and not attempt_times(posts, "p")
    assert observed[-1] == "pending"
    assert not state.conn.execute("SELECT 1 FROM cadence WHERE key LIKE '%:sceil:%'").fetchall()


@pytest.mark.parametrize(
    "direction",
    ["forward", "backward"],
    ids=["clock_forward_step_does_not_park", "clock_backward_step_waits_at_most_3600"],
)
def test_card223_clock_steps(local, direction):
    _, state, _ = local
    ops = seeded(local, ["p"])
    now, posts, hub = [NOW], [], Hub()
    active = active_session(
        local, now, poison_transport(now, posts, hub, status=400 if direction == "forward" else 500)
    )
    active.cycle()
    if direction == "forward":
        now[0] += 25 * 3600
        active.cycle()
        assert attempt_times(posts, "p") == [0, 0, 90000, 90000]  # RED
    else:
        now[0] -= 10 * 3600
        for step in (0, 3599, 3600):
            now[0] = NOW - 36000 + step
            active.cycle()
        assert attempt_times(posts, "p") == [0, -32400]  # RED
    assert state.outbox_all()[0].state == "pending"
    assert state.next_due(op_key(active, ops["p"]) + ":strikes") == 0


def test_card223_e2_persisted_far_deadline_is_rewritten(local):
    _, state, _ = local
    ops = seeded(local, ["p"])
    active = active_session(local, [NOW], Hub())
    keys = [token_key(active), active.key + ":writes:" + TOKEN_ENV["claude-code"] + ":0"]
    for key in keys:
        state.defer(key, NOW + 10000000)
    active.cycle()
    active._budget(TOKEN_ENV["claude-code"])
    assert all(state.next_due(key) == NOW + 3600 for key in keys)  # RED
    assert state.outbox_all()[0].op_id == ops["p"].op_id


def test_card223_status_shows_parks(local, monkeypatch, capsys):
    from commonplace import sync_cli

    config, state, root = local
    ops = seeded(local, ["failed", "unreadable", "rejected", "conflict", "revoked", "pending"])
    for sid, op in ops.items():
        state.conn.execute("UPDATE outbox SET state=? WHERE op_id=?", (sid, op.op_id))
    directory = root.parent / "status"
    directory.mkdir()
    (directory / "state.db").touch()
    monkeypatch.setattr(sync_cli, "_load", lambda *args: config)
    from commonplace import sync_state

    monkeypatch.setattr(sync_state, "open_state", lambda *args: state)
    # Leave the fixture's connection open after the status context manager.
    monkeypatch.setattr(state, "close", lambda: None)
    assert sync_cli._status(directory, root) == 0
    output = capsys.readouterr().out
    assert (  # RED
        "Parked operations: failed=1 unreadable=1 rejected=1 conflict=1 revoked=1" in output
    )
    assert str(root) not in output and "synthetic" not in output
    assert "pending=1" not in output


def test_card223_schema_unchanged(local):
    _, state, _ = local
    rows = [
        tuple(row)
        for row in state.conn.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
        )
    ]
    assert (
        sha(json.dumps(rows).encode())
        == "6a161fe89816dec009f1fc25096f7c8fe848cd58d442ddac4647b120214fecff"
    )


@pytest.mark.parametrize("phase", ["connect", "send", "read", "json", "oversize"])
def test_card223_request_connect_vs_sent_split(monkeypatch, phase):
    import http.client
    from types import SimpleNamespace
    from urllib.parse import urlsplit

    def connect():
        if phase == "connect":
            raise OSError("Synthetic connection refusal")

    def send(*args):
        if phase == "send":
            raise ConnectionResetError("Synthetic send reset")

    def read(limit):
        if phase == "read":
            raise TimeoutError("Synthetic read timeout")
        return b"x" * limit if phase == "oversize" else b"not json" if phase == "json" else b"{}"

    closed = []
    response = SimpleNamespace(status=200, read=read, getheader=lambda name: None)
    connection = SimpleNamespace(
        connect=connect,
        request=send,
        getresponse=lambda: response,
        close=lambda: closed.append(True),
    )
    monkeypatch.setattr(
        sync_daemon.http.client, "HTTPSConnection", lambda *args, **kwargs: connection
    )
    error = None
    try:
        sync_daemon.request(
            urlsplit("https://sync.example"), "synthetic", "POST", "/api/capture", {}
        )
    except (OSError, http.client.HTTPException, ValueError) as exc:
        error = type(exc).__name__
    assert error == ("OSError" if phase == "connect" else "SentError")  # RED
    assert closed == [True]


def test_card223_post_500_head_does_not_block_healthy(local):
    _, state, _ = local
    ops = seeded(local, ["p", "z"])
    now, posts, hub = [NOW], [], Hub()
    active = active_session(local, now, poison_transport(now, posts, hub))
    active.cycle()
    now[0] += 60
    active.cycle()
    assert posts == [(0, "p"), (60, "p"), (60, "z")]  # RED
    assert hub.stored["z"] == record("z")
    assert state.next_due(op_key(active, ops["p"]) + ":strikes") == 0


def test_card223_get_404_and_post_500_do_not_block_healthy(local):
    _, state, root = local
    seeded(local, ["z"])
    (root / "unknown.jsonl").write_bytes(record("p"))
    now, gets, hub = [NOW], [], Hub()

    def transport(target, token, method, route, body=None):
        if method == "GET" and "thread_id=p" in route:
            gets.append(now[0] - NOW)
            return 404, {}
        if method == "POST" and body["thread_id"] == "p":
            return 500, {}
        return hub(target, token, method, route, body)

    active = active_session(local, now, transport)
    acked_at = []
    for step in range(0, 601, 60):
        now[0] = NOW + step
        active.cycle()
        if "z" in hub.stored and not acked_at:
            acked_at.append(step)
    assert (gets, acked_at) == ([0, 60, 180, 420], [0])  # RED
    assert any(row["native_id"] == "p" and row["status"] == "revoked" for row in state.statuses())
