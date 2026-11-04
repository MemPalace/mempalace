"""W3: real HTTP/MCP, durable retries, filtering and ownership."""

import hashlib
import hmac
import http.client
import json
import queue
import sys
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from commonplace import events, sessions, webhooks
from commonplace.cli import main
from commonplace.control_db import ControlDB
from commonplace.extensions import ToolError
from tests._commonplace_helpers import hub_fixture  # noqa: F401


@pytest.fixture
def receiver():
    requests = queue.Queue()
    statuses = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            requests.put((dict(self.headers), body))
            self.send_response(statuses.pop(0) if statuses else 204)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/hook", requests, statuses
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture
def live(hub):
    from commonplace.boards.service import BoardService
    from commonplace.extensions import ToolContext

    owner = sessions.person_principal(hub.db, hub.admin, hub.workspace)
    BoardService(ToolContext(principal=owner, gateway=hub.gateway)).board_add(
        slug="work", wing="shared"
    )
    try:
        yield hub
    finally:
        webhooks.stop(hub.gateway)


def principal(hub, token=None):
    return hub.db.authenticate_token(token or hub.owner_token())


def rest(hub, method, path="/api/webhooks", token=None, body=None, cookie=None):
    conn = http.client.HTTPConnection("127.0.0.1", hub.port, timeout=3)
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if cookie:
        headers["Cookie"] = sessions.COOKIE + "=" + cookie
    try:
        conn.request(method, path, json.dumps(body) if body is not None else None, headers)
        response = conn.getresponse()
        return response.status, json.loads(response.read())
    finally:
        conn.close()


def rpc(hub, token, name, **args):
    return hub.rpc(token, "tools/call", {"name": name, "arguments": args})


def wait_for(fn, timeout=3):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        value = fn()
        if value:
            return value
        threading.Event().wait(0.01)
    raise AssertionError("condition not met")


def delivery_state(hub, p, wid, state):
    rows = webhooks.deliveries(hub.db, p, wid)
    return rows if rows and rows[0]["status"] == state else None


def emit(hub, kind="card.updated", **data):
    return events.emit(
        hub.db,
        hub.workspace,
        kind,
        subject="#7",
        actor="conn:agent",
        data={"board": "work", "number": 7, **data},
    )


def test_signature_payload_and_routes(live, receiver, monkeypatch):
    hub = live
    url, requests, _ = receiver
    monkeypatch.setenv("COMMONPLACE_BASE_URL", "https://memory.example")
    # The W8 module may already be installed in the merged gateway.
    import commonplace

    monkeypatch.setattr(
        commonplace,
        "urls",
        types.SimpleNamespace(share_link=lambda db, ws, path: "https://memory.example" + path),
        raising=False,
    )
    token = hub.owner_token()
    status, hook = rest(hub, "POST", token=token, body={"url": url, "events": ["card_changed"]})
    assert status == 201
    assert set(hook) == {"id", "secret"}
    event = emit(
        hub,
        title="A card",
        body="SECRET MEMORY",
        text="SECRET MEMORY",
        nested={"text": "SECRET MEMORY"},
    )
    headers, raw = requests.get(timeout=3)
    payload = json.loads(raw)
    assert payload == {
        "agent": "commonplace",
        "task": "#7 A card",
        "need": "info",
        "message": '#7 "A card" updated https://memory.example/app/#/card/7',
        "id": f"evt_{event['seq']}",
        "seq": event["seq"],
        "kind": "card.updated",
        "at": event["at"],
        "workspace": hub.workspace,
        "subject": "#7",
        "actor": "conn:agent",
        "data": {"board": "work", "number": 7, "title": "A card"},
        "url": "https://memory.example/app/#/card/7",
    }
    assert b"SECRET MEMORY" not in raw
    timestamp, signature = headers["X-Commonplace-Signature"].split(",")
    expected = hmac.new(
        hook["secret"].encode(), timestamp[2:].encode() + b"." + raw, hashlib.sha256
    ).hexdigest()
    assert hmac.compare_digest(signature, "v1=" + expected)
    assert headers["X-Commonplace-Event"] == "card.updated"
    p = principal(hub, token)
    rows = wait_for(lambda: delivery_state(hub, p, hook["id"], "delivered"))
    assert headers["X-Commonplace-Delivery"] == rows[0]["id"]
    assert rows[0]["attempt"] == 1
    status, result = rest(hub, "GET", token=token)
    assert status == 200 and len(result["webhooks"]) == 1
    assert "secret" not in json.dumps(result)
    status, result = rest(hub, "GET", f"/api/webhooks/{hook['id']}/deliveries", token)
    assert status == 200 and result["deliveries"][0]["attempts"][0]["http_status"] == 204
    status, result = rest(hub, "POST", f"/api/webhooks/{hook['id']}/test", token)
    assert status == 202 and result["ok"] is True
    assert result["delivery"]["id"].startswith("dlv_")
    requests.get(timeout=3)
    assert rest(hub, "DELETE", f"/api/webhooks/{hook['id']}", token) == (200, {"ok": True})
    assert rest(hub, "GET", token=token)[1] == {"webhooks": []}


def test_retry_then_success(live, receiver):
    hub = live
    url, requests, statuses = receiver
    statuses.extend([503, 204])
    hub.gateway.webhooks.schedule = (0.01,)
    p = principal(hub)
    hook = webhooks.add(hub.db, p, url, kinds=["card.updated"])
    emit(hub)
    first = requests.get(timeout=3)
    second = requests.get(timeout=3)
    assert first[1] == second[1]
    assert first[0]["X-Commonplace-Delivery"] == second[0]["X-Commonplace-Delivery"]
    rows = wait_for(lambda: delivery_state(hub, p, hook["id"], "delivered"))
    assert rows[0]["attempt"] == 2
    assert [a["http_status"] for a in rows[0]["attempts"]] == [503, 204]


def test_dead_failure_notification_and_no_feedback_loop(live, receiver):
    hub = live
    url, _, statuses = receiver
    statuses.extend([500] * 20)
    hub.gateway.webhooks.schedule = (0.001,)
    hub.gateway.webhooks.max_attempts = 2
    p = principal(hub)
    hook = webhooks.add(hub.db, p, url)
    emit(hub)

    def finished():
        rows = webhooks.deliveries(hub.db, p, hook["id"])
        return rows if len(rows) == 2 and all(r["status"] == "dead" for r in rows) else None

    rows = wait_for(finished)
    assert all(row["attempt"] == 2 for row in rows)
    failures = [e for e in events.after(hub.db, hub.workspace) if e["kind"] == "webhook.failed"]
    assert len(failures) == 1
    assert failures[0]["data"]["webhook_id"] == hook["id"]
    assert all(len(row["attempts"]) == 2 for row in rows)


def test_filters_and_presets(live, receiver):
    hub = live
    webhooks.stop(hub.gateway)
    url, _, _ = receiver
    p = principal(hub)
    hooks = [
        webhooks.add(hub.db, p, url, kinds=["card_changed"], board="work"),
        webhooks.add(hub.db, p, url, kinds=["review_needed"]),
        webhooks.add(hub.db, p, url, kinds=["waiting_on_me"]),
        webhooks.add(hub.db, p, url, waiting_on="user:avery"),
    ]
    listener = hub.gateway.webhooks.on_event
    cases = [
        ("card.updated", {}, [0]),
        ("card.review_needed", {"ask_to": "user:avery"}, [0, 1, 3]),
        ("card.handoff", {"assignee": p.actor}, [0, 2]),
        ("card.decided", {"return_to": p.actor}, [0, 2]),
        ("card.handoff", {"ask_to": "user:avery"}, [0, 3]),
        ("memory.saved", {"assignee": "user:avery"}, []),
        ("card.updated", {"board": "other"}, []),
    ]
    counts = [0] * len(hooks)
    for kind, data, matches in cases:
        listener(emit(hub, kind, **data))
        for index in matches:
            counts[index] += 1
    assert [len(webhooks.deliveries(hub.db, p, h["id"])) for h in hooks] == counts


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/hook",
        "https://10.0.0.1/hook",
        "http://localhost/hook",
        "http://127.0.0.1:99/hook",
        "http://127.10.2.3/hook",
        "http://[::1]/hook",
        "http://host.ts.net/hook",
        "http://HOST.TS.NET./hook",
        "http://100.64.0.0/hook",
        "http://100.127.255.255/hook",
    ],
)
def test_url_allowed(url):
    assert webhooks.validate_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "http://10.0.0.1",
        "http://192.168.1.1",
        "http://172.16.0.1",
        "http://100.63.255.255",
        "http://100.128.0.0",
        "http://ts.net",
        "http://fake-ts.net",
        "http://host.ts.net.evil.com",
        "http://localhost.evil.com",
        "http://127.0.0.1.evil.com",
        "http://[::ffff:127.0.0.1]",
        "http://[fe80::1%25eth0]",
        "ftp://127.0.0.1",
        "https://user:pass@example.com",
        "https://user@example.com",
        "http://127.1",
        "http://2130706433",
        "https://example.com/#fragment",
        "https://example.com:bad",
        "https://example.com:0",
        "https://",
        "http://localhost/\r\nX:foo",
        "http://localhost\\@evil.com",
    ],
)
def test_url_refused(url):
    with pytest.raises(ToolError):
        webhooks.validate_url(url)


def test_tool_gates_and_visibility(live, receiver):
    hub = live
    url, _, _ = receiver
    trusted = hub.owner_token()
    untrusted = hub.token("untrusted", profile="full", trust="untrusted", write_wing="inbox")
    slim = hub.token("slim", trust="trusted", profile="slim")
    no_scope = hub.token("no-scope", trust="trusted", profile="full", scopes=[])
    for token in (untrusted, slim, no_scope):
        assert "webhook_add" not in hub.tool_names(token)
        for name in ("add", "list", "remove", "test"):
            assert "error" in rpc(hub, token, "webhook_" + name, url=url, id="wh_missing")
        assert rest(hub, "GET", token=token)[0] == 403
    assert "webhook_add" in hub.tool_names(trusted)
    response = rpc(hub, trusted, "webhook_add", url=url, events=["review_needed"])
    text = response["result"]["content"][0]["text"]
    wid = text.split()[0]
    assert " secret " in text
    response = rpc(hub, trusted, "webhook_list")
    assert wid in json.dumps(response) and "secret" not in json.dumps(response)
    assert "error" not in rpc(hub, trusted, "webhook_remove", id=wid)


def test_connection_and_workspace_ownership_and_sessions(live, receiver):
    hub = live
    url, _, _ = receiver
    owner = hub.owner_token()
    other = hub.owner_token("other")
    p = principal(hub, owner)
    hook = webhooks.add(hub.db, p, url)
    # An owner-held token still sees only its own registrations.
    assert rest(hub, "GET", token=other)[1] == {"webhooks": []}
    for method, suffix in (("DELETE", ""), ("POST", "/test"), ("GET", "/deliveries")):
        assert rest(hub, method, "/api/webhooks/" + hook["id"] + suffix, other)[0] == 404
    link = sessions.create_login_link(hub.db, "owner@example.com", hub.workspace)
    cookie = sessions.redeem_login_link(hub.db, link)
    assert len(rest(hub, "GET", cookie=cookie)[1]["webhooks"]) == 1
    elsewhere = hub.db.create_workspace("elsewhere", hub.admin)
    outsider = hub.token("other-ws", workspace=elsewhere, profile="full", trust="trusted")
    assert rest(hub, "GET", token=outsider)[1] == {"webhooks": []}
    assert rest(hub, "DELETE", "/api/webhooks/" + hook["id"], outsider)[0] == 404
    assert rest(hub, "GET")[0] == 401


def test_restart_resumes_persisted_delivery(live, receiver):
    hub = live
    url, requests, statuses = receiver
    statuses.append(503)
    hub.gateway.webhooks.schedule = (60,)
    token = hub.owner_token()
    p = principal(hub, token)
    hook = webhooks.add(hub.db, p, url, kinds=["card.updated"])
    emit(hub)
    first = requests.get(timeout=3)
    wait_for(lambda: delivery_state(hub, p, hook["id"], "retry"))
    webhooks.stop(hub.gateway)
    with hub.db._lock:
        hub.db._conn.execute("UPDATE webhook_deliveries SET next_at = ?", (time.time(),))
    # A new database connection and dispatcher read the same durable queue.
    restarted_db = ControlDB(hub.db.path)
    dispatcher = webhooks.Dispatcher(restarted_db, gateway=hub.gateway, schedule=(0.001,)).start()
    try:
        second = requests.get(timeout=3)
        assert first[1] == second[1]
        rows = wait_for(lambda: delivery_state(hub, p, hook["id"], "delivered"))
        assert rows[0]["attempt"] == 2
    finally:
        dispatcher.stop()
        restarted_db.close()


def test_redirect_is_not_followed(live, receiver):
    hub = live
    url, requests, statuses = receiver
    statuses.append(302)
    hub.gateway.webhooks.max_attempts = 1
    p = principal(hub)
    hook = webhooks.add(hub.db, p, url, kinds=["card.updated"])
    emit(hub)
    requests.get(timeout=3)
    rows = wait_for(lambda: delivery_state(hub, p, hook["id"], "dead"))
    assert rows[0]["http_status"] == 302
    assert requests.empty()


def test_cli_add_list_remove(tmp_path, capsys):
    data = tmp_path / "data"
    prefix = ["--data", str(data)]
    assert main(prefix + ["init", "--admin-email", "owner@example.com", "--workspace", "home"]) == 0
    capsys.readouterr()
    assert (
        main(
            prefix
            + [
                "webhook",
                "add",
                "http://localhost:9999/hook",
                "--workspace",
                "home",
                "--events",
                "waiting_on_me",
            ]
        )
        == 0
    )
    hook = json.loads(capsys.readouterr().out)
    assert hook["waiting_on"].startswith("user:") and hook["secret"]
    assert main(prefix + ["webhook", "list", "--workspace", "home"]) == 0
    output = capsys.readouterr().out
    assert hook["id"] in output and "secret" not in output
    assert main(prefix + ["webhook", "remove", hook["id"], "--workspace", "home"]) == 0
    assert main(prefix + ["webhook", "add", "http://example.com", "--workspace", "home"]) == 2


def test_shared_links_and_app_url_fallback(live, receiver, monkeypatch):
    import commonplace

    hub = live
    url, requests, _ = receiver
    p = principal(hub)
    webhooks.add(hub.db, p, url, kinds=["card.updated"])
    # Exercise import fallback even after W8 is merged.
    monkeypatch.delattr(commonplace, "urls", raising=False)
    monkeypatch.setitem(sys.modules, "commonplace.urls", None)
    monkeypatch.setenv("COMMONPLACE_APP_URL", "https://app.example")
    monkeypatch.setenv("COMMONPLACE_BASE_URL", "https://old.example")
    emit(hub)
    assert json.loads(requests.get(timeout=3)[1])["url"] == "https://app.example/app/#/card/7"
    calls = []

    def share_link(db, workspace_id, path):
        calls.append((db, workspace_id, path))
        return "https://agent.example/s/Ab12Cd3"

    monkeypatch.setattr(
        commonplace, "urls", types.SimpleNamespace(share_link=share_link), raising=False
    )
    emit(hub)
    assert json.loads(requests.get(timeout=3)[1])["url"] == "https://agent.example/s/Ab12Cd3"
    assert calls == [(hub.db, hub.workspace, "/app/#/card/7")]


@pytest.fixture
def security(tmp_path):
    from types import SimpleNamespace

    from commonplace.boards.service import BoardService
    from commonplace.extensions import ToolContext
    from commonplace.policy import Policy

    db = ControlDB(str(tmp_path / "control.sqlite3"))
    uid = db.create_user("security@example.com", is_admin=True)
    ws = db.create_workspace("security", uid)
    gateway = SimpleNamespace(db=db, workspace_dir=lambda _: str(tmp_path / "workspace"))
    person = sessions.person_principal(db, uid, ws)
    owner = BoardService(ToolContext(principal=person, gateway=gateway))
    owner.board_add(slug="work", wing="shared")
    owner.board_add(slug="medical", wing="private")
    cid, token = db.create_token(
        workspace_ref=ws,
        user_ref=uid,
        name="agent",
        client="test",
        actor=uid,
        policy=Policy.build(
            profile="full",
            trust="trusted",
            scopes=["memory:read", "memory:write"],
            read_wings=["shared"],
            write_wings=["shared"],
        ),
    )
    agent = db.authenticate_token(token)
    webhooks.ensure_schema(db)
    events.ensure_schema(db)
    hook = webhooks.add(db, agent, "http://127.0.0.1:9/hook")
    dispatcher = webhooks.Dispatcher(db)
    # Set before authorization is implemented, keeping this a behavioral regression.
    dispatcher.gateway = gateway
    result = SimpleNamespace(
        db=db,
        uid=uid,
        ws=ws,
        cid=cid,
        agent=agent,
        person=person,
        owner=owner,
        hook=hook,
        dispatcher=dispatcher,
        gateway=gateway,
    )
    try:
        yield result
    finally:
        db.close()


def security_event(env, board="work", kind="card.updated"):
    return events.emit(
        env.db,
        env.ws,
        kind,
        subject="#1",
        actor="system",
        data={} if board is None else {"board": board, "number": 1, "title": "PRIVATE-CANARY"},
    )


@pytest.mark.parametrize(
    "board,kind",
    [
        ("medical", "card.updated"),
        ("missing", "card.updated"),
        (None, "memory.saved"),
        ("", "card.updated"),
    ],
)
def test_webhook_enqueue_requires_current_read_access(security, board, kind):
    s = security
    s.dispatcher.on_event(security_event(s, board, kind))
    assert s.dispatcher._due() is None
    assert webhooks.deliveries(s.db, s.agent, s.hook["id"]) == []


def invalidate_owner(s, reason):
    with s.db._lock:
        if reason == "revoked":
            s.db.revoke(s.cid, actor=s.uid)
        elif reason == "expired":
            s.db._conn.execute(
                "UPDATE connections SET expires_at='2000-01-01T00:00:00Z' WHERE id=?", (s.cid,)
            )
        elif reason == "disabled":
            s.db._conn.execute(
                "UPDATE users SET disabled_at='2000-01-01T00:00:00Z' WHERE id=?", (s.uid,)
            )
        elif reason == "membership":
            # Model stale connections left by administrative/older tooling.
            s.db._conn.execute("PRAGMA foreign_keys=OFF")
            try:
                s.db._conn.execute(
                    "DELETE FROM memberships WHERE workspace_id=? AND user_id=?", (s.ws, s.uid)
                )
            finally:
                s.db._conn.execute("PRAGMA foreign_keys=ON")
        elif reason == "policy":
            from dataclasses import replace

            policy = replace(s.agent.policy, read_wings=("elsewhere",))
            s.db._conn.execute(
                "UPDATE connections SET policy=? WHERE id=?", (policy.to_json(), s.cid)
            )
        elif reason == "deleted":
            webhooks.remove(s.db, s.agent, s.hook["id"])
        elif reason == "wing":
            with s.owner.store.transaction() as conn:
                conn.execute("UPDATE boards SET wing='private' WHERE slug='work'")
        elif reason == "lookup":
            s.gateway.workspace_dir = lambda _: (_ for _ in ()).throw(
                RuntimeError("PRIVATE-CANARY")
            )


@pytest.mark.parametrize(
    "reason",
    ["revoked", "expired", "disabled", "membership", "policy", "deleted", "wing", "lookup"],
)
@pytest.mark.parametrize("stage", ["enqueue", "attempt", "retry"])
def test_webhook_rechecks_owner_and_cancels_stale_deliveries(security, monkeypatch, reason, stage):
    s = security
    sent = []
    monkeypatch.setattr(webhooks, "_post", lambda *args: sent.append(args) or 503)
    event = security_event(s)
    if stage != "enqueue":
        s.dispatcher.on_event(event)
        row = s.dispatcher._due()
        assert row is not None
        if stage == "retry":
            s.dispatcher.schedule = (0,)
            s.dispatcher._deliver(row)
            row = s.dispatcher._due()
            assert len(sent) == 1 and row is not None
            sent.clear()
    invalidate_owner(s, reason)
    if stage == "enqueue":
        s.dispatcher.on_event(event)
        assert s.dispatcher._due() is None
        assert s.db._conn.execute("SELECT count(*) FROM webhook_deliveries").fetchone()[0] == 0
    else:
        s.dispatcher._deliver(row)
        actual = s.db._conn.execute(
            "SELECT status,attempt FROM webhook_deliveries WHERE id=?", (row["id"],)
        ).fetchone()
        assert actual["status"] == "cancelled"
        assert actual["attempt"] == (1 if stage == "retry" else 0)
    assert sent == []


def test_person_webhook_rebuilds_person_principal_and_rechecks_membership(security, monkeypatch):
    s = security
    webhooks.remove(s.db, s.agent, s.hook["id"])
    hook = webhooks.add(s.db, s.person, "http://127.0.0.1:9/hook")
    sent = []
    monkeypatch.setattr(webhooks, "_post", lambda *args: sent.append(args) or 204)
    s.dispatcher.on_event(security_event(s, "medical"))
    row = s.dispatcher._due()
    assert row is not None and row["webhook_id"] == hook["id"]
    s.dispatcher._deliver(row)
    assert len(sent) == 1
    s.dispatcher.on_event(security_event(s, None, "memory.saved"))
    row = s.dispatcher._due()
    assert row is not None
    invalidate_owner(s, "membership")
    s.dispatcher._deliver(row)
    assert len(sent) == 1
    assert (
        s.db._conn.execute(
            "SELECT status FROM webhook_deliveries WHERE id=?", (row["id"],)
        ).fetchone()[0]
        == "cancelled"
    )


def test_dispatcher_replays_committed_events_idempotently_from_durable_cursor(
    security, monkeypatch
):
    s = security
    first = security_event(s)
    original_enqueue = webhooks._enqueue

    def crash_after_enqueue(*args, **kwargs):
        original_enqueue(*args, **kwargs)
        raise RuntimeError("crash before cursor advance")

    with monkeypatch.context() as patch:
        patch.setattr(webhooks, "_enqueue", crash_after_enqueue)
        with pytest.raises(RuntimeError):
            s.dispatcher.on_event(first)
    second = security_event(s)
    late = webhooks.add(s.db, s.agent, "http://127.0.0.1:9/late")
    third = security_event(s)
    sent = []
    monkeypatch.setattr(webhooks, "_post", lambda *args: sent.append(args) or 204)
    reopened = ControlDB(s.db.path)
    dispatcher = webhooks.Dispatcher(reopened)
    dispatcher.gateway = s.gateway
    try:
        dispatcher.start()
        wait_for(lambda: len(sent) == 4)
        # The dispatcher thread shares this connection and polls it under _lock. Reading it
        # without the lock races that thread and can return a wrong value ('' == '3').
        with reopened._lock:
            rows = reopened._conn.execute(
                "SELECT webhook_id,event_seq FROM webhook_deliveries ORDER BY event_seq,webhook_id"
            ).fetchall()
        assert {(r["webhook_id"], r["event_seq"]) for r in rows} == {
            (s.hook["id"], e["seq"]) for e in (first, second, third)
        } | {(late["id"], third["seq"])}
        assert len(rows) == 4
        dispatcher.on_event(first)
        with reopened._lock:
            count = reopened._conn.execute("SELECT count(*) FROM webhook_deliveries").fetchone()[0]
            cursor = reopened._conn.execute(
                "SELECT value FROM meta WHERE key='webhook_dispatch_seq'"
            ).fetchone()[0]
        assert count == 4
        assert cursor == str(third["seq"])
    finally:
        dispatcher.stop()
        reopened.close()


def test_dispatcher_out_of_order_notifications_do_not_skip_events(security):
    s = security
    first, second = security_event(s), security_event(s)
    s.dispatcher.on_event(second)
    s.dispatcher.on_event(first)
    assert {r["event_seq"] for r in webhooks.deliveries(s.db, s.agent, s.hook["id"])} == {
        first["seq"],
        second["seq"],
    }


def test_committed_board_event_recovers_when_listener_enqueue_fails(security, monkeypatch, caplog):
    s = security
    before = events.latest_seq(s.db, s.ws)

    def fail(*args):
        raise RuntimeError("PRIVATE-CANARY")

    events.add_listener(s.dispatcher.on_event)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(webhooks, "_enqueue", fail)
            created = s.owner.task_add(board="work", title="PRIVATE-CANARY")
    finally:
        events.remove_listener(s.dispatcher.on_event)
    persisted = events.after(s.db, s.ws, before)
    assert len(persisted) == 1 and persisted[0]["subject"] == f"#{created['number']}"
    assert s.db._conn.execute("SELECT count(*) FROM webhook_deliveries").fetchone()[0] == 0
    assert "PRIVATE-CANARY" not in caplog.text
    sent = []
    monkeypatch.setattr(webhooks, "_post", lambda *args: sent.append(args) or 204)
    reopened = ControlDB(s.db.path)
    dispatcher = webhooks.Dispatcher(reopened, gateway=s.gateway)
    try:
        dispatcher.start()
        wait_for(lambda: len(sent) == 1)
        assert json.loads(sent[0][1])["seq"] == persisted[0]["seq"]
        assert s.owner.task_show(created["number"])["title"] == "PRIVATE-CANARY"
    finally:
        dispatcher.stop()
        reopened.close()


def test_auth_header_is_sent_and_never_shown(live, receiver):
    hub = live
    url, requests, _ = receiver
    token = hub.owner_token()
    status, hook = rest(
        hub,
        "POST",
        token=token,
        body={"url": url, "events": ["review_needed"], "auth": "Bearer coordinator-wake-key"},
    )
    assert status == 201
    emit(hub, kind="card.review_needed", title="Pick one", ask_to="user:usr_x")
    headers, raw = requests.get(timeout=3)
    assert headers["Authorization"] == "Bearer coordinator-wake-key"
    payload = json.loads(raw)
    assert payload["need"] == "decision" and payload["message"].startswith(
        '#7 "Pick one" waits on you'
    )
    listed = json.dumps(rest(hub, "GET", token=token)[1])
    assert "coordinator-wake-key" not in listed
    status, _ = rest(hub, "POST", token=token, body={"url": url, "auth": "X-Bad\r\nInjected: yes"})
    assert status == 403 or status == 400


def test_env_auth_reference_keeps_the_secret_out_of_the_database(live, receiver, monkeypatch):
    hub = live
    url, requests, _ = receiver
    monkeypatch.setenv("COMMONPLACE_WEBHOOK_AUTH_COORDINATOR", "Bearer from-env-only")
    token = hub.owner_token()
    status, hook = rest(
        hub,
        "POST",
        token=token,
        body={
            "url": url,
            "events": ["review_needed"],
            "auth": "env:COMMONPLACE_WEBHOOK_AUTH_COORDINATOR",
        },
    )
    assert status == 201
    stored = hub.db._conn.execute(
        "SELECT auth_header FROM webhooks WHERE id=?", (hook["id"],)
    ).fetchone()[0]
    assert "from-env-only" not in stored
    emit(hub, kind="card.review_needed", title="Pick one", ask_to="user:usr_x")
    headers, raw = requests.get(timeout=3)
    assert headers["Authorization"] == "Bearer from-env-only"
    payload = json.loads(raw)
    assert payload["task"] == "#7 Pick one" and payload["need"] == "decision"
    for bad in ("env:OPENAI_API_KEY", "env:COMMONPLACE_DATA"):
        status, _ = rest(hub, "POST", token=token, body={"url": url, "auth": bad})
        assert status in (400, 403)
    status, result = rest(hub, "POST", f"/api/webhooks/{hook['id']}/test", token)
    assert status == 202
    _headers, raw = requests.get(timeout=3)
    assert json.loads(raw)["message"].startswith("test wake ")


def test_scoped_owner_can_test_its_own_webhook(live, receiver):
    hub = live
    url, requests, _ = receiver
    token = hub.token(
        "scoped-hook",
        profile="full",
        scopes=["memory:read"],
        read_wings=["shared"],
        trust="trusted",
    )
    status, hook = rest(hub, "POST", token=token, body={"url": url, "events": ["review_needed"]})
    assert status == 201
    status, _ = rest(hub, "POST", f"/api/webhooks/{hook['id']}/test", token)
    assert status == 202
    _headers, raw = requests.get(timeout=3)
    payload = json.loads(raw)
    assert payload["message"].startswith("test wake ") and payload["data"] == {
        "webhook_id": hook["id"]
    }
