"""mempalace.tool_capabilities is the only read/write list.

The two security-relevant sets are pinned as literals: a tool moved to the
wrong class in the registry fails here instead of silently changing what a
read-only server or the peer-writer lease lets through.
"""

import subprocess
import sys

from mempalace import mcp_server, service, tool_capabilities

READ_ONLY_REFUSED = {
    "mempalace_add_drawer",
    "mempalace_artifact_put",
    "mempalace_checkpoint",
    "mempalace_create_tunnel",
    "mempalace_delete_by_source",
    "mempalace_delete_drawer",
    "mempalace_delete_drawers",
    "mempalace_delete_hallway",
    "mempalace_delete_tunnel",
    "mempalace_diary_write",
    "mempalace_event_ack",
    "mempalace_event_append",
    "mempalace_hook_settings",
    "mempalace_kg_add",
    "mempalace_kg_invalidate",
    "mempalace_kg_supersede",
    "mempalace_memories_filed_away",
    "mempalace_mine",
    "mempalace_patch_submit",
    "mempalace_sync",
    "mempalace_task_create",
    "mempalace_update_drawer",
}

MUTATING = READ_ONLY_REFUSED - {"mempalace_hook_settings", "mempalace_memories_filed_away"}


def test_read_only_refusals_are_pinned():
    assert mcp_server._READ_ONLY_REFUSED_TOOLS == READ_ONLY_REFUSED


def test_mutating_tools_are_pinned():
    assert mcp_server._MUTATING_TOOLS == MUTATING


def test_every_tool_has_a_capability():
    assert set(tool_capabilities.TOOL_CAPABILITIES) == set(mcp_server.TOOLS)
    for name in mcp_server.TOOLS:
        assert service.classify_tool(name) in {"read", "write", "maintenance"}
    assert service.classify_tool("not_a_tool") == "unknown"


def test_reads_advertise_read_only_hint_and_nothing_else_does():
    reads = {n for n in mcp_server.TOOLS if service.classify_tool(n) == "read"}
    hinted = {
        n
        for n, tool in mcp_server.TOOLS.items()
        if mcp_server._tool_catalog_entry(n, tool).get("annotations", {}).get("readOnlyHint")
    }
    assert hinted == reads
    assert hinted.isdisjoint(READ_ONLY_REFUSED)


def test_reconnect_stays_available_in_read_only_mode():
    assert service.classify_tool("mempalace_reconnect") == "maintenance"
    assert "mempalace_reconnect" not in READ_ONLY_REFUSED


def test_daemon_classification_does_not_import_the_mcp_server():
    code = (
        "import sys, mempalace.service; "
        "print('mempalace.mcp_server' in sys.modules, 'chromadb' in sys.modules)"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout
    assert out.strip() == "False False"
