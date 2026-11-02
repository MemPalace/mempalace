"""Optional hosted account plans and usage meters."""

from __future__ import annotations

import logging
import os
import stat
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from .control_db import ControlError

logger = logging.getLogger("commonplace.plans")
_warned_invalid = False
_switch_lock = threading.Lock()


@dataclass(frozen=True)
class Plan:
    name: str
    storage_bytes: Optional[int]
    transcription_seconds: Optional[int]
    sync_interval_seconds: int
    load_throttle: bool


FREE = Plan(
    "free",
    250 * 1024 * 1024,
    10 * 60,
    sync_interval_seconds=15 * 60,
    load_throttle=False,
)
PAID = Plan("paid", None, None, sync_interval_seconds=0, load_throttle=True)
PLANS = {plan.name: plan for plan in (FREE, PAID)}
PAID_FOOTNOTE = "** zolang de server het aankan / as long as the server can handle it"


class LimitReached(Exception):
    """A plan limit the caller can explain to the person."""


def enabled() -> bool:
    global _warned_invalid
    value = os.environ.get("COMMONPLACE_PLANS", "")
    if value == "on":
        return True
    if value not in ("", "off"):
        with _switch_lock:
            if not _warned_invalid:
                logger.warning("Invalid COMMONPLACE_PLANS value; plans are off")
                _warned_invalid = True
    return False


def _now() -> datetime:
    return datetime.now(timezone.utc)


def get_plan(db, user_id: str) -> Optional[Plan]:
    with db._lock:
        row = db._conn.execute(
            "SELECT plan FROM account_plans WHERE user_id = ?", (user_id,)
        ).fetchone()
    return PLANS[row["plan"]] if row else None


def workspace_account(db, workspace_id: str) -> Optional[str]:
    return db.workspace(workspace_id)["personal_of"]


def set_plan(db, user_id: str, name: str, *, actor: str) -> None:
    if name not in (*PLANS, "none"):
        raise ControlError("unknown plan")
    with db._lock, db._tx() as conn:
        if name == "none":
            conn.execute("DELETE FROM account_plans WHERE user_id = ?", (user_id,))
        else:
            conn.execute(
                "INSERT INTO account_plans(user_id, plan, set_by, set_at) VALUES (?,?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET "
                "plan = excluded.plan, set_by = excluded.set_by, set_at = excluded.set_at",
                (user_id, name, actor, _now().isoformat()),
            )
        db.audit(action="plan.set", decision="ok", user_id=actor, reason=name, target_ids=[user_id])


def assign_signup_plan(db, user_id: str, workspace_id: str, *, via: str) -> None:
    if not enabled():
        return
    with db._lock, db._tx() as conn:
        result = conn.execute(
            "INSERT OR IGNORE INTO account_plans(user_id, plan, set_by, set_at) VALUES (?,?,?,?)",
            (user_id, FREE.name, f"signup:{via}", _now().isoformat()),
        )
        if result.rowcount:
            db.audit(
                action="plan.set",
                decision="ok",
                user_id=user_id,
                workspace_id=workspace_id,
                reason=FREE.name,
            )


def storage_message(plan: Plan) -> str:
    mb = plan.storage_bytes // (1024 * 1024)
    return (
        f"Je opslag is vol: het gratis plan heeft {mb} MB. "
        "Verwijder iets om weer ruimte te maken. / "
        f"Your storage is full: the free plan includes {mb} MB. Delete something to make room."
    )


def transcription_message(plan: Plan) -> str:
    seconds = plan.transcription_seconds
    if seconds < 60:
        nl = f"{seconds} seconde" + ("" if seconds == 1 else "n")
        en = f"{seconds} second" + ("" if seconds == 1 else "s")
    else:
        nl, en = f"{seconds // 60} minuten", f"{seconds // 60} minutes"
    return (
        f"Je hebt de {nl} spraak-naar-tekst van vandaag gebruikt. "
        "Morgen (00:00 UTC) kan het weer. / "
        f"You've used today's {en} of voice transcription. "
        "It resets tomorrow at 00:00 UTC."
    )


class StorageMeter:
    """Count regular workspace files, caching each total for sixty seconds."""

    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self._cache = {}
        self._denied = {}
        self._lock = threading.Lock()

    def used(self, workspace_id: str, path) -> int:
        with self._lock:
            now = self.clock()
            cached = self._cache.get(workspace_id)
            if cached is not None and now - cached[0] < 60:
                return cached[1]
            total = self._walk(path)
            self._cache[workspace_id] = (now, total)
            return total

    @staticmethod
    def _walk(path) -> int:
        total = 0
        pending = [path]
        while pending:
            directory = pending.pop()
            try:
                if not stat.S_ISDIR(os.stat(directory, follow_symlinks=False).st_mode):
                    continue
                with os.scandir(directory) as entries:
                    for entry in entries:
                        try:
                            info = entry.stat(follow_symlinks=False)
                        except FileNotFoundError:
                            continue
                        if stat.S_ISDIR(info.st_mode):
                            pending.append(entry.path)
                        elif stat.S_ISREG(info.st_mode):
                            total += info.st_size
            except FileNotFoundError:
                continue
        return total

    def audit_limit(self, db, principal) -> None:
        with self._lock:
            now = self.clock()
            last = self._denied.get(principal.workspace_id)
            if last is not None and now - last < 60:
                return
            db.audit(
                action="plan.limit",
                decision="deny",
                reason="storage",
                workspace_id=principal.workspace_id,
                user_id=principal.user_id,
                connection_id=principal.connection_id,
                auth=principal.auth,
            )
            self._denied[principal.workspace_id] = now


def transcription_used(db, user_id: str) -> int:
    """Return today's transcription seconds (UTC)."""
    with db._lock:
        row = db._conn.execute(
            "SELECT seconds FROM transcription_usage WHERE user_id = ? AND day = ?",
            (user_id, _now().date().isoformat()),
        ).fetchone()
    return row["seconds"] if row else 0


def use_transcription(db, user_id: str, seconds: int) -> Optional[str]:
    """Atomically reserve whole seconds before transcribing.

    Return the charged UTC day, or None for unmetered accounts. On model failure,
    refund that reservation against its original day, even across midnight.
    """
    if not enabled():
        return None
    plan = get_plan(db, user_id)
    if plan is None or plan.transcription_seconds is None:
        return None
    if not isinstance(seconds, int) or isinstance(seconds, bool) or seconds < 0:
        raise ValueError("seconds must be a non-negative integer")
    today = _now().date()
    with db._lock, db._tx() as conn:
        row = conn.execute(
            "SELECT seconds FROM transcription_usage WHERE user_id = ? AND day = ?",
            (user_id, today.isoformat()),
        ).fetchone()
        used = row["seconds"] if row else 0
        if used + seconds > plan.transcription_seconds:
            raise LimitReached(transcription_message(plan))
        conn.execute(
            "INSERT INTO transcription_usage(user_id, day, seconds) VALUES (?,?,?) "
            "ON CONFLICT(user_id, day) DO UPDATE SET seconds = seconds + excluded.seconds",
            (user_id, today.isoformat(), seconds),
        )
        conn.execute(
            "DELETE FROM transcription_usage WHERE day < ?",
            ((today - timedelta(days=40)).isoformat(),),
        )
    return today.isoformat()


def refund_transcription(db, user_id: str, seconds: int, day: Optional[str]) -> None:
    """Release a failed request's reservation, regardless of later plan changes."""
    if day is None:
        return
    with db._lock, db._tx() as conn:
        conn.execute(
            "UPDATE transcription_usage SET seconds = seconds - ? WHERE user_id = ? AND day = ?",
            (seconds, user_id, day),
        )


def sync_interval(db, user_id: str) -> int:
    """The future chat-sync service uses zero for live updates."""
    if not enabled():
        return 0
    plan = get_plan(db, user_id)
    return plan.sync_interval_seconds if plan else 0


def load_ratio() -> float:
    try:
        return os.getloadavg()[0] / (os.cpu_count() or 1)
    except (AttributeError, OSError):
        return 0.0


def load_delay(plan: Optional[Plan]) -> float:
    if plan is None or not plan.load_throttle:
        return 0.0
    return min(2.0, max(0.0, (load_ratio() - 0.6) * 5))
