"""The gateway end to end: auth, tool lists, wing rules, stamps, audit."""

import json
import time
from types import SimpleNamespace

import pytest

from commonplace.control_db import ControlDB
from commonplace.gateway import _DROP, Gateway, _scrub
from commonplace.policy import Policy, PolicyError
from commonplace.workers import Worker, WorkerSupervisor
from tests._commonplace_helpers import hub_fixture  # noqa: F401
from tests._overview_helpers import overview_result

INBOX_BOT = dict(
    profile="slim",
    scopes=["memory:read", "memory:write", "kg:read"],
    read_wings=["shared", "inbox"],
    write_wing="inbox",
    trust="untrusted",
)
CODER = dict(
    profile="standard",
    scopes=["memory:read", "memory:write", "kg:read", "kg:write"],
    read_wings=["shared", "project_*"],
    write_wings=["project_*"],
    trust="standard",
)


def _last_call(hub):
    return next(e for e in hub.db.audit_entries(limit=50) if e["action"] == "tool.call")


# ── authentication ──
def test_no_or_bad_token_is_401(hub):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    for token in (None, "nope", "cmmn_" + "A" * 43, hub.owner_token() + "x"):
        status, text = hub.raw(token, body)
        assert status == 401, (token, status, text)


def test_wrong_auth_scheme_is_401(hub):
    token = hub.owner_token()
    status, _ = hub.raw(
        None, {"jsonrpc": "2.0", "id": 1, "method": "ping"}, {"Authorization": f"Basic {token}"}
    )
    assert status == 401


def test_revoked_token_stops_on_the_next_request(hub):
    token = hub.token("bot", **INBOX_BOT)
    assert hub.rpc(token, "ping")["result"] == {}
    hub.db.revoke("bot", actor=hub.admin)
    status, _ = hub.raw(token, {"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert status == 401


def test_expired_token_is_refused(hub):
    token = hub.token("bot", **INBOX_BOT)
    hub.db._conn.execute("UPDATE connections SET expires_at = '2000-01-01T00:00:00Z'")
    status, _ = hub.raw(token, {"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert status == 401


def test_non_json_and_oversized_and_batch_requests(hub):
    token = hub.owner_token()
    status, _ = hub.raw(token, b"{}", {"Content-Type": "text/plain"})
    assert status == 415
    status, _ = hub.raw(token, b"not json")
    assert status == 400
    status, _ = hub.raw(token, b"{}", {"Content-Length": str(64 * 1024 * 1024)})
    assert status == 413
    status, text = hub.raw(token, [{"jsonrpc": "2.0", "id": 1, "method": "ping"}])
    assert json.loads(text)["error"]["code"] == -32600


def test_health_and_unknown_paths(hub):
    assert hub.raw(None, b"", method="GET", path="/health") == (200, '{"ok": true}')
    assert hub.raw(None, b"", method="GET", path="/mcp")[0] == 401
    assert hub.raw(None, b"", method="GET", path="/oauth/authorize")[0] == 400
    assert hub.raw(None, {}, path="/elsewhere")[0] == 404


def test_routes_seam_serves_extra_endpoints(hub):
    def handler(request):
        request._send_json(200, {"issuer": "x"})

    hub.gateway.add_route("GET", "/.well-known/oauth-authorization-server", handler)
    status, text = hub.raw(None, b"", method="GET", path="/.well-known/oauth-authorization-server")
    assert (status, json.loads(text)) == (200, {"issuer": "x"})


def test_unknown_method_and_notifications(hub):
    token = hub.owner_token()
    assert hub.rpc(token, "resources/list")["error"]["code"] == -32601
    status, _ = hub.raw(token, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert status == 202
    assert hub.rpc(token, "initialize", {})["result"]["serverInfo"]["name"] == "mempalace"


# ── tools/list ──
def test_tools_list_follows_the_profile(hub):
    slim = hub.tool_names(hub.token("bot", **INBOX_BOT))
    # The integrated MVP also lists local board/memory tools; their own tests
    # cover those profiles. Keep this assertion exact for the MemPalace tools.
    assert {name for name in slim if name.startswith("mempalace_")} == {
        "mempalace_search",
        "mempalace_get_drawer",
        "mempalace_add_drawer",
        "mempalace_diary_write",
        "mempalace_diary_read",
        # Facts are wing-scoped, so kg:read on a scoped token reads its wings.
        "mempalace_kg_query",
    }
    coder = hub.tool_names(hub.token("coder", **CODER))
    assert "mempalace_kg_add" in coder and "mempalace_list_drawers" in coder
    # Cross-wing tools need every wing; delete needs trust.
    assert "mempalace_traverse" not in coder and "mempalace_delete_drawer" not in coder
    owner = hub.tool_names(hub.owner_token())
    for never in (
        "mempalace_mine",
        "mempalace_sync",
        "mempalace_hook_settings",
        "mempalace_reconnect",
    ):
        assert never not in owner


def test_every_listed_tool_is_callable_and_no_other(hub):
    token = hub.token("coder", **CODER)
    listed = hub.tool_names(token)
    for name in listed:
        response = hub.rpc(token, "tools/call", {"name": name, "arguments": {}})
        assert response.get("error", {}).get("code") != -32601, name


def test_explicit_tool_list(hub):
    with pytest.raises(PolicyError):
        Policy.build(tools=["mempalace_search", "mempalace_mine"], scopes=["memory:read"])
    token = hub.token(
        "one",
        tools=["mempalace_search", "mempalace_list_wings"],
        scopes=["memory:read"],
        read_wings=["shared"],
        trust="standard",
    )
    assert hub.tool_names(token) == {"mempalace_search", "mempalace_list_wings"}


# ── writes, wings and stamps ──
def test_untrusted_writes_land_in_the_inbox_with_a_stamp(hub):
    token = hub.token("grok", client="grok", **INBOX_BOT)
    result, _ = hub.call(
        token,
        "mempalace_add_drawer",
        wing="shared",
        room="facts",
        content="Avery likes tea",
        added_by="claude",
    )
    assert result["success"] and result["wing"] == "inbox"
    drawer, _ = hub.call(hub.owner_token(), "mempalace_get_drawer", drawer_id=result["drawer_id"])
    meta = drawer["metadata"]
    assert meta["hub_connection"] == "grok"
    assert meta["added_by"] == "grok"
    assert meta["claimed_by"] == "claude"
    assert meta["hub_client"] == "grok"
    assert meta["hub_auth"] == "pat"
    assert meta["hub_trust"] == "untrusted"
    assert meta["hub_user"] == hub.admin
    assert meta["hub_workspace"] == hub.workspace
    assert meta["hub_written_at"]
    entry = _last_call(hub)
    assert entry["tool"] == "mempalace_add_drawer" and entry["decision"] == "ok"
    assert entry["wing"] == "inbox" and "forced" in entry["reason"]
    assert result["drawer_id"] in json.loads(entry["target_ids"])


def test_untrusted_diary_goes_to_the_inbox_too(hub):
    token = hub.token("grok", **INBOX_BOT)
    result, _ = hub.call(token, "mempalace_diary_write", agent_name="grok", entry="hello")
    assert result["success"]
    read, _ = hub.call(token, "mempalace_diary_read", agent_name="grok", wing="inbox")
    assert [e["content"] for e in read["entries"]] == ["hello"]


def test_search_results_carry_the_author(hub):
    token = hub.token("grok", **INBOX_BOT)
    hub.call(token, "mempalace_add_drawer", wing="inbox", room="r", content="the kettle is blue")
    found, _ = hub.call(token, "mempalace_search", query="kettle blue")
    hit = found["results"][0]
    assert hit["hub_connection"] == "grok" and hit["hub_trust"] == "untrusted"


def test_standard_token_writes_only_its_wings(hub):
    token = hub.token("coder", **CODER)
    ok, _ = hub.call(
        token, "mempalace_add_drawer", wing="project_api", room="r", content="uses sqlite"
    )
    assert ok["success"]
    denied, _ = hub.call(token, "mempalace_add_drawer", wing="shared", room="r", content="x")
    assert denied["denied"] and "shared" in denied["error"]
    entry = _last_call(hub)
    assert entry["decision"] == "deny" and entry["tool"] == "mempalace_add_drawer"


def test_kg_writes_are_stamped(hub):
    token = hub.token("coder", **CODER)
    hub.call(
        token,
        "mempalace_kg_add",
        subject="api",
        predicate="uses",
        object="sqlite",
        wing="project_api",
    )
    facts, _ = hub.call(hub.owner_token(), "mempalace_kg_query", entity="api")
    assert facts["facts"][0]["hub_connection"] == "coder"


def test_kg_needs_its_scope(hub):
    token = hub.token(
        "nokg", profile="standard", scopes=["memory:read"], read_wings=["shared"], trust="standard"
    )
    response = hub.rpc(
        token, "tools/call", {"name": "mempalace_kg_query", "arguments": {"entity": "x"}}
    )
    assert response["error"]["message"] == "Unknown tool: mempalace_kg_query"


def test_private_wing_needs_private_scope_even_with_star(hub):
    owner = hub.owner_token()
    hub.call(owner, "mempalace_add_drawer", wing="private", room="r", content="secret")
    token = hub.token(
        "star",
        profile="standard",
        scopes=["memory:read", "memory:write"],
        read_wings=["*"],
        write_wings=["*"],
        trust="standard",
    )
    denied, _ = hub.call(token, "mempalace_list_drawers", wing="private")
    assert denied["denied"]
    denied, _ = hub.call(token, "mempalace_add_drawer", wing="wing_private", room="r", content="x")
    assert denied["denied"]
    wings, _ = hub.call(token, "mempalace_list_wings")
    assert "private" not in wings["wings"]


def test_read_wing_tools_need_a_wing_unless_one_is_granted(hub):
    owner = hub.owner_token()
    hub.call(owner, "mempalace_add_drawer", wing="shared", room="r", content="a")
    two = hub.token("two", **CODER)
    denied, _ = hub.call(two, "mempalace_list_drawers")
    assert denied["denied"] and "pass a wing" in denied["error"]
    one = hub.token(
        "one", profile="standard", scopes=["memory:read"], read_wings=["shared"], trust="standard"
    )
    listed, _ = hub.call(one, "mempalace_list_drawers")
    assert [d["wing"] for d in listed["drawers"]] == ["shared"]


def test_search_fans_out_over_readable_wings_only(hub):
    owner = hub.owner_token()
    for wing in ("shared", "project_a", "project_b", "vault"):
        hub.call(
            owner, "mempalace_add_drawer", wing=wing, room="r", content=f"lantern note in {wing}"
        )
    token = hub.token("coder", **CODER)
    found, _ = hub.call(token, "mempalace_search", query="lantern note", limit=10)
    assert sorted(h["wing"] for h in found["results"]) == ["project_a", "project_b", "shared"]
    assert found["wings_searched"] == ["project_a", "project_b", "shared"]
    pinned, _ = hub.call(token, "mempalace_search", query="lantern note", wing="project_a")
    assert {h["wing"] for h in pinned["results"]} == {"project_a"}
    denied, _ = hub.call(token, "mempalace_search", query="lantern", wing="vault")
    assert denied["denied"]


def test_multiwing_search_uses_one_joint_worker_ranking_before_limit():
    from mempalace.searcher import _hybrid_rank

    results = {
        "shared": _hybrid_rank(
            [
                {
                    "drawer_id": "lexical",
                    "wing": "shared",
                    "text": "ZXQ739251",
                    "distance": None,
                    "similarity": None,
                }
            ],
            "ZXQ739251",
        ),
        "project_a": _hybrid_rank(
            [
                {
                    "drawer_id": "vector",
                    "wing": "project_a",
                    "text": "Unrelated note",
                    "distance": 0.5,
                    "similarity": 0.5,
                }
            ],
            "ZXQ739251",
        ),
    }

    def call_tool(principal, name, args):
        assert principal.workspace_id == "fixture-workspace"
        overview = overview_result(
            name, args, dict.fromkeys(("shared", "project_a", "vault"), "notes")
        )
        if overview is not None:
            return overview
        assert args["wings"] == ["project_a", "shared"]
        joint = [hit for wing in args["wings"] for hit in results[wing]]
        return {"results": _hybrid_rank(joint, args["query"])[: args["limit"]]}

    from commonplace.gateway import Mediator

    gateway = object.__new__(Mediator)
    gateway.gateway = None
    gateway.workers = SimpleNamespace(call_tool=call_tool)
    principal = SimpleNamespace(workspace_id="fixture-workspace", policy=Policy.build(**CODER))
    result = gateway._fan_out_search(
        principal, "mempalace_search", {"query": "ZXQ739251", "limit": 1}
    )
    assert result["wings_searched"] == ["project_a", "shared"]
    assert [hit["drawer_id"] for hit in result["results"]] == ["lexical"]


def test_multiwing_search_receives_one_pool_instead_of_normalized_fragments():
    from commonplace.gateway import Mediator
    from mempalace.searcher import _hybrid_rank

    query = "ZXQ739251 port"
    results = {
        "shared": _hybrid_rank(
            [
                {
                    "drawer_id": "exact",
                    "wing": "shared",
                    "text": query,
                    "distance": None,
                    "similarity": None,
                }
            ],
            query,
        ),
        "project_a": _hybrid_rank(
            [
                {
                    "drawer_id": "partial",
                    "wing": "project_a",
                    "text": "port",
                    "distance": 0.8,
                    "similarity": 0.2,
                }
            ],
            query,
        ),
    }
    assert results["project_a"][0]["score"] > results["shared"][0]["score"]

    def call_tool(principal, name, args):
        assert principal.workspace_id == "fixture-workspace"
        overview = overview_result(name, args, {"shared": "notes", "project_a": "notes"})
        if overview is not None:
            return overview
        assert args["wings"] == ["project_a", "shared"]
        joint = [hit for wing in args["wings"] for hit in results[wing]]
        return {"results": _hybrid_rank(joint, args["query"])[: args["limit"]]}

    mediator = object.__new__(Mediator)
    mediator.gateway = None
    mediator.workers = SimpleNamespace(call_tool=call_tool)
    result = mediator._fan_out_search(
        SimpleNamespace(workspace_id="fixture-workspace", policy=Policy.build(**CODER)),
        "mempalace_search",
        {"query": query, "limit": 1},
    )
    assert [hit["drawer_id"] for hit in result["results"]] == ["exact"]


def test_get_drawers_hides_other_wings_as_missing(hub):
    owner = hub.owner_token()
    a, _ = hub.call(owner, "mempalace_add_drawer", wing="shared", room="r", content="one")
    b, _ = hub.call(owner, "mempalace_add_drawer", wing="vault", room="r", content="two")
    token = hub.token("coder", **CODER)
    got, _ = hub.call(token, "mempalace_get_drawers", drawer_ids=[a["drawer_id"], b["drawer_id"]])
    assert got["count"] == 2 and got["errors"] == 1
    assert got["results"][1] == {
        "drawer_id": b["drawer_id"],
        "error": f"Drawer not found: {b['drawer_id']}",
    }


def test_update_and_delete_respect_wings_and_trust(hub):
    owner = hub.owner_token()
    mine, _ = hub.call(owner, "mempalace_add_drawer", wing="project_a", room="r", content="draft")
    shared, _ = hub.call(owner, "mempalace_add_drawer", wing="shared", room="r", content="readable")
    trusted = hub.token(
        "trusted",
        profile="full",
        scopes=["memory:read", "memory:write", "memory:delete"],
        read_wings=["shared", "project_*"],
        write_wings=["project_*"],
        trust="trusted",
    )
    moved, _ = hub.call(
        trusted, "mempalace_update_drawer", drawer_id=mine["drawer_id"], wing="vault"
    )
    assert moved["denied"]
    denied, _ = hub.call(trusted, "mempalace_delete_drawer", drawer_id=shared["drawer_id"])
    assert denied["denied"] and "shared" in denied["error"]
    ok, _ = hub.call(trusted, "mempalace_delete_drawer", drawer_id=mine["drawer_id"])
    assert ok["success"]
    entry = _last_call(hub)
    assert entry["tool"] == "mempalace_delete_drawer" and mine["drawer_id"] in entry["target_ids"]
    # Standard trust never deletes, whatever its scopes say.
    standard = hub.token(
        "std",
        profile="full",
        scopes=["memory:read", "memory:delete"],
        read_wings=["*"],
        trust="standard",
    )
    assert "mempalace_delete_drawer" not in hub.tool_names(standard)


def test_untrusted_never_updates(hub):
    token = hub.token(
        "bot",
        tools=["mempalace_update_drawer"],
        **{k: v for k, v in INBOX_BOT.items() if k != "profile"},
    )
    assert hub.tool_names(token) == set()


def test_audit_never_stores_memory_text(hub):
    token = hub.token("grok", **INBOX_BOT)
    hub.call(token, "mempalace_add_drawer", wing="inbox", room="r", content="UNIQUETEXTMARKER")
    hub.call(token, "mempalace_add_drawer", wing="vault", room="r", content="UNIQUETEXTMARKER2")
    dump = json.dumps([dict(r) for r in hub.db.audit_entries(limit=100)])
    assert "UNIQUETEXTMARKER" not in dump


def test_last_used_is_recorded(hub):
    token = hub.token("bot", **INBOX_BOT)
    assert hub.db.connection("bot")["last_used_at"] is None
    hub.rpc(token, "ping")
    assert hub.db.connection("bot")["last_used_at"]


def test_client_cannot_send_its_own_identity_header(hub):
    from mempalace import provenance

    token = hub.token("grok", **INBOX_BOT)
    forged = provenance.encode_identity(
        {
            "user": "usr_x",
            "connection": "owner",
            "client": "x",
            "auth": "pat",
            "trust": "trusted",
            "workspace": "ws_x",
        }
    )
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "mempalace_add_drawer",
            "arguments": {"wing": "inbox", "room": "r", "content": "forged?"},
        },
    }
    status, text = hub.raw(token, body, {provenance.IDENTITY_HEADER: forged})
    drawer_id = json.loads(json.loads(text)["result"]["content"][0]["text"])["drawer_id"]
    drawer, _ = hub.call(hub.owner_token(), "mempalace_get_drawer", drawer_id=drawer_id)
    assert drawer["metadata"]["hub_connection"] == "grok"
    assert drawer["metadata"]["hub_trust"] == "untrusted"


# ── workspaces ──
class _RecordingLauncher:
    """Fake workers that answer every call with the workspace they serve."""

    def __init__(self):
        self.started = []
        self.servers = []

    def start(self, workspace_id, workspace_dir):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        from mempalace import provenance

        seen = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                length = int(self.headers["Content-Length"])
                request = json.loads(self.rfile.read(length))
                identity = provenance.decode_identity(self.headers[provenance.IDENTITY_HEADER])
                seen.append(identity)
                payload = {"served_by": workspace_id, "identity": identity}
                body = json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request.get("id"),
                        "result": {"content": [{"type": "text", "text": json.dumps(payload)}]},
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.started.append(workspace_id)
        self.servers.append(server)

        def stop():
            server.shutdown()
            server.server_close()

        return Worker(workspace_id, "127.0.0.1", server.server_address[1], "t", stop)


def test_each_token_reaches_only_its_own_workspace(tmp_path):
    from tests._commonplace_helpers import Hub

    launcher = _RecordingLauncher()
    h = Hub(tmp_path, launcher)
    try:
        alice = h.db.create_user("alice@example.com")
        ws_alice = h.db.create_workspace("alice", alice, personal=True)
        t_owner = h.token(
            "o", profile="slim", scopes=["memory:read"], read_wings=["shared"], trust="standard"
        )
        t_alice = h.db.create_token(
            workspace_ref=ws_alice,
            user_ref=alice,
            name="a",
            client="c",
            policy=Policy.build(
                profile="slim", scopes=["memory:read"], read_wings=["shared"], trust="standard"
            ),
            actor=alice,
        )[1]
        r1, _ = h.call(t_owner, "mempalace_get_drawer", drawer_id="x")
        r2, _ = h.call(t_alice, "mempalace_get_drawer", drawer_id="x")
        assert r1["served_by"] == h.workspace and r1["identity"]["workspace"] == h.workspace
        assert r2["served_by"] == ws_alice and r2["identity"]["user"] == alice
        assert sorted(launcher.started) == sorted([h.workspace, ws_alice])
    finally:
        h.close()


def test_supervisor_reuses_and_idle_stops_workers(tmp_path):
    launcher = _RecordingLauncher()
    sup = WorkerSupervisor(str(tmp_path), launcher=launcher, idle_stop_s=0.05)
    w1 = sup.get("ws_abc")
    assert sup.get("ws_abc") is w1
    time.sleep(0.1)
    assert sup.stop_idle() == ["ws_abc"]
    sup.get("ws_abc")
    assert launcher.started == ["ws_abc", "ws_abc"]
    sup.stop_all()
    with pytest.raises(ValueError):
        sup.workspace_dir("../etc")


def test_worker_down_is_a_clean_error(tmp_path):
    class Broken:
        def start(self, workspace_id, workspace_dir):
            raise RuntimeError("boom")

    db = ControlDB(str(tmp_path / "c.sqlite3"))
    uid = db.create_user("a@example.com")
    ws = db.create_workspace("w", uid)
    _cid, token = db.create_token(
        workspace_ref=ws,
        user_ref=uid,
        name="n",
        client="c",
        policy=Policy.build(
            profile="slim", scopes=["memory:read"], read_wings=["*"], trust="standard"
        ),
        actor=uid,
    )
    gw = Gateway(db, WorkerSupervisor(str(tmp_path), launcher=Broken()))
    principal = gw.authenticate(f"Bearer {token}")
    out = gw.mediator.handle(principal, {"jsonrpc": "2.0", "id": 3, "method": "ping"})
    assert out["error"]["code"] == -32603


def test_scrub_drops_list_items_and_fails_closed_on_objects():
    def can(w):
        return w == "ok"

    assert _scrub({"results": [{"wing": "ok"}, {"wing": "no"}]}, can) == {
        "results": [{"wing": "ok"}]
    }
    assert _scrub({"drawer": {"wing": "no"}}, can) is _DROP
    assert _scrub({"wing": "no", "text": "x"}, can) is _DROP


def test_update_records_who_changed_it(hub):
    author = hub.token("author", **CODER)
    editor = hub.token("editor", **{**CODER, "profile": "full"})
    added, _ = hub.call(author, "mempalace_add_drawer", wing="project_a", room="r", content="v1")
    updated, _ = hub.call(
        editor, "mempalace_update_drawer", drawer_id=added["drawer_id"], content="v2"
    )
    assert updated["success"], updated
    drawer, _ = hub.call(author, "mempalace_get_drawer", drawer_id=added["drawer_id"])
    meta = drawer["metadata"]
    assert meta["hub_connection"] == "author" and meta["hub_updated_by"] == "editor"
    assert meta["hub_updated_trust"] == "standard" and meta["hub_updated_at"]


@pytest.mark.parametrize(
    "ids",
    [lambda d: json.dumps([d]), lambda d: [d, 1], lambda d: 5, lambda d: []],
)
def test_coerced_drawer_ids_are_refused_not_forwarded(hub, ids):
    owner = hub.owner_token()
    vault, _ = hub.call(owner, "mempalace_add_drawer", wing="vault", room="r", content="keep me")
    trusted = hub.token(
        "t",
        profile="full",
        scopes=["memory:read", "memory:write", "memory:delete"],
        read_wings=["shared"],
        write_wings=["shared"],
        trust="trusted",
    )
    result, _ = hub.call(trusted, "mempalace_delete_drawers", drawer_ids=ids(vault["drawer_id"]))
    # Refused, or (a parsed JSON string) answered as missing: never deleted.
    assert result.get("denied") or result.get("error") or result.get("deleted") == 0, result
    still, _ = hub.call(owner, "mempalace_get_drawer", drawer_id=vault["drawer_id"])
    assert still["content"] == "keep me"


def test_non_string_wing_is_refused(hub):
    token = hub.token("coder", **CODER)
    result, _ = hub.call(token, "mempalace_list_drawers", wing=["project_a"])
    assert result["denied"]


# ── review round 2 ──
def test_hidden_drawers_answer_in_each_tools_own_not_found_shape(hub):
    """No existence oracle: a hidden id and a missing id read the same (review HIGH)."""
    owner = hub.owner_token()
    vault, _ = hub.call(owner, "mempalace_add_drawer", wing="vault", room="r", content="secret v")
    mine, _ = hub.call(owner, "mempalace_add_drawer", wing="shared", room="r", content="mine")
    trusted = hub.token(
        "t",
        profile="full",
        scopes=["memory:read", "memory:write", "memory:delete"],
        read_wings=["shared"],
        write_wings=["shared"],
        trust="trusted",
    )
    missing_id = "drawer_vault_r_000000000000000000000000"
    for tool, arg in (
        ("mempalace_delete_drawers", "drawer_ids"),
        ("mempalace_delete_drawer", "drawer_id"),
        ("mempalace_update_drawer", "drawer_id"),
    ):
        extra = {"content": "x"} if tool == "mempalace_update_drawer" else {}

        def ask(drawer_id, tool=tool, arg=arg, extra=extra):
            value = [drawer_id] if arg == "drawer_ids" else drawer_id
            result, _ = hub.call(trusted, tool, **{arg: value}, **extra)
            return json.loads(json.dumps(result).replace(drawer_id, "ID"))

        assert ask(vault["drawer_id"]) == ask(missing_id), tool
    # A hidden id in a batch is one not-found item; the visible one is deleted.
    both, _ = hub.call(
        trusted, "mempalace_delete_drawers", drawer_ids=[vault["drawer_id"], mine["drawer_id"]]
    )
    assert (both["deleted"], both["errors"]) == (1, 1)
    assert both["results"][0] == {
        "drawer_id": vault["drawer_id"],
        "error": f"Drawer not found: {vault['drawer_id']}",
    }
    still, _ = hub.call(owner, "mempalace_get_drawer", drawer_id=vault["drawer_id"])
    assert still["content"] == "secret v"
    assert "cmmn-hidden-" not in json.dumps([dict(r) for r in hub.db.audit_entries(limit=50)])


def _raw_exchange(port, payload: bytes) -> bytes:
    import socket

    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall(payload)
        chunks = []
        while True:
            try:
                data = sock.recv(65536)
            except socket.timeout:
                break
            if not data:
                break
            chunks.append(data)
        return b"".join(chunks)


def test_early_errors_close_the_connection(hub):
    """An unread body must never be parsed as the next request (review MEDIUM)."""
    smuggled = b"GET /health HTTP/1.1\r\nHost: x\r\n\r\n"
    body = b'{"jsonrpc":"2.0","id":1,"method":"ping"}' + smuggled
    head = (
        b"POST /mcp HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n"
    )
    reply = _raw_exchange(hub.port, head + body)
    assert reply.startswith(b"HTTP/1.1 401") and b"Connection: close" in reply
    assert reply.count(b"HTTP/1.1 ") == 1
    token = hub.owner_token().encode()
    head = (
        b"POST /mcp HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer " + token + b"\r\n"
        b"Content-Type: text/plain\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n"
    )
    reply = _raw_exchange(hub.port, head + body)
    assert reply.startswith(b"HTTP/1.1 415") and reply.count(b"HTTP/1.1 ") == 1


def test_transfer_encoding_is_refused(hub):
    head = (
        b"POST /mcp HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
        b"Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n"
    )
    reply = _raw_exchange(hub.port, head)
    assert reply.startswith(b"HTTP/1.1 411") and b"Connection: close" in reply


def test_untrusted_tokens_cannot_write_the_graph(hub):
    bot = hub.token(
        "grok",
        profile="full",
        scopes=["memory:read", "memory:write", "kg:write"],
        read_wings=["inbox"],
        write_wing="inbox",
        trust="untrusted",
    )
    response = hub.rpc(
        bot,
        "tools/call",
        {
            "name": "mempalace_kg_add",
            "arguments": {"subject": "a", "predicate": "b", "object": "c"},
        },
    )
    assert response["error"]["message"] == "Unknown tool: mempalace_kg_add"


def test_audit_fields_are_capped(hub):
    token = hub.token("coder", **CODER)
    hub.rpc(token, "tools/call", {"name": "x" * 10_000, "arguments": {}})
    hub.call(token, "mempalace_add_drawer", wing="w" * 5_000, room="r", content="c")
    for entry in hub.db.audit_entries(limit=5):
        assert len(entry["tool"] or "") <= 120 and len(entry["wing"] or "") <= 200


def test_all_reserved_metadata_is_removed_before_worker_forwarding(hub, monkeypatch):
    from commonplace.gateway import WorkerClient
    from mempalace.stored_text import RESERVED_METADATA

    forwarded = []
    original = WorkerClient.post

    def record(self, principal, request):
        if request.get("method") == "tools/call":
            forwarded.append(request["params"]["arguments"])
        return original(self, principal, request)

    monkeypatch.setattr(WorkerClient, "post", record)
    result, _ = hub.call(
        hub.owner_token(),
        "mempalace_add_drawer",
        wing="shared",
        room="notes",
        content="verbatim imported document",
        **dict.fromkeys(RESERVED_METADATA, "forged"),
    )
    assert result["success"]
    assert forwarded and all(not RESERVED_METADATA.intersection(args) for args in forwarded)


@pytest.mark.parametrize("wing", [None, "shared"])
def test_public_wings_are_intersected_and_searched_once(hub, monkeypatch, wing):
    from commonplace.gateway import WorkerClient

    owner = hub.owner_token()
    for stored_wing in ("shared", "project_a", "private_notes"):
        assert hub.call(
            owner,
            "mempalace_add_drawer",
            wing=stored_wing,
            room="notes",
            content="marigold ZXQ739251",
        )[0]["success"]
    token = hub.token("scoped-search", **CODER)
    recorded = []
    original = WorkerClient.post

    def post(self, principal, request):
        recorded.append(request)
        return original(self, principal, request)

    monkeypatch.setattr(WorkerClient, "post", post)
    result, _ = hub.call(
        token,
        "mempalace_search",
        query="ZXQ739251",
        **({"wing": wing} if wing else {}),
        wings=["shared", "project_a", "private_notes"],
        include_facts=False,
    )
    assert not result.get("error")
    searches = [
        r["params"] for r in recorded if r.get("params", {}).get("name") == "mempalace_search"
    ]
    assert len(searches) == 1
    assert sorted(searches[0]["arguments"]["wings"]) == ["project_a", "shared"]
    assert all(
        hit["wing"] in ({wing} if wing else {"project_a", "shared"}) for hit in result["results"]
    )
    from mempalace.mcp_server import TOOLS

    assert "lexical_terms" not in TOOLS["mempalace_search"]["input_schema"]["properties"]
