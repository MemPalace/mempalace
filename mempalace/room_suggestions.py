"""Optional local organization: background predictions, preview, explicit apply and undo.

Inference never acquires the palace writer lock or changes a drawer. Apply and
undo replay a journal under the existing writer lock; neither calls a model.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
import subprocess
import sys
import threading
import time
import weakref
from collections import Counter, defaultdict
from pathlib import Path

from .config import MempalaceConfig, sanitize_name
from .llm_client import LLMError
from .metadata_types import is_bookkeeping
from .room_decision import (
    RUBRIC_VERSION,
    DecisionRoomDecider,
    LocalOllama,
    drawer_state,
    input_hash,
    room_criteria,
)
from .room_suggestion_store import RoomSuggestionError, SuggestionStore
from .rooms import GENERIC_ROOMS, RoomSet, _iter_wing_rows, room_set_path
from .timeutil import utc_now_iso

_JOB_LOCKS = weakref.WeakValueDictionary()
_JOB_LOCKS_GUARD = threading.Lock()


class SuggestionBusy(RoomSuggestionError):
    """A worker or journal replay already owns this job."""


def document_hash(text):
    return hashlib.sha256((text or "").encode("utf-8", errors="surrogatepass")).hexdigest()


def taxonomy_hash(palace_path, wing):
    config = MempalaceConfig(palace_path=palace_path)
    return hashlib.sha256(Path(room_set_path(config, wing)).read_bytes()).hexdigest()


def check_taxonomy(store, job):
    config = job["config"]
    if taxonomy_hash(store.palace_path, config["wing"]) != config["taxonomy_hash"]:
        raise RoomSuggestionError("Room definitions changed; start a new suggestion job")
    if MempalaceConfig().collection_name != config["collection_name"]:
        raise RoomSuggestionError("The drawers collection changed; start a new suggestion job")


def create_job(
    store,
    *,
    wing,
    model,
    endpoint="http://127.0.0.1:11434",
    threshold=0.8,
    limit=100,
    from_rooms=None,
    backend=None,
    timeout=120,
):
    """Enqueue an explicitly requested bounded job; no model is downloaded."""
    from .palace import resolve_backend_name

    if sanitize_name(wing, "wing") != wing:
        raise RoomSuggestionError("Use the wing's exact approved spelling")
    if not math.isfinite(threshold) or not 0 <= threshold <= 1:
        raise RoomSuggestionError("Probability threshold must be between zero and one")
    if not 1 <= limit <= 10000 or not 1 <= timeout <= 600:
        raise RoomSuggestionError("Choose 1–10000 drawers and a 1–600 second request timeout")
    try:
        LocalOllama(endpoint, timeout)  # Reject remote/proxy endpoints before enqueueing.
    except ValueError as exc:
        raise RoomSuggestionError("Choose an HTTP loopback Ollama endpoint") from exc
    if not model or "cloud" in model.rsplit(":", 1)[-1].lower():
        raise RoomSuggestionError("Choose an installed local decision model")
    if (
        from_rooms is not None
        and from_rooms != "all"
        and (
            not isinstance(from_rooms, list)
            or not from_rooms
            or any(not isinstance(room, str) or not room for room in from_rooms)
        )
    ):
        raise RoomSuggestionError("Choose original room names or --from all")
    from .fsio import read_published_bytes

    room_bytes = read_published_bytes(
        room_set_path(MempalaceConfig(palace_path=store.palace_path), wing)
    )
    room_set = RoomSet.from_dict(json.loads(room_bytes), normalize_names=False)
    if room_set.wing != wing:
        raise RoomSuggestionError("The approved room set belongs to a different wing")
    room_criteria(room_set)
    return store.create(
        {
            "schema_version": 1,
            "wing": wing,
            "model": model,
            "endpoint": endpoint,
            "threshold": threshold,
            "limit": limit,
            "timeout": timeout,
            "from_rooms": sorted(GENERIC_ROOMS) if from_rooms is None else from_rooms,
            "backend": resolve_backend_name(store.palace_path, explicit=backend),
            "collection_name": MempalaceConfig().collection_name,
            "taxonomy": room_set.to_dict(),
            "taxonomy_hash": hashlib.sha256(room_bytes).hexdigest(),
            "rubric": RUBRIC_VERSION,
        }
    )


@contextlib.contextmanager
def worker_lock(store, job_id):
    # A distinct lock key: never occupy the real palace's writer lease during inference.
    from .palace import MineAlreadyRunning, mine_palace_lock

    store.job(job_id)
    key = str(store.root / job_id)
    with _JOB_LOCKS_GUARD:
        lock = _JOB_LOCKS.setdefault(key, threading.Lock())
    if not lock.acquire(blocking=False):
        raise SuggestionBusy("This job is still active; wait for its current operation")
    try:
        with contextlib.ExitStack() as stack:
            try:
                stack.enter_context(mine_palace_lock(key))
            except MineAlreadyRunning as exc:
                raise SuggestionBusy(
                    "This job is still active; wait for its current operation"
                ) from exc
            yield
    finally:
        lock.release()


def launch(store, job_id):
    """Detach one worker. The job lease excludes duplicate workers after a resume."""
    store.job(job_id)
    kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    try:
        return subprocess.Popen(
            [
                sys.executable,
                "-m",
                "mempalace.room_suggestions",
                "worker",
                "--palace",
                store.palace_path,
                "--job",
                job_id,
            ],
            **kwargs,
        )
    except OSError as exc:
        store.state(job_id, "failed", error="Worker could not start; resume the job to retry")
        raise RoomSuggestionError("Room suggestion worker could not start") from exc


def resume(store, job_id, *, foreground=False):
    """Resume only after the previous worker has released its lease."""
    with worker_lock(store, job_id):
        store.resume(job_id)
    if foreground:
        return run_worker(store, job_id)
    return launch(store, job_id)


def cancel(store, job_id):
    """Request cancellation; finalize it immediately if no worker owns the job."""
    store.cancel(job_id)
    try:
        with worker_lock(store, job_id):
            store.finish(job_id)
    except SuggestionBusy:
        pass  # The worker stops after its current bounded model request.


@contextlib.contextmanager
def open_drawers(store, config, *, writable=False):
    from .palace import get_collection

    collection = get_collection(
        store.palace_path,
        create=False,
        backend=config["backend"],
        collection_name=config["collection_name"],
        read_only=not writable,
    )
    try:
        yield collection
    finally:
        collection.close()


def fetch_rows(collection, ids):
    """Fetch aligned original text and metadata through the public collection API."""
    out = {}
    for start in range(0, len(ids), 200):
        batch = collection.get(ids=ids[start : start + 200], include=["documents", "metadatas"])
        if len(batch["ids"]) != len(batch["documents"]) or len(batch["ids"]) != len(
            batch["metadatas"]
        ):
            raise RoomSuggestionError("Backend returned unaligned drawer data")
        for key, document, metadata in zip(batch["ids"], batch["documents"], batch["metadatas"]):
            out[key] = {"id": key, "document": document or "", "metadata": metadata or {}}
    return out


def matches(row, metadata, doc_hash):
    return (
        row is not None
        and row["metadata"] == metadata
        and document_hash(row["document"]) == doc_hash
    )


def snapshot_candidates(collection, config):
    ids = []
    seen = set()
    scope = config["from_rooms"]
    for row in _iter_wing_rows(collection, config["wing"], include=["metadatas"]):
        meta = row["metadata"]
        room = meta.get("room")
        if row["id"] in seen or not isinstance(room, str) or not room:
            continue
        if scope != "all" and room not in scope:
            continue
        seen.add(row["id"])
        ids.append(row["id"])
        if len(ids) >= config["limit"]:
            break
    rows = fetch_rows(collection, ids)
    return [
        {
            "id": key,
            "metadata": rows[key]["metadata"],
            "document_hash": document_hash(rows[key]["document"]),
        }
        for key in ids
        if key in rows
        and rows[key]["metadata"].get("wing") == config["wing"]
        and not is_bookkeeping(rows[key]["metadata"])
        and (scope == "all" or rows[key]["metadata"].get("room") in scope)
    ]


def prediction_outcome(prediction, item, threshold):
    if not prediction["room"]:
        return "rejected"
    if prediction["score"] < threshold:
        return "uncertain"
    if prediction["room"] == item["metadata"]["room"]:
        return "unchanged"
    return "suggested"


def _run_predictions(store, job_id, job, collection, classifier):
    config = job["config"]
    for item in store.items(job_id):
        if store.job(job_id)["cancelled"]:
            return
        if item["outcome"] != "pending":
            continue
        row = fetch_rows(collection, [item["id"]]).get(item["id"])
        if not matches(row, item["metadata"], item["document_hash"]):
            store.prediction(
                job_id, item["id"], "stale", error="Drawer changed since this job started"
            )
            continue
        if not row["document"].strip():
            store.prediction(job_id, item["id"], "skipped", error="Empty drawer")
            continue
        try:
            row["document"].encode("utf-8")
        except UnicodeEncodeError:
            store.prediction(
                job_id, item["id"], "skipped", error="Input cannot be sent as valid UTF-8 unchanged"
            )
            continue
        key = input_hash(
            {
                "state": drawer_state(row, config["wing"]),
                "taxonomy": config["taxonomy_hash"],
                "model": job["identity"],
                "rubric": config["rubric"],
            }
        )
        started = time.monotonic()
        prediction = store.cache_get(key)
        cached = prediction is not None
        if prediction is None:
            # Bounded input failures are per-drawer skips; transport failures retain
            # the pending row and fail the job so resume can retry it later.
            try:
                prediction = classifier.predict(row).to_dict()
            except RuntimeError as exc:
                if (
                    "64 KiB" not in str(exc)
                    and "HTTP 400" not in str(exc)
                    and "HTTP 413" not in str(exc)
                ):
                    raise
                store.prediction(
                    job_id,
                    item["id"],
                    "skipped",
                    error="Input could not be classified without truncation",
                )
                continue
            store.cache_put(key, prediction)
        store.prediction(
            job_id,
            item["id"],
            prediction_outcome(prediction, item, config["threshold"]),
            prediction,
            seconds=time.monotonic() - started,
            cache_hit=cached,
        )


def run_worker(store, job_id):
    """Process a resumable job, saving each result without holding a writer lock."""
    try:
        with worker_lock(store, job_id):
            job = store.job(job_id)
            if job["state"] not in {"queued", "running", "cancelling"}:
                return
            if job["cancelled"]:
                store.finish(job_id)
                return
            store.state(job_id, "running")
            try:
                check_taxonomy(store, job)
                config = job["config"]
                if config["rubric"] != RUBRIC_VERSION:
                    raise RoomSuggestionError(
                        "Classifier rubric changed; start a new suggestion job"
                    )
                client = LocalOllama(config["endpoint"], config["timeout"])
                identity = client.inspect(config["model"], "decision")
                store.pin_identity(job_id, identity)
                job["identity"] = identity
                classifier = DecisionRoomDecider(
                    RoomSet.from_dict(config["taxonomy"], normalize_names=False),
                    client,
                    config["model"],
                )
                classifier._identity = identity
                with open_drawers(store, config) as collection:
                    if not job["enumerated"]:
                        store.snapshot(job_id, snapshot_candidates(collection, config))
                    _run_predictions(store, job_id, job, collection, classifier)
                check_taxonomy(store, job)
                store.finish(job_id)
            except (Exception, SystemExit) as exc:
                # Never persist an arbitrary backend exception containing user text.
                message = (
                    str(exc)
                    if isinstance(exc, (RoomSuggestionError, LLMError))
                    else f"{type(exc).__name__}: inference stopped; resume to retry"
                )
                store.state(job_id, "failed", error=message)
    except SuggestionBusy:
        return  # Another worker owns this job; it remains authoritative.


def status(store, job_id):
    job = store.job(job_id)
    items = store.items(job_id)
    counts = Counter(row["outcome"] for row in items)
    completed = len(items) - counts["pending"]
    seconds = sum(row["seconds"] for row in items)
    return {
        "id": job_id,
        "state": job["state"],
        "wing": job["config"]["wing"],
        "model": job["config"]["model"],
        "identity": job["identity"],
        "total": len(items),
        "completed": completed,
        "counts": dict(counts),
        "cache_hits": sum(row["cache_hit"] for row in items),
        "estimated_remaining_seconds": seconds / completed * counts["pending"]
        if completed
        else None,
        "error": job["error"],
        "threshold": job["config"]["threshold"],
        "created_at": job["created_at"],
        "updated_at": job["updated_at"],
    }


def preview(store, job_id, *, limit=20, offset=0):
    """Show references and verbatim excerpts. Preview never updates the palace."""
    job = store.job(job_id)
    items = store.items(job_id)
    # Suggested changes first, then uncertain/rejected rows for transparent review.
    items.sort(key=lambda row: (row["outcome"] != "suggested", row["ordinal"]))
    if not 1 <= limit <= 200 or offset < 0:
        raise RoomSuggestionError("Choose a preview limit of 1–200 and a nonnegative offset")
    picked = items[offset : offset + limit]
    with open_drawers(store, job["config"]) as collection:
        rows = fetch_rows(collection, [row["id"] for row in picked])
    out = []
    moves = {move["id"]: move for move in store.moves(job_id) if move["kind"] == "drawer"}
    for item in picked:
        row = rows.get(item["id"])
        expected = [item["metadata"]]
        if item["id"] in moves:
            move = moves[item["id"]]
            expected = [move["before"], move["after"]]
            if job["state"] == "applied":
                expected = [move["after"]]
            elif job["state"] == "undone":
                expected = [move["before"]]
        out.append(
            {
                "id": item["id"],
                "outcome": item["outcome"],
                "original_room": item["metadata"]["room"],
                "current_room": row["metadata"].get("room") if row else None,
                "suggested_room": (item["prediction"] or {}).get("room"),
                "probability": (item["prediction"] or {}).get("score"),
                "stale": not any(matches(row, meta, item["document_hash"]) for meta in expected),
                "excerpt": row["document"][:240] if row else None,
                "error": item["error"],
            }
        )
    return {
        **status(store, job_id),
        "items": out,
        "preview_offset": offset,
        "next_offset": offset + limit if offset + limit < len(items) else None,
    }


def _after_metadata(before, room):
    after = {**before, "room": room}
    # Avoid introducing a key that merge-only update APIs cannot remove on undo.
    if "last_modified" in before:
        after["last_modified"] = utc_now_iso()
    return after


def _move(kind, row, room):
    return {
        "kind": kind,
        "id": row["id"],
        "before": row["metadata"],
        "after": _after_metadata(row["metadata"], room),
        "document_hash": document_hash(row["document"]),
    }


def _assert_atomic_metadata(collection):
    from .backends.base import BaseCollection

    inner = getattr(collection, "_inner", collection)
    if type(inner).update is BaseCollection.update:
        raise RoomSuggestionError("This backend lacks an atomic metadata update implementation")


@contextlib.contextmanager
def _closets(store, config):
    from .backends import CollectionNotInitializedError
    from .palace import get_closets_collection

    try:
        collection = get_closets_collection(
            store.palace_path, create=False, backend=config["backend"]
        )
    except CollectionNotInitializedError:
        yield None
        return
    try:
        _assert_atomic_metadata(collection)
        yield collection
    finally:
        collection.close()


def _closet_moves(drawers, closets, config, selection):
    if closets is None:
        return []
    groups = defaultdict(list)
    sources = defaultdict(dict)
    for row in _iter_wing_rows(drawers, config["wing"], include=["metadatas"]):
        meta = row["metadata"]
        if meta.get("source_file"):
            groups[(meta["source_file"], meta.get("room", ""))].append(row["id"])
            sources[meta["source_file"]][row["id"]] = meta.get("room", "")
    targets = {}
    for group, ids in groups.items():
        if all(key in selection for key in ids):
            destinations = {selection[key] for key in ids}
            if len(destinations) == 1:
                targets[group] = next(iter(destinations))
    changes = []
    for row in _iter_wing_rows(closets, config["wing"], include=["metadatas", "documents"]):
        meta = row["metadata"]
        target = targets.get((meta.get("source_file"), meta.get("room", "")))
        if target and target != meta.get("room"):
            changes.append(
                {**_move("closet", row, target), "source_rooms": sources[meta["source_file"]]}
            )
    return changes


def _validate_closet_sources(moves, drawers, wing):
    """A resumed closet move must still describe the same source membership."""
    expected = {
        move["before"]["source_file"]: move["source_rooms"]
        for move in moves
        if move["kind"] == "closet" and move["source_rooms"] is not None
    }
    if not expected:
        return
    current = defaultdict(dict)
    selected = {move["id"] for move in moves if move["kind"] == "drawer"}
    for row in _iter_wing_rows(drawers, wing, include=["metadatas"]):
        meta = row["metadata"]
        source = meta.get("source_file")
        if source in expected:
            current[source][row["id"]] = meta.get("room", "")
    for source, rooms in expected.items():
        observed = current[source]
        if set(rooms) != set(observed) or any(
            observed[key] != room for key, room in rooms.items() if key not in selected
        ):
            raise RoomSuggestionError(
                "A closet's source membership changed; refusing an outdated index move"
            )


def _select(store, job_id, ids):
    candidates = {row["id"]: row for row in store.items(job_id) if row["outcome"] == "suggested"}
    selected = set(ids) if ids is not None else set(candidates)
    if not selected or selected - set(candidates):
        raise RoomSuggestionError(
            "Select at least one reviewed suggested drawer; unknown or uncertain IDs are refused"
        )
    return {key: candidates[key] for key in sorted(selected)}


def _prepare_journal(store, job_id, job, drawers, closets, ids):
    selected = _select(store, job_id, ids)
    rows = fetch_rows(drawers, list(selected))
    changes = []
    for key, item in selected.items():
        prediction = item["prediction"] or {}
        names = {room["name"] for room in job["config"]["taxonomy"]["rooms"]}
        score = prediction.get("score")
        if (
            prediction.get("room") not in names
            or isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not math.isfinite(score)
            or not job["config"]["threshold"] <= score <= 1
        ):
            raise RoomSuggestionError("Saved prediction is invalid; start a new suggestion job")
        row = rows.get(key)
        if not matches(row, item["metadata"], item["document_hash"]):
            raise RoomSuggestionError(f"Drawer {key!r} changed; no suggestions were applied")
        changes.append(_move("drawer", row, item["prediction"]["room"]))
    destinations = {key: item["prediction"]["room"] for key, item in selected.items()}
    changes.extend(_closet_moves(drawers, closets, job["config"], destinations))
    store.journal(job_id, list(selected), changes)


def _validate_moves(moves, collections, *, undo, undo_started=False):
    # Validate the entire journal before the first write, including records that
    # already reached their destination before an interrupted acknowledgment.
    for kind, collection in collections.items():
        subset = [move for move in moves if move["kind"] == kind]
        if not subset:
            continue
        if collection is None:
            raise RoomSuggestionError(
                "A planned closet collection is missing; refusing a stale plan"
            )
        rows = fetch_rows(collection, [move["id"] for move in subset])
        for move in subset:
            row = rows.get(move["id"])
            # Acknowledged writes must remain in their acknowledged state. Only
            # an interrupted, unacknowledged write may be at either endpoint.
            endpoints = [move["before"], move["after"]]
            if move["undone"]:
                endpoints = [move["before"]]
            elif move["applied"] and (not undo or not undo_started):
                endpoints = [move["after"]]
            if not any(matches(row, meta, move["document_hash"]) for meta in endpoints):
                raise RoomSuggestionError(
                    f"{kind.title()} {move['id']!r} was edited; refusing to overwrite later changes"
                )


def _replay(store, job_id, moves, collections, *, undo):
    changed = 0
    target_key = "before" if undo else "after"
    for move in moves:
        collection = collections[move["kind"]]
        row = fetch_rows(collection, [move["id"]]).get(move["id"])
        target = move[target_key]
        if not matches(row, target, move["document_hash"]):
            source = move["after" if undo else "before"]
            if not matches(row, source, move["document_hash"]):
                raise RoomSuggestionError("A record changed during journal replay")
            patch = {"room": target["room"]}
            if "last_modified" in target:
                patch["last_modified"] = target["last_modified"]
            collection.update(ids=[move["id"]], metadatas=[patch])
            changed += 1
        store.mark_move(job_id, move["kind"], move["id"], undo=undo)
    return changed


def apply(store, job_id, *, ids=None, yes=False, undo=False):
    """Apply reviewed predictions or undo exactly their journaled metadata changes."""
    from .palace import mine_palace_lock

    job = store.job(job_id)
    if not yes:
        return {"dry_run": True, "action": "undo" if undo else "apply", **status(store, job_id)}
    if job["selection"] is not None and ids is not None and sorted(set(ids)) != job["selection"]:
        raise RoomSuggestionError("Resume must use the original reviewed selection")
    if job["state"] == ("undone" if undo else "applied"):
        return {"state": job["state"], "changed": 0, "already_complete": True}
    allowed = (
        {"applied", "applying", "undoing", "apply_conflict", "undo_conflict"}
        if undo
        else {"ready", "applying", "apply_conflict"}
    )
    if job["state"] not in allowed:
        raise RoomSuggestionError(
            "Wait for a ready suggestion job, or resume the interrupted operation"
        )
    # Remote writers cannot share a local lease; add compare-and-set backend
    # capabilities before enabling explicit apply on remote stores.
    if job["config"]["backend"] not in {"chroma", "sqlite_exact", "rust_exact"}:
        raise RoomSuggestionError(
            "Apply and undo currently require a local Chroma, SQLite exact or Rust exact palace"
        )
    with worker_lock(store, job_id), mine_palace_lock(store.palace_path):
        job = store.job(job_id)
        if job["state"] == ("undone" if undo else "applied"):
            return {"state": job["state"], "changed": 0, "already_complete": True}
        if job["state"] not in allowed:
            raise RoomSuggestionError("Job state changed; inspect it before continuing")
        if not undo:
            check_taxonomy(store, job)
        with (
            open_drawers(store, job["config"], writable=True) as drawers,
            _closets(store, job["config"]) as closets,
        ):
            _assert_atomic_metadata(drawers)
            if job["state"] == "ready":
                _prepare_journal(store, job_id, job, drawers, closets, ids)
            elif ids is not None and sorted(set(ids)) != job["selection"]:
                raise RoomSuggestionError("Resume must use the original reviewed selection")
            moves = store.moves(job_id)
            collections = {"drawer": drawers, "closet": closets}
            try:
                _validate_closet_sources(moves, drawers, job["config"]["wing"])
                _validate_moves(
                    moves,
                    collections,
                    undo=undo,
                    undo_started=job["state"] in {"undoing", "undo_conflict"},
                )
                store.state(job_id, "undoing" if undo else "applying")
                changed = _replay(store, job_id, moves, collections, undo=undo)
            except RoomSuggestionError as exc:
                previous = store.job(job_id)["state"]
                if previous in {"applying", "undoing", "apply_conflict", "undo_conflict"}:
                    # A later edit can make recovery impossible until reviewed.
                    # Keep its journal, but do not block every future job.
                    conflict = "undo_conflict" if previous.startswith("undo") else "apply_conflict"
                    store.state(job_id, conflict, error=str(exc))
                raise
            state = "undone" if undo else "applied"
            store.state(job_id, state)
    return {
        "state": state,
        "changed": changed,
        "drawers": sum(m["kind"] == "drawer" for m in moves),
        "closets": sum(m["kind"] == "closet" for m in moves),
    }


def main():
    parser = argparse.ArgumentParser(description="Internal room suggestion worker")
    parser.add_argument("command", choices=["worker"])
    parser.add_argument("--palace", required=True)
    parser.add_argument("--job", required=True)
    args = parser.parse_args()
    run_worker(SuggestionStore(args.palace), args.job)


if __name__ == "__main__":
    main()
