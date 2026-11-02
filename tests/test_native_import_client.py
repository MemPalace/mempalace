"""Small-budget, synthetic checks for the standalone native import example."""

import hashlib
import importlib.util
import io
import json
from pathlib import Path

import pytest


@pytest.fixture
def client():
    spec = importlib.util.spec_from_file_location(
        "native_import_example", Path(__file__).parents[1] / "examples" / "import_memory.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def record(text="exact", ending="\n", *, codex=False):
    value = (
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            },
        }
        if codex
        else {"type": "user", "message": {"role": "user", "content": text}}
    )
    return (json.dumps(value, ensure_ascii=False) + ending).encode()


def assert_plan(client, raw, source="claude-code", identity='session-é-"\\', budget=900):
    stream = io.BytesIO(raw)
    plan = client.native_plan(stream, source, identity, budget=budget, block_size=1)
    assert not plan["legacy"] and len(plan["parts"]) >= 3
    position = 0
    prefix = hashlib.sha256()
    pieces = []
    for span in plan["parts"]:
        assert span["offset"] == position
        body = client.planned_body(stream, plan, span)
        assert len(body) <= budget
        item = json.loads(body)["threads"][0]
        assert item["thread_id"] == identity
        chunk = item["transcript"].encode()
        pieces.append(chunk)
        assert len(body) == len(
            client.serialize(source, identity, plan["format"], "", span["part"])
        ) + client.escaped_size(item["transcript"])
        assert span["part"]["prefix_sha256"] == prefix.hexdigest()
        assert span["sha256"] == hashlib.sha256(chunk).hexdigest()
        prefix.update(chunk)
        position += len(chunk)
        assert span["part"]["final"] == (position == len(raw))
    assert b"".join(pieces) == raw
    assert position == plan["session_bytes"] == len(raw)
    assert prefix.hexdigest() == plan["session_sha256"] == hashlib.sha256(raw).hexdigest()
    boundaries = set()
    position = 0
    for line in client.binary_records(io.BytesIO(raw), budget, block_size=1):
        position += len(line)
        boundaries.add(position)
    assert all(span["offset"] + span["size"] in boundaries for span in plan["parts"])
    return plan


@pytest.mark.parametrize("source", ["claude-code", "codex", "t3"])
def test_native_plan_bytes_records_unicode_and_repeats(client, source):
    typed = record('é 🦉\u2028\u2029\u0085 "\\' * 4, "\r\n", codex=source != "claude-code")
    # Unknown-only/blank/nonobject runs span parts, repeated records stay repeated.
    raw = (
        b" \r\n"
        + b"null\r" * 55
        + typed * 12
        + b'{"unknown": 2}\n' * 30
        + record("last", "", codex=source != "claude-code")
    )
    plan = assert_plan(client, raw, source)
    assert plan["format"] == ("claude-code" if source == "claude-code" else "codex")
    joined = b"".join(raw[p["offset"] : p["offset"] + p["size"]] for p in plan["parts"])
    assert joined.count(typed) == 12 and joined.endswith(b"}")
    assert client.MAX_IMPORT_BYTES == 64 * 1024 * 1024


@pytest.mark.parametrize("block", [1, 2, 3, 7, 64])
def test_native_scanner_mixed_terminators_and_unicode(client, block):
    lines = [b"\r\n", record("é 🦉\u2028\u2029\u0085", "\r"), b"null\n", record("last", "")]
    assert list(client.binary_records(io.BytesIO(b"".join(lines)), 1000, block)) == lines


def test_native_escape_budget_exact_equality_and_one_byte_under(client):
    raw = record('"\\' * 40)
    # Legacy equality remains accepted at its exact serialized byte bound.
    legacy_limit = len(client.serialize("claude-code", "id", "auto", raw.decode()))
    assert client.native_plan(io.BytesIO(raw), "claude-code", "id", budget=legacy_limit)["legacy"]
    with pytest.raises(ValueError, match="record_too_large"):
        client.native_plan(io.BytesIO(raw), "claude-code", "id", budget=legacy_limit - 1)
    # Multipart equality includes real counters/identity/hash/final flag overhead.
    repeated = raw * 20
    initial = client.native_plan(io.BytesIO(repeated), "claude-code", "id", budget=850)
    spans = initial["parts"]
    assert len(spans) > 1
    assert all(p["size"] == len(raw) for p in spans)
    # Later index/offset digits are part of the *actual* minimum envelope.
    exact = max(len(client.planned_body(io.BytesIO(repeated), initial, p)) for p in spans)
    plan = client.native_plan(io.BytesIO(repeated), "claude-code", "id", budget=exact)
    assert (
        max(len(client.planned_body(io.BytesIO(repeated), plan, p)) for p in plan["parts"]) == exact
    )
    with pytest.raises(ValueError, match="record_too_large"):
        client.native_plan(io.BytesIO(repeated), "claude-code", "id", budget=exact - 1)
    assert_plan(client, record('"\\\t' * 22) * 50, identity='"\\é' * 60, budget=1700)


@pytest.mark.parametrize(
    "bad", [b'{"type":', b'{"private secret": nope}\n', b'{"bad": nope}', b'"\xff"\n']
)
def test_native_preflight_bad_late_record_before_any_http(client, bad):
    calls = []
    with pytest.raises((ValueError, UnicodeError)):
        client.upload_native(
            io.BytesIO(record() * 20 + bad), "claude-code", "id", "auto", calls.append, budget=800
        )
    assert calls == []


def test_native_oversized_record_is_bounded_and_before_http(client):
    class BoundedRead(io.BytesIO):
        def read(self, count=-1):
            assert 0 <= count <= 801
            return super().read(count)

    calls = []
    with pytest.raises(ValueError, match="record_too_large"):
        client.upload_native(
            BoundedRead(record() * 10 + record("x" * 5000)),
            "claude-code",
            "id",
            "auto",
            calls.append,
            budget=800,
        )
    assert calls == []
    with pytest.raises(ValueError, match="record_too_large"):
        client.upload_native(
            io.BytesIO(record('"\\' * 170)), "claude-code", "id", "auto", calls.append, budget=800
        )
    assert calls == []


def test_native_fitting_live_tail_uses_exact_legacy_body(client):
    raw = record() + b'{"type":'
    stream = io.BytesIO(raw)
    plan = client.native_plan(stream, "claude-code", "original")
    assert plan["legacy"]
    assert json.loads(client.planned_body(stream, plan, plan["parts"][0])) == {
        "source": "claude-code",
        "threads": [{"thread_id": "original", "format": "auto", "transcript": raw.decode()}],
    }


def test_native_source_same_size_mutation_after_and_during_preflight(client):
    raw = record("AAAA") * 20
    stream = io.BytesIO(raw)
    plan = client.native_plan(stream, "claude-code", "id", budget=800)
    stream.seek(0)
    stream.write(raw.replace(b"AAAA", b"BBBB"))
    assert len(stream.getvalue()) == len(raw)
    with pytest.raises(ValueError, match="Source changed after preflight"):
        client.planned_body(stream, plan, plan["parts"][0])

    class MutatesBetweenPasses(io.BytesIO):
        seeks = 0

        def seek(self, *args):
            self.seeks += 1
            if self.seeks == 2:
                super().seek(0)
                self.write(raw.replace(b"AAAA", b"BBBB"))
            return super().seek(*args)

    calls = []
    with pytest.raises(ValueError, match="Source changed during preflight"):
        client.upload_native(
            MutatesBetweenPasses(raw), "claude-code", "id", "auto", calls.append, budget=800
        )
    assert not calls


def response_for(plan, span, *, cursor_index=None):
    index = span["part"]["index"] if cursor_index is None else cursor_index
    boundary = plan["parts"][index]
    offset = boundary["offset"] + boundary["size"]
    prefix = (
        plan["session_sha256"]
        if index + 1 == len(plan["parts"])
        else plan["parts"][index + 1]["part"]["prefix_sha256"]
    )
    return dict(
        source=plan["source"],
        errors=0,
        results=[
            dict(
                status="imported",
                unit="part",
                thread_id=plan["thread_id"],
                format=plan["format"],
                part=span["part"],
                stored_thread_id="thr_test",
                offset=offset,
                next_index=index + 1,
                prefix_sha256=prefix,
                session_state="complete" if offset == plan["session_bytes"] else "incomplete",
            )
        ],
    )


@pytest.mark.parametrize(
    "failure",
    [
        "old_error",
        "old_success",
        "part",
        "offset",
        "prefix_sha256",
        "session_state",
        "next_index",
        "thread_id",
        "stored_thread_id",
    ],
)
def test_native_refuses_old_server_and_bad_ack_without_fallback(client, failure):
    import copy

    raw = record() * 30
    plan = client.native_plan(io.BytesIO(raw), "claude-code", "original", budget=800)
    span = plan["parts"][0]
    report = copy.deepcopy(response_for(plan, span))
    if failure == "old_error":
        report = dict(errors=1, results=[dict(status="error", error="invalid request body")])
    elif failure == "old_success":
        report = dict(source="claude-code", errors=0, results=[dict(status="imported")])
    elif failure == "part":
        report["results"][0]["part"]["sha256"] = "0" * 64
    else:
        report["results"][0][failure] = None
    calls = []

    def send(body):
        calls.append(json.loads(body))
        return report

    with pytest.raises(ValueError, match="not acknowledged"):
        client.upload_native(io.BytesIO(raw), "claude-code", "original", "auto", send, budget=800)
    assert len(calls) == 1
    assert calls[0]["threads"][0]["thread_id"] == "original"
    assert calls[0]["threads"][0]["part"] == span["part"]


def test_native_stale_duplicate_progress_counts_unique_ranges(client, capsys):
    raw = record() * 30
    plan = client.native_plan(io.BytesIO(raw), "claude-code", "original", budget=800)
    index = 0

    def send(body):
        nonlocal index
        assert body == client.planned_body(io.BytesIO(raw), plan, plan["parts"][index])
        report = response_for(plan, plan["parts"][index], cursor_index=len(plan["parts"]) - 1)
        report["results"][0]["status"] = "duplicate"
        index += 1
        return report

    assert (
        client.upload_native(io.BytesIO(raw), "claude-code", "original", "auto", send, budget=800)
        is False
    )
    rows = [json.loads(line)["progress"] for line in capsys.readouterr().out.splitlines()]
    assert len(rows) == len(plan["parts"])
    assert [row["acknowledged_bytes"] for row in rows] == [
        p["offset"] + p["size"] for p in plan["parts"]
    ]
    assert rows[-1]["acknowledged_bytes"] == len(raw)


def test_native_ack_rejects_bool_counters_and_extra_keys(client):
    import copy

    raw = record() * 30
    plan = client.native_plan(io.BytesIO(raw), "claude-code", "id", budget=800)
    first = plan["parts"][0]
    for key, value in (("version", True), ("index", False), ("extra", 0)):
        report = copy.deepcopy(response_for(plan, first))
        report["results"][0]["part"][key] = value
        with pytest.raises(ValueError, match="not acknowledged"):
            client.acknowledge(report, plan, first)
