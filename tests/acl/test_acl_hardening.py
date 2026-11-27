"""Security-critical matching: exact bearer, exact paths, exact names, any-key refusals."""

import http.client
import json
import os
import threading

import pytest
from _acl_helpers import LEGACY, key, sha, write_grants

from mempalace import acl, mcp_server as mcp
from mempalace.acl.grants import (
    GrantsError,
    GrantStore,
    Key,
    TokenResolver,
    load_for_server,
    load_grants,
)

pytestmark = pytest.mark.acl_integration
LOCKED = acl.Scope("locked", "g1", read=frozenset({"alpha"}), write=frozenset({"alpha"}))


# 1. strict bearer -----------------------------------------------------------
@pytest.mark.parametrize("auth", ["Bearer:t1", "Bearer\tt1", "BearerXt1", "Bearer", "Bearert1"])
def test_bearer_scheme_must_be_exactly_bearer_space(tmp_path, auth):
    resolver = TokenResolver(load_grants(write_grants(tmp_path, [key("a", "t1", wings=["x"])])), "")
    assert resolver.resolve({"Authorization": auth}) is None
    assert resolver.resolve({"Authorization": "Bearer t1"}).subject == "a"


# 2. admin-only routes ---------------------------------------------------------
def _get(port, path, token):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", path, headers={"Authorization": f"Bearer {token}"})
        return conn.getresponse().status
    finally:
        conn.close()


@pytest.mark.parametrize(
    "path",
    ["/statusz?x=1", "/sync/x?y", "/sync/version_vector?a=b", "/logstream/stream?a=b", "/sync/"],
)
def test_admin_routes_stay_admin_only_with_query_strings(serve_with, path):
    port, _ = serve_with([key("g", "tok-g", wings=["alpha"])])
    assert _get(port, path, "tok-g") == 403


@pytest.mark.parametrize("path", ["//statusz", "/statusz/", "/Statusz", "/statusz%2f", "/sync"])
def test_slash_tricks_never_give_a_locked_key_more_than_admin_routes_allow(serve_with, path):
    """Python normalizes a leading ``//`` to ``/`` (3.12+), so ``//statusz`` IS the admin
    route (403 for a locked key, 200 for admin); the other spellings are not routes (404
    for both). Whatever the interpreter does, a locked key never gets a 200 the admin route
    would only give the admin."""
    port, _ = serve_with([key("g", "tok-g", wings=["alpha"])])
    admin = _get(port, path, LEGACY)
    assert _get(port, path, "tok-g") == (403 if admin == 200 else admin)
    assert admin in (200, 404)


# 3. exact name matching ---------------------------------------------------------
@pytest.mark.parametrize("order", [("al", "alice"), ("alice", "al")])
def test_each_key_resolves_only_to_its_own_wings(tmp_path, order):
    wings = {"al": ["wing_al"], "alice": ["wing_alice"]}
    keys = [key(n, f"tok-{n}", wings=wings[n]) for n in order]
    store = load_grants(write_grants(tmp_path, keys))
    resolver = TokenResolver(store, "")
    for name in ("al", "alice"):
        who = resolver.resolve({"Authorization": f"Bearer tok-{name}"})
        assert who.subject == name
        assert store.scope_for(who).write == frozenset(wings[name])
    assert store.scope_for(acl.Identity("ali", "grants")).kind == "denied"


def test_a_near_miss_hash_does_not_authenticate():
    real = sha("t1")
    store = GrantStore([Key("a", real[:-1] + ("0" if real[-1] != "0" else "1"))])
    assert TokenResolver(store, "").resolve({"Authorization": "Bearer t1"}) is None
    assert TokenResolver(GrantStore([Key("a", real)]), "").resolve({"Authorization": "Bearer t1"})


# 4. wing validation -----------------------------------------------------------
@pytest.mark.parametrize("wings", [[5], [""], ["!!"], ["a", "a"], [None], [["a"]], "a", {"a": 1}])
def test_key_wing_lists_are_validated(tmp_path, wings):
    with pytest.raises(GrantsError):
        load_grants(write_grants(tmp_path, [key("a", "1", wings=wings)]))
    with pytest.raises(GrantsError):
        load_grants(write_grants(tmp_path, [key("a", "1", read_wings=wings)]))


@pytest.mark.parametrize(
    "top",
    [{"w": {"mode": "locked", "typo": 1}}, {"w": {"modee": "locked"}}, {"w": {"wings": []}}],
)
def test_unknown_key_under_a_top_level_wing_is_refused(tmp_path, top):
    with pytest.raises(GrantsError, match="unknown key"):
        load_grants(write_grants(tmp_path, [key("a", "1")], wings=top))


# 5. INTERNAL vs ADMIN -------------------------------------------------------------
def test_internal_bypasses_and_differs_from_admin():
    assert acl.INTERNAL.bypass and acl.ADMIN.bypass
    assert not acl.DENIED.bypass and not LOCKED.bypass
    assert acl.INTERNAL != acl.ADMIN
    with acl.request_scope(acl.ADMIN):
        assert acl.current_scope() is acl.ADMIN
    with pytest.raises(ValueError):  # a request may be admin but can never claim INTERNAL
        with acl.request_scope(acl.INTERNAL):
            pass


def test_internal_thread_runs_denied_and_normal_tools_to_completion(monkeypatch, config, kg):
    from _mcp_server_helpers import _patch_mcp_server

    _patch_mcp_server(monkeypatch, config, kg)
    acl.enable()
    out = {}

    def job():
        out["denies"] = acl.denies_tool("mempalace_status")
        for name in ("mempalace_status", "mempalace_get_aaak_spec", "mempalace_list_wings"):
            body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call"}
            out[name] = mcp.handle_request({**body, "params": {"name": name, "arguments": {}}})
        out["listed"] = len(
            mcp.handle_request({"id": 1, "method": "tools/list"})["result"]["tools"]
        )

    t = threading.Thread(target=acl.run_internal(job))
    t.start()
    t.join(timeout=60)
    assert out["denies"] is False
    for name in ("mempalace_status", "mempalace_get_aaak_spec", "mempalace_list_wings"):
        assert "result" in out[name], name  # ran; not -32601 Unknown tool
    assert out["listed"] == len(mcp.TOOLS)
    with acl.request_scope(LOCKED):
        assert acl.denies_tool("mempalace_status")  # the same tool stays denied to a request


# 6. any-key refusals ------------------------------------------------------------------
@pytest.mark.parametrize(
    "extra, top",
    [
        ({"clearance": "staff"}, {"levels": ["staff"]}),
        ({"compartments": ["board"]}, {}),
    ],
)
def test_one_key_using_a_reserved_setting_is_enough_to_refuse(tmp_path, extra, top):
    mixed = [key("plain", "1", wings=["a"]), key("fancy", "2", wings=["b"], **extra)]
    everyone = [key("one", "1", **extra), key("two", "2", **extra)]
    for keys in (mixed, mixed[::-1], everyone):
        with pytest.raises(GrantsError, match="not enforced yet"):
            load_for_server(write_grants(tmp_path, keys, **top), "legacy")


def test_one_open_wing_among_locked_ones_is_enough_to_refuse(tmp_path):
    wings = {"a": {"mode": "locked"}, "b": {"mode": "open"}, "c": {"mode": "locked"}}
    with pytest.raises(GrantsError, match="mode: open"):
        load_for_server(write_grants(tmp_path, [key("k", "1")], wings=wings), "legacy")
    allopen = {"a": {"mode": "open"}, "b": {"mode": "open"}}
    with pytest.raises(GrantsError, match="mode: open"):
        load_for_server(write_grants(tmp_path, [key("k", "1")], wings=allopen), "legacy")


# 7. scrub_result ------------------------------------------------------------------------
SCRUB_CASES = [
    ({"error": None}, True),
    ({"error": ""}, True),
    ({"success": False, "error": ""}, True),
    ({"success": False}, True),
    ({"success": None}, False),
    ({"success": 0}, False),
    ({"success": True}, False),
    ({}, False),
]


@pytest.mark.parametrize("result, hidden", SCRUB_CASES)
def test_scrub_result_rule_is_exactly_an_error_key_or_an_explicit_false(
    monkeypatch, result, hidden
):
    """A result is a failure when it has an ``error`` key (any value, even None or "":
    handlers put error text there and an empty one still marks the failure) or
    ``success`` is exactly False. ``success`` None or 0 makes no failure claim: handlers
    use real booleans, and hiding a result that merely lacks one could swallow data the
    caller is entitled to. A locked caller sees the generic error for failures; the
    admin always sees the handler's result untouched."""
    monkeypatch.setitem(mcp.TOOLS["mempalace_get_aaak_spec"], "handler", lambda: dict(result))
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "mempalace_get_aaak_spec", "arguments": {}},
    }

    def text(scope):
        acl.enable()
        with acl.request_scope(scope):
            reply = mcp.handle_request(request)
        return json.loads(reply["result"]["content"][0]["text"])

    assert text(acl.ADMIN) == result
    assert text(LOCKED) == ({"success": False, "error": "Request failed"} if hidden else result)


# 8. empty and odd files -------------------------------------------------------------------
@pytest.mark.parametrize("text", ["{}", "[]", "null", "", "0", "false", '""'])
def test_empty_or_non_mapping_grants_files_are_refused(tmp_path, text):
    path = tmp_path / "g.json"
    path.write_text(text)
    os.chmod(path, 0o600)
    with pytest.raises(GrantsError):
        load_grants(str(path))


@pytest.mark.parametrize("levels", [[1, 2], [None], [["a"]], "staff", {"a": 1}, [True]])
def test_non_string_levels_refuse_even_when_no_key_uses_clearance(tmp_path, levels):
    with pytest.raises(GrantsError, match="levels"):
        load_grants(write_grants(tmp_path, [key("a", "1")], levels=levels))


def test_null_or_empty_levels_load_as_no_levels(tmp_path):
    for levels in (None, []):
        assert not load_grants(write_grants(tmp_path, [key("a", "1")], levels=levels)).levels
