"""Daemon-backed MCP job submission without palace/backend side effects."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from .daemon import (
    TERMINAL_STATES,
    DaemonError,
    canonical_palace_path,
    get_client_if_running,
    submit_job,
)

DAEMON_WRITES_ENV = "MEMPALACE_MCP_DAEMON_WRITES"
DAEMON_PROBE_TIMEOUT_SECONDS = 0.2


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


def _daemon_unavailable(error: Exception | None = None) -> dict[str, Any]:
    message = str(error) if error is not None else "daemon is not running"
    return {
        "success": False,
        "error": message,
        "error_class": "DaemonUnavailable",
        "hint": "Run `mempalace daemon start` and retry.",
    }


def _running_client(palace_path: str | None):
    return get_client_if_running(
        canonical_palace_path(palace_path),
        health_timeout=DAEMON_PROBE_TIMEOUT_SECONDS,
    )


def dispatch_daemon_write(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    palace_path: str | None = None,
    fast_wait_ms: int = 250,
    priority: int = 0,
) -> dict[str, Any]:
    """Durably queue an MCP mutation and briefly wait for a fast completion."""
    client = _running_client(palace_path)
    if client is None:
        return _daemon_unavailable()

    try:
        job = client.submit(
            "mcp_tool",
            {"name": tool_name, "arguments": arguments},
            dedupe_key=None,
            priority=priority,
        )
        current = job
        deadline = time.monotonic() + max(0, fast_wait_ms) / 1000
        while time.monotonic() < deadline:
            current = client.get_job(job["id"])
            if current.get("state") in TERMINAL_STATES:
                result = dict(current.get("result") or {})
                result.setdefault("success", current.get("state") == "succeeded")
                if current.get("state") != "succeeded" and "error" not in result:
                    error = current.get("error") or {}
                    result["error"] = error.get("message") or "daemon write failed"
                result["job_id"] = job["id"]
                result["delivery"] = "completed"
                return result
            time.sleep(0.02)
    except DaemonError as exc:
        return _daemon_unavailable(exc)

    return {
        "success": True,
        "accepted": True,
        "job_id": job["id"],
        "state": current.get("state", job.get("state", "queued")),
        "delivery": "durable_queue",
    }


def tool_job_status(job_id: str, *, palace_path: str | None = None) -> dict[str, Any]:
    client = _running_client(palace_path)
    if client is None:
        return _daemon_unavailable()
    try:
        return {"success": True, "job": client.get_job(job_id)}
    except DaemonError as exc:
        return {
            "success": False,
            "error": str(exc),
            "error_class": "DaemonJobError",
        }


def tool_list_jobs(
    limit: int = 20,
    state: str | None = None,
    kind: str | None = None,
    *,
    palace_path: str | None = None,
) -> dict[str, Any]:
    client = _running_client(palace_path)
    if client is None:
        return _daemon_unavailable()
    try:
        jobs = client.list_jobs(
            limit=max(1, min(int(limit), 100)),
            state=state or None,
            kind=kind or None,
        )
        return {"success": True, "jobs": jobs}
    except (DaemonError, ValueError, TypeError) as exc:
        return {
            "success": False,
            "error": str(exc),
            "error_class": "DaemonJobError",
        }


def active_maintenance_job(*, palace_path: str | None = None) -> dict[str, Any] | None:
    """Return a running mine/sync summary, without reading queued payloads."""
    if not daemon_writes_enabled():
        return None
    client = _running_client(palace_path)
    if client is None:
        return None
    try:
        jobs = client.list_jobs(limit=10, state="running", kind=None)
    except DaemonError:
        return None
    return next((job for job in jobs if job.get("kind") in {"mine", "sync"}), None)
