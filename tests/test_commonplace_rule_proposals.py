"""Fresh workspace agents propose shared rules without acquiring owner authority."""

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from commonplace import extensions, sessions
from commonplace.boards import BoardService
from commonplace.extensions import ToolContext, ToolError
from commonplace.policy import Policy
from commonplace.rules import RuleService, checks, render
from commonplace.rules.service import instructions_for
from tests._commonplace_helpers import hub_fixture  # noqa: F401
from tests.test_commonplace_boards import api, mcp


def rule(key="shared", text="Keep exact words.\n  Preserve their spacing.", **kwargs):
    return dict(key=key, text=text, kind="instruction", **kwargs)


@pytest.fixture
def fresh(hub):
    email = "new-owner@example.test"
    user = hub.db.create_user(email)
    workspace = hub.db.create_workspace("new-workspace", user)
    person = sessions.person_principal(hub.db, user, workspace)
    owner = RuleService(ToolContext(principal=person, gateway=hub.gateway))

    def connect(name, **policy):
        _, token = hub.db.create_token(
            name=name,
            user_ref=user,
            workspace_ref=workspace,
            client="generic",
            actor=user,
            policy=Policy.build(
                profile="standard",
                scopes=["memory:read", "memory:write"],
                read_wings=["shared"],
                write_wing="shared",
                **policy,
            ),
        )
        principal = hub.db.authenticate_token(token)
        return SimpleNamespace(
            token=token,
            service=RuleService(ToolContext(principal=principal, gateway=hub.gateway)),
        )

    writer, other = connect("writer"), connect("other")
    session = sessions.redeem_login_link(
        hub.db, sessions.create_login_link(hub.db, email, workspace)
    )
    assert owner.list_rules()["rules"] == []
    return SimpleNamespace(
        owner=owner,
        writer=writer.service,
        other=other.service,
        token=writer.token,
        cookie={"Cookie": f"{sessions.COOKIE}={session}"},
        connect=connect,
        user=user,
        workspace=workspace,
    )


def test_fresh_user_mcp_shared_proposal_needs_one_owner_acceptance(hub, fresh, monkeypatch):
    # Proposal acceptance and verbatim text must not depend on scheduler delays.
    monkeypatch.setattr(checks, "monotonic", lambda: 0.0)
    schema = extensions.get("rules_publish").input_schema["properties"]["agent"]
    assert schema == {"type": "string", "enum": ["self", "*"], "default": "self"}
    ok, reply = mcp(hub, fresh.token, "rules_publish", rules=[rule()], agent="*")
    assert ok and reply.startswith("shared proposed 1\nseq ")
    proposal = fresh.owner.list_rules()["rules"][0]
    assert proposal["agent"] == "*" and proposal["source"] == "agent"
    assert proposal["text"] == rule()["text"]
    assert proposal["agent_text_by"] == fresh.writer.principal.actor
    with fresh.owner.store.read() as conn:
        assert conn.execute("SELECT origin_agent FROM rules").fetchone()[0] == (
            fresh.writer.principal.actor
        )
    assert fresh.owner.history(proposal["id"])[0]["stamp_connection"] == "writer"
    for agent in (fresh.writer, fresh.other):
        assert agent.rules_get(effective=True)["rules"] == []
        assert instructions_for(agent.ctx) == ""
        with pytest.raises(ToolError, match="owner"):
            agent.action(proposal["id"], 1, "accept")
    status, _ = api(
        hub,
        fresh.token,
        "POST",
        f"/api/rules/{proposal['id']}/action",
        {"version": 1, "action": "accept"},
    )
    assert status == 403
    status, accepted = api(
        hub,
        fresh.cookie,
        "POST",
        f"/api/rules/{proposal['id']}/action",
        {"version": 1, "action": "accept"},
    )
    assert status == 200 and accepted["status"] == "accepted"
    for agent in (fresh.writer, fresh.other):
        effective = agent.rules_get(effective=True, format="md")
        assert [row["text"] for row in effective["rules"]] == [rule()["text"]]
        assert render.quote(rule()["text"]) in instructions_for(agent.ctx)


def test_other_proposer_cannot_overwrite_or_retire_a_shared_key(fresh):
    current = fresh.writer.rules_publish([rule()], agent="*")["rules"][0]
    for action in ("accept", "edit", "retire"):
        before = fresh.owner.list_rules()
        with pytest.raises(ToolError, match="owner or another proposer"):
            fresh.other.rules_publish(
                [rule(key="new", text="Must roll back"), rule(text="Other wording")],
                replace=True,
                agent="*",
            )
        assert fresh.owner.list_rules() == before
        assert fresh.other.rules_publish([], replace=True, agent="*")["rules"] == []
        if action == "edit":
            current = fresh.owner.edit(current["id"], current["version"], text="Owner wording")
        else:
            current = fresh.owner.action(current["id"], current["version"], action)
    # Retired keys retain their provenance and cannot be taken over either.
    with pytest.raises(ToolError, match="owner or another proposer"):
        fresh.other.rules_publish([rule()], agent="*")
    owner_rule = fresh.owner.create("*", "Owner's shared rule", "limit")
    with pytest.raises(ToolError, match="owner or another proposer"):
        fresh.writer.rules_publish([rule(key=owner_rule["key"])], agent="*")


def test_shared_acceptance_and_owner_edits_survive_republish_and_replace(fresh):
    proposal = fresh.writer.rules_publish(
        [rule(summary="Keep the original.", scope="repo", repo="example.test/org/project")],
        agent="*",
    )["rules"][0]
    accepted = fresh.owner.action(proposal["id"], 1, "accept")
    sequence = fresh.writer.rules_get(effective=True)["rules_seq"]
    pending = fresh.writer.rules_publish(
        [dict(rule(text="Suggested wording", summary="Different summary."), kind="limit")],
        agent="*",
    )["rules"][0]
    for field in ("text", "summary", "kind", "scope", "scope_ref", "status", "source"):
        assert pending[field] == accepted[field]
    assert pending["pending"] == 1 and pending["agent_text"] == "Suggested wording"
    assert fresh.writer.rules_get(effective=True)["rules_seq"] == sequence
    assert fresh.writer.rules_publish([], replace=True, agent="*")["rules"] == []
    taken = fresh.owner.action(pending["id"], pending["version"], "accept_agent")
    assert taken["text"] == "Suggested wording" and taken["status"] == "accepted"
    edited = fresh.owner.edit(taken["id"], taken["version"], text="Owner's final wording")
    suggested = fresh.writer.rules_publish([rule(text="Another suggestion")], agent="*")["rules"][0]
    assert suggested["text"] == edited["text"] and suggested["source"] == "owner"
    retired = fresh.owner.action(suggested["id"], suggested["version"], "retire")
    repeated = fresh.writer.rules_publish([rule(text="Try to revive")], agent="*")["rules"][0]
    assert repeated["status"] == retired["status"] == "retired"
    assert repeated["retired_by"] == "owner" and repeated["pending"] == 1
    assert fresh.writer.rules_publish([], replace=True, agent="*")["rules"] == []
    assert fresh.writer.rules_get(effective=True)["rules"] == []


def test_replace_is_scoped_to_caller_and_requested_target(fresh):
    own = fresh.writer.rules_publish([rule(key="same"), rule(key="self-drop")])["rules"]
    shared = fresh.writer.rules_publish([rule(key="same"), rule(key="accepted")], agent="*")[
        "rules"
    ]
    accepted = fresh.owner.action(shared[1]["id"], 1, "accept")
    other = fresh.other.rules_publish([rule(key="other")], agent="*")["rules"][0]
    result = fresh.writer.rules_publish([rule(key="same")], replace=True)["rules"]
    assert [(row["id"], row["status"]) for row in result] == [
        (own[0]["id"], "proposed"),
        (own[1]["id"], "retired"),
    ]
    result = fresh.writer.rules_publish([], replace=True, agent="*")["rules"]
    assert [(row["id"], row["status"]) for row in result] == [(shared[0]["id"], "retired")]
    listing = {row["id"]: row for row in fresh.owner.list_rules()["rules"]}
    assert listing[own[0]["id"]] == own[0]
    assert listing[accepted["id"]] == accepted and listing[other["id"]] == other
    revived = fresh.writer.rules_publish([rule(key="same")], agent="*")["rules"][0]
    assert revived["id"] == shared[0]["id"] and revived["status"] == "proposed"
    assert fresh.writer.rules_publish([rule(key="same")])["rules"] == [own[0]]


def test_owner_moves_keep_authority_and_original_publish_target(fresh):
    shared = fresh.writer.rules_publish([rule()], agent="*")["rules"][0]
    moved = fresh.owner.set_agent(shared["id"], 1, fresh.writer.principal.actor)
    assert fresh.writer.rules_publish([], replace=True)["rules"] == []
    assert fresh.writer.rules_publish([], replace=True, agent="*")["rules"] == []
    pending = fresh.writer.rules_publish([rule(text="New wording")], agent="*")["rules"][0]
    assert pending["id"] == moved["id"] and pending["text"] == moved["text"]
    assert pending["agent"] == moved["agent"] and pending["pending"] == 1
    hidden = fresh.owner.set_agent(pending["id"], pending["version"], fresh.other.principal.actor)
    assert fresh.writer.rules_publish([rule(text="Again")], agent="*")["rules"] == []
    assert fresh.owner.list_rules()["rules"] == [hidden]
    personal = fresh.writer.rules_publish([rule(key="personal")])["rules"][0]
    all_agents = fresh.owner.set_agent(personal["id"], 1, "*")
    assert fresh.writer.rules_publish([], replace=True, agent="*")["rules"] == []
    repeated = fresh.writer.rules_publish([rule(key="personal", text="Personal rewrite")])["rules"][
        0
    ]
    assert repeated["id"] == all_agents["id"] and repeated["text"] == all_agents["text"]
    assert repeated["pending"] == 1


def test_agent_shared_target_survives_owner_move_roundtrip(fresh):
    shared = fresh.writer.rules_publish([rule()], agent="*")["rules"][0]
    moved = fresh.owner.set_agent(shared["id"], 1, fresh.writer.principal.actor)
    back = fresh.owner.set_agent(moved["id"], moved["version"], "*")
    personal = fresh.writer.rules_publish([rule()])["rules"][0]
    assert personal["id"] != shared["id"]
    assert personal["agent"] == fresh.writer.principal.actor
    repeated = fresh.writer.rules_publish([rule()], agent="*")["rules"][0]
    assert repeated["id"] == shared["id"] and repeated["agent"] == "*"
    assert fresh.writer.rules_publish([], replace=True)["rules"][0]["id"] == personal["id"]
    current = {row["id"]: row for row in fresh.owner.list_rules()["rules"]}
    assert current[shared["id"]] == back
    assert current[personal["id"]]["status"] == "retired"


def test_shared_board_scope_preserves_private_visibility(hub, fresh):
    boards = BoardService(fresh.owner.ctx)
    boards.board_add(slug="visible", wing="shared")
    boards.board_add(slug="hidden", wing="private.notes")
    private = fresh.owner.create("*", "Private instructions", "limit", "board", "hidden")
    sequence = fresh.owner.store.seq
    with pytest.raises(ToolError, match="board not found"):
        fresh.writer.rules_publish([rule(scope="board", board="hidden")], agent="*")
    assert fresh.owner.store.seq == sequence
    visible = fresh.writer.rules_publish([rule(scope="board", board="visible")], agent="*")[
        "rules"
    ][0]
    fresh.owner.action(visible["id"], 1, "accept")
    for agent in (fresh.writer, fresh.other):
        effective = agent.rules_get(effective=True)["rules"]
        assert [row["id"] for row in effective] == [visible["id"]]
        assert private["text"] not in instructions_for(agent.ctx)
    # A hidden collision discloses no rule or wording and never changes its scope.
    assert fresh.writer.rules_publish([rule(key=private["key"])], agent="*")["rules"] == []
    assert fresh.owner.list_rules()["rules"][0] == private


def test_invalid_targets_and_forged_proposers_are_rejected(hub, fresh):
    for target in ("conn:writer", "all", "", None, 1, ["*"], {"agent": "*"}):
        with pytest.raises(ToolError, match="agent must be self or"):
            fresh.writer.rules_publish([rule()], agent=target)
        assert not mcp(hub, fresh.token, "rules_publish", rules=[rule()], agent=target)[0]
    for field in ("origin_agent", "agent_text_by", "status", "agent"):
        assert not mcp(
            hub, fresh.token, "rules_publish", rules=[rule(**{field: "conn:other"})], agent="*"
        )[0]
    assert fresh.owner.list_rules()["rules"] == []
    assert mcp(hub, fresh.token, "rules_publish", rules=[rule()])[0]
    personal = fresh.owner.list_rules()["rules"][0]
    assert personal["agent"] == fresh.writer.principal.actor
    assert fresh.writer.rules_publish([rule()], agent="self")["rules"] == [personal]


def test_shared_proposal_ui_shows_target_proposer_and_one_click_accept():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is needed for the Rules render check")
    static = Path(__file__).parents[1] / "commonplace" / "web" / "static"
    script = """
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
const staticDir = STATIC_DIR;
const data = source => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`;
const api = data(await readFile(`${staticDir}/api.js`, 'utf8'));
const source = (await readFile(`${staticDir}/rules.js`, 'utf8')).replace('"./api.js"', `"${api}"`);
const { rulesView, rulesHandlers } = await import(data(source));
const rule = { id: 1, agent: '*', key: 'shared', text: 'Full original. Second sentence.',
  summary: 'Compact display.', agent_text_by: 'conn:<writer>', kind: 'instruction',
  scope: 'global', status: 'proposed', source: 'agent', version: 1 };
const payload = { rules: [rule], waiting: 1, waiting_by_agent: { '*': 1 } };
const html = rulesView(payload);
assert.ok(html.includes('Applies to<select name="agent"><option value="*">All agents'));
assert.ok(html.includes('data-rule-agent="*"'));
assert.ok(html.includes('Proposed by &lt;writer&gt;') && !html.includes('conn:<writer>'));
assert.ok(html.includes('data-rule-action="accept" >Accept for all agents</button>'));
assert.ok(!html.includes('data-rule-action="accept-all"'));
assert.ok(html.includes('Compact display.') && html.includes('Full original. Second sentence.'));
assert.ok(html.includes('data-rule-full hidden') && html.includes('aria-expanded="false"'));
const readonly = rulesView(payload, { owner: false });
assert.ok(!readonly.includes('data-rule-action="accept"') && !readonly.includes('Proposed by'));
const handlers = {}, requests = [];
const root = { addEventListener: (name, fn) => { handlers[name] = fn; }, querySelector: () => null };
const ui = rulesHandlers(root, async (...request) => requests.push(request), async () => {}, assert.fail);
ui.update(payload);
const row = { dataset: { ruleId: '1', version: '1' }, querySelectorAll: () => [] };
const button = { dataset: { ruleAction: 'accept' }, closest: key => key === '[data-rule-id]' ? row : null };
await handlers.click({ target: { closest: () => button } });
assert.deepEqual(requests, [['/api/rules/1/action', { method: 'POST', body: { action: 'accept', version: 1 } }]]);
""".replace("STATIC_DIR", json.dumps(str(static)))
    subprocess.run([node, "--input-type=module", "-e", script], check=True, timeout=10)
