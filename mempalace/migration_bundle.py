"""Offline, copy-only migration bundle construction for reviewed palace plans."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import sys
import uuid
from collections import defaultdict
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, Iterable

from .daemon import state_dir
from .palace import mine_palace_lock, palace_read_lock
from .reorganize import (
    collect_duplicate_evidence,
    exact_hash,
    inventory_palace,
    palace_semantic_snapshot,
    palace_snapshot,
    plan_actions,
    read_manifest,
    sha256_file,
    validate_reviewed_manifest,
)

_SIDECAR_NAMES = ("config.json", "hallways.json", "tunnels.json")
_ACTIVATION_SIDECAR_NAMES = ("hallways.json", "tunnels.json")
_ACTIVATION_REPORT = "activation-report.json"


def _expanded(path: os.PathLike[str] | str) -> Path:
    return Path(path).expanduser().absolute()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _validate_new_bundle_path(path: Path, source_palace: Path) -> None:
    if path.exists() or path.is_symlink():
        raise ValueError(f"migration destination already exists: {path}")
    source = source_palace.resolve()
    destination = path.resolve(strict=False)
    if _is_within(destination, source) or _is_within(source, destination):
        raise ValueError("migration destination must not overlap the active palace")


def _reject_symlinks(root: Path) -> None:
    if root.is_symlink():
        raise ValueError(f"migration source must not be a symlink: {root}")
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in [*directories, *files]:
            candidate = current_path / name
            if candidate.is_symlink():
                raise ValueError(f"migration source contains a symlink: {candidate}")


def _make_owner_only(root: Path) -> None:
    for current, directories, files in os.walk(root):
        current_path = Path(current)
        os.chmod(current_path, 0o700)
        for name in directories:
            os.chmod(current_path / name, 0o700)
        for name in files:
            os.chmod(current_path / name, 0o600)


def _copy_bundle_source(source_palace: Path, destination_root: Path) -> None:
    destination_root.mkdir(mode=0o700, parents=False, exist_ok=False)
    shutil.copytree(
        source_palace,
        destination_root / "palace",
        copy_function=shutil.copy2,
        ignore=shutil.ignore_patterns("*-shm"),
    )
    source_config_root = source_palace.parent
    for name in _SIDECAR_NAMES:
        source = source_config_root / name
        if source.is_file() and not source.is_symlink():
            shutil.copy2(source, destination_root / name)
    daemon_source = state_dir(str(source_palace))
    if daemon_source.is_dir() and not daemon_source.is_symlink():
        _reject_symlinks(daemon_source)
        shutil.copytree(daemon_source, destination_root / "daemon", copy_function=shutil.copy2)
    _make_owner_only(destination_root)


def _temporary_sibling(destination: Path) -> Path:
    return destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")


def _ensure_bundle_parent(path: Path) -> None:
    parent = path.parent
    try:
        parent.mkdir(mode=0o700, parents=True, exist_ok=False)
    except FileExistsError:
        if parent.is_symlink() or not parent.is_dir():
            raise ValueError(f"migration destination parent is not a directory: {parent}")
    else:
        os.chmod(parent, 0o700)


def _raise_posix_rename_error(error: int, destination: Path) -> None:
    if error in {errno.EEXIST, errno.ENOTEMPTY}:
        raise FileExistsError(error, os.strerror(error), str(destination))
    raise OSError(error, os.strerror(error), str(destination))


def _publish_directory_no_replace(source: Path, destination: Path) -> None:
    """Atomically rename a directory only when ``destination`` is absent."""
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        move_file = kernel32.MoveFileExW
        move_file.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
        move_file.restype = ctypes.c_int
        if not move_file(str(source), str(destination), 0):
            error = ctypes.get_last_error()
            if error in {80, 183}:  # ERROR_FILE_EXISTS / ERROR_ALREADY_EXISTS
                raise FileExistsError(error, ctypes.FormatError(error), str(destination))
            raise OSError(error, ctypes.FormatError(error), str(destination))
        return

    libc = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source)
    destination_bytes = os.fsencode(destination)
    if sys.platform == "darwin":
        rename_exclusive = libc.renamex_np
        rename_exclusive.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        rename_exclusive.restype = ctypes.c_int
        result = rename_exclusive(source_bytes, destination_bytes, 0x00000004)
    elif sys.platform.startswith("linux"):
        try:
            rename_exclusive = libc.renameat2
        except AttributeError as exc:
            raise RuntimeError(
                "atomic no-replace migration publication is unavailable on this Linux runtime"
            ) from exc
        rename_exclusive.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename_exclusive.restype = ctypes.c_int
        result = rename_exclusive(
            -100,  # AT_FDCWD
            source_bytes,
            -100,
            destination_bytes,
            0x00000001,  # RENAME_NOREPLACE
        )
    else:
        raise RuntimeError(
            f"atomic no-replace migration publication is unsupported on {sys.platform}"
        )
    if result != 0:
        _raise_posix_rename_error(ctypes.get_errno(), destination)
    _fsync_directory(destination.parent)


def _validated_source_snapshot(
    reviewed: dict[str, Any],
    palace_path: Path,
    *,
    label: str,
    current_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Accept exact physical state or version-2 semantic equivalence."""
    expected_physical = reviewed.get("sqlite_snapshot")
    if not isinstance(expected_physical, dict):
        raise ValueError("reviewed manifest is missing its SQLite snapshot")
    current = current_snapshot or palace_snapshot(palace_path)
    if current == expected_physical:
        return current

    expected_semantic = reviewed.get("source_semantic_snapshot")
    if reviewed.get("version") != 2 or not isinstance(expected_semantic, dict):
        raise ValueError(f"{label} no longer matches the reviewed manifest snapshot")
    if palace_semantic_snapshot(palace_path) != expected_semantic:
        raise ValueError(
            f"{label} no longer matches the reviewed manifest; semantic state no longer matches"
        )
    return current


def prepare_migration_copies(
    *,
    source_palace: os.PathLike[str] | str,
    reviewed_manifest: os.PathLike[str] | str,
    rollback_root: os.PathLike[str] | str,
    migrated_root: os.PathLike[str] | str,
    canonical_root: os.PathLike[str] | str,
    worktree_roots: Iterable[os.PathLike[str] | str],
    session_roots: Iterable[os.PathLike[str] | str],
    hot_days: int = 90,
) -> dict[str, Any]:
    """Create verified rollback and migrated copies without touching source.

    Both destinations are published by same-filesystem rename only after the
    active SQLite+WAL snapshot and reviewed manifest have been reconciled. If
    rollback publishes but staging publication fails, the completed rollback
    is deliberately retained for explicit cleanup; automatic path deletion
    cannot safely distinguish a concurrently replaced destination.
    """

    source = _expanded(source_palace)
    rollback = _expanded(rollback_root)
    migrated = _expanded(migrated_root)
    if rollback == migrated:
        raise ValueError("rollback and migrated destinations must differ")
    _validate_new_bundle_path(rollback, source)
    _validate_new_bundle_path(migrated, source)
    _ensure_bundle_parent(rollback)
    _ensure_bundle_parent(migrated)

    reviewed = read_manifest(reviewed_manifest)
    rollback_tmp = _temporary_sibling(rollback)
    migrated_tmp = _temporary_sibling(migrated)
    try:
        _reject_symlinks(source)
        with palace_read_lock(str(source)) as acquired:
            if not acquired:
                raise RuntimeError("active palace is being written; retry when maintenance is idle")
            before = palace_snapshot(source)
            _validated_source_snapshot(
                reviewed,
                source,
                label="active palace",
                current_snapshot=before,
            )
            inventory = inventory_palace(
                source,
                canonical_root=canonical_root,
                worktree_roots=worktree_roots,
                session_roots=session_roots,
            )
            actions = plan_actions(inventory, hot_days=hot_days)
            evidence = collect_duplicate_evidence(inventory, actions)
            validate_reviewed_manifest(
                reviewed,
                inventory,
                actions,
                evidence,
                palace_path=source,
                sqlite_snapshot=reviewed["sqlite_snapshot"],
            )
            _copy_bundle_source(source, rollback_tmp)
            after = palace_snapshot(source)
            if before != after:
                raise RuntimeError("active palace changed during rollback copy")

        rollback_snapshot = palace_snapshot(rollback_tmp / "palace")
        if rollback_snapshot != before:
            raise RuntimeError("rollback copy SQLite+WAL checksum does not match source")
        shutil.copytree(rollback_tmp, migrated_tmp, copy_function=shutil.copy2)
        _make_owner_only(migrated_tmp)
        if palace_snapshot(migrated_tmp / "palace") != before:
            raise RuntimeError("migrated staging copy does not match rollback source")

        _publish_directory_no_replace(rollback_tmp, rollback)
        _publish_directory_no_replace(migrated_tmp, migrated)
        return {
            "success": True,
            "source_palace": str(source),
            "rollback_root": str(rollback),
            "migrated_root": str(migrated),
            "snapshot": before,
            "inventory_total": len(inventory),
            "duplicate_candidates": len(evidence),
        }
    except Exception:
        shutil.rmtree(rollback_tmp, ignore_errors=True)
        shutil.rmtree(migrated_tmp, ignore_errors=True)
        raise


def bundle_permissions(root: os.PathLike[str] | str) -> list[tuple[str, int]]:
    """Return non-private bundle paths for verification/reporting."""
    bundle = _expanded(root)
    violations: list[tuple[str, int]] = []
    for current, directories, files in os.walk(bundle):
        current_path = Path(current)
        for candidate in [current_path, *(current_path / name for name in directories)]:
            mode = stat.S_IMODE(candidate.stat().st_mode)
            if mode != 0o700:
                violations.append((str(candidate), mode))
        for name in files:
            candidate = current_path / name
            mode = stat.S_IMODE(candidate.stat().st_mode)
            if mode != 0o600:
                violations.append((str(candidate), mode))
    return violations


def _chunk_index(metadata: dict[str, Any]) -> int:
    value = metadata.get("chunk_index", 0)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def apply_actions_to_collection(
    collection,
    inventory,
    actions,
    evidence,
    *,
    batch_size: int = 250,
) -> dict[str, int]:
    """Idempotently update retained metadata and delete evidenced duplicates."""
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    records_by_id = {record.drawer_id: record for record in inventory}
    actions_by_id = {action.drawer_id: action for action in actions}
    evidence_ids = {item.worktree_drawer_id for item in evidence}
    candidate_ids = {
        action.drawer_id for action in actions if action.action == "duplicate_candidate"
    }
    if candidate_ids != evidence_ids:
        raise ValueError("duplicate candidates and exact evidence do not reconcile")
    if set(records_by_id) != set(actions_by_id):
        raise ValueError("inventory and migration actions do not reconcile")

    retained_actions = [
        action
        for action in sorted(actions, key=lambda item: item.drawer_id)
        if action.drawer_id not in candidate_ids
    ]
    for start in range(0, len(retained_actions), batch_size):
        batch = retained_actions[start : start + batch_size]
        metadatas = []
        for action in batch:
            metadata = {key: value for key, value in action.metadata.items() if value is not None}
            metadata["wing"] = action.destination_wing
            metadatas.append(metadata)
        collection.update(
            ids=[action.drawer_id for action in batch],
            metadatas=metadatas,
        )

    ordered_candidates = sorted(candidate_ids)
    for start in range(0, len(ordered_candidates), batch_size):
        collection.delete(ids=ordered_candidates[start : start + batch_size])

    return {
        "records_before": len(records_by_id),
        "records_updated": len(retained_actions),
        "records_deleted_as_verified_duplicates": len(candidate_ids),
        "records_expected_after": len(retained_actions),
    }


def verify_retained_records(collection, inventory, actions, evidence) -> dict[str, int]:
    """Prove exact retained content and destination metadata after apply."""
    evidence_ids = {item.worktree_drawer_id for item in evidence}
    records_by_id = {record.drawer_id: record for record in inventory}
    expected_ids = sorted(set(records_by_id) - evidence_ids)
    expected_actions = {action.drawer_id: action for action in actions}
    observed_ids: set[str] = set()
    checked = 0
    for start in range(0, len(expected_ids), 500):
        batch_ids = expected_ids[start : start + 500]
        result = collection.get(ids=batch_ids, include=["documents", "metadatas"])
        documents = result.get("documents") or []
        metadatas = result.get("metadatas") or []
        for drawer_id, document, metadata in zip(result.get("ids") or [], documents, metadatas):
            observed_ids.add(drawer_id)
            expected_record = records_by_id[drawer_id]
            expected_action = expected_actions[drawer_id]
            if exact_hash(document or "") != exact_hash(expected_record.content):
                raise RuntimeError(f"retained content hash mismatch: {drawer_id}")
            metadata = metadata or {}
            if metadata.get("wing") != expected_action.destination_wing:
                raise RuntimeError(f"destination wing mismatch: {drawer_id}")
            for key in ("memory_tier", "source_kind", "source_canonicality"):
                expected_value = expected_action.metadata.get(key)
                if expected_value is not None and metadata.get(key) != expected_value:
                    raise RuntimeError(f"retained metadata mismatch for {drawer_id}: {key}")
            checked += 1
    missing = set(expected_ids) - observed_ids
    if missing:
        raise RuntimeError(f"retained drawers missing after migration: {len(missing)}")
    if collection.count() != len(expected_ids):
        raise RuntimeError("migrated collection contains unexpected drawer IDs")

    for start in range(0, len(evidence_ids), 500):
        candidate_batch = sorted(evidence_ids)[start : start + 500]
        remaining = collection.get(ids=candidate_batch, include=[]).get("ids") or []
        if remaining:
            raise RuntimeError(
                f"verified duplicate drawers remain after migration: {len(remaining)}"
            )
    return {
        "retained_records_verified": checked,
        "verified_duplicates_absent": len(evidence_ids),
    }


def rebuild_closets_for_retained_records(
    *,
    backend,
    palace_path: str,
    inventory,
    actions,
    evidence,
) -> int:
    """Rebuild the derived regex closet index from retained verbatim drawers."""
    from .palace import build_closet_lines, get_closets_collection, upsert_closet_lines

    evidence_ids = {item.worktree_drawer_id for item in evidence}
    actions_by_id = {action.drawer_id: action for action in actions}
    groups: dict[tuple[str, str, str], list[Any]] = defaultdict(list)
    for record in inventory:
        if record.drawer_id in evidence_ids:
            continue
        action = actions_by_id[record.drawer_id]
        room = str(action.metadata.get("room") or "general")
        source_file = str(action.metadata.get("source_file") or f"drawer:{record.drawer_id}")
        groups[(source_file, action.destination_wing, room)].append(record)

    try:
        backend.delete_collection(palace_path, "mempalace_closets")
    except Exception:
        pass
    closets = get_closets_collection(palace_path, create=True)
    written = 0
    for (source_file, wing, room), records in sorted(groups.items()):
        ordered = sorted(
            records,
            key=lambda record: (_chunk_index(record.metadata), record.drawer_id),
        )
        drawer_ids = [record.drawer_id for record in ordered]
        content = "\n".join(record.content for record in ordered)
        drawer_metas = [
            {**actions_by_id[record.drawer_id].metadata, "wing": wing} for record in ordered
        ]
        lines = build_closet_lines(
            source_file,
            drawer_ids,
            content,
            wing,
            room,
            drawer_metas=drawer_metas,
        )
        identity = f"{wing}\0{room}\0{source_file}".encode("utf-8", errors="surrogatepass")
        closet_id_base = f"closet_{wing}_{room}_{hashlib.sha256(identity).hexdigest()[:24]}"
        written += upsert_closet_lines(
            closets,
            closet_id_base,
            lines,
            {
                "wing": wing,
                "room": room,
                "source_file": source_file,
                "drawer_count": len(drawer_ids),
                "memory_tier": drawer_metas[0].get("memory_tier", "hot"),
            },
        )
    return written


def _optional_collection(backend, palace_path: str, collection_name: str):
    from .backends.base import CollectionNotInitializedError

    try:
        return backend.get_collection(
            palace_path,
            collection_name=collection_name,
            create=False,
        )
    except CollectionNotInitializedError:
        return None


def _result_values(result, name: str):
    value = getattr(result, name, None)
    if value is None and hasattr(result, "get"):
        value = result.get(name)
    return [] if value is None else list(value)


def _read_available_embeddings(collection, drawer_ids: list[str]) -> dict[str, list[float]]:
    if not drawer_ids:
        return {}
    try:
        result = collection.get(ids=drawer_ids, include=["embeddings"])
        observed = dict(
            zip(
                _result_values(result, "ids"),
                _result_values(result, "embeddings"),
            )
        )
    except Exception:
        if len(drawer_ids) == 1:
            return {}
        midpoint = len(drawer_ids) // 2
        return {
            **_read_available_embeddings(collection, drawer_ids[:midpoint]),
            **_read_available_embeddings(collection, drawer_ids[midpoint:]),
        }
    missing = [drawer_id for drawer_id in drawer_ids if drawer_id not in observed]
    if missing and len(missing) < len(drawer_ids):
        observed.update(_read_available_embeddings(collection, missing))
    return observed


def rebuild_drawers_vector_index(
    *,
    backend,
    palace_path: str,
    inventory,
    actions,
    evidence,
    batch_size: int,
) -> tuple[Any, int]:
    """Rebuild retained drawers into a fresh HNSW collection without re-embedding."""
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    collection_name = "mempalace_drawers"
    temporary_name = f"{collection_name}__migration_tmp"
    records_by_id = {record.drawer_id: record for record in inventory}
    actions_by_id = {action.drawer_id: action for action in actions}
    removed_ids = {item.worktree_drawer_id for item in evidence}
    retained_ids = sorted(set(records_by_id) - removed_ids)

    original = _optional_collection(backend, palace_path, collection_name)
    temporary = _optional_collection(backend, palace_path, temporary_name)
    reembedded = 0
    if original is not None:
        if temporary is not None:
            backend.delete_collection(palace_path, temporary_name)
        metric = getattr(original, "distance_metric", "cosine")
        identity = original.get_stored_embedder_identity()
        temporary = backend.create_collection(
            palace_path,
            temporary_name,
            hnsw_space=metric,
        )
        for start in range(0, len(retained_ids), batch_size):
            batch_ids = retained_ids[start : start + batch_size]
            embeddings_by_id = _read_available_embeddings(original, batch_ids)

            def metadata_for(drawer_id: str) -> dict[str, Any]:
                action = actions_by_id[drawer_id]
                metadata = {
                    key: value for key, value in action.metadata.items() if value is not None
                }
                metadata["wing"] = action.destination_wing
                return metadata

            reusable_ids = [drawer_id for drawer_id in batch_ids if drawer_id in embeddings_by_id]
            if reusable_ids:
                temporary.upsert(
                    ids=reusable_ids,
                    documents=[records_by_id[drawer_id].content for drawer_id in reusable_ids],
                    metadatas=[metadata_for(drawer_id) for drawer_id in reusable_ids],
                    embeddings=[embeddings_by_id[drawer_id] for drawer_id in reusable_ids],
                )
            missing_ids = [
                drawer_id for drawer_id in batch_ids if drawer_id not in embeddings_by_id
            ]
            if missing_ids:
                temporary.upsert(
                    ids=missing_ids,
                    documents=[records_by_id[drawer_id].content for drawer_id in missing_ids],
                    metadatas=[metadata_for(drawer_id) for drawer_id in missing_ids],
                )
                reembedded += len(missing_ids)
        if identity is not None:
            temporary.set_embedder_identity(identity)
        verify_retained_records(temporary, inventory, actions, evidence)
    elif temporary is None:
        raise RuntimeError(
            "drawer vector rebuild has neither a live nor recoverable temporary collection"
        )
    else:
        verify_retained_records(temporary, inventory, actions, evidence)

    if original is not None:
        backend.delete_collection(palace_path, collection_name)
    raw_collection = getattr(temporary, "_collection", None)
    if raw_collection is None or not hasattr(raw_collection, "modify"):
        raise RuntimeError("Chroma collection rename is unavailable for vector rebuild")
    raw_collection.modify(name=collection_name)
    backend.close_palace(palace_path)

    rebuilt = _optional_collection(backend, palace_path, collection_name)
    if rebuilt is None:
        raise RuntimeError("rebuilt drawer collection was not published")
    verify_retained_records(rebuilt, inventory, actions, evidence)
    if _optional_collection(backend, palace_path, temporary_name) is not None:
        raise RuntimeError("temporary drawer collection still exists after vector rebuild")
    return rebuilt, reembedded


def _write_private_json(path: Path, payload: dict[str, Any]) -> None:
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_atomic_private_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = _temporary_sibling(path)
    try:
        _write_private_json(temporary, payload)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _replace_and_sync(source: Path, destination: Path) -> None:
    os.replace(source, destination)
    _fsync_directory(destination.parent)
    if source.parent != destination.parent:
        _fsync_directory(source.parent)


def _configure_migrated_bundle(bundle_root: Path) -> None:
    config_path = bundle_root / "config.json"
    config: dict[str, Any] = {}
    if config_path.is_file() and not config_path.is_symlink():
        with config_path.open("r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        if isinstance(loaded, dict):
            config = loaded
    config["palace_path"] = str(bundle_root / "palace")
    config["backend"] = "chroma"
    _write_private_json(config_path, config)


def _sqlite_integrity_errors(palace_path: Path) -> list[str]:
    sqlite_path = palace_path / "chroma.sqlite3"
    uri = f"file:{sqlite_path.as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        rows = connection.execute("PRAGMA integrity_check").fetchall()
    return [str(row[0]) for row in rows if str(row[0]).lower() != "ok"]


def prune_orphan_hnsw_directories(palace_path: os.PathLike[str] | str) -> int:
    """Remove UUID segment directories no longer referenced by Chroma SQLite."""
    palace = _expanded(palace_path)
    sqlite_path = palace / "chroma.sqlite3"
    with sqlite3.connect(f"file:{sqlite_path.as_posix()}?mode=ro", uri=True) as connection:
        live_segment_ids = {str(row[0]) for row in connection.execute("SELECT id FROM segments")}

    removed = 0
    for child in palace.iterdir():
        if child.is_symlink() or not child.is_dir() or child.name in live_segment_ids:
            continue
        try:
            parsed = uuid.UUID(child.name)
        except ValueError:
            continue
        if str(parsed) != child.name.lower() or not (child / "index_metadata.pickle").is_file():
            continue
        shutil.rmtree(child)
        removed += 1
    return removed


def _artifact_snapshot(path: Path) -> dict[str, int | str]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"migration artifact must be a regular file: {path}")
    stat_result = path.stat()
    return {
        "size": stat_result.st_size,
        "sha256": sha256_file(path),
    }


def drawer_logical_snapshot(palace_path: os.PathLike[str] | str) -> dict[str, int | str]:
    """Hash stable drawer content and metadata, independent of Chroma bookkeeping."""
    palace = _expanded(palace_path)
    records = inventory_palace(
        palace,
        canonical_root=palace,
        worktree_roots=[],
        session_roots=[],
    )
    digest = hashlib.sha256()
    for record in records:
        row = json.dumps(
            [record.drawer_id, exact_hash(record.content), record.metadata],
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8", errors="surrogatepass")
        digest.update(len(row).to_bytes(8, "big"))
        digest.update(row)
    return {"count": len(records), "sha256": digest.hexdigest()}


def _derived_sidecar_snapshots(bundle_root: Path) -> dict[str, dict[str, int | str]]:
    return {
        name: _artifact_snapshot(bundle_root / name)
        for name in _ACTIVATION_SIDECAR_NAMES
        if (bundle_root / name).exists()
    }


def _rebuild_graph_sidecars(bundle_root: Path, collection, wings: list[str]) -> dict[str, int]:
    from .hallways import compute_hallways_for_wing, list_hallways
    from .palace_graph import entity_tunnels_for_wing

    hallway_path = bundle_root / "hallways.json"
    tunnel_path = bundle_root / "tunnels.json"
    hallway_path.unlink(missing_ok=True)
    tunnel_path.unlink(missing_ok=True)
    previous = os.environ.get("MEMPALACE_PALACE_PATH")
    os.environ["MEMPALACE_PALACE_PATH"] = str(bundle_root / "palace")
    try:
        hallways_created = 0
        for wing in wings:
            hallways_created += len(compute_hallways_for_wing(wing, col=collection))
        hallways = list_hallways()
        tunnels_created = 0
        for wing in wings:
            tunnels_created += len(entity_tunnels_for_wing(wing, hallways))
    finally:
        if previous is None:
            os.environ.pop("MEMPALACE_PALACE_PATH", None)
        else:
            os.environ["MEMPALACE_PALACE_PATH"] = previous
    return {
        "hallways_created": hallways_created,
        "entity_tunnels_created": tunnels_created,
    }


def apply_reviewed_migration(
    *,
    source_palace: os.PathLike[str] | str,
    reviewed_manifest: os.PathLike[str] | str,
    rollback_root: os.PathLike[str] | str,
    migrated_root: os.PathLike[str] | str,
    canonical_root: os.PathLike[str] | str,
    worktree_roots: Iterable[os.PathLike[str] | str],
    session_roots: Iterable[os.PathLike[str] | str],
    hot_days: int = 90,
    batch_size: int = 250,
) -> dict[str, Any]:
    """Apply a reviewed plan to the migrated copy and verify it end-to-end."""
    source = _expanded(source_palace)
    rollback = _expanded(rollback_root)
    migrated = _expanded(migrated_root)
    rollback_palace = rollback / "palace"
    migrated_palace = migrated / "palace"
    if not rollback_palace.is_dir() or not migrated_palace.is_dir():
        raise ValueError("rollback and migrated bundle copies must already exist")
    reviewed = read_manifest(reviewed_manifest)
    reviewed_snapshot = reviewed.get("sqlite_snapshot")
    if not isinstance(reviewed_snapshot, dict):
        raise ValueError("reviewed manifest is missing its SQLite snapshot")
    _validated_source_snapshot(reviewed, rollback_palace, label="rollback copy")

    inventory = inventory_palace(
        rollback_palace,
        canonical_root=canonical_root,
        worktree_roots=worktree_roots,
        session_roots=session_roots,
    )
    actions = plan_actions(inventory, hot_days=hot_days)
    evidence = collect_duplicate_evidence(inventory, actions)
    validate_reviewed_manifest(
        reviewed,
        inventory,
        actions,
        evidence,
        palace_path=source,
        sqlite_snapshot=reviewed_snapshot,
    )

    from .palace import get_backend_for_palace, get_collection

    _configure_migrated_bundle(migrated)
    backend = get_backend_for_palace(str(migrated_palace), explicit="chroma")
    collection, vectors_reembedded = rebuild_drawers_vector_index(
        backend=backend,
        palace_path=str(migrated_palace),
        inventory=inventory,
        actions=actions,
        evidence=evidence,
        batch_size=batch_size,
    )
    duplicate_count = len(evidence)
    apply_counts = {
        "records_before": len(inventory),
        "records_updated": len(inventory) - duplicate_count,
        "records_deleted_as_verified_duplicates": duplicate_count,
        "records_expected_after": len(inventory) - duplicate_count,
    }
    verification = verify_retained_records(collection, inventory, actions, evidence)

    closets_written = rebuild_closets_for_retained_records(
        backend=backend,
        palace_path=str(migrated_palace),
        inventory=inventory,
        actions=actions,
        evidence=evidence,
    )
    graph_counts = _rebuild_graph_sidecars(
        migrated,
        collection,
        sorted(
            {
                action.destination_wing
                for action in actions
                if action.drawer_id not in {item.worktree_drawer_id for item in evidence}
            }
        ),
    )
    backend.close_palace(str(migrated_palace))
    orphan_hnsw_removed = prune_orphan_hnsw_directories(migrated_palace)

    integrity_errors = _sqlite_integrity_errors(migrated_palace)
    if integrity_errors:
        raise RuntimeError(f"migrated SQLite integrity_check failed: {integrity_errors[:3]}")
    from .service import _verify_chroma_readiness

    drawer_readiness = _verify_chroma_readiness(
        str(migrated_palace),
        "mempalace_drawers",
    )
    closet_readiness = _verify_chroma_readiness(
        str(migrated_palace),
        "mempalace_closets",
    )
    if not drawer_readiness.get("ready") or not closet_readiness.get("ready"):
        raise RuntimeError("migrated vector collections did not pass persisted readiness checks")

    collection = get_collection(
        str(migrated_palace),
        collection_name="mempalace_drawers",
        create=False,
        backend="chroma",
    )
    verification = verify_retained_records(collection, inventory, actions, evidence)
    get_backend_for_palace(str(migrated_palace), explicit="chroma").close_palace(
        str(migrated_palace)
    )

    cold_preserved = sum(
        1
        for action in actions
        if action.drawer_id not in {item.worktree_drawer_id for item in evidence}
        and action.metadata.get("memory_tier") == "cold"
    )
    report = {
        "version": 1,
        "status": "complete",
        "source_palace_sha256": hashlib.sha256(str(source).encode()).hexdigest(),
        "reviewed_manifest_sha256": hashlib.sha256(
            Path(reviewed_manifest).expanduser().read_bytes()
        ).hexdigest(),
        **apply_counts,
        **verification,
        "records_preserved_cold": cold_preserved,
        "records_preserved_unclassified": sum(
            1 for action in actions if action.action == "preserve_unclassified"
        ),
        "closets_rebuilt": closets_written,
        "drawer_vectors_reused": len(inventory) - duplicate_count - vectors_reembedded,
        "drawer_vectors_reembedded": vectors_reembedded,
        "orphan_hnsw_directories_removed": orphan_hnsw_removed,
        **graph_counts,
        "sqlite_integrity": "ok",
        "drawer_vector_readiness": drawer_readiness,
        "closet_vector_readiness": closet_readiness,
        "derived_sidecars": _derived_sidecar_snapshots(migrated),
        "migrated_logical_snapshot": drawer_logical_snapshot(migrated_palace),
        "migrated_semantic_snapshot": palace_semantic_snapshot(migrated_palace),
        "migrated_snapshot": palace_snapshot(migrated_palace),
    }
    _write_private_json(migrated / "apply-report.json", report)
    _make_owner_only(migrated)
    return report


def _read_json_object(path: Path, *, label: str) -> dict[str, Any]:
    if path.is_symlink():
        raise ValueError(f"{label} must not be a symlink: {path}")
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read {label}: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must contain a JSON object")
    return payload


def _require_real_directory(path: Path, *, label: str) -> None:
    if path.is_symlink():
        raise ValueError(f"{label} must not be a symlink: {path}")
    if not path.is_dir():
        raise ValueError(f"{label} is not a directory: {path}")


def _paths_overlap(left: Path, right: Path) -> bool:
    resolved_left = left.resolve(strict=False)
    resolved_right = right.resolve(strict=False)
    return _is_within(resolved_left, resolved_right) or _is_within(resolved_right, resolved_left)


def _validate_activation_paths(
    *,
    active_palace: Path,
    migrated_root: Path,
    previous_root: Path,
) -> Path:
    staged_palace = migrated_root / "palace"
    _require_real_directory(active_palace, label="active palace")
    _require_real_directory(migrated_root, label="migrated bundle")
    _require_real_directory(staged_palace, label="migrated palace")
    if previous_root.exists() or previous_root.is_symlink():
        raise ValueError(f"previous activation slot already exists: {previous_root}")
    for left, right in (
        (active_palace, migrated_root),
        (active_palace, previous_root),
        (migrated_root, previous_root),
    ):
        if _paths_overlap(left, right):
            raise ValueError("activation paths must not overlap")

    previous_root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    devices = {
        active_palace.parent.stat().st_dev,
        migrated_root.stat().st_dev,
        previous_root.parent.stat().st_dev,
    }
    if len(devices) != 1:
        raise ValueError("activation paths must be on the same filesystem")
    return staged_palace


def _validate_reviewed_activation(
    *,
    active_palace: Path,
    reviewed_manifest: os.PathLike[str] | str,
    migrated_root: Path,
    staged_palace: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    reviewed = read_manifest(reviewed_manifest)
    expected_path_hash = exact_hash(str(active_palace.resolve()))
    if reviewed.get("palace_path_sha256") != expected_path_hash:
        raise ValueError("reviewed manifest belongs to a different active palace")
    reviewed_snapshot = reviewed.get("sqlite_snapshot")
    if not isinstance(reviewed_snapshot, dict):
        raise ValueError("reviewed manifest is missing its SQLite snapshot")
    active_snapshot = _validated_source_snapshot(
        reviewed,
        active_palace,
        label="active palace",
    )

    apply_report = _read_json_object(
        migrated_root / "apply-report.json",
        label="migration apply report",
    )
    if apply_report.get("version") != 1 or apply_report.get("status") != "complete":
        raise ValueError("migrated bundle does not have a completed apply report")
    manifest_digest = sha256_file(reviewed_manifest)
    if apply_report.get("reviewed_manifest_sha256") != manifest_digest:
        raise ValueError("migration apply report belongs to a different reviewed manifest")
    if apply_report.get("source_palace_sha256") != expected_path_hash:
        raise ValueError("migration apply report belongs to a different source palace")
    if apply_report.get("sqlite_integrity") != "ok":
        raise ValueError("migration apply report does not attest SQLite integrity")
    if not (apply_report.get("drawer_vector_readiness") or {}).get("ready"):
        raise ValueError("migration apply report does not attest drawer vector readiness")
    if not (apply_report.get("closet_vector_readiness") or {}).get("ready"):
        raise ValueError("migration apply report does not attest closet vector readiness")
    counts = reviewed.get("counts") or {}
    inventory_total = counts.get("inventory_total")
    duplicate_total = counts.get("verified_duplicate_candidates")
    if not isinstance(inventory_total, int) or not isinstance(duplicate_total, int):
        raise ValueError("reviewed manifest is missing migration counts")
    expected_after = inventory_total - duplicate_total
    expected_report_counts = {
        "records_before": inventory_total,
        "records_expected_after": expected_after,
        "records_deleted_as_verified_duplicates": duplicate_total,
        "retained_records_verified": expected_after,
        "verified_duplicates_absent": duplicate_total,
    }
    if any(apply_report.get(key) != value for key, value in expected_report_counts.items()):
        raise ValueError("migration apply report counts do not match the reviewed manifest")
    migrated_snapshot = apply_report.get("migrated_snapshot")
    if not isinstance(migrated_snapshot, dict):
        raise ValueError("migration apply report is missing the migrated snapshot")
    logical_snapshot = apply_report.get("migrated_logical_snapshot")
    if not isinstance(logical_snapshot, dict):
        raise ValueError("migration apply report is missing the migrated logical snapshot")
    if drawer_logical_snapshot(staged_palace) != logical_snapshot:
        raise ValueError("migrated drawers no longer match the completed apply report")
    semantic_snapshot = apply_report.get("migrated_semantic_snapshot")
    if not isinstance(semantic_snapshot, dict):
        raise ValueError("migration apply report is missing the migrated semantic snapshot")
    if palace_semantic_snapshot(staged_palace) != semantic_snapshot:
        raise ValueError("migrated palace no longer matches the completed semantic snapshot")
    expected_sidecars = apply_report.get("derived_sidecars")
    if not isinstance(expected_sidecars, dict):
        raise ValueError("migration apply report is missing derived sidecar snapshots")
    if _derived_sidecar_snapshots(migrated_root) != expected_sidecars:
        raise ValueError("migrated sidecars no longer match the completed apply report")
    return reviewed, apply_report, active_snapshot


def _require_daemons_stopped(palace_paths: Iterable[Path]) -> None:
    from .daemon import _pid_alive, endpoint_path, get_client_if_running, pid_path

    for palace_path in palace_paths:
        palace_string = str(palace_path)
        if get_client_if_running(palace_string, health_timeout=0.2) is not None:
            raise RuntimeError(
                f"MemPalace daemon must be stopped before activation or rollback: {palace_path}"
            )
        pid_marker = pid_path(palace_string)
        endpoint_marker = endpoint_path(palace_string)
        if pid_marker.exists():
            try:
                pid = int(pid_marker.read_text(encoding="utf-8").strip())
            except (OSError, ValueError) as exc:
                raise RuntimeError(
                    f"cannot prove MemPalace daemon is stopped for {palace_path}"
                ) from exc
            if _pid_alive(pid):
                raise RuntimeError(f"MemPalace daemon PID {pid} is still alive for {palace_path}")
        elif endpoint_marker.exists():
            raise RuntimeError(
                f"cannot prove MemPalace daemon is stopped for {palace_path}: stale endpoint state"
            )


@contextmanager
def _exclusive_palace_locks(palace_paths: Iterable[Path]):
    ordered = sorted({str(path.absolute()) for path in palace_paths})
    with ExitStack() as stack:
        for palace_path in ordered:
            stack.enter_context(mine_palace_lock(palace_path, blocking=False))
        yield


def _close_palace_handles(*palace_paths: Path) -> None:
    from .palace import get_backend_for_palace

    backend = get_backend_for_palace(str(palace_paths[0]), explicit="chroma")
    for palace_path in palace_paths:
        backend.close_palace(str(palace_path))


def _validate_sidecars(
    root: Path,
    names: Iterable[str],
    *,
    expected: dict[str, dict[str, int | str]] | None = None,
) -> dict[str, dict[str, int | str]]:
    present: dict[str, dict[str, int | str]] = {}
    names_to_check = set(names)
    if expected is not None:
        names_to_check.update(_ACTIVATION_SIDECAR_NAMES)
    for name in sorted(names_to_check):
        path = root / name
        if path.is_symlink():
            raise ValueError(f"activation sidecar must not be a symlink: {path}")
        if path.exists():
            if not path.is_file():
                raise ValueError(f"activation sidecar must be a file: {path}")
            present[name] = _artifact_snapshot(path)
    if expected is not None and present != expected:
        raise ValueError(f"activation sidecars changed under {root}")
    return present


def _verify_staging_readiness(staged_palace: Path) -> None:
    integrity_errors = _sqlite_integrity_errors(staged_palace)
    if integrity_errors:
        raise RuntimeError(f"migrated SQLite integrity_check failed: {integrity_errors[:3]}")
    from .service import _verify_chroma_readiness

    for collection_name in ("mempalace_drawers", "mempalace_closets"):
        readiness = _verify_chroma_readiness(str(staged_palace), collection_name)
        if not readiness.get("ready"):
            raise RuntimeError(
                f"migrated {collection_name} failed persisted readiness before activation"
            )


def _journal_path(previous_root: Path) -> Path:
    return previous_root.parent / f".{previous_root.name}.activation-journal.json"


def _remove_journal(path: Path) -> None:
    path.unlink(missing_ok=True)
    _fsync_directory(path.parent)


def _palace_matches(path: Path, expected: dict[str, Any]) -> bool:
    try:
        return path.is_dir() and not path.is_symlink() and palace_snapshot(path) == expected
    except (OSError, RuntimeError, ValueError):
        return False


def _semantic_palace_matches(path: Path, expected: dict[str, Any]) -> bool:
    try:
        return (
            path.is_dir() and not path.is_symlink() and palace_semantic_snapshot(path) == expected
        )
    except (OSError, RuntimeError, ValueError):
        return False


def _sidecar_matches(path: Path, expected: dict[str, int | str]) -> bool:
    try:
        return _artifact_snapshot(path) == expected
    except (OSError, ValueError):
        return False


def _validated_journal(
    *,
    journal_path: Path,
    expected_operation: str,
    active: Path,
    migrated: Path,
    previous: Path,
) -> dict[str, Any] | None:
    if not journal_path.exists() and not journal_path.is_symlink():
        return None
    payload = _read_json_object(journal_path, label="activation journal")
    if payload.get("version") != 1 or payload.get("operation") != expected_operation:
        raise RuntimeError(
            f"unfinished {payload.get('operation', 'unknown')} operation requires matching recovery"
        )
    expected_paths = {
        "active_palace": str(active),
        "migrated_root": str(migrated),
        "previous_root": str(previous),
    }
    if payload.get("paths") != expected_paths:
        raise RuntimeError("activation journal paths do not match this request")
    return payload


def _recover_activation_swap(
    *,
    journal_path: Path,
    journal: dict[str, Any],
    active: Path,
    migrated: Path,
    previous: Path,
) -> dict[str, Any] | None:
    report = journal.get("report")
    if not isinstance(report, dict):
        raise RuntimeError("activation journal is missing its recovery report")
    previous_tmp = Path(str(journal.get("previous_tmp") or ""))
    if (
        previous_tmp.parent != previous.parent
        or not previous_tmp.name.startswith(f".{previous.name}.")
        or not previous_tmp.name.endswith(".tmp")
        or previous_tmp.is_symlink()
    ):
        raise RuntimeError("activation journal contains an unsafe temporary path")
    staged = migrated / "palace"
    old_snapshot = report.get("reviewed_snapshot")
    new_snapshot = report.get("migrated_semantic_snapshot")
    old_sidecars = report.get("active_sidecars")
    new_sidecars = report.get("migrated_sidecars")
    if not all(
        isinstance(value, dict)
        for value in (old_snapshot, new_snapshot, old_sidecars, new_sidecars)
    ):
        raise RuntimeError("activation journal is missing snapshot evidence")

    lock_paths = [active, staged, previous / "palace"]
    _require_daemons_stopped(lock_paths)
    with _exclusive_palace_locks(lock_paths):
        _require_daemons_stopped(lock_paths)
        if (
            _semantic_palace_matches(active, new_snapshot)
            and not staged.exists()
            and _palace_matches(previous / "palace", old_snapshot)
        ):
            _validate_sidecars(active.parent, new_sidecars, expected=new_sidecars)
            _validate_sidecars(previous, old_sidecars, expected=old_sidecars)
            _remove_journal(journal_path)
            return report
        if previous.exists() or previous.is_symlink():
            raise RuntimeError("cannot safely recover a partially published activation")
        if active.exists():
            if _semantic_palace_matches(active, new_snapshot) and not staged.exists():
                _replace_and_sync(active, staged)
            elif not _palace_matches(active, old_snapshot):
                raise RuntimeError("cannot identify the active palace during activation recovery")
        for name, snapshot in new_sidecars.items():
            active_sidecar = active.parent / name
            staged_sidecar = migrated / name
            if _sidecar_matches(active_sidecar, snapshot):
                if staged_sidecar.exists() or staged_sidecar.is_symlink():
                    raise RuntimeError(f"ambiguous migrated sidecar during recovery: {name}")
                _replace_and_sync(active_sidecar, staged_sidecar)
            elif not _sidecar_matches(staged_sidecar, snapshot):
                raise RuntimeError(f"cannot identify migrated sidecar during recovery: {name}")
        for name, snapshot in old_sidecars.items():
            active_sidecar = active.parent / name
            saved_sidecar = previous_tmp / name
            if _sidecar_matches(active_sidecar, snapshot):
                continue
            if not _sidecar_matches(saved_sidecar, snapshot):
                raise RuntimeError(f"cannot identify previous sidecar during recovery: {name}")
            if active_sidecar.exists() or active_sidecar.is_symlink():
                raise RuntimeError(
                    f"active sidecar destination is occupied during recovery: {name}"
                )
            _replace_and_sync(saved_sidecar, active_sidecar)
        if not _palace_matches(active, old_snapshot):
            saved_palace = previous_tmp / "palace"
            if not _palace_matches(saved_palace, old_snapshot) or active.exists():
                raise RuntimeError("cannot restore the previous active palace")
            _replace_and_sync(saved_palace, active)
        if not _semantic_palace_matches(staged, new_snapshot):
            raise RuntimeError("migrated palace was not restored during activation recovery")
        _validate_sidecars(active.parent, old_sidecars, expected=old_sidecars)
        _validate_sidecars(migrated, new_sidecars, expected=new_sidecars)
        shutil.rmtree(previous_tmp)
        _fsync_directory(previous_tmp.parent)
        _remove_journal(journal_path)
    return None


def _recover_rollback_swap(
    *,
    journal_path: Path,
    journal: dict[str, Any],
    active: Path,
    migrated: Path,
    previous: Path,
) -> dict[str, Any] | None:
    state = journal.get("report")
    rolled_back = journal.get("rolled_back_report")
    if not isinstance(state, dict) or not isinstance(rolled_back, dict):
        raise RuntimeError("rollback journal is missing its recovery report")
    staged = migrated / "palace"
    old_snapshot = state.get("reviewed_snapshot")
    new_snapshot = state.get("migrated_semantic_snapshot")
    old_sidecars = state.get("active_sidecars")
    new_sidecars = state.get("migrated_sidecars")
    if not all(
        isinstance(value, dict)
        for value in (old_snapshot, new_snapshot, old_sidecars, new_sidecars)
    ):
        raise RuntimeError("rollback journal is missing snapshot evidence")
    old_palace = previous / "palace"
    lock_paths = [active, staged, old_palace]
    _require_daemons_stopped(lock_paths)
    with _exclusive_palace_locks(lock_paths):
        _require_daemons_stopped(lock_paths)
        if (
            _palace_matches(active, old_snapshot)
            and _semantic_palace_matches(staged, new_snapshot)
            and not old_palace.exists()
        ):
            _validate_sidecars(active.parent, old_sidecars, expected=old_sidecars)
            _validate_sidecars(migrated, new_sidecars, expected=new_sidecars)
            _write_atomic_private_json(previous / _ACTIVATION_REPORT, rolled_back)
            _remove_journal(journal_path)
            return rolled_back
        if active.exists() and _palace_matches(active, old_snapshot) and not old_palace.exists():
            _replace_and_sync(active, old_palace)
        elif active.exists() and not _semantic_palace_matches(active, new_snapshot):
            raise RuntimeError("cannot identify the active palace during rollback recovery")
        for name, snapshot in old_sidecars.items():
            active_sidecar = active.parent / name
            saved_sidecar = previous / name
            if _sidecar_matches(active_sidecar, snapshot):
                if saved_sidecar.exists() or saved_sidecar.is_symlink():
                    raise RuntimeError(f"ambiguous previous sidecar during recovery: {name}")
                _replace_and_sync(active_sidecar, saved_sidecar)
            elif not _sidecar_matches(saved_sidecar, snapshot):
                raise RuntimeError(f"cannot identify previous sidecar during recovery: {name}")
        for name, snapshot in new_sidecars.items():
            active_sidecar = active.parent / name
            staged_sidecar = migrated / name
            if _sidecar_matches(active_sidecar, snapshot):
                continue
            if not _sidecar_matches(staged_sidecar, snapshot):
                raise RuntimeError(f"cannot identify migrated sidecar during recovery: {name}")
            if active_sidecar.exists() or active_sidecar.is_symlink():
                raise RuntimeError(
                    f"active sidecar destination is occupied during recovery: {name}"
                )
            _replace_and_sync(staged_sidecar, active_sidecar)
        if not _semantic_palace_matches(active, new_snapshot):
            if not _semantic_palace_matches(staged, new_snapshot) or active.exists():
                raise RuntimeError("cannot restore the migrated active palace")
            _replace_and_sync(staged, active)
        if not _palace_matches(old_palace, old_snapshot):
            raise RuntimeError("previous palace was not restored during rollback recovery")
        _validate_sidecars(active.parent, new_sidecars, expected=new_sidecars)
        _validate_sidecars(previous, old_sidecars, expected=old_sidecars)
        _remove_journal(journal_path)
    return None


def _recover_interrupted_swap(
    *,
    expected_operation: str,
    active: Path,
    migrated: Path,
    previous: Path,
) -> dict[str, Any] | None:
    journal_path = _journal_path(previous)
    journal = _validated_journal(
        journal_path=journal_path,
        expected_operation=expected_operation,
        active=active,
        migrated=migrated,
        previous=previous,
    )
    if journal is None:
        return None
    if expected_operation == "activate":
        return _recover_activation_swap(
            journal_path=journal_path,
            journal=journal,
            active=active,
            migrated=migrated,
            previous=previous,
        )
    return _recover_rollback_swap(
        journal_path=journal_path,
        journal=journal,
        active=active,
        migrated=migrated,
        previous=previous,
    )


def _restore_activation_failure(
    *,
    active_palace: Path,
    migrated_root: Path,
    previous_tmp: Path,
    active_moved: bool,
    staged_promoted: bool,
    old_sidecars_moved: list[str],
    new_sidecars_moved: list[str],
) -> list[str]:
    errors: list[str] = []

    def restore(source: Path, destination: Path) -> None:
        try:
            _replace_and_sync(source, destination)
        except OSError as exc:
            errors.append(f"{source} -> {destination}: {exc}")

    if staged_promoted:
        restore(active_palace, migrated_root / "palace")
    for name in reversed(new_sidecars_moved):
        restore(active_palace.parent / name, migrated_root / name)
    for name in reversed(old_sidecars_moved):
        restore(previous_tmp / name, active_palace.parent / name)
    if active_moved:
        restore(previous_tmp / "palace", active_palace)
    if not errors:
        shutil.rmtree(previous_tmp, ignore_errors=True)
    return errors


def activate_migrated_palace(
    *,
    active_palace: os.PathLike[str] | str,
    reviewed_manifest: os.PathLike[str] | str,
    migrated_root: os.PathLike[str] | str,
    previous_root: os.PathLike[str] | str,
) -> dict[str, Any]:
    """Promote a verified staging palace while retaining the previous palace."""
    active = _expanded(active_palace)
    migrated = _expanded(migrated_root)
    previous = _expanded(previous_root)
    recovered = _recover_interrupted_swap(
        expected_operation="activate",
        active=active,
        migrated=migrated,
        previous=previous,
    )
    if recovered is not None:
        return recovered
    staged = _validate_activation_paths(
        active_palace=active,
        migrated_root=migrated,
        previous_root=previous,
    )
    reviewed, apply_report, active_snapshot = _validate_reviewed_activation(
        active_palace=active,
        reviewed_manifest=reviewed_manifest,
        migrated_root=migrated,
        staged_palace=staged,
    )
    active_sidecars = _validate_sidecars(active.parent, _ACTIVATION_SIDECAR_NAMES)
    migrated_sidecars = _validate_sidecars(
        migrated,
        _ACTIVATION_SIDECAR_NAMES,
        expected=apply_report["derived_sidecars"],
    )
    config_path = active.parent / "config.json"
    if config_path.is_symlink():
        raise ValueError(f"active config must not be a symlink: {config_path}")
    _require_daemons_stopped([active, staged])

    previous_tmp = _temporary_sibling(previous)
    report = {
        "version": 1,
        "status": "active",
        "active_palace": str(active),
        "migrated_root": str(migrated),
        "previous_root": str(previous),
        "reviewed_snapshot": active_snapshot,
        "migrated_snapshot": apply_report["migrated_snapshot"],
        "migrated_logical_snapshot": apply_report["migrated_logical_snapshot"],
        "migrated_semantic_snapshot": apply_report["migrated_semantic_snapshot"],
        "active_sidecars": active_sidecars,
        "migrated_sidecars": migrated_sidecars,
    }
    try:
        previous_tmp.mkdir(mode=0o700, parents=False, exist_ok=False)
        if config_path.is_file():
            shutil.copy2(config_path, previous_tmp / "config.json")
        _write_private_json(previous_tmp / _ACTIVATION_REPORT, report)
        _reject_symlinks(active)
        _reject_symlinks(migrated)
        _make_owner_only(active)
        _make_owner_only(migrated)
        _make_owner_only(previous_tmp)
        for name in active_sidecars:
            os.chmod(active.parent / name, 0o600)
    except Exception:
        shutil.rmtree(previous_tmp, ignore_errors=True)
        raise

    active_moved = False
    staged_promoted = False
    old_sidecars_moved: list[str] = []
    new_sidecars_moved: list[str] = []
    journal_path = _journal_path(previous)
    try:
        with _exclusive_palace_locks([active, staged]):
            _require_daemons_stopped([active, staged])
            _validate_sidecars(
                active.parent,
                active_sidecars,
                expected=active_sidecars,
            )
            _validate_sidecars(
                migrated,
                migrated_sidecars,
                expected=migrated_sidecars,
            )
            _verify_staging_readiness(staged)
            _close_palace_handles(active, staged)
            if palace_snapshot(active) != active_snapshot:
                raise ValueError("active palace no longer matches the reviewed manifest")
            if palace_semantic_snapshot(staged) != apply_report["migrated_semantic_snapshot"]:
                raise ValueError("migrated palace changed before activation")

            _write_atomic_private_json(
                journal_path,
                {
                    "version": 1,
                    "operation": "activate",
                    "paths": {
                        "active_palace": str(active),
                        "migrated_root": str(migrated),
                        "previous_root": str(previous),
                    },
                    "previous_tmp": str(previous_tmp),
                    "report": report,
                },
            )
            _replace_and_sync(active, previous_tmp / "palace")
            active_moved = True
            for name in active_sidecars:
                _replace_and_sync(active.parent / name, previous_tmp / name)
                old_sidecars_moved.append(name)
            for name in migrated_sidecars:
                _replace_and_sync(migrated / name, active.parent / name)
                new_sidecars_moved.append(name)
            _replace_and_sync(staged, active)
            staged_promoted = True
            _replace_and_sync(previous_tmp, previous)
    except Exception as exc:
        errors = _restore_activation_failure(
            active_palace=active,
            migrated_root=migrated,
            previous_tmp=previous_tmp,
            active_moved=active_moved,
            staged_promoted=staged_promoted,
            old_sidecars_moved=old_sidecars_moved,
            new_sidecars_moved=new_sidecars_moved,
        )
        if errors:
            raise RuntimeError(
                "activation failed and automatic restoration was incomplete: " + "; ".join(errors)
            ) from exc
        _remove_journal(journal_path)
        raise

    _remove_journal(journal_path)
    return report


def _validate_rollback_state(
    *,
    active: Path,
    migrated: Path,
    previous: Path,
) -> tuple[
    dict[str, Any],
    dict[str, dict[str, int | str]],
    dict[str, dict[str, int | str]],
]:
    _require_real_directory(active, label="active palace")
    _require_real_directory(migrated, label="migrated bundle")
    _require_real_directory(previous, label="previous activation slot")
    state = _read_json_object(previous / _ACTIVATION_REPORT, label="activation report")
    expected_paths = {
        "active_palace": str(active),
        "migrated_root": str(migrated),
        "previous_root": str(previous),
    }
    if state.get("version") != 1 or state.get("status") != "active":
        raise ValueError("activation report is not in an active state")
    if any(state.get(key) != value for key, value in expected_paths.items()):
        raise ValueError("activation report paths do not match this rollback request")
    old_palace = previous / "palace"
    _require_real_directory(old_palace, label="retained previous palace")
    if (migrated / "palace").exists() or (migrated / "palace").is_symlink():
        raise ValueError("migrated bundle palace destination must be empty for rollback")
    if palace_semantic_snapshot(active) != state.get("migrated_semantic_snapshot"):
        raise ValueError("active migrated palace changed after activation")
    if palace_snapshot(old_palace) != state.get("reviewed_snapshot"):
        raise ValueError("retained previous palace changed after activation")
    active_sidecars = state.get("active_sidecars")
    migrated_sidecars = state.get("migrated_sidecars")
    if not isinstance(active_sidecars, dict) or not isinstance(migrated_sidecars, dict):
        raise ValueError("activation report is missing sidecar snapshots")
    sidecar_names = [*active_sidecars, *migrated_sidecars]
    if any(name not in _ACTIVATION_SIDECAR_NAMES for name in sidecar_names):
        raise ValueError("activation report contains an unsupported sidecar")
    for name in migrated_sidecars:
        if (migrated / name).exists() or (migrated / name).is_symlink():
            raise ValueError(f"migrated sidecar destination is not empty: {name}")
    return state, active_sidecars, migrated_sidecars


def _restore_rollback_failure(
    *,
    active: Path,
    migrated: Path,
    previous: Path,
    active_moved: bool,
    previous_promoted: bool,
    old_sidecars_promoted: list[str],
    new_sidecars_moved: list[str],
) -> list[str]:
    errors: list[str] = []

    def restore(source: Path, destination: Path) -> None:
        try:
            _replace_and_sync(source, destination)
        except OSError as exc:
            errors.append(f"{source} -> {destination}: {exc}")

    if previous_promoted:
        restore(active, previous / "palace")
    for name in reversed(old_sidecars_promoted):
        restore(active.parent / name, previous / name)
    for name in reversed(new_sidecars_moved):
        restore(migrated / name, active.parent / name)
    if active_moved:
        restore(migrated / "palace", active)
    return errors


def rollback_activated_palace(
    *,
    active_palace: os.PathLike[str] | str,
    migrated_root: os.PathLike[str] | str,
    previous_root: os.PathLike[str] | str,
) -> dict[str, Any]:
    """Reverse one reviewed activation without deleting either palace."""
    active = _expanded(active_palace)
    migrated = _expanded(migrated_root)
    previous = _expanded(previous_root)
    recovered = _recover_interrupted_swap(
        expected_operation="rollback",
        active=active,
        migrated=migrated,
        previous=previous,
    )
    if recovered is not None:
        return recovered
    state, active_sidecars, migrated_sidecars = _validate_rollback_state(
        active=active,
        migrated=migrated,
        previous=previous,
    )
    old_palace = previous / "palace"
    staged_palace = migrated / "palace"
    _require_daemons_stopped([active, old_palace, staged_palace])
    _reject_symlinks(active)
    _reject_symlinks(previous)
    _reject_symlinks(migrated)
    _make_owner_only(active)
    _make_owner_only(previous)
    _make_owner_only(migrated)
    for name in migrated_sidecars:
        os.chmod(active.parent / name, 0o600)
    rolled_back = {**state, "status": "rolled_back"}

    active_moved = False
    previous_promoted = False
    old_sidecars_promoted: list[str] = []
    new_sidecars_moved: list[str] = []
    journal_path = _journal_path(previous)
    try:
        with _exclusive_palace_locks([active, old_palace, staged_palace]):
            _require_daemons_stopped([active, old_palace, staged_palace])
            _validate_sidecars(
                active.parent,
                migrated_sidecars,
                expected=migrated_sidecars,
            )
            _validate_sidecars(
                previous,
                active_sidecars,
                expected=active_sidecars,
            )
            _close_palace_handles(active, old_palace)
            if palace_semantic_snapshot(active) != state["migrated_semantic_snapshot"]:
                raise ValueError("active migrated palace changed before rollback")
            if palace_snapshot(previous / "palace") != state["reviewed_snapshot"]:
                raise ValueError("retained previous palace changed before rollback")

            _write_atomic_private_json(
                journal_path,
                {
                    "version": 1,
                    "operation": "rollback",
                    "paths": {
                        "active_palace": str(active),
                        "migrated_root": str(migrated),
                        "previous_root": str(previous),
                    },
                    "report": state,
                    "rolled_back_report": rolled_back,
                },
            )
            _replace_and_sync(active, migrated / "palace")
            active_moved = True
            for name in migrated_sidecars:
                _replace_and_sync(active.parent / name, migrated / name)
                new_sidecars_moved.append(name)
            for name in active_sidecars:
                _replace_and_sync(previous / name, active.parent / name)
                old_sidecars_promoted.append(name)
            _replace_and_sync(previous / "palace", active)
            previous_promoted = True
            _write_atomic_private_json(previous / _ACTIVATION_REPORT, rolled_back)
    except Exception as exc:
        errors = _restore_rollback_failure(
            active=active,
            migrated=migrated,
            previous=previous,
            active_moved=active_moved,
            previous_promoted=previous_promoted,
            old_sidecars_promoted=old_sidecars_promoted,
            new_sidecars_moved=new_sidecars_moved,
        )
        if errors:
            raise RuntimeError(
                "rollback failed and automatic restoration was incomplete: " + "; ".join(errors)
            ) from exc
        _remove_journal(journal_path)
        raise

    _remove_journal(journal_path)
    return rolled_back
