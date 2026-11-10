"""Independent worker processes keep fact data and citations workspace-local."""

import json
import multiprocessing
import os
import secrets
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from commonplace import sessions
from commonplace.gateway import WorkerClient
from commonplace.handoff.facts import VERSION
from commonplace.policy import Policy
from commonplace.workers import Worker
from tests.test_card228_thread_mutations import SCOPES, call, store_thread, token
from tests.test_imported_history_search import (
    functional_worker_transport,  # noqa: F401
    mirror_record,
    private_mirror,
    refused,
    shared_read_control,
    world,  # noqa: F401
)

pytestmark = pytest.mark.usefixtures("functional_worker_transport")


def _serve_worker(directory, secret, ready):
    # Spawned workers use the suite's deterministic embedding, never a shared palace.
    from tests.conftest import _stable_embedding_function_for_tests

    patch = pytest.MonkeyPatch()
    embedding = _stable_embedding_function_for_tests.__wrapped__(
        SimpleNamespace(module=SimpleNamespace(__name__=__name__)), patch
    )
    next(embedding)
    root = Path(directory)
    os.environ["MEMPALACE_PALACE_PATH"] = str(root / "palace")
    os.environ["MEMPALACE_CONFIG_DIR"] = str(root / "config")
    os.environ["MEMPALACE_MCP_HTTP_TOKEN"] = secret
    os.environ["MEMPALACE_MCP_TRUSTED_IDENTITY"] = "1"
    from mempalace import mcp_server
    from mempalace.config import MempalaceConfig

    mcp_server._config = MempalaceConfig(config_dir=str(root / "config"))
    server = mcp_server._build_http_server("127.0.0.1", 0)
    ready.send(server.server_address[1])
    ready.close()
    server.serve_forever()


class IsolatedLauncher:
    def start(self, workspace_id, workspace_dir):
        for name in ("palace", "config", "home"):
            (workspace_dir / name).mkdir(parents=True, exist_ok=True)
        secret = secrets.token_urlsafe(32)
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe(duplex=False)
        process = context.Process(target=_serve_worker, args=(str(workspace_dir), secret, child))
        process.start()
        child.close()
        try:
            if not parent.poll(30):
                raise RuntimeError("isolated test worker did not start")
            port = parent.recv()
        except BaseException:
            process.terminate()
            process.join(10)
            raise
        finally:
            parent.close()

        def stop():
            process.terminate()
            process.join(10)
            if process.is_alive():
                process.kill()
                process.join(10)

        return Worker(
            workspace_id, "127.0.0.1", port, secret, stop, alive=process.is_alive, pid=process.pid
        )


@pytest.fixture
def two_workspaces(world, monkeypatch):  # noqa: F811
    y = world
    # The shared in-process launcher deliberately supports only one palace.
    # Production subprocesses provide independent globals, configs and SQLite files.
    monkeypatch.setattr(y.gateway.supervisor, "launcher", IsolatedLauncher())
    y.gateway.supervisor.get(y.me["workspace"])
    owner = token(y)
    text = "zqworkspace synthetic private original words"
    ids = private_mirror(y, text)
    record = mirror_record(y, ids, text, owner)
    assert record.get("drawer_id"), record
    record["source_file"] = record["metadata"]["source_file"]
    assert record["source_file"].startswith(f"handoff:{record['room']}#")
    db = y.client.db
    other = db.create_user("workspace-owner@example.test")
    xid = db.create_workspace("separate", other)
    db.add_member(xid, y.me["user"], "member", actor=other)
    x = SimpleNamespace(**{**vars(y), "me": {**y.me, "workspace": xid}})
    y.gateway.supervisor.get(xid)
    # Same owner-issued, member-bound token path as CLI token add --user.
    _, actor = db.create_token(
        workspace_ref=xid,
        user_ref=y.me["user"],
        actor=other,
        name="synthetic-x-member",
        client="generic",
        policy=Policy.build(
            profile="full",
            trust="trusted",
            scopes=[s for s in SCOPES if not s.startswith("private:")],
            read_wings=["*"],
            write_wings=["*"],
        ),
    )
    _, xowner = db.create_token(
        workspace_ref=xid,
        user_ref=other,
        actor=other,
        name="synthetic-x-owner",
        client="generic",
        policy=Policy.build(
            profile="full", trust="trusted", scopes=SCOPES, read_wings=["*"], write_wings=["*"]
        ),
    )
    assert db.role(xid, y.me["user"]) == "member"
    assert db.role(y.me["workspace"], y.me["user"]) == "owner"
    fy = call(
        y,
        owner,
        "kg_save",
        subject="WorkspaceFact",
        predicate="port",
        object="old",
        wing="shared",
        valid_from="2026-01-01",
    )
    fx = call(
        x,
        actor,
        "kg_save",
        subject="WorkspaceFact",
        predicate="port",
        object="visible",
        wing="shared",
    )
    assert fy.get("success") and fx.get("success"), (fy, fx)
    assert any(
        f.get("object") == "visible"
        for f in call(x, actor, "kg_query", entity="WorkspaceFact").get("facts", [])
    )
    shared_read_control(x, actor, owner=xowner)
    roots = [Path(y.gateway.supervisor.workspace_dir(z.me["workspace"])) for z in (x, y)]
    assert roots[0] != roots[1] and all(
        (p / "palace" / "knowledge_graph.sqlite3").exists() for p in roots
    )
    return SimpleNamespace(
        x=x,
        y=y,
        actor=actor,
        owner=owner,
        xowner=xowner,
        record=record,
        room=record["room"],
        fy=fy,
        roots=roots,
    )


def yfacts(c):
    return call(c.y, c.owner, "kg_query", entity="WorkspaceFact")["facts"]


def xfacts(c):
    return call(c.x, c.actor, "kg_query", entity="WorkspaceFact")


def mutation(c, tool, **kwargs):
    args = dict(subject="WorkspaceFact", predicate="port", wing="shared")
    args.update(
        dict(old_object="old", new_object="new", at="2026-02-01")
        if tool == "kg_supersede"
        else dict(object="old")
    )
    args.update(kwargs)
    return call(c.x, c.actor, tool, **args)


def test_cw_a01_invalidate_in_x_leaves_y_fact_open(two_workspaces):
    c = two_workspaces
    mutation(c, "kg_invalidate")
    assert any(f["object"] == "old" and f["current"] for f in yfacts(c))


def test_cw_a02_supersede_in_x_leaves_y_fact_open(two_workspaces):
    c = two_workspaces
    mutation(c, "kg_supersede")
    assert any(f["object"] == "old" and f["current"] for f in yfacts(c))


@pytest.mark.parametrize("tool", ["kg_invalidate", "kg_supersede"])
def test_cw_a01_a02_y_thread_citation_stays_in_y(two_workspaces, tool):
    c = two_workspaces
    cited = call(
        c.y,
        c.owner,
        "kg_save",
        subject="WorkspaceFact",
        predicate="port",
        object="old",
        wing="shared",
        source_thread=c.room,
        valid_from="2026-01-01",
    )
    assert cited.get("success") and c.room in json.dumps(yfacts(c))
    mutation(c, tool)
    assert any(f["object"] == "old" and f["current"] for f in yfacts(c))
    assert c.room in json.dumps(yfacts(c))


@pytest.mark.parametrize("mode", ["same_value", "single_valued"])
def test_cw_a03_kg_save_in_x_does_not_merge_y_fact(two_workspaces, mode):
    c = two_workspaces
    before = yfacts(c)
    mutation(c, "kg_save", single_valued=mode == "single_valued")
    assert yfacts(c) == before


def test_cw_a04_tool_argument_cannot_select_workspace(two_workspaces):
    c = two_workspaces
    mutation(c, "kg_invalidate", workspace=c.y.me["workspace"])
    assert any(f["object"] == "old" and f["current"] for f in yfacts(c))


def test_cw_a05_delete_by_source_in_x_leaves_y_drawers(two_workspaces):
    c = two_workspaces
    source = c.record["source_file"]
    call(
        c.x,
        c.actor,
        "add_drawer",
        wing="shared",
        room="notes",
        content="different synthetic words",
        source_file=source,
    )
    refused(c.x, c.actor, "mempalace_delete_by_source", source_file=source, dry_run=False)
    assert "zqworkspace" in call(c.y, c.owner, "get_drawer", drawer_id=c.record["drawer_id"]).get(
        "content", ""
    )


def test_cw_a06_extraction_skips_foreign_stored_thread(two_workspaces, monkeypatch):
    c = two_workspaces
    ctx = c.y.gateway.handoff
    store = ctx.store_for(c.y.me["workspace"])
    stored = store.get(c.room)
    before = store.fact_cursor(c.room, VERSION)
    calls = []
    monkeypatch.setattr(ctx.service, "save_fact", lambda *a: calls.append(a))
    principal = sessions.person_principal(c.x.client.db, c.x.me["user"], c.x.me["workspace"])
    ctx.service._extract_later(principal, stored)
    ctx.service.wait_for_facts(30)
    assert not calls and store.fact_cursor(c.room, VERSION) == before


def test_cw_a07_worker_routing_follows_principal_workspace(two_workspaces, monkeypatch):
    c = two_workspaces
    supervisor = c.x.gateway.supervisor
    original = supervisor.get
    seen = []

    def get(workspace):
        seen.append(workspace)
        return original(workspace)

    monkeypatch.setattr(supervisor, "get", get)
    principal = sessions.person_principal(c.x.client.db, c.x.me["user"], c.x.me["workspace"])
    WorkerClient(supervisor).call_tool(principal, "mempalace_kg_query", {"entity": "WorkspaceFact"})
    mutation(c, "kg_invalidate")
    assert seen and set(seen) == {c.x.me["workspace"]}


def cite(c, tool="kg_save", **fields):
    if "source_url" in fields:
        # A merged citation needs a source row to persist its origin_link.
        fields.setdefault("source_thread", "ext-citation")
    args = dict(subject="WorkspaceFact", predicate="port", object="visible", wing="shared")
    if tool.endswith("_padded"):
        tool = tool.removesuffix("_padded")
        fields["source_thread"] = " " + fields["source_thread"] + " "
    if tool == "kg_supersede":
        call(c.x, c.actor, "kg_save", **{**args, "object": "prior"})
        args.pop("object")
        args.update(old_object="prior", new_object="visible", at="2026-02-01")
    if tool == "fact_save":
        args["theme"] = args.pop("wing")
        args["source"] = {"thread": fields.pop("source_thread")}
    return call(c.x, c.actor, tool, **args, **fields)


@pytest.mark.parametrize(
    "tool", ["kg_save", "kg_add", "kg_supersede", "fact_save", "kg_save_padded"]
)
def test_cw_b01_x_cannot_cite_y_thread(two_workspaces, tool):
    c = two_workspaces
    cite(c, tool, source_thread=c.room)
    assert c.room not in json.dumps(xfacts(c)), "Foreign thread citation leaked"
    assert any(f.get("object") == "visible" for f in xfacts(c).get("facts", []))  # RED


@pytest.mark.parametrize("tool", ["kg_save", "kg_add"])
def test_cw_b02_x_cannot_cite_y_drawer(two_workspaces, tool):
    c = two_workspaces
    cite(c, tool, source_drawer_id=c.record["drawer_id"])
    assert c.record["drawer_id"] not in json.dumps(xfacts(c))  # RED


def test_cw_b03_x_cannot_cite_y_thread_link(two_workspaces):
    c = two_workspaces
    cite(c, source_url="t3-thread://v1/" + c.room)
    assert c.room not in json.dumps(xfacts(c))  # RED


@pytest.mark.parametrize("kind", ["import_private", "capture"])
def test_cw_b04_x_cannot_cite_y_private_source_id(two_workspaces, kind):
    c = two_workspaces
    source = (
        f"import-private:{c.y.me['user']}:foreign"
        if kind == "import_private"
        else "capture:foreign:s1"
    )
    store_thread(
        c.y, source=source, wing="private_threads" if kind == "import_private" else "private_chats"
    )
    cite(c, source_thread=source)
    assert source not in json.dumps(xfacts(c))  # RED


def test_cw_b05_x_kg_holds_no_y_identifier(two_workspaces):
    c = two_workspaces
    for fields in (
        {"source_thread": c.room},
        {"source_drawer_id": c.record["drawer_id"]},
        {"source_url": "t3-thread://v1/" + c.room},
        {"source_thread": f"import-private:{c.y.me['user']}:foreign"},
    ):
        cite(c, **fields)
    with sqlite3.connect(c.roots[0] / "palace" / "knowledge_graph.sqlite3") as conn:
        rows = [
            conn.execute("SELECT * FROM " + table).fetchall()
            for table in ("triples", "fact_sources")
        ]
    assert c.room not in repr(rows) and c.record["drawer_id"] not in repr(rows)  # RED


def test_cw_bg01_x_cites_own_x_thread_and_drawer(two_workspaces):
    c = two_workspaces
    own = store_thread(c.x)
    drawer = call(
        c.x,
        c.actor,
        "add_drawer",
        wing="shared",
        room=own.id,
        content="own X verbatim words",
    )
    cite(c, source_thread=own.id, source_drawer_id=drawer["drawer_id"])
    assert own.id in json.dumps(xfacts(c)) and drawer["drawer_id"] in json.dumps(xfacts(c))


def test_cw_bg02_app_threads_link_to_y_stays_scrubbed(two_workspaces):
    c = two_workspaces
    cite(c, source_url="/app/threads/" + c.room)
    assert c.room not in json.dumps(xfacts(c))


@pytest.mark.parametrize("kind", ["ext", "mcp", "https"])
def test_cw_bg03_external_ids_unchanged(two_workspaces, kind):
    c = two_workspaces
    source = {"ext": "ext-synthetic", "mcp": "mcp:synthetic", "https": "https://example.test/chat"}[
        kind
    ]
    cite(c, source_thread=source)
    assert source in json.dumps(xfacts(c))


def test_cw_a05_owner_delete_by_source_in_x_leaves_y_drawers(two_workspaces):
    c = two_workspaces
    source = c.record["source_file"]
    own = call(
        c.x,
        c.xowner,
        "add_drawer",
        wing="shared",
        room="notes",
        content="different synthetic words",
        source_file=source,
    )
    assert own.get("success"), own
    result = call(c.x, c.xowner, "delete_by_source", source_file=source, dry_run=False)
    assert result.get("deleted", 0) > 0, result
    assert "different synthetic words" not in call(
        c.x, c.xowner, "get_drawer", drawer_id=own["drawer_id"]
    ).get("content", "")
    assert "zqworkspace" in call(c.y, c.owner, "get_drawer", drawer_id=c.record["drawer_id"]).get(
        "content", ""
    ), "X deletion reached Y's drawer"
