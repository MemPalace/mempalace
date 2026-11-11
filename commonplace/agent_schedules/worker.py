"""Delivery of due scheduled prompts, through the existing board wake path.

A prompt is delivered the way an owner's own message to the agent is: an owner-authored
message on the board the agent coordinates (``space_post``). That emits the ordinary
``space_message`` event, which wakes the coordinator's webhook, and the agent reads it in
``space_read`` like any request. No second channel exists.

Delivery is checked again at every due time, not just when the schedule was made: the
creator must still be a workspace owner, the agent's connection live and the agent still
the board's coordinator. If not, the run fails with a code, nothing is posted, and five
failures in a row pause the schedule. A due time more than ``GRACE_SECONDS`` late (the hub
was down) is recorded as skipped instead of delivered long afterwards.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from commonplace import http_util, schedule_time as times, sessions
from commonplace.boards.service import BoardService
from commonplace.control_db import ControlError
from commonplace.extensions import ToolContext, ToolError

from . import store

logger = logging.getLogger(__name__)
GRACE_SECONDS = 600
POLL_SECONDS = 30
HEADER = 'Scheduled prompt "{name}" (due {due}):\n\n'


def moment(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, timezone.utc)


def agent_connection(db, workspace_id, ref, now: datetime):
    """The live connection row of an agent, or None."""
    try:
        row = db.connection(ref, workspace_id)
    except ControlError:
        return None
    if row["revoked_at"] or (row["expires_at"] and row["expires_at"] <= times.stamp(now)):
        return None
    return row


def coordinator_of(service, board: str):
    return service.space_read(board, last=1)["coordinator"]


def deliver(gateway, row: dict, now: datetime) -> tuple[bool, str, int | None]:
    """``(ok, code, message id)``: post the prompt as the owner, or say why not."""
    db = gateway.db
    principal = sessions.person_principal(db, row["created_by"], row["workspace_id"])
    if principal is None or principal.role != "owner":
        return False, "owner_gone", None
    connection = agent_connection(db, row["workspace_id"], row["agent_id"], now)
    if connection is None:
        return False, "agent_gone", None
    service = BoardService(ToolContext(principal=principal, gateway=gateway))
    try:
        if coordinator_of(service, row["board"]) != "conn:" + connection["name"]:
            return False, "not_coordinator", None
        text = HEADER.format(name=row["name"], due=row["next_due"]) + row["prompt"]
        message = service.space_post(row["board"], text)
    except ToolError:
        return False, "board_gone", None
    return True, "", message["id"]


class ScheduleWorker:
    def __init__(self, gateway, *, start=True, now=time.time):
        self.gateway = gateway
        self.now = now
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.thread = None
        store.recover(gateway.db, moment(now()))
        if start:
            self.thread = threading.Thread(target=self._run, name="agent-schedules", daemon=True)
            self.thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=5)

    def _run(self):
        while not self._stop.is_set():
            self._wake.clear()
            try:
                if self.run_once():
                    continue
            except Exception as exc:
                http_util.log_failure(logger, "Scheduled prompt pass failed", exc)
            self._wake.wait(timeout=POLL_SECONDS)

    def run_once(self, now: float | None = None) -> int:
        """Handle every schedule that is due at ``now``; returns how many were claimed."""
        when = moment(self.now() if now is None else now)
        handled = 0
        for row in store.due(self.gateway.db, when):
            late = (when - times.parse_stamp(row["next_due"])).total_seconds()
            if not store.claim(self.gateway.db, row, when, missed=late > GRACE_SECONDS):
                continue
            handled += 1
            if late > GRACE_SECONDS:
                continue
            try:
                ok, code, message_id = deliver(self.gateway, row, when)
            except Exception as exc:
                http_util.log_failure(logger, "Scheduled prompt delivery failed", exc)
                ok, code, message_id = False, "error", None
            store.finish(
                self.gateway.db, row, row["next_due"], when, ok=ok, code=code, message_id=message_id
            )
        return handled
