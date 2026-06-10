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
