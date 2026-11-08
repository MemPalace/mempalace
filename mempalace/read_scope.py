"""Request-only metadata descriptors for authenticated overview reads.

Descriptors bind a drawer identity to its current metadata. They contain no
words and are never persisted. Final reads use a fresh snapshot, so a cached
raw taxonomy cannot authorize a moved, replaced or newly inserted drawer.
"""

import contextlib
import hashlib
import json
import os
from time import monotonic

from .metadata_types import is_bookkeeping
from .stored_text import decode_record

PAGE_SIZE = 500


def descriptor(drawer_id, metadata):
    if not isinstance(drawer_id, str) or not isinstance(metadata, dict):
        raise ValueError("invalid metadata row")
    if is_bookkeeping(metadata):
        return None
    pair = ["unknown" if metadata.get(k) in (None, "?") else metadata[k] for k in ("wing", "room")]
    if any(not isinstance(v, str) for v in pair):
        return None
    stamp = hashlib.sha256(
        json.dumps(metadata, sort_keys=True, ensure_ascii=True, allow_nan=False).encode()
    ).hexdigest()
    return {"id": drawer_id, "wing": pair[0], "room": pair[1], "stamp": stamp}


def valid_descriptors(rows):
    return (
        isinstance(rows, list)
        and len(rows) <= PAGE_SIZE
        and all(
            isinstance(r, dict)
            and set(r) == {"id", "wing", "room", "stamp"}
            and all(isinstance(v, str) for v in r.values())
            and bool(r["id"])
            and len(r["stamp"]) == 64
            and all(c in "0123456789abcdef" for c in r["stamp"])
            for r in rows
        )
        and len({r["id"] for r in rows}) == len(rows)
    )


def _chroma_page(config, after, ids, end):
    from .backends._inproc_sqlite import open_reader
    from .backends.chroma_sqlite import (
        _cell_value,
        _metadata_value_columns,
        _sqlite_ready_metadata_segment,
    )

    path = os.path.join(config.palace_path, "chroma.sqlite3")
    if not os.path.isfile(path):
        return None
    with contextlib.closing(open_reader(path)) as conn:
        conn.set_progress_handler(lambda: int(monotonic() >= end), 1000)
        conn.execute("BEGIN")
        ready = _sqlite_ready_metadata_segment(conn, config.collection_name)
        if ready is None:
            return None
        _, segment = ready
        condition, params = "embedding_id > ?", [after or ""]
        if ids is not None:
            condition, params = "embedding_id IN (" + ",".join("?" for _ in ids) + ")", ids
        pairs = conn.execute(
            "SELECT id,embedding_id FROM embeddings WHERE segment_id=? AND "
            + condition
            + " ORDER BY embedding_id LIMIT ?",
            [segment, *params, PAGE_SIZE],
        ).fetchall()
        metadata = {row_id: {} for row_id, _ in pairs}
        if pairs:
            columns = _metadata_value_columns(conn)
            at = [
                columns.index(k) if k in columns else None
                for k in ("string_value", "int_value", "float_value", "bool_value")
            ]
            for row_id, key, *cells in conn.execute(
                "SELECT id,key,"
                + ",".join(columns)
                + " FROM embedding_metadata WHERE id IN ("
                + ",".join("?" for _ in pairs)
                + ") AND substr(key,1,7) != 'chroma:'",
                [row_id for row_id, _ in pairs],
            ):
                value = _cell_value(*(None if i is None else cells[i] for i in at))
                if value is not None:
                    metadata[row_id][key] = value
        return [
            (drawer_id, decode_record(None, metadata[row_id])[1]) for row_id, drawer_id in pairs
        ]


def _exact_page(config, backend, after, ids, end):
    from .backends._inproc_sqlite import open_reader

    filename = "compact.sqlite3" if backend == "compact" else "sqlite_exact.sqlite3"
    path = os.path.join(config.palace_path, filename)
    if not os.path.isfile(path):
        return None
    with contextlib.closing(open_reader(path)) as conn:
        conn.set_progress_handler(lambda: int(monotonic() >= end), 1000)
        conn.execute("BEGIN")
        collection = conn.execute(
            "SELECT id FROM collections WHERE name=?", (config.collection_name,)
        ).fetchone()
        if collection is None:
            return None
        condition, params = "id > ?", [after or ""]
        if ids is not None:
            condition, params = "id IN (" + ",".join("?" for _ in ids) + ")", ids
        rows = conn.execute(
            "SELECT id,metadata_json FROM documents WHERE collection_id=? AND "
            + condition
            + " ORDER BY id LIMIT ?",
            [collection[0], *params, PAGE_SIZE],
        ).fetchall()
        return [
            (drawer_id, decode_record(None, json.loads(meta or "{}"))[1])
            for drawer_id, meta in rows
        ]


def metadata_page(config, backend, get_collection, *, after=None, ids=None, timeout=660.0):
    """At most 500 ids/metadata, from one SQL snapshot when available.

    SQL uses indexed identity seeks and excludes document columns. Unsupported
    backends use bounded metadata-only collection pages. A full page always
    requires continuation, including a page containing only bookkeeping.
    """
    if ids == []:
        return [], None
    if ids is not None and (len(ids) > PAGE_SIZE or len(set(ids)) != len(ids)):
        raise ValueError("invalid metadata batch")
    if after is not None and (
        not isinstance(after, str) or not after.startswith(("id:", "offset:"))
    ):
        raise ValueError("invalid metadata cursor")
    if not isinstance(timeout, (int, float)) or not 0 < timeout <= 660:
        raise ValueError("invalid metadata deadline")
    end = monotonic() + timeout
    cursor = after[3:] if after and after.startswith("id:") else None
    rows = None
    if ids is not None or not after or after.startswith("id:"):
        if backend == "chroma":
            rows = _chroma_page(config, cursor, ids, end)
        elif backend in ("sqlite_exact", "rust_exact", "compact"):
            rows = _exact_page(config, backend, cursor, ids, end)
    if rows is not None:
        next_cursor = "id:" + rows[-1][0] if ids is None and len(rows) == PAGE_SIZE else None
    else:
        if after and after.startswith("id:"):
            raise ValueError("metadata source changed")
        col = get_collection()
        if col is None:
            raise ValueError("metadata unavailable")
        offset = int(after[7:]) if after else 0
        if offset < 0:
            raise ValueError("invalid metadata cursor")
        page = col.get(
            **({"ids": ids} if ids is not None else {"limit": PAGE_SIZE, "offset": offset}),
            include=["metadatas"],
        )
        keys, metas = page["ids"], page["metadatas"]
        if (
            not isinstance(keys, list)
            or not isinstance(metas, list)
            or len(keys) != len(metas)
            or len(keys) > PAGE_SIZE
        ):
            raise ValueError("invalid metadata page")
        rows = list(zip(keys, metas))
        next_cursor = (
            f"offset:{offset + len(rows)}" if ids is None and len(rows) == PAGE_SIZE else None
        )
    if monotonic() >= end:
        raise TimeoutError("metadata deadline expired")
    return rows, next_cursor
