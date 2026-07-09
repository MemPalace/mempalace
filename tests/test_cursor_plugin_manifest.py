"""Contract tests for ``.cursor-plugin/``.

These tests protect the four things a Cursor user actually relies on
once they install the plugin:

1. The manifest (``.cursor-plugin/plugin.json``) is valid JSON, satisfies
   Cursor's required + structural fields, and every component path it
   declares resolves to a real on-disk target.
2. The marketplace manifest (``.cursor-plugin/marketplace.json``) is
   valid JSON and points at the same plugin.
3. The MCP config (``.cursor-plugin/mcp.json``) is valid JSON, wraps
   server entries under the documented ``mcpServers`` key, and
   registers the ``mempalace-mcp`` binary that ships with the package.
4. Every skill ``SKILL.md`` and command ``*.md`` parses as YAML
   frontmatter + markdown body.  Cursor derives the slash-command
   slug from the **filename stem** (e.g. ``mempalace-help.md`` →
   ``/mempalace-help``), so command files do NOT need a ``name``
   frontmatter field — only ``description`` is required.

Run with::

    uv run pytest tests/test_cursor_plugin_manifest.py -v

All tests are pure file inspection (no subprocesses, no network) and
take milliseconds. They run on every CI platform without needing
Cursor itself.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO_ROOT / ".cursor-plugin"
MANIFEST_PATH = PLUGIN_DIR / "plugin.json"
MARKETPLACE_PATH = PLUGIN_DIR / "marketplace.json"
MCP_PATH = PLUGIN_DIR / "mcp.json"
README_PATH = PLUGIN_DIR / "README.md"

# Component directories: canonical location is at the plugin root (repo root),
# NOT inside .cursor-plugin/. Cursor's default discovery requires real
# directories at the plugin root; .cursor-plugin/ symlinks back to these.
SKILLS_DIR = REPO_ROOT / "skills"
COMMANDS_DIR = REPO_ROOT / "commands"
RULES_DIR = REPO_ROOT / "rules"

# The slugs we promise to ship. The README's "Available Slash Commands"
# table is the user-facing contract; if you add/remove a command,
# update both the README and this list.
EXPECTED_COMMAND_NAMES = {
    "mempalace-help",
    "mempalace-init",
    "mempalace-mine",
    "mempalace-search",
    "mempalace-status",
}

# Per cursor.com/docs/reference/plugins: "Plugin identifier. Lowercase,
# kebab-case (alphanumerics, hyphens, and periods). Must start and end
# with an alphanumeric character."
KEBAB_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")

# Cursor's submission checklist explicitly forbids these in manifest
# paths: "All paths in manifest are relative and valid (no `..`, no
# absolute paths)." Treat both as hard failures rather than warnings —
# the marketplace review bot would reject the plugin otherwise.
_FORBIDDEN_PATH_FRAGMENTS = ("..",)


# ── helpers ─────────────────────────────────────────────────────────


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split a markdown file with YAML frontmatter into ``(meta, body)``.

    The frontmatter must start at byte 0 with a literal ``---\\n`` and
    close with another ``---\\n`` line. Files without frontmatter are
    treated as having an empty ``meta`` dict so the caller can decide
    whether that's acceptable for the file type under test.
    """
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    raw = text[4:end]
    body = text[end + 5 :]
    parsed = yaml.safe_load(raw) or {}
    if not isinstance(parsed, dict):
        raise AssertionError(f"Frontmatter parsed to {type(parsed).__name__}, expected dict")
    return parsed, body


def _is_safe_relative(path_str: str) -> bool:
    """Return True iff ``path_str`` is a relative, ``..``-free path."""
    if not isinstance(path_str, str) or not path_str:
        return False
    p = Path(path_str)
    if p.is_absolute():
        return False
    return not any(part in _FORBIDDEN_PATH_FRAGMENTS for part in p.parts)


# ── plugin.json ─────────────────────────────────────────────────────


class TestPluginManifest:
    def test_manifest_exists(self):
        assert MANIFEST_PATH.is_file(), f"{MANIFEST_PATH} is missing"

    def test_manifest_is_valid_json(self):
        json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    def test_manifest_has_required_name_field(self):
        data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        assert isinstance(data.get("name"), str) and data["name"], (
            "plugin.json must have a non-empty 'name' (only required field per Cursor schema)"
        )

    def test_manifest_name_is_kebab_case(self):
        data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        assert KEBAB_RE.match(data["name"]), (
            f"name must be lowercase kebab-case; got {data['name']!r}"
        )

    def test_manifest_has_recommended_optional_fields(self):
        """``description`` and ``author.name`` aren't required by the
        schema but ARE required by the submission checklist, so failing
        early here saves a round-trip with the marketplace reviewers."""
        data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        assert isinstance(data.get("description"), str) and data["description"]
        author = data.get("author")
        assert isinstance(author, dict) and isinstance(author.get("name"), str)
        assert author["name"], "author.name must be non-empty"

    def test_manifest_omits_hardcoded_version(self):
        """plugin.json must NOT hardcode a ``version`` field.

        ``mempalace/version.py`` is the single source of truth (per
        CLAUDE.md). A hardcoded version here silently drifts on the next
        release (igorls review, PR #1632). The sibling Antigravity plugin
        omits the field entirely; we match that. The marketplace resolves
        the package version at publish time.
        """
        data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        assert "version" not in data, (
            "plugin.json must omit the hardcoded 'version' field to avoid "
            f"drift from mempalace/version.py; found {data.get('version')!r}"
        )

    def test_marketplace_entry_omits_hardcoded_version(self):
        """Same drift guard for the marketplace plugin entry."""
        data = json.loads(MARKETPLACE_PATH.read_text(encoding="utf-8"))
        for plugin in data.get("plugins", []):
            if isinstance(plugin, dict):
                assert "version" not in plugin, (
                    "marketplace.json plugin entry must omit the hardcoded "
                    f"'version' field; found {plugin.get('version')!r}"
                )

    @pytest.mark.parametrize("field", ["skills", "commands", "mcpServers"])
    def test_manifest_component_paths_are_safe(self, field: str):
        """Every path the manifest declares must be relative + ``..``-free.

        Cursor's submission checklist rejects ``..`` or absolute paths
        outright. We check here so a typo doesn't fail review.
        """
        data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        value = data.get(field)
        if value is None:
            return  # optional — if missing, default discovery kicks in
        if isinstance(value, str):
            paths = [value]
        elif isinstance(value, list):
            paths = [v for v in value if isinstance(v, str)]
        else:
            return  # inline object form; nothing to validate path-wise
        for p in paths:
            assert _is_safe_relative(p), f"{field}: {p!r} must be relative and contain no '..'"

    @pytest.mark.parametrize(
        "field,expected_type",
        [
            ("skills", "dir"),
            ("commands", "dir"),
            ("mcpServers", "file"),
        ],
    )
    def test_manifest_component_paths_resolve(self, field: str, expected_type: str):
        """Every component path must point at a real on-disk target.

        Use REPO_ROOT (not PLUGIN_DIR) as the resolution base because
        Cursor resolves manifest paths against the plugin root, which
        for our layout is the repo root (the dir containing
        ``.cursor-plugin/``).
        """
        data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        value = data.get(field)
        if not isinstance(value, str):
            return  # inline object form or absent
        target = (REPO_ROOT / value).resolve()
        if expected_type == "dir":
            assert target.is_dir(), f"{field}={value!r} -> {target} is not a directory"
        else:
            assert target.is_file(), f"{field}={value!r} -> {target} is not a file"


# ── marketplace.json ────────────────────────────────────────────────


class TestMarketplaceManifest:
    def test_marketplace_exists(self):
        assert MARKETPLACE_PATH.is_file(), f"{MARKETPLACE_PATH} is missing"

    def test_marketplace_is_valid_json(self):
        json.loads(MARKETPLACE_PATH.read_text(encoding="utf-8"))

    def test_marketplace_required_fields(self):
        data = json.loads(MARKETPLACE_PATH.read_text(encoding="utf-8"))
        assert isinstance(data.get("name"), str) and data["name"]
        owner = data.get("owner")
        assert isinstance(owner, dict) and isinstance(owner.get("name"), str)
        plugins = data.get("plugins")
        assert isinstance(plugins, list) and 1 <= len(plugins) <= 500

    def test_marketplace_lists_mempalace_plugin(self):
        """The marketplace must list our plugin, and the listed name must
        match the actual ``plugin.json::name`` — otherwise the marketplace
        resolver looks up ``my-plugin/.cursor-plugin/plugin.json`` and
        gets a name mismatch, which Cursor rejects at install time.
        """
        data = json.loads(MARKETPLACE_PATH.read_text(encoding="utf-8"))
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        names = {p.get("name") for p in data["plugins"] if isinstance(p, dict)}
        assert manifest["name"] in names, (
            f"marketplace.json plugins list does not include {manifest['name']!r}"
        )


# ── mcp.json ────────────────────────────────────────────────────────


class TestMcpConfig:
    def test_mcp_config_exists(self):
        assert MCP_PATH.is_file(), f"{MCP_PATH} is missing"

    def test_mcp_config_is_valid_json(self):
        json.loads(MCP_PATH.read_text(encoding="utf-8"))

    def test_mcp_config_wraps_servers_under_mcpservers_key(self):
        """Per cursor.com/docs/reference/plugins#mcp-servers, the MCP
        config file must contain server entries under a ``mcpServers``
        key. Using the flat shape (used by Claude's ``.mcp.json``) here
        would silently fail to register the server with Cursor.
        """
        data = json.loads(MCP_PATH.read_text(encoding="utf-8"))
        assert "mcpServers" in data and isinstance(data["mcpServers"], dict), (
            "mcp.json must wrap servers under an 'mcpServers' object"
        )

    def test_mcp_config_registers_mempalace_server(self):
        data = json.loads(MCP_PATH.read_text(encoding="utf-8"))
        servers = data["mcpServers"]
        assert "mempalace" in servers, "mcp.json must register a server named 'mempalace'"
        entry = servers["mempalace"]
        assert isinstance(entry, dict) and isinstance(entry.get("command"), str)
        assert entry["command"] == "mempalace-mcp", (
            f"mempalace server command must be 'mempalace-mcp' (the binary "
            f"shipped by the package); got {entry.get('command')!r}"
        )


# ── skills/ ─────────────────────────────────────────────────────────


class TestSkills:
    def test_skills_dir_exists(self):
        assert SKILLS_DIR.is_dir()

    def test_at_least_one_skill_present(self):
        skill_files = list(SKILLS_DIR.glob("*/SKILL.md"))
        assert skill_files, (
            f"{SKILLS_DIR} must contain at least one <name>/SKILL.md "
            "(otherwise Cursor's discovery treats the plugin as having no skills)"
        )

    def test_mempalace_skill_exists(self):
        assert (SKILLS_DIR / "mempalace" / "SKILL.md").is_file()

    def test_mempalace_recall_skill_exists(self):
        """The recall skill is the search-before-answer half of the
        plugin (the ``mempalace`` skill covers setup/mine/status). If it
        goes missing, recall silently regresses to model-memory guessing.
        """
        assert (SKILLS_DIR / "mempalace-recall" / "SKILL.md").is_file()

    def test_each_skill_has_valid_frontmatter(self):
        """Every SKILL.md must declare ``name`` (kebab-case) and a
        non-empty ``description``. Skills missing these fields silently
        fail to register in Cursor's skill picker (#1410 equivalent).
        """
        for skill_path in SKILLS_DIR.glob("*/SKILL.md"):
            text = skill_path.read_text(encoding="utf-8")
            meta, body = _parse_frontmatter(text)
            ctx = f"{skill_path.relative_to(REPO_ROOT)}"
            assert meta, f"{ctx}: missing YAML frontmatter"
            assert isinstance(meta.get("name"), str) and meta["name"], (
                f"{ctx}: 'name' must be a non-empty string"
            )
            assert KEBAB_RE.match(meta["name"]), (
                f"{ctx}: name must be lowercase kebab-case; got {meta['name']!r}"
            )
            assert isinstance(meta.get("description"), str) and meta["description"], (
                f"{ctx}: 'description' must be a non-empty string"
            )
            assert body.strip(), f"{ctx}: body must not be empty"

    def test_skill_name_matches_directory(self):
        """The skill's directory name should equal the frontmatter
        ``name`` — Cursor displays the directory name in the picker and
        the frontmatter name in the API; mismatches confuse both users
        and the agent.
        """
        for skill_path in SKILLS_DIR.glob("*/SKILL.md"):
            meta, _ = _parse_frontmatter(skill_path.read_text(encoding="utf-8"))
            dir_name = skill_path.parent.name
            assert meta.get("name") == dir_name, (
                f"{skill_path.relative_to(REPO_ROOT)}: name={meta.get('name')!r} "
                f"must match directory {dir_name!r}"
            )


# ── rules/ ──────────────────────────────────────────────────────────


class TestRules:
    """The plugin ships an optional recall rule at the plugin root under
    ``rules/``. Like skills and commands, rules are discovered from a
    real directory at the plugin root (the repo root), not from inside
    ``.cursor-plugin/``.
    """

    def test_rules_dir_exists(self):
        assert RULES_DIR.is_dir(), "rules/ missing at repo root"

    def test_rules_dir_is_real_not_symlink(self):
        assert not RULES_DIR.is_symlink(), (
            "rules/ must be a real directory, not a symlink — "
            "Cursor does not follow symlinks for local-plugin discovery"
        )

    def test_recall_rule_exists(self):
        assert (RULES_DIR / "mempalace-recall.mdc").is_file()

    def test_each_rule_has_valid_frontmatter(self):
        """Every ``.mdc`` rule must declare a non-empty ``description``
        (Cursor's matcher reads it to decide relevance) and a boolean
        ``alwaysApply``. A rule missing ``description`` never auto-applies.
        """
        rule_files = list(RULES_DIR.glob("*.mdc"))
        assert rule_files, f"{RULES_DIR} must contain at least one .mdc rule"
        for rule_path in rule_files:
            text = rule_path.read_text(encoding="utf-8")
            meta, body = _parse_frontmatter(text)
            ctx = f"{rule_path.relative_to(REPO_ROOT)}"
            assert meta, f"{ctx}: missing YAML frontmatter"
            assert isinstance(meta.get("description"), str) and meta["description"], (
                f"{ctx}: 'description' must be a non-empty string"
            )
            assert isinstance(meta.get("alwaysApply"), bool), (
                f"{ctx}: 'alwaysApply' must be a boolean"
            )
            assert body.strip(), f"{ctx}: body must not be empty"

    def test_shipped_recall_rule_is_not_always_apply(self):
        """The plugin-shipped recall rule must be ``alwaysApply: false``.

        An always-on rule loads on every turn in every workspace the
        plugin touches, adding MCP latency to unrelated work and fighting
        MemPalace's "memory should feel instant" budget. The aggressive
        ``alwaysApply: true`` variant is an opt-in shipped only under
        examples/, never wired into the default plugin bundle.
        """
        meta, _ = _parse_frontmatter(
            (RULES_DIR / "mempalace-recall.mdc").read_text(encoding="utf-8")
        )
        assert meta.get("alwaysApply") is False, (
            "the plugin-shipped recall rule must be alwaysApply: false; "
            "the always-on variant belongs in examples/cursor/rules/"
        )


# ── commands/ ───────────────────────────────────────────────────────


class TestDefaultDiscoveryLayout:
    """Cursor discovers plugin components from real ``commands/``,
    ``skills/``, and ``mcp.json`` at the *plugin root* (our repo root).

    These must be real directories/files — Cursor does not follow
    symlinks for local-plugin component discovery. We verified this
    behaviour by comparing the cached Cloudflare plugin structure
    (all real dirs) against our earlier broken symlink-only attempt.
    """

    def test_commands_is_real_dir_at_plugin_root(self):
        target = REPO_ROOT / "commands"
        assert target.is_dir(), "commands/ missing at repo root"
        assert not target.is_symlink(), (
            "commands/ must be a real directory, not a symlink — "
            "Cursor does not follow symlinks for local-plugin discovery"
        )

    def test_skills_is_real_dir_at_plugin_root(self):
        target = REPO_ROOT / "skills"
        assert target.is_dir(), "skills/ missing at repo root"
        assert not target.is_symlink(), (
            "skills/ must be a real directory, not a symlink — "
            "Cursor does not follow symlinks for local-plugin discovery"
        )

    def test_mcp_json_is_real_file_at_plugin_root(self):
        target = REPO_ROOT / "mcp.json"
        assert target.is_file(), "mcp.json missing at repo root"
        assert not target.is_symlink(), (
            "mcp.json must be a real file, not a symlink — "
            "Cursor does not follow symlinks for local-plugin discovery"
        )

    def test_no_symlinks_under_cursor_plugin_dir(self):
        """No path under ``.cursor-plugin/`` may be a symlink.

        igorls review (PR #1632): committed symlinks materialise as plain
        text files containing the link target on Windows clones with
        ``core.symlinks=false``, silently breaking the plugin. CI's
        manifest tests skip Windows, so this guard runs on every platform.
        The canonical components live at the repo root (``source: "."``);
        the old ``.cursor-plugin/{commands,skills}`` convenience symlinks
        were redundant and have been removed.
        """
        offenders = [p for p in PLUGIN_DIR.rglob("*") if p.is_symlink()]
        assert not offenders, f"Symlinks under .cursor-plugin/ break Windows clones: {offenders}"


class TestCommands:
    def test_commands_dir_exists(self):
        assert COMMANDS_DIR.is_dir()

    def test_command_set_matches_promised_set(self):
        """The README documents exactly five slash commands. The files
        on disk must match that set — no more, no fewer — otherwise the
        README is lying to users.

        Cursor derives the slash-command slug from the filename stem, so
        we compare stems, not frontmatter ``name`` values.
        """
        actual = {cmd_path.stem for cmd_path in COMMANDS_DIR.glob("*.md")}
        assert actual == EXPECTED_COMMAND_NAMES, (
            f"Command file stem set drifted from promised set. "
            f"On disk: {sorted(actual)}. "
            f"Expected: {sorted(EXPECTED_COMMAND_NAMES)}."
        )

    def test_each_command_has_valid_frontmatter(self):
        """Every command file must have YAML frontmatter with a non-empty
        ``description`` and a non-empty body.

        Cursor derives the slash-command slug from the filename stem, so
        a ``name`` field is intentionally absent — the ``description``
        field is what Cursor shows in the command picker.
        """
        for cmd_path in COMMANDS_DIR.glob("*.md"):
            text = cmd_path.read_text(encoding="utf-8")
            meta, body = _parse_frontmatter(text)
            ctx = f"{cmd_path.relative_to(REPO_ROOT)}"
            assert meta, f"{ctx}: missing YAML frontmatter"
            assert isinstance(meta.get("description"), str) and meta["description"], (
                f"{ctx}: 'description' must be a non-empty string"
            )
            assert body.strip(), f"{ctx}: body must not be empty"

    def test_each_command_name_prefixed_with_mempalace(self):
        """Cursor commands are global (not plugin-namespaced), so every
        command file must be named ``mempalace-*.md`` to avoid colliding
        with built-in or other-plugin commands.

        The slash-command slug is the filename stem, so
        ``mempalace-help.md`` → ``/mempalace-help``.
        """
        for cmd_path in COMMANDS_DIR.glob("*.md"):
            stem = cmd_path.stem
            assert stem.startswith("mempalace-"), (
                f"{cmd_path.relative_to(REPO_ROOT)}: filename stem {stem!r} "
                "must be prefixed with 'mempalace-' to avoid global-namespace collisions"
            )


# ── README.md ───────────────────────────────────────────────────────


class TestReadme:
    def test_readme_exists(self):
        assert README_PATH.is_file(), f"{README_PATH} is missing"

    def test_readme_documents_every_command(self):
        """If the README has a command table, every command we ship
        must be listed in it. This catches the drift case where someone
        adds a command file but forgets to update the docs."""
        text = README_PATH.read_text(encoding="utf-8")
        missing = [name for name in EXPECTED_COMMAND_NAMES if f"/{name}" not in text]
        assert not missing, f"README does not document: {missing}"

    def test_readme_cross_references_hooks_install_path(self):
        """Hooks are deliberately NOT part of the plugin (they're wired
        via hooks/cursor/install.sh). The README must tell users where
        to go for that, otherwise users will assume the plugin already
        installed the hooks and wonder why nothing saves.
        """
        text = README_PATH.read_text(encoding="utf-8")
        assert "hooks/cursor/install.sh" in text, (
            "README must reference hooks/cursor/install.sh so users know how to enable auto-save"
        )
