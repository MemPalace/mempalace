"""Additional pagination, read-budget and workspace isolation controls."""

import base64
import json
import sqlite3
from contextlib import contextmanager

import pytest

from commonplace import webhooks
from commonplace.policy import Policy
from tests._commonplace_helpers import hub_fixture as hub_fixture
from tests.test_admin_api import agent, clock, cookie, get, snapshot
from tests.test_admin_runtime_api import (
    FUTURE,
    OLD,
    logical,
    machine,
    runtime as runtime,
    switch,
    task,
    webhook,
)


@pytest.mark.parametrize(
    "section,kind", [("runtime", "orchestrators"), ("limits", "projects"), ("stuck", "cards")]
)
def test_runtime_pagination_counts_and_bound_cursors(hub, runtime, section, kind):
    with runtime[0].connection() as conn:
        for number in range(4, 106):
            conn.execute(
                "INSERT INTO boards(id,slug,name,wing,stamp_user,stamp_trust,created_at) "
                "VALUES (?, ?, 'Project','notes','synthetic','trusted',?)",
                (number, f"project-{number:03}", OLD),
            )
    for number in range(1, 104):
        task(runtime, number, claim=OLD)
    headers = cookie(hub)
    status, first, _ = get(hub, section, headers)
    assert status == 200
    if section == "runtime":
        data, items = first[kind], first[kind]["items"]
    elif section == "limits":
        data, items = first["pagination"][kind], first[kind]
    else:
        data, items = first, first[kind]
    assert len(items) == 100 and data["has_more"] is True
    expected = 103 if section == "stuck" else 104
    assert data["count"] == expected
    cursor = data["next_cursor"]
    status, last, _ = get(hub, section, headers, query="?cursor=" + cursor)
    assert status == 200
    if section == "runtime":
        data2, items2 = last[kind], last[kind]["items"]
    elif section == "limits":
        data2, items2 = last["pagination"][kind], last[kind]
    else:
        data2, items2 = last, last[kind]
    assert len(items2) == expected - 100 and not data2["has_more"] and data2["count"] == expected
    key = "number" if kind == "cards" else "id"
    assert len({r[key] for r in items + items2}) == expected
    if section == "stuck":
        assert last["reason_counts"]["expired_lease"] == 103
    other_section = "limits" if section != "limits" else "runtime"
    assert get(hub, other_section, headers, query="?cursor=" + cursor)[0] == 400
    decoded = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    decoded[0] = "other-workspace"
    wrong = base64.urlsafe_b64encode(json.dumps(decoded).encode()).decode().rstrip("=")
    assert get(hub, section, headers, query="?cursor=" + wrong)[0] == 400
    for query in ("?workspace=other", "?cursor=", "?cursor=bad", "?cursor=x&cursor=y", "?other"):
        assert get(hub, section, headers, query=query)[0] == 400


def test_machine_lists_page_independently(hub, runtime):
    for n in range(1, 103):
        machine(hub, n)
    headers = cookie(hub)
    for section in ("runtime", "limits"):
        status, first, _ = get(hub, section, headers)
        assert status == 200
        p = first["machines"] if section == "runtime" else first["pagination"]["machines"]
        assert p["count"] == 102 and p["has_more"]
        last = get(hub, section, headers, query="?cursor=" + p["next_cursor"])[1]
        items = last["machines"]["items"] if section == "runtime" else last["machines"]
        assert len(items) == 2
        assert items[-1]["id"] == "mach_0000000000000066"


def test_scoped_connections_and_worker_assignments(hub, runtime):
    owner = hub.db.create_user("other@example.com")
    foreign = hub.db.create_workspace("other", owner)
    hub.db.create_token(
        workspace_ref=foreign,
        user_ref=owner,
        name="lead",
        client="synthetic",
        policy=Policy.build(scopes=["memory:read"], read_wings=["notes"]),
        actor=owner,
    )
    lead, _ = agent(hub, "lead", expires_at=FUTURE)
    webhook(hub, lead, ws=foreign)
    headers = cookie(hub)
    assert get(hub, "runtime", headers)[1]["orchestrators"]["items"][0]["wakeable"] is False
    webhook(hub, lead, key="local", kinds='["card.*"]')
    assert get(hub, "runtime", headers)[1]["orchestrators"]["items"][0]["wakeable"] is False
    hub.db._conn.execute("UPDATE webhooks SET kinds='[\"*\"]' WHERE id='local'")
    assert get(hub, "runtime", headers)[1]["orchestrators"]["items"][0]["wakeable"] is True
    # A malformed matching subscription fails its source, without URL or exception details.
    hub.db._conn.execute("UPDATE webhooks SET kinds='not-json' WHERE id='local'")
    data = get(hub, "runtime", headers)[1]
    assert data["orchestrators"]["state"] == "error" and data["machines"]["state"] == "ready"
    assert "not-json" not in json.dumps(data)
    switch(hub)
    mid, *_ = machine(hub)
    foreign_worker, _ = hub.db.create_token(
        workspace_ref=foreign,
        user_ref=owner,
        name="worker-1",
        client="synthetic",
        policy=Policy.build(scopes=["memory:read"], read_wings=["notes"]),
        actor=owner,
    )
    hub.db._conn.execute(
        "UPDATE runner_machines SET worker_connection_id=? WHERE id=?", (foreign_worker, mid)
    )
    task(runtime, 1, assignee="conn:worker-1")
    task(runtime, 2, assignee="conn:worker-1")
    # A cross-workspace dangling assignment must not turn an ordinary agent into a worker.
    assert get(hub, "limits", headers)[1]["projects"][0]["used"] == 1


def test_budget_preserves_completed_sources(hub, runtime, monkeypatch):
    from commonplace.web import admin

    headers = cookie(hub)
    tick = 0
    clock(hub, monotonic=lambda: tick)
    original = admin.read_control
    calls = 0

    @contextmanager
    def later_source(*args):
        nonlocal calls, tick
        calls += 1
        if calls == 2:
            tick = 3
        with original(*args) as conn:
            yield conn

    with monkeypatch.context() as patch:
        patch.setattr(admin, "read_control", later_source)
        data = get(hub, "runtime", headers)[1]
        assert data["orchestration"]["state"] == "ready"
        assert data["orchestrators"]["state"] == data["machines"]["state"] == "error"
    clock(hub)
    assert get(hub, "users", headers)[0] == 200


def test_absent_optional_tables_never_initialized(hub, runtime):
    headers = cookie(hub)
    with hub.db._lock:
        for table in (
            "project_orchestrators",
            "runner_machines",
            "runner_connections",
            "webhooks",
            "orchestration_settings",
        ):
            hub.db._conn.execute(f'DROP TABLE "{table}"')
    before = logical(runtime, hub)
    data = get(hub, "runtime", headers)[1]
    assert data["orchestration"]["enabled"] is False and data["machines"]["items"] == []
    assert data["orchestrators"]["items"][0]["connected"] is False
    assert get(hub, "limits", headers)[0] == get(hub, "stuck", headers)[0] == 200
    assert logical(runtime, hub) == before
    assert snapshot(hub)[2] == before[1]


@pytest.mark.parametrize("present", [False, True])
def test_missing_runner_tolerance_is_explicit(hub, runtime, present):
    from contextlib import nullcontext

    from commonplace.projects import orchestration
    from mempalace.sqlite_crypto import connect

    task(runtime, 1)
    runner_context = connect(str(runtime[1] / "runner.sqlite3")) if present else nullcontext(None)
    with runtime[0].connection() as bc, runner_context as rc:
        if rc is not None:
            rc.execute("CREATE TABLE unrelated (id INTEGER)")
        args = dict(board_id=1, board_slug="alpha", workers=set(), now="2026-11-07T12:00:00Z")
        assert orchestration.usage(bc, rc, **args, allow_missing_runner=True).used == 1
        with pytest.raises((sqlite3.OperationalError, AttributeError)):
            orchestration.usage(bc, rc, **args)


@pytest.mark.parametrize("section,kind", [("runtime", "orchestrators"), ("limits", "projects")])
def test_full_length_slug_paginates(hub, runtime, section, kind):
    boundary = "p" * 100
    with runtime[0].connection() as conn:
        for number in range(4, 102):
            conn.execute(
                "INSERT INTO boards(id,slug,name,wing,stamp_user,stamp_trust,created_at) "
                "VALUES (?,?, 'Project','notes','synthetic','trusted',?)",
                (number, f"a-{number:03}", OLD),
            )
        conn.execute("UPDATE boards SET slug=? WHERE id=2", (boundary,))
        conn.execute(
            "INSERT INTO boards(id,slug,name,wing,stamp_user,stamp_trust,created_at) "
            "VALUES (102,'z-last','Last','notes','synthetic','trusted',?)",
            (OLD,),
        )
    headers = cookie(hub)
    status, first, _ = get(hub, section, headers)
    assert status == 200
    items = first[kind]["items"] if section == "runtime" else first[kind]
    pagination = first[kind] if section == "runtime" else first["pagination"][kind]
    assert len(items) == 100 and items[-1]["slug"] == boundary
    status, second, _ = get(hub, section, headers, query="?cursor=" + pagination["next_cursor"])
    assert status == 200
    items = second[kind]["items"] if section == "runtime" else second[kind]
    assert [r["slug"] for r in items] == ["z-last"]


def test_legacy_columns_are_read_without_migration(hub, runtime):
    # The report dispatcher independently migrates existing sources. Stop and
    # join it before constructing a legacy schema for the Admin no-write check.
    webhooks.stop(hub.gateway)
    assert not hub.gateway.webhooks.thread.is_alive(), "report dispatcher did not stop within 7s"
    # These workers can also write snapshot tables; isolate all of them.
    for name in ("hold_worker", "chat_worker", "phone_push"):
        worker = getattr(hub.gateway, name, None)
        if worker is not None:
            worker.stop()
            assert worker.thread is None or not worker.thread.is_alive(), f"{name} did not stop"
    task(runtime, 1, claim=OLD)
    with runtime[0].connection() as conn:
        # Legacy stores predate the report trigger that references heartbeat_at.
        conn.execute("DROP TRIGGER IF EXISTS project_report_reset")
        conn.execute("ALTER TABLE boards DROP COLUMN coordinator")
        conn.execute("ALTER TABLE tasks DROP COLUMN heartbeat_at")
    headers = cookie(hub)
    before = logical(runtime, hub)
    status, data, _ = get(hub, "runtime", headers)
    assert status == 200 and data["orchestrators"]["state"] == "ready"
    assert all(
        not r["connected"] and r["coordinator"] is None for r in data["orchestrators"]["items"]
    )
    assert get(hub, "limits", headers)[0] == 200
    status, data, _ = get(hub, "stuck", headers)
    assert status == 200 and data["cards"][0]["heartbeat_at"] is None
    assert logical(runtime, hub) == before


def test_only_enabled_unrevoked_machine_grants(hub, runtime):
    active, *_ = machine(hub)
    machine(hub, 2, enabled=0)
    machine(hub, 3, revoked_at=OLD)
    headers = cookie(hub)
    status, data, _ = get(hub, "limits", headers)
    assert status == 200
    assert [r["id"] for r in data["machines"]] == [active]
    assert data["pagination"]["machines"]["count"] == 1
    assert get(hub, "runtime", headers)[1]["machines"]["count"] == 3


def test_invalid_runtime_metadata_is_not_exposed(hub, runtime):
    from tests.test_admin_runtime_api import job, runner

    mid, *_ = machine(hub)
    hub.db._conn.execute("PRAGMA ignore_check_constraints=ON")
    hub.db._conn.execute(
        "UPDATE runner_machines SET id='invalid-machine',binding_state='invalid-binding' WHERE id=?",
        (mid,),
    )
    with runtime[0].connection() as conn:
        conn.execute("UPDATE boards SET slug='invalid/slug' WHERE id=2")
    task(runtime, 1, claim=OLD)
    rc = runner(runtime)
    try:
        job(rc, 1)
        rc.execute("UPDATE runner_jobs SET id='invalid-job'")
    finally:
        rc.close()
    headers = cookie(hub)
    data = get(hub, "runtime", headers)[1]
    assert data["machines"]["items"][0]["id"] is None
    assert data["machines"]["items"][0]["binding_state"] is None
    assert data["orchestrators"]["items"][1]["slug"] is None
    assert get(hub, "limits", headers)[1]["projects"][1]["slug"] is None
    assert get(hub, "stuck", headers)[1]["cards"][0]["expired_jobs"][0]["job_id"] is None
