"""Real-backend lifecycle tests. All data and model predictions are isolated."""

import json

import pytest

from mempalace import room_suggestions as s
from mempalace.config import MempalaceConfig
from mempalace.room_decision import RoomPrediction
from mempalace.room_suggestion_store import RoomSuggestionError, SuggestionStore
from mempalace.rooms import RoomSet, RoomSpec, save_room_set


class Client:
    digest = "pinned-digest"

    def __init__(self, *args):
        pass

    def inspect(self, model, capability):
        return {
            "model": model,
            "digest": self.digest,
            "capabilities": ["decision"],
            "parameters": "num_ctx 4096",
        }


class Classifier:
    calls = []
    hook = None
    failure = None

    def __init__(self, *args):
        pass

    def predict(self, row):
        self.calls.append(row["id"])
        if self.hook is not None:
            type(self).hook(row)
        if self.failure == row["id"]:
            raise RuntimeError("Network went away")
        room = "releases" if "release" in row["document"] else "bugs"
        return RoomPrediction(room, 0.95, "model_probability", {room: 0.95, "__none__": 0.05}, 0.9)


@pytest.fixture(params=["sqlite_exact", "chroma"])
def palace(tmp_path, monkeypatch, request):
    from mempalace.palace import get_closets_collection, get_collection

    backend = request.param
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.json").write_text('{"embedding_model":"minilm"}', encoding="utf-8")
    monkeypatch.setenv("MEMPALACE_CONFIG_DIR", str(config_dir))
    path = str(tmp_path / "palace")
    col = get_collection(path, backend=backend)
    col.add(
        ids=["a", "b", "manual"],
        documents=[
            "PRIVATE ORIGINAL\nfix a bug",
            "PRIVATE ORIGINAL\nrelease the package",
            "A manually filed diary",
        ],
        metadatas=[
            {
                "wing": "w",
                "room": "general",
                "source_file": "source",
                "last_modified": "original-a",
                "custom": ["kept", "also-kept"],
            },
            {
                "wing": "w",
                "room": "general",
                "source_file": "source",
                "last_modified": "original-b",
            },
            {"wing": "w", "room": "diary", "source_file": "other"},
        ],
        embeddings=[[1.0] + [0.0] * 383] * 3,
    )
    from mempalace.backends.base import EmbedderIdentity

    col.set_embedder_identity(EmbedderIdentity("minilm", 384))
    col.close()
    closets = get_closets_collection(path, backend=backend)
    closets.add(
        ids=["index"],
        documents=["Original source index"],
        metadatas=[
            {
                "wing": "w",
                "room": "general",
                "source_file": "source",
                "last_modified": "original-index",
            }
        ],
        embeddings=[[1.0] + [0.0] * 383],
    )
    closets.close()
    config = MempalaceConfig(palace_path=path)
    save_room_set(
        config,
        RoomSet("w", [RoomSpec("bugs", "Bug fixes"), RoomSpec("releases", "Release management")]),
    )
    store = SuggestionStore(path, root=tmp_path / "private-state")
    # Let child-style Store construction resolve to the same explicit root in tests.
    Classifier.calls = []
    Classifier.hook = None
    Classifier.failure = None
    Client.digest = "pinned-digest"
    monkeypatch.setattr(s, "LocalOllama", Client)
    monkeypatch.setattr(s, "DecisionRoomDecider", Classifier)
    return path, backend, store


def start(palace, **kwargs):
    _path, backend, store = palace
    return s.create_job(store, wing="w", model="d3:4b", backend=backend, **kwargs)


def content(palace, *, closets=False):
    from mempalace.palace import get_closets_collection, get_collection

    path, backend, _store = palace
    col = (get_closets_collection if closets else get_collection)(
        path, backend=backend, create=False
    )
    try:
        batch = col.get(include=["documents", "metadatas", "embeddings"])
        return {
            key: {"document": doc, "metadata": meta, "embedding": list(map(float, emb))}
            for key, doc, meta, emb in zip(
                batch["ids"], batch["documents"], batch["metadatas"], batch["embeddings"]
            )
        }
    finally:
        col.close()


def ready(palace, **kwargs):
    job = start(palace, **kwargs)
    s.run_worker(palace[2], job)
    assert palace[2].job(job)["state"] == "ready"
    return job


def test_inference_and_preview_never_move_drawers_or_copy_private_documents(palace):
    store = palace[2]
    before = content(palace)
    job = ready(palace)
    assert set(Classifier.calls) == {"a", "b"}
    assert content(palace) == before
    shown = s.preview(store, job)
    assert shown["counts"] == {"suggested": 2}
    assert shown["items"][0]["excerpt"] == before[shown["items"][0]["id"]]["document"]
    assert not any(item["stale"] for item in shown["items"])
    assert b"PRIVATE ORIGINAL" not in store.path.read_bytes()
    assert s.apply(store, job)["dry_run"]
    assert content(palace) == before
    assert store.moves(job) == []


def test_explicit_apply_and_undo_preserve_verbatim_text_vectors_and_custom_metadata(palace):
    store = palace[2]
    before, before_closets = content(palace), content(palace, closets=True)
    job = ready(palace)
    result = s.apply(store, job, yes=True)
    assert result["drawers"] == 2
    # The two drawers chose different destinations, so their shared closet stays.
    assert result["closets"] == 0
    after = content(palace)
    assert after["a"]["metadata"]["room"] == "bugs"
    assert after["b"]["metadata"]["room"] == "releases"
    assert after["manual"] == before["manual"]
    for key in before:
        assert after[key]["document"] == before[key]["document"]
        assert after[key]["embedding"] == before[key]["embedding"]
    assert after["a"]["metadata"]["custom"] == ["kept", "also-kept"]
    assert not any(item["stale"] for item in s.preview(store, job)["items"])
    assert s.apply(store, job, yes=True)["already_complete"]
    assert s.apply(store, job, undo=True)["dry_run"]
    assert content(palace) == after
    assert s.apply(store, job, yes=True, undo=True)["state"] == "undone"
    assert content(palace) == before
    assert content(palace, closets=True) == before_closets
    assert s.apply(store, job, yes=True, undo=True)["already_complete"]
    assert not any(item["stale"] for item in s.preview(store, job)["items"])


def test_closet_follows_only_when_all_source_drawers_move_to_one_room(palace, monkeypatch):
    class SameRoom(Classifier):
        def predict(self, row):
            return RoomPrediction("bugs", 0.99, "model_probability")

    monkeypatch.setattr(s, "DecisionRoomDecider", SameRoom)
    before, before_closets = content(palace), content(palace, closets=True)
    job = ready(palace)
    assert s.apply(palace[2], job, yes=True)["closets"] == 1
    assert content(palace, closets=True)["index"]["metadata"]["room"] == "bugs"
    s.apply(palace[2], job, yes=True, undo=True)
    assert content(palace) == before
    assert content(palace, closets=True) == before_closets


def test_selected_subset_does_not_move_unselected_drawers_or_shared_closet(palace):
    before, before_closets = content(palace), content(palace, closets=True)
    job = ready(palace)
    s.apply(palace[2], job, ids=["a"], yes=True)
    assert content(palace)["b"] == before["b"]
    assert content(palace, closets=True) == before_closets
    s.apply(palace[2], job, yes=True, undo=True)
    assert content(palace) == before


def test_apply_refuses_stale_content_before_any_writes(palace):
    from mempalace.palace import get_collection

    path, backend, store = palace
    job = ready(palace)
    col = get_collection(path, backend=backend)
    col.update(ids=["b"], metadatas=[{"note": "a later user edit"}])
    col.close()
    edited = content(palace)
    with pytest.raises(RoomSuggestionError, match="changed"):
        s.apply(store, job, yes=True)
    assert content(palace) == edited
    assert store.moves(job) == []
    assert any(item["stale"] for item in s.preview(store, job)["items"])


def test_undo_refuses_later_edits_without_overwriting_any_other_row(palace):
    from mempalace.palace import get_collection

    path, backend, store = palace
    job = ready(palace)
    s.apply(store, job, yes=True)
    col = get_collection(path, backend=backend)
    col.update(ids=["b"], metadatas=[{"note": "a later edit"}])
    col.close()
    edited = content(palace)
    with pytest.raises(RoomSuggestionError, match="edited"):
        s.apply(store, job, yes=True, undo=True)
    assert content(palace) == edited


def test_apply_and_undo_recover_a_committed_write_with_lost_acknowledgment(palace, monkeypatch):
    store = palace[2]
    before = content(palace)
    job = ready(palace)
    original = store.mark_move

    def fail_ack(*args, **kwargs):
        raise RuntimeError("Simulated death after backend commit")

    monkeypatch.setattr(store, "mark_move", fail_ack)
    with pytest.raises(RuntimeError, match="Simulated"):
        s.apply(store, job, yes=True)
    assert store.job(job)["state"] == "applying"
    assert content(palace)["a"]["metadata"]["room"] == "bugs"
    monkeypatch.setattr(store, "mark_move", original)
    s.apply(store, job, yes=True)
    monkeypatch.setattr(store, "mark_move", fail_ack)
    with pytest.raises(RuntimeError, match="Simulated"):
        s.apply(store, job, yes=True, undo=True)
    assert store.job(job)["state"] == "undoing"
    monkeypatch.setattr(store, "mark_move", original)
    s.apply(store, job, yes=True, undo=True)
    assert content(palace) == before


def test_partial_apply_can_be_undone_without_finishing_the_apply(palace, monkeypatch):
    store = palace[2]
    before = content(palace)
    job = ready(palace)
    original = store.mark_move
    monkeypatch.setattr(
        store, "mark_move", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("stop"))
    )
    with pytest.raises(RuntimeError):
        s.apply(store, job, yes=True)
    monkeypatch.setattr(store, "mark_move", original)
    s.apply(store, job, yes=True, undo=True)
    assert content(palace) == before
    with pytest.raises(RoomSuggestionError):
        s.apply(store, job, yes=True)


def test_source_changes_invalidate_apply_but_do_not_prevent_undo(palace):
    from mempalace.rooms import room_set_path

    path, _backend, store = palace
    before = content(palace)
    job = ready(palace)
    config = MempalaceConfig(palace_path=path)
    filename = room_set_path(config, "w")
    original = open(filename, encoding="utf-8").read()
    with open(filename, "a", encoding="utf-8") as stream:
        stream.write("\n")
    with pytest.raises(RoomSuggestionError, match="definitions changed"):
        s.apply(store, job, yes=True)
    with open(filename, "w", encoding="utf-8") as stream:
        stream.write(original)
    s.apply(store, job, yes=True)
    import os

    os.unlink(filename)
    s.apply(store, job, yes=True, undo=True)
    assert content(palace) == before


def test_cancellation_resume_and_cache_never_write_palace(palace):
    store = palace[2]
    before = content(palace)
    job = start(palace)
    Classifier.hook = lambda row: s.cancel(store, job)
    s.run_worker(store, job)
    assert store.job(job)["state"] == "paused"
    assert len(Classifier.calls) == 1
    Classifier.hook = None
    s.resume(store, job, foreground=True)
    assert store.job(job)["state"] == "ready"
    assert len(Classifier.calls) == 2
    another = ready(palace)
    assert s.status(store, another)["cache_hits"] == 2
    assert len(Classifier.calls) == 2
    assert content(palace) == before


def test_failed_inference_resumes_pending_rows_and_pins_model(palace):
    store = palace[2]
    before = content(palace)
    job = start(palace)
    Classifier.failure = "b"
    s.run_worker(store, job)
    assert store.job(job)["state"] == "failed"
    assert [item["outcome"] for item in store.items(job)] == ["suggested", "pending"]
    Client.digest = "a different model"
    Classifier.failure = None
    s.resume(store, job, foreground=True)
    assert store.job(job)["state"] == "failed"
    assert "model changed" in store.job(job)["error"]
    Client.digest = "pinned-digest"
    s.resume(store, job, foreground=True)
    assert store.job(job)["state"] == "ready"
    assert content(palace) == before


def test_active_worker_excludes_duplicate_worker_and_resume(palace):
    store = palace[2]
    job = start(palace)
    with s.worker_lock(store, job):
        with pytest.raises(s.SuggestionBusy):
            s.resume(store, job)
        s.run_worker(store, job)
        assert not Classifier.calls
    s.run_worker(store, job)
    assert set(Classifier.calls) == {"a", "b"}


def test_writer_lease_is_not_held_during_model_calls(palace):
    from mempalace.palace import mine_palace_lock

    path, _backend, store = palace
    job = start(palace)

    def prove_writer_free(row):
        with mine_palace_lock(path):
            pass

    Classifier.hook = prove_writer_free
    s.run_worker(store, job)
    assert store.job(job)["state"] == "ready"


def test_apply_invalid_selection_and_missing_original_are_safe(palace):
    store = palace[2]
    before = content(palace)
    job = ready(palace)
    with pytest.raises(RoomSuggestionError, match="Select"):
        s.apply(store, job, ids=["manual"], yes=True)
    assert content(palace) == before


def test_unavailable_model_does_not_change_or_download_anything(palace, monkeypatch):
    from mempalace.llm_client import LLMError

    class Unavailable(Client):
        def inspect(self, *args):
            raise LLMError("No installed digest")

    monkeypatch.setattr(s, "LocalOllama", Unavailable)
    before = content(palace)
    job = start(palace)
    s.run_worker(palace[2], job)
    assert palace[2].job(job)["state"] == "failed"
    assert content(palace) == before


@pytest.mark.parametrize(
    "kwargs", [{"threshold": float("nan")}, {"threshold": 2}, {"limit": 0}, {"timeout": 0}]
)
def test_invalid_resource_settings_are_refused(palace, kwargs):
    with pytest.raises(RoomSuggestionError):
        start(palace, **kwargs)


def test_start_default_is_background_and_apply_default_is_preview(palace, monkeypatch, capsys):
    from mempalace import cli

    path, backend, _store = palace
    # cli.main deliberately sets these for its process. Restore them after this
    # embedded invocation so a failing test cannot change later tests' backend.
    monkeypatch.setenv("MEMPALACE_BACKEND_EXPLICIT", backend)
    monkeypatch.setenv("MEMPALACE_BACKEND", backend)
    # This CLI uses the normal per-palace state directory (isolated HOME).
    launched = []
    monkeypatch.setattr(s, "launch", lambda store, job: launched.append((store, job)))
    monkeypatch.setattr(
        "sys.argv",
        [
            "mempalace",
            "--palace",
            path,
            "--backend",
            backend,
            "rooms",
            "suggestions",
            "start",
            "--wing",
            "w",
            "--model",
            "d3:4b",
            "--json",
        ],
    )
    cli.main()
    result = json.loads(capsys.readouterr().out)
    assert result["state"] == "queued"
    assert len(launched) == 1
    store, job = launched[0]
    assert not Classifier.calls
    s.run_worker(store, job)
    before = content(palace)
    monkeypatch.setattr(
        "sys.argv", ["mempalace", "--palace", path, "rooms", "suggestions", "apply", job, "--json"]
    )
    cli.main()
    assert json.loads(capsys.readouterr().out)["dry_run"]
    assert content(palace) == before


def test_detached_worker_cancel_and_resume_with_local_http_model(palace):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from mempalace.palace import mine_palace_lock

    path, backend, _ = palace
    store = SuggestionStore(path)
    entered, allow = threading.Event(), threading.Event()
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def send_json(self, data):
            body = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self.send_json({"models": [{"name": "test-model", "digest": "local-test-digest"}]})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/api/show":
                self.send_json({"capabilities": ["decision"], "parameters": "num_ctx 4096"})
                return
            assert self.path == "/v1/systemone"
            requests.append(body)
            entered.set()
            allow.wait(10)
            self.send_json(
                {
                    "answers": {
                        "room": {
                            "type": "choice",
                            "choice": "bugs",
                            "probabilities": {"bugs": 0.96, "releases": 0.02, "__none__": 0.02},
                        }
                    }
                }
            )

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    before = content(palace)
    process = None
    try:
        job = s.create_job(
            store,
            wing="w",
            model="test-model",
            backend=backend,
            endpoint=f"http://127.0.0.1:{server.server_port}",
        )
        # The independent inference process must progress while the actual
        # palace writer lease is held by this process.
        with mine_palace_lock(path):
            process = s.launch(store, job)
            assert entered.wait(15), s.status(store, job)
            assert s.status(store, job)["state"] == "running"
            s.cancel(store, job)
            assert store.job(job)["state"] == "cancelling"
            allow.set()
            assert process.wait(timeout=15) == 0
        assert store.job(job)["state"] == "paused"
        assert len(requests) == 1
        process = s.resume(store, job)
        assert process.wait(timeout=15) == 0
        assert store.job(job)["state"] == "ready"
        assert s.status(store, job)["completed"] == 2
        assert len(requests) == 2
        assert content(palace) == before
    finally:
        allow.set()
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
        server.shutdown()
        server.server_close()
        thread.join()


def test_preview_pages_cover_every_record_and_reject_invalid_offsets(palace):
    store = palace[2]
    job = ready(palace)
    first = s.preview(store, job, limit=1)
    second = s.preview(store, job, limit=1, offset=first["next_offset"])
    assert first["items"][0]["id"] != second["items"][0]["id"]
    assert second["next_offset"] is None
    assert s.preview(store, job, offset=10)["items"] == []
    with pytest.raises(RoomSuggestionError):
        s.preview(store, job, offset=-1)


def test_cancel_queued_job_and_explicitly_include_manually_filed_rooms(palace):
    store = palace[2]
    job = start(palace, from_rooms="all")
    s.cancel(store, job)
    assert store.job(job)["state"] == "paused"
    assert not Classifier.calls
    s.resume(store, job, foreground=True)
    assert set(Classifier.calls) == {"a", "b", "manual"}


def test_competing_job_cannot_partially_publish_apply_journal(palace):
    store = palace[2]
    job = ready(palace)
    other = start(palace)
    before = content(palace)
    with pytest.raises(RoomSuggestionError, match="Another suggestion operation"):
        s.apply(store, job, yes=True)
    assert store.job(job)["state"] == "ready"
    assert store.moves(job) == []
    assert content(palace) == before
    s.cancel(store, other)
    assert s.apply(store, job, yes=True)["state"] == "applied"


def test_saved_invalid_prediction_and_new_selection_cannot_move_drawers(palace):
    store = palace[2]
    job = ready(palace)
    before = content(palace)
    store.prediction(job, "a", "suggested", {"room": "invented", "score": 0.99})
    with pytest.raises(RoomSuggestionError, match="invalid"):
        s.apply(store, job, yes=True)
    assert content(palace) == before
    s.apply(store, job, ids=["b"], yes=True)
    with pytest.raises(RoomSuggestionError, match="original reviewed selection"):
        s.apply(store, job, ids=["a"], yes=True)


def test_resume_refuses_changed_rubric_and_collection(palace, monkeypatch):
    store = palace[2]
    job = start(palace)
    with monkeypatch.context() as patch:
        patch.setattr(s, "RUBRIC_VERSION", "new-rubric")
        s.run_worker(store, job)
    assert "rubric changed" in store.job(job)["error"]
    config = MempalaceConfig()
    config._file_config["collection_name"] = "other_drawers"
    (config.config_dir / "config.json").write_text(
        json.dumps(config._file_config), encoding="utf-8"
    )
    s.resume(store, job, foreground=True)
    assert "collection changed" in store.job(job)["error"]
    assert not Classifier.calls


def test_worker_launch_failure_is_recoverable_without_inference(palace, monkeypatch):
    store = palace[2]
    job = start(palace)

    def fail(*args, **kwargs):
        raise OSError("cannot fork")

    monkeypatch.setattr(s.subprocess, "Popen", fail)
    with pytest.raises(RoomSuggestionError, match="could not start"):
        s.launch(store, job)
    assert store.job(job)["state"] == "failed"
    assert not Classifier.calls
    s.resume(store, job, foreground=True)
    assert store.job(job)["state"] == "ready"


def test_cli_review_apply_undo_and_status_are_usable_as_json(palace, monkeypatch, capsys):
    from mempalace import cli

    path, backend, _ = palace
    monkeypatch.setenv("MEMPALACE_BACKEND_EXPLICIT", backend)
    monkeypatch.setenv("MEMPALACE_BACKEND", backend)

    def command(*args):
        monkeypatch.setattr(
            "sys.argv", ["mempalace", "--palace", path, "rooms", "suggestions", *args, "--json"]
        )
        cli.main()
        return json.loads(capsys.readouterr().out)

    job = command("start", "--wing", "w", "--model", "d3:4b", "--foreground")["id"]
    assert command("list")[0]["id"] == job
    assert command("status", job)["state"] == "ready"
    assert command("preview", job, "--limit", "1")["next_offset"] == 1
    assert command("apply", job)["dry_run"]
    assert command("apply", job, "--ids", "a", "--yes")["state"] == "applied"
    assert command("undo", job)["dry_run"]
    assert command("undo", job, "--yes")["state"] == "undone"


def test_undo_refuses_a_closet_move_after_new_source_drawers_arrive(palace, monkeypatch):
    from mempalace.palace import get_collection

    class SameRoom(Classifier):
        def predict(self, row):
            return RoomPrediction("bugs", 0.99, "model_probability")

    monkeypatch.setattr(s, "DecisionRoomDecider", SameRoom)
    path, backend, store = palace
    job = ready(palace)
    assert s.apply(store, job, yes=True)["closets"] == 1
    col = get_collection(path, backend=backend, create=False)
    col.add(
        ids=["arrived-later"],
        documents=["More original content from the same source"],
        metadatas=[{"wing": "w", "room": "bugs", "source_file": "source"}],
        embeddings=[[1.0] + [0.0] * 383],
    )
    col.close()
    before, before_closets = content(palace), content(palace, closets=True)
    with pytest.raises(RoomSuggestionError, match="source membership changed"):
        s.apply(store, job, yes=True, undo=True)
    assert content(palace) == before
    assert content(palace, closets=True) == before_closets


def test_conflicted_partial_apply_keeps_its_journal_without_blocking_new_jobs(palace, monkeypatch):
    from mempalace.palace import get_collection

    path, backend, store = palace
    job = ready(palace)
    with monkeypatch.context() as patch:
        patch.setattr(
            store, "mark_move", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("lost ack"))
        )
        with pytest.raises(RuntimeError):
            s.apply(store, job, yes=True)
    col = get_collection(path, backend=backend, create=False)
    col.update(ids=["b"], metadatas=[{"note": "later user edit"}])
    col.close()
    before = content(palace)
    for undo in (False, True):
        with pytest.raises(RoomSuggestionError, match="edited"):
            s.apply(store, job, yes=True, undo=undo)
        assert store.job(job)["state"] == "apply_conflict"
        assert content(palace) == before
    assert store.moves(job)
    other = start(palace)
    assert store.job(other)["state"] == "queued"
