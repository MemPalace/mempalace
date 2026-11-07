"""Paired route regressions; fresh.connect is synthetic non-admin setup.

Only base-existing APIs are used here. New report fields are positive calls
wrapped by allowed(), so an old server refuses them with AssertionError.
The parent owns all execution and paired evidence.
"""

import re
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from commonplace import control_db, events, sessions
from commonplace.boards import display, service as board_service, store
from commonplace.extensions import ToolError
from commonplace.policy import Policy
from tests._commonplace_helpers import (
    REVIEW_CONTEXT,
    REVIEW_OPTIONS,
    REVIEW_QUESTION,
    hub_fixture,  # noqa: F401
)
from tests.test_commonplace_board_authority import fresh as fresh
from tests.test_commonplace_boards import api, mcp
from tests.test_commonplace_web import request

SHA1, SHA2 = "a" * 40, "b" * 40
PR = "https://example.test/o/r/pull/7"
BUCKETS = ("asks", "done_review", "backlog_from_agents", "ready_to_merge", "stale")


def allowed(fn):
    try:
        return fn()
    except ToolError as exc:
        raise AssertionError(f"refused: {exc}") from exc


def clock(monkeypatch, iso):
    frozen = lambda at=None: iso  # noqa: E731
    monkeypatch.setattr(board_service, "stale_before", frozen)
    monkeypatch.setattr(display, "stale_before", frozen)


def shifted(iso, minutes):
    return (datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ") + timedelta(minutes=minutes)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def owner_items(hub, fresh):
    code, result = api(hub, fresh.cookie, "GET", "/api/needs-me")
    assert code == 200, result
    return result


def absent(result, number):
    assert all(number not in {c["number"] for c in result[k]} for k in BUCKETS)


def listed(result, bucket, number):
    matches = [c for c in result[bucket] if c["number"] == number]
    assert len(matches) == 1, (bucket, result)
    return matches[0]


def shown(hub, fresh, number):
    code, result = api(hub, fresh.cookie, "GET", f"/api/cards/{number}")
    assert code == 200, result
    return result


def mine(hub, agent):
    ok, text = mcp(hub, agent.token, "task_list", mine=True)
    assert ok, text
    return {int(n) for n in re.findall(r"^#(\d+)\b", text, re.MULTILINE)}


def holding(fresh, agent):
    card = allowed(lambda: agent.work.task_add(board="work", title="Work to finish"))
    if card.get("proposal_status") == "pending":
        allowed(lambda: fresh.owner.task_approve(card["number"]))
    return allowed(lambda: agent.work.task_take_next(task=card["number"]))


def report(agent, number, **overrides):
    fields = dict(pr=PR, pr_state="open", pr_head=SHA1, checks="passing")
    fields.update(overrides)
    return allowed(lambda: agent.work.task_update(number, **fields))


def ask_owner(agent, number):
    return allowed(
        lambda: agent.work.task_handoff(
            number,
            to="owner",
            note=REVIEW_QUESTION,
            context=REVIEW_CONTEXT,
            options=REVIEW_OPTIONS,
        )
    )


def owner_reason(agent, card):
    name = agent.principal.actor[5:]
    at = card.get("claim_until") or card.get("heartbeat_at") or card["updated_at"]
    return (
        f"{name} has a claim that ran out at {at}, and it has no available parent, "
        "top-level agent or board coordinator. Put it back in the queue with a note, "
        "give it to someone else, or park it."
    )


def supervisor_reason(agent, card, role):
    name = agent.principal.actor[5:]
    return (
        f"{name} has a claim that ran out at {card['claim_until']}. You are its {role}: "
        f"resume {name}, or ask the owner."
    )


def revoke(hub, fresh, agent):
    code, result = api(hub, fresh.cookie, "DELETE", f"/api/connections/{agent.id}")
    assert code == 200, result


def coordinator(hub, fresh, agent):
    code, result = api(
        hub,
        fresh.cookie,
        "PATCH",
        "/api/boards/work",
        {"coordinator": agent.principal.actor if agent else None},
    )
    assert code == 200, result


def expiring(hub, fresh, name, *, parent=None, days=1):
    """Synthetic non-admin ControlDB credential setup, not signup proof."""
    assert not hub.db.user(fresh.user)["is_admin"]
    cid, token = hub.db.create_token(
        workspace_ref=fresh.workspace,
        user_ref=fresh.user,
        actor=fresh.user,
        name=name,
        client="generic",
        parent=parent,
        expires_days=days,
        policy=Policy.build(
            profile="standard",
            scopes=["memory:read", "memory:write"],
            read_wings=["shared"],
            write_wing="shared",
            trust="trusted",
        ),
    )
    principal = hub.db.authenticate_token(token)
    from tests.test_commonplace_boards import service

    return SimpleNamespace(
        id=cid,
        token=token,
        principal=principal,
        work=service(hub, principal),
        expires_at=hub.db.connection(cid, fresh.workspace)["expires_at"],
    )


def foreign(hub):
    """Synthetic X owns its own workspace and creates its own token directly."""
    user = hub.db.create_user("foreign-owner@example.test")
    workspace = hub.db.create_workspace("foreign-work", user)
    assert not hub.db.user(user)["is_admin"]
    cid, token = hub.db.create_token(
        workspace_ref=workspace,
        user_ref=user,
        actor=user,
        name="foreign",
        client="generic",
        policy=Policy.build(
            profile="standard",
            scopes=["memory:read", "memory:write"],
            read_wings=["shared"],
            write_wing="shared",
            trust="trusted",
        ),
    )
    return SimpleNamespace(id=cid, token=token)


def next_second():
    """One bounded wait for the server's second-resolution heartbeat clock."""
    previous = store.now()
    fraction = datetime.now(timezone.utc).microsecond / 1_000_000
    time.sleep(1.02 - fraction)
    assert store.now() > previous


def test_legacy_pr_report_is_never_ready_to_merge(hub, fresh):
    agent = fresh.connect("worker")
    card = holding(fresh, agent)
    agent.work.task_update(card["number"], pr=PR, checks="passing")
    absent(owner_items(hub, fresh), card["number"])
    assert shown(hub, fresh, card["number"])["need"] != "merge"


def test_merge_ask_needs_fresh_open_head_report(hub, fresh, monkeypatch):
    agent = fresh.connect("worker")
    number = holding(fresh, agent)["number"]
    reported = report(agent, number)
    ask_owner(agent, number)
    result = owner_items(hub, fresh)
    item = listed(result, "asks", number)
    assert listed(result, "ready_to_merge", number) == item
    assert item.get("why", "").startswith(
        "worker asks you to merge PR #7 (checks passed on aaaaaaa): "
    )
    assert item.get("actions") == ["answer", "open_pr"]
    agent.work.task_update(number, checks="failing")
    assert not owner_items(hub, fresh)["ready_to_merge"]
    assert (
        listed(owner_items(hub, fresh), "asks", number).get("why")
        == f"worker asks you: {REVIEW_QUESTION}"
    )
    agent.work.task_update(number, checks="passing")
    listed(owner_items(hub, fresh), "ready_to_merge", number)
    clock(monkeypatch, reported.get("pr_reported_at"))
    result = owner_items(hub, fresh)
    assert not result["ready_to_merge"]
    assert listed(result, "asks", number).get("actions") == ["answer"]
    assert shown(hub, fresh, number)["need"] != "merge"
    before = shown(hub, fresh, number)
    for fields in ({"pr_state": "merged"}, {"pr_head": SHA2}):
        with pytest.raises(ToolError, match="waiting on a person"):
            agent.work.task_update(number, **fields)
        assert shown(hub, fresh, number) == before


def test_pr_head_or_pr_change_clears_merge_readiness(hub, fresh):
    agent = fresh.connect("worker")
    number = holding(fresh, agent)["number"]
    report(agent, number)
    assert shown(hub, fresh, number)["need"] == "merge"
    changed = allowed(lambda: agent.work.task_update(number, pr_head=SHA2))
    assert changed["checks"] == "" and changed["need"] != "merge"
    report(agent, number)
    assert allowed(lambda: agent.work.task_update(number, pr_state="merged"))["need"] != "merge"
    changed = agent.work.task_update(number, pr="https://example.test/o/r/pull/8")
    assert all(changed.get(k) is None for k in ("pr_state", "pr_head", "pr_reported_at"))
    assert changed["checks"] == ""
    report(agent, number)
    changed = allowed(lambda: agent.work.task_update(number, pr_head=SHA2, checks="passing"))
    assert changed.get("pr_reported_at") is None and changed["need"] != "merge"
    assert allowed(lambda: agent.work.task_update(number, pr_state="open"))["need"] == "merge"
    second = holding(fresh, agent)["number"]
    report(agent, second)
    allowed(lambda: agent.work.task_update(second, pr_head=SHA2, checks="passing"))
    ask_owner(agent, second)
    result = owner_items(hub, fresh)
    item = listed(result, "asks", second)
    assert not result["ready_to_merge"]
    assert item.get("why") == f"worker asks you: {REVIEW_QUESTION}"
    assert item.get("pr_reported_at") is None
    assert shown(hub, fresh, second)["need"] != "merge"


def test_live_or_recently_expired_lease_is_not_stuck(hub, fresh, monkeypatch):
    agent = fresh.connect("worker")
    card = holding(fresh, agent)  # Default 30-minute lease; never shorten it.
    absent(owner_items(hub, fresh), card["number"])
    clock(monkeypatch, shifted(card["claim_until"], -15))
    absent(owner_items(hub, fresh), card["number"])


def test_ordinary_and_external_wait_backlog_is_not_an_ask(hub, fresh):
    agent = fresh.connect("worker")
    for title, status_line in (("Later", ""), ("External wait", "waiting on an upstream release")):
        card = agent.work.task_add(board="work", title=title, status="backlog")
        agent.work.task_update(card["number"], status_line=status_line)
        absent(owner_items(hub, fresh), card["number"])


@pytest.mark.parametrize("action", ["requeue", "reassign", "park"])
def test_unsupervised_stuck_card_reason_and_each_action_resolves(hub, fresh, monkeypatch, action):
    agent, other, unrelated = (fresh.connect(n) for n in ("worker", "receiver", "unrelated"))
    card = holding(fresh, agent)
    number = card["number"]
    clock(monkeypatch, shifted(card["claim_until"], 1))
    item = listed(owner_items(hub, fresh), "stale", number)
    assert item.get("why") == owner_reason(agent, card)
    assert item.get("actions") == ["requeue", "reassign", "park"]
    absent(unrelated.work.needs_me(), number)
    assert number not in mine(hub, unrelated)
    if action == "park":
        code, changed = api(
            hub,
            fresh.cookie,
            "PATCH",
            f"/api/cards/{number}",
            {"version": item["version"], "status": "backlog"},
        )
        assert code == 200 and changed["status"] == "backlog"
    else:
        code, changed = api(
            hub,
            fresh.cookie,
            "POST",
            f"/api/cards/{number}/handoff",
            {
                "version": item["version"],
                "to": "anyone" if action == "requeue" else other.principal.actor,
                "note": "Retry after the fix",
            },
        )
        assert code == 200 and changed["status"] == "todo"
        if action == "requeue":
            assert changed["assignee"] is None
            assert shown(hub, fresh, number)["events"][-1]["text"] == "Retry after the fix"
        claimed = allowed(
            lambda: other.work.task_take_next(
                **({"board": "work"} if action == "requeue" else {"task": number})
            )
        )
        assert claimed["number"] == number
    if action != "park":
        # The previous cutoff is beyond even a newly issued real-time lease.
        clock(monkeypatch, shifted(claimed["claim_until"], -1))
    absent(owner_items(hub, fresh), number)


def test_only_unreleasable_proposals_reach_the_owner(hub, fresh):
    parent = fresh.connect("parent")
    coordinator(hub, fresh, parent)
    parent_card = parent.work.task_add(board="work", title="Parent task")
    child = fresh.connect("child", parent=parent.id)
    proposal = child.work.task_add(
        board="work", title="Child proposal", parent=parent_card["number"]
    )
    number = proposal["number"]
    absent(owner_items(hub, fresh), number)
    assert shown(hub, fresh, number)["need"] == "plan"
    container = shown(hub, fresh, parent_card["number"])
    assert container["child_cards"][0]["need"] == "plan"
    assert not container["children_need_you"]
    absent(owner_items(hub, fresh), parent_card["number"])
    revoke(hub, fresh, parent)
    item = listed(owner_items(hub, fresh), "backlog_from_agents", number)
    assert (
        item.get("why")
        == "child proposed this, and no agent can approve it any more. Approve or reject it."
    )
    assert item.get("actions") == ["approve", "reject"]
    assert shown(hub, fresh, number)["need"] == "you"
    code, _ = api(
        hub,
        fresh.cookie,
        "POST",
        f"/api/cards/{number}/approve",
        {"version": item["version"], "approve": True},
    )
    assert code == 200
    absent(owner_items(hub, fresh), number)


def test_release_authority_uses_live_write_read_predicates(hub, fresh):
    parent = fresh.connect("parent", scopes=["memory:read"])
    child = fresh.connect("child", parent=parent.id)
    proposal = child.work.task_add(board="work", title="Child proposal")
    number = proposal["number"]
    item = listed(owner_items(hub, fresh), "backlog_from_agents", number)
    assert (
        item.get("why")
        == "child proposed this, and no agent can approve it any more. Approve or reject it."
    )
    assert shown(hub, fresh, number)["need"] == "you"
    keeper = fresh.connect("coordinator")
    coordinator(hub, fresh, keeper)
    absent(owner_items(hub, fresh), number)
    assert shown(hub, fresh, number)["need"] == "plan"
    coordinator(hub, fresh, None)
    listed(owner_items(hub, fresh), "backlog_from_agents", number)
    root = fresh.connect("root")
    middle = fresh.connect("middle", parent=root.id, scopes=["memory:read"])
    grandchild = fresh.connect("grandchild", parent=middle.id)
    coordinator(hub, fresh, root)
    second = grandchild.work.task_add(board="work", title="Grandchild proposal")
    absent(owner_items(hub, fresh), second["number"])


def member_cookie(hub, fresh):
    user = hub.db.create_user("member@example.test")
    hub.db.add_member(fresh.workspace, user, "member", actor=fresh.user)
    link = sessions.create_login_link(hub.db, "member@example.test", fresh.workspace)
    token = sessions.redeem_login_link(hub.db, link)
    return {"Cookie": f"{sessions.COOKIE}={token}"}


def test_done_review_accepts_to_done_owner_only(hub, fresh):
    agent = fresh.connect("worker")
    # Existing explicit owner import represents a legacy completion review.
    card = fresh.owner.seed_card(board="work", title="Finished work", body="Done?", status="review")
    number = card["number"]
    item = listed(owner_items(hub, fresh), "done_review", number)
    assert item.get("actions") == ["accept", "answer"]
    before = shown(hub, fresh, number)
    body = {"version": item["version"], "reply": "Looks right", "then": "done"}
    for auth in (agent.token, member_cookie(hub, fresh)):
        assert api(hub, auth, "POST", f"/api/cards/{number}/decide", body)[0] == 403
        assert shown(hub, fresh, number) == before
    code, result = api(hub, fresh.cookie, "POST", f"/api/cards/{number}/decide", body)
    assert code == 200 and result["status"] == "done"
    absent(owner_items(hub, fresh), number)


def test_mcp_needs_me_lines_carry_reason(hub, fresh, monkeypatch):
    parent = fresh.connect("parent")
    agent = fresh.connect("worker", parent=parent.id)
    card = holding(fresh, agent)
    clock(monkeypatch, card["claim_until"])
    ok, text = mcp(hub, parent.token, "needs_me")
    assert ok, text
    assert "Stuck:" in text
    assert (
        f'#{card["number"]} "{card["title"]}" — {supervisor_reason(agent, card, "parent")}' in text
    )
    ok, text = mcp(hub, agent.token, "needs_me")
    assert ok and text == "nothing waiting"


@pytest.mark.parametrize("trust", ["trusted", "standard"])
def test_expired_claim_goes_to_live_parent_not_owner(hub, fresh, monkeypatch, trust):
    parent = fresh.connect("parent", trust=trust)
    holder = fresh.connect("holder", parent=parent.id)
    card = holding(fresh, holder)
    number = card["number"]
    clock(monkeypatch, card["claim_until"])
    absent(owner_items(hub, fresh), number)
    assert number in mine(hub, parent) and number in mine(hub, holder)
    item = listed(parent.work.needs_me(), "stale", number)
    assert item.get("why") == supervisor_reason(holder, card, "parent")
    assert item.get("actions") == []
    ok, text = mcp(
        hub,
        parent.token,
        "ask",
        question=REVIEW_QUESTION,
        options=REVIEW_OPTIONS,
        board="work",
        context=REVIEW_CONTEXT,
    )
    assert ok, text
    assert owner_items(hub, fresh)["asks"]
    absent(owner_items(hub, fresh), number)
    code, _ = api(
        hub,
        fresh.cookie,
        "POST",
        f"/api/cards/{number}/handoff",
        {"version": item["version"], "to": "anyone", "note": "Retry after the fix"},
    )
    assert code == 200


def test_root_or_coordinator_supervises_when_no_parent_is_available(hub, fresh, monkeypatch):
    root = fresh.connect("root")
    parent = fresh.connect("parent", parent=root.id)
    holder = fresh.connect("holder", parent=parent.id)
    card = holding(fresh, holder)
    revoke(hub, fresh, parent)
    clock(monkeypatch, card["claim_until"])
    absent(owner_items(hub, fresh), card["number"])
    assert card["number"] in mine(hub, root)
    assert listed(root.work.needs_me(), "stale", card["number"]).get("why") == supervisor_reason(
        holder, card, "top-level agent"
    )
    second_holder = fresh.connect("second-holder")
    second = holding(fresh, second_holder)
    keeper = fresh.connect("coordinator")
    coordinator(hub, fresh, keeper)
    clock(monkeypatch, second["claim_until"])
    absent(owner_items(hub, fresh), second["number"])
    assert second["number"] in mine(hub, keeper)
    assert listed(keeper.work.needs_me(), "stale", second["number"]).get(
        "why"
    ) == supervisor_reason(second_holder, second, "board coordinator")
    coordinator(hub, fresh, None)
    assert listed(owner_items(hub, fresh), "stale", second["number"]).get("why") == owner_reason(
        second_holder, second
    )
    assert second["number"] not in mine(hub, keeper)


@pytest.mark.parametrize("condition", ["revoked", "expired", "unreadable", "read_only"])
def test_unavailable_supervisor_falls_back_to_owner(hub, fresh, monkeypatch, condition):
    if condition == "expired":
        parent = expiring(hub, fresh, "parent")
    else:
        kwargs = (
            {"read_wings": ["elsewhere"]}
            if condition == "unreadable"
            else {"scopes": ["memory:read"]}
            if condition == "read_only"
            else {}
        )
        parent = fresh.connect("parent", **kwargs)
    holder = fresh.connect("holder", parent=parent.id)
    card = holding(fresh, holder)
    clock(monkeypatch, card["claim_until"])
    if condition in ("revoked", "expired"):
        absent(owner_items(hub, fresh), card["number"])
        assert card["number"] in mine(hub, parent)
        if condition == "revoked":
            revoke(hub, fresh, parent)
        else:
            # Credential clock only; _parse returns a UTC-aware datetime.
            expiry = control_db._parse(parent.expires_at) + timedelta(minutes=1)
            monkeypatch.setattr(control_db, "_now", lambda: expiry)
    item = listed(owner_items(hub, fresh), "stale", card["number"])
    assert item.get("why") == owner_reason(holder, card)
    assert item.get("actions") == ["requeue", "reassign", "park"]


def test_unrelated_trusted_or_foreign_agent_is_not_a_supervisor(hub, fresh, monkeypatch):
    holder, unrelated = fresh.connect("holder"), fresh.connect("unrelated")
    outsider = foreign(hub)
    card = holding(fresh, holder)
    clock(monkeypatch, card["claim_until"])
    assert listed(owner_items(hub, fresh), "stale", card["number"]).get("why") == owner_reason(
        holder, card
    )
    assert card["number"] not in mine(hub, unrelated)
    absent(unrelated.work.needs_me(), card["number"])
    assert api(hub, outsider.token, "GET", f"/api/cards/{card['number']}")[0] == 404
    assert card["number"] not in mine(hub, outsider)


def test_long_task_heartbeat_clears_supervisor_item(hub, fresh, monkeypatch):
    parent = fresh.connect("parent")
    holder = fresh.connect("holder", parent=parent.id)
    card = holding(fresh, holder)
    clock(monkeypatch, card["claim_until"])
    absent(owner_items(hub, fresh), card["number"])
    assert card["number"] in mine(hub, parent)
    next_second()
    ok, text = mcp(hub, holder.token, "task_heartbeat", task=card["number"])
    assert ok, text
    assert shown(hub, fresh, card["number"])["claim_until"] > card["claim_until"]
    assert card["number"] not in mine(hub, parent)
    absent(owner_items(hub, fresh), card["number"])
    assert not parent.work.needs_me()["stale"]


def test_guard_person_ask_stays_with_owner(hub, fresh, monkeypatch):
    """G1/G9: normal ask and holder handoff keep existing person decisions."""
    parent = fresh.connect("parent")
    asked = parent.work.ask(
        REVIEW_QUESTION, options=REVIEW_OPTIONS, board="work", context=REVIEW_CONTEXT
    )
    assert listed(owner_items(hub, fresh), "asks", asked["number"])["need"] == "you"
    holder = fresh.connect("holder", parent=parent.id)
    card = holding(fresh, holder)
    ask_owner(holder, card["number"])
    clock(monkeypatch, card["claim_until"])
    listed(owner_items(hub, fresh), "asks", card["number"])
    assert not parent.work.needs_me()["stale"]
    assert not mcp(hub, holder.token, "decide", task=card["number"], reply="Looks right")[0]


def test_guard_foreign_scope_and_member_owner_gate(hub, fresh):
    """G2/G3: synthetic foreign owner never gains another workspace's card."""
    agent = fresh.connect("worker")
    card = holding(fresh, agent)
    outsider = foreign(hub)
    before = shown(hub, fresh, card["number"])
    assert (
        api(
            hub,
            outsider.token,
            "POST",
            f"/api/cards/{card['number']}/handoff",
            {"to": "anyone", "note": "Continue this work"},
        )[0]
        == 404
    )
    assert shown(hub, fresh, card["number"]) == before
    assert api(hub, member_cookie(hub, fresh), "GET", "/api/needs-me")[0] == 403


def test_guard_csrf_logout_and_version(hub, fresh):
    """G4/G5/G6: cookie writes retain origin, JSON, session and version guards."""
    agent = fresh.connect("worker")
    card = holding(fresh, agent)
    number = card["number"]
    body = {"version": card["version"], "to": "anyone", "note": "Continue this work"}
    before = shown(hub, fresh, number)
    for extra in ({"Origin": "https://evil.example"}, {"Content-Type": "text/plain"}):
        assert (
            api(hub, {**fresh.cookie, **extra}, "POST", f"/api/cards/{number}/handoff", body)[0]
            == 403
        )
        assert shown(hub, fresh, number) == before
    assert (
        api(
            hub,
            fresh.cookie,
            "POST",
            f"/api/cards/{number}/handoff",
            {**body, "version": card["version"] - 1},
        )[0]
        == 409
    )
    assert shown(hub, fresh, number) == before
    for extra in ({"Content-Type": "text/plain"}, {"Origin": "https://evil.example"}):
        code, _, _ = request(
            hub,
            "/app/logout",
            method="POST",
            headers={**fresh.cookie, "Content-Type": "application/json", **extra},
        )
        assert code == 403
        assert shown(hub, fresh, number) == before
    code, _headers, _body = request(
        hub,
        "/app/logout",
        method="POST",
        headers={**fresh.cookie, "Content-Type": "application/json"},
    )
    assert code == 204
    assert api(hub, fresh.cookie, "POST", f"/api/cards/{number}/handoff", body)[0] == 401


def test_guard_lists_do_not_add_events(hub, fresh):
    """G7: live real leases mean these reads cannot cause lazy expiries."""
    parent = fresh.connect("parent")
    holder = fresh.connect("holder", parent=parent.id)
    holding(fresh, holder)
    before = events.after(hub.db, fresh.workspace)
    for _ in range(2):
        owner_items(hub, fresh)
        mine(hub, parent)
    assert events.after(hub.db, fresh.workspace) == before


def test_guard_supervisor_cannot_take_over(hub, fresh, monkeypatch):
    """G8: notification never grants holder, release or claim authority."""
    parent = fresh.connect("parent")
    holder = fresh.connect("holder", parent=parent.id)
    card = holding(fresh, holder)
    clock(monkeypatch, card["claim_until"])
    before = shown(hub, fresh, card["number"])
    for fn in (
        lambda: parent.work.task_handoff(card["number"], to="anyone", note="Continue this work"),
        lambda: parent.work.task_update(card["number"], status="todo"),
        lambda: parent.work.task_take_next(task=card["number"]),
    ):
        with pytest.raises(ToolError):
            fn()
        assert shown(hub, fresh, card["number"]) == before


@pytest.mark.parametrize("path", ["take_next", "update", "handoff"])
def test_claim_requires_new_holder_pr_report(hub, fresh, path):
    reporter = fresh.connect("reporter")
    holder = fresh.connect("holder")
    number = fresh.owner.task_add(board="work", title="Unclaimed work", status="todo")["number"]
    if path == "handoff":
        reporter.work.task_take_next(task=number)
    report(reporter, number)
    if path == "handoff":
        reporter.work.task_handoff(number, to=holder.principal.actor, note="Continue work")
    claimed = (
        holder.work.task_update(number, status="doing")
        if path == "update"
        else holder.work.task_take_next(task=number)
    )
    assert claimed["need"] != "merge"
    assert claimed["pr_url"] == PR
    assert all(claimed[k] is None for k in ("pr_state", "pr_head", "pr_reported_at"))
    assert claimed["checks"] == ""
    ask_owner(holder, number)
    assert not owner_items(hub, fresh)["ready_to_merge"]
    fresh.owner.decide(number, reply="Continue work", then="return")
    report(holder, number)
    ask_owner(holder, number)
    listed(owner_items(hub, fresh), "ready_to_merge", number)


@pytest.mark.parametrize("kind", ["ask", "merge", "done"])
def test_ask_wording_matches_caller_role(hub, fresh, kind):
    worker = fresh.connect("worker")
    observer = fresh.connect("observer")
    number = holding(fresh, worker)["number"]
    if kind == "merge":
        report(worker, number)
    if kind == "done":
        number = fresh.owner.seed_card(
            board="work", title="Finished work", body="Done?", status="review"
        )["number"]
    else:
        ask_owner(worker, number)
    bucket = "done_review" if kind == "done" else "asks"
    person = listed(owner_items(hub, fresh), bucket, number)
    agent = listed(observer.work.needs_me(), bucket, number)
    # .get keeps a missing reason an assertion failure, not a KeyError.
    assert "asks you" in person.get("why", "")
    assert "asks the owner" in agent.get("why", "")
    assert "asks you" not in agent.get("why", "")
    assert agent["actions"] == []


def test_status_line_cannot_hide_person_release(hub, fresh):
    parent = fresh.connect("parent")
    agent = fresh.connect("worker", parent=parent.id)
    card = agent.work.task_add(board="work", title="Proposed work")
    revoke(hub, fresh, parent)
    assert card["proposal_status"] == "pending"
    listed(owner_items(hub, fresh), "backlog_from_agents", card["number"])
    agent.work.task_update(card["number"], status_line="waiting on an upstream release")
    listed(owner_items(hub, fresh), "backlog_from_agents", card["number"])


def test_claim_with_same_call_pr_report(hub, fresh):
    holder = fresh.connect("holder")
    number = fresh.owner.task_add(board="work", title="Unclaimed work", status="todo")["number"]
    assert report(holder, number, status="doing")["need"] == "merge"
