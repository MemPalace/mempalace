"""Regression contracts for card delivery, schema gating and durable label edits."""

import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from commonplace.orch import durable, runner_bridge, triggers
from commonplace.projects import orchestration
from commonplace.runner import jobs
from tests._commonplace_helpers import hub_fixture  # noqa: F401
from tests._split_helpers import invoke, tool, world
from tests.runner_harness import http


@pytest.mark.parametrize("operation", ["add", "update", "inbox", "split", "project"])
def test_non_lock_errors_are_not_busy(hub, monkeypatch, operation):
    from commonplace.boards.service import BoardService

    w = world(hub)
    token = hub.owner_token("coordinator")
    cookie = None
    w.boards.board_set("acme", "conn:coordinator")
    a = w.card()
    assert w.boards.task_show(a).get("blocked_by") == []

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("no such table: unavailable_store")

    if operation in ("inbox", "split"):
        assert tool(w, "orchestrator_inbox", board="acme")[0] == 200
        from commonplace.projects import split

        monkeypatch.setattr(split, "inbox", broken)
        method, path = (
            ("GET" if operation == "inbox" else "POST"),
            "/api/projects/acme/orchestrator/" + operation,
        )
        body = {"task": a, "state": "kept"}
    elif operation == "project":
        # The new inbox contract is the assertion-level base precondition; the old
        # project error classifier itself is already fixed by the dependency.
        assert tool(w, "orchestrator_inbox", board="acme")[0] == 200
        from commonplace.projects import routes

        monkeypatch.setattr(routes, "_view", broken)
        from commonplace import sessions

        cookie = f"{sessions.COOKIE}={sessions.start_session(w.db, w.owner.user_id, w.ws)}"
        token = None
        method, path, body = "GET", "/api/projects/acme", None
    else:
        monkeypatch.setattr(
            BoardService, "task_add" if operation == "add" else "task_update", broken
        )
        method = "POST" if operation == "add" else "PATCH"
        path = "/api/boards/acme/cards" if operation == "add" else f"/api/cards/{a}"
        body = (
            {"title": "Unchanged", "blocked_by": []}
            if operation == "add"
            else {"version": 1, "blocked_by": []}
        )
    status, result = http(hub.port, method, path, token=token, cookie=cookie, body=body)
    assert status == 500 and "busy" not in str(result)
    assert w.boards.task_show(a)["version"] == 1


@pytest.mark.parametrize("task", [[], {}, True, None, -1, 0, 2**64, "invalid"])
def test_split_task_input_is_bounded(hub, task):
    w = world(hub)
    token = hub.owner_token("coordinator")
    w.boards.board_set("acme", "conn:coordinator")
    w.card()
    status, error = http(
        hub.port,
        "POST",
        "/api/projects/acme/orchestrator/split",
        token=token,
        body={"task": task, "state": "kept"},
    )
    assert status == 400 and error.get("code") == "invalid_arguments"
    assert len(tool(w, "orchestrator_inbox", board="acme")[1]) == 1


@pytest.mark.parametrize("operation", ["inbox", "split"])
def test_split_settings_unavailable_is_retryable(hub, operation):
    w = world(hub)
    token = hub.owner_token("coordinator")
    w.boards.board_set("acme", "conn:coordinator")
    a = w.card()
    with w.db._lock:
        w.db._conn.execute("DROP TABLE orchestration_settings")
        w.db._conn.execute(
            "CREATE VIEW orchestration_settings AS SELECT * FROM missing_settings_source"
        )
    status, error = http(
        hub.port,
        "GET" if operation == "inbox" else "POST",
        "/api/projects/acme/orchestrator/" + operation,
        token=token,
        body={"task": a, "state": "kept"},
    )
    assert status == 503 and error.get("code") == "settings_unavailable"


def test_blocker_schema_tracks_workspace_switch(hub):
    w = world(hub)
    token = hub.owner_token("schema-reader")

    def properties():
        return {
            t["name"]: t["inputSchema"]["properties"]
            for t in hub.rpc(token, "tools/list")["result"]["tools"]
            if t["name"] in ("task_add", "task_update")
        }

    assert all("blocked_by" in values for values in properties().values())
    orchestration.set_enabled(w.db, w.owner, False)
    assert all("blocked_by" not in values for values in properties().values())
    orchestration.set_enabled(w.db, w.owner, True)
    assert all("blocked_by" in values for values in properties().values())


@pytest.mark.parametrize(
    "labels", [["split:1:child"], ["split:bad"], ["split:1:one", "split:1:two"]]
)
def test_root_label_update_cannot_evade_inbox(hub, labels):
    w = world(hub)
    a = w.card()
    status, error = invoke(w.boards.task_update, a, labels=labels)
    assert status == 400 and error.get("code") in ("split_parent_mismatch", "invalid_split_key")
    assert [c["number"] for c in tool(w, "orchestrator_inbox", board="acme")[1]] == [a]


def test_split_key_updates_preserve_identity(hub):
    w = world(hub)
    a = w.card()
    status, child = invoke(
        w.boards.task_add, "Child", board="acme", parent=a, labels=[f"split:{a}:one"]
    )
    assert status == 200, child
    for labels in ([], [f"split:{a}:two"], [f"split:{a}:one", f"split:{a}:two"]):
        status, error = invoke(w.boards.task_update, child["number"], labels=labels)
        assert status == 400 and error.get("code") in ("split_key_immutable", "invalid_split_key")
    updated = w.boards.task_update(child["number"], labels=[f"split:{a}:one", "tested"])
    assert updated["parent"] == a
    status, error = invoke(
        w.boards.task_add, "Retry", board="acme", parent=a, labels=[f"split:{a}:one"]
    )
    assert status == 409 and error.get("number") == child["number"]


def test_label_edit_cannot_duplicate_a_sibling_split_key(hub):
    w = world(hub)
    parent = w.card()
    key = f"split:{parent}:one"
    status, keyed = invoke(
        w.boards.task_add, "Keyed child", board="acme", parent=parent, labels=[key]
    )
    assert status == 200, keyed
    unkeyed = w.card()
    # Model an existing ordinary child, before any split key was assigned.
    with w.boards.store.transaction() as conn:
        conn.execute("UPDATE tasks SET parent_number=? WHERE id=?", (parent, unkeyed))
    before = w.boards.task_show(unkeyed)
    status, error = invoke(w.boards.task_update, unkeyed, version=before["version"], labels=[key])
    assert status == 409 and error.get("code") == "split_key_exists"
    assert error.get("number") == keyed["number"]
    after = w.boards.task_show(unkeyed)
    assert after["labels"] == before["labels"]
    assert after["version"] == before["version"] and after["events"] == before["events"]
    assert w.boards.task_show(keyed["number"])["labels"] == [key]


def test_lease_payload_survives_inbox_read_failure(hub, monkeypatch, caplog):
    w = world(hub, runner=True)
    a = w.card()
    triggers.run(w.js)
    assert w.jobs_for(a)[0].get("launch_role") == "main"
    leased = False
    original_lease, original_read = jobs.lease, orchestration.board_read

    def lease(*args, **kwargs):
        nonlocal leased
        row = original_lease(*args, **kwargs)
        leased = row is not None
        return row

    @contextmanager
    def board_read(*args, **kwargs):
        if leased:
            raise sqlite3.OperationalError("database is locked: private-failure-detail")
        with original_read(*args, **kwargs) as conn:
            yield conn

    monkeypatch.setattr(jobs, "lease", lease)
    monkeypatch.setattr(orchestration, "board_read", board_read)
    status, payload = w.machine_call("lease", {})
    assert (
        status == 200
        and payload.get("lease_token")
        and "Coordinate this project" in payload.get("message", "")
    )
    assert w.jobs_for(a)[0]["state"] == "leased"
    assert any(
        record.name == "commonplace.orch.runner_bridge"
        and "lease inbox snapshot unavailable (OperationalError)" in record.getMessage()
        for record in caplog.records
    )
    assert "private-failure-detail" not in caplog.text


def test_existing_runner_database_migrates_launch_role(hub):
    w = world(hub, on=False, runner=True)
    a = w.card()
    w.person_start(a)
    with w.js.connection() as conn:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(runner_jobs)")}
        if "launch_role" in columns:
            conn.execute("ALTER TABLE runner_jobs DROP COLUMN launch_role")
        before = dict(
            conn.execute("SELECT * FROM runner_jobs WHERE task_number=?", (a,)).fetchone()
        )
        runner_bridge.ensure_schema(conn)
        assert "launch_role" in {r[1] for r in conn.execute("PRAGMA table_info(runner_jobs)")}
        runner_bridge.ensure_schema(conn)
        after = dict(conn.execute("SELECT * FROM runner_jobs WHERE task_number=?", (a,)).fetchone())
        assert after.pop("launch_role") is None and after == before
        message = runner_bridge.launch_fields(conn, {**after, "launch_role": None})["message"]
        assert "Build the card" in message and "Read orchestrator_inbox" not in message


@pytest.mark.parametrize("state", ["doing", "review"])
def test_release_reason_only_applies_to_unstarted_work(hub, state):
    w = world(hub)
    a = w.card()
    assert "waiting" in w.boards.task_show(a)
    with w.boards.store.transaction() as conn:
        if state == "doing":
            conn.execute(
                "UPDATE tasks SET status='doing',restricted_origin=1,assignee=? WHERE id=?",
                (w.a.actor, a),
            )
        else:
            conn.execute(
                "UPDATE tasks SET status='review',ask='Check?',ask_to=NULL,restricted_origin=1 WHERE id=?",
                (a,),
            )
    from commonplace.boards import why

    with w.boards.store.connection() as conn:
        row = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (a,)).fetchone())
    # Remove the independent person wait to isolate the release condition.
    row["ask"] = None
    assert why.waiting(row, blockers=[], released=False, usage=None) is None


def test_empty_disabled_sweep_does_not_resolve_config(hub, monkeypatch):
    w = world(hub, on=False, runner=True)
    # Guard the new enabled inbox feature before testing the disabled hot path.
    orchestration.set_enabled(w.db, w.owner, True)
    assert tool(w, "orchestrator_inbox", board="acme") == (200, [])
    orchestration.set_enabled(w.db, w.owner, False)

    def broken(*args):
        raise RuntimeError("unexpected config read")

    from commonplace.orch import store

    monkeypatch.setattr(store, "resolve_in", broken)
    assert triggers.run(w.js) == {"acme": []}


def test_checkpoint_survives_inbox_failure(hub, monkeypatch):
    w = world(hub, runner=True)
    a = w.card()
    assert tool(w, "orchestrator_inbox", board="acme")[0] == 200
    from commonplace.projects import split

    def broken(*args):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(split, "context", broken)
    try:
        result = durable.checkpoint(w.ctx(w.owner), "acme", "main")
    except sqlite3.OperationalError:
        result = None
    assert (
        isinstance(result, dict)
        and result.get("board") == "acme"
        and a in {row["card"] for row in result["unstarted"]}
    )


def test_board_summary_reuses_usage_and_runner_read(hub, monkeypatch):
    w = world(hub)
    for _ in range(5):
        w.card()
    assert w.boards.task_show(1).get("waiting") is None and "blocked_by" in w.boards.task_show(1)
    calls = []
    original = orchestration.usage

    def usage(*args, **kwargs):
        if kwargs["board_slug"] == "acme":
            calls.append(kwargs.get("lease", False))
        return original(*args, **kwargs)

    monkeypatch.setattr(orchestration, "usage", usage)
    from commonplace.projects import split

    def no_extra_read(*args, **kwargs):
        raise AssertionError("summary opened a runner connection per card")

    monkeypatch.setattr(split, "runner_read", no_extra_read)
    board = next(b for b in w.boards.board_list() if b["slug"] == "acme")
    assert sum(board["waiting"].values()) == 0 and calls == [True, False]


def test_split_mark_is_a_checked_write(hub):
    w = world(hub)
    assert tool(w, "orchestrator_inbox", board="acme") == (200, [])
    from commonplace.rules import enforce

    assert "card_split_done" in enforce.CHECKED_WRITES
    assert any(
        route.tool == "card_split_done" and route.kind == "checked" for route in enforce.REST_ROUTES
    )


def test_capacity_refusal_survives_missing_waiting_reason(hub, monkeypatch):
    from commonplace.boards.service import BoardService
    from tests._phase_b_helpers import cap, doing
    from tests._split_helpers import take

    w = world(hub)
    target = w.card()
    assert "waiting" in w.boards.task_show(target)
    cap(w, 1)
    doing(w, w.card(), w.b.actor)
    before = w.boards.task_show(target)
    monkeypatch.setattr(BoardService, "_waiting", lambda *args, **kwargs: None)
    result = take(w, target)
    assert isinstance(result, tuple) and result[0] == 409
    assert result[1]["code"] == "project_limit" and result[1]["error"] == "project_limit 1/1"
    after = w.boards.task_show(target)
    assert after["version"] == before["version"] and after["events"] == before["events"]


def test_card_list_shares_one_runner_read_and_usage(hub, monkeypatch):
    from commonplace.boards.service import BoardService

    w = world(hub, runner=True)
    expected = {w.card() for _ in range(5)}
    assert "waiting" in w.boards.task_show(min(expected))
    from commonplace.projects import split

    reads, usages = [], []
    original_read, original_usage = split.runner_read, BoardService._usage

    @contextmanager
    def counted_read(*args, **kwargs):
        reads.append("open")
        with original_read(*args, **kwargs) as runner:
            try:
                yield runner
            finally:
                reads.append("close")

    def counted_usage(self, *args, **kwargs):
        usages.append(1)
        return original_usage(self, *args, **kwargs)

    monkeypatch.setattr(split, "runner_read", counted_read)
    monkeypatch.setattr(BoardService, "_usage", counted_usage)
    cards = w.boards.task_list(board="acme")
    assert {card["number"] for card in cards} == expected
    assert reads == ["open", "close"] and usages == [1]


def test_off_capacity_refusal_keeps_legacy_text_and_state(hub, monkeypatch):
    from tests._phase_b_helpers import cap, doing
    from tests._split_helpers import take

    w = world(hub, on=False)
    cap(w, 1)
    doing(w, w.card(), w.b.actor)
    target = w.card()
    before = w.boards.task_show(target)
    result = take(w, target)
    assert result[0] == 400
    assert "already has its maximum of 1 agents working" in result[1]["error"]

    # Preserve even a coded refusal without consulting the enabled projection.
    from commonplace.extensions import ToolError
    from commonplace.projects import limits

    def refuse(*args, **kwargs):
        error = ToolError("no: project_limit 1/1")
        error.code = "project_limit"
        error.status = 409
        raise error

    monkeypatch.setattr(limits, "check", refuse)
    result = take(w, target)
    assert result[0] == 409
    assert result[1]["error"] == "project_limit 1/1"
    after = w.boards.task_show(target)
    assert after["version"] == before["version"]
    assert after["events"] == before["events"]


@pytest.mark.parametrize("operation", ["task_add", "task_update"])
@pytest.mark.parametrize("failure", ["missing_db", "no_gateway", "read_error"])
def test_card_schema_defaults_off_when_settings_unreadable(monkeypatch, operation, failure):
    from commonplace import extensions

    gateway = None if failure == "no_gateway" else SimpleNamespace()
    if failure == "read_error":
        gateway.db = object()

        def unavailable(*args):
            raise RuntimeError("settings read failed")

        monkeypatch.setattr(orchestration, "enabled", unavailable)
    try:
        schema = extensions.get(operation).schema(SimpleNamespace(workspace_id="ws"), gateway)
    except Exception as error:
        raise AssertionError("Card schema must fall back to the default/off shape") from error
    assert "blocked_by" not in schema["inputSchema"]["properties"]
