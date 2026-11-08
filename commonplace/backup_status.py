"""Opt-in, path-free last-completed snapshot receipts; never a backup prerequisite."""

from __future__ import annotations

import json
import os
import re
import stat
import sys
from datetime import datetime, timezone
from uuid import UUID, uuid4

LIMIT = 16 * 1024
MODES = ("online", "offline")
FIELDS = {"schema", "run_id", "mode", "result", "started_at", "finished_at", "stage", "scope"}


def _utc_now():
    return datetime.now(timezone.utc)


def _iso(value):
    if value.tzinfo is None or value.utcoffset().total_seconds() != 0:
        raise ValueError()
    return value.isoformat().replace("+00:00", "Z")


def _timestamp(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?Z", value
    ):
        raise ValueError()
    result = datetime.fromisoformat(value[:-1] + "+00:00")
    if result.utcoffset().total_seconds() != 0:
        raise ValueError()
    return result


def _validate(value, mode):
    if not isinstance(value, dict) or set(value) != FIELDS:
        raise ValueError()
    if (
        mode not in MODES
        or type(value["schema"]) is not int
        or value["schema"] != 1
        or value["mode"] != mode
    ):
        raise ValueError()
    if value["scope"] != "instance_snapshot" or value["result"] not in ("success", "failure"):
        raise ValueError()
    if value["stage"] != ("complete" if value["result"] == "success" else "backup_failed"):
        raise ValueError()
    if not isinstance(value["run_id"], str) or str(UUID(value["run_id"])) != value["run_id"]:
        raise ValueError()
    if _timestamp(value["finished_at"]) < _timestamp(value["started_at"]):
        raise ValueError()
    return value


def _directory(path):
    # The path is configuration only. Both reader and writer require provisioning.
    if not os.path.isabs(path):
        raise ValueError()
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK)


def read_receipts(context):
    """Read fixed basenames only; one hostile record cannot hide its sibling."""
    path = os.environ.get("COMMONPLACE_BACKUP_STATUS_DIR")
    result = {}
    for mode in MODES:
        try:
            context.remaining()
            if not path:
                result[mode] = {"state": "unknown", "reason": "not_configured"}
                continue
            directory = _directory(path)
            try:
                try:
                    fd = os.open(
                        mode + ".json",
                        os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                        dir_fd=directory,
                    )
                except FileNotFoundError:
                    result[mode] = {"state": "unknown", "reason": "no_record"}
                    continue
                try:
                    info = os.fstat(fd)
                    if not stat.S_ISREG(info.st_mode) or info.st_size > LIMIT:
                        raise ValueError()
                    raw = b""
                    while len(raw) <= LIMIT:
                        context.remaining()
                        part = os.read(fd, LIMIT + 1 - len(raw))
                        if not part:
                            break
                        raw += part
                    if len(raw) > LIMIT:
                        raise ValueError()
                finally:
                    os.close(fd)
                value = _validate(json.loads(raw), mode)
                context.remaining()
                result[mode] = {"state": "ready", **value}
            finally:
                os.close(directory)
        except Exception:
            result[mode] = {"state": "error", "error": "Backup status unavailable"}
    return result


def _write(path, value):
    """Publish a private regular file relative to the opened provisioned directory."""
    directory = _directory(path)
    temporary = ".receipt-" + uuid4().hex
    try:
        fd = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
        )
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write((json.dumps(value, separators=(",", ":")) + "\n").encode())
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(
                temporary, value["mode"] + ".json", src_dir_fd=directory, dst_dir_fd=directory
            )
            os.fsync(directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
    finally:
        os.close(directory)


class Reporter:
    """CLI-only reporting boundary; all status faults leave backup outcomes intact."""

    def __init__(self, *, utc_now=_utc_now):
        self.path = os.environ.get("COMMONPLACE_BACKUP_STATUS_DIR")
        self.utc_now = utc_now
        self.started = None
        self.run_id = None
        if self.path:
            try:
                self.started = _iso(utc_now())
                self.run_id = str(uuid4())
            except Exception:
                pass  # Diagnose once at completion, preserving the snapshot call.

    def finish(self, mode, success):
        if not self.path:
            return
        try:
            value = _validate(
                {
                    "schema": 1,
                    "run_id": self.run_id,
                    "mode": mode,
                    "result": "success" if success else "failure",
                    "started_at": self.started,
                    "finished_at": _iso(self.utc_now()),
                    "stage": "complete" if success else "backup_failed",
                    "scope": "instance_snapshot",
                },
                mode,
            )
            _write(self.path, value)
        except Exception:
            # Fixed diagnostic only: paths and exception text never enter receipts/logs.
            try:
                print("backup status unavailable", file=sys.stderr)
            except Exception:
                pass
