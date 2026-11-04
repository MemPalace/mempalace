"""The HTTP door: identity resolution, admin-only routes, refusals, real writes."""

import http.client
import json
import os
import subprocess
import sys
import threading

import pytest
from _acl_golden import FLOW, post
from _acl_helpers import LEGACY, key, write_grants

from mempalace import mcp_server as mcp
from mempalace.acl.grants import GrantsError
from mempalace.tool_capabilities import TOOL_CAPABILITIES

pytestmark = pytest.mark.acl_integration

G1, G2, RO = "tok-g1", "tok-g2", "tok-ro"
KEYS = [
    key("g1", G1, wings=["alpha"]),
    key("g2", G2, wings=["beta"]),
    key("g1ro", RO, read_wings=["alpha"]),
    key("gone", "tok-gone", wings=["alpha"], revoked=True),
]


def tool(port, token, name, arguments=None, rid=1):
    body = {
        "jsonrpc": "2.0",
        "id": rid,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments or {}},
    }
    return post(port, body, token=token)


def add(port, token, wing, content="Drawer text.", rid=1):
    args = {"wing": wing, "room": "notes", "content": content}
    return tool(port, token, "mempalace_add_drawer", args, rid=rid)


def wings_in_palace(port):
    status, body = tool(port, LEGACY, "mempalace_list_wings")
    assert status == 200
    return json.loads(json.loads(body)["result"]["content"][0]["text"])["wings"]


def get(port, path, token=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", path, headers={"Authorization": f"Bearer {token}"} if token else {})
        resp = conn.getresponse()
        return resp.status, resp.read().decode()
    finally:
        conn.close()


def test_unknown_revoked_and_missing_tokens_get_401_with_no_detail(serve_with):
    port, _ = serve_with(KEYS)
    ping = FLOW[1][1]
    bodies = set()
    for token in ("nope", "tok-gone", "", None):
        status, body = post(port, ping, token=token)
        assert status == 401
        assert "gone" not in body and "g1" not in body and "alpha" not in body
        bodies.add(body)
    assert len(bodies) == 1  # unknown and revoked look identical


def test_legacy_token_is_admin_and_sees_everything(serve_with):
    port, _ = serve_with(KEYS)
    status, body = post(port, FLOW[2][1], token=LEGACY)
    assert status == 200
    assert len(json.loads(body)["result"]["tools"]) == len(TOOL_CAPABILITIES)
    status, body = tool(port, LEGACY, "mempalace_status")
    assert status == 200 and "error" not in json.loads(body)


def test_locked_token_lists_and_calls_only_the_allowed_tools(serve_with):
    port, _ = serve_with(KEYS)
    status, body = post(port, FLOW[2][1], token=G1)
    names = sorted(t["name"] for t in json.loads(body)["result"]["tools"])
    assert names == ["mempalace_add_drawer", "mempalace_diary_write", "mempalace_get_aaak_spec"]
    for name in ("mempalace_search", "mempalace_get_drawer", "mempalace_list_wings"):
        status, body = tool(port, G1, name, {"query": "x", "drawer_id": "d"})
        assert json.loads(body)["error"] == {"code": -32601, "message": f"Unknown tool: {name}"}
    status, body = tool(port, G1, "mempalace_get_aaak_spec")
    assert "aaak_spec" in body


def test_writes_land_only_in_granted_wings(serve_with):
    port, _ = serve_with(KEYS)
    assert json.loads(add(port, G1, "alpha", "alpha text")[1])["result"]
    assert json.loads(add(port, G2, "beta", "beta text")[1])["result"]
    before = wings_in_palace(port)
    assert before == {"alpha": 1, "beta": 1}
    forbidden = add(port, G1, "beta", "attack")
    missing = add(port, G1, "brand_new_wing", "attack")
    nowing = tool(port, G1, "mempalace_add_drawer", {"room": "r", "content": "attack"})
    readonly = add(port, RO, "alpha", "attack")
    assert forbidden == missing == nowing == readonly
    assert json.loads(forbidden[1])["error"]["message"] == "Invalid params: wing not available"
    assert "beta" not in forbidden[1] and "brand_new_wing" not in forbidden[1]
    assert wings_in_palace(port) == before  # nothing landed


def test_grant_names_are_normalized_per_request(serve_with):
    port, _ = serve_with([key("g", "tok-n", wings=["Alpha-Team"])])
    assert "result" in json.loads(add(port, "tok-n", "ALPHA_TEAM")[1])
    assert "result" in json.loads(add(port, "tok-n", "alpha-team ", rid=2)[1])
    assert wings_in_palace(port) == {"alpha_team": 1}  # same text, same drawer
    # a fragmented sibling spelling is not implicitly granted
    assert "error" in json.loads(add(port, "tok-n", "alpha-teamx")[1])


def test_diary_write_needs_an_explicit_granted_wing(serve_with):
    port, _ = serve_with(KEYS)
    args = {"agent_name": "bot", "entry": "dear diary"}
    assert "error" in json.loads(tool(port, G1, "mempalace_diary_write", args)[1])
    other = {**args, "wing": "beta"}
    assert "error" in json.loads(tool(port, G1, "mempalace_diary_write", other)[1])
    ok = tool(port, G1, "mempalace_diary_write", {**args, "wing": "alpha"})
    assert json.loads(json.loads(ok[1])["result"]["content"][0]["text"])["success"] is True
    assert wings_in_palace(port) == {"alpha": 1}


def test_interleaved_callers_never_cross_wings(serve_with):
    port, _ = serve_with(KEYS)
    results = []

    def worker(token, wing, n):
        for i in range(n):
            results.append((wing, add(port, token, wing, f"{wing} text {i}", rid=i)))
            results.append((wing, add(port, token, "alpha" if wing == "beta" else "beta", "x")))

    threads = [
        threading.Thread(target=worker, args=(G1, "alpha", 5)),
        threading.Thread(target=worker, args=(G2, "beta", 5)),
    ]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert wings_in_palace(port) == {"alpha": 5, "beta": 5}


@pytest.mark.parametrize(
    "path", ["/statusz", "/sync/version_vector", "/logstream/stream", "/mcp/light"]
)
def test_admin_only_routes(serve_with, path):
    port, _ = serve_with(KEYS)
    assert get(port, path, token=G1)[0] == 403
    assert get(port, path, token="nope")[0] == 401
    assert get(port, path)[0] == 401


def test_light_endpoint_is_admin_only(serve_with):
    port, _ = serve_with(KEYS)
    ping = FLOW[1][1]
    assert post(port, ping, token=G1, path="/mcp/light")[0] == 403
    assert post(port, ping, token="nope", path="/mcp/light")[0] == 401
    assert post(port, ping, token=LEGACY, path="/mcp/light")[0] == 200


def test_statusz_open_to_legacy_and_healthz_stays_open(serve_with):
    port, _ = serve_with(KEYS)
    assert get(port, "/statusz", token=LEGACY)[0] == 200
    assert get(port, "/healthz") == (200, "ok\n")
    assert get(port, "/nothing", token=G1)[0] == 404


def test_other_methods_and_paths(serve_with):
    port, _ = serve_with(KEYS)
    for method in ("initialize", "ping"):
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": {}}
        assert post(port, body, token=G1)[0] == 200
    odd = {"jsonrpc": "2.0", "id": 1, "method": "resources/list"}
    assert json.loads(post(port, odd, token=G1)[1])["error"]["code"] == -32601
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("POST", "/other", body=b"{}", headers={"Authorization": f"Bearer {G1}"})
    assert conn.getresponse().status == 404


def test_levels_alone_start(serve_with):
    port, _ = serve_with(KEYS, levels=["staff"])
    assert post(port, FLOW[1][1], token=G1)[0] == 200


@pytest.mark.parametrize(
    "keys, top",
    [
        ([key("a", "1", clearance="staff")], {"levels": ["staff"]}),
        ([key("a", "1", compartments=["board"])], {}),
        ([key("a", "1")], {"default_wing_mode": "open"}),
        ([key("a", "1")], {"wings": {"w": {"mode": "open"}}}),
    ],
)
def test_server_refuses_unenforced_settings(monkeypatch, tmp_path, keys, top):
    monkeypatch.setenv("MEMPALACE_MCP_HTTP_TOKEN", LEGACY)
    monkeypatch.setenv("MEMPALACE_MCP_GRANTS_FILE", write_grants(tmp_path, keys, **top))
    with pytest.raises(GrantsError, match="not enforced yet"):
        mcp._build_http_server("127.0.0.1", 0)


def test_server_refuses_tokenless_and_empty_token(monkeypatch, tmp_path, tokenless_http_hub):
    monkeypatch.setenv("MEMPALACE_MCP_GRANTS_FILE", write_grants(tmp_path, KEYS))
    # loopback, no token: local_only_tokenless
    monkeypatch.delenv("MEMPALACE_MCP_HTTP_ALLOW_INSECURE_NO_TOKEN")
    with pytest.raises(GrantsError, match="local_only_tokenless"):
        mcp._build_http_server("127.0.0.1", 0)
    # explicitly tokenless (--allow-insecure behind a proxy): empty token
    monkeypatch.setenv("MEMPALACE_MCP_HTTP_ALLOW_INSECURE_NO_TOKEN", "1")
    with pytest.raises(GrantsError, match="token"):
        mcp._build_http_server("0.0.0.0", 0)


def _serve_cli(tmp_path, *extra, env_extra=None):
    env = {**os.environ, "HOME": str(tmp_path), "USERPROFILE": str(tmp_path)}
    env.pop("MEMPALACE_MCP_HTTP_TOKEN", None)
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, "-m", "mempalace", "--palace", str(tmp_path / "p"), "serve", *extra],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_cli_refuses_tokenless_grants(tmp_path):
    grants = write_grants(tmp_path, KEYS)
    done = _serve_cli(tmp_path, "--grants-file", grants)
    assert done.returncode == 2 and "local_only_tokenless" in done.stderr
    done = _serve_cli(
        tmp_path, "--grants-file", grants, env_extra={"MEMPALACE_MCP_GRANTS_FILE": ""}
    )
    assert done.returncode == 2


def test_cli_refuses_loose_perms_and_reserved_keys(tmp_path):
    loose = write_grants(tmp_path, KEYS, mode=0o644, name="loose.json")
    done = _serve_cli(tmp_path, "--grants-file", loose, "--token", "t")
    assert done.returncode == 2 and "too open" in done.stderr
    reserved = write_grants(tmp_path, KEYS, name="res.json", default_wing_mode="open")
    done = _serve_cli(tmp_path, "--grants-file", reserved, "--token", "t")
    assert done.returncode == 2 and "not enforced yet" in done.stderr


def drawers(port, wing):
    status, body = tool(port, LEGACY, "mempalace_list_drawers", {"wing": wing})
    text = json.loads(body)["result"]["content"][0]["text"]
    return json.loads(text)["drawers"]


def test_locked_caller_cannot_forge_provenance_or_make_the_server_stat_a_path(
    serve_with, monkeypatch, tmp_path
):
    port, _ = serve_with(KEYS)
    probed = []
    monkeypatch.setattr(mcp, "identity_metadata", lambda path: probed.append(path) or {})
    secret = tmp_path / "secret.txt"
    secret.write_text("x")
    args = {
        "wing": "alpha",
        "room": "notes",
        "content": "forged?",
        "added_by": "admin",
        "source_file": str(secret),
    }
    assert "result" in json.loads(tool(port, G1, "mempalace_add_drawer", args)[1])
    assert probed == []  # the server never touched the caller's path
    [drawer] = drawers(port, "alpha")
    meta = drawer["metadata"]
    assert meta["added_by"] == "g1"
    assert not meta.get("source_file")
    assert str(secret) not in json.dumps(drawer)
    # the admin may still set both
    admin_args = {**args, "content": "by admin", "wing": "beta"}
    tool(port, LEGACY, "mempalace_add_drawer", admin_args)
    [adrawer] = drawers(port, "beta")
    assert adrawer["metadata"]["added_by"] == "admin"
    assert adrawer["metadata"]["source_file"] == "secret.txt"
    assert probed == [str(secret)]


@pytest.mark.parametrize("tool_name", ["mempalace_add_drawer", "mempalace_diary_write"])
@pytest.mark.parametrize(
    "failure",
    [
        "returns_error_dict",
        "raises_value_error",
        "raises_runtime",
        "raises_type_error",
        "raises_key_error",
    ],
)
def test_locked_caller_sees_one_generic_error(serve_with, monkeypatch, tool_name, failure):
    port, _ = serve_with(KEYS)
    boom = "/srv/secret/path beta_wing CANARY_TEXT"

    exc_class = {
        "raises_runtime": RuntimeError,
        "raises_type_error": TypeError,
        "raises_key_error": KeyError,
    }.get(failure, ValueError)

    def fail(*a, **k):
        raise exc_class(boom)

    if failure == "returns_error_dict":
        monkeypatch.setattr(mcp, "sanitize_content", fail)  # handler turns it into {"error": ...}
    elif tool_name == "mempalace_add_drawer":
        monkeypatch.setattr(mcp, "make_drawer_id_from_content", fail)
    else:
        monkeypatch.setattr(mcp, "_get_collection", fail)
    args = {"wing": "alpha", "room": "r", "content": "c", "agent_name": "bot", "entry": "e"}
    args = {
        k: v for k, v in args.items() if k in mcp.TOOLS[tool_name]["input_schema"]["properties"]
    }
    status, body = tool(port, G1, tool_name, args)
    assert status == 200
    for leaked in ("/srv", "secret", "beta", "CANARY", "Error"):
        assert leaked not in body, leaked
    # the admin keeps the old behaviour: real text for ValueErrors, the class otherwise
    admin = tool(port, LEGACY, tool_name, args)[1]
    if failure in ("returns_error_dict", "raises_value_error"):
        assert "CANARY" in admin
    else:
        assert "Internal tool error" in admin and exc_class.__name__ in admin


def test_cli_deeply_nested_grants_file_exits_2_without_traceback(tmp_path):
    path = tmp_path / "deep.json"
    path.write_text("[" * 100000)
    os.chmod(path, 0o600)
    done = _serve_cli(tmp_path, "--grants-file", str(path), "--token", "t")
    assert done.returncode == 2 and "Traceback" not in done.stderr


def test_locked_diary_entries_are_always_written_as_the_key(serve_with):
    port, _ = serve_with([key("Dana", "tok-dana", wings=["alpha"])])
    args = {"agent_name": "admin", "entry": "not written as admin", "wing": "alpha"}
    reply = json.loads(
        json.loads(tool(port, "tok-dana", "mempalace_diary_write", args)[1])["result"]["content"][
            0
        ]["text"]
    )
    assert reply["success"] is True and reply["agent"] == "dana"
    drawers_ = drawers(port, "alpha")
    assert [d["metadata"]["agent"] for d in drawers_] == ["dana"]


def _reply_text(body):
    return json.loads(json.loads(body)["result"]["content"][0]["text"])


def test_granted_key_gets_useful_validation_errors_but_forbidden_wings_stay_equal(serve_with):
    port, _ = serve_with(KEYS)
    empty = {"wing": "alpha", "room": "notes", "content": "   "}
    reply = _reply_text(tool(port, G1, "mempalace_add_drawer", empty)[1])
    assert reply == {"success": False, "error": "content must be a non-empty string"}
    badroom = {"wing": "alpha", "room": "../x", "content": "ok"}
    assert "room" in _reply_text(tool(port, G1, "mempalace_add_drawer", badroom)[1])["error"]
    diary = {"agent_name": "x", "entry": "", "wing": "alpha"}
    assert "content" in _reply_text(tool(port, G1, "mempalace_diary_write", diary)[1])["error"]
    # the validation text never decides what a forbidden or nonexistent wing answers
    forbidden = tool(port, G1, "mempalace_add_drawer", {**empty, "wing": "beta"})
    nonexistent = tool(port, G1, "mempalace_add_drawer", {**empty, "wing": "nope_wing"})
    assert forbidden == nonexistent
    assert json.loads(forbidden[1])["error"]["message"] == "Invalid params: wing not available"


def test_acl_flag_follows_the_server_being_built(serve_with, monkeypatch, config, kg):
    from _mcp_server_helpers import _patch_mcp_server

    port, httpd = serve_with(KEYS)
    assert tool(port, LEGACY, "mempalace_status")[0] == 200
    httpd.shutdown()
    httpd.server_close()
    # same process, now a server with no grants file
    _patch_mcp_server(monkeypatch, config, kg)
    monkeypatch.delenv("MEMPALACE_MCP_GRANTS_FILE")
    monkeypatch.setenv("MEMPALACE_MCP_HTTP_TOKEN", LEGACY)
    off = mcp._build_http_server("127.0.0.1", 0)
    thread = threading.Thread(target=off.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        status, body = tool(off.server_address[1], LEGACY, "mempalace_status")
        assert status == 200 and "error" not in json.loads(body)
        status, body = tool(off.server_address[1], LEGACY, "mempalace_list_wings")
        assert "result" in json.loads(body)
    finally:
        off.shutdown()
        off.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("blank", ["", " ", "   ", "\t", " \n "])
def test_blank_grants_env_var_refuses_instead_of_running_open(monkeypatch, blank):
    monkeypatch.setenv("MEMPALACE_MCP_HTTP_TOKEN", LEGACY)
    monkeypatch.setenv("MEMPALACE_MCP_GRANTS_FILE", blank)
    with pytest.raises(GrantsError, match="set but blank"):
        mcp._build_http_server("127.0.0.1", 0)


def test_absent_grants_env_var_is_acl_off_and_a_valid_path_is_on(monkeypatch, config, kg, tmp_path):
    from mempalace import acl

    monkeypatch.setenv("MEMPALACE_MCP_HTTP_TOKEN", LEGACY)
    monkeypatch.delenv("MEMPALACE_MCP_GRANTS_FILE", raising=False)
    off = mcp._build_http_server("127.0.0.1", 0)
    off.server_close()
    assert off.grants is None and acl.current_scope() is acl.ADMIN
    monkeypatch.setenv("MEMPALACE_MCP_GRANTS_FILE", write_grants(tmp_path, KEYS))
    on = mcp._build_http_server("127.0.0.1", 0)
    on.server_close()
    assert on.grants is not None and acl.current_scope() is acl.DENIED


def test_cli_blank_grants_flag_or_env_never_starts_open(tmp_path):
    done = _serve_cli(tmp_path, "--grants-file", "", "--token", "t")
    assert done.returncode == 2
    done = _serve_cli(
        tmp_path, "--port", "0", "--token", "t", env_extra={"MEMPALACE_MCP_GRANTS_FILE": "  "}
    )
    assert done.returncode != 0 and "set but blank" in done.stderr + done.stdout


def test_denied_write_tool_does_not_queue_on_the_request_lock(serve_with):
    port, _ = serve_with(KEYS)
    body = {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {"name": "mempalace_delete_drawer", "arguments": {"drawer_id": "x"}},
    }

    def short_post(payload):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
        conn.request(
            "POST",
            "/mcp",
            json.dumps(payload),
            {"Content-Type": "application/json", "Authorization": f"Bearer {G1}"},
        )
        return conn.getresponse().read().decode()

    with mcp._HTTP_REQUEST_LOCK:  # another request holds the write lock
        denied = short_post(body)  # must answer at once, not wait for the lock
    ghost = {**body, "params": {**body["params"], "name": "mempalace_no_such_tool"}}
    want = short_post(ghost)
    assert json.loads(denied)["error"] == {
        "code": -32601,
        "message": "Unknown tool: mempalace_delete_drawer",
    }
    assert json.loads(want)["error"]["code"] == -32601


def test_provenance_is_the_key_even_when_the_caller_omits_the_argument(serve_with):
    port, _ = serve_with([key("Dana", "tok-dana", wings=["alpha"])])
    bare = {"wing": "alpha", "room": "notes", "content": "no added_by sent"}
    assert "result" in json.loads(tool(port, "tok-dana", "mempalace_add_drawer", bare)[1])
    diary = {"wing": "alpha", "entry": "no agent_name sent"}  # agent_name omitted entirely
    reply = json.loads(tool(port, "tok-dana", "mempalace_diary_write", diary)[1])
    assert _reply_text(json.dumps(reply))["agent"] == "dana"
    by_room = {d["metadata"]["room"]: d["metadata"] for d in drawers(port, "alpha")}
    assert by_room["notes"]["added_by"] == "dana"  # not the handler default "mcp"
    assert by_room["diary"]["agent"] == "dana"
    tool(port, LEGACY, "mempalace_add_drawer", {**bare, "wing": "beta", "content": "admin"})
    assert drawers(port, "beta")[0]["metadata"]["added_by"] == "mcp"  # admin keeps the default


def test_grants_env_path_with_surrounding_whitespace_is_stripped_and_loaded(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMPALACE_MCP_HTTP_TOKEN", LEGACY)
    path = write_grants(tmp_path, KEYS)
    monkeypatch.setenv("MEMPALACE_MCP_GRANTS_FILE", f"  {path}\t")
    httpd = mcp._build_http_server("127.0.0.1", 0)
    httpd.server_close()
    assert httpd.grants is not None and len(httpd.grants.keys) == len(KEYS)


@pytest.mark.parametrize("blank", ["", "  ", "\t"])
def test_cli_blank_grants_flag_says_so_and_exits_2(tmp_path, blank):
    done = _serve_cli(tmp_path, "--grants-file", blank, "--token", "t")
    assert done.returncode == 2
    assert "--grants-file is set but blank" in done.stderr
    assert "too open" not in done.stderr
