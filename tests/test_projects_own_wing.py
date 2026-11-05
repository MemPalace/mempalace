"""Project own-wing behaviour and guards (synthetic data only)."""

import json
import subprocess
from pathlib import Path

import pytest

from commonplace import control_db
from commonplace.boards.service import BoardService
from commonplace.extensions import ToolContext
from commonplace.gateway import _public_route
from tests._projects_helpers import projects_hub_fixture  # noqa: F401
from tests.test_console_guards import PIN_PUBLIC


@pytest.fixture
def h(projects_hub):
    h = projects_hub
    assert h.create_board(h.owner, "shared-board", wing="shared")[0] == 201
    return h


def own(h, auth=None, slug="shared-board", body=None):
    return h.request(
        auth or h.owner, "POST", f"/api/projects/{slug}/wing", {} if body is None else body
    )[:2]


def service(h):
    if not hasattr(h, "store_principal"):
        h.store_principal = h.gateway.authenticate("Bearer " + h.hub.owner_token("store-owner"))
    return BoardService(ToolContext(principal=h.store_principal, gateway=h.gateway))


def store(h):
    return service(h).store


def token(h, name="broad", **kw):
    return h.hub.token(
        name,
        profile="full",
        scopes=kw.pop("scopes", ["memory:read", "memory:write"]),
        read_wings=kw.pop("read_wings", ["shared", "project_*"]),
        write_wings=kw.pop("write_wings", ["shared", "project_*"]),
        trust="trusted",
        **kw,
    )


def card(h, slug="shared-board"):
    status, body, _ = h.request(
        h.owner, "POST", f"/api/boards/{slug}/cards", {"title": "Synthetic task"}
    )
    assert status == 201
    return body["number"]


def save(h, task=None, auth=None, **kw):
    args = {"text": "zephyr-marker exact words", **kw}
    if task is not None:
        args["task"] = task
    status, body, _ = h.request(auth or h.owner, "POST", "/api/memory", args)
    assert status == 201, body
    return body["id"]


def crash(h):
    status, body = h.connect(h.owner, "shared-board", {"allow_shared": True})
    assert status == 201
    with store(h).transaction() as conn:
        conn.execute("UPDATE boards SET wing='project_shared_board' WHERE slug='shared-board'")
    return body


def test_r1_convert_idempotent(h):
    status, body = own(h)
    assert status == 200  # RED
    assert body["wing"] == "project_shared_board" and body["changed"] is True
    assert h.board("shared-board")["wing"] == body["wing"]
    assert own(h)[1]["changed"] is False


def test_r2_member(h):
    assert own(h, h.member)[0] == 403  # RED


def test_r3_mcp(h):
    result = h.mcp(token(h), "board_own_wing", board="shared-board")[1]
    assert result["ok"]  # RED
    assert json.loads(result["text"])["wing"] == "project_shared_board"


def test_r4_consent(h):
    status, body = h.connect(h.owner, "shared-board")
    assert body.get("fix", {}).get("path") == "/api/projects/shared-board/wing"  # RED
    assert status == 409 and body["explanation"]
    assert body["alternative"]["body"] == {"allow_shared": True}
    assert (
        body["message"]
        == "This project uses the shared memory wing. Connecting gives the agent read and write access to all shared memory."
    )


def test_r5_revoke(h):
    _, old = h.connect(h.owner, "shared-board", {"allow_shared": True})
    cid = old["connection"]["id"]
    status, body = own(h)
    assert status == 200  # RED
    assert body["coordinator"] is None and cid in body["revoked"]
    assert h.db.connection(cid, h.workspace)["revoked_at"] is not None
    assert next(s for s in h.seats() if s["board"] == "shared-board")["connection_id"] is None
    assert h.mcp(old["token"], "space_read", board="shared-board")[0] == 401
    code, new = h.connect(h.owner, "shared-board")
    assert code == 201 and new["wing"] == "project_shared_board" and new["connection"]["id"] != cid


def test_r6_keep_coordinator(h):
    caller = token(h)
    assert h.mcp(caller, "board_set", board="shared-board", coordinator="conn:broad")[1]["ok"]
    status, body = own(h)
    assert status == 200  # RED
    assert body["coordinator"] == "conn:broad" and body["unbound"] == []


def test_r7_default(h):
    n = card(h)
    assert own(h)[0] == 200  # RED
    mid = save(h, n)
    assert h.get(h.owner, f"/api/memory/{mid}")[1]["theme"] == "project_shared_board"


def test_r9_collision(h):
    h.create_board(h.owner, "a-b", wing="shared")
    h.create_board(h.owner, "a_b", wing="shared")
    assert own(h, slug="a-b")[0] == 200  # RED
    status, body = own(h, slug="a_b")
    assert status == 409 and body.get("error") == "wing_taken"
    assert h.board("a_b")["wing"] == "shared"


def test_r10_view(h):
    assert h.project(h.owner, "shared-board")[1].get("memory") == {
        "wing": "shared",
        "shared": True,
        "own_wing": "project_shared_board",
    }  # RED


def test_r11_view_js():
    path = Path(__file__).resolve().parents[1] / "commonplace/web/static/projects.js"
    script = r"""
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const ui = await import('data:text/javascript;base64,' + Buffer.from(readFileSync(process.argv[1])).toString('base64'));
const e = v => String(v ?? '').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
assert.equal(typeof ui.sharedWingHTML, 'function');
const html = ui.sharedWingHTML({explanation:'<b>unsafe</b>', fix:{effect:ui.PROJECT_COPY.ownWingEffect}}, e);
assert.ok(html.indexOf(ui.PROJECT_COPY.ownWing) < html.indexOf(ui.PROJECT_COPY.allowShared));
assert.ok(html.includes(ui.PROJECT_COPY.ownWingEffect));
assert.ok(!html.includes('<b>') && html.includes('&lt;b&gt;'));
assert.ok(ui.memoryHTML({memory:{wing:'<b>',shared:false},can_configure:true},e).includes('&lt;b&gt;'));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr  # RED


@pytest.mark.parametrize("body", [{"wing": "x"}, [], None, "x"])
def test_r12_body(h, body):
    # None exercises the invalid-object fallback, not a literal JSON null.
    actual = {"wing": "x"} if body is None else body
    assert own(h, body=actual)[0] == 400  # RED
    assert h.board("shared-board")["wing"] == "shared"


@pytest.mark.parametrize("gain", ["read", "write"])
def test_r13_widening(h, gain):
    token(
        h,
        "gainer",
        scopes=["memory:" + gain],
        read_wings=["project_shared_board"] if gain == "read" else ["shared"],
        write_wings=["project_*"] if gain == "write" else [],
    )
    status, body = own(h)
    assert status == 409 and body.get("error") == "wing_in_use" and body.get("count") == 1  # RED
    assert h.board("shared-board")["wing"] == "shared"


def test_r14_connect_heals(h):
    old = crash(h)
    status, new = h.connect(h.owner, "shared-board")
    assert status == 201  # RED
    assert new["connection"]["id"] != old["connection"]["id"]
    assert h.db.connection(old["connection"]["id"], h.workspace)["revoked_at"]


def test_r15_noop_heals(h):
    old = crash(h)
    status, body = own(h)
    assert status == 200 and body.get("changed") is False  # RED
    assert body["coordinator"] is None
    assert old["connection"]["id"] in body["revoked"]
    assert next(s for s in h.seats() if s["board"] == "shared-board")["connection_id"] is None
    assert any(r["action"] == "project.wing.own" for r in h.db.audit_entries(h.workspace))


def test_r16_scoped_search(h, monkeypatch):
    import commonplace.session as session

    monkeypatch.setattr(session, "MEMORY_TIMEOUT", 30)
    old = save(h, card(h), theme="shared")
    assert own(h)[0] == 200  # RED
    new = save(h, theme="project_shared_board")
    result = h.mcp(token(h), "session_start", board="shared-board", query="zephyr-marker")[1]
    assert result["ok"] and new in result["text"] and old not in result["text"]


def test_r17_mcp_full_flow(h):
    _, old = h.connect(h.owner, "shared-board", {"allow_shared": True})
    result = h.mcp(token(h), "board_own_wing", board="shared-board")[1]
    assert result["ok"]  # RED
    body = json.loads(result["text"])
    assert old["connection"]["id"] in body["revoked"]
    assert h.db.connection(old["connection"]["id"], h.workspace)["revoked_at"]
    assert any(r["action"] == "project.wing.own" for r in h.db.audit_entries(h.workspace))


def test_r18_runner_seat(h):
    _, old = h.connect(h.owner, "shared-board", {"allow_shared": True})
    cid = old["connection"]["id"]
    with h.db._lock:
        control_db.link_runner(h.db._conn, cid, "synthetic-machine", "worker")
    status, body = own(h)
    assert status == 200  # RED
    assert cid in body["runner_linked"] and h.db.connection(cid, h.workspace)["revoked_at"] is None
    assert next(s for s in h.seats() if s["board"] == "shared-board")["connection_id"] is None


def test_r19_affected(h):
    token(h, "shared-worker", read_wings=["shared"], write_wings=["shared"])
    cid = h.db.connection("shared-worker", h.workspace)["id"]
    with h.db._lock:
        control_db.link_runner(h.db._conn, cid, "synthetic-machine", "worker")
    status, body = own(h)
    assert status == 200  # RED
    assert {"name": "shared-worker", "lost": "read+write", "runner": True} in body["affected"]
    assert h.db.connection(cid, h.workspace)["revoked_at"] is None


def test_r20_wildcard(h):
    h.create_board(h.owner, "wild", wing="project_*")
    status, body = own(h, slug="wild")
    assert status == 409 and body.get("error") == "not_shared"  # RED


def test_r21_archived_collision(h):
    h.create_board(h.owner, "taken", wing="project_shared_board")
    with store(h).transaction() as conn:
        conn.execute("UPDATE boards SET archived=1 WHERE slug='taken'")
    status, body = own(h)
    assert status == 409 and body.get("error") == "wing_taken"  # RED


def test_r22_nonconverted_connect_heals(h):
    h.create_board(h.owner, "alpha", wing="alpha")
    _, old = h.connect(h.owner, "alpha")
    with store(h).transaction() as conn:
        conn.execute("UPDATE boards SET wing='beta' WHERE slug='alpha'")
    status, new = h.connect(h.owner, "alpha")
    assert status == 201  # RED
    assert new["connection"]["id"] != old["connection"]["id"] and new["wing"] == "beta"


def test_g1_allow_shared(h):
    status, body = h.connect(h.owner, "shared-board", {"allow_shared": True})
    assert status == 201 and body["wing"] == "shared"


def test_g2_authority(h):
    assert h.connect(h.member, "shared-board")[0] == 403
    assert own(h, token(h))[0] in (403, 404)
    assert own(h, slug="absent")[0] == 404
    with store(h).transaction() as conn:
        conn.execute("UPDATE boards SET archived=1 WHERE slug='shared-board'")
    assert own(h)[0] == 404
    with store(h).connection() as conn:
        assert (
            conn.execute("SELECT wing FROM boards WHERE slug='shared-board'").fetchone()[0]
            == "shared"
        )


def test_g3_verbatim_links(h):
    n = card(h)
    caller = token(h, "shared-only", read_wings=["shared"], write_wings=["shared"])
    mid = save(h, n, auth=caller)
    own(h)
    memory = h.get(caller, f"/api/memory/{mid}")[1]
    assert memory["theme"] == "shared" and memory["text"] == "zephyr-marker exact words"
    with store(h).connection() as conn:
        assert (
            conn.execute("SELECT theme FROM task_links WHERE drawer_id=?", (mid,)).fetchone()[0]
            == "shared"
        )


def test_g4_custom_noop(h):
    h.create_board(h.owner, "alpha", wing="alpha")
    own(h, slug="alpha")
    assert h.board("alpha")["wing"] == "alpha"


def test_g5_public(h):
    installed = set(h.gateway.routes) | {(m, p) for m, p, _ in h.gateway.prefix_routes}
    assert {(m, p) for m, p in installed if _public_route(m, p)} == PIN_PUBLIC


def test_g6_broad_not_gain(h):
    token(h)
    assert own(h)[1].get("error") != "wing_in_use"


def test_g7_unscoped(h):
    mid = save(h, card(h), theme="shared")
    own(h)
    assert mid in json.dumps(h.get(h.owner, "/api/memory/search?q=zephyr-marker")[1])


@pytest.mark.parametrize("wing", ["alpha", "project_*"])
def test_g8_custom_default(h, wing):
    h.create_board(h.owner, "alpha", wing=wing)
    mid = save(h, card(h, "alpha"))
    assert h.get(h.owner, f"/api/memory/{mid}")[1]["theme"] == "shared"


def test_g9_member_mcp(h):
    caller = token(h, user=h.member_user)
    result = h.mcp(caller, "board_own_wing", board="shared-board")[1]
    assert not result["ok"] and "Unknown tool" in result["text"]
    assert h.board("shared-board")["wing"] == "shared"


def test_g10_literal_seat_match(h):
    h.create_board(h.owner, "a-b", wing="shared")
    h.create_board(h.owner, "a_b", wing="shared")
    _, old = h.connect(h.owner, "a-b", {"allow_shared": True})
    own(h, slug="a_b")
    cid = old["connection"]["id"]
    assert h.db.connection(cid, h.workspace)["revoked_at"] is None
    assert next(s for s in h.seats() if s["board"] == "a-b")["connection_id"] == cid
