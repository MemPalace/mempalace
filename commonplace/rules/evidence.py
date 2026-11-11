"""Evidence about how rules fare in use: bounded events and counters that survive pruning."""

import re

from commonplace.boards.service import validate_text
from commonplace.extensions import ToolError

from .store import now

# What an agent or owner may report through rules_feedback / REST. Other writers (guards, the
# gardener) call record() with their own kind and source; the kind column is deliberately free.
FEEDBACK_KINDS = ("followed", "helped", "violated", "conflict", "outdated", "unclear", "note")
SOURCES = ("agent", "person", "system")
NOTE_LIMIT = 500
KEEP_PER_RULE = 200
KEEP_PER_ACTOR = 50
READ_LIMIT = 50
_KIND = re.compile(r"[a-z][a-z_]{0,31}\Z")

# Additive and idempotent: run inside store.migrate()'s transaction, so a crash leaves either
# the old file or the complete tables. Old databases gain them on first open.
STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS rule_events (
        id INTEGER PRIMARY KEY,
        rule_id INTEGER NOT NULL REFERENCES rules(id),
        rule_version INTEGER NOT NULL,
        kind TEXT NOT NULL,
        source TEXT NOT NULL CHECK (source IN ('agent','person','system')),
        actor TEXT NOT NULL,
        note TEXT,
        created_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS rule_events_by_rule ON rule_events(rule_id, id)",
    "CREATE INDEX IF NOT EXISTS rule_events_by_actor ON rule_events(rule_id, actor, id)",
    """CREATE TRIGGER IF NOT EXISTS rule_events_no_update BEFORE UPDATE ON rule_events
    BEGIN SELECT RAISE(ABORT, 'rule_events is append-only'); END""",
    # Counters are written with the event, so pruning old events never loses a count.
    """CREATE TABLE IF NOT EXISTS rule_stats (
        rule_id INTEGER NOT NULL REFERENCES rules(id),
        kind TEXT NOT NULL,
        count INTEGER NOT NULL,
        first_at TEXT NOT NULL,
        last_at TEXT NOT NULL,
        PRIMARY KEY (rule_id, kind)
    ) WITHOUT ROWID""",
)


def ensure(conn):
    for statement in STATEMENTS:
        conn.execute(statement)


def feedback_kind(kind):
    if kind not in FEEDBACK_KINDS:
        raise ToolError("no: kind must be one of " + ", ".join(FEEDBACK_KINDS))
    return kind


def feedback_note(kind, note):
    if note is not None:
        note = validate_text(note, "note", limit=NOTE_LIMIT).strip() or None
    if kind == "note" and note is None:
        raise ToolError("no: a note needs text")
    return note


def record(conn, rule, kind, *, source, actor, note=None):
    """Append one event and bump its counter in the caller's write transaction."""
    if not isinstance(kind, str) or not _KIND.match(kind) or source not in SOURCES:
        raise ToolError("no: invalid evidence event")
    stamp = now()
    cursor = conn.execute(
        "INSERT INTO rule_events(rule_id,rule_version,kind,source,actor,note,created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (rule["id"], rule["version"], kind, source, actor, note, stamp),
    )
    conn.execute(
        "INSERT INTO rule_stats(rule_id,kind,count,first_at,last_at) VALUES (?,?,1,?,?) "
        "ON CONFLICT(rule_id,kind) DO UPDATE SET count=count+1,last_at=excluded.last_at",
        (rule["id"], kind, stamp, stamp),
    )
    # One noisy writer cannot push everyone else's events out of the window.
    conn.execute(
        "DELETE FROM rule_events WHERE rule_id=? AND actor=? AND id NOT IN "
        "(SELECT id FROM rule_events WHERE rule_id=? AND actor=? ORDER BY id DESC LIMIT ?)",
        (rule["id"], actor, rule["id"], actor, KEEP_PER_ACTOR),
    )
    conn.execute(
        "DELETE FROM rule_events WHERE rule_id=? AND id NOT IN "
        "(SELECT id FROM rule_events WHERE rule_id=? ORDER BY id DESC LIMIT ?)",
        (rule["id"], rule["id"], KEEP_PER_RULE),
    )
    return event_json(
        conn.execute("SELECT * FROM rule_events WHERE id=?", (cursor.lastrowid,)).fetchone()
    )


def event_json(row):
    return {
        key: row[key] for key in "id rule_id rule_version kind source actor note created_at".split()
    }


def stats(conn, rule_ids=None):
    """Counters per rule id: {id: {total, kinds: {kind: {count, first_at, last_at}}}}."""
    sql = "SELECT rule_id,kind,count,first_at,last_at FROM rule_stats"
    args = ()
    if rule_ids is not None:
        rule_ids = list(rule_ids)
        sql += f" WHERE rule_id IN ({','.join('?' * len(rule_ids))})"
        args = rule_ids
    result = {}
    for row in conn.execute(sql + " ORDER BY rule_id,kind", args):
        entry = result.setdefault(row["rule_id"], {"total": 0, "kinds": {}})
        entry["total"] += row["count"]
        entry["kinds"][row["kind"]] = {
            "count": row["count"],
            "first_at": row["first_at"],
            "last_at": row["last_at"],
        }
    return result


def events(conn, rule_id, *, actor=None, limit=READ_LIMIT):
    """Newest first; ``actor`` narrows the read to that writer's own events."""
    sql, args = "SELECT * FROM rule_events WHERE rule_id=?", [rule_id]
    if actor is not None:
        sql += " AND actor=?"
        args.append(actor)
    return [
        event_json(row) for row in conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit))
    ]
