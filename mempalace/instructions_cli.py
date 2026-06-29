"""
Instruction text output for MemPalace CLI commands.

Each instruction lives as a .md file in the instructions/ directory
inside the package. The CLI reads and prints the file content.
"""

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
