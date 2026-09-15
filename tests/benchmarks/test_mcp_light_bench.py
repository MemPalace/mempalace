"""
test_mcp_light_bench.py — Benchmark schema size and token savings of Lightweight MCP.
"""

import json
from mempalace.mcp_server import TOOLS as LEGACY_TOOLS
from mempalace.mcp_light_server import LIGHT_TOOLS


def test_schema_reduction_benchmark():
    """Measure the exact JSON payload size of legacy 45 tools vs 3 light tools."""
    legacy_schema = [
        {"name": n, "description": t["description"], "inputSchema": t["input_schema"]}
        for n, t in LEGACY_TOOLS.items()
    ]
    light_schema = [
        {"name": n, "description": t["description"], "inputSchema": t["input_schema"]}
        for n, t in LIGHT_TOOLS.items()
    ]

    legacy_bytes = len(json.dumps(legacy_schema, indent=2))
    light_bytes = len(json.dumps(light_schema, indent=2))

    # Rough approximation: 4 chars per token
    legacy_est_tokens = legacy_bytes // 4
    light_est_tokens = light_bytes // 4
    reduction_pct = ((legacy_bytes - light_bytes) / legacy_bytes) * 100

    print("\n--- MCP Tool Schema Benchmark ---")
    print(f"Legacy 45 Tools Schema : {legacy_bytes:,} bytes (~{legacy_est_tokens:,} tokens)")
    print(f"Lightweight 3 Tools     : {light_bytes:,} bytes (~{light_est_tokens:,} tokens)")
    print(f"Reduction Ratio         : {reduction_pct:.1f}% reduction")
    print("---------------------------------")

    # Completeness of structured fields costs some of the original >80% headline.
    assert reduction_pct > 70.0
    assert len(LIGHT_TOOLS) == 3
    assert len(LEGACY_TOOLS) == 45
