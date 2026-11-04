"""Head-only integration checks, final in the skeleton."""

import threading
from pathlib import Path
from time import monotonic

from commonplace.handoff.wiring import HandoffContext
from tests._console_helpers import console_hub_fixture, optional, successful  # noqa: F401


def activity_rows(h):
    store = optional("commonplace.console.store")
    assert store is not None
    activity = store.peek(h.gateway, h.workspace)
    return [] if activity is None else activity.after(h.a1, 0, 100)


def test_observe_is_bounded_when_control_lock_is_held(console_hub):
    h = console_hub
    assert h.toggle()[0] == 200
    settings = optional("commonplace.console.settings")
    recorder = optional("commonplace.console.recorder")
    assert settings is not None and recorder is not None
    settings.forget(h.db, h.a1)
    principal = h.principal(h.t1)
    request = {"method": "tools/call", "params": {"name": "task_list", "arguments": {}}}
    response = {"result": {"content": []}}
    held, release, done = threading.Event(), threading.Event(), threading.Event()
    failures = []

    def hold():
        with h.db._lock:
            held.set()
            release.wait(10)

    def observe_once():
        try:
            recorder.observe(h.gateway.mediator, principal, request, response, monotonic())
        except Exception as exc:
            failures.append(exc)
        else:
            done.set()

    holder = threading.Thread(target=hold, daemon=True)
    worker = threading.Thread(target=observe_once, daemon=True)
    holder.start()
    started = monotonic()
    try:
        assert held.wait(5)
        worker.start()
        assert done.wait(5)
        assert not failures
        assert not activity_rows(h)
    finally:
        release.set()
        holder.join(5)
        if worker.ident is not None:
            worker.join(5)
        print(f"observe lock-held elapsed (info): {monotonic() - started:.3f}s")
    assert not holder.is_alive() and not worker.is_alive()
    recorder.observe(h.gateway.mediator, principal, request, response, monotonic())
    assert len(activity_rows(h)) == 1


def test_capture_fact_save_is_not_recorded(console_hub, tmp_path):
    h = console_hub
    assert h.toggle()[0] == 200
    ctx = HandoffContext(gateway=h.gateway, control_db=h.db, data_dir=Path(tmp_path))
    try:
        # Deliberately incomplete arguments refuse locally, without a memory worker.
        ctx.save_fact(h.principal(h.t1), {})
    except RuntimeError:
        pass
    finally:
        ctx.close()
    assert not activity_rows(h)
    successful(h, h.t1, "task_list")
    assert len(activity_rows(h)) == 1
