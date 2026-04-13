"""Verify WRITE_TOOL_NAMES enumerates every write tool and is a frozenset."""
from mempalace.mcp_server import TOOLS, WRITE_TOOL_NAMES


def test_write_tool_names_is_frozenset():
    assert isinstance(WRITE_TOOL_NAMES, frozenset)


def test_write_tool_names_all_exist_in_tools():
    missing = WRITE_TOOL_NAMES - set(TOOLS.keys())
    assert not missing, f"WRITE_TOOL_NAMES includes unknown tools: {missing}"


def test_known_writes_are_listed():
    """Sanity: the canonical write tools from the spec appear."""
    expected = {
        "mempalace_add_drawer",
        "mempalace_delete_drawer",
        "mempalace_diary_write",
        "mempalace_kg_add",
        "mempalace_kg_invalidate",
    }
    assert expected.issubset(WRITE_TOOL_NAMES)
