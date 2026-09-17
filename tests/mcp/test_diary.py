"""MCP server tests — diary tools and chunked diary ids."""

from datetime import datetime

from _mcp_server_helpers import (
    _get_collection,
    _patch_mcp_server,
)


def test_diary_write_chunked_logical_id_fetches_deletes_and_lists_as_one(
    monkeypatch, config, palace_path, kg
):
    """Regression for #2185: the ``entry_id`` returned by a chunked
    ``tool_diary_write`` must behave like any other logical drawer id.

    Before the fix the diary chunking path stamped only ``parent_entry_id``
    while logical-id resolution queried only ``parent_drawer_id``, so
    get/update/delete answered "Drawer not found" for the one id the diary
    tools ever hand to MCP clients, and ``list_drawers`` showed the entry as
    N unrelated chunk rows. Mirrors the ``tool_add_drawer`` contract locked
    in by #1782.
    """
    _patch_mcp_server(monkeypatch, config, kg)
    _client, _col = _get_collection(palace_path, create=True)
    del _client

    from mempalace.mcp_server import (
        tool_delete_drawer,
        tool_diary_write,
        tool_get_drawer,
        tool_list_drawers,
    )

    oversized = "Z" * 5000
    written = tool_diary_write(agent_name="TestAgent", entry=oversized, topic="general")
    assert written["success"] is True
    assert written["chunks"] > 1

    entry_id = written["entry_id"]

    fetched = tool_get_drawer(entry_id)
    assert "error" not in fetched
    assert fetched["drawer_id"] == entry_id
    assert fetched["content"] == oversized, "must return the entry verbatim, not one chunk"
    assert fetched["chunks"] == written["chunks"]
    assert fetched["chunk_ids"] == written["chunk_ids"]

    listed = tool_list_drawers(wing="wing_testagent", room="diary")
    assert listed["total"] == 1, "a chunked entry is ONE logical drawer, not N chunk rows"
    assert listed["drawers"][0]["drawer_id"] == entry_id
    assert listed["drawers"][0]["chunks"] == written["chunks"]

    deleted = tool_delete_drawer(entry_id)
    assert deleted["success"] is True
    assert deleted["chunks_deleted"] == written["chunks"]

    missing = tool_get_drawer(entry_id)
    assert "error" in missing


def test_diary_write_chunked_logical_id_updates_group(monkeypatch, config, palace_path, kg):
    """Regression for #2185: updating a chunked diary entry by its
    ``entry_id`` must rewrite the whole underlying chunk group."""
    _patch_mcp_server(monkeypatch, config, kg)
    _client, _col = _get_collection(palace_path, create=True)
    del _client

    from mempalace.mcp_server import (
        tool_diary_write,
        tool_get_drawer,
        tool_update_drawer,
    )

    written = tool_diary_write(agent_name="TestAgent", entry="A" * 4000, topic="general")
    assert written["chunks"] > 1
    entry_id = written["entry_id"]

    updated = tool_update_drawer(entry_id, content="B" * 2600)
    assert updated["success"] is True
    assert updated["drawer_id"] == entry_id

    fetched = tool_get_drawer(entry_id)
    assert fetched["content"] == "B" * 2600
    _client2, col = _get_collection(palace_path)
    del _client2
    assert "".join(col.get()["documents"]) == "B" * 2600, "stale chunks must not survive"


def test_legacy_diary_chunks_resolve_without_parent_drawer_id(monkeypatch, config, palace_path, kg):
    """Regression for #2185: palaces written BEFORE this fix carry diary
    chunks tagged only with ``parent_entry_id``. The read paths must resolve
    that shape too, so existing palaces are repaired with no data migration.
    """
    _patch_mcp_server(monkeypatch, config, kg)
    _client, col = _get_collection(palace_path, create=True)
    del _client

    from mempalace.mcp_server import (
        tool_delete_drawer,
        tool_get_drawer,
        tool_list_drawers,
    )

    entry_id = "diary_wing_lily_20260808_142113121027_3e4c74763d73"
    # Exactly what mempalace 3.6.0 wrote: parent_entry_id only.
    col.upsert(
        ids=[f"{entry_id}_chunk_{i:06d}" for i in range(3)],
        documents=["legacy-0 ", "legacy-1 ", "legacy-2"],
        metadatas=[
            {
                "wing": "wing_lily",
                "room": "diary",
                "type": "diary_entry",
                "chunk_index": i,
                "parent_entry_id": entry_id,
                "filed_at": "2026-08-08T14:21:13",
            }
            for i in range(3)
        ],
    )

    fetched = tool_get_drawer(entry_id)
    assert "error" not in fetched, f"legacy diary chunks must resolve; got {fetched}"
    assert fetched["content"] == "legacy-0 legacy-1 legacy-2"
    assert fetched["chunks"] == 3

    listed = tool_list_drawers(wing="wing_lily", room="diary")
    assert listed["total"] == 1
    assert listed["drawers"][0]["drawer_id"] == entry_id

    deleted = tool_delete_drawer(entry_id)
    assert deleted["success"] is True
    assert deleted["chunks_deleted"] == 3


# ── Delete by source (#1722) ────────────────────────────────────────────


class TestDiaryTools:
    def test_diary_write_and_read(self, monkeypatch, config, palace_path, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        _client, _col = _get_collection(palace_path, create=True)
        del _client
        from mempalace.mcp_server import tool_diary_write, tool_diary_read

        w = tool_diary_write(
            agent_name="TestAgent",
            entry="Today we discussed authentication patterns.",
            topic="architecture",
        )
        assert w["success"] is True
        # agent_name is normalized to lowercase on write (#1243).
        assert w["agent"] == "testagent"

        r = tool_diary_read(agent_name="TestAgent")
        assert r["total"] == 1
        assert r["entries"][0]["topic"] == "architecture"
        assert "authentication" in r["entries"][0]["content"]

    def test_diary_read_empty(self, monkeypatch, config, palace_path, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        _client, _col = _get_collection(palace_path, create=True)
        del _client
        from mempalace.mcp_server import tool_diary_read

        r = tool_diary_read(agent_name="Nobody")
        assert r["entries"] == []

    def test_diary_read_pages_past_10000_and_returns_true_latest(self, monkeypatch, config, kg):
        """Entries beyond the old 10k cap must affect both recency and total."""
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace import mcp_server

        class PagedDiaryCollection:
            total = 10001

            def __init__(self):
                self.calls = []

            def get(self, *, where, include, limit, offset):
                self.calls.append(
                    {"where": where, "include": include, "limit": limit, "offset": offset}
                )
                stop = min(offset + limit, self.total)
                indices = range(offset, stop)
                return {
                    "ids": [f"diary-{index}" for index in indices],
                    "documents": [f"entry-{index}" for index in indices],
                    "metadatas": [
                        {
                            "filed_at": f"2026-01-01T00:00:00.{index:06d}Z",
                            "date": "2026-01-01",
                            "topic": "pagination",
                        }
                        for index in indices
                    ],
                }

        collection = PagedDiaryCollection()
        monkeypatch.setattr(mcp_server, "_get_collection", lambda: collection)

        result = mcp_server.tool_diary_read(agent_name="TestAgent", last_n=10)

        assert result["total"] == 10001
        assert result["showing"] == 10
        assert [entry["content"] for entry in result["entries"]] == [
            f"entry-{index}" for index in range(10000, 9990, -1)
        ]
        assert len(collection.calls) == 11
        assert all(call["limit"] == 1000 for call in collection.calls)
        assert [call["offset"] for call in collection.calls] == list(range(0, 11000, 1000))
        assert all(
            call["where"] == {"$and": [{"room": "diary"}, {"agent": "testagent"}]}
            for call in collection.calls
        )

    def test_diary_write_same_second_shared_prefix_no_collision(
        self, monkeypatch, config, palace_path, kg
    ):
        _patch_mcp_server(monkeypatch, config, kg)
        _client, _col = _get_collection(palace_path, create=True)
        del _client

        from mempalace import mcp_server

        class FrozenDateTime:
            calls = [
                datetime(2026, 4, 13, 22, 15, 30, 123456),
                datetime(2026, 4, 13, 22, 15, 30, 123457),
            ]
            fallback = datetime(2026, 4, 13, 22, 15, 30, 123457)

            @classmethod
            def now(cls):
                if cls.calls:
                    return cls.calls.pop(0)
                return cls.fallback

        monkeypatch.setattr(mcp_server, "datetime", FrozenDateTime)

        from mempalace.mcp_server import tool_diary_read, tool_diary_write

        entry1 = "A" * 50 + " entry one"
        entry2 = "A" * 50 + " entry two"

        result1 = tool_diary_write(agent_name="TestAgent", entry=entry1, topic="status")
        result2 = tool_diary_write(agent_name="TestAgent", entry=entry2, topic="status")

        assert result1["success"] is True
        assert result2["success"] is True
        assert result1["entry_id"] != result2["entry_id"]

        read_result = tool_diary_read(agent_name="TestAgent")
        contents = [entry["content"] for entry in read_result["entries"]]
        assert read_result["total"] == 2
        assert entry1 in contents
        assert entry2 in contents

    def test_diary_read_empty_wing_spans_all_wings(self, monkeypatch, config, palace_path, kg):
        """diary_read(wing='') must return entries from every wing this agent
        wrote to. Hooks write to project-derived wings (#659); a reader that
        silos by default wing would never see those entries."""
        _patch_mcp_server(monkeypatch, config, kg)
        _client, _col = _get_collection(palace_path, create=True)
        del _client
        from mempalace.mcp_server import tool_diary_read, tool_diary_write

        w1 = tool_diary_write(
            agent_name="TestAgent",
            entry="default-wing entry",
            topic="general",
        )
        w2 = tool_diary_write(
            agent_name="TestAgent",
            entry="project-wing entry",
            topic="general",
            wing="wing_someproject",
        )
        assert w1["success"] and w2["success"]

        # Empty wing → return both entries
        r = tool_diary_read(agent_name="TestAgent", wing="")
        assert r["total"] == 2
        contents = {e["content"] for e in r["entries"]}
        assert "default-wing entry" in contents
        assert "project-wing entry" in contents

        # Explicit wing → return only that wing's entries
        r_scoped = tool_diary_read(agent_name="TestAgent", wing="wing_someproject")
        assert r_scoped["total"] == 1
        assert r_scoped["entries"][0]["content"] == "project-wing entry"

    def test_diary_read_case_insensitive_agent(self, monkeypatch, config, palace_path, kg):
        """Regression for #1243: diary_read must be case-insensitive over
        agent_name. Writing as "Claude" and reading as "claude" (or vice
        versa) must surface the same entries — sanitize_name preserved
        case, which silently dropped reads when the agent name's casing
        differed from the write."""
        _patch_mcp_server(monkeypatch, config, kg)
        _client, _col = _get_collection(palace_path, create=True)
        del _client
        from mempalace.mcp_server import tool_diary_read, tool_diary_write

        # Write as "Claude" → read as "claude" should match.
        w1 = tool_diary_write(
            agent_name="Claude",
            entry="entry written as Claude",
            topic="general",
        )
        assert w1["success"]

        r1 = tool_diary_read(agent_name="claude")
        assert "entries" in r1, r1
        contents1 = {e["content"] for e in r1["entries"]}
        assert "entry written as Claude" in contents1

        # Write as "CLAUDE" → read as "Claude" should also match the
        # same agent. After normalization both writes target the same
        # lowercase agent identity, so both entries are returned.
        w2 = tool_diary_write(
            agent_name="CLAUDE",
            entry="entry written as CLAUDE",
            topic="general",
        )
        assert w2["success"]

        r2 = tool_diary_read(agent_name="Claude")
        contents2 = {e["content"] for e in r2["entries"]}
        assert "entry written as Claude" in contents2
        assert "entry written as CLAUDE" in contents2

        # The stored agent metadata is the lowercase form, and the
        # default wing is derived from that lowercase form too.
        assert w1["agent"] == "claude"
        assert w2["agent"] == "claude"

    # ── #1539: oversized-entry chunking ────────────────────────────

    def test_diary_write_normal_entry_single_drawer(self, monkeypatch, config, palace_path, kg):
        """Regression catch: a normal entry (< CHUNK_SIZE) must produce
        exactly one drawer with ``chunks == 1`` in the result. Existing
        pre-#1539 behaviour preserved for the common path."""
        _patch_mcp_server(monkeypatch, config, kg)
        _client, _col = _get_collection(palace_path, create=True)
        del _client
        from mempalace.mcp_server import tool_diary_write

        r = tool_diary_write(
            agent_name="TestAgent",
            entry="A normal-length entry that fits comfortably under chunk_size.",
            topic="general",
        )
        assert r["success"] is True
        assert r["chunks"] == 1
        _client2, col = _get_collection(palace_path)
        del _client2
        assert col.count() == 1

    def test_diary_write_oversized_entry_chunked(self, monkeypatch, config, palace_path, kg):
        """Regression for #1539: an entry far above CHUNK_SIZE must be
        sliced into bounded per-chunk drawers, each linked by a
        ``parent_entry_id`` metadata field. No single document stored
        may exceed CHUNK_SIZE."""
        _patch_mcp_server(monkeypatch, config, kg)
        _client, _col = _get_collection(palace_path, create=True)
        del _client
        from mempalace.mcp_server import tool_diary_write

        # 5000 chars: well above CHUNK_SIZE=800. Expected chunks: ceil(5000/800) = 7.
        oversized = "Z" * 5000
        r = tool_diary_write(agent_name="TestAgent", entry=oversized, topic="general")

        assert r["success"] is True
        assert r["chunks"] > 1, f"oversized entry must produce >1 chunks; got {r['chunks']}"
        assert "chunk_ids" in r and len(r["chunk_ids"]) == r["chunks"]

        _client2, col = _get_collection(palace_path)
        del _client2
        stored = col.get()
        assert all(len(d) <= 800 for d in stored["documents"]), (
            f"no stored document may exceed CHUNK_SIZE=800; "
            f"got max={max(len(d) for d in stored['documents'])}"
        )
        joined = "".join(stored["documents"])
        assert joined == oversized, "joined chunks must equal original entry verbatim"

        parent_ids = {m.get("parent_entry_id") for m in stored["metadatas"]}
        assert len(parent_ids) == 1 and None not in parent_ids, (
            f"all chunks must share one parent_entry_id; got {parent_ids}"
        )

    def test_diary_write_chunk_index_metadata(self, monkeypatch, config, palace_path, kg):
        """Regression for #1539: each oversized-entry chunk must carry a
        ``chunk_index`` metadata field that runs 0, 1, 2, ... in order."""
        _patch_mcp_server(monkeypatch, config, kg)
        _client, _col = _get_collection(palace_path, create=True)
        del _client
        from mempalace.mcp_server import tool_diary_write

        oversized = "Q" * 3500  # ~5 chunks at CHUNK_SIZE=800
        r = tool_diary_write(agent_name="TestAgent", entry=oversized, topic="general")
        assert r["success"] is True and r["chunks"] > 1

        _client2, col = _get_collection(palace_path)
        del _client2
        stored = col.get()
        indices = sorted(m["chunk_index"] for m in stored["metadatas"])
        assert indices == list(range(len(indices))), (
            f"chunk_index must be 0..N-1 contiguous; got {indices}"
        )


# ── Cache Invalidation (inode/mtime) ──────────────────────────────────
