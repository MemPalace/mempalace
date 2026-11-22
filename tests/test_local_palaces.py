import json
import os
import subprocess
import sys

import pytest


@pytest.fixture
def local_cli(tmp_path):
    workspace = tmp_path / "example with spaces"
    workspace.mkdir()
    (workspace / "docs").mkdir()
    (workspace / "chats").mkdir()
    ambient = tmp_path / "ambient"
    env = {
        **os.environ,
        "MEMPALACE_CONFIG_DIR": str(ambient / "config"),
        "MEMPALACE_PALACE_PATH": str(ambient / "palace"),
        "MEMPALACE_BACKEND": "unexpected-ambient-backend",
    }

    def invoke(*args, input=None):
        return subprocess.run(
            [sys.executable, "-m", "mempalace", "local", *args],
            cwd=workspace,
            env=env,
            input=input,
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=30,
            check=False,
        )

    return workspace, ambient, invoke


def test_create_list_and_status_bind_named_storage(local_cli):
    workspace, ambient, invoke = local_cli
    assert invoke("create", "handbook", "--source", "projects=docs").returncode == 0
    assert invoke("create", "decisions", "--source", "convos=chats").returncode == 0

    listing = invoke("list", "--json")
    assert listing.returncode == 0, listing.stderr
    assert {row["name"]: row["sources"] for row in json.loads(listing.stdout)} == {
        "decisions": {"convos": ["chats"]},
        "handbook": {"projects": ["docs"]},
    }
    status = invoke("run", "handbook", "--", "status")
    assert status.returncode == 0, status.stderr
    assert str(workspace / ".mempalace" / "palaces" / "handbook" / "palace") in status.stdout
    assert not ambient.exists()


def test_create_does_not_replace_existing_scope(local_cli):
    _, _, invoke = local_cli
    assert invoke("create", "handbook", "--source", "projects=docs").returncode == 0
    duplicate = invoke("create", "handbook", "--source", "convos=chats")
    assert duplicate.returncode != 0
    assert "already exists" in duplicate.stderr
    assert json.loads(invoke("list", "--json").stdout)[0]["sources"] == {"projects": ["docs"]}


@pytest.mark.parametrize("name", ["../escape", "CON", "Docs", "draft."])
def test_create_rejects_nonportable_names(local_cli, name):
    workspace, _, invoke = local_cli
    assert invoke("create", name, "--source", "projects=docs").returncode != 0
    assert not (workspace / ".mempalace").exists()


def test_missing_or_corrupt_profile_does_not_fall_back(local_cli):
    workspace, ambient, invoke = local_cli
    absent = invoke("run", "missing", "--", "status")
    assert absent.returncode != 0
    assert "missing" in absent.stderr
    assert invoke("create", "handbook", "--source", "projects=docs").returncode == 0
    (workspace / ".mempalace" / "palaces" / "handbook" / "scope.json").write_text(
        "null", encoding="utf-8"
    )
    assert invoke("run", "handbook", "--", "status").returncode != 0
    assert not ambient.exists()


def test_explicit_destination_overrides_are_refused(local_cli):
    _, ambient, invoke = local_cli
    assert invoke("create", "handbook", "--source", "projects=docs").returncode == 0
    assert invoke("run", "handbook", "--", "--palace", str(ambient), "status").returncode != 0
    assert invoke("run", "handbook", "--", "--backend", "pgvector", "status").returncode != 0
    assert not ambient.exists()


def test_stdio_mcp_reports_the_same_named_palace(local_cli):
    workspace, ambient, invoke = local_cli
    assert invoke("create", "handbook", "--source", "projects=docs").returncode == 0
    request = {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {"name": "mempalace_status", "arguments": {}},
    }
    result = invoke("mcp", "handbook", input=json.dumps(request) + "\n")
    assert result.returncode == 0, result.stderr
    response = json.loads(result.stdout)
    assert response["id"] == 7
    body = json.loads(response["result"]["content"][0]["text"])
    assert body.get("palace_path") == str(
        workspace / ".mempalace" / "palaces" / "handbook" / "palace"
    ), body
    assert not ambient.exists()
