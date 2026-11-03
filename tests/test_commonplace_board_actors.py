"""Actor ancestry groups web cards without hiding agent work or owner asks."""

import shutil
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path

import pytest

from commonplace import events
from commonplace.boards.service import ParentError
from commonplace.boards.store import BoardStore
from commonplace.cli import main
from commonplace.control_db import ControlDB, ControlError
from commonplace.oauth import store as oauth_store
from commonplace.policy import Policy
from tests._commonplace_helpers import (
    REVIEW_CONTEXT,
    hub_fixture,  # noqa: F401
)
from tests.test_commonplace_boards import api, env, mcp, service  # noqa: F401


def actor(hub, name, parent=None, workspace=None):
    cid, token = hub.db.create_token(
        workspace_ref=workspace or hub.workspace,
        user_ref=hub.admin,
        name=name,
        client="codex",
        policy=Policy.build(
            profile="full",
            scopes=["memory:read", "memory:write"],
            read_wings=["shared"],
            write_wings=["shared"],
            trust="standard",
        ),
        actor=hub.admin,
        parent=parent,
    )
    return cid, token, service(hub, hub.db.authenticate_token(token))


def test_deep_actors_roll_up_four_children_and_agents_still_see_them(hub, env):  # noqa: F811
    token, principal, _person, cookie, owner = env
    top = service(hub, principal)
    parent = top.task_add(title="Build the board", board="work")
    top.task_take_next(task=parent["number"])
    cid, sub_token, sub = actor(hub, "builder", parent=principal.connection)
    _, _, deeper = actor(hub, "test-builder", parent=cid)
    children = [
        sub.task_add(title="  Original\nwords <script>  ", body="  Keep me\n", board="work"),
        sub.task_add(title="Check the phone", board="work"),
        deeper.task_add(title="Test the deeper actor", board="work"),
        deeper.task_add(title="Test the counts", board="work"),
    ]
    for child in children[:2]:
        top.task_approve(child["number"])
    for child in children[2:]:
        sub.task_approve(child["number"])
    for worker, child in ((sub, children[0]), (deeper, children[2])):
        worker.task_update(child["number"], status="done")
    assert {c["parent"] for c in children} == {parent["number"]}
    assert len(top.task_list(board="work", all_statuses=True)) == 5
    status, payload = api(hub, cookie, "GET", "/api/boards/work/cards")
    assert status == 200 and len(payload["cards"]) == 1
    rolled = payload["cards"][0]
    assert rolled["number"] == parent["number"] and rolled["parent"] is None
    assert rolled["children"] == {"done": 2, "total": 4}
    assert {c["number"] for c in rolled["child_cards"]} == {c["number"] for c in children}
    assert (
        next(c for c in rolled["child_cards"] if c["number"] == children[0]["number"])["title"]
        == children[0]["title"]
    )
    assert top.task_show(children[0]["number"])["body"] == "  Keep me\n"
    assert api(hub, cookie, "GET", "/api/boards/work/cards?status=done")[1]["cards"] == []
    assert api(hub, cookie, "GET", "/api/cards")[1]["total"] == 1
    assert api(hub, cookie, "GET", "/api/boards")[1]["boards"][0]["counts"] == {
        "backlog": 0,
        "todo": 0,
        "doing": 1,
        "review": 0,
        "done": 0,
    }
    assert top.board_list()[0]["counts"] == {
        "backlog": 0,
        "todo": 2,
        "doing": 1,
        "review": 0,
        "done": 2,
    }
    for caller in (token, sub_token):
        ok, output = mcp(hub, caller, "board_list")
        assert ok and "work  todo 2  doing 1  review 0" in output
        assert api(hub, caller, "GET", "/api/boards")[1]["boards"][0]["counts"]["todo"] == 0
        ok, output = mcp(hub, caller, "task_list", board="work", status="done")
        assert ok and f"#{children[0]['number']} done" in output
        assert f"parent:#{parent['number']}" in output


@pytest.mark.parametrize("name", ["claude:builder:sub", "codex:builder:sub"])
def test_user_started_connections_and_people_keep_top_level_cards(hub, env, name):  # noqa: F811
    _token, principal, _person, cookie, owner = env
    top = service(hub, principal)
    top.task_add(title="Orchestrate", board="work")
    cid, _token, independent = actor(hub, name)
    created = independent.task_add(title="User started", board="work")
    person_card = owner.task_add(title="A person's task", board="work")
    assert hub.db.connection(cid)["started_by"] == "user"
    assert hub.db.connection(cid)["parent_id"] is None
    assert created["parent"] is None and person_card["parent"] is None
    visible = api(hub, cookie, "GET", "/api/boards/work/cards")[1]["cards"]
    assert {created["number"], person_card["number"]} <= {c["number"] for c in visible}


def test_current_parent_precedence_fallback_and_board_scope(hub, env):  # noqa: F811
    _token, principal, _person, _cookie, owner = env
    top = service(hub, principal)
    _, _, sub = actor(hub, "worker", parent=principal.connection)
    older = top.task_add(title="Older doing", board="work")
    top.task_take_next(task=older["number"])
    newer = top.task_add(title="Newer doing", board="work")
    top.task_take_next(task=newer["number"])
    fallback = top.task_add(title="Newest created", board="work")
    assert sub.task_add(title="Doing wins", board="work")["parent"] == newer["number"]
    top.task_update(newer["number"], status="done")
    assert sub.task_add(title="Other doing wins", board="work")["parent"] == older["number"]
    top.task_update(older["number"], status="done")
    assert sub.task_add(title="Created fallback", board="work")["parent"] == fallback["number"]
    top.task_update(fallback["number"], status="done")
    assert sub.task_add(title="No current work", board="work")["parent"] is None
    owner.board_add(slug="other", wing="shared")
    other = top.task_add(title="Elsewhere", board="other")
    top.task_take_next(task=other["number"])
    assert sub.task_add(title="Never use another board", board="work")["parent"] is None
    later = top.task_add(title="Later parent", board="work")
    assert sub.task_add(title="New child", board="work")["parent"] == later["number"]
    assert all(
        c["parent"] is None
        for c in sub.task_list(board="work")
        if c["title"] in ("No current work", "Never use another board")
    )


def test_assigned_current_card_and_nested_explicit_parent_are_flattened(hub, env):  # noqa: F811
    _token, principal, _person, _cookie, owner = env
    top = service(hub, principal)
    parent = owner.task_add(title="Assigned by a person", board="work", assignee=principal.actor)
    top.task_take_next(task=parent["number"])
    _, _, sub = actor(hub, "worker", parent=principal.connection)
    nested = sub.task_add(title="Nested", board="work")
    assert (
        sub.task_add(title="Explicit flatten", board="work", parent=nested["number"])["parent"]
        == parent["number"]
    )
    owner.task_approve(nested["number"])
    owner.task_handoff(nested["number"], to=principal.actor, note="Take the nested task")
    top.task_take_next(task=nested["number"])
    assert sub.task_add(title="Automatic flatten", board="work")["parent"] == parent["number"]


def test_reassigned_child_cannot_roll_work_into_another_actors_top_level_card(hub, env):  # noqa: F811
    _token, principal, _person, cookie, owner = env
    first = service(hub, principal)
    own = first.task_add(title="First root", board="work")
    first.task_take_next(task=own["number"])
    other_id, _, second = actor(hub, "second-root")
    foreign = second.task_add(title="Second root", board="work")
    _, _, second_sub = actor(hub, "second-worker", parent=other_id)
    nested = second_sub.task_add(title="Reassigned child", board="work", assignee=principal.actor)
    assert nested["parent"] == foreign["number"]
    owner.task_approve(nested["number"])
    owner.task_handoff(nested["number"], to=principal.actor, note="Reassign approved work")
    first.task_take_next(task=nested["number"])
    _, sub_token, first_sub = actor(hub, "first-worker", parent=principal.connection_id)
    status, refused = api(
        hub,
        sub_token,
        "POST",
        "/api/boards/work/cards",
        {
            "title": "Foreign explicit parent",
            "parent": nested["number"],
        },
    )
    assert status == 400 and "top-level actor" in refused["error"]
    ok, output = mcp(
        hub,
        sub_token,
        "task_add",
        board="work",
        title="Foreign explicit parent",
        parent=nested["number"],
    )
    assert not ok and "top-level actor" in output
    standalone = first_sub.task_add(title="Automatic stays top-level", board="work")
    assert standalone["parent"] is None
    assert owner.task_show(foreign["number"])["children"] == {"done": 0, "total": 1}
    assert standalone["number"] in {
        c["number"] for c in api(hub, cookie, "GET", "/api/boards/work/cards")[1]["cards"]
    }


def test_chat_linked_children_keep_asks_and_answer_history(hub, env):  # noqa: F811
    _token, principal, _person, cookie, owner = env
    message = owner.space_post("work", "  Keep the decision with this message.\n")
    top = service(hub, principal)
    parent = top.task_add(title="Parent", board="work", message_id=message["id"])
    _, _, sub = actor(hub, "worker", parent=principal.connection_id)
    child = sub.task_add(title="Child approval", board="work", message_id=message["id"])
    top.task_approve(child["number"])
    sub.ask(
        'Ship <script> & "original"?',
        ["Ship", "Wait"],
        task=child["number"],
        context=REVIEW_CONTEXT,
    )
    sub.task_add(title="Not chat-linked", board="work")
    owner.board_add(slug="other", wing="shared")
    other_message = owner.space_post("other", "Other board")
    top.task_add(title="Other linked card", board="other", message_id=other_message["id"])
    status, payload = api(hub, cookie, "GET", "/api/boards/work/cards")
    assert status == 200 and [c["number"] for c in payload["cards"]] == [parent["number"]]
    assert {c["number"] for c in payload["linked"]} == {parent["number"], child["number"]}
    linked_child = next(c for c in payload["linked"] if c["number"] == child["number"])
    assert (
        linked_child["parent"] == parent["number"] and linked_child["message_id"] == message["id"]
    )
    assert linked_child["need"] == "you" and linked_child["ask_options"] == ["Ship", "Wait"]
    assert linked_child["ask"] == 'Ship <script> & "original"?'
    status, filtered = api(hub, cookie, "GET", "/api/boards/work/cards?status=review")
    assert status == 200 and filtered["cards"] == []
    assert [c["number"] for c in filtered["linked"]] == [child["number"]]
    status, answered = api(
        hub,
        cookie,
        "POST",
        f"/api/cards/{child['number']}/decide",
        {
            "version": linked_child["version"],
            "choice": "Ship",
        },
    )
    assert status == 200 and answered["status"] == "todo"
    refreshed = api(hub, cookie, "GET", "/api/boards/work/cards")[1]
    answered = next(c for c in refreshed["linked"] if c["number"] == child["number"])
    assert answered["ask"] is None and answered["message_id"] == message["id"]
    assert any(
        e["kind"] == "decide" and e["choice"] == "Ship"
        for e in owner.task_show(child["number"])["events"]
    )


def test_explicit_parent_refuses_foreign_cards_and_top_level_callers(hub, env):  # noqa: F811
    token, principal, _person, cookie, owner = env
    top = service(hub, principal)
    parent = top.task_add(title="Parent", board="work")
    foreign = owner.task_add(title="Other actor", board="work")
    owner.board_add(slug="other", wing="shared")
    elsewhere = top.task_add(title="Other board", board="other")
    _, sub_token, sub = actor(hub, "worker", parent=principal.connection)
    for invalid in (foreign["number"], elsewhere["number"], 0, 999999, True, [], 2**70):
        status, payload = api(
            hub,
            sub_token,
            "POST",
            "/api/boards/work/cards",
            {"title": "Refused", "parent": invalid},
        )
        assert status == 400 and "parent" in payload["error"]
    assert (
        api(
            hub,
            cookie,
            "POST",
            "/api/boards/work/cards",
            {"title": "Refused", "parent": parent["number"]},
        )[0]
        == 400
    )
    status, linked = api(
        hub,
        token,
        "POST",
        "/api/boards/work/cards",
        {"title": "Top-level child", "parent": parent["number"]},
    )
    assert status == 201 and linked["parent"] == parent["number"]
    status, payload = api(
        hub,
        token,
        "POST",
        "/api/boards/work/cards",
        {"title": "Refused", "parent": foreign["number"]},
    )
    assert status == 400 and "top-level actor" in payload["error"]
    ok, output = mcp(
        hub, sub_token, "task_add", title="Refused", board="work", parent=foreign["number"]
    )
    assert not ok and "top-level actor" in output
    top.task_update(parent["number"], status="done")
    status, explicit = api(
        hub,
        sub_token,
        "POST",
        "/api/boards/work/cards",
        {"title": "Known parent", "parent": f"#{parent['number']}"},
    )
    assert status == 201 and explicit["parent"] == parent["number"]
    ok, output = mcp(hub, sub_token, "task_comment", task=explicit["number"], text="Progress")
    assert ok and f"parent:#{parent['number']}" in output
    ok, output = mcp(
        hub, sub_token, "task_add", title="Known by tool", board="work", parent=parent["number"]
    )
    assert ok and f"parent:#{parent['number']}" in output
    assert not any(c["title"] == "Refused" for c in sub.task_list(board="work"))


def test_child_asks_stay_in_needs_me_and_refresh_parent_count(hub, env):  # noqa: F811
    _token, principal, person, cookie, owner = env
    top = service(hub, principal)
    parent = top.task_add(title="Parent", board="work")
    _, _, sub = actor(hub, "worker", parent=principal.connection)
    child = sub.task_add(title="Pick the release", board="work")
    top.task_approve(child["number"])
    child = sub.ask(
        "Pick the release? Which approach should we take?",
        ["Now", "Later"],
        task=child["number"],
        context=REVIEW_CONTEXT,
    )
    assert child["parent"] == parent["number"]
    parent_view = owner.task_show(parent["number"])
    assert parent_view["children_need_you"] is True
    assert parent_view["children"] == {"done": 0, "total": 1}
    assert top.task_show(parent["number"])["children_need_you"] is False
    status, needs = api(hub, cookie, "GET", "/api/needs-me")
    assert status == 200 and [c["number"] for c in needs["asks"]] == [child["number"]]
    before = events.latest_seq(hub.db, hub.workspace)
    changes = owner.store.changes
    owner.decide(child["number"], choice="Now", then="done")
    updated = owner.task_show(parent["number"])
    assert updated["children"] == {"done": 1, "total": 1} and not updated["children_need_you"]
    assert updated["version"] == parent_view["version"]
    assert owner.store.changes > changes
    changed = events.after(hub.db, hub.workspace, before)
    assert any(
        e["data"].get("board") == "work" and e["data"].get("parent") == parent["number"]
        for e in changed
    )
    assert service(hub, person).needs_me()["count"] == 0


def test_parent_connections_require_same_workspace_active_parent_and_no_cycles(hub, env):  # noqa: F811
    _token, principal, _person, _cookie, _owner = env
    cid, _token, _sub = actor(hub, "worker", parent=principal.connection_id)
    row = hub.db.connection(cid)
    assert row["parent_id"] == principal.connection_id and row["started_by"] == "agent"
    assert hub.db.top_level_connection(cid, hub.workspace)["id"] == principal.connection_id
    actor(hub, principal.connection_id)  # A name that resembles an id cannot change ancestry.
    assert hub.db.top_level_connection(cid, hub.workspace)["id"] == principal.connection_id
    for parent in ("missing", "self"):
        with pytest.raises(ControlError):
            actor(hub, "self", parent=parent)
    other = hub.db.create_workspace("Other", hub.admin)
    foreign, _, _ = actor(hub, "foreign", workspace=other)
    with pytest.raises(ControlError):
        actor(hub, "cross-workspace", parent=foreign)
    with hub.db._lock:
        assert not hub.db._conn.in_transaction
    # Exercise persistent database guards through an independent client, rather
    # than issuing unlocked statements on the gateway's shared connection.
    with closing(sqlite3.connect(hub.db.path, timeout=5, isolation_level=None)) as probe:
        probe.execute("PRAGMA foreign_keys=ON")
        selection = "SELECT * FROM connections WHERE id IN (?,?) ORDER BY id"
        ids = (principal.connection_id, cid)
        before = probe.execute(selection, ids).fetchall()
        assert len(before) == 2
        for target in ids:
            with pytest.raises(sqlite3.IntegrityError, match="cycle"):
                probe.execute(
                    "UPDATE connections SET parent_id=?,started_by='agent' WHERE id=?",
                    (target, principal.connection_id),
                )
            assert probe.execute(selection, ids).fetchall() == before
        with pytest.raises(sqlite3.IntegrityError):
            probe.execute("UPDATE connections SET started_by='user' WHERE id=?", (cid,))
        assert probe.execute(selection, ids).fetchall() == before
    hub.db.revoke(cid, actor=hub.admin)
    with pytest.raises(ControlError, match="revoked"):
        actor(hub, "revoked-parent", parent=cid)


def test_parent_triggers_protect_fresh_non_admin_connections(hub):
    user = hub.db.create_user("fresh-ancestry@example.test")
    workspace = hub.db.create_workspace("Fresh ancestry", user)
    assert not hub.db.user(user)["is_admin"]
    policy = Policy.build(scopes=["memory:read"], read_wings=["shared"], trust="standard")
    parent, _ = hub.db.create_token(
        workspace_ref=workspace,
        user_ref=user,
        name="parent",
        client="test",
        policy=policy,
        actor=user,
    )
    child, _ = hub.db.create_token(
        workspace_ref=workspace,
        user_ref=user,
        name="child",
        client="test",
        policy=policy,
        actor=user,
        parent=parent,
    )
    with closing(sqlite3.connect(hub.db.path, timeout=5, isolation_level=None)) as probe:
        probe.execute("PRAGMA foreign_keys=ON")
        selection = "SELECT * FROM connections WHERE id IN (?,?) ORDER BY id"
        ids = (parent, child)
        before = probe.execute(selection, ids).fetchall()
        assert len(before) == 2
        with pytest.raises(sqlite3.IntegrityError):
            probe.execute("UPDATE connections SET started_by='user' WHERE id=?", (child,))
        assert probe.execute(selection, ids).fetchall() == before
        with pytest.raises(sqlite3.IntegrityError, match="cycle"):
            probe.execute(
                "UPDATE connections SET parent_id=?,started_by='agent' WHERE id=?", (child, parent)
            )
        assert probe.execute(selection, ids).fetchall() == before


def test_connection_listing_and_cli_expose_ancestry(hub, env, tmp_path, capsys):  # noqa: F811
    _token, principal, _person, cookie, _owner = env
    cid, _, _ = actor(hub, "worker", parent=principal.connection)
    status, payload = api(hub, cookie, "GET", "/api/connections")
    assert status == 200
    row = next(c for c in payload["connections"] if c["id"] == cid)
    assert row["parent_id"] == principal.connection_id and row["started_by"] == "agent"
    data = ["--data", str(tmp_path / "cli")]
    assert main(data + ["init", "--admin-email", "owner@example.com"]) == 0
    assert (
        main(data + ["token", "add", "parent", "--workspace", "personal", "--write-wing", "inbox"])
        == 0
    )
    assert (
        main(
            data
            + [
                "token",
                "add",
                "child",
                "--workspace",
                "personal",
                "--parent",
                "parent",
                "--write-wing",
                "inbox",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert main(data + ["token", "list"]) == 0
    listing = capsys.readouterr().out
    assert "started_by agent" in listing and "started_by user" in listing
    with sqlite3.connect(tmp_path / "cli" / "control.sqlite3") as conn:
        parent_id = conn.execute("SELECT id FROM connections WHERE name='parent'").fetchone()[0]
    assert f"parent {parent_id}" in listing


def test_oauth_connections_remain_top_level(hub, env):  # noqa: F811
    _token, _principal, person, _cookie, _owner = env
    with hub.db._lock, hub.db._tx() as conn:
        conn.execute(
            "INSERT INTO oauth_clients(client_id,client_name,redirect_uris,created_at) VALUES ('client','Claude','[]',0)"
        )
        client = conn.execute("SELECT * FROM oauth_clients WHERE client_id='client'").fetchone()
        oauth_store.create_code(
            conn,
            client,
            person,
            {
                "scope": "memory:read memory:write",
                "redirect_uri": "https://client.example/callback",
                "resource": "https://memory.example/mcp",
                "code_challenge": "challenge",
            },
        )
    row = next(c for c in hub.db.connections(hub.workspace) if c["auth"] == "oauth")
    assert row["parent_id"] is None and row["started_by"] == "user"


def test_existing_databases_migrate_without_backfilling_cards(tmp_path):
    path = tmp_path / "control.sqlite3"
    legacy = sqlite3.connect(path)
    legacy.executescript("""
        CREATE TABLE connections (
            id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, user_id TEXT NOT NULL,
            name TEXT NOT NULL, client TEXT NOT NULL, auth TEXT NOT NULL,
            token_hash TEXT UNIQUE, token_hint TEXT NOT NULL DEFAULT '', policy TEXT NOT NULL,
            created_by TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT,
            revoked_at TEXT, last_used_at TEXT, UNIQUE(workspace_id,name)
        );
        INSERT INTO connections(id,workspace_id,user_id,name,client,auth,policy,created_by,created_at)
        VALUES ('old','workspace','owner','old-agent','codex','pat','{}','owner','2020-01-01T00:00:00Z');
    """)
    legacy.close()
    for _ in range(2):
        db = ControlDB(str(path))
        row = db.connection("old")
        assert row["parent_id"] is None and row["started_by"] == "user"
        assert row["created_at"] == "2020-01-01T00:00:00Z"
        db.close()
    schema = Path("commonplace/boards/schema.sql").read_text()
    schema = "\n".join(line for line in schema.splitlines() if "parent_number" not in line)
    boards = sqlite3.connect(tmp_path / "boards.sqlite3")
    boards.executescript(schema)
    boards.execute(
        "INSERT INTO boards(id,slug,name,wing,stamp_user,stamp_trust,created_at) VALUES (1,'work','Work','shared','owner','trusted','2020')"
    )
    for number in range(3, 53):
        boards.execute(
            "INSERT INTO tasks(id,board_id,title,status,position,stamp_user,stamp_trust,created_at,updated_at,updated_by) VALUES (?,1,?,'todo',?,'owner','trusted','2020','2020','user:owner')",
            (number, f"Original {number}\n  words", number),
        )
    before = boards.execute("SELECT id,title,status,updated_at FROM tasks ORDER BY id").fetchall()
    boards.commit()
    boards.close()
    for _ in range(2):
        store = BoardStore(tmp_path)
        with store.connection() as conn:
            assert [
                tuple(r)
                for r in conn.execute("SELECT id,title,status,updated_at FROM tasks ORDER BY id")
            ] == before
            assert (
                conn.execute("SELECT count(*) FROM tasks WHERE parent_number IS NULL").fetchone()[0]
                == 50
            )


def test_invalid_ancestry_is_refused_instead_of_looping(hub, env):  # noqa: F811
    _token, principal, _person, _cookie, _owner = env
    cid, _, sub = actor(hub, "worker", parent=principal.connection)
    hub.db._conn.execute("DROP TRIGGER connections_parent_update")
    hub.db._conn.execute(
        "UPDATE connections SET parent_id=?,started_by='agent' WHERE id=?",
        (cid, principal.connection_id),
    )
    with pytest.raises(ParentError, match="cycle"):
        sub.task_add(title="Invalid ancestry", board="work")


def test_child_progress_rendering():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is needed for the child-progress render check")
    subprocess.run(
        [node, str(Path(__file__).with_name("commonplace_board_actors_render.mjs"))],
        check=True,
        timeout=10,
    )
