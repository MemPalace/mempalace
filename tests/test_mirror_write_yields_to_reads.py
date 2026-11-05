"""Mirror batches admit queued reads between complete units."""

import json
import threading
import time


def _unit(seq, text):
    return {
        "seq_start": seq,
        "seq_end": seq,
        "part": 0,
        "message_class": "conversation",
        "text": text,
        "index_spans": None,
    }


def _fake_write(lock, order, u1_entered, release, *, block_u2):
    def fake_write(**kw):
        content = kw["content"]
        order.append(content)
        if content == "u1":
            u1_entered.set()
            deadline = time.monotonic() + 10
            while lock._waiting_readers < 1:
                assert time.monotonic() < deadline, "reader never queued"
                time.sleep(0.001)
        if content == "u2" and block_u2:
            assert release.wait(10), "sweep was never released"
        return {"success": True, "drawer_id": kw["drawer_id"]}

    return fake_write


def _fake_handle(order, reader_done):
    def fake_handle(request):
        order.append("read")
        reader_done.set()
        return {"jsonrpc": "2.0", "id": "r", "result": {}}

    return fake_handle


def _install(monkeypatch, mcp, lock, fake_write, fake_handle, preflight=lambda *a, **k: None):
    monkeypatch.setattr(mcp, "_HTTP_REQUEST_LOCK", lock)
    monkeypatch.setattr(mcp._provenance, "current", lambda: object())
    monkeypatch.setattr(mcp, "_mcp_tool_preflight_refusal", preflight)
    monkeypatch.setattr(mcp, "_write_drawer", fake_write)
    monkeypatch.setattr(mcp, "handle_request", fake_handle)


def _sweep(mcp, result, errors, units=None):
    try:
        result["response"] = mcp._http_dispatch(
            {
                "jsonrpc": "2.0",
                "id": "w",
                "method": "internal/write_mirror_units",
                "params": {
                    "thread_id": "t-synth",
                    "units": units or [_unit(1, "u1"), _unit(2, "u2"), _unit(3, "u3")],
                },
            }
        )
    except BaseException as exc:  # surfaced on the main thread
        errors.append(exc)


def _reader(mcp, errors):
    try:
        mcp._http_dispatch(
            {
                "jsonrpc": "2.0",
                "id": "r",
                "method": "tools/call",
                "params": {"name": "mempalace_get_drawer", "arguments": {"drawer_id": "x"}},
            }
        )
    except BaseException as exc:  # surfaced on the main thread
        errors.append(exc)


def _payload(result):
    return json.loads(result["response"]["result"]["content"][0]["text"])


def test_startup_read_runs_between_mirror_units_not_after_batch(monkeypatch):
    from mempalace import mcp_server as mcp

    lock = mcp._RWLock()
    order, errors, result = [], [], {}
    u1_entered, reader_done, release = threading.Event(), threading.Event(), threading.Event()
    _install(
        monkeypatch,
        mcp,
        lock,
        _fake_write(lock, order, u1_entered, release, block_u2=True),
        _fake_handle(order, reader_done),
    )
    sweep = threading.Thread(target=_sweep, args=(mcp, result, errors), daemon=True)
    reader = threading.Thread(target=_reader, args=(mcp, errors), daemon=True)
    sweep.start()
    try:
        assert u1_entered.wait(10), "sweep never reached unit 1"
        reader.start()
        assert reader_done.wait(5), "startup read waited for the whole mirror batch"
        assert order[:2] == ["u1", "read"]
    finally:
        release.set()
        sweep.join(10)
        reader.join(10)
    assert not errors, errors
    assert order == ["u1", "read", "u2", "u3"]
    assert len(_payload(result)["drawer_ids"]) == 3


def test_mirror_write_refuses_after_yield_when_preflight_changes(monkeypatch):
    from mempalace import mcp_server as mcp

    lock = mcp._RWLock()
    order, errors, result = [], [], {}
    u1_entered, reader_done, release = threading.Event(), threading.Event(), threading.Event()
    calls = []

    def preflight(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            return None
        return {
            "jsonrpc": "2.0",
            "id": None,
            "error": {
                "code": -32003,
                "message": "Server is in read-only mode; this tool is disabled",
            },
        }

    _install(
        monkeypatch,
        mcp,
        lock,
        _fake_write(lock, order, u1_entered, release, block_u2=False),
        _fake_handle(order, reader_done),
        preflight=preflight,
    )
    sweep = threading.Thread(target=_sweep, args=(mcp, result, errors), daemon=True)
    reader = threading.Thread(target=_reader, args=(mcp, errors), daemon=True)
    sweep.start()
    assert u1_entered.wait(10), "sweep never reached unit 1"
    reader.start()
    sweep.join(10)
    reader.join(10)
    assert not errors, errors
    payload = _payload(result)
    assert payload["success"] is False
    assert order == ["u1", "read"]
    assert "u2" not in json.dumps(payload) and "u3" not in json.dumps(payload)

    # A refused range is retried by the next trigger; already-written ids replay.
    from mempalace.ids import make_mirror_unit_id

    first_id = make_mirror_unit_id("t-synth", 1, 1, "conversation", "u1")
    written = {first_id}
    new_writes = []

    def replay(**kw):
        if kw["drawer_id"] not in written:
            written.add(kw["drawer_id"])
            new_writes.append(kw["content"])
        return {"success": True, "drawer_id": kw["drawer_id"]}

    monkeypatch.setattr(mcp, "_write_drawer", replay)
    monkeypatch.setattr(mcp, "_mcp_tool_preflight_refusal", lambda *a, **k: None)
    retry = {}
    _sweep(mcp, retry, errors)
    assert not errors
    assert _payload(retry)["drawer_ids"][0] == first_id
    assert new_writes == ["u2", "u3"]


def test_every_unit_completes_with_a_reader_at_each_boundary(monkeypatch):
    from mempalace import mcp_server as mcp

    lock = mcp._RWLock()
    order, errors, result, readers = [], [], {}, []
    units = [_unit(i, f"u{i}") for i in range(1, 65)]

    def fake_write(**kw):
        order.append(kw["content"])
        n = int(kw["content"][1:])
        if n < 64:
            thread = threading.Thread(target=_reader, args=(mcp, errors), daemon=True)
            readers.append(thread)
            thread.start()
            deadline = time.monotonic() + 10
            while lock._waiting_readers < 1:
                assert time.monotonic() < deadline, "reader never queued (setup)"
                time.sleep(0.001)
        return {"success": True, "drawer_id": kw["drawer_id"]}

    def fake_handle(request):
        order.append("read")
        return {"jsonrpc": "2.0", "id": "r", "result": {}}

    monkeypatch.setattr(mcp, "_HTTP_REQUEST_LOCK", lock)
    monkeypatch.setattr(mcp._provenance, "current", lambda: object())
    monkeypatch.setattr(mcp, "_mcp_tool_preflight_refusal", lambda *a, **k: None)
    monkeypatch.setattr(mcp, "_write_drawer", fake_write)
    monkeypatch.setattr(mcp, "handle_request", fake_handle)

    sweep = threading.Thread(target=_sweep, args=(mcp, result, errors, units), daemon=True)
    sweep.start()
    sweep.join(30)
    for thread in readers:
        thread.join(10)
    assert not errors, errors
    from mempalace.ids import make_mirror_unit_id

    assert _payload(result)["drawer_ids"] == [
        make_mirror_unit_id("t-synth", i, i, "conversation", f"u{i}") for i in range(1, 65)
    ]
    expected = []
    for i in range(1, 65):
        expected.append(f"u{i}")
        if i < 64:
            expected.append("read")
    assert order == expected
