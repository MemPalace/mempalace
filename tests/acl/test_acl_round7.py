"""Exact credential matching, YAML safety, field types, and the real CLI reaching the server."""

import contextlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from _acl_golden import post
from _acl_helpers import LEGACY, key, write_grants

from mempalace import acl
from mempalace.acl.grants import GrantsError, TokenResolver, load_grants

LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
LOCKED = acl.Scope("locked", "g", read=frozenset({"alpha"}), write=frozenset({"alpha"}))


# 1. legacy and grant tokens match exactly, never by prefix ---------------------------
def test_tokens_must_match_exactly_not_by_prefix_or_suffix(tmp_path):
    store = load_grants(write_grants(tmp_path, [key("a", "grant-token", wings=["x"])]))
    resolver = TokenResolver(store, "legacy-token")

    def who(auth):
        found = resolver.resolve({"Authorization": auth})
        return found and found.subject

    assert who("Bearer legacy-token") == "legacy"
    assert who("Bearer grant-token") == "a"
    for bad in (
        "Bearer legacy-tokenEXTRA",
        "Bearer legacy-token ",
        "Bearer legacy-toke",
        "Bearer legacy",
        "Bearer legacy-token\n",
        "Bearer grant-tokenEXTRA",
        "Bearer grant-toke",
        "Bearer grant-token\t",
    ):
        assert who(bad) is None, bad


# 2. YAML is parsed safely: no object construction, no side effects -------------------
@pytest.mark.parametrize(
    "tag",
    ["!!python/object/apply:os.system", "!!python/object/new:os.system", "!!python/name:os.system"],
)
def test_python_yaml_tags_are_refused_and_never_executed(tmp_path, tag):
    marker = tmp_path / "pwned.marker"
    arg = f'["touch {marker}"]'
    value = f"{tag} {arg}" if "name" not in tag else f"{tag} ''"
    path = tmp_path / "evil.yaml"
    path.write_text(f"keys:\n  - {value}\n")
    os.chmod(path, 0o600)
    with pytest.raises(GrantsError):
        load_grants(str(path))
    assert not marker.exists()


# 3 and 4. wing list types and overlap ----------------------------------------------
@pytest.mark.parametrize("field", ["wings", "read_wings"])
@pytest.mark.parametrize("value", ['"ab"', "{a: 1}", "5", "true", "!!set {a, b}", "3.5"])
def test_wing_fields_must_be_real_lists(tmp_path, field, value):
    def load(text):
        path = tmp_path / "g.yaml"
        path.write_text(
            f'keys:\n  - name: a\n    token_sha256: "{"0" * 63}1"\n    {field}: {text}\n'
        )
        os.chmod(path, 0o600)
        return load_grants(str(path))

    assert getattr(load("[a, b]").keys[0], field) == {"a", "b"}  # the harness itself is sound
    with pytest.raises(GrantsError, match="list of wing names"):
        load(value)


@pytest.mark.parametrize(
    "wings, read_wings",
    [(["a", "b"], ["b", "c"]), (["a"], ["a"]), (["A-b"], ["a_b", "z"]), (["x", "y", "z"], ["z"])],
)
def test_a_wing_in_both_lists_is_refused_even_on_partial_overlap(tmp_path, wings, read_wings):
    with pytest.raises(GrantsError, match="both wings and read_wings"):
        load_grants(write_grants(tmp_path, [key("k", "1", wings=wings, read_wings=read_wings)]))
    disjoint = write_grants(tmp_path, [key("k", "1", wings=["a", "b"], read_wings=["c"])], name="d")
    assert load_grants(disjoint).keys[0].read_wings == {"c"}


# 7. read and write wing logic (pins the behaviour PR 2 builds on) ---------------------
def test_scoped_args_checks_read_tools_against_read_wings_and_writes_against_write_wings():
    """No allowed read tool carries a wing argument in PR 1 (they are all pending), but
    ``scoped_args`` is what PR 2 will use for them, so pin it now with tools taken
    straight from the policy table: a read tool needs the wing in ``scope.read``, a
    write tool needs it in ``scope.write``."""
    read_tool, write_tool = "mempalace_list_rooms", "mempalace_add_drawer"
    from mempalace.tool_capabilities import ACL_POLICY, classify_tool

    assert ACL_POLICY[read_tool][1] == ACL_POLICY[write_tool][1] == "wing"
    assert (classify_tool(read_tool), classify_tool(write_tool)) == ("read", "write")
    rw = acl.Scope("locked", "rw", read=frozenset({"alpha"}), write=frozenset({"alpha"}))
    read_only = acl.Scope("locked", "ro", read=frozenset({"alpha"}), write=frozenset())
    write_only = acl.Scope("locked", "wo", read=frozenset(), write=frozenset({"alpha"}))
    ask = {"wing": "Alpha"}  # normalized on the way through
    assert acl.scoped_args(rw, read_tool, ask)["wing"] == "alpha"
    assert acl.scoped_args(rw, write_tool, ask)["wing"] == "alpha"
    assert acl.scoped_args(read_only, read_tool, ask)["wing"] == "alpha"
    assert acl.scoped_args(read_only, write_tool, ask) is None
    assert acl.scoped_args(write_only, read_tool, ask) is None
    assert acl.scoped_args(write_only, write_tool, ask)["wing"] == "alpha"
    assert acl.scoped_args(read_only, read_tool, {"wing": "beta"}) is None


# 8. a fresh process starts with ACL off ----------------------------------------------------
def _clean_env(home):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MEMPALACE_", "MEMPAL_"))}
    env.update(HOME=str(home), USERPROFILE=str(home))
    return env


def test_fresh_process_import_state_is_acl_off_and_admin(tmp_path):
    code = (
        "from mempalace import acl\n"
        "assert acl._on == [False] and acl.current_scope() is acl.ADMIN\n"
        "import mempalace.mcp_server\n"
        "assert acl.current_scope() is acl.ADMIN and not acl.denies_tool('mempalace_status')\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code],
        env=_clean_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=120,
        cwd=Path(__file__).parents[2],
    )
    assert done.returncode == 0, done.stderr


# 5 and 6. the real CLI --------------------------------------------------------------------
@contextlib.contextmanager
def running_server(tmp_path, *extra):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    log = open(tmp_path / "server.log", "w")
    cmd = [sys.executable, "-m", "mempalace", "--palace", str(tmp_path / "palace")]
    cmd += ["serve", "--host", "127.0.0.1", "--port", str(port), "--token", LEGACY, *extra]
    proc = subprocess.Popen(
        cmd,
        env=_clean_env(tmp_path),
        stdout=log,
        stderr=log,
        cwd=Path(__file__).parents[2],
    )
    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            assert proc.poll() is None, (
                f"server exited {proc.returncode}: "
                + ((tmp_path / "server.log").read_text()[-800:])
            )
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    break
            except OSError:
                time.sleep(0.3)
        else:
            raise AssertionError("server never listened")
        yield port
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        log.close()


def _tool_names(port, token):
    status, body = post(port, LIST, token=token)
    assert status == 200
    return sorted(t["name"] for t in json.loads(body)["result"]["tools"])


def test_cli_grants_flag_reaches_the_server_which_comes_up_locked(tmp_path):
    grants = write_grants(tmp_path, [key("g1", "tok-g1", wings=["alpha"])])
    with running_server(tmp_path, "--grants-file", grants) as port:
        assert _tool_names(port, "tok-g1") == [
            "mempalace_add_drawer",
            "mempalace_diary_write",
            "mempalace_get_aaak_spec",
        ]
        assert post(port, LIST, token="nobody")[0] == 401
        assert post(port, LIST, token=None)[0] == 401
        assert len(_tool_names(port, LEGACY)) > 40  # the legacy token is still admin


def test_cli_without_grants_flag_starts_open_and_serves(tmp_path):
    with running_server(tmp_path) as port:
        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/healthz")
        resp = conn.getresponse()
        assert (resp.status, resp.read()) == (200, b"ok\n")
        assert len(_tool_names(port, LEGACY)) > 40  # ACL off: the whole catalogue
        assert post(port, LIST, token="nobody")[0] == 401  # the legacy token check is unchanged
