"""Offline, backup-gated and restartable workspace store cutovers.

One durable generation pointer publishes palace, threads, boards and settings
at once. Interrupted staging generations are never served. Old generations
remain available for comparison for at least fourteen days.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import shlex
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
from contextlib import closing
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from mempalace.fsio import atomic_write_json, atomic_write_text, fsync_directory

from .backup import BackupError, _snapshot, _verify_offline, data_lock
from .workspace_store import (
    DEFAULT_MAX_RECORDS,
    INVENTORIES,
    POINTER,
    POINTER_PREVIOUS,
    POINTER_REPAIR,
    PUBLICATION,
    RECEIPT,
    SETTINGS,
    STATE,
    evidence_digest,
    generation_path,
    migration_limit,
    read_json,
    resolve_workspace,
    retired_files,
    settings_for,
    thread_path,
    validate_pointer,
    worker_config,
    workspace_root,
)

RETENTION_SECONDS = 14 * 24 * 60 * 60
BACKUP_STDERR = ".store-backup.stderr.log"
_ADMIN_FILES = frozenset(
    {POINTER, POINTER_PREVIOUS, INVENTORIES, PUBLICATION, RECEIPT, STATE, BACKUP_STDERR}
)
logger = logging.getLogger(__name__)


def _write_state(root, state):
    previous = read_json(root / STATE)
    if "prune_findings" in previous and "prune_findings" not in state:
        state = dict(state, prune_findings=previous["prune_findings"])
    atomic_write_json(root / STATE, state, follow_symlinks=False)


def _write_pointer(root, pointer, *, store=None, checkpoint=lambda step: None):
    atomic_write_json(root / POINTER, pointer, follow_symlinks=False)
    if store is not None:
        atomic_write_json(
            root / PUBLICATION,
            {"pointer": pointer, "digest": evidence_digest(pointer), "store": store},
            follow_symlinks=False,
        )
        checkpoint("pointer")
        _record_published(root, pointer, store)
        checkpoint("publish_state")
    atomic_write_json(root / POINTER_PREVIOUS, pointer, follow_symlinks=False)
    if store is not None:
        checkpoint("pointer_mirrored")


def _write_inventory(root, files):
    inventories = read_json(root / INVENTORIES)
    digest = evidence_digest(files)
    inventories[digest] = files
    atomic_write_json(root / INVENTORIES, inventories, follow_symlinks=False)
    return digest


def _record_published(root, pointer, store):
    _write_state(
        root,
        {
            "state": "published",
            "staging": pointer["active"],
            "store": store,
            "pointer": pointer,
            "sealing": "pending",
        },
    )


def _finish_publication(root, pointer, state):
    try:
        _seal_retired(root, pointer)
    except BaseException:
        _write_state(root, dict(state, sealing="failed"))
        raise
    _write_state(
        root,
        {"state": "complete", "store": state["store"], "sealing": "complete"},
    )


def _publication_pointer(root, state):
    if state.get("state") not in ("copying", "published"):
        return None
    candidates = []
    if state.get("state") == "published":
        candidates.append(state.get("pointer"))
    for filename in (POINTER, POINTER_PREVIOUS):
        try:
            candidates.append(read_json(root / filename))
        except (ValueError, OSError):
            continue
    for pointer in candidates:
        try:
            validate_pointer(root, pointer)
        except (ValueError, TypeError):
            continue
        if pointer["active"] != state["staging"]:
            continue
        return pointer
    return None


def _publication_conflict(root, candidate):
    if candidate is None:
        return None
    try:
        main = read_json(root / POINTER)
        validate_pointer(root, main)
    except (ValueError, OSError):
        return None
    if main["active"] != candidate["active"]:
        return (
            "Publication recovery conflicts with the valid main pointer: "
            f"{main['active']} differs from {candidate['active']}; "
            "preserve both generations and reconcile the pointers before retrying"
        )
    if not _pointers_equal(main, candidate):
        difference = _previous_pointer_difference(root, main)
        return (difference or {}).get("repair") or (
            "Publication recovery conflicts with the valid main pointer's history; "
            "reconcile the main pointer and recorded publication before retrying"
        )
    return None


def _recover_publication(root):
    candidate = _publication_pointer(root, _backfill_state(root))
    conflict = _publication_conflict(root, candidate)
    if conflict:
        raise ValueError(conflict)
    if candidate is not None:
        for item in candidate.get("retained", []):
            retired_files(root, item)
        previous, parsed = _read_previous_pointer(root)
        if (
            parsed
            and not _pointers_equal(previous, candidate)
            and not _publication_extends_previous(root, candidate, previous)
        ):
            _refresh_previous_pointer(root, candidate)
        store = settings_for(generation_path(root, candidate["active"])).get("store", "v1")
        _write_pointer(root, candidate, store=store)
        _finish_publication(root, candidate, _backfill_state(root))


def _publication_extends_previous(root, pointer, previous):
    """Recognize the intact history of an interrupted tool-written publication."""
    try:
        validate_pointer(root, previous)
        retained = pointer["retained"]
        marker = read_json(generation_path(root, pointer["active"]) / ".store-comparison.json")
        return (
            pointer["active"] != previous["active"]
            and retained[:-1] == previous.get("retained", [])
            and retained[-1]["name"] == previous["active"]
            and marker["previous"] == previous["active"]
        )
    except (ValueError, OSError, KeyError, TypeError, IndexError):
        return False


def _backfill_state(root):
    try:
        state = read_json(root / STATE)
        if state and state.get("state") not in (
            "copying",
            "published",
            "complete",
            "failed",
            "deleting",
        ):
            raise ValueError
        if state.get("state") in ("copying", "published"):
            if generation_path(root, state["staging"]) == root:
                raise ValueError
        if state.get("state") == "deleting":
            deletion = state["deletion"]
            generation_path(root, deletion["entry"]["name"])
            validate_pointer(root, deletion["pointer"])
            if not isinstance(deletion["files"], dict):
                raise ValueError
        if "prune_findings" in state:
            if not isinstance(state["prune_findings"], list):
                raise ValueError
            for finding in state["prune_findings"]:
                generation_path(root, finding["generation"])
                if finding["kind"] not in (
                    "incomplete (interrupted deletion)",
                    "newer writes",
                    "changed since retirement",
                ):
                    raise ValueError
                float(finding["at"])
        return state
    except (ValueError, KeyError, TypeError):
        raise ValueError(
            f"Corrupt workspace backfill state; repair {STATE} from verified evidence; "
            "prune-stores provides a read-only report until repaired"
        ) from None


def newest_write(directory, *, exclude=()):
    """Latest durable data mtime, excluding diagnostics and disposable model cache."""
    newest = 0

    def visit(path):
        nonlocal newest
        if (
            path.name in _ADMIN_FILES
            or path.name in exclude
            or path.name == "worker.log"
            or path.name == "chroma"
            and path.parent.name == ".cache"
        ):
            return
        if path.is_symlink():
            return
        if path.is_dir():
            for child in path.iterdir():
                visit(child)
        elif not path.name.endswith(("-shm", "-journal")):
            stat = path.stat()
            if not (path.name.endswith("-wal") and stat.st_size == 0):
                newest = max(newest, stat.st_mtime_ns)

    if Path(directory).exists():
        visit(Path(directory))
    return newest


def _retirement_digest(directory, *, entries=None):
    """Detect added/changed data even within the filesystem's mtime resolution."""
    directory = Path(directory)
    checksum = hashlib.sha256()

    def visit(path):
        if (
            path.name in _ADMIN_FILES
            or path.name == "worker.log"
            or path.name == "chroma"
            and path.parent.name == ".cache"
        ):
            return
        if path.is_symlink():
            checksum.update(path.relative_to(directory).as_posix().encode("utf-8"))
            checksum.update(os.fsencode(os.readlink(path)))
            if entries is not None:
                entries[path.relative_to(directory).as_posix()] = ["symlink", os.readlink(path)]
        elif path.is_dir():
            for child in sorted(path.iterdir()):
                visit(child)
        elif path.is_file():
            if (
                path.name.endswith(("-shm", "-journal"))
                or path.name.endswith("-wal")
                and path.stat().st_size == 0
            ):
                return
            checksum.update(
                json.dumps([path.relative_to(directory).as_posix(), path.stat().st_size]).encode(
                    "utf-8"
                )
            )
            with path.open("rb") as stream:
                file_hash = hashlib.sha256()
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    checksum.update(block)
                    if entries is not None:
                        file_hash.update(block)
            if entries is not None:
                entries[path.relative_to(directory).as_posix()] = ["file", file_hash.hexdigest()]

    if directory.exists():
        visit(directory)
    return checksum.hexdigest()


def _staging_paths(staging):
    return [
        staging.with_name(staging.name + suffix)
        for suffix in (
            "",
            ".source-palace",
            ".probe",
            ".rollback-probe",
            ".probe.source-palace",
            ".rollback-probe.source-palace",
        )
    ]


def _preflight_maintenance_metadata(root):
    """Inspect every maintenance target without resolving or repairing pointers."""
    try:
        generation_path(root, root.name)
        directories = {root}
        pattern = re.compile(re.escape(root.name) + r"\.generation-[0-9a-f]{24}")
        for path in root.parent.iterdir():
            match = pattern.match(path.name)
            if match:
                staging = generation_path(root, match.group())
                if path in _staging_paths(staging):
                    directories.add(path)
        for directory in sorted(directories):
            try:
                mode = directory.lstat().st_mode
            except FileNotFoundError:
                continue
            if not stat.S_ISDIR(mode):
                raise ValueError("Workspace maintenance requires directories without symlinks")
            config = directory / SETTINGS
            try:
                mode = config.lstat().st_mode
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(mode):
                raise ValueError("Workspace settings must be regular files without symlinks")
            settings = json.loads(config.read_text(encoding="utf-8"))
            if not isinstance(settings, dict):
                raise ValueError("Invalid workspace maintenance settings")
            if "encryption" in settings:
                raise ValueError(
                    "Encrypted workspace maintenance requires a verified recovery plan"
                )
    except OSError:
        raise ValueError("Workspace maintenance metadata is unavailable") from None


def _remove_staging(staging):
    for path in _staging_paths(staging):
        if path.is_symlink():
            raise ValueError("Refusing symlink in staging cleanup")
        if path.exists():
            shutil.rmtree(path)


def _reset_staging_state(root, pointer):
    state = _backfill_state(root)
    if state.get("state") != "copying" or not state.get("staging"):
        return
    if state["staging"] == pointer.get("active"):
        status = "complete"
    else:
        staging = generation_path(root, state["staging"])
        if staging.exists():
            return
        status = "failed"
    _write_state(root, {"state": status})


def _writes_after_markers(directory):
    markers = [
        directory / ".store-comparison.json",
        directory / "palace" / ".store-comparison.json",
    ]
    stamps = [path.stat().st_mtime_ns for path in markers if path.is_file()]
    return bool(stamps) and newest_write(directory, exclude={".store-comparison.json"}) > min(
        stamps
    )


def _cleanup_recorded_staging(root):
    """Without a valid pointer, remove only provably unpublished recorded work."""
    state = _backfill_state(root)
    if state.get("state") != "copying" or not state.get("staging"):
        return
    staging = generation_path(root, state["staging"])
    # Publication can precede the final state update. A fully prepared generation
    # may have been active before the pointer was lost; preserve it for recovery.
    if staging == root or (staging / ".store-comparison.json").exists():
        return
    _remove_staging(staging)
    _reset_staging_state(root, {})


def _prepare_generations(root):
    """Validate publication evidence before allowing any orphan cleanup."""
    if not (root / POINTER).exists() and (
        (root / POINTER_PREVIOUS).exists() or (root / POINTER_PREVIOUS).is_symlink()
    ):
        resolve_workspace(root)
    _recover_publication(root)
    try:
        pointer = read_json(root / POINTER)
        if (root / POINTER).exists():
            resolve_workspace(root)
        else:
            _cleanup_recorded_staging(root)
            resolve_workspace(root)
            return {}
    except (ValueError, KeyError, TypeError):
        _cleanup_recorded_staging(root)
        raise ValueError(POINTER_REPAIR) from None
    for item in pointer.get("retained", []):
        retired_files(root, item)
    _refresh_previous_pointer(root, pointer)
    _cleanup_unpublished(root, pointer)
    return pointer


def _cleanup_unpublished(root, pointer):
    state = _backfill_state(root)
    recorded = state.get("staging") if state.get("state") == "copying" else None
    protected = {
        root.name,
        pointer.get("active"),
        *(item["name"] for item in pointer.get("retained", [])),
    }
    pattern = re.compile(re.escape(root.name) + r"\.generation-[0-9a-f]{24}")
    for path in root.parent.iterdir():
        match = pattern.match(path.name)
        if not match:
            continue
        base = match.group()
        # Match only this command's generation and scratch names.
        staging = generation_path(root, base)
        if path not in _staging_paths(staging):
            continue
        if path.name == base and base in protected:
            continue
        if path.is_symlink():
            raise ValueError("Refusing symlink in staging cleanup")
        if path.is_dir():
            if (
                path.name == base
                and (path / ".store-comparison.json").exists()
                and not (
                    base == recorded and base not in protected and not _writes_after_markers(path)
                )
            ):
                logger.warning(
                    "Preserving unlisted published generation %s; recover its retained entry from %s or commonplace backup, or follow the documented manual removal procedure",
                    path.name,
                    POINTER_PREVIOUS,
                )
                continue
            shutil.rmtree(path)
    _reset_staging_state(root, pointer)


def _seal_retired(root, pointer):
    for item in pointer.get("retained", []):
        directory = generation_path(root, item["name"])
        if directory.name == pointer.get("active") or not directory.exists():
            continue
        for path in directory.rglob("*"):
            if path.parent == root and path.name in _ADMIN_FILES:
                continue
            if path.is_file() and not path.is_symlink():
                path.chmod(path.stat().st_mode & ~0o222)


def _read_previous_pointer(root):
    try:
        return json.loads((root / POINTER_PREVIOUS).read_text(encoding="utf-8")), True
    except (FileNotFoundError, UnicodeError, json.JSONDecodeError):
        return None, False


def _pointers_equal(first, second):
    def normalized(pointer):
        return {"retained": [], **pointer} if isinstance(pointer, dict) else pointer

    return normalized(first) == normalized(second)


def _generation_gone(root, directory):
    return (
        not directory.exists()
        or directory == root
        and all(
            child.name in _ADMIN_FILES and child.is_file() and not child.is_symlink()
            for child in root.iterdir()
        )
    )


def _emptied_retained_root(root, pointer):
    if any(item["name"] == root.name for item in pointer.get("retained", [])) and _generation_gone(
        root, root
    ):
        return root.name
    return None


def _emptied_root_repair(name):
    return (
        f"Retained root {name} contains only store administration files; "
        "rerun workspace prune-stores to complete the interrupted prune"
    )


def _deletion_repair(name, *, recorded):
    if recorded:
        return f"Interrupted deletion of {name}; the next workspace prune-stores finishes it"
    return (
        f"Interrupted deletion or fingerprint mismatch of {name} without a deletion record; "
        "preserve and verify its remaining contents with commonplace backup, then follow "
        "the documented manual removal procedure"
    )


def _legacy_required_file(directory):
    if not (directory / "palace").is_dir():
        return None
    compact = (directory / "threads.v2.sqlite3").exists() or (
        directory / "palace" / "compact.sqlite3"
    ).exists()
    chroma = (directory / "threads.sqlite3").exists() or (
        directory / "palace" / "chroma.sqlite3"
    ).exists()
    if compact == chroma:
        return None
    if compact and not (directory / "palace" / "compact.sqlite3").exists():
        return None
    return "palace/compact.sqlite3" if compact else "palace/chroma.sqlite3"


def _missing_retired_files(root, directory, item):
    expected = retired_files(root, item)
    if expected is None:
        required = _legacy_required_file(directory)
        expected = [required] if required else []
    return [
        name
        for name in expected
        if not (directory / name).exists() and not (directory / name).is_symlink()
    ]


def _retired_kind(root, item, *, current=None):
    directory = generation_path(root, item["name"])
    digest = item.get("digest")
    if current is None and digest:
        current = _retirement_digest(directory)
    expected = retired_files(root, item)
    if expected is None and digest and current == digest:
        return None
    if _missing_retired_files(root, directory, item):
        return "incomplete (interrupted deletion)"
    changed = bool(digest) and current != digest
    newer = newest_write(directory) > int(item["retired_at"] * 1_000_000_000)
    if changed and newer:
        return "newer writes"
    if not digest or changed or newer:
        return "changed since retirement"
    return None


def _retired_repair(name, kind, *, recorded=False):
    if recorded and kind == "incomplete (interrupted deletion)":
        return _deletion_repair(name, recorded=True)
    description = (
        f"Interrupted deletion of {name} is blocked: {kind}"
        if recorded
        else f"Generation {name} is {kind}"
    )
    return (
        description + "; preserve and verify its remaining contents with commonplace backup, "
        "then follow the documented manual removal procedure"
    )


def _save_prune_findings(root, findings):
    state = _backfill_state(root)
    if findings or state.get("prune_findings"):
        _write_state(
            root, dict(state, state=state.get("state", "complete"), prune_findings=findings)
        )


def _retired_findings(root, pointer, state, *, inspect_expired=False, now=None):
    known = {item["generation"]: item["kind"] for item in state.get("prune_findings", [])}
    findings = []
    pending = None
    if state.get("state") == "deleting":
        pending = state["deletion"]["entry"]["name"]
        kind = known.get(pending, "incomplete (interrupted deletion)")
        findings.append(
            {
                "name": pending,
                "kind": kind,
                "recorded": True,
                "repair": _retired_repair(pending, kind, recorded=True),
            }
        )
    for item in pointer.get("retained", []):
        name = item["name"]
        directory = generation_path(root, name)
        if name == pending or _generation_gone(root, directory):
            continue
        missing = _missing_retired_files(root, directory, item)
        if (
            inspect_expired
            and (time.time() if now is None else now) - item["retired_at"] >= RETENTION_SECONDS
        ):
            kind = _retired_kind(root, item)
        elif missing:
            kind = "incomplete (interrupted deletion)"
        else:
            kind = known.get(name)
            if retired_files(root, item) is None and kind == "incomplete (interrupted deletion)":
                kind = None
            if kind is None and newest_write(directory) > int(item["retired_at"] * 1_000_000_000):
                kind = "changed since retirement"
        if kind:
            findings.append(
                {
                    "name": name,
                    "kind": kind,
                    "recorded": False,
                    "missing_files": missing,
                    "repair": _retired_repair(name, kind),
                }
            )
    return findings


def _remove_retired_directory(root, directory):
    if directory == root:
        for child in directory.iterdir():
            if child.name not in _ADMIN_FILES:
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink()
        fsync_directory(root)
    else:
        if directory.exists():
            shutil.rmtree(directory)
        fsync_directory(directory.parent)


def _finish_deletion(root, pointer, state, *, remaining=None):
    deletion = state["deletion"]
    item, recorded = deletion["entry"], deletion["pointer"]
    name = item["name"]
    if (
        name == pointer["active"]
        or item not in recorded.get("retained", [])
        or not (
            _pointers_equal(pointer, recorded)
            or _only_gone_retained_entries(root, pointer, recorded)
        )
    ):
        raise ValueError(
            f"Recorded deletion of {name} conflicts with the current pointer; preserve both"
        )
    directory = generation_path(root, name)
    if remaining is None:
        remaining = {}
        _retirement_digest(directory, entries=remaining)
    changed = sorted(
        path for path, value in remaining.items() if deletion["files"].get(path) != value
    )
    if changed:
        _save_prune_findings(
            root, [{"generation": name, "kind": "changed since retirement", "at": time.time()}]
        )
        raise ValueError(
            f"Refusing to finish deletion of {name}: remaining files changed or were added: "
            + ", ".join(changed)
        )
    _remove_retired_directory(root, directory)
    pointer = dict(
        pointer, retained=[entry for entry in pointer.get("retained", []) if entry["name"] != name]
    )
    _write_pointer(root, pointer)
    _write_state(
        root,
        {"state": "complete", "store": state.get("store", "v1"), "sealing": "complete"},
    )
    return pointer


def _start_deletion(root, pointer, item, files):
    state = {
        "state": "deleting",
        "store": settings_for(generation_path(root, pointer["active"])).get("store", "v1"),
        "deletion": {"entry": item, "pointer": pointer, "files": files},
    }
    _write_state(root, state)
    return _finish_deletion(root, pointer, state, remaining=files)


def _only_gone_retained_entries(root, pointer, previous):
    if not isinstance(previous, dict):
        return False
    if {key: value for key, value in previous.items() if key != "retained"} != {
        key: value for key, value in pointer.items() if key != "retained"
    }:
        return False
    try:
        before = {item["name"]: item for item in previous.get("retained", [])}
        after = {item["name"]: item for item in pointer.get("retained", [])}
        removed = before.keys() - after.keys()
        return (
            bool(removed)
            and all(before.get(name) == item for name, item in after.items())
            and all(_generation_gone(root, generation_path(root, name)) for name in removed)
        )
    except (KeyError, TypeError, ValueError):
        return False


def _previous_pointer_repair(entries):
    return (
        "Pointer differs from the last-good prev copy (entries: "
        + (", ".join(entries) or "unavailable")
        + f"); restore {POINTER_PREVIOUS} over {POINTER} to undo the pointer change, "
        + f"or delete {POINTER_PREVIOUS} to accept the current pointer"
    )


def _refresh_previous_pointer(root, pointer):
    """Refuse divergent history before cleanup, backfill or pointer writes."""
    try:
        previous, parsed = _read_previous_pointer(root)
    except OSError:
        raise ValueError(_previous_pointer_repair([])) from None
    if parsed and _pointers_equal(previous, pointer):
        return
    if not parsed or _only_gone_retained_entries(root, pointer, previous):
        atomic_write_json(root / POINTER_PREVIOUS, pointer, follow_symlinks=False)
    else:
        difference = _previous_pointer_difference(root, pointer)
        raise ValueError(difference["repair"])


def _previous_pointer_difference(root, pointer):
    if not pointer:
        return None
    try:
        previous, parsed = _read_previous_pointer(root)
    except OSError:
        return {
            "message": "prev copy could not be read",
            "entries": [],
            "repair": _previous_pointer_repair([]),
        }
    if not parsed:
        return {
            "message": "prev copy is missing or cannot be parsed",
            "entries": [],
            "repair": "Run workspace prune-stores to refresh the prev copy from the valid main pointer",
        }
    if _pointers_equal(previous, pointer):
        return None
    try:
        before = {item["name"]: item for item in previous.get("retained", [])}
        after = {item["name"]: item for item in pointer.get("retained", [])}
        entries = {
            name for name in before.keys() | after.keys() if before.get(name) != after.get(name)
        }
        if previous.get("active") != pointer.get("active"):
            entries.update(name for name in (previous.get("active"), pointer.get("active")) if name)
        entries = sorted(entries)
    except (AttributeError, KeyError, TypeError):
        entries = [pointer["active"]] if pointer.get("active") else []
    difference = {"message": "pointer differs from prev copy", "entries": entries}
    if not _only_gone_retained_entries(root, pointer, previous):
        difference["repair"] = _previous_pointer_repair(entries)
    else:
        difference["repair"] = (
            "Rerun workspace prune-stores to finish pruning and refresh the prev copy"
        )
    return difference


def _leftover_generation_directories(root, pointer):
    protected = {pointer.get("active"), *(item["name"] for item in pointer.get("retained", []))}
    return sorted(
        path.name
        for path in root.parent.glob(root.name + ".generation-*")
        if path.is_dir() and path.name not in protected
    )


def _preserved_generations(root, pointer):
    protected = {
        root.name,
        pointer.get("active"),
        *(item["name"] for item in pointer.get("retained", [])),
    }
    preserved = []
    for path in sorted(root.parent.glob(root.name + ".generation-*")):
        if not re.fullmatch(re.escape(root.name) + r"\.generation-[0-9a-f]{24}", path.name):
            continue
        if path.name in protected or not (path / ".store-comparison.json").is_file():
            continue
        generation_path(root, path.name)
        preserved.append({"name": path.name, "writes_after_markers": _writes_after_markers(path)})
    return preserved


def _prune_report(root, *, repair=None, now=None):
    findings = []
    try:
        resolve_workspace(root)
        pointer = read_json(root / POINTER)
    except ValueError as error:
        pointer = {}
        repair = repair or str(error)
        findings.append("invalid_pointer")
    try:
        state = _backfill_state(root)
    except ValueError as error:
        state = {}
        repair = repair or str(error)
        findings.append("corrupt_backfill_state")
    preserved = _preserved_generations(root, pointer)
    if preserved:
        findings.append("unlisted_published_generations")
    difference = _previous_pointer_difference(root, pointer)
    if difference:
        findings.append("previous_pointer_differs")
        repair = repair or difference.get("repair")
    leftovers = _leftover_generation_directories(root, pointer)
    if leftovers:
        findings.append("generation_leftovers")
    conflict = _publication_conflict(root, _publication_pointer(root, state))
    if conflict:
        findings.append("recovery_conflict")
        repair = conflict
    retired_findings = _retired_findings(root, pointer, state, inspect_expired=True, now=now)
    deletions = [
        item
        for item in retired_findings
        if item["recorded"] or item["kind"] == "incomplete (interrupted deletion)"
    ]
    if retired_findings:
        findings.append("retired_generation_findings")
        if not conflict and "invalid_pointer" not in findings:
            repair = retired_findings[0]["repair"]
    emptied_root = _emptied_retained_root(root, pointer)
    if emptied_root:
        findings.append("emptied_retained_root")
        repair = repair or _emptied_root_repair(emptied_root)
    if state.get("sealing") == "failed":
        findings.append("sealing_failed")
    if state.get("state") in ("copying", "published"):
        findings.append("backfill_incomplete")
    report = {
        "state": "report_only",
        "findings": findings,
        "generations": sorted(
            path.name
            for path in [root, *root.parent.glob(root.name + ".generation-*")]
            if path.is_dir()
        ),
        "preserved_generations": preserved,
        "previous_pointer_difference": difference,
        "leftover_generation_directories": leftovers,
        "emptied_retained_root": emptied_root,
        "interrupted_deletions": deletions,
        "retired_findings": retired_findings,
        "last_prune_findings": state.get("prune_findings", []),
    }
    if repair:
        report["repair"] = repair
    return report


def _preflight(source, target_store):
    from mempalace.backend_copy import palace_preflight

    from .handoff.thread_store_migrate import read_store, require_v1_representable

    source_store = settings_for(source).get("store", "v1")
    source_backend = "compact" if source_store == "compact" else "chroma"
    configured = read_json(source / "config" / "config.json").get("backend", source_backend)
    if str(configured).strip().lower() != source_backend:
        raise ValueError("Source backend configuration disagrees with workspace store")
    limit = migration_limit(source) if target_store == "compact" else DEFAULT_MAX_RECORDS
    palace_preflight(
        source / "palace",
        source_backend,
        "compact" if target_store == "compact" else "chroma",
        max_records=limit,
    )
    expanded_threads = 0
    if target_store == "v1" and thread_path(source).exists():
        with read_store(thread_path(source), compact=True) as store:
            require_v1_representable(store)
            for row in store._conn.execute("SELECT id FROM threads"):
                expanded_threads += (
                    sum(
                        len(item.text.encode("utf-8")) + len(item.raw.encode("utf-8"))
                        for item in store.messages(row[0])
                    )
                    * 4
                )
    size = sum(
        path.stat().st_size
        for path in source.rglob("*")
        if path.is_file() and not path.is_symlink()
    )
    if source_store == "compact" and (source / "palace" / "compact.sqlite3").exists():
        with closing(
            sqlite3.connect((source / "palace" / "compact.sqlite3").as_uri() + "?mode=ro", uri=True)
        ) as conn:
            expanded_palace = (
                conn.execute("SELECT coalesce(sum(raw_size),0) FROM documents").fetchone()[0] * 4
            )
        size = max(size, expanded_palace + expanded_threads)
    # Four simultaneous generations/snapshots, plus SQLite/index overhead.
    estimated_peak = size * 4 + 16 * 1024 * 1024
    if shutil.disk_usage(source.parent).free < estimated_peak:
        raise ValueError(
            f"Insufficient free space for estimated migration peak ({estimated_peak} bytes)"
        )
    return estimated_peak


def _verify_generation(source, destination, target_store):
    from mempalace.backend_copy import verify_sidecars

    from .handoff.thread_store_migrate import read_store, verify_stores

    skip = {
        *_ADMIN_FILES,
        SETTINGS,
        "palace",
        "threads.sqlite3",
        "threads.v2.sqlite3",
        ".store-comparison.json",
        "config/config.json",
    }
    verify_sidecars(source, destination, skip=skip)
    expected = settings_for(source)
    expected["store"] = target_store
    if read_json(destination / SETTINGS) != expected:
        raise ValueError("Copied workspace settings mismatch")
    config = read_json(source / "config" / "config.json")
    config["backend"] = "compact" if target_store == "compact" else "chroma"
    if read_json(destination / "config" / "config.json") != config:
        raise ValueError("Copied worker configuration mismatch")
    source_store = settings_for(source).get("store", "v1")
    if (source / "palace").exists():
        verify_sidecars(
            source / "palace",
            destination / "palace",
            "compact" if source_store == "compact" else "chroma",
            skip={".store-comparison.json"},
        )
    if thread_path(source).exists():
        with (
            read_store(thread_path(source), compact=source_store == "compact") as old,
            read_store(thread_path(destination), compact=target_store == "compact") as new,
        ):
            verify_stores(old, new)


def backup_workspace(root, active, workspace_id):
    """Run the operator's off-host command; store its receipt, never backup data."""
    command = os.environ.get("COMMONPLACE_BACKUP_CMD")
    if not command or not command.strip():
        raise BackupError("Store switch requires COMMONPLACE_BACKUP_CMD for an off-host backup")
    before = newest_write(active)
    nonce = secrets.token_hex(16)
    env = dict(
        os.environ,
        COMMONPLACE_WORKSPACE_DIR=str(active),
        COMMONPLACE_WORKSPACE_ID=workspace_id,
        COMMONPLACE_BACKUP_NONCE=nonce,
    )
    started = time.time_ns()
    try:
        result = subprocess.run(
            shlex.split(command), env=env, capture_output=True, text=True, timeout=3600
        )
    except (OSError, subprocess.SubprocessError) as exc:
        diagnostic = getattr(exc, "stderr", "") or ""
        if isinstance(diagnostic, bytes):
            diagnostic = diagnostic.decode("utf-8", "replace")
        atomic_write_text(root / BACKUP_STDERR, diagnostic, follow_symlinks=False)
        raise BackupError("Off-host workspace backup command failed") from None
    atomic_write_text(root / BACKUP_STDERR, result.stderr, follow_symlinks=False)
    if result.returncode != 0:
        raise BackupError("Off-host workspace backup command failed")
    try:
        receipt = json.loads(result.stdout)
        stamp = datetime.fromisoformat(receipt["completed_at"].replace("Z", "+00:00"))
        if stamp.tzinfo is None or receipt.get("off_host") is not True:
            raise ValueError
        timestamp = int(stamp.timestamp() * 1_000_000_000)
        if not isinstance(receipt.get("nonce"), str) or not secrets.compare_digest(
            receipt["nonce"], nonce
        ):
            raise ValueError
        if timestamp <= before or timestamp < started or timestamp > time.time_ns() + 5_000_000_000:
            raise ValueError
        if newest_write(active) != before:
            raise ValueError
    except (ValueError, KeyError, TypeError, AttributeError):
        raise BackupError(
            "Backup receipt must echo this run nonce and confirm off-host completion after its start and the latest write"
        ) from None
    recorded = {
        "completed_at": stamp.isoformat(),
        "off_host": True,
        "last_write_ns": before,
        "nonce": nonce,
    }
    atomic_write_json(root / RECEIPT, recorded, follow_symlinks=False)
    return recorded


def _copy_tree(source, target, *, skip=()):
    """Staging copy: checkpoint-safe SQLite plus verbatim files and sidecars."""
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    for path in source.iterdir():
        if path.name in skip:
            continue
        if any(
            path.name.endswith(suffix) and path.with_name(path.name[: -len(suffix)]).is_file()
            for suffix in ("-wal", "-shm", "-journal")
        ):
            continue
        dest = target / path.name
        if path.is_symlink():
            # Only the disposable shared model cache may point outside the tree.
            if path.name != "chroma" or path.parent.name != ".cache":
                raise ValueError("Unsupported symlink in workspace staging copy")
            dest.symlink_to(os.readlink(path), target_is_directory=True)
        elif path.is_dir():
            _copy_tree(path, dest)
        elif path.is_file():
            with path.open("rb") as stream:
                sqlite = stream.read(16) == b"SQLite format 3\0"
            if sqlite:
                _snapshot(path, dest)
            else:
                shutil.copy2(path, dest)
                dest.chmod(0o600)

    from mempalace.backend_copy import verify_sidecars

    verify_sidecars(source, target, skip=skip)


def _embedder(directory):
    from .workers import _PASSED_ENV

    directory = Path(directory)

    def embed(documents, metadatas, *, identity=None, dimension=None):
        env = {key: value for key, value in os.environ.items() if key in _PASSED_ENV}
        env.update(
            HOME=str(directory / "home"),
            USERPROFILE=str(directory / "home"),
            MEMPALACE_CONFIG_DIR=str(directory / "config"),
            MEMPALACE_PALACE_PATH=str(directory / "palace"),
        )
        result = subprocess.run(
            [sys.executable, "-m", "commonplace.store_embed"],
            env=env,
            input=json.dumps(
                {
                    "documents": documents,
                    "metadatas": metadatas,
                    "identity": asdict(identity) if identity else None,
                    "dimension": dimension,
                },
                ensure_ascii=True,
            ),
            capture_output=True,
            text=True,
            timeout=600,
        )
        if result.returncode == 2:
            from mempalace.backend_copy import RollbackEmbedderMismatch

            if json.loads(result.stdout).get("error") == "embedder_identity_mismatch":
                raise RollbackEmbedderMismatch(
                    "Rollback refused: configured embedder model or dimension differs from stored identity"
                )
        if result.returncode:
            raise ValueError("Workspace embedder unavailable")
        return json.loads(result.stdout)

    return embed


def _stage(source, destination, target_store, *, embed=None, checkpoint=lambda step: None):
    from mempalace.backend_copy import copy_palace

    from .handoff.thread_store_migrate import migrate_v1, migrate_v2

    source_store = settings_for(source).get("store", "v1")
    _copy_tree(
        source,
        destination,
        skip=(
            *_ADMIN_FILES,
            "palace",
            "threads.sqlite3",
            "threads.v2.sqlite3",
            ".store-comparison.json",
        ),
    )
    checkpoint("files")
    if (source / "palace").exists():
        snapshot = destination.with_name(destination.name + ".source-palace")
        try:
            # Chroma's client may checkpoint or recover even a read-only API
            # open. Only open that client on a staging snapshot of the palace.
            _copy_tree(source / "palace", snapshot)
            copy_palace(
                snapshot,
                destination / "palace",
                "compact" if source_store == "compact" else "chroma",
                "compact" if target_store == "compact" else "chroma",
                max_records=migration_limit(source)
                if target_store == "compact"
                else DEFAULT_MAX_RECORDS,
                embed=embed,
            )
        finally:
            shutil.rmtree(snapshot, ignore_errors=True)
    else:
        (destination / "palace").mkdir(mode=0o700)
    checkpoint("palace")
    settings = settings_for(source)
    settings["store"] = target_store
    atomic_write_json(destination / SETTINGS, settings)
    if thread_path(source).exists():
        if target_store == "compact":
            migrate_v1(str(thread_path(source)), str(thread_path(destination)))
        else:
            migrate_v2(str(thread_path(source)), str(thread_path(destination)))
    worker_config(destination)
    _verify_generation(source, destination, target_store)
    checkpoint("threads")


def _sync_tree(directory):
    for root, _directories, files in os.walk(directory, followlinks=False):
        for name in files:
            path = Path(root) / name
            if not path.is_symlink():
                with path.open("rb") as stream:
                    os.fsync(stream.fileno())
        fsync_directory(root)
    fsync_directory(Path(directory).parent)


def _rehearse(directory):
    """Exercise rollback including post-switch writes before publishing compact."""
    from mempalace.backend_copy import open_collection, verify_palaces
    from mempalace.backends import CompactBackend
    from mempalace.backends.compact import KEYWORD_ONLY

    from .handoff.models import ThreadMessage
    from .handoff.thread_store_migrate import read_store, verify_stores
    from .handoff.thread_store_v2 import ThreadStoreV2

    probe = directory.with_name(directory.name + ".probe")
    restored = directory.with_name(directory.name + ".rollback-probe")
    try:
        _copy_tree(directory, probe)
        token = secrets.token_hex(12)
        with closing(ThreadStoreV2(str(probe / "threads.v2.sqlite3"))) as store:
            store.store(
                "rollback-test",
                "rollback-test-" + token,
                [
                    ThreadMessage(
                        seq=0,
                        role="user",
                        kind="text",
                        name="",
                        call_id="",
                        at="",
                        text="rollback verification",
                        raw="rollback verification",
                    )
                ],
                actor="test",
            )
        backend = CompactBackend()
        try:
            col = open_collection(backend, probe / "palace", "rollback_probe_" + token, create=True)
            col.add(
                ids=["probe_" + token],
                documents=["rollback verification"],
                metadatas=[{"last_modified": "2026-01-01"}],
                embeddings=[[1.0, 0.0]],
            )
            drawers = read_json(directory / "config" / "config.json").get(
                "collection_name", "mempalace_drawers"
            )
            col = open_collection(backend, probe / "palace", drawers, create=True)
            col.add(
                ids=["keyword_probe_" + token],
                documents=["rollback keyword verification"],
                metadatas=[{KEYWORD_ONLY: True, "last_modified": "2026-01-01"}],
            )
        finally:
            backend.close()
        _stage(probe, restored, "v1", embed=_embedder(directory))
        verify_palaces(probe / "palace", restored / "palace", "compact", "chroma")
        with (
            read_store(probe / "threads.v2.sqlite3", compact=True) as src,
            read_store(restored / "threads.sqlite3") as dst,
        ):
            verify_stores(src, dst)
    finally:
        shutil.rmtree(probe, ignore_errors=True)
        shutil.rmtree(restored, ignore_errors=True)


def switch_workspace(
    data_dir, workspace_id, target_store, *, checkpoint=lambda step: None, authorize=None
):
    """Back up, stage, verify, rehearse and atomically publish while offline."""
    if target_store not in ("v1", "compact"):
        raise ValueError("Workspace store must be v1 or compact")
    data_dir = Path(data_dir).resolve()
    with data_lock(data_dir, exclusive=True):
        if authorize is not None:
            workspace_id = authorize(workspace_id)
        root = workspace_root(data_dir, workspace_id)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        _verify_offline(data_dir)
        _preflight_maintenance_metadata(root)
        state = _backfill_state(root)
        if state.get("state") == "deleting":
            raise ValueError(_deletion_repair(state["deletion"]["entry"]["name"], recorded=True))
        pointer = _prepare_generations(root)
        if _backfill_state(root).get("sealing") != "complete":
            _seal_retired(root, pointer)
        source = resolve_workspace(root)
        source_store = settings_for(source).get("store", "v1")
        if source_store == target_store:
            return {"store": source_store, "state": "unchanged"}
        from mempalace.palace import mine_palace_lock

        with mine_palace_lock(
            str(source / "palace"), lock_dir=str(source / "home" / ".mempalace" / "locks")
        ):
            _preflight(source, target_store)
            receipt = backup_workspace(root, source, workspace_id)
            checkpoint("backup")
            if not pointer:
                pointer = {"active": root.name, "retained": []}
                _write_pointer(root, pointer)
            name = root.name + ".generation-" + secrets.token_hex(12)
            staging = generation_path(root, name)
            _write_state(
                root,
                {"state": "copying", "staging": name, "store": target_store},
            )
            try:
                checkpoint("staging")
                _stage(
                    source, staging, target_store, embed=_embedder(source), checkpoint=checkpoint
                )
                checkpoint("copied")
                if target_store == "compact":
                    _rehearse(staging)
                checkpoint("rehearsed")
                _verify_generation(source, staging, target_store)
                atomic_write_json(
                    staging / ".store-comparison.json",
                    {"previous": source.name, "store": source_store},
                    follow_symlinks=False,
                )
                atomic_write_json(
                    staging / "palace" / ".store-comparison.json",
                    {
                        "previous": source.name,
                        "workspace": workspace_id,
                        "backend": "compact" if source_store == "compact" else "chroma",
                    },
                    follow_symlinks=False,
                )
                _sync_tree(staging)
                if newest_write(source) != receipt["last_write_ns"]:
                    raise ValueError("Workspace changed after backup; refusing store switch")
                _verify_offline(data_dir)
                checkpoint("verified")
                expected_files = {}
                digest = _retirement_digest(source, entries=expected_files)
                retained = pointer.get("retained", []) + [
                    {
                        "name": source.name,
                        "retired_at": time.time(),
                        "digest": digest,
                        "inventory": _write_inventory(root, sorted(expected_files)),
                    }
                ]
                published = {"active": name, "retained": retained}
                _write_pointer(root, published, store=target_store, checkpoint=checkpoint)
                _finish_publication(root, published, _backfill_state(root))
                checkpoint("published")
                checkpoint("complete")
                return {"store": target_store, "state": "complete"}
            except BaseException:
                if read_json(root / POINTER).get("active") != name:
                    _remove_staging(staging)
                    _write_state(
                        root,
                        {"state": "failed", "store": source_store},
                    )
                raise


def prune_generations(data_dir, workspace_id, *, now=None, authorize=None, report_only=False):
    with data_lock(data_dir, exclusive=True):
        if authorize is not None:
            workspace_id = authorize(workspace_id)
        root = workspace_root(data_dir, workspace_id)
        _verify_offline(Path(data_dir).resolve())
        _preflight_maintenance_metadata(root)
        if report_only:
            return _prune_report(root, now=now)
        try:
            _backfill_state(root)
        except ValueError as error:
            logger.warning("%s", error)
            return _prune_report(root, repair=str(error), now=now)
        pointer = _prepare_generations(root)
        if not pointer:
            return None
        if _backfill_state(root).get("sealing") != "complete":
            _seal_retired(root, pointer)
        state = _backfill_state(root)
        if state.get("state") == "deleting":
            pointer = _finish_deletion(root, pointer, state)
        refused = []
        for item in list(pointer.get("retained", [])):
            path = generation_path(root, item["name"])
            if path.name == pointer.get("active"):
                raise ValueError("Refusing to delete the active workspace generation")
            if _generation_gone(root, path):
                pointer = dict(
                    pointer, retained=[entry for entry in pointer["retained"] if entry != item]
                )
                _write_pointer(root, pointer)
                continue
            if (time.time() if now is None else now) - item["retired_at"] < RETENTION_SECONDS:
                continue
            files = {}
            current = _retirement_digest(path, entries=files)
            kind = _retired_kind(root, item, current=current)
            if kind:
                refused.append({"generation": path.name, "kind": kind, "at": time.time()})
                continue
            pointer = _start_deletion(root, pointer, item, files)
        _save_prune_findings(root, refused)
        if refused:
            raise ValueError(
                "; ".join(_retired_repair(item["generation"], item["kind"]) for item in refused)
            )
    return None


def doctor_workspace(data_dir, workspace_id):
    root = workspace_root(data_dir, workspace_id)
    active = resolve_workspace(root)
    store = settings_for(active).get("store", "v1")
    path = thread_path(active)
    index = None
    if path.exists():
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            index = (
                conn.execute("SELECT index_version FROM index_meta").fetchone()[0]
                if store == "compact"
                else conn.execute("PRAGMA user_version").fetchone()[0]
            )
    palace = active / "palace" / ("compact.sqlite3" if store == "compact" else "chroma.sqlite3")
    schema = None
    if palace.exists():
        with closing(sqlite3.connect(palace.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            schema = conn.execute("PRAGMA user_version").fetchone()[0]
    repair = None
    try:
        recorded = _backfill_state(root)
    except ValueError as error:
        recorded = {"state": "corrupt"}
        repair = str(error)
    state = recorded.get("state", "not_started")
    pointer = read_json(root / POINTER)
    if state == "copying" and recorded.get("staging") == pointer.get("active"):
        state = "published"
    conflict = _publication_conflict(root, _publication_pointer(root, recorded))
    repair = repair or conflict
    difference = _previous_pointer_difference(root, pointer)
    if difference:
        repair = repair or difference.get("repair")
    retired_findings = _retired_findings(root, pointer, recorded)
    deletions = [
        item
        for item in retired_findings
        if item["recorded"] or item["kind"] == "incomplete (interrupted deletion)"
    ]
    if retired_findings and not conflict:
        repair = retired_findings[0]["repair"]
    emptied_root = _emptied_retained_root(root, pointer)
    if emptied_root:
        repair = repair or _emptied_root_repair(emptied_root)
    retained = []
    for item in pointer.get("retained", []):
        path = generation_path(root, item["name"])
        retained.append(
            {
                "name": item["name"],
                "older_than_14_days": time.time() - item["retired_at"] >= RETENTION_SECONDS,
                "newer_writes": newest_write(path) > int(item["retired_at"] * 1_000_000_000),
                "verification": (
                    "unverified (legacy entry; prune checks it)"
                    if retired_files(root, item) is None and _legacy_required_file(path) is None
                    else None
                ),
            }
        )
    return {
        "active_path": str(active),
        "retained_generations": retained,
        "workspace": workspace_id,
        "store": store,
        "thread_index_version": index,
        "palace_schema_version": schema,
        "backfill": state,
        "sealing": recorded.get("sealing", "pending" if state == "published" else None),
        "previous_pointer_difference": difference,
        "leftover_generation_directories": _leftover_generation_directories(root, pointer),
        "recovery_conflict": conflict,
        "emptied_retained_root": emptied_root,
        "interrupted_deletions": deletions,
        "retired_findings": retired_findings,
        "last_prune_findings": recorded.get("prune_findings", []),
        "preserved_generations": _preserved_generations(root, pointer),
        "repair": repair,
    }
