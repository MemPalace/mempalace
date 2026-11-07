"""Orchestrator surfaces: MCP tools, REST routes and the extension registration.

Head-only feature and guard tests (class (b)). R9: an agent token whose role is owner
(a Connect-style seat or any PAT) is refused every owner action; a signed-in owner and
the CLI (local principal) are not. The board lives in a real workspace behind the
production gateway; the machine row is written with raw SQL (only its requester grant
matters here). Names and tokens are synthetic.
"""

import dataclasses
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from commonplace import events, extensions, sessions
from commonplace.boards.service import BoardService
from commonplace.control_db import Principal
from commonplace.extensions import ToolContext, ToolError
from commonplace.orch import rest, store as orch_store, tools
from commonplace.policy import Policy
from commonplace.runner import registry
from commonplace.workspace_store import workspace_path
from tests._commonplace_helpers import hub_fixture  # noqa: F401
from tests.runner_harness import http

BOARD = "acme"
FRESH = "beta"
MACHINE = "mach_0123456789abcdef"
ROLE = "main"
HEAD = "b" * 40
REPO = "example-org/example-repo"
SECRET_PROMPT = "Synthetic prompt text that never leaves the config store."
READ_TOOLS = {
    "orch_config_get",
    "orch_config_render",
    "orch_config_diff",
    "orch_checkpoint",
    "orch_restore_status",
}
OWNER_TOOLS = {
    "orch_config_accept",
    "orch_config_write",
    "orch_config_reject",
    "orch_exp_retire",
    "orch_restore_ack",
    "orch_force_release",
    "orch_void_step",
}


def config_text(board=BOARD, prompt=SECRET_PROMPT, runner=True):
    lines = [
        'schema = "commonplace.orchestrator/1"',
        f'project = "{board}"',
        "[limits]",
        "max_agents = 3",
        "[roles.main]",
        'kind = "orchestrator"',
        'harness = "t3"',
        'topic = "orchestrate"',
        'model_role = "main"',
        'workflow = "card-pipeline"',
        f"prompt = '''{prompt}'''",
        "[roles.builder]",
        'kind = "builder"',
        'parent = "main"',
        'harness = "t3"',
        'topic = "worker"',
        'model_role = "worker"',
        'workflow = "build-unit"',
        "prompt = '''Build the card.'''",
        "[workflows.card-pipeline]",
        'steps = ["builder"]',
        "[workflows.build-unit]",
        'steps = ["implement", "report"]',
    ]
    if runner:
        lines += [
            "[runner]",
            'role = "builder"',
            f'machine = "{MACHINE}"',
            'project = "demo"',
            'runtime_mode = "approval-required"',
            "[[triggers]]",
            'on = "card.todo"',
        ]
    return "\n".join(lines) + "\n"


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


class Env:
    def __init__(self, hub):
        self.hub = hub
        self.ws = hub.workspace
        self.owner = sessions.person_principal(hub.db, hub.admin, hub.workspace)
        link = sessions.create_login_link(hub.db, "owner@example.com", hub.workspace)
        self.cookie = f"{sessions.COOKIE}={sessions.redeem_login_link(hub.db, link)}"
        boards = BoardService(self.ctx(self.owner))
        for slug, wing in ((BOARD, "shared"), (FRESH, "shared"), ("closed", "closed")):
            boards.board_add(slug=slug, wing=wing)
        self.boards = boards
        self.a_token, self.a = self.pat("orch-a")
        self.b_token, self.b = self.pat("orch-b")
        self.seat_token, self.seat = self.pat("connect-seat", trust="standard")
        self.untrusted_token, self.untrusted = self.pat(
            "untrusted-writer", trust="untrusted", write_wing="shared", write_wings=None
        )
        self.reader_token, self.reader = self.pat(
            "reader", scopes=["memory:read"], write_wings=None
        )
        self.machine([self.a.connection_id, self.b.connection_id, self.seat.connection_id])
        text = config_text()
        orch_store.open_store(hub.gateway, self.ws).write(
            self.owner, BOARD, text, connections=[], sha256=sha(text)
        )

    def ctx(self, principal):
        return ToolContext(principal=principal, gateway=self.hub.gateway)

    def pat(self, name, trust="trusted", scopes=("memory:read", "memory:write"), **wings):
        policy = {"read_wings": ["shared"], "write_wings": ["shared"], **wings}
        policy = {k: v for k, v in policy.items() if v is not None}
        token = self.hub.token(name, profile="full", scopes=list(scopes), trust=trust, **policy)
        return token, self.hub.gateway.authenticate(f"Bearer {token}")

    def machine(self, requesters):
        registry.ensure(self.hub.db)
        now = "2030-01-01T00:00:00Z"
        with self.hub.db._lock, self.hub.db._tx() as conn:
            conn.execute(
                "INSERT INTO runner_machines(id, workspace_id, label, machine_connection_id, "
                "worker_connection_id, projects, topics, wings, requesters, binding_state, "
                "enrolled_by, updated_by, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,'owner_attested',?,?,?,?)",
                (
                    MACHINE,
                    self.ws,
                    "runner-one",
                    "conn_machine_example",
                    "conn_worker_example",
                    '["demo"]',
                    '["worker"]',
                    '["shared"]',
                    json.dumps(requesters),
                    self.hub.admin,
                    self.hub.admin,
                    now,
                    now,
                ),
            )

    # transports
    def mcp(self, token, tool, /, **arguments):
        """(ok, payload): payload is the parsed result, or the refusal text."""
        response = self.hub.rpc(token, "tools/call", {"name": tool, "arguments": arguments})
        if "error" in response:
            return False, response["error"]["message"]
        result = response["result"]
        text = result["content"][0]["text"]
        if result.get("isError"):
            return False, text
        return True, json.loads(text)

    def rest(self, method, path, body=None, token=None, cookie=None):
        return http(
            self.hub.port, method, "/api/orch/" + path, token=token, cookie=cookie, body=body
        )

    def person(self, method, path, body=None):
        return self.rest(method, path, body, cookie=self.cookie)

    def runner_db(self):
        conn = sqlite3.connect(
            str(self.hub.gateway.workspace_dir(self.ws)) + "/runner.sqlite3", isolation_level=None
        )
        conn.row_factory = sqlite3.Row
        return conn

    def propose(self, board=BOARD, **kw):
        text = config_text(board=board, **kw)
        version = orch_store.open_store(self.hub.gateway, self.ws).propose(
            self.a, board, text, connections=[]
        )
        return version.version, sha(text)

    def status(self, board, version):
        return orch_store.open_store(self.hub.gateway, self.ws).get(board, version).status

    def events(self):
        return [
            e
            for e in events.after(self.hub.db, self.ws, 0, 10_000)
            if e["kind"].startswith("orch.")
        ]

    def local(self):
        """The CLI's principal: the data-dir owner (``auth="local"``)."""
        return Principal(
            user_id=self.hub.admin,
            user_email="owner@example.com",
            workspace_id=self.ws,
            connection_id="",
            connection="cli",
            client="commonplace",
            auth="local",
            role="owner",
            policy=Policy.build(
                profile="full",
                scopes=["memory:read", "memory:write", "admin"],
                read_wings=["*"],
                trust="trusted",
            ),
        )


@pytest.fixture
def env(hub):
    return Env(hub)


def code_in(text, code):
    return f'"code": "{code}"' in text


# ---------------------------------------------------------------------- R9


def test_r9_owner_role_agent_tokens_are_refused_every_owner_action(env):
    version, digest = env.propose()
    calls = {
        "orch_config_accept": {"board": BOARD, "version": version, "sha256": digest},
        "orch_config_write": {"board": BOARD, "text": config_text(), "sha256": sha(config_text())},
        "orch_config_reject": {"board": BOARD, "version": version},
        "orch_exp_retire": {"board": BOARD, "name": "trial"},
        "orch_restore_ack": {"text": "effects after 2030-01-01T00:00:00Z may be unknown"},
        "orch_force_release": {"board": BOARD, "role": ROLE},
        "orch_void_step": {"board": BOARD, "role": ROLE, "step_key": "s1"},
    }
    assert set(calls) == OWNER_TOOLS
    for token, principal in ((env.seat_token, env.seat), (env.a_token, env.a)):
        assert principal.role == "owner" and not principal.person
        for name, arguments in calls.items():
            ok, text = env.mcp(token, name, **arguments)
            assert not ok and code_in(text, "owner_only"), (name, text)
        ok, text = env.mcp(token, "orch_resume", board=BOARD, role=ROLE, force=True)
        assert not ok and code_in(text, "owner_only"), text
    status, body = env.rest(
        "POST",
        f"boards/{BOARD}/configs/{version}/accept",
        {"sha256": digest},
        token=env.seat_token,
    )
    assert (status, body["code"]) == (403, "owner_only")
    status, body = env.rest("POST", "restore/ack", {"text": "x"}, token=env.a_token)
    assert (status, body["code"]) == (403, "owner_only")
    status, body = env.rest(
        "POST", f"boards/{BOARD}/roles/{ROLE}/steps/s1/void", {}, token=env.seat_token
    )
    assert (status, body["code"]) == (403, "owner_only")
    assert env.status(BOARD, version) == "proposed"
    assert env.events() == []

    # A signed-in owner may.
    status, body = env.person(
        "POST", f"boards/{BOARD}/configs/{version}/accept", {"sha256": digest}
    )
    assert status == 200, body
    assert body["accepted"] == f"v{version}" and env.status(BOARD, version) == "accepted"


def test_r9_cli_principal_and_person_owner_pass_runner_child_does_not(env):
    version, digest = env.propose()
    local = env.ctx(env.local())
    result = tools.dispatch(
        local, "orch_config_accept", {"board": BOARD, "version": version, "sha256": digest}
    )
    assert result["accepted"] == f"v{version}"
    second, digest2 = env.propose(prompt="Second synthetic prompt.")
    child = dataclasses.replace(env.owner, runner=MACHINE)
    with pytest.raises(ToolError) as excinfo:
        tools.dispatch(
            env.ctx(child),
            "orch_config_accept",
            {"board": BOARD, "version": second, "sha256": digest2},
        )
    assert excinfo.value.code == "owner_only"
    assert not tools.available(child, env.hub.gateway)
    assert not rest.visible(child, env.hub.gateway, "orch_config_get")
    assert tools.available(env.owner, env.hub.gateway)
    assert rest.visible(env.owner, env.hub.gateway, "orch_config_accept")
    result = tools.dispatch(
        env.ctx(env.owner), "orch_config_reject", {"board": BOARD, "version": second}
    )
    assert result["status"] == "rejected"


# --------------------------------------------------------------- visibility


def test_untrusted_tokens_see_only_read_and_propose(env):
    orch = {name for name in extensions.names() if name.startswith("orch_")}
    assert len(orch) == 21
    assert {n for n in env.hub.tool_names(env.untrusted_token) if n.startswith("orch_")} == (
        READ_TOOLS | {"orch_config_propose"}
    )
    assert {n for n in env.hub.tool_names(env.a_token) if n.startswith("orch_")} == orch
    for name in orch - READ_TOOLS - {"orch_config_propose"}:
        assert extensions.get(name).trusted_only, name
        assert extensions.get(name).available is tools.available, name
    ok, text = env.mcp(env.untrusted_token, "orch_resume", board=BOARD, role=ROLE)
    assert not ok and "Unknown tool" in text
    status, body = env.rest("POST", f"boards/{BOARD}/roles/{ROLE}/resume", {}, env.untrusted_token)
    assert status == 403 and body == {"error": "tool not allowed for this connection"}
    status, body = env.rest("GET", f"boards/{BOARD}/configs", token=env.untrusted_token)
    assert status == 200 and body["accepted"]["status"] == "accepted"


def test_owner_tools_are_hidden_from_member_tokens(env, hub):
    member = hub.db.create_user("member@example.com")
    hub.db.add_member(hub.workspace, member, "member", actor=hub.admin)
    token = hub.token(
        "member-pat",
        user=member,
        profile="full",
        scopes=["memory:read", "memory:write"],
        read_wings=["shared"],
        write_wings=["shared"],
        trust="trusted",
    )
    names = {n for n in hub.tool_names(token) if n.startswith("orch_")}
    assert names and not names & OWNER_TOOLS


def test_untrusted_proposal_is_stamped_and_wing_access_applies(env):
    text = config_text(prompt="Untrusted synthetic proposal.")
    ok, body = env.mcp(env.untrusted_token, "orch_config_propose", board=BOARD, text=text)
    assert ok, body
    assert body["status"] == "proposed"
    assert body["proposer_trust"] == "untrusted" and body["proposed_by"] == "conn:untrusted-writer"
    assert "body" not in body
    ok, text_ = env.mcp(env.reader_token, "orch_config_propose", board=BOARD, text=text)
    assert not ok and "Unknown tool" in text_  # no memory:write scope
    status, body = env.rest("POST", f"boards/{BOARD}/configs", {"text": text}, token=env.b_token)
    assert status == 201 and body["proposed_by"] == "conn:orch-b"
    for name, args in (
        ("orch_config_get", {"board": "closed"}),
        ("orch_config_propose", {"board": "closed", "text": config_text(board="closed")}),
        ("orch_checkpoint", {"board": "closed", "role": ROLE}),
        ("orch_config_get", {"board": "missing"}),
    ):
        ok, text_ = env.mcp(env.a_token, name, **args)
        assert not ok and code_in(text_, "board_not_found"), (name, text_)
    status, body = env.rest("GET", "boards/closed/configs", token=env.a_token)
    assert (status, body["code"]) == (404, "board_not_found")


def test_propose_needs_write_access_to_the_wing(env, hub):
    token = hub.token(
        "read-shared",
        profile="full",
        scopes=["memory:read", "memory:write"],
        read_wings=["shared"],
        write_wings=["elsewhere"],
        trust="trusted",
    )
    ok, text = env.mcp(token, "orch_config_propose", board=BOARD, text=config_text())
    assert not ok and code_in(text, "not_allowed"), text
    ok, body = env.mcp(token, "orch_config_get", board=BOARD)
    assert ok and body["accepted"]["body"] == config_text()


# ------------------------------------------------- accept, arming, events


def test_person_accept_arms_the_cursor_and_events_carry_refs_only(env):
    env.boards.task_add("Old todo card", board=FRESH, status="todo")
    text = config_text(board=FRESH)
    status, body = env.rest("POST", f"boards/{FRESH}/configs", {"text": text}, token=env.a_token)
    assert status == 201, body
    version = body["version"]
    status, body = env.person(
        "POST", f"boards/{FRESH}/configs/{version}/accept", {"sha256": sha(text)}
    )
    assert status == 200, body
    assert body["cursor"] == "armed" and body["previous"] is None
    assert "+ runner" in body["diff"]
    with env.runner_db() as conn:
        row = conn.execute("SELECT cursor, gen FROM orch_cursor WHERE board=?", (FRESH,)).fetchone()
    assert row["cursor"] is not None and row["cursor"] >= 1
    kinds = [(e["kind"], e["subject"]) for e in env.events()]
    assert kinds == [("orch.config.proposed", FRESH), ("orch.config.accepted", FRESH)]
    for event in env.events():
        assert set(event["data"]) <= {"board", "version", "channel", "sha256", "status"}
        assert event["data"]["board"] == FRESH
    dumped = json.dumps(events.after(env.hub.db, env.ws, 0, 10_000))
    assert SECRET_PROMPT not in dumped and "[runner]" not in dumped


def test_owner_write_keeps_the_cursor_when_runner_stays(env):
    text = config_text(prompt="Rewritten synthetic prompt.")
    status, body = env.person("POST", f"boards/{BOARD}/write", {"text": text, "sha256": sha(text)})
    assert status == 200, body
    assert body["cursor"] is None and body["previous"] == "v1"
    assert "roles.main.prompt" in body["diff"]
    plain = config_text(runner=False)
    status, body = env.person(
        "POST", f"boards/{BOARD}/write", {"text": plain, "sha256": sha(plain)}
    )
    assert status == 200 and body["cursor"] == "dropped"


def test_read_render_diff_and_reject_over_both_transports(env):
    v2, _digest = env.propose(prompt="Second synthetic prompt.")
    ok, body = env.mcp(env.a_token, "orch_config_get", board=BOARD, version=v2)
    assert ok and body["version"]["status"] == "proposed" and "Second" in body["version"]["body"]
    ok, body = env.mcp(env.a_token, "orch_config_get", board=BOARD)
    assert [row["ref"] for row in body["versions"]] == ["v2", "v1"]
    assert all("body" not in row for row in body["versions"])
    ok, body = env.mcp(env.a_token, "orch_config_diff", board=BOARD, old=1, new=v2)
    assert ok and "roles.main.prompt" in body["diff"]
    status, body = env.rest("GET", f"boards/{BOARD}/diff?old=v1&new={v2}", token=env.a_token)
    assert status == 200 and "roles.main.prompt" in body["diff"]
    status, body = env.rest("GET", f"boards/{BOARD}/render", token=env.a_token)
    assert status == 200 and body["version"] == "v1" and SECRET_PROMPT in body["markdown"]
    status, body = env.rest("GET", f"boards/{BOARD}/configs/{v2}", token=env.a_token)
    assert status == 200 and body["version"]["ref"] == f"v{v2}"
    status, body = env.person("POST", f"boards/{BOARD}/configs/{v2}/reject", {})
    assert status == 200 and body["status"] == "rejected"
    status, body = env.person("POST", f"boards/{BOARD}/configs/{v2}/reject", {})
    assert (status, body["code"]) == (409, "not_proposed")
    assert [e["kind"] for e in env.events()] == ["orch.config.rejected"]


def test_restore_status_and_ack_rearm_configured_boards(env):
    directory = env.hub.gateway.workspace_dir(env.ws)
    marker = {"version": 1, "backup_created_at": "2030-02-03T04:05:06Z", "id": "c" * 32}
    with open(f"{directory}/{orch_store.RESTORE_MARKER}", "w") as handle:
        json.dump(marker, handle)
    status, body = env.rest("GET", "restore", token=env.reader_token)
    assert status == 200 and body["restore_pending"] is True
    expected = "effects after 2030-02-03T04:05:06Z may be unknown"
    assert body["ack_text"] == expected
    ok, text = env.mcp(env.a_token, "orch_resume", board=BOARD, role=ROLE)
    assert not ok and code_in(text, "restore_pending"), text
    status, body = env.person("POST", "restore/ack", {"text": "effects may be unknown"})
    assert (status, body["code"]) == (403, "restore_text")
    status, body = env.person("POST", "restore/ack", {"text": expected})
    assert status == 200, body
    assert [(a["board"], a["armed"]) for a in body["arming"]] == [(BOARD, True)]
    with env.runner_db() as conn:
        row = conn.execute("SELECT cursor FROM orch_cursor WHERE board=?", (BOARD,)).fetchone()
    assert row["cursor"] is not None
    assert [e["kind"] for e in env.events()] == ["orch.restore.acked"]
    status, body = env.person("POST", "restore/ack", {"text": expected})
    assert (status, body["code"]) == (409, "restore_not_pending")


def test_cli_accept_and_restore_ack_arm_so_the_next_todo_card_starts(hub, tmp_path, capsys):
    """CLI ``orch accept`` and ``orch restore-ack`` arm the cursor at once: a todo card
    added after either (before any sweep) starts exactly one job on the next sweep."""
    from commonplace.cli import main as cli_main
    from tests import test_orch_runner as runner_tests

    world = runner_tests.World(hub)
    data = str(tmp_path / "data")
    assert hub.gateway.workspace_dir(hub.workspace) == str(
        Path(workspace_path(data, hub.workspace))
    )
    path = tmp_path / "orchestrator.toml"
    path.write_text(runner_tests.config_text(world.machine_id), encoding="utf-8")
    ws, board = ("--workspace", hub.workspace), ("--board", runner_tests.BOARD)

    def cli(*args):
        capsys.readouterr()
        code = cli_main(["--data", data, "orch", *args])
        out = capsys.readouterr()
        assert code == 0, out.err
        return json.loads(out.out)

    proposed = cli("submit", str(path), *ws)
    accepted = cli("accept", *ws, *board, proposed["ref"], "--sha256", proposed["sha256"])
    first = world.card(title="Added after the CLI accept")
    world.sweep()
    assert [job["task_number"] for job in world.all_jobs()] == [first]
    assert accepted["cursor"] == "armed"

    state = world.flag_restore()
    acked = cli("restore-ack", *ws, "--text", state.ack_text)
    second = world.card(title="Added after the CLI restore ack")
    world.sweep()
    assert [job["task_number"] for job in world.all_jobs()] == [first, second]
    assert [(a["board"], a["armed"]) for a in acked["arming"]] == [(runner_tests.BOARD, True)]


# ------------------------------------------------- lease, journal, gates


def test_lease_journal_and_gate_round_trip_over_mcp(env):
    ok, claim = env.mcp(env.a_token, "orch_resume", board=BOARD, role=ROLE)
    assert ok, claim
    epoch, token = claim["epoch"], claim["token"]
    assert claim["checkpoint"]["lease"]["epoch"] == epoch
    lease = {"board": BOARD, "role": ROLE, "epoch": epoch, "token": token}
    ok, text = env.mcp(env.b_token, "orch_resume", board=BOARD, role=ROLE)
    assert not ok and code_in(text, "held") and '"epoch": 1' in text and token not in text
    ok, body = env.mcp(env.a_token, "orch_heartbeat", **lease)
    assert ok and body["epoch"] == epoch
    ok, step = env.mcp(env.a_token, "orch_journal_intent", **lease, step_key="m1", kind="merge")
    assert ok and step["state"] == "intent"
    ok, step = env.mcp(
        env.a_token,
        "orch_journal_record",
        **lease,
        step_key="m1",
        state="started",
        evidence={"type": "holder_attested", "note": "Synthetic note."},
    )
    assert ok and step["state"] == "started"
    ok, gate = env.mcp(
        env.a_token, "orch_gate_open", **lease, step_key="g1", repo=REPO, pr=7, head=HEAD
    )
    assert ok and gate["gate"]["state"] == "open"
    ok, gate = env.mcp(env.a_token, "orch_gate_record", **lease, step_key="g1", verdict="fail")
    assert ok and gate["gate"]["state"] == "gate_fail"
    ok, text = env.mcp(env.b_token, "orch_heartbeat", **{**lease, "token": "not-the-token"})
    assert not ok and code_in(text, "fenced")
    ok, checkpoint = env.mcp(env.reader_token, "orch_checkpoint", board=BOARD, role=ROLE)
    assert ok and [g["state"] for g in checkpoint["gates"]] == ["gate_fail"]
    ok, body = env.mcp(env.a_token, "orch_release", **lease)
    assert ok and body["released"] is True
    leaked = json.dumps(events.after(env.hub.db, env.ws, 0, 10_000))
    with env.hub.db._lock:
        audit = json.dumps([dict(r) for r in env.hub.db._conn.execute("SELECT * FROM audit_log")])
    assert token not in leaked and token not in audit


def test_lease_and_owner_actions_over_rest(env):
    base = f"boards/{BOARD}/roles/{ROLE}"
    status, claim = env.rest("POST", f"{base}/resume", {}, token=env.a_token)
    assert status == 200, claim
    lease = {"epoch": claim["epoch"], "token": claim["token"]}
    status, body = env.rest("POST", f"{base}/resume", {}, token=env.b_token)
    assert (status, body["code"]) == (409, "held") and body["details"]["epoch"] == 1
    status, step = env.rest(
        "POST", f"{base}/journal", {**lease, "step_key": "h1", "kind": "hold"}, token=env.a_token
    )
    assert status == 200 and step["state"] == "intent"
    status, step = env.rest(
        "POST",
        f"{base}/journal/h1",
        {**lease, "state": "failed", "code": "gave_up"},
        token=env.a_token,
    )
    assert status == 200 and step["state"] == "failed"
    status, body = env.rest(
        "POST",
        f"{base}/gates",
        {**lease, "step_key": "g1", "repo": REPO, "pr": 3, "head": HEAD},
        token=env.a_token,
    )
    assert status == 200 and body["gate"]["state"] == "open"
    status, body = env.rest(
        "POST", f"{base}/gates/g1", {**lease, "verdict": "pass"}, token=env.a_token
    )
    assert status == 200 and body["gate"]["state"] == "gate_pass"
    status, body = env.rest("POST", f"{base}/heartbeat", lease, token=env.a_token)
    assert status == 200
    status, body = env.person("POST", f"{base}/steps/g1/void", {})
    assert status == 200 and body["voided"] is True
    status, body = env.person("POST", f"{base}/force-release", {})
    assert status == 200 and body == {"epoch": 1, "released": True}
    status, body = env.rest("POST", f"{base}/heartbeat", lease, token=env.a_token)
    assert (status, body["code"]) == (409, "fenced")
    status, body = env.rest("GET", f"{base}/checkpoint", token=env.b_token)
    assert status == 200 and body["lease"]["live"] is False
    assert [e["kind"] for e in env.events()] == ["orch.step.voided", "orch.lease.forced"]
    for event in env.events():
        assert set(event["data"]) <= {"board", "role", "step_key", "epoch"}


def test_orch_card_start_maps_refusals_and_reply_status(env, monkeypatch):
    card = env.boards.task_add("Synthetic card", board=BOARD, status="todo")["number"]
    ok, claim = env.mcp(env.a_token, "orch_resume", board=BOARD, role=ROLE)
    assert ok, claim
    start = {"card": card, "step_key": "start-1", "epoch": claim["epoch"], "token": "wrong"}
    ok, text = env.mcp(env.a_token, "orch_card_start", board=BOARD, role=ROLE, **start)
    # No project model is set: the bridge refuses before the fenced transaction.
    assert not ok and code_in(text, "model_unresolved"), text
    status, body = env.rest(
        "POST", f"boards/{BOARD}/roles/{ROLE}/card-starts", start, token=env.a_token
    )
    assert (status, body["code"]) == (409, "model_unresolved")
    from commonplace.orch import runner_bridge

    seen = []

    def fake(ctx, board, role, card_, step_key, epoch, token, config_ref=None):
        seen.append((board, role, card_, step_key, epoch, token, config_ref))
        return {"status": 201, "job": {"id": "job_example"}}

    monkeypatch.setattr(runner_bridge, "orch_card_start", fake)
    status, body = env.rest(
        "POST",
        f"boards/{BOARD}/roles/{ROLE}/card-starts",
        {**start, "token": claim["token"], "config_ref": "live"},
        token=env.a_token,
    )
    assert status == 201 and body["job"]["id"] == "job_example"
    assert seen == [(BOARD, ROLE, card, "start-1", claim["epoch"], claim["token"], "live")]


# ------------------------------------------------------------ REST shape


def test_rest_rejects_unknown_routes_bad_arguments_and_anonymous_calls(env):
    assert env.rest("GET", "boards/acme/nothing", token=env.a_token)[0] == 404
    assert env.rest("GET", "elsewhere", token=env.a_token)[0] == 404
    assert (
        env.rest("POST", f"boards/{BOARD}/roles/{ROLE}/checkpoint", {}, token=env.a_token)[0] == 404
    )
    status, body = env.rest("GET", f"boards/{BOARD}/configs/v0", token=env.a_token)
    assert (status, body["code"]) == (400, "invalid_argument")
    status, body = env.rest(
        "POST", f"boards/{BOARD}/configs", {"text": "x", "board": "other"}, token=env.a_token
    )
    assert (status, body["code"]) == (400, "invalid_argument")
    status, body = env.rest(
        "POST",
        f"boards/{BOARD}/roles/{ROLE}/heartbeat",
        {"epoch": "1", "token": "t"},
        token=env.a_token,
    )
    assert (status, body["code"]) == (400, "invalid_argument")
    status, _body = env.rest("GET", f"boards/{BOARD}/configs")
    assert status == 401


def test_dispatch_validates_arguments_before_any_store(env):
    ctx = env.ctx(env.a)
    for name, args in (
        ("orch_config_get", {"board": BOARD, "extra": 1}),
        ("orch_config_get", {}),
        ("orch_config_get", {"board": BOARD, "version": True}),
        ("orch_heartbeat", {"board": BOARD, "role": ROLE, "epoch": 0, "token": "t"}),
        (
            "orch_gate_record",
            {
                "board": BOARD,
                "role": ROLE,
                "epoch": 1,
                "token": "t",
                "step_key": "g",
                "verdict": "maybe",
            },
        ),
        ("orch_config_accept", {"board": BOARD, "version": 1, "sha256": "short"}),
    ):
        with pytest.raises(ToolError) as excinfo:
            tools.dispatch(ctx, name, args)
        assert excinfo.value.code == "invalid_argument", (name, args)
        assert excinfo.value.status == 400


def test_extension_module_is_registered_after_the_runner():
    modules = extensions.MODULES
    assert "commonplace.orch" in modules
    assert modules.index("commonplace.orch") > modules.index("commonplace.runner")
    loaded = {module.__name__ for module in extensions.load_modules()}
    assert "commonplace.orch" in loaded


def test_rest_writes_run_the_rules_check_and_are_audited_by_name(env, monkeypatch):
    from commonplace.rules import enforce

    text = config_text(prompt="Audited synthetic proposal.")
    status, body = env.rest(
        "POST", f"boards/{BOARD}/configs", {"text": text, "rules_seq": "ignored"}, env.a_token
    )
    assert status == 201, body
    with env.hub.db._lock:
        rows = env.hub.db._conn.execute(
            "SELECT decision, reason FROM audit_log WHERE tool='orch_config_propose'"
        ).fetchall()
    assert [(r["decision"], r["reason"]) for r in rows] == [("ok", "transport=rest")]
    seen = []

    def stale(ctx, tool, supplied):
        seen.append(tool)
        return enforce.Check("rules_seq stale", "no: synthetic refresh notice", True)

    monkeypatch.setattr(enforce, "check", stale)
    status, body = env.rest("POST", f"boards/{BOARD}/configs", {"text": text}, env.a_token)
    assert (status, body) == (428, {"error": "no: synthetic refresh notice", "rules_refresh": True})
    assert seen == ["orch_config_propose"]
    status, _body = env.rest("GET", f"boards/{BOARD}/configs", token=env.a_token)
    assert status == 200 and seen == ["orch_config_propose"]


def test_repeat_void_has_one_journal_event_and_two_feed_notifications(env):
    ok, lease = env.mcp(env.a_token, "orch_resume", board=BOARD, role=ROLE)
    assert ok
    ok, _ = env.mcp(
        env.a_token,
        "orch_journal_intent",
        board=BOARD,
        role=ROLE,
        epoch=lease["epoch"],
        token=lease["token"],
        step_key="h1",
        kind="hold",
    )
    assert ok
    first = tools.void_step(env.ctx(env.owner), BOARD, ROLE, "h1")
    second = tools.void_step(env.ctx(env.local()), BOARD, ROLE, "h1")
    assert first == second
    with env.runner_db() as conn:
        rows = conn.execute("SELECT * FROM orch_journal WHERE event='void'").fetchall()
    assert len(rows) == 1
    feed = [e for e in env.events() if e["kind"] == "orch.step.voided"]
    assert len(feed) == 2
    assert all(e["data"] == {"board": BOARD, "role": ROLE, "step_key": "h1"} for e in feed)
