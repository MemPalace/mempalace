"""
Instruction text output for MemPalace CLI commands.

Each instruction lives as a .md file in the instructions/ directory
inside the package. The CLI reads and prints the file content.
"""

import re
import sys
from pathlib import Path

INSTRUCTIONS_DIR = Path(__file__).parent / "instructions"

AVAILABLE = ["init", "search", "mine", "help", "status"]


def run_instructions(name: str):
    """Read and print the instruction .md file for the given name."""
    if name not in AVAILABLE:
        print(f"Unknown instructions: {name}", file=sys.stderr)
        print(f"Available: {', '.join(sorted(AVAILABLE))}", file=sys.stderr)
        sys.exit(1)

    md_path = INSTRUCTIONS_DIR / f"{name}.md"
    if not md_path.is_file():
        print(f"Instructions file not found: {md_path}", file=sys.stderr)
        sys.exit(1)

    print(md_path.read_text(encoding="utf-8"))


SHARED_BRAIN_RULES_FILE = INSTRUCTIONS_DIR / "shared_brain_rules.md"

_RULES_MARKER_START = (
    "<!-- mempalace-shared-brain:start (canonical source: mempalace repo "
    "integrations/shared/coordination-protocol.md — edit there, re-render "
    "with `mempalace rules --agent {agent_id}`) -->"
)
_RULES_MARKER_END = "<!-- mempalace-shared-brain:end -->"


def render_shared_brain_rules(agent_id: str) -> str:
    """Render the canonical shared-brain rules block for one agent identity.

    The template ships inside the package and is test-pinned to the
    System-Prompt Snippet in integrations/shared/coordination-protocol.md,
    so every harness pastes the same battle-tested block and a protocol
    lesson lands in one file instead of N system prompts. The output is
    wrapped in HTML-comment markers so a later re-render can replace the
    block in place.
    """
    agent_id = agent_id.strip()
    # The id lands inside an HTML comment marker and in every from_agent
    # field, so it must be a plain token — no whitespace, no markup.
    if not re.fullmatch(r"[A-Za-z0-9._-]+", agent_id):
        raise ValueError("--agent must be a single token like mac-claude (machine-harness)")
    body = SHARED_BRAIN_RULES_FILE.read_text(encoding="utf-8").replace("<AGENT_ID>", agent_id)
    return "\n".join(
        [
            _RULES_MARKER_START.format(agent_id=agent_id),
            "",
            body.rstrip("\n"),
            "",
            _RULES_MARKER_END,
        ]
    )


def run_rules(agent_id: str):
    """Print the rendered shared-brain rules block for the CLI."""
    try:
        print(render_shared_brain_rules(agent_id))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
    except OSError as exc:
        print(f"Rules template not readable: {exc}", file=sys.stderr)
        sys.exit(1)
