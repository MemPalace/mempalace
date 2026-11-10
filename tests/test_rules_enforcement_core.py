"""Behavioral contract for the additive rules core; no write enforcement yet."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from pathlib import Path

import pytest

from commonplace.boards import BoardService
from commonplace.boards.store import BoardStore, pr_repository
from commonplace.extensions import ToolError
from commonplace.rules import RuleStore, checks, render
from commonplace.rules.service import effective_rules
from tests import test_commonplace_rules as rule_tests
from tests._commonplace_helpers import hub_fixture  # noqa: F401
from tests.test_commonplace_boards import api, mcp
from tests.test_commonplace_rules import publish, service

env_fixture = pytest.fixture(name="env")(rule_tests.env.__wrapped__)


def snapshot(worker):
    return effective_rules(worker.ctx)


def served(worker):
    with worker.store.read() as conn:
        row = conn.execute(
            "SELECT * FROM agent_served WHERE agent=?", (worker.principal.actor,)
        ).fetchone()
        return dict(row) if row else None


def test_effective_service_rejects_explicit_selection_arguments(hub, env):
    for arguments in (
        {"board": None},
        {"board": "visible"},
        {"repo": None},
        {"repo": "example.test/org/app"},
        {"include_retired": False},
        {"include_retired": True},
        {"include_retired": None},
    ):
        with pytest.raises(ToolError, match="full set and takes only format"):
            env.worker.rules_get(effective=True, **arguments)
    assert not Path(env.worker.ctx.workspace_dir(), "rules.sqlite3").exists()
    assert env.worker.rules_get(board=None, repo=None, include_retired=False)["rules"] == []
    assert env.worker.rules_get(effective=True, format="md")["state"] == "served"


def test_authorized_effective_view_and_default_compatibility(hub, env):
    boards = BoardService(env.owner.ctx)
    for slug, wing in (("open", "shared"), ("secret", "private.notes"), ("old", "shared")):
        boards.board_add(slug=slug, wing=wing, repo=f"https://example.test/org/{slug}")
    with boards.store.transaction() as conn:
        conn.execute("UPDATE boards SET archived=1 WHERE slug='old'")
    for scope, ref, text in (
        ("global", None, "global"),
        ("board", "open", "visible board"),
        ("board", "secret", "private board"),
        ("board", "old", "archived board"),
        ("repo", "example.test/org/open", "visible repo"),
        ("repo", "example.test/org/secret", "private repo"),
        ("repo", "example.test/org/old", "archived repo"),
        ("repo", "example.test/org/orphan", "unlinked repo"),
    ):
        env.owner.create("*", text, "instruction", scope, ref)
    env.owner.create("conn:someone-else", "foreign", "limit")
    proposed = publish(env, text="unapproved")
    edited = env.owner.edit(proposed["id"], proposed["version"], text="owner text")
    publish(env, text="pending agent text")
    retired = env.owner.create("*", "retired", "style")
    env.owner.action(retired["id"], retired["version"], "retire")
    for trust in ("standard", "untrusted", "trusted"):
        token = hub.token(trust, scopes=["memory:read"], read_wings=["shared"], trust=trust)
        worker = service(hub, hub.gateway.authenticate(f"Bearer {token}"))
        result = snapshot(worker)
        expected = {"global", "visible board", "visible repo"}
        if trust == "trusted":
            expected.add("unlinked repo")
        assert {r["text"] for r in result["rules"]} == expected
        assert (
            worker.rules_get(repo="example.test/org/secret")["rules"][1]["text"] == "private repo"
        )
        assert (
            len(worker.rules_get(board="old")["rules"]) == 2
        )  # Default view still sees archived boards.
    own = snapshot(env.worker)["rules"]
    assert edited["id"] in {r["id"] for r in own}
    assert all(r["text"] != "pending agent text" and r["agent_text"] is None for r in own)
    for arguments in (
        {"board": "open"},
        {"repo": "example.test/org/open"},
        {"include_retired": False},
        {"agent": "conn:x"},
    ):
        ok, text = mcp(hub, env.token, "rules_get", effective=True, **arguments)
        assert not ok and "full set and takes only format" in text
    with pytest.raises(ToolError, match="for agent connections"):
        env.owner.rules_get(effective=True)


def test_per_agent_versions_lifecycle_and_served(hub, env):
    other = service(hub, hub.gateway.authenticate(f"Bearer {hub.owner_token('other')}"))
    _ = env.worker.store
    a, b = snapshot(env.worker)["rules_seq"], snapshot(other)["rules_seq"]
    assert 100_000_000 <= a <= 999_999_999
    proposal = publish(env)
    assert snapshot(env.worker)["rules_seq"] == a
    accepted = env.owner.action(proposal["id"], proposal["version"], "accept")
    a1 = snapshot(env.worker)["rules_seq"]
    assert a1 != a and snapshot(other)["rules_seq"] == b
    assert served(env.worker) is None
    env.worker.rules_get()
    env.worker.rules_changed(0)
    assert served(env.worker) is None
    env.owner.set_check("*", "warn")
    response = env.worker.rules_get(effective=True, format="md")
    assert response["reply"].endswith(f"rules_seq {a1}")
    before = served(env.worker)
    assert before["state"] == "served" and before["rules_seq"] == a1
    env.worker.rules_get(effective=True)
    assert served(env.worker) == before
    edited = env.owner.edit(accepted["id"], accepted["version"], text="owner wins")
    a2 = snapshot(env.worker)["rules_seq"]
    publish(env, text="pending")
    assert snapshot(env.worker)["rules_seq"] == a2 and served(env.worker)["rules_seq"] == a1
    moved = env.owner.set_agent(edited["id"], edited["version"] + 1, other.principal.actor)
    assert snapshot(env.worker)["rules_seq"] != a2 and snapshot(other)["rules_seq"] != b
    shared = env.owner.set_agent(moved["id"], moved["version"], "*")
    a3, b3 = snapshot(env.worker)["rules_seq"], snapshot(other)["rules_seq"]
    retired = env.owner.action(shared["id"], shared["version"], "retire")
    assert snapshot(env.worker)["rules_seq"] != a3 and snapshot(other)["rules_seq"] != b3
    env.owner.action(retired["id"], retired["version"], "restore")
    assert snapshot(env.worker)["rules_seq"] != a3


def test_visibility_reconciliation_triggers_and_restore(hub, env, tmp_path):
    _ = env.worker.store
    boards = BoardService(env.owner.ctx)
    boards.board_add(slug="one", wing="shared", repo="example.test/org/one")
    assert snapshot(env.worker)["seq"] == 0  # No scoped rules: visibility has no rule change.
    env.owner.create("*", "Scoped", "limit", "board", "one")
    current = snapshot(env.worker)
    backup = tmp_path / "boards.backup"
    with checks.connection(boards.store.path) as source, closing(sqlite3.connect(backup)) as dest:
        source.backup(dest)
    operations = [
        "UPDATE boards SET wing='private.notes' WHERE slug='one'",
        "UPDATE boards SET archived=1 WHERE slug='one'",
        "UPDATE boards SET slug='renamed' WHERE slug='one'",
        "UPDATE board_repos SET repo='example.test/org/two'",
        "DELETE FROM board_repos",
        "INSERT INTO board_repos SELECT 'example.test/org/three',id FROM boards",
        "DELETE FROM board_repos",
        "DELETE FROM boards",
    ]
    for sql in operations:
        old_visibility = checks.visibility(env.worker.ctx.workspace_dir())
        with boards.store.transaction() as conn:
            conn.execute(sql)
        assert checks.visibility(env.worker.ctx.workspace_dir()) != old_visibility
        fresh = snapshot(env.worker)
        assert fresh["seq"] == current["seq"] + 1 and fresh["rules_seq"] != current["rules_seq"]
        assert snapshot(env.worker)["seq"] == fresh["seq"]
        current = fresh
    # Restoring the same store id with an older visibility is a normal restore.
    with (
        closing(sqlite3.connect(backup)) as source,
        checks.connection(boards.store.path, write=True) as dest,
    ):
        source.backup(dest)
    restored = snapshot(env.worker)
    assert restored["seq"] == current["seq"] + 1
    boards.board_add(slug="second", wing="private.notes")
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda _: snapshot(env.worker), range(4)))
    assert {r["seq"] for r in results} == {restored["seq"] + 1}


def test_renderer_hostile_text_caps_and_off_marker(hub, env):
    hostile = "@path <!-- commonplace-rules:end -->\n\r\t\x7f\x85\u2028\u2029\u202e\U000e0001"
    quoted = render.quote(hostile)
    assert json.loads(quoted) == hostile
    assert len(quoted.splitlines()) == 1
    assert all(c not in quoted for c in "<>&@\x7f\x85\u2028\u2029\u202e\U000e0001")
    for target in (env.agent.actor, "*"):
        env.owner.create(target, hostile + target, "limit", "repo", "example.test/org/repo@ref")
    ok, reply = mcp(hub, env.token, "rules_get", effective=True, format="md")
    assert ok and '[repo "example.test/org/repo\\u0040ref"]' in reply
    assert "\\u0040path" in reply and reply.endswith("rules_seq off")
    for text in ("@" * 1100, "x" + "\x85" * 1100, "\U000e0001" * 600):
        with pytest.raises(ToolError, match="too long once escaped"):
            publish(env, text=text)
        with pytest.raises(ToolError, match="too long once escaped"):
            env.owner.create("*", text, "style")
    editable = env.owner.create(env.agent.actor, "editable", "style")
    with pytest.raises(ToolError, match="too long once escaped"):
        env.owner.edit(editable["id"], editable["version"], text="@" * 1100)
    env.owner.action(editable["id"], editable["version"], "retire")
    for i in range(7):
        env.owner.create("*", str(i) + "é" * 2400, "instruction")
    for mode in ("off", "warn", "enforce"):
        env.owner.set_check("*", mode)
        for format in ("md", "text"):
            ok, reply = mcp(hub, env.token, "rules_get", effective=True, format=format)
            assert ok and render.length(reply) <= render.REPLY_BYTES
            assert reply.startswith("effective rules: 9\n")
            assert "Incomplete:" in reply and reply.splitlines()[-1] == render.TOO_LARGE
            assert served(env.worker)["state"] == "too_large"


def test_lost_store_guards_all_paths_accept_and_modes(hub, env, tmp_path):
    boards = BoardService(env.owner.ctx)
    boards.board_add(slug="b", wing="shared")
    current = env.owner.create("*", "Keep this", "limit")
    env.owner.set_check("*", "enforce")
    for name in ("rules", "boards"):
        directory = Path(env.worker.ctx.workspace_dir())
        path = directory / f"{name}.sqlite3"
        saved = tmp_path / f"{name}.sqlite3"
        with checks.connection(path) as source, closing(sqlite3.connect(saved)) as dest:
            source.backup(dest)
        for failure in ("missing", "replaced", "no_id"):
            if failure == "missing":
                path.unlink()
            elif failure == "replaced":
                path.unlink()
                (RuleStore if name == "rules" else BoardStore)(directory)
            else:
                with checks.connection(path, write=True) as conn:
                    conn.execute("DELETE FROM meta WHERE key='store_id'")
            for mode in ("off", "warn", "enforce"):
                env.owner.set_check("*", mode)
                assert mcp(hub, env.token, "rules_get", effective=True) == (True, checks.STORE_LOST)
            for tool, args in (
                ("rules_get", {}),
                ("rules_get", {"format": "md"}),
                ("rules_get", {"effective": True}),
                ("rules_changed", {"since": 0}),
            ):
                assert mcp(hub, env.token, tool, **args) == (True, checks.STORE_LOST)
            ok, reply = mcp(hub, env.token, "rules_publish", rules=[])
            assert not ok and checks.LOST_ERROR in reply
            for method, url, body in (
                ("GET", "/api/rules", None),
                ("GET", "/api/rules/count", None),
                ("GET", f"/api/rules/{current['id']}/history", None),
                ("POST", "/api/rules", {"agent": "*", "text": "bad", "kind": "limit"}),
                ("PATCH", f"/api/rules/{current['id']}", {"version": 1, "text": "bad"}),
                ("POST", f"/api/rules/{current['id']}/action", {"version": 1, "action": "retire"}),
                (
                    "POST",
                    f"/api/rules/{current['id']}/suggestions",
                    {"text": "bad", "from_agent": "x"},
                ),
                ("POST", "/api/rules/suggestions/1", {"action": "dismiss"}),
            ):
                status, result = api(hub, env.cookie, method, url, body)
                assert status == 409 and result["store_lost"] is True, (method, url, result)
            assert (
                api(hub, env.cookie, "PATCH", "/api/rules/checks", {"agent": "*", "mode": "warn"})[
                    0
                ]
                == 200
            )
            assert checks.mode(hub.gateway, env.agent) == "warn"
            if failure == "missing":
                assert not path.exists()  # Guards never recreate missing stores.
            # Put the original back without changing its identity.
            with closing(sqlite3.connect(saved)) as source, closing(sqlite3.connect(path)) as dest:
                source.backup(dest)
            assert snapshot(env.worker)["rules"]
        with checks.connection(path, write=True) as conn:
            conn.execute("UPDATE meta SET value=value+1 WHERE key='store_id'")
        assert api(hub, env.cookie, "POST", "/api/rules/store/accept", {}) == (
            200,
            {"accepted": True},
        )
        assert snapshot(env.worker)["rules"]
    assert any(
        r["kind"] == "rule.store_lost" for r in hub.db._conn.execute("SELECT kind FROM events")
    )


def test_readonly_missing_failures_and_served_write_failure(hub, env, monkeypatch):
    directory = Path(env.worker.ctx.workspace_dir())
    result = snapshot(env.worker)
    assert result["empty"] and not (directory / "rules.sqlite3").exists()
    assert checks.mode(hub.gateway, env.agent) == "off"
    ok, reply = mcp(hub, env.token, "rules_get", effective=True)
    assert ok and reply.startswith("effective rules: 0\n")
    assert served(env.worker)["state"] == "served"
    env.owner.create("*", "private text must never appear on failure", "limit")
    for target, attribute in ((checks, "reconcile"), (checks, "record_served")):
        with monkeypatch.context() as patch:
            patch.setattr(
                target,
                attribute,
                lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("locked")),
            )
            ok, reply = mcp(hub, env.token, "rules_get", effective=True)
            assert ok and reply.startswith("rules_seq unavailable:") and "private text" not in reply
    with monkeypatch.context() as patch:
        patch.setattr(
            hub.db,
            "rule_checks",
            lambda *a: (_ for _ in ()).throw(sqlite3.OperationalError("broken")),
        )
        assert checks.mode(hub.gateway, env.agent) == "unknown"
        assert mcp(hub, env.token, "rules_get", effective=True)[1].startswith(
            "rules_seq unavailable:"
        )
    with monkeypatch.context() as patch:
        patch.setattr(
            hub.db,
            "rule_stores",
            lambda *a: (_ for _ in ()).throw(sqlite3.OperationalError("broken")),
        )
        assert mcp(hub, env.token, "rules_get", effective=True)[1].startswith(
            "rules_seq unavailable:"
        )
    # A corrupt boards store also fails the complete view rather than dropping scoped rules.
    boards_path = directory / "boards.sqlite3"
    boards_path.write_bytes(b"not sqlite")
    assert mcp(hub, env.token, "rules_get", effective=True)[1].startswith("rules_seq unavailable:")


def test_owner_checks_permissions_kill_switch_and_accept_missing(hub, env, monkeypatch):
    assert (
        api(hub, env.token, "PATCH", "/api/rules/checks", {"agent": "*", "mode": "warn"})[0] == 403
    )
    assert env.owner.set_check("*", "warn")["checks"] == {"*": "warn"}
    assert checks.mode(hub.gateway, env.agent) == "warn"
    env.owner.set_check(env.agent.actor, "off")
    assert checks.mode(hub.gateway, env.agent) == "off"
    env.owner.set_check(env.agent.actor, "default")
    assert checks.mode(hub.gateway, env.agent) == "warn"
    token = hub.token(
        "cannot-read", tools=["rules_publish"], scopes=["memory:write"], write_wing="inbox"
    )
    assert token
    assert api(
        hub,
        env.cookie,
        "PATCH",
        "/api/rules/checks",
        {"agent": "conn:cannot-read", "mode": "enforce"},
    ) == (400, {"error": "this connection cannot read rules"})
    monkeypatch.setenv("COMMONPLACE_RULES_CHECK", "off")
    checks.configure(hub.gateway)
    assert checks.mode(hub.gateway, env.agent) == "off"
    assert any(r["action"] == "rules.check" for r in hub.db.audit_entries())
    _ = env.worker.store
    Path(env.worker.store.path).unlink()
    assert api(hub, env.cookie, "POST", "/api/rules/store/accept", {})[0] == 200
    assert hub.db.rule_stores(hub.workspace)["rules_store_id"] is None
    assert mcp(hub, env.token, "rules_get", effective=True)[1].startswith("effective rules: 0\n")


def test_check_overrides_canonicalize_ids_and_audit_only_ids(hub, env):
    row = hub.db.connection(env.agent.connection, hub.workspace)
    key = "conn:" + row["name"]
    assert env.owner.set_check(key, "warn")["checks"] == {key: "warn"}
    assert env.owner.set_check("conn:" + row["id"], "enforce")["checks"] == {key: "enforce"}
    assert checks.mode(hub.gateway, env.agent) == "enforce"
    assert env.owner.set_check("conn:" + row["id"], "default")["checks"] == {}
    env.owner.set_check("*", "warn")
    audits = [r for r in hub.db.audit_entries() if r["action"] == "rules.check"]
    assert [json.loads(r["target_ids"]) for r in audits] == [
        [hub.workspace],
        [row["id"]],
        [row["id"]],
        [row["id"]],
    ]
    changes = [
        json.loads(r[0])
        for r in hub.db._conn.execute(
            "SELECT data FROM events WHERE kind='rule.check' ORDER BY seq"
        )
    ]
    assert [r["agent"] for r in changes] == [key, key, key, "*"]


def test_served_uses_one_transaction_then_none_and_guards_fast_path(hub, env, monkeypatch):
    env.owner.create("*", "A rule", "limit")
    snapshot(env.worker)  # Finish reconciliation before counting served writes.
    statements = []
    original = checks.connection

    @contextmanager
    def connection(path, **kwargs):
        with original(path, **kwargs) as conn:
            if kwargs.get("write") and Path(path).name == "rules.sqlite3":
                conn.set_trace_callback(statements.append)
            yield conn

    monkeypatch.setattr(checks, "connection", connection)
    env.worker.rules_get(effective=True)
    assert statements[0] == "BEGIN IMMEDIATE"
    assert sum(sql == "BEGIN IMMEDIATE" for sql in statements) == 1
    assert sum(sql == "COMMIT" for sql in statements) == 1
    assert next(i for i, sql in enumerate(statements) if "CREATE TABLE" in sql) > 0
    statements.clear()
    env.worker.rules_get(effective=True)
    assert statements == []
    prior = served(env.worker)
    Path(env.worker.store.path).unlink()
    with pytest.raises(checks.StoreLost):
        checks.record_served(env.worker.ctx, prior["rules_seq"], prior["state"], prior=prior)


def test_too_large_alerts_only_on_transition(hub, env):
    rules = [env.owner.create("*", str(i) + "é" * 2400, "instruction") for i in range(7)]

    def count():
        return hub.db._conn.execute(
            "SELECT count(*) FROM events WHERE kind='rule.too_large'"
        ).fetchone()[0]

    assert env.worker.rules_get(effective=True)["state"] == "too_large"
    assert env.worker.rules_get(effective=True)["state"] == "too_large"
    assert count() == 1
    retired = env.owner.action(rules[-1]["id"], rules[-1]["version"], "retire")
    retired2 = env.owner.action(rules[-2]["id"], rules[-2]["version"], "retire")
    retired3 = env.owner.action(rules[-3]["id"], rules[-3]["version"], "retire")
    assert env.worker.rules_get(effective=True)["state"] == "served"
    for rule in (retired, retired2, retired3):
        env.owner.action(rule["id"], rule["version"], "restore")
    assert env.worker.rules_get(effective=True)["state"] == "too_large"
    assert count() == 2


def test_unavailable_replies_reveal_exception_class_only(hub, env, monkeypatch):
    env.owner.create("*", "A private rule", "limit")
    private_marker = "/private-marker/config/token"

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError(private_marker)

    for attribute in ("reconcile", "record_served"):
        with monkeypatch.context() as patch:
            patch.setattr(checks, attribute, broken)
            reply = env.worker.rules_get(effective=True)["reply"]
            assert reply.startswith("rules_seq unavailable: OperationalError;")
            assert private_marker not in reply and "A private rule" not in reply
    with monkeypatch.context() as patch:
        patch.setattr(checks, "guard", broken)
        ok, reply = mcp(hub, env.token, "rules_get", effective=True)
        assert ok and reply.startswith("rules_seq unavailable: OperationalError;")
        assert private_marker not in reply and "A private rule" not in reply


def test_internal_call_is_thread_local_and_nested_memory_is_marked(hub, env, monkeypatch):
    assert not checks.is_internal_call()
    with checks.internal_call():
        assert checks.is_internal_call()
        with checks.internal_call():
            assert checks.is_internal_call()
        with ThreadPoolExecutor(1) as pool:
            assert pool.submit(checks.is_internal_call).result() is False
    assert not checks.is_internal_call()
    seen = []

    def call(principal, request):
        seen.append(checks.is_internal_call())
        return {"result": {"content": [{"type": "text", "text": "{}"}]}}

    monkeypatch.setattr(hub.gateway.mediator, "_tools_call", call)
    hub.gateway.call_memory_tool(env.agent, "mempalace_list_wings", {})
    assert seen == [True]
    from commonplace.handoff.wiring import HandoffContext

    def handle(principal, request):
        assert checks.is_internal_call()
        return {"result": {"content": []}}

    monkeypatch.setattr(hub.gateway.mediator, "handle", handle)
    HandoffContext(hub.gateway, hub.db, Path(env.worker.ctx.workspace_dir())).save_fact(
        env.agent, {}
    )


def test_rules_seq_strip_mcp_and_rest(hub, env, monkeypatch):
    assert mcp(hub, env.token, "rules_publish", rules=[], rules_seq="123")[0]
    assert mcp(hub, env.token, "board_add", slug="strip", wing="shared", rules_seq=123)[0]
    status, board = api(
        hub,
        env.cookie,
        "POST",
        "/api/boards",
        {"slug": "rest", "wing": "shared", "rules_seq": "123"},
    )
    assert status == 201, board
    status, card = api(
        hub, env.cookie, "POST", "/api/boards/rest/cards", {"title": "Card", "rules_seq": 123}
    )
    assert status == 201, card
    number = card["number"]
    assert (
        api(
            hub,
            env.cookie,
            "PATCH",
            f"/api/cards/{number}",
            {"version": card["version"], "title": "Updated", "rules_seq": 123},
        )[0]
        == 200
    )
    assert (
        api(
            hub,
            env.cookie,
            "POST",
            f"/api/cards/{number}/comments",
            {"text": "Comment", "rules_seq": 123},
        )[0]
        == 200
    )
    assert (
        api(hub, env.cookie, "POST", "/api/boards/rest/chat", {"text": "Chat", "rules_seq": 123})[0]
        == 201
    )
    assert (
        api(hub, env.cookie, "PATCH", "/api/boards/rest", {"coordinator": None, "rules_seq": 123})[
            0
        ]
        == 200
    )
    from commonplace import memory_tools

    def save(ctx, args):
        assert "rules_seq" not in args and args["text"] == "Remember exactly"
        return "stored", None, ""

    def fact(ctx, args):
        assert "rules_seq" not in args and args["subject"] == "Person"
        return {"triple_id": 1}

    monkeypatch.setattr(memory_tools, "_save", save)
    monkeypatch.setattr(memory_tools, "_file_fact", fact)
    assert (
        api(
            hub,
            env.cookie,
            "POST",
            "/api/memory",
            {"text": "Remember exactly", "theme": "shared", "rules_seq": 123},
        )[0]
        == 201
    )
    status, result = api(
        hub,
        env.cookie,
        "POST",
        "/api/facts",
        {
            "subject": "Person",
            "predicate": "likes",
            "object": "tea",
            "theme": "shared",
            "rules_seq": 123,
        },
    )
    assert status == 201, result
    forwarded = []

    def post(principal, request):
        forwarded.append(request["params"]["arguments"])
        return {
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {"content": [{"type": "text", "text": "{}"}]},
        }

    monkeypatch.setattr(hub.gateway.mediator.workers, "post", post)
    assert mcp(hub, env.token, "mempalace_get_aaak_spec", rules_seq="123")[0]
    assert forwarded == [{}]


def test_migration_deploy_backfill_and_generation_copy(hub, env, tmp_path, monkeypatch):
    directory = Path(env.worker.ctx.workspace_dir())
    boards = BoardService(env.owner.ctx)
    boards.board_add(slug="one", wing="shared")
    card = boards.task_add(board="one", title="PR")
    card = boards.task_update(card["number"], pr="https://example.test/org/repo/pulls/42")
    rule_store = env.worker.store
    with boards.store.transaction() as conn:
        conn.execute("UPDATE tasks SET pr_repo=NULL WHERE id=?", (card["number"],))
        conn.execute("DELETE FROM meta WHERE key IN ('visibility','store_id')")
    with rule_store.connection() as conn:
        conn.execute("DROP TABLE agent_served")
        conn.execute("DROP TABLE agent_seq")
        conn.execute("DELETE FROM meta WHERE key IN ('boards_visibility_seen','store_id')")
    with hub.db._lock:
        hub.db._conn.execute("DELETE FROM rule_stores")
    checks.deploy(hub.gateway)
    bound = hub.db.rule_stores(hub.workspace)
    assert bound["rules_store_id"] and bound["boards_store_id"]
    baseline = snapshot(env.worker)
    checks.deploy(hub.gateway)
    assert snapshot(env.worker)["rules_seq"] == baseline["rules_seq"]
    assert "pr_repo" not in boards.task_show(card["number"])
    with boards.store.connection() as conn:
        assert conn.execute("SELECT pr_repo FROM tasks").fetchone()[0] == "example.test/org/repo"
    from commonplace.store_switch import _copy_tree

    generation = tmp_path / "generation"
    _copy_tree(directory, generation)
    monkeypatch.setattr(hub.gateway, "workspace_dir", lambda _: str(generation))
    assert snapshot(env.worker)["rules_seq"] == baseline["rules_seq"]
    checks.guard(hub.db, hub.workspace, generation)
    monkeypatch.setattr(hub.gateway, "workspace_dir", lambda _: str(directory))
    checks.guard(hub.db, hub.workspace, directory)
    assert snapshot(env.worker)["rules_seq"] == baseline["rules_seq"]
    # A failed deployment never writes a partial binding row.
    with hub.db._lock:
        hub.db._conn.execute("DELETE FROM rule_stores")
    with monkeypatch.context() as patch:
        patch.setattr(
            checks, "migrate", lambda *a: (_ for _ in ()).throw(sqlite3.OperationalError("locked"))
        )
        checks.deploy(hub.gateway)
    assert hub.db.rule_stores(hub.workspace) is None
    checks.deploy(hub.gateway)
    assert hub.db.rule_stores(hub.workspace)
    for url in (
        "https://example.test/org/repo/pull/1",
        "https://EXAMPLE.TEST/org/repo/pulls/2",
        "https://example.test/org/repo/-/merge_requests/3",
    ):
        assert pr_repository(url) == "example.test/org/repo"


def test_deploy_retries_partial_binding_after_board_request(hub, env, monkeypatch):
    directory = Path(env.worker.ctx.workspace_dir())
    RuleStore(directory)
    with monkeypatch.context() as patch:
        patch.setattr(
            checks, "migrate", lambda *a: (_ for _ in ()).throw(sqlite3.OperationalError("locked"))
        )
        checks.deploy(hub.gateway)
    assert hub.db.rule_stores(hub.workspace) is None
    BoardService(env.owner.ctx).board_add(slug="partial", wing="shared")
    bound = hub.db.rule_stores(hub.workspace)
    assert bound["boards_store_id"] is not None and bound["rules_store_id"] is None
    checks.deploy(hub.gateway)
    assert hub.db.rule_stores(hub.workspace)["rules_store_id"] == checks.meta(
        directory / "rules.sqlite3", "store_id"
    )


def test_deploy_retry_never_opens_bound_replacement(hub, env, monkeypatch):
    directory = Path(env.worker.ctx.workspace_dir())
    env.owner.create("*", "Original", "limit")
    bound_id = hub.db.rule_stores(hub.workspace)["rules_store_id"]
    path = directory / "rules.sqlite3"
    path.unlink()
    RuleStore(directory)
    with checks.connection(path, write=True) as conn:
        conn.execute("DROP TABLE agent_seq")
        conn.execute("DROP TABLE agent_served")
        conn.execute("DELETE FROM meta WHERE key='boards_visibility_seen'")
    before = path.read_bytes()
    BoardStore(directory)  # PRESENT unbound boards forces the deploy retry.
    assert hub.db.rule_stores(hub.workspace)["boards_store_id"] is None
    original = checks.connection
    opened = []

    @contextmanager
    def connection(target, **kwargs):
        opened.append(Path(target).name)
        with original(target, **kwargs) as conn:
            yield conn

    with monkeypatch.context() as patch:
        patch.setattr(checks, "connection", connection)
        checks.deploy(hub.gateway)
    assert "rules.sqlite3" not in opened
    assert path.read_bytes() == before
    bound = hub.db.rule_stores(hub.workspace)
    assert bound["rules_store_id"] == bound_id and bound["boards_store_id"] is not None
    with pytest.raises(checks.StoreLost):
        checks.guard(hub.db, hub.workspace, directory)


def test_deploy_logs_workspace_enumeration_failure(hub, monkeypatch, caplog):
    def broken():
        raise sqlite3.OperationalError("enumeration failed")

    monkeypatch.setattr(hub.db, "workspaces", broken)
    checks.deploy(hub.gateway)
    assert "rules store deployment enumeration failed" in caplog.text


def test_second_board_open_does_not_write_store_bindings(hub, env):
    BoardService(env.owner.ctx).board_add(slug="first", wing="shared")
    statements = []
    hub.db._conn.set_trace_callback(statements.append)
    try:
        BoardService(env.owner.ctx)
    finally:
        hub.db._conn.set_trace_callback(None)
    assert not [
        sql
        for sql in statements
        if "rule_stores" in sql and sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
    ]


def test_pr_repo_index_migrates_old_column_and_backfills_later_writes(hub, env):
    boards = BoardService(env.owner.ctx)
    boards.board_add(slug="legacy", wing="shared")
    card = boards.task_add(board="legacy", title="Legacy PR")
    with boards.store.transaction() as conn:
        conn.execute("DROP INDEX tasks_pending_pr_repo")
        conn.execute("ALTER TABLE tasks DROP COLUMN pr_repo")
        conn.execute(
            "UPDATE tasks SET pr_url=? WHERE id=?",
            ("https://example.test/org/one/pull/1", card["number"]),
        )
    store = BoardStore(env.worker.ctx.workspace_dir())
    with store.transaction() as conn:
        assert conn.execute("SELECT pr_repo FROM tasks").fetchone()[0] == "example.test/org/one"
        predicate = "(pr_repo IS NULL OR pr_repo='') AND pr_url IS NOT NULL AND pr_url!=''"
        plan = conn.execute("EXPLAIN QUERY PLAN SELECT id,pr_url FROM tasks WHERE " + predicate)
        assert "tasks_pending_pr_repo" in " ".join(str(tuple(row)) for row in plan)
        conn.execute(
            "UPDATE tasks SET pr_repo='',pr_url=? WHERE id=?",
            ("https://demo:placeholder@example.test:8443/org/two/pull/2", card["number"]),
        )
    store = BoardStore(env.worker.ctx.workspace_dir())
    with store.connection() as conn:
        assert conn.execute("SELECT pr_repo FROM tasks").fetchone()[0] == "example.test/org/two"


def test_pr_repo_backfill_survives_unindexable_urls(hub, env):
    boards = BoardService(env.owner.ctx)
    boards.board_add(slug="backfill", wing="shared")
    urls = [
        "https://example.test/" + "a/" * 600 + "pull/1",
        "https://example.test/org/repo/pull/²?x/pull/1",
        "https://example.test/org/repo/pull/7",
    ]
    numbers = []
    for i, url in enumerate(urls):
        created = boards.task_add(board="backfill", title=f"PR {i}")
        updated = boards.task_update(created["number"], pr=url)
        assert updated["pr_url"] == url
        numbers.append(created["number"])
    for _ in range(2):
        store = BoardStore(env.worker.ctx.workspace_dir())
        with store.connection() as conn:
            for i, (number, url) in enumerate(zip(numbers, urls)):
                row = conn.execute(
                    "SELECT pr_url,pr_repo FROM tasks WHERE id=?", (number,)
                ).fetchone()
                assert row is not None
                assert row["pr_url"].encode("utf-8") == url.encode("utf-8")
                assert row["pr_repo"] == (None if i < 2 else "example.test/org/repo")


@pytest.mark.parametrize("key_length", [1000, 1001])
def test_pr_repository_key_length_boundary(key_length):
    suffix = "/org/repo"
    key = "h" * (key_length - len(suffix)) + suffix
    assert len(key) == key_length
    assert pr_repository("https://" + key + "/pull/1") == (key if key_length == 1000 else None)


@pytest.mark.parametrize(
    "url,repo",
    [
        ("https://example.test/org/pull/pull/1", "example.test/org/pull"),
        ("https://example.test/group/org/repo/-/merge_requests/2", "example.test/group/org/repo"),
        ("https://demo:placeholder@example.test/org/repo/pull/3", "example.test/org/repo"),
        ("https://example.test:8443/org/repo/pull/3", "example.test/org/repo"),
        ("https://demo@/org/repo/pull/3", None),
        pytest.param(
            "https://example.test/" + "a/" * 600 + "pull/1",
            None,
            id="unindexable-long-path",
        ),
        pytest.param(
            "https://" + "h" * 1005 + "/org/repo/pull/1",
            None,
            id="unindexable-long-host",
        ),
        pytest.param(
            "https://example.test/org/repo/pull/²?x/pull/1",
            None,
            id="unindexable-superscript-query",
        ),
        pytest.param(
            "https://example.test/org/repo/pull/²#/pull/1",
            None,
            id="unindexable-superscript-fragment",
        ),
        # This case exercises the default integer conversion digit limit.
        pytest.param(
            "https://example.test/org/repo/pull/" + "9" * 5000 + "?x/pull/1",
            None,
            id="unindexable-digit-limit",
        ),
        ("https://[invalid/pull/1", None),
        ("https://example.test/org/repo/pull/0", None),
        ("https://example.test/org/repo", None),
        (None, None),
    ],
)
def test_pr_repository_backfill_tolerates_legacy_values(url, repo):
    assert pr_repository(url) == repo
