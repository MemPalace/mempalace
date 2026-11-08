"""Metadata-only hallway probes and exact final-snapshot association proof."""

import contextlib
import json

PAGE_SIZE = 500
_FIELDS = ("id", "wing", "rooms", "label")


def descriptor(record):
    return {key: record[key] for key in _FIELDS if key in record}


def read(scope, wing=None, config=None):
    from . import hallways

    conn = hallways._reader(config)
    if conn is None:
        records = hallways._load_hallways_json(config)
        rows = [(i + 1, r) for i, r in enumerate(records) if isinstance(r, dict)]
        return _select(rows, scope, wing)
    with contextlib.closing(conn):
        conn.execute("BEGIN")
        if scope.get("probe") is True:
            after = scope.get("after", 0)
            if type(after) is not int or after < 0:
                raise ValueError("invalid hallway cursor")
            where, params = ("AND wing = ?", (wing,)) if wing is not None else ("", ())
            rows = conn.execute(
                "SELECT rowid, (SELECT json_group_object(key, "
                "CASE WHEN type IN ('array','object') THEN json(value) ELSE value END) "
                "FROM json_each(record) WHERE key IN ('id','wing','rooms','label')) "
                f"FROM hallways WHERE rowid > ? {where} ORDER BY rowid LIMIT ?",
                (after, *params, PAGE_SIZE + 1),
            )
        else:
            allow = scope.get("allow")
            if not isinstance(allow, list) or len(allow) > PAGE_SIZE:
                raise ValueError("invalid hallway allow descriptors")
            positions = [r["position"] for r in allow]
            if not positions:
                return {"hallways": []}
            marks = ",".join("?" for _ in positions)
            candidates = conn.execute(
                "SELECT rowid, (SELECT json_group_object(key, "
                "CASE WHEN type IN ('array','object') THEN json(value) ELSE value END) "
                "FROM json_each(record) WHERE key IN ('id','wing','rooms','label')) "
                f"FROM hallways WHERE rowid IN ({marks}) ORDER BY rowid",
                positions,
            )
            expected = {r["position"]: r["record"] for r in allow}
            positions = [pos for pos, raw in candidates if json.loads(raw) == expected[pos]]
            if not positions:
                return {"hallways": []}
            marks = ",".join("?" for _ in positions)
            rows = conn.execute(
                f"SELECT rowid, record FROM hallways WHERE rowid IN ({marks}) ORDER BY rowid",
                positions,
            )
        return _select([(pos, json.loads(raw)) for pos, raw in rows], scope, wing)


def _select(rows, scope, wing):
    if scope.get("probe") is True:
        after = scope.get("after", 0)
        if type(after) is not int or after < 0:
            raise ValueError("invalid hallway cursor")
        rows = [(p, r) for p, r in rows if p > after and (wing is None or r.get("wing") == wing)]
        page = [{"position": p, "record": descriptor(r)} for p, r in rows[:PAGE_SIZE]]
        return {
            "probe": {"rows": page, "next": page[-1]["position"] if len(rows) > len(page) else None}
        }
    allow = scope.get("allow")
    if not isinstance(allow, list) or len(allow) > PAGE_SIZE:
        raise ValueError("invalid hallway allow descriptors")
    expected = {r["position"]: r["record"] for r in allow}
    return {
        "hallways": [
            r
            for p, r in rows
            if p in expected
            and descriptor(r) == expected[p]
            and (wing is None or r.get("wing") == wing)
        ]
    }
