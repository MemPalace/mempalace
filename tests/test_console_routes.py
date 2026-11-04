"""U4 authority and HTTP contracts with the other console units faked."""

import http.client
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from commonplace.console import authority, routes, settings, store, stream, types
from commonplace.control_db import _iso
from tests._console_helpers import console_hub_fixture  # noqa: F401


@pytest.fixture
def upstream(monkeypatch):
    recording = set()
    latest = {}
    activity = SimpleNamespace(latest_at=Mock(side_effect=lambda cids: dict(latest)))
    peek = Mock(return_value=activity)
    page = Mock(return_value={"items": [], "cursor": "positions", "more": False, "output": []})

    def set_recording(db, principal, cid, record):
        row = db.connection(cid, principal.workspace_id)
        assert principal.person and row["user_id"] == principal.user_id
        if record:
            recording.add(cid)
        else:
            recording.discard(cid)

    setter = Mock(side_effect=set_recording)
    monkeypatch.setattr(settings, "recording_ids", lambda db, ws: set(recording))
    monkeypatch.setattr(settings, "set_recording", setter)
    monkeypatch.setattr(store, "peek", peek)
    monkeypatch.setattr(stream, "page", page)
    return SimpleNamespace(
        recording=recording, latest=latest, activity=activity, peek=peek, page=page, setter=setter
    )


def raw_post(hub, auth, path, body):
    connection = http.client.HTTPConnection("127.0.0.1", hub.port, timeout=5)
    headers = {"Content-Type": "application/json"}
    if isinstance(auth, dict):
        headers.update(auth)
    elif auth:
        headers["Authorization"] = "Bearer " + auth
    try:
        connection.request("POST", path, body, headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


@pytest.mark.parametrize(
    "seconds, expected",
    [
        (None, "unused"),
        (-1, "working"),
        (0, "working"),
        (120, "working"),
        (121, "idle"),
        (1800, "idle"),
        (1801, "quiet"),
    ],
)
def test_status_boundaries(seconds, expected):
    now = datetime(2026, 11, 4, tzinfo=timezone.utc)
    seen = _iso(now - timedelta(seconds=seconds)) if seconds is not None else None
    assert authority.status(seen, now) == expected


def test_agent_set_and_views(console_hub, upstream):
    h = console_hub
    child, _ = h.db.create_token(
        workspace_ref=h.workspace,
        user_ref=h.admin,
        name="child",
        client="test",
        policy=h.principal(h.t1).policy,
        actor=h.admin,
        parent=h.a1,
    )
    machine, _ = h.agent(h.admin, "machine")
    worker, _ = h.agent(h.admin, "worker")
    expired, _ = h.agent(h.admin, "expired")
    revoked, _ = h.agent(h.admin, "revoked")
    foreign, _ = h.db.create_token(
        workspace_ref=h.other_workspace,
        user_ref=h.other_user,
        name="foreign",
        client="test",
        policy=h.principal(h.t1).policy,
        actor=h.other_user,
    )
    with h.db._lock, h.db._conn:
        h.db._conn.executemany(
            "INSERT INTO runner_connections VALUES (?, 'test-machine', ?)",
            [(machine, "machine"), (worker, "worker")],
        )
        h.db._conn.execute(
            "UPDATE connections SET expires_at='2000-01-01T00:00:00Z' WHERE id=?", (expired,)
        )
        h.db._conn.execute(
            "UPDATE connections SET revoked_at='2000-01-01T00:00:00Z' WHERE id=?", (revoked,)
        )
    upstream.recording.update([h.a1, h.m1])
    views = authority.agents(h.gateway, h.person(h.admin))
    by_id = {view.id: view for view in views}
    assert set(by_id) == {h.a1, h.a2, h.m1, child, worker}
    assert by_id[child].parent_id == h.a1
    assert by_id[h.a1].own and by_id[h.a1].recording is True
    assert by_id[h.a2].recording is False
    assert by_id[h.m1].own is False and by_id[h.m1].recording is None
    assert by_id[worker].own
    member_views = authority.agents(h.gateway, h.person(h.member_user))
    assert [view.id for view in member_views] == [h.m1]
    for cid in (machine, expired, revoked, foreign, "unknown", "a1"):
        with pytest.raises(LookupError, match="Agent not found"):
            authority.agent(h.gateway, h.person(h.admin), cid)
    with pytest.raises(types.ConsoleDenied):
        authority.agents(h.gateway, h.principal(h.t1))


def test_query_order_cap_then_expiry_filter_then_visible_cap(console_hub, upstream):
    h = console_hub
    template = dict(h.db.connection(h.a1, h.workspace))
    template.update(last_used_at=None, expires_at=None)
    rows = []
    for index in range(510):
        row = dict(template, id=f"cap-{index}", name=f"cap-{index}", token_hash=f"hash-{index}")
        row["created_at"] = _iso(
            datetime(2000, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=index)
        )
        if index < 301:
            row["expires_at"] = "2001-01-01T00:00:00Z"
        rows.append(tuple(row.values()))
    with h.db._lock, h.db._conn:
        h.db._conn.execute("UPDATE connections SET last_used_at=NULL")
        h.db._conn.executemany(
            f"INSERT INTO connections ({','.join(template)}) VALUES ({','.join('?' for _ in template)})",
            rows,
        )
    views = authority.agents(h.gateway, h.person(h.admin))
    # The first 500 rows include 301 expired rows; no refill past the query cap.
    assert len(views) == 199
    assert views[0].id == "cap-301" and views[-1].id == "cap-499"
    with h.db._lock, h.db._conn:
        h.db._conn.execute("UPDATE connections SET expires_at=NULL")
        h.db._conn.execute(
            "UPDATE connections SET last_used_at='2026-11-04T00:00:00Z' WHERE id=?", (h.a2,)
        )
        h.db._conn.execute(
            "UPDATE connections SET last_used_at='2026-11-03T00:00:00Z' WHERE id=?", (h.a1,)
        )
    views = authority.agents(h.gateway, h.person(h.admin))
    assert len(views) == 200
    assert [view.id for view in views[:3]] == [h.a2, h.a1, "cap-0"]


def test_list_batches_only_own_activity_and_has_no_private_fields(console_hub, upstream):
    h = console_hub
    now = _iso(datetime.now(timezone.utc))
    with h.db._lock, h.db._conn:
        h.db._conn.execute(
            "UPDATE connections SET last_used_at='2000-01-01T00:00:00Z' WHERE id IN (?,?)",
            (h.a1, h.m1),
        )
    upstream.latest.update({h.a1: now, h.m1: now})
    code, payload, _ = h.get(h.owner, "/api/console")
    assert code == 200 and payload["role"] == "owner"
    agents = {item["id"]: item for item in payload["agents"]}
    assert set(agents[h.a1]) == {
        "id",
        "name",
        "client",
        "parent_id",
        "own",
        "recording",
        "can_toggle",
        "status",
        "last_seen",
    }
    assert agents[h.a1]["last_seen"] == now and agents[h.a1]["status"] == "working"
    assert agents[h.m1]["last_seen"] == "2000-01-01T00:00:00Z"
    assert agents[h.m1]["status"] == "quiet" and agents[h.m1]["recording"] is None
    assert agents[h.m1]["can_toggle"] is False
    upstream.peek.assert_called_once_with(h.gateway, h.workspace)
    assert set(upstream.activity.latest_at.call_args.args[0]) == {h.a1, h.a2}
    upstream.activity.latest_at.assert_called_once()


def test_list_without_activity_or_own_agents(console_hub, upstream):
    h = console_hub
    upstream.peek.return_value = None
    assert h.get(h.member, "/api/console")[1]["agents"][0]["recording"] is False
    upstream.activity.latest_at.assert_not_called()
    upstream.peek.reset_mock()
    with h.db._lock, h.db._conn:
        h.db._conn.execute(
            "UPDATE connections SET revoked_at='2000-01-01T00:00:00Z' WHERE user_id=?", (h.admin,)
        )
    payload = h.get(h.owner, "/api/console")[1]
    assert [item["id"] for item in payload["agents"]] == [h.m1]
    upstream.peek.assert_not_called()


@pytest.mark.parametrize("principal_kind", ["anonymous", "agent", "worker", "machine"])
def test_all_handlers_authenticate_before_query_or_body(monkeypatch, principal_kind):
    principal = None if principal_kind == "anonymous" else SimpleNamespace(person=False)
    gateway = SimpleNamespace(
        principal_for=Mock(return_value=principal),
        add_route=Mock(),
        add_prefix_route=Mock(),
    )
    routes.install(gateway)
    monkeypatch.setattr(routes, "query", Mock(side_effect=AssertionError("query read")))
    monkeypatch.setattr(routes, "read_json", Mock(side_effect=AssertionError("body read")))
    monkeypatch.setattr(routes, "path_parts", Mock(side_effect=AssertionError("path read")))
    responses = Mock()
    monkeypatch.setattr(routes, "send_json", responses)
    # require_principal uses the shared HTTP module's sender for anonymous calls.
    monkeypatch.setattr("commonplace.http_util.send_json", responses)
    for call in gateway.add_route.call_args_list + gateway.add_prefix_route.call_args_list:
        call.args[2](object())
    assert [call.args[1] for call in responses.call_args_list] == [
        401 if principal is None else 403
    ] * 3
    assert gateway.principal_for.call_count == 3


def test_http_auth_before_invalid_input_and_machine_boundary(console_hub, upstream):
    h = console_hub
    for token, runner, expected in [
        (h.t1, None, 403),
        (h.t2, "worker", 403),
        (h.tm, "machine", 404),
    ]:
        if runner:
            cid = h.principal(token).connection_id
            with h.db._lock, h.db._conn:
                h.db._conn.execute(
                    "INSERT INTO runner_connections VALUES (?, 'test-machine', ?)", (cid, runner)
                )
        for path in ("/api/console", "/api/console/stream?limit=bad&after=bad"):
            assert h.get(token, path)[0] == expected
        assert raw_post(h, token, "/api/console/agents/unknown", b"{")[0] == expected
    assert h.get(None, "/api/console/stream?limit=bad&after=bad")[0] == 401
    assert raw_post(h, None, "/api/console/agents/unknown", b"{")[0] == 401
    upstream.setter.assert_not_called()
    upstream.page.assert_not_called()


def test_invisible_agent_errors_are_identical_and_authority_is_fresh(console_hub, upstream):
    h = console_hub
    foreign, _ = h.db.create_token(
        workspace_ref=h.other_workspace,
        user_ref=h.other_user,
        name="foreign",
        client="test",
        policy=h.principal(h.t1).policy,
        actor=h.other_user,
    )
    for method in ("GET", "POST"):
        responses = []
        for cid in (h.a1, foreign, "unknown"):
            path = (
                "/api/console/stream?agent=" + cid
                if method == "GET"
                else "/api/console/agents/" + cid
            )
            responses.append(
                h.request(h.member, method, path, {"record": True} if method == "POST" else None)
            )
        assert all(response == responses[0] for response in responses)
        assert responses[0][0:2] == (404, {"error": "Agent not found"})
    assert h.stream()[0] == 200
    h.db.revoke(h.a1, actor=h.admin)
    assert h.stream()[0] == 404
    upstream.setter.assert_not_called()


@pytest.mark.parametrize("limit", ["0", "101", "-1", "abc", "1.5", ""])
def test_invalid_limits(console_hub, upstream, limit):
    assert console_hub.stream(suffix="&limit=" + limit)[:2] == (400, {"error": "Invalid limit"})
    upstream.page.assert_not_called()


def test_stream_delegates_cursor_defaults_and_current_authority(console_hub, upstream):
    h = console_hub
    valid = stream.encode_cursor(0, 0, 0, 0)
    for suffix, cursor, limit in [
        ("", None, None),
        ("&after=" + valid + "&limit=100", valid, 100),
        ("&limit=1", None, 1),
    ]:
        assert h.stream(suffix=suffix)[0] == 200
        args = upstream.page.call_args.args
        assert args[0] is h.gateway and args[2].id == h.a1 and args[2].own
        assert args[3:] == (cursor, limit)
    assert h.stream(h.m1)[0] == 200
    assert upstream.page.call_args.args[2].own is False
    assert upstream.page.call_args.args[2].recording is None
    upstream.recording.add(h.m1)
    assert h.stream(h.m1, h.member)[0] == 200
    assert upstream.page.call_args.args[2].own and upstream.page.call_args.args[2].recording
    upstream.page.side_effect = ValueError("untrusted cursor text")
    assert h.stream(suffix="&after=bad")[:2] == (400, {"error": "Invalid console cursor"})
    # A valid cursor cannot turn an unrelated page failure into a client error.
    assert h.stream(suffix="&after=" + valid)[:2] == (500, {"error": "internal error"})


@pytest.mark.parametrize("limit", ["1_0", "%2B5", "%205", "5%20", "%D9%A5", "%EF%BC%95"])
def test_limit_requires_ascii_digits(console_hub, upstream, limit):
    assert console_hub.stream(suffix="&limit=" + limit)[:2] == (400, {"error": "Invalid limit"})
    upstream.page.assert_not_called()


@pytest.mark.parametrize("field", ["name", "client"])
def test_agent_display_fields_are_redacted(console_hub, upstream, field):
    h = console_hub
    with h.db._lock:
        h.db._conn.execute(
            f"UPDATE connections SET {field}=? WHERE id=?", ("password=synthetic", h.a1)
        )
    code, result, _ = h.get(h.owner, "/api/console")
    assert code == 200
    view = next(row for row in result["agents"] if row["id"] == h.a1)
    assert view[field] == "password=[redacted:assignment]"
    assert h.toggle()[1][field] == "password=[redacted:assignment]"


@pytest.mark.parametrize("record", [True, False])
def test_toggle_returns_updated_agent_for_own_person(console_hub, upstream, record):
    h = console_hub
    code, payload, _ = h.toggle(h.m1, h.member, record)
    assert code == 200 and payload["id"] == h.m1
    assert payload["recording"] is record and payload["can_toggle"]
    args = upstream.setter.call_args.args
    assert args[0] is h.db and args[1].user_id == h.member_user
    assert args[2:] == (h.m1, record)
    assert (h.m1 in upstream.recording) is record


@pytest.mark.parametrize(
    "body", [{}, {"record": 1}, {"record": "true"}, {"record": None}, {"record": True, "extra": 0}]
)
def test_toggle_rejects_non_exact_boolean_body(console_hub, upstream, body):
    assert (
        console_hub.post(console_hub.owner, "/api/console/agents/" + console_hub.a1, body)[0] == 400
    )
    upstream.setter.assert_not_called()


@pytest.mark.parametrize("body", [b"{", b"[]", b"null", b""])
def test_toggle_rejects_malformed_json(console_hub, upstream, body):
    h = console_hub
    assert raw_post(h, h.owner, "/api/console/agents/" + h.a1, body)[0] == 400
    upstream.setter.assert_not_called()


def test_toggle_denies_other_person_and_settings_race(console_hub, upstream):
    h = console_hub
    expected = {"error": "Only the person this agent acts for can change this"}
    assert h.toggle(h.m1)[:2] == (403, expected)
    upstream.setter.assert_not_called()
    upstream.setter.side_effect = types.ConsoleDenied
    assert h.toggle()[:2] == (403, expected)


@pytest.mark.parametrize("suffix", ["", "unknown/extra", "unknown"])
def test_toggle_unknown_path(console_hub, upstream, suffix):
    h = console_hub
    assert h.post(h.owner, "/api/console/agents/" + suffix, {"record": True})[:2] == (
        404,
        {"error": "Agent not found"},
    )
    upstream.setter.assert_not_called()


def test_success_uses_json_no_store_and_utf8(console_hub, upstream):
    h = console_hub
    with h.db._lock, h.db._conn:
        h.db._conn.execute("UPDATE connections SET name='équipe' WHERE id=?", (h.a1,))
    handler = SimpleNamespace(
        path="/api/console",
        wfile=BytesIO(),
        send_response=Mock(),
        send_header=Mock(),
        end_headers=Mock(),
    )
    original = h.gateway.principal_for
    h.gateway.principal_for = lambda request: h.person(h.admin)
    try:
        h.gateway.find_route("GET", "/api/console")(handler)
    finally:
        h.gateway.principal_for = original
    handler.send_response.assert_called_once_with(200)
    assert ("Cache-Control", "no-store") in [
        call.args for call in handler.send_header.call_args_list
    ]
    assert "équipe".encode() in handler.wfile.getvalue()


def test_last_seen_never_regresses_to_older_activity(console_hub, upstream):
    h = console_hub
    upstream.latest[h.a1] = "2000-01-01T00:00:00Z"
    agent = authority.agent(h.gateway, h.person(h.admin), h.a1)
    view = routes._views(h.gateway, h.person(h.admin), [agent])[0]
    assert view["last_seen"] == agent.last_used_at
    # Even an inconsistent upstream object cannot expose private activity.
    private = replace(agent, own=False, recording=True)
    upstream.peek.reset_mock()
    view = routes._views(h.gateway, h.person(h.admin), [private])[0]
    assert view["recording"] is None and view["last_seen"] == private.last_used_at
    upstream.peek.assert_not_called()


@pytest.mark.parametrize("method", ["list", "toggle"])
def test_broken_pointer_list_and_toggle_degrade(console_hub, upstream, method):
    h = console_hub
    upstream.peek.side_effect = ValueError("broken generation pointer")
    code, result, _ = h.get(h.owner, "/api/console") if method == "list" else h.toggle()
    assert code == 200
    agent = (
        next(row for row in result["agents"] if row["id"] == h.a1) if method == "list" else result
    )
    assert agent["id"] == h.a1
    assert agent["last_seen"] == h.db.connection(h.a1, h.workspace)["last_used_at"]
    if method == "toggle":
        assert agent["recording"] is True
