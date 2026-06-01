"""Tests for first-class Kiro IDE support.

Covers three surfaces:
  1. normalize._try_kiro_json — parsing Kiro's session JSON transcripts.
  2. kiro_install — MCP registration, steering, path detection, status.
  3. .kiro-plugin artifacts staying in sync with the install module + that
     the kiro harness is wired through hooks_cli / the CLI.
"""

import json
from pathlib import Path

import pytest

from mempalace.normalize import (
    _try_kiro_json,
    _try_normalize_json,
    normalize,
)
from mempalace import kiro_install as k

REPO_ROOT = Path(__file__).resolve().parents[1]


# ── _try_kiro_json ──────────────────────────────────────────────────────


def _session(history):
    return {
        "sessionId": "sess-1",
        "title": "t",
        "workspaceDirectory": "/Users/me/code/app",
        "history": history,
    }


def test_kiro_json_valid_string_and_list_content():
    data = _session(
        [
            {"message": {"role": "user", "content": [{"type": "text", "text": "Why fail?"}]}},
            {"message": {"role": "assistant", "content": "Cookie not set."}},
        ]
    )
    out = _try_kiro_json(data)
    assert out is not None
    assert "> Why fail?" in out
    assert "Cookie not set." in out


def test_kiro_json_skips_injected_system_prompt():
    data = _session(
        [
            {"message": {"role": "user", "content": "<identity>You are Kiro...</identity>"}},
            {"message": {"role": "user", "content": [{"type": "text", "text": "real question"}]}},
            {"message": {"role": "assistant", "content": "real answer"}},
        ]
    )
    out = _try_kiro_json(data)
    assert out is not None
    assert "You are Kiro" not in out
    assert "> real question" in out
    assert "real answer" in out


def test_kiro_json_you_are_kiro_prefix_skipped():
    data = _session(
        [
            {"message": {"role": "user", "content": "You are Kiro, an AI assistant. Do X."}},
            {"message": {"role": "user", "content": [{"type": "text", "text": "hello there"}]}},
            {"message": {"role": "assistant", "content": "hi"}},
        ]
    )
    out = _try_kiro_json(data)
    assert out is not None
    assert "an AI assistant" not in out
    assert "> hello there" in out


def test_kiro_json_too_few_messages():
    data = _session([{"message": {"role": "user", "content": "only one"}}])
    assert _try_kiro_json(data) is None


def test_kiro_json_skips_empty_turns():
    data = _session(
        [
            {"message": {"role": "user", "content": [{"type": "text", "text": "  "}]}},
            {"message": {"role": "user", "content": "Q"}},
            {"message": {"role": "assistant", "content": "A"}},
        ]
    )
    out = _try_kiro_json(data)
    assert out is not None
    assert "> Q" in out


def test_kiro_json_non_dict_entries_tolerated():
    data = _session(
        [
            "not a dict",
            {"message": {"role": "user", "content": "Q"}},
            {"message": "also not a dict"},
            {"message": {"role": "assistant", "content": "A"}},
        ]
    )
    out = _try_kiro_json(data)
    assert out is not None
    assert "> Q" in out


# ── detection negatives (no false positives) ────────────────────────────


def test_kiro_json_rejects_non_dict():
    assert _try_kiro_json([{"role": "user", "content": "hi"}]) is None


def test_kiro_json_requires_session_id():
    # history present but no sessionId -> not a Kiro transcript
    assert _try_kiro_json({"history": [{"message": {"role": "user", "content": "Q"}}]}) is None


def test_kiro_json_requires_history_list():
    assert _try_kiro_json({"sessionId": "x", "history": "nope"}) is None


def test_kiro_json_does_not_match_chatgpt():
    assert _try_kiro_json({"mapping": {}}) is None


def test_kiro_does_not_break_claude_ai_flat_list():
    # A Claude.ai flat-list export must still parse via the dispatch chain.
    data = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there"},
    ]
    out = _try_normalize_json(json.dumps(data))
    assert out is not None
    assert "> Hello" in out


# ── normalize() end-to-end on a Kiro .json file ─────────────────────────


def test_normalize_kiro_session_file(tmp_path):
    session = _session(
        [
            {
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "switch to GraphQL"}],
                }
            },
            {"message": {"role": "assistant", "content": "Good call."}},
            {"message": {"role": "user", "content": [{"type": "text", "text": "caching?"}]}},
            {"message": {"role": "assistant", "content": "Persisted queries."}},
        ]
    )
    f = tmp_path / "sess-1.json"
    f.write_text(json.dumps(session))
    out = normalize(str(f))
    assert "> switch to GraphQL" in out
    assert "Persisted queries." in out
    assert sum(1 for line in out.split("\n") if line.startswith(">")) == 2


# ── kiro_install: mcp.json + steering ───────────────────────────────────


def test_install_merges_without_clobber(tmp_path):
    base = tmp_path / ".kiro"
    (base / "settings").mkdir(parents=True)
    (base / "settings" / "mcp.json").write_text(
        json.dumps({"mcpServers": {"other": {"command": "x"}}, "top": 1})
    )
    k.install(local=str(tmp_path), palace="/tmp/p")
    cfg = json.loads((base / "settings" / "mcp.json").read_text())
    assert cfg["mcpServers"]["other"] == {"command": "x"}  # untouched
    assert cfg["top"] == 1  # unrelated top-level key preserved
    assert cfg["mcpServers"]["mempalace"]["command"] == "mempalace-mcp"
    assert cfg["mcpServers"]["mempalace"]["args"] == ["--palace", "/tmp/p"]


def test_install_writes_steering_with_always_inclusion(tmp_path):
    k.install(local=str(tmp_path))
    steering = tmp_path / ".kiro" / "steering" / "mempalace.md"
    assert steering.exists()
    assert steering.read_text().startswith("---\ninclusion: always\n---")


def test_uninstall_removes_only_mempalace(tmp_path):
    base = tmp_path / ".kiro"
    (base / "settings").mkdir(parents=True)
    (base / "settings" / "mcp.json").write_text(
        json.dumps({"mcpServers": {"other": {"command": "x"}}})
    )
    k.install(local=str(tmp_path))
    k.uninstall(local=str(tmp_path))
    cfg = json.loads((base / "settings" / "mcp.json").read_text())
    assert "mempalace" not in cfg["mcpServers"]
    assert "other" in cfg["mcpServers"]
    assert not (base / "steering" / "mempalace.md").exists()


def test_autoapprove_excludes_destructive_deletes():
    assert "mempalace_search" in k.AUTO_APPROVE
    assert "mempalace_delete_drawer" not in k.AUTO_APPROVE
    assert "mempalace_delete_tunnel" not in k.AUTO_APPROVE


def test_status_reports_registration(tmp_path):
    lines = k.status(local=str(tmp_path))
    assert any("not registered" in line for line in lines)
    k.install(local=str(tmp_path))
    lines = k.status(local=str(tmp_path))
    assert any("registered" in line and "not registered" not in line for line in lines)


# ── path detection ──────────────────────────────────────────────────────


def test_session_dir_detection_via_env(tmp_path, monkeypatch):
    agent = tmp_path / "globalStorage" / "kiro.kiroagent"
    (agent / "workspace-sessions" / "abc").mkdir(parents=True)
    monkeypatch.setenv("MEMPALACE_KIRO_AGENT_DIR", str(agent))
    assert k.kiro_agent_dir() == agent
    assert k.kiro_sessions_dir() == agent / "workspace-sessions"


def test_sync_without_kiro_returns_guidance():
    lines = k.sync(agent_dir="/nonexistent/kiro/path")
    assert any("Could not locate" in line for line in lines)
    # must not raise / must not require chromadb on the no-op path
    assert any("--mode convos" in line for line in lines)


# ── .kiro-plugin artifacts stay in sync with the module ─────────────────


def test_plugin_mcp_json_matches_module():
    plugin = json.loads((REPO_ROOT / ".kiro-plugin" / "mcp.json").read_text())
    expected = {"mcpServers": {k.SERVER_KEY: k._mcp_entry("mempalace-mcp", None)}}
    assert plugin == expected


def test_plugin_steering_matches_module():
    plugin_steering = (REPO_ROOT / ".kiro-plugin" / "steering" / k.STEERING_FILENAME).read_text()
    assert plugin_steering == k.STEERING_CONTENT


# ── kiro harness wired through ──────────────────────────────────────────


def test_kiro_harness_supported():
    from mempalace.hooks_cli import SUPPORTED_HARNESSES

    assert "kiro" in SUPPORTED_HARNESSES


# ── _is_ai_tool_path (needs chromadb via convo_miner import) ────────────


def test_kiro_path_routes_to_wing_api():
    pytest.importorskip("chromadb")
    from mempalace.convo_miner import _is_ai_tool_path

    kiro_path = Path(
        "/Users/me/Library/Application Support/Kiro/User/globalStorage/"
        "kiro.kiroagent/workspace-sessions/abc/sess.json"
    )
    assert _is_ai_tool_path(kiro_path) is True


# ── exec-log splicing (correct agent data, parity with kiro-recall) ──────

from mempalace import kiro_ingest  # noqa: E402
from mempalace.normalize import _KIRO_ASSISTANT_STUB  # noqa: E402


def _build_kiro_tree(tmp_path, history, exec_objects):
    """Create a fake Kiro agent dir with a workspace-session + exec store.

    Returns the path to the session transcript file.
    """
    agent = tmp_path / "globalStorage" / "kiro.kiroagent"
    # transcript: <agent>/workspace-sessions/<wsHash>/<sessionId>.json
    sess_dir = agent / "workspace-sessions" / "d3JrSGFzaA"
    sess_dir.mkdir(parents=True)
    session = {
        "sessionId": "sess-exec-1",
        "title": "exec splice",
        "workspaceDirectory": "/Users/me/code/app",
        "history": history,
    }
    sess_file = sess_dir / "sess-exec-1.json"
    sess_file.write_text(json.dumps(session))
    # exec store: <agent>/<wsHash>/<chatHash>/<leaf> (depth-2 dirs, no ext)
    for i, obj in enumerate(exec_objects):
        leaf_dir = agent / "wsHashDir" / f"chatHashDir{i}"
        leaf_dir.mkdir(parents=True, exist_ok=True)
        (leaf_dir / f"exec{i}").write_text(json.dumps(obj))
    kiro_ingest.clear_cache()
    return sess_file


def test_exec_splice_replaces_stub(tmp_path):
    history = [
        {
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "Why does login fail?"}],
            },
            "executionId": "e1",
        },
        {"message": {"role": "assistant", "content": "On it."}, "executionId": "exec-A"},
    ]
    exec_objects = [
        {
            "executionId": "exec-A",
            "chatSessionId": "sess-exec-1",
            "actions": [
                {"actionType": "reasoning", "output": {"message": "Checking the cookie flags."}},
                {
                    "actionType": "say",
                    "output": {"message": "The session cookie lacked SameSite=None; Secure."},
                },
                {"actionType": "tool_call", "output": {"message": "(ignored non-prose action)"}},
            ],
        }
    ]
    sess_file = _build_kiro_tree(tmp_path, history, exec_objects)
    out = normalize(str(sess_file))
    assert "The session cookie lacked SameSite=None; Secure." in out
    assert "Checking the cookie flags." in out  # reasoning included
    assert "On it." not in out  # stub replaced
    assert "(ignored non-prose action)" not in out  # non say/reasoning dropped


def test_no_exec_store_keeps_stub(tmp_path):
    history = [
        {
            "message": {"role": "user", "content": [{"type": "text", "text": "Question one here"}]},
            "executionId": "e1",
        },
        {"message": {"role": "assistant", "content": "On it."}, "executionId": "exec-missing"},
        {
            "message": {"role": "user", "content": [{"type": "text", "text": "Question two here"}]},
            "executionId": "e2",
        },
        {"message": {"role": "assistant", "content": "A real answer."}, "executionId": "e3"},
    ]
    sess_file = _build_kiro_tree(tmp_path, history, [])  # no exec files
    out = normalize(str(sess_file))
    assert "On it." in out  # nothing to splice -> stub preserved verbatim
    assert "A real answer." in out


def test_exec_map_only_for_kiro_session_paths(tmp_path):
    # A plain JSON file not under kiro.kiroagent/workspace-sessions -> None.
    f = tmp_path / "random.json"
    f.write_text("{}")
    assert kiro_ingest.exec_map_for_session(str(f)) is None


def test_build_exec_map_parses_and_skips(tmp_path):
    agent = tmp_path / "globalStorage" / "kiro.kiroagent"
    (agent / "workspace-sessions").mkdir(parents=True)  # must be skipped
    good = agent / "ws" / "chat"
    good.mkdir(parents=True)
    (good / "exec-good").write_text(
        json.dumps(
            {
                "executionId": "X1",
                "actions": [{"actionType": "say", "output": {"message": "hello world"}}],
            }
        )
    )
    (good / "exec-noprose").write_text(
        json.dumps(
            {
                "executionId": "X2",
                "actions": [{"actionType": "tool_call", "output": {"message": "noise"}}],
            }
        )
    )
    (good / "exec-garbage").write_text("not json {{{")
    m = kiro_ingest.build_exec_map(agent)
    assert m == {"X1": "hello world"}  # X2 (no prose) + garbage skipped


def test_tool_and_unknown_roles_kept():
    sess = {
        "sessionId": "s",
        "history": [
            {"message": {"role": "user", "content": "the question"}},
            {"message": {"role": "tool", "content": "TOOL OUTPUT: build passed"}},
            {"message": {"role": "weird", "content": "ambiguous-role text"}},
        ],
    }
    out = _try_kiro_json(sess)
    assert out is not None
    assert "TOOL OUTPUT: build passed" in out  # tool frame kept
    assert "ambiguous-role text" in out  # unknown role kept


def test_stub_constant_in_sync():
    assert _KIRO_ASSISTANT_STUB == kiro_ingest.ASSISTANT_STUB
