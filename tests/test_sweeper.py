"""TDD: tandem sweeper that catches what the primary miner missed.

The primary miner (miner.py / convo_miner.py) runs at file granularity
and can drop data (size caps, silent OSError, dedup false-positives).
The sweeper is a second miner that works at MESSAGE granularity,
using timestamp as the coordination cursor.

For each session in the transcript directory:
  1. Look up max(timestamp) across all drawers with matching session_id
  2. Stream the jsonl, yielding only user/assistant messages after the cursor
  3. Write one small drawer per message with:
       session_id, uuid, timestamp, role, content
  4. Idempotent: re-running sweeps should find nothing new on a complete palace.

This test file is TDD — written BEFORE mempalace/sweeper.py exists.
"""

import json

import pytest


@pytest.fixture
def mock_claude_jsonl(tmp_path):
    """Real Claude Code jsonl shape: user/assistant records among progress noise."""
    path = tmp_path / "session_abc.jsonl"
    lines = [
        # Noise: progress event, no message
        {
            "type": "progress",
            "timestamp": "2026-04-18T10:00:00Z",
            "sessionId": "abc",
            "uuid": "p-1",
        },
        # User message
        {
            "type": "user",
            "timestamp": "2026-04-18T10:00:05Z",
            "sessionId": "abc",
            "uuid": "u-1",
            "message": {"role": "user", "content": "What's the capital of France?"},
        },
        # Assistant reply
        {
            "type": "assistant",
            "timestamp": "2026-04-18T10:00:06Z",
            "sessionId": "abc",
            "uuid": "a-1",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "Paris."}]},
        },
        # Noise: file-history-snapshot
        {"type": "file-history-snapshot", "messageId": "abc-snap"},
        # Second user/assistant exchange
        {
            "type": "user",
            "timestamp": "2026-04-18T10:01:00Z",
            "sessionId": "abc",
            "uuid": "u-2",
            "message": {"role": "user", "content": "And of Germany?"},
        },
        {
            "type": "assistant",
            "timestamp": "2026-04-18T10:01:01Z",
            "sessionId": "abc",
            "uuid": "a-2",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "Berlin."}]},
        },
    ]
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    return path


class TestSweeperParsing:
    def test_parse_yields_only_user_and_assistant(self, mock_claude_jsonl):
        from mempalace.sweeper import parse_claude_jsonl

        records = list(parse_claude_jsonl(str(mock_claude_jsonl)))
        roles = [r["role"] for r in records]
        assert roles == ["user", "assistant", "user", "assistant"], (
            f"Expected 4 user/assistant in order, got {roles}. "
            "Noise records (progress, file-history-snapshot) must be "
            "filtered out."
        )

    def test_parse_extracts_session_id_and_timestamp(self, mock_claude_jsonl):
        from mempalace.sweeper import parse_claude_jsonl

        records = list(parse_claude_jsonl(str(mock_claude_jsonl)))
        first = records[0]
        assert first["session_id"] == "abc"
        assert first["timestamp"] == "2026-04-18T10:00:05Z"
        assert first["uuid"] == "u-1"

    def test_parse_normalizes_assistant_content_list_to_text(self, mock_claude_jsonl):
        from mempalace.sweeper import parse_claude_jsonl

        records = list(parse_claude_jsonl(str(mock_claude_jsonl)))
        assistant_rec = records[1]
        assert assistant_rec["role"] == "assistant"
        assert "Paris" in assistant_rec["content"], (
            f"Assistant content blocks must be flattened to text; got: {assistant_rec['content']!r}"
        )

    def test_parse_preserves_tool_blocks_verbatim(self, tmp_path):
        """Per the design principle "verbatim always", tool_use and
        tool_result blocks must NOT be truncated. A long tool input
        (e.g. a large diff handed to a code-edit tool) must round-trip
        in full, otherwise we silently lose user-adjacent data.
        """
        import json as _json

        from mempalace.sweeper import parse_claude_jsonl

        big_input = {"diff": "x" * 5000}  # well past the old 500-char cap
        path = tmp_path / "session_tools.jsonl"
        path.write_text(
            _json.dumps(
                {
                    "type": "assistant",
                    "timestamp": "2026-04-18T10:00:00Z",
                    "sessionId": "tools-1",
                    "uuid": "a-tool",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "tool_use", "name": "Edit", "input": big_input},
                        ],
                    },
                }
            )
            + "\n"
        )

        records = list(parse_claude_jsonl(str(path)))
        assert len(records) == 1
        content = records[0]["content"]
        # The full 5000-char value must be present — no truncation marker,
        # no [:500] slice. Look for the raw string in the serialized form.
        assert big_input["diff"] in content, (
            "tool_use input was truncated. The verbatim guarantee requires "
            f"the full payload to round-trip. Got len={len(content)}."
        )


class TestSweeperTandem:
    """The sweeper coordinates with other miners via max(timestamp)."""

    def test_sweep_empty_palace_ingests_all_messages(self, mock_claude_jsonl, tmp_path):
        from mempalace.sweeper import sweep

        palace_path = str(tmp_path / "palace")
        result = sweep(str(mock_claude_jsonl), palace_path)
        assert result["drawers_added"] == 4, (
            f"Empty palace: all 4 user/assistant messages should ingest. "
            f"Got drawers_added={result['drawers_added']}."
        )

    def test_sweep_is_idempotent(self, mock_claude_jsonl, tmp_path):
        """Running the sweep twice must not duplicate drawers."""
        from mempalace.sweeper import sweep

        palace_path = str(tmp_path / "palace")
        first = sweep(str(mock_claude_jsonl), palace_path)
        second = sweep(str(mock_claude_jsonl), palace_path)
        assert first["drawers_added"] == 4
        assert second["drawers_added"] == 0, (
            f"Second sweep must be a no-op on unchanged data. "
            f"Got drawers_added={second['drawers_added']} — "
            "cursor logic is broken."
        )

    def test_sweep_resumes_from_cursor(self, tmp_path):
        """If half the messages are already in the palace, sweep picks up
        only the later half."""
        from mempalace.sweeper import sweep

        jsonl_path = tmp_path / "session.jsonl"
        lines = [
            {
                "type": "user",
                "timestamp": "2026-04-18T09:00:00Z",
                "sessionId": "s1",
                "uuid": "u1",
                "message": {"role": "user", "content": "first"},
            },
            {
                "type": "assistant",
                "timestamp": "2026-04-18T09:00:01Z",
                "sessionId": "s1",
                "uuid": "a1",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "one"}]},
            },
        ]
        jsonl_path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")

        palace_path = str(tmp_path / "palace")
        first = sweep(str(jsonl_path), palace_path)
        assert first["drawers_added"] == 2

        # Append two more exchanges simulating live session growth.
        more_lines = [
            {
                "type": "user",
                "timestamp": "2026-04-18T09:05:00Z",
                "sessionId": "s1",
                "uuid": "u2",
                "message": {"role": "user", "content": "second"},
            },
            {
                "type": "assistant",
                "timestamp": "2026-04-18T09:05:01Z",
                "sessionId": "s1",
                "uuid": "a2",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "two"}]},
            },
        ]
        with open(jsonl_path, "a") as f:
            for x in more_lines:
                f.write(json.dumps(x) + "\n")

        second = sweep(str(jsonl_path), palace_path)
        assert second["drawers_added"] == 2, (
            f"Second sweep should pick up only the 2 new exchanges, "
            f"got {second['drawers_added']}. Cursor (max-timestamp) "
            "coordination is broken."
        )

    def test_sweep_recovers_untaken_message_at_cursor_timestamp(self, tmp_path):
        """Regression for Copilot PR #998 review: with a `<= cursor` skip,
        any message sharing the max timestamp but not yet ingested (e.g.
        crash mid-batch) would be lost forever. The skip must be `<` and
        tie-break via deterministic drawer ID.

        Scenario: three messages share timestamp T. First sweep ingests
        two of them and the process dies before the third. Second sweep
        must pick up the third — not skip it because cursor == T.
        """
        from mempalace.palace import get_collection
        from mempalace.sweeper import (
            _drawer_id_for_message,
            parse_claude_jsonl,
            sweep,
        )

        shared_ts = "2026-04-18T11:00:00Z"
        lines = [
            {
                "type": "user",
                "timestamp": shared_ts,
                "sessionId": "s-tie",
                "uuid": f"u-{i}",
                "message": {"role": "user", "content": f"msg {i}"},
            }
            for i in range(3)
        ]
        jsonl_path = tmp_path / "tied.jsonl"
        jsonl_path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")

        palace_path = str(tmp_path / "palace")
        # Simulate a partial ingest: write 2 of 3 directly via the backend
        # with the same drawer IDs the sweeper would use.
        col = get_collection(palace_path, create=True)
        recs = list(parse_claude_jsonl(str(jsonl_path)))
        partial_ids = [_drawer_id_for_message(r["session_id"], r["uuid"]) for r in recs[:2]]
        col.upsert(
            ids=partial_ids,
            documents=[f"USER: {r['content']}" for r in recs[:2]],
            metadatas=[
                {
                    "session_id": r["session_id"],
                    "timestamp": r["timestamp"],
                    "message_uuid": r["uuid"],
                    "role": r["role"],
                    "ingest_mode": "sweep",
                }
                for r in recs[:2]
            ],
        )

        # Now run the sweeper. It must pick up the 3rd message, not skip
        # it because cursor == its timestamp.
        result = sweep(str(jsonl_path), palace_path)
        assert result["drawers_added"] == 1, (
            f"Sweeper lost the untaken message at cursor timestamp. "
            f"Expected drawers_added=1 (the 3rd record), got "
            f"{result['drawers_added']}. Cursor skip is still `<=` "
            "instead of `<`, or tie-break via drawer-id is broken."
        )
        assert result["drawers_already_present"] == 2, (
            f"Expected 2 drawers already present (the partial ingest), "
            f"got {result['drawers_already_present']}."
        )


class TestSweeperDrawerMetadata:
    """Each drawer must carry the metadata the tandem-miner coordination
    depends on: session_id, timestamp, uuid, role."""

    def test_drawer_has_session_id_and_timestamp_metadata(self, mock_claude_jsonl, tmp_path):
        from mempalace.sweeper import sweep
        from mempalace.palace import get_collection

        palace_path = str(tmp_path / "palace")
        sweep(str(mock_claude_jsonl), palace_path)

        col = get_collection(palace_path, create=False)
        data = col.get(include=["metadatas"])
        metas = data["metadatas"]
        assert metas, "No drawers written"

        for m in metas:
            assert m.get("session_id") == "abc", f"Drawer missing session_id metadata: {m}"
            assert m.get("timestamp"), f"Drawer missing timestamp metadata: {m}"
            assert m.get("message_uuid"), f"Drawer missing message_uuid metadata: {m}"
            assert m.get("role") in (
                "user",
                "assistant",
            ), f"Drawer missing or wrong role metadata: {m}"
