"""Reconcile board events into private, deduplicated phone notifications."""

import json
import logging
import threading
import time
import uuid

from commonplace import events, webhooks
from commonplace.http_util import log_failure

from . import eligibility, evaluate, schema, sender

logger = logging.getLogger(__name__)
_CATEGORIES = ("needs_you", "blocked", "done")


class SeedRace(Exception):
    """The event snapshot changed while seeding a subscription."""


def _max_seq(conn):
    return int(conn.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0])


def _caught_up(conn, workspace, cursor):
    return (
        conn.execute(
            "SELECT 1 FROM events WHERE workspace_id=? AND seq>? AND kind LIKE 'card.%' LIMIT 1",
            (workspace, cursor),
        ).fetchone()
        is None
    )


def _leaves(event, category):
    kind, data = event["kind"], event["data"]
    if category == "needs_you":
        return kind in ("card.decided", "card.ask_withdrawn") or (
            kind == "card.moved" and data.get("from") == "review"
        )
    return category == "done" and kind == "card.moved" and data.get("from") == "done"


def _delete(conn, subscription, number, category):
    conn.execute(
        "DELETE FROM phone_push_state WHERE subscription_id=? AND number=? AND category=?",
        (subscription, number, category),
    )
    conn.execute(
        "UPDATE phone_push_deliveries SET status='cancelled', error=NULL "
        "WHERE subscription_id=? AND number=? AND category=? AND status IN ('pending','retry')",
        (subscription, number, category),
    )


def _insert(conn, sub, number, category, entered_seq, *, notify):
    inserted = conn.execute(
        "INSERT OR IGNORE INTO phone_push_state(subscription_id,number,category,entered_seq) "
        "VALUES (?,?,?,?)",
        (sub["id"], number, category, entered_seq),
    ).rowcount
    if inserted and notify:
        conn.execute(
            "INSERT INTO phone_push_deliveries "
            "(id,subscription_id,workspace_id,number,category,status,attempt,next_at,created_at) "
            "VALUES (?,?,?,?,?,'pending',0,?,?)",
            (
                uuid.uuid4().hex,
                sub["id"],
                sub["workspace_id"],
                number,
                category,
                time.time(),
                events._now(),
            ),
        )


class PushDispatcher:
    def __init__(self, db, *, gateway, schedule, max_attempts, sweep_seconds=300):
        if not schedule or any(delay < 0 for delay in schedule) or max_attempts < 1:
            raise ValueError("invalid phone notification retry schedule")
        if sweep_seconds <= 0:
            raise ValueError("invalid phone notification sweep interval")
        self.db, self.gateway = db, gateway
        self.schedule, self.max_attempts = tuple(schedule), max_attempts
        self.sweep_seconds = sweep_seconds
        self._work = threading.RLock()
        self._stop, self._wake = threading.Event(), threading.Event()
        self._evaluation_failed = False
        self._ineligible_logged = set()
        self.thread = threading.Thread(target=self._run, name="commonplace-phone-push", daemon=True)

    @property
    def work(self):
        return self._work

    def start(self):
        events.add_listener(self.on_event)
        try:
            self._wake.set()
            self.thread.start()
        except Exception:
            events.remove_listener(self.on_event)
            raise
        return self

    def stop(self):
        events.remove_listener(self.on_event)
        self._stop.set()
        self._wake.set()
        if self.thread.ident is not None:
            self.thread.join(timeout=webhooks.HTTP_TIMEOUT + 2)

    def wake(self):
        self._wake.set()

    def _run(self):
        last_sweep = time.monotonic()
        backoff = 5
        recover_claims = False
        while not self._stop.is_set():
            self._wake.wait(timeout=max(0, self.sweep_seconds - (time.monotonic() - last_sweep)))
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                if recover_claims:
                    with self.work:
                        sender.recover(self.db, self.max_attempts)
                    recover_claims = False
                # A failed batch is retried at the next wake, never skipped or spun on.
                while not self._stop.is_set():
                    if self.reconcile() or self._evaluation_failed:
                        break
                if self._stop.is_set():
                    break
                if time.monotonic() - last_sweep >= self.sweep_seconds:
                    self.sweep()
                    last_sweep = time.monotonic()
                self.drain()
            except Exception as exc:
                log_failure(logger, "phone push worker failed", exc)
                recover_claims = True
                if self._stop.wait(backoff):
                    break
                backoff = min(backoff * 2, 300)
                self._wake.set()
                continue
            backoff = 5

    def on_event(self, e):
        with self.db._lock:
            stored = self.db._conn.execute(
                "SELECT * FROM events WHERE seq=?", (e["seq"],)
            ).fetchone()
            if stored is None or events._row(stored) != e:
                return
        self.wake()

    def _owner(self, sub):
        owner = eligibility.owner_principal(self.db, sub["user_id"], sub["workspace_id"])
        if owner is None and sub["id"] not in self._ineligible_logged:
            self._ineligible_logged.add(sub["id"])
            logger.warning("phone push subscription is ineligible")
        return owner

    def reconcile(self) -> bool:
        # One batch per call: the daemon releases this lock between batches.
        with self.work:
            self._evaluation_failed = False
            try:
                with self.db._lock:
                    cursor = int(schema.meta_get(self.db, "phone_push_seq"))
                    batch = self.db._conn.execute(
                        "SELECT seq,workspace_id,kind,actor,data FROM events "
                        "WHERE seq>? ORDER BY seq LIMIT 200",
                        (cursor,),
                    ).fetchall()
                    if not batch:
                        return True
                    subscriptions = self.db._conn.execute(
                        "SELECT * FROM phone_push_subscriptions WHERE active=1"
                    ).fetchall()
                    states = self.db._conn.execute("SELECT * FROM phone_push_state").fetchall()
                grouped = {}
                for row in batch:
                    if not row["kind"].startswith("card."):
                        continue
                    data = json.loads(row["data"] or "{}")
                    number, board = data.get("number"), data.get("board")
                    if type(number) is not int or not isinstance(board, str):
                        continue
                    # Keep only routing labels, never user content.
                    event = {
                        "seq": row["seq"],
                        "kind": row["kind"],
                        "actor": row["actor"],
                        "data": {
                            "number": number,
                            "board": board,
                            "from": data.get("from"),
                            "to": data.get("to"),
                        },
                    }
                    grouped.setdefault(row["workspace_id"], []).append(event)
                old = {(s["subscription_id"], s["number"], s["category"]): s for s in states}
                changes = []
                for sub in subscriptions:
                    window = [
                        e
                        for e in grouped.get(sub["workspace_id"], ())
                        if e["seq"] > sub["from_seq"]
                    ]
                    if not window:
                        continue
                    owner = self._owner(sub)
                    if owner is None:
                        continue
                    cur = evaluate.current(self.gateway, owner)
                    paused = eligibility.paused(self.db, sub["user_id"], sub["workspace_id"])
                    for number in dict.fromkeys(e["data"]["number"] for e in window):
                        card_events = [e for e in window if e["data"]["number"] == number]
                        for category in _CATEGORIES:
                            state = old.get((sub["id"], number, category))
                            after = max(sub["from_seq"], state["entered_seq"] if state else 0)
                            leaves = [
                                e["seq"]
                                for e in card_events
                                if e["seq"] > after and _leaves(e, category)
                            ]
                            ended = bool(leaves)
                            if leaves:
                                after = max(leaves)
                            applicable = evaluate.applies(
                                self.gateway, owner, number, category, cur=cur
                            )
                            if not applicable:
                                if state:
                                    changes.append((sub, number, category, True, None, False))
                                continue
                            if state and not ended:
                                continue
                            entering = evaluate.enters(
                                card_events, number, category, after_seq=after
                            )
                            if category == "done" and not entering:
                                if state and ended:
                                    changes.append((sub, number, category, True, None, False))
                                continue
                            notify = not paused and (
                                not entering
                                or any(e["actor"] != f"user:{sub['user_id']}" for e in entering)
                            )
                            entered_seq = max(e["seq"] for e in card_events)
                            changes.append((sub, number, category, ended, entered_seq, notify))
            except Exception:
                self._evaluation_failed = True
                logger.warning("phone push evaluation failed")
                return False
            with self.db._lock, self.db._tx() as conn:
                for sub, number, category, ended, entered_seq, notify in changes:
                    if not conn.execute(
                        "SELECT 1 FROM phone_push_subscriptions WHERE id=? AND active=1",
                        (sub["id"],),
                    ).fetchone():
                        continue
                    if ended:
                        _delete(conn, sub["id"], number, category)
                    if entered_seq is not None:
                        _insert(conn, sub, number, category, entered_seq, notify=notify)
                schema.meta_set(conn, "phone_push_seq", batch[-1]["seq"])
                return batch[-1]["seq"] >= _max_seq(conn)

    def sweep(self):
        with self.work:
            try:
                with self.db._lock:
                    cursor = int(schema.meta_get(self.db, "phone_push_seq"))
                    subscriptions = self.db._conn.execute(
                        "SELECT * FROM phone_push_subscriptions WHERE active=1"
                    ).fetchall()
                    states = self.db._conn.execute(
                        "SELECT * FROM phone_push_state WHERE category IN ('needs_you','blocked')"
                    ).fetchall()
                    caught = {
                        s["workspace_id"]: _caught_up(self.db._conn, s["workspace_id"], cursor)
                        for s in subscriptions
                    }
                evaluated = []
                for sub in subscriptions:
                    owner = self._owner(sub)
                    if owner is None:
                        continue
                    cur = evaluate.current(self.gateway, owner)
                    paused = eligibility.paused(self.db, sub["user_id"], sub["workspace_id"])
                    evaluated.append((sub, cur, paused))
            except Exception:
                logger.warning("phone push evaluation failed")
                return
            with self.db._lock, self.db._tx() as conn:
                for sub, cur, paused in evaluated:
                    if not conn.execute(
                        "SELECT 1 FROM phone_push_subscriptions WHERE id=? AND active=1",
                        (sub["id"],),
                    ).fetchone():
                        continue
                    for state in states:
                        if state["subscription_id"] == sub["id"] and state[
                            "category"
                        ] not in cur.get(state["number"], ()):
                            _delete(conn, sub["id"], state["number"], state["category"])
                    allow_needs = caught[sub["workspace_id"]] and _caught_up(
                        conn, sub["workspace_id"], cursor
                    )
                    for number, categories in cur.items():
                        for category in categories:
                            if category == "blocked" or (category == "needs_you" and allow_needs):
                                _insert(
                                    conn, sub, number, category, _max_seq(conn), notify=not paused
                                )

    def drain(self) -> int:
        return sender.drain(self)

    def seed(self, owner, topic) -> str:
        with self.work:
            for _ in range(3):
                with self.db._lock:
                    existing = self.db._conn.execute(
                        "SELECT id FROM phone_push_subscriptions WHERE workspace_id=? AND user_id=? AND active=1",
                        (owner.workspace_id, owner.user_id),
                    ).fetchone()
                    if existing:
                        return existing["id"]
                    snap = _max_seq(self.db._conn)
                cur = evaluate.current(self.gateway, owner)
                with self.db._lock, self.db._tx() as conn:
                    if _max_seq(conn) != snap:
                        continue
                    subscription = uuid.uuid4().hex
                    conn.execute(
                        "INSERT INTO phone_push_subscriptions "
                        "(id,workspace_id,user_id,topic,active,from_seq,created_at) VALUES (?,?,?,?,1,?,?)",
                        (
                            subscription,
                            owner.workspace_id,
                            owner.user_id,
                            topic,
                            snap,
                            events._now(),
                        ),
                    )
                    sub = {"id": subscription, "workspace_id": owner.workspace_id}
                    for number, categories in cur.items():
                        for category in categories & {"needs_you", "blocked"}:
                            _insert(conn, sub, number, category, snap, notify=False)
                    return subscription
            logger.error("phone push subscription snapshot changed")
            raise SeedRace()
