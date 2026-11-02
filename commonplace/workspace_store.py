"""One resolver and thread factory for opt-in workspace store generations."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path

POINTER = ".store-pointer.json"
POINTER_PREVIOUS = ".store-pointer.prev.json"
INVENTORIES = ".store-retired-files.json"
PUBLICATION = ".store-publication.json"
POINTER_REPAIR = (
    "Invalid or missing workspace generation pointer; stop the gateway and verify "
    f"{POINTER_PREVIOUS} before restoring {POINTER}"
)
SETTINGS = "workspace.json"
STATE = ".store-backfill.json"
RECEIPT = ".store-backup.json"
DEFAULT_MAX_RECORDS = 100_000


def read_json(path, default=None):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {} if default is None else default
    if not isinstance(value, dict):
        raise ValueError("Workspace store configuration must be an object")
    return value


def evidence_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode("utf-8")).hexdigest()


def retired_files(root, item):
    """Read structural evidence only for maintenance, never runtime resolution."""
    expected = item.get("expected_files")
    if "inventory" in item:
        try:
            expected = read_json(root / INVENTORIES)[item["inventory"]]
            if not isinstance(expected, list) or evidence_digest(expected) != item["inventory"]:
                raise ValueError
        except (KeyError, TypeError, ValueError, OSError):
            raise ValueError(
                f"Invalid or missing retired generation inventory in {INVENTORIES}; "
                "preserve generations and repair it from verified evidence"
            ) from None
    if expected is not None and (
        not isinstance(expected, list)
        or any(
            not isinstance(name, str) or Path(name).is_absolute() or ".." in Path(name).parts
            for name in expected
        )
    ):
        raise ValueError("Invalid retired generation file inventory")
    return expected


def publication_for_adoption(root, state, previous):
    """A pointer-loss hint needs a complete, checked publication and history."""
    try:
        if state.get("state") not in ("copying", "published"):
            return None
        receipt = read_json(root / PUBLICATION)
        pointer = receipt["pointer"]
        validate_pointer(root, pointer)
        validate_pointer(root, previous)
        active = pointer["active"]
        directory = generation_path(root, active)
        marker = read_json(directory / ".store-comparison.json")
        retained = pointer["retained"]
        if (
            directory == root
            or active != state.get("staging")
            or state.get("state") == "published"
            and state.get("pointer") != pointer
            or evidence_digest(pointer) != receipt["digest"]
            or receipt["store"] != settings_for(directory).get("store", "v1")
            or not retained
            or marker["previous"] != retained[-1]["name"]
            or not (
                pointer == previous
                or retained[:-1] == previous.get("retained", [])
                and retained[-1]["name"] == previous["active"]
            )
        ):
            return None
        for item in retained:
            retired_files(root, item)
        return pointer
    except (ValueError, KeyError, TypeError, OSError):
        return None


def workspace_root(data_dir, workspace_id):
    if not workspace_id.startswith("ws_") or not workspace_id[3:].isalnum():
        raise ValueError("Invalid workspace id")
    return Path(data_dir) / "workspaces" / workspace_id


def generation_path(root, name):
    root = Path(root)
    if root.is_symlink():
        raise ValueError("Workspace root cannot be a symlink")
    if name == root.name:
        return root
    if not re.fullmatch(re.escape(root.name) + r"\.generation-[0-9a-f]{24}", name):
        raise ValueError("Invalid workspace generation")
    target = root.parent / name
    if target.is_symlink():
        raise ValueError("Workspace generation cannot be a symlink")
    return target


def resolve_workspace(root):
    root = Path(root)
    if root.is_symlink():
        raise ValueError("Workspace root cannot be a symlink")
    if not (root / POINTER).exists():
        if (root / POINTER_PREVIOUS).exists() or (root / POINTER_PREVIOUS).is_symlink():
            raise ValueError(previous_without_pointer_repair(root))
        if any(root.parent.glob(root.name + ".generation-*")):
            raise ValueError(POINTER_REPAIR)
        return root
    try:
        pointer = read_json(root / POINTER)
    except ValueError:
        raise ValueError(POINTER_REPAIR) from None
    return validate_pointer(root, pointer)


def previous_without_pointer_repair(root):
    choices = []
    state = {}
    try:
        state = read_json(root / STATE)
        published = state.get("pointer", {})
        validate_pointer(root, published)
        recoverable = (
            state.get("state") == "published"
            and published["active"] == state.get("staging")
            and generation_path(root, published["active"]) != root
        )
    except (ValueError, OSError):
        recoverable = False
    try:
        previous = read_json(root / POINTER_PREVIOUS)
        validate_pointer(root, previous)
        if not recoverable or {"retained": [], **previous} == {"retained": [], **published}:
            choices.append(f"restore {POINTER_PREVIOUS} as {POINTER} to recover its history")
    except (ValueError, OSError):
        previous = None
        if not recoverable:
            choices.append(
                f"repair {POINTER_PREVIOUS} from verified evidence before restoring {POINTER}"
            )
    if recoverable:
        choices.append(
            f"delete {POINTER_PREVIOUS} and rerun workspace prune-stores to republish "
            f"the recorded generation {published['active']}"
        )
    elif not any(root.parent.glob(root.name + ".generation-*")):
        choices.append(f"delete {POINTER_PREVIOUS} to accept the original workspace root")
    candidate = publication_for_adoption(root, state, previous)
    if candidate is not None:
        choices.append(
            f"adopt {candidate['active']} by atomically restoring the full pointer object "
            f"from {PUBLICATION} as {POINTER}, then rerun workspace prune-stores"
        )
    return (
        f"Missing workspace generation pointer: {POINTER_PREVIOUS} exists without {POINTER}; "
        "verify the selected data, then " + ", or ".join(choices)
    )


def validate_pointer(root, pointer):
    try:
        if not isinstance(pointer, dict):
            raise ValueError
        if not isinstance(pointer.get("active"), str):
            raise ValueError
        active = generation_path(root, pointer["active"])
        if not active.is_dir():
            raise ValueError
        retained = pointer.get("retained", [])
        if not isinstance(retained, list):
            raise ValueError
        for item in retained:
            generation_path(root, item["name"])
            if item["name"] == pointer["active"]:
                raise ValueError
            float(item["retired_at"])
    except (ValueError, KeyError, TypeError):
        raise ValueError(POINTER_REPAIR) from None
    return active


def workspace_path(data_dir, workspace_id):
    return resolve_workspace(workspace_root(data_dir, workspace_id))


def settings_for(directory):
    settings = read_json(Path(directory) / SETTINGS)
    if settings.get("store", "v1") not in ("v1", "compact"):
        raise ValueError("Workspace store must be v1 or compact")
    return settings


def migration_limit(directory):
    limit = settings_for(directory).get("compact_max_collection_records", DEFAULT_MAX_RECORDS)
    if type(limit) is not int or limit < 1:
        raise ValueError("Compact collection size limit must be a positive integer")
    return limit


def thread_path(directory):
    compact = settings_for(directory).get("store", "v1") == "compact"
    return Path(directory) / ("threads.v2.sqlite3" if compact else "threads.sqlite3")


def open_threads(directory, *, sample_every=100):
    from .handoff.thread_store import ThreadStore
    from .handoff.thread_store_v2 import ThreadStoreV2

    directory = Path(directory)
    path = thread_path(directory)
    other = directory / (
        "threads.sqlite3" if path.name == "threads.v2.sqlite3" else "threads.v2.sqlite3"
    )
    if not path.exists() and other.exists():
        raise ValueError(
            "Workspace thread format disagrees with settings; a verified store switch is required"
        )
    store = (ThreadStoreV2 if path.name == "threads.v2.sqlite3" else ThreadStore)(str(path))
    try:
        comparison = read_json(directory / ".store-comparison.json")
    except ValueError:
        return store
    if comparison:
        previous = directory.parent / comparison["previous"]
        if previous.is_dir() and thread_path(previous).exists():
            from mempalace.dual_read import SampledReads

            from .handoff.thread_store_migrate import read_store

            context = read_store(thread_path(previous), compact=comparison["store"] == "compact")
            try:
                old = context.__enter__()
            except (OSError, ValueError, sqlite3.Error):
                return store
            reader = SampledReads(
                store,
                old,
                reads=("get", "messages", "chunks", "search", "doc", "parent_of", "by_source"),
                sample_every=sample_every,
            )

            def close():
                try:
                    store.close()
                finally:
                    context.__exit__(None, None, None)

            reader.close = close
            return reader
    return store


def worker_config(directory):
    """Workspace settings override backend environment selection."""
    from mempalace.fsio import atomic_write_json

    directory = Path(directory)
    settings = settings_for(directory)
    path = directory / "config" / "config.json"
    config = read_json(path)
    desired = "compact" if settings.get("store", "v1") == "compact" else "chroma"
    if config.get("backend") != desired:
        config["backend"] = desired
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_write_json(path, config)
    return config
