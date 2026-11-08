"""Bounded metadata-only graph projection protocol; no shared graph cache."""

import hashlib
import json
import math
import os
import sqlite3
from contextlib import closing
from time import monotonic

from .read_scope import PAGE_SIZE, metadata_page, valid_descriptors
from .stored_text import decode_value


def graph_descriptor(drawer_id, metadata, sort=None):
    """Bind literal graph values, without overview placeholder normalization."""
    stamp = hashlib.sha256(
        json.dumps([metadata, sort], sort_keys=True, ensure_ascii=True, allow_nan=False).encode()
    ).hexdigest()
    return {"id": drawer_id, "wing": metadata["wing"], "room": metadata["room"], "stamp": stamp}


def graph_metadata(meta, *, text_loci=False, chroma_grouped=False, cast_text=str):
    if not isinstance(meta, dict):
        return None
    projected = dict(meta)
    if text_loci:
        for key in ("wing", "room", "hall"):
            value = meta.get(key)
            if type(value) in (int, float):
                projected[key] = cast_text(value)
    if any(not isinstance(projected.get(k), str) or not projected[k] for k in ("wing", "room")):
        return None
    if any(
        projected.get(k) is not None and not isinstance(projected[k], (str, int, float, bool))
        for k in ("hall", "date")
    ):
        return None
    result = {k: str(projected.get(k) or "") for k in ("wing", "room", "hall", "date")}
    if chroma_grouped:
        # Chroma ignores the separate boolean column in grouped hall reads.
        if isinstance(meta.get("hall"), bool):
            result["hall"] = ""
        # Chroma's grouped dates read only the string column.
        result["date"] = meta.get("date") if isinstance(meta.get("date"), str) else ""
    return result


def _sort_cell(value):
    # SQLite sorts NULL, numeric values, then text. Keep the stored value:
    # decoding or converting it to text first can change sibling tie order.
    if value is None:
        return [0, ""]
    if type(value) in (int, float):
        if math.isinf(value):
            return [1 if value < 0 else 3, ""]
        return [2, value]
    return [4, value]


def valid_sort(value):
    return value is None or (
        isinstance(value, list)
        and len(value) == 3
        and all(
            isinstance(cell, list)
            and len(cell) == 2
            and type(cell[0]) is int
            and (
                cell[0] in (0, 1, 3)
                and cell[1] == ""
                or cell[0] == 2
                and type(cell[1]) in (int, float)
                and math.isfinite(cell[1])
                or cell[0] == 4
                and isinstance(cell[1], str)
            )
            for cell in value
        )
    )


def _exact_graph_page(config, backend, after, ids, timeout):
    """Read local graph expressions and schema from one metadata-only snapshot."""
    from .backends._inproc_sqlite import open_reader
    from .backends.sqlite_exact import _documents_has_locus_columns

    filename = "compact.sqlite3" if backend == "compact" else "sqlite_exact.sqlite3"
    path = os.path.join(config.palace_path, filename)
    if not os.path.isfile(path) or after and after.startswith("offset:"):
        return None
    end = monotonic() + timeout
    with closing(open_reader(path)) as conn:
        conn.set_progress_handler(lambda: int(monotonic() >= end), 1000)
        conn.execute("BEGIN")
        collection = conn.execute(
            "SELECT id FROM collections WHERE name=?", (config.collection_name,)
        ).fetchone()
        if collection is None:
            return None
        locus = _documents_has_locus_columns(conn)
        order = ["wing", "room", "hall"] if locus else ["room", "wing", "hall"]
        expressions = [
            key if locus else f"json_extract(metadata_json, '$.{key}')"
            for key in ("wing", "room", "hall")
        ]
        condition, params = "id > ?", [after[3:] if after else ""]
        if ids is not None:
            condition, params = "id IN (" + ",".join("?" for _ in ids) + ")", ids
        rows = conn.execute(
            "SELECT id,"
            + ",".join(expressions)
            + ",json_extract(metadata_json, '$.date') FROM documents WHERE collection_id=? AND "
            + condition
            + " ORDER BY id LIMIT ?",
            [collection[0], *params, PAGE_SIZE],
        ).fetchall()
    result = []
    for key, *values in rows:
        raw = dict(zip(("wing", "room", "hall", "date"), values))
        decoded = {k: decode_value(v) for k, v in raw.items()}
        projected = (
            None
            if not decoded["wing"] or not decoded["room"]
            else {k: str(v or "") for k, v in decoded.items()}
        )
        result.append((key, projected, [_sort_cell(raw[k]) for k in order]))
    if monotonic() >= end:
        raise TimeoutError("metadata deadline expired")
    cursor = "id:" + rows[-1][0] if ids is None and len(rows) == PAGE_SIZE else None
    return result, cursor, order


def scoped_rows(scope, config, backend, get_collection):
    if not isinstance(scope, dict):
        raise ValueError("invalid graph scope")
    probe = scope.get("probe") is True
    if set(scope) != ({"probe", "after", "timeout"} if probe else {"allow", "timeout"}):
        raise ValueError("invalid graph scope")
    allowed = scope.get("allow")
    if not probe and not valid_descriptors(allowed):
        raise ValueError("invalid graph descriptors")
    after, timeout = scope.get("after"), scope.get("timeout")
    if after is not None and (
        not isinstance(after, str) or not after.startswith(("id:", "offset:"))
    ):
        raise ValueError("invalid metadata cursor")
    if not isinstance(timeout, (int, float)) or not 0 < timeout <= 660:
        raise ValueError("invalid metadata deadline")
    ids = None if probe else [r["id"] for r in allowed]
    exact = (
        _exact_graph_page(config, backend, after, ids, timeout)
        if backend in ("sqlite_exact", "rust_exact", "compact")
        else None
    )
    if exact is not None:
        records, cursor, order = exact
    else:
        used_collection = False

        def collection():
            nonlocal used_collection
            used_collection = True
            return get_collection()

        records, cursor = metadata_page(
            config, backend, collection, after=after, ids=ids, timeout=timeout
        )
        order = [] if used_collection else ["room", "wing", "hall"]
        with closing(sqlite3.connect(":memory:")) as casts:
            projected_rows = []
            for key, meta in records:
                projected = graph_metadata(
                    meta,
                    text_loci=not used_collection and backend == "chroma",
                    chroma_grouped=backend == "chroma" and not used_collection,
                    cast_text=lambda value: casts.execute(
                        "SELECT CAST(? AS TEXT)", (value,)
                    ).fetchone()[0],
                )
                sort = [_sort_cell(projected[k]) for k in order] if projected and order else None
                projected_rows.append((key, projected, sort))
            records = projected_rows
    rows = []
    for key, projected, sort in records:
        if projected is None:
            continue
        item = graph_descriptor(key, projected, sort)
        if probe:
            rows.append(item)
        elif item in allowed:
            rows.append({"descriptor": item, "metadata": projected, "sort": sort})
    if probe:
        return {"rows": rows, "next": cursor, "order": order}
    by_id = {r["descriptor"]["id"]: r for r in rows}
    return {"rows": [by_id[r["id"]] for r in allowed if r["id"] in by_id]}
