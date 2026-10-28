"""Meaningful benchmark checks: read-only access, holdout, metrics and resumes."""

import json
import sqlite3
from argparse import Namespace

import pytest

from mempalace.room_benchmark import (
    _pick_real_rows,
    _row_fingerprint,
    audit_snapshot,
    metrics,
    prepare,
    read_palace,
    report,
    run,
)
from mempalace.rooms import RoomSet, RoomSpec


def test_sqlite_reader_cannot_modify_or_create_palace(tmp_path):
    with sqlite3.connect(tmp_path / "sqlite_exact.sqlite3") as con:
        con.execute("CREATE TABLE sentinel (value TEXT)")
        con.execute("INSERT INTO sentinel VALUES ('unchanged')")
    before = (tmp_path / "sqlite_exact.sqlite3").read_bytes()
    with read_palace(tmp_path) as con:
        assert con.execute("PRAGMA query_only").fetchone() == (1,)
        with pytest.raises(sqlite3.OperationalError):
            con.execute("UPDATE sentinel SET value='changed'")
    assert (tmp_path / "sqlite_exact.sqlite3").read_bytes() == before
    missing = tmp_path / "missing"
    missing.mkdir()
    with pytest.raises(sqlite3.OperationalError):
        read_palace(missing)
    assert not (missing / "sqlite_exact.sqlite3").exists()


def test_real_sampling_holds_out_whole_exemplar_sources():
    room_set = RoomSet("w", [RoomSpec("a", "A"), RoomSpec("b", "B")])
    metadata = [
        (str(i), json.dumps({"room": "a" if i % 2 else "b", "source_file": f"s{i // 2}"}))
        for i in range(20)
    ]
    exemplars = [{"id": "0", "metadata": {"source_file": "s0"}}]
    picked = _pick_real_rows(metadata, room_set, exemplars, 10, 7)
    assert len(picked) == 10
    assert not set(picked) & {"0", "1"}
    assert picked == _pick_real_rows(metadata, room_set, exemplars, 10, 7)


def test_metrics_distinguish_truth_reference_rejects_errors_and_scales():
    rows = [
        {"id": "a", "kind": "controlled", "expected_room": "x", "language": "en"},
        {"id": "b", "kind": "controlled", "expected_room": "", "language": "pt"},
        {"id": "c", "kind": "controlled", "expected_room": "x", "language": "pt"},
        {"id": "d", "kind": "observational", "reference_room": "y"},
    ]
    predictions = [
        {"id": "a", "room": "x", "score": 0.95, "score_kind": "model_probability", "seconds": 1},
        {"id": "b", "room": "", "score": 0.9, "score_kind": "model_probability", "seconds": 2},
        {"id": "c", "error": "overflow", "seconds": 3},
        {"id": "d", "room": "y", "score": 0.8, "score_kind": "model_probability", "seconds": 4},
    ]
    for prediction in predictions:
        if "score" in prediction:
            prediction["probabilities"] = {
                prediction["room"] or "__none__": prediction["score"],
                "alternative": 1 - prediction["score"],
            }
    result = metrics(rows, predictions)
    assert result["controlled_accuracy"] == pytest.approx(2 / 3)
    assert result["reference_agreement"] == 1
    assert result["errors"] == 1
    assert result["rejects"] == 1
    assert result["p95_seconds"] == 4
    selected = next(r for r in result["selective"] if r["threshold"] == 0.9)
    assert selected["precision"] == 1
    assert selected["coverage"] == pytest.approx(1 / 3)
    assert result["by_language"]["pt"]["accuracy"] == 0.5
    assert result["in_taxonomy_accuracy"] == 0.5
    assert result["reject_accuracy"] == 1
    assert result["calibration"]["cases"] == 2
    assert result["calibration"]["ece_10_bins"] == pytest.approx(0.075)


@pytest.mark.parametrize("algorithm", ["embeddings", "embedding-descriptions"])
def test_embedding_runner_is_resumable_without_opening_palace(tmp_path, monkeypatch, algorithm):
    data = {
        "embedding_identity": {"model_name": "fake", "dimension": 2},
        "prototypes": {"x": [1, 0], "y": [0, 1]},
        "room_set": {
            "wing": "w",
            "rooms": [{"name": "x", "description": "X"}, {"name": "y", "description": "Y"}],
        },
        "rows": [
            {"id": "a", "kind": "controlled", "document": "private sentence", "embedding": [1, 0]},
            {
                "id": "b",
                "kind": "controlled",
                "document": "another private sentence",
                "embedding": [0, 1],
            },
        ],
    }
    dataset = tmp_path / "dataset.json"
    dataset.write_text(json.dumps(data))
    output = tmp_path / "results"
    args = Namespace(
        dataset=str(dataset),
        output=str(output),
        algorithm=algorithm,
        endpoint="http://127.0.0.1:11434",
        timeout=1,
        limit=1,
    )
    monkeypatch.setattr(
        "mempalace.room_benchmark.read_palace",
        lambda *a: pytest.fail("inference cannot open palace"),
    )

    class Embed:
        def embed_query(self, texts):
            return [[1, 0], [0, 1]]

    monkeypatch.setattr("mempalace.embedding.get_embedding_function", lambda **kwargs: Embed())
    run(args)
    args.limit = 0
    run(args)
    result = json.loads((output / (algorithm + ".json")).read_text())
    assert [r["room"] for r in result["predictions"]] == ["x", "y"]
    assert result["complete"]
    assert "private sentence" not in (output / (algorithm + ".json")).read_text()
    dataset.write_text(json.dumps({**data, "changed": True}))
    with pytest.raises(ValueError, match="Resume refused"):
        run(args)


def test_row_fingerprint_detects_room_and_content_drift():
    row = {"id": "a", "document": "words", "metadata": {"room": "old"}, "embedding": [1, 0]}
    assert _row_fingerprint(row) != _row_fingerprint({**row, "metadata": {"room": "new"}})
    assert _row_fingerprint(row) != _row_fingerprint({**row, "document": "edited"})


def test_audit_detects_drift_in_read_only_sample(monkeypatch):
    from contextlib import nullcontext

    row = {"id": "a", "document": "words", "metadata": {"room": "old"}, "embedding": [1, 0]}
    monkeypatch.setattr("mempalace.room_benchmark.read_palace", lambda *a: nullcontext(None))
    monkeypatch.setattr(
        "mempalace.room_benchmark._fetch_rows", lambda *a: [{**row, "metadata": {"room": "new"}}]
    )
    data = {
        "palace_path": "/unused",
        "rows": [{**row, "kind": "observational", "fingerprint": _row_fingerprint(row)}],
    }
    assert audit_snapshot(data)["changed_or_missing"] == 1


def _small_palace(tmp_path):
    import numpy as np

    palace = tmp_path / "palace"
    (palace / "rooms").mkdir(parents=True)
    rooms = [{"name": "bugs", "description": "Bug reports", "exemplars": ["example"]}]
    (palace / "rooms" / "w.json").write_text(json.dumps({"wing": "w", "rooms": rooms}))
    (palace / "mempalace_embedder.json").write_text(
        json.dumps({"mempalace_drawers": {"model_name": "minilm", "dimension": 2}})
    )
    with sqlite3.connect(palace / "sqlite_exact.sqlite3") as con:
        con.execute("CREATE TABLE collections (id INTEGER, name TEXT)")
        con.execute("INSERT INTO collections VALUES (1, 'mempalace_drawers')")
        con.execute(
            "CREATE TABLE documents (collection_id INTEGER, id TEXT, document TEXT, metadata_json TEXT, embedding BLOB, wing TEXT)"
        )
        for key, source in [
            ("example", "training"),
            ("same-source", "training"),
            ("heldout", "evaluation"),
        ]:
            metadata = {"wing": "w", "room": "bugs", "source_file": source}
            con.execute(
                "INSERT INTO documents VALUES (1, ?, ?, ?, ?, 'w')",
                (
                    key,
                    "Exact\nprivate words",
                    json.dumps(metadata),
                    np.asarray([1, 0], dtype=np.float32).tobytes(),
                ),
            )
    cases = tmp_path / "cases.json"
    cases.write_text(
        json.dumps(
            {
                "rooms": [{"name": "bugs", "description": "Bug reports"}],
                "cases": [
                    {
                        "id": "controlled-a",
                        "text": "A bug",
                        "expected_room": "bugs",
                        "language": "en",
                    }
                ],
            }
        )
    )
    return palace, cases


def test_prepare_snapshots_exact_words_and_excludes_training_sources(tmp_path, monkeypatch):
    palace, cases = _small_palace(tmp_path)
    monkeypatch.setattr(
        "mempalace.embedding.get_embedding_function",
        lambda **kwargs: lambda texts: [[1, 0] for _ in texts],
    )
    before = (palace / "sqlite_exact.sqlite3").read_bytes()
    output = tmp_path / "pilot" / "dataset.json"
    args = Namespace(
        palace=str(palace), output=str(output), wing="w", cases=str(cases), real_limit=2, seed=0
    )
    prepare(args)
    data = json.loads(output.read_text())
    assert [row["id"] for row in data["rows"]] == ["heldout", "controlled-a"]
    assert data["rows"][0]["document"] == "Exact\nprivate words"
    assert (palace / "sqlite_exact.sqlite3").read_bytes() == before
    assert audit_snapshot(data)["changed_or_missing"] == 0
    assert data["prototype_source"]["bugs"] == "centroid of 1"
    with pytest.raises(FileExistsError):
        prepare(args)
    args.output = str(palace / "forbidden.json")
    with pytest.raises(ValueError, match="outside"):
        prepare(args)


def test_prepare_refuses_changed_taxonomy(tmp_path, monkeypatch):
    palace, cases = _small_palace(tmp_path)
    content = json.loads(cases.read_text())
    content["rooms"][0]["description"] = "A changed definition"
    cases.write_text(json.dumps(content))
    args = Namespace(
        palace=str(palace),
        output=str(tmp_path / "dataset.json"),
        wing="w",
        cases=str(cases),
        real_limit=2,
        seed=0,
    )
    with pytest.raises(ValueError, match="taxonomy differs"):
        prepare(args)


def test_model_runner_warms_up_resumes_and_counts_failures(tmp_path, monkeypatch):
    class Client:
        def __init__(self, *args):
            pass

        def request(self, path):
            return {"version": "test"} if path == "/api/version" else {"models": []}

        def inspect(self, *args):
            return {"digest": "abc"}

        def unload(self, model):
            unloaded.append(model)

    from mempalace.room_decision import RoomPrediction

    class Classifier:
        def __init__(self, *args):
            pass

        def predict(self, row):
            called.append(row["id"])
            if row["id"] == "overflow":
                raise RuntimeError("Overflow")
            return RoomPrediction("bugs", 0.9, "model_probability", {"bugs": 0.9, "__none__": 0.1})

    called, unloaded = [], []
    monkeypatch.setattr("mempalace.room_benchmark.LocalOllama", Client)
    monkeypatch.setattr("mempalace.room_benchmark.DecisionRoomDecider", Classifier)
    data = {
        "room_set": {"wing": "w", "rooms": [{"name": "bugs", "description": "Bugs"}]},
        "rows": [
            {"id": "overflow", "kind": "observational"},
            {"id": "controlled-a", "kind": "controlled", "language": "en", "expected_room": "bugs"},
        ],
    }
    dataset = tmp_path / "dataset.json"
    dataset.write_text(json.dumps(data))
    args = Namespace(
        dataset=str(dataset),
        output=str(tmp_path / "results"),
        algorithm="d3",
        endpoint="http://127.0.0.1:11434",
        timeout=1,
        limit=0,
    )
    run(args)
    assert called == ["controlled-a", "overflow", "controlled-a"]
    run(args)
    assert len(called) == 3
    assert unloaded == ["d3:4b", "d3:4b"]
    result = json.loads((tmp_path / "results" / "d3.json").read_text())
    assert result["complete"]
    assert result["predictions"][0]["error"] == "Overflow"


def test_report_rejects_results_from_another_dataset(tmp_path, monkeypatch):
    dataset = tmp_path / "dataset.json"
    dataset.write_text('{"rows": []}')
    (tmp_path / "embeddings.json").write_text(
        json.dumps({"provenance": {"dataset_sha256": "wrong"}})
    )
    monkeypatch.setattr("mempalace.room_benchmark.audit_snapshot", lambda data: {})
    with pytest.raises(ValueError, match="hash mismatch"):
        report(Namespace(dataset=str(dataset), output=str(tmp_path)))


def test_rooms_benchmark_cli_dispatch_never_opens_live_collection(monkeypatch):
    from mempalace import cli

    calls = []
    monkeypatch.setattr("mempalace.room_benchmark.run", lambda args: calls.append(args))
    monkeypatch.setattr(
        "sys.argv",
        [
            "mempalace",
            "rooms",
            "benchmark",
            "--dataset",
            "snapshot.json",
            "--output",
            "results",
            "--algorithm",
            "d3",
        ],
    )
    cli.main()
    assert calls[0].algorithm == "d3"
    assert calls[0].dataset == "snapshot.json"
    assert not hasattr(calls[0], "yes")


def test_inference_refuses_to_publish_results_inside_live_palace(tmp_path):
    dataset = tmp_path / "dataset.json"
    palace = tmp_path / "palace"
    dataset.write_text(json.dumps({"palace_path": str(palace)}))
    args = Namespace(
        dataset=str(dataset),
        output=str(palace / "rooms"),
        algorithm="embeddings",
        endpoint="http://127.0.0.1:11434",
        timeout=1,
        limit=0,
    )
    with pytest.raises(ValueError, match="outside"):
        run(args)
    assert not palace.exists()


def test_prepare_rejects_invalid_gold_label_before_embedding(tmp_path):
    palace, cases = _small_palace(tmp_path)
    fixture = json.loads(cases.read_text())
    fixture["cases"][0]["expected_room"] = "typo-not-in-taxonomy"
    cases.write_text(json.dumps(fixture))
    args = Namespace(
        palace=str(palace),
        output=str(tmp_path / "dataset.json"),
        wing="w",
        cases=str(cases),
        real_limit=2,
        seed=0,
    )
    with pytest.raises(ValueError, match="Invalid controlled case"):
        prepare(args)
