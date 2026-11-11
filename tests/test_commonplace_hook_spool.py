"""Capture hook spool: offline write, ordered upload, retries and exactly-once."""

import errno
import io
import json
import re
import socket
import stat
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from test_commonplace_capture import connection, enable, native, saved
from test_commonplace_import import surface as surface

from commonplace import capture_client, client_hooks, hook_spool, plans, sync_protocol
from commonplace.hook_spool import Permanent, Skipped, Spool, Transient

CLOSED_PORT_URL = "http://127.0.0.1:9"


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def hook(home, payload, *, client="claude", monkeypatch=None):
    """Run the capture hook with the child spawn stubbed; returns (stdout, spawn mock)."""
    out = io.StringIO()
    with patch.object(client_hooks.subprocess, "Popen") as spawn:
        code = client_hooks.main(
            ["--event", "capture", "--client", client, "--home", str(home)],
            stdin=io.StringIO(payload),
            stdout=out,
        )
    assert code == 0
    return out.getvalue(), spawn


def payload_for(path, **extra):
    # Odd spacing and non-ASCII on purpose: the spool must keep the text as sent.
    return (
        json.dumps({"transcript_path": str(path), "note": "café 🦉", **extra}, indent=None) + " \n"
    )


def transcript(tmp_path, text="Keep every word.", name="session.jsonl"):
    path = tmp_path / name
    path.write_bytes(native(text=text).encode())
    return path


# -- the hook: spool and return -------------------------------------------------


def test_hook_spools_payload_verbatim_without_network_inside_the_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMONPLACE_CAPTURE", "on")
    monkeypatch.setenv("COMMONPLACE_TOKEN", "synthetic-secret")

    def refuse(*_a, **_k):
        raise AssertionError("the hook must not touch the network")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    path = tmp_path / "s.jsonl"
    raw = payload_for(path)
    started = time.perf_counter()
    out, spawn = hook(tmp_path / "home", raw, monkeypatch=monkeypatch)
    elapsed = time.perf_counter() - started

    assert elapsed < 0.5
    assert json.loads(out) == {}
    root = hook_spool.default_root(tmp_path / "home")
    [item] = Spool(root).items()
    saved_item = json.loads(item.read_text(encoding="utf-8"))
    assert saved_item["payload"] == raw
    assert saved_item["source"] == "claude-code"
    assert re.fullmatch(sync_protocol.OP_ID_PATTERN, saved_item["key"])
    assert "synthetic-secret" not in item.read_text(encoding="utf-8")
    # The upload is a detached child that carries no secret on its command line.
    command = spawn.call_args.args[0]
    assert command[1:5] == ["-I", "-m", "commonplace.hook_spool", "drain"]
    assert "synthetic-secret" not in " ".join(command)


@pytest.mark.skipif(
    not hasattr(stat, "S_IMODE") or __import__("os").name == "nt", reason="POSIX modes"
)
def test_spool_is_private_and_atomic(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMONPLACE_CAPTURE", "on")
    monkeypatch.setenv("COMMONPLACE_TOKEN", "x")
    hook(tmp_path, payload_for(tmp_path / "s.jsonl"))
    root = hook_spool.default_root(tmp_path)
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    [item] = Spool(root).items()
    assert stat.S_IMODE(item.stat().st_mode) == 0o600
    # No half-written temporary file survives a completed write.
    assert not list(root.glob(".tmp-*"))


def test_failed_atomic_write_leaves_no_partial_item(tmp_path):
    spool = Spool(tmp_path / "spool")
    spool.enqueue("codex", "{}")
    with patch.object(hook_spool.os, "replace", side_effect=OSError("disk")):
        with pytest.raises(OSError):
            spool.enqueue("codex", '{"b":1}')
    assert len(spool.items()) == 1
    assert not list((tmp_path / "spool").glob(".tmp-*"))


def test_hook_fails_open_when_the_spool_is_unusable(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMONPLACE_CAPTURE", "on")
    real = tmp_path / "real"
    real.mkdir()
    home = tmp_path / "home"
    (home / ".commonplace").mkdir(parents=True)
    (home / ".commonplace" / "spool").symlink_to(real)
    out, spawn = hook(home, payload_for(tmp_path / "s.jsonl"))
    assert json.loads(out) == {}
    assert list(real.iterdir()) == []
    spawn.assert_not_called()


def test_hook_without_opt_in_or_path_stores_nothing(tmp_path, monkeypatch):
    out, spawn = hook(tmp_path, payload_for(tmp_path / "s.jsonl"))
    assert json.loads(out) == {}
    monkeypatch.setenv("COMMONPLACE_CAPTURE", "on")
    hook(tmp_path, json.dumps({"transcript_path": "relative.jsonl"}))
    assert not hook_spool.default_root(tmp_path).exists()
    spawn.assert_not_called()


def test_oversize_hook_payload_is_logged_not_spooled(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMONPLACE_CAPTURE", "on")
    huge = json.dumps({"transcript_path": "/x/s.jsonl", "pad": "a" * client_hooks.MAX_INPUT_BYTES})
    out, spawn = hook(tmp_path, huge)
    assert json.loads(out) == {}
    spool = Spool(hook_spool.default_root(tmp_path))
    assert spool.items() == []
    [line] = [json.loads(x) for x in spool.log_path.read_text().splitlines()]
    assert line["event"] == "dropped" and "over" in line["reason"]
    assert "aaaa" not in spool.log_path.read_text()


# -- bounded size ---------------------------------------------------------------


def test_full_spool_drops_oldest_first_and_logs_every_drop(tmp_path):
    spool = Spool(tmp_path / "spool", max_items=3)
    keys = [spool.enqueue("claude-code", json.dumps({"n": n, "secret": "TEXT"})) for n in range(5)]
    remaining = [json.loads(p.read_text())["key"] for p in spool.items()]
    assert remaining == keys[2:]
    dropped = [json.loads(x) for x in spool.log_path.read_text().splitlines()]
    assert [d["key"] for d in dropped] == keys[:2]
    assert all(d["event"] == "dropped" and d["reason"] == "spool_full" for d in dropped)
    assert "TEXT" not in spool.log_path.read_text()


def test_byte_bound_and_item_larger_than_the_spool(tmp_path):
    one = len(json.dumps({"p": "x" * 200}))
    spool = Spool(tmp_path / "spool", max_bytes=one * 3)
    keys = [spool.enqueue("codex", json.dumps({"p": "x" * 200})) for _ in range(6)]
    kept = [json.loads(p.read_text())["key"] for p in spool.items()]
    assert kept == keys[-len(kept) :] and 1 <= len(kept) < 6
    assert sum(p.stat().st_size for p in spool.items()) <= spool.max_bytes
    assert spool.enqueue("codex", "y" * (one * 10)) is None
    last = json.loads(spool.log_path.read_text().splitlines()[-1])
    assert last["reason"] == "item_larger_than_spool"
    assert [json.loads(p.read_text())["key"] for p in spool.items()] == kept


def test_log_rotates_instead_of_growing_without_bound(tmp_path, monkeypatch):
    spool = Spool(tmp_path / "spool", max_items=1)
    monkeypatch.setattr(hook_spool, "MAX_LOG_BYTES", 300)
    for n in range(40):
        spool.enqueue("codex", json.dumps({"n": n}))
    assert spool.log_path.stat().st_size < 600
    assert spool.log_path.with_name("spool.log.1").exists()


def test_order_survives_a_clock_step_back(tmp_path):
    clock = Clock(2_000_000.0)
    spool = Spool(tmp_path / "spool", clock=clock)
    first = spool.enqueue("codex", "{}")
    clock.now = 1.0
    second = spool.enqueue("codex", "{}")
    assert [json.loads(p.read_text())["key"] for p in spool.items()] == [first, second]


# -- drain: order, backoff, outcomes --------------------------------------------


def fill(spool, count):
    return [spool.enqueue("claude-code", json.dumps({"n": n})) for n in range(count)]


def test_drain_uploads_in_order_and_removes_only_confirmed_items(tmp_path):
    spool = Spool(tmp_path / "spool", clock=Clock())
    keys = fill(spool, 3)
    sent = []

    def upload(item):
        # While uploading, the item is still on disk: removal follows confirmation.
        assert len(spool.items()) == 3 - len(sent)
        sent.append(item["key"])

    assert spool.drain(upload) == "done"
    assert sent == keys and spool.items() == []
    assert spool.drain(upload) == "empty"


def test_failure_keeps_the_item_stops_the_pass_and_backs_off(tmp_path):
    clock = Clock()
    spool = Spool(tmp_path / "spool", clock=clock)
    keys = fill(spool, 3)
    calls = []

    def flaky(item):
        calls.append(item["key"])
        if len(calls) == 2:
            raise Transient("hub unreachable")

    assert spool.drain(flaky) == "failed"
    # The first went up; the second failed; the third was not tried out of order.
    assert calls == keys[:2]
    assert [json.loads(p.read_text())["key"] for p in spool.items()] == keys[1:]
    # Within the backoff window nothing is attempted.
    assert spool.drain(flaky) == "waiting"
    assert calls == keys[:2]
    # After it, the failed item is retried first and everything completes.
    clock.now += hook_spool.BACKOFF_BASE_S + 1
    assert spool.drain(flaky) == "done"
    assert calls == keys[:2] + keys[1:]
    assert spool.items() == [] and spool.state() == {}


def test_backoff_doubles_up_to_the_cap_and_resets_on_success(tmp_path):
    clock = Clock()
    spool = Spool(tmp_path / "spool", clock=clock)
    fill(spool, 1)

    def down(_item):
        raise Transient("down")

    delays = []
    for _ in range(9):
        assert spool.drain(down, force=True) == "failed"
        delays.append(spool.state()["next_at"] - clock.now)
    assert delays[:4] == [5, 10, 20, 40]
    assert delays[-1] == hook_spool.BACKOFF_CAP_S
    assert spool.drain(lambda item: None, force=True) == "done"
    assert spool.state() == {}


def test_item_that_keeps_failing_is_set_aside_not_blocking_later_ones(tmp_path):
    clock = Clock()
    spool = Spool(tmp_path / "spool", clock=clock)
    first, second = fill(spool, 2)

    def upload(item):
        if item["key"] == first:
            raise Transient("always")

    for _ in range(hook_spool.MAX_ATTEMPTS):
        spool.drain(upload, force=True)
    assert [json.loads(p.read_text())["key"] for p in spool.items()] == []
    kept = list((spool.root / "rejected").glob("*.json"))
    assert [json.loads(p.read_text())["key"] for p in kept] == [first]
    events = [json.loads(x)["event"] for x in spool.log_path.read_text().splitlines()]
    assert "rejected" in events


def test_permanent_failure_is_kept_aside_and_the_next_item_still_uploads(tmp_path):
    spool = Spool(tmp_path / "spool", clock=Clock())
    bad, good = fill(spool, 2)
    sent = []

    def upload(item):
        if item["key"] == bad:
            raise Permanent("transcript missing")
        sent.append(item["key"])

    assert spool.drain(upload) == "done"
    assert sent == [good]
    assert [p.name.split("-", 1)[1] for p in (spool.root / "rejected").glob("*.json")] == [
        bad + ".json"
    ]
    assert "transcript missing" in spool.log_path.read_text()


def test_skipped_item_is_removed_with_a_log_line(tmp_path):
    spool = Spool(tmp_path / "spool", clock=Clock())
    [key] = fill(spool, 1)

    def upload(_item):
        raise Skipped("capture is off")

    assert spool.drain(upload) == "done"
    assert spool.items() == []
    assert json.loads(spool.log_path.read_text().splitlines()[-1])["key"] == key


def test_corrupt_item_is_set_aside_and_logged(tmp_path):
    spool = Spool(tmp_path / "spool", clock=Clock())
    [key] = fill(spool, 1)
    spool.items()[0].write_text("{not json")
    assert spool.drain(lambda item: None) == "done"
    assert spool.items() == []
    assert len(list((spool.root / "rejected").glob("*.json"))) == 1
    assert "unreadable item" in spool.log_path.read_text()
    assert key in spool.log_path.read_text()


def test_only_one_drain_runs_at_a_time(tmp_path):
    spool = Spool(tmp_path / "spool", clock=Clock())
    fill(spool, 1)
    nested = []

    def upload(_item):
        nested.append(Spool(spool.root, clock=spool.clock).drain(lambda item: None))

    assert spool.drain(upload) == "done"
    assert nested == ["busy"]


# -- upload against the real gateway --------------------------------------------


def hub(s, monkeypatch, source="claude-code"):
    enable(s)
    plans.set_plan(s.db, s.user, "paid", actor=s.user)
    monkeypatch.setattr(plans, "load_ratio", lambda: 0)
    return f"http://127.0.0.1:{s.port}", connection(s, source)


def test_offline_spool_then_later_upload_with_a_retry_after_failure(surface, tmp_path, monkeypatch):
    s = surface
    url, bearer = hub(s, monkeypatch)
    path = transcript(tmp_path)
    clock = Clock()
    spool = Spool(tmp_path / "spool", clock=clock)
    key = spool.enqueue("claude-code", payload_for(path))

    # Offline: nothing can upload, the item and the transcript are untouched.
    assert spool.drain(hook_spool.uploader(CLOSED_PORT_URL, bearer)) == "failed"
    assert [json.loads(p.read_text())["key"] for p in spool.items()] == [key]
    assert spool.status()["failures"] == 1
    # Still inside the backoff: no attempt, even though the hub is now reachable.
    assert spool.drain(hook_spool.uploader(url, bearer)) == "waiting"
    # A later pass, after the backoff, uploads and only then removes the item.
    clock.now += hook_spool.BACKOFF_BASE_S + 1
    assert spool.drain(hook_spool.uploader(url, bearer)) == "done"
    assert spool.items() == [] and spool.status()["failures"] == 0
    state = capture_client.request(
        capture_client.endpoint(url),
        bearer,
        "GET",
        "/api/capture?source=claude-code&thread_id=session-1",
    )
    assert state["offset"] == path.stat().st_size
    texts = [m["text"] for m in saved(s, state["stored_thread_id"])]
    assert sum("Keep every word." in t for t in texts) >= 1


def test_no_duplicate_after_a_crash_between_upload_and_delete(surface, tmp_path, monkeypatch):
    s = surface
    url, bearer = hub(s, monkeypatch)
    path = transcript(tmp_path)
    spool = Spool(tmp_path / "spool", clock=Clock())
    key = spool.enqueue("claude-code", payload_for(path))
    upload = hook_spool.uploader(url, bearer)

    class Crash(BaseException):
        pass

    def upload_then_die(item):
        upload(item)  # the hub has confirmed and stored it ...
        raise Crash  # ... and the process dies before the item is deleted

    with pytest.raises(Crash):
        spool.drain(upload_then_die)
    assert [json.loads(p.read_text())["key"] for p in spool.items()] == [key]
    thread = capture_client.request(
        capture_client.endpoint(url),
        bearer,
        "GET",
        "/api/capture?source=claude-code&thread_id=session-1",
    )["stored_thread_id"]
    before = saved(s, thread)

    # Restart: the same item is sent again and the hub keeps exactly one copy.
    assert spool.drain(hook_spool.uploader(url, bearer)) == "done"
    assert spool.items() == []
    assert saved(s, thread) == before


def test_chunks_carry_an_idempotency_key_bound_to_item_and_offset(surface, tmp_path, monkeypatch):
    s = surface
    url, bearer = hub(s, monkeypatch)
    path = transcript(tmp_path)
    spool = Spool(tmp_path / "spool", clock=Clock())
    key = spool.enqueue("claude-code", payload_for(path))
    bodies = []
    real = capture_client.request

    def spy(target, token, method, route, body=None):
        if method == "POST":
            bodies.append(body)
        return real(target, token, method, route, body)

    with patch.object(capture_client, "request", spy):
        assert spool.drain(hook_spool.uploader(url, bearer)) == "done"
    [body] = bodies
    assert body["client_op_id"] == f"{key}-0"
    assert re.fullmatch(sync_protocol.OP_ID_PATTERN, body["client_op_id"])


def test_two_queued_events_for_one_transcript_store_it_once_and_in_order(
    surface, tmp_path, monkeypatch
):
    s = surface
    url, bearer = hub(s, monkeypatch)
    path = transcript(tmp_path)
    spool = Spool(tmp_path / "spool", clock=Clock())
    spool.enqueue("claude-code", payload_for(path))
    with path.open("ab") as stream:
        stream.write(native(text="second turn").encode())
    spool.enqueue("claude-code", payload_for(path))
    assert spool.drain(hook_spool.uploader(url, bearer)) == "done"
    thread = capture_client.request(
        capture_client.endpoint(url),
        bearer,
        "GET",
        "/api/capture?source=claude-code&thread_id=session-1",
    )["stored_thread_id"]
    texts = [m["text"] for m in saved(s, thread) if m.get("name") != "capture.raw"]
    assert [t for t in texts if "Keep every word." in t] != []
    assert sum("second turn" in t for t in texts) == 1


def test_opted_out_workspace_drops_the_item_with_a_log_and_reads_no_transcript(
    surface, tmp_path, monkeypatch
):
    s = surface
    bearer = connection(s)  # capture stays off
    path = transcript(tmp_path)
    spool = Spool(tmp_path / "spool", clock=Clock())
    spool.enqueue("claude-code", payload_for(path))
    with patch.object(capture_client, "sync_file", side_effect=AssertionError("read")):
        assert spool.drain(hook_spool.uploader(f"http://127.0.0.1:{s.port}", bearer)) == "done"
    assert spool.items() == []
    assert "capture is off" in spool.log_path.read_text()


def test_missing_transcript_is_set_aside_without_blocking(surface, tmp_path, monkeypatch):
    s = surface
    url, bearer = hub(s, monkeypatch)
    spool = Spool(tmp_path / "spool", clock=Clock())
    spool.enqueue("claude-code", payload_for(tmp_path / "gone.jsonl"))
    good = transcript(tmp_path, name="good.jsonl")
    spool.enqueue("claude-code", payload_for(good))
    assert spool.drain(hook_spool.uploader(url, bearer)) == "done"
    assert spool.items() == []
    assert len(list((spool.root / "rejected").glob("*.json"))) == 1


def test_main_requires_explicit_hub_and_reports_status(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("COMMONPLACE_URL", raising=False)
    monkeypatch.delenv("COMMONPLACE_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        hook_spool.main(["drain", "--home", str(tmp_path)])
    assert hook_spool.main(["status", "--home", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["pending"] == 0


def test_spooling_many_events_stays_inside_the_hook_budget(tmp_path, monkeypatch):
    # Hook runs against a hub that would hang: only the spool write is timed.
    monkeypatch.setenv("COMMONPLACE_CAPTURE", "on")
    runs = []
    for _ in range(5):
        started = time.perf_counter()
        hook(tmp_path / "home", payload_for(Path("/x/s.jsonl")))
        runs.append(time.perf_counter() - started)
    assert max(runs) < 0.5
    assert len(Spool(hook_spool.default_root(tmp_path / "home")).items()) == 5


def test_a_transcript_of_many_chunks_uploads_in_one_drain_without_backoff(
    surface, tmp_path, monkeypatch
):
    s = surface
    url, bearer = hub(s, monkeypatch)
    monkeypatch.setattr(capture_client, "CHUNK_BYTES", 300)
    path = tmp_path / "long.jsonl"
    path.write_bytes("".join(native(text=f"turn {n}") for n in range(200)).encode())
    spool = Spool(tmp_path / "spool", clock=Clock())
    spool.enqueue("claude-code", payload_for(path))
    assert spool.drain(hook_spool.uploader(url, bearer)) == "done"
    assert spool.items() == [] and spool.state() == {}
    state = capture_client.request(
        capture_client.endpoint(url),
        bearer,
        "GET",
        "/api/capture?source=claude-code&thread_id=session-1",
    )
    assert state["offset"] == path.stat().st_size


def test_a_cursor_that_does_not_advance_is_a_failure_not_an_endless_loop(tmp_path):
    spool = Spool(tmp_path / "spool", clock=Clock())
    spool.enqueue("claude-code", payload_for(tmp_path / "s.jsonl"))
    upload = hook_spool.uploader(CLOSED_PORT_URL, "token")

    def stuck(target, token, source, path, op_id=None):
        return {"status": "appended", "offset": 10}

    with (
        patch.object(capture_client, "request", return_value={"enabled": True}),
        patch.object(capture_client, "sync_file", stuck),
    ):
        assert spool.drain(upload) == "failed"
    assert len(spool.items()) == 1


def test_items_removed_by_a_concurrent_drain_do_not_break_enqueue_or_status(tmp_path):
    spool = Spool(tmp_path / "spool")
    spool.enqueue("codex", "{}")
    ghost = spool.root / "99999999999999999999-gone.json"
    with patch.object(Spool, "items", lambda self: [*sorted(self.root.glob("0*.json")), ghost]):
        assert spool.status()["pending"] == 2
        assert spool.enqueue("codex", "{}") is not None


def test_a_refused_credential_keeps_the_queue_and_never_gives_it_up(surface, tmp_path, monkeypatch):
    s = surface
    url, bearer = hub(s, monkeypatch)
    spool = Spool(tmp_path / "spool", clock=Clock())
    spool.enqueue("claude-code", payload_for(transcript(tmp_path)))
    spool.enqueue("claude-code", payload_for(transcript(tmp_path, name="two.jsonl")))
    # The settings request already fails with 401: far more passes than MAX_ATTEMPTS.
    for _ in range(hook_spool.MAX_ATTEMPTS + 3):
        assert spool.drain(hook_spool.uploader(url, "revoked-token"), force=True) == "failed"
    assert len(spool.items()) == 2
    assert not list((spool.root / "rejected").glob("*.json"))
    # A fixed token drains everything.
    assert spool.drain(hook_spool.uploader(url, bearer), force=True) == "done"
    assert spool.items() == []


def test_a_refusal_on_the_chunk_request_halts_instead_of_rejecting(tmp_path):
    spool = Spool(tmp_path / "spool", clock=Clock())
    spool.enqueue("claude-code", payload_for(tmp_path / "s.jsonl"))

    def refused(*_a, **_k):
        raise capture_client.CaptureAuth("Capture request refused (HTTP 401); saved data kept")

    with (
        patch.object(capture_client, "request", return_value={"enabled": True}),
        patch.object(capture_client, "sync_file", refused),
    ):
        for _ in range(hook_spool.MAX_ATTEMPTS + 2):
            assert spool.drain(hook_spool.uploader(CLOSED_PORT_URL, "t"), force=True) == "failed"
    assert len(spool.items()) == 1
    assert not list((spool.root / "rejected").glob("*.json"))


@pytest.mark.parametrize(
    "error,kept",
    [(OSError(errno.EIO, "io"), True), (FileNotFoundError(errno.ENOENT, "gone"), False)],
)
def test_only_unrecoverable_read_errors_set_the_item_aside(tmp_path, error, kept):
    spool = Spool(tmp_path / "spool", clock=Clock())
    spool.enqueue("claude-code", payload_for(tmp_path / "s.jsonl"))

    def broken(*_a, **_k):
        raise error

    with (
        patch.object(capture_client, "request", return_value={"enabled": True}),
        patch.object(capture_client, "sync_file", broken),
    ):
        spool.drain(hook_spool.uploader(CLOSED_PORT_URL, "t"))
    assert bool(spool.items()) is kept


def test_a_rate_limited_settings_check_keeps_the_item(tmp_path):
    spool = Spool(tmp_path / "spool", clock=Clock())
    spool.enqueue("claude-code", payload_for(tmp_path / "s.jsonl"))
    upload = hook_spool.uploader(CLOSED_PORT_URL, "t")
    with patch.object(capture_client, "request", return_value={"status": "deferred"}):
        assert spool.drain(upload) == "failed"
    assert len(spool.items()) == 1
    assert not spool.log_path.exists() or "capture is off" not in spool.log_path.read_text()
