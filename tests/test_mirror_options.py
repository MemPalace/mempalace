"""Opt-in mirror filtering and resumable worker boundaries."""

import hashlib
import json

import pytest

from commonplace.handoff.models import ThreadMessage
from commonplace.handoff.palace_mirror import MirrorError, mirror_pending
from commonplace.handoff.thread_store import ThreadStore


class Worker:
    def __init__(self):
        self.batches = []

    def __call__(self, principal, name, arguments):
        assert name == "write_mirror_units"
        self.batches.append(arguments["units"])
        return {"drawer_ids": [f"drawer_{u['seq_start']}_{u['part']}" for u in arguments["units"]]}


@pytest.fixture
def store(tmp_path):
    value = ThreadStore(str(tmp_path / "threads.sqlite3"))
    yield value
    value.close()


def save(store, texts):
    messages = [
        ThreadMessage(i, "user", kind, text, "raw archive") for i, (kind, text) in enumerate(texts)
    ]
    stored = store.store("workspace", "import:sample", messages, actor="actor")
    store.begin_mirror(stored.id, -1, bootstrap=-1)
    return stored


def test_u2_a_skip_empty_keeps_original_ranges(store):
    stored = save(
        store,
        [
            ("capture.raw", ""),
            ("message", " first\r\n\t"),
            ("import.part", ""),
            ("message", "second  🦉"),
            ("capture.base", ""),
            ("import.raw", ""),
        ],
    )
    worker = Worker()
    mirror_pending(worker, None, stored, store, skip_empty=True)
    assert (
        worker.batches[0][0]["text"]
        == "[1] user message:  first\r\n\t\n[3] user message: second  🦉"
    )
    assert (worker.batches[0][0]["seq_start"], worker.batches[0][0]["seq_end"]) == (1, 3)
    assert store.mirror_progress(stored.id) == (5, 5)


def test_u2_a_empty_only_range_completes_without_worker(store):
    stored = save(store, [("import.raw", ""), ("capture.base", "")])
    worker = Worker()
    assert mirror_pending(worker, None, stored, store, skip_empty=True) == []
    assert worker.batches == []
    assert store.mirror_progress(stored.id) == (1, 1)


def test_u2_b_default_matches_base_golden_bytes(store):
    stored = save(
        store,
        [
            ("capture.raw", ""),
            ("message", " leading\r\n\t  café 🦉"),
            ("tool_result", "output " * 800),
            ("message", "tail"),
        ],
    )
    worker = Worker()
    mirror_pending(worker, None, stored, store)
    encoded = json.dumps(worker.batches, ensure_ascii=False, separators=(",", ":")).encode()
    # Golden worker payload from S0 commit 112a041f, including index provenance.
    assert (
        hashlib.sha256(encoded).hexdigest()
        == "151d4eabb554b35fbec7953a3ff9ab089b7b289066876430632c7883538de1cb"
    )


@pytest.mark.parametrize("allowed_batches", [0, 1])
def test_u2_c_callback_stops_before_write_and_leaves_pending(store, allowed_batches):
    stored = save(store, [("tool_result", f"unit {i} " + "x" * 3980) for i in range(130)])
    worker = Worker()
    checks = []

    def may_write():
        checks.append(len(worker.batches))
        return len(worker.batches) < allowed_batches

    with pytest.raises(MirrorError, match="paused"):
        mirror_pending(worker, None, stored, store, should_continue=may_write)
    assert checks == list(range(allowed_batches + 1))
    assert len(worker.batches) == allowed_batches
    assert all(len(batch) <= 64 for batch in worker.batches)
    assert store.mirror_progress(stored.id) == (-1, 129)
    mirror_pending(worker, None, stored, store, should_continue=lambda: True)
    assert store.mirror_progress(stored.id) == (129, 129)


def test_u2_d_long_message_keeps_late_marker_verbatim(store):
    marker = " unique-marker café 🦉\r\n\t "
    text = "a" * 6000 + marker + "b" * (3000 - len(marker))
    stored = save(store, [("message", text)])
    worker = Worker()
    mirror_pending(worker, None, stored, store, skip_empty=True)
    units = worker.batches[0]
    assert len(units) >= 3
    assert any(marker in unit["text"] for unit in units)
    assert "".join(unit["text"] for unit in units) == "[0] user message: " + text
