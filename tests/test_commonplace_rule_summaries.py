"""Display summaries stay separate from verbatim rules and their enforcement."""

import sqlite3
from types import SimpleNamespace

import pytest

from commonplace import sessions
from commonplace.extensions import ToolContext, ToolError
from commonplace.policy import Policy
from commonplace.rules import RuleService, RuleStore, checks
from commonplace.rules.service import instructions_for
from tests._commonplace_helpers import hub_fixture  # noqa: F401
from tests.test_commonplace_boards import api, mcp


@pytest.fixture
def fresh(hub):
    email = "new-user@example.com"
    uid = hub.db.create_user(email)
    workspace = hub.db.create_workspace("new-workspace", uid)
    _, token = hub.db.create_token(
        name="writer",
        user_ref=uid,
        workspace_ref=workspace,
        client="generic",
        actor=uid,
        policy=Policy.build(
            profile="standard",
            trust="trusted",
            read_wings=["shared"],
            write_wing="shared",
        ),
    )
    agent = hub.db.authenticate_token(token)
    person = sessions.person_principal(hub.db, uid, workspace)
    session = sessions.redeem_login_link(
        hub.db, sessions.create_login_link(hub.db, email, workspace)
    )
    worker = RuleService(ToolContext(principal=agent, gateway=hub.gateway))
    owner = RuleService(ToolContext(principal=person, gateway=hub.gateway))
    assert owner.list_rules()["rules"] == []
    return SimpleNamespace(
        worker=worker,
        owner=owner,
        token=token,
        agent=agent,
        cookie={"Cookie": f"{sessions.COOKIE}={session}"},
    )


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Keep exact words", "Keep exact words"),
        ("Keep exact words. Never discard context. Keep every line.", "Keep exact words."),
        ("Use e.g. structured names. Keep the original.", "Use e.g. structured names."),
        ("Use version 1.2. Keep compatibility.", "Use version 1.2."),
        ('Keep "exact words." Preserve context.', 'Keep "exact words."'),
        ("保持原文。保留上下文。", "保持原文。"),
        ("Keep " + "every word " * 70, ("Keep " + "every word " * 70)[:239].rstrip() + "…"),
    ],
)
def test_fresh_workspace_automatic_summary_preserves_full_rule(fresh, text, expected):
    current = fresh.owner.create("*", text, "instruction")
    assert current["summary"] is None and current["display_summary"] == expected
    assert current["text"] == text
    assert fresh.owner.history(current["id"])[0]["text"] == text
    assert fresh.owner.list_rules()["rules"][0]["display_summary"] == expected


def test_agent_mcp_summary_omitted_preserved_and_explicitly_cleared(hub, fresh):
    payload = {
        "key": "work",
        "text": "Keep the complete original. Keep its context.",
        "kind": "instruction",
        "summary": "Keep the original and its context.",
    }
    assert mcp(hub, fresh.token, "rules_publish", rules=[payload])[0]
    current = fresh.worker.rules_get()["rules"][0]
    assert current["summary"] == payload["summary"]
    assert current["display_summary"] == payload["summary"]
    payload.pop("summary")
    assert fresh.worker.rules_publish([payload])["rules"][0] == current
    for summary in ("", None):
        payload["summary"] = summary
        current = fresh.worker.rules_publish([payload])["rules"][0]
        assert current["summary"] is None
        assert current["display_summary"] == "Keep the complete original."
        assert current["text"] == payload["text"]
    assert [v["summary"] for v in fresh.owner.history(current["id"])] == [
        "Keep the original and its context.",
        None,
    ]


def test_owner_rest_summary_updates_leave_injection_and_acceptance_unchanged(
    hub, fresh, monkeypatch
):
    # Summary edits and verbatim injection must not depend on scheduler delays.
    monkeypatch.setattr(checks, "monotonic", lambda: 0.0)
    text = "Keep the exact rule text.\n  Preserve its spacing and context."
    status, current = api(
        hub,
        fresh.cookie,
        "POST",
        "/api/rules",
        {
            "agent": "*",
            "text": text,
            "kind": "instruction",
            "summary": "Preserve rule text.",
        },
    )
    assert status == 201
    fresh.owner.set_check(fresh.agent.actor, "enforce")
    before = fresh.worker.rules_get(effective=True, format="md")
    injected = instructions_for(fresh.worker.ctx)
    status, edited = api(
        hub,
        fresh.cookie,
        "PATCH",
        f"/api/rules/{current['id']}",
        {
            "version": current["version"],
            "summary": "Keep every word. Keep its spacing.",
        },
    )
    assert status == 200
    assert edited["status"] == "accepted" and edited["source"] == "owner"
    assert edited["text"] == text and edited["summary"] == "Keep every word. Keep its spacing."
    after = fresh.worker.rules_get(effective=True, format="md")
    assert after["rules_seq"] == before["rules_seq"]
    assert instructions_for(fresh.worker.ctx) == injected
    assert "Keep every word" not in injected and "Preserve its spacing" in injected
    assert fresh.owner.history(current["id"])[-1]["summary"] == edited["summary"]


def test_agent_summary_does_not_reopen_accepted_rule_or_override_owner(fresh):
    payload = {"key": "one", "text": "Preserve exact wording.", "kind": "instruction"}
    original = fresh.worker.rules_publish([payload])["rules"][0]
    accepted = fresh.owner.action(original["id"], original["version"], "accept")
    payload["summary"] = "Preserve wording."
    updated = fresh.worker.rules_publish([payload])["rules"][0]
    assert updated["status"] == accepted["status"] == "accepted"
    owner = fresh.owner.edit(
        updated["id"],
        updated["version"],
        text="Keep the full exact wording.",
        summary="Keep full wording.",
    )
    republished = fresh.worker.rules_publish([payload])["rules"][0]
    assert republished["text"] == owner["text"] and republished["summary"] == owner["summary"]


@pytest.mark.parametrize(
    "summary",
    [
        "First. Second. Third.",
        '"First." "Second." "Third."',
        "第一句。第二句。第三句。",
        "x" * 401,
        12,
        ["words"],
    ],
)
def test_invalid_summary_rejected_atomically(fresh, summary):
    with pytest.raises(ToolError, match="summary"):
        fresh.worker.rules_publish(
            [
                {"key": "valid", "text": "Valid words", "kind": "instruction"},
                {"key": "invalid", "text": "More words", "kind": "instruction", "summary": summary},
            ]
        )
    assert fresh.owner.list_rules()["rules"] == []


def test_migration_keeps_original_text_and_append_only_history(fresh):
    current = fresh.owner.create("*", "Original long rule.\nSecond sentence.", "limit")
    store = fresh.owner.store
    with store.connection() as conn:
        conn.execute("ALTER TABLE rules DROP COLUMN summary")
        conn.execute("ALTER TABLE rule_versions DROP COLUMN summary")
    RuleStore._initialized.discard(store.path)
    migrated = RuleStore(fresh.owner.ctx.workspace_dir())
    with migrated.connection() as conn:
        assert conn.execute("SELECT text,summary FROM rules").fetchone()[0] == current["text"]
        assert conn.execute("SELECT summary FROM rule_versions").fetchone()[0] is None
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE rule_versions SET summary='changed'")
    listed = fresh.owner.list_rules()["rules"][0]
    assert listed["display_summary"] == "Original long rule."
