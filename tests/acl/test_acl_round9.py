"""Tokens are case-sensitive, and a refused start never prints a token."""

import hashlib
import http.client
import os
import subprocess
import sys
from pathlib import Path

import pytest
from _acl_golden import post
from _acl_helpers import LEGACY, key, write_grants

pytestmark = pytest.mark.acl_integration
G1 = "tok-g1"
KEYS = [key("g1", G1, wings=["alpha"])]
PING = {"jsonrpc": "2.0", "id": 1, "method": "ping"}


def _cased(token):
    return {token.swapcase(), token.upper(), token.lower(), token.title()} - {token}


def _get_status(port, path, token):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", path, headers={"Authorization": f"Bearer {token}"})
        return conn.getresponse().status
    finally:
        conn.close()


def test_legacy_and_grant_tokens_are_case_sensitive_on_every_route(serve_with):
    port, _ = serve_with(KEYS)
    assert post(port, PING, token=LEGACY)[0] == 200  # the exact token works
    assert post(port, PING, token=G1)[0] == 200
    for exact in (LEGACY, G1):
        variants = _cased(exact)
        assert variants, exact
        for variant in variants:
            assert post(port, PING, token=variant)[0] == 401, variant
            for path in ("/statusz", "/sync/version_vector", "/logstream/stream"):
                assert _get_status(port, path, variant) == 401, (path, variant)


SECRET = "Zq9-DistinctiveServerToken-7f3a"


def _run_cli(tmp_path, args, env_token=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MEMPALACE_", "MEMPAL_"))}
    env.update(HOME=str(tmp_path), USERPROFILE=str(tmp_path))
    if env_token:
        env["MEMPALACE_MCP_HTTP_TOKEN"] = env_token
    return subprocess.run(
        [sys.executable, "-m", "mempalace", "--palace", str(tmp_path / "p"), "serve", *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,  # a server that wrongly started would hit this and fail the test
        cwd=Path(__file__).parents[2],
    )


def _assert_no_token(done, *tokens):
    out = done.stdout + done.stderr
    for token in tokens:
        assert token not in out
        assert hashlib.sha256(token.encode()).hexdigest() not in out


@pytest.mark.parametrize("case", ["missing", "loose", "blank", "reserved", "garbage"])
def test_a_refused_start_never_prints_the_environment_token(tmp_path, case):
    bad = {
        "missing": str(tmp_path / "nope.json"),
        "loose": write_grants(tmp_path, KEYS, mode=0o644, name="loose.json"),
        "blank": "  ",
        "reserved": write_grants(tmp_path, KEYS, name="res.json", default_wing_mode="open"),
        "garbage": str(tmp_path / "garbage.json"),
    }[case]
    if case == "garbage":
        Path(bad).write_text("{{{ not: [valid")
        os.chmod(bad, 0o600)
    done = _run_cli(tmp_path, ["--port", "0", "--grants-file", bad], env_token=SECRET)
    assert done.returncode == 2
    assert "refused" in done.stderr
    _assert_no_token(done, SECRET)


def test_a_refused_start_never_prints_an_auto_generated_token(tmp_path):
    bad = str(tmp_path / "nope.json")
    done = _run_cli(tmp_path, ["--host", "0.0.0.0", "--port", "0", "--grants-file", bad])
    assert done.returncode == 2
    stored = [p.read_text().strip() for p in (tmp_path / ".mempalace").rglob("token")]
    assert stored and all(len(t) >= 16 for t in stored)  # the token file really was generated
    _assert_no_token(done, *stored)


def test_a_tokenless_refusal_has_no_token_to_print(tmp_path):
    done = _run_cli(tmp_path, ["--port", "0", "--grants-file", write_grants(tmp_path, KEYS)])
    assert done.returncode == 2 and "local_only_tokenless" in done.stderr
    _assert_no_token(done, SECRET, G1)
