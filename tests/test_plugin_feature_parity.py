"""Cross-plugin parity checks for Claude and Codex plugin bundles."""

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CLAUDE_PLUGIN_DIR = REPO_ROOT / ".claude-plugin"
CODEX_PLUGIN_DIR = REPO_ROOT / ".codex-plugin"


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_mcp_servers(manifest: dict, plugin_dir: Path) -> dict:
    """Accept either inline map or file path (as documented in Codex build docs)."""
    source = manifest.get("mcpServers")
    if isinstance(source, str):
        mcp = _load_json(plugin_dir / source.removeprefix("./"))
        if "mcp_servers" in mcp and isinstance(mcp["mcp_servers"], dict):
            return mcp["mcp_servers"]
        return mcp
    if isinstance(source, dict):
        return source
    return {}


def _hook_event_names(hook_cfg: dict) -> set[str]:
    hooks = hook_cfg.get("hooks", {})
    if isinstance(hooks, dict):
        return set(hooks.keys())
    return set()


def test_plugin_manifests_share_core_identity_fields() -> None:
    claude_manifest = _load_json(CLAUDE_PLUGIN_DIR / "plugin.json")
    codex_manifest = _load_json(CODEX_PLUGIN_DIR / "plugin.json")

    for field in ("name", "version", "description", "license", "repository", "keywords"):
        assert claude_manifest.get(field) == codex_manifest.get(field), (
            f"manifest field drift for {field!r}: "
            f"claude={claude_manifest.get(field)!r}, codex={codex_manifest.get(field)!r}"
        )

    assert claude_manifest.get("author", {}).get("name") == codex_manifest.get("author", {}).get(
        "name"
    )


def test_plugins_expose_common_workflow_entrypoints() -> None:
    claude_commands = {p.stem for p in (CLAUDE_PLUGIN_DIR / "commands").glob("*.md")}
    codex_skills = {p.name for p in (CODEX_PLUGIN_DIR / "skills").iterdir() if p.is_dir()}

    expected = {"help", "init", "mine", "search", "status"}
    assert expected.issubset(claude_commands), (
        f"missing claude commands: {sorted(expected - claude_commands)}"
    )
    assert expected.issubset(codex_skills), f"missing codex skills: {sorted(expected - codex_skills)}"

    # Keep the Codex bundle's generic umbrella skill in parity with Claude's
    # single-skill entrypoint style.
    assert "mempalace" in codex_skills


def test_plugins_wire_the_same_mcp_server_command() -> None:
    claude_manifest = _load_json(CLAUDE_PLUGIN_DIR / "plugin.json")
    codex_manifest = _load_json(CODEX_PLUGIN_DIR / "plugin.json")

    claude_mcp = _load_mcp_servers(claude_manifest, CLAUDE_PLUGIN_DIR)
    codex_mcp = _load_mcp_servers(codex_manifest, CODEX_PLUGIN_DIR)

    assert claude_mcp.get("mempalace", {}).get("command") == "mempalace-mcp"
    assert codex_mcp.get("mempalace", {}).get("command") == "mempalace-mcp"


def test_plugins_include_stop_and_precompact_hooks() -> None:
    claude_hooks = _load_json(CLAUDE_PLUGIN_DIR / "hooks" / "hooks.json")
    codex_hooks = _load_json(CODEX_PLUGIN_DIR / "hooks.json")

    required = {"Stop", "PreCompact"}
    assert required.issubset(_hook_event_names(claude_hooks))
    assert required.issubset(_hook_event_names(codex_hooks))
