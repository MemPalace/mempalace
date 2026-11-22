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
        "PYTHONUTF8": "1",
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


@pytest.mark.parametrize(
    ("source", "mode"),
    [("chats", "projects"), ("docs", "convos"), (".mempalace/palaces", "projects")],
)
def test_cli_refuses_mining_outside_scope(local_cli, source, mode):
    _, _, invoke = local_cli
    assert invoke("create", "handbook", "--source", "projects=docs").returncode == 0
    result = invoke("run", "handbook", "--", "mine", source, "--mode", mode, "--dry-run")
    assert result.returncode == 2, result.stdout + result.stderr
    assert "outside the local palace scope" in result.stderr


def test_mcp_applies_scope_to_mine_and_keeps_valid_source_usable(local_cli):
    _, _, invoke = local_cli
    assert invoke("create", "handbook", "--source", "projects=docs").returncode == 0
    requests = [
        {
            "jsonrpc": "2.0",
            "id": index,
            "method": "tools/call",
            "params": {
                "name": "mempalace_mine",
                "arguments": {"source": source, "mode": "projects", "dry_run": True},
            },
        }
        for index, source in enumerate(("chats", "docs"), start=1)
    ]
    result = invoke("mcp", "handbook", input="\n".join(map(json.dumps, requests)) + "\n")
    assert result.returncode == 0, result.stderr
    refused, allowed = [
        json.loads(json.loads(line)["result"]["content"][0]["text"])
        for line in result.stdout.splitlines()
    ]
    assert refused["success"] is False
    assert "outside the local palace scope" in refused["error"]
    assert allowed["success"] is True


def test_mcp_knowledge_graph_does_not_read_another_palace(local_cli):
    _, ambient, invoke = local_cli
    for name in ("documents", "decisions"):
        assert invoke("create", name, "--source", "projects=docs").returncode == 0

    def call(palace, tool, arguments):
        request = {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": f"mempalace_{tool}", "arguments": arguments},
        }
        result = invoke("mcp", palace, input=json.dumps(request) + "\n")
        assert result.returncode == 0, result.stderr
        return json.loads(json.loads(result.stdout)["result"]["content"][0]["text"])

    subject = "Synthetic Orchard"
    write = call(
        "documents",
        "kg_add",
        {"subject": subject, "predicate": "uses", "object": "Synthetic Pebble"},
    )
    assert write["success"] is True
    assert call("documents", "kg_query", {"entity": subject})["count"] == 1
    assert call("decisions", "kg_query", {"entity": subject})["count"] == 0
    assert not ambient.exists()


def test_multiple_sources_and_modes_share_one_selected_palace(local_cli):
    workspace, _, invoke = local_cli
    (workspace / "visuals").mkdir()
    (workspace / "visuals" / "diagram.png").write_bytes(b"reference-only test data")
    assert (
        invoke(
            "create",
            "research",
            "--source",
            "projects=docs",
            "--source",
            "convos=chats",
            "--source",
            "media=visuals",
            "--source",
            "extract=docs",
        ).returncode
        == 0
    )
    for source, option, mode in [
        ("docs", "--mode", "projects"),
        ("chats", "--mode", "convos"),
        ("visuals", "--source", "media"),
        ("docs", "--mode", "extract"),
    ]:
        result = invoke("run", "research", "--", "mine", source, option, mode, "--dry-run")
        assert result.returncode == 0, result.stderr
    refused = invoke("run", "research", "--", "mine", "docs", "--source", "media", "--dry-run")
    assert refused.returncode == 2


def test_state_cannot_be_selected_as_a_source(local_cli):
    _, _, invoke = local_cli
    assert invoke("create", "one", "--source", "projects=docs").returncode == 0
    assert invoke("create", "two", "--source", "projects=.mempalace/palaces/one").returncode != 0


def test_source_replaced_by_symlink_is_not_mined(local_cli):
    workspace, _, invoke = local_cli
    assert invoke("create", "handbook", "--source", "projects=docs").returncode == 0
    (workspace / "docs").rename(workspace / "original-docs")
    try:
        (workspace / "docs").symlink_to(workspace / "chats", target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")
    result = invoke("run", "handbook", "--", "mine", "docs", "--dry-run")
    assert result.returncode == 2
    assert "outside the local palace scope" in result.stderr


def test_document_and_conversation_drawers_stay_in_distinct_real_indexes(tmp_path, monkeypatch):
    from mempalace import paths
    from mempalace.config import MempalaceConfig
    from mempalace.convo_miner import mine_convos
    from mempalace.local_palaces import LocalPalace
    from mempalace.miner import mine
    from mempalace.searcher import search_memories

    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide.md").write_text(
        "Topaz orchard is the synthetic phrase for this very specific document.",
        encoding="utf-8",
    )
    (tmp_path / "chats").mkdir()
    (tmp_path / "chats" / "session.txt").write_text(
        "> What is the marker for the conversation fixture?\nJasper harbor is our distinct synthetic answer.\n",
        encoding="utf-8",
    )
    docs = LocalPalace.create(tmp_path, "documents", ["projects=docs"])
    chats = LocalPalace.create(tmp_path, "discussions", ["convos=chats"])
    global_palace = tmp_path / "global"
    monkeypatch.setenv("MEMPALACE_PALACE_PATH", str(global_palace))

    for profile, source_path, ingest, needle, other in [
        (docs, "docs", mine, "Topaz orchard", "Jasper harbor"),
        (chats, "chats", mine_convos, "Jasper harbor", "Topaz orchard"),
    ]:
        monkeypatch.setenv(paths.LOCAL_ROOT_ENV, str(profile.root))
        palace = MempalaceConfig().palace_path
        ingest(str(tmp_path / source_path), palace)
        for query in (needle, other):
            result = search_memories(query, palace)
            content = "\n".join(hit["text"] for hit in result["results"])
            assert other not in content
            if query == needle:
                assert needle in content

    monkeypatch.delenv(paths.LOCAL_ROOT_ENV)
    assert MempalaceConfig().palace_path == str(global_palace)
    assert not global_palace.exists()
