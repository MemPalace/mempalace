"""Contract tests for the Codex plugin marketplace metadata."""

from __future__ import annotations

import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
MARKETPLACE_PATH = REPO_ROOT / ".agents" / "plugins" / "marketplace.json"
MANIFEST_PATH = REPO_ROOT / ".codex-plugin" / "plugin.json"
MCP_PATH = REPO_ROOT / ".mcp.json"


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


def test_mcp_config_registers_mempalace_server():
    config = _read_json(MCP_PATH)

    assert config == {
        "mcpServers": {
            "mempalace": {
                "command": "mempalace-mcp",
            }
        }
    }
