"""The convo miner's verified append cursor (issue #3057, miner part).

A re-mine of a grown Claude Code transcript reads from the cursor's resume
point instead of the whole file, and must file exactly the drawers a full
pass files.
"""

import json
import os
import sqlite3

import pytest

from mempalace import convo_cursor, convo_miner, palace
from mempalace.convo_miner import mine_convos
from mempalace.normalize import _try_claude_code_jsonl, recording_transforms
from mempalace.palace.source_state import SourceStateStore, source_stream
from mempalace.transcript_cursor import Cursor

WING = "cursor_test"
# Per-source stamps a re-mine keeps on unchanged drawers (see writes.py).
STAMPS = {"filed_at", "authored_at", "source_mtime", "source_fingerprint", "chunk_total"}

TOOLS = ["Bash", "Grep", "Read", "Agent"]


def _records(n_turns: int, *, seed: int = 0) -> list:
    """A deterministic Claude Code session: prompts, replies, tool loops, noise."""
    records = []
    for turn in range(n_turns):
        ts = f"2026-03-01T{(turn // 60) % 24:02d}:{turn % 60:02d}:00Z"
        short = turn % 4 == 1
        prompt = (
            "ok"
            if short
            else f"Question {turn} from seed {seed}: how should the parser handle case {turn * 7}?"
        )
        records.append(
            {"type": "user", "timestamp": ts, "message": {"role": "user", "content": prompt}}
        )
        tool = TOOLS[turn % len(TOOLS)]
        tool_id = f"toolu_{seed}_{turn}"
        records.append(
            {
                "type": "assistant",
                "timestamp": ts,
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": f"Looking into case {turn}. " * (1 + turn % 3)},
                        {
                            "type": "tool_use",
                            "id": tool_id,
                            "name": tool,
                            "input": {
                                "command": f"echo {turn}",
                                "pattern": "x",
                                "file_path": "/tmp/f",
                            },
                        },
                    ],
                },
            }
        )
        records.append(
            {
                "type": "user",
                "timestamp": ts,
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_id,
                            "content": "\n".join(
                                f"line {k} of {turn}" for k in range(turn % 5 + 1)
                            ),
                        }
                    ],
                },
            }
        )
        if turn % 6 == 3:
            records.append({"type": "system", "content": "bookkeeping", "timestamp": ts})
        records.append(
            {
                "type": "assistant",
                "timestamp": ts,
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": f"Answer {turn}: the parser keeps every word. "
                            * (2 + turn % 9),
                        }
                    ],
                },
            }
        )
    return records


def _lines(records: list) -> list:
    return [(json.dumps(r) + "\n").encode("utf-8") for r in records]


def _drawers(palace_path: str, source_file) -> dict:
    source_file = str(os.path.realpath(source_file))
    collection = palace.get_collection(palace_path)
    got = collection.get(where={"source_file": source_file}, include=["documents", "metadatas"])
    return {
        drawer_id: (doc, meta)
        for drawer_id, doc, meta in zip(got["ids"], got["documents"], got["metadatas"])
        if meta.get("ingest_mode") == "convos"
    }


def _strip_stamps(drawers: dict) -> dict:
    return {
        drawer_id: (doc, {k: v for k, v in meta.items() if k not in STAMPS})
        for drawer_id, (doc, meta) in drawers.items()
    }


def _fresh_full(tmp_path, data: bytes, name: str = "session.jsonl"):
    """Drawers of a first mine of ``data`` into a new palace."""
    folder = tmp_path / "fresh"
    folder.mkdir(exist_ok=True)
    source = folder / name
    source.write_bytes(data)
    palace_path = str(tmp_path / "fresh_palace")
    mine_convos(str(source), palace_path, wing=WING)
    return _drawers(palace_path, str(source.resolve()))


def _rekey(drawers: dict, old_source: str, new_source: str) -> dict:
    """Map another palace's drawers onto ``new_source``'s ids and metadata."""
    from mempalace.ids import make_convo_drawer_id

    out = {}
    for _, (doc, meta) in drawers.items():
        meta = dict(meta)
        assert meta["source_file"] == old_source
        meta["source_file"] = new_source
        meta.pop("source_dir_ino", None)
        drawer_id = make_convo_drawer_id(
            meta["wing"], meta["room"], new_source, meta["extract_mode"], meta["chunk_index"]
        )
        out[drawer_id] = (doc, meta)
    return out


# What differs between two files of the same bytes: their snapshot, their
# directory, and the backend's own write time.
PER_FILE = {"source_fingerprint", "source_mtime", "source_dir_ino", "last_modified"}


def _per_file_aside(drawers: dict) -> dict:
    return {
        drawer_id: (doc, {k: v for k, v in meta.items() if k not in PER_FILE})
        for drawer_id, (doc, meta) in drawers.items()
    }


def _comparable(drawers: dict) -> dict:
    return _per_file_aside(_strip_stamps(drawers))


@pytest.fixture
def scans(monkeypatch):
    """Every TranscriptScan the miner makes, in order."""
    seen = []
    real = convo_cursor.scan_transcript

    def spy(*args, **kwargs):
        scan = real(*args, **kwargs)
        seen.append(scan)
        return scan

    monkeypatch.setattr(convo_cursor, "scan_transcript", spy)
    return seen


@pytest.fixture
def clock(monkeypatch):
    """A filed_at per mine, so two palaces mined in step stamp the same times."""
    tick = {"n": 0}
    monkeypatch.setattr(
        convo_miner, "utc_now_iso", lambda: f"2026-04-01T00:00:{tick['n']:02d}+00:00"
    )
    return tick


def _record(palace_path: str, source):
    source = str(os.path.realpath(source))
    store = SourceStateStore(palace_path)
    return store.get(source_stream(source, extract_mode="exchange", wing=WING))


def _cursor(palace_path: str, source: str) -> Cursor:
    head = _record(palace_path, source)
    return Cursor.from_json(json.loads(head.cursor))


def _assert_same_as_fresh(tmp_path, palace_path, source):
    data = open(source, "rb").read()
    source = os.path.realpath(source)
    fresh_source = os.path.realpath(tmp_path / "fresh" / os.path.basename(source))
    fresh = _rekey(_fresh_full(tmp_path, data, os.path.basename(source)), fresh_source, source)
    got = _drawers(palace_path, source)
    assert len(got) == len(fresh)
    assert _comparable(got) == _comparable(fresh)


# --- incremental equals full --------------------------------------------------


def test_an_incremental_mine_files_exactly_the_drawers_of_a_full_mine(
    tmp_path, scans, clock, monkeypatch
):
    lines = _lines(_records(40))
    lines.insert(30, b"\x00\x00\x00\n")
    lines.insert(55, b'{"type": "user", "message": \n')
    data = b"".join(lines)
    # Cut points: mid-exchange (a prompt in one append, its reply in the next)
    # and one in the middle of a line.
    cuts = [len(b"".join(lines[:60])), len(b"".join(lines[:97])), len(b"".join(lines[:98])) + 17]
    cuts += [len(b"".join(lines[:130])), len(data)]

    folder = tmp_path / "convos"
    folder.mkdir()
    source = folder / "session.jsonl"
    plain_folder = tmp_path / "plain"
    plain_folder.mkdir()
    plain = plain_folder / "session.jsonl"
    palace_path = str(tmp_path / "palace")
    plain_palace = str(tmp_path / "plain_palace")

    # The same appends mined with the cursor ignored: a full pass each time.
    real_stored = convo_miner._stored_cursor
    resumed = []
    for step, cut in enumerate(cuts):
        clock["n"] = step
        source.write_bytes(data[:cut])
        plain.write_bytes(data[:cut])
        mine_convos(str(source), palace_path, wing=WING)
        resumed.append(any(scan.resumed for scan in scans))
        scans.clear()
        monkeypatch.setattr(convo_miner, "_stored_cursor", lambda *a: None)
        mine_convos(str(plain), plain_palace, wing=WING)
        monkeypatch.setattr(convo_miner, "_stored_cursor", real_stored)
        assert not any(scan.resumed for scan in scans)
        scans.clear()

        src, plain_src = str(source.resolve()), str(plain.resolve())
        got = _drawers(palace_path, src)
        expected = _rekey(_drawers(plain_palace, plain_src), plain_src, src)
        assert _per_file_aside(got) == _per_file_aside(expected), f"append {step}"

    assert resumed[0] is False
    assert all(resumed[1:]), resumed
    _assert_same_as_fresh(tmp_path, palace_path, str(source.resolve()))


def test_the_cursor_is_stored_with_the_complete_mark(tmp_path):
    source = tmp_path / "session.jsonl"
    source.write_bytes(b"".join(_lines(_records(30))))
    palace_path = str(tmp_path / "palace")
    mine_convos(str(source), palace_path, wing=WING)

    head = _record(palace_path, str(source))
    assert head.complete
    cursor = _cursor(palace_path, str(source))
    assert cursor.offset == source.stat().st_size
    state = cursor.state
    assert 0 < state["resume_offset"] < cursor.offset
    assert 0 < state["next_chunk_index"] < head.chunk_total
    assert state["mode"] == "exchange"
    # The resume point is the start of a user line.
    data = source.read_bytes()
    assert data[state["resume_offset"] - 1 : state["resume_offset"]] == b"\n"
    assert json.loads(data[state["resume_offset"] :].split(b"\n", 1)[0])["type"] == "user"


# --- in-place rewrite, truncate, replace --------------------------------------


def test_a_tail_rewritten_in_place_with_the_same_inode_and_size_runs_a_full_pass(tmp_path, scans):
    source = tmp_path / "session.jsonl"
    data = b"".join(_lines(_records(30)))
    source.write_bytes(data)
    palace_path = str(tmp_path / "palace")
    mine_convos(str(source), palace_path, wing=WING)
    before = os.stat(source)

    # Rewrite the last reply's words in place: same inode, same size.
    pos = data.rindex(b"keeps every word")
    with open(source, "r+b") as f:
        f.seek(pos)
        f.write(b"drops every word")
    after = os.stat(source)
    assert (after.st_ino, after.st_size) == (before.st_ino, before.st_size)

    scans.clear()
    mine_convos(str(source), palace_path, wing=WING)
    assert [scan.reason for scan in scans] == ["anchor"]
    assert not scans[0].resumed
    _assert_same_as_fresh(tmp_path, palace_path, str(source))
    assert any("drops every word" in doc for doc, _ in _drawers(palace_path, str(source)).values())


def test_a_rewrite_inside_the_resumed_span_but_outside_the_anchor_fails_the_digest(tmp_path, scans):
    source = tmp_path / "session.jsonl"
    records = _records(30)
    # A last exchange longer than the anchor, so its start is outside it.
    records[-1]["message"]["content"][0]["text"] = "long reply " * 2000
    data = b"".join(_lines(records))
    source.write_bytes(data)
    palace_path = str(tmp_path / "palace")
    mine_convos(str(source), palace_path, wing=WING)
    cursor = _cursor(palace_path, str(source))
    resume = cursor.state["resume_offset"]
    assert cursor.offset - resume > cursor.anchor_len

    target = data.index(b"Looking into case", resume)
    assert target < cursor.offset - cursor.anchor_len
    with open(source, "r+b") as f:
        f.seek(target)
        f.write(b"Peeking")
    scans.clear()
    mine_convos(str(source), palace_path, wing=WING)
    assert [scan.reason for scan in scans] == ["digest"]
    _assert_same_as_fresh(tmp_path, palace_path, str(source))


@pytest.mark.parametrize("how", ["truncate", "replace"])
def test_a_truncated_or_replaced_file_runs_a_full_pass(tmp_path, scans, how):
    source = tmp_path / "session.jsonl"
    lines = _lines(_records(30))
    source.write_bytes(b"".join(lines))
    palace_path = str(tmp_path / "palace")
    mine_convos(str(source), palace_path, wing=WING)

    if how == "truncate":
        with open(source, "r+b") as f:
            f.truncate(len(b"".join(lines[:70])))
        expected_reason = "shrunk"
    else:
        other = tmp_path / "other.jsonl"
        other.write_bytes(b"".join(_lines(_records(35, seed=1))))
        os.replace(other, source)
        expected_reason = "anchor"

    scans.clear()
    mine_convos(str(source), palace_path, wing=WING)
    assert [scan.reason for scan in scans] == [expected_reason]
    _assert_same_as_fresh(tmp_path, palace_path, str(source))


# --- parsing ------------------------------------------------------------------


def test_lone_surrogates_parse_through_the_stdlib_fallback_and_bad_lines_are_skipped(
    tmp_path,
):
    records = _records(6)
    lines = _lines(records)
    surrogate = b'{"type": "user", "message": {"role": "user", "content": "odd \\ud800 text"}}\n'
    lines[4:4] = [surrogate, b"\x00\x00\x00\x00\n", b'{"type": "assistant", "mess\n', b"not json\n"]
    data = b"".join(lines)
    source = tmp_path / "s.jsonl"
    source.write_bytes(data)

    with recording_transforms() as run:
        scan = convo_cursor.scan_transcript(source, None, run=run, source_metadata={})
    assert scan is not None
    texts = [text for _, text in scan.messages]
    assert "odd \ud800 text" in texts

    from mempalace.normalize import _messages_to_transcript

    with recording_transforms():
        legacy = _try_claude_code_jsonl(data.decode("utf-8"))
    with recording_transforms():
        assert _messages_to_transcript(scan.messages) == legacy


def test_nul_and_malformed_lines_do_not_stop_the_mine(tmp_path, scans):
    lines = _lines(_records(30))
    lines[10:10] = [b"\x00" * 50 + b"\n", b'{"type": "user", "message": {"content": "cut\n']
    source = tmp_path / "session.jsonl"
    source.write_bytes(b"".join(lines))
    palace_path = str(tmp_path / "palace")
    mine_convos(str(source), palace_path, wing=WING)
    with open(source, "ab") as f:
        f.write(b"\x00\x00\n{bad json\n")
        f.write(b"".join(_lines(_records(3, seed=5))))
    scans.clear()
    mine_convos(str(source), palace_path, wing=WING)
    assert scans[-1].resumed
    _assert_same_as_fresh(tmp_path, palace_path, str(source))


# --- crash safety -------------------------------------------------------------


def test_a_crash_between_the_drawer_write_and_the_cursor_advance_redoes_the_work(
    tmp_path, scans, monkeypatch
):
    source = tmp_path / "session.jsonl"
    lines = _lines(_records(40))
    source.write_bytes(b"".join(lines[:100]))
    palace_path = str(tmp_path / "palace")
    mine_convos(str(source), palace_path, wing=WING)
    old = _record(palace_path, str(source))

    with open(source, "ab") as f:
        f.write(b"".join(lines[100:]))
    real = SourceStateStore.mark_complete

    def crash(self, stream, **fields):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(SourceStateStore, "mark_complete", crash)
    scans.clear()
    with pytest.raises(BaseException):
        mine_convos(str(source), palace_path, wing=WING)
    assert scans[-1].resumed
    monkeypatch.setattr(SourceStateStore, "mark_complete", real)

    # The record is pending and still holds the old cursor, never the new one.
    crashed = _record(palace_path, str(source))
    assert not crashed.complete
    assert crashed.cursor == old.cursor

    scans.clear()
    mine_convos(str(source), palace_path, wing=WING)
    assert not scans[-1].resumed
    head = _record(palace_path, str(source))
    assert head.complete and head.cursor != old.cursor
    got = _drawers(palace_path, str(source))
    assert len(got) == head.chunk_total
    assert sorted(m["chunk_index"] for _, m in got.values()) == list(range(head.chunk_total))
    _assert_same_as_fresh(tmp_path, palace_path, str(source))


# --- version bump -------------------------------------------------------------


def test_a_chunker_version_bump_runs_a_full_pass(tmp_path, scans, monkeypatch):
    source = tmp_path / "session.jsonl"
    lines = _lines(_records(30))
    source.write_bytes(b"".join(lines[:90]))
    palace_path = str(tmp_path / "palace")
    mine_convos(str(source), palace_path, wing=WING)
    with open(source, "ab") as f:
        f.write(b"".join(lines[90:]))

    key, version = palace.CHUNKER_VERSIONS["exchange"]
    monkeypatch.setitem(palace.CHUNKER_VERSIONS, "exchange", (key, version + 1))
    scans.clear()
    mine_convos(str(source), palace_path, wing=WING)
    assert scans and not any(scan.resumed for scan in scans)
    drawers = _drawers(palace_path, str(source))
    assert {meta[key] for _, meta in drawers.values()} == {version + 1}
    assert f":c{version + 1}:" in _cursor(palace_path, str(source)).recipe


def test_a_cursor_from_another_recipe_is_not_resumed(tmp_path, scans, monkeypatch):
    source = tmp_path / "session.jsonl"
    lines = _lines(_records(30))
    source.write_bytes(b"".join(lines[:90]))
    palace_path = str(tmp_path / "palace")
    mine_convos(str(source), palace_path, wing=WING)
    with open(source, "ab") as f:
        f.write(b"".join(lines[90:]))
    monkeypatch.setattr(
        convo_miner, "_cursor_recipe", lambda *a: "convo:exchange:another-normalizer"
    )
    scans.clear()
    mine_convos(str(source), palace_path, wing=WING)
    assert scans and not any(scan.resumed for scan in scans)
    _assert_same_as_fresh(tmp_path, palace_path, str(source))


# --- cost ---------------------------------------------------------------------


def test_an_append_only_re_mine_reads_only_from_the_resume_point(tmp_path, monkeypatch):
    source = tmp_path / "session.jsonl"
    source.write_bytes(b"".join(_lines(_records(400))))
    palace_path = str(tmp_path / "palace")
    mine_convos(str(source), palace_path, wing=WING)
    cursor = _cursor(palace_path, str(source))
    reread = cursor.offset - cursor.state["resume_offset"]

    appended = b"".join(_lines(_records(2, seed=9)))
    with open(source, "ab") as f:
        f.write(appended)
    size = source.stat().st_size

    read = {"bytes": 0}
    real_open = convo_cursor._open_source

    class Counting:
        def __init__(self, f):
            self._f = f

        def read(self, n=-1):
            data = self._f.read(n)
            read["bytes"] += len(data)
            return data

        def __getattr__(self, name):
            return getattr(self._f, name)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._f.close()

    def counting_open(path):
        f, st = real_open(path)
        return Counting(f), st

    monkeypatch.setattr(convo_cursor, "_open_source", counting_open)
    mine_convos(str(source), palace_path, wing=WING)

    # The appended bytes, the open turn before them and the anchor; never
    # the whole file.
    assert read["bytes"] <= len(appended) + reread + 2 * cursor.anchor_len
    assert read["bytes"] * 20 < size
    _assert_same_as_fresh(tmp_path, palace_path, str(source))


# --- the record's cursor column -----------------------------------------------


def test_a_record_database_from_before_the_cursor_column_reads_and_gains_it(tmp_path):
    path = tmp_path / "source_state.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE source_head (source_file TEXT NOT NULL, owner TEXT NOT NULL, "
        "extract_mode TEXT NOT NULL, wing TEXT NOT NULL, state TEXT NOT NULL, "
        "chunk_total INTEGER, source_fingerprint TEXT, source_mtime REAL, "
        "normalize_version INTEGER, chunker_version INTEGER, updated_at TEXT NOT NULL, "
        "PRIMARY KEY (source_file, owner, extract_mode, wing))"
    )
    conn.execute(
        "INSERT INTO source_head VALUES ('/a.jsonl', 'convo', 'exchange', 'w', 'complete', "
        "3, '1:2:3', 1.0, 1, 1, 'now')"
    )
    conn.commit()
    conn.close()

    store = SourceStateStore(str(tmp_path))
    stream = source_stream("/a.jsonl", extract_mode="exchange", wing="w")
    head = SourceStateStore(str(tmp_path), read_only=True).get(stream)
    assert head.complete and head.chunk_total == 3 and head.cursor is None

    store.mark_complete(stream, chunk_total=4, cursor='{"format":1}')
    assert store.get(stream).cursor == '{"format":1}'
    # The pending mark keeps it; a complete mark without one clears it.
    store.mark_pending(stream, chunk_total=5)
    assert store.get(stream).cursor == '{"format":1}'
    assert not store.get(stream).complete
    store.mark_complete(stream, chunk_total=5)
    assert store.get(stream).cursor is None
