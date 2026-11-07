"""Inbox eligibility, durable keyed children and narrow coordinator authority."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from commonplace.boards.service import BoardService
from tests._commonplace_helpers import hub_fixture  # noqa: F401
from tests._phase_b_helpers import seed, trusted_seat
from tests._split_helpers import invoke, tool, world


def test_r19_inbox_without_webhook(hub):
    w = world(hub)
    seat, _ = trusted_seat(w)
    a = w.card()
    status, inbox = tool(w, "orchestrator_inbox", seat, board="acme")
    assert status == 200 and [c["number"] for c in inbox] == [a]  # RED
    assert inbox[0]["keyed_children"] == []


def test_r19b_crash_resume_keys_and_mark(hub):
    w = world(hub)
    seat, _ = trusted_seat(w)
    s = BoardService(w.ctx(seat))
    a = w.card()
    status, child = invoke(
        s.task_add, "First wording", board="acme", parent=a, labels=[f"split:{a}:build"]
    )
    assert status == 200 and child.get("parent") == a  # RED
    again, refused = invoke(
        s.task_add, "Reworded after crash", board="acme", parent=a, labels=[f"split:{a}:build"]
    )
    assert (
        again == 409
        and refused["code"] == "split_key_exists"
        and refused["number"] == child["number"]
    )
    inbox = tool(w, "orchestrator_inbox", seat, board="acme")[1]
    assert inbox[0]["keyed_children"][0]["number"] == child["number"]
    assert tool(w, "card_split_done", seat, task=a, state="split")[0] == 200
    assert tool(w, "card_split_done", seat, task=a, state="kept")[1]["state"] == "split"
    assert tool(w, "orchestrator_inbox", seat, board="acme") == (200, [])
    with w.boards.store.connection() as conn:
        assert (
            conn.execute("SELECT count(*) FROM tasks WHERE parent_number=?", (a,)).fetchone()[0]
            == 1
        )
        assert (
            conn.execute("SELECT count(*) FROM card_splits WHERE task_id=?", (a,)).fetchone()[0]
            == 1
        )
    assert (
        sum(e["kind"] == "edit" and e["choice"] == "split" for e in w.boards.task_show(a)["events"])
        == 1
    )


def test_r19c_children_and_seat_roots_excluded(hub):
    w = world(hub)
    seat, _ = trusted_seat(w)
    root, child = w.card(by=seat), w.card()
    with w.boards.store.transaction() as conn:
        conn.execute("UPDATE tasks SET parent_number=? WHERE id=?", (root, child))
    status, inbox = tool(w, "orchestrator_inbox", seat, board="acme")
    assert status == 200 and inbox == []  # RED


def test_r19d_existing_seat_setup_and_session(hub):
    w = world(hub, on=False)
    seat, _ = trusted_seat(w)
    seed(w)
    w.card()
    status, inbox = tool(w, "orchestrator_inbox", seat, board="acme")
    assert status == 200 and len(inbox) == 1  # RED
    from commonplace.projects.routes import _view
    from commonplace.session import session_start

    assert "card.created" in _view(w.gw, w.owner, w.board_row())["setup"]["webhook_add"]["events"]
    assert "1 unsplit cards" in session_start(w.ctx(seat), {"board": "acme"})


@pytest.mark.parametrize("actor", ["agent", "member"])
def test_r19e_access(hub, actor):
    w = world(hub)
    trusted_seat(w)
    if actor == "member":
        from dataclasses import replace

        principal = replace(w.owner, role="member")
    else:
        principal = w.a
    status, error = tool(w, "orchestrator_inbox", principal, board="acme")
    assert status == 403 and "not_allowed" in str(error)  # RED


def test_r19f_concurrent_same_key(hub):
    w = world(hub)
    seat, _ = trusted_seat(w)
    a = w.card()
    services = [BoardService(w.ctx(seat)), BoardService(w.ctx(seat))]

    def add(s):
        return invoke(s.task_add, "Child", board="acme", parent=a, labels=[f"split:{a}:build"])

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(add, services))
    assert sorted(s for s, _ in results) == [200, 409]  # RED
    inserted = next(b for s, b in results if s == 200)
    refused = next(b for s, b in results if s == 409)
    assert refused["number"] == inserted["number"]
    assert len(tool(w, "orchestrator_inbox", seat, board="acme")[1][0]["keyed_children"]) == 1
    assert sum(e["kind"] == "add" for e in w.boards.task_show(inserted["number"])["events"]) == 1


def test_r19g_doing_root_stays_inbox(hub):
    w = world(hub)
    a = w.card(status="doing")
    status, inbox = tool(w, "orchestrator_inbox", board="acme")
    assert status == 200 and [c["number"] for c in inbox] == [a]  # RED


def test_guard_g9_off_shape_and_arguments(hub):
    w = world(hub, on=False)
    a = w.card()
    view = w.boards.task_show(a)
    assert not {"waiting", "blocked_by", "blocks"} & view.keys()
    from commonplace.boards import tools

    assert (
        invoke(
            tools.run,
            w.ctx(w.owner),
            "task_add",
            {"title": "Refused", "board": "acme", "blocked_by": [a]},
        )[0]
        >= 400
    )
    assert tool(w, "orchestrator_inbox", board="acme")[0] >= 400


def test_guard_g10_off_order(hub):
    w = world(hub, on=False)
    a, b = w.card(), w.card()
    from tests._split_helpers import blockers, take

    blockers(w, a, b)
    assert take(w)[1]["number"] == a


@pytest.mark.parametrize(
    "route,method",
    [
        ("/api/projects/acme/orchestrator/inbox", "GET"),
        ("/api/projects/acme/orchestrator/split", "POST"),
    ],
)
def test_guard_g6_routes_require_auth(hub, route, method):
    from tests.runner_harness import http

    assert http(hub.port, method, route, body={})[0] == 401


def test_guard_g7_no_private_or_outbound_dependencies():
    import ast
    import re
    from pathlib import Path

    for name in ("commonplace/boards/why.py", "commonplace/projects/split.py"):
        path = Path(name)
        if not path.exists():
            continue
        source = path.read_text()
        tree = ast.parse(source)
        # Pure modules contain no deployment URLs or absolute filesystem paths.
        literals = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        ]
        assert not any(
            re.search(r"https?://|(?:^|\s)/(?:[^/\s]+/){2}|[A-Za-z]:\\", value)
            for value in literals
        )
        imports = [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        imports += [
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        ]
        assert not any(
            n and n.split(".")[0] in ("http", "urllib", "requests", "httpx", "socket")
            for n in imports
        )


@pytest.mark.parametrize("operation", ["inbox", "split", "add", "update"])
def test_busy_routes_bounded_and_unchanged(hub, monkeypatch, operation):
    import sqlite3

    from tests.runner_harness import http

    w = world(hub)
    token = hub.owner_token("coordinator")
    w.boards.board_set("acme", "conn:coordinator")
    a = w.card()
    before = w.boards.task_show(a)

    def busy(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    if operation in ("inbox", "split"):
        assert tool(w, "orchestrator_inbox", board="acme")[0] == 200
        from commonplace.projects import split

        monkeypatch.setattr(split, "inbox", busy)
        method = "GET" if operation == "inbox" else "POST"
        path = "/api/projects/acme/orchestrator/" + operation
        body = {"task": a, "state": "kept"}
    else:
        monkeypatch.setattr(BoardService, "task_add" if operation == "add" else "task_update", busy)
        method = "POST" if operation == "add" else "PATCH"
        path = "/api/boards/acme/cards" if operation == "add" else f"/api/cards/{a}"
        body = (
            {"title": "Busy", "blocked_by": []}
            if operation == "add"
            else {"version": before["version"], "blocked_by": []}
        )
    status, error = http(hub.port, method, path, token=token, body=body)
    assert status == 409 and error == {"error": "busy", "code": "busy"}
    assert w.boards.task_show(a) == before
    with w.boards.store.connection() as conn:
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM card_splits").fetchone()[0] == 0


def test_rest_inbox_and_split_real_effects(hub):
    from tests.runner_harness import http

    w = world(hub)
    token = hub.owner_token("coordinator")
    w.boards.board_set("acme", "conn:coordinator")
    a = w.card()
    status, inbox = http(hub.port, "GET", "/api/projects/acme/orchestrator/inbox", token=token)
    assert status == 200 and inbox[0]["number"] == a
    status, marker = http(
        hub.port,
        "POST",
        "/api/projects/acme/orchestrator/split",
        token=token,
        body={"task": a, "state": "kept"},
    )
    assert status == 200 and marker["state"] == "kept"
    assert http(hub.port, "GET", "/api/projects/acme/orchestrator/inbox", token=token) == (200, [])
    assert w.boards.task_show(a)["events"][-1]["actor"] == "conn:coordinator"
    assert w.boards.task_show(a)["events"][-1]["choice"] == "split"


def test_split_key_rejects_wrong_parent_and_access(hub):
    w = world(hub)
    a, b = w.card(), w.card()
    status, result = invoke(
        w.boards.task_add, "Bad parent", board="acme", parent=a, labels=[f"split:{b}:build"]
    )
    assert status == 400 and result.get("code") == "split_parent_mismatch"
    status, _ = invoke(
        BoardService(w.ctx(w.a)).task_add,
        "No authority",
        board="acme",
        parent=a,
        labels=[f"split:{a}:build"],
    )
    assert status == 403
    with w.boards.store.connection() as conn:
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 2
