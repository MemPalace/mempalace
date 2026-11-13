"""Reproducible room-classifier pilot, with no palace mutation path.

``prepare`` reads SQLite through mode=ro + query_only and snapshots selected
drawers privately. ``run`` only reads that snapshot. No product collection is
opened, and neither command calls room-application functions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import sqlite3
import statistics
import sys
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path

from .room_decision import (
    RUBRIC_VERSION,
    ChatRoomBaseline,
    DecisionRoomDecider,
    LocalOllama,
    RoomPrediction,
    input_hash,
)
from .rooms import EmbeddingRoomDecider, RoomSet, _cosine

ALGORITHMS = {"d3": "d3:4b", "d1": "d1:3b", "qwen": "qwen3.5:4b"}
EMBEDDING_ALGORITHMS = ["embeddings", "embedding-descriptions"]


def _outside_palace(output: Path, palace: Path) -> None:
    output, palace = output.resolve(), palace.resolve()
    if output == palace or palace in output.parents:
        raise ValueError("Write pilot artifacts outside the live palace directory")


def write_private(path: Path, data: dict) -> None:
    from .private_state import restrict_private

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    restrict_private(path.parent, directory=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".room-pilot-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            restrict_private(Path(name))
            json.dump(data, stream, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_palace(palace: Path):
    """Open an existing SQLite store with SQLite-enforced read-only semantics."""
    path = palace.resolve() / "sqlite_exact.sqlite3"
    con = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    con.execute("PRAGMA query_only=ON")
    return con


def _fetch_rows(con, ids: list[str]) -> list[dict]:
    import numpy as np

    rows = []
    for start in range(0, len(ids), 400):
        batch = ids[start : start + 400]
        if not batch:
            continue
        placeholders = ",".join("?" for _ in batch)
        for row in con.execute(
            f"SELECT d.id, d.document, d.metadata_json, d.embedding FROM documents d "
            f"JOIN collections c ON c.id=d.collection_id "
            f"WHERE c.name='mempalace_drawers' AND d.id IN ({placeholders}) ORDER BY d.id",
            batch,
        ):
            rows.append(
                {
                    "id": row[0],
                    "document": row[1],
                    "metadata": json.loads(row[2]),
                    "embedding": np.frombuffer(row[3], dtype=np.float32).astype(float).tolist(),
                }
            )
    return rows


def _row_fingerprint(row: dict) -> str:
    # Include all metadata, including original room, so auditing detects any drift.
    return input_hash(row)


class SnapshotCollection:
    """An in-memory, get-only collection for the existing embedding decider."""

    def __init__(self, rows):
        self.rows = {row["id"]: row for row in rows}

    def get(self, *, ids, include):
        rows = [self.rows[key] for key in ids if key in self.rows]
        return {
            "ids": [row["id"] for row in rows],
            "embeddings": [row["embedding"] for row in rows],
        }


def _pick_real_rows(metadata_rows, room_set, exemplars, limit, seed):
    excluded_ids = {row["id"] for row in exemplars}
    excluded_sources = {row["metadata"].get("source_file") for row in exemplars} - {None, ""}
    buckets = defaultdict(list)
    for key, metadata_json in metadata_rows:
        metadata = json.loads(metadata_json)
        if (
            key in excluded_ids
            or metadata.get("source_file") in excluded_sources
            or metadata.get("is_sentinel")
        ):
            continue
        buckets[str(metadata.get("room", ""))].append(key)
    rng = random.Random(seed)
    for bucket in buckets.values():
        rng.shuffle(bucket)
    chosen = []
    names = room_set.names()
    while len(chosen) < limit and any(buckets[name] for name in names):
        for name in names:
            if buckets[name] and len(chosen) < limit:
                chosen.append(buckets[name].pop())
    remainder = [key for bucket in buckets.values() for key in bucket]
    rng.shuffle(remainder)
    return chosen + remainder[: max(0, limit - len(chosen))]


def prepare(args):
    from .embedding import get_embedding_function

    started = time.perf_counter()
    palace, output = (
        Path(args.palace).expanduser().resolve(),
        Path(args.output).expanduser().resolve(),
    )
    _outside_palace(output, palace)
    if args.real_limit < 0:
        raise ValueError("Real case limit cannot be negative")
    if output.exists():
        raise FileExistsError("Refusing to overwrite an existing pilot snapshot")
    if Path(args.wing).name != args.wing or args.wing in {".", ".."}:
        raise ValueError("Wing must be a single path component")
    room_data = json.loads((palace / "rooms" / (args.wing + ".json")).read_text())
    room_set = RoomSet.from_dict(room_data, normalize_names=False)
    fixture = json.loads(Path(args.cases).read_text())
    if fixture["rooms"] != [
        {"name": room.name, "description": room.description} for room in room_set.rooms
    ]:
        raise ValueError("Controlled fixture taxonomy differs from the approved room definitions")
    allowed = {*room_set.names(), ""}
    for case in fixture["cases"]:
        if (
            not isinstance(case.get("id"), str)
            or not case["id"]
            or not isinstance(case.get("text"), str)
            or not case["text"].strip()
            or not isinstance(case.get("expected_room"), str)
            or case["expected_room"] not in allowed
        ):
            raise ValueError("Invalid controlled case or expected room")
    identity = json.loads((palace / "mempalace_embedder.json").read_text())["mempalace_drawers"]
    model = identity["model_name"]
    if model not in {"minilm", "embeddinggemma"}:
        raise ValueError("Pilot snapshot supports local minilm/embeddinggemma identities only")
    with read_palace(palace) as con:
        con.execute("BEGIN")
        metadata_rows = con.execute(
            "SELECT d.id, d.metadata_json FROM documents d JOIN collections c "
            "ON c.id=d.collection_id WHERE c.name='mempalace_drawers' AND d.wing=? ORDER BY d.id",
            (args.wing,),
        ).fetchall()
        exemplar_ids = sorted({key for room in room_set.rooms for key in room.exemplars})
        exemplars = _fetch_rows(con, exemplar_ids)
        ids = _pick_real_rows(metadata_rows, room_set, exemplars, args.real_limit, args.seed)
        real_rows = _fetch_rows(con, ids)
    rows = []
    for row in real_rows:
        rows.append(
            {
                **row,
                "kind": "observational",
                "reference_room": row["metadata"].get("room", ""),
                "fingerprint": _row_fingerprint(row),
            }
        )
    ef = get_embedding_function(model=model)
    embed_started = time.perf_counter()
    vectors = ef([case["text"] for case in fixture["cases"]])
    if len(vectors) != len(fixture["cases"]):
        raise ValueError("Missing controlled-case embeddings")
    controlled_embedding_seconds = time.perf_counter() - embed_started
    for case, vector in zip(fixture["cases"], vectors):
        rows.append(
            {
                "id": case["id"],
                "document": case["text"],
                "metadata": {},
                "embedding": list(map(float, vector)),
                "kind": "controlled",
                "expected_room": case["expected_room"],
                "language": case["language"],
                "tags": case.get("tags", []),
            }
        )
    if len({row["id"] for row in rows}) != len(rows):
        raise ValueError("Duplicate pilot case IDs")
    dimensions = {len(row["embedding"]) for row in rows + exemplars}
    if dimensions != {identity["dimension"]}:
        raise ValueError("Snapshot embedding dimensions differ from the recorded identity")
    decider = EmbeddingRoomDecider(room_set, ef, col=SnapshotCollection(exemplars))
    snapshot = {
        "schema_version": 1,
        "palace_path": str(palace),
        "wing": args.wing,
        "seed": args.seed,
        "room_set": room_data,
        "embedding_identity": identity,
        "prototype_source": decider.prototype_source,
        "prototypes": dict(zip(decider.names, decider.prototypes)),
        "source_wing_count": len(metadata_rows),
        "source_file_holdout": True,
        "controlled_embedding_seconds": controlled_embedding_seconds,
        "preparation_seconds": time.perf_counter() - started,
        "rows": rows,
    }
    write_private(output, snapshot)
    print(json.dumps({"snapshot": str(output), "counts": dict(Counter(r["kind"] for r in rows))}))


def _embedding_prediction(row, prototypes):
    scores = [
        (name, max(0.0, _cosine(row["embedding"], vector))) for name, vector in prototypes.items()
    ]
    name, score = max(scores, key=lambda item: item[1])
    return RoomPrediction(name, score, "cosine_similarity")


def run(args):
    dataset_path = Path(args.dataset).expanduser()
    data = json.loads(dataset_path.read_text())
    dataset_hash = hashlib.sha256(dataset_path.read_bytes()).hexdigest()
    output = Path(args.output).expanduser()
    if data.get("palace_path"):
        _outside_palace(output, Path(data["palace_path"]))
    if args.limit < 0:
        raise ValueError("Case limit cannot be negative")
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    client = LocalOllama(args.endpoint, args.timeout)
    model = ALGORITHMS.get(args.algorithm)
    classifier = None
    prototypes = data["prototypes"] if "prototypes" in data else {}
    provenance = {
        "algorithm": args.algorithm,
        "rubric": RUBRIC_VERSION,
        "dataset_sha256": dataset_hash,
    }
    if model:
        provenance["runtime"] = client.request("/api/version")
        provenance["model"] = client.inspect(
            model, "completion" if args.algorithm == "qwen" else "decision"
        )
        room_set = RoomSet.from_dict(data["room_set"], normalize_names=False)
        cls = ChatRoomBaseline if args.algorithm == "qwen" else DecisionRoomDecider
        classifier = cls(room_set, client, model)
        classifier._identity = provenance["model"]
        if args.algorithm == "qwen":
            provenance["chat_prompt_version"] = ChatRoomBaseline.prompt_version
    else:
        provenance["embedding_identity"] = data["embedding_identity"]
        if args.algorithm == "embedding-descriptions":
            from .embedding import get_embedding_function

            ef = get_embedding_function(model=data["embedding_identity"]["model_name"])
            room_set = RoomSet.from_dict(data["room_set"], normalize_names=False)
            texts = [room.prototype_text() for room in room_set.rooms]
            query = getattr(ef, "embed_query", None)
            vectors = query(texts) if callable(query) else ef(texts)
            if len(vectors) != len(room_set.rooms):
                raise ValueError("Missing room description embeddings")
            prototypes = {
                room.name: list(map(float, vector)) for room, vector in zip(room_set.rooms, vectors)
            }
            provenance["prototype_strategy"] = "room_description"
            provenance["prototype_sha256"] = input_hash(prototypes)
    fingerprint = input_hash(provenance)
    path = output / (args.algorithm + ".json")
    result = {
        "provenance": provenance,
        "fingerprint": fingerprint,
        "predictions": [],
        "complete": False,
    }
    if path.exists():
        result = json.loads(path.read_text())
        if result["fingerprint"] != fingerprint:
            raise ValueError("Resume refused: dataset, model or rubric has changed")
    seen = {p["id"] for p in result["predictions"]}
    rows = data["rows"][: args.limit] if args.limit else data["rows"]
    if classifier and not seen:
        started = time.perf_counter()
        warmup = next((row for row in data["rows"] if row["kind"] == "controlled"), rows[0])
        classifier.predict(warmup)
        result["cold_first_request_seconds"] = time.perf_counter() - started
        result["loaded_models"] = client.request("/api/ps").get("models", [])
        write_private(path, result)
    for index, row in enumerate(rows, 1):
        if row["id"] in seen:
            continue
        started = time.perf_counter()
        prediction = {"id": row["id"], "kind": row["kind"]}
        try:
            pred = classifier.predict(row) if classifier else _embedding_prediction(row, prototypes)
            prediction.update(pred.to_dict())
        except (RuntimeError, ValueError) as exc:
            prediction["error"] = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
        prediction["seconds"] = time.perf_counter() - started
        result["predictions"].append(prediction)
        write_private(path, result)
        if index % 25 == 0 or index == len(rows):
            print(
                json.dumps({"algorithm": args.algorithm, "done": index, "total": len(rows)}),
                flush=True,
            )
    result["complete"] = len(result["predictions"]) == len(data["rows"])
    write_private(path, result)
    if model:
        client.unload(model)


def percentile(values, fraction):
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)] if values else None


def _prediction_label(prediction):
    if prediction is None:
        return "__missing__"
    if "error" in prediction:
        return "__error__"
    return prediction.get("room") or "__none__"


def _calibration(gold, by_id):
    bins = defaultdict(list)
    brier = []
    for row in gold:
        pred = by_id.get(row["id"], {})
        if pred.get("score_kind") != "model_probability":
            continue
        expected = row["expected_room"] or "__none__"
        score = pred["score"]
        bins[min(9, int(score * 10))].append((score, pred["room"] == row["expected_room"]))
        brier.append(sum((p - (key == expected)) ** 2 for key, p in pred["probabilities"].items()))
    count = sum(len(bucket) for bucket in bins.values())
    if not count:
        return None
    return {
        "cases": count,
        "ece_10_bins": sum(
            len(bucket)
            * abs(statistics.mean(p for p, _ in bucket) - statistics.mean(ok for _, ok in bucket))
            for bucket in bins.values()
        )
        / count,
        "multiclass_brier": statistics.mean(brier),
    }


def metrics(rows, predictions):
    by_id = {p["id"]: p for p in predictions}
    gold = [row for row in rows if row["kind"] == "controlled"]
    real = [row for row in rows if row["kind"] == "observational"]
    in_taxonomy = [row for row in gold if row["expected_room"]]
    unknown = [row for row in gold if not row["expected_room"]]

    def correct(row, field):
        p = by_id.get(row["id"], {})
        return "error" not in p and "room" in p and p["room"] == row[field]

    latencies = [p["seconds"] for p in predictions]
    result = {
        "cases": len(rows),
        "completed": len(predictions),
        "errors": sum("error" in p for p in predictions),
        "controlled_cases": len(gold),
        "in_taxonomy_accuracy": sum(correct(r, "expected_room") for r in in_taxonomy)
        / len(in_taxonomy)
        if in_taxonomy
        else None,
        "reject_accuracy": sum(correct(r, "expected_room") for r in unknown) / len(unknown)
        if unknown
        else None,
        "controlled_accuracy": sum(correct(r, "expected_room") for r in gold) / len(gold)
        if gold
        else None,
        "real_cases": len(real),
        "reference_agreement": sum(correct(r, "reference_room") for r in real) / len(real)
        if real
        else None,
        "median_seconds": statistics.median(latencies) if latencies else None,
        "p95_seconds": percentile(latencies, 0.95),
        "total_seconds": sum(latencies),
        "rejects": sum(p.get("room") == "" for p in predictions),
        "by_language": {},
        "confusion": dict(
            Counter(
                (r["expected_room"] or "__none__") + " -> " + _prediction_label(by_id.get(r["id"]))
                for r in gold
                if not correct(r, "expected_room")
            )
        ),
    }
    result["calibration"] = _calibration(gold, by_id)
    result["latency_by_kind"] = {}
    for kind in ("controlled", "observational"):
        times = [p["seconds"] for p in predictions if p.get("kind") == kind]
        result["latency_by_kind"][kind] = {
            "median_seconds": statistics.median(times) if times else None,
            "p95_seconds": percentile(times, 0.95),
        }
    for language in sorted({r["language"] for r in gold}):
        subset = [r for r in gold if r["language"] == language]
        result["by_language"][language] = {
            "cases": len(subset),
            "accuracy": sum(correct(r, "expected_room") for r in subset) / len(subset),
        }
    result["selective"] = []
    score_kind = next((p.get("score_kind") for p in predictions if "score_kind" in p), None)
    thresholds = (
        [0.3, 0.4, 0.5, 0.6, 0.7]
        if score_kind == "cosine_similarity"
        else [0.5, 0.7, 0.8, 0.9, 0.95, 0.99]
    )
    if score_kind != "unscored_label":
        for threshold in thresholds:
            accepted = [
                r
                for r in gold
                if by_id.get(r["id"], {}).get("room")
                and (by_id[r["id"]].get("score") or 0) >= threshold
            ]
            result["selective"].append(
                {
                    "threshold": threshold,
                    "score_kind": score_kind,
                    "accepted": len(accepted),
                    "coverage": len(accepted) / len(gold) if gold else None,
                    "precision": sum(correct(r, "expected_room") for r in accepted) / len(accepted)
                    if accepted
                    else None,
                }
            )
    return result


def audit_snapshot(data):
    rows = [r for r in data["rows"] if r["kind"] == "observational"]
    with read_palace(Path(data["palace_path"])) as con:
        current = {r["id"]: _row_fingerprint(r) for r in _fetch_rows(con, [r["id"] for r in rows])}
    changed = [r["id"] for r in rows if current.get(r["id"]) != r["fingerprint"]]
    return {
        "sampled_drawers": len(rows),
        "changed_or_missing": len(changed),
        "palace_access": "SQLite mode=ro and query_only=ON; inference uses snapshot only",
    }


def report(args):
    data = json.loads(Path(args.dataset).expanduser().read_text())
    output = Path(args.output).expanduser()
    if data.get("palace_path"):
        _outside_palace(output, Path(data["palace_path"]))
    result = {
        "caveats": [
            "Controlled cases are authored fixtures, not independently human-reviewed production labels.",
            "Existing real room labels measure agreement, not correctness.",
            "Embedding classification uses stored vectors; its latency excludes embedding preparation.",
            "Probability thresholds are diagnostic, not calibrated deployment recommendations.",
        ],
        "algorithms": {},
        "audit": audit_snapshot(data),
    }
    for algorithm in [*EMBEDDING_ALGORITHMS, *ALGORITHMS]:
        path = output / (algorithm + ".json")
        if path.exists():
            run_data = json.loads(path.read_text())
            expected_hash = hashlib.sha256(Path(args.dataset).expanduser().read_bytes()).hexdigest()
            if run_data["provenance"]["dataset_sha256"] != expected_hash:
                raise ValueError("Result dataset hash mismatch")
            result["algorithms"][algorithm] = {
                **metrics(data["rows"], run_data["predictions"]),
                "provenance": run_data["provenance"],
                "cold_first_request_seconds": run_data.get("cold_first_request_seconds"),
                "loaded_models": run_data.get("loaded_models", []),
            }
    write_private(output / "report.json", result)
    print(json.dumps(result, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("prepare", help="Create a private snapshot using read-only SQLite")
    p.add_argument("--palace", required=True)
    p.add_argument("--wing", required=True)
    p.add_argument("--cases", required=True)
    p.add_argument("--real-limit", type=int, default=480)
    p.add_argument("--seed", type=int, default=20261028)
    p.add_argument("--output", required=True)
    p = commands.add_parser("run", help="Classify snapshot cases; never open the live palace")
    p.add_argument("--dataset", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--algorithm", choices=[*EMBEDDING_ALGORITHMS, *ALGORITHMS], required=True)
    p.add_argument("--endpoint", default="http://127.0.0.1:11434")
    p.add_argument("--timeout", type=int, default=120)
    p.add_argument(
        "--limit", type=int, default=0, help="Optional smoke-run case count; 0 means all"
    )
    p = commands.add_parser("report", help="Summarize predictions and audit sampled source rows")
    p.add_argument("--dataset", required=True)
    p.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        {"prepare": prepare, "run": run, "report": report}[args.command](args)
    except (ValueError, OSError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
