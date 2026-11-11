"""The owner-granted cards:decide scope through the production gateway, synthetic data only."""

import http.client
import json
import re
from html import unescape
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from commonplace import sessions
from commonplace.boards.service import BoardService
from commonplace.control_db import ControlError
from commonplace.extensions import ToolContext
from commonplace.policy import Policy
from tests._commonplace_helpers import hub_fixture as hub_fixture
from tests._projects_helpers import projects_hub_fixture as projects_hub_fixture
from tests.test_commonplace_oauth import Client

OPTIONS = ["Ship it", "Hold"]
REFUSAL = "only a signed-in workspace owner can answer this"
# Today's REST refusal for an untrusted browser-made PAT, before the service runs.
HIDDEN = "tool not allowed for this connection"
FULL = "memory:read memory:write cards:decide"
# Literal copies: this file must run (and fail behaviourally) on a base without the scope.
SCOPE_TEXT = (
    "Allow this app to answer your Needs-me questions as you (scope cards:decide). "
    "Every answer is recorded with this connection, and you can stop this at any time "
    "under Agents. It can answer only questions addressed to you, never another "
    "person's, and only on boards you can write to."
)
OWNER_ONLY_TEXT = "Only the workspace owner can let an app answer Needs-me questions."
TOP_LEVEL_TEXT = "Only a top-level connection of the workspace owner can answer Needs-me questions."
AGENT_TEXT = "Agent, runner and orchestrator connections cannot answer Needs-me questions."
INACTIVE_TEXT = "Only an active connection can change; this one is revoked or expired."
NOT_YOURS_TEXT = (
    "This connection belongs to another member. Only your own apps can answer your "
    "Needs-me questions."
)
OAUTH_GRANT_TEXT = "An OAuth app gets this only through a fresh sign-in consent."
AUDIT_ACTION = "card.decide.delegated"


def api(hub, auth, method, path, body=None):
    connection = http.client.HTTPConnection("127.0.0.1", hub.port, timeout=10)
    headers = {"Content-Type": "application/json"}
    if isinstance(auth, dict):
        headers.update(auth)
    elif auth:
        headers["Authorization"] = f"Bearer {auth}"
    try:
        connection.request(
            method, path, json.dumps(body).encode() if body is not None else None, headers
        )
        response = connection.getresponse()
        return response.status, json.loads(response.read() or b"null")
    finally:
        connection.close()


def mcp(hub, token, name, **args):
    response = hub.rpc(token, "tools/call", {"name": name, "arguments": args})
    if "error" in response:
        return False, response["error"]["message"]
    return not response["result"].get("isError", False), response["result"]["content"][0]["text"]


def service(hub, principal):
    return BoardService(ToolContext(principal=principal, gateway=hub.gateway))


def cookie_for(hub, email):
    session = sessions.redeem_login_link(
        hub.db, sessions.create_login_link(hub.db, email, hub.workspace)
    )
    return {"Cookie": f"{sessions.COOKIE}={session}"}


@pytest.fixture
def world(hub):
    person = sessions.person_principal(hub.db, hub.admin, hub.workspace)
    owner = service(hub, person)
    # Default browser-made and OAuth apps write only the inbox wing, so the
    # answerable board lives there; "team" is a board they cannot write.
    owner.board_add(slug="work", wing="inbox")
    owner.board_add(slug="team", wing="shared")
    asker_token = hub.owner_token("asker")
    asker = service(hub, hub.gateway.authenticate(f"Bearer {asker_token}"))
    return SimpleNamespace(
        hub=hub,
        owner=owner,
        cookie=cookie_for(hub, "owner@example.com"),
        asker=asker,
        asker_token=asker_token,
        serial=0,
    )


def ask(w, question="Ship the release?", recipient=None, board="work"):
    card = w.asker.ask(
        f"{question} Choose one of the options.",
        options=OPTIONS,
        board=board,
        context="The release notes are ready for review.",
    )
    assert card["ask_to"] == f"user:{recipient or w.hub.admin}"
    return card["number"], card["version"]


def mint(w, cookie=None, **extra):
    w.serial += 1
    return api(
        w.hub,
        cookie or w.cookie,
        "POST",
        "/api/connections",
        {"name": f"phone-{w.serial}", "client": "generic", **extra},
    )


def owner_app(w, **extra):
    status, body = mint(w, decide=True, **extra)
    assert status == 201, body
    assert body["connection"]["decide"] is True
    return body["token"], body["connection"]["id"]


def rest_decide(w, token, number, version, choice=OPTIONS[0]):
    return api(
        w.hub,
        token,
        "POST",
        f"/api/cards/{number}/decide",
        {"version": version, "choice": choice, "then": "return"},
    )


def oauth_grant(hub, *, email="owner@example.com", scope=FULL, tick=True):
    client = Client(hub)
    client.login(email)
    cid = client.register(client_name="Owner Phone")
    path = client.authorize_path(cid, scope=scope)
    status, _, page = client.request("GET", path)
    assert status == 200, page
    request = unescape(re.search(r'name="request" value="([^"]+)"', page)[1])
    data = {"request": request, "decision": "allow"}
    if tick:
        data["grant_decide"] = "1"
    status, headers, _ = client.request("POST", path, data)
    assert status == 303
    query = parse_qs(urlsplit(headers["Location"]).query)
    assert "code" in query, query
    status, _, raw = client.exchange(cid, query["code"][0])
    assert status == 200, raw
    issued = json.loads(raw)
    principal = hub.gateway.authenticate("Bearer " + issued["access_token"])
    return SimpleNamespace(page=page, issued=issued, token=issued["access_token"], p=principal)


def delegated_audit(hub):
    return [
        row
        for row in hub.db.audit_entries(hub.workspace, limit=500)
        if row["action"] == AUDIT_ACTION
    ]


# ── 1. an owner's PAT or OAuth token with cards:decide answers that owner's ask ──


def test_a1_owner_pat_with_scope_answers_own_ask_over_rest_and_mcp(world):
    w = world
    token, _cid = owner_app(w)
    # The browser-made token is untrusted; the owner's explicit grant admits decide.
    assert "decide" in w.hub.tool_names(token)
    number, version = ask(w)
    status, card = rest_decide(w, token, number, version)
    assert status == 200, card
    assert card["status"] == "todo" and card["ask_to"] is None
    shown = w.owner.task_show(number)
    decision = [e for e in shown["events"] if e["kind"] == "decide"][-1]
    assert decision["choice"] == OPTIONS[0]
    app = w.hub.gateway.authenticate(f"Bearer {token}")
    assert decision["actor"] == app.actor

    number, version = ask(w, "Second question?")
    ok, text = mcp(w.hub, token, "decide", task=number, version=version, choice=OPTIONS[1])
    assert ok, text
    assert w.owner.task_show(number)["status"] == "todo"


def test_a1_owner_oauth_with_scope_answers_own_ask(world):
    w = world
    status, _, raw = Client(w.hub).request("GET", "/.well-known/oauth-authorization-server")
    assert status == 200 and "cards:decide" in json.loads(raw)["scopes_supported"]
    grant = oauth_grant(w.hub)
    assert SCOPE_TEXT in unescape(grant.page)
    assert "cards:decide" in grant.issued["scope"].split()
    assert grant.p.policy.has_scope("cards:decide")
    number, version = ask(w)
    status, card = rest_decide(w, grant.token, number, version)
    assert status == 200, card
    assert w.owner.task_show(number)["status"] == "todo"


# ── 2. without the scope it is refused exactly as today ──


def test_a2_without_scope_is_refused_as_today(world):
    w = world
    # Consent shown but left unticked: the client gets a token without the scope.
    unticked = oauth_grant(w.hub, tick=False)
    assert SCOPE_TEXT in unescape(unticked.page)
    assert "cards:decide" not in unticked.issued["scope"].split()
    status, body = mint(w)
    assert status == 201 and body["connection"]["decide"] is False
    plain = body["token"]
    number, version = ask(w)
    for token, refusal in ((unticked.token, REFUSAL), (plain, HIDDEN), (w.asker_token, REFUSAL)):
        status, refused = rest_decide(w, token, number, version)
        assert status == 403 and refusal in refused["error"], refused
    # The untrusted browser PAT still does not see decide; the OAuth client sees it and is refused.
    assert "decide" not in w.hub.tool_names(plain)
    ok, text = mcp(w.hub, unticked.token, "decide", task=number, version=version, choice=OPTIONS[0])
    assert not ok and REFUSAL in text
    assert w.owner.task_show(number)["status"] == "review"
    assert delegated_audit(w.hub) == []


# ── 3. a token cannot decide another person's ask ──


def test_a3_token_cannot_decide_another_persons_ask(world):
    w = world
    hub = w.hub
    token, _cid = owner_app(w)
    member = hub.db.create_user("member@example.com")
    hub.db.add_member(hub.workspace, member, "member", actor=hub.admin)
    co_owner = hub.db.create_user("co-owner@example.com")
    hub.db.add_member(hub.workspace, co_owner, "owner", actor=hub.admin)

    # An ask addressed to a member.
    card = w.asker.task_add(board="work", title="Member review")
    w.asker.task_handoff(
        card["number"],
        to="member@example.com",
        note="Member, do you approve this change?",
        options=OPTIONS,
        context="Only the member decides this question.",
    )
    for_member = w.owner.task_show(card["number"])
    assert for_member["ask_to"] == f"user:{member}"
    status, refused = rest_decide(w, token, card["number"], for_member["version"])
    assert status == 403 and REFUSAL in refused["error"]

    # Another owner's app cannot answer an ask addressed to a different owner.
    status, other = mint(w, cookie=cookie_for(hub, "co-owner@example.com"), decide=True)
    assert status == 201 and other["connection"]["decide"] is True
    # Asks go to the first owner by membership time, which can tie within a second.
    addressed = w.asker._owner_actor().removeprefix("user:")
    apps = {hub.admin: token, co_owner: other["token"]}
    token = apps[addressed]
    stranger = apps[co_owner if addressed == hub.admin else hub.admin]
    number, version = ask(w, recipient=addressed)
    status, refused = rest_decide(w, stranger, number, version)
    assert status == 403 and REFUSAL in refused["error"]
    assert w.owner.task_show(number)["status"] == "review"
    assert w.owner.task_show(card["number"])["status"] == "review"

    # The owner's own ask is answerable by the same app.
    p = hub.gateway.authenticate(f"Bearer {token}")
    status, ok = rest_decide(w, token, number, version)
    assert status == 200, ok
    assert delegated_audit(hub)[0]["connection_id"] == p.connection_id

    # An ask routed to the board's agent coordinator is not the owner's either.
    coordinator = hub.owner_token("coordinator")
    w.owner.board_set("work", "conn:coordinator")
    routed = w.asker.ask(
        "Coordinator, should this ship today? Choose one.",
        options=OPTIONS,
        board="work",
        context="Routine release question for the coordinator.",
    )
    assert routed["ask_to"] == "conn:coordinator"
    status, refused = rest_decide(w, token, routed["number"], routed["version"])
    assert status == 403 and REFUSAL in refused["error"]
    assert w.owner.task_show(routed["number"])["status"] == "review"
    assert coordinator


# ── 4. a non-owner or an agent connection cannot grant the scope ──


def test_a4_non_owner_or_agent_cannot_grant(world):
    w = world
    hub = w.hub
    _token, cid = owner_app(w)
    member = hub.db.create_user("member@example.com")
    hub.db.add_member(hub.workspace, member, "member", actor=hub.admin)

    # A member sees no consent box; a forged tick grants nothing.
    forged = oauth_grant(hub, email="member@example.com")
    assert OWNER_ONLY_TEXT in unescape(forged.page)
    assert 'name="grant_decide"' not in forged.page
    assert "cards:decide" not in forged.issued["scope"].split()
    member_cookie = cookie_for(hub, "member@example.com")
    assert mint(w, cookie=member_cookie, decide=True)[0] == 403
    assert api(hub, member_cookie, "PATCH", f"/api/connections/{cid}", {"decide": True})[0] == 403

    # Agent connections cannot grant themselves or others, through REST or consent.
    agent = w.asker_token
    me = hub.gateway.authenticate(f"Bearer {agent}")
    assert api(hub, agent, "POST", "/api/connections", {"name": "x", "decide": True})[0] == 403
    status, _ = api(hub, agent, "PATCH", f"/api/connections/{me.connection_id}", {"decide": True})
    assert status == 403
    assert not me.policy.has_scope("cards:decide")

    # A child agent token never holds it, at creation or later.
    status, refused = mint(w, decide=True, parent=cid)
    assert status == 403 and refused["error"] == TOP_LEVEL_TEXT
    status, child = mint(w, parent=cid)
    assert status == 201
    child_id = child["connection"]["id"]
    status, refused = api(hub, w.cookie, "PATCH", f"/api/connections/{child_id}", {"decide": True})
    assert status == 409 and refused["error"] == AGENT_TEXT

    # Control-level guards: only the owner's own top-level connection.
    policy = Policy.build(scopes=["memory:read", "cards:decide"], read_wings=["*"])
    with pytest.raises(ControlError, match="cards:decide"):
        hub.db.create_token(
            workspace_ref=hub.workspace,
            user_ref=member,
            name="member-app",
            client="test",
            policy=policy,
            actor=hub.admin,
        )
    # A former owner's connection loses the scope with the role.
    assert not policy.for_role("member").has_scope("cards:decide")
    assert "cards:decide" not in sessions.person_policy("owner").scopes


def test_a4_cli_refuses_the_scope(monkeypatch):
    from commonplace import cli

    monkeypatch.setattr(cli, "_db", lambda args: None)
    with pytest.raises(ValueError, match="browser consent"):
        cli.cmd_token_add(
            SimpleNamespace(
                scopes="memory:read,cards:decide",
                client="generic",
                **dict.fromkeys(
                    ("profile", "tools", "read_wings", "write_wings", "write_wing", "trust")
                ),
            )
        )


# ── 5. revoking the scope stops it at once ──


def test_a5_revoking_the_scope_stops_it_at_once(world):
    w = world
    hub = w.hub
    token, cid = owner_app(w)
    number, version = ask(w)
    assert rest_decide(w, token, number, version)[0] == 200

    status, body = api(hub, w.cookie, "PATCH", f"/api/connections/{cid}", {"decide": False})
    assert status == 200 and body["connection"]["decide"] is False
    number, version = ask(w, "After revoke?")
    status, refused = rest_decide(w, token, number, version)
    # Exactly the refusal of a PAT that never held the scope.
    assert status == 403 and HIDDEN in refused["error"]
    assert "decide" not in hub.tool_names(token)
    revoked = [r for r in hub.db.audit_entries(hub.workspace) if r["connection_id"] == cid]
    assert "connection.decide.revoke" in {r["action"] for r in revoked}

    # Granting again on the same PAT, then revoking the whole connection.
    status, body = api(hub, w.cookie, "PATCH", f"/api/connections/{cid}", {"decide": True})
    assert status == 200 and body["connection"]["decide"] is True
    assert api(hub, w.cookie, "DELETE", f"/api/connections/{cid}")[0] == 200
    assert rest_decide(w, token, number, version)[0] == 401

    # An OAuth client's grant is withdrawn from the same Agents API.
    grant = oauth_grant(hub)
    oauth_id = grant.p.connection_id
    status, body = api(hub, w.cookie, "PATCH", f"/api/connections/{oauth_id}", {"decide": False})
    assert status == 200 and body["connection"]["decide"] is False
    status, refused = rest_decide(w, grant.token, number, version)
    assert status == 403 and REFUSAL in refused["error"]
    # Re-granting an OAuth client needs fresh consent.
    status, _ = api(hub, w.cookie, "PATCH", f"/api/connections/{oauth_id}", {"decide": True})
    assert status == 409
    assert w.owner.task_show(number)["status"] == "review"


# ── 6. an audit row per decide ──


def test_a6_audit_row_per_delegated_decide(world):
    w = world
    hub = w.hub
    token, cid = owner_app(w)
    grant = oauth_grant(hub)
    first, v1 = ask(w, "One?")
    second, v2 = ask(w, "Two?")
    third, v3 = ask(w, "Three?")
    assert rest_decide(w, token, first, v1)[0] == 200
    assert mcp(hub, token, "decide", task=second, version=v2, choice=OPTIONS[1])[0]
    assert rest_decide(w, grant.token, third, v3)[0] == 200
    # A refused attempt and a signed-in person's decide add no delegated row.
    fourth, v4 = ask(w, "Four?")
    assert rest_decide(w, w.asker_token, fourth, v4)[0] == 403
    w.owner.decide(fourth, choice=OPTIONS[0], version=v4)

    rows = sorted(delegated_audit(hub), key=lambda r: r["seq"])
    assert [(r["connection_id"], r["auth"], json.loads(r["target_ids"])) for r in rows] == [
        (cid, "pat", [str(first)]),
        (cid, "pat", [str(second)]),
        (grant.p.connection_id, "oauth", [str(third)]),
    ]
    assert all(
        r["user_id"] == hub.admin
        and r["decision"] == "ok"
        and r["tool"] == "decide"
        and r["reason"] == "board=work"
        for r in rows
    )


# ── seats, write wings, audit timing, control layer, messages ──


def _seat(projects_hub):
    h = projects_hub
    status, body = h.connect(h.owner, "work", {})
    assert status in (200, 201), body
    return body["connection"]["id"], body["token"]


def _owner_ask(hub, board="work", asker=None):
    """An agent ask routed to the owner even when the board has a coordinator."""
    if asker is None:
        asker = service(hub, hub.gateway.authenticate(f"Bearer {hub.owner_token('asker')}"))
    card = asker.ask(
        "Should the owner ship this release now? Choose one.",
        options=OPTIONS,
        board=board,
        context="The release notes are ready for review.",
        person_reason="Only the owner decides releases.",
    )
    assert card["ask_to"] == f"user:{hub.admin}"
    return card["number"], card["version"]


def test_r1_seats_are_never_granted_and_never_answer(projects_hub):
    h = projects_hub
    hub = h.hub
    seat_id, seat_token = _seat(h)
    status, refused = api(hub, h.owner, "PATCH", f"/api/connections/{seat_id}", {"decide": True})
    assert status == 409 and refused["error"] == AGENT_TEXT, refused
    # Still a seat after it stops coordinating the board.
    person = sessions.person_principal(hub.db, hub.admin, hub.workspace)
    service(hub, person).board_set("work", None)
    status, refused = api(hub, h.owner, "PATCH", f"/api/connections/{seat_id}", {"decide": True})
    assert status == 409 and refused["error"] == AGENT_TEXT, refused
    status, listing = api(hub, h.owner, "GET", "/api/connections")
    seat_row = next(c for c in listing["connections"] if c["id"] == seat_id)
    assert seat_row["decide"] is False and seat_row["decide_grantable"] is False
    # Nor can a new connection that claims the seat client be created with it.
    status, _ = api(
        hub,
        h.owner,
        "POST",
        "/api/connections",
        {"name": "fake-seat", "client": "commonplace-orchestrator", "decide": True},
    )
    assert status == 400
    # A grant stored by other means (an older build) still never answers.
    with hub.db._lock:
        row = hub.db._conn.execute(
            "SELECT policy FROM connections WHERE id=?", (seat_id,)
        ).fetchone()
        policy = json.loads(row["policy"])
        policy["scopes"] = sorted(set(policy["scopes"]) | {"cards:decide"})
        hub.db._conn.execute(
            "UPDATE connections SET policy=? WHERE id=?", (json.dumps(policy), seat_id)
        )
    assert hub.gateway.authenticate(f"Bearer {seat_token}").policy.has_scope("cards:decide")
    number, version = _owner_ask(hub)
    w = SimpleNamespace(hub=hub)
    status, refused = rest_decide(w, seat_token, number, version)
    assert status == 403 and REFUSAL in refused["error"], refused


def _coordinator_app(hub, name="coordinator-app"):
    token = hub.token(
        name,
        profile="full",
        scopes=["memory:read", "memory:write"],
        read_wings=["*"],
        write_wings=["inbox"],
        trust="trusted",
    )
    return token, hub.gateway.authenticate(f"Bearer {token}").connection_id


def test_r1_board_coordinator_is_never_granted(world):
    w = world
    hub = w.hub
    token, cid = _coordinator_app(hub)
    w.owner.board_set("work", "conn:coordinator-app")
    status, refused = api(hub, w.cookie, "PATCH", f"/api/connections/{cid}", {"decide": True})
    assert status == 409, refused
    assert refused["error"] == AGENT_TEXT
    # Refused before any write: no grant is recorded and then withdrawn.
    grants = [
        r
        for r in hub.db.audit_entries(hub.workspace, limit=500)
        if r["connection_id"] == cid and r["action"] == "connection.decide.grant"
    ]
    assert grants == []
    status, listing = api(hub, w.cookie, "GET", "/api/connections")
    row = next(c for c in listing["connections"] if c["id"] == cid)
    assert row["decide"] is False and row["decide_grantable"] is False
    # A grant stored by other means (an older build) still never answers.
    with hub.db._lock:
        row = hub.db._conn.execute("SELECT policy FROM connections WHERE id=?", (cid,)).fetchone()
        policy = json.loads(row["policy"])
        policy["scopes"] = sorted(set(policy["scopes"]) | {"cards:decide"})
        hub.db._conn.execute(
            "UPDATE connections SET policy=? WHERE id=?", (json.dumps(policy), cid)
        )
    assert hub.gateway.authenticate(f"Bearer {token}").policy.has_scope("cards:decide")
    number, version = _owner_ask(hub, asker=w.asker)
    status, refused = rest_decide(w, token, number, version)
    assert status == 403 and REFUSAL in refused["error"], refused
    assert w.owner.task_show(number)["status"] == "review"
    assert delegated_audit(hub) == []


def test_r1_assigning_a_coordinator_clears_its_grant(world):
    w = world
    hub = w.hub
    token, cid = _coordinator_app(hub)
    status, body = api(hub, w.cookie, "PATCH", f"/api/connections/{cid}", {"decide": True})
    assert status == 200 and body["connection"]["decide"] is True
    w.owner.board_set("work", "conn:coordinator-app")
    stored = json.loads(hub.db.connection(cid, hub.workspace)["policy"])
    assert "cards:decide" not in stored["scopes"]
    cleared = [
        r
        for r in hub.db.audit_entries(hub.workspace, limit=500)
        if r["action"] == "connection.decide.revoke" and r["connection_id"] == cid
    ]
    assert [r["reason"] for r in cleared] == ["coordinator"]
    # Unassigning does not bring it back: answering needs fresh consent.
    w.owner.board_set("work", None)
    number, version = _owner_ask(hub, asker=w.asker)
    status, refused = rest_decide(w, token, number, version)
    assert status == 403 and REFUSAL in refused["error"], refused
    assert w.owner.task_show(number)["status"] == "review"


def test_r2_answers_where_the_owner_can_write_as_a_person(world, monkeypatch):
    w = world
    hub = w.hub
    app, cid = owner_app(w)
    # The app itself writes only inbox; the owner, signed in, writes the team board.
    assert not hub.gateway.authenticate(f"Bearer {app}").policy.can_write_wing("shared")
    number, version = ask(w, "Team board question?", board="team")
    status, card = rest_decide(w, app, number, version)
    assert status == 200, card
    assert [r["connection_id"] for r in delegated_audit(hub)] == [cid]

    # Refused where the owner, as a person, could not write the board's wing.
    narrow = Policy.build(
        profile="full",
        scopes=["memory:read", "memory:write"],
        read_wings=["*"],
        write_wings=["inbox"],
        trust="trusted",
    )
    monkeypatch.setattr(sessions, "person_policy", lambda role=None: narrow)
    number, version = ask(w, "Second team question?", board="team")
    status, refused = rest_decide(w, app, number, version)
    assert status == 403 and "cannot change this board" in refused["error"], refused
    monkeypatch.undo()
    assert w.owner.task_show(number)["status"] == "review"
    assert len(delegated_audit(hub)) == 1


def test_r3_no_audit_row_for_an_answer_that_rolls_back(world, monkeypatch):
    w = world
    token, _cid = owner_app(w)
    number, version = ask(w)
    app = service(w.hub, w.hub.gateway.authenticate(f"Bearer {token}"))

    def fail(*args, **kwargs):
        raise RuntimeError("board commit failed")

    monkeypatch.setattr(app, "_card", fail)
    with pytest.raises(RuntimeError):
        app.decide(number, choice=OPTIONS[0], version=version)
    assert w.owner.task_show(number)["status"] == "review"
    # Written in the transaction; the rollback appends a row that cancels it.
    rows = delegated_audit(w.hub)
    assert len(rows) == 1, "the audit row is written inside the board transaction"
    written = rows[0]
    cancels = [
        r
        for r in w.hub.db.audit_entries(w.hub.workspace, limit=500)
        if r["action"] == "card.decide.rolled_back"
    ]
    assert [(r["reason"], json.loads(r["target_ids"])) for r in cancels] == [
        (f"audit_seq={written['seq']}", [str(number)])
    ]


def test_r8_a_failed_audit_write_fails_the_answer(world, monkeypatch):
    w = world
    token, _cid = owner_app(w)
    number, version = ask(w)
    real = w.hub.db.audit

    def broken(**entry):
        if entry.get("action") == AUDIT_ACTION:
            raise RuntimeError("audit log unavailable")
        return real(**entry)

    monkeypatch.setattr(w.hub.db, "audit", broken)
    app = service(w.hub, w.hub.gateway.authenticate(f"Bearer {token}"))
    with pytest.raises(RuntimeError):
        app.decide(number, choice=OPTIONS[0], version=version)
    monkeypatch.undo()
    assert w.owner.task_show(number)["status"] == "review"
    assert delegated_audit(w.hub) == []


def test_r9_a_person_policy_lookup_error_fails_closed(world, monkeypatch):
    w = world
    token, _cid = owner_app(w)
    number, version = ask(w)

    def unavailable(*args, **kwargs):
        raise RuntimeError("person lookup failed")

    monkeypatch.setattr(sessions, "person_principal", unavailable)
    app = service(w.hub, w.hub.gateway.authenticate(f"Bearer {token}"))
    with pytest.raises(RuntimeError):
        app.decide(number, choice=OPTIONS[0], version=version)
    monkeypatch.undo()
    assert w.owner.task_show(number)["status"] == "review"
    assert delegated_audit(w.hub) == []


def test_r10_rotating_an_expired_app_drops_its_grant(world):
    w = world
    hub = w.hub
    token, cid = owner_app(w)
    # Rotating a live app keeps the grant.
    status, body = api(hub, w.cookie, "POST", f"/api/connections/{cid}/rotate", {})
    assert status == 200 and body["connection"]["decide"] is True
    token = body["token"]
    number, version = ask(w, "Live rotation?")
    assert rest_decide(w, token, number, version)[0] == 200
    # Expiry ends eligibility: rotation brings the token back, not the grant.
    with hub.db._lock:
        hub.db._conn.execute(
            "UPDATE connections SET expires_at=? WHERE id=?", ("2000-01-01T00:00:00Z", cid)
        )
    number, version = ask(w, "Expired app question?")
    assert rest_decide(w, token, number, version)[0] == 401
    status, body = api(hub, w.cookie, "POST", f"/api/connections/{cid}/rotate", {})
    assert status == 200 and body["connection"]["decide"] is False
    status, refused = rest_decide(w, body["token"], number, version)
    assert status == 403 and HIDDEN in refused["error"], refused
    assert w.owner.task_show(number)["status"] == "review"
    expired = [
        r["reason"]
        for r in hub.db.audit_entries(hub.workspace, limit=500)
        if r["action"] == "connection.decide.revoke" and r["connection_id"] == cid
    ]
    assert expired == ["expired"]


def test_r11_other_members_rows_are_never_offered(world):
    w = world
    hub = w.hub
    member = hub.db.create_user("member@example.com")
    hub.db.add_member(hub.workspace, member, "member", actor=hub.admin)
    token = hub.token(
        "member-app",
        user=member,
        profile="slim",
        scopes=["memory:read", "memory:write"],
        read_wings=["*"],
        write_wing="inbox",
        trust="untrusted",
    )
    cid = hub.gateway.authenticate(f"Bearer {token}").connection_id
    _own, own_id = owner_app(w)
    status, listing = api(hub, w.cookie, "GET", "/api/connections")
    rows = {c["id"]: c for c in listing["connections"]}
    assert rows[cid]["decide_grantable"] is False
    assert api(hub, w.cookie, "PATCH", f"/api/connections/{own_id}", {"decide": False})[0] == 200
    status, listing = api(hub, w.cookie, "GET", "/api/connections")
    rows = {c["id"]: c for c in listing["connections"]}
    assert rows[own_id]["decide_grantable"] is True


def test_r4_control_layer_needs_the_signed_in_owner(world):
    w = world
    hub = w.hub
    status, body = mint(w)
    assert status == 201
    cid = body["connection"]["id"]
    agent = hub.gateway.authenticate(f"Bearer {w.asker_token}")
    # A bare user id or an agent principal is not the signed-in owner.
    for actor in (hub.admin, agent):
        with pytest.raises(ControlError):
            hub.db.set_connection_decide(cid, True, actor=actor, workspace_ref=hub.workspace)
    with pytest.raises(ControlError):
        hub.db.create_token(
            workspace_ref=hub.workspace,
            user_ref=hub.admin,
            name="no-grantor",
            client="test",
            policy=Policy.build(scopes=["memory:read", "cards:decide"], read_wings=["*"]),
            actor=hub.admin,
        )
    assert "no-grantor" not in {c["name"] for c in hub.db.connections(hub.workspace)}
    # OAuth code minting never issues the scope to an agent principal.
    from commonplace.oauth import store

    client = Client(hub)
    client_row = store.client(hub.db, client.register())
    params = {
        "scope": FULL,
        "redirect_uri": json.loads(client_row["redirect_uris"])[0],
        "resource": "unused",
        "code_challenge": "x" * 43,
    }
    with pytest.raises(store.OAuthError):
        with hub.db._lock, hub.db._tx() as conn:
            store.create_code(conn, client_row, agent, params)


def test_r5_refusal_messages_name_the_actual_reason(world):
    w = world
    hub = w.hub
    _token, cid = owner_app(w)
    status, body = mint(w)
    revoked = body["connection"]["id"]
    assert api(hub, w.cookie, "DELETE", f"/api/connections/{revoked}")[0] == 200
    status, refused = api(hub, w.cookie, "PATCH", f"/api/connections/{revoked}", {"decide": True})
    assert (status, refused["error"]) == (409, INACTIVE_TEXT)
    # A runner-linked connection keeps the runner message.
    from commonplace import control_db

    with hub.db._lock:
        control_db.link_runner(hub.db._conn, cid, "mach_example", "worker")
    status, refused = api(hub, w.cookie, "PATCH", f"/api/connections/{cid}", {"decide": False})
    assert (status, refused["error"]) == (409, "manage this runner under /api/runners")
    grant = oauth_grant(hub)
    status, refused = api(
        hub, w.cookie, "PATCH", f"/api/connections/{grant.p.connection_id}", {"decide": True}
    )
    assert (status, refused["error"]) == (409, OAUTH_GRANT_TEXT)


def test_r6_losing_ownership_clears_the_stored_grant(world):
    w = world
    hub = w.hub
    token, cid = owner_app(w)
    co_owner = hub.db.create_user("co-owner@example.com")
    hub.db.add_member(hub.workspace, co_owner, "owner", actor=hub.admin)
    for _round in range(2):
        with hub.db._lock:
            for role in ("member", "owner"):
                hub.db._conn.execute(
                    "UPDATE memberships SET role=? WHERE workspace_id=? AND user_id=?",
                    (role, hub.workspace, hub.admin),
                )
        stored = json.loads(hub.db.connection(cid, hub.workspace)["policy"])
        assert "cards:decide" not in stored["scopes"]
        assert not hub.gateway.authenticate(f"Bearer {token}").policy.has_scope("cards:decide")
        # Granting again needs the owner's fresh, explicit action.
        assert api(hub, w.cookie, "PATCH", f"/api/connections/{cid}", {"decide": True})[0] == 200
    lost = [
        r
        for r in hub.db.audit_entries(hub.workspace, limit=500)
        if r["action"] == "connection.decide.revoke" and r["reason"] == "ownership lost"
    ]
    assert [r["connection_id"] for r in lost] == [cid, cid]


def test_r7_consent_copy_has_one_source_and_names_the_scope(world):
    from pathlib import Path

    w = world
    grant = oauth_grant(w.hub)
    label = re.search(r'name="grant_decide" value="1"> ([^<]+)</label>', grant.page)[1]
    assert unescape(label) == SCOPE_TEXT and "cards:decide" in SCOPE_TEXT
    assert SCOPE_TEXT.endswith("only on boards you can write to.")
    status, listing = api(w.hub, w.cookie, "GET", "/api/connections")
    assert status == 200 and listing["decide_consent"] == SCOPE_TEXT
    static = Path(__file__).parents[1] / "commonplace" / "web" / "static" / "agents.js"
    assert "answer your Needs-me questions as you" not in static.read_text()


# ── refresh, spellings, runners, other workspaces ──


def test_refresh_does_not_restore_a_withdrawn_grant(world):
    w = world
    hub = w.hub
    client = Client(hub)
    client.login("owner@example.com")
    cid = client.register(client_name="Owner Phone")
    path = client.authorize_path(cid, scope=FULL)
    _, _, page = client.request("GET", path)
    request = unescape(re.search(r'name="request" value="([^"]+)"', page)[1])
    _, headers, _ = client.request(
        "POST", path, {"request": request, "decision": "allow", "grant_decide": "1"}
    )
    code = parse_qs(urlsplit(headers["Location"]).query)["code"][0]
    issued = json.loads(client.exchange(cid, code)[2])
    conn_id = hub.gateway.authenticate("Bearer " + issued["access_token"]).connection_id
    assert api(hub, w.cookie, "PATCH", f"/api/connections/{conn_id}", {"decide": False})[0] == 200
    status, _, raw = client.refresh(cid, issued["refresh_token"])
    assert status == 200, raw
    fresh = json.loads(raw)
    assert "cards:decide" in issued["scope"].split()
    # The response lists only what is live after the withdrawal.
    assert fresh["scope"].split() == ["memory:read", "memory:write"]
    assert not hub.gateway.authenticate("Bearer " + fresh["access_token"]).policy.has_scope(
        "cards:decide"
    )
    number, version = ask(w, "After refresh?")
    assert rest_decide(w, fresh["access_token"], number, version)[0] == 403


def test_refresh_cannot_widen_into_the_scope(world):
    client = Client(world.hub)
    client.login("owner@example.com")
    cid = client.register(client_name="Owner Phone")
    path = client.authorize_path(cid, scope=FULL)
    _, _, page = client.request("GET", path)
    request = unescape(re.search(r'name="request" value="([^"]+)"', page)[1])
    _, headers, _ = client.request("POST", path, {"request": request, "decision": "allow"})
    code = parse_qs(urlsplit(headers["Location"]).query)["code"][0]
    issued = json.loads(client.exchange(cid, code)[2])
    assert "cards:decide" not in issued["scope"].split()
    status, _, raw = client.refresh(cid, issued["refresh_token"], scope=FULL)
    assert status == 400 and "invalid_scope" in raw


def test_scope_spellings_and_untyped_consent_are_refused(world):
    from commonplace.oauth.store import OAuthError, scope as parse_scope
    from commonplace.policy import PolicyError

    for bad in ("cards:decide,memory:read", "CARDS:DECIDE", "cards:*", "memory:*", "cards:decide_"):
        with pytest.raises(OAuthError):
            parse_scope(bad)
    for bad in ("cards:decide ", "CARDS:DECIDE", "cards:*"):
        with pytest.raises(PolicyError):
            Policy.build(scopes=[bad])
    for value in ("true", 1, None):
        assert mint(world, decide=value)[0] == 400, value


def test_runner_connections_never_answer(world):
    from commonplace import control_db

    w = world
    hub = w.hub
    token, cid = owner_app(w)
    with hub.db._lock:
        control_db.link_runner(hub.db._conn, cid, "mach_example", "worker")
    number, version = ask(w)
    status, refused = rest_decide(w, token, number, version)
    assert status == 403, refused
    status, listing = api(hub, w.cookie, "GET", "/api/connections")
    row = next(c for c in listing["connections"] if c["id"] == cid)
    assert row["decide_grantable"] is False
    assert w.owner.task_show(number)["status"] == "review"


def test_an_app_answers_only_in_its_own_workspace(world):
    w = world
    hub = w.hub
    token, _cid = owner_app(w)
    other = hub.db.create_workspace("second", hub.admin)
    second = service(hub, sessions.person_principal(hub.db, hub.admin, other))
    second.board_add(slug="work", wing="inbox")
    asker_token = hub.token(
        "second-asker",
        workspace=other,
        profile="full",
        scopes=["memory:read", "memory:write"],
        read_wings=["*"],
        write_wings=["*"],
        trust="trusted",
    )
    asker = service(hub, hub.gateway.authenticate(f"Bearer {asker_token}"))
    card = asker.ask(
        "Second workspace: ship this now? Choose one.",
        options=OPTIONS,
        board="work",
        context="Notes in the second workspace are ready.",
    )
    assert card["ask_to"] == f"user:{hub.admin}"
    status, _ = rest_decide(w, token, card["number"], card["version"])
    assert status == 404
    assert second.task_show(card["number"])["status"] == "review"
    assert delegated_audit(hub) == []


# ── grant/assignment races, expired rows, other members ──


def test_r12_a_grant_racing_a_coordinator_assignment_is_withdrawn(world, monkeypatch):
    from commonplace.web import agents

    w = world
    hub = w.hub
    token, cid = _coordinator_app(hub)
    real = agents.coordinators
    calls = []

    def stale_then_assign(gateway, principal):
        # The grant request reads no coordinator; the owner's other request then
        # assigns the app and commits before the grant is written.
        calls.append(1)
        if len(calls) == 1:
            snapshot = real(gateway, principal)
            w.owner.board_set("work", "conn:coordinator-app")
            return snapshot
        return real(gateway, principal)

    monkeypatch.setattr(agents, "coordinators", stale_then_assign)
    status, refused = api(hub, w.cookie, "PATCH", f"/api/connections/{cid}", {"decide": True})
    monkeypatch.undo()
    assert status == 409, refused
    assert refused["error"] == AGENT_TEXT
    assert (
        "cards:decide" not in json.loads(hub.db.connection(cid, hub.workspace)["policy"])["scopes"]
    )
    w.owner.board_set("work", None)
    number, version = _owner_ask(hub, asker=w.asker)
    status, refused = rest_decide(w, token, number, version)
    assert status == 403 and REFUSAL in refused["error"], refused


def test_r13_a_grant_written_before_the_coordinator_commit_is_cleared(world, monkeypatch):
    w = world
    hub = w.hub
    token, cid = _coordinator_app(hub)
    person = sessions.person_principal(hub.db, hub.admin, hub.workspace)
    real = hub.db.clear_decide
    calls = []

    def grant_lands_inside_the_assignment(connection_id, **kwargs):
        # The in-transaction clear runs, then a grant (whose coordinator check ran
        # before this assignment committed) is written before the board COMMIT.
        removed = real(connection_id, **kwargs)
        calls.append(1)
        if len(calls) == 1:
            hub.db.set_connection_decide(cid, True, actor=person, workspace_ref=hub.workspace)
        return removed

    monkeypatch.setattr(hub.db, "clear_decide", grant_lands_inside_the_assignment)
    w.owner.board_set("work", "conn:coordinator-app")
    monkeypatch.undo()
    assert (
        "cards:decide" not in json.loads(hub.db.connection(cid, hub.workspace)["policy"])["scopes"]
    )
    w.owner.board_set("work", None)
    number, version = _owner_ask(hub, asker=w.asker)
    status, refused = rest_decide(w, token, number, version)
    assert status == 403 and REFUSAL in refused["error"], refused


def test_r14_expired_rows_are_not_offered_and_can_still_be_withdrawn(world):
    w = world
    hub = w.hub
    status, body = mint(w)
    plain = body["connection"]["id"]
    _token, granted = owner_app(w)
    with hub.db._lock:
        hub.db._conn.execute(
            "UPDATE connections SET expires_at=? WHERE id IN (?, ?)",
            ("2000-01-01T00:00:00Z", plain, granted),
        )
    status, listing = api(hub, w.cookie, "GET", "/api/connections")
    rows = {c["id"]: c for c in listing["connections"]}
    assert rows[plain]["decide_grantable"] is False
    assert rows[granted]["decide_grantable"] is False and rows[granted]["decide"] is True
    status, body = api(hub, w.cookie, "PATCH", f"/api/connections/{granted}", {"decide": False})
    assert status == 200, body
    assert body["connection"]["decide"] is False
    stored = json.loads(hub.db.connection(granted, hub.workspace)["policy"])
    assert "cards:decide" not in stored["scopes"]
    # Granting still needs a live connection.
    status, refused = api(hub, w.cookie, "PATCH", f"/api/connections/{plain}", {"decide": True})
    assert (status, refused["error"]) == (409, INACTIVE_TEXT)


def test_r15_another_members_row_gets_its_own_message(world):
    w = world
    hub = w.hub
    member = hub.db.create_user("member@example.com")
    hub.db.add_member(hub.workspace, member, "member", actor=hub.admin)
    token = hub.token(
        "member-app",
        user=member,
        profile="slim",
        scopes=["memory:read", "memory:write"],
        read_wings=["*"],
        write_wing="inbox",
        trust="untrusted",
    )
    cid = hub.gateway.authenticate(f"Bearer {token}").connection_id
    status, refused = api(hub, w.cookie, "PATCH", f"/api/connections/{cid}", {"decide": True})
    assert status == 409, refused
    assert refused["error"] == NOT_YOURS_TEXT


def test_r16_the_in_transaction_clear_survives_a_kill_after_the_commit(world, monkeypatch):
    w = world
    hub = w.hub
    token, cid = _coordinator_app(hub)
    status, _ = api(hub, w.cookie, "PATCH", f"/api/connections/{cid}", {"decide": True})
    assert status == 200
    real = hub.db.clear_decide

    def committed():
        with w.owner.store.connection() as conn:
            row = conn.execute("SELECT coordinator FROM boards WHERE slug='work'").fetchone()
        return row["coordinator"] == "conn:coordinator-app"

    def killed_after_commit(connection_id, **kwargs):
        # The process dies after the board COMMIT, before the second clear runs.
        if committed():
            raise RuntimeError("killed after the board COMMIT")
        return real(connection_id, **kwargs)

    monkeypatch.setattr(hub.db, "clear_decide", killed_after_commit)
    with pytest.raises(RuntimeError, match="killed after"):
        w.owner.board_set("work", "conn:coordinator-app")
    monkeypatch.undo()
    assert committed()
    assert (
        "cards:decide" not in json.loads(hub.db.connection(cid, hub.workspace)["policy"])["scopes"]
    )


# ── removal paths clear a leftover grant before COMMIT ──


def _stored_decide(hub, cid):
    return "cards:decide" in json.loads(hub.db.connection(cid, hub.workspace)["policy"])["scopes"]


def test_r17_unassigning_clears_a_grant_left_by_a_failed_post_commit_clear(world, monkeypatch):
    w = world
    hub = w.hub
    token, cid = _coordinator_app(hub)
    person = sessions.person_principal(hub.db, hub.admin, hub.workspace)
    real = hub.db.clear_decide
    calls = []

    def window_grant_then_failed_post_commit(connection_id, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            removed = real(connection_id, **kwargs)  # the in-transaction clear
            # A grant whose coordinator read ran before the assignment committed.
            hub.db.set_connection_decide(cid, True, actor=person, workspace_ref=hub.workspace)
            return removed
        raise RuntimeError("post-commit clear failed")

    monkeypatch.setattr(hub.db, "clear_decide", window_grant_then_failed_post_commit)
    with pytest.raises(RuntimeError, match="post-commit"):
        w.owner.board_set("work", "conn:coordinator-app")
    monkeypatch.undo()
    assert _stored_decide(hub, cid)  # the leftover grant the unassign must remove
    w.owner.board_set("work", None)
    assert not _stored_decide(hub, cid)
    number, version = _owner_ask(hub, asker=w.asker)
    status, body = rest_decide(w, token, number, version)
    assert status == 403, body


def test_r18_unassigning_clears_a_grant_left_by_a_request_that_died(world, monkeypatch):
    from commonplace.web import agents

    w = world
    hub = w.hub
    token, cid = _coordinator_app(hub)
    real = agents.coordinators
    calls = []

    def assign_then_die(gateway, principal):
        calls.append(1)
        if len(calls) == 1:
            snapshot = real(gateway, principal)
            w.owner.board_set("work", "conn:coordinator-app")
            return snapshot
        raise RuntimeError("killed between the grant write and the re-check")

    monkeypatch.setattr(agents, "coordinators", assign_then_die)
    try:
        api(hub, w.cookie, "PATCH", f"/api/connections/{cid}", {"decide": True})
    except Exception:
        pass  # the request dies after the grant write
    monkeypatch.undo()
    assert _stored_decide(hub, cid)
    w.owner.board_set("work", None)
    number, version = _owner_ask(hub, asker=w.asker)
    status, body = rest_decide(w, token, number, version)
    assert status == 403, body


def test_r19_replacing_a_coordinator_clears_its_leftover_grant(world):
    w = world
    hub = w.hub
    token, cid = _coordinator_app(hub)
    _other, _other_id = _coordinator_app(hub, "other-coordinator")
    w.owner.board_set("work", "conn:coordinator-app")
    person = sessions.person_principal(hub.db, hub.admin, hub.workspace)
    hub.db.set_connection_decide(cid, True, actor=person, workspace_ref=hub.workspace)
    w.owner.board_set("work", "conn:other-coordinator")
    assert not _stored_decide(hub, cid)
    number, version = _owner_ask(hub, asker=w.asker)
    status, body = rest_decide(w, token, number, version)
    assert status == 403, body


def test_r20_auto_unbind_clears_a_leftover_grant(world):
    w = world
    hub = w.hub
    token = hub.token(
        "team-app",
        profile="full",
        scopes=["memory:read", "memory:write"],
        read_wings=["*"],
        write_wings=["shared", "inbox"],
        trust="trusted",
    )
    cid = hub.gateway.authenticate(f"Bearer {token}").connection_id
    w.owner.board_set("team", "conn:team-app")
    person = sessions.person_principal(hub.db, hub.admin, hub.workspace)
    # The leftover state from a failed clear: a grant stored while it coordinates.
    hub.db.set_connection_decide(cid, True, actor=person, workspace_ref=hub.workspace)
    w.owner.board_own_wing("team")  # the app cannot coordinate the new wing: auto-unbind
    assert "conn:team-app" not in w.owner.coordinator_actors()
    assert not _stored_decide(hub, cid)
    number, version = _owner_ask(hub, board="team", asker=w.asker)
    status, body = rest_decide(w, token, number, version)
    assert status == 403, body


def _failing_clear(*args, **kwargs):
    raise RuntimeError("clear failed")


def _board_wing(w, slug):
    with w.owner.store.connection() as conn:
        return conn.execute("SELECT wing FROM boards WHERE slug=?", (slug,)).fetchone()["wing"]


def test_r21_a_failed_clear_rolls_back_the_unassign(world, monkeypatch):
    w = world
    hub = w.hub
    _token, cid = _coordinator_app(hub)
    w.owner.board_set("work", "conn:coordinator-app")
    person = sessions.person_principal(hub.db, hub.admin, hub.workspace)
    hub.db.set_connection_decide(cid, True, actor=person, workspace_ref=hub.workspace)
    monkeypatch.setattr(hub.db, "clear_decide", _failing_clear)
    with pytest.raises(RuntimeError, match="clear failed"):
        w.owner.board_set("work", None)
    monkeypatch.undo()
    # The clear runs before the board COMMIT, so the removal rolled back with it.
    assert "conn:coordinator-app" in w.owner.coordinator_actors()
    assert _stored_decide(hub, cid)
    # A retry with a working clear completes the removal and leaves no grant.
    w.owner.board_set("work", None)
    assert "conn:coordinator-app" not in w.owner.coordinator_actors()
    assert not _stored_decide(hub, cid)


def test_r22_a_failed_clear_rolls_back_the_auto_unbind(world, monkeypatch):
    w = world
    hub = w.hub
    token = hub.token(
        "team-app",
        profile="full",
        scopes=["memory:read", "memory:write"],
        read_wings=["*"],
        write_wings=["shared", "inbox"],
        trust="trusted",
    )
    cid = hub.gateway.authenticate(f"Bearer {token}").connection_id
    w.owner.board_set("team", "conn:team-app")
    person = sessions.person_principal(hub.db, hub.admin, hub.workspace)
    hub.db.set_connection_decide(cid, True, actor=person, workspace_ref=hub.workspace)
    wing_before = _board_wing(w, "team")
    monkeypatch.setattr(hub.db, "clear_decide", _failing_clear)
    with pytest.raises(RuntimeError, match="clear failed"):
        w.owner.board_own_wing("team")
    monkeypatch.undo()
    assert _board_wing(w, "team") == wing_before
    assert "conn:team-app" in w.owner.coordinator_actors()
    assert _stored_decide(hub, cid)


def test_r23_a_crash_after_the_clear_leaves_the_coordinator_without_a_grant(world, monkeypatch):
    w = world
    hub = w.hub
    token, cid = _coordinator_app(hub)
    w.owner.board_set("work", "conn:coordinator-app")
    person = sessions.person_principal(hub.db, hub.admin, hub.workspace)
    hub.db.set_connection_decide(cid, True, actor=person, workspace_ref=hub.workspace)
    real = hub.db.clear_decide

    def clear_then_die(connection_id, **kwargs):
        real(connection_id, **kwargs)  # its own control-DB commit, before the board COMMIT
        raise RuntimeError("killed after the clear, before the board COMMIT")

    monkeypatch.setattr(hub.db, "clear_decide", clear_then_die)
    with pytest.raises(RuntimeError, match="killed"):
        w.owner.board_set("work", None)
    monkeypatch.undo()
    # Fail closed: the removal rolled back, the grant stays cleared.
    assert "conn:coordinator-app" in w.owner.coordinator_actors()
    assert not _stored_decide(hub, cid)
    number, version = _owner_ask(hub, asker=w.asker)
    status, body = rest_decide(w, token, number, version)
    assert status == 403, body
