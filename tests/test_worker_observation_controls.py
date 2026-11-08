"""Deterministic bounds and actual-leaf controls for allocation observation."""

import builtins
import gc
import io
import json
import os
import pickle
import subprocess
import sys
import threading
import weakref
from types import SimpleNamespace

import pytest

from tests.test_worker_observation import ROOT_KEYS, serving as _serving

serving = _serving


def test_observed_leaf_counts_green(serving, monkeypatch, tmp_path):
    from mempalace import embedding
    from mempalace.backends.chroma import _SafePersistentDataUnpickler

    server, invoke, records, _ = serving
    path = tmp_path / "synthetic.pickle"
    path.write_bytes(pickle.dumps({"count": 1}))
    (tmp_path / "chroma.sqlite3").touch()
    raw = object()
    monkeypatch.setattr(server._config, "_palace_path_override", str(tmp_path), raising=False)
    monkeypatch.setattr(server._config, "_file_config", {"palace_path": str(tmp_path)})
    monkeypatch.setattr(server, "_selected_backend_name", lambda: "chroma")
    monkeypatch.setattr(server, "_collection_cache", None)
    monkeypatch.setattr(
        server, "_get_client", lambda: SimpleNamespace(get_collection=lambda *a, **kw: raw)
    )
    monkeypatch.setattr(server, "_pin_hnsw_threads", lambda *a: None)
    monkeypatch.setattr(server, "_checked_chroma_collection", lambda value, **kw: value)
    monkeypatch.setattr(server, "_restamp_palace_db", lambda: None)
    monkeypatch.setattr(server.ChromaBackend, "_resolve_embedding_function", lambda **kw: None)
    monkeypatch.setattr(embedding, "_intra_op_session_options", lambda threads: None)
    ort = SimpleNamespace(InferenceSession=lambda *a, **kw: object())
    seen = []

    def handler(**kwargs):
        seen.append(_SafePersistentDataUnpickler.load(path))
        seen.append(server._get_collection())
        seen.append(server._get_collection())  # cached facade is not another open
        embedding._new_embeddinggemma_session(ort, "synthetic", None, 1, [])
        return {}

    monkeypatch.setitem(server.TOOLS["mempalace_status"], "handler", handler)
    invoke()
    ends = [r for r in records() if r["event"] == "call_end"]
    assert seen == [{"count": 1}, raw, raw]
    assert len(ends) == 1
    assert ends[0]["restricted_pickle_load_count"] == 1
    assert ends[0]["collection_open_count"] == 1
    assert ends[0]["model_session_create_count"] == 1
    assert ends[0]["model_backend"] == {"model": "embeddinggemma", "backend": "chroma"}


def test_closed_schema_rejects_entire_candidate(serving):
    from mempalace.worker_observation import encode_record

    _, invoke, records, _ = serving
    invoke()
    row = records()[0]
    assert set(row) == ROOT_KEYS
    candidates = [
        dict(row, extra="PRIVATE"),
        dict(row, model_backend={"model": None, "backend": None, "extra": "PRIVATE"}),
        dict(row, record_seq=True),
        dict(row, record_seq=-1),
        dict(row, outcome="PRIVATE"),
        dict(row, tool_name="PRIVATE"),
        dict(row, rss_source="PRIVATE"),
        dict(row, schema_version=True),
        dict(row, timestamp_utc="PRIVATE"),
        dict(row, model_backend={"model": "PRIVATE", "backend": None}),
    ]
    missing = dict(row)
    del missing["outcome"]
    candidates.append(missing)
    for candidate in candidates:
        with pytest.raises(ValueError, match="invalid observation"):
            encode_record(candidate)


def test_two_current_rss_reads_and_null_failures(serving, monkeypatch):
    from mempalace import worker_observation as obs

    _, invoke, records, instances = serving
    invoke()  # activate, then count only the next call
    opened = []
    real_open = builtins.open

    def current(path, *args, **kwargs):
        if path == "/proc/self/status":
            opened.append(path)
            return io.StringIO("VmRSS:\t100 kB\nRssAnon:\t60 kB\nRssFile:\t40 kB\n")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", current)
    monkeypatch.setattr(obs.sys, "platform", "linux")
    invoke()
    assert len(opened) == 2
    end = records()[-1]
    assert (
        end["rss_before_bytes"],
        end["rss_after_bytes"],
        end["rss_anon_after_bytes"],
        end["rss_file_after_bytes"],
    ) == (102400, 102400, 61440, 40960)
    monkeypatch.setattr(obs.sys, "platform", "unsupported")
    invoke()
    end = records()[-1]
    assert end["rss_before_bytes"] is None and end["rss_after_bytes"] is None
    assert end["rss_status"] == "unsupported"
    assert instances[0].active == 0


def test_opt_out_does_not_activate(serving, monkeypatch):
    _, invoke, records, instances = serving
    monkeypatch.setenv("MEMPALACE_WORKER_MEMORY_OBSERVE", "0")
    invoke()
    assert records() == [] and instances == []


def test_transport_release_precedes_end_and_nested_reuses_token(serving, monkeypatch):
    server, invoke, records, _ = serving
    references = []

    class Payload(dict):
        pass

    def handler(**kw):
        payload = Payload(private="RESULT-PRIVATE")
        references.append(weakref.ref(payload))
        with server._worker_local_scope("mempalace_search", "light_local"):
            pass
        return payload

    monkeypatch.setitem(server.TOOLS["mempalace_status"], "handler", handler)
    with server._worker_transport():
        response = invoke()
        assert not any(r["event"] == "call_end" for r in records())
        del response
        gc.collect()
        assert references[0]() is None
    calls = [r for r in records() if r["event"].startswith("call_")]
    assert len(calls) == 2 and calls[-1]["tool_name"] == "mempalace_status"


def test_phase_pairs_and_in_call_phase_reuses_token(serving):
    server, invoke, records, _ = serving
    server._activate_worker_observation()
    with server._worker_phase("startup_preflight"):
        with server._worker_phase("capacity_probe"):
            pass
    with server._worker_local_scope("mempalace_status"):
        with server._worker_phase("capacity_probe"):
            invoke()
    assert [r["event"] for r in records()] == [
        "generation_start",
        "phase_begin",
        "phase_end",
        "call_begin",
        "call_end",
    ]
    assert records()[1]["tool_name"] is None
    assert records()[1]["startup_phase"] == "startup_preflight"


def test_bounded_ring_retention_green(tmp_path, capsys):
    from mempalace.worker_observation import Observer, RingSink

    # Tiny cap permits one complete line per file, including generation events.
    sink = RingSink(tmp_path, max_bytes=2048)
    for _ in range(4):
        observer = Observer(tmp_path, synchronous=True, sink=sink)
        with observer.scope("mempalace_status"):
            pass
        observer.close()
    directory = tmp_path / "diagnostics"
    assert {p.name for p in directory.iterdir()} == {
        "worker-memory.jsonl",
        "worker-memory.jsonl.1",
        "worker-memory.lock",
    }
    for path in directory.glob("*.jsonl*"):
        assert path.stat().st_size <= 2048
        for line in path.read_text().splitlines():
            assert set(json.loads(line)) == ROOT_KEYS
    if os.name != "nt":
        assert directory.stat().st_mode & 0o777 == 0o700
        assert all(p.stat().st_mode & 0o777 == 0o600 for p in directory.iterdir())
    assert not (tmp_path / "worker.log").exists()
    assert capsys.readouterr() == ("", "")


def test_cross_process_ring_lock_rotation_and_symlink_refusal(tmp_path):
    from mempalace.worker_observation import Observer, RingSink

    sink = RingSink(tmp_path, max_bytes=2048)
    observer = Observer(tmp_path, synchronous=True, sink=sink)
    observer.close()
    code = """
import sys
from mempalace.worker_observation import Observer, RingSink
for _ in range(8):
    observer = Observer(sys.argv[1], synchronous=True, sink=RingSink(sys.argv[1], max_bytes=2048))
    with observer.scope('mempalace_status'):
        pass
    observer.close()
"""
    children = [subprocess.Popen([sys.executable, "-c", code, str(tmp_path)]) for _ in range(2)]
    try:
        assert [p.wait(timeout=5) for p in children] == [0, 0]
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait()
    assert all(p.stat().st_size <= 2048 for p in (tmp_path / "diagnostics").glob("*.jsonl*"))
    if os.name != "nt":
        import fcntl

        with (tmp_path / "diagnostics" / "worker-memory.lock").open("r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            observer = Observer(tmp_path, synchronous=True, sink=sink)
            assert observer.lost == 1
            observer.close()
        target = tmp_path / "untouched"
        target.write_text("PRIVATE")
        active = tmp_path / "diagnostics" / "worker-memory.jsonl"
        active.unlink()
        active.symlink_to(target)
        observer = Observer(tmp_path, synchronous=True, sink=sink)
        assert observer.lost == 1 and target.read_text() == "PRIVATE"
        observer.close()


def test_blocked_writer_queue_and_context_caps(tmp_path):
    from mempalace.worker_observation import Observer

    entered, release, all_entered = threading.Event(), threading.Event(), threading.Barrier(65)

    def sink(line):
        entered.set()
        assert release.wait(5), "invalid blocked-writer harness"

    observer = Observer(tmp_path, sink=sink)
    workers = []
    scopes_release = threading.Event()

    def hold():
        with observer.scope("mempalace_status"):
            all_entered.wait(timeout=5)
            assert scopes_release.wait(5), "invalid context harness"

    try:
        assert entered.wait(5)
        workers = [threading.Thread(target=hold) for _ in range(64)]
        for worker in workers:
            worker.start()
        all_entered.wait(timeout=5)
        assert observer.active == 64
        before = observer.lost
        for _ in range(200):
            with observer.scope("mempalace_status") as token:
                assert token is None
        assert observer.lost == before + 200
        scopes_release.set()
        for worker in workers:
            worker.join(timeout=5)
            assert not worker.is_alive()
        for _ in range(100):
            with observer.scope("mempalace_status"):
                pass
        assert observer.queue.qsize() == 128
        assert observer.active == 0 and observer.lost > 200
        assert all(type(line) is bytes and len(line) <= 4096 for line in observer.queue.queue)
    finally:
        scopes_release.set()
        release.set()
        for worker in workers:
            worker.join(timeout=5)
        observer.close()


def test_factory_object_is_not_a_native_session(serving, monkeypatch):
    from mempalace.embedding import EmbeddinggemmaONNX

    server, invoke, records, _ = serving

    def handler(**kwargs):
        function = EmbeddinggemmaONNX()
        assert function._session is None
        return {}

    monkeypatch.setitem(server.TOOLS["mempalace_status"], "handler", handler)
    invoke()
    assert records()[-1]["model_session_create_count"] is None


def test_frozen_tool_labels_match_reviewable_schema_source():
    import ast
    from pathlib import Path

    from mempalace.worker_observation import TOOL_NAMES

    tree = ast.parse((Path(__file__).parents[1] / "mempalace/mcp_server/schemas.py").read_text())
    assignment = next(
        n
        for n in tree.body
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "TOOLS" for t in n.targets)
    )
    names = {ast.literal_eval(k) for k in assignment.value.keys}
    assert TOOL_NAMES == names | {
        "unknown",
        "internal/write_mirror_units",
        "internal/read_associations",
    }


def test_failure_loss_and_saturation_do_not_escape(tmp_path):
    from mempalace.worker_observation import MAX_U, Observer

    delivered = []

    def broken(line):
        raise OSError("PRIVATE-EXCEPTION")

    observer = Observer(tmp_path, synchronous=True, sink=broken)
    try:
        assert observer.lost == 1
        observer.sink = delivered.append
        with observer.scope("mempalace_status") as token:
            token.facts["not_allowlisted"] = "PRIVATE-CONTENT"
        assert observer.lost == 2
        assert all(b"PRIVATE" not in line for line in delivered)
        observer.lost = MAX_U
        observer.loss()
        assert observer.lost == MAX_U
        observer.ids = MAX_U
        with observer.scope("mempalace_status") as token:
            assert token is None
        assert observer.active == 0
    finally:
        observer.close()


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_pairing_oracle_on_unsupported_rss_platform(serving, monkeypatch, platform):
    from mempalace import worker_observation
    from tests.test_worker_observation import test_default_paired_records_use_generated_ids

    monkeypatch.setattr(worker_observation.sys, "platform", platform)
    test_default_paired_records_use_generated_ids(serving)
