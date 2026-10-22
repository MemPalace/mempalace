"""Drawers filed under an older wing slug stay reachable by project.

Before one slug function, the hooks filed diaries under ``wing_<slug>`` and
the Cursor hooks kept hyphens, while ``mempalace mine`` used the bare slug.
Existing drawers are never re-keyed, so a wing filter has to match every one
of those spellings.
"""

from _mcp_server_helpers import _patch_mcp_server

OLD_SPELLINGS = {
    "mine": "my_app",  # `mempalace mine` of ~/dev/my-app
    "hook": "wing_my_app",  # Claude Code / Antigravity hook diary
    "cursor": "my-app",  # Cursor hook kept the hyphen
}


def _seed_old_palace(collection):
    collection.add(
        ids=[f"old_{name}" for name in OLD_SPELLINGS],
        documents=[
            f"release notes for the my-app project, filed by {name}" for name in OLD_SPELLINGS
        ],
        metadatas=[
            {
                "wing": wing,
                "room": "notes",
                "source_file": f"{name}.md",
                "filed_at": "2026-01-01T00:00:00",
            }
            for name, wing in OLD_SPELLINGS.items()
        ],
    )


def test_search_and_list_find_drawers_under_every_old_spelling(
    monkeypatch, config, palace_path, collection, kg
):
    _seed_old_palace(collection)
    _patch_mcp_server(monkeypatch, config, kg)
    from mempalace import mcp_server

    written = mcp_server.tool_add_drawer(
        wing="my_app", room="notes", content="new notes for my-app"
    )
    assert written.get("success"), written

    listed = mcp_server.tool_list_drawers(wing="my_app", limit=20)
    assert listed["total"] == 4, listed
    assert {d["wing"] for d in listed["drawers"]} == {"my_app", "wing_my_app", "my-app"}

    # The old spelling a client remembers works too.
    assert mcp_server.tool_list_drawers(wing="wing_my_app", limit=20)["total"] == 4

    found = mcp_server.tool_search(
        query="release notes for the my-app project", wing="my_app", limit=10
    )
    assert {hit["wing"] for hit in found["results"]} >= {"my_app", "wing_my_app", "my-app"}, found


def test_diary_read_finds_entries_under_the_old_project_and_agent_wings(
    monkeypatch, config, palace_path, collection, kg
):
    collection.add(
        ids=["old_hook_diary", "old_agent_diary"],
        documents=["old hook checkpoint", "old agent note"],
        metadatas=[
            {
                "wing": "wing_my_app",
                "room": "diary",
                "agent": "claude",
                "topic": "checkpoint",
                "date": "2026-01-01",
                "filed_at": "2026-01-01T00:00:00",
            },
            {
                "wing": "wing_cursor-ide",
                "room": "diary",
                "agent": "cursor-ide",
                "topic": "general",
                "date": "2026-01-01",
                "filed_at": "2026-01-01T00:00:00",
            },
        ],
    )
    _patch_mcp_server(monkeypatch, config, kg)
    from mempalace import mcp_server

    # D-17: hook diaries now file into the project wing itself.
    assert mcp_server.tool_diary_write("claude", "new checkpoint", wing="my_app")["success"]
    project = mcp_server.tool_diary_read("claude", last_n=10, wing="my_app")
    assert {e["content"] for e in project["entries"]} == {"old hook checkpoint", "new checkpoint"}

    # An agent's default wing slugs like a project; the hyphenated old wing still reads.
    written = mcp_server.tool_diary_write("cursor-ide", "new agent note")
    assert written["success"] and written["entry_id"].startswith("diary_wing_cursor_ide_"), written
    agent = mcp_server.tool_diary_read("cursor-ide", last_n=10, wing="wing_cursor_ide")
    assert {e["content"] for e in agent["entries"]} == {"old agent note", "new agent note"}


def test_bm25_fallback_rooms_and_wake_up_match_every_old_spelling(
    monkeypatch, config, palace_path, collection, kg
):
    from mempalace.backends import chroma_sqlite
    from mempalace.backends.base import BaseCollection
    from mempalace.layers import Layer1
    from mempalace.searcher import search_memories

    _seed_old_palace(collection)
    _patch_mcp_server(monkeypatch, config, kg)
    from mempalace import mcp_server

    expected = set(OLD_SPELLINGS.values())
    for driven in (10**9, 0):
        # Both candidate pickers: read the wing's drawers, or rank the palace.
        monkeypatch.setattr(chroma_sqlite, "_FTS_FILTER_DRIVEN_MAX", driven)
        found = search_memories(
            "release notes", palace_path, wing="my_app", vector_disabled=True, n_results=10
        )
        assert {hit["wing"] for hit in found["results"]} == expected, found

    assert mcp_server.tool_list_rooms(wing="my_app")["rooms"] == {"notes": 3}

    def _no_paged_window(*_args, **_kwargs):
        raise AssertionError("wing-scoped L1 left the sqlite recency read")

    monkeypatch.setattr(BaseCollection, "get_recent", _no_paged_window)
    text = Layer1(palace_path=palace_path, wing="my_app").generate()
    for name in OLD_SPELLINGS:
        assert f"filed by {name}" in text, text


def test_stored_wings_no_rule_derives_still_reach_their_project(
    monkeypatch, config, palace_path, collection, kg
):
    from pathlib import Path

    from mempalace.config import normalize_wing_name
    from mempalace.hooks_cli import _workstation_wing

    home_diary = f"wing_{normalize_wing_name(Path.home().name)}"
    collection.add(
        ids=["ingest_cpp", "cursor_cafe", "home_diary"],
        documents=["c++tool build notes", "cafe notes from cursor", "home checkpoint"],
        metadatas=[
            # `mempalace mine` of ~/dev/c++tool kept the plus signs.
            {"wing": "c++tool", "room": "notes", "filed_at": "2026-01-01T00:00:00"},
            # The Cursor hooks turned each non-ASCII byte into `_`.
            {"wing": "caf_notes", "room": "notes", "filed_at": "2026-01-01T00:00:00"},
            # A session in the home directory filed its diary under the user name.
            {
                "wing": home_diary,
                "room": "diary",
                "agent": "claude",
                "topic": "checkpoint",
                "date": "2026-01-01",
                "filed_at": "2026-01-01T00:00:00",
            },
        ],
    )
    _patch_mcp_server(monkeypatch, config, kg)
    from mempalace import mcp_server

    listed = mcp_server.tool_list_drawers(wing="c_tool")
    assert [d["wing"] for d in listed["drawers"]] == ["c++tool"], listed
    found = mcp_server.tool_search(query="c++tool build notes", wing="c_tool", limit=5)
    assert [hit["wing"] for hit in found["results"]] == ["c++tool"], found
    listed = mcp_server.tool_list_drawers(wing="café_notes")
    assert [d["wing"] for d in listed["drawers"]] == ["caf_notes"], listed
    diary = mcp_server.tool_diary_read("claude", last_n=10, wing=_workstation_wing())
    assert [e["content"] for e in diary["entries"]] == ["home checkpoint"], diary
