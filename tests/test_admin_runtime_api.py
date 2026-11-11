"""Read-only runtime, effective limits and stuck facts at a fixed request instant."""

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest

from commonplace import sessions, webhooks
from commonplace.boards import store as boards
from commonplace.projects import limits, orchestration, seats
from commonplace.runner import jobs
from mempalace.sqlite_crypto import connect, row_factory
from tests._commonplace_helpers import hub_fixture as hub_fixture
from tests.test_admin_api import DENIED, agent, clock, cookie, get, member, snapshot
from tests.test_commonplace_web import request
from tests.test_orch_store import config_text

NOW = "2026-11-07T12:00:00Z"
OLD = "2026-11-07T10:00:00Z"
FUTURE = "2026-11-07T13:00:00Z"


@pytest.fixture
def runtime(hub):
    clock(hub)
    path = Path(hub.supervisor.workspace_dir(hub.workspace))
    store = boards.BoardStore(path)
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO boards(id,slug,name,wing,stamp_user,stamp_trust,created_at,coordinator) "
            "VALUES (1,'alpha','Alpha','notes',?,'trusted',?,'conn:lead')",
            (hub.admin, OLD),
        )
        conn.execute(
            "INSERT INTO boards(id,slug,name,wing,stamp_user,stamp_trust,created_at) "
            "VALUES (2,'beta','Beta','notes',?,'trusted',?)",
            (hub.admin, OLD),
        )
        conn.execute(
            "INSERT INTO boards(id,slug,name,wing,stamp_user,stamp_trust,created_at,archived) "
            "VALUES (3,'archived','Archived','notes',?,'trusted',?,1)",
            (hub.admin, OLD),
        )
    return store, path


def task(
    runtime,
    number,
    *,
    board=1,
    status="doing",
    assignee="conn:worker",
    claim=None,
    heartbeat=OLD,
    parent=None,
    ask=None,
):
    with runtime[0].connection() as conn:
        conn.execute(
            "INSERT INTO tasks(id,board_id,title,status,position,assignee,claim_until,heartbeat_at,"
            "parent_number,ask,stamp_user,stamp_trust,created_at,updated_at,updated_by) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,'synthetic','trusted',?,?,'synthetic')",
            (
                number,
                board,
                f"Task {number}",
                status,
                number,
                assignee,
                claim,
                heartbeat,
                parent,
                ask,
                OLD,
                OLD,
            ),
        )


def runner(runtime):
    conn = connect(str(runtime[1] / "runner.sqlite3"), isolation_level=None)
    conn.row_factory = row_factory
    conn.executescript(jobs.SCHEMA)
    return conn


def job(conn, number, state="leased", lease=OLD, started=None, board="alpha"):
    states = ("leased", "starting", "uncertain", "queued", "started", "done", "failed", "cancelled")
    conn.execute(
        "INSERT INTO runner_jobs(id,task_number,board_slug,machine_id,project,topic,grant_version,"
        "runtime_mode,requested_by,state,lease_until,lease_token,started_at,created_at,updated_at) "
        "VALUES (?,?,?,'mach_0000000000000001','project','build',1,'approval-required','synthetic',"
        "?,?,?,?,?,?)",
        (
            f"job_{number * 16 + states.index(state):016x}",
            number,
            board,
            state,
            lease,
            "synthetic-lease-canary",
            started,
            OLD,
            OLD,
        ),
    )


def machine(hub, n=1, **updates):
    mid = f"mach_{n:016x}"
    mc, mh = agent(hub, f"machine-{n}", last_used_at=OLD)
    wc, wh = agent(hub, f"worker-{n}", last_used_at="2026-11-07T11:59:00Z")
    hub.db._conn.execute(
        "INSERT INTO runner_machines(id,workspace_id,label,machine_connection_id,worker_connection_id,"
        "projects,topics,wings,enrolled_by,updated_by,created_at,updated_at) "
        "VALUES (?,?,?,?,?,'[]','[]','[]',?,?,?,?)",
        (mid, hub.workspace, f"Machine {n}", mc, wc, hub.admin, hub.admin, OLD, OLD),
    )
    for cid, kind in ((mc, "machine"), (wc, "worker")):
        hub.db._conn.execute(
            "INSERT INTO runner_connections(connection_id,machine_id,kind) VALUES (?,?,?)",
            (cid, mid, kind),
        )
    for key, value in updates.items():
        if key not in {"enabled", "revoked_at", "binding_state", "label"}:
            raise RuntimeError("Invalid synthetic machine field")
        hub.db._conn.execute(f"UPDATE runner_machines SET {key}=? WHERE id=?", (value, mid))
    return mid, mc, wc, mh, wh


def switch(hub, enabled=True):
    hub.db._conn.execute(
        "INSERT OR REPLACE INTO orchestration_settings VALUES (?,?,?,?)",
        (hub.workspace, int(enabled), hub.admin, NOW),
    )


def webhook(hub, cid, *, name="lead", board="", kinds='["*"]', ws=None, key="wake"):
    hub.db._conn.execute(
        "INSERT INTO webhooks(id,workspace_id,connection_id,owner_actor,url,secret_hash,secret,"
        "kinds,board,created_at) VALUES (?,?,?,?, 'https://example.invalid','synthetic','synthetic',?,?,?)",
        (key, ws or hub.workspace, cid, "conn:" + name, kinds, board, OLD),
    )


def logical(runtime, hub):
    values = []
    for path in (Path(hub.db.path), runtime[1] / "boards.sqlite3", runtime[1] / "runner.sqlite3"):
        if not path.exists():
            values.append(None)
            continue
        conn = connect(str(path)) if path.name != "control.sqlite3" else sqlite3.connect(path)
        try:
            values.append(
                (
                    list(conn.execute("SELECT sql FROM sqlite_master ORDER BY name")),
                    {
                        r[0]: [tuple(x) for x in conn.execute(f'SELECT * FROM "{r[0]}"')]
                        for r in conn.execute(
                            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                        )
                    },
                )
            )
        finally:
            conn.close()
    return values, snapshot(hub)[2]


def wrong_wing_policy():
    from commonplace.policy import Policy

    return Policy.build(scopes=["memory:read"], read_wings=["other"]).to_json()


def test_runtime_orchestrators(hub, runtime, monkeypatch):
    lead, _ = agent(hub, "lead", expires_at=FUTURE, last_used_at="2026-11-07T11:59:00Z")
    webhook(hub, lead)
    switch(hub)
    hub.db._conn.execute("DELETE FROM project_orchestrators WHERE workspace_id=?", (hub.workspace,))
    hub.db._conn.execute(
        "INSERT INTO project_orchestrators VALUES (?, 'beta','main','unheld',NULL,?,?)",
        (hub.workspace, OLD, OLD),
    )
    headers = cookie(hub)
    before = logical(runtime, hub)
    status, data, _ = get(hub, "runtime", headers)
    assert status == 200
    assert data["orchestration"] == {"state": "ready", "enabled": True}
    items = data["orchestrators"]["items"]
    assert [r["slug"] for r in items] == ["alpha", "beta"]
    assert items[0]["source"] == "coordinator" and items[0]["connected"] is True
    assert items[0]["activity"] == "working" and items[0]["wakeable"] is True
    assert items[1]["source"] == "seat" and items[1]["connected"] is False
    assert items[1]["wakeable"] is False
    assert logical(runtime, hub) == before
    from commonplace import control_db
    from commonplace.boards import service
    from commonplace.chat import front
    from commonplace.console import authority
    from commonplace.runner import registry
    from commonplace.web import admin

    principal = sessions.session_principal(hub.db, sessions.cookie_value(headers["Cookie"]))

    def forbidden(*a, **kw):
        raise AssertionError("Forbidden write/lock wrapper from Admin")

    class NoLock:
        def __enter__(self):
            forbidden()

        def __exit__(self, *args):
            pass

    resolver = hub.supervisor.workspace_dir
    resolved = []

    def existing_path(ws):
        assert ws == hub.workspace
        path = resolver(ws)
        assert Path(path) == runtime[1]
        resolved.append(ws)
        return path

    with monkeypatch.context() as patch:
        patch.setattr(hub.supervisor, "workspace_dir", existing_path)
        patch.setattr(hub.gateway, "principal_for", lambda h: principal)
        patch.setattr(hub.db, "_lock", NoLock())
        patch.setattr(hub.gateway, "workspace_dir", forbidden)
        for module, names in (
            (service, ["BoardService", "open_store"]),
            (boards, ["BoardStore"]),
            (jobs, ["JobStore", "open_store"]),
            (seats, ["ensure_main", "main", "active_connection", "ensure_schema"]),
            (front, ["orchestrator"]),
            (authority, ["agent", "agents"]),
            (webhooks, ["has_space_subscription", "_can_receive", "ensure_schema"]),
            (orchestration, ["enabled", "workers", "ensure_schema"]),
            (registry, ["machines", "ensure"]),
            (
                control_db.ControlDB,
                [
                    "workspace",
                    "workspaces",
                    "members",
                    "users",
                    "user",
                    "connection",
                    "connections",
                    "authenticate_connection",
                ],
            ),
        ):
            for name in names:
                patch.setattr(module, name, forbidden)
        assert get(hub, "runtime", headers)[1] == data
        for section in ("limits", "stuck"):
            assert get(hub, section, headers)[0] == 200
        context = admin.ReadContext(hub.gateway.admin_clock)
        with admin.read_store(hub.gateway, hub.workspace, "boards.sqlite3", context) as conn:
            with pytest.raises(Exception) as exc:
                conn.execute("DELETE FROM boards")
            assert "readonly" in str(exc.value).lower() or "read-only" in str(exc.value).lower()
    assert resolved, "The approved generation-aware resolver must be exercised"
    assert logical(runtime, hub) == before
    # Current subscription eligibility, not subscription existence, controls wakeability.
    for field, bad, good in (
        (
            "policy",
            wrong_wing_policy(),
            hub.db._conn.execute("SELECT policy FROM connections WHERE id=?", (lead,)).fetchone()[
                0
            ],
        ),
        ("expires_at", OLD, FUTURE),
        ("revoked_at", OLD, None),
    ):
        hub.db._conn.execute(f"UPDATE connections SET {field}=? WHERE id=?", (bad, lead))
        assert get(hub, "runtime", headers)[1]["orchestrators"]["items"][0]["wakeable"] is False
        hub.db._conn.execute(f"UPDATE connections SET {field}=? WHERE id=?", (good, lead))
    hub.db._conn.execute("UPDATE webhooks SET board='beta'")
    assert get(hub, "runtime", headers)[1]["orchestrators"]["items"][0]["wakeable"] is False
    hub.db._conn.execute("UPDATE webhooks SET board='alpha',kinds='[\"space_message\"]'")
    assert get(hub, "runtime", headers)[1]["orchestrators"]["items"][0]["wakeable"] is True
    hub.db._conn.execute("UPDATE users SET disabled_at=? WHERE id=?", (OLD, hub.admin))
    # Keep the owner session usable by making the inspected coordinator a different user.
    hub.db._conn.execute("UPDATE users SET disabled_at=NULL WHERE id=?", (hub.admin,))
    uid = member(hub)
    hub.db._conn.execute("UPDATE connections SET user_id=? WHERE id=?", (uid, lead))
    hub.db._conn.execute("UPDATE users SET disabled_at=? WHERE id=?", (OLD, uid))
    assert get(hub, "runtime", headers)[1]["orchestrators"]["items"][0]["connected"] is False


def test_runtime_machines(hub, runtime):
    rows = [
        machine(hub),
        machine(hub, 2, enabled=0),
        machine(hub, 3, revoked_at=OLD),
        machine(
            hub,
            4,
            binding_state="owner_attested",
            label="<img> cmmn_session=synthetic-redaction-canary",
        ),
    ]
    headers = cookie(hub)
    status, data, _ = get(hub, "runtime", headers)
    assert status == 200
    found = data["machines"]["items"]
    assert len(found) == 4
    expected_keys = {
        "id",
        "label",
        "enabled",
        "revoked_at",
        "binding_state",
        "verified",
        "machine_last_used_at",
        "worker_last_used_at",
        "activity",
        "worker_activity",
        "max_in_flight",
        "max_starts_per_hour",
        "created_at",
        "requesters",
        "requesters_total",
        "grant_version",
    }
    assert all(set(row) == expected_keys for row in found)
    assert all(r["verified"] is False for r in found)
    assert found[0]["machine_last_used_at"] == OLD
    assert found[0]["worker_last_used_at"] == "2026-11-07T11:59:00Z"
    assert found[0]["activity"] == "quiet" and found[0]["worker_activity"] == "working"
    assert [r["enabled"] for r in found] == [True, False, True, True]
    assert found[2]["revoked_at"] == OLD and found[3]["binding_state"] == "owner_attested"
    assert "synthetic-redaction-canary" not in json.dumps(data)
    assert all("token" not in k and "connection_id" not in k for r in found for k in r)
    # Machine credentials are not visible orchestrators.
    with runtime[0].connection() as conn:
        conn.execute("UPDATE boards SET coordinator='conn:machine-1' WHERE id=1")
    assert get(hub, "runtime", headers)[1]["orchestrators"]["items"][0]["connected"] is False
    for auth, expected in ((rows[0][3], 404), (rows[0][4], 403)):
        assert get(hub, "runtime", auth)[0] == expected


def test_limits_orchestration_off(hub, runtime):
    task(runtime, 1)
    task(runtime, 2)
    task(runtime, 3, assignee="conn:other")
    task(runtime, 4, board=3)
    with runtime[0].connection() as conn:
        limits.put(conn, 1, {"max_agents": 1})
    status, data, _ = get(hub, "limits", cookie(hub))
    assert status == 200
    assert data["orchestration_enabled"] is False
    assert (
        data["workspace_max_agents"] is None and data["workspace_limit_source"] == "not_configured"
    )
    assert [
        (
            r["slug"],
            r["configured_max_agents"],
            r["effective_max_agents"],
            r["used"],
            r["counting_mode"],
        )
        for r in data["projects"]
    ] == [
        ("alpha", 1, 1, 2, "distinct_doing_agents"),
        ("beta", None, None, 0, "distinct_doing_agents"),
    ]


def test_limits_orchestration_on(hub, runtime):
    switch(hub)
    machine(hub, enabled=0)
    task(runtime, 1, assignee="conn:worker-1")
    task(runtime, 2, assignee="conn:worker-1")
    task(runtime, 3, assignee="conn:ordinary")
    task(runtime, 4, assignee="conn:ordinary")
    task(runtime, 5, status="todo")
    task(runtime, 6, status="todo")
    task(runtime, 7, status="todo")
    with runtime[0].connection() as conn:
        limits.put(conn, 1, {"max_agents": 2})
    with runner(runtime) as conn:
        job(conn, 1)
        job(conn, 3)
        job(conn, 5, "queued")
        job(conn, 6, "started", started="2026-11-07T11:30:00Z")
        job(conn, 7, "started", started=OLD)
        conn.executescript(Path("commonplace/orch/schema.sql").read_text())
        body = config_text(board="alpha", max_agents=1)
        conn.execute(
            "INSERT INTO orch_configs(board,version,channel,status,sha256,body,proposed_by,proposer_trust,created_at) "
            "VALUES ('alpha',1,'live','accepted',?,?,'synthetic','trusted',?)",
            (hashlib.sha256(body.encode()).hexdigest(), body, OLD),
        )
        proposed = config_text(board="beta", max_agents=1)
        conn.execute(
            "INSERT INTO orch_configs(board,version,channel,status,sha256,body,proposed_by,proposer_trust,created_at) "
            "VALUES ('beta',1,'live','proposed',?,?,'synthetic','trusted',?)",
            (hashlib.sha256(proposed.encode()).hexdigest(), proposed, OLD),
        )
    status, data, _ = get(hub, "limits", cookie(hub))
    assert status == 200
    first, second = data["projects"]
    assert (first["used"], first["effective_max_agents"], first["accepted_config_max_agents"]) == (
        5,
        1,
        1,
    )
    assert first["counting_mode"] == "orchestration_slots"
    assert second["effective_max_agents"] == 3 and second["accepted_config_max_agents"] is None
    with runtime[0].connection() as bc, connect(str(runtime[1] / "runner.sqlite3")) as rc:
        rc.row_factory = row_factory
        used = orchestration.usage(
            bc, rc, board_id=1, board_slug="alpha", workers={"conn:worker-1"}, now=NOW
        )
    assert (first["used"], first["effective_max_agents"]) == (used.used, used.cap)


def test_stuck_expired_lease(hub, runtime):
    task(runtime, 1, claim=OLD)
    task(runtime, 2, claim=FUTURE, heartbeat=NOW)
    task(runtime, 3, claim=NOW)
    with runner(runtime) as conn:
        job(conn, 1)
        job(conn, 2, lease=FUTURE)
        job(conn, 3, "starting", lease=NOW)
        job(conn, 4, "started", lease=OLD, started=OLD)
    headers = cookie(hub)
    before = logical(runtime, hub)
    status, data, _ = get(hub, "stuck", headers)
    assert status == 200
    assert [r["number"] for r in data["cards"]] == [1, 3]
    assert data["reason_counts"]["expired_lease"] == 2
    assert data["cards"][0]["expired_jobs"] == [
        {"job_id": "job_0000000000000010", "state": "leased", "lease_until": OLD}
    ]
    assert data["cards"][1]["open_url"] == "#/card/3"
    assert "lease_token" not in json.dumps(data) and "synthetic-lease-canary" not in json.dumps(
        data
    )
    assert logical(runtime, hub) == before


def test_stuck_blockers(hub, runtime):
    switch(hub)
    task(runtime, 1, status="todo", heartbeat=NOW)
    task(runtime, 2, status="done")
    task(runtime, 3, status="todo", parent=1)
    with runtime[0].connection() as conn:
        conn.executemany(
            "INSERT INTO task_blockers VALUES (?,?,'synthetic',?)", [(3, 1, OLD), (3, 2, OLD)]
        )
    status, data, _ = get(hub, "stuck", cookie(hub))
    assert status == 200
    assert data["blockers_enforced"] is True
    assert [r["number"] for r in data["cards"]] == [3]
    assert data["cards"][0]["active_blockers"] == [{"number": 1, "title": "Task 1"}]
    switch(hub, False)
    assert get(hub, "stuck", cookie(hub))[1]["blockers_enforced"] is False


def test_stuck_waiting_and_stale(hub, runtime):
    from commonplace.boards.display import stale_before
    from commonplace.boards.service import _stuck
    from tests.test_admin_api import FIXED_NOW

    task(runtime, 1, claim=OLD)
    task(runtime, 2, assignee="user:synthetic", claim=None)
    task(runtime, 3, status="review", ask="A decision")
    task(runtime, 4, status="done")
    with runtime[0].connection() as conn:
        assert _stuck(
            dict(conn.execute("SELECT * FROM tasks WHERE id=1").fetchone()), stale_before(FIXED_NOW)
        )
    status, data, _ = get(hub, "stuck", cookie(hub))
    assert status == 200
    assert data["count"] == 3
    assert data["cards"][0]["reasons"] == ["expired_lease", "stale_agent"]
    assert all(r["reasons"] == ["waiting_on_person"] for r in data["cards"][1:])
    assert data["reason_counts"] == {
        "expired_lease": 1,
        "blocked": 0,
        "waiting_on_person": 2,
        "stale_agent": 1,
    }


def test_optional_stores_no_initialization(hub, runtime):
    headers = cookie(hub)
    switch(hub)
    status, data, _ = get(hub, "runtime", headers)
    assert status == 200
    assert data["orchestrators"]["items"][1]["source"] == "not_configured"
    task(runtime, 1)
    assert get(hub, "limits", headers)[1]["projects"][0]["used"] == 1
    assert not (runtime[1] / "runner.sqlite3").exists()
    assert get(hub, "stuck", headers)[0] == 200
    (runtime[1] / "runner.sqlite3").write_bytes(b"synthetic-corrupt-store")
    for section in ("limits", "stuck"):
        status, data, _ = get(hub, section, headers)
        assert status == 503 and data == {"error": f"{section.title()} unavailable; try again"}
    (runtime[1] / "runner.sqlite3").unlink()
    with runtime[0].connection() as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    for suffix in ("", "-wal", "-shm"):
        (runtime[1] / ("boards.sqlite3" + suffix)).unlink(missing_ok=True)
    before = snapshot(hub)
    for section in ("runtime", "limits", "stuck"):
        assert get(hub, section, headers)[0] == 200
    assert snapshot(hub) == before


def test_all_workspace_sections_authorization(hub, runtime, monkeypatch):
    headers = cookie(hub)
    status, _, _ = get(hub, "runtime", headers)
    assert status == 200
    member(hub)
    member_cookie = cookie(hub, "member@example.com")
    _, pat = agent(hub, "owner-agent")
    other = hub.db.create_workspace("also-owned", hub.admin)
    foreign_user = hub.db.create_user("foreign@example.com")
    foreign = hub.db.create_workspace("foreign", foreign_user)
    from commonplace.web import admin

    def forbidden(*args, **kw):
        raise AssertionError("Denied source access")

    with monkeypatch.context() as patch:
        patch.setattr(admin, "read_control", forbidden)
        patch.setattr(hub.supervisor, "workspace_dir", forbidden)
        for section in ("runtime", "limits", "stuck"):
            for auth in (member_cookie, pat):
                assert get(hub, section, auth)[:2] == (403, DENIED)
            for ws in (other, foreign, "nonexistent"):
                assert get(hub, section, headers, ws)[:2] == (404, {"error": "not found"})
    for section in ("runtime", "limits", "stuck"):
        assert get(hub, section, {})[0] == 401
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            status, _, _ = request(
                hub,
                f"/api/admin/workspaces/{hub.workspace}/{section}",
                method,
                body="{}",
                headers={**headers, "Content-Type": "application/json"},
            )
            assert status == 404


def test_section_read_deadline_and_fault_isolation(hub, runtime, monkeypatch):
    headers = cookie(hub)
    status, data, _ = get(hub, "runtime", headers)
    assert status == 200
    from commonplace.web import admin

    original = admin.read_control
    calls = 0

    @contextmanager
    def fail_machines(*args):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise sqlite3.OperationalError("private-source-canary")
        with original(*args) as conn:
            yield conn

    with monkeypatch.context() as patch:
        patch.setattr(admin, "read_control", fail_machines)
        status, data, _ = get(hub, "runtime", headers)
        assert status == 200
        assert data["orchestration"]["state"] == "ready"
        assert data["orchestrators"]["state"] == "ready"
        assert data["machines"] == {"state": "error", "error": "Machines unavailable; try again"}
        assert "private-source-canary" not in json.dumps(data)
    ticks = iter([0, 3])
    clock(hub, monotonic=lambda: next(ticks, 3))
    status, data, _ = get(hub, "runtime", headers)
    assert status == 200 and all(
        data[k]["state"] == "error" for k in ("orchestration", "orchestrators", "machines")
    )
    clock(hub)
    assert get(hub, "users", headers)[0] == 200
