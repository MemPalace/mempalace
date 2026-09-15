"""Tests for `mempalace rules` — the canonical shared-brain rules renderer."""

import re
import subprocess
import sys
from pathlib import Path

import pytest

from mempalace.instructions_cli import (
    MCP_LIGHT_SUBSTITUTIONS,
    SHARED_BRAIN_RULES_FILE,
    apply_mcp_shape,
    render_shared_brain_rules,
)

REPO_ROOT = Path(__file__).parent.parent
PROTOCOL_DOC = REPO_ROOT / "integrations" / "shared" / "coordination-protocol.md"


def _snippet_from_protocol_doc() -> str:
    """The ```text fence under '## System-Prompt Snippet' in the canonical doc."""
    text = PROTOCOL_DOC.read_text(encoding="utf-8")
    match = re.search(
        r"^## System-Prompt Snippet$.*?^```text$\n(.*?)^```$",
        text,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert match, "coordination-protocol.md lost its System-Prompt Snippet fence"
    return match.group(1)


def _strip_tool_tokens(text: str) -> str:
    """Replace classic and light tool tokens so the remaining prose can be compared."""
    tokens = [full for full, _light in MCP_LIGHT_SUBSTITUTIONS]
    tokens.extend(light for _full, light in MCP_LIGHT_SUBSTITUTIONS)
    for token in sorted(set(tokens), key=len, reverse=True):
        text = text.replace(token, "<TOOL>")
    return text


class TestSharedBrainRulesTemplate:
    def test_packaged_template_matches_canonical_doc(self):
        """The doc says it is the single source of truth; the packaged copy the
        CLI renders must be byte-identical, or the two drift per-agent — the
        exact failure the template exists to prevent."""
        assert SHARED_BRAIN_RULES_FILE.read_text(encoding="utf-8") == _snippet_from_protocol_doc()

    def test_website_guide_copy_matches_canonical_doc(self):
        """website/guide/shared-brain.md embeds the snippet for readers; it
        drifts silently without a pin (there is no runtime path through it)."""
        guide = REPO_ROOT / "website" / "guide" / "shared-brain.md"
        match = re.search(
            r"^## 5\. Wire the protocol into each agent$.*?^```text$\n(.*?)^```$",
            guide.read_text(encoding="utf-8"),
            flags=re.MULTILINE | re.DOTALL,
        )
        assert match, "shared-brain.md lost its snippet fence"
        assert match.group(1) == SHARED_BRAIN_RULES_FILE.read_text(encoding="utf-8")

    def test_template_keeps_the_identity_placeholders(self):
        content = SHARED_BRAIN_RULES_FILE.read_text(encoding="utf-8")
        assert "<HOST>" in content
        assert "<HARNESS>" in content
        assert "<PROJECT>" in content
        assert "<AGENT_ID>" not in content

    def test_watcher_triggers_are_imperative_and_enumerated(self):
        """A capability-conditional watcher rule ('if your harness can...')
        reads as optional and agents skip it. Declared-idle chat plus an
        enumerated listen/claim/delegate list is the load-bearing sentence."""
        content = SHARED_BRAIN_RULES_FILE.read_text(encoding="utf-8")
        collapsed = " ".join(content.lower().split())
        assert "do not arm a background watcher at session start" in collapsed
        assert "the user asked you to listen" in collapsed
        assert "status=claimed" in collapsed
        assert "you delegate" in collapsed
        assert "if your harness can run a background process, start" not in collapsed
        assert "always include a topic" not in collapsed


class TestRenderSharedBrainRules:
    def test_substitutes_tuple_everywhere(self):
        rendered = render_shared_brain_rules("windows", "grok", "mempalace")
        assert "<HOST>" not in rendered
        assert "<HARNESS>" not in rendered
        assert "<PROJECT>" not in rendered
        assert "windows:grok:<project>" in rendered
        assert "windows:grok:mempalace" in rendered
        # The default names the 45-tool server the plugins and skills register.
        assert "mempalace_event_list" in rendered
        assert "palace_coordinate EVENT LIST" not in rendered

        light = render_shared_brain_rules("windows", "grok", "mempalace", mcp="light")
        assert "palace_coordinate EVENT LIST" in light

    def test_light_mcp_swaps_tool_tokens_only(self):
        full = render_shared_brain_rules("mac", "claude", "myapp", mcp="full")
        light = render_shared_brain_rules("mac", "claude", "myapp", mcp="light")
        assert "palace_coordinate EVENT LIST" in light
        assert "mempalace_event_list" not in light
        assert "mempalace logstream watch" in light
        assert "mempalace logstream watch" in full
        assert _strip_tool_tokens(full) == _strip_tool_tokens(light)

    def test_apply_mcp_shape_rejects_unknown(self):
        with pytest.raises(ValueError, match="full"):
            apply_mcp_shape("x", "pql")

    def test_wrapped_in_sync_markers(self):
        rendered = render_shared_brain_rules("aero", "opencode", "myapp")
        lines = rendered.splitlines()
        assert lines[0].startswith("<!-- mempalace-shared-brain:start")
        assert lines[-1] == "<!-- mempalace-shared-brain:end -->"
        assert "canonical source" in lines[0]
        assert "--host aero --harness opencode --project myapp" in lines[0]

    @pytest.mark.parametrize(
        "bad",
        ["", "   ", "two words", "tab\tid", "x-->", "a{b}", "Windows", "grok:tui", "MemPalace"],
    )
    def test_rejects_non_token_components(self, bad):
        """Each component is a lowercase token; colons join them, they are not inside them."""
        with pytest.raises(ValueError):
            render_shared_brain_rules(bad, "grok", "mempalace")
        with pytest.raises(ValueError):
            render_shared_brain_rules("windows", bad, "mempalace")
        with pytest.raises(ValueError):
            render_shared_brain_rules("windows", "grok", bad)


class TestRulesCli:
    def test_cli_renders_rules(self):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "mempalace.cli",
                "rules",
                "--host",
                "windows",
                "--harness",
                "codex",
                "--project",
                "mempalace",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "windows:codex:mempalace" in result.stdout
        assert "<HOST>" not in result.stdout
        assert "mempalace-shared-brain:start" in result.stdout
        assert "mempalace_event_list" in result.stdout
        assert "palace_coordinate EVENT LIST" not in result.stdout

    def test_cli_renders_light_mcp(self):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "mempalace.cli",
                "rules",
                "--host",
                "mac",
                "--harness",
                "claude",
                "--project",
                "myapp",
                "--mcp",
                "light",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "palace_query FIND" in result.stdout
        assert "mempalace_search" not in result.stdout

    def test_cli_rejects_bad_host(self):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "mempalace.cli",
                "rules",
                "--host",
                "two words",
                "--harness",
                "grok",
                "--project",
                "mempalace",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1
        assert "lowercase token" in result.stderr
