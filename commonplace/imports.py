"""Authenticated, bounded one-time import. No capture, models or external calls."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import re
import threading
from collections import Counter
from urllib.parse import parse_qs, urlsplit

from . import continuity_schema, import_grant, ingest_cadence, plans, sync_protocol
from .capture import PRIVATE_WING
from .handoff.api import _object, _query, _read_json, _RequestError, _text
from .handoff.models import THREADS_WING
from .handoff.recall import readable
from .handoff.thread_store import ThreadConflict, ThreadStorageLimit, canonical_sha256
from .http_util import log_failure
from .import_parts import append_part, native_records, validate_part
from .import_sources import SOURCES, parse_import
from .workspace_store import thread_path, workspace_path

MAX_IMPORT_BYTES = 64 * 1024 * 1024
MAX_THREADS = 50


def _acl_import_allowed(policy):
    return (
        policy.has_scope("memory:read")
        and policy.has_scope("memory:write")
        and policy.has_scope("threads:read")
        and policy.can_write_wing(THREADS_WING)
        and policy.can_read_wing(THREADS_WING)
        and policy.trust != "untrusted"
    )


def _import_envelope(ctx, principal, handler):
    policy = principal.policy
    acl_allowed = _acl_import_allowed(policy)
    acl_error = _RequestError(
        403, "Import requires memory:read, memory:write and threads:read for threads"
    )
    path = "acl"
    if policy.has_scope(import_grant.IMPORT_SCOPE):
        if not import_grant.grant_path(ctx.control_db, principal):
            raise _RequestError(403, import_grant.INELIGIBLE_TEXT)
        path = "grant"
    if path == "acl" and not acl_allowed and not principal.person:
        raise acl_error
    data = _read_json(handler, MAX_IMPORT_BYTES)
    if isinstance(data, dict) and "private" in data:
        if not principal.person or data["private"] is not True:
            raise _RequestError(400, "private requires a person and true")
        if not import_grant.eligible(ctx.control_db, principal.user_id, principal.workspace_id):
            raise _RequestError(403, import_grant.OWNER_ONLY_TEXT)
        path = "grant"
    if path == "acl" and not acl_allowed:
        raise acl_error
    data = _object(data, {"source", "threads", "private"})
    return data, path


def register(ctx):
    ingest_cadence.ensure_schema(ctx.control_db)
    continuity_schema.ensure_schema(ctx.control_db)
    # Serialize imports so simultaneous batches cannot spend the same quota.
    lock = threading.Lock()

    def import_batch(principal, source, threads, *, path):
        with lock:
            result = run_batch(ctx, principal, source, threads, path=path)
        try:
            from .continuity_read import content_threads

            ws = principal.workspace_id
            touched = []
            for row in result["results"]:
                if row["status"] not in ("accepted", "imported", "duplicate"):
                    continue
                identity = (
                    import_grant.grant_id(principal.user_id, source, row["thread_id"])
                    if path == "grant"
                    else f"import:{source}:{row['thread_id']}"
                )
                stored = ctx.store_for(ws).by_source(ws, identity)
                if stored:
                    touched.append(stored.id)
                touched.extend(content_threads(ctx.control_db, ws, identity))
            if touched:
                ctx.service.index_later(ws, list(dict.fromkeys(touched)))
        except Exception as error:
            log_failure(logging.getLogger(__name__), "Import index scheduling failed", error)
        return result

    def admit(principal, digest) -> int:
        return grant_admit(ctx, principal, digest)

    ctx.import_batch = import_batch
    ctx.grant_admit = admit

    def handle(handler):
        handler._handoff_body_read = False
        try:
            principal = ctx.gateway.principal_for(handler)
            if principal is None:
                raise _RequestError(401, "Sign in")
            data, path = _import_envelope(ctx, principal, handler)
            source = _text(data, "source", required=True)
            if source not in (*SOURCES, sync_protocol.MEMORY_IMPORT_SOURCE) or (
                source == sync_protocol.MEMORY_IMPORT_SOURCE and path != "grant"
            ):
                raise _RequestError(400, "Unsupported source")
            threads = data.get("threads")
            if not isinstance(threads, list) or not 1 <= len(threads) <= MAX_THREADS:
                raise _RequestError(400, f"Send between 1 and {MAX_THREADS} threads")
            if path == "grant":
                digest = import_grant.request_digest("http:/api/import", data)
                wait = grant_admit(
                    ctx,
                    principal,
                    digest,
                    ride=source
                    in (*sync_protocol.TRANSCRIPT_SOURCES, sync_protocol.MEMORY_IMPORT_SOURCE),
                )
                if wait > 0:
                    raise _RequestError(429, f"Import not due; retry in {wait} seconds")
            result = import_batch(principal, source, threads, path=path)
            status = 200
        except _RequestError as error:
            status, result = error.status, {"error": error.message}
        except Exception as error:
            log_failure(logging.getLogger(__name__), "Import failed", error)
            status, result = 500, {"error": "Import unavailable; retry this batch"}
        # Batches are independently retryable. Close even after errors so an
        # unread or oversized body cannot become another request.
        handler.close_connection = True
        handler._send_json(status, result, {"Connection": "close"})

    def list_imports(handler):
        from .continuity_read import listed_messages

        try:
            principal = ctx.gateway.principal_for(handler)
            if principal is None:
                raise _RequestError(401, "Sign in")
            if not principal.person:
                raise _RequestError(403, "Sign in to list your imports")
            query = parse_qs(urlsplit(handler.path).query, keep_blank_values=True)
            try:
                limit = int(_query(query, "limit", "50"))
                if not 1 <= limit <= 100:
                    raise ValueError()
            except ValueError:
                raise _RequestError(400, "invalid limit") from None
            after = _decode_cursor(_query(query, "cursor")) if "cursor" in query else None
            rows = ctx.store_for(principal.workspace_id).list_source_range(
                principal.workspace_id,
                f"import-private:{principal.user_id}:",
                after=after,
                limit=limit,
            )
            result = {"imports": [], "next": None}
            result["imports"] = [
                _import_row(row, listed_messages(ctx.control_db, row))
                for row in rows
                if readable(principal, row)
            ]
            if len(rows) == limit:
                result["next"] = _encode_cursor(rows[-1])
            status = 200
        except _RequestError as error:
            status, result = error.status, {"error": error.message}
        handler._send_json(status, result)

    ctx.gateway.add_route("GET", "/api/imports", list_imports)
    ctx.gateway.add_route("POST", "/api/import", handle)


def _decode_cursor(cursor):
    """An opaque keyset cursor: base64url JSON [source_thread_id, stored_id]."""
    try:
        decoded = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        after = json.loads(decoded)
        if not (
            isinstance(after, list)
            and len(after) == 2
            and all(isinstance(value, str) for value in after)
        ):
            raise ValueError()
        for value in after:
            value.encode("utf-8")  # A lone surrogate is invalid input, not a 500.
    except (ValueError, binascii.Error, UnicodeError):
        raise _RequestError(400, "invalid cursor") from None
    return after


def _encode_cursor(row):
    raw = json.dumps([row.source_thread_id, row.id]).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _import_row(row, listed=None):
    """Metadata only; the words stay behind the existing thread read route."""
    _, _, source, identity = row.source_thread_id.split(":", 3)
    match = re.search(r"#(sha256:[0-9a-fA-F]{64})$", identity)
    return {
        "stored_thread_id": row.id,
        "source": source,
        "thread_id": identity[: match.start()] if match else identity,
        "conflict_of": match.group(1) if match else None,
        "created_at": row.created_at,
        "stored_messages": listed if listed is not None else row.message_count - 1,
    }


def grant_admit(ctx, principal, digest, *, ride=False) -> int:
    return ingest_cadence.admit(
        ctx.control_db,
        account_id=principal.user_id,
        connection_id=principal.connection_id,
        request_sha256=digest,
        interval=ingest_cadence.interval_for(ctx.control_db, principal.user_id),
        now=plans._now(),
        ride=ride,
    )


def _validate_memory_identity(thread_id, transcript, path):
    try:
        label, rest = thread_id.split(":", 1)
        relpath, _ = rest.rsplit(":", 1)
        expected = sync_protocol.memory_thread_id(
            label, relpath, hashlib.sha256(transcript.encode("utf-8")).hexdigest()
        )
        if path != "grant" or thread_id != expected:
            raise ValueError()
    except ValueError:
        raise _RequestError(400, "Invalid memory identity") from None


def _validate_native_part(source, fmt, transcript, part):
    if (
        source not in sync_protocol.TRANSCRIPT_SOURCES
        or fmt not in ("claude-code", "codex")
        or (source != "t3" and source != fmt)
    ):
        raise ValueError("Parts require an explicit native format")
    validate_part(part, transcript.encode("utf-8"))
    native_records(transcript, fmt, part["final"])


def _store_private_snapshot(
    ctx,
    principal,
    store,
    *,
    source,
    fmt,
    thread_id,
    transcript,
    messages,
    source_id,
    maximum,
    continuity_item,
):
    if continuity_item:
        from . import continuity

        with ctx.control_db._lock, ctx.control_db._tx() as conn:
            with store._transaction():
                grouped = continuity.import_item(
                    conn,
                    store,
                    ws=principal.workspace_id,
                    user_id=principal.user_id,
                    source=source,
                    fmt=fmt,
                    thread_id=thread_id,
                    transcript=transcript,
                    messages=messages,
                    actor=principal.actor,
                    maximum=maximum,
                )
        if grouped is not None:
            return
    with store._transaction():
        # Refuse uniformly, including an already covered request.
        store._check_storage_budget(maximum, messages)
        try:
            store.store(
                principal.workspace_id,
                source_id,
                messages,
                actor=principal.actor,
                max_storage_bytes=maximum,
                wing=PRIVATE_WING,
            )
        except ThreadConflict:
            store.store(
                principal.workspace_id,
                f"{source_id}#sha256:{canonical_sha256(messages)}",
                messages,
                actor=principal.actor,
                max_storage_bytes=maximum,
                wing=PRIVATE_WING,
            )


def run_batch(ctx, principal, source, threads, *, path="acl"):
    if path not in ("acl", "grant"):
        raise ValueError("Unknown import path")
    directory = workspace_path(ctx.data_dir, principal.workspace_id)
    account = plans.workspace_account(ctx.control_db, principal.workspace_id)
    plan = plans.get_plan(ctx.control_db, account) if plans.enabled() and account else None
    if path == "grant" and plans.enabled():
        plan = plan or plans.FREE
    limit = plan.storage_bytes if plan else None
    results = []
    for index, item in enumerate(threads):
        row = {"index": index, "status": "error"}
        try:
            _object(item, {"thread_id", "transcript", "format", "part"})
            thread_id = _text(item, "thread_id", required=True)
            if len(thread_id) > 512:
                raise _RequestError(400, "thread_id must be at most 512 characters")
            row["thread_id"] = thread_id
            multipart = "part" in item
            transcript = _text(item, "transcript", required=not multipart)
            fmt = _text(item, "format", "auto")
            if source == sync_protocol.MEMORY_IMPORT_SOURCE:
                _validate_memory_identity(thread_id, transcript, path)
            messages = None if multipart else parse_import(source, transcript, fmt)
            if multipart:
                # Preserve the strict multipart policy before any continuity write.
                _validate_native_part(source, fmt, transcript, item["part"])
            # A fresh store has schema/WAL overhead. Refuse before creating it
            # when the account cannot fit even that, using an uncached meter.
            used = plans.StorageMeter._walk(directory)
            if limit is not None and not ctx.has_threads(principal.workspace_id):
                if used + 1024 * 1024 > limit:
                    raise ThreadStorageLimit()
            store = ctx.store_for(principal.workspace_id)
            native_source = sync_protocol.native_source(source, fmt, transcript)
            continuity_item = (
                path == "grant"
                and native_source is not None
                and (
                    multipart
                    or not sync_protocol.live_tail(transcript)
                    and sync_protocol.native_identity(transcript.encode("utf-8"), native_source)
                    is not None
                )
            )
            if continuity_item:
                from . import continuity

                continuity.backfill(
                    ctx.control_db, store, principal.workspace_id, principal.user_id
                )
            source_id = (
                import_grant.grant_id(principal.user_id, source, thread_id)
                if path == "grant"
                else f"import:{source}:{thread_id}"
            )
            existing = store.by_source(principal.workspace_id, source_id) if path == "acl" else None
            maximum = None
            if limit is not None:
                thread_file = thread_path(directory)
                own_bytes = sum(
                    candidate.stat().st_size
                    for candidate in (
                        thread_file,
                        thread_file.with_name(thread_file.name + "-wal"),
                        thread_file.with_name(thread_file.name + "-shm"),
                    )
                    if candidate.exists()
                )
                maximum = limit - (plans.StorageMeter._walk(directory) - own_bytes)
            if multipart:
                # Compute allowance inside the writer transaction, including
                # the post-write quota check. Exact retries bypass writes.
                def allowance():
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

                placement = (
                    {"source_id": source_id, "wing": PRIVATE_WING} if path == "grant" else {}
                )
                if continuity_item:
                    with ctx.control_db._lock, ctx.control_db._tx() as conn:
                        with store._transaction():
                            part_result = continuity.stream_push(
                                conn,
                                store,
                                ws=principal.workspace_id,
                                user_id=principal.user_id,
                                connection_id=principal.connection_id,
                                kind="import",
                                stream_id=source_id,
                                source=source,
                                native_source=native_source,
                                data=transcript.encode("utf-8"),
                                offset=item["part"]["offset"],
                                prefix=item["part"]["prefix_sha256"],
                                actor=principal.actor,
                                maximum=allowance,
                                gate=lambda: None,
                                part=item["part"],
                            )
                else:
                    part_result = append_part(
                        store,
                        principal.workspace_id,
                        source,
                        thread_id,
                        fmt,
                        transcript,
                        item["part"],
                        actor=principal.actor,
                        maximum=allowance,
                        **placement,
                    )
                if path == "grant":
                    row.update(
                        {
                            key: part_result[key]
                            for key in (
                                "unit",
                                "format",
                                "part",
                                "source_bytes",
                                "messages",
                                "offset",
                                "prefix_sha256",
                                "next_index",
                                "session_state",
                            )
                        }
                    )
                    row["status"] = "accepted"
                else:
                    row.update(part_result)
                results.append(row)
                continue
            if path == "grant":
                _store_private_snapshot(
                    ctx,
                    principal,
                    store,
                    source=source,
                    fmt=fmt,
                    thread_id=thread_id,
                    transcript=transcript,
                    messages=messages,
                    source_id=source_id,
                    maximum=maximum,
                    continuity_item=continuity_item,
                )
                row.update(
                    status="accepted",
                    messages=len(messages) - 1,
                    source_bytes=len(transcript.encode("utf-8")),
                )
            else:
                stored = store.store(
                    principal.workspace_id,
                    source_id,
                    messages,
                    actor=principal.actor,
                    max_storage_bytes=maximum,
                )
                row.update(
                    status="duplicate" if existing else "imported",
                    stored_thread_id=stored.id,
                    messages=stored.message_count - 1,
                    source_bytes=len(transcript.encode("utf-8")),
                )
            if source == "grok-bot" and fmt in ("auto", "grok-facts"):
                row["facts"] = len(messages) - 1
                row["source_counts"] = dict(
                    Counter(json.loads(m.raw)["source"] for m in messages[1:])
                )
        except ThreadStorageLimit:
            row.update(error=plans.storage_message(plan), code="storage_limit")
            ctx.gateway.storage_meter.audit_limit(ctx.control_db, principal)
        except ThreadConflict:
            row.update(
                error=(
                    "Part does not continue this import; resend from the start or the last accepted part"
                    if path == "grant" and multipart
                    else "Thread already imported with different content; original kept"
                ),
                code="conflict",
            )
        except _RequestError as error:
            row.update(error=error.message, code="invalid_thread")
        except (ValueError, TypeError, OverflowError, RecursionError):
            # Never echo parser exceptions: they may contain private content.
            row.update(
                error="Invalid or unsupported transcript; check the source and format",
                code="invalid_transcript",
            )
        except Exception as error:
            log_failure(logging.getLogger(__name__), "Thread import failed", error)
            row.update(
                error="Thread import unavailable; retry this thread", code="import_unavailable"
            )
        results.append(row)
    errors = sum(row["status"] == "error" for row in results)
    if path == "grant":
        return {
            "source": source,
            "status": "completed_with_errors" if errors else "completed",
            "processed": len(results),
            "total": len(threads),
            "accepted": sum(row["status"] == "accepted" for row in results),
            "errors": errors,
            "facts": sum(row.get("facts", 0) for row in results),
            "results": results,
        }
    return {
        "source": source,
        "status": "completed_with_errors" if errors else "completed",
        "processed": len(results),
        "total": len(threads),
        "imported": sum(row["status"] == "imported" for row in results),
        "duplicates": sum(row["status"] == "duplicate" for row in results),
        "errors": errors,
        "facts": sum(row.get("facts", 0) for row in results),
        "storage_bytes": plans.StorageMeter._walk(directory),
        "storage_limit_bytes": limit,
        "results": results,
    }
