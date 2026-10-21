"""Tests for the turnkey `mempalace serve` command (#1877).

These exercise the wrapper's security-relevant behavior — token autogeneration
and 0600 persistence, the secure-by-default non-loopback gate, and that the
bearer token is passed via the environment (never argv, so it can't leak via
``ps``) — without binding a real socket. ``cmd_serve`` ends by exec'ing the real
server; we intercept ``os.execve`` to capture the child invocation instead.
"""

import argparse
import os
import stat

import pytest

from mempalace import cli
from mempalace.server_registry import load_or_create_server_token, server_token_path


class _ExecCalled(Exception):
    """Raised by the patched os.execve to stop cmd_serve at the exec boundary."""


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Point ~ at a temp dir so server token state never touches the real home."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows
    # Don't inherit a token or an opt-out from the ambient environment.
    monkeypatch.delenv("MEMPALACE_MCP_HTTP_TOKEN", raising=False)
    monkeypatch.delenv("MEMPALACE_MCP_HTTP_ALLOW_INSECURE_NO_TOKEN", raising=False)
    return tmp_path


@pytest.fixture
def capture_exec(monkeypatch):
    """Capture the child argv/env cmd_serve would launch, instead of running it.

    cmd_serve takes the os.execve branch on POSIX and the subprocess.run branch
    on Windows. Patch both (rather than forcing os.name, which breaks
    Path.home() on Windows) so the test is platform-agnostic.
    """
    import subprocess

    captured = {}

    def _capture(argv, env):
        captured["argv"] = argv
        captured["env"] = env
        raise _ExecCalled()

    monkeypatch.setattr(cli.os, "execve", lambda path, argv, env: _capture(argv, env))
    monkeypatch.setattr(subprocess, "run", lambda argv, env=None, **kw: _capture(argv, env))
    return captured


def _serve_args(tmp_path, **over):
    base = dict(
        host="127.0.0.1",
        port=8765,
        backend=None,
        global_backend=None,
        palace=str(tmp_path / "palace"),
        token=None,
        tls_cert=None,
        tls_key=None,
        read_only=False,
        allow_insecure=False,
        no_token=False,
    )
    base.update(over)
    return argparse.Namespace(**base)


def test_token_helper_creates_0600_and_reuses(isolated_home):
    palace = str(isolated_home / "palace")
    token1, created1 = load_or_create_server_token(palace)
    assert created1 is True
    assert token1

    path = server_token_path(palace)
    assert path.exists()
    if os.name == "posix":
        # POSIX permission bits aren't meaningful on Windows (files report 0o666).
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600, oct(mode)
        dir_mode = stat.S_IMODE(path.parent.stat().st_mode)
        assert dir_mode == 0o700, oct(dir_mode)

    token2, created2 = load_or_create_server_token(palace)
    assert created2 is False
    assert token2 == token1  # stable across restarts


@pytest.mark.parametrize("leftover", [b"", b"\xff\xfe\x00bad"], ids=["empty", "not-utf8"])
def test_token_helper_replaces_a_file_left_by_a_crash(isolated_home, leftover):
    palace = str(isolated_home / "palace")
    path = server_token_path(palace)
    path.parent.mkdir(parents=True)
    path.write_bytes(leftover)

    token, created = load_or_create_server_token(palace)

    assert token and created is True
    assert path.read_text().strip() == token


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symlinks")
def test_token_helper_never_writes_through_a_symlink(isolated_home):
    palace = str(isolated_home / "palace")
    path = server_token_path(palace)
    path.parent.mkdir(parents=True)
    victim = isolated_home / "victim.txt"
    victim.write_text("")
    try:
        os.symlink(str(victim), str(path))
    except OSError:
        pytest.skip("symlink creation not permitted here")

    token, _created = load_or_create_server_token(palace)

    assert victim.read_text() == ""
    assert not path.is_symlink()
    assert path.read_text().strip() == token


@pytest.mark.parametrize("leftover", [None, b""], ids=["fresh", "empty-leftover"])
def test_concurrent_starts_settle_on_the_token_on_disk(isolated_home, leftover):
    import subprocess
    import sys

    palace = str(isolated_home / "palace")
    path = server_token_path(palace)
    if leftover is not None:
        path.parent.mkdir(parents=True)
        path.write_bytes(leftover)
    child = (
        "import sys;from mempalace import server_registry as s;"
        "print(s.load_or_create_server_token(sys.argv[1])[0])"
    )
    env = dict(os.environ, HOME=str(isolated_home), USERPROFILE=str(isolated_home))
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", child, palace], env=env, stdout=subprocess.PIPE, text=True
        )
        for _ in range(6)
    ]
    tokens = {p.communicate(timeout=120)[0].strip() for p in procs}

    assert tokens == {path.read_text().strip()}


def test_loopback_serve_needs_no_token_by_default(isolated_home, capture_exec, capsys):
    with pytest.raises(_ExecCalled):
        cli.cmd_serve(_serve_args(isolated_home, host="127.0.0.1"))
    env = capture_exec["env"]
    assert "MEMPALACE_MCP_HTTP_TOKEN" not in env
    assert "MEMPALACE_MCP_HTTP_ALLOW_INSECURE_NO_TOKEN" not in env
    assert not server_token_path(str(isolated_home / "palace")).exists()
    out = capsys.readouterr().out
    assert "none (loopback bind)" in out
    assert "Authorization" not in out


def test_loopback_serve_keeps_an_explicit_token(isolated_home, capture_exec, capsys):
    with pytest.raises(_ExecCalled):
        cli.cmd_serve(_serve_args(isolated_home, host="127.0.0.1", token="s3cret-token"))
    env = capture_exec["env"]
    assert env.get("MEMPALACE_MCP_HTTP_TOKEN") == "s3cret-token"
    assert all("s3cret-token" not in part for part in capture_exec["argv"])
    assert "s3cret-token" not in capsys.readouterr().out


def test_proxied_loopback_serve_generates_a_token(isolated_home, capture_exec, monkeypatch):
    monkeypatch.setenv("MEMPALACE_MCP_EXTRA_ALLOWED_HOSTS", "box.example.ts.net")
    with pytest.raises(_ExecCalled):
        cli.cmd_serve(_serve_args(isolated_home, host="127.0.0.1"))
    token = capture_exec["env"].get("MEMPALACE_MCP_HTTP_TOKEN")
    assert token
    assert server_token_path(str(isolated_home / "palace")).read_text().strip() == token


def test_no_token_is_a_no_op_on_loopback(isolated_home, capture_exec):
    with pytest.raises(_ExecCalled):
        cli.cmd_serve(_serve_args(isolated_home, host="127.0.0.1", no_token=True))
    env = capture_exec["env"]
    assert "MEMPALACE_MCP_HTTP_TOKEN" not in env
    assert not server_token_path(str(isolated_home / "palace")).exists()


def test_no_token_is_refused_on_a_network_bind(isolated_home, capture_exec):
    with pytest.raises(SystemExit) as exc:
        cli.cmd_serve(_serve_args(isolated_home, host="0.0.0.0", no_token=True))
    assert exc.value.code == 2
    assert "argv" not in capture_exec


def test_no_token_is_refused_on_a_proxied_loopback_bind(isolated_home, capture_exec, monkeypatch):
    """A proxy publishes this hub, so only --allow-insecure may make it tokenless."""
    monkeypatch.setenv("MEMPALACE_MCP_EXTRA_ALLOWED_HOSTS", "memory.example.com")
    with pytest.raises(SystemExit) as exc:
        cli.cmd_serve(_serve_args(isolated_home, host="127.0.0.1", no_token=True))
    assert exc.value.code == 2
    assert "argv" not in capture_exec


def test_non_loopback_autogenerates_token_in_env_not_argv(isolated_home, capture_exec):
    with pytest.raises(_ExecCalled):
        cli.cmd_serve(_serve_args(isolated_home, host="0.0.0.0"))
    env = capture_exec["env"]
    argv = capture_exec["argv"]
    token = env.get("MEMPALACE_MCP_HTTP_TOKEN")
    assert token, "a token must be generated for a network-exposed bind"
    # Security: the token rides in the env, never on the command line.
    assert all(token not in part for part in argv)
    assert "--token" not in argv
    # And it was persisted for reuse on the next start (0600 on POSIX).
    path = server_token_path(str(isolated_home / "palace"))
    assert path.exists()
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_allow_insecure_skips_token_and_sets_escape_hatch(isolated_home, capture_exec):
    with pytest.raises(_ExecCalled):
        cli.cmd_serve(_serve_args(isolated_home, host="0.0.0.0", allow_insecure=True))
    env = capture_exec["env"]
    assert env.get("MEMPALACE_MCP_HTTP_ALLOW_INSECURE_NO_TOKEN") == "1"
    assert "MEMPALACE_MCP_HTTP_TOKEN" not in env
    assert not server_token_path(str(isolated_home / "palace")).exists()


def test_read_only_flag_forwarded_to_child(isolated_home, capture_exec):
    with pytest.raises(_ExecCalled):
        cli.cmd_serve(_serve_args(isolated_home, read_only=True))
    assert "--read-only" in capture_exec["argv"]


def test_explicit_token_is_used_and_not_in_argv(isolated_home, capture_exec):
    with pytest.raises(_ExecCalled):
        cli.cmd_serve(_serve_args(isolated_home, host="0.0.0.0", token="my-secret-token"))
    env = capture_exec["env"]
    argv = capture_exec["argv"]
    assert env["MEMPALACE_MCP_HTTP_TOKEN"] == "my-secret-token"
    assert all("my-secret-token" not in part for part in argv)
    # An explicitly-provided token is not persisted to the server token file.
    assert not server_token_path(str(isolated_home / "palace")).exists()


def test_tls_paths_forwarded_and_validated(isolated_home, capture_exec, tmp_path):
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    cert.write_text("x")
    key.write_text("x")
    with pytest.raises(_ExecCalled):
        cli.cmd_serve(_serve_args(isolated_home, tls_cert=str(cert), tls_key=str(key)))
    argv = capture_exec["argv"]
    assert "--tls-cert" in argv and "--tls-key" in argv


def test_tls_requires_both_cert_and_key(isolated_home, capture_exec, tmp_path):
    cert = tmp_path / "cert.pem"
    cert.write_text("x")
    with pytest.raises(SystemExit):
        cli.cmd_serve(_serve_args(isolated_home, tls_cert=str(cert), tls_key=None))
