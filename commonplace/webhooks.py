"""Persistent, signed webhook deliveries for Commonplace events.

The listener only queues work. One stoppable daemon per gateway performs HTTP.
Payloads contain an explicit metadata allowlist, never arbitrary event data.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import ipaddress
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from . import events
from .extensions import WORK_PROFILES, LocalTool, ToolError, register
from .http_util import log_failure

logger = logging.getLogger(__name__)
BACKOFF = (10, 60, 300, 1800, 7200, 21600)
MAX_ATTEMPTS = 6
HTTP_TIMEOUT = 5
# Seconds the worker waits after an unexpected pass error, doubling up to the cap.
ERROR_BACKOFF = 5
MAX_ERROR_BACKOFF = 300
SCHEMA = """
CREATE TABLE IF NOT EXISTS webhooks (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    connection_id TEXT NOT NULL DEFAULT '',
    owner_actor TEXT NOT NULL,
    url TEXT NOT NULL,
    secret_hash TEXT NOT NULL,
    secret TEXT NOT NULL,
    kinds TEXT NOT NULL,
    board TEXT NOT NULL DEFAULT '',
    waiting_on TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    start_seq INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS webhooks_workspace ON webhooks(workspace_id, active);
CREATE TABLE IF NOT EXISTS webhook_deliveries (
    id TEXT PRIMARY KEY,
    webhook_id TEXT NOT NULL REFERENCES webhooks(id),
    event_seq INTEGER NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    http_status INTEGER,
    error TEXT NOT NULL DEFAULT '',
    next_at REAL NOT NULL,
    delivered_at TEXT,
    payload BLOB NOT NULL,
    UNIQUE(webhook_id, event_seq)
);
CREATE INDEX IF NOT EXISTS webhook_due ON webhook_deliveries(status, next_at);
CREATE TABLE IF NOT EXISTS webhook_attempts (
    delivery_id TEXT NOT NULL REFERENCES webhook_deliveries(id),
    attempt INTEGER NOT NULL,
    at TEXT NOT NULL,
    http_status INTEGER,
    error TEXT NOT NULL,
    PRIMARY KEY(delivery_id, attempt)
);
"""
# Titles are card metadata in the contract. Text/body/ask/note/reply are excluded.
_DATA_FIELDS = frozenset(
    {
        "board",
        "number",
        "title",
        "status",
        "from",
        "to",
        "assignee",
        "ask_to",
        "return_to",
        "actor",
        "id",
        "theme",
        "topic",
        "webhook_id",
        "delivery_id",
        # Context handoff: ids only, never thread text.
        "handoff_id",
        "stored_thread_id",
        "task_ref",
        "successor_agent_ref",
        "decision",
        "message_id",
        "coordinator",
    }
)
_PRESETS = {
    "card_changed": ("card.*",),
    "review_needed": ("card.review_needed",),
    "waiting_on_me": ("card.handoff", "card.decided"),
    "handoff": (
        "handoff.started",
        "handoff.stored",
        "handoff.completed",
        "handoff.failed",
        "handoff.launch_failed",
        "handoff.settings_changed",
    ),
}


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_schema(db):
    events.ensure_schema(db)
    with db._lock:
        db._conn.executescript(SCHEMA)
        columns = {row[1] for row in db._conn.execute("PRAGMA table_info(webhooks)")}
        if "auth_header" not in columns:
            db._conn.execute("ALTER TABLE webhooks ADD COLUMN auth_header TEXT")
        if "start_seq" not in columns:
            db._conn.execute("ALTER TABLE webhooks ADD COLUMN start_seq INTEGER NOT NULL DEFAULT 0")
            # Older registrations lack a sequence boundary; exclude events from
            # before their creation, retaining same-second events for recovery.
            db._conn.execute(
                "UPDATE webhooks SET start_seq = coalesce((SELECT max(seq) FROM events "
                "WHERE workspace_id = webhooks.workspace_id AND at < webhooks.created_at), 0)"
            )
        delivery_columns = {r[1] for r in db._conn.execute("PRAGMA table_info(webhook_deliveries)")}
        for name, datatype in {
            "report_auth_retry_count": "INTEGER NOT NULL DEFAULT 0",
            "report_auth_retry_since": "TEXT DEFAULT NULL",
            "report_envelope": "TEXT",
        }.items():
            if name not in delivery_columns:
                db._conn.execute(f"ALTER TABLE webhook_deliveries ADD COLUMN {name} {datatype}")
        from commonplace.projects import reports

        reports.ensure_control(db)
        db._conn.execute(
            "INSERT OR IGNORE INTO meta(key,value) VALUES ('webhook_dispatch_seq','0')"
        )


def validate_url(url):
    """Validate structurally; HTTP is limited to local and tailnet endpoints."""
    if not isinstance(url, str) or not url or len(url) > 4096:
        raise ToolError("no: invalid webhook URL")
    if any(ord(c) <= 32 or ord(c) == 127 for c in url) or "\\" in url:
        raise ToolError("no: invalid webhook URL")
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
        if (
            parts.scheme not in ("http", "https")
            or not host
            or parts.username is not None
            or parts.password is not None
            or parts.fragment
            or port == 0
            or "%" in host
        ):
            raise ValueError()
        host = host.lower().rstrip(".")
        if parts.scheme == "https":
            return url
        if host == "localhost" or (host.endswith(".ts.net") and host != "ts.net"):
            return url
        address = ipaddress.ip_address(host)
        # IPv4-mapped IPv6 hosts are refused outright: Python versions
        # disagree on whether ::ffff:127.0.0.1 counts as loopback.
        if getattr(address, "ipv4_mapped", None) is not None:
            raise ValueError()
        if address.is_loopback or (
            isinstance(address, ipaddress.IPv4Address)
            and address in ipaddress.ip_network("100.64.0.0/10")
        ):
            return url
    except ValueError:
        pass
    raise ToolError("no: use HTTPS or a loopback or tailnet HTTP URL")


def _authorize(principal, action):
    if not principal.person and not principal.policy.tool_visible("webhook_" + action):
        raise ToolError("no: webhook access refused")


def _visible(principal):
    # Tokens stay scoped to their connection even when their holder is an owner.
    if principal.person and principal.role == "owner":
        return "workspace_id = ?", (principal.workspace_id,)
    if principal.connection_id:
        return "workspace_id = ? AND connection_id = ?", (
            principal.workspace_id,
            principal.connection_id,
        )
    return "workspace_id = ? AND owner_actor = ? AND connection_id = ''", (
        principal.workspace_id,
        principal.actor,
    )


def _get(db, principal, webhook_id):
    ensure_schema(db)
    where, args = _visible(principal)
    with db._lock:
        row = db._conn.execute(
            f"SELECT * FROM webhooks WHERE {where} AND id = ? AND active = 1",
            (*args, webhook_id),
        ).fetchone()
    if row is None:
        raise ToolError("no: webhook not found")
    return row


def _public(row):
    return {
        key: (json.loads(row[key]) if key == "kinds" else row[key])
        for key in (
            "id",
            "workspace_id",
            "connection_id",
            "owner_actor",
            "url",
            "kinds",
            "board",
            "waiting_on",
            "active",
            "created_at",
        )
    }


def _auth_header(auth):
    """``"Bearer xyz"`` (sent as Authorization) or ``"X-Name: value"``.
    Stored to send with each delivery; never returned by any API."""
    if auth is None or auth == "":
        return None
    if isinstance(auth, str) and auth.startswith("env:"):
        # A reference, resolved at send time from the gateway's environment:
        # the secret never enters the database. Only this prefix, so no
        # caller can point a webhook at some other secret.
        name = auth[4:]
        if not re.fullmatch(r"COMMONPLACE_WEBHOOK_AUTH_[A-Z0-9_]{1,40}", name):
            raise ToolError("no: env auth must name COMMONPLACE_WEBHOOK_AUTH_<NAME>")
        return json.dumps({"env": name})
    if not isinstance(auth, str) or len(auth) > 1000 or any(c in auth for c in "\r\n\x00"):
        raise ToolError("no: invalid auth header")
    name, sep, value = auth.partition(":")
    if sep and re.fullmatch(r"Authorization|X-[A-Za-z0-9-]{1,40}", name.strip()):
        name, value = name.strip(), value.strip()
    else:
        name, value = "Authorization", auth.strip()
    if not value:
        raise ToolError("no: invalid auth header")
    return json.dumps({"name": name, "value": value})


def add(db, principal, url, *, kinds=None, board="", waiting_on="", auth=None):
    _authorize(principal, "add")
    validate_url(url)
    auth_header = _auth_header(auth)
    if isinstance(kinds, str):
        kinds = [kinds]
    if kinds is None:
        kinds = ["*"]
    if not isinstance(kinds, list) or not kinds or len(kinds) > 100:
        raise ToolError("no: events must be a nonempty list")
    expanded = []
    for kind in kinds:
        if not isinstance(kind, str) or not re.fullmatch(r"[a-z][a-z0-9_.]*|\*|card\.\*", kind):
            raise ToolError("no: invalid event kind")
        expanded.extend(_PRESETS.get(kind, (kind,)))
    if not isinstance(board, str) or not isinstance(waiting_on, str):
        raise ToolError("no: board and waiting_on must be strings")
    if "waiting_on_me" in kinds:
        if waiting_on and waiting_on != principal.actor:
            raise ToolError("no: waiting_on_me targets your actor")
        waiting_on = principal.actor
    if waiting_on and not waiting_on.startswith(("conn:", "user:")):
        raise ToolError("no: waiting_on must be an actor reference")
    ensure_schema(db)
    secret = secrets.token_urlsafe(32)
    webhook_id = "wh_" + secrets.token_hex(12)
    with db._lock, db._tx():
        start_seq = db._conn.execute(
            "SELECT coalesce(max(seq),0) FROM events WHERE workspace_id=?",
            (principal.workspace_id,),
        ).fetchone()[0]
        db._conn.execute(
            "INSERT INTO webhooks(id, workspace_id, connection_id, owner_actor, url, "
            "secret_hash, secret, kinds, board, waiting_on, created_at, start_seq, auth_header) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                webhook_id,
                principal.workspace_id,
                principal.connection_id,
                principal.actor,
                url,
                hashlib.sha256(secret.encode()).hexdigest(),
                secret,
                json.dumps(expanded),
                board,
                waiting_on,
                _now(),
                start_seq,
                auth_header,
            ),
        )
    return {**_public(_get(db, principal, webhook_id)), "secret": secret}


def list_webhooks(db, principal):
    _authorize(principal, "list")
    ensure_schema(db)
    where, args = _visible(principal)
    with db._lock:
        rows = db._conn.execute(
            f"SELECT * FROM webhooks WHERE {where} AND active = 1 ORDER BY created_at, id", args
        ).fetchall()
    return [_public(row) for row in rows]


def remove(db, principal, webhook_id):
    _authorize(principal, "remove")
    with db._lock:
        _get(db, principal, webhook_id)
        with db._tx():
            db._conn.execute("UPDATE webhooks SET active = 0 WHERE id = ?", (webhook_id,))
            db._conn.execute(
                "UPDATE webhook_deliveries SET status = 'cancelled',error='', "
                "report_auth_retry_count=0,report_auth_retry_since=NULL "
                "WHERE webhook_id = ? AND status IN ('pending','retry')",
                (webhook_id,),
            )
    return {"removed": webhook_id}


def deliveries(db, principal, webhook_id):
    _authorize(principal, "list")
    with db._lock:
        _get(db, principal, webhook_id)
        rows = db._conn.execute(
            "SELECT id, webhook_id, event_seq, attempt, status, http_status, error, next_at, "
            "delivered_at, report_auth_retry_count AS count, report_auth_retry_since AS retry_since "
            "FROM webhook_deliveries WHERE webhook_id = ? AND (report_auth_retry_count > 0 OR "
            "rowid IN (SELECT rowid FROM webhook_deliveries WHERE webhook_id = ? "
            "ORDER BY rowid DESC LIMIT 100)) ORDER BY rowid DESC",
            (webhook_id, webhook_id),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["attempts"] = [
                dict(a)
                for a in db._conn.execute(
                    "SELECT attempt, at, http_status, error FROM webhook_attempts "
                    "WHERE delivery_id = ? ORDER BY attempt",
                    (row["id"],),
                ).fetchall()
            ]
            result.append(item)
    return result


def _matches(row, event):
    if row["workspace_id"] != event["workspace_id"]:
        return False
    if event["kind"] == "project.report":
        return False  # Reserved routing must precede every wildcard path.
    kinds = json.loads(row["kinds"])
    kind = event["kind"]
    # Replies refresh the app but must never turn a wildcard wake into a reply loop.
    if kind == "space_replied":
        return False
    if not (kind in kinds or "*" in kinds or ("card.*" in kinds and kind.startswith("card."))):
        return False
    data = event["data"]
    if row["board"] and row["board"] != data.get("board"):
        return False
    if kind == "space_message":
        return data.get("coordinator") == row["owner_actor"]
    if row["waiting_on"]:
        return kind in ("card.review_needed", "card.handoff", "card.decided") and row[
            "waiting_on"
        ] in (data.get("ask_to"), data.get("return_to"), data.get("assignee"))
    return True


def _payload(db, event):
    from commonplace.projects.report_dispatch import PUBLIC_FIELDS

    fields = PUBLIC_FIELDS if event["kind"] == "project.report" else _DATA_FIELDS
    data = {
        k: v
        for k, v in event["data"].items()
        if k in fields and (isinstance(v, (str, int, float, bool)) or v is None)
    }
    number = data.get("number")
    if number is None and re.fullmatch(r"#\d+", event["subject"]):
        number = event["subject"][1:]
    path = "/app/"
    if str(number).isdigit():
        path += "#/card/" + str(number)
    elif event["kind"] == "space_message":
        from urllib.parse import quote

        path += "#/space/" + quote(data["board"], safe="")
    try:
        from commonplace import urls
    except ImportError:
        base = (
            os.environ.get("COMMONPLACE_APP_URL")
            or os.environ.get("COMMONPLACE_BASE_URL")
            or "http://127.0.0.1:8765"
        )
        url = base.rstrip("/") + path
    else:
        url = (
            urls.app_link(path)
            if event["kind"] == "project.report"
            else urls.share_link(db, event["workspace_id"], path)
        )
    decision = event["kind"] == "card.review_needed" or (
        event["kind"] == "card.handoff" and str(data.get("ask_to") or "").startswith("user:")
    )
    label = {
        "card.review_needed": "waits on you",
        "card.handoff": "handed off",
        "card.decided": "decided",
        "card.created": "new card",
        "card.updated": "updated",
        "card.moved": f"moved to {data.get('to') or data.get('status') or ''}".strip(),
        "card.commented": "new comment",
        "card.claimed": "picked up",
        "card.expired": "claim expired",
        "space_message": "new request",
        "handoff.completed": "context handed off",
        "handoff.failed": "handoff failed, compacted instead",
        "handoff.launch_failed": "successor didn't start",
    }.get(event["kind"], event["kind"])
    title = data.get("title")
    message = " ".join(
        part for part in (event["subject"], f'"{title}"' if title else "", label, url) if part
    )
    if event["kind"] == "webhook.test":
        message = f"test wake {event['subject']} {url}"
    return json.dumps(
        {
            # Read by agents' configured wake endpoints.
            "agent": "commonplace",
            "task": f"{event['subject']} {title}" if title else event["subject"],
            "need": "decision" if decision else "info",
            "message": message,
            "id": f"evt_{event['seq']}",
            "seq": event["seq"],
            "kind": event["kind"],
            "at": event["at"],
            "workspace": event["workspace_id"],
            "subject": event["subject"],
            "actor": event["actor"],
            "data": data,
            "url": url,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _enqueue(db, row, event):
    delivery_id = "dlv_" + secrets.token_hex(12)
    with db._lock:
        db._conn.execute(
            "INSERT OR IGNORE INTO webhook_deliveries "
            "(id, webhook_id, event_seq, next_at, payload, report_envelope) VALUES (?,?,?,?,?,?)",
            (
                delivery_id,
                row["id"],
                event["seq"],
                time.time(),
                _payload(db, event),
                json.dumps(event) if event["kind"] == "project.report" else None,
            ),
        )
    return delivery_id


def _can_receive(db, gateway, row, event):
    """Subscriptions retain an identity, never a snapshot of its authority."""
    try:
        if not row["active"] or row["workspace_id"] != event["workspace_id"]:
            return False
        if row["connection_id"]:
            principal = db.authenticate_connection(row["connection_id"])
        elif row["owner_actor"].startswith("user:"):
            from .sessions import person_principal

            principal = person_principal(db, row["owner_actor"][5:], row["workspace_id"])
        else:
            return False
        if principal is None or principal.workspace_id != row["workspace_id"]:
            return False
        if not principal.policy.has_scope("memory:read"):
            return False
        # Delivery rows carry webhook_id; registration rows are the webhook.
        own_id = row["webhook_id"] if "webhook_id" in row.keys() else row["id"]
        if set(event["data"]) == {"webhook_id"} and event["data"]["webhook_id"] == own_id:
            # A test of this very webhook carries nothing but its own id.
            return True
        if "board" not in event["data"]:
            return principal.policy.sees_all_wings
        board = event["data"]["board"]
        if not isinstance(board, str) or not board or gateway is None:
            return False
        from .boards.service import open_store

        store = open_store(
            gateway.workspace_dir(row["workspace_id"]),
            db=gateway.db,
            workspace_id=row["workspace_id"],
        )
        with store.connection() as conn:
            found = conn.execute(
                "SELECT wing,coordinator FROM boards WHERE slug=? AND archived=0", (board,)
            ).fetchone()
        if event["kind"] == "space_message" and (
            found is None or found["coordinator"] != principal.actor
        ):
            return False
        return found is not None and principal.policy.can_read_wing(found["wing"])
    except Exception as exc:
        log_failure(logger, "webhook owner authorization failed", exc)
        return False


def has_space_subscription(db, gateway, workspace_id, actor, board):
    """Whether a coordinator can currently receive this board's request wakes."""
    ensure_schema(db)
    event = {
        "workspace_id": workspace_id,
        "kind": "space_message",
        "data": {"board": board, "coordinator": actor},
    }
    with db._lock:
        rows = db._conn.execute(
            "SELECT * FROM webhooks WHERE workspace_id=? AND owner_actor=? AND active=1",
            (workspace_id, actor),
        ).fetchall()
    return any(_matches(row, event) and _can_receive(db, gateway, row, event) for row in rows)


def test_webhook(db, principal, webhook_id):
    _authorize(principal, "test")
    row = _get(db, principal, webhook_id)
    if not _can_receive(
        db, None, row, {"workspace_id": principal.workspace_id, "data": {"webhook_id": row["id"]}}
    ):
        raise ToolError("no: webhook access refused")
    # A test is deliberately delivered only to the selected registration.
    event = events.emit(
        db,
        principal.workspace_id,
        "webhook.test",
        subject=webhook_id,
        actor=principal.actor,
        data={"webhook_id": webhook_id},
    )
    _enqueue(db, row, event)
    with db._lock:
        delivery = db._conn.execute(
            "SELECT id FROM webhook_deliveries WHERE webhook_id = ? AND event_seq = ?",
            (webhook_id, event["seq"]),
        ).fetchone()
    return {"id": delivery["id"], "status": "pending"}


def _extra_header(auth_header) -> dict:
    if not auth_header:
        return {}
    try:
        parsed = json.loads(auth_header)
        if "env" in parsed:
            return _header_from_value(os.environ.get(parsed["env"], ""))
        return {parsed["name"]: parsed["value"]}
    except (ValueError, KeyError, TypeError):
        return {}


def _header_from_value(raw: str) -> dict:
    """``"Bearer xyz"`` or ``"X-Name: value"`` from an env value."""
    raw = raw.strip()
    if not raw or any(c in raw for c in "\r\n\x00"):
        return {}
    name, sep, value = raw.partition(":")
    if sep and re.fullmatch(r"Authorization|X-[A-Za-z0-9-]{1,40}", name.strip()):
        return {name.strip(): value.strip()}
    return {"Authorization": raw}


def _post(url, body, secret, delivery_id, kind, auth_header=None):
    validate_url(url)
    parts = urlsplit(url)
    timestamp = str(int(time.time()))
    signature = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256)
    cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    conn = cls(parts.hostname, parts.port, timeout=HTTP_TIMEOUT)
    try:
        # http.client does not follow redirects or use environment HTTP proxies.
        conn.request(
            "POST",
            (parts.path or "/") + ("?" + parts.query if parts.query else ""),
            body=body,
            headers={
                "Content-Type": "application/json",
                "X-Commonplace-Event": kind,
                "X-Commonplace-Delivery": delivery_id,
                "X-Commonplace-Signature": f"t={timestamp},v1={signature.hexdigest()}",
                **_extra_header(auth_header),
            },
        )
        response = conn.getresponse()
        return response.status
    finally:
        conn.close()


class Dispatcher:
    def __init__(self, db, *, gateway=None, schedule=BACKOFF, max_attempts=MAX_ATTEMPTS):
        if not schedule or any(delay < 0 for delay in schedule) or max_attempts < 1:
            raise ValueError("invalid webhook retry schedule")
        self.db = db
        self.gateway = gateway
        self.schedule = tuple(schedule)
        self.max_attempts = max_attempts
        self.error_backoff = ERROR_BACKOFF
        self.max_error_backoff = MAX_ERROR_BACKOFF
        self._next_report_scan = 0.0
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.thread = threading.Thread(target=self._run, name="commonplace-webhooks", daemon=True)

    def start(self):
        ensure_schema(self.db)
        events.add_listener(self.on_event)
        try:
            self.reconcile()
            self.thread.start()
        except Exception:
            events.remove_listener(self.on_event)
            raise
        return self

    def stop(self):
        events.remove_listener(self.on_event)
        self._stop.set()
        self._wake.set()
        self.thread.join(timeout=HTTP_TIMEOUT + 2)

    def on_event(self, event):
        with self.db._lock:
            # Listeners are process-global; accept only events from our control DB.
            stored = self.db._conn.execute(
                "SELECT * FROM events WHERE seq = ?", (event["seq"],)
            ).fetchone()
            if stored is None or events._row(stored) != event:
                return
            # Drain in sequence even when live callbacks arrive out of order.
            self.reconcile()
        self._wake.set()

    def reconcile(self):
        from commonplace.projects import report_dispatch

        with self.db._lock:
            cursor = int(
                self.db._conn.execute(
                    "SELECT value FROM meta WHERE key='webhook_dispatch_seq'"
                ).fetchone()[0]
            )
            while True:
                batch = self.db._conn.execute(
                    "SELECT * FROM events WHERE seq > ? ORDER BY seq LIMIT 200", (cursor,)
                ).fetchall()
                if not batch:
                    break
                for stored in batch:
                    event = events._row(stored)
                    if event["kind"] == "project.report":
                        with self.db._tx():
                            report_dispatch.defer_in(self.db, event)
                            self.db._conn.execute(
                                "UPDATE meta SET value=? WHERE key='webhook_dispatch_seq'",
                                (str(event["seq"]),),
                            )
                        cursor = event["seq"]
                        continue
                    rows = self.db._conn.execute(
                        "SELECT * FROM webhooks WHERE workspace_id=? AND active=1 AND start_seq < ?",
                        (event["workspace_id"], event["seq"]),
                    ).fetchall()
                    for row in rows:
                        matches = (
                            row["id"] == event["data"].get("webhook_id")
                            if event["kind"] == "webhook.test"
                            else _matches(row, event)
                        )
                        if matches and _can_receive(self.db, self.gateway, row, event):
                            _enqueue(self.db, row, event)
                    # Enqueues autocommit first. Replaying a crash before this
                    # write is harmless: (webhook_id,event_seq) is unique.
                    cursor = event["seq"]
                    self.db._conn.execute(
                        "UPDATE meta SET value=? WHERE key='webhook_dispatch_seq'", (str(cursor),)
                    )

        report_dispatch.process(self.db)

    def _due(self):
        with self.db._lock:
            return self.db._conn.execute(
                "SELECT d.*, w.url, w.secret, w.workspace_id FROM webhook_deliveries d "
                "JOIN webhooks w ON w.id = d.webhook_id WHERE w.active = 1 "
                "AND d.status IN ('pending','retry') AND d.next_at <= ? "
                "ORDER BY d.next_at, d.rowid LIMIT 1",
                (time.time(),),
            ).fetchone()

    def _deliver(self, row):
        if json.loads(bytes(row["payload"])).get("kind") == "project.report":
            from commonplace.projects import report_dispatch

            row = report_dispatch.prepare(self, row)
            if row is not None:
                self._send(row)
            return
        with self.db._lock:
            current = self.db._conn.execute(
                "SELECT d.*, w.url, w.secret, w.workspace_id, w.connection_id, w.owner_actor, "
                "w.active, w.auth_header FROM webhook_deliveries d "
                "JOIN webhooks w ON w.id=d.webhook_id "
                "WHERE d.id=?",
                (row["id"],),
            ).fetchone()
            if current is None or current["status"] not in ("pending", "retry"):
                return
            payload = json.loads(bytes(current["payload"]))
            event = {
                "workspace_id": current["workspace_id"],
                "kind": payload["kind"],
                "data": payload["data"],
            }
            if not _can_receive(self.db, self.gateway, current, event):
                self.db._conn.execute(
                    "UPDATE webhook_deliveries SET status='cancelled' WHERE id=?", (current["id"],)
                )
                return
            if current["attempt"] != row["attempt"]:
                return
            row = current
        self._send(row)

    def _send(self, row):
        body = bytes(row["payload"])
        kind = json.loads(body)["kind"]
        status, error = None, ""
        attempt = row["attempt"] + 1
        try:
            status = _post(row["url"], body, row["secret"], row["id"], kind, row["auth_header"])
            if not 200 <= status < 300:
                error = f"HTTP {status}"
        except Exception:
            # Never retain response bodies, URL credentials, or arbitrary exception text.
            error = "delivery connection failed"
        delivered = status is not None and 200 <= status < 300
        dead = not delivered and attempt >= self.max_attempts
        state = "delivered" if delivered else "dead" if dead else "retry"
        delay = self.schedule[min(attempt - 1, len(self.schedule) - 1)]
        with self.db._lock, self.db._tx():
            self.db._conn.execute(
                "INSERT INTO webhook_attempts(delivery_id, attempt, at, http_status, error) "
                "VALUES (?,?,?,?,?)",
                (row["id"], attempt, _now(), status, error),
            )
            self.db._conn.execute(
                "UPDATE webhook_deliveries SET attempt = ?, status = ?, http_status = ?, "
                "error = ?, next_at = ?, delivered_at = ? WHERE id = ? "
                "AND status IN ('pending','retry')",
                (
                    attempt,
                    state,
                    status,
                    error,
                    time.time() + delay,
                    _now() if delivered else None,
                    row["id"],
                ),
            )
        # Failure notifications are deliverable, but cannot spawn recursive failures.
        if dead and kind != "webhook.failed":
            events.emit(
                self.db,
                row["workspace_id"],
                "webhook.failed",
                subject=row["webhook_id"],
                actor="system",
                data={"webhook_id": row["webhook_id"], "delivery_id": row["id"]},
            )

    def scan_reports(self):
        pump = getattr(self.gateway, "project_reports", None)
        now = time.monotonic()
        if pump is not None and now >= self._next_report_scan:
            self._next_report_scan = now + 30
            pump.pass_once()

    def _run(self):
        backoff = self.error_backoff
        try:
            while not self._stop.is_set():
                try:
                    self._wake.clear()
                    self.scan_reports()
                    self.reconcile()
                    row = self._due()
                    if row is not None:
                        self._deliver(row)
                        backoff = self.error_backoff
                        continue
                except sqlite3.ProgrammingError:
                    # A test or embedding application may close its ControlDB first.
                    return
                except Exception as exc:
                    # Contain the pass (e.g. a control DB lock held past the busy
                    # timeout); deliveries stay queued and resume after a bounded backoff.
                    log_failure(logger, "webhook dispatcher pass failed", exc)
                    if self._stop.wait(backoff):
                        return
                    backoff = min(backoff * 2, self.max_error_backoff)
                    continue
                backoff = self.error_backoff
                self._wake.wait(0.1)
        finally:
            events.remove_listener(self.on_event)


def stop(gateway):
    dispatcher = getattr(gateway, "webhooks", None)
    if dispatcher:
        dispatcher.stop()


def _tool_add(ctx, args):
    result = add(
        ctx.db,
        ctx.principal,
        args.get("url"),
        kinds=args.get("events"),
        board=args.get("board", ""),
        waiting_on=args.get("waiting_on", ""),
        auth=args.get("auth"),
    )
    return f"{result['id']} secret {result['secret']}"


def _tool_list(ctx, args):
    return list_webhooks(ctx.db, ctx.principal)


def _tool_remove(ctx, args):
    return remove(ctx.db, ctx.principal, args.get("id"))


def _tool_test(ctx, args):
    return test_webhook(ctx.db, ctx.principal, args.get("id"))


for _name, _handler, _properties, _required in (
    (
        "add",
        _tool_add,
        {
            "url": {"type": "string"},
            "events": {
                "oneOf": [{"type": "array", "items": {"type": "string"}}, {"type": "string"}]
            },
            "board": {"type": "string"},
            "waiting_on": {"type": "string"},
            "auth": {"type": "string"},
        },
        ["url"],
    ),
    ("list", _tool_list, {}, []),
    ("remove", _tool_remove, {"id": {"type": "string"}}, ["id"]),
    ("test", _tool_test, {"id": {"type": "string"}}, ["id"]),
):
    register(
        LocalTool(
            name="webhook_" + _name,
            description=_name.capitalize() + " webhooks",
            input_schema={
                "type": "object",
                "properties": _properties,
                "required": _required,
                "additionalProperties": False,
            },
            handler=_handler,
            scope="memory:read",
            kind="read" if _name == "list" else "write",
            profiles=WORK_PROFILES,
            trusted_only=True,
        )
    )


def _send_json(h, status, data):
    raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
    h.send_response(status)
    h.send_header("Content-Type", "application/json")
    h.send_header("Content-Length", str(len(raw)))
    h.send_header("Cache-Control", "no-store")
    h.end_headers()
    h.wfile.write(raw)


def _read_json(h):
    try:
        length = int(h.headers.get("Content-Length", "0"))
        if not 0 < length <= 1024 * 1024:
            raise ValueError()
        value = json.loads(h.rfile.read(length))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, UnicodeError):
        raise ToolError("no: invalid JSON body") from None


def _rest(gateway, h):
    principal = gateway.principal_for(h)
    if principal is None:
        _send_json(h, 401, {"error": "unauthorized"})
        return
    parts = urlsplit(h.path).path.removeprefix("/api/webhooks").strip("/").split("/")
    if parts == [""]:
        parts = []
    method = h.command
    action = "list" if method == "GET" else "remove" if method == "DELETE" else "add"
    if len(parts) == 2 and parts[1] == "test":
        action = "test"
    try:
        _authorize(principal, action)
        if not parts and method == "GET":
            _send_json(h, 200, {"webhooks": list_webhooks(gateway.db, principal)})
        elif not parts and method == "POST":
            args = _read_json(h)
            result = add(
                gateway.db,
                principal,
                args.get("url"),
                kinds=args.get("events"),
                board=args.get("board", ""),
                waiting_on=args.get("waiting_on", ""),
                auth=args.get("auth"),
            )
            _send_json(h, 201, {"id": result["id"], "secret": result["secret"]})
        elif len(parts) == 1 and method == "DELETE":
            remove(gateway.db, principal, parts[0])
            _send_json(h, 200, {"ok": True})
        elif len(parts) == 2 and parts[1] == "deliveries" and method == "GET":
            _send_json(h, 200, {"deliveries": deliveries(gateway.db, principal, parts[0])})
        elif len(parts) == 2 and parts[1] == "test" and method == "POST":
            delivery = test_webhook(gateway.db, principal, parts[0])
            _send_json(h, 202, {"ok": True, "delivery": delivery})
        else:
            _send_json(h, 404, {"error": "not found"})
    except ToolError as exc:
        message = str(exc)
        status = (
            404
            if message == "no: webhook not found"
            else (403 if message == "no: webhook access refused" else 400)
        )
        _send_json(h, status, {"error": message})


def install(gateway):
    if getattr(gateway, "webhooks", None) is not None:
        return
    gateway.webhooks = Dispatcher(gateway.db, gateway=gateway).start()
    for method in ("GET", "POST", "DELETE"):
        gateway.add_prefix_route(method, "/api/webhooks/", lambda h: _rest(gateway, h))


def cli_command(args):
    """CLI uses the local administrator's person policy, like other control commands."""
    from .cli import _actor, _db
    from .control_db import ControlError
    from .sessions import person_principal

    db = _db(args)
    try:
        principal = person_principal(db, _actor(db, args), db.workspace(args.workspace)["id"])
        if principal is None or principal.role != "owner":
            raise ControlError("workspace owner required")
        if args.action == "add":
            result = add(
                db,
                principal,
                args.url,
                kinds=args.events,
                board=args.board,
                waiting_on=args.waiting_on,
            )
        elif args.action == "list":
            result = list_webhooks(db, principal)
        else:
            result = remove(db, principal, args.id)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except ToolError as exc:
        raise ControlError(str(exc)) from None
    finally:
        db.close()
