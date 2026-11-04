"""Daemon orchestration with synthetic sources and an injected HTTP transport."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from commonplace import cli, plans, sync_daemon
from commonplace.sync_protocol import TOKEN_ENV

EMPTY = hashlib.sha256(b"").hexdigest()
TOKEN = "synthetic-credential"


@dataclass(frozen=True)
class Op:
    op_id: str
    kind: str
    source: str
    path: str
    native_id: str
    offset: int
    length: int


class State:
    """Small durable cursor fake; orchestration uses the public State API only."""

    def __init__(self, path):
        self.conn = sqlite3.connect(path)
        self.conn.execute("CREATE TABLE cursors(source,path,native_id,offset,prefix_sha256)")
        self.pending, self.feeds, self.due, self.status = [], {}, {}, {}
        self.acked, self.attempts, self.marked = [], [], {}

    def cursor(self, source, path, native_id):
        row = self.conn.execute(
            "SELECT offset,prefix_sha256 FROM cursors WHERE source=? AND path=? AND native_id=?",
            (source, str(path), native_id),
        ).fetchone()
        return dict(zip(("offset", "prefix_sha256"), row)) if row else None

    def store_cursor(self, source, path, native_id, offset, prefix_sha256):
        self.conn.execute(
            "DELETE FROM cursors WHERE source=? AND path=? AND native_id=?",
            (source, str(path), native_id),
        )
        self.conn.execute(
            "INSERT INTO cursors VALUES (?,?,?,?,?)",
            (source, str(path), native_id, offset, prefix_sha256),
        )
        self.conn.commit()

    def set_status(self, source, path, native_id, status):
        self.status[source, str(path), native_id] = status

    def retry_rejected(self):
        self.marked = {op: status for op, status in self.marked.items() if status != "revoked"}

    def outbox_pending(self):
        return [op for op in self.pending if op.op_id not in self.marked]

    def outbox_attempt(self, op_id):
        self.attempts.append(op_id)

    def outbox_mark(self, op_id, status):
        self.marked[op_id] = status

    def outbox_ack(self, op_id, response):
        op = next(op for op in self.pending if op.op_id == op_id)
        if op.kind == "capture":
            self.store_cursor(
                op.source, op.path, op.native_id, response["offset"], response["prefix_sha256"]
            )
        self.pending.remove(op)
        self.acked.append(op)

    def feed_cursor(self, key):
        return self.feeds.get(key)

    def store_feed(self, key, envelope, *, fetched_at):
        self.feeds[key] = {
            "seq": envelope["seq"],
            "state_json": json.dumps(envelope["state"]),
            "fetched_at": fetched_at,
        }

    def next_due(self, key):
        return self.due.get(key, 0)

    def defer(self, key, until):
        self.due[key] = until


class Sources:
    class SliceConflict(ValueError):
        pass

    def __init__(self, root):
        self.path = root / "session.jsonl"
        self.path.write_bytes(b"first\nsecond\n")
        self.broken = False
        self.memory = False
        self.bodies = {}
        self.targets = []

    def discover(self, config):
        return [
            SimpleNamespace(
                source="claude-code",
                path=str(self.path),
                native_id="session",
                size=self.path.stat().st_size,
            )
        ]

    def snapshot_targets(self, config):
        return {str(self.path): self.path.stat().st_size}

    def scan(self, config, state, *, now):
        self.targets.append(config["targets"])
        current = state.cursor("claude-code", self.path, "session")
        if current and not state.outbox_pending() and not state.marked:
            target = config["targets"][str(self.path)]
            if current["offset"] < target:
                op = Op(
                    "a" * 32,
                    "capture",
                    "claude-code",
                    str(self.path),
                    "session",
                    current["offset"],
                    target - current["offset"],
                )
                state.pending.append(op)
                self.bodies[op.op_id] = {
                    "source": op.source,
                    "thread_id": op.native_id,
                    "offset": op.offset,
                    "prefix_sha256": current["prefix_sha256"],
                    "transcript": self.path.read_bytes()[op.offset : target].decode(),
                    "client_op_id": op.op_id,
                }
        if self.memory and not any(op.kind == "import" for op in state.pending + state.acked):
            op = Op("b" * 32, "import", "native-memory", "memory.md", "notes:file:hash", 0, 5)
            state.pending.append(op)
            self.bodies[op.op_id] = {
                "source": "native-memory",
                "threads": [{"thread_id": op.native_id, "format": "file", "transcript": "hello"}],
            }
        return state.outbox_pending()

    def body(self, op):
        if self.broken:
            raise self.SliceConflict()
        return self.bodies[op.op_id]


class Hub:
    def __init__(self, interval=0, stored=b""):
        self.interval, self.stored = interval, stored
        self.calls, self.posts = [], []
        self.reply = None
        self.feed_error = None
        self.sequence = 8
        self.waited = threading.Event()

    def __call__(self, target, token, method, route, body=None):
        self.calls.append((method, route, body))
        if route.startswith("/api/events?"):
            query = parse_qs(urlsplit(route).query)
            if query["wait"] == ["25"]:
                self.waited.set()
            if self.feed_error:
                raise self.feed_error
            return 200, {
                "feed": "sync",
                "events": [],
                "seq": self.sequence,
                "more": False,
                "reset": False,
                "waited": False,
                "state": {
                    "capture_enabled": True,
                    "interval_seconds": self.interval,
                    "next_window_at": None,
                },
            }
        if method == "GET":
            return 200, {
                "offset": len(self.stored),
                "prefix_sha256": hashlib.sha256(self.stored).hexdigest(),
            }
        self.posts.append((route, body))
        if self.reply is not None:
            if isinstance(self.reply, Exception):
                raise self.reply
            return self.reply
        if route == "/api/import":
            return 200, {"accepted": 1, "errors": 0}
        if (
            body["offset"] != len(self.stored)
            or body["prefix_sha256"] != hashlib.sha256(self.stored).hexdigest()
        ):
            return 409, {"code": "prefix_conflict"}
        self.stored += body["transcript"].encode()
        return 200, {
            "offset": len(self.stored),
            "prefix_sha256": hashlib.sha256(self.stored).hexdigest(),
        }


@pytest.fixture
def setup(tmp_path, monkeypatch):
    sources = Sources(tmp_path)
    monkeypatch.setitem(sys.modules, "commonplace.sync_sources", sources)
    import commonplace

    monkeypatch.setattr(commonplace, "sync_sources", sources, raising=False)
    state = State(tmp_path / "state.db")
    config = {
        "hub": "https://sync.example",
        "tokens": {TOKEN_ENV["claude-code"]: TOKEN, TOKEN_ENV["native-memory"]: TOKEN},
        "sources": [],
        "memories": [],
    }
    now = [datetime(2026, 11, 4, tzinfo=timezone.utc).timestamp()]
    monkeypatch.setattr(plans, "_now", lambda: datetime.fromtimestamp(now[0], timezone.utc))
    yield sources, state, config, now
    state.conn.close()


def once(setup, hub):
    _, state, config, _ = setup
    return sync_daemon.run(
        config, state, clock=plans._now, sleep=lambda _: None, transport=hub, once=True
    )


@pytest.mark.parametrize(
    "reply,code,status",
    [
        (None, 0, None),
        ((429, {"error": "Capture not due; retry in 840 seconds"}), 0, None),
        ((429, {"error": "Import not due; retry in 900 seconds"}), 0, None),
        ((429, {"error": "Rate limited"}), 0, None),
        ((409, {"error": "Workspace capture is off"}), 0, "paused"),
        ((409, {"code": "prefix_conflict"}), 1, "conflict"),
        ((409, {"code": "op_conflict"}), 1, "conflict"),
        ((401, {"error": "revoked"}), 1, "revoked"),
        ((500, {}), 1, None),
        ((200, {"offset": 999, "prefix_sha256": EMPTY}), 1, None),
        (ConnectionError(), 1, None),
        (TimeoutError(), 1, None),
    ],
)
def test_exit_table_and_pending_cursor(setup, reply, code, status):
    sources, state, config, now = setup
    hub = Hub()
    hub.reply = reply
    assert once(setup, hub) == code
    if reply is None:
        assert not state.outbox_pending()
        assert (
            state.cursor("claude-code", sources.path, "session")["offset"]
            == sources.path.stat().st_size
        )
    else:
        assert state.cursor("claude-code", sources.path, "session")["offset"] == 0
        assert len(state.pending) == 1
    if status:
        assert state.status["claude-code", str(sources.path), "session"] == status
    if reply and not isinstance(reply, Exception) and reply[0] == 429:
        delay = 840 if "840" in reply[1]["error"] else 900 if "900" in reply[1]["error"] else 60
        assert (
            state.next_due(sync_daemon._feed_key(config["hub"], TOKEN_ENV["claude-code"]))
            == now[0] + delay
        )


def test_fresh_state_database_adopts_grown_server_cursor_before_push(setup):
    sources, state, _, _ = setup
    hub = Hub(stored=b"first\n")
    assert state.cursor("claude-code", sources.path, "session") is None
    assert once(setup, hub) == 0
    assert hub.stored == sources.path.read_bytes()
    assert hub.posts[0][1]["offset"] == len(b"first\n")
    assert hub.posts[0][1]["transcript"] == "second\n"
    capture_calls = [
        (method, route) for method, route, _ in hub.calls if route.startswith("/api/capture")
    ]
    assert capture_calls[0][0] == "GET" and capture_calls[1] == ("POST", "/api/capture")
    assert state.cursor("claude-code", sources.path, "session")["offset"] == len(hub.stored)


def test_capture_precedes_memory_and_cycle_end_is_frozen(setup):
    sources, _, _, _ = setup
    sources.memory = True
    hub = Hub()
    assert once(setup, hub) == 0
    assert [route for route, _ in hub.posts] == ["/api/capture", "/api/import"]
    assert all(target == sources.targets[0] for target in sources.targets)


def test_slice_conflict_is_terminal_without_send_or_cursor_reset(setup):
    sources, state, _, _ = setup
    sources.broken = True
    hub = Hub()
    assert once(setup, hub) == 1
    assert not hub.posts
    assert state.marked == {"a" * 32: "conflict"}
    assert state.cursor("claude-code", sources.path, "session")["offset"] == 0


@pytest.mark.parametrize("interval", [0, 900])
def test_once_never_long_polls_and_ignores_next_due(setup, interval):
    _, state, config, now = setup
    state.defer(sync_daemon._feed_key(config["hub"], TOKEN_ENV["claude-code"]), now[0] + 900)
    hub = Hub(interval)
    assert once(setup, hub) == 0
    assert hub.posts
    assert all(
        parse_qs(urlsplit(route).query)["wait"] == ["0"]
        for _, route, _ in hub.calls
        if route.startswith("/api/events?")
    )


def test_unknown_account_once_uses_nonblocking_feed(setup):
    hub = Hub(900)
    assert once(setup, hub) == 0
    assert "wait=0" in hub.calls[0][1]


def test_121st_write_waits_for_oldest_slot_then_recovers(setup):
    _, state, config, now = setup
    session = sync_daemon._Session(config, state, plans._now, Hub())
    assert all(session._budget(TOKEN_ENV["claude-code"]) for _ in range(120))
    # A new process cannot evade the budget through an empty in-memory bucket.
    session = sync_daemon._Session(config, state, plans._now, Hub())
    assert not session._budget(TOKEN_ENV["claude-code"])
    assert state.next_due(session.key) == now[0] + 3600
    assert not state.feeds
    now[0] += 3600
    assert session._budget(TOKEN_ENV["claude-code"])


def test_reconnect_backoff_caps_and_resumes_cached_cursor(setup):
    _, state, config, _ = setup
    hub = Hub()
    hub.feed_error = ConnectionError()
    key = sync_daemon._feed_key(config["hub"], TOKEN_ENV["claude-code"])
    state.store_feed(
        key, {"seq": 42, "state": {"interval_seconds": 0, "capture_enabled": True}}, fetched_at=0
    )
    sleeps = []
    assert (
        sync_daemon.run(
            config,
            state,
            clock=plans._now,
            sleep=sleeps.append,
            transport=hub,
            stop=lambda: len(sleeps) == 8,
        )
        == 0
    )
    assert sleeps == [1, 2, 4, 8, 16, 32, 60, 60]
    assert all(
        parse_qs(urlsplit(route).query)["after"] == ["42"]
        for _, route, _ in hub.calls
        if route.startswith("/api/events?")
    )


@pytest.mark.parametrize("interval", [0, 900])
def test_continuous_long_polls_only_at_interval_zero(setup, interval):
    _, state, config, _ = setup
    hub = Hub(interval)
    ticks = []

    def sleep(seconds):
        ticks.append(seconds)
        if interval == 0:
            assert hub.waited.wait(2)

    assert (
        sync_daemon.run(
            config,
            state,
            clock=plans._now,
            sleep=sleep,
            transport=hub,
            stop=lambda: len(ticks) == 2,
        )
        == 0
    )
    waits = [
        int(parse_qs(urlsplit(route).query)["wait"][0])
        for _, route, _ in hub.calls
        if route.startswith("/api/events?")
    ]
    assert (25 in waits) == (interval == 0)
    assert all(wait == 0 for wait in waits) if interval else all(tick == 2 for tick in ticks)


def test_install_inert_then_private_configuration(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    before = list(home.rglob("*"))
    assert cli.main(["sync", "install", "--source", "claude-code"]) == 0
    assert list(home.rglob("*")) == before
    assert "--enable" in capsys.readouterr().out
    env = tmp_path / "client.env"
    env.write_text(
        "COMMONPLACE_URL=https://sync.example\nCOMMONPLACE_CAPTURE_TOKEN_CLAUDE_CODE="
        + TOKEN
        + "\n"
    )
    env.chmod(0o600)
    assert (
        cli.main(
            [
                "sync",
                "install",
                "--source",
                "claude-code",
                "--enable",
                "--no-service",
                "--env-file",
                str(env),
            ]
        )
        == 0
    )
    directory = home / ".commonplace/sync"
    config = directory / "config.json"
    assert directory.stat().st_mode & 0o777 == 0o700
    assert config.stat().st_mode & 0o777 == 0o600
    assert TOKEN not in config.read_text()


def test_bad_environment_mode_and_cursor_install_write_nothing(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    env = tmp_path / "client.env"
    env.write_text("COMMONPLACE_URL=https://sync.example\n")
    env.chmod(0o644)
    assert (
        cli.main(
            [
                "sync",
                "install",
                "--source",
                "claude-code",
                "--enable",
                "--no-service",
                "--env-file",
                str(env),
            ]
        )
        == 2
    )
    assert not list(home.rglob("*"))
    assert cli.main(["sync", "install", "--source", "cursor", "--enable", "--no-service"]) == 1
    assert not list(home.rglob("*"))
    assert "unsupported" in capsys.readouterr().out


def test_run_bad_config_exits_two_without_server_database(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    # Reject missing or invalid configuration before opening any client state.
    assert cli.main(["sync", "run", "--once"]) == 2
    assert (
        cli.main(["sync", "install", "--source", "claude-code", "--hub", "http://remote.example"])
        == 2
    )
    assert not (tmp_path / ".commonplace").exists()


def test_unacknowledged_suffix_replays_before_new_growth(setup):
    sources, state, _, _ = setup
    hub = Hub()
    hub.reply = ConnectionError()
    assert once(setup, hub) == 1
    original = hub.posts[0][1].copy()
    sources.path.write_bytes(sources.path.read_bytes() + b"third\n")
    hub.reply = None
    assert once(setup, hub) == 0
    assert hub.posts[1][1] == original
    assert hub.posts[2][1]["transcript"] == "third\n"
    assert hub.stored == sources.path.read_bytes()
    assert not state.outbox_pending()


def test_cycle_ignores_growth_after_targets_were_frozen(setup):
    sources, _, _, _ = setup
    hub = Hub()

    def transport(*args):
        if args[2] == "GET" and args[3].startswith("/api/capture?"):
            sources.path.write_bytes(sources.path.read_bytes() + b"later\n")
        return hub(*args)

    _, state, config, _ = setup
    assert (
        sync_daemon.run(
            config, state, clock=plans._now, sleep=lambda _: None, transport=transport, once=True
        )
        == 0
    )
    assert hub.stored == b"first\nsecond\n"
    assert once(setup, hub) == 0
    assert hub.stored == b"first\nsecond\nlater\n"


def test_rollback_feed_and_client_op_fallback_keep_capture_working(setup):
    _, state, config, _ = setup
    hub = Hub()
    post_bodies = []

    def transport(target, token, method, route, body=None):
        if route.startswith("/api/events?"):
            return 403, {}
        if route == "/api/settings/capture":
            return 200, {"enabled": True, "interval_seconds": 0}
        if method == "POST":
            post_bodies.append(body)
            if "client_op_id" in body:
                return 400, {}
        return hub(target, token, method, route, body)

    assert (
        sync_daemon.run(
            config, state, clock=plans._now, sleep=lambda _: None, transport=transport, once=True
        )
        == 0
    )
    assert len(post_bodies) == 2
    assert post_bodies[1] == {k: v for k, v in post_bodies[0].items() if k != "client_op_id"}
    assert not state.outbox_pending()


def test_private_config_mode_is_checked_before_opening_state(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert cli.main(["sync", "install", "--source", "claude-code", "--enable", "--no-service"]) == 0
    config = tmp_path / ".commonplace/sync/config.json"
    config.chmod(0o644)
    assert cli.main(["sync", "run", "--once"]) == 2
    assert not (config.parent / "state.db").exists()


def test_missing_selected_token_is_a_configuration_error(setup):
    _, state, config, _ = setup
    config["tokens"] = {TOKEN_ENV["native-memory"]: TOKEN}
    with pytest.raises(sync_daemon.ConfigError):
        once(setup, Hub())
    assert not state.outbox_pending()


def test_memory_only_install_does_not_select_unrequested_transcripts(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert cli.main(["sync", "install", "--memory", "notes=notes", "--enable", "--no-service"]) == 0
    config = json.loads((tmp_path / ".commonplace/sync/config.json").read_text())
    assert config["sources"] == []
    assert config["memories"][0]["label"] == "notes"
