"""Capture cadence and idempotency through synthetic public routes."""

import re
from dataclasses import replace

from commonplace import plans
from tests.commonplace_sync_fixtures import (
    appended,
    hub as hub,
    item,
    raw,
    reached,
    records,
    reset_reached as reset_reached,
    sha,
)


def test_fixture_on_base(hub):
    h = hub()
    p = h.signup()
    assert h.db.user(p.user)["is_admin"] == 0
    token, connection = h.pat(p, client="claude-code")
    assert token and connection
    h.enable_capture(p)
    grant, _ = h.pat(p, import_=True)
    assert grant
    page = h.get("/api/plan", p)
    assert page.status == 200
    assert page.json["plan"] == "free"


def test_plan_page_reports_plan_row_only(hub):
    h = hub(plans="off")
    p = h.signup()
    h.restart(plans="on")
    page = h.get("/api/plan", p)
    assert page.status == 200
    assert page.json["plan"] is None
    assert page.json["sync"] == {"interval_seconds": 0}


def test_free_one_window_per_900_per_account(hub):
    h = hub()
    p = h.signup()
    h.enable_capture(p)
    t1, _ = h.pat(p, client="claude-code")
    t2, _ = h.pat(p, client="claude-code")
    t3, _ = h.pat(p, client="claude-code")
    grant, _ = h.pat(p, import_=True)
    F = raw(records("S1"))
    appended(h.push(t1, "claude-code", "S1", F))
    h.at(30)
    assert h.push(t1, "claude-code", "S1", raw(records("S1", 1, "g")), len(F), sha(F)).status == 429
    appended(h.push(t2, "claude-code", "S2", raw(records("S2"))))
    h.at(61)
    late = h.push(t3, "claude-code", "S3", raw(records("S3")))
    assert late.status == 429  # COUNTED:C1
    reached("C1")
    assert int(re.search(r"retry in (\d+) seconds", late.json["error"])[1]) == 840
    h.restart()
    h.at(120)
    assert h.push(t3, "claude-code", "S3", raw(records("S3"))).status == 429
    assert h.imp("claude-code", [item("S4", raw(records("S4")))], token=grant).status == 429
    h.at(900)
    appended(h.push(t3, "claude-code", "S3", raw(records("S3"))))


def test_unknown_account_fails_closed(hub, monkeypatch):
    h = hub(plans="off")
    p = h.signup()
    h.enable_capture(p)
    h.restart(plans="on")
    response = h.settings(p)
    assert response.status == 200
    shown = response.json
    assert set(shown) == {"enabled", "interval_seconds", "private"}
    assert shown["interval_seconds"] == 900  # COUNTED:C2
    reached("C2")
    page = h.get("/api/plan", p)
    assert page.status == 200
    assert page.json["sync"] == {"interval_seconds": 0}
    token, _ = h.pat(p, client="claude-code")
    appended(h.push(token, "claude-code", "S1", raw(records("S1"))))
    h.at(120)
    assert h.push(token, "claude-code", "S2", raw(records("S2"))).status == 429
    tiny = replace(plans.FREE, storage_bytes=1)
    monkeypatch.setattr(plans, "FREE", tiny)
    monkeypatch.setitem(plans.PLANS, "free", tiny)
    h.at(901)
    assert h.push(token, "claude-code", "S3", raw(records("S3"))).status == 507


def test_capture_client_op_id_replay_and_conflict(hub):
    h = hub()
    p = h.signup()
    h.enable_capture(p)
    token, _ = h.pat(p, client="claude-code")
    F = records("S1")
    op = "synthetic_op_00000001"
    first = h.push(token, "claude-code", "S1", raw(F), op=op)
    assert first.status == 200  # COUNTED:C3
    reached("C3")
    assert first.json["status"] == "appended"
    before = h.oracle(p, F)
    h.at(5)
    again = h.push(token, "claude-code", "S1", raw(F), op=op)
    assert again.status == 200
    assert again.raw == first.raw
    assert h.oracle(p, F) == before
    conflict = h.push(token, "claude-code", "S1", raw(records("S1", 1, "other")), op=op)
    assert conflict.status == 409
    assert conflict.json["code"] == "op_conflict"
    assert h.oracle(p, F) == before
    duplicate = h.push(token, "claude-code", "S1", raw(F))
    assert duplicate.status == 200
    assert duplicate.json["status"] == "duplicate"


def test_owner_sync_now_settings_api(hub):
    h = hub()
    p = h.signup()
    h.enable_capture(p)
    token, _ = h.pat(p, client="claude-code")
    now = h.sync_now(p)
    assert now.status == 200  # COUNTED:C4
    reached("C4")
    assert set(now.json) == {"enabled", "interval_seconds", "private"}
    assert h.post_settings(p, {"sync_now": False}).status == 400
    assert h.post_settings(p, {}).status == 400
    assert h.post_settings(token, {"sync_now": True}).status == 403
