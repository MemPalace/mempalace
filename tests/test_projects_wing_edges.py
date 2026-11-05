"""Project memory boundary, crash recovery and presentation edge cases."""

import http.client
import subprocess
from pathlib import Path

import pytest

from commonplace import control_db
from commonplace.projects.copy import OWN_WING_EFFECT, SHARED_EXPLAIN
from commonplace.projects.wing import own_wing
from tests._projects_helpers import projects_hub_fixture  # noqa: F401


@pytest.fixture
def h(projects_hub):
    h = projects_hub
    assert h.create_board(h.owner, "shared-board", wing="shared")[0] == 201
    return h


def own(h):
    return h.request(h.owner, "POST", "/api/projects/shared-board/wing", {})[:2]


@pytest.mark.parametrize("wing", ["project_*", "*", "sha*"])
def test_pattern_project_view_keeps_connect_rejection(h, wing):
    assert h.create_board(h.owner, "wild", wing=wing)[0] == 201
    status, body = h.project(h.owner, "wild")
    assert status == 200
    assert body["memory"] == {"wing": wing, "shared": False, "own_wing": "project_wild"}
    assert h.connect(h.owner, "wild")[0] == 400


def test_bracket_shared_pattern_still_requires_connect_consent(h):
    assert h.create_board(h.owner, "bracket", wing="[s]hared")[0] == 201
    status, body = h.connect(h.owner, "bracket")
    assert status == 409 and body["error"] == "shared_wing"


def test_revoke_then_crash_repaired_without_reauditing(h, monkeypatch):
    _, connected = h.connect(h.owner, "shared-board", {"allow_shared": True})
    cid = connected["connection"]["id"]
    principal = h.gateway.authenticate("Bearer " + h.hub.owner_token("repair-owner"))
    revoke = h.db.revoke

    def interrupted(*args, **kwargs):
        revoke(*args, **kwargs)
        raise RuntimeError("synthetic crash after revoke")

    monkeypatch.setattr(h.db, "revoke", interrupted)
    with pytest.raises(RuntimeError, match="synthetic crash"):
        own_wing(h.gateway, principal, "shared-board")
    assert h.db.connection(cid, h.workspace)["revoked_at"] is not None
    assert next(s for s in h.seats() if s["board"] == "shared-board")["connection_id"] == cid
    monkeypatch.setattr(h.db, "revoke", revoke)
    before = sum(r["action"] == "project.wing.own" for r in h.db.audit_entries(h.workspace))
    status, body = own(h)
    assert status == 200 and body["changed"] is False and body["revoked"] == []
    assert next(s for s in h.seats() if s["board"] == "shared-board")["connection_id"] is None
    after = sum(r["action"] == "project.wing.own" for r in h.db.audit_entries(h.workspace))
    assert after == before + 1
    assert own(h)[0] == 200
    assert sum(r["action"] == "project.wing.own" for r in h.db.audit_entries(h.workspace)) == after


def test_runner_repeat_reports_without_reauditing(h):
    _, connected = h.connect(h.owner, "shared-board", {"allow_shared": True})
    cid = connected["connection"]["id"]
    with h.db._lock:
        control_db.link_runner(h.db._conn, cid, "synthetic-machine", "worker")
    assert own(h)[1]["runner_linked"] == [cid]
    before = sum(r["action"] == "project.wing.own" for r in h.db.audit_entries(h.workspace))
    status, body = own(h)
    assert status == 200 and body["runner_linked"] == [cid] and body["changed"] is False
    assert sum(r["action"] == "project.wing.own" for r in h.db.audit_entries(h.workspace)) == before


def test_literal_json_null_refused(h):
    conn = http.client.HTTPConnection("127.0.0.1", h.hub.port, timeout=30)
    try:
        conn.request(
            "POST",
            "/api/projects/shared-board/wing",
            b"null",
            {**h.owner, "Content-Type": "application/json"},
        )
        response = conn.getresponse()
        assert response.status == 400
        response.read()
    finally:
        conn.close()
    assert h.board("shared-board")["wing"] == "shared"


@pytest.mark.parametrize("lost", ["read", "write", "read+write"])
def test_affected_nonrunner_permissions(h, lost):
    h.hub.token(
        "shared-helper",
        profile="full",
        scopes=["memory:" + p for p in lost.split("+")],
        read_wings=["shared"] if "read" in lost else [],
        write_wings=["shared"] if "write" in lost else [],
        trust="trusted",
    )
    status, body = own(h)
    assert status == 200
    assert {"name": "shared-helper", "lost": lost, "runner": False} in body["affected"]
    assert h.db.connection("shared-helper", h.workspace)["revoked_at"] is None


@pytest.mark.parametrize("missing", ["read", "write"])
def test_owner_pat_needs_both_target_grants(h, missing):
    caller = h.hub.token(
        "limited-owner",
        profile="full",
        scopes=["memory:read", "memory:write"],
        read_wings=["shared"] if missing == "read" else ["shared", "project_*"],
        write_wings=["shared"] if missing == "write" else ["shared", "project_*"],
        trust="trusted",
    )
    result = h.mcp(caller, "board_own_wing", board="shared-board")[1]
    assert not result["ok"] and "cannot change this board" in result["text"]
    assert h.board("shared-board")["wing"] == "shared"


def test_reconcile_preserves_seat_timestamp(h):
    _, connected = h.connect(h.owner, "shared-board", {"allow_shared": True})
    with h.db._lock:
        h.db._conn.execute(
            "UPDATE project_orchestrators SET updated_at='2000-01-01T00:00:00Z' WHERE connection_id=?",
            (connected["connection"]["id"],),
        )
    assert own(h)[0] == 200
    seat = next(s for s in h.seats() if s["board"] == "shared-board")
    assert seat["connection_id"] is None and seat["updated_at"] == "2000-01-01T00:00:00Z"


def test_memory_ui_choices_messages_and_copy():
    path = Path(__file__).resolve().parents[1] / "commonplace/web/static/projects.js"
    script = r"""
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const ui = await import('data:text/javascript;base64,' + Buffer.from(readFileSync(process.argv[1])).toString('base64'));
const e = v => String(v ?? '').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
const connect = ui.sharedWingHTML({}, e);
const settings = ui.sharedWingHTML({}, e, {allowShared:false});
assert.ok(connect.includes('data-wing-choice="shared"'));
assert.ok(!settings.includes('data-wing-choice="shared"'));
assert.ok(settings.includes('data-wing-choice="own"') && settings.includes('data-wing-choice="cancel"'));
assert.equal(ui.PROJECT_COPY.ownWingEffect, process.argv[2]);
assert.equal(ui.PROJECT_COPY.sharedExplain, process.argv[3]);
assert.equal(ui.ownWingError({error:'wing_changed'}), ui.PROJECT_COPY.wingChanged);
assert.equal(ui.ownWingError({error:'not_shared'}), ui.PROJECT_COPY.ownWingError);
assert.ok(ui.ownWingError({error:'wing_in_use',connections:['narrow-me']}).includes('narrow-me'));
const status = ui.ownWingStatus({changed:true, runner_linked:['runner-id'], affected:[{name:'worker', lost:'read',runner:true}]});
assert.ok(status.includes(ui.PROJECT_COPY.ownWingDone) && status.includes('runner-id') && status.includes('worker (read; runner-managed)'));
assert.equal(ui.ownWingStatus({changed:false,runner_linked:[],affected:[]}), '');
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(path), OWN_WING_EFFECT, SHARED_EXPLAIN],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_connect_seat_cannot_convert_its_board(h):
    _, connected = h.connect(h.owner, "shared-board", {"allow_shared": True})
    result = h.mcp(connected["token"], "board_own_wing", board="shared-board")[1]
    assert not result["ok"] and "cannot change this board" in result["text"]
    assert h.board("shared-board")["wing"] == "shared"
    assert h.db.connection(connected["connection"]["id"], h.workspace)["revoked_at"] is None
