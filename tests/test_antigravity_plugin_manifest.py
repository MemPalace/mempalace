"""Schema tests for the .antigravity-plugin/ directory.

Covers:

* `plugin.json` matches the verified-minimal Antigravity schema
  (`{"name": "..."}`, no fabricated fields).
* `mcp_config.json` registers `mempalace-mcp` under the `mcpServers`
  key with the verified shape from
  https://antigravity.google/docs/mcp.
* `hooks.json.tmpl` is valid JSON, references both hook scripts via
  the `__PLUGIN_DIR__` placeholder, and pins per-event timeouts
  inside the safety bounds.
* `skills/mempalace/SKILL.md` exists as a real file (no symlinks) and
  carries the required YAML frontmatter (`description`).

These are contract tests — they fail as soon as anyone changes the
in-repo shape in a way that drifts from Antigravity's documented
schema. See [hooks/antigravity/INVESTIGATION.md](../hooks/antigravity/INVESTIGATION.md)
for the source-of-truth audit driving the assertions.
"""

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = REPO_ROOT / ".antigravity-plugin"

PLUGIN_JSON = PLUGIN_DIR / "plugin.json"
MCP_CONFIG = PLUGIN_DIR / "mcp_config.json"
HOOKS_TMPL = PLUGIN_DIR / "hooks.json.tmpl"
SKILL_MD = PLUGIN_DIR / "skills" / "mempalace" / "SKILL.md"
PLUGIN_README = PLUGIN_DIR / "README.md"

# Recall layer (mirrors the Cursor branch's recall skill + rule + shared protocol)
RECALL_SKILL_MD = PLUGIN_DIR / "skills" / "mempalace-recall" / "SKILL.md"
RECALL_RULE_MD = PLUGIN_DIR / "rules" / "mempalace-recall.md"
SHARED_PROTOCOL = REPO_ROOT / "integrations" / "shared" / "recall-protocol.md"
INSTALL_SH = REPO_ROOT / "hooks" / "antigravity" / "install.sh"
SHARED_PROTOCOL_REF = (
    "https://github.com/MemPalace/mempalace/blob/main/integrations/shared/recall-protocol.md"
)

EXPECTED_HOOKS = {
    "Stop": {
        "script_basename": "mempal_save_hook_antigravity.sh",
        "timeout_floor": 10,
        "timeout_ceiling": 60,
    },
    "PreInvocation": {
        "script_basename": "mempal_wake_hook_antigravity.sh",
        "timeout_floor": 1,
        "timeout_ceiling": 10,
    },
}


def test_plugin_dir_exists() -> None:
    """The in-repo plugin directory exists and is laid out as expected."""
    assert PLUGIN_DIR.is_dir(), f"missing: {PLUGIN_DIR}"
    for required in (PLUGIN_JSON, MCP_CONFIG, HOOKS_TMPL, SKILL_MD, PLUGIN_README):
        assert required.is_file(), f"missing: {required}"


def test_plugin_json_minimal_schema() -> None:
    """plugin.json must be `{"name": "mempalace"}` exactly — no fabricated fields.

    The third-party "antigravity-plugins" community skill at
    ~/.gemini/skills/antigravity-plugins/SKILL.md documents a
    `permissions` field that does not exist in any real
    Google-shipped plugin. We pin to the verified minimal shape and
    fail loudly if anyone re-introduces the fabrication.
    """
    data = json.loads(PLUGIN_JSON.read_text(encoding="utf-8"))
    assert isinstance(data, dict), "plugin.json must be a JSON object"
    assert data == {"name": "mempalace"}, (
        f"plugin.json must equal {{'name': 'mempalace'}} (verified shape); "
        f"got {data!r}. The `permissions` field documented in the third-party "
        "antigravity-plugins community skill is fabricated; do not add it."
    )


def test_mcp_config_registers_mempalace_mcp() -> None:
    """mcp_config.json must register the mempalace stdio server."""
    data = json.loads(MCP_CONFIG.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert "mcpServers" in data, "missing top-level mcpServers key"
    servers = data["mcpServers"]
    assert isinstance(servers, dict)
    assert "mempalace" in servers, "mcpServers.mempalace not registered"
    entry = servers["mempalace"]
    assert isinstance(entry, dict)
    assert entry.get("command") == "mempalace-mcp", (
        f"mcpServers.mempalace.command must be 'mempalace-mcp'; got {entry.get('command')!r}"
    )


def test_hooks_template_valid_json() -> None:
    """hooks.json.tmpl must be valid JSON (the `__PLUGIN_DIR__` placeholder is JSON-safe)."""
    body = HOOKS_TMPL.read_text(encoding="utf-8")
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        pytest.fail(f"hooks.json.tmpl is not valid JSON: {exc}")
    assert isinstance(data, dict)


def test_hooks_template_uses_plugin_dir_placeholder() -> None:
    """hooks.json.tmpl must use __PLUGIN_DIR__ — never bake an absolute path."""
    body = HOOKS_TMPL.read_text(encoding="utf-8")
    assert "__PLUGIN_DIR__" in body, (
        "hooks.json.tmpl must use __PLUGIN_DIR__ as the install-dir placeholder. "
        "Hard-coded absolute paths break the installer's idempotency promise."
    )
    # Any `/Users/`, `/home/`, or `~/` segment in the template body is a sign
    # that an absolute path leaked in.
    forbidden = ["/Users/", "/home/", "~/"]
    for prefix in forbidden:
        assert prefix not in body, (
            f"hooks.json.tmpl must not contain a hard-coded path segment {prefix!r}; "
            "use the __PLUGIN_DIR__ placeholder instead."
        )


@pytest.mark.parametrize("event", sorted(EXPECTED_HOOKS))
def test_hooks_template_event_present(event: str) -> None:
    """Each expected event has exactly one entry pointing at the right script with bounded timeout."""
    data = json.loads(HOOKS_TMPL.read_text(encoding="utf-8"))
    bounds = EXPECTED_HOOKS[event]
    # Outer keys are hook namespace names, e.g. "mempalace-save".
    matching = [
        (ns, payload[event])
        for ns, payload in data.items()
        if isinstance(payload, dict) and event in payload
    ]
    assert len(matching) == 1, (
        f"expected exactly one hook namespace declaring event {event!r}; "
        f"found {len(matching)}: {[m[0] for m in matching]}"
    )
    _, entries = matching[0]
    assert isinstance(entries, list)
    assert len(entries) == 1, (
        f"{event}: expected exactly one handler entry, got {len(entries)}; "
        "duplicate entries would double-fire the hook"
    )
    handler = entries[0]
    assert handler.get("type", "command") == "command", (
        f"{event}: only type=command is supported by Antigravity"
    )
    cmd = handler.get("command", "")
    assert cmd.startswith("__PLUGIN_DIR__/"), (
        f"{event}: command must be rooted at __PLUGIN_DIR__/, got {cmd!r}"
    )
    assert cmd.endswith("/" + bounds["script_basename"]), (
        f"{event}: command must end with the expected script basename "
        f"{bounds['script_basename']!r}; got {cmd!r}"
    )
    timeout = handler.get("timeout")
    is_int = isinstance(timeout, int) and not isinstance(timeout, bool)
    assert is_int and bounds["timeout_floor"] <= timeout <= bounds["timeout_ceiling"], (
        f"{event}: timeout must be an int in "
        f"[{bounds['timeout_floor']}, {bounds['timeout_ceiling']}]s; got {timeout!r}"
    )


def test_skill_is_real_file_not_symlink() -> None:
    """SKILL.md at the discovery path must be a real file.

    Antigravity (like Cursor) loads skills by reading
    `<plugin>/skills/<name>/SKILL.md` directly. A symlink at that path
    would work locally but break under any installer that does a
    plain `cp`. Honouring constraint #6 in the integration brief.
    """
    assert SKILL_MD.is_file(), f"missing: {SKILL_MD}"
    assert not SKILL_MD.is_symlink(), (
        f"{SKILL_MD} must be a real file, not a symlink — installers that "
        "cp without -L would otherwise carry the symlink into the install."
    )


def test_skill_has_required_frontmatter() -> None:
    """SKILL.md must carry YAML frontmatter with a non-empty description.

    Antigravity's skill loader uses the `description` field to decide
    when to surface the skill. An empty / missing description would
    silently disable progressive disclosure.
    """
    body = SKILL_MD.read_text(encoding="utf-8")
    assert body.startswith("---\n"), "SKILL.md must begin with YAML frontmatter"
    end = body.find("\n---\n", 4)
    assert end > 0, "SKILL.md frontmatter is missing the closing fence"
    front = body[4:end]
    desc_match = re.search(r"^description:\s*(.+)$", front, re.MULTILINE)
    assert desc_match is not None, "SKILL.md frontmatter missing `description` key"
    desc_value = desc_match.group(1).strip()
    assert desc_value, "SKILL.md `description` is empty"
    # Sanity: the description should be substantive enough for the
    # skill loader to act on. 30 chars is a soft floor, not a tight bound.
    assert len(desc_value) >= 30, (
        f"SKILL.md description looks too short to be useful: {desc_value!r}"
    )


def test_no_symlinks_inside_plugin_dir() -> None:
    """Nothing inside .antigravity-plugin/ may be a symlink.

    This is the broader version of `test_skill_is_real_file_not_symlink`
    and a guard against silent regressions if someone re-introduces
    the `skills -> ../skills` symlink pattern from the original plan
    without honouring `cp -RL` semantics in the installer.
    """
    leaks = [p for p in PLUGIN_DIR.rglob("*") if p.is_symlink()]
    assert not leaks, (
        f"symlinks found inside .antigravity-plugin/: {[str(p.relative_to(PLUGIN_DIR)) for p in leaks]}; "
        "the entire plugin tree must be made of real files so any installer "
        "(including those that cp without -L) gets a working install."
    )


def test_plugin_readme_present_and_substantive() -> None:
    """README.md inside the plugin dir must exist and be substantive.

    Empty / placeholder READMEs are a frequent symptom of half-finished
    refactors; a 200-byte floor catches those without being so tight
    it discourages legitimate rewrites.
    """
    body = PLUGIN_README.read_text(encoding="utf-8")
    assert len(body) >= 200, (
        f".antigravity-plugin/README.md looks too short ({len(body)} bytes); "
        "expected a substantive description of layout + install."
    )
    # Must mention key concepts so the README can't degrade into prose
    # that drops the operational links.
    for needle in ("plugin.json", "mcp_config.json", "hooks.json"):
        assert needle in body, f"README.md must mention {needle}"


# ── Recall layer: skill, rule, shared protocol ───────────────────────
#
# Mirrors the three-layer recall wiring added for Cursor on
# feat/cursor-hooks-support, adapted for Antigravity's plugin surface.
# The wake hook (PreInvocation) is the eager layer; these files are the
# on-demand layers (skill + optional rule), both anchored to the single
# canonical protocol so they can never drift.


def test_shared_protocol_exists() -> None:
    """The canonical recall protocol is the single source of truth.

    The recall skill and rule both link here rather than restating the
    protocol, so the rule can never drift from the skill.
    """
    assert SHARED_PROTOCOL.is_file(), f"missing: {SHARED_PROTOCOL}"
    body = SHARED_PROTOCOL.read_text(encoding="utf-8")
    assert "MemPalace Recall Protocol" in body, "shared protocol must carry its canonical title"


def test_recall_skill_exists() -> None:
    """The recall-only skill must be a real file at the discovery path."""
    assert RECALL_SKILL_MD.is_file(), f"missing: {RECALL_SKILL_MD}"
    assert not RECALL_SKILL_MD.is_symlink(), (
        f"{RECALL_SKILL_MD} must be a real file, not a symlink — installers "
        "that cp without -L would otherwise carry the symlink into the install."
    )


def test_recall_skill_has_required_frontmatter() -> None:
    """The recall skill must carry YAML frontmatter with a non-empty description.

    Antigravity's skill loader uses `description` for progressive
    disclosure, exactly like the ops `mempalace` skill.
    """
    body = RECALL_SKILL_MD.read_text(encoding="utf-8")
    assert body.startswith("---\n"), "recall SKILL.md must begin with YAML frontmatter"
    end = body.find("\n---\n", 4)
    assert end > 0, "recall SKILL.md frontmatter is missing the closing fence"
    front = body[4:end]
    desc_match = re.search(r"^description:\s*(.+)$", front, re.MULTILINE)
    assert desc_match is not None, "recall SKILL.md frontmatter missing `description` key"
    assert desc_match.group(1).strip(), "recall SKILL.md `description` is empty"


def test_recall_skill_has_required_sections() -> None:
    """The recall skill must carry the load-bearing protocol sections."""
    body = RECALL_SKILL_MD.read_text(encoding="utf-8")
    for section in (
        "When to recall",
        "Protocol",
        "Tool selection",
        "Unhappy paths",
        "Anti-patterns",
    ):
        assert section in body, f"recall SKILL.md must contain a '{section}' section"


def test_recall_skill_links_to_shared_protocol() -> None:
    """The recall skill must defer to the canonical protocol, not restate it."""
    body = RECALL_SKILL_MD.read_text(encoding="utf-8")
    assert SHARED_PROTOCOL_REF in body, (
        f"recall SKILL.md must reference {SHARED_PROTOCOL_REF} so the protocol stays single-sourced"
    )


def test_recall_rule_exists() -> None:
    """The optional recall rule must be a real file under the plugin rules dir."""
    assert RECALL_RULE_MD.is_file(), f"missing: {RECALL_RULE_MD}"
    assert not RECALL_RULE_MD.is_symlink(), f"{RECALL_RULE_MD} must be a real file"


def test_recall_rule_is_plain_markdown_not_mdc() -> None:
    """Antigravity rules are plain `.md` with no YAML frontmatter.

    Per https://antigravity.google/docs plugins use `rules/<name>.md`
    (no `.mdc`, no Cursor-style `alwaysApply` frontmatter). Pin the
    plain-markdown shape so nobody copies the Cursor `.mdc` verbatim.
    """
    assert RECALL_RULE_MD.suffix == ".md", "Antigravity rule must use the .md extension"
    assert not (RECALL_RULE_MD.parent / "mempalace-recall.mdc").exists(), (
        "an .mdc rule leaked in; Antigravity rules are plain .md"
    )
    body = RECALL_RULE_MD.read_text(encoding="utf-8")
    assert not body.startswith("---"), (
        "Antigravity rule must NOT carry YAML frontmatter (no Cursor-style "
        "`alwaysApply`); it is plain markdown"
    )


def test_recall_rule_references_shared_protocol() -> None:
    """The rule must point at the canonical protocol and the deeper skill."""
    body = RECALL_RULE_MD.read_text(encoding="utf-8")
    assert SHARED_PROTOCOL_REF in body, f"recall rule must reference {SHARED_PROTOCOL_REF}"


def test_installer_creates_rules_dir() -> None:
    """install.sh must create the skills/mempalace-recall and rules dirs."""
    body = INSTALL_SH.read_text(encoding="utf-8")
    assert '"$INSTALL_DIR/rules"' in body, "install.sh mkdir block must create the rules/ directory"
    assert '"$INSTALL_DIR/skills/mempalace-recall"' in body, (
        "install.sh mkdir block must create the skills/mempalace-recall/ directory"
    )


def test_installer_copies_recall_skill() -> None:
    """install.sh must copy the recall skill into the install dir."""
    body = INSTALL_SH.read_text(encoding="utf-8")
    assert "skills/mempalace-recall/SKILL.md" in body, (
        "install.sh must copy_file the mempalace-recall skill"
    )


def test_installer_copies_recall_rule() -> None:
    """install.sh must copy the recall rule into the install dir."""
    body = INSTALL_SH.read_text(encoding="utf-8")
    assert "rules/mempalace-recall.md" in body, (
        "install.sh must copy_file the mempalace-recall rule"
    )
