"""Fresh workspace defaults, child approval boundaries and durable claims."""

import re
from pathlib import Path

import pytest

from commonplace import sessions
from commonplace.boards.store import BoardStore
from commonplace.bridge import CLIENT_PRESETS
from commonplace.cli import main
from commonplace.extensions import ToolError
from tests._commonplace_helpers import hub_fixture  # noqa: F401
from tests.test_commonplace_board_actors import actor
from tests.test_commonplace_boards import api, mcp, service

QUESTION = "Should this change ship now or wait for the next release?"
CONTEXT = "The implementation is complete and the affected tests have passed."
OPTIONS = ["Ship this change", "Wait for the next release"]


@pytest.mark.parametrize("client", [*CLIENT_PRESETS, "cursor", "another-agent"])
@pytest.mark.parametrize("member", [False, True])
def test_brand_new_workspace_default_client_owns_work(hub, capsys, client, member):
    user = hub.admin
    if member:
        user = hub.db.create_user("worker@example.com")
        hub.db.add_member(hub.workspace, user, "member", actor=hub.admin)
    assert (
        main(
            [
                "--data",
                str(Path(hub.db.path).parent),
                "token",
                "add",
                "worker",
                "--workspace",
                hub.workspace,
                "--client",
                client,
                "--user",
                user,
                "--by",
                "owner@example.com",
            ]
        )
        == 0
    )
    token = capsys.readouterr().out.strip()
    agent = hub.db.authenticate_token(token)
    worker = service(hub, agent)
    assert worker.board_list() == []
    assert {"task_add", "task_take_next", "task_update", "task_heartbeat"} <= hub.tool_names(token)
    ok, reply = mcp(hub, token, "task_add", title="Implement the requested change", status="todo")
    assert ok, reply
    number = int(re.search(r"#(\d+)", reply)[1])
    assert worker.task_show(number)["status"] == "todo"
    assert agent.parent_agent is None
    assert mcp(hub, token, "task_update", task=number, status="doing")[0]
    assert mcp(hub, token, "task_update", task=number, status_line=CONTEXT)[0]
    assert mcp(hub, token, "task_heartbeat", task=number)[0]
    assert mcp(
        hub, token, "task_update", task=number, status="review", question=QUESTION, options=OPTIONS
    )[0]
    assert worker.task_show(number)["ask"] == QUESTION
    assert not mcp(hub, token, "task_update", task=number, status="done")[0]
    owner = service(hub, sessions.person_principal(hub.db, hub.admin, hub.workspace))
    owner.decide(number, choice=OPTIONS[0])
    assert mcp(hub, token, "task_take_next", task=number)[0]
    assert mcp(hub, token, "task_update", task=number, status="done")[0]
    assert worker.task_show(number)["status"] == "done"
    ok, reply = mcp(hub, token, "task_add", title="Next workstream", assignee="self")
    assert ok and "doing" in reply
    next_card = worker.task_list(mine=True)[0]
    assert next_card["assignee"] == agent.actor and next_card["claim_until"]


def test_explicit_grok_overrides_and_legacy_own_backlog(hub, capsys):
    assert (
        main(
            [
                "--data",
                str(Path(hub.db.path).parent),
                "token",
                "add",
                "coordinator",
                "--workspace",
                hub.workspace,
                "--client",
                "grok",
                "--profile",
                "standard",
                "--trust",
                "trusted",
                "--by",
                "owner@example.com",
            ]
        )
        == 0
    )
    token = capsys.readouterr().out.strip()
    worker = service(hub, hub.db.authenticate_token(token))
    assert worker.principal.policy.trust == "trusted"
    assert worker.principal.policy.tools == "standard"
    worker.board_add(slug="project")
    created = worker.task_add(title="Direct to todo", board="project", status="todo")
    assert created["status"] == "todo"
    with worker.store.transaction() as conn:
        conn.execute(
            "UPDATE tasks SET status='backlog',restricted_origin=1 WHERE id=?", (created["number"],)
        )
    assert worker.task_update(created["number"], status="todo")["status"] == "todo"
    assert (
        worker.task_update(created["number"], status="doing")["assignee"] == worker.principal.actor
    )
    assert worker.task_update(created["number"], status="done")["status"] == "done"


def test_direct_parent_creates_child_cards_and_approves_proposals(hub):
    parent_id, parent_token, parent = actor(hub, "orchestrator")
    root = parent.task_add(title="Main workstream", claim=True)
    child_id, child_token, child = actor(hub, "builder", parent=parent_id)
    _, unrelated_token, unrelated = actor(hub, "unrelated")
    assert child.principal.parent_agent == parent.principal.actor
    assert child.principal.identity()["parent_agent"] == parent.principal.actor
    proposed = child.task_add(title="Unexpected follow-up", claim=True, assignee="self")
    number = proposed["number"]
    assert proposed["status"] == "backlog" and proposed["proposal_status"] == "pending"
    assert proposed["parent"] == root["number"]
    assert proposed["agent_parent"] == proposed["proposed_to"] == parent.principal.actor
    assert not proposed["claim_until"]
    assert number in {c["number"] for c in parent.task_list(mine=True)}
    for token in (child_token, unrelated_token):
        assert not mcp(hub, token, "task_approve", task=number, reason="synthetic reason")[0]
    for status in ("todo", "doing", "review", "done"):
        assert not mcp(hub, child_token, "task_update", task=number, status=status)[0]
    assert not mcp(hub, child_token, "task_take_next", task=number)[0]
    assert not mcp(hub, child_token, "task_handoff", task=number, to="anyone", note=CONTEXT)[0]
    assert api(hub, child_token, "POST", f"/api/cards/{number}/approve", {})[0] == 403
    assert mcp(hub, parent_token, "task_approve", task=number, reason="synthetic reason")[0]
    assert child.task_show(number)["approved_by"] == parent.principal.actor
    assert mcp(hub, child_token, "task_update", task=number, status="doing")[0]
    assert mcp(hub, child_token, "task_update", task=number, status="done")[0]
    assigned = parent.task_add(title="Planned child work", assignee=child.principal.actor)
    assert assigned["status"] == "todo" and assigned["proposal_status"] is None
    assert assigned["parent"] == root["number"]
    assert child.task_take_next(task=assigned["number"])["assignee"] == child.principal.actor
    active = parent.task_add(
        title="Start another child task", assignee=child.principal.actor, claim=True
    )
    assert active["status"] == "doing" and active["claim_until"]
    assert not mcp(hub, unrelated_token, "task_update", task=active["number"], status="done")[0]
    assert hub.db.connection(child_id)["parent_id"] == parent_id


def test_rejection_owner_fallback_and_nested_parent(hub):
    root_id, _, root = actor(hub, "root")
    root.task_add(title="Root work", claim=True)
    child_id, _, child = actor(hub, "child", parent=root_id)
    _, _, nested = actor(hub, "nested", parent=child_id)
    proposal = nested.task_add(title="Nested proposal")
    _, _, unrelated = actor(hub, "unrelated-root")
    with pytest.raises(ToolError, match="direct parent"):
        unrelated.task_approve(proposal["number"], reason="synthetic reason")
    rejected = child.task_approve(proposal["number"], approve=False, reason="synthetic reason")
    assert rejected["proposal_status"] == "rejected"
    for status in ("todo", "doing", "done"):
        with pytest.raises(ToolError):
            nested.task_update(proposal["number"], status=status)
    person = service(hub, sessions.person_principal(hub.db, hub.admin, hub.workspace))
    proposal = nested.task_add(title="Owner fallback")
    assert (
        person.task_approve(proposal["number"], reason="synthetic reason")["proposal_status"]
        == "approved"
    )
    assert nested.task_update(proposal["number"], status="doing")["status"] == "doing"
    assert nested.task_update(proposal["number"], status="done")["status"] == "done"
    proposal = nested.task_add(title="Root release")
    assert (
        root.task_approve(proposal["number"], reason="synthetic reason")["approved_by"]
        == root.principal.actor
    )


def test_heartbeat_expiry_keeps_assignment_and_records_once(hub):
    _, token, holder = actor(hub, "holder")
    _, other_token, other = actor(hub, "other")
    created = holder.task_add(title="Long workstream", claim=True)
    number = created["number"]
    with holder.store.transaction() as conn:
        conn.execute("UPDATE tasks SET claim_until='2000-01-01T00:00:00Z' WHERE id=?", (number,))
    expired = other.task_show(number)
    assert expired["status"] == "doing" and expired["assignee"] == holder.principal.actor
    assert expired["claim_expired"] and expired["need"] == "expired"
    assert other.task_show(number) == expired
    assert len([e for e in expired["events"] if e["kind"] == "expire"]) == 1
    assert "expired" in mcp(hub, token, "task_show", task=number)[1]
    assert not mcp(hub, other_token, "task_heartbeat", task=number)[0]
    assert not mcp(hub, other_token, "task_take_next", task=number)[0]
    assert mcp(hub, token, "task_heartbeat", task=number)[0]
    renewed = holder.task_show(number)
    assert not renewed["claim_expired"] and renewed["claim_expired_at"] is None
    assert renewed["assignee"] == holder.principal.actor and renewed["heartbeat_at"]
    assert renewed["claim_until"] > expired["claim_until"]


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"question": "Review?", "options": OPTIONS, "context": CONTEXT},
        {"question": QUESTION, "context": CONTEXT},
        {"question": QUESTION, "options": ["Only option"], "context": CONTEXT},
        {"question": QUESTION, "options": ["Yes", "yes"], "context": CONTEXT},
        {"question": QUESTION, "options": OPTIONS},
    ],
)
def test_contextless_reviews_are_rejected_atomically(hub, body):
    _, token, worker = actor(hub, "worker")
    created = worker.task_add(title="Needs a decision", claim=True)
    before = worker.task_show(created["number"])
    ok, output = mcp(hub, token, "task_update", task=created["number"], status="review", **body)
    assert not ok and "Stay in doing" in output
    assert worker.task_show(created["number"]) == before
    ok, output = mcp(
        hub,
        token,
        "ask",
        task=created["number"],
        question=body.get("question", "Review?"),
        options=body.get("options"),
        context=body.get("context", ""),
    )
    assert not ok
    assert worker.task_show(created["number"]) == before


def test_fresh_users_and_trusted_agents_create_spaces_normally(hub):
    member = hub.db.create_user("member@example.com")
    hub.db.add_member(hub.workspace, member, "member", actor=hub.admin)
    person = service(hub, sessions.person_principal(hub.db, member, hub.workspace))
    created = person.board_add(slug="team", name="Team")
    assert created["wing"] == "shared"
    token = hub.token(
        "trusted-chat",
        user=member,
        profile="slim",
        trust="trusted",
        read_wings=["shared", "inbox"],
        write_wing="inbox",
    )
    assert "board_add" in hub.tool_names(token)
    assert mcp(hub, token, "board_add", slug="new-project", name="New project")[0]
    assert api(hub, token, "POST", "/api/boards", {"slug": "another-project"})[0] == 201
    assert (
        api(hub, token, "POST", "/api/boards", {"slug": "secret", "wing": "private.secret"})[0]
        == 403
    )
    assert api(hub, token, "PATCH", "/api/boards/team", {"coordinator": None})[0] == 403


def test_additive_migration_preserves_existing_top_level_cards(hub):
    _, _, worker = actor(hub, "worker")
    created = worker.task_add(title="Existing work", body=CONTEXT, claim=True)
    with worker.store.transaction() as conn:
        for name in (
            "claim_expired_at",
            "agent_parent",
            "proposal_status",
            "proposed_to",
            "approved_by",
        ):
            conn.execute(f"ALTER TABLE tasks DROP COLUMN {name}")
    with worker.store.connection() as conn:
        conn.execute("ALTER TABLE task_events DROP COLUMN context")
    migrated = BoardStore(worker.ctx.workspace_dir())
    assert migrated.changes >= 1
    current = worker.task_show(created["number"])
    assert current["title"] == created["title"] and current["body"] == CONTEXT
    assert current["status"] == "doing" and current["assignee"] == worker.principal.actor
    assert current["proposal_status"] is None and current["agent_parent"] is None


@pytest.mark.parametrize(
    "repo",
    ["example/project", "github.com/example/project", "https://github.com/example/project.git"],
)
def test_pr_number_builds_full_generic_url(hub, repo):
    _, _, worker = actor(hub, "worker")
    created = worker.task_add(title="Ready for merge", claim=True)
    updated = worker.task_update(created["number"], repo=repo, pr=42, checks="passing")
    assert updated["pr_url"] == "https://github.com/example/project/pull/42"


@pytest.mark.parametrize(
    "endpoint,tool", [("heartbeat", "task_heartbeat"), ("approve", "task_approve")]
)
@pytest.mark.parametrize("limit", ["scope", "tools"])
def test_new_tools_preserve_explicit_token_limits(hub, endpoint, tool, limit):
    _, _, holder = actor(hub, "holder")
    created = holder.task_add(title="Scoped work", claim=True)
    token = hub.token(
        "limited",
        profile="slim",
        trust="standard",
        read_wings=["shared"],
        write_wings=["shared"],
        scopes=["memory:read"] if limit == "scope" else ["memory:read", "memory:write"],
        tools=[] if limit == "tools" else [tool],
    )
    assert tool not in hub.tool_names(token)
    assert not mcp(hub, token, tool, task=created["number"])[0]
    assert api(hub, token, "POST", f"/api/cards/{created['number']}/{endpoint}", {})[0] == 403


def test_signed_in_people_must_provide_a_real_decision(hub):
    person = service(hub, sessions.person_principal(hub.db, hub.admin, hub.workspace))
    person.board_add(slug="team")
    created = person.task_add(title="User workstream", board="team", claim=True)
    with pytest.raises(ToolError, match="Stay in doing"):
        person.task_update(created["number"], status="review", question="Review?", options=OPTIONS)
    assert person.task_show(created["number"])["status"] == "doing"
    reviewed = person.task_update(
        created["number"], status="review", question=QUESTION, context=CONTEXT, options=OPTIONS
    )
    assert reviewed["ask"] == QUESTION


def test_review_uses_body_even_when_status_line_is_short(hub):
    _, _, worker = actor(hub, "worker")
    created = worker.task_add(title="Decision context", body=CONTEXT, claim=True)
    worker.task_update(created["number"], status_line="Ready")
    reviewed = worker.task_update(
        created["number"], status="review", question=QUESTION, options=OPTIONS
    )
    assert reviewed["ask"] == QUESTION and reviewed["body"] == CONTEXT


def test_inbox_memory_links_renew_only_the_held_card(hub):
    token = hub.token(
        "inbox-worker",
        profile="slim",
        trust="untrusted",
        read_wings=["shared", "inbox"],
        write_wing="inbox",
    )
    worker = service(hub, hub.db.authenticate_token(token))
    created = worker.task_add(title="Linked work", claim=True)
    with worker.store.transaction() as conn:
        conn.execute(
            "UPDATE tasks SET claim_until='2000-01-01T00:00:00Z' WHERE id=?", (created["number"],)
        )
    worker.store.link_drawer(
        created["number"],
        "memory-example",
        worker.principal.actor,
        principal=worker.principal,
        theme="inbox",
    )
    assert not worker.task_show(created["number"])["claim_expired"]


def test_board_default_ui_routes_and_forms():
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("node is needed for board UI checks")
    subprocess.run(
        [node, str(Path(__file__).with_name("commonplace_board_defaults_render.mjs"))],
        check=True,
        timeout=10,
    )


def test_restricted_origin_stays_private_to_its_creator_until_owner_release(hub):
    token = hub.token(
        "inbox-creator",
        profile="slim",
        trust="untrusted",
        read_wings=["shared", "inbox"],
        write_wing="inbox",
    )
    creator = service(hub, hub.db.authenticate_token(token))
    created = creator.task_add(title="Untrusted workstream")
    number = created["number"]
    assert created["status"] == "todo" and created["assignee"] == creator.principal.actor
    with creator.store.connection() as conn:
        assert (
            conn.execute("SELECT restricted_origin FROM tasks WHERE id=?", (number,)).fetchone()[0]
            == 1
        )
    other_token = hub.owner_token("other-worker")
    other = service(hub, hub.db.authenticate_token(other_token))
    assert other.task_take_next() is None
    assert not mcp(hub, other_token, "task_take_next", task=number)[0]
    creator.task_take_next(task=number)
    creator.task_handoff(number, to="anyone", note="Ready for the shared queue")
    assert not mcp(hub, other_token, "task_update", task=number, status="doing")[0]
    assert not mcp(hub, token, "task_update", task=number, approved_by="owner")[0]
    member = hub.db.create_user("member@example.com")
    hub.db.add_member(hub.workspace, member, "member", actor=hub.admin)
    person = service(hub, sessions.person_principal(hub.db, member, hub.workspace))
    with pytest.raises(ToolError, match="owner release"):
        person.task_update(number, status="doing")
    assert creator.task_show(number)["approved_by"] is None
    queued = other.task_take_next()
    assert queued is not None
    assert queued["number"] == number
    # Child-created work still needs a releasing authority; an intermediate
    # restricted parent may approve its child's own work without releasing it.
    top_id, _, _ = actor(hub, "top")
    from commonplace.policy import Policy

    def restricted_child(name, parent):
        cid, token = hub.db.create_token(
            workspace_ref=hub.workspace,
            user_ref=hub.admin,
            name=name,
            client="chatgpt",
            actor=hub.admin,
            parent=parent,
            policy=Policy.build(trust="untrusted", read_wings=["shared"], write_wing="inbox"),
        )
        return cid, service(hub, hub.db.authenticate_token(token))

    mid_id, mid = restricted_child("mid", top_id)
    _, child = restricted_child("inbox-child", mid_id)
    proposal = child.task_add(title="Restricted child work")
    number = proposal["number"]
    assert mid.task_approve(number, reason="synthetic reason")["approved_by"] is None
    child.task_take_next(task=number)
    child.task_handoff(number, to="anyone", note="Ready for approval")
    assert not mcp(hub, other_token, "task_take_next", task=number)[0]
    assert not mcp(hub, other_token, "task_update", task=number, status="doing")[0]
    assert other.task_take_next() is None
    with pytest.raises(ToolError, match="owner release"):
        person.task_update(number, status="doing")
    owner = service(hub, sessions.person_principal(hub.db, hub.admin, hub.workspace))
    owner.task_handoff(number, to="anyone", note="Approved for the shared queue")
    assert child.task_show(number)["approved_by"] == owner.principal.actor
    assert other.task_take_next()["number"] == number


def test_decision_context_preserves_progress_body_and_history(hub):
    _, _, worker = actor(hub, "worker")
    created = worker.task_add(
        title="Context preservation", body="Original detailed implementation notes.", claim=True
    )
    worker.task_update(created["number"], status_line="Still waiting on the release decision.")
    reviewed = worker.task_update(
        created["number"], status="review", question=QUESTION, context=CONTEXT, options=OPTIONS
    )
    assert reviewed["status_line"] == "Still waiting on the release decision."
    assert reviewed["body"] == "Original detailed implementation notes."
    assert reviewed["review_context"] == CONTEXT
    event = worker.task_show(created["number"])["events"][-1]
    assert event["context"] == CONTEXT
    owner = service(hub, sessions.person_principal(hub.db, hub.admin, hub.workspace))
    owner.decide(created["number"], choice=OPTIONS[0], then="done")
    completed = worker.task_show(created["number"])
    assert completed["status"] == "done"
    assert completed["body"] == reviewed["body"]
    assert completed["status_line"] == reviewed["status_line"]
    assert completed["review_context"] is None
    assert any(event.get("context") == CONTEXT for event in completed["events"])
    assert completed["events"][-1]["kind"] == "decide"


def test_restricted_child_can_claim_after_direct_parent_approval(hub):
    parent_id, _, parent = actor(hub, "parent")
    parent.task_add(title="Parent workstream", claim=True)
    from commonplace.policy import Policy

    _, token = hub.db.create_token(
        workspace_ref=hub.workspace,
        user_ref=hub.admin,
        name="chat-child",
        client="grok",
        actor=hub.admin,
        parent=parent_id,
        policy=Policy.build(
            profile="slim", trust="untrusted", read_wings=["shared", "inbox"], write_wing="inbox"
        ),
    )
    child = service(hub, hub.db.authenticate_token(token))
    created = child.task_add(title="Child proposal", claim=True)
    assert created["proposal_status"] == "pending"
    parent.task_approve(created["number"], reason="synthetic reason")
    assert mcp(hub, token, "task_update", task=created["number"], status="doing")[0]
    assert mcp(hub, token, "task_update", task=created["number"], status="done")[0]
