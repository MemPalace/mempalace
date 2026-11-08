"""Approval delay contracts, using real authenticated HTTP and a fixed clock."""

import importlib
import importlib.util
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from tests._chat_helpers import (  # noqa: F401
    chat_environment,
    chat_hub_fixture,
)
from tests.test_commonplace_boards import service


@pytest.fixture(autouse=True)
def hold_environment(monkeypatch):
    monkeypatch.setenv("COMMONPLACE_HOLD_WORKER", "off")


def module():
    assert importlib.util.find_spec("commonplace.holds") is not None  # RED
    return importlib.import_module("commonplace.holds")


def setup_hold(h, kind="decide", **extra):
    s = service(h, h.person(h.admin))
    if kind == "approve":
        agent = service(h, replace(h.principal(h.t1), parent_agent="conn:" + h.a2))
        card = agent.task_add(board="work", title="A proposed change")
        assert card["proposal_status"] == "pending"
    else:
        card = s.ask(
            "Which approach should we take next?",
            board="work",
            options=["First", "Second"],
            context="Exact context for this decision, preserved verbatim.",
        )
    body = {"card": card["number"], "kind": kind, "version": card["version"]}
    if kind == "decide":
        body["choice"] = "First"
    body.update(extra)
    return s, card, body


def post(h, body):
    return h.request(h.owner, "POST", "/api/holds", body)


def test_r01_http_hold(chat_hub):
    h = chat_hub
    s, card, body = setup_hold(h)
    status, result, _ = post(h, body)
    assert status == 201  # RED
    assert "id" in result and "release_at" in result
    assert s.task_show(card["number"])["status"] == "review"
    assert module().HOLD_SECONDS == 60


@pytest.mark.parametrize("kind", ["approve", "decide"])
def test_r02_worker_settles_current_service(chat_hub, kind):
    m = module()
    h = chat_hub
    s, card, body = setup_hold(
        h,
        kind,
        **(
            {"reason": "Approved exactly as proposed"}
            if kind == "approve"
            else {"reply": "Exact reply"}
        ),
    )
    status, result, _ = post(h, body)
    assert status == 201
    worker = h.gateway.hold_worker
    assert not worker.run_once(now=worker.now())
    assert worker.run_once(now=worker.now() + 61)
    after = s.task_show(card["number"])
    event = next(e for e in reversed(after["events"]) if e["kind"] == "decide")
    assert event["actor"] == s.principal.actor
    assert event.get("reason") == body.get("reason")
    if kind == "decide":
        assert event["text"] == "Exact reply"
    assert m.listing(h.db, s.principal, worker.now() + 61)[0]["state"] == "released"
    assert not worker.run_once(now=worker.now() + 62)


def test_r03_stop_and_duplicate(chat_hub):
    m = module()
    h = chat_hub
    s, card, body = setup_hold(h)
    status, result, _ = post(h, body)
    assert status == 201
    assert post(h, body)[0] == 409
    status, answer, _ = h.request(h.owner, "POST", f"/api/holds/{result['id']}/stop", {})
    assert status == 200 and answer["state"] == "stopped"
    assert not h.gateway.hold_worker.run_once(now=h.gateway.hold_worker.now() + 61)
    assert s.task_show(card["number"])["status"] == "review"
    assert m.listing(h.db, s.principal, h.gateway.hold_worker.now())[0]["state"] == "stopped"


def test_r04_concurrent_settlement_and_late_stop(chat_hub):
    module()
    h = chat_hub
    s, card, body = setup_hold(h)
    status, result, _ = post(h, body)
    assert status == 201
    worker = h.gateway.hold_worker
    now = worker.now() + 61
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(worker.run_once, [now, now]))
    assert sorted(results) == [False, True]
    assert len([e for e in s.task_show(card["number"])["events"] if e["kind"] == "decide"]) == 1
    assert h.request(h.owner, "POST", f"/api/holds/{result['id']}/stop", {})[0] == 409


def test_r05_stale_version(chat_hub):
    m = module()
    h = chat_hub
    s, card, body = setup_hold(h)
    assert post(h, {**body, "version": body["version"] - 1})[0] == 409
    assert post(h, body)[0] == 201
    s.task_comment(card["number"], "A newer fact")
    h.gateway.hold_worker.run_once(now=h.gateway.hold_worker.now() + 61)
    row = m.listing(h.db, s.principal, h.gateway.hold_worker.now() + 61)[0]
    assert row["state"] == "failed" and row["code"] == "stale_version"


@pytest.mark.parametrize("kind", ["approve", "decide"])
def test_r06_crash_recovery(chat_hub, kind):
    m = module()
    h = chat_hub
    s, card, body = setup_hold(h, kind)
    status, result, _ = post(h, body)
    assert status == 201
    with h.db._lock:
        h.db._conn.execute("UPDATE answer_holds SET state='settling' WHERE id=?", (result["id"],))
    if kind == "approve":
        s.task_approve(
            card["number"],
            approve=True,
            version=body["version"],
            reason="Independent owner recovery",
        )
    else:
        s.decide(card["number"], choice="First", version=body["version"])
    recovery = m.HoldWorker(h.gateway, start=False)
    assert recovery.run_once(now=recovery.now() + 61)
    assert m.listing(h.db, s.principal, recovery.now() + 61)[0]["state"] == "released"
    assert len([e for e in s.task_show(card["number"])["events"] if e["kind"] == "decide"]) == 1


def test_r07_recover_before_effect(chat_hub):
    m = module()
    h = chat_hub
    s, card, body = setup_hold(h)
    status, result, _ = post(h, body)
    assert status == 201
    with h.db._lock:
        h.db._conn.execute("UPDATE answer_holds SET state='settling' WHERE id=?", (result["id"],))
    recovery = m.HoldWorker(h.gateway, start=False)
    assert recovery.run_once(now=recovery.now() + 61)
    assert m.listing(h.db, s.principal, recovery.now() + 61)[0]["state"] == "released"


@pytest.mark.parametrize("who", ["member", "agent"])
def test_r09_owner_only(chat_hub, who):
    h = chat_hub
    _, _, body = setup_hold(h)
    auth = h.member if who == "member" else h.t1
    assert h.request(auth, "POST", "/api/holds", body)[0] == 403  # RED


def test_r08_listing_is_read_only_and_deadline_is_strict(chat_hub):
    m = module()
    h = chat_hub
    s, card, body = setup_hold(h)
    worker = h.gateway.hold_worker
    worker.now = lambda: 1800000000
    status, result, _ = post(h, body)
    assert status == 201
    worker.now = lambda: 1800000060
    status, payload, _ = h.get(h.owner, "/api/holds")
    assert status == 200 and "holds" in payload and payload["holds"]
    assert payload["holds"][0]["state"] == "waiting"
    assert h.request(h.owner, "POST", f"/api/holds/{result['id']}/stop", {})[0] == 409
    assert m.stop(h.db, h.person(h.member_user), result["id"], worker.now())[0] == 404
    assert s.task_show(card["number"])["status"] == "review"


@pytest.mark.parametrize("kind", ["approve", "decide"])
def test_g01_service_still_refuses_member(chat_hub, kind):
    from commonplace.extensions import ToolError

    h = chat_hub
    _, card, _ = setup_hold(h, kind)
    member = service(h, h.person(h.member_user))
    with pytest.raises(ToolError):
        if kind == "approve":
            member.task_approve(card["number"], approve=True)
        else:
            member.decide(card["number"], choice="First")


def test_r10_stop_after_claim(chat_hub, monkeypatch):
    m = module()
    h = chat_hub
    s, card, body = setup_hold(h)
    status, result, _ = post(h, body)
    assert status == 201
    from commonplace.boards.service import BoardService

    original = BoardService.decide
    calls = []

    def decide(self, *args, **kwargs):
        calls.append(args)
        assert h.request(h.owner, "POST", f"/api/holds/{result['id']}/stop", {})[0] == 409
        return original(self, *args, **kwargs)

    monkeypatch.setattr(BoardService, "decide", decide)
    assert h.gateway.hold_worker.run_once(now=h.gateway.hold_worker.now() + 61)
    assert len(calls) == 1
    assert m.listing(h.db, s.principal, h.gateway.hold_worker.now() + 61)[0]["state"] == "released"


def test_r11_creation_checks_write_permission(chat_hub, monkeypatch):
    m = module()
    from commonplace.boards.service import BoardService
    from commonplace.extensions import ToolError

    h = chat_hub
    s, _, body = setup_hold(h)

    def deny(*args, **kwargs):
        raise ToolError("no: write refused")

    monkeypatch.setattr(BoardService, "_write", deny)
    assert post(h, body)[0] == 403
    assert m.listing(h.db, s.principal, h.gateway.hold_worker.now()) == []


def test_r12_full_delay_and_retention(chat_hub):
    m = module()
    h = chat_hub
    s, _, body = setup_hold(h)
    worker = h.gateway.hold_worker
    worker.now = lambda: 1800000000.75
    status, result, _ = post(h, body)
    assert status == 201
    assert not worker.run_once(now=1800000060.5)
    assert m.stop(h.db, s.principal, result["id"], 1800000060.5)[0] == 200
    assert m.listing(h.db, s.principal, 1800086460.51) == []


@pytest.mark.parametrize("kind", ["link", "unlink"])
def test_r13_recovery_after_hidden_event(chat_hub, monkeypatch, kind):
    m = module()
    from commonplace.boards.service import BoardService

    h = chat_hub
    s, card, body = setup_hold(h)
    original = BoardService._history_link_visible

    def visible(self, memory_id, visibility):
        return False if memory_id == "private-drawer" else original(self, memory_id, visibility)

    monkeypatch.setattr(BoardService, "_history_link_visible", visible)
    with s.transaction() as conn:
        s._log(conn, card["number"], kind, target="private-drawer")
        raw_count = conn.execute(
            "SELECT count(*) FROM task_events WHERE task_id=?", (card["number"],)
        ).fetchone()[0]
    assert len(s.task_show(card["number"])["events"]) == raw_count - 1
    status, result, _ = post(h, body)
    assert status == 201
    with h.db._lock:
        h.db._conn.execute("UPDATE answer_holds SET state='settling' WHERE id=?", (result["id"],))
    s.decide(card["number"], choice="First", version=body["version"])
    recovery = m.HoldWorker(h.gateway, start=False)
    assert recovery.run_once(now=recovery.now() + 61)
    row = m.listing(h.db, s.principal, recovery.now() + 61)[0]
    assert row["state"] == "released" and row["code"] == ""  # RED
    assert len([e for e in s.task_show(card["number"])["events"] if e["kind"] == "decide"]) == 1


@pytest.mark.parametrize("content_type", [None, "text/plain", "application/x-www-form-urlencoded"])
def test_g03_non_json_person_write_creates_no_hold(chat_hub, content_type):
    h = chat_hub
    _, _, body = setup_hold(h)
    headers = {**h.owner, "Content-Type": content_type or ""}
    assert h.request(headers, "POST", "/api/holds", body)[0] == 403
    with h.db._lock:
        exists = h.db._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='answer_holds'"
        ).fetchone()
        if exists:
            assert h.db._conn.execute("SELECT count(*) FROM answer_holds").fetchone()[0] == 0


@pytest.mark.parametrize("content_type", ["application/json", "application/json; charset=utf-8"])
def test_g02_hold_accepts_json_content_type(chat_hub, content_type):
    h = chat_hub
    _, _, body = setup_hold(h)
    headers = {**h.owner, "Content-Type": content_type}
    assert h.request(headers, "POST", "/api/holds", body)[0] == 201  # RED on pre-hold base
