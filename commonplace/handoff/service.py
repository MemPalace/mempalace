"""The handoff itself: one call from the client hook. Owner: part T10.

Hook contract: ``action=handoff`` means the thread is safely stored, so the
agent may stop. ``action=compact`` means it must keep working or compact;
in particular, another request's pending handoff is not safe to stop for.

``run`` order (every step idempotent, see docs/handoff.md):
1. settings off → ``HandoffResult("compact", reason="off")``; nothing stored.
2. ``HandoffDB.begin`` by idempotency key (default ``thread:<thread_id>``);
   an existing non-failed handoff is returned as-is with ``launch=False``
   (unless this is the first launch claim, see 6). A canonical transcript
   prefix extension appends its new messages, chunks and search entries to
   the same thread in one transaction. The handoff and successor stay the
   same; a non-extension retry leaves the stored text unchanged.
   A pending writer keeps its lease for 600 seconds; an expired pending row
   may be taken over atomically and its idempotent storage resumed. Each
   attempt carries an opaque token checked by mark_stored, mark_failed and
   claim_launch; a paused original writer cannot mutate its replacement.
3. parse transcript, store it (``ThreadStore.store``). Any failure →
   ``mark_failed`` + ``EVENT_FAILED`` + ``HandoffResult("compact",
   reason=...)``: no successor ever starts without a stored thread.
4. Additional Memory search mirror (best effort, by orchestrator decision).
5. build the doc (``writer``) with the chain (parent handoff's stored ids),
   ``mark_stored``, ``EVENT_STORED``.
6. ``claim_launch``: only the winner launches. ``client`` launcher: return
   ``launch=True`` and the prompt. Other launchers: launch here, then
   ``set_successor`` + ``EVENT_COMPLETED``, or ``mark_launch_failed`` +
   ``EVENT_LAUNCH_FAILED`` (the thread stays stored; the agent is told to
   stop anyway, since its work is safe in Memory).

The DB does not persist the begin-time launcher settings. Keep these and
the doc for retries in this service's lifetime. After restart, ``by_key``
still finds stored handoffs even with the toggle off; an unclaimed launch
reconstructs the doc and uses the current launcher. Explicit owner relaunch
also reconstructs it regardless of the toggle. A claimed launch without a
registered successor is ambiguous and never automatically starts again.
After verifying that no successor started, the owner can report a launch
failure and explicitly relaunch. If external launch succeeded but registering
its result failed, preserve ``launched`` and its claim; registration can be
retried, whereas relaunch could create a duplicate external successor.

Handoff documents, including all transcript quotes and notes, live in the
workspace ThreadStore. The control row keeps only metadata and pointers;
the DB hydrates the document for its callers and migrates pre-release rows.

Completion events are durable and exactly once: ``set_successor`` queues
``handoff.completed`` in ``handoff_outbox`` in the same transaction as the
successor, and ``Events.deliver_pending`` writes the audit entry, the hub
event and the delivered mark in one transaction. Undelivered rows are retried
on later calls (``run``, ``register_successor``) and replayed when the gateway
starts, also across gateways that share one control database. A webhook
accepted before its acknowledgement was saved may be delivered again with the
same delivery id, for receiver deduplication.

Launch transitions use the same shared lock to preserve a concurrently
completed handoff, and the database validates state changes in transactions.
External launch calls never hold a database transaction.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait as futures_wait
from dataclasses import dataclass, replace
from typing import Optional

from . import launchers, palace_mirror, search_index, thread_store, transcripts, writer
from .authority import HandoffPermissionError, require_launch_authority
from .db import HandoffDBError, HandoffLeaseError
from .models import (
    EVENT_FAILED,
    EVENT_LAUNCH_FAILED,
    EVENT_STARTED,
    EVENT_STORED,
    Handoff,
    HandoffDoc,
    HandoffRequest,
    HandoffResult,
    HandoffSettings,
    LauncherConfig,
    SuccessorRef,
    ThreadMeta,
)


@dataclass(frozen=True)
class _Snapshot:
    settings: HandoffSettings
    meta: ThreadMeta
    doc: Optional[HandoffDoc] = None
    lease_token: Optional[str] = None
    next_message: str = ""


class HandoffService:
    def __init__(
        self,
        *,
        control_db,
        handoff_db,
        settings,
        store_for,
        events,
        call_tool=None,
        save_fact=None,
        can_read=None,
        progress_can_read=None,
        prompt_dir: str = "",
    ):
        self.control_db = control_db
        self.db = handoff_db
        self.settings = settings
        self.store_for = store_for
        self.events = events
        self.call_tool = call_tool
        self.save_fact = save_fact
        self.can_read = can_read
        self.progress_can_read = progress_can_read
        self.prompt_dir = prompt_dir
        self.db.set_document_store(store_for)
        self._mirror_lock = threading.Lock()
        self._mirror_condition = threading.Condition(self._mirror_lock)
        self._index_states = {}
        self._index_active = set()
        self._index_timers = {}
        self._mirror_pool = None
        self._mirror_jobs: list = []
        self._fact_lock = threading.Lock()
        self._fact_pool = None
        self._fact_jobs: list = []
        self._background_closed = False
        self._snapshots: dict[tuple[str, str], _Snapshot] = {}

    def run(self, principal, request: HandoffRequest, *, manual: bool = False) -> HandoffResult:
        require_launch_authority(principal)
        meta = request.meta
        if not meta.thread_id or not meta.thread_id.strip():
            raise ValueError("thread_id must not be empty")
        if meta.thread_id.startswith(("import-private:", "capture:", "capture-fork:")):
            raise ValueError("thread_id uses a reserved namespace")
        workspace_id = principal.workspace_id
        # Retry events a failed delivery left queued in this workspace.
        self.events.deliver_pending(workspace_id=workspace_id)
        key = request.idempotency_key or f"thread:{meta.thread_id}"
        current = self.settings.effective(
            workspace_id, meta.agent or principal.connection, meta.task_ref
        )
        if manual:
            # Per-call selection belongs to the local T3 client. Never execute
            # the workspace's automatic launcher as well as its manual one.
            current = replace(current, launcher=LauncherConfig())
        with self.control_db._lock:
            snapshot = self._snapshots.get((workspace_id, key))
        if manual and snapshot:
            snapshot = replace(
                snapshot, settings=replace(snapshot.settings, launcher=LauncherConfig())
            )
        settings = snapshot.settings if snapshot else current
        if not settings.enabled:
            # Turning handoff off later never strands a handoff already under way:
            # a stored thread still gets its one successor.
            with self.control_db._lock:
                existing = self.db.by_key(workspace_id, key)
                if existing is not None and not self._can_reuse(principal, existing):
                    return HandoffResult("compact", reason="in progress")
            if existing is None or existing.state in ("failed", "pending"):
                return HandoffResult("compact", reason="off")
            settings = replace(current, enabled=True)

        lease_token = secrets.token_hex(16)
        try:
            with self.control_db._lock:
                existing = self.db.by_key(workspace_id, key)
                if existing is not None and not self._can_reuse(principal, existing):
                    return HandoffResult("compact", reason="in progress")
                handoff, created = self.db.begin(
                    workspace_id,
                    key,
                    meta,
                    connection_id=principal.connection_id,
                    lease_token=lease_token,
                    usage=request.usage,
                    threshold=settings.threshold,
                )
                if not created and not self._can_reuse(principal, handoff):
                    return HandoffResult("compact", reason="in progress")
        except Exception:
            reason = "thread storage failed"
            self._emit(EVENT_FAILED, principal, None, decision="error", reason=reason)
            return HandoffResult("compact", reason=reason)
        if not created:
            if handoff.stored_thread_id:
                failed = self._append_retry(principal, request, handoff)
                if failed is not None:
                    return failed
            if handoff.state in ("launched", "done", "launch_failed"):
                return HandoffResult("handoff", handoff)
            if handoff.state != "stored":
                return HandoffResult("compact", handoff, reason="in progress")
            with self.control_db._lock:
                snapshot = self._snapshots.get((workspace_id, key))
            if manual and snapshot:
                snapshot = replace(
                    snapshot, settings=replace(snapshot.settings, launcher=LauncherConfig())
                )
            if snapshot is None:
                snapshot = _Snapshot(settings, self._meta(handoff))
            if request.next_message or not snapshot.next_message:
                snapshot = replace(snapshot, next_message=request.next_message)
            with self.control_db._lock:
                # A relaunch in this process still passes the message on. The
                # control DB holds no text, so a restart loses it.
                self._snapshots[(workspace_id, key)] = snapshot
            return self._launch(principal, handoff, snapshot)

        snapshot = _Snapshot(
            current, meta, lease_token=lease_token, next_message=request.next_message
        )
        with self.control_db._lock:
            self._snapshots[(workspace_id, key)] = snapshot
        self._emit(EVENT_STARTED, principal, handoff)
        try:
            messages = transcripts.parse(request.transcript, request.transcript_format)
            store = self.store_for(workspace_id)
            parent_id = ""
            if meta.parent_handoff_id:
                parent = self.db.get(meta.parent_handoff_id)
            else:
                # A successor that didn't inherit COMMONPLACE_HANDOFF_ID is still
                # linked when its own thread id was registered as the successor.
                parent = self.db.by_successor_thread(workspace_id, meta.thread_id)
            if parent and parent.workspace_id == workspace_id:
                parent_id = parent.stored_thread_id
            stored = store.store(
                workspace_id,
                meta.thread_id,
                messages,
                actor=principal.actor,
                parent_stored_id=parent_id,
            )

            self._mirror_later(principal, stored, store)

            doc = writer.build_doc(
                messages,
                meta,
                note=request.note,
                source_actor=stored.created_by,
                stored_thread_id=stored.id,
                handoff_id=handoff.id,
                chain=self._chain(store, parent_id, workspace_id, exclude=stored.id),
            )
            markdown = writer.render_markdown(doc)
            snapshot = replace(snapshot, doc=doc)
            with self.control_db._lock:
                self._snapshots[(workspace_id, key)] = snapshot
            handoff = self.db.mark_stored(handoff.id, stored.id, markdown, lease_token=lease_token)
        except HandoffLeaseError:
            return self._superseded(workspace_id, key, lease_token, handoff)
        except Exception:
            reason = "thread storage failed"
            try:
                handoff = self.db.mark_failed(handoff.id, reason, lease_token=lease_token)
            except HandoffLeaseError:
                return self._superseded(workspace_id, key, lease_token, handoff)
            except Exception:
                # A broken DB must still allow the calling agent to compact.
                pass
            with self.control_db._lock:
                self._snapshots.pop((workspace_id, key), None)
            self._emit(EVENT_FAILED, principal, handoff, decision="error", reason=reason)
            return HandoffResult("compact", handoff, reason=reason)

        self._emit(EVENT_STORED, principal, handoff)
        self._extract_later(principal, stored)
        return self._launch(principal, handoff, snapshot)

    def _append_retry(self, principal, request: HandoffRequest, handoff: Handoff):
        try:
            messages = transcripts.parse(request.transcript, request.transcript_format)
        except ValueError:
            return None  # Non-extension retries retain their previous behavior.
        try:
            store = self.store_for(handoff.workspace_id)
            stored = store.append(handoff.stored_thread_id, messages)
        except thread_store.ThreadConflict:
            return None
        except Exception:
            reason = "thread storage failed"
            self._emit(EVENT_FAILED, principal, handoff, decision="error", reason=reason)
            return HandoffResult("compact", handoff, reason=reason)
        self._mirror_later(principal, stored, store)
        self._extract_later(principal, stored)
        return None

    def _extract_later(self, principal, stored) -> None:
        """File facts independently of the mirror, after the source is durable."""
        if self.save_fact is None:
            return

        def job():
            try:
                from . import facts

                # Internal worker calls bypass policy. Extraction instead uses
                # the gateway's fact_save path, and must also be able to read
                # the private source before opening it.
                if (
                    stored.workspace_id != principal.workspace_id
                    or not principal.policy.has_scope("threads:read")
                    or not principal.policy.can_read_wing(stored.wing)
                    or (self.can_read is not None and not self.can_read(principal, stored.id))
                ):
                    return
                store = self.store_for(stored.workspace_id)
                facts.file_pending(store, stored, lambda args: self.save_fact(principal, args))
            except Exception:
                # Never log exception text or a candidate; it can contain chat
                # content. A retry resumes from the last committed cursor.
                logging.getLogger(__name__).warning(
                    "handoff fact extraction incomplete: %s", stored.id
                )

        try:
            with self._fact_lock:
                if self._background_closed:
                    return
                if self._fact_pool is None:
                    self._fact_pool = ThreadPoolExecutor(
                        max_workers=1, thread_name_prefix="handoff-facts"
                    )
                self._fact_jobs.append(self._fact_pool.submit(job))
                self._fact_jobs = [f for f in self._fact_jobs if not f.done()]
        except RuntimeError:
            logging.getLogger(__name__).warning("handoff fact scheduling incomplete: %s", stored.id)

    def wait_for_facts(self, timeout: Optional[float] = None) -> None:
        """Wait for fact jobs in tests; handoff requests never wait for them."""
        with self._fact_lock:
            jobs = list(self._fact_jobs)
        futures_wait(jobs, timeout=timeout)

    def close(self, timeout: float = 30) -> None:
        """Bound shutdown waiting across both background indexes."""
        with self._mirror_lock, self._fact_lock:
            self._background_closed = True
            for timer in self._index_timers.values():
                timer.cancel()
            self._index_timers.clear()
            self._index_active.clear()
            self._mirror_condition.notify_all()
            jobs = [*self._mirror_jobs, *self._fact_jobs]
            pools = [self._mirror_pool, self._fact_pool]
        futures_wait(jobs, timeout=timeout)
        for pool in pools:
            if pool is not None:
                pool.shutdown(wait=False, cancel_futures=True)

    def _mirror_later(self, principal, stored, store) -> None:
        """Mirror into Memory search in the background, one job at a time.

        Best effort and off the request path: the full thread is already safe
        in the lossless store, and embedding a large thread takes far longer
        than a hook may wait."""
        if self.call_tool is None:
            return

        def job():
            try:
                palace_mirror.mirror_pending(self.call_tool, principal, stored, store)
            except Exception:
                pass

        with self._mirror_lock:
            if self._background_closed:
                return
            if self._mirror_pool is None:
                self._mirror_pool = ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="handoff-mirror"
                )
            self._mirror_jobs.append(self._mirror_pool.submit(job))
            self._mirror_jobs = [f for f in self._mirror_jobs if not f.done()]

    def index_later(self, workspace_id, stored_ids=()) -> None:
        """Enroll sources and coalesce one asynchronous workspace sweep."""
        try:
            if not search_index.enabled() or self.call_tool is None:
                return
            if stored_ids:
                store = self.store_for(workspace_id)
                for stored_id in stored_ids:
                    stored = store.get(stored_id)
                    if stored is not None and stored.workspace_id == workspace_id:
                        store.begin_mirror(stored_id, -1, bootstrap=-1)
            with self._mirror_lock:
                if self._background_closed:
                    return
                state = self._index_states.setdefault(workspace_id, search_index.State())
                state.dirty = True
                if workspace_id in self._index_active:
                    return
                self._index_active.add(workspace_id)
                state.state = "running"
                self._submit_index(workspace_id)
        except Exception:
            pass

    def index_status(self, workspace_id, *, principal=None):
        """Project authenticated progress; local operators explicitly stay unscoped."""
        if principal is not None:
            return search_index.projected_status(self, workspace_id, principal)
        counts = self.store_for(workspace_id).index_counts(workspace_id)
        with self._mirror_lock:
            state = self._index_states.get(workspace_id, search_index.State())
            return {
                **counts,
                "state": state.state if search_index.enabled() else "off",
                "units_written": state.units_written,
            }

    def _submit_index(self, workspace_id):
        if self._mirror_pool is None:
            self._mirror_pool = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="handoff-mirror"
            )
        self._index_timers.pop(workspace_id, None)
        self._mirror_jobs = [f for f in self._mirror_jobs if not f.done()]
        self._mirror_jobs.append(self._mirror_pool.submit(self._index_job, workspace_id))

    def _index_job(self, workspace_id):
        state = self._index_states[workspace_id]
        again = search_index.sweep(self, workspace_id, state)
        with self._mirror_condition:
            if (again or state.dirty) and not self._background_closed and search_index.enabled():
                timer = threading.Timer(
                    search_index.PAUSE_SECONDS, self._resume_index, args=(workspace_id,)
                )
                timer.daemon = True
                self._index_timers[workspace_id] = timer
                timer.start()
            else:
                self._index_active.discard(workspace_id)
            self._mirror_condition.notify_all()

    def _resume_index(self, workspace_id):
        with self._mirror_condition:
            if self._background_closed:
                self._index_active.discard(workspace_id)
                self._index_timers.pop(workspace_id, None)
            else:
                self._submit_index(workspace_id)
            self._mirror_condition.notify_all()

    def wait_for_mirrors(self, timeout: Optional[float] = None) -> None:
        """Drain mirrors, including delayed sweep continuations."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            remaining = None if deadline is None else max(0, deadline - time.monotonic())
            with self._mirror_condition:
                jobs = [f for f in self._mirror_jobs if not f.done()]
                if not jobs and not self._index_active:
                    return
                if remaining == 0:
                    return
                if not jobs:
                    self._mirror_condition.wait(remaining)
                    continue
            futures_wait(jobs, timeout=remaining)

    def _superseded(self, workspace_id: str, key: str, lease_token: str, handoff: Handoff):
        with self.control_db._lock:
            snapshot = self._snapshots.get((workspace_id, key))
            if snapshot and snapshot.lease_token == lease_token:
                self._snapshots.pop((workspace_id, key), None)
            handoff = self.db.get(handoff.id) or handoff
        return HandoffResult("compact", handoff, reason="in progress")

    @staticmethod
    def _can_reuse(principal, handoff: Handoff) -> bool:
        return principal.role == "owner" or handoff.connection_id == principal.connection_id

    def register_successor(self, principal, handoff_id: str, ref: SuccessorRef) -> Handoff:
        with self.control_db._lock:
            handoff = self._authorized(principal, handoff_id)
            if handoff.state == "done":
                # A retry also delivers a completion event a crash left queued.
                self.events.deliver_pending(handoff_id=handoff_id)
                if handoff.successor == ref:
                    return handoff
                # Let the database validate retries and fill missing metadata;
                # enriching an existing successor is not another completion.
                return self.db.set_successor(handoff_id, ref, caller=principal)
            if handoff.state not in ("stored", "launched"):
                raise HandoffDBError("handoff is not ready for a successor")
            # handoff.completed is queued in the same transaction as the
            # successor, then delivered: a crash in between never loses it.
            handoff = self.db.set_successor(handoff_id, ref, caller=principal)
            self.events.deliver_pending(handoff_id=handoff_id)
            return handoff

    def replay_pending(self) -> int:
        """Deliver every queued event (gateway start)."""
        return self.events.deliver_pending()

    def report_launch_failure(self, principal, handoff_id: str, error: str) -> Handoff:
        """The client launcher couldn't start the successor. Same permission
        as ``register_successor``; stored/launched → ``launch_failed``,
        ``EVENT_LAUNCH_FAILED`` once (repeats are no-ops); ``error`` is cut
        to 200 chars and goes to the event reason only."""
        with self.control_db._lock:
            handoff = self._authorized(principal, handoff_id)
            if handoff.state == "launch_failed":
                return handoff
            if handoff.state not in ("stored", "launched"):
                raise HandoffDBError("handoff is not ready to report a launch failure")
            handoff = self.db.mark_launch_failed(handoff_id, "successor launch failed")
            self._emit(
                EVENT_LAUNCH_FAILED, principal, handoff, decision="error", reason=error[:200]
            )
            return handoff

    def relaunch(self, principal, handoff_id: str) -> HandoffResult:
        require_launch_authority(principal)
        person = getattr(principal, "person", False)
        if not person and principal.policy.write_wing in ("inbox", "wing_inbox"):
            raise HandoffPermissionError("restricted connections cannot relaunch handoffs")
        with self.control_db._lock:
            handoff = self._authorized(principal, handoff_id, owner_only=person)
            if handoff.state != "launch_failed":
                raise HandoffDBError("handoff is not ready to relaunch")
            snapshot = self._snapshots.get((handoff.workspace_id, handoff.idempotency_key))
            if snapshot is None:
                snapshot = _Snapshot(
                    self.settings.effective(
                        handoff.workspace_id,
                        handoff.agent or principal.connection,
                        handoff.task_ref,
                    ),
                    self._meta(handoff),
                )
            handoff = self.db.release_launch(handoff.id, actor=principal.actor)
        return self._launch(principal, handoff, snapshot)

    def _launch(self, principal, handoff: Handoff, snapshot: _Snapshot) -> HandoffResult:
        claimed = False
        try:
            lease_token = snapshot.lease_token
            if lease_token is None:
                lease_token = self.db.lease_token(handoff.id)
            if not self.db.claim_launch(handoff.id, lease_token=lease_token):
                return HandoffResult("handoff", handoff)
            claimed = True
            doc = snapshot.doc
            if doc is None:
                store = self.store_for(handoff.workspace_id)
                doc = writer.build_doc(
                    store.messages(handoff.stored_thread_id),
                    snapshot.meta,
                    stored_thread_id=handoff.stored_thread_id,
                    source_actor=store.get(handoff.stored_thread_id).created_by,
                    handoff_id=handoff.id,
                    chain=self._chain(
                        store,
                        store.parent_of(handoff.stored_thread_id) or "",
                        handoff.workspace_id,
                        exclude=handoff.stored_thread_id,
                    ),
                )
            if snapshot.doc is None and handoff.doc_markdown:
                prompt = writer.successor_prompt(doc, markdown=handoff.doc_markdown)
            else:
                prompt = writer.successor_prompt(doc)
            prompt = writer.with_next_message(prompt, snapshot.next_message)
            with self.control_db._lock:
                current = self.db.get(handoff.id)
                if current is None or current.state != "stored" or current.successor is not None:
                    return HandoffResult("handoff", current or handoff)
                handoff = self.db.mark_launched(handoff.id)
            successor = launchers.launch(
                snapshot.settings.launcher, handoff, prompt, prompt_dir=self.prompt_dir
            )
            if successor is not None:
                try:
                    handoff = self.register_successor(principal, handoff.id, successor)
                except Exception:
                    # The launcher already succeeded. A registration failure
                    # must not release its claim via a launch_failed relaunch:
                    # that would start a duplicate external successor.
                    try:
                        handoff = self.db.get(handoff.id) or handoff
                    except Exception:
                        pass
                    return HandoffResult(
                        "handoff",
                        handoff,
                        reason="" if handoff.state == "done" else "successor registration failed",
                    )
            else:
                with self.control_db._lock:
                    handoff = self.db.get(handoff.id) or handoff
            client_launch = (
                snapshot.settings.launcher.kind == "client"
                and handoff.state == "launched"
                and handoff.successor is None
            )
            return HandoffResult(
                "handoff",
                handoff,
                launch=client_launch,
                prompt=prompt if client_launch else "",
            )
        except launchers.LaunchPolicyError as error:
            return self._launch_failure(principal, handoff, claimed=claimed, reason=str(error))
        except Exception:
            return self._launch_failure(principal, handoff, claimed=claimed)

    def _launch_failure(
        self, principal, handoff: Handoff, *, claimed: bool, reason: str = "successor launch failed"
    ) -> HandoffResult:
        try:
            with self.control_db._lock:
                current = self.db.get(handoff.id)
                if current is not None:
                    handoff = current
                if (
                    not claimed
                    or current is None
                    or current.state not in ("stored", "launched")
                    or current.successor is not None
                ):
                    return HandoffResult(
                        "handoff", handoff, reason="" if handoff.state == "done" else reason
                    )
                handoff = self.db.mark_launch_failed(handoff.id, reason)
                self._emit(EVENT_LAUNCH_FAILED, principal, handoff, decision="error", reason=reason)
        except Exception:
            pass
        return HandoffResult("handoff", handoff, reason=reason)

    def _authorized(self, principal, handoff_id: str, *, owner_only: bool = False) -> Handoff:
        handoff = self.db.get(handoff_id)
        if (
            handoff is None
            or handoff.workspace_id != principal.workspace_id
            or (
                principal.role != "owner"
                and (owner_only or handoff.connection_id != principal.connection_id)
            )
        ):
            raise LookupError("handoff not found")
        return handoff

    @staticmethod
    def _meta(handoff: Handoff) -> ThreadMeta:
        return ThreadMeta(
            thread_id=handoff.source_thread_id,
            agent=handoff.agent,
            task_ref=handoff.task_ref,
            branch=handoff.branch,
            pr_url=handoff.pr_url,
            cwd=handoff.cwd,
            parent_handoff_id=handoff.parent_handoff_id,
        )

    @staticmethod
    def _chain(store, parent_id: str, workspace_id: str, *, exclude: str) -> tuple:
        chain = []
        seen = {exclude}
        while parent_id and parent_id not in seen:
            seen.add(parent_id)
            parent = store.get(parent_id)
            if parent is None or parent.workspace_id != workspace_id:
                break
            chain.append(parent_id)
            parent_id = store.parent_of(parent_id)
        return tuple(chain)

    def _emit(self, event: str, principal, handoff: Optional[Handoff], **details) -> None:
        try:
            self.events.emit(
                event,
                workspace_id=handoff.workspace_id if handoff else principal.workspace_id,
                handoff=handoff,
                principal=principal,
                **details,
            )
        except Exception:
            # Event delivery must not jeopardize storage or stop compaction.
            pass
