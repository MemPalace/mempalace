"""Explicit workspace opt-in for private, append-only native chat capture."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import replace
from datetime import datetime, timedelta
from io import StringIO

from . import continuity_schema, import_grant, ingest_cadence, plans, sync_protocol
from .handoff.api import _integer, _object, _read_json, _RequestError, _text
from .handoff.models import ThreadMessage
from .handoff.thread_store import ThreadConflict, ThreadStorageLimit
from .handoff.transcripts import parse
from .http_util import log_failure, query
from .workspace_store import thread_path, workspace_path

SOURCES = ("claude-code", "codex")
PRIVATE_WING = "private_chats"
RAW_NAME = "capture.raw"
MAX_CAPTURE_BYTES = 64 * 1024 * 1024
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


def _identity(principal, data):
    source = _text(data, "source", required=True)
    identity = _text(data, "thread_id", required=True)
    if source not in SOURCES or len(identity) > 512:
        raise _RequestError(400, "Invalid source or thread_id")
    # Opt-in grants only a write-only capture channel for the filing actor.
    # It never widens the connection's memory/read/private policy.
    if not principal.policy.has_scope("memory:write") or (
        not principal.person and principal.client != source
    ):
        raise _RequestError(403, "Capture requires a matching client and memory:write")
    actor_id = principal.user_id if principal.person else principal.connection_id
    return source, f"capture:{actor_id}:{source}:{identity}"


def _archives(store, stored):
    if stored:
        for start in range(0, stored.message_count, 100):
            for message in store.messages(stored.id, start, start + 99):
                if message.kind == "other" and message.name == RAW_NAME:
                    yield message


def cursor(store, stored):
    digest = hashlib.sha256()
    offset = 0
    last_sync = ""
    for message in _archives(store, stored):
        raw = message.raw.encode("utf-8")
        digest.update(raw)
        offset += len(raw)
        last_sync = message.at
    return {
        "offset": offset,
        "prefix_sha256": digest.hexdigest(),
        "last_sync": last_sync,
        "stored_thread_id": stored.id if stored else None,
    }


def append_capture(
    store, workspace, identity, source, text, offset, prefix, *, actor, maximum, interval, gate=None
):
    """Cursor validation, raw archive, readable suffix and quota commit together.

    Archive segments retain exact UTF-8 JSONL including whitespace and endings.
    Reconstructing their digest also recovers a cursor after client state loss.
    """
    raw = text.encode("utf-8")
    if not raw or not raw.endswith(b"\n"):
        raise _RequestError(400, "Send complete newline-terminated JSONL records")
    # Unlike snapshot import, a partial JSON object can never become a saved
    # prefix. The local runner retains unflushed tails for a later invocation.
    for line in StringIO(text, newline=""):
        if line.strip():
            json.loads(line)
    messages = parse(text, source, require_recognized=offset == 0) if text.strip() else []
    with store._transaction():
        stored = store.by_source(workspace, identity)
        current = cursor(store, stored)
        if offset < current["offset"]:
            # A lost response may retry a chunk after subsequent chunks landed.
            digest = hashlib.sha256()
            position = 0
            repeated = bytearray()
            for archive in _archives(store, stored):
                segment = archive.raw.encode("utf-8")
                digest.update(segment[: max(0, offset - position)])
                lower, upper = max(0, offset - position), offset + len(raw) - position
                if upper > lower:
                    repeated.extend(segment[lower:upper])
                position += len(segment)
            if digest.hexdigest() != prefix or repeated != raw:
                raise ThreadConflict()
            return dict(current, status="duplicate")
        if offset != current["offset"] or prefix != current["prefix_sha256"]:
            raise ThreadConflict()
        now = plans._now()
        if current["last_sync"] and interval:
            remaining = (
                interval - (now - datetime.fromisoformat(current["last_sync"])).total_seconds()
            )
            if remaining > 0:
                raise _RequestError(429, f"Capture not due; retry in {int(remaining) + 1} seconds")
        if gate is not None:
            gate()
        previous = store.messages(stored.id) if stored else []
        start = len(previous)
        archive = ThreadMessage(
            start, "system", "other", "", text, name=RAW_NAME, at=now.isoformat()
        )
        combined = (
            previous + [archive] + [replace(m, seq=start + i + 1) for i, m in enumerate(messages)]
        )
        if stored:
            stored = store.append(stored.id, combined, max_storage_bytes=maximum)
        else:
            stored = store.store(
                workspace,
                identity,
                combined,
                actor=actor,
                max_storage_bytes=maximum,
                wing=PRIVATE_WING,
            )
        return dict(cursor(store, stored), status="appended", added_messages=len(messages))


def _push(db, conn, store, principal, data, identity, source, maximum, continuity):
    actor_id = principal.user_id if principal.person else principal.connection_id
    op = data.get("client_op_id")
    if "client_op_id" in data and (
        not isinstance(op, str) or re.fullmatch(sync_protocol.OP_ID_PATTERN, op) is None
    ):
        raise _RequestError(400, "Invalid client_op_id")
    now = plans._now()
    conn.execute(
        "DELETE FROM capture_ops WHERE at < ?",
        ((now - timedelta(seconds=ingest_cadence.RETRY_WINDOW_SECONDS)).isoformat(),),
    )
    digest = import_grant.request_digest("http:/api/capture", data)
    if op is not None:
        row = conn.execute(
            "SELECT request_sha256, response FROM capture_ops "
            "WHERE workspace_id=? AND actor_id=? AND client_op_id=?",
            (principal.workspace_id, actor_id, op),
        ).fetchone()
        if row:
            if row["request_sha256"] != digest:
                return {
                    "error": "client_op_id was used for a different request",
                    "code": "op_conflict",
                }
            return json.loads(row["response"])

    def gate():
        wait = ingest_cadence.admit(
            db,
            account_id=ingest_cadence.account_key(db, principal),
            connection_id=actor_id,
            request_sha256=digest,
            interval=ingest_cadence.account_interval(db, principal),
            now=plans._now(),
            window=sync_protocol.PUSH_WINDOW_SECONDS,
            conn=conn,
        )
        if wait:
            raise _RequestError(429, f"Account capture not due; retry in {wait} seconds")

    result = continuity.stream_push(
        conn,
        store,
        ws=principal.workspace_id,
        user_id=principal.user_id,
        connection_id=principal.connection_id,
        kind="capture",
        stream_id=identity,
        source=source,
        native_source=source,
        data=_text(data, "transcript", required=True).encode("utf-8"),
        offset=_integer(data.get("offset"), "offset", maximum=2**63 - 1),
        prefix=_text(data, "prefix_sha256", required=True),
        actor=principal.actor,
        maximum=maximum,
        gate=gate,
    )
    if op is not None:
        conn.execute(
            "INSERT INTO capture_ops VALUES (?,?,?,?,?,?)",
            (principal.workspace_id, actor_id, op, digest, json.dumps(result), now.isoformat()),
        )
    return result


def _backfill(ctx, principal, data, continuity):
    db = ctx.control_db
    try:
        _object(
            data,
            {
                "source",
                "thread_id",
                "offset",
                "prefix_sha256",
                "transcript",
                "client_op_id",
            },
        )
        source, _ = _identity(principal, data)
        raw = _text(data, "transcript", required=True).encode("utf-8")
        groupable = sync_protocol.native_identity(raw, source) is not None
    except (_RequestError, ValueError):
        groupable = False
    if groupable:
        if ctx.has_threads(principal.workspace_id):
            continuity.backfill(
                db,
                ctx.store_for(principal.workspace_id),
                principal.workspace_id,
                principal.user_id,
            )
        else:
            with db._lock, db._tx() as conn:
                conn.execute(
                    "INSERT INTO sync_backfill(workspace_id,user_id,done) VALUES (?,?,1) "
                    "ON CONFLICT(workspace_id,user_id) DO UPDATE SET done=1",
                    (principal.workspace_id, principal.user_id),
                )


def _update_settings(db, conn, principal, data, sync_feed):
    _object(data, {"enabled", "sync_now"})
    if not data:
        raise _RequestError(400, "Provide enabled or sync_now")
    if "enabled" in data and type(data["enabled"]) is not bool:
        raise _RequestError(400, "enabled must be a boolean")
    if "sync_now" in data and data["sync_now"] is not True:
        raise _RequestError(400, "sync_now must be true")
    if "enabled" in data:
        conn.execute(
            "INSERT INTO capture_settings VALUES (?,?) ON CONFLICT(workspace_id) "
            "DO UPDATE SET enabled=excluded.enabled",
            (principal.workspace_id, int(data["enabled"])),
        )
        db.audit(
            action="capture.setting",
            decision="ok",
            workspace_id=principal.workspace_id,
            user_id=principal.user_id,
            reason="enabled" if data["enabled"] else "disabled",
        )
        sync_feed.record(
            conn,
            principal.workspace_id,
            "sync.capture",
            {"enabled": data["enabled"]},
        )
    if "sync_now" in data:
        sync_feed.record(conn, principal.workspace_id, "sync.request", {})


def register(ctx):
    from . import continuity, sync_feed

    db = ctx.control_db
    ingest_cadence.ensure_schema(db)
    continuity_schema.ensure_schema(db)
    with db._lock:
        db._conn.execute(
            "CREATE TABLE IF NOT EXISTS capture_settings ("
            "workspace_id TEXT PRIMARY KEY REFERENCES workspaces(id), enabled INTEGER NOT NULL)"
        )

    def settings(principal):
        row = db._conn.execute(
            "SELECT enabled FROM capture_settings WHERE workspace_id=?", (principal.workspace_id,)
        ).fetchone()
        return {
            "enabled": bool(row and row[0]),
            "interval_seconds": ingest_cadence.account_interval(db, principal),
            "private": True,
        }

    def route(handler):
        handler._handoff_body_read = False
        changed = False
        try:
            principal = ctx.gateway.principal_for(handler)
            if principal is None:
                raise _RequestError(401, "Sign in")
            is_settings = handler.path.split("?", 1)[0] == "/api/settings/capture"
            if (
                is_settings
                and handler.command == "POST"
                and (not principal.person or principal.role != "owner")
            ):
                raise _RequestError(403, "Workspace owner session required")
            # Read uploads before taking any shared database writer lock. A
            # slow client must not hold up other users or workspace opt-out.
            data = (
                query(handler)
                if handler.command == "GET"
                else _read_json(handler, 1024 * 1024 if is_settings else MAX_CAPTURE_BYTES)
            )
            if not is_settings and handler.command == "POST":
                _backfill(ctx, principal, data, continuity)
            # This control transaction serializes capture with opt-out, including
            # across gateway processes. Opt-out returns after earlier writes finish.
            with db._lock, db._tx() as conn:
                if is_settings:
                    if handler.command == "POST":
                        _update_settings(db, conn, principal, data, sync_feed)
                        changed = True
                    result = settings(principal)
                else:
                    setting = settings(principal)
                    if not setting["enabled"]:
                        raise _RequestError(409, "Workspace capture is off")
                    _object(
                        data,
                        {"source", "thread_id"}
                        if handler.command == "GET"
                        else {
                            "source",
                            "thread_id",
                            "offset",
                            "prefix_sha256",
                            "transcript",
                            "client_op_id",
                        },
                    )
                    source, identity = _identity(principal, data)
                    directory = workspace_path(ctx.data_dir, principal.workspace_id)
                    account = plans.workspace_account(db, principal.workspace_id)
                    plan = plans.get_plan(db, account) if plans.enabled() and account else None
                    plan = (plan or plans.FREE) if plans.enabled() else None
                    limit = plan.storage_bytes if plan else None
                    if handler.command == "GET" and not ctx.has_threads(principal.workspace_id):
                        result = dict(
                            cursor(None, None), interval_seconds=setting["interval_seconds"]
                        )
                    else:
                        if limit is not None and not ctx.has_threads(principal.workspace_id):
                            if plans.StorageMeter._walk(directory) + 1024 * 1024 > limit:
                                raise ThreadStorageLimit()
                        store = ctx.store_for(principal.workspace_id)
                        if handler.command == "GET":
                            grouped = conn.execute(
                                "SELECT 1 FROM sync_streams WHERE workspace_id=? AND stream_id=?",
                                (principal.workspace_id, identity),
                            ).fetchone()
                            result = dict(
                                continuity.stream_cursor(
                                    conn,
                                    store,
                                    ws=principal.workspace_id,
                                    stream_id=identity,
                                    own_identity=identity,
                                )
                                if grouped
                                else cursor(
                                    store, store.by_source(principal.workspace_id, identity)
                                ),
                                interval_seconds=setting["interval_seconds"],
                            )
                        else:

                            def maximum():
                                if limit is None:
                                    return None
                                path = thread_path(directory)
                                own = sum(
                                    p.stat().st_size
                                    for p in (
                                        path,
                                        path.with_name(path.name + "-wal"),
                                        path.with_name(path.name + "-shm"),
                                    )
                                    if p.exists()
                                )
                                return limit - (plans.StorageMeter._walk(directory) - own)

                            result = _push(
                                db,
                                conn,
                                store,
                                principal,
                                data,
                                identity,
                                source,
                                maximum,
                                continuity,
                            )
            if (
                not is_settings
                and handler.command == "POST"
                and result.get("status") in ("appended", "duplicate")
            ):
                try:
                    from .continuity_read import content_threads

                    ws = principal.workspace_id
                    touched = content_threads(db, ws, identity)
                    if not touched:
                        stored = store.by_source(ws, identity)
                        touched = [stored.id] if stored else []
                    ctx.service.index_later(ws, touched)
                except Exception as error:
                    log_failure(
                        logging.getLogger(__name__), "Capture index scheduling failed", error
                    )
            if changed:
                sync_feed.notify(principal.workspace_id)
            status = 409 if result.get("code") == "op_conflict" else 200
        except _RequestError as error:
            status, result = error.status, {"error": error.message}
        except ThreadConflict:
            status, result = (
                409,
                {"error": "Saved prefix differs; original kept", "code": "prefix_conflict"},
            )
        except ThreadStorageLimit:
            ctx.gateway.storage_meter.audit_limit(db, principal)
            status, result = 507, {"error": plans.storage_message(plan), "code": "storage_limit"}
        except (ValueError, TypeError, OverflowError, RecursionError):
            status, result = 400, {"error": "Invalid or unsupported native transcript"}
        except Exception as error:
            log_failure(logging.getLogger(__name__), "Capture failed", error)
            status, result = 500, {"error": "Capture unavailable; retry the same chunk"}
        handler.close_connection = True
        handler._send_json(status, result, {"Connection": "close", "Cache-Control": "no-store"})

    for method in ("GET", "POST"):
        ctx.gateway.add_route(method, "/api/settings/capture", route)
        ctx.gateway.add_route(method, "/api/capture", route)
