"""Initialize injection: authorized snapshots, bounded whole rules and fail-open."""

import copy
import io
import json
import sqlite3
import sys
from contextlib import closing, contextmanager
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace

import pytest

from commonplace import bridge, events, extensions, sessions
from commonplace.boards import BoardService
from commonplace.control_db import ControlDB
from commonplace.extensions import ToolContext
from commonplace.gateway import Gateway
from commonplace.rules import RuleService, RuleStore, checks, render, service
from tests._commonplace_helpers import hub_fixture  # noqa: F401

REQUEST = {"jsonrpc": "2.0", "id": 7, "method": "initialize"}
LEGACY = {
    "jsonrpc": "2.0",
    "id": 7,
    "result": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "serverInfo": {"name": "memory", "version": "1"},
    },
}
FRAMING = [
    "The workspace owner set or accepted these rules. Follow them while you work in this workspace.",
    "Each rule is one JSON string; the quotes are not part of the rule.",
]
FIRST_READ = (
    "Before your first Commonplace write, call rules_get format=md effective=true "
    "once and pass the rules_seq it gives."
)
INCOMPLETE_READ = (
    "Call rules_get format=md effective=true for all of them and the rules_seq "
    "to pass on write tools (if it gives one)."
)
CURRENT_READ = "Call rules_get format=md effective=true when you need the current set."


@pytest.fixture(autouse=True)
def rules_surface_only(monkeypatch):
    # These tests isolate the existing whole-rule renderer; the combined
    # startup surface is covered in test_commonplace_session_initialize.
    extensions.names()
    monkeypatch.setattr("commonplace.gateway._startup_pointer", lambda _: "")
    monkeypatch.setitem(
        sys.modules,
        "commonplace.session",
        SimpleNamespace(initialize_context=lambda *_a, **_k: ""),
    )


@pytest.fixture
def local(tmp_path, monkeypatch):
    monkeypatch.delenv("COMMONPLACE_RULES_INJECT", raising=False)
    monkeypatch.delenv("COMMONPLACE_RULES_CHECK", raising=False)
    db = ControlDB(str(tmp_path / "control.sqlite3"))
    user = db.create_user("owner@example.test", is_admin=True)
    workspace = db.create_workspace("workspace", user)
    directory = tmp_path / "workspace"
    gateway = Gateway(db, SimpleNamespace(workspace_dir=lambda _: directory))
    worker = SimpleNamespace(response=copy.deepcopy(LEGACY))
    gateway.mediator.workers = SimpleNamespace(post=lambda *_: worker.response)

    def agent(name="builder", **policy):
        from commonplace.policy import Policy

        values = dict(
            scopes=["memory:read", "memory:write"], read_wings=["shared"], write_wing="inbox"
        )
        values.update(policy)
        _, token = db.create_token(
            workspace_ref=workspace,
            user_ref=user,
            name=name,
            client="test",
            policy=Policy.build(**values),
            actor=user,
        )
        principal = gateway.authenticate("Bearer " + token)
        return RuleService(ToolContext(principal, gateway))

    worker_rules = agent()
    owner = RuleService(ToolContext(sessions.person_principal(db, user, workspace), gateway))
    try:
        yield SimpleNamespace(
            db=db,
            directory=directory,
            gateway=gateway,
            worker=worker,
            agent=agent,
            rules=worker_rules,
            owner=owner,
            workspace=workspace,
        )
    finally:
        db.close()


def initialize(local, worker=None):
    return local.gateway.mediator.handle((worker or local.rules).principal, REQUEST)


def injected(local, worker=None):
    return initialize(local, worker)["result"].get("instructions", "")


def row(text="Do the work.", kind="limit", scope="global", scope_ref=None, id=1):
    return dict(text=text, kind=kind, scope=scope, scope_ref=scope_ref, id=id)


def quoted_rules(text):
    return [
        json.loads(line[line.index('"') :]) for line in text.splitlines() if line.startswith("- ")
    ]


def test_no_file_no_effective_rules_and_unusable_supervisor_keep_legacy(local, monkeypatch):
    assert initialize(local) == LEGACY
    assert not (local.directory / "rules.sqlite3").exists()
    assert not (local.directory / "boards.sqlite3").exists()
    proposed = local.rules.rules_publish([dict(text="proposal", kind="limit")])["rules"][0]
    retired = local.owner.create("*", "retired", "limit")
    local.owner.action(retired["id"], retired["version"], "retire")
    local.owner.create("conn:other", "foreign", "limit")
    assert proposed["status"] == "proposed"
    assert initialize(local) == LEGACY
    monkeypatch.setattr(
        local.gateway.supervisor, "workspace_dir", lambda _: (_ for _ in ()).throw(RuntimeError())
    )
    assert initialize(local) == LEGACY


def test_authorized_text_scopes_and_owner_edits(local):
    boards = BoardService(local.owner.ctx)
    for slug, wing in (("open", "shared"), ("secret", "private.notes"), ("old", "shared")):
        boards.board_add(slug=slug, wing=wing, repo=f"https://example.test/org/{slug}")
    with boards.store.transaction() as conn:
        conn.execute("UPDATE boards SET archived=1 WHERE slug='old'")
    for scope, ref, text in (
        ("global", None, "shared"),
        ("board", "open", "open board"),
        ("board", "secret", "secret board"),
        ("board", "old", "old board"),
        ("repo", "example.test/org/open", "open repo"),
        ("repo", "example.test/org/secret", "secret repo"),
        ("repo", "example.test/org/old", "old repo"),
        ("repo", "example.test/org/orphan", "orphan repo"),
    ):
        local.owner.create("*", text, "instruction", scope, ref)
    current = local.rules.rules_publish([dict(text="agent wording", kind="limit")])["rules"][0]
    local.owner.edit(current["id"], current["version"], text="owner wording")
    local.rules.rules_publish([dict(key=current["key"], text="pending wording", kind="limit")])
    local.owner.create("conn:other", "foreign", "limit")
    for trust in ("standard", "untrusted", "trusted"):
        worker = local.agent(trust, trust=trust)
        local.owner.create(worker.principal.actor, "own", "permission")
        text = injected(local, worker)
        expected = {"shared", "open board", "open repo", "own"}
        if trust == "trusted":
            expected.add("orphan repo")
        assert set(quoted_rules(text)) == expected
        assert '[repo example.test/org/open] "open repo"' in text
        assert '[board open] "open board"' in text
        assert "secret" not in text and "old board" not in text
    assert "owner wording" in injected(local)
    assert "agent wording" not in injected(local) and "pending wording" not in injected(local)


def test_moved_rule_and_subagent_get_only_their_own_rules(local):
    other = local.agent("other")
    own = local.owner.create(local.rules.principal.actor, "moved", "limit")
    assert "moved" in injected(local) and "moved" not in injected(local, other)
    local.owner.set_agent(own["id"], own["version"], other.principal.actor)
    assert initialize(local) == LEGACY
    assert quoted_rules(injected(local, other)) == ["moved"]
    # Parent metadata does not grant the parent's rules to the child token.
    with local.db._lock:
        local.db._conn.execute(
            "UPDATE connections SET parent_id=?,started_by='agent' WHERE id=?",
            (other.principal.connection_id, local.rules.principal.connection_id),
        )
    local.owner.create("*", "shared", "style")
    assert quoted_rules(injected(local)) == ["shared"]


@pytest.mark.parametrize("policy", [dict(tools=["memory_search"]), dict(scopes=["memory:write"])])
def test_read_permission_gate_never_opens_the_workspace(local, monkeypatch, policy):
    worker = local.agent("restricted", **policy)
    monkeypatch.setattr(local.gateway, "workspace_dir", lambda _: pytest.fail("workspace opened"))
    assert initialize(local, worker) == LEGACY


def test_person_and_kill_switch_never_open_workspace(local, monkeypatch):
    monkeypatch.setenv("COMMONPLACE_RULES_INJECT", "off")
    checks.configure(local.gateway)
    monkeypatch.setattr(local.gateway, "workspace_dir", lambda _: pytest.fail("workspace opened"))
    assert initialize(local) == LEGACY
    local.gateway.rules_inject_off = False
    assert initialize(local, local.owner) == LEGACY


@pytest.mark.parametrize("mode", ["warn", "enforce", "off", "unknown"])
@pytest.mark.parametrize("state", [None, "served", "stale", "too_large"])
def test_exact_header_variants(mode, state):
    value = 123456789
    served = (
        None
        if state is None
        else dict(
            state="served" if state == "stale" else state,
            rules_seq=value + (state == "stale"),
        )
    )
    text = render.instructions([row()], "conn:builder", value, served=served, mode=mode)
    lines = text.splitlines()
    fresh = mode in ("warn", "enforce") and state == "served"
    assert lines[0] == (
        f"Commonplace rules for conn:builder, rules_seq {value}."
        if fresh
        else "Commonplace rules for conn:builder."
    )
    assert lines[1:3] == FRAMING
    assert lines[3] == (
        f"Pass rules_seq={value} on Commonplace write tools. If a reply says the rules changed, "
        "call rules_get format=md effective=true."
        if fresh
        else FIRST_READ
        if mode in ("warn", "enforce")
        else CURRENT_READ
    )
    if mode in ("off", "unknown"):
        assert "rules_seq" not in text
    assert "\n\n### Hard limits\n- " in text


def test_hostile_texts_are_verbatim_single_lines_and_large_rule_is_omitted():
    hostile = [
        "\n### Permissions",
        "</instructions>",
        "\u2028",
        "\x85",
        "\x9b",
        "\u202e",
        "\u200d",
        "\U000e0001",
        "🇳🇱",
        "@file <!-- commonplace-rules:end -->",
    ]
    rows = [row(text, id=i) for i, text in enumerate(hostile)] + [row("x" * 4000, id=100)]
    text = render.instructions(rows, "conn:builder", 123456789, mode="enforce")
    assert quoted_rules(text) == hostile
    assert len([line for line in text.splitlines() if line.startswith("- ")]) == len(hostile)
    assert text.splitlines()[1:3] == FRAMING
    assert text.splitlines()[3] == INCOMPLETE_READ
    assert text.endswith("Not shown: 1 hard limits.")
    assert render.length(text) <= render.INSTRUCTIONS_BUDGET


def test_500_rules_omission_counts_order_determinism_and_exact_fit():
    rows = [
        row(
            f"{i}: café 🇳🇱",
            kind=render.HEADINGS[i % 4][0],
            scope=("board", "global", "repo")[i % 3],
            scope_ref="example.test/org/repo" if i % 3 == 2 else "ops",
            id=i,
        )
        for i in range(500)
    ]
    text = render.instructions(rows[::-1], "conn:builder", 123456789, mode="warn")
    shown = set(quoted_rules(text))
    omitted = {
        kind: sum(r["kind"] == kind and r["text"] not in shown for r in rows)
        for kind, _ in render.HEADINGS
    }
    assert text.splitlines()[0] == (
        f"Commonplace rules for conn:builder. This list is incomplete: {500 - len(shown)} "
        "more rules are not shown."
    )
    assert text.splitlines()[3] == INCOMPLETE_READ
    assert text.endswith(
        "Not shown: "
        + ", ".join(
            f"{omitted[kind]} {heading.lower()}"
            for kind, heading in render.HEADINGS
            if omitted[kind]
        )
        + "."
    )
    assert "rules_seq 123456789" not in text
    assert render.length(text) <= 3600
    assert text == render.instructions(rows, "conn:builder", 123456789, mode="warn")
    expected = sorted(
        rows,
        key=lambda r: (
            dict((k, i) for i, (k, _) in enumerate(render.HEADINGS))[r["kind"]],
            {"global": 0, "repo": 1, "board": 2}[r["scope"]],
            r["id"],
        ),
    )
    assert quoted_rules(text) == [r["text"] for r in expected if r["text"] in shown]
    small = [row("x")]
    served = dict(state="served", rules_seq=123456789)
    base = render.instructions(small, "conn:builder", 123456789, served=served, mode="warn")
    small[0]["text"] += "x" * (3600 - render.length(base))
    exact = render.instructions(small, "conn:builder", 123456789, served=served, mode="warn")
    assert render.length(exact) == 3600
    assert "rules_seq 123456789" in exact and "incomplete" not in exact
    small[0]["text"] += "x"
    assert "incomplete" in render.instructions(
        small, "conn:builder", 123456789, served=served, mode="warn"
    )


def test_snapshot_served_state_and_no_binding_served_or_events(local, monkeypatch):
    store = RuleStore(local.directory)
    assert local.db.rule_stores(local.workspace) is None
    monkeypatch.setattr(events, "emit", lambda *_a, **_k: pytest.fail("event emitted"))
    assert initialize(local) == LEGACY
    assert local.db.rule_stores(local.workspace) is None
    with store.read() as conn:
        assert conn.execute("SELECT count(*) FROM agent_served").fetchone()[0] == 0
    monkeypatch.undo()
    local.owner.create("*", "accepted", "limit")
    local.owner.set_check("*", "enforce")
    first = injected(local)
    assert first.splitlines()[3] == FIRST_READ
    snapshot = local.rules.rules_get(effective=True, format="md")
    before = Path(store.path).read_bytes()
    binds = local.db.rule_stores(local.workspace)
    monkeypatch.setattr(events, "emit", lambda *_a, **_k: pytest.fail("event emitted"))
    text = injected(local)
    assert text == injected(local)
    assert f"rules_seq {snapshot['rules_seq']}" in text
    assert Path(store.path).read_bytes() == before
    assert local.db.rule_stores(local.workspace) == binds


def test_rules_version_and_served_are_one_snapshot_during_concurrent_edit(local, monkeypatch):
    # Logical snapshot consistency must not depend on scheduler delays.
    monkeypatch.setattr(checks, "monotonic", lambda: 0.0)
    current = local.owner.create("*", "old words", "limit")
    local.owner.set_check("*", "enforce")
    snapshot = local.rules.rules_get(effective=True, format="md")
    with closing(sqlite3.connect(local.directory / "rules.sqlite3")) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
    execute = checks._DeadlineConnection.execute
    changed = []

    def edit_between_reads(conn, sql, *args, **kwargs):
        if sql.startswith("SELECT * FROM agent_served") and not changed:
            changed.append(True)
            local.owner.edit(current["id"], current["version"], text="new words")
        return execute(conn, sql, *args, **kwargs)

    monkeypatch.setattr(checks._DeadlineConnection, "execute", edit_between_reads)
    text = injected(local)
    assert changed and quoted_rules(text) == ["old words"]
    assert text.startswith(
        f"Commonplace rules for conn:builder, rules_seq {snapshot['rules_seq']}."
    )
    next_text = injected(local)
    assert quoted_rules(next_text) == ["new words"]
    assert next_text.splitlines()[3] == FIRST_READ


@pytest.mark.parametrize("mode", ["off", "enforce"])
@pytest.mark.parametrize("store", ["rules", "boards"])
def test_lost_bound_store_has_only_header_and_pointer(local, monkeypatch, mode, store):
    BoardService(local.owner.ctx).board_add(slug="ops", wing="shared")
    local.owner.create("*", "secret text", "limit")
    local.owner.set_check("*", mode)
    (local.directory / f"{store}.sqlite3").unlink()
    before = local.db.rule_stores(local.workspace)
    monkeypatch.setattr(events, "emit", lambda *_a, **_k: pytest.fail("event emitted"))
    assert injected(local) == (
        "Commonplace rules for conn:builder.\n"
        "A rules or boards store was replaced; the owner must confirm it. "
        "Call rules_get format=md effective=true later."
    )
    assert not (local.directory / f"{store}.sqlite3").exists()
    assert local.db.rule_stores(local.workspace) == before


@pytest.mark.parametrize("store", ["rules", "boards", "control"])
def test_broken_store_fails_open_without_partial_rules(local, store, caplog):
    local.owner.create("*", "must never leak a partial set", "limit")
    if store == "control":
        # Make independent control reads unavailable while leaving the worker live.
        local.db.path = str(local.directory / "absent-control.sqlite3")
    else:
        (local.directory / f"{store}.sqlite3").write_bytes(b"broken database")
    assert initialize(local) == LEGACY
    assert len([r for r in caplog.records if "instruction injection failed" in r.message]) == 1
    assert not (local.directory / "absent-control.sqlite3").exists()


def test_unknown_snapshot_reconciliation_fails_open(local, monkeypatch):
    local.owner.create("*", "visible", "limit")

    def fail(*_a, **_k):
        raise sqlite3.OperationalError("unavailable")

    monkeypatch.setattr(checks, "reconcile", fail)
    assert initialize(local) == LEGACY


def test_rendering_stops_at_deadline_without_processing_the_rest(local, monkeypatch):
    now, quoted = [0.0], []
    monkeypatch.setattr(checks, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        service,
        "effective_rules",
        lambda *_a, **_k: dict(
            rules=[row(f"rule {i}", id=i) for i in range(500)],
            rules_seq=123456789,
            served=None,
        ),
    )
    quote = render.quote

    def costly(text):
        quoted.append(text)
        now[0] += 0.02
        return quote(text)

    monkeypatch.setattr(render, "quote", costly)
    assert initialize(local) == LEGACY
    assert 1 <= len(quoted) <= 4
    assert not (local.directory / "rules.sqlite3").exists()


def test_completed_render_is_discarded_after_deadline(local, monkeypatch):
    local.owner.create("*", "visible", "limit")
    now = [0.0]
    monkeypatch.setattr(checks, "monotonic", lambda: now[0])
    real_render = render.instructions

    def expired(*args, **kwargs):
        result = real_render(*args, **kwargs)
        now[0] += 0.081
        return result

    monkeypatch.setattr(render, "instructions", expired)
    assert initialize(local) == LEGACY


def test_each_connection_and_statement_uses_remaining_time(local, monkeypatch):
    local.owner.create("*", "visible", "limit")
    now, waits, statements = [0.0], [], []
    monkeypatch.setattr(checks, "monotonic", lambda: now[0])
    connect = sqlite3.connect

    def tracked(*args, **kwargs):
        waits.append(kwargs["timeout"])
        conn = connect(*args, **kwargs)

        def trace(sql):
            if sql.startswith("PRAGMA busy_timeout="):
                statements.append(int(sql.split("=")[1]))
            now[0] += 0.001

        conn.set_trace_callback(trace)
        now[0] += 0.002
        return conn

    monkeypatch.setattr(checks.sqlite3, "connect", tracked)
    assert "visible" in injected(local)
    assert 0 < waits[-1] < waits[0] <= 0.08
    assert 0 < statements[-1] < statements[0] <= 80
    with checks.deadline(0.005):
        with checks.deadline(0.08):
            assert checks.remaining(0.1) <= 0.005
            assert checks.remaining(0.001) <= 0.001
    assert checks._deadline.get() is None


@pytest.mark.timing
def test_sql_work_is_interrupted_and_expired_transaction_rolls_back(local):
    local.owner.create("*", "visible", "limit")
    before = local.rules.store.seq
    with pytest.raises(sqlite3.OperationalError, match="interrupted"):
        with checks.deadline(0.01), checks.connection(local.directory / "rules.sqlite3") as conn:
            conn.execute(
                "WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n WHERE x<100000000) "
                "SELECT sum(x) FROM n"
            ).fetchone()
    with pytest.raises(TimeoutError):
        with (
            checks.deadline(0.01),
            checks.connection(local.directory / "rules.sqlite3", write=True) as conn,
        ):
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE meta SET value=value+1 WHERE key='seq'")
            end = checks._deadline.set(0)
            try:
                with pytest.raises(TimeoutError):
                    conn.commit()
                conn.rollback()
            finally:
                checks._deadline.reset(end)
            raise TimeoutError()
    assert quoted_rules(injected(local)) == ["visible"]
    assert local.rules.store.seq == before


@pytest.mark.timing
@pytest.mark.parametrize("store", ["rules", "boards"])
def test_locked_store_stays_inside_overall_deadline(local, store):
    BoardService(local.owner.ctx).board_add(slug="ops", wing="shared")
    local.owner.create("*", "visible", "limit")
    path = local.directory / f"{store}.sqlite3"
    blocker = sqlite3.connect(path, isolation_level=None)
    blocker.execute("PRAGMA journal_mode=DELETE")
    blocker.execute("BEGIN EXCLUSIVE")
    start = perf_counter()
    result = initialize(local)
    elapsed = perf_counter() - start
    blocker.rollback()
    blocker.close()
    assert result == LEGACY
    assert elapsed < 0.1


def test_reconciliation_writer_also_uses_remaining_deadline(local, monkeypatch):
    now, waits, statements = [0.0], [], []
    monkeypatch.setattr(checks, "monotonic", lambda: now[0])
    boards = BoardService(local.owner.ctx)
    boards.board_add(slug="ops", wing="shared")
    local.owner.create("*", "scoped", "limit", "board", "ops")
    assert quoted_rules(injected(local)) == ["scoped"]
    boards.board_add(slug="new", wing="shared")
    connection = checks.connection
    connect = sqlite3.connect

    def tracked(*args, **kwargs):
        if "mode=rw" in str(args[0]):
            waits.append(kwargs["timeout"])
        return connect(*args, **kwargs)

    @contextmanager
    def delayed(path, *, write=False, **kwargs):
        if write:
            # Spend half the budget on reads, then more before BEGIN IMMEDIATE.
            now[0] += 0.04
        with connection(path, write=write, **kwargs) as conn:
            if write:
                conn.set_trace_callback(statements.append)
                now[0] += 0.02
            yield conn

    monkeypatch.setattr(checks.sqlite3, "connect", tracked)
    monkeypatch.setattr(checks, "connection", delayed)
    with closing(connect(local.directory / "rules.sqlite3", isolation_level=None)) as blocker:
        blocker.execute("PRAGMA journal_mode=WAL")
        blocker.execute("BEGIN IMMEDIATE")
        assert initialize(local) == LEGACY
        blocker.rollback()
    assert waits == [pytest.approx(0.04)]
    begin = statements.index("BEGIN IMMEDIATE")
    assert statements[begin - 1].startswith("PRAGMA busy_timeout=")
    assert 0 < int(statements[begin - 1].split("=")[1]) <= 20
    assert quoted_rules(injected(local)) == ["scoped"]


@pytest.mark.timing
def test_500_rule_initialize_p95_over_empty_store(local, record_property):
    store = local.rules.store

    def measure():
        samples = []
        for _ in range(20):
            start = perf_counter()
            initialize(local)
            samples.append(perf_counter() - start)
        return sorted(samples)[18]

    empty = measure()
    with store.transaction() as conn:
        conn.executemany(
            "INSERT INTO rules(agent,key,text,kind,scope,status,source,seq,created_at,updated_at,updated_by) "
            "VALUES ('*',?,?,'limit','global','accepted','owner',0,'now','now','owner')",
            [(f"rule-{i}", f"Rule {i}: keep exact words.") for i in range(500)],
        )
    assert "incomplete" in injected(local)
    populated = measure()
    record_property("empty_p95_ms", empty * 1000)
    record_property("500_rules_p95_ms", populated * 1000)
    record_property("added_p95_ms", (populated - empty) * 1000)
    assert populated - empty < 0.025


def test_existing_worker_instructions_share_budget_and_remain_unchanged(local, monkeypatch):
    # Deadline/fail-open behavior has separate coverage; isolate composition here.
    monkeypatch.setattr(checks, "monotonic", lambda: 0.0)
    local.owner.create("*", "accepted", "limit")
    local.worker.response["result"]["instructions"] = "Worker instructions: café."
    original = copy.deepcopy(local.worker.response)
    response = initialize(local)
    assert response["result"]["instructions"].endswith("\n\nWorker instructions: café.")
    assert render.length(response["result"]["instructions"]) <= 3600
    assert local.worker.response == original
    local.worker.response["result"]["instructions"] = "é" * 1750
    assert initialize(local) == local.worker.response


@pytest.mark.parametrize("response", [None, {"error": {"code": -32603}}, {"result": None}])
def test_worker_error_and_notifications_are_forwarded_unchanged(local, response):
    local.worker.response = response
    assert initialize(local) == response
    for method in ("ping", "notifications/initialized"):
        assert (
            local.gateway.mediator.handle(local.rules.principal, dict(REQUEST, method=method))
            == response
        )


def test_http_person_baseline_and_stdio_passthrough(hub, monkeypatch):
    token = hub.owner_token("bridge")
    agent = hub.gateway.authenticate("Bearer " + token)
    person = sessions.person_principal(hub.db, hub.admin, hub.workspace)
    owner = RuleService(ToolContext(person, hub.gateway))
    baseline = hub.rpc(token, "initialize")["result"]
    owner.create("*", "Follow the canary.", "limit")
    session = sessions.redeem_login_link(
        hub.db, sessions.create_login_link(hub.db, person.user_email, hub.workspace)
    )
    status, reply = hub.raw(None, REQUEST, headers={"Cookie": f"{sessions.COOKIE}={session}"})
    assert status == 200 and json.loads(reply)["result"] == baseline
    monkeypatch.setenv("INJECT_BRIDGE_TOKEN", token)
    output, error = io.StringIO(), io.StringIO()
    assert (
        bridge.run_bridge(
            f"http://127.0.0.1:{hub.port}/mcp",
            "INJECT_BRIDGE_TOKEN",
            stdin=io.StringIO(json.dumps(REQUEST) + "\n"),
            stdout=output,
            stderr=error,
        )
        == 0
    )
    assert not error.getvalue()
    assert json.loads(output.getvalue())["result"]["instructions"] == service.instructions_for(
        ToolContext(agent, hub.gateway)
    )
