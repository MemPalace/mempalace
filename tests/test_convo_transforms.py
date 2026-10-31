"""Lossy steps of conversation parsing are stamped on the drawers (W1-10, D-02)."""

import json

import chromadb

from mempalace.convo_miner import mine_convos
from mempalace.normalize import (
    _format_tool_result,
    _format_tool_use,
    _split_conversations,
    _try_chatgpt_json,
    _try_claude_ai_json_split,
    _try_claude_code_jsonl,
    _try_codex_jsonl,
    _try_continue_json,
    _try_gemini_json,
    _try_gemini_jsonl,
    _try_pi_jsonl,
    _try_slack_json,
    recording_transforms,
    strip_noise,
)
from mempalace.sources import transforms as transform_refs
from mempalace.sources.transforms import format_transforms


def _claude_code(*entries):
    return "\n".join(json.dumps(e) for e in entries) + "\n"


def _user(content):
    return {"type": "user", "message": {"role": "user", "content": content}}


def _assistant(content):
    return {"type": "assistant", "message": {"role": "assistant", "content": content}}


def _bash_session(output_lines):
    return _claude_code(
        _user("run the test suite for the palace and tell me what failed"),
        _assistant(
            [
                {"type": "text", "text": "Running the suite now."},
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "pytest"}},
            ]
        ),
        _user(
            [
                {
                    "type": "tool_result",
                    "tool_use_id": "t1",
                    "content": "\n".join(f"line {i}" for i in range(output_lines)),
                }
            ]
        ),
        _assistant([{"type": "text", "text": "All of the palace tests passed this time."}]),
    )


def _mine(tmp_path, files):
    convo_dir = tmp_path / "convos"
    convo_dir.mkdir()
    for name, text in files.items():
        (convo_dir / name).write_text(text, encoding="utf-8")
    palace_path = str(tmp_path / "palace")
    mine_convos(str(convo_dir), palace_path, wing="test")
    col = chromadb.PersistentClient(path=palace_path).get_collection("mempalace_drawers")
    rows = col.get(include=["metadatas"])
    return {m["source_file"].rsplit("/", 1)[-1].rsplit("\\", 1)[-1]: m for m in rows["metadatas"]}


def test_truncated_tool_output_is_stamped_on_the_drawers(tmp_path):
    metas = _mine(tmp_path, {"long.jsonl": _bash_session(100), "short.jsonl": _bash_session(3)})
    assert "tool_result_truncate" in metas["long.jsonl"]["transforms"].split(",")
    assert "tool_result_truncate" not in metas["short.jsonl"].get("transforms", "").split(",")


def test_a_plain_transcript_carries_no_stamp(tmp_path):
    plain = (
        "> what does the hall index point at\n"
        "Each hall points at the drawers filed under it.\n\n"
        "> and the closets\n"
        "Closets hold the compressed index of a room.\n\n"
        "> and tunnels\n"
        "Tunnels link rooms across wings.\n"
    )
    metas = _mine(tmp_path, {"plain.txt": plain})
    assert "transforms" not in metas["plain.txt"]


def test_each_lossy_tool_step_records_its_rfc_name():
    with recording_transforms() as run:
        _format_tool_result("a\nb", "Bash")
    assert run.applied == set()

    with recording_transforms() as run:
        _format_tool_result("file body", "Read")
        _format_tool_result("\n".join(str(i) for i in range(50)), "Grep")
    assert run.applied == {"tool_result_omitted", "tool_result_truncate", "synthesized_marker"}

    with recording_transforms() as run:
        _format_tool_result("x" * 5000, "SomeTool")
    assert run.applied == {"tool_result_truncate", "synthesized_marker"}

    with recording_transforms() as run:
        _format_tool_result([{"type": "text", "text": "ok"}, {"type": "image"}], "SomeTool")
    assert run.applied == {"conversations.content_block_drop"}


def test_tool_calls_and_dropped_blocks_are_recorded():
    session = _claude_code(
        _user("look at the config file for me"),
        _assistant(
            [
                {"type": "thinking", "thinking": "the user wants the config"},
                {"type": "tool_use", "id": "r1", "name": "Read", "input": {"file_path": "a.py"}},
            ]
        ),
        _user([{"type": "tool_result", "tool_use_id": "r1", "content": "x = 1"}]),
        _assistant([{"type": "text", "text": "It sets x to one."}]),
    )
    with recording_transforms() as run:
        assert _try_claude_code_jsonl(session)
    assert run.applied == {"conversations.content_block_drop", "tool_result_omitted"}


def test_tool_input_is_recorded_only_when_the_one_liner_leaves_some_out():
    kept_whole = [
        {"name": "Bash", "input": {"command": "pytest"}},
        {"name": "Read", "input": {"file_path": "a.py", "offset": 1, "limit": 5}},
        {"name": "Grep", "input": {"pattern": "x", "path": "src"}},
        {"name": "Other", "input": {"q": "short"}},
    ]
    for block in kept_whole:
        with recording_transforms() as run:
            _format_tool_use(block)
        assert run.applied == set(), block

    left_out = [
        {"name": "Bash", "input": {"command": "pytest", "description": "run tests"}},
        {"name": "Bash", "input": {"command": "x" * 300}},
        {"name": "Edit", "input": {"file_path": "a.py", "old_string": "a", "new_string": "b"}},
        {"name": "Grep", "input": {"pattern": "x", "path": "src", "glob": "*.py"}},
        {"name": "Other", "input": {"q": "y" * 300}},
    ]
    for block in left_out:
        with recording_transforms() as run:
            _format_tool_use(block)
        assert run.applied == {"conversations.tool_input_summarize"}, block


def test_a_parser_that_gives_up_leaves_no_record():
    one_turn = _claude_code(_user("<system-reminder>\nx\n</system-reminder>\nhello there"))
    with recording_transforms() as run:
        assert _split_conversations(one_turn, "s.jsonl") == [one_turn]
    assert run.applied == set()


def test_a_tool_result_with_no_turn_before_it_is_recorded_as_omitted():
    session = _claude_code(
        _user([{"type": "tool_result", "tool_use_id": "t0", "content": "orphan output"}]),
        _user("what changed in the palace"),
        _assistant([{"type": "text", "text": "Nothing changed."}]),
    )
    with recording_transforms() as run:
        assert _try_claude_code_jsonl(session)
    assert run.applied == {"tool_result_omitted"}


def test_gemini_json_records_dropped_parts_and_roles():
    session = {
        "contents": [
            {"role": "system", "parts": [{"text": "be brief"}]},
            {"role": "user", "parts": [{"text": "what is "}, {"text": "this"}, {"inlineData": {}}]},
            {"role": "model", "parts": [{"text": "A chart."}, {"functionCall": {"name": "f"}}]},
        ]
    }
    with recording_transforms() as run:
        transcript = _try_gemini_json(session)
    assert "> what is this\n" in transcript
    assert run.applied == {"conversations.content_block_drop"}

    clean = {
        "contents": [
            {"role": "user", "parts": [{"text": "hello"}]},
            {"role": "model", "parts": [{"text": "hi"}]},
        ]
    }
    with recording_transforms() as run:
        assert _try_gemini_json(clean)
    assert run.applied == set()


def test_claude_ai_and_slack_record_what_they_leave_out():
    claude_ai = [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    with recording_transforms() as run:
        assert _try_claude_ai_json_split(claude_ai)
    assert run.applied == {"conversations.content_block_drop"}

    slack = [
        {"type": "message", "user": "U1", "text": "see the file"},
        {"type": "message", "user": "U2", "text": "", "files": [{"name": "a.png"}]},
        {"type": "message", "user": "U2", "text": "got it"},
    ]
    with recording_transforms() as run:
        assert _try_slack_json(slack)
    assert "conversations.content_block_drop" in run.applied


def test_codex_tool_output_and_reasoning_are_recorded():
    rollout = _claude_code(
        {"type": "session_meta", "payload": {}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "fix it"}},
        {"type": "response_item", "payload": {"type": "message", "content": []}},
        {"type": "response_item", "payload": {"type": "reasoning"}},
        {"type": "response_item", "payload": {"type": "function_call_output", "output": "ok"}},
        {"type": "event_msg", "payload": {"type": "agent_message", "message": "fixed"}},
    )
    with recording_transforms() as run:
        assert _try_codex_jsonl(rollout)
    assert run.applied == {"tool_result_omitted", "conversations.content_block_drop"}


def test_gemini_and_pi_record_what_they_leave_out():
    gemini = _claude_code(
        {"type": "session_metadata"},
        {"type": "user", "content": [{"text": "list the files"}]},
        {"type": "gemini", "content": [{"text": "Here."}, {"functionCall": {"name": "ls"}}]},
    )
    with recording_transforms() as run:
        assert _try_gemini_jsonl(gemini)
    assert run.applied == {"conversations.content_block_drop"}

    pi = _claude_code(
        {"type": "session", "version": 1},
        {"type": "message", "message": {"role": "user", "content": "run it"}},
        {"type": "message", "message": {"role": "toolResult", "content": "output"}},
        {"type": "message", "message": {"role": "assistant", "content": "done"}},
    )
    with recording_transforms() as run:
        assert _try_pi_jsonl(pi)
    assert run.applied == {"tool_result_omitted"}


def _chatgpt(nodes, current):
    mapping = {"r": {"id": "r", "message": None, "parent": None, "children": []}}
    for node_id, parent, role, parts in nodes:
        mapping[node_id] = {
            "id": node_id,
            "parent": parent,
            "children": [],
            "message": {"author": {"role": role}, "content": {"parts": parts}},
        }
        mapping[parent]["children"].append(node_id)
    return {"mapping": mapping, "current_node": current}


def test_chatgpt_records_dropped_parts_tool_messages_and_joined_parts():
    convo = _chatgpt(
        [
            ("u", "r", "user", ["see", "this", {"asset_pointer": "img"}]),
            ("t", "u", "tool", ["search results"]),
            ("a", "t", "assistant", ["looks fine"]),
        ],
        "a",
    )
    with recording_transforms() as run:
        assert _try_chatgpt_json(convo)
    assert run.applied == {
        "conversations.content_block_drop",
        "line_join_spaces",
        "tool_result_omitted",
    }


def test_chatgpt_keeps_no_record_from_the_branch_it_fell_back_from():
    convo = _chatgpt(
        [
            ("u1", "r", "user", ["hello"]),
            ("a1", "u1", "assistant", ["hi"]),
            ("u2", "r", "user", ["x", {"asset_pointer": "img"}]),
        ],
        "u2",
    )
    with recording_transforms() as run:
        assert "hello" in _try_chatgpt_json(convo)
    assert run.applied == set()


def test_continue_records_system_messages_tool_markers_and_orphan_output():
    session = {
        "history": [
            {"role": "system", "content": "you are helpful"},
            {"role": "tool", "content": "orphan"},
            {"role": "user", "content": "check the build"},
            {"role": "assistant", "content": "checking"},
            {"role": "tool", "content": "build ok"},
        ]
    }
    with recording_transforms() as run:
        assert "[tool] build ok" in _try_continue_json(session)
    assert run.applied == {
        "conversations.content_block_drop",
        "synthesized_marker",
        "tool_result_omitted",
    }


def test_strip_noise_records_only_when_it_removes_chrome():
    with recording_transforms() as run:
        assert strip_noise("plain words\n") == "plain words\n"
    assert run.applied == set()

    with recording_transforms() as run:
        assert strip_noise("<system-reminder>\nnote\n</system-reminder>\nkeep") == "keep"
    assert run.applied == {"strip_tool_chrome"}


def test_strip_noise_keeps_blank_lines_the_writer_typed():
    typed = "first paragraph\n\n\n\n\nafter a long gap"
    with recording_transforms() as run:
        assert strip_noise(typed) == typed
    assert run.applied == set()

    with recording_transforms() as run:
        cleaned = strip_noise("<system-reminder>\nnote\n</system-reminder>\n" + typed)
    assert cleaned == "first paragraph\n\n\nafter a long gap"
    assert run.applied == {"strip_tool_chrome", "whitespace_collapse_internal"}


def test_gemini_parts_join_as_written():
    streamed = _claude_code(
        {"type": "session_metadata"},
        {"type": "user", "content": [{"text": "list the "}, {"text": "files\n\nplease"}]},
        {"type": "gemini", "content": [{"text": "Here th"}, {"text": "ey are."}]},
    )
    with recording_transforms() as run:
        transcript = _try_gemini_jsonl(streamed)
    assert "> list the files\n\nplease\nHere they are.\n" in transcript
    assert run.applied == set()

    session = {
        "contents": [
            {"role": "user", "parts": [{"text": "Hel"}, {"text": "lo there"}]},
            {"role": "model", "parts": [{"text": "Hi."}]},
        ]
    }
    with recording_transforms() as run:
        transcript = _try_gemini_json(session)
    assert "> Hello there\n" in transcript
    assert run.applied == set()


def test_slack_records_assigned_roles_and_its_footer():
    export = [
        {"type": "message", "user": "U1", "text": "morning"},
        {"type": "message", "user": "U2", "text": "hello"},
    ]
    with recording_transforms() as run:
        assert _try_slack_json(export)
    assert run.applied == {"speaker_role_assignment", "synthesized_marker"}


def test_nothing_is_recorded_outside_a_recording_block():
    assert _format_tool_result("file body", "Read") == ""


def test_transforms_compose_into_one_sorted_string():
    assert format_transforms({"tool_result_truncate", "spellcheck_user"}) == (
        "spellcheck_user,tool_result_truncate"
    )
    with recording_transforms() as run:
        pass
    assert run.transforms == ""


def test_custom_names_have_reference_implementations():
    for name in ("conversations.tool_input_summarize", "conversations.content_block_drop"):
        assert getattr(transform_refs, name.replace(".", "_"))("text") == "text"
