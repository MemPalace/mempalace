"""Mirror writes resume safely and watch only active unit writes."""

import threading
import time


def _unit(seq):
    return {
        "seq_start": seq,
        "seq_end": seq,
        "part": 0,
        "message_class": "conversation",
        "text": f"u{seq}",
        "index_spans": None,
    }


def test_watch_covers_each_unit_and_is_off_while_parked(monkeypatch):
    from mempalace import mcp_server as mcp

    lock = mcp._RWLock()
    order, watch_in_write, watch_in_read = [], [], []
    errors, result = [], {}
    u1_entered, reader_done, release = threading.Event(), threading.Event(), threading.Event()

    def fake_write(**kw):
        order.append(kw["content"])
        watch = mcp._write_stall_inflight
        watch_in_write.append(None if watch is None else watch["tool"])
        if kw["content"] == "u1":
            u1_entered.set()
            deadline = time.monotonic() + 10
            while lock._waiting_readers < 1:
                assert time.monotonic() < deadline, "reader never queued"
                time.sleep(0.001)
        if kw["content"] == "u2":
            assert release.wait(10), "sweep never released"
        return {"success": True, "drawer_id": kw["drawer_id"]}

    def fake_handle(request):
        order.append("read")
        watch_in_read.append(mcp._write_stall_inflight)
        reader_done.set()
        return {"jsonrpc": "2.0", "id": "r", "result": {}}

    def sweep():
        try:
            result["response"] = mcp._http_dispatch(
                {
                    "jsonrpc": "2.0",
                    "id": "w",
                    "method": "internal/write_mirror_units",
                    "params": {
                        "thread_id": "t-synth",
                        "units": [_unit(1), _unit(2), _unit(3)],
                    },
                }
            )
        except BaseException as exc:
            errors.append(exc)

    def reader():
        try:
            mcp._http_dispatch(
                {
                    "jsonrpc": "2.0",
                    "id": "r",
                    "method": "tools/call",
                    "params": {"name": "mempalace_get_drawer", "arguments": {"drawer_id": "x"}},
                }
            )
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(mcp, "_HTTP_REQUEST_LOCK", lock)
    monkeypatch.setattr(mcp._provenance, "current", lambda: object())
    monkeypatch.setattr(mcp, "_mcp_tool_preflight_refusal", lambda *a, **k: None)
    monkeypatch.setattr(mcp, "_write_drawer", fake_write)
    monkeypatch.setattr(mcp, "handle_request", fake_handle)

    sweep_t = threading.Thread(target=sweep, daemon=True)
    reader_t = threading.Thread(target=reader, daemon=True)
    sweep_t.start()
    try:
        assert u1_entered.wait(10), "sweep never reached unit 1"
        reader_t.start()
        assert reader_done.wait(5), "startup read waited for the whole mirror batch"
    finally:
        release.set()
        sweep_t.join(10)
        if reader_t.ident is not None:
            reader_t.join(10)
    assert not errors, errors
    assert order == ["u1", "read", "u2", "u3"]
    assert watch_in_write == ["mempalace_add_drawer"] * 3, watch_in_write
    assert watch_in_read == [None], watch_in_read
    assert mcp._write_stall_inflight is None


def test_sweep_completes_after_a_queued_long_writer(monkeypatch):
    import json

    from mempalace import mcp_server as mcp
    from mempalace.palace import mine_yield_hook, mine_yield_point

    lock = mcp._RWLock()
    writer_entered, release_writer = threading.Event(), threading.Event()
    errors, written, writer_units, result = [], [], [], {}

    def long_writer():
        try:
            with lock, mine_yield_hook(lock.yield_write):
                writer_entered.set()
                assert release_writer.wait(10), "writer was never released"
                for i in range(5):
                    writer_units.append(i)
                    mine_yield_point()
        except BaseException as exc:
            errors.append(exc)

    writer = threading.Thread(target=long_writer, daemon=True)

    def fake_write(**kw):
        written.append(kw["drawer_id"])
        if len(written) == 1:
            writer.start()
            deadline = time.monotonic() + 10
            while lock._waiting_writers < 1:
                assert time.monotonic() < deadline, "writer never queued"
                time.sleep(0.001)
        return {"success": True, "drawer_id": kw["drawer_id"]}

    def sweep():
        try:
            result["response"] = mcp._http_dispatch(
                {
                    "jsonrpc": "2.0",
                    "id": "w",
                    "method": "internal/write_mirror_units",
                    "params": {"thread_id": "synthetic", "units": [_unit(i) for i in range(1, 65)]},
                }
            )
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(mcp, "_HTTP_REQUEST_LOCK", lock)
    monkeypatch.setattr(mcp._provenance, "current", lambda: object())
    monkeypatch.setattr(mcp, "_mcp_tool_preflight_refusal", lambda *a, **k: None)
    monkeypatch.setattr(mcp, "_write_drawer", fake_write)
    sweep_thread = threading.Thread(target=sweep, daemon=True)
    sweep_thread.start()
    try:
        assert writer_entered.wait(10), "queued writer never entered"
    finally:
        release_writer.set()
        sweep_thread.join(10)
        if writer.ident is not None:
            writer.join(10)
    assert not sweep_thread.is_alive() and not writer.is_alive()
    assert not errors, errors
    payload = json.loads(result["response"]["result"]["content"][0]["text"])
    assert payload["success"] is True
    assert payload["drawer_ids"] == written
    assert len(written) == len(set(written)) == 64
    assert writer_units == list(range(5))
