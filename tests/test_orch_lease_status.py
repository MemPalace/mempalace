"""The coordinator status block and its "End lease" button: the REST contract they rely on.

``web/static/orchstatus.js`` reads ``GET /api/orch/boards/<b>/roles/main/checkpoint`` and the
owner presses ``POST .../force-release``. These tests pin the fields the block reads, the
refusals (member, agent token, a person of another workspace) and that ending a live lease
leaves unfinished work visible instead of dropping it. Names and tokens are synthetic.
"""

import pytest

from commonplace import sessions
from commonplace.boards.service import BoardService
from tests._commonplace_helpers import hub_fixture  # noqa: F401
from tests.runner_harness import http
from tests.test_orch_surfaces import BOARD, ROLE, Env

BASE = f"boards/{BOARD}/roles/{ROLE}"


@pytest.fixture
def env(hub):
    return Env(hub)


def cookie_for(hub, email, *, workspace):
    link = sessions.create_login_link(hub.db, email, workspace)
    return f"{sessions.COOKIE}={sessions.redeem_login_link(hub.db, link)}"


def lease_of(env):
    status, body = env.person("GET", f"{BASE}/checkpoint")
    assert status == 200, body
    return body["lease"]


def claim(env):
    status, claimed = env.rest("POST", f"{BASE}/resume", {}, token=env.a_token)
    assert status == 200, claimed
    return {"epoch": claimed["epoch"], "token": claimed["token"]}


def test_checkpoint_reports_what_the_status_block_shows(env):
    # No lease yet: the block says nobody is coordinating.
    status, body = env.person("GET", f"{BASE}/checkpoint")
    assert status == 200 and body["lease"] is None
    assert body["config"]["version"] >= 1
    # A claimed lease: holder, expiry and live flag, plus the three counts.
    lease = claim(env)
    card = env.boards.task_add("Synthetic todo card", board=BOARD, status="todo")["number"]
    status, step = env.rest(
        "POST",
        f"{BASE}/journal",
        {**lease, "step_key": "w1", "kind": "card", "card": card},
        token=env.a_token,
    )
    assert status == 200, step
    status, body = env.person("GET", f"{BASE}/checkpoint")
    assert status == 200
    assert body["lease"]["live"] is True
    assert body["lease"]["holder"]
    assert isinstance(body["lease"]["expires_at"], str) and body["lease"]["epoch"] == 1
    assert [s["step_key"] for s in body["open"]] == ["w1"]
    assert body["in_doubt"] == [] and body["needs_person"] == []
    assert len(body["unstarted"]) == 1


def test_owner_ends_the_lease_and_the_status_turns_to_nobody(env):
    claim(env)
    status, body = env.person("POST", f"{BASE}/force-release", {})
    assert (status, body) == (200, {"epoch": 1, "released": True})
    lease = lease_of(env)
    assert lease["live"] is False and lease["epoch"] == 1
    # Ending it twice is harmless and says that nothing was live.
    status, body = env.person("POST", f"{BASE}/force-release", {})
    assert (status, body) == (200, {"epoch": 1, "released": False})


def test_ending_a_live_lease_leaves_unfinished_work_visible(env):
    lease = claim(env)
    status, step = env.rest(
        "POST",
        f"{BASE}/journal",
        {**lease, "step_key": "merge-1", "kind": "merge"},
        token=env.a_token,
    )
    assert status == 200, step
    status, step = env.rest(
        "POST",
        f"{BASE}/journal",
        {**lease, "step_key": "thread-1", "kind": "thread", "detail": "builder running"},
        token=env.a_token,
    )
    assert status == 200, step
    before = env.person("GET", f"{BASE}/checkpoint")[1]
    assert {s["step_key"] for s in before["open"]} == {"merge-1", "thread-1"}

    status, body = env.person("POST", f"{BASE}/force-release", {})
    assert (status, body["released"]) == (200, True)

    # Still there, not voided, not finished: the work is listed as open under a dead lease.
    after = env.person("GET", f"{BASE}/checkpoint")[1]
    assert after["lease"]["live"] is False
    assert {s["step_key"] for s in after["open"]} == {"merge-1", "thread-1"}
    assert all(not s["voided"] for s in after["open"])
    assert after["finished"] == []

    # The next claim turns it into in-doubt work for the new holder to settle; nothing is lost.
    status, claimed = env.rest("POST", f"{BASE}/resume", {}, token=env.b_token)
    assert status == 200 and claimed["epoch"] == 2
    status, now = env.person("GET", f"{BASE}/checkpoint")
    assert {s["step_key"] for s in now["in_doubt"]} == {"merge-1", "thread-1"}
    assert now["open"] == []


@pytest.mark.parametrize("who", ["member", "agent", "seat", "signed_out"])
def test_end_lease_is_refused_for_everyone_but_the_signed_in_owner(env, hub, who):
    claim(env)
    path = f"/api/orch/{BASE}/force-release"
    if who == "member":
        member = hub.db.create_user("member@example.com")
        hub.db.add_member(hub.workspace, member, "member", actor=hub.admin)
        cookie = cookie_for(hub, "member@example.com", workspace=hub.workspace)
        status, body = http(hub.port, "POST", path, cookie=cookie, body={})
        expected = (403, "owner_only")
    elif who == "signed_out":
        status, body = http(hub.port, "POST", path, body={})
        assert status == 401, body
        assert lease_of(env)["live"] is True
        return
    else:
        token = env.a_token if who == "agent" else env.seat_token
        status, body = env.rest("POST", f"{BASE}/force-release", {}, token=token)
        expected = (403, "owner_only")
    assert (status, body.get("code")) == expected, body
    assert lease_of(env)["live"] is True


def test_a_person_of_another_workspace_cannot_end_this_lease(env, hub):
    claim(env)
    outsider = hub.db.create_user("outsider@example.com")
    other = hub.db.create_workspace("Other", outsider)
    cookie = cookie_for(hub, "outsider@example.com", workspace=other)
    # The slug does not exist in their workspace: refused as not found, nothing released.
    status, body = http(hub.port, "POST", f"/api/orch/{BASE}/force-release", cookie=cookie, body={})
    assert status == 404 and body["code"] == "board_not_found", body
    assert lease_of(env)["live"] is True
    # Even with a same-named board there, they only see and touch their own workspace.
    principal = sessions.person_principal(hub.db, outsider, other)
    BoardService(env.ctx(principal)).board_add(slug=BOARD, wing="shared")
    status, body = http(hub.port, "POST", f"/api/orch/{BASE}/force-release", cookie=cookie, body={})
    assert status == 200 and body["released"] is False, body
    status, theirs = http(hub.port, "GET", f"/api/orch/{BASE}/checkpoint", cookie=cookie)
    assert status == 200 and theirs["lease"] is None
    assert lease_of(env)["live"] is True
    assert [e for e in env.events() if e["kind"] == "orch.lease.forced"] == []
