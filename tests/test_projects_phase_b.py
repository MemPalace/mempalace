"""Opt-in admission: the RED selection runs unchanged on the pinned implementation."""

import json

import pytest

from commonplace.boards.service import BoardService
from commonplace.extensions import ToolError
from commonplace.runner import jobs, registry, tools
from tests._commonplace_helpers import hub_fixture  # noqa: F401
from tests._phase_b_helpers import (
    World,
    board,
    call,
    cap,
    claim,
    count,
    doing,
    machine_two,
    raw_state,
    seed,
    trusted_seat,
)
from tests._projects_helpers import projects_hub_fixture  # noqa: F401


@pytest.fixture
def world(hub):
    return World(hub)


def test_r1_settings(projects_hub):
    h = projects_hub
    status, payload, _ = h.request(h.owner, "PUT", "/api/settings/orchestration", {"enabled": True})
    assert (status, payload) == (200, {"enabled": True})  # RED
    assert h.get(h.owner, "/api/settings/orchestration")[1] == {"enabled": True}
    audit = [r for r in h.db.audit_entries(h.workspace) if r["action"] == "workspace.orchestration"]
    assert len(audit) == 1
    assert audit[0]["decision"] == "allow" and audit[0]["user_id"] == h.admin
    assert audit[0]["workspace_id"] == h.workspace


def test_r1b_authority_and_workspace(projects_hub):
    h = projects_hub
    results = [
        h.request(a, "PUT", "/api/settings/orchestration", {"enabled": True})[0]
        for a in (h.member, h.a1, None)
    ]
    h.request(h.owner, "PUT", "/api/settings/orchestration", {"enabled": True})
    other = h.get(h.other, "/api/settings/orchestration")[:2]
    with h.db._lock:
        h.db._conn.execute(
            "UPDATE memberships SET role='owner' WHERE workspace_id=? AND user_id=?",
            (h.workspace, h.member_user),
        )
    second = h.request(h.member, "PUT", "/api/settings/orchestration", {"enabled": False})[0]
    assert (results, other, second) == ([403, 403, 401], (200, {"enabled": False}), 200)  # RED


@pytest.mark.parametrize("path", ["claim", "start", "lease", "starting"])
def test_r1c_unreadable_switch(world, path):
    w = world
    a = w.card()
    w.person_start(a)
    payload = w.lease()
    with w.db._lock:
        w.db._conn.execute("DROP TABLE IF EXISTS orchestration_settings")
        w.db._conn.execute(
            "CREATE VIEW orchestration_settings AS SELECT * FROM missing_switch_source"
        )
    if path == "claim":
        result = claim(w, w.card())
    elif path == "start":
        result = call(w.person_start, w.card())
    elif path == "lease":
        result = call(jobs.lease, w.js, w.machine())
    else:
        result = w.machine_call("starting", {"lease_token": payload["lease_token"]})
    assert "settings_unavailable" in str(result)  # RED


def test_r2_only_coordinator(world):
    w = world
    seed(w)
    result = call(tools.card_start, w.ctx(w.a), w.card(), w.machine_id, "demo", "worker")
    assert "not_requested_by_orchestrator" in str(result) and not w.all_jobs()  # RED


def test_r2b_recheck_coordinator(world):
    w = world
    w.boards.board_set("acme", "conn:orch-a")
    seed(w)
    tools.card_start(w.ctx(w.a), w.card(), w.machine_id, "demo", "worker")
    payload = w.lease()
    w.boards.board_set("acme", "conn:orch-b")
    result = w.machine_call("starting", {"lease_token": payload["lease_token"]})
    assert (
        "not_requested_by_orchestrator" in str(result) and w.all_jobs()[0]["state"] == "cancelled"
    )  # RED


def test_r3_default_cap(world):
    w = world
    seed(w)
    results = [call(w.person_start, w.card()) for _ in range(4)]
    assert [r[0] for r in results] == [201, 201, 201, 409] and "project_limit 3/3" in str(
        results[-1]
    )  # RED


def test_r4_jobs_block_claim(world):
    w = world
    seed(w)
    cap(w, 2)
    w.person_start(w.card())
    w.person_start(w.card())
    result = claim(w, w.card())
    assert result[0] == 409 and "project_limit" in str(result)  # RED


def test_r5_one_job_blocks_claim(world):
    w = world
    seed(w)
    cap(w, 1)
    w.person_start(w.card())
    result = claim(w, w.card())
    assert result[0] == 409 and "project_limit 1/1" in str(result)  # RED


def test_r6_busy_assignment(world):
    w = world
    seed(w)
    doing(w, w.card(), "conn:orch-a")
    result = call(w.boards.task_add, "Reserved", board="acme", assignee="conn:orch-a")
    handoff = call(w.boards.task_handoff, w.card(), "conn:orch-a", "Reserve this")
    assert all(r[0] == 409 and "agent_busy" in str(r) for r in (result, handoff))  # RED


def test_r7_workers_count_cards(world):
    w = world
    seed(w)
    cap(w, 2)
    worker = w.db.connection(w.machine()["worker_connection_id"], w.ws)["name"]
    for _ in range(2):
        doing(w, w.card(), "conn:" + worker)
    result = claim(w, w.card())
    assert result[0] == 409 and "project_limit 2/2" in str(result)  # RED


def lifecycle(w, monkeypatch, *, second=True):
    seed(w)
    m2 = machine_two(w) if second else w.machine()
    now = "2030-01-01T00:00:00Z"
    monkeypatch.setattr(jobs, "_now", lambda: now)
    a, b = w.card(), w.card()
    w.person_start(a)
    if second:
        tools.card_start(w.ctx(w.owner), b, m2["id"], "demo", "worker")
    else:
        w.person_start(b)
    cap(w, 1)
    payload = w.lease()
    w.launch(payload)
    doing(w, a, "conn:" + w.db.connection(w.machine()["worker_connection_id"], w.ws)["name"])
    return a, b, m2


def test_r8_cap_refuses_idle_machine(world, monkeypatch):
    w = world
    _, _, m2 = lifecycle(w, monkeypatch)
    leased = jobs.lease(w.js, m2)
    assert leased is None and (board(w).get("used"), board(w).get("max_agents")) == (1, 1)  # RED
    assert not w.rows(
        "SELECT id FROM runner_jobs WHERE machine_id=? AND state IN ('leased','starting','uncertain')",
        (m2["id"],),
    )


# R8 carries the behavioral RED on develop. These lifecycle checks already
# lease correctly there; their used-field assertions additionally pin accounting.
def test_r8a_done_frees_project_slot(world, monkeypatch):
    w = world
    a, b, m2 = lifecycle(w, monkeypatch)
    raw_state(w, a, "done")
    result = jobs.lease(w.js, m2)
    assert result is not None and result["task_number"] == b and count(w) == 1


def test_r8b_machine_fence(world, monkeypatch):
    w = world
    a, b, m = lifecycle(w, monkeypatch, second=False)
    raw_state(w, a, "done")
    early = jobs.lease(w.js, m)
    monkeypatch.setattr(jobs, "_now", lambda: "2030-01-01T01:00:01Z")
    later = jobs.lease(w.js, m)
    assert early is None and later["task_number"] == b and count(w) == 1


def trust(h, body, auth=None):
    return h.request(auth or h.owner, "POST", "/api/projects/work/orchestrator/trust", body)[:2]


def trust_setup(h):
    h.connect(h.owner, "work")
    with h.db._lock:
        h.db._conn.execute(
            "CREATE TABLE IF NOT EXISTS orchestration_settings (workspace_id TEXT PRIMARY KEY,enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),updated_by TEXT NOT NULL,updated_at TEXT NOT NULL)"
        )
        h.db._conn.execute(
            "INSERT OR REPLACE INTO orchestration_settings VALUES (?,1,?,'2030-01-01T00:00:00Z')",
            (h.workspace, h.admin),
        )
    from commonplace.sessions import person_principal

    owner = person_principal(h.db, h.admin, h.workspace)
    m = registry.enroll(
        h.db, owner, {"label": "one", "projects": ["demo"], "topics": ["worker"], "wings": ["work"]}
    )
    registry.attest(
        h.db,
        owner,
        m["machine_id"],
        {
            "topics": ["worker"],
            "provider_instance_id": "worker",
            "worker_connection_id": m["worker_connection_id"],
        },
    )
    return m


def test_r9_trust(projects_hub):
    h = projects_hub
    m = trust_setup(h)
    result = trust(h, {"confirm": True, "machines": [m["machine_id"]]})
    seat = h.seats()[0]["connection_id"]
    policy = h.db.authenticate_connection(seat).policy.trust
    machine = registry.get_machine(h.db, h.workspace, m["machine_id"])
    assert (
        result[0] == 200 and policy == "trusted" and seat in json.loads(machine["requesters"])
    )  # RED
    audit = [
        r for r in h.db.audit_entries(h.workspace) if r["action"] == "project.orchestrator.trust"
    ]
    assert len(audit) == 1 and audit[0]["decision"] == "allow"
    assert audit[0]["user_id"] == h.admin and audit[0]["connection_id"] == seat
    assert audit[0]["workspace_id"] == h.workspace
    h.request(h.owner, "POST", "/api/boards/work/cards", {"title": "Start me"})
    seat_token = h.connect(h.owner, "work")[1]["token"]
    status, cards, _ = h.get(h.owner, "/api/boards/work/cards")
    task = cards["cards"][0]["number"]
    result = h.request(
        seat_token,
        "POST",
        "/api/card-starts",
        {"task": task, "machine": m["machine_id"], "project": "demo", "topic": "worker"},
    )
    assert result[0] == 201


def test_r9b_trust_authority(projects_hub):
    h = projects_hub
    m = trust_setup(h)
    bad = trust(h, {"machines": [m["machine_id"]]})[0]
    member = trust(h, {"confirm": True, "machines": [m["machine_id"]]}, h.member)[0]
    h.request(h.owner, "PUT", "/api/settings/orchestration", {"enabled": False})
    off = trust(h, {"confirm": True, "machines": [m["machine_id"]]})
    assert (bad, member, off[0]) == (400, 403, 409) and "orchestration_off" in str(off)  # RED


def test_r9c_atomic_trust(projects_hub):
    h = projects_hub
    m = trust_setup(h)
    from commonplace.sessions import person_principal

    owner = person_principal(h.db, h.admin, h.workspace)
    m2 = registry.enroll(
        h.db, owner, {"label": "two", "projects": ["demo"], "topics": ["worker"], "wings": ["work"]}
    )
    registry.revoke(h.db, owner, m2["machine_id"])
    before = dict(registry.get_machine(h.db, h.workspace, m["machine_id"]))
    result = trust(h, {"confirm": True, "machines": [m["machine_id"], m2["machine_id"]]})
    seat = h.seats()[0]["connection_id"]
    assert (
        result[0] == 409
        and h.db.authenticate_connection(seat).policy.trust == "standard"
        and dict(registry.get_machine(h.db, h.workspace, m["machine_id"])) == before
    )  # RED


def test_r9d_demote_keeps_stale_requester(projects_hub):
    h = projects_hub
    m = trust_setup(h)
    seat = h.seats()[0]["connection_id"]
    with h.db._lock:
        h.db._conn.execute(
            "UPDATE runner_machines SET requesters=? WHERE id=?",
            (json.dumps([seat, "stale-requester"]), m["machine_id"]),
        )
    result = trust(h, {"trusted": False})
    assert result[0] == 200 and json.loads(
        registry.get_machine(h.db, h.workspace, m["machine_id"])["requesters"]
    ) == ["stale-requester"]  # RED
    assert seat_start(h, m)[0] == 403


@pytest.mark.parametrize("implicit", [False, True])
def test_r9e_seat_cannot_create(world, implicit):
    w = world
    seed(w)
    seat, _ = trusted_seat(w)
    service = BoardService(w.ctx(seat))
    result = (
        call(service.task_add, "Root", board="fresh")
        if implicit
        else call(service.board_add, "fresh", wing="shared")
    )
    assert "seat_cannot_create_boards" in str(result)  # RED


def test_r9f_off_demotes(projects_hub):
    h = projects_hub
    m = trust_setup(h)
    seat = h.seats()[0]["connection_id"]
    other = h.gateway.authenticate("Bearer " + h.a1).connection_id
    trust(h, {"confirm": True, "machines": [m["machine_id"]]})
    with h.db._lock:
        h.db._conn.execute(
            "UPDATE runner_machines SET requesters=? WHERE id=?",
            (json.dumps([seat, other]), m["machine_id"]),
        )
    result = h.request(h.owner, "PUT", "/api/settings/orchestration", {"enabled": False})
    assert (
        result[0] == 200
        and h.db.authenticate_connection(seat).policy.trust == "standard"
        and json.loads(registry.get_machine(h.db, h.workspace, m["machine_id"])["requesters"])
        == [other]
    )  # RED
    assert seat_start(h, m)[0] == 403
    assert seat_start(h, m, h.a1)[0] == 201


def test_r10_configured_dedupe(world):
    w = world
    seed(w)
    for _ in range(2):
        doing(w, w.card(), "conn:orch-b")
    a = w.card()
    w.configure(max_agents=2)
    result = call(w.person_start, a)
    assert result[0] == 201  # RED


def test_g1_off_listed_requester(world):
    w = world
    assert call(tools.card_start, w.ctx(w.a), w.card(), w.machine_id, "demo", "worker")[0] == 201


def test_g2_off_no_default_cap(world):
    w = world
    assert [call(w.person_start, w.card())[0] for _ in range(5)] == [201] * 5


def test_g3_off_distinct_agents(world):
    w = world
    cap(w, 2)
    for _ in range(2):
        doing(w, w.card(), "conn:orch-b")
    assert claim(w, w.card())[0] == 200


def test_g4_off_busy_reservation(world):
    w = world
    doing(w, w.card(), "conn:orch-a")
    assert call(w.boards.task_add, "Reserve", board="acme", assignee="conn:orch-a")[0] == 200


def test_g5_configured_off(world):
    w = world
    cards = [w.card() for _ in range(2)]
    w.configure(max_agents=1)
    assert "use orch_card_start" in str(
        call(tools.card_start, w.ctx(w.a), cards[0], w.machine_id, "demo", "worker")
    )
    assert w.person_start(cards[0])[0] == 201
    assert "cap" in str(call(w.person_start, cards[1]))


def test_g6_no_public_routes(projects_hub):
    h = projects_hub
    assert h.get(None, "/api/settings/orchestration")[0] != 200
    assert trust(h, {"trusted": False}, h.a1)[0] != 200


def test_g7_private_hygiene():
    from pathlib import Path

    paths = [
        Path(p)
        for p in (
            "MODULES.md",
            "SYSTEM.md",
            "commonplace/boards/service.py",
            "commonplace/orch/runner_bridge.py",
            "commonplace/projects/__init__.py",
            "commonplace/projects/connect.py",
            "commonplace/projects/limits.py",
            "commonplace/projects/orchestration.py",
            "commonplace/projects/routes.py",
            "commonplace/runner/jobs.py",
            "commonplace/runner/registry.py",
            "commonplace/runner/runner_api.py",
            "commonplace/runner/tools.py",
            "commonplace/web/static/app.js",
            "commonplace/web/static/projects.js",
            "docs/projects.md",
            "docs/runner.md",
            "tests/_phase_b_helpers.py",
            "tests/test_commonplace_public_surface.py",
            "tests/test_projects_admission.py",
            "tests/test_projects_phase_b.py",
        )
    ]
    forbidden = [
        "jeff" + "rey",
        "edge" + "1",
        "t3" + "code",
        "/" + "home/",
        "cmmn" + ".dev",
        "http" + ".client",
        "urllib" + ".request",
    ]
    assert all(not any(s in p.read_text() for s in forbidden) for p in paths if p.exists())


def test_g8_no_hub_splitting():
    import ast
    from pathlib import Path

    for p in Path("commonplace/projects").glob("*.py"):
        tree = ast.parse(p.read_text())
        assert not any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "task_add"
            for n in ast.walk(tree)
        )


def test_g9_off_json_and_arguments(world):
    w = world
    assert set(board(w)) == {
        "slug",
        "name",
        "wing",
        "coordinator",
        "can_configure",
        "counts",
        "agents_working",
    }
    with pytest.raises((ToolError, TypeError)):
        w.boards.task_add("Blocked", board="acme", blocked_by=[1])


def test_g10_off_claim_order(world):
    w = world
    cards = [w.card() for _ in range(2)]
    assert BoardService(w.ctx(w.a)).task_take_next(board="acme")["number"] == cards[0]


def test_g11_off_row_limit(world):
    w = world
    cap(w, 2)
    doing(w, w.card(), "conn:first")
    doing(w, w.card(), "conn:second")
    assert claim(w, w.card())[0] == 409


def test_g12_absent_table_off(world):
    w = world
    with w.db._lock:
        w.db._conn.execute("DROP TABLE IF EXISTS orchestration_settings")
    assert call(tools.card_start, w.ctx(w.a), w.card(), w.machine_id, "demo", "worker")[0] == 201


def test_g13_overlap_dedupe(world):
    w = world
    seed(w)
    a = w.card()
    w.person_start(a)
    with w.js.transaction() as conn:
        conn.execute("UPDATE runner_jobs SET state='leased' WHERE task_number=?", (a,))
    doing(w, a, "conn:orch-a")
    if "used" in board(w):
        used = count(w)
    else:
        from types import SimpleNamespace

        from commonplace.orch.runner_bridge import _cap_count

        with w.js.connection() as conn:
            used = _cap_count(conn, SimpleNamespace(board="acme", doing={a}), jobs._now())
    assert used == 1


def test_g14_existing_agent_and_own_worker_card(world):
    w = world
    seed(w)
    cap(w, 1)
    a = w.card()
    doing(w, a, "conn:orch-a")
    assert claim(w, w.card())[0] == 200
    raw_state(w, a, "done")
    for row in w.boards.task_list(board="acme"):
        raw_state(w, row["number"], "done")
    b = w.card()
    w.person_start(b)
    worker = w.db.authenticate_connection(w.machine()["worker_connection_id"])
    assert claim(w, b, worker)[0] == 200


def test_g15_wait_is_not_owner_work(world):
    w = world
    a, b = w.card(), w.card()
    with w.boards.store.transaction() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS task_blockers(task_id INTEGER,blocker_id INTEGER,created_by TEXT,created_at TEXT,PRIMARY KEY(task_id,blocker_id))"
        )
        conn.execute(
            "INSERT INTO task_blockers VALUES (?,?,?,?)", (b, a, w.owner.actor, jobs._now())
        )
    # An ordinary todo card waiting for another card is not an owner decision.
    groups = w.boards.needs_me()
    assert all(
        b not in {c["number"] for c in value}
        for value in groups.values()
        if isinstance(value, list)
    )


def test_head_seed_schema_matches_module():
    import sqlite3

    from commonplace.projects.orchestration import SCHEMA
    from tests._phase_b_helpers import SEED_DDL

    definitions = []
    for ddl in (SEED_DDL, SCHEMA):
        with sqlite3.connect(":memory:") as conn:
            conn.execute(ddl)
            definitions.append(conn.execute("PRAGMA table_info(orchestration_settings)").fetchall())
    assert definitions[0] == definitions[1]


def test_head_connect_hint_and_trust_failures(projects_hub):
    h = projects_hub
    assert h.connect(h.owner, "work")[1]["setup"]["webhook_add"]["events"] == "space_message"
    m = trust_setup(h)
    connected = h.connect(h.owner, "work")[1]
    assert connected["setup"]["webhook_add"]["events"] == "space_message,card.created"
    assert trust(h, {"confirm": True, "machines": ["mach_0000000000000000"]})[0] == 404
    from commonplace.sessions import person_principal

    owner = person_principal(h.db, h.admin, h.workspace)
    registry.patch(h.db, owner, m["machine_id"], {"enabled": False})
    assert trust(h, {"confirm": True, "machines": [m["machine_id"]]})[0] == 409
    registry.patch(h.db, owner, m["machine_id"], {"enabled": True})
    assert trust(h, {"confirm": True, "machines": [m["machine_id"]]})[0] == 200
    seat = h.seats()[0]["connection_id"]
    registry.revoke(h.db, owner, m["machine_id"])
    assert h.request(h.owner, "PUT", "/api/settings/orchestration", {"enabled": False})[0] == 200
    machine = registry.get_machine(h.db, h.workspace, m["machine_id"])
    assert seat not in json.loads(machine["requesters"])
    assert h.db.authenticate_connection(seat).policy.trust == "standard"


def test_head_orchestration_controls():
    import shutil
    import subprocess

    script = r"""
import assert from "node:assert/strict";
import {mountWorkspaceOrchestration, mountOrchestratorTrust, mountAgentLimit} from "./commonplace/web/static/projects.js";
class Host {
  isConnected = true;
  set innerHTML(html) {
    this.html = html;
    this.nodes = [...html.matchAll(/<(input|button|p|output)\b([^>]*)>/g)].map(([,tag,attrs]) => {
      const a = Object.fromEntries([...attrs.matchAll(/([\w-]+)(?:="([^"]*)")?/g)].map(m => [m[1],m[2] ?? ""]));
      return {a, tag, checked: "checked" in a, value: a.value || "", disabled: "disabled" in a, textContent: "", getAttribute(n) { return this.a[n]; }};
    });
  }
  querySelectorAll(selector) {
    const m = selector.match(/^\[([\w-]+)(?:=([\w-]+))?\](:checked)?$/);
    assert.ok(m, selector);
    return this.nodes.filter(n => m[1] in n.a && (!m[2] || n.a[m[1]] === m[2]) && (!m[3] || n.checked));
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  replaceChildren() { this.innerHTML = ""; }
}
const e = s => String(s).replaceAll("<", "&lt;");
const host = new Host(), calls = [];
mountWorkspaceOrchestration(host, {e, enabled:false, api:async(p,o) => {calls.push([p,o]); throw Error("offline");}});
assert.equal(host.querySelector("[data-orchestration-enabled]").checked, false);
await host.querySelector("[data-orchestration-enabled]").onchange({target:{checked:true}});
assert.equal(calls.length,1);
assert.equal(calls[0][1].body.enabled,true);
assert.equal(host.querySelector("[data-orchestration-enabled]").checked,false);
assert.match(host.html,/Could not save/);
calls.length=0;
const project={board:"atlas",can_configure:true,main:{connected:true},limits:{max_agents:null},orchestration:{trusted:false,default_max_agents:3,machines:[{id:"one",label:"<runner>"}]}};
mountOrchestratorTrust(host,{project,e,api:async(p,o)=>{calls.push([p,o]);return {trusted:o.body.trusted !== false};}});
assert.match(host.html,/&lt;runner>/);
await host.querySelector("[data-trust-grant]").onclick();
assert.equal(calls.length,0);
host.querySelector("[data-trust-machine]").checked=true;
host.querySelector("[data-trust-confirm]").checked=true;
await host.querySelector("[data-trust-grant]").onclick();
assert.equal(calls.length,1);
assert.deepEqual(calls[0][1].body,{confirm:true,machines:["one"]});
await host.querySelector("[data-trust-remove]").onclick();
assert.deepEqual(calls[1][1].body,{trusted:false});
mountAgentLimit(host,{project,e,api:async()=>assert.fail("render must not save")});
assert.match(host.html,/>3<\/output>/);
assert.equal(host.querySelector("[data-limit-clear]"),null);
"""
    result = subprocess.run(
        [shutil.which("node"), "--input-type=module"],
        input=script,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_head_admission_busy_is_bounded(world, monkeypatch):
    import sqlite3
    from contextlib import contextmanager

    from commonplace.projects import orchestration

    @contextmanager
    def locked():
        raise sqlite3.OperationalError("database is locked")
        yield  # pragma: no cover

    monkeypatch.setattr(world.js, "transaction", locked)
    with pytest.raises(orchestration.AdmissionRefused, match="busy"):
        with orchestration.admission(world.gw, world.ws, store=world.js):
            pytest.fail("a locked store must not admit")


def seat_start(h, machine, auth=None):
    token = auth or h.connect(h.owner, "work")[1]["token"]
    _, card, _ = h.request(h.owner, "POST", "/api/boards/work/cards", {"title": "Start me"})
    return h.request(
        token,
        "POST",
        "/api/card-starts",
        {
            "task": card["number"],
            "machine": machine["machine_id"],
            "project": "demo",
            "topic": "worker",
        },
    )[:2]


def test_person_claim_exempt_at_project_cap(world):
    w = world
    seed(w)
    cap(w, 1)
    doing(w, w.card(), "conn:orch-a")
    card = w.card()
    result = claim(w, card, w.owner)
    assert result[0] == 200 and result[1]["number"] == card
    with w.boards.store.connection() as conn:
        row = conn.execute("SELECT status,assignee FROM tasks WHERE id=?", (card,)).fetchone()
        assert tuple(row) == ("doing", w.owner.actor)
    assert count(w) == 1


def test_lease_skips_capped_project(world):
    w = world
    seed(w)
    registry.patch(w.db, w.owner, w.machine_id, {"projects": ["demo", "other"]})
    a, b = w.card(), w.card(board="plain")
    w.person_start(a)
    tools.card_start(w.ctx(w.owner), b, w.machine_id, "other", "worker")
    cap(w, 1)
    doing(w, w.card(), "conn:orch-a")
    before = w.jobs_for(a)[0]
    result = jobs.lease(w.js, w.machine())
    assert result is not None and result["task_number"] == b
    assert result["project"] == "other" and result["state"] == "leased"
    assert w.jobs_for(a)[0] == before


def test_member_can_get_orchestration(projects_hub):
    h = projects_hub
    assert h.request(h.owner, "PUT", "/api/settings/orchestration", {"enabled": True})[0] == 200
    assert h.get(h.member, "/api/settings/orchestration")[:2] == (200, {"enabled": True})
    assert h.request(h.member, "PUT", "/api/settings/orchestration", {"enabled": False})[0] == 403
    assert h.get(h.member, "/api/settings/orchestration")[1] == {"enabled": True}


def test_trust_lock_timeout_is_busy_and_atomic(projects_hub, monkeypatch):
    import sqlite3
    from time import monotonic

    h = projects_hub
    m = trust_setup(h)
    seat = h.seats()[0]["connection_id"]
    before_machine = dict(registry.get_machine(h.db, h.workspace, m["machine_id"]))
    before_policy = h.db.authenticate_connection(seat).policy
    before_audit = [
        r for r in h.db.audit_entries(h.workspace) if r["action"] == "project.orchestrator.trust"
    ]

    def locked(*args, **kwargs):
        # Fail after the seat policy write, proving the transaction rolls it back.
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(registry, "patch_in", locked)
    started = monotonic()
    result = trust(h, {"confirm": True, "machines": [m["machine_id"]]})
    assert result == (409, {"error": "busy", "code": "busy"})
    assert monotonic() - started < 2
    assert h.db.authenticate_connection(seat).policy == before_policy
    assert dict(registry.get_machine(h.db, h.workspace, m["machine_id"])) == before_machine
    assert [
        r for r in h.db.audit_entries(h.workspace) if r["action"] == "project.orchestrator.trust"
    ] == before_audit


def test_off_runner_lock_timeout_returns_busy(world, monkeypatch):
    import sqlite3
    from contextlib import contextmanager
    from time import monotonic

    w = world
    seed(w, False)
    card = w.card()
    w.person_start(card)
    payload = w.lease()
    before = w.all_jobs()

    @contextmanager
    def locked(store):
        # Simulate SQLite's bounded BEGIN IMMEDIATE lock timeout.
        raise sqlite3.OperationalError("database is locked")
        yield  # pragma: no cover

    monkeypatch.setattr(jobs.JobStore, "transaction", locked)
    began = monotonic()
    result = w.machine_call("starting", {"lease_token": payload["lease_token"]})
    assert result == (409, {"error": "busy"})
    assert monotonic() - began < 2
    assert w.all_jobs() == before
    with w.boards.store.connection() as conn:
        assert conn.execute("SELECT status FROM tasks WHERE id=?", (card,)).fetchone()[0] == "todo"


def test_non_cap_lease_refusal_returns_no_content(world, monkeypatch):
    from commonplace.projects import orchestration

    w = world
    seed(w)
    w.person_start(w.card())
    before = w.all_jobs()

    def refused(*args, **kwargs):
        raise orchestration.AdmissionRefused("board_not_found")

    monkeypatch.setattr(orchestration, "check_start", refused)
    assert w.machine_call("lease") == (204, "")
    assert w.all_jobs() == before


def test_trust_initial_seat_lock_timeout_returns_busy(projects_hub, monkeypatch):
    import sqlite3
    from time import monotonic

    from commonplace.projects import seats

    h = projects_hub
    m = trust_setup(h)
    seat = h.seats()[0]["connection_id"]
    before_machine = dict(registry.get_machine(h.db, h.workspace, m["machine_id"]))
    before_policy = h.db.authenticate_connection(seat).policy
    before_audit = [
        r for r in h.db.audit_entries(h.workspace) if r["action"] == "project.orchestrator.trust"
    ]

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(seats, "ensure_main", locked)
    began = monotonic()
    result = trust(h, {"confirm": True, "machines": [m["machine_id"]]})
    assert result == (409, {"error": "busy", "code": "busy"})
    assert monotonic() - began < 2
    assert h.db.authenticate_connection(seat).policy == before_policy
    assert dict(registry.get_machine(h.db, h.workspace, m["machine_id"])) == before_machine
    assert [
        r for r in h.db.audit_entries(h.workspace) if r["action"] == "project.orchestrator.trust"
    ] == before_audit


def test_lease_checks_capped_project_once(world, monkeypatch):
    from commonplace.projects import orchestration

    w = world
    seed(w)
    registry.patch(w.db, w.owner, w.machine_id, {"projects": ["demo", "other"]})
    for _ in range(2):
        w.person_start(w.card())
    eligible = w.card(board="plain")
    tools.card_start(w.ctx(w.owner), eligible, w.machine_id, "other", "worker")
    cap(w, 1)
    doing(w, w.card(), "conn:orch-a")
    checked = []
    check = orchestration.check_start

    def counted(gateway, ws, conn, slug, *args, **kwargs):
        checked.append(slug)
        return check(gateway, ws, conn, slug, *args, **kwargs)

    monkeypatch.setattr(orchestration, "check_start", counted)
    result = jobs.lease(w.js, w.machine())
    assert result is not None and result["task_number"] == eligible
    assert checked == ["acme", "plain"]


def test_capped_board_does_not_block_shared_runner_project(world):
    w = world
    seed(w)
    capped = [w.card(), w.card()]
    for card in capped:
        w.person_start(card)
    eligible = w.card(board="plain")
    w.person_start(eligible)
    cap(w, 1)
    doing(w, w.card(), "conn:orch-a")
    before = [w.jobs_for(card)[0] for card in capped]
    result = jobs.lease(w.js, w.machine())
    assert result is not None and result["task_number"] == eligible
    assert result["board_slug"] == "plain" and result["project"] == "demo"
    assert [w.jobs_for(card)[0] for card in capped] == before


def test_runner_non_lock_operational_error_stays_500(world, monkeypatch):
    import sqlite3

    w = world
    seed(w, False)
    w.person_start(w.card())
    before = w.all_jobs()

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("no such column: missing")

    monkeypatch.setattr(jobs, "lease", broken)
    status, body = w.machine_call("lease")
    assert status == 500 and body == {"error": "internal error"}
    assert w.all_jobs() == before


def test_project_non_lock_operational_error_stays_500(projects_hub, monkeypatch):
    import sqlite3

    from commonplace.projects import seats

    h = projects_hub
    machine = trust_setup(h)
    seat = h.seats()[0]["connection_id"]
    before = dict(registry.get_machine(h.db, h.workspace, machine["machine_id"]))
    before_policy = h.db.authenticate_connection(seat).policy

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("no such table: missing")

    monkeypatch.setattr(seats, "ensure_main", broken)
    assert trust(h, {"confirm": True, "machines": [machine["machine_id"]]}) == (
        500,
        {"error": "internal error"},
    )
    assert dict(registry.get_machine(h.db, h.workspace, machine["machine_id"])) == before
    assert h.db.authenticate_connection(seat).policy == before_policy
