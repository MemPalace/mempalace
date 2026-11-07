"""Canonical Space REST foundation, exercised through the real gateway."""

import json
import socket
import threading
from collections import Counter
from http.client import HTTPConnection
from pathlib import Path

import pytest

from commonplace import events, sessions
from commonplace.boards import BoardService
from commonplace.boards.store import STATUSES
from commonplace.extensions import ToolContext
from commonplace.gateway import _rest_tool
from commonplace.rules import enforce
from tests import test_commonplace_rules_enforce as rules_tests

env = rules_tests.env
refresh = rules_tests.refresh
setup = rules_tests.setup


@pytest.fixture
def http(env):
    server = env.gateway.build_server("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()

    def request(
        method,
        path,
        body=None,
        seq=None,
        *,
        principal=None,
        token=None,
        anonymous=False,
        headers=None,
        raw=None,
    ):
        auth = {"Content-Type": "application/json"}
        if not anonymous:
            if principal is not None and principal.person:
                session = sessions.redeem_login_link(
                    env.db,
                    sessions.create_login_link(
                        env.db, principal.user_email, principal.workspace_id
                    ),
                )
                auth["Cookie"] = f"{sessions.COOKIE}={session}"
            else:
                auth["Authorization"] = "Bearer " + (token or env.token)
        if seq is not None:
            auth["Commonplace-Rules-Seq"] = str(seq)
        auth.update(headers or {})
        conn = HTTPConnection("127.0.0.1", server.server_address[1], timeout=3)
        try:
            conn.request(
                method,
                path,
                raw if raw is not None else (json.dumps(body) if body is not None else None),
                auth,
            )
            response = conn.getresponse()
            return response.status, json.loads(response.read()), dict(response.getheaders())
        finally:
            conn.close()

    request.port = server.server_address[1]
    try:
        yield request
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def service(env, principal=None):
    return BoardService(ToolContext(principal or env.person, env.gateway))


def snapshot(env):
    s = service(env)
    with s.store.connection() as conn:
        boards = [tuple(r) for r in conn.execute("SELECT * FROM boards ORDER BY id")]
        tasks = [tuple(r) for r in conn.execute("SELECT * FROM tasks ORDER BY id")]
    return boards, tasks, events.latest_seq(env.db, env.workspace)


def spies(monkeypatch):
    counts = Counter()
    for name in ("_board", "board_list", "board_add", "transaction", "space_metadata"):
        original = getattr(BoardService, name, lambda self, *a, **kw: {})

        def wrapper(self, *args, _name=name, _original=original, **kwargs):
            counts[_name] += 1
            return _original(self, *args, **kwargs)

        monkeypatch.setattr(BoardService, name, wrapper, raising=False)
    return counts


def assert_error(response, status, error):
    assert response[0] == status, response
    assert response[1] == {"error": error}


def test_route_principal_contract(env, http):
    for method, path in (
        ("GET", "/api/spaces"),
        ("GET", "/api/spaces/work"),
        ("POST", "/api/spaces"),
    ):
        response = http(method, path, {"slug": "anonymous"}, anonymous=True)
        assert response[0] == 401, response
        assert response[1] == {"error": "unauthorized"}
        assert "WWW-Authenticate" in response[2]
    service(env).board_add(slug="work")
    excluded, _ = env.agent("excluded", tools=["rules_get"])
    for method, path in (
        ("GET", "/api/spaces"),
        ("GET", "/api/spaces/work"),
        ("POST", "/api/spaces"),
    ):
        assert_error(
            http(method, path, {"slug": "denied"}, token=excluded),
            403,
            "tool not allowed for this connection",
        )
    # Resolve real member authority through the session, independently of PAT policy.
    user = env.db.create_user("member@example.test")
    env.db.add_member(env.workspace, user, "member", actor=env.user)
    member = sessions.person_principal(env.db, user, env.workspace)
    for i, principal in enumerate((None, env.person, member)):
        for path in ("/api/spaces", "/api/spaces/work"):
            assert http("GET", path, principal=principal)[0] == 200
        response = http("POST", "/api/spaces", {"slug": f"new-{i}"}, principal=principal)
        assert response[0] == 201, response
        assert service(env).space_metadata(f"new-{i}")["slug"] == f"new-{i}"
    for name, overrides in (
        ("untrusted", {"trust": "untrusted", "write_wing": "inbox"}),
        ("read-only", {"scopes": ["memory:read"]}),
    ):
        token, _ = env.agent(name, **overrides)
        before = snapshot(env)
        assert_error(
            http("POST", "/api/spaces", {"slug": name}, token=token),
            403,
            "tool not allowed for this connection",
        )
        assert snapshot(env) == before


def test_configure_truth_table(env, http):
    service(env).board_add(slug="work")
    response = http("GET", "/api/spaces/work", principal=env.person)
    assert response[0] == 200, response
    assert response[1]["can_configure"] is True
    user = env.db.create_user("member@example.test")
    env.db.add_member(env.workspace, user, "member", actor=env.user)
    member = sessions.person_principal(env.db, user, env.workspace)
    assert http("GET", "/api/spaces/work", principal=member)[1]["can_configure"] is False
    assert env.principal.role == "owner" and not env.principal.person
    assert http("GET", "/api/spaces/work")[1]["can_configure"] is False
    # The legacy projection intentionally retains its role-only behavior.
    assert service(env, env.principal).board_list()[0]["can_configure"] is True


def test_metadata_plain_read_and_list_expiry(env, http, monkeypatch):
    s = service(env)
    s.board_add(slug="work", repo="example/repository")
    s.board_add(slug="hidden", wing="private")
    s.board_add(slug="archived")
    card = service(env, env.principal).task_add(board="work", title="Expired", claim=True)
    with s.store.transaction() as conn:
        conn.execute("UPDATE boards SET archived=1 WHERE slug='archived'")
        conn.execute(
            "UPDATE tasks SET claim_until='2000-01-01T00:00:00Z' WHERE id=?", (card["number"],)
        )
    token, _ = env.agent("limited", read_wings=["shared"], write_wings=["shared"])
    foreign = env.db.create_workspace("foreign", env.user)
    env.gateway.supervisor.workspace_dir = lambda ws: (
        env.directory if ws == env.workspace else env.directory.parent / "foreign"
    )
    service(env, sessions.person_principal(env.db, env.user, foreign)).board_add(slug="foreign")
    before = snapshot(env)
    calls = spies(monkeypatch)
    response = http("GET", "/api/spaces/work", token=token)
    assert response[0] == 200, response
    assert calls == {"space_metadata": 1}
    assert snapshot(env) == before
    for slug in ("hidden", "foreign", "missing", "archived", "example%2Frepository"):
        assert_error(http("GET", "/api/spaces/" + slug, token=token), 404, "space_not_found")
    assert calls == {"space_metadata": 6}
    assert snapshot(env) == before
    assert http("GET", "/api/spaces", token=token)[0] == 200
    assert calls["board_list"] == 1 and calls["transaction"] == 1
    assert snapshot(env) != before


def test_dto_counts_names_and_tenants(env, http):
    s = service(env)
    s.board_add(slug="work", name="Same")
    parent = s.task_add(board="work", title="Parent")
    child = s.task_add(board="work", title="Child")
    with s.store.transaction() as conn:
        conn.execute(
            "UPDATE tasks SET parent_number=? WHERE id=?", (parent["number"], child["number"])
        )
    response = http("GET", "/api/spaces")
    assert response[0] == 200, response
    dto = response[1]["spaces"][0]
    assert set(dto) == {
        "slug",
        "name",
        "wing",
        "coordinator",
        "counts",
        "can_configure",
        "open_url",
        "version",
        "deleted",
    }
    assert dto["version"] == 1 and dto["deleted"] is False
    assert dto["open_url"] == "/app/#/board/work"
    assert set(dto["counts"]) == set(STATUSES) and sum(dto["counts"].values()) == 1
    assert http("GET", "/api/spaces/work")[1] == dto
    for slug in ("same-one", "same-two"):
        assert http("POST", "/api/spaces", {"slug": slug, "name": "Same"})[0] == 201
    other = env.db.create_workspace("other", env.user)
    principal = sessions.person_principal(env.db, env.user, other)
    # The gateway directory resolver must model actual per-workspace isolation.
    env.gateway.supervisor.workspace_dir = lambda ws: (
        env.directory if ws == env.workspace else env.directory.parent / "other"
    )
    assert (
        http("POST", "/api/spaces", {"slug": "work", "name": "Other"}, principal=principal)[0]
        == 201
    )
    assert http("GET", "/api/spaces/work", principal=principal)[1]["name"] == "Other"
    assert http("GET", "/api/spaces/work")[1]["name"] == "Same"


def test_null_defaults(env, http):
    response = http("POST", "/api/spaces", dict(slug="defaults", name=None, wing=None, repo=None))
    assert response[0] == 201, response
    assert response[1]["name"] == "defaults" and response[1]["wing"] == "shared"
    assert response[1] == http("GET", "/api/spaces/defaults")[1]
    with service(env).store.connection() as conn:
        assert conn.execute("SELECT count(*) FROM board_repos").fetchone()[0] == 0


def test_rejected_shapes_and_domain_calls(env, http, monkeypatch):
    before = snapshot(env)
    calls = spies(monkeypatch)
    for method, path, allow in (
        ("PATCH", "/api/spaces", "GET, POST"),
        ("DELETE", "/api/spaces", "GET, POST"),
        ("PATCH", "/api/spaces/work", "GET"),
        ("DELETE", "/api/spaces/work", "GET"),
        ("POST", "/api/spaces/work", "GET"),
    ):
        response = http(method, path, {})
        assert response[0] == 405, response
        assert response[1] == {"error": "method_not_allowed"}
        assert response[2]["Allow"] == allow
        assert not calls
    for suffix in ("cards", "chat", "settings", "conversation"):
        for method in ("GET", "POST", "PATCH", "DELETE"):
            assert_error(http(method, "/api/spaces/work/" + suffix, {}), 404, "not_found")
            assert not calls
    assert_error(http("PUT", "/api/spaces", {}), 404, "not found")
    assert snapshot(env) == before


def test_validation_and_transport_errors(env, http):
    before = snapshot(env)
    response = http("POST", "/api/spaces", {"slug": "bad", "unknown": 1})
    assert response[0] == 400, response
    assert response[1] == {"error": "invalid_arguments"}
    for body in (
        {},
        {"slug": None},
        {"slug": 3},
        {"slug": "Bad"},
        {"slug": ""},
        {"slug": "bad", "wing": ""},
        {"slug": "bad", "name": ""},
        {"slug": "bad", "repo": ""},
        *({"slug": "bad", key: 1} for key in ("name", "wing", "repo")),
        {"slug": "bad", "rules_seq": 1},
    ):
        assert_error(http("POST", "/api/spaces", body, seq=1), 400, "invalid_arguments")
    for raw in ("{", "[]", "null", '"text"'):
        assert_error(http("POST", "/api/spaces", raw=raw), 400, "invalid_arguments")
    assert_error(
        http("POST", "/api/spaces", {"slug": "bad"}, headers={"Idempotency-Key": ""}),
        400,
        "unsupported_idempotency",
    )
    for method in ("GET", "POST"):
        assert_error(
            http(method, "/api/spaces?include_deleted=", {"slug": "bad"}),
            400,
            "unsupported_include_deleted",
        )
    assert snapshot(env) == before


def test_gateway_body_transport_regressions(env, http):
    for length, status, error in (
        ("Content-Length: nope\r\n", 400, "invalid request body"),
        ("Content-Length: 0\r\nContent-Length: 0\r\n", 400, "invalid request body"),
        ("Content-Length: 1048577\r\n", 413, "request too large"),
    ):
        with socket.create_connection(("127.0.0.1", http.port), timeout=3) as sock:
            sock.sendall(
                ("POST /api/spaces HTTP/1.1\r\nHost: localhost\r\n" + length + "\r\n").encode()
            )
            stream = sock.makefile("rb")
            assert int(stream.readline().split()[1]) == status
            headers = {}
            while (line := stream.readline()) != b"\r\n":
                key, value = line.decode().split(":", 1)
                headers[key.lower()] = value.strip()
            assert json.loads(stream.read(int(headers["content-length"]))) == {"error": error}
            assert stream.read(1) == b""


def test_collision_authorization_order(env, http):
    s = service(env)
    for slug, wing in (
        ("readable", "shared"),
        ("hidden", "private"),
        ("old-readable", "shared"),
        ("old-hidden", "private"),
    ):
        s.board_add(slug=slug, wing=wing, repo=f"example/{slug}")
    with s.store.transaction() as conn:
        conn.execute("UPDATE boards SET archived=1 WHERE slug LIKE 'old-%'")
    token, _ = env.agent("limited", read_wings=["shared"], write_wings=["shared"])
    before = snapshot(env)
    response = http("POST", "/api/spaces", {"slug": "readable"}, token=token)
    assert response[0] == 409, response
    assert response[1] == {"error": "space_exists"}
    for slug in ("readable", "old-readable", "hidden", "old-hidden"):
        hidden = "hidden" in slug
        assert_error(
            http("POST", "/api/spaces", {"slug": slug}, token=token),
            404 if hidden else 409,
            "space_not_found" if hidden else "space_exists",
        )
        assert_error(
            http("POST", "/api/spaces", {"slug": "new", "repo": f"example/{slug}"}, token=token),
            403 if hidden else 409,
            "forbidden" if hidden else "repo_assigned",
        )
        assert_error(
            http("POST", "/api/spaces", {"slug": slug, "wing": "private"}, token=token),
            404 if hidden else 403,
            "space_not_found" if hidden else "forbidden",
        )
    assert snapshot(env) == before
    from types import SimpleNamespace

    from tests._phase_b_helpers import seed, trusted_seat

    # Match the existing admission test seed without introducing a second gateway.
    s.board_add(slug="acme")
    world = SimpleNamespace(db=env.db, ws=env.workspace, gw=env.gateway, owner=env.person)
    seed(world, on=True)
    seat, payload = trusted_seat(world)
    assert seat.policy.trust == "trusted"
    before = snapshot(env)
    assert_error(
        http("POST", "/api/spaces", {"slug": "seat-new"}, token=payload["token"]), 403, "forbidden"
    )
    assert snapshot(env) == before


def test_rules_missing_stale_fresh_create(env, http):
    setup(env)
    body = {"slug": "fresh"}
    before = snapshot(env)
    response = http("POST", "/api/spaces", body)
    assert response[0] == 428, response
    assert response[1] == {"error": enforce.MESSAGES["missing"], "rules_refresh": True}
    assert snapshot(env) == before
    seq = refresh(env)
    env.owner.create("*", "Changed after serving", "limit")
    before = snapshot(env)
    stale = http("POST", "/api/spaces", body, seq)
    assert stale[0] == 428
    assert stale[1] == {"error": enforce.MESSAGES["stale"], "rules_refresh": True}
    assert snapshot(env) == before
    seq = refresh(env)
    assert_error(
        http("POST", "/api/spaces", {**body, "rules_seq": seq}, seq), 400, "invalid_arguments"
    )
    assert http("POST", "/api/spaces", body, seq)[0] == 201
    assert any(
        r.path == "/api/spaces" and r.tool == "board_add" and r.kind == "checked"
        for r in enforce.REST_ROUTES
    )
    created = [
        e for e in events.after(env.db, env.workspace, before[2]) if e["kind"] == "space_created"
    ]
    assert len(created) == 1
    assert created[0]["subject"] == "space:fresh" and created[0]["data"] == {"board": "fresh"}
    assert http("POST", "/api/spaces", body, seq)[0] == 409
    with service(env).store.connection() as conn:
        assert conn.execute("SELECT count(*) FROM boards WHERE slug='fresh'").fetchone()[0] == 1


def surfaces(env):
    result = {}
    for profile in ("slim", "standard", "full"):
        _, p = env.agent("surface-" + profile, profile=profile)
        visible = sorted(p.policy.visible_tools())
        response = env.gateway.mediator.handle(
            p, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )
        result[profile] = {"visible": visible, "mcp": response["result"]}
    return result


def test_inventory_and_visibility_contract(env, http):
    service(env).board_add(slug="work")
    response = http("GET", "/api/spaces/work")
    assert response[0] == 200, response
    for method, path, tool in (
        ("GET", "/api/spaces", "board_list"),
        ("POST", "/api/spaces", "board_add"),
        ("GET", "/api/spaces/work", "board_list"),
    ):
        assert _rest_tool(method, path) == tool
    for method in ("PATCH", "DELETE"):
        assert _rest_tool(method, "/api/spaces") is None
    for method in ("GET", "POST", "PATCH", "DELETE"):
        assert any(m == method and p == "/api/spaces/" for m, p, _ in env.gateway.prefix_routes)
    assert any(
        r.path == "/api/spaces" and r.tool == "board_add" and r.kind == "checked"
        for r in enforce.REST_ROUTES
    )
    expected = json.loads(Path(__file__).with_name("spaces_surfaces.json").read_text())
    assert surfaces(env) == expected


def test_legacy_boards_projects_and_verbatim_chat(env, http):
    response = http("POST", "/api/boards", {"slug": "legacy", "rules_seq": 0})
    assert response[0] == 201
    assert {"slug", "name", "wing", "counts", "coordinator"} <= response[1].keys()
    assert http("GET", "/api/boards")[0] == 200
    response = http("GET", "/api/projects/legacy", principal=env.person)
    assert response[0] == 200, response
    assert response[1]["board"] == "legacy"
    text = 'Every word.\n"Quoted" @reference — 그대로'
    assert http("POST", "/api/boards/legacy/chat", {"text": text})[0] == 201
    response = http("GET", "/api/boards/legacy/chat")
    assert response[0] == 200
    assert text in json.dumps(response[1], ensure_ascii=False).replace("\\n", "\n").replace(
        '\\"', '"'
    )
