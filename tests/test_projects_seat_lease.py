"""A seat made in Project settings can hold its own board's orchestrator lease (#348).

The owner only uses the routes behind the settings screen: the orchestration switch,
Connect and "Trust and allow starts". The seat then uses its own token over MCP. No
orchestrator config is written and no requester list is edited by hand.
"""

import json

import pytest

from commonplace import sessions
from commonplace.extensions import ToolError
from commonplace.orch import runner_bridge, store as orch_store
from tests._commonplace_helpers import hub_fixture  # noqa: F401
from tests.runner_harness import http
from tests.test_orch_runner import BOARD, PLAIN, ROLE, World, config_text


class Settings:
    """The owner's browser session against the real routes."""

    def __init__(self, hub):
        self.w = World(hub)
        link = sessions.create_login_link(hub.db, "owner@example.com", hub.workspace)
        self.cookie = f"{sessions.COOKIE}={sessions.redeem_login_link(hub.db, link)}"
        status, body = self.owner("PUT", "/api/settings/orchestration", {"enabled": True})
        assert status == 200, body

    def owner(self, method, path, body=None):
        return http(self.w.hub.port, method, path, cookie=self.cookie, body=body)

    def connect(self, board=BOARD):
        status, body = self.owner(
            "POST", f"/api/projects/{board}/orchestrator/connect", {"allow_shared": True}
        )
        assert status == 201, body
        return body["token"]

    def trust(self, board=BOARD, body=None):
        return self.owner(
            "POST",
            f"/api/projects/{board}/orchestrator/trust",
            body or {"confirm": True, "machines": [self.w.machine_id]},
        )

    def seat(self, board=BOARD):
        token = self.connect(board)
        status, body = self.trust(board)
        assert status == 200 and body["trusted"] is True, body
        return token

    def tool(self, bearer, name, /, **arguments):
        status, body = http(
            self.w.hub.port,
            "POST",
            "/mcp",
            token=bearer,
            body={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
        )
        assert status == 200, body
        result = body["result"]
        text = "\n".join(part.get("text", "") for part in result["content"])
        return not result.get("isError", False), text

    def resume(self, token, board=BOARD):
        return self.tool(token, "orch_resume", board=board, role=ROLE)

    def live(self, board=BOARD):
        return orch_store.open_store(self.w.gw, self.w.ws).accepted(board)


@pytest.fixture
def settings(hub, monkeypatch):
    monkeypatch.setenv("COMMONPLACE_RATE_LIMITS", "off")
    return Settings(hub)


def test_seat_from_settings_takes_holds_and_renews_its_lease(settings):
    s = settings
    token = s.seat()

    assert s.live() is None  # no config: nothing was set up behind the app
    ok, text = s.resume(token)
    assert ok, text
    lease = json.loads(text)
    assert lease["epoch"] == 1 and lease["board"] == BOARD and lease["role"] == ROLE
    held = {"board": BOARD, "role": ROLE, "epoch": 1, "token": lease["token"]}

    # Held: a second claim, even by the same seat, waits for release or expiry.
    ok, text = s.resume(token)
    assert not ok and '"code": "held"' in text and '"epoch": 1' in text, text

    for _ in range(2):
        ok, text = s.tool(token, "orch_heartbeat", **held)
        assert ok, text
        renewed = json.loads(text)
        assert renewed["epoch"] == 1 and renewed["expires_at"] >= lease["expires_at"]

    ok, text = s.tool(token, "orch_release", **held)
    assert ok, text
    ok, text = s.resume(token)
    assert ok and json.loads(text)["epoch"] == 2, text


def test_seat_keeps_its_direct_starts_and_the_board_stays_unconfigured(settings):
    s = settings
    token = s.seat()
    assert s.resume(token)[0]
    assert s.live() is None and not runner_bridge.configured(s.w.gw, s.w.ws, BOARD)
    status, body = http(
        s.w.hub.port,
        "POST",
        "/api/card-starts",
        token=token,
        body={"task": s.w.card(), "machine": s.w.machine_id, "project": "demo", "topic": "worker"},
    )
    assert status == 201, body


@pytest.mark.parametrize("runner", [True, False])
def test_seat_lease_with_an_owner_config(settings, runner):
    s = settings
    text = config_text(s.w.machine_id, runner=runner)
    written = s.w.write(text).version
    token = s.seat()
    ok, answer = s.resume(token)
    assert ok and json.loads(answer)["epoch"] == 1, answer
    assert s.live().version == written.version and s.live().body == text


def test_connected_seat_without_trust_is_refused(settings):
    s = settings
    token = s.connect()
    ok, text = s.resume(token)
    assert not ok and '"code": "not_allowed"' in text and "Project settings" in text, text
    # On a configured board Connect alone is not the authority either.
    s.w.configure()
    ok, text = s.resume(token)
    assert not ok and '"code": "not_allowed"' in text, text
    with s.w.js.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM orch_leases").fetchone()[0] == 0


@pytest.mark.parametrize("change", ["demote", "off", "revoke"])
def test_seat_loses_the_lease_with_its_authority(settings, change):
    s = settings
    token = s.seat()
    ok, text = s.resume(token)
    assert ok, text
    held = {"board": BOARD, "role": ROLE, "epoch": 1, "token": json.loads(text)["token"]}
    if change == "demote":
        status, body = s.trust(body={"trusted": False})
        assert status == 200 and body == {"trusted": False}, body
    elif change == "off":
        assert s.owner("PUT", "/api/settings/orchestration", {"enabled": False})[0] == 200
    else:
        seat = s.w.gw.authenticate("Bearer " + token).connection_id
        s.w.db.revoke(seat, actor=s.w.owner.user_id, workspace_ref=s.w.ws)
    if change == "revoke":
        status, body = http(
            s.w.hub.port, "POST", "/mcp", token=token, body={"jsonrpc": "2.0", "id": 1}
        )
        assert status == 401, body
        return
    for name, arguments in (
        ("orch_heartbeat", held),
        ("orch_resume", {"board": BOARD, "role": ROLE}),
    ):
        ok, text = s.tool(token, name, **arguments)
        assert not ok and '"code": "not_allowed"' in text, text


def test_seat_of_another_project_is_refused(settings):
    s = settings
    s.seat()
    other = s.seat(PLAIN)
    # Same workspace, same shared wing, trusted: still only its own board.
    ok, text = s.resume(other)
    assert not ok and '"code": "not_configured"' in text, text
    # Also when the board has a runner whose machine lists that seat as a requester.
    s.w.configure()
    ok, text = s.resume(other)
    assert not ok and '"code": "not_allowed"' in text, text
    with s.w.js.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM orch_leases").fetchone()[0] == 0
    ok, text = s.resume(other, PLAIN)
    assert ok and json.loads(text)["board"] == PLAIN, text


def test_seat_outside_the_boards_wing_cannot_see_the_board(settings):
    s = settings
    s.w.boards.board_add(slug="apart", wing="apart")
    status, body = s.owner("POST", "/api/projects/apart/orchestrator/connect", {})
    assert status == 201, body
    assert s.trust("apart")[0] == 200
    ok, text = s.resume(body["token"])
    assert not ok and '"code": "board_not_found"' in text, text


def test_only_the_seat_and_only_its_role_pass_without_a_runner(settings):
    s = settings
    token = s.seat()
    ok, text = s.tool(token, "orch_resume", board=BOARD, role="builder")
    assert not ok and '"code": "not_configured"' in text, text
    # Owners and trusted requesters of a machine keep the existing refusal.
    for principal in (s.w.owner, s.w.a):
        with pytest.raises(ToolError) as refused:
            s.w.claim(principal)
        assert refused.value.code == "not_configured"


def test_a_config_accepted_during_the_claim_is_noticed(settings, monkeypatch):
    s = settings
    token = s.seat()
    seat = s.w.gw.authenticate("Bearer " + token)
    check = orch_store.check_restore_in

    def accept_then_check(conn):
        monkeypatch.setattr(orch_store, "check_restore_in", check)
        conn.execute(
            "INSERT INTO orch_configs(board, version, channel, status, sha256, body, "
            "proposed_by, proposer_trust, created_at) "
            "VALUES (?, 1, 'live', 'accepted', ?, '', 'user:example', 'trusted', ?)",
            (BOARD, "0" * 64, "2030-01-01T00:00:00Z"),
        )
        return check(conn)

    monkeypatch.setattr(orch_store, "check_restore_in", accept_then_check)
    with pytest.raises(ToolError) as refused:
        s.w.claim(seat)
    assert refused.value.code == "config_changed"
