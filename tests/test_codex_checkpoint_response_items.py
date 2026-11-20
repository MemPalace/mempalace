"""Codex response-item rollouts must reach the diary checkpoint save path."""

import json
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mempalace import hooks_cli


def _item(text, *, role="user", kinds=None):
    blocks = text if isinstance(text, list) else [{"type": "input_text", "text": text}]
    payload = {"type": "message", "role": role, "content": blocks}
    if kinds is not None:
        payload["internal_chat_message_metadata_passthrough"] = {"content_item_kinds": kinds}
    return {"type": "response_item", "payload": payload}


def _rollout(tmp_path, rows, *, source="cli"):
    transcript = tmp_path / "rollout.jsonl"
    entries = [{"type": "session_meta", "payload": {"source": source}}, *rows]
    transcript.write_text("".join(json.dumps(row) + "\n" for row in entries), encoding="utf-8")
    return str(transcript)


def test_response_item_only_checkpoint_extracts_user_messages(tmp_path):
    expected = [f"Synthetic user decision {i}" for i in range(hooks_cli.SAVE_INTERVAL)]
    transcript = _rollout(tmp_path, [_item(text) for text in expected])

    assert hooks_cli._count_human_messages(transcript) == hooks_cli.SAVE_INTERVAL
    assert hooks_cli._extract_recent_messages(transcript) == expected
    assert hooks_cli._extract_recent_messages(transcript, count=2) == expected[-2:]


def test_response_item_checkpoint_uses_normalizer_user_block_filtering(tmp_path):
    transcript = _rollout(
        tmp_path,
        [
            _item("<environment_context>injected</environment_context>"),
            _item("# AGENTS.md instructions for /workspace"),
            _item("system text", role="system"),
            _item("assistant text", role="assistant"),
            _item("<task-notification>injected</task-notification>"),
            _item(
                [
                    {"type": "input_text", "text": "app state"},
                    {"type": "input_text", "text": "Keep these exact words: café → 中文"},
                    {"type": "input_text", "text": "<image name=[Image #1]>"},
                    {"type": "input_image", "image_url": "synthetic"},
                    {"type": "input_text", "text": "and this second block"},
                ],
                kinds=["app.state", "user.text", "user.text", "user.image", "user.text"],
            ),
        ],
    )

    assert hooks_cli._extract_recent_messages(transcript) == [
        "Keep these exact words: café → 中文\nand this second block"
    ]


@pytest.mark.parametrize("event_type", ["user_message", "agent_message"])
def test_response_items_are_fallback_only_when_no_canonical_turn_exists(tmp_path, event_type):
    transcript = _rollout(
        tmp_path,
        [
            _item("duplicate or noncanonical user text"),
            {"type": "event_msg", "payload": {"type": event_type, "message": "canonical turn"}},
            _item("another noncanonical user message"),
        ],
    )

    expected = ["canonical turn"] if event_type == "user_message" else []
    assert hooks_cli._extract_recent_messages(transcript) == expected


def test_response_item_fallback_excludes_subagent_parent_prompts(tmp_path):
    transcript = _rollout(
        tmp_path,
        [_item("Parent-agent instructions are not the user's words")],
        source={"subagent": {"thread_spawn": {"parent_thread_id": "parent"}}},
    )

    assert hooks_cli._extract_recent_messages(transcript) == []


def test_response_item_stop_checkpoint_waits_for_hub_ack_then_stops_repeating(
    tmp_path, monkeypatch
):
    from mempalace import hub_client

    transcript = _rollout(
        tmp_path, [_item(f"Synthetic user decision {i}") for i in range(hooks_cli.SAVE_INTERVAL)]
    )
    state = tmp_path / "hook_state"
    state.mkdir()
    monkeypatch.setattr(hooks_cli, "PALACE_ROOT", tmp_path)
    monkeypatch.setattr(hooks_cli, "STATE_DIR", state)
    config = SimpleNamespace(
        hooks_auto_save=True,
        hook_silent_save=True,
        hook_desktop_toast=False,
        palace_path=str(tmp_path / "palace"),
    )
    monkeypatch.setattr(hooks_cli, "MempalaceConfig", lambda: config)
    routing = SimpleNamespace(blocked=False, use_daemon=False)
    monkeypatch.setattr(hooks_cli, "_current_hook_write_routing", lambda: routing)
    monkeypatch.setattr(hooks_cli, "_hook_write_routing_context", lambda: nullcontext(routing))
    monkeypatch.setattr(hooks_cli, "_project_wing", lambda _path: "synthetic-project")
    queue = Mock(return_value=True)
    monkeypatch.setattr(hooks_cli, "_queue_diary_checkpoint", queue)
    output = Mock()
    monkeypatch.setattr(hooks_cli, "_output", output)
    ingest = Mock()
    monkeypatch.setattr(hooks_cli, "_ingest_transcript", ingest)
    monkeypatch.setattr(hooks_cli, "_maybe_auto_ingest", Mock())
    data = {"session_id": "response-only", "transcript_path": transcript}

    hooks_cli.hook_stop(data, "codex")

    queue.assert_called_once()
    payload = queue.call_args.args[0]
    assert payload["messages_folded"] == hooks_cli.SAVE_INTERVAL
    assert payload["exchange_count"] == hooks_cli.SAVE_INTERVAL
    assert "Synthetic user decision 14" in payload["entry"]
    assert "checkpoint queued" in output.call_args.args[0]["systemMessage"]
    ingest.assert_not_called()
    marker = state / "response-only_last_save"
    assert not marker.exists()

    forward = Mock(return_value=(True, {"success": False, "error": "synthetic failure"}))
    monkeypatch.setattr(hub_client, "forward_tool_call", forward)
    assert hooks_cli._apply_diary_checkpoint(payload) is False
    assert not marker.exists()

    forward.return_value = (True, {"success": True, "entry_id": "synthetic-entry"})
    assert hooks_cli._apply_diary_checkpoint(payload) is True
    assert marker.read_text(encoding="utf-8") == str(hooks_cli.SAVE_INTERVAL)
    assert json.loads((state / "last_checkpoint").read_text(encoding="utf-8"))["msgs"] == 15
    assert forward.call_args.args[1] == "mempalace_diary_write"
    assert forward.call_args.args[2]["entry"] == payload["entry"]

    hooks_cli.hook_stop(data, "codex")
    queue.assert_called_once()
    assert output.call_args.args[0] == {}
