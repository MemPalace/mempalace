"""Authenticated, bounded one-time import. No capture, models or external calls."""

from __future__ import annotations

import json
import logging
import threading
from collections import Counter

from . import plans
from .handoff.api import _object, _read_json, _RequestError, _text
from .handoff.models import THREADS_WING
from .handoff.thread_store import ThreadConflict, ThreadStorageLimit
from .http_util import log_failure
from .import_sources import SOURCES, parse_import
from .workspace_store import thread_path, workspace_path

MAX_IMPORT_BYTES = 64 * 1024 * 1024
MAX_THREADS = 50


def register(ctx):
    # Serialize imports so simultaneous batches cannot spend the same quota.
    lock = threading.Lock()

    def handle(handler):
        handler._handoff_body_read = False
        try:
            principal = ctx.gateway.principal_for(handler)
            if principal is None:
                raise _RequestError(401, "Sign in")
            policy = principal.policy
            if not (
                policy.has_scope("memory:read")
                and policy.has_scope("memory:write")
                and policy.has_scope("threads:read")
                and policy.can_write_wing(THREADS_WING)
                and policy.can_read_wing(THREADS_WING)
                and policy.trust != "untrusted"
            ):
                raise _RequestError(
                    403, "Import requires memory:read, memory:write and threads:read for threads"
                )
            data = _object(_read_json(handler, MAX_IMPORT_BYTES), {"source", "threads"})
            source = _text(data, "source", required=True)
            if source not in SOURCES:
                raise _RequestError(400, "Unsupported source")
            threads = data.get("threads")
            if not isinstance(threads, list) or not 1 <= len(threads) <= MAX_THREADS:
                raise _RequestError(400, f"Send between 1 and {MAX_THREADS} threads")
            with lock:
                result = _batch(ctx, principal, source, threads)
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

    ctx.gateway.add_route("POST", "/api/import", handle)


def _batch(ctx, principal, source, threads):
    directory = workspace_path(ctx.data_dir, principal.workspace_id)
    account = plans.workspace_account(ctx.control_db, principal.workspace_id)
    plan = plans.get_plan(ctx.control_db, account) if plans.enabled() and account else None
    limit = plan.storage_bytes if plan else None
    results = []
    for index, item in enumerate(threads):
        row = {"index": index, "status": "error"}
        try:
            _object(item, {"thread_id", "transcript", "format"})
            thread_id = _text(item, "thread_id", required=True)
            if len(thread_id) > 512:
                raise _RequestError(400, "thread_id must be at most 512 characters")
            row["thread_id"] = thread_id
            transcript = _text(item, "transcript", required=True)
            fmt = _text(item, "format", "auto")
            messages = parse_import(source, transcript, fmt)
            # A fresh store has schema/WAL overhead. Refuse before creating it
            # when the account cannot fit even that, using an uncached meter.
            used = plans.StorageMeter._walk(directory)
            if limit is not None and not ctx.has_threads(principal.workspace_id):
                if used + 1024 * 1024 > limit:
                    raise ThreadStorageLimit()
            store = ctx.store_for(principal.workspace_id)
            source_id = f"import:{source}:{thread_id}"
            existing = store.by_source(principal.workspace_id, source_id)
            maximum = None
            if limit is not None:
                path = thread_path(directory)
                own_bytes = sum(
                    candidate.stat().st_size
                    for candidate in (
                        path,
                        path.with_name(path.name + "-wal"),
                        path.with_name(path.name + "-shm"),
                    )
                    if candidate.exists()
                )
                maximum = limit - (plans.StorageMeter._walk(directory) - own_bytes)
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
                error="Thread already imported with different content; original kept",
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
