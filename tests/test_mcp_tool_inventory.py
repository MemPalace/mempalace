#!/usr/bin/env python3
"""
Ensure MCP tool-count claims in documentation stay aligned with the implementation.
"""

import re
from pathlib import Path


DOC_PATHS = [
    Path("README.md"),
    Path("mempalace/README.md"),
    Path("website/guide/mcp-integration.md"),
    Path("website/guide/claude-code.md"),
    Path("website/guide/openclaw.md"),
    Path("website/reference/modules.md"),
    Path("website/reference/mcp-tools.md"),
    Path("skills/mempalace/SKILL.md"),
    Path("integrations/openclaw/SKILL.md"),
]


def _tool_count_from_server() -> int:
    server_src = Path("mempalace/mcp_server.py").read_text(encoding="utf-8")
    return len(re.findall(r'"(mempalace_\w+)":\s*\{', server_src))


def _doc_tool_counts(path: Path) -> list[int]:
    text = path.read_text(encoding="utf-8", errors="replace")
    # Tool-count claims can be adjacent ("30 MCP tools") or with
    # an extra qualifier ("all 30 MemPalace MCP tools") on the same line.
    return [int(m.group(1)) for m in re.finditer(r"\b(\d+)\b(?:(?!\n).){0,40}\btools?\b", text)]


def test_mcp_tool_count_claims_match_code():
    actual = _tool_count_from_server()
    for path in DOC_PATHS:
        counts = _doc_tool_counts(path)
        assert counts, f"{path} is expected to claim MCP tool count in a 'N tools' phrase"
        if len(set(counts)) != 1:
            raise AssertionError(f"{path} has inconsistent tool count claims: {counts}")
        assert counts[0] == actual, (
            f"{path} claims {counts[0]} MCP tools, but mcp_server.py registers {actual}"
        )
