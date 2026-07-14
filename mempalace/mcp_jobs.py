"""Daemon-backed MCP job submission without palace/backend side effects."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from .daemon import DaemonError, canonical_palace_path, submit_job

DAEMON_WRITES_ENV = "MEMPALACE_MCP_DAEMON_WRITES"


def daemon_writes_enabled() -> bool:
    return os.environ.get(DAEMON_WRITES_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _file_sha256(path: str) -> str | None:
    if not os.path.isfile(path):
        return None
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def mine_dedupe_key(palace_path: str, payload: dict[str, Any]) -> str:
    normalized = dict(payload)
    source = os.path.realpath(os.path.expanduser(str(payload.get("source") or "")))
    normalized["source"] = source
    normalized.pop("priority", None)
    normalized["config_sha256"] = _file_sha256(os.path.join(source, "mempalace.yaml"))
    encoded = json.dumps(
        {
            "palace": canonical_palace_path(palace_path),
            "payload": normalized,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _git_output(source: str, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", source, *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _primary_worktree(source: str) -> str | None:
    listing = _git_output(source, "worktree", "list", "--porcelain")
    if not listing:
        return None
    for line in listing.splitlines():
        if line.startswith("worktree "):
            return os.path.realpath(line.removeprefix("worktree ").strip())
    return None


def detect_linked_worktree(source: str) -> tuple[bool, str | None]:
    source = os.path.realpath(os.path.expanduser(source))
    git_dir_raw = _git_output(source, "rev-parse", "--absolute-git-dir")
    common_raw = _git_output(source, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if not git_dir_raw or not common_raw:
        return False, None

    git_dir = Path(git_dir_raw).resolve()
    common_dir = Path(common_raw).resolve()
    linked_root = common_dir / "worktrees"
    try:
        linked = git_dir.is_relative_to(linked_root)
    except ValueError:
        linked = False
    return linked, _primary_worktree(source)


def submit_mine(palace_path: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = dict(payload)
    source = os.path.realpath(os.path.expanduser(str(request.get("source") or "")))
    request["source"] = source
    allow_linked = bool(request.pop("allow_linked_worktree", False))
    priority = int(request.pop("priority", 0) or 0)

    if request.get("mode", "projects") == "projects":
        linked, canonical_root = detect_linked_worktree(source)
        if linked and not allow_linked:
            return {
                "success": False,
                "error": "linked Git worktree mining is disabled; mine the canonical checkout",
                "error_class": "LinkedWorktreeRejected",
                "source": source,
                "canonical_root": canonical_root,
            }
        request["source_canonicality"] = "linked-worktree" if linked else "canonical"

    dedupe_key = mine_dedupe_key(palace_path, request)
    try:
        job = submit_job(
            "mine",
            request,
            palace_path=palace_path,
            dedupe_key=dedupe_key,
            priority=priority,
            wait=False,
            auto_start=False,
        )
    except DaemonError as exc:
        return {
            "success": False,
            "error": str(exc),
            "error_class": "DaemonUnavailable",
            "hint": "Run `mempalace daemon start` and retry.",
        }

    return {
        "success": True,
        "accepted": True,
        "job_id": job["id"],
        "state": job["state"],
        "deduplicated": bool(job.get("deduplicated", False)),
        "source": source,
        "mode": request.get("mode", "projects"),
        "wing": request.get("wing"),
        "submitted_at": job.get("created_at"),
    }
