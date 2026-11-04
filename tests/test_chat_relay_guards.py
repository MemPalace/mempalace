"""RG1–RG7: safety contracts shared by the pin and the relay implementation."""

import ast
import json
import sys
from pathlib import Path

from commonplace.gateway import _public_route
from commonplace.social.accounts import signup_enabled
from tests._console_helpers import optional
from tests._relay_helpers import relay_hub_fixture  # noqa: F401
from tests.runner_harness import _private_json, runner, runner_config
from tests.test_chat_guards import _host_specific
from tests.test_console_guards import PIN_PUBLIC

ROOT = Path(__file__).resolve().parents[1]


def test_hub_never_calls_a_model(relay_hub, monkeypatch):
    h = relay_hub
    calls = h.fake_model(monkeypatch)
    attempts = h.no_outbound(monkeypatch)
    h.post(h.owner, "No machine is online.")
    h.drain()
    h.drain(now=h.late())
    m = h.machine()
    h.poll(m)
    h.post(h.owner, "Use only my runner.")
    status, body = h.poll(m)
    if status == 200 and isinstance(body, dict) and body.get("job"):
        h.answer(m, body["job"], body.get("lease_token", ""), "ok")
    assert calls == [] and attempts == []


def test_card_runner_routes_unchanged(relay_hub):
    h = relay_hub
    m = h.machine()
    token = m.get("machine_token", "")
    assert h.request(token, "POST", "/api/runner/lease", {})[0] == 204
    assert h.get(token, "/api/runner/jobs")[0] == 200


def test_public_surface_unchanged(relay_hub, monkeypatch):
    h = relay_hub
    installed = set(h.gateway.routes) | {(m, p) for m, p, _ in h.gateway.prefix_routes}
    assert {(m, p) for m, p in installed if _public_route(m, p)} == PIN_PUBLIC
    assert h.request(None, "POST", "/api/runner/chat/lease", {"clis": ["claude"]})[0] == 401
    monkeypatch.delenv("COMMONPLACE_SIGNUP", raising=False)
    assert signup_enabled() is False


def test_runner_config_shapes(relay_hub, tmp_path):
    h = relay_hub
    path = runner_config(tmp_path, h.hub, "synthetic-machine-credential")
    assert runner(["check", "--config", str(path)], {}).returncode == 0
    config = json.loads(path.read_text())
    _private_json(path, {**config, "x": True})
    assert runner(["check", "--config", str(path)], {}).returncode == 4
    _private_json(
        path,
        {
            "hub_url": config["hub_url"],
            "token_env": config["token_env"],
            "chat": {
                "default_cli": "claude",
                "clis": {
                    "claude": {
                        "argv": [sys.executable],
                        "default_model": "claude-haiku-5-5",
                        "tools": True,
                    }
                },
            },
        },
    )
    result = runner(
        ["run", "--config", str(path), "--once"],
        {"COMMONPLACE_RUNNER_TOKEN": "synthetic-machine-credential"},
    )
    assert result.returncode == 4


def test_no_server_runner_provider(relay_hub):
    from commonplace.chat.types import PROVIDERS

    h = relay_hub
    assert h.settings(h.owner, {"front": {"provider": "runner"}})[0] == 400
    assert PROVIDERS == ("none", "openai_compatible", "anthropic")


def test_relay_sources_safe():
    forbidden_imports = {
        "commonplace.chat.model",
        "http.client",
        "urllib.request",
        "anthropic",
        "openai",
    }
    for name in ("chat_relay", "chat_context", "chat_routes", "chat_agent"):
        path = ROOT / "commonplace" / "runner" / (name + ".py")
        if not path.exists():
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert not _host_specific(node.value), path
            if isinstance(node, ast.Call):
                assert not any(
                    k.arg == "shell" and isinstance(k.value, ast.Constant) and k.value.value is True
                    for k in node.keywords
                ), path
                assert ast.unparse(node.func) not in {"os.system", "os.popen"}, path
                if name == "chat_agent":
                    func = ast.unparse(node.func)
                    assert not func.endswith((".write_text", ".write_bytes")), path
                    if func in {"print", "log_failure"} or func.startswith("logger."):
                        arguments = list(node.args) + [k.value for k in node.keywords]
                        assert not any(
                            isinstance(n, ast.Name)
                            and n.id
                            in {
                                "prompt",
                                "system",
                                "text",
                                "answer",
                                "stdout",
                                "content",
                            }
                            for arg in arguments
                            for n in ast.walk(arg)
                        ), path
            if name != "chat_agent":
                imports = []
                if isinstance(node, ast.Import):
                    imports = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    imports = [node.module or ""] + [
                        (node.module or "") + "." + a.name for a in node.names
                    ]
                assert not any(
                    module == blocked or module.startswith(blocked + ".")
                    for module in imports
                    for blocked in forbidden_imports
                ), path
    ca = optional("commonplace.runner.chat_agent")
    cr = optional("commonplace.runner.chat_relay")
    if ca is not None and cr is not None:
        assert 3 * ca.POLL_S <= cr.CHAT_ONLINE_S


def test_server_key_mode_stays_off(relay_hub, monkeypatch):
    from commonplace.chat.settings import key_allowed
    from commonplace.chat.types import FrontSettings

    h = relay_hub
    monkeypatch.delenv("COMMONPLACE_CHAT_KEY_WORKSPACES", raising=False)
    settings = FrontSettings(
        workspace_id=h.workspace,
        enabled=True,
        provider="anthropic",
        model="claude-haiku-5-5",
        base_url=None,
        key_ref="env:COMMONPLACE_CHAT_KEY_TEST",
        daily_limit=200,
        user_daily_limit=50,
        updated_by=None,
        updated_at=None,
    )
    assert key_allowed(settings) is False
    assert (
        h.settings(h.owner, {"front": {"provider": "anthropic", "key_ref": settings.key_ref}})[0]
        == 403
    )
