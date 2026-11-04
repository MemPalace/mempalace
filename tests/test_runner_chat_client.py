"""B2 and B9: the opt-in runner command and closed chat config schema."""

import copy
import json
import sys
from pathlib import Path

import pytest

from tests._relay_helpers import relay_hub_fixture  # noqa: F401
from tests.runner_harness import _private_json, runner, runner_config


@pytest.mark.parametrize("shape", ["harness", "chat_only"])
def test_runner_chat_command_end_to_end(relay_hub, tmp_path, shape):
    h = relay_hub
    m = h.machine()
    env = {"COMMONPLACE_RUNNER_TOKEN": m.get("machine_token", "")}
    record = tmp_path / "record.json"
    chat = {
        "default_cli": "claude",
        "clis": {
            "claude": {
                "argv": [
                    sys.executable,
                    str(Path(__file__).with_name("runner_fake_cli.py").resolve()),
                    "--record",
                    str(record),
                    "--model",
                    "{model}",
                ],
                "default_model": "claude-haiku-5-5",
                "tools": True,
            }
        },
    }
    if shape == "harness":
        config = runner_config(tmp_path, h.hub, m.get("machine_token", ""), chat=chat)
    else:
        config = tmp_path / "chat.json"
        _private_json(
            config,
            {
                "hub_url": f"http://127.0.0.1:{h.hub.port}",
                "token_env": "COMMONPLACE_RUNNER_TOKEN",
                "chat": chat,
            },
        )
    result = runner(["chat", "--config", str(config), "--once"], env)
    assert result.returncode == 0  # RED
    message = "Please list the open cards for this project."
    h.post(h.owner, message)
    result = runner(["chat", "--config", str(config), "--once"], env)
    assert result.returncode == 0
    replies = [r for r in h.chat(h.owner)[1]["replies"] if r["kind"] == "answer"]
    assert len(replies) == 1 and replies[0]["text"] == "Fake answer from the runner."
    observed = json.loads(record.read_text())
    assert message in observed["stdin"] and observed["cwd"] == []
    assert m["machine_token"] not in observed["env"].values()
    index = observed["argv"].index("--model")
    assert observed["argv"][index + 1] == "claude-haiku-5-5"
    assert "chat: answers run your configured CLI" in result.stderr
    assert "runs with its tools on" in result.stderr
    assert message not in result.stdout + result.stderr


def test_chat_config_validation(relay_hub, tmp_path):
    h = relay_hub
    for name in ("claude", "codex", "unknown-cli"):
        executable = tmp_path / name
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o700)
    argv = [
        str(tmp_path / "claude"),
        "-p",
        "--tools",
        "",
        "--strict-mcp-config",
        "--no-session-persistence",
        "--model",
        "{model}",
    ]
    chat = {
        "default_cli": "claude",
        "clis": {
            "claude": {
                "argv": argv,
                "default_model": "claude-haiku-5-5",
            }
        },
    }
    config = {
        "hub_url": f"http://127.0.0.1:{h.hub.port}",
        "token_env": "COMMONPLACE_RUNNER_TOKEN",
        "chat": chat,
    }
    path = tmp_path / "chat.json"
    _private_json(path, config)
    result = runner(["check", "--config", str(path)], {})
    assert result.returncode == 0  # RED
    assert "chat: answers run your configured CLI" in result.stdout
    full = runner_config(tmp_path / "full", h.hub, "synthetic-machine-credential", chat=chat)
    assert runner(["check", "--config", str(full)], {}).returncode == 0

    invalid = {}
    missing_tools = copy.deepcopy(config)
    missing_tools["chat"]["clis"]["claude"]["argv"] = [v for v in argv if v not in ("--tools", "")]
    invalid["a"] = missing_tools
    missing_strict = copy.deepcopy(config)
    missing_strict["chat"]["clis"]["claude"]["argv"].remove("--strict-mcp-config")
    invalid["b"] = missing_strict
    dangerous = copy.deepcopy(config)
    dangerous["chat"]["clis"]["claude"]["argv"].append("--dangerously-skip-permissions")
    invalid["c"] = dangerous
    codex = copy.deepcopy(config)
    codex["chat"] = {
        "default_cli": "codex",
        "clis": {
            "codex": {
                "argv": [
                    str(tmp_path / "codex"),
                    "exec",
                    "--sandbox",
                    "read-only",
                    "--ignore-user-config",
                    "--ephemeral",
                ],
                "default_model": "example-model",
            }
        },
    }
    invalid["d"] = codex
    unknown = copy.deepcopy(config)
    unknown["chat"]["clis"]["claude"]["argv"][0] = str(tmp_path / "unknown-cli")
    invalid["e"] = unknown
    relative = copy.deepcopy(config)
    relative["chat"]["clis"]["claude"]["argv"][0] = "claude"
    invalid["f"] = relative
    embedded = copy.deepcopy(config)
    embedded["chat"]["clis"]["claude"]["argv"][-2:] = ["--model={model}"]
    invalid["g"] = embedded
    knob = copy.deepcopy(config)
    knob["chat"]["poll_seconds"] = 3
    invalid["k"] = knob
    default = copy.deepcopy(config)
    default["chat"]["default_cli"] = "missing"
    invalid["l"] = default
    credential = "synthetic-machine-credential"
    exposed = copy.deepcopy(config)
    exposed["chat"]["clis"]["claude"]["argv"].append(credential)
    invalid["m"] = exposed
    context = copy.deepcopy(config)
    context["chat"]["context"] = "all"
    invalid["n"] = context
    missing_model = copy.deepcopy(config)
    del missing_model["chat"]["clis"]["claude"]["default_model"]
    invalid["o"] = missing_model
    for case, value in invalid.items():
        _private_json(path, value)
        env = {"COMMONPLACE_RUNNER_TOKEN": credential} if case == "m" else {}
        result = runner(["check", "--config", str(path)], env)
        assert result.returncode == 4, case
        assert "config_invalid" in result.stderr, case

    flag_model = copy.deepcopy(config)
    flag_model["chat"]["clis"]["claude"]["default_model"] = "--tools"
    _private_json(path, flag_model)
    result = runner(["check", "--config", str(path)], {})
    assert result.returncode == 4  # RED: a model cannot be an option
    assert "config_invalid" in result.stderr

    for value, label in ((missing_tools, "claude"), (codex, "codex")):
        acknowledged = copy.deepcopy(value)
        acknowledged["chat"]["clis"][label]["tools"] = True
        _private_json(path, acknowledged)
        result = runner(["check", "--config", str(path)], {})
        assert result.returncode == 0
        assert "runs with its tools on" in result.stdout
