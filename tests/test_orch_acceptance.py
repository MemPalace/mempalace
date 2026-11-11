"""Acceptance for config-driven starts on a board with an accepted live ``[runner]`` config.

Every ``test_r*`` test is pin-RED: the config is seeded with raw SQL
(``tests/_orch_helpers.py``), then the test drives only public routes that exist
before the orchestrator package (card REST, ``card_start`` over REST and MCP,
the runner lease route, ``card_start_status``) and asserts what a configured
board must do. Setup raises ``RuntimeError`` or fails inside a shared harness
helper; only an ``AssertionError`` raised in a test body is evidence.

The runner lease route runs the job sweep, so each lease call is one trigger
sweep. Cards a test needs to exist before configuration are created before the
seed, which arms the trigger cursor past their events.
"""

import sqlite3
from pathlib import Path

from commonplace import events
from commonplace.runner.agent import SPAWN_MESSAGE
from tests._orch_helpers import (
    MODEL,
    PROMPT,
    PROVIDER,
    SEED_DDL,
    SEED_TABLES,
    STEPS,
    config_toml,
    seed_config,
)
from tests.runner_harness import (
    attest,
    card,
    cli_token,
    enroll,
    http,
    owners as owners,
    signup_hub as signup_hub,
)

BOARD = "shared"


def _must(condition, message):
    if not condition:
        raise RuntimeError(message)


def _machine(hub, owner):
    """Enroll and attest a machine whose requester list holds one trusted PAT."""
    cid, token = cli_token(owner, "requester", profile="standard")
    status, body = http(
        hub.port,
        "POST",
        "/api/memory",
        cookie=owner["cookie"],
        body={"text": "Synthetic workspace example.", "theme": BOARD, "topic": "demo"},
    )
    _must(status == 201, f"memory seed failed: {status}")
    machine = enroll(hub, owner, requesters=[cid])
    attest(hub, owner, machine)
    status, body = http(
        hub.port, "POST", "/api/boards", cookie=owner["cookie"], body={"slug": BOARD, "wing": BOARD}
    )
    _must(status == 201, f"board add failed: {status} {body}")
    # The workspace's model list is declared first; the project's choice then picks from it.
    http(
        hub.port,
        "POST",
        f"/api/projects/{BOARD}/models/catalog",
        cookie=owner["cookie"],
        body={"provider": PROVIDER, "model": MODEL},
    )
    status, body = http(
        hub.port,
        "PUT",
        f"/api/projects/{BOARD}/models",
        cookie=owner["cookie"],
        body={
            "main": {"provider": PROVIDER, "model": MODEL},
            "worker": {"provider": PROVIDER, "model": MODEL},
        },
    )
    _must(status == 200, f"project models failed: {status} {body}")
    machine["requester_token"] = token
    return machine


def _configure(hub, owner, machine, **fields):
    body = config_toml(board=BOARD, machine=machine["machine_id"], **fields)
    return seed_config(hub.gateway.workspace_dir(owner["workspace"]), BOARD, body)


def _jobs(hub, owner, task):
    status, body = http(
        hub.port, "GET", f"/api/card-starts?task={task['number']}", cookie=owner["cookie"]
    )
    _must(status == 200 and isinstance(body, list), f"start status failed: {status} {body}")
    return body


def _lease(hub, machine):
    """One runner lease call: it sweeps, then may lease a queued job."""
    return http(hub.port, "POST", "/api/runner/lease", token=machine["machine_token"], body={})


def _person_start(hub, owner, machine, task):
    return http(
        hub.port,
        "POST",
        "/api/card-starts",
        cookie=owner["cookie"],
        body={
            "task": task["number"],
            "machine": machine["machine_id"],
            "project": "demo",
            "topic": "worker",
        },
    )


def _patch_card(hub, owner, task, **fields):
    status, body = http(hub.port, "GET", f"/api/cards/{task['number']}", cookie=owner["cookie"])
    _must(status == 200, f"card read failed: {status}")
    status, body = http(
        hub.port,
        "PATCH",
        f"/api/cards/{task['number']}",
        cookie=owner["cookie"],
        body={"version": body["version"], **fields},
    )
    _must(status == 200, f"card update failed: {status} {body}")
    return body


def test_r1_one_todo_event_starts_exactly_one_job(signup_hub, owners):
    """R1: the same todo event seen by the listener, the sweep and a replay is one job."""
    hub, owner = signup_hub, owners["A"]
    machine = _machine(hub, owner)
    _configure(hub, owner, machine)
    task = card(hub, owner, board=BOARD)
    for _ in range(3):
        _lease(hub, machine)
    assert len(_jobs(hub, owner, task)) == 1


def test_r1b_swallowed_emission_is_found_by_the_sweep(signup_hub, owners, monkeypatch):
    """R1b: the todo move commits but its event emission raises; a later sweep starts it."""
    hub, owner = signup_hub, owners["A"]
    machine = _machine(hub, owner)
    _configure(hub, owner, machine)
    original = events.emit

    def lost(*_args, **_kwargs):
        raise RuntimeError("synthetic emission failure")

    monkeypatch.setattr(events, "emit", lost)
    task = card(hub, owner, board=BOARD)
    monkeypatch.setattr(events, "emit", original)
    _lease(hub, machine)
    _lease(hub, machine)
    assert len(_jobs(hub, owner, task)) == 1


def test_r1d_decide_row_into_todo_starts_one_job(signup_hub, owners):
    """R1d: an owner decision that returns a card to todo writes a decide row that fires."""
    hub, owner = signup_hub, owners["A"]
    machine = _machine(hub, owner)
    task = card(hub, owner, board=BOARD)
    status, body = http(
        hub.port,
        "POST",
        f"/api/cards/{task['number']}/handoff",
        cookie=owner["cookie"],
        body={
            "to": "owner",
            "note": "Should a worker start this card now, or wait?",
            "context": "The card is ready for a worker to pick up.",
            "options": ["Start it now", "Wait for later"],
        },
    )
    _must(status == 200 and body["status"] == "review", f"handoff failed: {status} {body}")
    _configure(hub, owner, machine)
    status, body = http(
        hub.port,
        "POST",
        f"/api/cards/{task['number']}/decide",
        cookie=owner["cookie"],
        body={"version": body["version"], "choice": "Start it now", "then": "todo"},
    )
    _must(status == 200 and body["status"] == "todo", f"decide failed: {status} {body}")
    _lease(hub, machine)
    _lease(hub, machine)
    assert len(_jobs(hub, owner, task)) == 1


def test_r1e_task_update_into_todo_starts_one_job(signup_hub, owners):
    """R1e: a task_update that moves a backlog card to todo fires once."""
    hub, owner = signup_hub, owners["A"]
    machine = _machine(hub, owner)
    _configure(hub, owner, machine)
    task = card(hub, owner, board=BOARD, status="backlog")
    _lease(hub, machine)
    _must(_jobs(hub, owner, task) == [], "a backlog card must not start")
    _patch_card(hub, owner, task, status="todo")
    _lease(hub, machine)
    _lease(hub, machine)
    assert len(_jobs(hub, owner, task)) == 1


def test_r8b_pat_card_start_on_configured_board_is_redirected(signup_hub, owners):
    """R8b: a listed trusted PAT gets ``use orch_card_start`` on REST and MCP, and no job."""
    hub, owner = signup_hub, owners["A"]
    machine = _machine(hub, owner)
    task = card(hub, owner, board=BOARD)
    _configure(hub, owner, machine)
    arguments = {
        "task": task["number"],
        "machine": machine["machine_id"],
        "project": "demo",
        "topic": "worker",
    }
    code, body = http(
        hub.port, "POST", "/api/card-starts", token=machine["requester_token"], body=arguments
    )
    assert code == 403, (code, body)
    assert "use orch_card_start" in str(body.get("error")), body
    status, reply = http(
        hub.port,
        "POST",
        "/mcp",
        token=machine["requester_token"],
        body={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "card_start", "arguments": arguments},
        },
    )
    _must(status == 200, f"MCP call failed: {status}")
    assert "error" in reply or reply["result"].get("isError"), reply
    assert "use orch_card_start" in str(reply), reply
    assert _jobs(hub, owner, task) == []


def test_r10a_person_start_lease_carries_prompt_workflow_and_model(signup_hub, owners):
    """R10a: the runner lease payload of a configured start has the pinned message and model."""
    hub, owner = signup_hub, owners["A"]
    machine = _machine(hub, owner)
    task = card(hub, owner, board=BOARD)
    _configure(hub, owner, machine)
    code, body = _person_start(hub, owner, machine, task)
    assert code == 201, (code, body)
    code, payload = _lease(hub, machine)
    assert code == 200, (code, payload)
    message = payload.get("message") or ""
    assert message.startswith(SPAWN_MESSAGE.format(n=task["number"], job_id=payload["job_id"])), (
        payload
    )
    assert PROMPT in message, payload
    assert all(step in message for step in STEPS), payload
    assert payload.get("modelSelection") == {"instanceId": PROVIDER, "model": MODEL}, payload


def test_r11e_person_start_respects_the_config_cap(signup_hub, owners):
    """R11e: a person start is admitted below ``max_agents`` and refused at it, with no lease."""
    hub, owner = signup_hub, owners["A"]
    machine = _machine(hub, owner)
    first = card(hub, owner, board=BOARD)
    second = card(hub, owner, board=BOARD)
    _configure(hub, owner, machine, max_agents=1)
    code, body = _person_start(hub, owner, machine, first)
    assert code == 201, (code, body)
    code, body = _person_start(hub, owner, machine, second)
    assert code in (403, 409), (code, body)
    assert _jobs(hub, owner, second) == []


def _objects(script):
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(script)
        rows = conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE tbl_name IN (%s) ORDER BY type, name" % ",".join("?" for _ in SEED_TABLES),
            SEED_TABLES,
        ).fetchall()
    finally:
        conn.close()
    return [(kind, name, table, " ".join((sql or "").split())) for kind, name, table, sql in rows]


def test_seed_ddl_matches_orch_schema():
    """Head only: the raw-SQL seed creates exactly what ``orch/schema.sql`` creates."""
    import commonplace

    schema = Path(commonplace.__file__).parent / "orch" / "schema.sql"
    assert schema.is_file(), "commonplace/orch/schema.sql is missing"
    assert {table for _kind, _name, table, _sql in _objects(SEED_DDL)} == set(SEED_TABLES)
    assert _objects(schema.read_text(encoding="utf-8")) == _objects(SEED_DDL)
