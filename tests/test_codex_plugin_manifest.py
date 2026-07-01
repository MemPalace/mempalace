"""Contract tests for the Codex plugin marketplace manifest."""

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MARKETPLACE = REPO_ROOT / ".agents" / "plugins" / "marketplace.json"
PLUGIN_MANIFEST = REPO_ROOT / ".codex-plugin" / "plugin.json"
MCP_MANIFEST = REPO_ROOT / ".codex-plugin" / ".mcp.json"
PLUGIN_README = REPO_ROOT / ".codex-plugin" / "README.md"
SKILLS_DIR = REPO_ROOT / ".codex-plugin" / "skills"
RECALL_SKILL = SKILLS_DIR / "mempalace-recall" / "SKILL.md"
SHARED_PROTOCOL = REPO_ROOT / "integrations" / "shared" / "recall-protocol.md"
SHARED_PROTOCOL_REF = (
    "https://github.com/MemPalace/mempalace/blob/main/integrations/shared/recall-protocol.md"
)


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    raw = text[4:end]
    body = text[end + 5 :]
    meta: dict[str, str] = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip().strip('"')
    return meta, body


def test_codex_marketplace_authentication_enum_is_current() -> None:
    """Codex rejects legacy `authentication: NONE` marketplace entries."""
    data = json.loads(MARKETPLACE.read_text(encoding="utf-8"))
    plugin = data["plugins"][0]
    assert plugin["name"] == "mempalace"
    assert plugin["policy"]["installation"] == "AVAILABLE"
    assert plugin["policy"]["authentication"] in {"ON_INSTALL", "ON_USE"}


def test_codex_plugin_declares_mcp_companion_file() -> None:
    """Codex local MCP plugins use a root `.mcp.json` companion file."""
    plugin = json.loads(PLUGIN_MANIFEST.read_text(encoding="utf-8"))
    mcp = json.loads(MCP_MANIFEST.read_text(encoding="utf-8"))

    assert plugin["mcpServers"] == "./.mcp.json"
    assert mcp["mcpServers"]["mempalace"]["command"] == "mempalace-mcp"


def test_codex_plugin_uses_default_hook_discovery() -> None:
    """Codex validation rejects a top-level `hooks` field."""
    plugin = json.loads(PLUGIN_MANIFEST.read_text(encoding="utf-8"))

    assert "hooks" not in plugin
    assert (REPO_ROOT / ".codex-plugin" / "hooks.json").is_file()


def test_codex_skills_are_discoverable() -> None:
    """Every shipped Codex skill needs frontmatter for progressive disclosure."""
    assert SKILLS_DIR.is_dir()
    skill_paths = sorted(SKILLS_DIR.glob("*/SKILL.md"))
    assert skill_paths, "Codex plugin must ship at least one skill"
    for skill_path in skill_paths:
        meta, body = _parse_frontmatter(skill_path.read_text(encoding="utf-8"))
        ctx = skill_path.relative_to(REPO_ROOT)
        assert meta.get("name") == skill_path.parent.name, (
            f"{ctx}: frontmatter name must match the skill directory"
        )
        assert meta.get("description"), f"{ctx}: missing description"
        assert body.strip(), f"{ctx}: empty skill body"


def test_codex_recall_skill_exists() -> None:
    """Codex needs a recall skill, not just setup/search/status skills."""
    assert RECALL_SKILL.is_file(), f"missing: {RECALL_SKILL}"
    assert not RECALL_SKILL.is_symlink(), f"{RECALL_SKILL} must be a real file"


def test_codex_recall_skill_triggers_on_natural_memory_questions() -> None:
    """Natural prompts should select recall without the user naming MemPalace."""
    meta, body = _parse_frontmatter(RECALL_SKILL.read_text(encoding="utf-8"))
    searchable = f"{meta.get('description', '')}\n{body}".lower()
    for phrase in (
        "do you remember",
        "what did we decide",
        "where did we leave off",
        "what happened before",
        "last time",
        "who is",
        "what is",
    ):
        assert phrase in searchable, f"recall skill should mention trigger phrase {phrase!r}"


def test_codex_recall_skill_links_protocol_and_prefers_mcp() -> None:
    body = RECALL_SKILL.read_text(encoding="utf-8")
    assert "integrations/shared/recall-protocol.md" in body
    assert SHARED_PROTOCOL_REF in body
    assert "mempalace_search" in body
    assert "mempalace search" in body, "CLI fallback should be documented for Codex hosts"


def test_codex_recall_skill_requires_source_plus_verbatim_answer_shape() -> None:
    body = RECALL_SKILL.read_text(encoding="utf-8").lower()
    for needle in ("answer shape", "cite the source", "exact drawer text", "inference"):
        assert needle in body
    assert "broad filesystem" in body
    assert "do not switch to broad filesystem search" in body


def test_codex_readme_lists_recall_skill() -> None:
    body = PLUGIN_README.read_text(encoding="utf-8").lower()
    assert "mempalace-recall" in body
    assert "do you remember" in body
    assert "where did we leave off" in body


def test_shared_recall_protocol_defines_answer_shape() -> None:
    body = SHARED_PROTOCOL.read_text(encoding="utf-8").lower()
    assert re.search(r"^## answer shape$", body, re.MULTILINE)
    assert "quote the drawer's exact stored words" in body
    assert "source location" in body
