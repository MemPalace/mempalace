"""Tests for `mempalace rules` — the canonical shared-brain rules renderer."""

import re
import subprocess
import sys
from pathlib import Path

import pytest

from mempalace.instructions_cli import (
    SHARED_BRAIN_RULES_FILE,
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

    def test_template_keeps_the_placeholder(self):
        content = SHARED_BRAIN_RULES_FILE.read_text(encoding="utf-8")
        assert "<AGENT_ID>" in content

    def test_watcher_instruction_is_imperative_not_conditional(self):
        """A capability-conditional watcher rule ('if your harness can...')
        reads as optional and agents skip it; the template must make watcher
        discipline imperative."""
        content = SHARED_BRAIN_RULES_FILE.read_text(encoding="utf-8")
        assert "you must watch the stream actively" in " ".join(content.lower().split())
        assert "if your harness can run a background process, start" not in content.lower()


class TestRenderSharedBrainRules:
    def test_substitutes_agent_id_everywhere(self):
        rendered = render_shared_brain_rules("mac-claude")
        assert "<AGENT_ID>" not in rendered
        assert "mac-claude" in rendered

    def test_wrapped_in_sync_markers(self):
        rendered = render_shared_brain_rules("aero-opencode")
        lines = rendered.splitlines()
        assert lines[0].startswith("<!-- mempalace-shared-brain:start")
        assert lines[-1] == "<!-- mempalace-shared-brain:end -->"
        assert "canonical source" in lines[0]

    @pytest.mark.parametrize("bad", ["", "   ", "two words", "tab\tid", "x-->", "a{b}"])
    def test_rejects_non_token_identities(self, bad):
        """Whitespace aside, the id lands inside an HTML comment marker, so
        markup-breaking characters must be refused too."""
        with pytest.raises(ValueError):
            render_shared_brain_rules(bad)


class TestRulesCli:
    def test_cli_renders_rules(self):
        result = subprocess.run(
            [sys.executable, "-m", "mempalace.cli", "rules", "--agent", "windows-codex"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "windows-codex" in result.stdout
        assert "<AGENT_ID>" not in result.stdout
        assert "mempalace-shared-brain:start" in result.stdout

    def test_cli_rejects_bad_agent(self):
        result = subprocess.run(
            [sys.executable, "-m", "mempalace.cli", "rules", "--agent", "two words"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1
        assert "single token" in result.stderr
