from __future__ import annotations

import argparse
import json
import os
import shutil
import site
import subprocess
import time
from contextlib import contextmanager, suppress
from pathlib import Path

import pytest

from mempalace import daemon, mcp_bridge
from mempalace.config import MempalaceConfig, get_configured_collection_name


def _args(**overrides):
    values = {
        "palace": None,
        "backend": None,
        "read_only": False,
        "no_auto_start": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_collection_name_environment_override_is_authoritative(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(
        json.dumps({"collection_name": "configured_drawers"}),
        encoding="utf-8",
    )

    monkeypatch.setenv("MEMPALACE_COLLECTION_NAME", "env_drawers")
    get_configured_collection_name.cache_clear()

    assert MempalaceConfig(config_dir=config_dir).collection_name == "env_drawers"


def test_daemon_resolves_env_backend_and_collection_once(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv(daemon.STATE_ROOT_ENV, str(tmp_path / "state"))
    monkeypatch.setenv("MEMPALACE_BACKEND", "qdrant")
    monkeypatch.setenv("MEMPALACE_COLLECTION_NAME", "env_drawers")
    monkeypatch.delenv("MEMPALACE_BACKEND_EXPLICIT", raising=False)

    palace = tmp_path / "palace"
    palace.mkdir()

    runtime = daemon.DaemonRuntime(str(palace))

    # No explicit CLI argument was supplied, but the daemon independently
    # resolved and pinned the effective environment-selected backend.
    assert runtime.backend is None
    assert runtime.effective_backend == "qdrant"
    assert runtime.collection_name == "env_drawers"

    # Older/omitting clients inherit both authoritative values.
    runtime._check_mcp_identity(
        {
            "palace_path": runtime.palace_path,
            "backend": "",
            "read_only": False,
            "collection_name": "",
        }
    )

    assert runtime._mcp_identity == {
        "palace_path": runtime.palace_path,
        "backend": "qdrant",
        "read_only": False,
        "collection_name": "env_drawers",
    }

    # Explicit mismatches must still fail.
    with pytest.raises(daemon.DaemonError, match="backend mismatch"):
        runtime._check_mcp_identity(
            {
                "palace_path": runtime.palace_path,
                "backend": "chroma",
                "read_only": False,
                "collection_name": "env_drawers",
            }
        )

    with pytest.raises(daemon.DaemonError, match="identity mismatch"):
        runtime._check_mcp_identity(
            {
                "palace_path": runtime.palace_path,
                "backend": "qdrant",
                "read_only": False,
                "collection_name": "other_drawers",
            }
        )


def test_bridge_env_identity_matches_effective_daemon_health(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("MEMPALACE_BACKEND", "qdrant")
    monkeypatch.setenv("MEMPALACE_COLLECTION_NAME", "env_drawers")
    monkeypatch.delenv("MEMPALACE_BACKEND_EXPLICIT", raising=False)
    get_configured_collection_name.cache_clear()

    palace = tmp_path / "palace"
    palace.mkdir()

    args = _args(palace=str(palace))
    identity = mcp_bridge.build_daemon_identity(args)
    captured = {}

    class FakeClient:
        def health(self, *, timeout):
            assert timeout == 5.0
            return {
                "palace_path": identity["palace_path"],
                "backend": "qdrant",
                "collection_name": "env_drawers",
            }

    def fake_ensure_client(palace_path, *, backend, auto_start):
        captured.update(
            palace_path=palace_path,
            backend=backend,
            auto_start=auto_start,
        )
        return FakeClient()

    monkeypatch.setattr(daemon, "ensure_client", fake_ensure_client)

    client = mcp_bridge.connect_daemon(args, identity)

    assert isinstance(client, FakeClient)
    assert identity["backend"] == "qdrant"
    assert identity["collection_name"] == "env_drawers"

    # The ordinary MEMPALACE_BACKEND value is inherited by the child process.
    # Do not promote it to an explicit CLI override, because an actual
    # config-file backend still has its documented precedence.
    assert captured == {
        "palace_path": daemon.canonical_palace_path(str(palace)),
        "backend": None,
        "auto_start": True,
    }


def test_bridge_health_rejects_explicit_collection_mismatch(tmp_path):
    palace = daemon.canonical_palace_path(str(tmp_path / "palace"))
    identity = {
        "palace_path": palace,
        "backend": "",
        "read_only": False,
        "collection_name": "expected_drawers",
    }

    with pytest.raises(mcp_bridge.BridgeError, match="different collection"):
        mcp_bridge.validate_daemon_health(
            {
                "palace_path": palace,
                "backend": "chroma",
                "collection_name": "other_drawers",
            },
            identity,
        )


@contextmanager
def _using_state_root(path: Path):
    previous = os.environ.get(daemon.STATE_ROOT_ENV)
    os.environ[daemon.STATE_ROOT_ENV] = str(path)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(daemon.STATE_ROOT_ENV, None)
        else:
            os.environ[daemon.STATE_ROOT_ENV] = previous


def _stop_test_daemon(palace_path: str) -> None:
    client = daemon.get_client_if_running(
        palace_path,
        health_timeout=1.0,
    )
    if client is not None:
        with suppress(Exception):
            client.shutdown()

    deadline = time.monotonic() + 10.0
    while daemon.endpoint_path(palace_path).exists() and time.monotonic() < deadline:
        time.sleep(0.05)


def test_installed_mempalace_mcp_env_identity_round_trip(tmp_path):
    executable = shutil.which("mempalace-mcp")
    assert executable is not None, (
        'mempalace-mcp is not installed; run `python3 -m pip install -e ".[dev]"`'
    )

    home = tmp_path / "home"
    palace = tmp_path / "palace"
    state_root = tmp_path / "daemon-state"

    home.mkdir()
    palace.mkdir()
    state_root.mkdir()

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            # Isolating HOME also changes Python's default user-site path.
            # Preserve the user installation base so a console script installed
            # under ~/.local can still import the editable mempalace package.
            "PYTHONUSERBASE": str(site.getuserbase()),
            daemon.STATE_ROOT_ENV: str(state_root),
            "MEMPALACE_BACKEND": "qdrant",
            "MEMPALACE_COLLECTION_NAME": "e2e_drawers",
        }
    )

    for key in (
        "MEMPALACE_BACKEND_EXPLICIT",
        "MEMPALACE_MCP_DISABLE_DAEMON",
        "MEMPALACE_MCP_READ_ONLY",
        "PYTHONNOUSERSITE",
    ):
        env.pop(key, None)

    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "ping",
    }
    palace_path = daemon.canonical_palace_path(str(palace))

    try:
        completed = subprocess.run(
            [executable, "--palace", str(palace)],
            input=json.dumps(request) + "\n",
            text=True,
            capture_output=True,
            env=env,
            timeout=60,
            check=False,
        )

        assert completed.returncode == 0, completed.stderr

        output_lines = [line for line in completed.stdout.splitlines() if line.strip()]
        assert output_lines, f"no JSON-RPC response; stderr={completed.stderr!r}"

        assert json.loads(output_lines[-1]) == {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {},
        }

        with _using_state_root(state_root):
            endpoint = json.loads(daemon.endpoint_path(palace_path).read_text(encoding="utf-8"))
            client = daemon.DaemonClient(palace_path)
            health = client.health(timeout=5.0)

        assert endpoint["backend"] == "qdrant"
        assert endpoint["collection_name"] == "e2e_drawers"

        assert health["backend"] == "qdrant"
        assert health["collection_name"] == "e2e_drawers"
        assert health["mcp_identity"] == {
            "palace_path": palace_path,
            "backend": "qdrant",
            "read_only": False,
            "collection_name": "e2e_drawers",
        }
    finally:
        with _using_state_root(state_root):
            _stop_test_daemon(palace_path)
