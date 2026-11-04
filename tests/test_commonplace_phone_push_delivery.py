"""Phone delivery acceptance through signup, public routes and a local stub."""

import importlib
import json
import os
import re
from urllib.parse import urlsplit

import pytest

from commonplace import urls
from commonplace.boards import service as board_service
from tests.phone_push_harness import (
    agent,
    api,
    cli,
    clock,
    enable,
    fresh as fresh,
    hub as hub,
    off,
    on,
    positive_get,
    receiver as receiver,
    remote as remote,
    rows,
    s1_counter,
    signup as signup,
)


def create(hub, owner=None, *, worker=None, **data):
    code, body = api(
        hub,
        owner,
        "POST",
        "/api/boards/commonplace/cards",
        {"title": "Synthetic work", **data},
        token=worker.token if worker else None,
    )
    assert code == 201, body
    return body


def ask(hub, worker, *, marker="Synthetic review"):
    card = create(hub, worker=worker, title=marker, body=marker)
    code, body = api(
        hub,
        None,
        "POST",
        f"/api/cards/{card['number']}/handoff",
        {
            "version": card["version"],
            "to": "owner",
            "note": f"Should we proceed with this change? {marker}",
            "context": f"The implementation and checks are complete. {marker}",
            "options": ["Proceed with the change", "Wait for more information"],
        },
        token=worker.token,
    )
    assert code == 200, body
    return body


def delivery(hub, *, number=None, category=None, topic=None):
    subscription_ids = {
        row["id"]
        for row in rows(hub, "phone_push_subscriptions")
        if topic is None or row["topic"] == topic
    }
    found = [
        row
        for row in rows(hub, "phone_push_deliveries")
        if row["number"] == number
        and row["subscription_id"] in subscription_ids
        and (category is None or row["category"] == category)
    ]
    assert len(found) == 1, found
    return found[0]


def queue_test(hub, owner):
    code, body = api(hub, owner, "POST", "/api/phone-push/test", {})
    assert code == 202, body
    assert body == {"status": "pending"}
    assert "deliver" not in json.dumps(body).lower()


def hits(hub, topic):
    return [request for request in hub.ntfy.requests if request.path == "/" + topic]


def accepted_control(hub, owner, dispatcher):
    queue_test(hub, owner)
    dispatcher.drain()
    found = rows(hub, "phone_push_deliveries")
    assert len(found) == 1, found
    assert found[0]["status"] == "accepted", found
    assert len(hub.ntfy.requests) == 1
    code, body = api(hub, owner, "GET", "/api/phone-push")
    assert code == 200, body
    assert body["test"] == {"status": "accepted"}
    assert "deliver" not in json.dumps(body).lower()


def test_request_contract_and_private_content_never_escape(hub, signup, monkeypatch, caplog):
    _, _, register = signup
    owner, _ = register(email="owner@example.test")
    positive_get(hub, owner)
    dispatcher = enable(monkeypatch, hub, token="tk_testtoken")
    subscription = on(hub, owner)
    topic = subscription["topic"]
    assert re.fullmatch(r"cp[A-Za-z0-9_-]{40}", topic)
    worker = agent(hub, owner, "worker")
    marker = "PRIVATE_CONTENT_8f1b7159"
    card = ask(hub, worker, marker=marker)
    dispatcher.reconcile()
    dispatcher.drain()
    assert len(hub.ntfy.requests) == 1
    request = hub.ntfy.requests[0]
    assert request.method == "POST"
    assert request.path == "/" + topic
    assert request.body == f"#{card['number']} needs you".encode()
    headers = {key.lower(): value for key, value in request.headers.items()}
    assert headers["content-type"] == "text/plain; charset=utf-8"
    assert headers["title"] == "Commonplace"
    assert headers["priority"] == "high"
    assert headers["authorization"] == "Bearer tk_testtoken"
    assert re.fullmatch(r"https://cp\.example/s/[A-Za-z0-9]{7}", headers["click"])
    assert not {"cache", "tags", "firebase"} & headers.keys()
    assert marker not in repr(request)
    outcome = delivery(hub, number=card["number"])
    assert outcome["status"] == "accepted"
    code, error = api(hub, owner, "POST", "/api/phone-push/test", {"extra": marker})
    assert code == 400, error
    private_surfaces = json.dumps(rows(hub, "phone_push_deliveries")) + caplog.text + repr(error)
    for secret in (marker, topic, "tk_testtoken"):
        assert secret not in private_surfaces


@pytest.mark.parametrize("change", ["off", "answered", "requeued"])
def test_pending_delivery_is_rechecked_with_untouched_control(hub, signup, monkeypatch, change):
    _, _, register = signup
    owner, _ = register(email="owner@example.test")
    positive_get(hub, owner)
    dispatcher = enable(monkeypatch, hub)
    subscription = on(hub, owner)
    other, _ = register(email="other@example.test")
    control_subscription = on(hub, other)
    worker = agent(hub, owner, "worker")
    control_worker = agent(hub, other, "control")
    if change == "requeued":
        card = create(hub, worker=worker, status="doing")
        assert card["claim_until"], card
        clock(monkeypatch, card["claim_until"])
    else:
        card = ask(hub, worker)
    control = ask(hub, control_worker)
    dispatcher.reconcile()
    if change == "requeued":
        dispatcher.sweep()
    assert delivery(hub, number=card["number"], topic=subscription["topic"])["status"] == "pending"
    if change == "off":
        off(hub, owner)
    else:
        code, current = api(hub, owner, "GET", f"/api/cards/{card['number']}")
        assert code == 200, current
        action, data = (
            ("decide", {"reply": "Continue the work", "then": "todo"})
            if change == "answered"
            else ("handoff", {"to": "anyone", "note": "Retry the work"})
        )
        code, body = api(
            hub,
            owner,
            "POST",
            f"/api/cards/{card['number']}/{action}",
            {"version": current["version"], **data},
        )
        assert code == 200, body
    # No reconcile: this must be the sender's own category recheck.
    dispatcher.drain()
    cancelled = delivery(hub, number=card["number"], topic=subscription["topic"])
    assert cancelled["status"] == "cancelled", cancelled
    assert not cancelled["error"]
    assert hits(hub, subscription["topic"]) == []
    assert (
        delivery(hub, number=control["number"], topic=control_subscription["topic"])["status"]
        == "accepted"
    )
    assert len(hits(hub, control_subscription["topic"])) == 1


def test_signout_cancels_pending_and_never_backfills_missed_changes(hub, signup, monkeypatch):
    _, _, register = signup
    owner, _ = register(email="owner@example.test")
    positive_get(hub, owner)
    me = hub.me(owner)
    dispatcher = enable(monkeypatch, hub)
    subscription = on(hub, owner)
    other, _ = register(email="other@example.test")
    control_subscription = on(hub, other)
    worker = agent(hub, owner, "worker")
    control_worker = agent(hub, other, "control")
    pending = ask(hub, worker)
    control = ask(hub, control_worker)
    dispatcher.reconcile()
    assert (
        delivery(hub, number=pending["number"], topic=subscription["topic"])["status"] == "pending"
    )
    code, body = api(hub, owner, "POST", "/app/logout", {})
    assert code == 204, body
    code, body = api(hub, owner, "GET", "/api/phone-push")
    assert code == 401, body
    assert body == {"error": "unauthorized"}
    dispatcher.drain()
    assert (
        delivery(hub, number=pending["number"], topic=subscription["topic"])["status"]
        == "cancelled"
    )
    assert hits(hub, subscription["topic"]) == []
    assert (
        delivery(hub, number=control["number"], topic=control_subscription["topic"])["status"]
        == "accepted"
    )
    assert len(hits(hub, control_subscription["topic"])) == 1
    missed = ask(hub, worker)
    dispatcher.reconcile()
    dispatcher.drain()
    state = rows(hub, "phone_push_state")
    assert any(
        row["number"] == missed["number"] and row["category"] == "needs_you" for row in state
    ), state
    assert not any(row["number"] == missed["number"] for row in rows(hub, "phone_push_deliveries"))
    assert hits(hub, subscription["topic"]) == []
    login = cli(
        "--data",
        os.path.dirname(hub.db.path),
        "login-link",
        "owner@example.test",
        "--workspace",
        me["workspace"],
    )
    parts = urlsplit(login)
    code, headers, content = hub.request(parts.path + "?" + parts.query)
    assert code == 302, (headers, content)
    assert headers.get("Set-Cookie"), headers
    owner = headers["Set-Cookie"].split(";", 1)[0]
    positive_get(hub, owner)
    resumed = ask(hub, worker)
    dispatcher.reconcile()
    dispatcher.drain()
    assert delivery(hub, number=resumed["number"])["status"] == "accepted"
    requests = hits(hub, subscription["topic"])
    assert len(requests) == 1, requests
    assert requests[0].body == f"#{resumed['number']} needs you".encode()


@pytest.mark.parametrize(
    "reply,expected,error",
    [
        (429, "retry", "HTTP 429"),
        (401, "dead", "HTTP 401"),
        (404, "dead", "HTTP 404"),
        (413, "dead", "HTTP 413"),
        ((200, "ok", {}), "dead", "unexpected response"),
        ((200, {"id": "bad-id"}, {}), "dead", "unexpected response"),
        ((200, {"id": "a" * 33}, {}), "dead", "unexpected response"),
        ((200, ["abc123"], {}), "dead", "unexpected response"),
        ((200, " " * 4096 + '{"id":"abc123"}', {}), "dead", "unexpected response"),
    ],
)
def test_http_outcomes_and_public_status(hub, signup, monkeypatch, reply, expected, error):
    _, _, register = signup
    owner, _ = register(email="owner@example.test")
    positive_get(hub, owner)
    dispatcher = enable(monkeypatch, hub)
    on(hub, owner)
    accepted_control(hub, owner, dispatcher)
    hub.ntfy.replies.append(reply)
    queue_test(hub, owner)
    dispatcher.drain()
    found = rows(hub, "phone_push_deliveries")
    assert len(found) == 2, found
    outcome = next(row for row in found if row["status"] != "accepted")
    assert outcome["status"] == expected, outcome
    assert outcome["error"] == error
    assert outcome["attempt"] == 1
    assert outcome["http_status"] == (reply[0] if isinstance(reply, tuple) else reply)
    assert len(hub.ntfy.requests) == 2
    code, body = api(hub, owner, "GET", "/api/phone-push")
    assert code == 200, body
    assert body["test"]["status"] in {"pending", "not_accepted"}
    assert "deliver" not in json.dumps(body).lower()


def test_retry_then_acceptance_reuses_click(hub, signup, monkeypatch):
    _, _, register = signup
    owner, _ = register(email="owner@example.test")
    positive_get(hub, owner)
    dispatcher = enable(monkeypatch, hub)
    on(hub, owner)
    card = ask(hub, agent(hub, owner, "worker"))
    hub.ntfy.replies.extend([503, 200])
    dispatcher.reconcile()
    dispatcher.drain()
    outcome = delivery(hub, number=card["number"])
    assert outcome["status"] == "retry", outcome
    assert outcome["attempt"] == 1 and outcome["http_status"] == 503
    dispatcher.drain()
    outcome = delivery(hub, number=card["number"])
    assert outcome["status"] == "accepted", outcome
    assert outcome["attempt"] == 2 and outcome["http_status"] == 200
    assert outcome["accepted_at"] and not outcome["error"]
    assert len(hub.ntfy.requests) == 2
    assert hub.ntfy.requests[0].headers["Click"] == hub.ntfy.requests[1].headers["Click"]


def test_refused_connections_exhaust_attempts_without_leaking_errors(
    hub, signup, monkeypatch, caplog
):
    _, _, register = signup
    owner, _ = register(email="owner@example.test")
    positive_get(hub, owner)
    dispatcher = enable(monkeypatch, hub)
    on(hub, owner)
    accepted_control(hub, owner, dispatcher)
    sender = importlib.import_module("commonplace.phone_push.sender")
    original = sender.http.client.HTTPConnection.connect

    def refused(self):
        if self.port == hub.ntfy.server.server_port:
            raise ConnectionRefusedError("PRIVATE_CONNECTION_ERROR")
        return original(self)

    monkeypatch.setattr(sender.http.client.HTTPConnection, "connect", refused)
    queue_test(hub, owner)
    for attempt in range(1, 4):
        dispatcher.drain()
        found = rows(hub, "phone_push_deliveries")
        assert len(found) == 2, found
        outcome = next(row for row in found if row["status"] != "accepted")
        assert outcome["attempt"] == attempt
        assert outcome["status"] == ("dead" if attempt == 3 else "retry"), outcome
        assert outcome["error"] == "delivery connection failed"
        assert outcome["http_status"] is None
    assert len(hub.ntfy.requests) == 1
    assert "PRIVATE_CONNECTION_ERROR" not in json.dumps(found) + caplog.text


def test_redirect_is_dead_and_target_is_never_contacted(hub, signup, monkeypatch):
    _, _, register = signup
    owner, _ = register(email="owner@example.test")
    positive_get(hub, owner)
    dispatcher = enable(monkeypatch, hub)
    on(hub, owner)
    accepted_control(hub, owner, dispatcher)
    hub.ntfy.replies.append((302, {}, {"Location": hub.ntfy.redirect_url}))
    queue_test(hub, owner)
    dispatcher.drain()
    found = rows(hub, "phone_push_deliveries")
    assert len(found) == 2, found
    outcome = next(row for row in found if row["status"] != "accepted")
    assert outcome["status"] == "dead"
    assert outcome["http_status"] == 302
    assert outcome["error"] == "HTTP 302" and outcome["attempt"] == 1
    assert len(hub.ntfy.requests) == 2
    assert hub.ntfy.redirect_hits == 0


def test_test_delivery_and_workspace_scoped_opaque_click(hub, signup, monkeypatch):
    _, _, register = signup
    owner, _ = register(email="owner@example.test")
    positive_get(hub, owner)
    workspace = hub.me(owner)["workspace"]
    dispatcher = enable(monkeypatch, hub)
    subscription = on(hub, owner)
    accepted_control(hub, owner, dispatcher)
    request = hub.ntfy.requests[0]
    assert request.body == b"Test from Commonplace"
    assert request.headers["Priority"] == "default"
    assert "Click" not in request.headers
    other, _ = register(email="other@example.test")
    other_workspace = hub.me(other)["workspace"]
    other_subscription = on(hub, other)
    worker = agent(hub, owner, "worker")
    card = create(hub, owner, assignee="conn:" + worker.id, status="doing")
    code, done = api(
        hub,
        None,
        "PATCH",
        f"/api/cards/{card['number']}",
        {"version": card["version"], "status": "done"},
        token=worker.token,
    )
    assert code == 200, done
    opened = []
    original = board_service.open_store

    def recorded(workspace_dir, **kwargs):
        opened.append(str(workspace_dir))
        return original(workspace_dir, **kwargs)

    monkeypatch.setattr(board_service, "open_store", recorded)
    sender = importlib.import_module("commonplace.phone_push.sender")
    original_post = sender.post
    original_link = urls.share_link

    def post(*args, **kwargs):
        assert not hub.db._lock._is_owned()
        assert not hub.db._conn.in_transaction
        return original_post(*args, **kwargs)

    def link(*args, **kwargs):
        assert not hub.db._conn.in_transaction
        return original_link(*args, **kwargs)

    monkeypatch.setattr(sender, "post", post)
    monkeypatch.setattr(urls, "share_link", link)
    dispatcher.reconcile()
    dispatcher.drain()
    assert opened.count(str(hub.gateway.workspace_dir(workspace))) >= 1, opened
    assert opened.count(str(hub.gateway.workspace_dir(other_workspace))) == 0, opened
    assert hits(hub, other_subscription["topic"]) == []
    requests = hits(hub, subscription["topic"])
    assert len(requests) == 2, requests
    request = requests[-1]
    assert request.body == f"#{card['number']} is done".encode()
    assert request.headers["Priority"] == "default"
    assert "Authorization" not in request.headers
    click = request.headers["Click"]
    assert re.fullmatch(r"https://cp\.example/s/[A-Za-z0-9]{7}", click)
    code, headers, content = hub.request(urlsplit(click).path, cookie=owner)
    assert code == 302, (headers, content)
    assert headers["Location"] == f"https://cp.example/app/?w={workspace}#/card/{card['number']}"


@pytest.mark.parametrize("interrupted_attempt", [1, 3])
def test_recovery_retries_counted_claim_or_marks_exhausted_dead(
    hub, signup, monkeypatch, interrupted_attempt
):
    _, _, register = signup
    owner, _ = register(email="owner@example.test")
    positive_get(hub, owner)
    dispatcher = enable(monkeypatch, hub)
    on(hub, owner)
    queue_test(hub, owner)
    sender = importlib.import_module("commonplace.phone_push.sender")
    original = sender._outcome

    def interrupted(dispatcher, row, *args):
        if row["attempt"] == interrupted_attempt:
            raise RuntimeError("Synthetic interruption before outcome commit")
        return original(dispatcher, row, *args)

    monkeypatch.setattr(sender, "_outcome", interrupted)
    hub.ntfy.replies.extend([503] * (interrupted_attempt - 1) + [200])
    for _ in range(interrupted_attempt - 1):
        dispatcher.drain()
    with pytest.raises(RuntimeError, match="Synthetic interruption"):
        dispatcher.drain()
    outcome = delivery(hub, category="test")
    assert outcome["status"] == "sending", outcome
    assert outcome["attempt"] == interrupted_attempt
    assert len(hub.ntfy.requests) == interrupted_attempt
    monkeypatch.setattr(sender, "_outcome", original)
    module = importlib.import_module("commonplace.phone_push")
    module.stop(hub.gateway)
    module.install(hub.gateway)
    hub.gateway.phone_push.drain()
    outcome = delivery(hub, category="test")
    if interrupted_attempt == 1:
        assert outcome["status"] == "accepted", outcome
        assert outcome["attempt"] == 2
        assert len(hub.ntfy.requests) == 2
    else:
        assert outcome["status"] == "dead", outcome
        assert outcome["attempt"] == 3
        assert outcome["error"] == "delivery interrupted"
        assert len(hub.ntfy.requests) == 3


def test_drain_scans_needs_me_once_for_three_pending_cards(hub, signup, monkeypatch):
    _, _, register = signup
    owner, _ = register(email="owner@example.test")
    positive_get(hub, owner)
    dispatcher = enable(monkeypatch, hub)
    subscription = on(hub, owner)
    worker = agent(hub, owner, "worker")
    cards = [ask(hub, worker) for _ in range(3)]
    dispatcher.reconcile()
    pending = rows(hub, "phone_push_deliveries")
    assert len(pending) == 3, pending
    assert all(row["status"] == "pending" for row in pending)
    counter = s1_counter(monkeypatch)
    dispatcher.drain()
    assert counter.calls == 1
    requests = hits(hub, subscription["topic"])
    assert len(requests) == 3, requests
    assert {request.body for request in requests} == {
        f"#{card['number']} needs you".encode() for card in cards
    }
    assert all(row["status"] == "accepted" for row in rows(hub, "phone_push_deliveries"))


@pytest.mark.parametrize("phase", ["reconcile", "sweep", "outcome"])
def test_worker_survives_phase_exception(hub, signup, monkeypatch, caplog, phase):
    import threading

    owner, _ = signup[2]()
    positive_get(hub, owner)
    module = importlib.import_module("commonplace.phone_push.dispatcher")
    sender = importlib.import_module("commonplace.phone_push.sender")
    start = module.PushDispatcher.start
    dispatcher = enable(monkeypatch, hub)
    on(hub, owner)
    queue_test(hub, owner)
    dispatcher.sweep_seconds = 0.01
    failed, accepted = threading.Event(), threading.Event()
    target = sender if phase == "outcome" else dispatcher
    name = "_outcome" if phase == "outcome" else phase
    original = getattr(target, name)
    outcome = sender._outcome
    delays = []
    wait = dispatcher._stop.wait

    def backoff(delay):
        delays.append(delay)
        return wait(0.01)

    def fail_once(*args, **kwargs):
        if not failed.is_set():
            failed.set()
            raise RuntimeError("synthetic private exception text")
        return original(*args, **kwargs)

    def observed(*args):
        result = outcome(*args)
        if args[2] == "accepted":
            accepted.set()
        return result

    drain = dispatcher.drain

    def after_sweep():
        return drain() if failed.is_set() else 0

    if phase == "sweep":
        monkeypatch.setattr(dispatcher, "drain", after_sweep)
    monkeypatch.setattr(sender, "_outcome", observed)
    if phase == "outcome":
        original = observed
    monkeypatch.setattr(target, name, fail_once)
    monkeypatch.setattr(dispatcher._stop, "wait", backoff)
    start(dispatcher)
    try:
        dispatcher.wake()
        assert failed.wait(1), "failure phase was not reached"
        dispatcher.wake()
        assert accepted.wait(2), "worker did not recover after the next wake"
        assert dispatcher.thread.is_alive()
        assert delivery(hub, category="test")["status"] == "accepted"
        assert delays == [5]
        assert "phone push worker failed" in caplog.text
        assert "synthetic private exception text" not in caplog.text
    finally:
        dispatcher.stop()
    assert not dispatcher.thread.is_alive()


def test_stop_during_drain_bounds_join_and_leaves_remaining_pending(hub, signup, monkeypatch):
    import threading
    import time

    from commonplace import webhooks

    owner, _ = signup[2]()
    positive_get(hub, owner)
    module = importlib.import_module("commonplace.phone_push.dispatcher")
    start = module.PushDispatcher.start
    dispatcher = enable(monkeypatch, hub)
    on(hub, owner)
    for _ in range(3):
        queue_test(hub, owner)
    entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
    calls = []

    def stalled(*args, **kwargs):
        calls.append(1)
        entered.set()
        release.wait(5)
        return "accepted", 200, None

    sender = importlib.import_module("commonplace.phone_push.sender")
    monkeypatch.setattr(sender, "post", stalled)
    monkeypatch.setattr(webhooks, "HTTP_TIMEOUT", 0.05)
    start(dispatcher)
    stopper = threading.Thread(target=lambda: (dispatcher.stop(), stopped.set()))
    try:
        assert entered.wait(1), "transport was not reached"
        began = time.monotonic()
        stopper.start()
        assert stopped.wait(webhooks.HTTP_TIMEOUT + 2.5), "stop exceeded its bounded join"
        assert time.monotonic() - began < webhooks.HTTP_TIMEOUT + 2.5
    finally:
        release.set()
        stopper.join(timeout=2)
        dispatcher.stop()
    assert not dispatcher.thread.is_alive() and not stopper.is_alive()
    assert len(calls) == 1
    found = rows(hub, "phone_push_deliveries")
    assert [row["status"] for row in found] == ["accepted", "pending", "pending"]
    assert [row["attempt"] for row in found] == [1, 0, 0]


def test_recovery_uses_dispatcher_attempt_limit(hub, signup, monkeypatch):
    from commonplace import webhooks

    owner, _ = signup[2]()
    positive_get(hub, owner)
    enable(monkeypatch, hub)
    on(hub, owner)
    queue_test(hub, owner)
    with hub.db._lock, hub.db._tx() as conn:
        conn.execute("UPDATE phone_push_deliveries SET status='sending',attempt=2")
    module = importlib.import_module("commonplace.phone_push")
    module.stop(hub.gateway)
    constructor = module.PushDispatcher

    def limited(*args, **kwargs):
        return constructor(*args, **(kwargs | {"max_attempts": 2}))

    monkeypatch.setattr(module, "PushDispatcher", limited)
    assert webhooks.MAX_ATTEMPTS == 3
    module.install(hub.gateway)
    assert hub.gateway.phone_push.max_attempts == 2
    found = delivery(hub, category="test")
    assert found["status"] == "dead", found
    assert found["attempt"] == 2 and found["error"] == "delivery interrupted"
    assert hub.gateway.phone_push.drain() == 0 and not hub.ntfy.requests


def test_recheck_failure_releases_claim_without_spending_attempt(hub, signup, monkeypatch):
    import time

    owner, _ = signup[2]()
    positive_get(hub, owner)
    dispatcher = enable(monkeypatch, hub)
    on(hub, owner)
    queue_test(hub, owner)
    eligibility = importlib.import_module("commonplace.phone_push.eligibility")
    original = eligibility.owner_principal
    calls = []

    def fail_once(*args):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("synthetic recheck failure")
        return original(*args)

    monkeypatch.setattr(eligibility, "owner_principal", fail_once)
    began = time.time()
    assert dispatcher.drain() == 1
    found = delivery(hub, category="test")
    assert found["status"] == "pending", found
    assert found["attempt"] == 0 and found["error"] is None and found["http_status"] is None
    assert began < found["next_at"] <= time.time() + 5
    assert not hub.ntfy.requests
    now = time.time()
    with monkeypatch.context() as clock_patch:
        clock_patch.setattr(time, "time", lambda: now + 6)
        assert dispatcher.drain() == 1
    found = delivery(hub, category="test")
    assert found["status"] == "accepted" and found["attempt"] == 1, found
    assert len(hub.ntfy.requests) == 1
