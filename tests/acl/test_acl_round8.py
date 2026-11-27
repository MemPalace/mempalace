"""Odd tokens, exact wing and route matching, nothing served after an error, loader and CLI edges."""

import hashlib
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from _acl_helpers import LEGACY, key, sha, write_grants

from mempalace import acl, mcp_server as mcp
from mempalace.acl.grants import GrantsError, TokenResolver, load_for_server, load_grants
from mempalace.tool_capabilities import ACL_PENDING, ACL_POLICY, TOOL_CAPABILITIES

pytestmark = pytest.mark.acl_integration
REPO = Path(__file__).parents[2]
G1 = "tok-g1"
KEYS = [key("g1", G1, wings=["alpha"])]


# 1. odd tokens -------------------------------------------------------------------------
def test_odd_token_shapes_are_rejected_for_grant_keys_and_the_legacy_token(tmp_path):
    long64 = "a" * 64
    store = load_grants(write_grants(tmp_path, [key("g", "tok"), key("long", long64)]))
    resolver = TokenResolver(store, "L" * 64)
    for token in ("tok", long64, "L" * 64):
        assert resolver.resolve({"Authorization": f"Bearer {token}"}), token
    odd = [
        "tok,x", "tok, tok", "tok,", ",tok", "tok;x", "toké", "tok\u0000", "tоk",
        long64 + "b" * 36, long64 + ",x", "L" * 64 + "x" * 36, "L" * 64 + ",x", "L" * 63,
        "tok tok", "tok\r",
    ]  # fmt: skip
    for token in odd:
        assert resolver.resolve({"Authorization": f"Bearer {token}"}) is None, repr(token)


# 2. exact wing membership ---------------------------------------------------------------
def _call(port, token, name, arguments):
    from _acl_golden import post

    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    return post(port, body, token=token)


@pytest.mark.parametrize(
    "wing", ["alph", "alp", "alpha2", "alphabeta", "a", "xalpha", "_alpha_beta"]
)
def test_wing_membership_is_exact_not_by_prefix(serve_with, wing):
    port, _ = serve_with(KEYS)
    drawer = {"room": "r", "content": "c"}
    diary = {"agent_name": "x", "entry": "e"}
    for tool_name, rest in (("mempalace_add_drawer", drawer), ("mempalace_diary_write", diary)):
        got = _call(port, G1, tool_name, {**rest, "wing": wing})
        ghost = _call(port, G1, tool_name, {**rest, "wing": "no_such_wing"})
        assert got == ghost  # forbidden and nonexistent are byte-equal
        assert json.loads(got[1])["error"]["message"] == "Invalid params: wing not available"
        assert "result" in json.loads(_call(port, G1, tool_name, {**rest, "wing": "alpha"})[1])


# 3 and 4 and 5. what is served after an error, route normalization, every admin route --------
def _raw(port, target, token, method="GET", body=b""):
    head = f"{method} {target} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n"
    if token is not None:
        head += f"Authorization: Bearer {token}\r\n"
    if body:
        head += f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(head.encode() + b"\r\n" + body)
        data = b""
        while chunk := sock.recv(65536):
            data += chunk
    return data


def _status(data):
    return int(data.split(b" ", 2)[1])


def _single_response(data):
    head, _, rest = data.partition(b"\r\n\r\n")
    length = int(
        [ln for ln in head.split(b"\r\n") if ln.lower().startswith(b"content-length")][0].split(
            b":"
        )[1]
    )
    return len(rest) == length  # no byte beyond the one error response


@pytest.mark.parametrize(
    "method, target, token, expect, body",
    [
        ("POST", "/mcp", "nobody", 401, b'{"jsonrpc":"2.0","id":1,"method":"ping"}'),
        ("POST", "/mcp", None, 401, b'{"jsonrpc":"2.0","id":1,"method":"ping"}'),
        ("GET", "/statusz", "nobody", 401, b""),
        ("GET", "/statusz", G1, 403, b""),
        ("GET", "/sync/version_vector", G1, 403, b""),
        ("GET", "/logstream/stream", G1, 403, b""),
    ],
)
def test_nothing_is_served_after_a_401_or_403(
    serve_with, monkeypatch, method, target, token, expect, body
):
    reached = []

    def spy(name):
        return lambda *a, **k: reached.append(name) or {}

    for name in ("_http_dispatch", "_http_status_payload", "_http_serve_sync"):
        monkeypatch.setattr(mcp, name, spy(name))
    monkeypatch.setattr(mcp, "_http_serve_logstream_stream", spy("stream"))
    port, httpd = serve_with(KEYS)
    errors = []
    httpd.handle_error = lambda *a: errors.append(a)  # a handler that blew up after the reply
    data = _raw(port, target, token, method, body)
    assert _status(data) == expect
    assert _single_response(data), data[-200:]
    assert reached == [] and errors == []


ADMIN_ROUTES = [
    "/statusz",
    "/sync/version_vector",
    "/sync/ops",
    "/sync/artifact",
    "/sync/peers",
    "/logstream/stream",
]


@pytest.mark.parametrize("route", ADMIN_ROUTES)
def test_every_admin_route_refuses_a_locked_key_but_not_the_server_token(serve_with, route):
    port, _ = serve_with(KEYS)
    assert _status(_raw(port, route, G1)) == 403
    assert _status(_raw(port, route, "nobody")) == 401
    if route != "/logstream/stream":  # the admin stream never ends
        assert _status(_raw(port, route, LEGACY)) not in (401, 403)


@pytest.mark.parametrize(
    "target",
    [
        "http://h/statusz",
        "http://h/sync/version_vector",
        "http://h/sync/ops?after=0",
        "/statusz?x",
        "/statusz?",
        "/statusz#f",
        "/sync/version_vector#f",
        "/%2fstatusz",
        "/statusz%2f",
        "/./statusz",
        "/x/../statusz",
        "//statusz",
        "///statusz",
        "/statusz;x=1",
        "/STATUSZ",
    ],
)
def test_odd_request_targets_never_give_a_locked_key_what_only_admin_gets(serve_with, target):
    port, _ = serve_with(KEYS)
    admin = _status(_raw(port, target, LEGACY))
    locked = _status(_raw(port, target, G1))
    assert locked != 200 or admin != 200
    # a recognised admin route is 403 for a locked key whatever the admin's own answer is
    assert locked == (404 if admin == 404 else 403)


# 6. loader robustness -----------------------------------------------------------------------
def test_a_large_file_is_read_whole_so_a_trailing_revoked_key_is_honoured(tmp_path):
    keys = [key(f"k{i:03d}", f"tok-{i}", wings=[f"wing{i}"]) for i in range(70)]
    keys[-1]["revoked"] = True
    path = write_grants(tmp_path, keys)
    assert os.path.getsize(path) > 4096 * 2
    store = load_grants(path)
    resolver = TokenResolver(store, "")
    assert resolver.resolve({"Authorization": "Bearer tok-0"}).subject == "k000"
    assert resolver.resolve({"Authorization": "Bearer tok-69"}) is None  # revoked, last key


@pytest.mark.parametrize("raw", [b"caf\xe9", b"caf\xc3", b"\xff\xfe"])
def test_a_non_utf8_file_is_refused_not_silently_repaired(tmp_path, raw):
    path = tmp_path / "g.json"
    path.write_bytes(
        b'{"keys":[{"name":"'
        + raw
        + b'","token_sha256":"'
        + sha("x").encode()
        + b'","wings":["w"]}]}'
    )
    os.chmod(path, 0o600)
    with pytest.raises(GrantsError):
        load_grants(str(path))


def test_symlinks_follow_the_target_and_its_mode(tmp_path):
    """Pinned: a symlink is accepted when the file it points to is 0600 and refused when the
    target is looser, because the mode that counts is the real file's, not the link's."""
    good = Path(write_grants(tmp_path, KEYS, name="real-good.json"))
    loose = Path(write_grants(tmp_path, KEYS, name="real-loose.json", mode=0o644))
    (tmp_path / "good-link").symlink_to(good)
    (tmp_path / "loose-link").symlink_to(loose)
    assert load_grants(str(tmp_path / "good-link")).keys[0].name == "g1"
    with pytest.raises(GrantsError, match="too open"):
        load_grants(str(tmp_path / "loose-link"))


def test_unknown_top_level_keys_such_as_admin_are_refused(tmp_path):
    for extra in ({"admin": True}, {"admin": "yes"}, {"superuser": ["g1"]}, {"Keys": []}):
        with pytest.raises(GrantsError, match="unknown key"):
            load_grants(write_grants(tmp_path, KEYS, **extra))


def test_a_key_whose_hash_is_the_server_token_hash_is_refused_for_the_server(tmp_path):
    path = write_grants(tmp_path, [key("sneaky", "the-server-token", wings=["w"])])
    assert load_grants(path).keys  # fine on its own
    with pytest.raises(GrantsError, match="server token"):
        load_for_server(path, "the-server-token")
    assert hashlib.sha256(b"the-server-token").hexdigest() == sha("the-server-token")


# 7. CLI hand-off ------------------------------------------------------------------------------
def _cli(tmp_path, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MEMPALACE_", "MEMPAL_"))}
    env.update(HOME=str(tmp_path), USERPROFILE=str(tmp_path))
    return subprocess.run(
        [sys.executable, "-m", "mempalace", "--palace", str(tmp_path / "p"), "serve", *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,  # a server that wrongly started would hit this and fail the test
        cwd=REPO,
    )


def test_a_bare_grants_file_flag_is_a_usage_error_and_never_starts(tmp_path):
    done = _cli(tmp_path, "--port", "0", "--grants-file")
    assert done.returncode == 2 and "expected one argument" in done.stderr


@pytest.mark.parametrize(
    "args",
    [
        ["--host", "127.0.0.1"],
        ["--host", "127.0.0.1", "--allow-insecure"],
        ["--host", "127.0.0.1", "--no-token"],
        ["--host", "0.0.0.0", "--allow-insecure"],
        ["--host", "localhost", "--allow-insecure", "--no-token"],
    ],
)
def test_grants_file_without_a_server_token_never_starts_in_any_combination(tmp_path, args):
    grants = write_grants(tmp_path, KEYS)
    done = _cli(tmp_path, "--port", "0", *args, "--grants-file", grants)
    assert done.returncode == 2, done.stderr  # refused by the parent, before any server exists
    assert "Starting MemPalace" not in done.stdout


# 8. the PR 1 policy table, spelled out -----------------------------------------------------------
DENY = """status get_taxonomy traverse find_tunnels graph_stats mesh_peers create_tunnel
list_tunnels delete_tunnel list_hallways delete_hallway follow_tunnels check_duplicate checkpoint
delete_drawer delete_drawers mine delete_by_source sync update_drawer hook_settings
memories_filed_away reconnect event_append task_create event_list event_wait event_ack
artifact_put artifact_get patch_submit""".split()
KG = "kg_query kg_add kg_invalidate kg_supersede kg_timeline kg_stats".split()
SCOPED_NO_WING = "list_wings get_aaak_spec get_drawer get_drawers".split()
SCOPED_WING = "list_rooms search add_drawer list_drawers diary_write diary_read".split()
EXPECTED = {
    **{f"mempalace_{n}": ("deny", None) for n in DENY},
    **{f"mempalace_{n}": ("kg", None) for n in KG},
    **{f"mempalace_{n}": ("scoped", None) for n in SCOPED_NO_WING},
    **{f"mempalace_{n}": ("scoped", "wing") for n in SCOPED_WING},
}
PENDING = {
    f"mempalace_{n}"
    for n in "list_wings list_rooms search get_drawer get_drawers list_drawers diary_read".split()
    + KG
}
ALLOWED = {"mempalace_add_drawer", "mempalace_diary_write", "mempalace_get_aaak_spec"}
LOCKED = acl.Scope("locked", "g", read=frozenset({"alpha"}), write=frozenset({"alpha"}))


def test_the_policy_table_is_exactly_the_documented_one():
    assert len(EXPECTED) == len(TOOL_CAPABILITIES) == 47
    assert ACL_POLICY == EXPECTED  # every kind and wing argument, tool by tool
    assert set(ACL_PENDING) == PENDING
    assert ALLOWED == {t for t, (k, _) in EXPECTED.items() if k == "scoped"} - PENDING


@pytest.mark.parametrize("tool", sorted(TOOL_CAPABILITIES))
def test_each_tool_is_allowed_or_denied_for_a_locked_key_as_documented(tool):
    with acl.request_scope(LOCKED):
        assert acl.denies_tool(tool) is (tool not in ALLOWED), tool
    # and the other scopes: admin never denied, a scope-less request always denied
    with acl.request_scope(acl.ADMIN):
        assert not acl.denies_tool(tool)
    acl.enable()
    assert acl.denies_tool(tool)
