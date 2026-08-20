"""Contract tests for the Codex plugin marketplace metadata."""

from __future__ import annotations

import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
MARKETPLACE_PATH = REPO_ROOT / ".agents" / "plugins" / "marketplace.json"
MANIFEST_PATH = REPO_ROOT / ".codex-plugin" / "plugin.json"
MCP_PATH = REPO_ROOT / ".mcp.json"
HOOKS_PATH = REPO_ROOT / "hooks" / "hooks.json"
LEGACY_HOOKS_PATH = REPO_ROOT / ".codex-plugin" / "hooks.json"
LEGACY_RUNNER_PATH = REPO_ROOT / ".codex-plugin" / "hooks" / "mempal-hook.sh"


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_marketplace_entry_uses_supported_codex_schema():
    marketplace = _read_json(MARKETPLACE_PATH)
    plugin = marketplace["plugins"][0]

    assert plugin["name"] == "mempalace"
    assert plugin["source"] == {"source": "local", "path": "./"}
    assert plugin["policy"] == {
        "installation": "AVAILABLE",
        "authentication": "ON_INSTALL",
    }


def test_plugin_manifest_references_supported_components():
    manifest = _read_json(MANIFEST_PATH)

    assert manifest["mcpServers"] == "./.mcp.json"
    assert "hooks" not in manifest


def test_default_hook_definition_uses_canonical_plugin_root_layout():
    assert HOOKS_PATH.is_file(), f"missing default Codex hook definition: {HOOKS_PATH}"
    assert not LEGACY_HOOKS_PATH.exists(), f"legacy hook definition remains: {LEGACY_HOOKS_PATH}"
    assert not LEGACY_RUNNER_PATH.exists(), f"legacy hook runner remains: {LEGACY_RUNNER_PATH}"


def test_mcp_config_registers_mempalace_server():
    config = _read_json(MCP_PATH)

    assert config == {
        "mcpServers": {
            "mempalace": {
                "command": "mempalace-mcp",
            }
        }
    }
