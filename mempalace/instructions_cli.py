"""
Instruction text output for MemPalace CLI commands.

Each instruction lives as a .md file in the instructions/ directory
inside the package. The CLI reads and prints the file content.
"""

import re
import sys
from pathlib import Path

INSTRUCTIONS_DIR = Path(__file__).parent / "instructions"

AVAILABLE = ["init", "search", "mine", "help", "status", "audit"]

# Longest-first so mempalace_kg_add cannot eat a prefix of a longer name.
MCP_LIGHT_SUBSTITUTIONS = (
    ("mempalace_kg_supersede", "palace_exec KG SUPERSEDE"),
    ("mempalace_kg_invalidate", "palace_exec KG INVALIDATE"),
    ("mempalace_kg_query", "palace_query KG"),
    ("mempalace_kg_add", "palace_exec KG ADD"),
    ("mempalace_add_drawer", "palace_exec ADD"),
    ("mempalace_event_append", "palace_coordinate EVENT APPEND"),
    ("mempalace_event_list", "palace_coordinate EVENT LIST"),
    ("mempalace_event_wait", "palace_coordinate EVENT WAIT"),
    ("mempalace_event_ack", "palace_coordinate EVENT ACK"),
    ("mempalace_patch_submit", "palace_coordinate PATCH SUBMIT"),
    ("mempalace_artifact_get", "palace_coordinate ARTIFACT GET"),
    ("mempalace_search", "palace_query FIND"),
)

_COMPONENT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


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
    "with `mempalace rules --host {host} --harness {harness} --project {project}`) -->"
)
_RULES_MARKER_END = "<!-- mempalace-shared-brain:end -->"


def validate_identity_component(name: str, value: str) -> str:
    """Return a stripped identity component, or raise ValueError."""
    value = (value or "").strip()
    if not _COMPONENT_RE.fullmatch(value):
        raise ValueError(
            f"--{name} must be a stable lowercase token like windows, grok, or "
            "mempalace (letters, digits, '.', '_', '-'; no colons, no uppercase)"
        )
    return value


def apply_mcp_shape(body: str, mcp: str) -> str:
    """Swap classic MCP tool names for the lightweight triad, or leave as-is."""
    if mcp == "full":
        return body
    if mcp != "light":
        raise ValueError("--mcp must be 'full' or 'light'")
    for full_name, light_name in MCP_LIGHT_SUBSTITUTIONS:
        body = body.replace(full_name, light_name)
    return body


def render_shared_brain_rules(host: str, harness: str, project: str, mcp: str = "full") -> str:
    """Render the canonical shared-brain rules block for one agent identity.

    The template ships inside the package and is test-pinned to the
    System-Prompt Snippet in integrations/shared/coordination-protocol.md,
    so every harness pastes the same battle-tested block and a protocol
    lesson lands in one file instead of N system prompts. The output is
    wrapped in HTML-comment markers so a later re-render can replace the
    block in place.

    ``mcp`` selects tool names: ``full`` (default, the 47-tool server) or ``light``
    (palace_query / palace_exec / palace_coordinate). Prose is identical;
    only the tool tokens change.
    """
    host = validate_identity_component("host", host)
    harness = validate_identity_component("harness", harness)
    project = validate_identity_component("project", project)
    body = SHARED_BRAIN_RULES_FILE.read_text(encoding="utf-8")
    body = body.replace("<HOST>", host).replace("<HARNESS>", harness).replace("<PROJECT>", project)
    body = apply_mcp_shape(body, mcp)
    return "\n".join(
        [
            _RULES_MARKER_START.format(host=host, harness=harness, project=project),
            "",
            body.rstrip("\n"),
            "",
            _RULES_MARKER_END,
        ]
    )


def run_rules(host: str, harness: str, project: str, mcp: str = "full"):
    """Print the rendered shared-brain rules block for the CLI."""
    try:
        print(render_shared_brain_rules(host, harness, project, mcp=mcp))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
    except OSError as exc:
        print(f"Rules template not readable: {exc}", file=sys.stderr)
        sys.exit(1)
