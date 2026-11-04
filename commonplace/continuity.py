"""Append-only native session continuity and resumable archive adoption."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime
from io import StringIO

from . import capture, import_parts, plans, sync_protocol
from .continuity_schema import MARKER, archive_segments, ensure_schema, fork_id
from .handoff.api import _RequestError
from .handoff.models import ThreadMessage
from .handoff.thread_store import ThreadConflict, ThreadStore, canonical_sha256
from .handoff.transcripts import parse
from .import_grant import grant_id

BACKFILL_PAGE = 500
BACKFILL_PAGE_BYTES = 16 * 1024 * 1024


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _stream(conn, ws, identity):
    return conn.execute(
        "SELECT * FROM sync_streams WHERE workspace_id=? AND stream_id=?", (ws, identity)
    ).fetchone()


def _group(conn, ws, user, source, native):
    return conn.execute(
        "SELECT * FROM sync_groups WHERE workspace_id=? AND user_id=? AND source=? AND native_id=?",
        (ws, user, source, native),
    ).fetchone()


def _archive(store, stored):
    if not stored:
        return b"", ""
    segments = archive_segments(store.messages(stored.id))
    return b"".join(s.raw for s in segments), segments[-1].at if segments else ""


def _private(stored):
    if stored and stored.wing != capture.PRIVATE_WING:
        raise ThreadConflict()


def _own_bytes(conn, store, row):
    group = conn.execute(
        "SELECT * FROM sync_groups WHERE group_id=?", (row["group_id"],)
    ).fetchone()
    body, _ = _archive(store, store.get(group["holder_stored_id"]))
    if row["fork_stored_id"]:
        suffix, _ = _archive(store, store.get(row["fork_stored_id"]))
        body = body[: row["fork_offset"]] + suffix
    return body[: row["offset"]]


def stream_cursor(conn, store, *, ws, stream_id, own_identity):
    row = _stream(conn, ws, stream_id)
    if row is None:
        return capture.cursor(store, store.by_source(ws, own_identity))
    return {
        "offset": row["offset"],
        "prefix_sha256": _sha(_own_bytes(conn, store, row)),
        "last_sync": row["last_sync"] or "",
        "stored_thread_id": row["own_stored_id"],
    }


def _interval(conn, ws):
    if not plans.enabled():
        return 0
    row = conn.execute(
        "SELECT p.plan FROM workspaces w LEFT JOIN account_plans p ON p.user_id=w.personal_of "
        "WHERE w.id=?",
        (ws,),
    ).fetchone()
    return (plans.PLANS[row[0]] if row and row[0] else plans.FREE).sync_interval_seconds


def _due(last, interval, now):
    if last and interval:
        remaining = interval - (now - datetime.fromisoformat(last)).total_seconds()
        if remaining > 0:
            raise _RequestError(429, f"Capture not due; retry in {int(remaining) + 1} seconds")


def _save_stream(conn, **values):
    columns = list(values)
    conn.execute(
        f"INSERT INTO sync_streams ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)}) "
        "ON CONFLICT(workspace_id,stream_id) DO UPDATE SET "
        + ",".join(
            f"{key}=excluded.{key}" for key in columns if key not in ("workspace_id", "stream_id")
        ),
        tuple(values.values()),
    )


def _new_group(conn, ws, user, source, native, holder):
    key = _sha(json.dumps([ws, user, source, native]).encode())
    conn.execute(
        "INSERT INTO sync_groups VALUES (?,?,?,?,?,?)", (key, ws, user, source, native, holder)
    )
    return _group(conn, ws, user, source, native)


def _marker(store, ws, identity, holder, actor, offset=None):
    value = {"v": 1}
    if offset is not None:
        value["offset"] = offset
    existing = store.by_source(ws, identity)
    if existing:
        first = store.messages(existing.id, 0, 0)
        _private(existing)
        if (
            not first
            or first[0].name != MARKER
            or json.loads(first[0].raw) != value
            or store.parent_of(existing.id) != holder
        ):
            raise ThreadConflict()
        return existing
    return store.store(
        ws,
        identity,
        [ThreadMessage(0, "system", "other", "", json.dumps(value), name=MARKER)],
        actor=actor,
        parent_stored_id=holder,
        wing=capture.PRIVATE_WING,
        max_storage_bytes=None,
    )


def _part_identity(stream_id, source, fmt, part):
    # The immutable grant prefix is followed by the source and the caller's id.
    thread_id = stream_id.split(f":{source}:", 1)[1]
    return dict(
        source=source,
        thread_id=thread_id,
        format=fmt,
        version=part["version"],
        session_bytes=part["session_bytes"],
        session_sha256=part["session_sha256"],
    )


def _rows(raw, fmt, at, *, part=None, identity=None, recognized=True):
    text = raw.decode("utf-8")
    parsed = parse(text, fmt, require_recognized=False) if text else []
    rows = [
        ThreadMessage(
            0,
            "system",
            "other",
            "",
            text,
            name="import.raw" if part is not None else capture.RAW_NAME,
            at=at,
        )
    ]
    if part is not None:
        metadata = dict(
            identity=identity,
            part=part,
            recognized=recognized,
            session_state="complete" if part["final"] else "incomplete",
            message_count=len(parsed),
        )
        rows.append(
            ThreadMessage(
                1, "system", "other", "", json.dumps(metadata, sort_keys=True), name="import.part"
            )
        )
    rows.extend(parsed)
    return rows


def _write(store, ws, identity, rows, actor, stored=None):
    previous = store.messages(stored.id) if stored else []
    combined = previous + [replace(m, seq=len(previous) + i) for i, m in enumerate(rows)]
    if stored:
        return store.append(stored.id, combined, max_storage_bytes=None)
    return store.store(
        ws, identity, combined, actor=actor, wing=capture.PRIVATE_WING, max_storage_bytes=None
    )


def _fallback_capture(
    conn, store, ws, identity, source, data, offset, prefix, actor, maximum, gate
):
    # Preserve base validation and replay ordering, including for an existing
    # divergent archive at offset zero. Admission runs only for a new suffix.
    stored = store.by_source(ws, identity)
    _private(stored)
    current = capture.cursor(store, stored)
    if offset == current["offset"] and prefix == current["prefix_sha256"]:
        _due(current["last_sync"], _interval(conn, ws), plans._now())
        gate()
    return capture.append_capture(
        store,
        ws,
        identity,
        source,
        data.decode(),
        offset,
        prefix,
        actor=actor,
        maximum=maximum,
        interval=_interval(conn, ws),
    )


def _validate_cursor(conn, ws, stream_id, row, previous, data, offset, prefix, fmt, part):
    if part is not None and row:
        if (row["session_bytes"], row["session_sha256"], row["part_format"]) != (
            part["session_bytes"],
            part["session_sha256"],
            fmt,
        ):
            raise ThreadConflict()
    duplicate = (
        offset < len(previous) if part is None else bool(row and part["index"] < row["next_index"])
    )
    if duplicate:
        if _sha(previous[:offset]) != prefix or previous[offset : offset + len(data)] != data:
            raise ThreadConflict()
        if part is not None:
            saved = conn.execute(
                "SELECT descriptor FROM sync_parts WHERE workspace_id=? AND stream_id=? AND idx=?",
                (ws, stream_id, part["index"]),
            ).fetchone()
            if saved is None or json.loads(saved[0]) != part:
                raise ThreadConflict()
        return True
    if offset != len(previous) or prefix != _sha(previous):
        raise ThreadConflict()
    if part is not None:
        if (row and row["final"]) or part["index"] != (row["next_index"] if row else 0):
            raise ThreadConflict()
        if previous.endswith(b"\r") and data.startswith(b"\n"):
            raise ThreadConflict()
        if part["final"] and _sha(previous + data) != part["session_sha256"]:
            raise ThreadConflict()
    return False


def stream_push(
    conn,
    store,
    *,
    ws,
    user_id,
    connection_id,
    kind,
    stream_id,
    source,
    native_source,
    data: bytes,
    offset,
    prefix,
    actor,
    maximum,
    gate,
    part=None,
):
    """Accept one stream suffix inside the caller's control transaction."""
    text = data.decode("utf-8")
    if part is None:
        if not data or not data.endswith(b"\n"):
            raise _RequestError(400, "Send complete newline-terminated JSONL records")
        for line in StringIO(text, newline=""):
            if line.strip():
                json.loads(line)
        parsed = (
            parse(text, native_source or source, require_recognized=offset == 0)
            if text.strip()
            else []
        )
    else:
        import_parts.validate_part(part, data)
        import_parts.native_records(text, native_source, part["final"])
        parsed = parse(text, native_source, require_recognized=False)
    with store._transaction():
        row = _stream(conn, ws, stream_id)
        own = store.by_source(ws, stream_id)
        _private(own)
        marker = bool(own and store.messages(own.id, 0, 0)[0].name == MARKER)
        if row is None and own is not None and not marker:
            _adopt_orphan(conn, store, ws, user_id, own, source, kind, connection_id)
            row = _stream(conn, ws, stream_id)
        if row is None and not marker and (own is not None or offset > 0):
            if part is None:
                return _fallback_capture(
                    conn, store, ws, stream_id, source, data, offset, prefix, actor, maximum, gate
                )
            return import_parts.append_part(
                store,
                ws,
                source,
                _part_identity(stream_id, source, native_source, part)["thread_id"],
                native_source,
                text,
                part,
                actor=actor,
                maximum=maximum,
                source_id=stream_id,
                wing=capture.PRIVATE_WING,
            )
        native = sync_protocol.native_identity(data, native_source) if row is None else None
        if row is None and native is None:
            if marker:
                raise ThreadConflict()
            if part is None:
                return _fallback_capture(
                    conn, store, ws, stream_id, source, data, offset, prefix, actor, maximum, gate
                )
            return import_parts.append_part(
                store,
                ws,
                source,
                _part_identity(stream_id, source, native_source, part)["thread_id"],
                native_source,
                text,
                part,
                actor=actor,
                maximum=maximum,
                source_id=stream_id,
                wing=capture.PRIVATE_WING,
            )
        previous = _own_bytes(conn, store, row) if row else b""
        duplicate = _validate_cursor(
            conn, ws, stream_id, row, previous, data, offset, prefix, native_source, part
        )
        if duplicate:
            return _push_result(
                row, previous, parsed, part, duplicate=True, source_bytes=len(data), conn=conn
            )
        recognized = True
        if part is not None:
            recognized = import_parts.native_records(
                (previous + data).decode(), native_source, part["final"]
            )
            if part["final"] and not recognized:
                raise ThreadConflict()
        now = plans._now()
        if part is None:
            _due(row["last_sync"] if row else "", _interval(conn, ws), now)
            gate()
        identity = (
            _part_identity(stream_id, source, native_source, part) if part is not None else None
        )
        admission = _rows(
            data,
            native_source,
            now.isoformat(),
            part=part,
            identity=identity,
            recognized=recognized,
        )
        ThreadStore._check_storage_budget(getattr(store, "_current", store), maximum, admission)
        group = (
            conn.execute(
                "SELECT * FROM sync_groups WHERE group_id=?", (row["group_id"],)
            ).fetchone()
            if row
            else _group(conn, ws, user_id, native_source, native)
        )
        fork_stored, fork_offset = (
            (row["fork_stored_id"], row["fork_offset"]) if row else (None, None)
        )
        candidate = previous + data
        group = _find_orphan(conn, store, ws, user_id, native_source, native, candidate, group)
        if group is None:
            if marker:
                raise ThreadConflict()
            own = _write(store, ws, stream_id, admission, actor)
            group = _new_group(conn, ws, user_id, native_source, native, own.id)
        else:
            holder = store.get(group["holder_stored_id"])
            _private(holder)
            body, _ = _archive(store, holder)
            if fork_stored:
                target = store.get(fork_stored)
                # A store commit may precede its control commit. A retry only
                # writes the portion not already in the durable fork.
                fork_bytes, _ = _archive(store, target)
                expected = candidate[fork_offset:]
                if not expected.startswith(fork_bytes):
                    raise ThreadConflict()
                suffix = expected[len(fork_bytes) :]
            elif body.startswith(candidate):
                target, suffix = holder, b""
            elif candidate.startswith(body):
                target, suffix = holder, candidate[len(body) :]
            else:
                differing = next(i for i, (a, b) in enumerate(zip(candidate, body)) if a != b)
                fork_offset = max(offset, candidate.rfind(b"\n", 0, differing) + 1)
                target = _marker(
                    store, ws, fork_id(user_id, stream_id), holder.id, actor, fork_offset
                )
                fork_stored = target.id
                fork_bytes, _ = _archive(store, target)
                expected = candidate[fork_offset:]
                if not expected.startswith(fork_bytes):
                    raise ThreadConflict()
                suffix = expected[len(fork_bytes) :]
            if suffix:
                rows = _rows(
                    suffix,
                    native_source,
                    now.isoformat(),
                    part=part,
                    identity=identity,
                    recognized=recognized,
                )
                _write(store, ws, target.source_thread_id, rows, actor, target)
            if own is None:
                own = _marker(store, ws, stream_id, holder.id, actor)
        values = (
            dict(row)
            if row
            else dict(
                workspace_id=ws,
                stream_id=stream_id,
                user_id=user_id,
                connection_id=connection_id,
                kind=kind,
                group_id=group["group_id"],
                own_stored_id=own.id,
            )
        )
        values.update(
            offset=len(candidate),
            last_sync=now.isoformat(),
            fork_stored_id=fork_stored,
            fork_offset=fork_offset,
        )
        if part is not None:
            values.update(
                next_index=part["index"] + 1,
                session_bytes=part["session_bytes"],
                session_sha256=part["session_sha256"],
                part_format=native_source,
                final=int(part["final"]),
                listed_messages=(
                    row["listed_messages"] if row and row["listed_messages"] is not None else -1
                )
                + 2
                + len(parsed),
            )
            conn.execute(
                "INSERT INTO sync_parts VALUES (?,?,?,?)",
                (ws, stream_id, part["index"], json.dumps(part, sort_keys=True)),
            )
        _save_stream(conn, **values)
        return _push_result(
            _stream(conn, ws, stream_id), candidate, parsed, part, source_bytes=len(data), conn=conn
        )


def _mark_part_submission(conn, ws, stream_id):
    # Kept outside the frozen base schema: absence preserves pre-upgrade listings.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sync_import_submissions ("
        "workspace_id TEXT NOT NULL, stream_id TEXT NOT NULL, "
        "PRIMARY KEY(workspace_id,stream_id))"
    )
    conn.execute("INSERT OR IGNORE INTO sync_import_submissions VALUES (?,?)", (ws, stream_id))


def _push_result(row, raw, parsed, part, duplicate=False, source_bytes=0, *, conn):
    result = dict(
        status="duplicate" if duplicate else "appended",
        offset=row["offset"],
        prefix_sha256=_sha(raw),
        last_sync=row["last_sync"] or "",
        stored_thread_id=row["own_stored_id"],
    )
    if part is None:
        if not duplicate:
            result["added_messages"] = len(parsed)
    else:
        if not duplicate:
            _mark_part_submission(conn, row["workspace_id"], row["stream_id"])
        result.update(
            status="accepted",
            unit="part",
            format=row["part_format"],
            part=part,
            source_bytes=source_bytes,
            messages=len(parsed),
            next_index=row["next_index"],
            session_state="complete" if row["final"] else "incomplete",
        )
    return result


def import_item(
    conn, store, *, ws, user_id, source, fmt, thread_id, transcript, messages, actor, maximum
):
    """Import a complete snapshot; return None for the unchanged import path."""
    native_source = sync_protocol.native_source(source, fmt, transcript)
    data = transcript.encode("utf-8")
    native = sync_protocol.native_identity(data, native_source)
    if native is None or sync_protocol.live_tail(transcript):
        return None
    with store._transaction():
        store._check_storage_budget(maximum, messages)
        base = grant_id(user_id, source, thread_id)
        canonical = canonical_sha256(messages)
        identity = base
        existing = store.by_source(ws, identity)
        _private(existing)
        row = _stream(conn, ws, identity)
        if row is None and existing is not None:
            _adopt_orphan(conn, store, ws, user_id, existing, source, "import", "")
            row = _stream(conn, ws, identity)
        result = dict(status="accepted", messages=len(messages) - 1, source_bytes=len(data))
        if (
            row
            and _own_bytes(conn, store, row) == data
            or existing
            and existing.sha256 == canonical
        ):
            return result
        if existing or row:
            identity = f"{base}#sha256:{canonical}"
            existing = store.by_source(ws, identity)
            _private(existing)
            row = _stream(conn, ws, identity)
            if row or existing and existing.sha256 == canonical:
                return result
        group = _group(conn, ws, user_id, native_source, native)
        if group is None:
            group = _find_orphan(conn, store, ws, user_id, native_source, native, data)
        body = _archive(store, store.get(group["holder_stored_id"]))[0] if group else b""
        if group and not (body.startswith(data) or data.startswith(body)):
            return None
        now = plans._now().isoformat()
        if group is None:
            if existing:
                return None
            rows = [replace(m, at=now) if m.seq == 0 else m for m in messages]
            own = _write(store, ws, identity, rows, actor)
            group = _new_group(conn, ws, user_id, native_source, native, own.id)
        else:
            if data.startswith(body) and len(data) > len(body):
                holder = store.get(group["holder_stored_id"])
                _write(
                    store,
                    ws,
                    holder.source_thread_id,
                    _rows(data[len(body) :], native_source, now),
                    actor,
                    holder,
                )
            own = _marker(store, ws, identity, group["holder_stored_id"], actor)
        _save_stream(
            conn,
            workspace_id=ws,
            stream_id=identity,
            user_id=user_id,
            connection_id="",
            kind="import",
            group_id=group["group_id"],
            own_stored_id=own.id,
            offset=len(data),
            last_sync=now,
            listed_messages=len(messages) - 1,
        )
        return result


class _RetryPage(Exception):
    """The page changed after its unlocked read."""


def _backfill_cursor(conn, ws, user):
    row = conn.execute(
        "SELECT * FROM sync_backfill WHERE workspace_id=? AND user_id=?", (ws, user)
    ).fetchone()
    return (
        tuple(row[key] for key in ("prefix_idx", "after_source", "after_id", "done"))
        if row
        else (0, None, None, 0)
    )


def backfill(db, store, ws, user_id) -> None:
    """Adopt archives in bounded pages without holding a control lock for I/O."""
    if db._lock._is_owned():
        raise RuntimeError("Backfill requires no control lock or transaction")
    with db._lock:
        if db._conn.in_transaction:
            raise RuntimeError("Backfill requires no control lock or transaction")
    ensure_schema(db)
    with db._lock:
        connections = db._conn.execute(
            "SELECT id FROM connections WHERE workspace_id=? AND user_id=? ORDER BY id",
            (ws, user_id),
        ).fetchall()
    prefixes = [
        (f"import-private:{user_id}:", "import", ""),
        (f"capture:{user_id}:", "capture", ""),
    ]
    prefixes.extend((f"capture:{row[0]}:", "capture", row[0]) for row in connections)
    while not _backfill_page(db, store, ws, user_id, prefixes):
        pass


def _adoption_candidate(stored, prefix, kind, connection, read):
    if stored.wing != capture.PRIVATE_WING or "#sha256:" in stored.source_thread_id:
        return None
    source = stored.source_thread_id[len(prefix) :].split(":", 1)[0]
    _, raw, last, messages = read(stored)
    text = raw.decode("utf-8")
    native_source = sync_protocol.native_source(source, "auto", text)
    native = sync_protocol.native_identity(raw, native_source)
    if native is None or sync_protocol.live_tail(text):
        return None
    try:
        parse(text, native_source)
        state, parts = {}, []
        if kind == "import" and messages and messages[0].name == "import.raw":
            identity = json.loads(messages[1].raw)["identity"]
            normalized = [replace(m, at="") if m.name == "import.raw" else m for m in messages]
            _, _, _, parts, final = import_parts._state(normalized, identity)
            state = dict(
                next_index=len(parts),
                session_bytes=identity["session_bytes"],
                session_sha256=identity["session_sha256"],
                part_format=identity["format"],
                final=int(final),
            )
        return stored, raw, last, (native_source, native), kind, connection, state, parts
    except (ValueError, TypeError, KeyError, IndexError, ThreadConflict):
        return None


def _adopt_orphan(conn, store, ws, user, stored, source, kind, connection):
    """Recover a store commit whose corresponding control transaction rolled back."""
    cache = {}

    def read(thread):
        messages = store.messages(thread.id)
        segments = archive_segments(messages)
        raw = b"".join(segment.raw for segment in segments)
        value = (thread, raw, segments[-1].at if segments else "", messages)
        cache[thread.id] = value
        return value

    prefix = stored.source_thread_id.split(f":{source}:", 1)[0] + ":"
    candidate = _adoption_candidate(stored, prefix, kind, connection, read)
    if candidate is not None:
        group = _group(conn, ws, user, *candidate[3])
        if group:
            read(store.get(group["holder_stored_id"]))
        _join_adopted(conn, ws, user, candidate, cache)


# One equality term per branch so each uses its own index (an OR here scans the
# workspace's streams, which made every orphan search quadratic in palace size).
_CONTROLLED_PROBE = (
    "SELECT 1 FROM sync_streams WHERE workspace_id=? AND own_stored_id=? UNION ALL "
    "SELECT 1 FROM sync_streams WHERE workspace_id=? AND fork_stored_id=? UNION ALL "
    "SELECT 1 FROM sync_groups WHERE workspace_id=? AND holder_stored_id=? LIMIT 1"
)


def _find_orphan(conn, store, ws, user, source, native, incoming, group=None):
    """Page the filer's archives, bounding only uncontrolled archive reads."""
    if group is not None:
        return group
    connections = conn.execute(
        "SELECT id FROM connections WHERE workspace_id=? AND user_id=? ORDER BY id LIMIT ?",
        (ws, user, BACKFILL_PAGE),
    ).fetchall()
    prefixes = [(f"capture:{user}:", "capture", ""), (f"import-private:{user}:", "import", "")]
    prefixes.extend((f"capture:{r[0]}:", "capture", r[0]) for r in connections)
    remaining, read_bytes = BACKFILL_PAGE, 0
    cache = {}
    for prefix, kind, connection in prefixes:
        after = None
        while True:
            listed = store.list_source_range(ws, prefix, after=after, limit=BACKFILL_PAGE)
            for stored in listed:
                after = (stored.source_thread_id, stored.id)
                # Controlled archives need only metadata checks, not archive reads.
                controlled = conn.execute(
                    _CONTROLLED_PROBE, (ws, stored.id, ws, stored.id, ws, stored.id)
                ).fetchone()
                if controlled:
                    continue

                def read(thread):
                    nonlocal remaining, read_bytes
                    messages = store.messages(thread.id)
                    segments = archive_segments(messages)
                    raw = b"".join(s.raw for s in segments)
                    remaining -= 1
                    read_bytes += len(raw)
                    cache[thread.id] = (thread, raw, segments[-1].at if segments else "", messages)
                    return cache[thread.id]

                candidate = _adoption_candidate(stored, prefix, kind, connection, read)
                if candidate is not None:
                    raw = candidate[1]
                    if candidate[3] == (source, native) and (
                        raw.startswith(incoming) or incoming.startswith(raw)
                    ):
                        _join_adopted(conn, ws, user, candidate, cache)
                        return _group(conn, ws, user, source, native)
                cache.clear()
                if remaining == 0 or read_bytes >= BACKFILL_PAGE_BYTES:
                    return None
            if len(listed) < BACKFILL_PAGE:
                break
    return None


def _backfill_page(db, store, ws, user, prefixes):
    # All large objects live in this call only: a committed or rejected page
    # releases them before the next listing or a caller's next operation.
    with db._lock:
        start = _backfill_cursor(db._conn, ws, user)
    if start[3]:
        return True
    idx, after_source, after_id, _ = start
    after = (after_source, after_id) if after_source is not None else None
    cache, groups, candidates = {}, {}, []
    read_bytes = 0

    def read(stored):
        nonlocal read_bytes
        if stored.id not in cache:
            messages = store.messages(stored.id)
            segments = archive_segments(messages)
            raw = b"".join(s.raw for s in segments)
            cache[stored.id] = (stored, raw, segments[-1].at if segments else "", messages)
            read_bytes += len(raw)
        return cache[stored.id]

    while idx < len(prefixes):
        prefix, kind, connection = prefixes[idx]
        listed = store.list_source_range(ws, prefix, after=after, limit=BACKFILL_PAGE)
        for stored in listed:
            candidate = _adoption_candidate(stored, prefix, kind, connection, read)
            if candidate is not None:
                key = candidate[3]
                if key not in groups:
                    with db._lock:
                        group = _group(db._conn, ws, user, *key)
                    groups[key] = dict(group) if group else None
                    if group:
                        read(store.get(group["holder_stored_id"]))
            candidates.append((stored, candidate))
            after = (stored.source_thread_id, stored.id)
            if len(candidates) >= BACKFILL_PAGE or read_bytes >= BACKFILL_PAGE_BYTES:
                break
        if len(candidates) >= BACKFILL_PAGE or read_bytes >= BACKFILL_PAGE_BYTES:
            break
        # A short listing exhausted this prefix. Empty prefixes do not need
        # separate commits, and the page can continue in the next namespace.
        if len(listed) < BACKFILL_PAGE:
            idx += 1
            after = None
        else:
            break
    done = idx == len(prefixes)
    # When the last row also crosses a cap, determine EOF with metadata only.
    if not done and after is not None:
        if not store.list_source_range(ws, prefixes[idx][0], after=after, limit=1):
            probe = idx + 1
            while probe < len(prefixes) and not store.list_source_range(
                ws, prefixes[probe][0], limit=1
            ):
                probe += 1
            if probe == len(prefixes):
                idx, after, done = probe, None, True
    try:
        with db._lock, db._tx() as conn:
            if _backfill_cursor(conn, ws, user) != start:
                raise _RetryPage()
            for stored, _, _, _ in cache.values():
                current = store.get(stored.id)
                if current is None or current.message_count != stored.message_count:
                    raise _RetryPage()
            for key, group in groups.items():
                current = _group(conn, ws, user, *key)
                if (current["holder_stored_id"] if current else None) != (
                    group["holder_stored_id"] if group else None
                ):
                    raise _RetryPage()
            for _, candidate in candidates:
                if candidate is not None:
                    _join_adopted(conn, ws, user, candidate, cache)
            conn.execute(
                "INSERT INTO sync_backfill VALUES (?,?,?,?,?,?) ON CONFLICT(workspace_id,user_id) "
                "DO UPDATE SET prefix_idx=excluded.prefix_idx,after_source=excluded.after_source,after_id=excluded.after_id,done=excluded.done",
                (
                    ws,
                    user,
                    idx,
                    after[0] if after else None,
                    after[1] if after else None,
                    int(done),
                ),
            )
    except _RetryPage:
        return False
    return done


def _join_adopted(conn, ws, user, candidate, cache):
    stored, raw, last, key, kind, connection, state, parts = candidate
    if _stream(conn, ws, stored.source_thread_id):
        return
    group = _group(conn, ws, user, *key)
    legacy = 0
    if group is None:
        group = _new_group(conn, ws, user, *key, stored.id)
    else:
        body = cache[group["holder_stored_id"]][1]
        if body.startswith(raw):
            legacy = int(stored.id != group["holder_stored_id"])
        elif raw.startswith(body):
            conn.execute(
                "UPDATE sync_streams SET legacy=1 WHERE group_id=? AND own_stored_id=?",
                (group["group_id"], group["holder_stored_id"]),
            )
            conn.execute(
                "UPDATE sync_groups SET holder_stored_id=? WHERE group_id=?",
                (stored.id, group["group_id"]),
            )
        else:
            return
    _save_stream(
        conn,
        workspace_id=ws,
        stream_id=stored.source_thread_id,
        user_id=user,
        connection_id=connection,
        kind=kind,
        group_id=group["group_id"],
        own_stored_id=stored.id,
        offset=len(raw),
        last_sync=last,
        legacy=legacy,
        listed_messages=stored.message_count - 1 if kind == "import" else None,
        **state,
    )
    for descriptor, _ in parts:
        conn.execute(
            "INSERT INTO sync_parts VALUES (?,?,?,?)",
            (
                ws,
                stored.source_thread_id,
                descriptor["index"],
                json.dumps(descriptor, sort_keys=True),
            ),
        )
