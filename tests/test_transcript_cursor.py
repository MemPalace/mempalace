"""Verified append cursor (``mempalace.transcript_cursor``) and the Stop-hook
human-message count built on it."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from mempalace import hooks_cli, transcript_cursor
from mempalace.normalize import codex_response_message
from mempalace.transcript_cursor import Cursor, advance, load_cursor, save_cursor


def _legacy_count(transcript_path: str) -> int:
    """``_count_human_messages`` as it was before the cursor, kept as the oracle.

    Validation is done by the caller; this is the parsing loop verbatim.
    """
    path = Path(transcript_path)
    count = 0
    codex_events = 0
    codex_items = 0
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    msg = entry.get("message", {})
                    if isinstance(msg, dict) and msg.get("role") == "user":
                        content = msg.get("content", "")
                        if isinstance(content, str):
                            if "<command-message>" in content:
                                continue
                        elif isinstance(content, list):
                            text = " ".join(
                                b.get("text", "") for b in content if isinstance(b, dict)
                            )
                            if "<command-message>" in text:
                                continue
                        count += 1
                    elif entry.get("type") == "event_msg":
                        payload = entry.get("payload", {})
                        if isinstance(payload, dict) and payload.get("type") == "user_message":
                            msg_text = payload.get("message", "")
                            if isinstance(msg_text, str) and "<command-message>" not in msg_text:
                                codex_events += 1
                    elif entry.get("type") == "response_item":
                        payload = entry.get("payload", {})
                        if isinstance(payload, dict):
                            turn = codex_response_message(payload)
                            if turn and turn[0] == "user":
                                codex_items += 1
                except (json.JSONDecodeError, AttributeError):
                    pass
    except OSError:
        return 0
    return count + (codex_events or codex_items)


def _j(obj) -> bytes:
    return json.dumps(obj).encode()


def _codex_item(role, text, kind):
    return _j(
        {
            "type": "response_item",
            "payload": {"type": "message", "role": role, "content": [{"type": kind, "text": text}]},
        }
    )


# Lines the legacy counter handles without raising. Each pool entry is a
# complete record without its terminator; terminators are chosen separately.
LINE_POOL = [
    _j({"message": {"role": "user", "content": "hello"}}),
    _j({"type": "user", "userType": "external", "message": {"role": "user", "content": "q"}}),
    _j({"type": "assistant", "userType": "external", "message": {"role": "assistant"}}),
    _j({"message": {"role": "user", "content": "<command-message>x</command-message>"}}),
    _j({"message": {"role": "user", "content": [{"type": "text", "text": "list"}]}}),
    _j({"message": {"role": "user", "content": [{"type": "text", "text": "<command-message>"}]}}),
    _j({"message": {"role": "user", "content": [{"type": "tool_result", "content": "\x1b[0m"}]}}),
    _j({"message": {"role": "user", "content": None}}),
    _j({"type": "event_msg", "payload": {"type": "user_message", "message": "codex"}}),
    _j({"type": "event_msg", "payload": {"type": "user_message", "message": "<command-message>"}}),
    _codex_item("user", "first", "input_text"),
    _codex_item("user", "<environment_context>cwd</environment_context>", "input_text"),
    _codex_item("assistant", "ok", "output_text"),
    b'{"message": {"r\\u006fle": "\\u0075ser", "content": "escaped"}}',
    b'{"type": "event_msg", "payload": {"type": "user\\u005fmessage", "message": "e"}}',
    b'{"message": {"role": "user", "content": "lone \\ud800 surrogate"}}',
    b'{"message": {"role": "user", "content": "nan"}, "x": NaN}',
    b'{"message": {"role": "user", "content": "big"}, "x": 1e400}',
    b'{"message": {"role": "user", "content": "int"}, "x": 123456789012345678901234567890}',
    b'{"message": {"role": "user", "content": "bad \xff\xfe utf8"}}',
    b'\xef\xbb\xbf{"message": {"role": "user", "content": "bom"}}',
    b'{"message":\r{"role": "user", "content": "lone cr between tokens"}}',
    b'{"message": {"role": "user", "content": "nul \x00"}}',
    b'{"message": {"role": "user", "content": "dup"}, "message": {"role": "assistant"}}',
    b"[1, 2, 3]",
    b'"user"',
    b"not json",
    b"",
    b"{" * 1500 + b"}" * 1500,
]
TERMINATORS = [b"\n", b"\n", b"\n", b"\r\n", b"\r"]


@pytest.fixture(params=["orjson", "stdlib"])
def parser(request, monkeypatch):
    if request.param == "stdlib":
        monkeypatch.setattr(transcript_cursor, "_orjson", None)
        monkeypatch.setattr(transcript_cursor, "_orjson_loaded", True)
    else:
        assert transcript_cursor._get_orjson() is not None
    return request.param


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    d = tmp_path / "hook_state"
    monkeypatch.setattr(hooks_cli, "STATE_DIR", d)
    monkeypatch.setattr(hooks_cli, "_palace_root_exists", lambda: True)
    return d


# --- cursor primitives -------------------------------------------------------


def _collect(path, cursor, recipe="r/1"):
    fed = []

    def feed(state, chunk):
        fed.append(chunk)
        state["bytes"] += len(chunk)

    scan = advance(path, cursor, recipe=recipe, initial_state=lambda: {"bytes": 0}, feed=feed)
    return scan, b"".join(fed)


def test_first_scan_commits_complete_lines_and_returns_partial_tail(tmp_path):
    f = tmp_path / "t.jsonl"
    f.write_bytes(b"one\ntwo\nthr")
    scan, fed = _collect(f, None)
    assert fed == b"one\ntwo\n"
    assert scan.tail == b"thr"
    assert scan.cursor.offset == 8
    assert scan.resumed is False and scan.reason == "no-cursor"


def test_append_resumes_and_feeds_only_new_bytes(tmp_path):
    f = tmp_path / "t.jsonl"
    f.write_bytes(b"one\ntwo\nthr")
    first, _ = _collect(f, None)
    with open(f, "ab") as fh:
        fh.write(b"ee\nfour\n")
    scan, fed = _collect(f, first.cursor)
    assert scan.resumed and scan.reason == "match"
    assert fed == b"three\nfour\n"
    assert scan.cursor.state == {"bytes": len(b"one\ntwo\nthree\nfour\n")}
    assert scan.tail == b""


def test_in_place_tail_rewrite_on_same_inode_is_detected(tmp_path):
    f = tmp_path / "t.jsonl"
    f.write_bytes(b"aaaa\nbbbb\n")
    first, _ = _collect(f, None)
    ino = os.stat(f).st_ino
    with open(f, "r+b") as fh:  # truncate and rewrite the tail, same size and inode
        fh.seek(5)
        fh.truncate()
        fh.write(b"cccc\nmore\n")
    assert os.stat(f).st_ino == ino
    scan, fed = _collect(f, first.cursor)
    assert scan.reason == "anchor" and not scan.resumed
    assert fed == b"aaaa\ncccc\nmore\n"


def test_rewrite_of_a_long_last_line_keeping_its_last_4k_is_detected(tmp_path):
    f = tmp_path / "t.jsonl"
    long_line = b"A" * 20000 + b"Z" * 8000 + b"\n"
    f.write_bytes(b"head\n" + long_line)
    first, _ = _collect(f, None)
    with open(f, "r+b") as fh:  # change the start of the line, keep its last 8 KiB
        fh.seek(5)
        fh.write(b"B" * 100)
    scan, _ = _collect(f, first.cursor)
    assert scan.reason == "anchor"


def test_unchanged_file_keeps_an_identical_cursor(tmp_path):
    f = tmp_path / "t.jsonl"
    f.write_bytes(b"a\n" * 3000 + b"x" * 5000 + b"\n")
    first, _ = _collect(f, None)
    again, fed = _collect(f, first.cursor)
    assert again.resumed and fed == b""
    assert again.cursor == first.cursor


def test_shrunk_file_is_rescanned(tmp_path):
    f = tmp_path / "t.jsonl"
    f.write_bytes(b"aaaa\nbbbb\n")
    first, _ = _collect(f, None)
    f.write_bytes(b"aa\n")
    scan, fed = _collect(f, first.cursor)
    assert scan.reason == "shrunk"
    assert fed == b"aa\n"


def test_cursor_from_another_recipe_is_not_resumed(tmp_path):
    f = tmp_path / "t.jsonl"
    f.write_bytes(b"aaaa\n")
    first, _ = _collect(f, None, recipe="r/1")
    scan, fed = _collect(f, first.cursor, recipe="r/2")
    assert scan.reason == "recipe"
    assert fed == b"aaaa\n"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFOs only")
def test_fifo_is_not_a_transcript(tmp_path):
    fifo = tmp_path / "t.jsonl"
    os.mkfifo(fifo)
    scan, _ = _collect(fifo, None)
    assert scan is None


def test_sidecar_round_trip_and_corruption(tmp_path):
    side = tmp_path / "cur" / "x.json"
    cur = Cursor("r/1", 10, "ab" * 32, 4, 1, 2, 12, {"count": 3})
    save_cursor(side, cur)
    assert load_cursor(side) == cur
    assert not [p for p in side.parent.iterdir() if p.name != "x.json"]  # no temp left
    for junk in [
        "",
        "{",
        "[]",
        '{"format": 1}',
        '{"format": 1, "recipe": "r", "offset": -1, "anchor_sha256": "x"}',
        '{"format": 99, "recipe": "r", "offset": 0, "anchor_sha256": "x"}',
    ]:
        side.write_text(junk)
        assert load_cursor(side) is None
    assert load_cursor(tmp_path / "missing.json") is None


def test_sidecar_name_is_stable_per_resolved_path(tmp_path):
    a = transcript_cursor.sidecar_name(tmp_path / "t.jsonl")
    assert a == transcript_cursor.sidecar_name(tmp_path / "." / "t.jsonl")
    assert a != transcript_cursor.sidecar_name(tmp_path / "u.jsonl")


def test_iter_records_splits_like_text_mode():
    chunk = b'{"a":1}\r{"b":2}\r\n{"c":3}\n'
    assert list(transcript_cursor.iter_records(chunk)) == [{"a": 1}, {"b": 2}, {"c": 3}]


# --- the Stop-hook count -----------------------------------------------------


def test_count_matches_legacy_on_every_pool_line(tmp_path, state_dir, parser):
    for i, line in enumerate(LINE_POOL):
        for term in (b"\n", b"\r\n", b""):
            f = tmp_path / f"t{i}.jsonl"
            f.write_bytes(line + term)
            assert hooks_cli._count_human_messages(str(f)) == _legacy_count(str(f)), line[:60]


def test_unterminated_last_line_counts_once(tmp_path, state_dir, parser):
    f = tmp_path / "t.jsonl"
    f.write_bytes(LINE_POOL[0] + b"\n" + LINE_POOL[0])
    assert hooks_cli._count_human_messages(str(f)) == 2
    with open(f, "ab") as fh:
        fh.write(b"\n")
    assert hooks_cli._count_human_messages(str(f)) == 2


def test_second_stop_parses_only_appended_bytes(tmp_path, state_dir, monkeypatch):
    f = tmp_path / "t.jsonl"
    f.write_bytes((LINE_POOL[0] + b"\n") * 50)
    assert hooks_cli._count_human_messages(str(f)) == 50
    seen = []
    real = hooks_cli._feed_count
    monkeypatch.setattr(hooks_cli, "_feed_count", lambda s, c: (seen.append(c), real(s, c)))
    with open(f, "ab") as fh:
        fh.write(LINE_POOL[0] + b"\n")
    assert hooks_cli._count_human_messages(str(f)) == 51
    assert seen == [LINE_POOL[0] + b"\n"]
    assert len(list((state_dir / "transcript_cursors").iterdir())) == 1


def test_lines_that_crashed_the_old_counter_are_skipped(tmp_path, state_dir):
    """Deliberate change: a non-string ``text`` block used to raise TypeError
    out of the hook; it is now skipped like any malformed line."""
    f = tmp_path / "t.jsonl"
    f.write_bytes(
        _j({"message": {"role": "user", "content": [{"type": "text", "text": 5}]}})
        + b"\n"
        + LINE_POOL[0]
        + b"\n"
    )
    assert hooks_cli._count_human_messages(str(f)) == 1


def test_nesting_too_deep_for_stdlib_json_is_skipped(tmp_path, state_dir, parser):
    """Deliberate change: 100k-deep nesting raised RecursionError out of the hook."""
    f = tmp_path / "t.jsonl"
    f.write_bytes(
        b'{"x":' + b"[" * 100000 + b"]" * 100000 + b', "role": "user"}\n' + LINE_POOL[0] + b"\n"
    )
    assert hooks_cli._count_human_messages(str(f)) == 1


def test_corrupt_sidecar_falls_back_to_full_count(tmp_path, state_dir):
    f = tmp_path / "t.jsonl"
    f.write_bytes((LINE_POOL[0] + b"\n") * 3)
    assert hooks_cli._count_human_messages(str(f)) == 3
    side = hooks_cli._cursor_sidecar(f.resolve())
    side.write_text('{"format": 1, "recipe": "human-messages/1", "offset": 0, "anchor')
    assert hooks_cli._count_human_messages(str(f)) == 3
    assert load_cursor(side) is not None


def test_sidecar_with_a_bad_count_state_is_recounted(tmp_path, state_dir):
    f = tmp_path / "t.jsonl"
    f.write_bytes((LINE_POOL[0] + b"\n") * 3)
    assert hooks_cli._count_human_messages(str(f)) == 3
    side = hooks_cli._cursor_sidecar(f.resolve())
    data = json.loads(side.read_text())
    data["state"] = {"count": 99}
    side.write_text(json.dumps(data))
    assert hooks_cli._count_human_messages(str(f)) == 3
    assert load_cursor(side).state == {"count": 3, "codex_events": 0, "codex_items": 0}


def test_cursor_failure_never_raises_into_the_hook(tmp_path, state_dir, monkeypatch):
    f = tmp_path / "t.jsonl"
    f.write_bytes(LINE_POOL[0] + b"\n")

    def boom(*a, **k):
        raise RuntimeError("bad sidecar dir")

    monkeypatch.setattr(transcript_cursor, "load_cursor", boom)
    assert hooks_cli._count_human_messages(str(f)) == 1


def test_no_sidecar_written_once_palace_is_removed(tmp_path, state_dir, monkeypatch):
    monkeypatch.setattr(hooks_cli, "_palace_root_exists", lambda: False)
    f = tmp_path / "t.jsonl"
    f.write_bytes(LINE_POOL[0] + b"\n")
    assert hooks_cli._count_human_messages(str(f)) == 1
    assert not state_dir.exists()


def test_session_end_clears_the_cursor(tmp_path, state_dir):
    f = tmp_path / "t.jsonl"
    f.write_bytes(LINE_POOL[0] + b"\n")
    hooks_cli._count_human_messages(str(f))
    side = hooks_cli._cursor_sidecar(f.resolve())
    assert side.exists()
    hooks_cli._clear_transcript_cursor(str(f))
    assert not side.exists()
    hooks_cli._clear_transcript_cursor("bad\x00path.jsonl")  # must not raise


@pytest.mark.parametrize("line", LINE_POOL)
def test_prefilter_only_drops_lines_that_cannot_count(line):
    if hooks_cli._may_count(line):
        return
    state = hooks_cli._new_count_state()
    for entry in transcript_cursor.iter_records(line):  # no prefilter
        hooks_cli._count_entry(state, entry)
    assert hooks_cli._count_total(state) == 0


_json_text = st.sampled_from(["user", "user_message", "assistant", "message", "role", "type"])
_json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | _json_text | st.text(max_size=8),
    lambda kids: st.lists(kids, max_size=3) | st.dictionaries(_json_text, kids, max_size=4),
    max_leaves=12,
)


@settings(max_examples=300, deadline=None)
@given(value=_json_values, ensure_ascii=st.booleans())
def test_prefilter_never_drops_a_counted_record(value, ensure_ascii):
    line = json.dumps(value, ensure_ascii=ensure_ascii).encode("utf-8", "surrogatepass")
    if hooks_cli._may_count(line):
        return
    state = hooks_cli._new_count_state()
    for entry in transcript_cursor.iter_records(line):
        hooks_cli._count_entry(state, entry)
    assert hooks_cli._count_total(state) == 0


# Edits applied to the transcript between Stops.
_edit = st.one_of(
    st.tuples(
        st.just("append"),
        st.lists(st.integers(0, len(LINE_POOL) - 1), max_size=6),
        st.integers(0, len(TERMINATORS) - 1),
    ),
    st.tuples(st.just("half"), st.integers(0, len(LINE_POOL) - 1), st.floats(0, 1)),
    st.tuples(st.just("truncate"), st.floats(0, 1), st.just(0)),
    st.tuples(
        st.just("rewrite"),
        st.floats(0, 1),
        st.lists(st.integers(0, len(LINE_POOL) - 1), max_size=4),
    ),
    st.tuples(
        st.just("replace"), st.lists(st.integers(0, len(LINE_POOL) - 1), max_size=6), st.just(0)
    ),
)


def _apply(path: Path, edit, rng_term):
    kind, a, b = edit
    data = path.read_bytes()
    if kind == "append":
        with open(path, "ab") as fh:
            fh.write(b"".join(LINE_POOL[i] + TERMINATORS[(i + b) % len(TERMINATORS)] for i in a))
    elif kind == "half":
        line = LINE_POOL[a] + b"\n"
        with open(path, "ab") as fh:
            fh.write(line[: int(len(line) * b)])
    elif kind == "truncate":
        with open(path, "r+b") as fh:
            fh.truncate(int(len(data) * a))
    elif kind == "rewrite":  # same inode: truncate, then write different lines
        with open(path, "r+b") as fh:
            fh.seek(int(len(data) * a))
            fh.truncate()
            fh.write(b"".join(LINE_POOL[i] + b"\n" for i in b))
    elif kind == "replace":
        tmp = path.with_suffix(".new")
        tmp.write_bytes(data[: len(data) // 2] + b"".join(LINE_POOL[i] + b"\n" for i in a))
        os.replace(tmp, path)


@pytest.mark.parametrize("anchor", [(16, 40), (4096, 1024 * 1024)])
@settings(
    max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(edits=st.lists(_edit, min_size=1, max_size=12))
def test_incremental_count_equals_full_recount(edits, anchor, parser, monkeypatch):
    monkeypatch.setattr(transcript_cursor, "ANCHOR_BYTES", anchor[0])
    monkeypatch.setattr(transcript_cursor, "MAX_ANCHOR_BYTES", anchor[1])
    monkeypatch.setattr(transcript_cursor, "READ_BLOCK", 97)  # many block boundaries
    monkeypatch.setattr(hooks_cli, "_palace_root_exists", lambda: True)
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setattr(hooks_cli, "STATE_DIR", Path(d) / "hook_state")
        path = Path(d) / "t.jsonl"
        path.write_bytes(b"")
        for edit in edits:
            _apply(path, edit, None)
            assert hooks_cli._count_human_messages(str(path)) == _legacy_count(str(path))


def test_hook_import_does_not_load_chromadb_or_numpy():
    code = (
        "import sys, mempalace.hooks_cli as h, mempalace.transcript_cursor as t\n"
        "t._get_orjson()\n"
        "print([m for m in ('chromadb', 'numpy') if m in sys.modules])"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"
