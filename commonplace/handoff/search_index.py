"""Bounded, best-effort search backfill on the handoff mirror executor."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from commonplace import continuity_read, plans, sessions

from . import palace_mirror

MAX_THREADS = 20
MAX_UNITS = 2000
PAUSE_SECONDS = 1
STORAGE_MARGIN = 16 * 1024 * 1024
logger = logging.getLogger(__name__)


def enabled():
    return os.environ.get("COMMONPLACE_SEARCH_INDEX") != "off"


@dataclass
class State:
    state: str = "idle"
    units_written: int = 0
    units_by_thread: dict[str, int] = field(default_factory=dict)
    active_threads: set[str] = field(default_factory=set)
    blocked_threads: set[str] = field(default_factory=set)
    skipped: int = 0
    after_id: str = ""
    dirty: bool = False
    enrolled: bool = False


class _JobFull(Exception):
    pass


def import_groups(db, workspace_id):
    """Read only control metadata; never mutate continuity rows."""
    with db._lock:
        rows = db._conn.execute(
            "SELECT g.holder_stored_id, s.fork_stored_id FROM sync_streams s "
            "JOIN sync_groups g ON g.workspace_id=s.workspace_id AND g.group_id=s.group_id "
            "WHERE s.workspace_id=? AND s.kind='import'",
            (workspace_id,),
        ).fetchall()
    return {value for row in rows for value in row if value}


def principal_for(db, stored):
    filer = continuity_read.filing_user(db, stored)
    if stored.source_thread_id.startswith("import-private:"):
        filer = stored.source_thread_id.split(":", 2)[1]
    if filer:
        try:
            principal = sessions.person_principal(db, filer, stored.workspace_id)
            if principal is not None:
                return principal
        except Exception:
            pass
    with db._lock:
        owners = db._conn.execute(
            "SELECT user_id FROM memberships WHERE workspace_id=? AND role='owner' ORDER BY user_id",
            (stored.workspace_id,),
        ).fetchall()
    for owner in owners:
        try:
            principal = sessions.person_principal(db, owner[0], stored.workspace_id)
            if principal is not None:
                return principal
        except Exception:
            pass
    return None


def quota_ok(service, workspace_id):
    if not plans.enabled():
        return True
    account = plans.workspace_account(service.control_db, workspace_id)
    plan = plans.get_plan(service.control_db, account) if account else None
    if plan is None or plan.storage_bytes is None:
        return True
    from commonplace.workspace_store import workspace_path

    # store_for is the bound HandoffContext method in the gateway.
    context = getattr(service.store_for, "__self__", None)
    root = getattr(context, "data_dir", Path(service.control_db.path).parent)
    return plans.StorageMeter._walk(workspace_path(root, workspace_id)) < (
        plan.storage_bytes - STORAGE_MARGIN
    )


def sweep(service, workspace_id, state):
    """Return whether another bounded job is needed. Never log user content."""
    written = 0
    visited = 0
    deferred = False
    if state.dirty:
        state.after_id = ""
        state.dirty = False
    try:
        if not enabled():
            state.state = "off"
            return False
        store = service.store_for(workspace_id)
        if not state.enrolled:
            for stored_id in import_groups(service.control_db, workspace_id):
                stored = store.get(stored_id)
                if stored is not None and stored.workspace_id == workspace_id:
                    store.begin_mirror(stored_id, -1, bootstrap=-1)
            state.enrolled = True
        threads = store.index_backlog(workspace_id, after_id=state.after_id, limit=MAX_THREADS)
        for stored in threads:
            if not enabled():
                state.state = "off"
                return False
            principal = principal_for(service.control_db, stored)
            if principal is None:
                state.skipped += 1
                state.after_id = stored.id
                visited += 1
                continue

            deferred = False

            def allowed():
                nonlocal deferred
                if not quota_ok(service, workspace_id):
                    deferred = True
                    return False
                return True

            def budget(units):
                if written and written + len(units) > MAX_UNITS:
                    raise _JobFull()

            def call(principal, name, arguments, *, stored_id=stored.id):
                nonlocal written
                if name != "write_mirror_units":
                    return service.call_tool(principal, name, arguments)
                count = len(arguments["units"])
                result = service.call_tool(principal, name, arguments)
                ids = result.get("drawer_ids") if isinstance(result, dict) else None
                if (
                    isinstance(ids, list)
                    and len(ids) == count
                    and all(isinstance(i, str) and i for i in ids)
                    and not result.get("error")
                    and result.get("success") is not False
                    and not result.get("isError")
                ):
                    written += count
                    with service._mirror_lock:
                        state.units_written += count
                        state.units_by_thread[stored_id] = (
                            state.units_by_thread.get(stored_id, 0) + count
                        )
                return result

            with service._mirror_lock:
                state.active_threads.add(stored.id)
                state.blocked_threads.discard(stored.id)
            try:
                while True:
                    if written >= MAX_UNITS:
                        return True
                    palace_mirror.mirror_pending(
                        call,
                        principal,
                        stored,
                        store,
                        skip_empty=True,
                        should_continue=allowed,
                        max_messages=max(1, min(32, MAX_UNITS // 2)),
                        max_units=MAX_UNITS,
                        before_range=budget,
                    )
                    if store.mirror_progress(stored.id)[0] >= stored.message_count - 1:
                        break
            except _JobFull:
                return True
            except palace_mirror.MirrorError:
                if deferred:
                    with service._mirror_lock:
                        state.blocked_threads.add(stored.id)
                    # Later readable work must get its own actual quota refusal.
                    # Workspace storage accounting remains unchanged.
                    state.state = "deferred_storage"
                # A failed range stays pinned; the next trigger replays only it.
            finally:
                with service._mirror_lock:
                    state.active_threads.discard(stored.id)
            state.after_id = stored.id
            visited += 1
        if len(threads) == MAX_THREADS or state.dirty:
            return True
        state.after_id = ""
        state.state = "deferred_storage" if state.blocked_threads else "idle"
        return False
    except Exception:
        state.state = "idle"
        return False
    finally:
        logger.info("search index job threads=%d units=%d state=%s", visited, written, state.state)


PROGRESS_TIMEOUT = 1.0
PROGRESS_PAGE = 500


class ProgressUnavailable(Exception):
    """Current readable progress could not be proved completely."""


def projected_status(service, workspace_id, principal):
    """One metadata snapshot, fresh authority per poll, no global fallback."""
    from commonplace.continuity_read import _reader_locks
    from commonplace.rules import checks

    try:
        if principal.workspace_id != workspace_id or service.progress_can_read is None:
            raise ProgressUnavailable()
        with checks.deadline(PROGRESS_TIMEOUT):
            store = service.store_for(workspace_id)
            with _reader_locks(service.control_db._lock, store._lock), store.index_snapshot():
                allowed = {}
                after = ""
                while True:
                    checks.remaining(0)
                    rows = store.index_descriptors(
                        workspace_id, after_id=after, limit=PROGRESS_PAGE
                    )
                    if not rows:
                        break
                    for stored, _, _ in rows:
                        checks.remaining(0)
                        if stored.id <= after or stored.workspace_id != workspace_id:
                            raise ProgressUnavailable()
                        after = stored.id
                        if service.progress_can_read(principal, stored.id):
                            allowed[stored.id] = stored
                # Revalidate concrete descriptors in the aggregation snapshot.
                indexed, pending, readable = set(), set(), set()
                after = ""
                while True:
                    checks.remaining(0)
                    rows = store.index_descriptors(
                        workspace_id, after_id=after, limit=PROGRESS_PAGE
                    )
                    if not rows:
                        break
                    for stored, is_indexed, is_pending in rows:
                        if stored.id <= after or stored.workspace_id != workspace_id:
                            raise ProgressUnavailable()
                        after = stored.id
                        if allowed.get(stored.id) != stored:
                            continue
                        readable.add(stored.id)
                        if is_indexed:
                            indexed.add(stored.id)
                        if is_pending:
                            pending.add(stored.id)
                with _reader_locks(service._mirror_lock):
                    state = service._index_states.get(workspace_id, State())
                    if not enabled():
                        public_state = "off"
                    elif state.blocked_threads & pending:
                        public_state = "deferred_storage"
                    elif state.active_threads & readable:
                        public_state = "running"
                    else:
                        public_state = "idle"
                    result = {
                        "threads_indexed": len(indexed),
                        "threads_pending": len(pending),
                        "state": public_state,
                        "units_written": sum(state.units_by_thread.get(i, 0) for i in readable),
                    }
                checks.remaining(0)
            return result
    except Exception:
        raise ProgressUnavailable() from None
