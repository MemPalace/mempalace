"""Gateway startup composition and worker I/O share bounded, private context."""

import copy
import json
import socket
import sys
from time import perf_counter
from types import SimpleNamespace

import pytest

from commonplace import extensions
from commonplace.gateway import WORKER_TIMEOUT_S, WorkerClient, WorkerUnavailable
from commonplace.rules import checks, render
from tests.test_commonplace_rules_inject import LEGACY, REQUEST, initialize, local as local

STARTUP = (
    "Commonplace is the single source for rules and memory. "
    "Automatically call session_start at start/resume/compaction and when context is incomplete. "
    "Use memory_save to remember and rules_publish for new rules."
)


@pytest.fixture
def session_module(local, monkeypatch):
    calls = []

    def context(ctx, *, budget, query=None):
        calls.append((ctx, budget, query))
        return STARTUP

    module = SimpleNamespace(initialize_context=context, calls=calls)
    monkeypatch.setitem(sys.modules, "commonplace.session", module)
    # The parallel session service registers this tool in the integrated tree.
    monkeypatch.setitem(
        extensions.REGISTRY,
        "session_start",
        extensions.LocalTool("session_start", "Read startup context", {}, context, "memory:read"),
    )
    return module


def test_empty_startup_preserves_worker_and_principal_without_creating_stores(
    local, session_module
):
    local.worker.response["result"]["instructions"] = "Worker instructions: café."
    original = copy.deepcopy(local.worker.response)
    response = initialize(local)
    assert response["result"]["instructions"] == STARTUP + "\n\nWorker instructions: café."
    assert local.worker.response == original
    ctx, budget, query = session_module.calls[0]
    assert ctx.principal is local.rules.principal
    assert ctx.gateway is local.gateway and query is None
    assert 0 < budget < render.INSTRUCTIONS_BUDGET
    assert not (local.directory / "rules.sqlite3").exists()
    assert not (local.directory / "boards.sqlite3").exists()
    assert local.db.rule_stores(local.workspace) is None


def test_rules_keep_framing_priority_and_do_not_serve_enforcement(local, session_module):
    local.owner.create("*", "Keep this exact rule.\nSecond line.", "limit")
    local.owner.create("conn:other", "foreign rule", "limit")
    local.rules.rules_publish([dict(text="unaccepted proposal", kind="limit")])
    local.owner.set_check("*", "enforce")
    local.worker.response["result"]["instructions"] = "Worker instructions."
    before = (local.directory / "rules.sqlite3").read_bytes()
    text = initialize(local)["result"]["instructions"]
    assert text.startswith("Commonplace rules for conn:builder.\n")
    assert "The workspace owner set or accepted these rules." in text
    assert "Each rule is one JSON string" in text
    assert '- "Keep this exact rule.\\nSecond line."' in text
    assert text.index("Keep this exact rule.") < text.index(STARTUP)
    assert text.endswith("\n\nWorker instructions.")
    assert "foreign rule" not in text and "unaccepted proposal" not in text
    assert "Before your first Commonplace write, call rules_get" in text
    assert (local.directory / "rules.sqlite3").read_bytes() == before
    with checks.connection(local.directory / "rules.sqlite3") as conn:
        assert conn.execute("SELECT count(*) FROM agent_served").fetchone()[0] == 0


def test_whole_rules_take_budget_before_optional_memory(local, session_module):
    for index in range(30):
        local.owner.create("*", f"rule {index}: " + "é" * 65, "instruction")
    offered = []
    memory = 'quoted data: "' + "large memory " * 250 + '"'

    def context(_ctx, *, budget):
        offered.append(budget)
        return STARTUP + "\n" + memory

    session_module.initialize_context = context
    local.worker.response["result"]["instructions"] = "Original worker guidance."
    text = initialize(local)["result"]["instructions"]
    assert "This list is incomplete" in text
    assert "Automatically call session_start for full context." in text
    assert "quoted data:" not in text and "large memory" not in text
    assert text.endswith("\n\nOriginal worker guidance.")
    assert render.length(text) <= render.INSTRUCTIONS_BUDGET
    assert offered[0] < 600
    for line in text.splitlines():
        if line.startswith('- "'):
            assert json.loads(line[2:]).endswith("é" * 65)


@pytest.mark.parametrize("failure", [TimeoutError, WorkerUnavailable, ValueError])
def test_optional_context_failure_keeps_rules_and_automatic_followup(
    local, session_module, failure
):
    local.owner.create("*", "Keep accepted guidance.", "limit")

    def unavailable(_ctx, *, budget):
        assert 0 < checks.remaining(WORKER_TIMEOUT_S) <= 0.08
        raise failure("unavailable private content")

    session_module.initialize_context = unavailable
    started = perf_counter()
    text = initialize(local)["result"]["instructions"]
    assert perf_counter() - started < 0.1
    assert "Keep accepted guidance." in text
    assert "Commonplace is the single source for rules and memory." in text
    assert "Automatically call session_start for full context." in text
    assert "unavailable private content" not in text
    assert checks.remaining(WORKER_TIMEOUT_S) == WORKER_TIMEOUT_S


def test_rules_and_memory_share_one_deadline_and_late_memory_is_discarded(
    local, session_module, monkeypatch
):
    local.owner.create("*", "Accepted rule.", "limit")
    now = [0.0]
    monkeypatch.setattr(checks, "monotonic", lambda: now[0])
    original = render.instructions

    def costly_rules(*args, **kwargs):
        text = original(*args, **kwargs)
        now[0] += 0.06
        return text

    def late_memory(_ctx, *, budget):
        assert checks.remaining(WORKER_TIMEOUT_S) == pytest.approx(0.02)
        with checks.deadline(0.08):
            assert checks.remaining(WORKER_TIMEOUT_S) == pytest.approx(0.02)
            now[0] += 0.021
            return STARTUP + '\nquoted data: "late memory"'

    monkeypatch.setattr(render, "instructions", costly_rules)
    session_module.initialize_context = late_memory
    text = initialize(local)["result"]["instructions"]
    assert "Accepted rule." in text and "late memory" not in text
    assert "Automatically call session_start for full context." in text


@pytest.mark.parametrize(
    "policy,expected,absent",
    [
        (dict(tools=["memory_search"]), "call memory_search", "call session_start"),
        (dict(scopes=["memory:write"]), "no context read tools", "call session_start"),
    ],
)
def test_fallback_respects_exact_tool_allowlist_and_scopes(
    local, session_module, monkeypatch, policy, expected, absent
):
    agent = local.agent("restricted", **policy)
    monkeypatch.setattr(local.gateway, "workspace_dir", lambda _: pytest.fail("workspace opened"))

    def unavailable(ctx, *, budget):
        assert ctx.principal is agent.principal
        assert ctx.principal.policy is agent.principal.policy
        raise WorkerUnavailable()

    session_module.initialize_context = unavailable
    request = copy.deepcopy(REQUEST)
    request["params"] = {"_meta": {"query": "topic", "read_wings": ["*"], "private:read": True}}
    text = local.gateway.mediator.handle(agent.principal, request)["result"]["instructions"]
    assert expected in text and absent not in text
    assert "topic" not in text and "private:read" not in text


def test_person_malformed_and_budget_exhausted_worker_are_unchanged(local, session_module):
    assert initialize(local, local.owner) == LEGACY
    for previous in (None, {"bad": "instructions"}, "é" * 1800):
        local.worker.response["result"]["instructions"] = previous
        assert initialize(local) == local.worker.response
    assert session_module.calls == []


@pytest.fixture
def http_worker(local):
    return SimpleNamespace(host="127.0.0.1", port=1, host_header="127.0.0.1:1", token="worker")


def test_http_deadline_rearms_each_read_and_restores_normal_timeout(
    local, http_worker, monkeypatch
):
    now, timeouts, connections = [0.0], [], []
    monkeypatch.setattr(checks, "monotonic", lambda: now[0])

    class Response:
        status = 200

        def __init__(self):
            self.parts = iter([b'{"result":', b"{}}", b""])
            self.closed = False

        def isclosed(self):
            return self.closed

        def read1(self, _size):
            now[0] += 0.01
            chunk = next(self.parts)
            self.closed = not chunk
            return chunk

        def close(self):
            self.closed = True

    class Connection:
        def __init__(self, host, port, timeout):
            self.timeout = timeout
            self.sock = SimpleNamespace(settimeout=timeouts.append)
            self.closed = False
            connections.append(self)

        def request(self, *_args, **_kwargs):
            now[0] += 0.01

        def getresponse(self):
            now[0] += 0.01
            self.sock = None  # HTTPConnection detaches Connection: close sockets.
            return Response()

        def close(self):
            self.closed = True

    monkeypatch.setattr("commonplace.gateway.http.client.HTTPConnection", Connection)
    client = WorkerClient(None)
    with checks.deadline(0.08):
        assert client._post(http_worker, local.rules.principal, REQUEST) == {"result": {}}
    assert connections[0].timeout == pytest.approx(0.08)
    assert timeouts == pytest.approx([0.07, 0.06, 0.05, 0.04])
    assert connections[0].closed
    timeouts.clear()
    assert client._post(http_worker, local.rules.principal, REQUEST) == {"result": {}}
    assert connections[1].timeout == WORKER_TIMEOUT_S
    assert timeouts == [WORKER_TIMEOUT_S] * 4


@pytest.mark.timing
def test_stalled_worker_read_fails_open_within_startup_budget(local, session_module, monkeypatch):
    # A peer that receives requests but sends no HTTP response exercises the
    # real socket timeout without a sleeping server or an orphaned thread.
    left, right = socket.socketpair()
    client = WorkerClient(None)

    def connect(conn):
        conn.sock = left
        left.settimeout(conn.timeout)

    def context(ctx, *, budget):
        with checks.deadline(0.04):
            client._post(
                SimpleNamespace(host="127.0.0.1", port=1, host_header="127.0.0.1:1", token="test"),
                ctx.principal,
                REQUEST,
            )
        return "unreachable"

    session_module.initialize_context = context
    monkeypatch.setattr("commonplace.gateway.http.client.HTTPConnection.connect", connect)
    try:
        started = perf_counter()
        text = initialize(local)["result"]["instructions"]
        assert perf_counter() - started < 0.1
        assert "Automatically call session_start for full context." in text
        assert "unreachable" not in text
        assert left.fileno() == -1
    finally:
        left.close()
        right.close()


@pytest.mark.parametrize("phase", ["headers", "body"])
def test_real_socket_timeout_survives_a_frozen_logical_clock(
    local, session_module, monkeypatch, phase
):
    left, right = socket.socketpair()
    client = WorkerClient(None)
    failures = []
    monkeypatch.setattr(checks, "monotonic", lambda: 0.0)

    def connect(conn):
        conn.sock = left
        left.settimeout(conn.timeout)

    def context(ctx, *, budget):
        with checks.deadline(0.04):
            try:
                client._post(
                    SimpleNamespace(
                        host="127.0.0.1", port=1, host_header="127.0.0.1:1", token="test"
                    ),
                    ctx.principal,
                    REQUEST,
                )
            except WorkerUnavailable as error:
                failures.append(error)
                raise
        return "unreachable"

    session_module.initialize_context = context
    monkeypatch.setattr("commonplace.gateway.http.client.HTTPConnection.connect", connect)
    if phase == "body":
        right.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\nConnection: close\r\n\r\n")
    try:
        text = initialize(local)["result"]["instructions"]
        assert len(failures) == 1
        assert isinstance(failures[0].__cause__, TimeoutError)
        assert "Automatically call session_start for full context." in text
        assert "unreachable" not in text
        assert left.fileno() == -1
    finally:
        left.close()
        right.close()


def test_expired_worker_deadline_refuses_before_opening_connection(local, http_worker, monkeypatch):
    def unexpected_connection(*args, **kwargs):
        pytest.fail("Expired request must not open a connection")

    monkeypatch.setattr("commonplace.gateway.http.client.HTTPConnection", unexpected_connection)
    token = checks._deadline.set(0.0)
    try:
        with pytest.raises(WorkerUnavailable):
            WorkerClient(None)._post(http_worker, local.rules.principal, REQUEST)
    finally:
        checks._deadline.reset(token)


def test_real_session_composes_on_fresh_workspace_without_creating_stores(local, monkeypatch):
    import importlib

    module = importlib.import_module("commonplace.session")
    original = module.initialize_context
    calls = []

    def observe(ctx, **kwargs):
        calls.append(ctx.principal)
        return original(ctx, **kwargs)

    monkeypatch.setattr(module, "initialize_context", observe)
    text = initialize(local)["result"]["instructions"]
    assert calls == [local.rules.principal]
    assert "Commonplace is the single source" in text and "session_start" in text
    assert render.length(text) <= render.INSTRUCTIONS_BUDGET
    assert not (local.directory / "rules.sqlite3").exists()
    assert not (local.directory / "boards.sqlite3").exists()
