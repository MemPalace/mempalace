#!/usr/bin/env python3
"""
mine_runner.py — Structured mine orchestration for MCP (develop branch).

This is the develop-branch port of the v3.0.0 mine_runner. It wraps
upstream's mining primitives (palace.mine_palace_lock, miner.scan_project,
miner.process_file, palace.bulk_check_mined) and adds:

  - mempalace_mine MCP tool entry-points (sync + async)
  - Auto-discovery of mempalace.yaml under a scan root
  - Job-file persistence with heartbeat (sleep/crash detection)
  - Subprocess spawning for wait=False mode

The diff categorization (added / updated / unchanged) uses develop's
own bulk_check_mined() batch API instead of our own mtime field —
develop already persists source_mtime on each drawer via the project
miner, so we just compare against the live filesystem.

Orphan handling is intentionally NOT done here on develop: develop's
mempalace_sync tool already covers that surface area with richer
filters (gitignore-aware, project_dirs filter, etc.). Callers needing
orphan detection should invoke mempalace_sync separately.

Subprocess entry point:
    python -m mempalace.mine_runner --job-id <uuid>
        [--project-dir P | --scan-root R]
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import io
import json
import logging
import os
import subprocess
import sys
import time
import traceback
import uuid
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import MempalaceConfig

logger = logging.getLogger("mempalace_mine_runner")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

MEMPALACE_DIR = Path(os.path.expanduser("~/.mempalace"))
# Separate lock file from develop's palace-level mine_palace_lock — we use
# this only to serialize MCP-driven mine job spawning; the actual heavy
# work serializes through palace.mine_palace_lock inside miner.mine().
RUNNER_LOCK_PATH = MEMPALACE_DIR / "mine_runner.lock"
JOBS_DIR = MEMPALACE_DIR / "jobs"

HEARTBEAT_STALE_SECONDS = 300
DEFAULT_SCAN_ROOT = "~/Claude"

DISCOVERY_SKIP_DIRS = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "env",
    "dist",
    "build",
    ".next",
    ".mempalace",
    ".claude",
    ".vscode",
    ".idea",
    "Library",
    "Pictures",
    "git",  # ~/Claude/git holds ephemeral worktrees
}


class LockHeldError(RuntimeError):
    def __init__(self, active_job_id: Optional[str]):
        super().__init__(f"mine_runner.lock held by job {active_job_id}")
        self.active_job_id = active_job_id


# ---------------------------------------------------------------------------
# Job-file helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now().isoformat()


def _jobs_dir() -> Path:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    return JOBS_DIR


def _job_path(job_id: str) -> Path:
    return _jobs_dir() / f"{job_id}.json"


def _write_job(job_id: str, **fields) -> None:
    path = _job_path(job_id)
    data: dict = {}
    if path.exists():
        try:
            with open(path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            data = {}
    data["job_id"] = job_id
    data.update(fields)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, path)


def _read_job(job_id: str) -> Optional[dict]:
    path = _job_path(job_id)
    if not path.exists():
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _find_active_job() -> Optional[str]:
    if not JOBS_DIR.exists():
        return None
    best_id = None
    best_started = ""
    for f in JOBS_DIR.glob("*.json"):
        try:
            with open(f) as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError):
            continue
        if data.get("status") == "running":
            started = data.get("started_at", "")
            if started > best_started:
                best_started = started
                best_id = data.get("job_id") or f.stem
    return best_id


# ---------------------------------------------------------------------------
# Lock (MCP-spawn serialization only — actual mining locks via palace.mine_palace_lock)
# ---------------------------------------------------------------------------


@contextmanager
def runner_lock():
    """
    Exclusive non-blocking flock on ~/.mempalace/mine_runner.lock.
    Protects against double-spawn from concurrent MCP calls. The actual
    palace write serialization is handled by palace.mine_palace_lock
    inside miner.mine().
    """
    MEMPALACE_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(RUNNER_LOCK_PATH), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as e:
            if e.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                os.close(fd)
                raise
            os.close(fd)
            raise LockHeldError(_find_active_job())
        yield fd
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def discover_targets(scan_root: str = DEFAULT_SCAN_ROOT) -> list[Path]:
    """Walk scan_root and return every dir containing mempalace.yaml or mempal.yaml."""
    root = Path(scan_root).expanduser().resolve()
    if not root.exists():
        return []
    targets: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames if d not in DISCOVERY_SKIP_DIRS and not d.startswith(".")
        ]
        if "mempalace.yaml" in filenames or "mempal.yaml" in filenames:
            targets.append(Path(dirpath))
            dirnames[:] = []  # project boundary
    return sorted(targets)


# ---------------------------------------------------------------------------
# Structured mine — categorize files, then mine
# ---------------------------------------------------------------------------


def _mine_one_target(target: Path, palace_path: str, heartbeat_cb=None) -> dict:
    """
    Mine a single target directory. Categorizes files using develop's
    bulk_check_mined() and persists drawers via develop's process_file()
    (which handles the upsert + closet maintenance + idempotency).

    Returns:
      {
        "path":  str,
        "wing":  str,
        "added":     {"files": int, "drawers": int},
        "updated":   {"files": int, "drawers": int},
        "unchanged": {"files": int},
        "skipped":   {"files": int},
        "files_total": int,
      }

    Orphan detection is delegated to mempalace_sync. We do not touch
    drawers whose source_file is missing from disk — sync handles that.
    """
    from .miner import (
        load_config,
        scan_project,
        process_file,
        MAX_FILE_SIZE,
    )
    from .palace import (
        get_collection,
        get_closets_collection,
        bulk_check_mined,
    )

    yaml_path = target / "mempalace.yaml"
    if not yaml_path.exists():
        yaml_path = target / "mempal.yaml"
    if not yaml_path.exists():
        raise FileNotFoundError(f"No mempalace.yaml in {target}")

    cfg = load_config(str(target))
    wing = cfg["wing"]
    rooms = cfg.get("rooms", [{"name": "general", "description": "All project files"}])

    # Develop's scan_project respects .gitignore by default — preserve that.
    files = scan_project(str(target), respect_gitignore=True)

    collection = get_collection(palace_path)
    closets_col = get_closets_collection(palace_path)

    # One-shot fetch of every (source_file, mtime) in the palace — develop's
    # batched alternative to per-file already_mined queries.
    mined = bulk_check_mined(collection)

    added_files = added_drawers = 0
    updated_files = updated_drawers = 0
    unchanged_files = 0
    skipped_files = 0

    for i, filepath in enumerate(files, 1):
        src = str(filepath)
        try:
            file_mtime = filepath.stat().st_mtime
            file_size = filepath.stat().st_size
        except OSError:
            skipped_files += 1
            continue

        if file_size > MAX_FILE_SIZE:
            skipped_files += 1
            continue

        # Categorize BEFORE mining so we know whether this is an
        # "added" or "updated" outcome — process_file is idempotent
        # via deterministic drawer IDs so the actual filing logic is
        # the same either way.
        stored_mtime = mined.get(src)
        if stored_mtime is not None and abs(float(stored_mtime) - file_mtime) < 0.5:
            unchanged_files += 1
            continue

        was_in_palace = stored_mtime is not None

        try:
            drawers, _room = process_file(
                filepath=filepath,
                project_path=target,
                collection=collection,
                wing=wing,
                rooms=rooms,
                agent="mempalace_mcp_mine",
                dry_run=False,
                closets_col=closets_col,
            )
        except Exception as e:
            logger.warning(f"process_file failed for {filepath}: {e}")
            skipped_files += 1
            continue

        if was_in_palace:
            updated_files += 1
            updated_drawers += drawers
        else:
            added_files += 1
            added_drawers += drawers

        if heartbeat_cb and (i % 25 == 0 or i == len(files)):
            heartbeat_cb()

    return {
        "path": str(target),
        "wing": wing,
        "added": {"files": added_files, "drawers": added_drawers},
        "updated": {"files": updated_files, "drawers": updated_drawers},
        "unchanged": {"files": unchanged_files},
        "skipped": {"files": skipped_files},
        "files_total": len(files),
    }


@contextmanager
def _redirect_stdout(buf):
    old = sys.stdout
    sys.stdout = buf
    try:
        yield
    finally:
        sys.stdout = old


def run_mine(
    project_dir: Optional[str] = None,
    scan_root: Optional[str] = None,
    palace_path: Optional[str] = None,
    heartbeat_cb=None,
) -> dict:
    """
    Run a mine across one project_dir or all auto-discovered targets.

    Caller must hold runner_lock() to prevent double-spawn; the actual
    palace write lock is acquired internally by miner-level primitives.
    """
    from .palace import mine_palace_lock, MineAlreadyRunning

    started = time.monotonic()
    started_at = _now_iso()

    config = MempalaceConfig()
    if palace_path is None:
        palace_path = config.palace_path

    if project_dir:
        targets = [Path(project_dir).expanduser().resolve()]
    else:
        targets = discover_targets(scan_root or DEFAULT_SCAN_ROOT)

    target_results: list[dict] = []
    errors: list[dict] = []
    wings_agg: dict[str, dict[str, int]] = defaultdict(
        lambda: {
            "added_files": 0,
            "added_drawers": 0,
            "updated_files": 0,
            "updated_drawers": 0,
            "unchanged_files": 0,
            "skipped_files": 0,
        }
    )

    try:
        with mine_palace_lock(palace_path):
            for t in targets:
                try:
                    buf = io.StringIO()
                    with _redirect_stdout(buf):
                        tr = _mine_one_target(t, palace_path=palace_path, heartbeat_cb=heartbeat_cb)
                    target_results.append(tr)
                    w = wings_agg[tr["wing"]]
                    w["added_files"] += tr["added"]["files"]
                    w["added_drawers"] += tr["added"]["drawers"]
                    w["updated_files"] += tr["updated"]["files"]
                    w["updated_drawers"] += tr["updated"]["drawers"]
                    w["unchanged_files"] += tr["unchanged"]["files"]
                    w["skipped_files"] += tr["skipped"]["files"]
                except Exception as e:
                    errors.append(
                        {
                            "path": str(t),
                            "error": f"{type(e).__name__}: {e}",
                            "traceback": traceback.format_exc(),
                        }
                    )
    except MineAlreadyRunning as e:
        return {
            "status": "already_running_palace_lock",
            "error": str(e),
            "note": (
                "Another process holds palace.mine_palace_lock — "
                "could be a CLI `mempalace mine`, the hooks pre-commit, "
                "or a previously-spawned MCP job. Re-run after it finishes."
            ),
        }

    def _sum(key, sub):
        return sum(tr[key][sub] for tr in target_results) if target_results else 0

    return {
        "added": {"files": _sum("added", "files"), "drawers": _sum("added", "drawers")},
        "updated": {"files": _sum("updated", "files"), "drawers": _sum("updated", "drawers")},
        "unchanged": {"files": _sum("unchanged", "files")},
        "skipped": {"files": _sum("skipped", "files")},
        "targets": target_results,
        "wings": dict(wings_agg),
        "duration_seconds": round(time.monotonic() - started, 2),
        "started_at": started_at,
        "finished_at": _now_iso(),
        "errors": errors,
        "palace_path": palace_path,
        "note": "Orphan handling is provided by mempalace_sync — not by this tool.",
    }


def run_with_lock(
    job_id: str,
    mode: str,
    project_dir: Optional[str] = None,
    scan_root: Optional[str] = None,
) -> dict:
    """Acquire runner_lock, run mine, persist to job file."""
    try:
        with runner_lock():
            _write_job(
                job_id,
                status="running",
                mode=mode,
                started_at=_now_iso(),
                heartbeat=_now_iso(),
                project_dir=project_dir,
                scan_root=scan_root,
            )

            def _heartbeat():
                _write_job(job_id, heartbeat=_now_iso())

            try:
                result = run_mine(
                    project_dir=project_dir,
                    scan_root=scan_root,
                    heartbeat_cb=_heartbeat,
                )
                _write_job(
                    job_id,
                    status="done",
                    finished_at=_now_iso(),
                    heartbeat=_now_iso(),
                    result=result,
                )
                return {"status": "done", "job_id": job_id, **result}
            except Exception as e:
                err = {
                    "error": f"{type(e).__name__}: {e}",
                    "traceback": traceback.format_exc(),
                }
                _write_job(
                    job_id,
                    status="failed",
                    finished_at=_now_iso(),
                    heartbeat=_now_iso(),
                    **err,
                )
                raise
    except LockHeldError as e:
        return {"status": "already_running", "job_id": e.active_job_id}


def spawn_background(
    mode: str,
    project_dir: Optional[str] = None,
    scan_root: Optional[str] = None,
) -> dict:
    """Spawn a detached subprocess for the async mode."""
    try:
        with runner_lock():
            pass
    except LockHeldError as e:
        return {"status": "already_running", "job_id": e.active_job_id}

    job_id = str(uuid.uuid4())
    _write_job(
        job_id,
        status="queued",
        mode=mode,
        started_at=_now_iso(),
        heartbeat=_now_iso(),
        project_dir=project_dir,
        scan_root=scan_root,
    )

    log_path = _jobs_dir() / f"{job_id}.log"
    cmd = [sys.executable, "-m", "mempalace.mine_runner", "--job-id", job_id, "--mode", mode]
    if project_dir:
        cmd += ["--project-dir", project_dir]
    if scan_root:
        cmd += ["--scan-root", scan_root]

    log_fh = open(log_path, "w")
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        close_fds=True,
    )
    return {
        "status": "spawned",
        "job_id": job_id,
        "pid": proc.pid,
        "log_path": str(log_path),
    }


def read_status(job_id: str) -> dict:
    data = _read_job(job_id)
    if data is None:
        return {"error": f"Job not found: {job_id}", "job_id": job_id}
    status = data.get("status")
    if status == "running":
        last_hb = data.get("heartbeat") or data.get("started_at")
        if last_hb:
            try:
                last_dt = datetime.fromisoformat(last_hb)
                age = (datetime.now() - last_dt).total_seconds()
                if age > HEARTBEAT_STALE_SECONDS:
                    data = dict(data)
                    data["status"] = "stale"
                    data["heartbeat_age_seconds"] = round(age, 1)
                    data["stale_reason"] = (
                        f"No heartbeat for {round(age)}s (>{HEARTBEAT_STALE_SECONDS}s) — "
                        "job process likely died (sleep, crash, kill)."
                    )
            except ValueError:
                pass
    return data


def _cli_main():
    parser = argparse.ArgumentParser(description="Background mine runner (internal)")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--mode", default="full")
    parser.add_argument("--project-dir", default=None)
    parser.add_argument("--scan-root", default=None)
    args = parser.parse_args()

    try:
        run_with_lock(
            job_id=args.job_id,
            mode=args.mode,
            project_dir=args.project_dir,
            scan_root=args.scan_root,
        )
    except Exception:
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    _cli_main()
