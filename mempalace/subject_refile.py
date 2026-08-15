"""Content-preserving room cleanup for an already-mined project corpus."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .palace import (
    NORMALIZE_VERSION,
    build_closet_lines,
    get_closets_collection,
    get_collection,
    mine_palace_lock,
    upsert_closet_lines,
)
from .subject_router import SubjectRouter

SUBJECT_REFILE_ROOTS_ENV = "MEMPALACE_SUBJECT_REFILE_ROOTS_JSON"
_JOURNAL_SCHEMA = "mempalace-subject-refile-journal/v1"
_JOURNAL_NAME = ".subject-refile-journal.json"
_PAGE_SIZE = 500


class SubjectRefileError(RuntimeError):
    """Raised when a subject refile cannot be performed safely."""


def _allowed_source_roots(configured: Iterable[str] | None = None) -> tuple[Path, ...]:
    if configured is None:
        raw = os.environ.get(SUBJECT_REFILE_ROOTS_ENV, "").strip()
        if not raw:
            raise SubjectRefileError(
                f"{SUBJECT_REFILE_ROOTS_ENV} must explicitly allow retained project roots"
            )
        try:
            configured = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SubjectRefileError(f"{SUBJECT_REFILE_ROOTS_ENV} must be valid JSON") from exc
    if not isinstance(configured, (list, tuple)) or not configured:
        raise SubjectRefileError(f"{SUBJECT_REFILE_ROOTS_ENV} must be a non-empty list")
    roots: list[Path] = []
    for item in configured:
        if not isinstance(item, str) or not item.strip():
            raise SubjectRefileError(f"{SUBJECT_REFILE_ROOTS_ENV} entries must be paths")
        root = Path(item).expanduser().resolve(strict=True)
        if root == Path(root.anchor):
            raise SubjectRefileError("subject refile roots cannot be a filesystem root")
        roots.append(root)
    return tuple(roots)


def _resolve_allowed_source(source: str, configured: Iterable[str] | None = None) -> Path:
    source_root = Path(source).expanduser().resolve(strict=True)
    if source_root not in _allowed_source_roots(configured):
        raise SubjectRefileError("source must exactly match an allowed retained project root")
    return source_root


def _within_source_root(source_file: Any, source_root: Path) -> bool:
    if not isinstance(source_file, str) or not source_file:
        return False
    try:
        Path(source_file).expanduser().resolve(strict=False).relative_to(source_root)
        return True
    except (OSError, ValueError):
        return False


def _has_project_provenance(metadata: dict[str, Any], source_root: Path) -> bool:
    ingest_mode = metadata.get("ingest_mode")
    if ingest_mode == "projects":
        stamped_root = metadata.get("source_root")
        return stamped_root is None or Path(str(stamped_root)).resolve(strict=False) == source_root
    if ingest_mode is not None:
        return False
    # Legacy project-miner drawers predate ingest_mode. Require the fields its
    # metadata builder always stamped so a manual drawer with a source label is
    # not mistaken for retained workspace mining.
    return (
        isinstance(metadata.get("chunk_index"), int)
        and isinstance(metadata.get("source_mtime"), (int, float))
        and isinstance(metadata.get("id_recipe"), str)
        and isinstance(metadata.get("normalize_version"), int)
    )


def _eligible_rows(collection, source_root: Path, wing: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        batch = collection.get(
            where={"wing": wing},
            limit=_PAGE_SIZE,
            offset=offset,
            include=["documents", "metadatas"],
        )
        ids = batch.get("ids") or []
        documents = batch.get("documents") or []
        metadatas = batch.get("metadatas") or []
        for drawer_id, document, metadata in zip(ids, documents, metadatas):
            metadata = dict(metadata or {})
            if not _has_project_provenance(metadata, source_root):
                continue
            if not _within_source_root(metadata.get("source_file"), source_root):
                continue
            rows.append({"id": drawer_id, "document": document or "", "metadata": metadata})
        if not ids:
            break
        offset += len(ids)
    return rows


def _route_rows(rows: list[dict[str, Any]], router: SubjectRouter) -> None:
    for start in range(0, len(rows), _PAGE_SIZE):
        batch = rows[start : start + _PAGE_SIZE]
        routes = router.route_many([row["document"] for row in batch])
        if len(routes) != len(batch):
            raise SubjectRefileError("subject router did not classify every retained drawer")
        for row, route in zip(batch, routes):
            row["route"] = route


def _report(rows: list[dict[str, Any]], router: SubjectRouter) -> dict[str, Any]:
    transitions: Counter[tuple[str, str]] = Counter()
    destinations: Counter[str] = Counter()
    route_methods: Counter[str] = Counter()
    moved = 0
    for row in rows:
        old_room = str(row["metadata"].get("room") or "")
        route = row["route"]
        transitions[(old_room, route.room)] += 1
        destinations[route.room] += 1
        route_methods[route.method] += 1
        moved += old_room != route.room
    return {
        "schema": "mempalace-subject-refile/v1",
        "subject_policy": router.fingerprint,
        "policy_version": router.version,
        "plan_sha256": _plan_sha256(rows),
        "eligible_drawers": len(rows),
        "moved_drawers": moved,
        "unchanged_drawers": len(rows) - moved,
        "destinations": dict(sorted(destinations.items())),
        "route_methods": dict(sorted(route_methods.items())),
        "transitions": [
            {"from": old, "to": new, "count": count}
            for (old, new), count in sorted(transitions.items())
        ],
    }


def _plan_sha256(rows: list[dict[str, Any]]) -> str:
    """Fingerprint the exact content and metadata plan without disclosing either."""

    plan = [
        {
            "id": row["id"],
            "document": row["document"],
            "metadata": row["metadata"],
            "target_room": row["route"].room,
            "route_method": row["route"].method,
            "route_score": float(row["route"].score),
        }
        for row in sorted(rows, key=lambda item: item["id"])
    ]
    payload = json.dumps(
        plan,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _apply_rows(collection, rows: list[dict[str, Any]], router: SubjectRouter) -> None:
    for start in range(0, len(rows), _PAGE_SIZE):
        batch = rows[start : start + _PAGE_SIZE]
        metadatas: list[dict[str, Any]] = []
        for row in batch:
            route = row["route"]
            metadata = dict(row["metadata"])
            metadata.update(
                {
                    "room": route.room,
                    "subject_policy": router.fingerprint,
                    "subject_route": route.method,
                    "subject_score": float(route.score),
                }
            )
            row["metadata"] = metadata
            metadatas.append(metadata)
        collection.update(ids=[row["id"] for row in batch], metadatas=metadatas)


def _purge_source_wing_rooms(closets, source_file: str, wing: str, rooms: set[str]) -> None:
    if not rooms:
        return
    result = closets.get(
        where={"$and": [{"source_file": source_file}, {"wing": wing}]},
        include=["metadatas"],
    )
    delete_ids = [
        closet_id
        for closet_id, metadata in zip(result.get("ids") or [], result.get("metadatas") or [])
        if str((metadata or {}).get("room") or "") in rooms
    ]
    if delete_ids:
        closets.delete(ids=delete_ids)


def _rebuild_closets(
    closets,
    rows: list[dict[str, Any]],
    *,
    policy_override: str | None = None,
    previous_rooms: dict[str, set[str]] | None = None,
) -> None:
    by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_source[row["metadata"]["source_file"]].append(row)
    for source_file, source_rows in by_source.items():
        wing = str(source_rows[0]["metadata"]["wing"])
        rooms_to_purge = {str(row["metadata"]["room"]) for row in source_rows}
        if previous_rooms is not None:
            rooms_to_purge.update(previous_rooms.get(source_file, set()))
        _purge_source_wing_rooms(closets, source_file, wing, rooms_to_purge)
        by_room: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in source_rows:
            by_room[row["metadata"]["room"]].append(row)
        for room, room_rows in by_room.items():
            content = "\n\n".join(row["document"] for row in room_rows)
            drawer_ids = [row["id"] for row in room_rows]
            drawer_metas = [row["metadata"] for row in room_rows]
            lines = build_closet_lines(
                source_file,
                drawer_ids,
                content,
                wing,
                room,
                drawer_metas=drawer_metas,
            )
            source_hash = hashlib.sha256(source_file.encode()).hexdigest()[:24]
            metadata = {
                "wing": wing,
                "room": room,
                "source_file": source_file,
                "drawer_count": len(room_rows),
                "filed_at": datetime.now().isoformat(),
                "normalize_version": NORMALIZE_VERSION,
            }
            subject_policy = policy_override or drawer_metas[0].get("subject_policy")
            if subject_policy:
                metadata["subject_policy"] = subject_policy
            upsert_closet_lines(
                closets,
                f"closet_{wing}_{room}_{source_hash}",
                lines,
                metadata,
            )


def _verify_rows(collection, rows: list[dict[str, Any]], router: SubjectRouter) -> None:
    expected = {row["id"]: row["route"].room for row in rows}
    ids = list(expected)
    for start in range(0, len(ids), _PAGE_SIZE):
        batch_ids = ids[start : start + _PAGE_SIZE]
        batch = collection.get(ids=batch_ids, include=["metadatas"])
        actual = dict(zip(batch.get("ids") or [], batch.get("metadatas") or []))
        if set(actual) != set(batch_ids):
            raise SubjectRefileError("subject refile verification lost a drawer")
        for drawer_id in batch_ids:
            metadata = actual[drawer_id] or {}
            if (
                metadata.get("room") != expected[drawer_id]
                or metadata.get("subject_policy") != router.fingerprint
            ):
                raise SubjectRefileError("subject refile verification found mixed policy state")


def _verify_closets(closets, rows: list[dict[str, Any]], policy: str) -> None:
    expected: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in rows:
        metadata = row["metadata"]
        expected[(metadata["source_file"], metadata["wing"])].add(metadata["room"])
    for (source_file, wing), rooms in expected.items():
        result = closets.get(
            where={"$and": [{"source_file": source_file}, {"wing": wing}]},
            include=["metadatas"],
        )
        actual = {
            metadata.get("room")
            for metadata in result.get("metadatas") or []
            if metadata and metadata.get("subject_policy") == policy
        }
        if not rooms.issubset(actual):
            raise SubjectRefileError("subject refile verification found missing closet coverage")


def _journal_path(palace_path: str) -> Path:
    return Path(palace_path).expanduser().resolve() / _JOURNAL_NAME


def _write_journal(path: Path, source_root: Path, wing: str, rows: list[dict[str, Any]]) -> None:
    payload = {
        "schema": _JOURNAL_SCHEMA,
        "source": str(source_root),
        "wing": wing,
        "rows": [
            {
                "id": row["id"],
                "metadata": row["metadata"],
                "target_room": row["route"].room,
            }
            for row in rows
        ],
    }
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except Exception:
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise


def _load_journal(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SubjectRefileError("subject refile journal is unreadable") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != _JOURNAL_SCHEMA
        or not isinstance(payload.get("rows"), list)
    ):
        raise SubjectRefileError("subject refile journal has an unsupported schema")
    return payload


def _restore_journal(collection, closets, payload: dict[str, Any]) -> None:
    journal_rows = payload["rows"]
    ids = [item["id"] for item in journal_rows]
    restored_rows: list[dict[str, Any]] = []
    for start in range(0, len(ids), _PAGE_SIZE):
        batch_items = journal_rows[start : start + _PAGE_SIZE]
        batch_ids = [item["id"] for item in batch_items]
        current = collection.get(ids=batch_ids, include=["documents", "embeddings"])
        documents = dict(zip(current.get("ids") or [], current.get("documents") or []))
        if set(documents) != set(batch_ids):
            raise SubjectRefileError("subject refile recovery cannot find every drawer")
        raw_embeddings = current.get("embeddings")
        if raw_embeddings is None or len(raw_embeddings) != len(batch_ids):
            raise SubjectRefileError("subject refile recovery cannot read every embedding")
        embeddings = dict(zip(current.get("ids") or [], raw_embeddings))
        # Chroma metadata updates merge keys, so they cannot remove the new
        # subject fields during rollback. Reinsert each drawer with its exact
        # stored vector and document to restore the complete original metadata
        # without asking the embedding model to recompute sacred content.
        collection.delete(ids=batch_ids)
        collection.upsert(
            ids=batch_ids,
            documents=[documents[item["id"]] or "" for item in batch_items],
            embeddings=[embeddings[item["id"]] for item in batch_items],
            metadatas=[item["metadata"] for item in batch_items],
        )
        restored_rows.extend(
            {
                "id": item["id"],
                "document": documents[item["id"]] or "",
                "metadata": item["metadata"],
            }
            for item in batch_items
        )
    previous_rooms: dict[str, set[str]] = defaultdict(set)
    for item in journal_rows:
        previous_rooms[item["metadata"]["source_file"]].add(item["target_room"])
    _rebuild_closets(closets, restored_rows, previous_rooms=previous_rooms)


def _recover_pending_refile(collection, closets, journal_path: Path) -> bool:
    if not journal_path.exists():
        return False
    payload = _load_journal(journal_path)
    _restore_journal(collection, closets, payload)
    journal_path.unlink()
    return True


def subject_refile(
    palace_path: str,
    source: str,
    wing: str,
    *,
    router: SubjectRouter,
    dry_run: bool = True,
    expected_plan_sha256: str | None = None,
    allowed_source_roots: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Plan or apply a recoverable metadata refile without changing drawer text."""

    source_root = _resolve_allowed_source(source, allowed_source_roots)
    journal_path = _journal_path(palace_path)

    def run(*, recover: bool) -> dict[str, Any]:
        collection = get_collection(palace_path)
        closets = get_closets_collection(palace_path)
        if journal_path.exists() and not recover:
            raise SubjectRefileError(
                "an interrupted subject refile needs an apply run to restore its journal"
            )
        recovered = _recover_pending_refile(collection, closets, journal_path) if recover else False
        rows = _eligible_rows(collection, source_root, wing)
        _route_rows(rows, router)
        report = _report(rows, router)
        if not dry_run:
            if not isinstance(expected_plan_sha256, str) or not re.fullmatch(
                r"sha256:[0-9a-f]{64}", expected_plan_sha256
            ):
                raise SubjectRefileError("apply requires an expected plan SHA-256")
            if report["plan_sha256"] != expected_plan_sha256:
                raise SubjectRefileError("subject refile plan changed before apply")
        report.update(
            {
                "wing": wing,
                "source": str(source_root),
                "dry_run": dry_run,
                "recovered_pending_refile": recovered,
            }
        )
        if dry_run or not rows:
            return report

        previous_rooms: dict[str, set[str]] = defaultdict(set)
        for row in rows:
            previous_rooms[row["metadata"]["source_file"]].add(row["metadata"]["room"])
        _write_journal(journal_path, source_root, wing, rows)
        try:
            _apply_rows(collection, rows, router)
            _rebuild_closets(
                closets,
                rows,
                policy_override=router.fingerprint,
                previous_rooms=previous_rooms,
            )
            _verify_rows(collection, rows, router)
            _verify_closets(closets, rows, router.fingerprint)
        except Exception as original_error:
            try:
                _restore_journal(collection, closets, _load_journal(journal_path))
                journal_path.unlink()
            except Exception as recovery_error:
                raise SubjectRefileError(
                    "subject refile failed and automatic recovery did not complete; rerun apply"
                ) from recovery_error
            raise original_error
        journal_path.unlink()
        return report

    if dry_run:
        return run(recover=False)
    with mine_palace_lock(palace_path):
        return run(recover=True)
