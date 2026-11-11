"""Local spool for opt-in capture hook events: write now, upload later.

A hook only writes its payload here, which takes no network time. A detached
``python -m commonplace.hook_spool drain`` child uploads the items oldest first.

* Each item is one private file holding the hook JSON verbatim plus an
  idempotency key. It is removed only after the hub confirmed the upload.
* A failed upload keeps the item, stops the pass (order is kept) and backs off
  exponentially; the next hook event or a manual ``drain`` retries.
* The spool is bounded. When full, the oldest items are dropped first and every
  drop is written to ``spool.log``; nothing is discarded silently.
* Uploading reuses the hub's cursor and ``client_op_id`` checks, so an item that
  was uploaded but not yet deleted (a crash in between) is a no-op on retry.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import capture_client

MAX_ITEMS = 500
MAX_BYTES = 8 * 1024 * 1024
MAX_REJECTED = 100
MAX_LOG_BYTES = 256 * 1024
BACKOFF_BASE_S = 5
BACKOFF_CAP_S = 900
# Consecutive failures of the same head item before it is set aside; with the
# backoff above the delays before giving up add up to about 66 minutes.
MAX_ATTEMPTS = 12
SOURCES = ("claude-code", "codex")


class Transient(Exception):
    """The upload failed in a way a later retry can fix; the item stays."""


class Halt(Exception):
    """The hub refused the credential: wait for a fixed token; never give up the items."""


class Permanent(Exception):
    """The item can never upload; it is set aside, not deleted."""


class Skipped(Exception):
    """The workspace opted out; the transcript stays local and a later opt-in recaptures it."""


_GONE = (FileNotFoundError, NotADirectoryError, IsADirectoryError, PermissionError)


def default_root(home=None):
    return Path(home or Path.home()) / ".commonplace" / "spool"


@contextmanager
def _locked(path, *, blocking):
    """Hold an exclusive lock on ``path``; yields False if it is busy and not ``blocking``."""
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    held = False
    try:
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(descriptor, msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            held = True
        except OSError:
            if blocking:
                raise
        yield held
    finally:
        os.close(descriptor)


def _atomic(path, data):
    """Write ``data`` so a reader sees the whole file or none of it, owner-only."""
    temporary = path.with_name(f".tmp-{uuid.uuid4().hex}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    _sync_directory(path.parent)


def _sync_directory(directory):
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _size(path):
    """File size, or None when a concurrent drain already removed it."""
    try:
        return path.stat().st_size
    except OSError:
        return None


def payload_path(payload):
    """The transcript path the hook named, or None; same rule as the hook."""
    try:
        value = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(value, dict):
        return None
    path = value.get("transcript_path") or value.get("rollout_path")
    return Path(path) if isinstance(path, str) and Path(path).is_absolute() else None


class Spool:
    def __init__(self, root, *, clock=time.time, max_items=MAX_ITEMS, max_bytes=MAX_BYTES):
        self.root = Path(root)
        self.clock = clock
        self.max_items = max_items
        self.max_bytes = max_bytes

    # -- layout -------------------------------------------------------------

    def _prepare(self):
        if self.root.is_symlink():
            raise OSError("spool directory must not be a symlink")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)
        (self.root / "rejected").mkdir(mode=0o700, exist_ok=True)

    def items(self):
        """Pending item files, oldest first (names start with a zero-padded counter)."""
        try:
            return sorted(p for p in self.root.glob("*.json") if p.name[0].isdigit())
        except OSError:
            return []

    @property
    def state_path(self):
        return self.root / "state.json"

    @property
    def log_path(self):
        return self.root / "spool.log"

    def state(self):
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_state(self, **state):
        _atomic(self.state_path, json.dumps(state).encode())

    def log(self, event, **fields):
        """Append one JSON line; never payload text. Rotates once past MAX_LOG_BYTES."""
        line = {"at": datetime.now(timezone.utc).isoformat(), "event": event, **fields}
        try:
            if self.log_path.exists() and self.log_path.stat().st_size > MAX_LOG_BYTES:
                os.replace(self.log_path, self.log_path.with_name("spool.log.1"))
            descriptor = os.open(self.log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
                stream.write(json.dumps(line, sort_keys=True) + "\n")
        except OSError:
            # The spool must never break the hook; stderr is the last resort.
            print(f"commonplace spool: {event} not logged", file=sys.stderr)

    # -- write side (the hook) ----------------------------------------------

    def enqueue(self, source, payload):
        """Store ``payload`` (the hook's stdin text, verbatim) and return its key.

        Returns None, after logging, when the item alone exceeds the byte bound.
        Makes room by dropping the oldest items, each one logged.
        """
        if source not in SOURCES:
            raise ValueError("unknown source")
        self._prepare()
        key = uuid.uuid4().hex
        envelope = json.dumps(
            {"v": 1, "key": key, "source": source, "created": self.clock(), "payload": payload},
            ensure_ascii=False,
        ).encode("utf-8")
        with _locked(self.root / ".enqueue.lock", blocking=True):
            if len(envelope) > self.max_bytes:
                self.log("dropped", key=key, reason="item_larger_than_spool", bytes=len(envelope))
                return None
            existing = [(p, size) for p in self.items() if (size := _size(p)) is not None]
            total = sum(size for _, size in existing) + len(envelope)
            while existing and (len(existing) + 1 > self.max_items or total > self.max_bytes):
                victim, size = existing.pop(0)
                victim.unlink(missing_ok=True)
                total -= size
                self.log(
                    "dropped", key=victim.stem.split("-", 1)[-1], reason="spool_full", bytes=size
                )
            # The counter only grows, so a clock step back cannot reorder items.
            last = int(existing[-1][0].name[:20]) if existing else 0
            counter = max(int(self.clock() * 1e9), last + 1)
            _atomic(self.root / f"{counter:020d}-{key}.json", envelope)
        return key

    # -- upload side (the child) --------------------------------------------

    def _load(self, path):
        item = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(item, dict)
            or item.get("source") not in SOURCES
            or not isinstance(item.get("payload"), str)
            or not isinstance(item.get("key"), str)
        ):
            raise ValueError("invalid item")
        return item

    def _set_aside(self, path, reason):
        """Keep an item that cannot upload under rejected/, bounded and logged."""
        key = path.stem.split("-", 1)[-1]
        try:
            os.replace(path, self.root / "rejected" / path.name)
        except OSError:
            path.unlink(missing_ok=True)
            self.log("dropped", key=key, reason=reason + "; could not keep")
            return
        self.log("rejected", key=key, reason=reason)
        kept = sorted((self.root / "rejected").glob("*.json"))
        for victim in kept[: max(0, len(kept) - MAX_REJECTED)]:
            victim.unlink(missing_ok=True)
            self.log("dropped", key=victim.stem.split("-", 1)[-1], reason="rejected_full")

    def drain(self, upload, *, force=False):
        """Upload pending items oldest first; returns one of
        ``busy``, ``waiting``, ``empty``, ``failed`` or ``done``.

        ``upload(item)`` returns once the hub confirmed the item, raises
        Transient, Permanent or Skipped otherwise.
        """
        self._prepare()
        with _locked(self.root / ".drain.lock", blocking=False) as held:
            if not held:
                return "busy"
            state = self.state()
            if not force and self.clock() < state.get("next_at", 0):
                return "waiting"
            pending = self.items()
            if not pending:
                return "empty"
            for path in pending:
                try:
                    item = self._load(path)
                except FileNotFoundError:
                    continue
                except (OSError, ValueError):
                    self._set_aside(path, "unreadable item")
                    continue
                try:
                    upload(item)
                except (Transient, Halt) as error:
                    failures = (
                        state.get("failures", 0) + 1 if state.get("head") == item["key"] else 1
                    )
                    if isinstance(error, Transient) and failures >= MAX_ATTEMPTS:
                        self._set_aside(path, f"gave up after {failures} attempts: {error}")
                        state = {}
                        continue
                    delay = min(BACKOFF_BASE_S * 2 ** (failures - 1), BACKOFF_CAP_S)
                    self._save_state(
                        head=item["key"],
                        failures=failures,
                        next_at=self.clock() + delay,
                        last_error=str(error),
                    )
                    self.log(
                        "retry", key=item["key"], attempt=failures, delay=delay, error=str(error)
                    )
                    return "failed"
                except Permanent as error:
                    self._set_aside(path, str(error))
                except Skipped as error:
                    path.unlink(missing_ok=True)
                    self.log("skipped", key=item["key"], reason=str(error))
                else:
                    # Only now, after the hub's confirmation, does the item go away.
                    path.unlink(missing_ok=True)
                state = {}
            self._save_state()
            return "done"

    def status(self):
        items = self.items()
        state = self.state()
        return {
            "pending": len(items),
            "pending_bytes": sum(_size(p) or 0 for p in items),
            "rejected": len(list((self.root / "rejected").glob("*.json"))),
            "failures": state.get("failures", 0),
            "next_attempt_at": state.get("next_at"),
            "last_error": state.get("last_error"),
        }


def uploader(url, token):
    """Return ``upload(item)`` bound to one hub; fetches the opt-in setting once."""
    target = capture_client.endpoint(url)
    setting = {}

    def upload(item):
        if "enabled" not in setting:
            try:
                setting.update(
                    capture_client.request(target, token, "GET", "/api/settings/capture")
                )
            except capture_client.CaptureAuth as error:
                raise Halt(str(error)) from None
            except capture_client.CaptureError as error:
                raise Transient(str(error)) from None
        if setting.get("status") == "deferred":
            setting.clear()  # ask again on the next pass
            raise Transient("hub asked to retry later")
        if not setting.get("enabled"):
            raise Skipped("capture is off for this workspace")
        path = payload_path(item["payload"])
        if path is None:
            raise Permanent("payload names no absolute transcript path")
        offset = -1
        while True:
            try:
                result = capture_client.sync_file(target, token, item["source"], path, item["key"])
            except capture_client.CaptureAuth as error:
                raise Halt(str(error)) from None
            except capture_client.CaptureUnavailable as error:
                raise Transient(str(error)) from None
            except capture_client.CaptureError as error:
                raise Permanent(str(error)) from None
            except OSError as error:
                # A missing, unreadable or symlinked transcript never recovers;
                # anything else (EIO, EAGAIN, ...) may.
                if isinstance(error, _GONE) or error.errno == errno.ELOOP:
                    raise Permanent("transcript unreadable") from None
                raise Transient("transcript read failed") from None
            status = result.get("status")
            if status == "deferred":
                raise Transient("hub asked to retry later")
            if status in ("unchanged", "duplicate"):
                return
            # Each appended chunk moves the hub's cursor; a large transcript is
            # progress, not failure. A cursor that does not move would loop forever.
            if type(result.get("offset")) is not int or result["offset"] <= offset:
                raise Transient("hub cursor did not advance")
            offset = result["offset"]

    return upload


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("drain", "status"))
    parser.add_argument("--home", type=Path)
    parser.add_argument("--url", default=os.environ.get("COMMONPLACE_URL"))
    parser.add_argument("--force", action="store_true", help="ignore the retry backoff")
    args = parser.parse_args(argv)
    spool = Spool(default_root(args.home))
    if args.action == "status":
        print(json.dumps(spool.status(), indent=2))
        return 0
    token = os.environ.get("COMMONPLACE_TOKEN", "")
    if not token or not args.url:
        parser.error("Set COMMONPLACE_URL and COMMONPLACE_TOKEN explicitly")
    try:
        result = spool.drain(uploader(args.url, token), force=args.force)
    except capture_client.CaptureError as error:
        print(str(error), file=sys.stderr)
        return 1
    print(result)
    return 1 if result == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
