"""An owner can revoke an OAuth-connected client from Settings > Agents (DELETE /api/connections).

Before this the route answered 409 "Manage this connection in its sign-in client" for every
OAuth row, so an owner had no way to cut off a client they had approved.
"""

import json
from types import SimpleNamespace

import pytest

from commonplace import sessions
from tests._commonplace_helpers import hub_fixture as hub_fixture
from tests.test_commonplace_agent_connections import http, signup
from tests.test_commonplace_oauth import Client, assert_error


def _cookie(hub, email, role):
    user = hub.db.create_user(email)
    hub.db.add_member(hub.workspace, user, role, actor=hub.admin)
    session = sessions.redeem_login_link(
        hub.db, sessions.create_login_link(hub.db, email, hub.workspace)
    )
    return f"{sessions.COOKIE}={session}"


@pytest.fixture
def connected(hub):
    client = Client(hub)
    client_id = client.register()
    pair = client.grant(client_id)
    row = next(c for c in hub.db.connections(hub.workspace) if c["auth"] == "oauth")
    assert hub.raw(pair["access_token"], {"jsonrpc": "2.0", "id": 1, "method": "ping"})[0] == 200
    return SimpleNamespace(
        client=client,
        client_id=client_id,
        pair=pair,
        id=row["id"],
        path=f"/api/connections/{row['id']}",
        owner=f"{sessions.COOKIE}={client.cookies[sessions.COOKIE]}",
    )


def _live(hub, connected):
    return not hub.db.connection(connected.id)["revoked_at"]


def _ping(hub, token):
    return hub.raw(token, {"jsonrpc": "2.0", "id": 1, "method": "ping"})[0]


def test_owner_revokes_oauth_client_and_both_tokens_stop_working(hub, connected):
    status, _, listed = http(hub, "GET", cookie=connected.owner)
    row = next(c for c in listed["connections"] if c["id"] == connected.id)
    assert status == 200 and row["auth"] == "oauth" and not row["revoked_at"]

    assert http(hub, "DELETE", connected.path, cookie=connected.owner)[:1] == (200,)

    assert _ping(hub, connected.pair["access_token"]) == 401
    assert_error(connected.client.refresh(connected.client_id, connected.pair["refresh_token"]))
    tokens = hub.db._conn.execute(
        "SELECT kind, revoked_at FROM oauth_tokens WHERE connection_id = ?", (connected.id,)
    ).fetchall()
    assert {t["kind"] for t in tokens} == {"access", "refresh"}
    assert all(t["revoked_at"] for t in tokens)
    _, _, listed = http(hub, "GET", cookie=connected.owner)
    assert next(c for c in listed["connections"] if c["id"] == connected.id)["revoked_at"]
    assert {
        (e["action"], e["auth"], e["connection_id"])
        for e in hub.db.audit_entries(hub.workspace)
        if e["action"] == "token.revoke"
    } == {("token.revoke", "oauth", connected.id)}
    # Revoking twice is harmless, as it is for a personal access token.
    assert http(hub, "DELETE", connected.path, cookie=connected.owner)[0] == 200


def test_revoke_also_kills_an_unredeemed_authorization_code(hub, connected):
    code = connected.client.consent(connected.client_id)
    # Every consent makes its own connection; the code belongs to the newest one.
    pending = next(
        c["id"]
        for c in hub.db.connections(hub.workspace)
        if c["auth"] == "oauth" and c["id"] != connected.id
    )
    assert http(hub, "DELETE", f"/api/connections/{pending}", cookie=connected.owner)[0] == 200
    assert_error(connected.client.exchange(connected.client_id, code))
    assert _live(hub, connected) and _ping(hub, connected.pair["access_token"]) == 200


def test_revoking_keeps_what_the_client_wrote(hub, connected):
    token = connected.pair["access_token"]
    saved = hub.rpc(
        token,
        "tools/call",
        {
            "name": "mempalace_add_drawer",
            "arguments": {"wing": "inbox", "room": "oauth", "content": "WRITTEN_BY_OAUTH_CLIENT"},
        },
    )["result"]
    assert not saved.get("isError"), saved
    drawer = json.loads(saved["content"][0]["text"])["drawer_id"]

    assert http(hub, "DELETE", connected.path, cookie=connected.owner)[0] == 200

    read = hub.rpc(
        hub.owner_token("after"),
        "tools/call",
        {
            "name": "mempalace_get_drawer",
            "arguments": {"drawer_id": drawer},
        },
    )["result"]
    assert "WRITTEN_BY_OAUTH_CLIENT" in json.dumps(read)
    # The row stays as history; it is revoked, not deleted.
    assert hub.db.connection(connected.id)["revoked_at"]


def test_a_member_cannot_revoke(hub, connected):
    member = _cookie(hub, "member@example.test", "member")
    assert http(hub, "DELETE", connected.path, cookie=member)[0] == 403
    assert _live(hub, connected) and _ping(hub, connected.pair["access_token"]) == 200


def test_another_workspaces_owner_cannot_revoke(hub, connected):
    stranger = signup(hub, "stranger@example.test")
    assert http(hub, "DELETE", connected.path, cookie=stranger.cookie)[0] == 404
    assert _live(hub, connected) and _ping(hub, connected.pair["access_token"]) == 200


def test_anonymous_and_the_client_itself_cannot_revoke(hub, connected):
    assert http(hub, "DELETE", connected.path)[0] == 401
    status, _, body = http(hub, "DELETE", connected.path, token=connected.pair["access_token"])
    assert status == 403, body
    assert _live(hub, connected) and _ping(hub, connected.pair["access_token"]) == 200


@pytest.mark.parametrize(
    "method, suffix, body",
    [("POST", "/rotate", {}), ("PATCH", "", {"read_wings": ["shared"]})],
)
def test_rotate_and_edit_stay_unavailable_for_oauth_rows(hub, connected, method, suffix, body):
    status, _, payload = http(
        hub, method, connected.path + suffix, cookie=connected.owner, body=body
    )
    assert status == 409 and payload["error"]
    assert _live(hub, connected) and _ping(hub, connected.pair["access_token"]) == 200


def test_agents_screen_offers_revoke_but_not_rotate_for_oauth_rows():
    import shutil
    import subprocess
    from pathlib import Path

    script = r"""
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
const url = (text) => `data:text/javascript;base64,${Buffer.from(text).toString("base64")}`;
const apiSource = await readFile("commonplace/web/static/api.js", "utf8");
const source = await readFile("commonplace/web/static/agents.js", "utf8");
const agents = await import(url(source.replace('"./api.js"', JSON.stringify(url(apiSource)))));
const oauth = { id: "conn-oauth", name: "oauth:claude <x>", auth: "oauth", client: "claude-ai", read_wings: ["shared"], revoked_at: null };
const pat = { id: "conn-pat", name: "my-agent", auth: "pat", client: "codex", read_wings: ["shared"], revoked_at: null };
const row = (html, id) => html.split('<div class="setting-row">').find((part) => part.includes(`data-agent-id="${id}"`)) || "";
const html = agents.agentsView({ connections: [oauth, pat], wings: ["shared"] });
const live = row(html, "conn-oauth");
assert.equal(live.split('data-action="agent-revoke"').length - 1, 1, "one Revoke for a live OAuth row");
assert.ok(!live.includes('data-action="agent-rotate"'), "no Rotate for an OAuth row");
assert.ok(!live.includes('data-action="agent-wings"'), "no read-access editor for an OAuth row");
assert.ok(live.includes("Connected through sign-in (OAuth)"));
assert.ok(live.includes("oauth:claude &lt;x&gt;"));
const patRow = row(html, "conn-pat");
assert.ok(patRow.includes('data-action="agent-rotate"') && patRow.includes('data-action="agent-revoke"'));
assert.ok(!patRow.includes("Connected through sign-in"));
const revoked = agents.agentsView({ connections: [{ ...oauth, revoked_at: "2026-01-01T00:00:00Z" }], wings: ["shared"] });
assert.ok(revoked.includes("Revoked"));
assert.ok(!revoked.includes('data-action="agent-revoke"') && !revoked.includes('data-action="agent-rotate"'));
assert.ok(!agents.agentsView({ connections: [oauth], wings: [] }).includes('<option value="conn-oauth"'), "an OAuth client is never a parent choice");
"""
    result = subprocess.run(
        [shutil.which("node"), "--input-type=module", "-e", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
