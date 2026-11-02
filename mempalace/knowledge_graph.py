"""
knowledge_graph.py — Temporal Entity-Relationship Graph for MemPalace
=====================================================================

Real knowledge graph with:
  - Entity nodes (people, projects, tools, concepts)
  - Typed relationship edges (daughter_of, does, loves, works_on, etc.)
  - Temporal validity (valid_from → valid_to — knows WHEN facts are true)
  - Closet references (links back to the verbatim memory)

Storage: SQLite (local, no dependencies, no subscriptions)
Query: entity-first traversal with time filtering

This is what competes with Zep's temporal knowledge graph.
Zep uses Neo4j in the cloud ($25/mo+). We use SQLite locally (free).

Usage:
    from mempalace.knowledge_graph import KnowledgeGraph

    kg = KnowledgeGraph()
    kg.add_triple("Max", "child_of", "Alice", valid_from="2015-04-01")
    kg.add_triple("Max", "does", "swimming", valid_from="2025-01-01")
    kg.add_triple("Max", "loves", "chess", valid_from="2025-10-01")

    # Query: everything about Max
    kg.query_entity("Max")

    # Query: what was true about Max in January 2026?
    kg.query_entity("Max", as_of="2026-01-15")

    # Query: who is connected to Alice?
    kg.query_entity("Alice", direction="both")

    # Invalidate: Max's sports injury resolved
    kg.invalidate("Max", "has_issue", "sports_injury", ended="2026-02-15")
"""

import json
import re
import sqlite3
import threading
import unicodedata
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Optional

from mempalace.sqlite_crypto import connect, row_factory

from . import fact_provenance, paths, provenance
from .config import DEFAULT_SINGLE_VALUED_PREDICATES, sanitize_iso_temporal
from .ids import make_triple_id
from .timeutil import utc_now_iso, utc_now_z

DEFAULT_KG_PATH = paths.legacy_knowledge_graph_path()
_MIN_ENTITY_CANDIDATE_LEN = 3
_FACT_SOURCES_BACKFILL_VERSION = 1


def knowledge_graph_notice(
    palace_path: str, legacy: Optional[str] = None, explicit: bool = False
) -> Optional[str]:
    """Say so when a graph that belongs to ``palace_path`` holds facts it does not read.

    That happens when the palace's own graph and the one it used before both
    hold facts: the palace reads its own, and this names the other file and the
    explicit command that copies its facts in. Graphs are never merged
    automatically.
    """
    used = paths.knowledge_graph_path(palace_path, legacy=legacy, explicit=explicit)
    other = paths.historical_knowledge_graph_path(palace_path, legacy=legacy, explicit=explicit)
    if other is None or paths.same_path(other, used) or not paths.knowledge_graph_has_rows(other):
        return None
    return (
        f"The knowledge graph at {other} holds facts this palace does not read; it reads "
        f"{used}. Graphs are never merged automatically; review and copy them in with "
        f"`mempalace kg import-legacy --from {other}`."
    )


def open_graph(path: str, read: bool = False) -> "KnowledgeGraph":
    """The graph at ``path``; for a read, an empty in-memory one when there is no file.

    A read never creates the graph file: an empty file would then be taken
    for the palace's graph.
    """
    if read and not Path(path).is_file():
        return KnowledgeGraph.empty()
    return KnowledgeGraph(db_path=path)


def _read_only(path: str) -> sqlite3.Connection:
    conn = connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = row_factory
    return conn


def import_graph_rows(source_path: str, target_path: str, apply: bool = False) -> dict:
    """Copy facts and their evidence in one transaction, preserving target history.

    Identity is subject/predicate/object, wing and validity window. An open
    identical target may also acquire a source's superseded end, subject to
    person-approval safety. Successor links and source observations use target
    triple ids, never an unchecked id from the source graph. Unresolvable links
    are omitted and reported with both ids and a count.

    Current-value and reused-id conflicts are reported rather than overwritten.
    Preview uses an in-memory target snapshot, including legacy migrations;
    neither stored graph changes and a missing target is not created.
    """
    if paths.same_path(source_path, target_path):
        raise ValueError("the source and the target are the same knowledge graph")
    source = _read_only(source_path)
    target = None
    kg = None

    try:
        # Keep all passes on the same source snapshot, even if its writer appends.
        source.execute("BEGIN")
        if apply:
            kg = KnowledgeGraph(db_path=target_path)
        else:
            kg = KnowledgeGraph.empty()
            if Path(target_path).is_file():
                existing = _read_only(target_path)
                try:
                    existing.backup(kg._conn())
                finally:
                    existing.close()
                # Applying opens KnowledgeGraph and migrates legacy schemas.
                # Preview those same changes only in the in-memory copy.
                kg._init_db()
        target = kg._conn()
        target.execute("BEGIN IMMEDIATE")
        with target:
            kg._backfill_fact_sources(target, writing=True)
            entities = {"added": 0, "present": 0}
            for row in _graph_import_rows(source, "entities"):
                if target.execute("SELECT 1 FROM entities WHERE id=?", (row["id"],)).fetchone():
                    entities["present"] += 1
                else:
                    _insert_import_row(target, "entities", row)
                    entities["added"] += 1
            rows = _graph_import_rows(source, "triples")
            triples, id_map = _import_triples(target, rows)
            sources = _import_fact_sources(source, target, rows, id_map)
        return {"entities": entities, "triples": triples, "fact_sources": sources}
    finally:
        source.close()
        if kg is not None:
            kg.close()


def _graph_import_rows(conn, table):
    available = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    columns = [c for c in KnowledgeGraph._REPLICATION_TABLES[table] if c in available]
    if "id" not in columns:
        return []
    return [
        dict(r) for r in conn.execute(f"SELECT {', '.join(columns)} FROM {table} ORDER BY rowid")
    ]


def _insert_import_row(conn, table, row):
    inserted = conn.execute(
        f"INSERT INTO {table} ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})",
        tuple(row.values()),
    ).rowcount
    if inserted != 1:
        raise sqlite3.IntegrityError(
            f"knowledge graph import did not insert {table} row {row.get('id')!r}"
        )


_TRIPLE_IDENTITY = ("subject", "predicate", "object", "valid_from", "valid_to", "wing")


def _same_triple_instance(conn, row, candidate, source_ids):
    if candidate["id"] == row.get("id"):
        return True
    return (
        candidate["id"] not in source_ids
        and not conn.execute("SELECT 1 FROM triples WHERE id=?", (row.get("id"),)).fetchone()
    )


def _identical_triples(conn, row, *, source_ids=(), allow_superseded_end=False):
    columns = [c for c in _TRIPLE_IDENTITY if c != "valid_to"]
    where = " AND ".join(f"{c} IS ?" for c in columns)
    args = [row.get(c) for c in columns]
    exact = conn.execute(
        f"SELECT * FROM triples WHERE {where} AND valid_to IS ? ORDER BY id=? DESC, id LIMIT 1",
        (*args, row.get("valid_to"), row.get("id")),
    ).fetchall()
    if exact or not (
        allow_superseded_end and row.get("status") == "superseded" and row.get("valid_to")
    ):
        return exact if exact and _same_triple_instance(conn, row, exact[0], source_ids) else []
    # A reassertion can share the old fact's start (including NULL). An exact
    # history row always wins; an ambiguous open instance is never reconciled.
    opened = conn.execute(
        f"SELECT * FROM triples WHERE {where} AND valid_to IS NULL AND status='active' LIMIT 2",
        args,
    ).fetchall()
    if len(opened) != 1:
        return []
    # A remap is safe only when neither graph already identifies the other
    # instance. This applies to exact matches and open fallbacks alike.
    if not _same_triple_instance(conn, row, opened[0], source_ids):
        return []
    return opened


def _import_triples(conn, rows):
    numbers = {
        "added": 0,
        "present": 0,
        "superseded": 0,
        "conflicts": [],
        "id_conflicts": [],
        "unresolved_link_count": 0,
        "unresolved_links": [],
        "end_not_carried": [],
    }
    by_id = {r["id"]: r for r in rows}
    source_ids = set(by_id)
    native_ids = {
        source_id
        for source_id in source_ids
        if conn.execute("SELECT 1 FROM triples WHERE id=?", (source_id,)).fetchone()
    }
    id_map, links, pending_ends = {}, [], {}
    claimed, duplicates = {}, {}
    # Stage history first. A current successor may replace only the open rows
    # waiting for that very successor; carry their ends after it is accepted.
    # Prefer native instances so duplicate aliases also work when the target
    # holds a later duplicate's id rather than the first source row's id.
    for raw in sorted(rows, key=lambda r: (r.get("valid_to") is None, r["id"] not in native_ids)):
        row = {**raw, **fact_provenance.imported_fields(raw)}
        duplicate_key = tuple(row.get(c) for c in (*_TRIPLE_IDENTITY, "status", "superseded_by"))
        duplicate_id = duplicates.get(duplicate_key)
        if (
            duplicate_id is not None
            and not conn.execute("SELECT 1 FROM triples WHERE id=?", (row["id"],)).fetchone()
        ):
            # Legacy duplicates in this source alias the accepted instance for
            # observations only. Never stage another end or copy fact metadata.
            id_map[row["id"]] = id_map[duplicate_id]
            numbers["present"] += 1
            _carry_imported_ends(conn, pending_ends, id_map, numbers, row["id"])
            continue
        same = _identical_triples(conn, row, source_ids=source_ids, allow_superseded_end=True)
        if same:
            existing = same[0]
            if existing["id"] in claimed:
                numbers["conflicts"].append(
                    {
                        **{k: row[k] for k in ("subject", "predicate", "object")},
                        "id": row["id"],
                        "target_id": existing["id"],
                        "current_object": existing["object"],
                        "reason": "target_already_claimed",
                        "claimed_by": claimed[existing["id"]],
                    }
                )
                continue
            claimed[existing["id"]] = row["id"]
            id_map[row["id"]] = existing["id"]
            duplicates.setdefault(duplicate_key, row["id"])
            _merge_imported_triple(conn, existing, row, by_id, numbers, links, pending_ends)
            _carry_imported_ends(conn, pending_ends, id_map, numbers, row.get("superseded_by"))
            _carry_imported_ends(conn, pending_ends, id_map, numbers, row["id"])
            numbers["present"] += 1
            continue
        if conn.execute("SELECT 1 FROM triples WHERE id=?", (row["id"],)).fetchone():
            numbers["id_conflicts"].append(row["id"])
            continue
        clash = None
        if row.get("valid_to") is None:
            waiting = {target_id for _, target_id in pending_ends.get(row["id"], [])}
            rivals = conn.execute(
                "SELECT id, object FROM triples WHERE subject=? AND predicate=? "
                "AND object != ? AND wing IS ? AND valid_to IS NULL",
                (row["subject"], row["predicate"], row["object"], row.get("wing")),
            ).fetchall()
            clash = next((r for r in rivals if r["id"] not in waiting), None)
        if clash:
            numbers["conflicts"].append(
                {
                    **{k: row[k] for k in ("subject", "predicate", "object")},
                    "current_object": clash["object"],
                }
            )
            continue
        successor = row.get("superseded_by")
        row["superseded_by"] = None
        _insert_import_row(conn, "triples", row)
        id_map[row["id"]] = row["id"]
        claimed[row["id"]] = row["id"]
        duplicates.setdefault(duplicate_key, row["id"])
        numbers["added"] += 1
        if successor:
            links.append((row["id"], row["id"], successor))
        _carry_imported_ends(conn, pending_ends, id_map, numbers, row["id"])
    for source_id, target_id, successor in links:
        mapped = id_map.get(successor)
        if mapped is None or mapped == target_id:
            numbers["unresolved_links"].append({"id": source_id, "superseded_by": successor})
            continue
        conn.execute("UPDATE triples SET superseded_by=? WHERE id=?", (mapped, target_id))
    numbers["unresolved_link_count"] = len(numbers["unresolved_links"])
    numbers["end_not_carried"].extend(
        {"id": row["id"], "target_id": target_id, "superseded_by": successor}
        for successor, ends in pending_ends.items()
        for row, target_id in ends
    )
    return numbers, id_map


def _carry_imported_ends(conn, pending, id_map, numbers, successor):
    mapped = id_map.get(successor)
    if mapped is None:
        return
    for row, target_id in pending.pop(successor, []):
        if mapped == target_id:
            numbers["end_not_carried"].append(
                {"id": row["id"], "target_id": target_id, "superseded_by": successor}
            )
            continue
        conn.execute(
            "UPDATE triples SET status='superseded', valid_to=?, superseded_by=? WHERE id=?",
            (row.get("valid_to"), mapped, target_id),
        )
        numbers["superseded"] += 1


def _merge_imported_triple(conn, existing, row, by_id, numbers, links, pending_ends):
    raised = strictest(existing["sensitivity"], row.get("sensitivity"))
    if raised != existing["sensitivity"]:
        conn.execute("UPDATE triples SET sensitivity=? WHERE id=?", (raised, existing["id"]))
    ending = row.get("status") == "superseded" and existing["status"] == "active"
    if ending:
        successor = by_id.get(row.get("superseded_by"))
        approved_end = row["approval_status"] == "approved" and (
            successor is None
            or fact_provenance.imported_fields(successor)["approval_status"] == "approved"
        )
        if existing["approval_status"] == "approved" and not approved_end:
            numbers["conflicts"].append(
                {
                    **{k: row[k] for k in ("subject", "predicate", "object")},
                    "id": row["id"],
                    "target_id": existing["id"],
                    "current_object": existing["object"],
                    "reason": "approved_target",
                }
            )
            return
        pending_ends.setdefault(row.get("superseded_by"), []).append((row, existing["id"]))
    fact_provenance.merge_imported_approval(conn, existing["id"], row)
    if (
        row.get("superseded_by")
        and not existing["superseded_by"]
        and existing["status"] == "superseded"
    ):
        links.append((row["id"], existing["id"], row["superseded_by"]))


def _import_fact_sources(source, target, triples, id_map):
    numbers = {"added": 0, "present": 0, "skipped": []}
    rows = _graph_import_rows(source, "fact_sources")
    if not source.execute("PRAGMA table_info(fact_sources)").fetchall():
        # Old peers have only the first observation on each triple. Preserve
        # that pointer, with unknown evidence just like the legacy migration.
        rows = [
            {
                **{
                    k: r.get(k)
                    for k in (
                        "source_thread",
                        "source_seq_start",
                        "source_seq_end",
                        "source_drawer_id",
                        "source_file",
                    )
                },
                "id": r["id"],
                "triple_id": r["id"],
                "added_at": r.get("extracted_at") or utc_now_iso(),
            }
            for r in triples
            if any(
                r.get(k) is not None for k in ("source_thread", "source_drawer_id", "source_file")
            )
        ]
    for row in rows:
        target_id = id_map.get(row["triple_id"])
        if target_id is None:
            numbers["skipped"].append(row["id"])
            continue
        added = _merge_imported_source(target, row, target_id)
        numbers["added" if added else "present"] += 1
    return numbers


def _merge_imported_source(conn, row, triple_id):
    """Graph-local observation ids never cross the import/replication boundary."""
    fields = {
        k: row[k]
        for k in KnowledgeGraph._REPLICATION_TABLES["fact_sources"]
        if k in row and k not in ("id", "triple_id")
    }
    fields.update(fact_provenance.imported_fields(row))
    fields["triple_id"] = triple_id
    inserted = conn.execute(
        f"INSERT INTO fact_sources ({', '.join(fields)}) "
        f"VALUES ({', '.join('?' for _ in fields)}) ON CONFLICT DO NOTHING",
        tuple(fields.values()),
    ).rowcount
    if not inserted and fields["approval_status"] == "approved":
        key = (
            "triple_id",
            "source_thread",
            "source_seq_start",
            "source_seq_end",
            "source_drawer_id",
            "source_file",
        )
        # Match the expression-based unique key, including its NULL sentinels.
        where = " AND ".join(f"COALESCE({k}, ?) = COALESCE(?, ?)" for k in key)
        args = []
        for k in key:
            default = -1 if k in ("source_seq_start", "source_seq_end") else ""
            args.extend((default, fields.get(k), default))
        conn.execute(
            "UPDATE fact_sources SET approval_status='approved', approved_by=?, approval_link=? "
            "WHERE approval_status != 'approved' AND " + where,
            (fields["approved_by"], fields["approval_link"], *args),
        )
    fact_provenance.merge_imported_approval(conn, triple_id, fields)
    return inserted == 1


_REPLICATED_AUTHORSHIP = {
    "origin_kind",
    "origin_id",
    "origin_link",
    "source_closet",
    "source_file",
    "source_drawer_id",
    "adapter_name",
    "extracted_at",
    "source_thread",
    "source_seq_start",
    "source_seq_end",
}


def _replicated_lifecycle_regresses(existing, row):
    if existing is None:
        return False
    # Once an end or successor is known it is history, not a peer-editable
    # field. Missing keys from old peers still leave the stored value alone.
    for column in ("valid_to", "superseded_by"):
        if existing[column] is not None and column in row and row[column] != existing[column]:
            return True
    return (
        existing["status"] != "active"
        and "status" in row
        and (row["status"] or "active") != existing["status"]
    )


def _prepare_replicated_triple(conn, incoming):
    """Validate peer authority and links against the local row before any write."""
    row = dict(incoming)
    evidence = fact_provenance.imported_fields(row)
    for col in fact_provenance.COLUMNS:
        if col in row or ("approval_status" in row and col in ("approved_by", "approval_link")):
            row[col] = evidence[col]
    report = {"id": row["id"], "applied": True, "unresolved_links": []}
    existing = conn.execute("SELECT * FROM triples WHERE id=?", (row["id"],)).fetchone()
    if existing and any(
        c in row and row[c] != existing[c] for c in _TRIPLE_IDENTITY if c != "valid_to"
    ):
        return row, {**report, "applied": False, "reason": "id_conflict"}
    successor_id = row.get("superseded_by")
    successor = (
        conn.execute("SELECT * FROM triples WHERE id=?", (successor_id,)).fetchone()
        if successor_id and successor_id != row["id"]
        else None
    )
    if successor_id and successor is None:
        report["unresolved_links"].append({"id": row["id"], "superseded_by": successor_id})
    if _replicated_lifecycle_regresses(existing, row):
        return row, {**report, "applied": False, "reason": "lifecycle_regression"}
    lifecycle_changed = existing is not None and any(
        c in row and row[c] != existing[c] for c in ("status", "valid_to", "superseded_by")
    )
    approved_change = evidence["approval_status"] == "approved" and (
        successor is None
        or fact_provenance.imported_fields(dict(successor))["approval_status"] == "approved"
    )
    if (
        existing
        and existing["approval_status"] == "approved"
        and lifecycle_changed
        and not approved_change
    ):
        return row, {**report, "applied": False, "reason": "approved_target"}
    closes_open = (
        existing is not None
        and existing["valid_to"] is None
        and (row.get("valid_to") is not None or row.get("status", "active") != "active")
    )
    if closes_open and successor is None and (successor_id or row.get("status") == "superseded"):
        return row, {
            **report,
            "applied": False,
            "reason": "unresolved_successor",
            "end_not_carried": {"id": row["id"], "superseded_by": successor_id},
        }
    if successor_id and successor is None:
        if existing:
            row.pop("superseded_by")  # Preserve any already recorded local link.
        else:
            row["superseded_by"] = None
    return row, report


def _upsert_replicated_row(conn, table, row, columns):
    sent = [col for col in columns if col in row]
    defaults = {
        "status": "active",
        "sensitivity": DEFAULT_SENSITIVITY,
        "origin_kind": "unknown",
        "origin_id": "unknown",
        "approval_status": "unknown",
    }
    values = [row[col] if row[col] is not None else defaults.get(col) for col in sent]
    updates = []
    for col in sent:
        if col == "id" or (table == "triples" and col in _REPLICATED_AUTHORSHIP):
            continue
        if col == "sensitivity" and table == "triples":
            updates.append(f"sensitivity = {_STRICTER_SENSITIVITY}")
        elif table == "triples" and col in ("approval_status", "approved_by", "approval_link"):
            updates.append(
                f"{col} = CASE WHEN triples.approval_status='approved' "
                f"THEN triples.{col} ELSE excluded.{col} END"
            )
        else:
            updates.append(f"{col} = excluded.{col}")
    conflict = "DO UPDATE SET " + ", ".join(updates) if updates else "DO NOTHING"
    conn.execute(
        f"INSERT INTO {table} ({', '.join(sent)}) VALUES ({', '.join('?' for _ in sent)}) "
        f"ON CONFLICT(id) {conflict}",
        values,
    )


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def canonical_entity_name(name: str) -> str:
    """NFC, lower-cased name with whitespace runs collapsed.

    ``lower()``, not ``casefold()``: casefold maps "ß" to "ss" and would merge
    distinct names.
    """
    text = unicodedata.normalize("NFC", name or "").lower().strip()
    return re.sub(r"\s+", " ", text)


def entity_id_for_name(name: str) -> str:
    """Id that keeps punctuation, so distinct names stay distinct.

    Whitespace is an underscore; an underscore that was part of the name is
    ``%5F`` and a percent sign is ``%25``, so no two canonical names share an id.
    """
    text = canonical_entity_name(name).replace("%", "%25").replace("_", "%5F")
    return text.replace(" ", "_")


def legacy_entity_id(name: str) -> str:
    """Id written before punctuation was kept. It stripped apostrophes and
    turned spaces into underscores, so distinct names could share one."""
    return (name or "").lower().replace(" ", "_").replace("'", "")


# Lifecycle of a fact record. ``status`` says whether a fact was replaced;
# ``valid_to`` (``current`` on reads) says whether it still holds in time. An
# invalidated fact ended but was never replaced, so it stays ``active``: a
# status that depended on the clock would go stale on its own.
FACT_STATUSES = ("active", "superseded", "retracted", "expired")

# Predicates that hold one value at a time: saving a new value replaces the
# open one (see :meth:`KnowledgeGraph.save_fact`). ``single_valued_predicates``
# in config.json adds more.
SINGLE_VALUED_PREDICATES = DEFAULT_SINGLE_VALUED_PREDICATES
_MAX_SOURCE_THREAD_LEN = 256


def _seq(value, field: str) -> Optional[int]:
    if value is None or value == "":
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        number = value
    elif isinstance(value, float) and value.is_integer():
        number = int(value)
    elif isinstance(value, str) and value.strip().isdecimal():
        number = int(value.strip())
    else:
        raise ValueError(f"{field} must be a whole message number")
    if number < 0:
        raise ValueError(f"{field} must not be negative")
    return number


def source_range(thread=None, seq_start=None, seq_end=None) -> tuple:
    """Validate a fact's pointer back to the chat it came from.

    Returns ``(thread, seq_start, seq_end)``. A range needs a thread; one
    message number alone means that single message. All three may be
    ``None`` (no source).
    """
    if thread is not None and not isinstance(thread, str):
        raise ValueError("source_thread must be a string")
    thread = (thread or "").strip() or None
    if thread is not None:
        if len(thread) > _MAX_SOURCE_THREAD_LEN:
            raise ValueError("source_thread is too long")
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in thread):
            raise ValueError("source_thread contains control characters")
    start = _seq(seq_start, "source_seq_start")
    end = _seq(seq_end, "source_seq_end")
    if start is None and end is not None:
        start = end
    elif end is None and start is not None:
        end = start
    if start is not None:
        if thread is None:
            raise ValueError("a message range needs source_thread")
        if end < start:
            raise ValueError("source_seq_end is before source_seq_start")
    return thread, start, end


# How far a fact may travel, least to most restricted. A reader sees a fact
# only if it may read the fact's wing and its sensitivity.
FACT_SENSITIVITIES = ("public", "internal", "private", "secret")
DEFAULT_SENSITIVITY = "internal"


def fact_wing(wing) -> Optional[str]:
    """A fact's wing, or ``None`` for a fact filed before facts had wings."""
    if wing is None:
        return None
    if not isinstance(wing, str):
        raise ValueError("wing must be a string")
    wing = wing.strip()
    if not wing:
        return None
    if len(wing) > 128 or any(ord(ch) < 32 or ord(ch) == 127 for ch in wing):
        raise ValueError("wing must be a short name without control characters")
    return wing


def fact_sensitivity(value) -> str:
    if value is None or value == "":
        return DEFAULT_SENSITIVITY
    if value not in FACT_SENSITIVITIES:
        raise ValueError(f"sensitivity must be one of {', '.join(FACT_SENSITIVITIES)}")
    return value


def strictest(*values) -> str:
    """The most restricted of ``values`` (unset ones count as the default)."""
    return max((fact_sensitivity(v) for v in values), key=FACT_SENSITIVITIES.index)


def visibility(wings=None, sensitivities=None) -> tuple:
    """Validate a read filter. ``None`` means no filter; an empty list matches nothing."""
    out = []
    for value, name, check in (
        (wings, "wings", fact_wing),
        (sensitivities, "sensitivities", fact_sensitivity),
    ):
        if value is None:
            out.append(None)
            continue
        if isinstance(value, str) or not isinstance(value, (list, tuple)):
            raise ValueError(f"{name} must be a list")
        items = []
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise ValueError(f"{name} must be a list of names")
            items.append(check(item))
        out.append(sorted(set(items)))
    return tuple(out)


def _sensitivity_rank(column: str) -> str:
    cases = " ".join(f"WHEN '{name}' THEN {i}" for i, name in enumerate(FACT_SENSITIVITIES))
    return f"(CASE {column} {cases} ELSE 1 END)"


# SQL for an upsert: keep the stricter of the stored and the incoming sensitivity.
_STRICTER_SENSITIVITY = (
    f"CASE WHEN {_sensitivity_rank('excluded.sensitivity')} > "
    f"{_sensitivity_rank('triples.sensitivity')} "
    "THEN excluded.sensitivity ELSE triples.sensitivity END"
)


def _visibility_sql(wings, sensitivities, alias: str = "t") -> tuple:
    """``AND`` clauses for a read filter. A fact with no wing passes no wing filter."""
    sql, params = "", []
    for column, allowed in (("wing", wings), ("sensitivity", sensitivities)):
        if allowed is None:
            continue
        if not allowed:
            sql += " AND 0"
            continue
        sql += f" AND {alias}.{column} IN ({', '.join('?' for _ in allowed)})"
        params.extend(allowed)
    return sql, params


def _fact_fields(row) -> dict:
    """The id, wing, status and chat pointer every fact read returns."""
    return {
        "id": row["id"],
        "wing": row["wing"],
        "sensitivity": row["sensitivity"] or DEFAULT_SENSITIVITY,
        "status": row["status"] or "active",
        "superseded_by": row["superseded_by"],
        "seen_count": row["seen_count"] or 1,
        "source_thread": row["source_thread"],
        "source_seq_start": row["source_seq_start"],
        "source_seq_end": row["source_seq_end"],
        **fact_provenance.fields(row),
    }


def _drop_none(props: dict) -> dict:
    return {
        k: _drop_none(v) if isinstance(v, dict) else v for k, v in props.items() if v is not None
    }


def _is_date_only_temporal(value: str) -> bool:
    return isinstance(value, str) and len(value) == 10 and value[4] == "-" and value[7] == "-"


def _temporal_start_key(value: Optional[str]) -> Optional[str]:
    """Return the comparable instant for a valid_from/as_of value."""

    if value is None:
        return None

    if _is_date_only_temporal(value):
        return f"{value}T00:00:00Z"

    return value


def _temporal_end_key(value: Optional[str]) -> Optional[str]:
    """Return the comparable instant for a valid_to value.

    Date-only valid_to values represent the whole day for backward
    compatibility with existing KG facts.
    """

    if value is None:
        return None

    if _is_date_only_temporal(value):
        return f"{value}T23:59:59Z"

    return value


def _sql_temporal_start_expr(column: str) -> str:
    """SQLite expression for comparing valid_from-style temporal values."""

    return (
        f"CASE WHEN length({column}) = 10 "
        f"AND substr({column}, 5, 1) = '-' "
        f"AND substr({column}, 8, 1) = '-' "
        f"THEN {column} || 'T00:00:00Z' ELSE {column} END"
    )


def _sql_temporal_end_expr(column: str) -> str:
    """SQLite expression for comparing valid_to-style temporal values."""

    return (
        f"CASE WHEN length({column}) = 10 "
        f"AND substr({column}, 5, 1) = '-' "
        f"AND substr({column}, 8, 1) = '-' "
        f"THEN {column} || 'T23:59:59Z' ELSE {column} END"
    )


def _temporal_filter_sql(as_of: str) -> tuple[str, list[str]]:
    """Return SQL and parameters for an as-of temporal filter.

    Date-only KG values are normalized for comparison:

    - valid_from='2026-05-06' compares as '2026-05-06T00:00:00Z'
    - valid_to='2026-05-06' compares as '2026-05-06T23:59:59Z'

    This keeps legacy date-only facts working when callers query with
    canonical UTC datetimes such as '2026-05-06T15:00:00Z'.

    The upper bound is *strict* (``valid_to > as_of``): a fact whose
    ``valid_to`` equals the query instant has already ended at that instant,
    so the interval is treated as half-open ``[valid_from, valid_to)``. This
    is what lets a fact and its successor share a boundary instant without an
    as-of query returning both. Date-only ``valid_to`` still expands to the
    end of that day (``T23:59:59Z``), so a standalone date-only fact stays
    valid through its whole final day exactly as before.
    """

    as_of_key = _temporal_start_key(as_of)
    valid_from_expr = _sql_temporal_start_expr("t.valid_from")
    valid_to_expr = _sql_temporal_end_expr("t.valid_to")

    return (
        f" AND (t.valid_from IS NULL OR {valid_from_expr} <= ?) "
        f"AND (t.valid_to IS NULL OR {valid_to_expr} > ?)",
        [as_of_key, as_of_key],
    )


class KnowledgeGraph:
    def __init__(self, db_path: str = None):
        self.db_path = db_path or DEFAULT_KG_PATH
        db_parent = Path(self.db_path).parent
        db_parent.mkdir(parents=True, exist_ok=True)
        try:
            db_parent.chmod(0o700)
        except (OSError, NotImplementedError):
            pass
        self._connection = None
        self._lock = threading.Lock()
        self._init_db()

    @classmethod
    def empty(cls) -> "KnowledgeGraph":
        """An in-memory graph with no facts, for reading a palace that has none yet."""
        kg = cls.__new__(cls)
        kg.db_path = ":memory:"
        kg._connection = None
        kg._lock = threading.Lock()
        kg._init_db()
        return kg

    def _init_db(self):
        conn = self._conn()
        new_graph = (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='triples'"
            ).fetchone()
            is None
        )
        conn.executescript("""
            PRAGMA journal_mode=WAL;

            CREATE TABLE IF NOT EXISTS entities (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                type TEXT DEFAULT 'unknown',
                properties TEXT DEFAULT '{}',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS triples (
                id TEXT PRIMARY KEY,
                subject TEXT NOT NULL,
                predicate TEXT NOT NULL,
                object TEXT NOT NULL,
                valid_from TEXT,
                valid_to TEXT,
                confidence REAL DEFAULT 1.0,
                source_closet TEXT,
                source_file TEXT,
                source_drawer_id TEXT,
                adapter_name TEXT,
                extracted_at TEXT DEFAULT CURRENT_TIMESTAMP,
                hub_stamp TEXT,
                source_thread TEXT,
                source_seq_start INTEGER,
                source_seq_end INTEGER,
                status TEXT NOT NULL DEFAULT 'active',
                superseded_by TEXT,
                wing TEXT,
                sensitivity TEXT NOT NULL DEFAULT 'internal',
                seen_count INTEGER NOT NULL DEFAULT 1,
                last_seen_at TEXT,
                FOREIGN KEY (subject) REFERENCES entities(id),
                FOREIGN KEY (object) REFERENCES entities(id)
            );

            CREATE INDEX IF NOT EXISTS idx_triples_subject ON triples(subject);
            CREATE INDEX IF NOT EXISTS idx_triples_object ON triples(object);
            CREATE INDEX IF NOT EXISTS idx_triples_predicate ON triples(predicate);
            CREATE INDEX IF NOT EXISTS idx_triples_valid ON triples(valid_from, valid_to);

            -- Every chat range or drawer a fact was seen in. The first is
            -- also on the triple; save_fact adds the rest when it merges.
            CREATE TABLE IF NOT EXISTS fact_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                triple_id TEXT NOT NULL,
                source_thread TEXT,
                source_seq_start INTEGER,
                source_seq_end INTEGER,
                source_drawer_id TEXT,
                source_file TEXT,
                added_at TEXT NOT NULL,
                hub_stamp TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_fact_sources_triple ON fact_sources(triple_id);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_fact_sources_once ON fact_sources(
                triple_id,
                COALESCE(source_thread, ''),
                COALESCE(source_seq_start, -1),
                COALESCE(source_seq_end, -1),
                COALESCE(source_drawer_id, ''),
                COALESCE(source_file, '')
            );
        """)
        self._migrate_schema(conn)
        from .fact_index import try_install

        self._fact_index_ready = try_install(conn)
        with conn:
            self._backfill_fact_sources(conn, writing=new_graph)

    def _backfill_fact_sources(self, conn, *, writing=False):
        if conn.execute("PRAGMA user_version").fetchone()[0] >= _FACT_SOURCES_BACKFILL_VERSION:
            return
        missing = (
            "(t.source_thread IS NOT NULL OR t.source_drawer_id IS NOT NULL "
            "OR t.source_file IS NOT NULL) AND NOT EXISTS "
            "(SELECT 1 FROM fact_sources WHERE triple_id = t.id)"
        )
        probe = "SELECT 1 FROM triples t WHERE " + missing + " LIMIT 1"
        pending = conn.execute(probe).fetchone() is not None
        if not pending and not writing:
            # An already-sourced, unmarked graph stays read-only on open. Its
            # next normal write records completion before accepting new facts.
            return
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        # Another opener may have completed the migration while we waited.
        if conn.execute("PRAGMA user_version").fetchone()[0] >= _FACT_SOURCES_BACKFILL_VERSION:
            return
        if pending or conn.execute(probe).fetchone() is not None:
            conn.execute(
                "INSERT OR IGNORE INTO fact_sources (triple_id, source_thread, "
                "source_seq_start, source_seq_end, source_drawer_id, source_file, "
                "added_at, hub_stamp) SELECT t.id, t.source_thread, t.source_seq_start, "
                "t.source_seq_end, t.source_drawer_id, t.source_file, "
                "COALESCE(t.extracted_at, ?), t.hub_stamp FROM triples t WHERE " + missing,
                (utc_now_iso(),),
            )
        # This marker and the recovered sources commit or roll back together.
        conn.execute(f"PRAGMA user_version = {_FACT_SOURCES_BACKFILL_VERSION}")

    @contextmanager
    def _write(self, conn):
        with conn:
            self._backfill_fact_sources(conn, writing=True)
            yield

    def _migrate_schema(self, conn):
        """Backwards-compatible schema migration for older triples tables.

        Fresh palaces get ``source_drawer_id`` / ``adapter_name`` (RFC 002 §5.5)
        directly from the canonical ``CREATE TABLE`` above, so this path is a
        no-op on new installs. It exists for palaces that were created before
        those columns were added: SQLite has no ``ADD COLUMN IF NOT EXISTS``,
        so we introspect the schema and only issue the ALTER when the column
        is missing.
        """
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(triples)")}
        if "source_drawer_id" not in existing:
            conn.execute("ALTER TABLE triples ADD COLUMN source_drawer_id TEXT")
        if "adapter_name" not in existing:
            conn.execute("ALTER TABLE triples ADD COLUMN adapter_name TEXT")
        if "hub_stamp" not in existing:
            conn.execute("ALTER TABLE triples ADD COLUMN hub_stamp TEXT")
        for column, decl in (
            ("source_thread", "TEXT"),
            ("source_seq_start", "INTEGER"),
            ("source_seq_end", "INTEGER"),
            ("status", "TEXT NOT NULL DEFAULT 'active'"),
            ("superseded_by", "TEXT"),
            ("wing", "TEXT"),
            ("sensitivity", "TEXT NOT NULL DEFAULT 'internal'"),
            ("seen_count", "INTEGER NOT NULL DEFAULT 1"),
            ("last_seen_at", "TEXT"),
        ):
            if column not in existing:
                conn.execute(f"ALTER TABLE triples ADD COLUMN {column} {decl}")
        for column, decl in fact_provenance.COLUMNS.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE triples ADD COLUMN {column} {decl}")
        source_columns = {r["name"] for r in conn.execute("PRAGMA table_info(fact_sources)")}
        for column, decl in fact_provenance.COLUMNS.items():
            if column not in source_columns:
                conn.execute(f"ALTER TABLE fact_sources ADD COLUMN {column} {decl}")
        # Indexes on migrated columns live here: in the CREATE script above
        # they would fail on a legacy table that does not have them yet.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_triples_status ON triples(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_triples_wing ON triples(wing)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_triples_source_thread "
            "ON triples(source_thread, source_seq_start)"
        )

    def _conn(self):
        if self._connection is None:
            self._connection = connect(self.db_path, timeout=10, check_same_thread=False)
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.row_factory = row_factory
        return self._connection

    def close(self):
        """Close the database connection."""
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def __enter__(self):
        """Allow KnowledgeGraph to be used as a context manager."""
        return self

    def __exit__(self, exc_type, exc, tb):
        """Close the SQLite connection when leaving a context manager block."""
        self.close()
        return False

    def _resolve_entity_id(self, conn, name: str) -> str:
        """Id for ``name``: its own id, else a legacy row it used to map to.

        Stored ids are never rewritten. A legacy row is reused only when its
        stored name no longer owns that id, so a name written after this
        change (``OBrien``) never absorbs a different one (``O'Brien``).
        """
        eid = entity_id_for_name(name)
        if conn.execute("SELECT 1 FROM entities WHERE id = ?", (eid,)).fetchone():
            return eid
        legacy = legacy_entity_id(name)
        if legacy != eid:
            row = conn.execute("SELECT name FROM entities WHERE id = ?", (legacy,)).fetchone()
            if row is not None and entity_id_for_name(row["name"]) != legacy:
                return legacy
        return eid

    # ── Write operations ──────────────────────────────────────────────────

    def add_entity(self, name: str, entity_type: str = "unknown", properties: dict = None):
        """Add an entity, or merge into the existing node.

        The stored name takes the latest spelling, a type other than
        ``unknown`` replaces the stored one, and properties merge key by key.
        A ``None`` value leaves the stored key as it is. A legacy row keeps
        its stored name: renaming it would change which spellings reach it.
        """
        props = json.dumps(_drop_none(properties or {}))
        with self._lock:
            conn = self._conn()
            with self._write(conn):
                eid = self._resolve_entity_id(conn, name)
                row = conn.execute("SELECT name FROM entities WHERE id = ?", (eid,)).fetchone()
                rename = row is None or entity_id_for_name(row["name"]) == eid
                conn.execute(
                    """INSERT INTO entities (id, name, type, properties)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(id) DO UPDATE SET
                         name = CASE WHEN ? THEN excluded.name ELSE entities.name END,
                         type = COALESCE(NULLIF(excluded.type, 'unknown'), entities.type),
                         properties = json_patch(
                             COALESCE(entities.properties, '{}'), excluded.properties
                         )""",
                    (eid, name, entity_type, props, rename),
                )
        return eid

    def add_triple(
        self,
        subject: str,
        predicate: str,
        obj: str,
        valid_from: str = None,
        valid_to: str = None,
        confidence: float = 1.0,
        source_closet: str = None,
        source_file: str = None,
        source_drawer_id: str = None,
        adapter_name: str = None,
        source_thread: str = None,
        source_seq_start: int = None,
        source_seq_end: int = None,
        source_url: str = None,
        wing: str = None,
        sensitivity: str = None,
        sensitivities=None,
    ):
        """
        Add a relationship triple: subject → predicate → object.

        ``source_drawer_id`` and ``adapter_name`` are RFC 002 §5.5 provenance
        fields populated by adapters that advertise ``supports_kg_triples``;
        they default to ``None`` so every existing caller stays
        source-compatible.

        ``source_thread`` with ``source_seq_start``/``source_seq_end`` points
        the fact back at the chat messages it came from (see
        :func:`source_range`). The chat stays the record; the fact indexes it.

        ``wing`` scopes the fact: a fact is the same fact only within one
        wing, and wing-filtered reads never return another wing's facts.
        ``sensitivity`` is one of :data:`FACT_SENSITIVITIES`.
        ``sensitivities`` limits which stored facts count as the same one, so
        a caller never merges into (or learns of) a fact it may not read.

        Examples:
            add_triple("Max", "child_of", "Alice", valid_from="2015-04-01")
            add_triple("Max", "does", "swimming", valid_from="2025-01-01")
            add_triple("Alice", "worried_about", "Max injury", valid_from="2026-01-01")
        """

        valid_from = sanitize_iso_temporal(valid_from, "valid_from")
        valid_to = sanitize_iso_temporal(valid_to, "valid_to")
        source_thread, source_seq_start, source_seq_end = source_range(
            source_thread, source_seq_start, source_seq_end
        )
        fact_provenance.source_link(source_thread, source_url)
        wing = fact_wing(wing)
        sensitivity = fact_sensitivity(sensitivity)
        _, sensitivities = visibility(None, sensitivities)
        within_sql, within_params = _visibility_sql(None, sensitivities, alias="triples")

        # Reject inverted intervals. Use temporal comparison keys rather than
        # raw string comparison so legacy date-only values and canonical UTC
        # datetimes can safely coexist.
        if (
            valid_from is not None
            and valid_to is not None
            and _temporal_end_key(valid_to) < _temporal_start_key(valid_from)
        ):
            raise ValueError(
                f"valid_to={valid_to!r} is before valid_from={valid_from!r}; "
                "an inverted interval would be invisible to every KG query"
            )

        pred = predicate.lower().replace(" ", "_")

        # Auto-create entities if they don't exist
        with self._lock:
            conn = self._conn()
            with self._write(conn):
                sub_id = self._resolve_entity_id(conn, subject)
                obj_id = self._resolve_entity_id(conn, obj)
                conn.execute(
                    "INSERT OR IGNORE INTO entities (id, name) VALUES (?, ?)",
                    (sub_id, subject),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO entities (id, name) VALUES (?, ?)",
                    (obj_id, obj),
                )

                # Check for existing identical triple in the same wing
                existing = conn.execute(
                    "SELECT id FROM triples WHERE subject=? AND predicate=? AND object=? "
                    "AND wing IS ? AND valid_to IS NULL" + within_sql,
                    (sub_id, pred, obj_id, wing, *within_params),
                ).fetchone()
                if existing:
                    # Already exists and still valid: keep where it was seen again.
                    self._add_source(
                        conn,
                        existing["id"],
                        source_thread,
                        source_seq_start,
                        source_seq_end,
                        source_drawer_id,
                        source_file,
                        utc_now_iso(),
                        source_url,
                    )
                    fact_provenance.record(conn, existing["id"], source_thread, source_url)
                    return existing["id"]

                triple_id = make_triple_id(sub_id, pred, obj_id, valid_from, utc_now_iso())
                conn.execute(
                    """INSERT INTO triples (
                        id, subject, predicate, object, valid_from, valid_to,
                        confidence, source_closet, source_file,
                        source_drawer_id, adapter_name, hub_stamp,
                        source_thread, source_seq_start, source_seq_end,
                        wing, sensitivity
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        triple_id,
                        sub_id,
                        pred,
                        obj_id,
                        valid_from,
                        valid_to,
                        confidence,
                        source_closet,
                        source_file,
                        source_drawer_id,
                        adapter_name,
                        provenance.stamp_json(),
                        source_thread,
                        source_seq_start,
                        source_seq_end,
                        wing,
                        sensitivity,
                    ),
                )
                self._add_source(
                    conn,
                    triple_id,
                    source_thread,
                    source_seq_start,
                    source_seq_end,
                    source_drawer_id,
                    source_file,
                    utc_now_iso(),
                    source_url,
                )
                fact_provenance.record(conn, triple_id, source_thread, source_url, new=True)
                return triple_id

    def invalidate(
        self,
        subject: str,
        predicate: str,
        obj: str,
        ended: str = None,
        wing: str = None,
        sensitivities=None,
    ) -> int:
        """Mark a relationship in ``wing`` as no longer valid (set valid_to date/time).

        Only facts of ``sensitivities`` (``None``: any) are touched. Returns
        how many open facts were closed.
        """
        pred = predicate.lower().replace(" ", "_")
        ended = sanitize_iso_temporal(ended or date.today().isoformat(), "ended")
        wing = fact_wing(wing)
        _, sensitivities = visibility(None, sensitivities)
        within_sql, within_params = _visibility_sql(None, sensitivities, alias="triples")

        with self._lock:
            conn = self._conn()
            with self._write(conn):
                sub_id = self._resolve_entity_id(conn, subject)
                obj_id = self._resolve_entity_id(conn, obj)
                rows = conn.execute(
                    "SELECT id, valid_from, approval_status FROM triples "
                    "WHERE subject=? AND predicate=? AND object=? AND wing IS ? "
                    "AND valid_to IS NULL" + within_sql,
                    (sub_id, pred, obj_id, wing, *within_params),
                ).fetchall()

                if any(r["approval_status"] == "approved" for r in rows) and not (
                    provenance.current() or {}
                ).get("person"):
                    raise ValueError("only a signed-in person can invalidate an approved fact")
                for row in rows:
                    valid_from = row["valid_from"]
                    if valid_from is not None and _temporal_end_key(ended) < _temporal_start_key(
                        valid_from
                    ):
                        raise ValueError(
                            f"valid_to={ended!r} is before valid_from={valid_from!r}; "
                            "an inverted interval would be invisible to every KG query"
                        )

                return conn.execute(
                    "UPDATE triples SET valid_to=? "
                    "WHERE subject=? AND predicate=? AND object=? AND wing IS ? "
                    "AND valid_to IS NULL" + within_sql,
                    (ended, sub_id, pred, obj_id, wing, *within_params),
                ).rowcount

    def supersede(
        self,
        subject: str,
        predicate: str,
        old_obj: str,
        new_obj: str,
        at: str = None,
        confidence: float = 1.0,
        source_closet: str = None,
        source_file: str = None,
        source_drawer_id: str = None,
        adapter_name: str = None,
        source_thread: str = None,
        source_seq_start: int = None,
        source_seq_end: int = None,
        source_url: str = None,
        wing: str = None,
        sensitivity: str = None,
        sensitivities=None,
    ):
        """Atomically replace one fact with another at a single shared boundary.

        Closes the currently-open ``(subject, predicate, old_obj)`` triple with
        ``valid_to = at`` and opens ``(subject, predicate, new_obj)`` with
        ``valid_from = at`` in one transaction, at a single shared instant.
        Paired with the half-open upper bound in ``_temporal_filter_sql``, an
        as-of query at that instant returns only the successor.

        This is the primitive for a value change. Hand-rolling a handover as
        ``invalidate(ended=D)`` + ``add_triple(valid_from=D)`` with date-only
        ``D`` leaves two facts sharing the whole day ``D`` (``valid_to`` expands
        to ``T23:59:59Z`` while ``valid_from`` expands to ``T00:00:00Z``), so an
        as-of query on ``D`` returns both. ``supersede`` avoids this by writing
        one identical precise instant to both sides.

        ``at`` defaults to the current UTC instant. A date-only ``at`` is
        normalized to ``<date>T00:00:00Z`` so both sides carry the same precise
        value rather than the asymmetric whole-day expansion.

        The closed fact gets ``status = 'superseded'`` and ``superseded_by`` =
        the successor's id, so a reader can walk from an old value to the new.

        Only facts in ``wing`` (and of ``sensitivities``, when given) are
        replaced, and the successor lands there. The successor is at least as
        sensitive as the fact it replaces.

        Returns the new triple's id. If no open ``old_obj`` triple exists the
        successor is still opened, so ``supersede`` degrades to ``add_triple``.
        """
        if at is None:
            boundary = utc_now_z()
        elif _is_date_only_temporal(at):
            boundary = f"{at}T00:00:00Z"
        else:
            boundary = at
        boundary = sanitize_iso_temporal(boundary, "at")
        source_thread, source_seq_start, source_seq_end = source_range(
            source_thread, source_seq_start, source_seq_end
        )
        fact_provenance.source_link(source_thread, source_url)
        wing = fact_wing(wing)
        sensitivity = fact_sensitivity(sensitivity)
        _, sensitivities = visibility(None, sensitivities)
        within_sql, within_params = _visibility_sql(None, sensitivities, alias="triples")

        pred = predicate.lower().replace(" ", "_")

        with self._lock:
            conn = self._conn()
            with self._write(conn):
                sub_id = self._resolve_entity_id(conn, subject)
                old_id = self._resolve_entity_id(conn, old_obj)
                new_id = self._resolve_entity_id(conn, new_obj)
                # Only create entities we actually open a fact for. old_obj is
                # matched by id in the UPDATE below whether or not its row
                # exists, so inserting it would just orphan an entity when no
                # open old fact is present (the degrade-to-add path).
                for name, eid in ((subject, sub_id), (new_obj, new_id)):
                    conn.execute(
                        "INSERT OR IGNORE INTO entities (id, name) VALUES (?, ?)",
                        (eid, name),
                    )

                # Reject a boundary that precedes the old fact's start — an
                # inverted interval would be invisible to every KG query.
                rows = conn.execute(
                    "SELECT id, valid_from, sensitivity, approval_status FROM triples "
                    "WHERE subject=? AND predicate=? AND object=? AND wing IS ? "
                    "AND valid_to IS NULL" + within_sql,
                    (sub_id, pred, old_id, wing, *within_params),
                ).fetchall()
                if any(
                    r["approval_status"] == "approved" for r in rows
                ) and not fact_provenance.may_replace_approved(source_thread, source_url):
                    raise ValueError(
                        "only a signed-in person with a source can supersede an approved fact"
                    )
                sensitivity = strictest(sensitivity, *(r["sensitivity"] for r in rows))
                for row in rows:
                    valid_from = row["valid_from"]
                    if valid_from is not None and _temporal_end_key(boundary) < _temporal_start_key(
                        valid_from
                    ):
                        raise ValueError(
                            f"at={boundary!r} is before valid_from={valid_from!r}; "
                            "an inverted interval would be invisible to every KG query"
                        )

                # Close the open old fact at the shared boundary.
                conn.execute(
                    "UPDATE triples SET valid_to=? "
                    "WHERE subject=? AND predicate=? AND object=? AND wing IS ? "
                    "AND valid_to IS NULL" + within_sql,
                    (boundary, sub_id, pred, old_id, wing, *within_params),
                )

                # Open the successor at the same instant (idempotent if already open).
                existing = conn.execute(
                    "SELECT id, sensitivity FROM triples "
                    "WHERE subject=? AND predicate=? AND object=? AND wing IS ? "
                    "AND valid_to IS NULL" + within_sql,
                    (sub_id, pred, new_id, wing, *within_params),
                ).fetchone()
                if existing:
                    triple_id = existing["id"]
                    raised = strictest(sensitivity, existing["sensitivity"])
                    if raised != existing["sensitivity"]:
                        conn.execute(
                            "UPDATE triples SET sensitivity=? WHERE id=?", (raised, triple_id)
                        )
                    self._mark_superseded(conn, [r["id"] for r in rows], triple_id)
                    fact_provenance.record(conn, triple_id, source_thread, source_url)
                    self._add_source(
                        conn,
                        triple_id,
                        source_thread,
                        source_seq_start,
                        source_seq_end,
                        source_drawer_id,
                        source_file,
                        utc_now_iso(),
                        source_url,
                    )
                    return triple_id

                triple_id = make_triple_id(sub_id, pred, new_id, boundary, utc_now_iso())
                conn.execute(
                    """INSERT INTO triples (
                        id, subject, predicate, object, valid_from, valid_to,
                        confidence, source_closet, source_file,
                        source_drawer_id, adapter_name, hub_stamp,
                        source_thread, source_seq_start, source_seq_end,
                        wing, sensitivity
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        triple_id,
                        sub_id,
                        pred,
                        new_id,
                        boundary,
                        None,
                        confidence,
                        source_closet,
                        source_file,
                        source_drawer_id,
                        adapter_name,
                        provenance.stamp_json(),
                        source_thread,
                        source_seq_start,
                        source_seq_end,
                        wing,
                        sensitivity,
                    ),
                )
                self._add_source(
                    conn,
                    triple_id,
                    source_thread,
                    source_seq_start,
                    source_seq_end,
                    source_drawer_id,
                    source_file,
                    utc_now_iso(),
                    source_url,
                )
                self._mark_superseded(conn, [r["id"] for r in rows], triple_id)
                fact_provenance.record(conn, triple_id, source_thread, source_url, new=True)
                return triple_id

    @staticmethod
    def _add_source(
        conn, triple_id, thread, start, end, drawer_id, source_file, now, source_url=None
    ) -> bool:
        """Record a source or its new approval evidence; return False for repeats or no source."""
        if not any(v is not None for v in (thread, drawer_id, source_file)):
            return False
        evidence = fact_provenance.evidence(thread, source_url)
        return (
            conn.execute(
                "INSERT INTO fact_sources (triple_id, source_thread, "
                "source_seq_start, source_seq_end, source_drawer_id, source_file, "
                "added_at, hub_stamp, "
                + ", ".join(evidence)
                + ") VALUES ("
                + ", ".join("?" for _ in range(8 + len(evidence)))
                + ") ON CONFLICT DO UPDATE SET approval_status='approved', "
                "approved_by=excluded.approved_by, approval_link=excluded.approval_link "
                "WHERE excluded.approval_status='approved' AND "
                "(fact_sources.approval_status != 'approved' "
                "OR fact_sources.approved_by IS NOT excluded.approved_by "
                "OR fact_sources.approval_link IS NOT excluded.approval_link)",
                (
                    triple_id,
                    thread,
                    start,
                    end,
                    drawer_id,
                    source_file,
                    now,
                    provenance.stamp_json(),
                    *evidence.values(),
                ),
            ).rowcount
            == 1
        )

    def save_fact(
        self,
        subject: str,
        predicate: str,
        obj: str,
        *,
        valid_from: str = None,
        confidence: float = 1.0,
        source_thread: str = None,
        source_seq_start: int = None,
        source_seq_end: int = None,
        source_url: str = None,
        source_drawer_id: str = None,
        source_file: str = None,
        wing: str = None,
        sensitivity: str = None,
        sensitivities=None,
        single_valued: bool = False,
    ) -> dict:
        """File a fact, folding it into what the graph already holds. One transaction.

        - The same open fact in the same wing: **merge**. No new row; the new
          source joins :meth:`sources`, ``seen_count`` goes up, and the stored
          sensitivity and confidence only ever rise.
        - ``single_valued`` and the subject holds another open value in this
          wing: **supersede** it at ``valid_from`` (default now), as
          :meth:`supersede` does. If that value started after ``valid_from``,
          the new fact is older news: it is filed as **history**, ending
          where the open value begins, and nothing is closed. A person's
          approval can be history only relative to another approved value;
          competing proposals end at the approval boundary or their own start,
          whichever is later, and link to the approved successor.
        - Otherwise: **add**.

        ``sensitivities`` limits which stored facts count, so a caller never
        merges into, replaces or learns of a fact it may not read.

        Returns ``{"action": "merged"|"superseded"|"historical"|"added",
        "triple_id": id, ...}``; a supersede also lists ``superseded`` ids and
        their ``previous`` objects.
        """
        valid_from = sanitize_iso_temporal(valid_from, "valid_from")
        source_thread, source_seq_start, source_seq_end = source_range(
            source_thread, source_seq_start, source_seq_end
        )
        fact_provenance.source_link(source_thread, source_url)
        can_approve = fact_provenance.may_replace_approved(source_thread, source_url)
        wing = fact_wing(wing)
        sensitivity = fact_sensitivity(sensitivity)
        _, sensitivities = visibility(None, sensitivities)
        within_sql, within_params = _visibility_sql(None, sensitivities, alias="triples")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise ValueError("confidence must be a number from 0 to 1")
        if not 0 <= confidence <= 1:
            raise ValueError("confidence must be a number from 0 to 1")
        pred = predicate.lower().replace(" ", "_")
        now = utc_now_iso()
        if valid_from is None:
            boundary = utc_now_z()
        elif _is_date_only_temporal(valid_from):
            boundary = f"{valid_from}T00:00:00Z"
        else:
            boundary = valid_from

        with self._lock:
            conn = self._conn()
            with self._write(conn):
                sub_id = self._resolve_entity_id(conn, subject)
                obj_id = self._resolve_entity_id(conn, obj)
                for name, eid in ((subject, sub_id), (obj, obj_id)):
                    conn.execute(
                        "INSERT OR IGNORE INTO entities (id, name) VALUES (?, ?)", (eid, name)
                    )
                source = (
                    source_thread,
                    source_seq_start,
                    source_seq_end,
                    source_drawer_id,
                    source_file,
                    now,
                    source_url,
                )

                same = conn.execute(
                    "SELECT id, sensitivity FROM triples WHERE subject=? AND predicate=? "
                    "AND object=? AND wing IS ? AND valid_to IS NULL" + within_sql,
                    (sub_id, pred, obj_id, wing, *within_params),
                ).fetchone()
                if same:
                    conn.execute(
                        "UPDATE triples SET seen_count = seen_count + 1, last_seen_at = ?, "
                        "sensitivity = ?, confidence = MAX(COALESCE(confidence, 0), ?) "
                        "WHERE id = ?",
                        (now, strictest(sensitivity, same["sensitivity"]), confidence, same["id"]),
                    )
                    if single_valued and can_approve:
                        closing = conn.execute(
                            "SELECT id, valid_from FROM triples WHERE subject=? AND predicate=? AND object != ? "
                            "AND wing IS ? AND valid_to IS NULL "
                            "AND (approval_status != 'approved' OR valid_from IS NULL "
                            f"OR {_sql_temporal_start_expr('valid_from')} <= ?)" + within_sql,
                            (sub_id, pred, obj_id, wing, boundary, *within_params),
                        ).fetchall()
                        for old in closing:
                            ended = max(
                                boundary, _temporal_start_key(old["valid_from"]) or boundary
                            )
                            conn.execute(
                                "UPDATE triples SET valid_to=? WHERE id=?", (ended, old["id"])
                            )
                        self._mark_superseded(conn, [r["id"] for r in closing], same["id"])
                    fact_provenance.record(conn, same["id"], source_thread, source_url)
                    added = self._add_source(conn, same["id"], *source)
                    return {"action": "merged", "triple_id": same["id"], "source_added": added}

                rivals = []
                if single_valued:
                    rivals = conn.execute(
                        "SELECT t.id, t.valid_from, t.sensitivity, t.approval_status, e.name AS obj_name "
                        "FROM triples t JOIN entities e ON t.object = e.id "
                        "WHERE t.subject=? AND t.predicate=? AND t.object != ? AND t.wing IS ? "
                        "AND t.valid_to IS NULL" + within_sql.replace("triples.", "t."),
                        (sub_id, pred, obj_id, wing, *within_params),
                    ).fetchall()
                if not can_approve:
                    rivals = [r for r in rivals if r["approval_status"] != "approved"]
                later = sorted(
                    (
                        r
                        for r in rivals
                        if r["valid_from"] is not None
                        and (not can_approve or r["approval_status"] == "approved")
                    ),
                    key=lambda r: _temporal_start_key(r["valid_from"]),
                )
                later = [
                    r
                    for r in later
                    if _temporal_start_key(r["valid_from"]) > _temporal_start_key(boundary)
                ]
                if later:
                    # End exactly where the open value starts: a date-only end
                    # would read as the whole day and overlap it.
                    action, start, closing = "historical", valid_from, []
                    end = _temporal_start_key(later[0]["valid_from"])
                    same_history = conn.execute(
                        "SELECT id, sensitivity FROM triples WHERE subject=? AND predicate=? "
                        "AND object=? AND wing IS ? AND valid_from IS ? AND valid_to = ?"
                        + within_sql,
                        (sub_id, pred, obj_id, wing, start, end, *within_params),
                    ).fetchone()
                    if same_history:
                        # A retry of the same history merges like an open fact.
                        conn.execute(
                            "UPDATE triples SET seen_count = seen_count + 1, last_seen_at = ?, "
                            "sensitivity = ?, confidence = MAX(COALESCE(confidence, 0), ?) "
                            "WHERE id = ?",
                            (
                                now,
                                strictest(sensitivity, same_history["sensitivity"]),
                                confidence,
                                same_history["id"],
                            ),
                        )
                        fact_provenance.record(conn, same_history["id"], source_thread, source_url)
                        added = self._add_source(conn, same_history["id"], *source)
                        return {
                            "action": "merged",
                            "triple_id": same_history["id"],
                            "source_added": added,
                        }
                else:
                    closing = rivals
                    action = "superseded" if rivals else "added"
                    start = boundary if rivals else valid_from
                    end = None
                sensitivity = strictest(sensitivity, *(r["sensitivity"] for r in closing))

                triple_id = make_triple_id(sub_id, pred, obj_id, start, now)
                conn.execute(
                    """INSERT INTO triples (
                        id, subject, predicate, object, valid_from, valid_to,
                        confidence, source_file, source_drawer_id, hub_stamp,
                        source_thread, source_seq_start, source_seq_end,
                        wing, sensitivity, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        triple_id,
                        sub_id,
                        pred,
                        obj_id,
                        start,
                        end,
                        confidence,
                        source_file,
                        source_drawer_id,
                        provenance.stamp_json(),
                        source_thread,
                        source_seq_start,
                        source_seq_end,
                        wing,
                        sensitivity,
                        now,
                    ),
                )
                fact_provenance.record(conn, triple_id, source_thread, source_url, new=True)
                self._add_source(conn, triple_id, *source)
                for rival in closing:
                    ended = max(boundary, _temporal_start_key(rival["valid_from"]) or boundary)
                    conn.execute(
                        "UPDATE triples SET valid_to = ? WHERE id = ?", (ended, rival["id"])
                    )
                self._mark_superseded(conn, [r["id"] for r in closing], triple_id)
                out = {"action": action, "triple_id": triple_id}
                if closing:
                    out["superseded"] = [r["id"] for r in closing]
                    out["previous"] = [r["obj_name"] for r in closing]
                if later:
                    out["current"] = later[0]["id"]
                return out

    def sources(self, triple_ids) -> dict:
        """Every recorded source of each fact, oldest first: ``{triple_id: [source, ...]}``."""
        ids = [i for i in dict.fromkeys(triple_ids) if isinstance(i, str)]
        out = {i: [] for i in ids}
        if not ids:
            return out
        with self._lock:
            conn = self._conn()
            for start in range(0, len(ids), 500):
                batch = ids[start : start + 500]
                rows = conn.execute(
                    "SELECT triple_id, source_thread, source_seq_start, source_seq_end, "
                    "source_drawer_id, source_file, added_at, "
                    + ", ".join(fact_provenance.COLUMNS)
                    + " FROM fact_sources "
                    f"WHERE triple_id IN ({', '.join('?' for _ in batch)}) ORDER BY id",
                    batch,
                ).fetchall()
                for row in rows:
                    out[row["triple_id"]].append(
                        {k: row[k] for k in row.keys() if k != "triple_id" and row[k] is not None}
                    )
        return out

    def search_fact_rows(
        self,
        tokens,
        *,
        candidate_limit,
        common_term_fraction,
        wings=None,
        sensitivities=None,
        as_of=None,
    ):
        """Indexed lexical lookup with the same visibility as other KG reads."""
        from .fact_index import candidates

        wings, sensitivities = visibility(wings, sensitivities)
        as_of = sanitize_iso_temporal(as_of, "as_of")
        visible_sql, params = _visibility_sql(wings, sensitivities)
        temporal_sql, temporal_params = _temporal_filter_sql(as_of) if as_of else ("", [])
        with self._lock:
            if not self._fact_index_ready:
                raise RuntimeError("Fact search index unavailable")
            return candidates(
                self._conn(),
                tokens,
                visible_sql,
                params,
                temporal_sql,
                temporal_params,
                limit=candidate_limit,
                common_term_fraction=common_term_fraction,
            )

    @staticmethod
    def _mark_superseded(conn, old_ids, new_id):
        """Point each replaced fact at its successor. A fact never supersedes itself."""
        for old in old_ids:
            if old != new_id:
                conn.execute(
                    "UPDATE triples SET status='superseded', superseded_by=? WHERE id=?",
                    (new_id, old),
                )

    def rewrite(
        self,
        triple_id: str,
        predicate: str,
        obj: str,
        at: str = None,
        source_file: str = None,
    ):
        """Close the open fact ``triple_id`` and open its rewrite in one transaction.

        The successor keeps the subject, takes ``predicate`` and ``obj`` and
        opens at the same instant the original closes, so an as-of query
        before the boundary still returns the original wording. Unlike
        :meth:`supersede` the predicate may change, which is what
        ``mempalace kg normalize`` needs.

        Addressed by the triple's ``id`` so that a fact closed or replaced
        after a plan was written is not resurrected: if ``triple_id`` is no
        longer open, nothing is written and ``None`` is returned. Returns the
        successor's id otherwise. One SQLite transaction covers both writes,
        so an interruption leaves the original fact open and untouched.

        The successor inherits the original's ``confidence`` and provenance
        (``source_closet``, ``source_file``, ``source_drawer_id``,
        ``adapter_name`` and the chat range): rewording a predicate is not new evidence, and a
        normalized fact must still point at the drawer it came from.
        ``source_file`` here is only a fallback for an original carrying none.
        """
        if at is None:
            boundary = utc_now_z()
        elif _is_date_only_temporal(at):
            boundary = f"{at}T00:00:00Z"
        else:
            boundary = at
        boundary = sanitize_iso_temporal(boundary, "at")
        pred = str(predicate).lower().replace(" ", "_")

        with self._lock:
            conn = self._conn()
            with self._write(conn):
                obj_id = self._resolve_entity_id(conn, obj)
                row = conn.execute(
                    "SELECT * FROM triples WHERE id=? AND valid_to IS NULL",
                    (triple_id,),
                ).fetchone()
                if row is None:
                    return None
                valid_from = row["valid_from"]
                if valid_from is not None and _temporal_end_key(boundary) < _temporal_start_key(
                    valid_from
                ):
                    raise ValueError(
                        f"at={boundary!r} is before valid_from={valid_from!r}; "
                        "an inverted interval would be invisible to every KG query"
                    )
                sub_id = row["subject"]
                conn.execute(
                    "INSERT OR IGNORE INTO entities (id, name) VALUES (?, ?)", (obj_id, obj)
                )
                conn.execute("UPDATE triples SET valid_to=? WHERE id=?", (boundary, triple_id))
                # A scheduled future fact cannot cover the rewrite boundary.
                # Nor can another witness stand in for this fact's evidence:
                # reuse only an already-valid successor with equal provenance.
                existing = conn.execute(
                    "SELECT id FROM triples "
                    "WHERE subject=? AND predicate=? AND object=? AND valid_to IS NULL "
                    f"AND (valid_from IS NULL OR {_sql_temporal_start_expr('valid_from')} <= ?) "
                    "AND confidence IS ? AND source_closet IS ? AND source_file IS ? "
                    "AND source_drawer_id IS ? AND adapter_name IS ? "
                    "AND source_thread IS ? AND source_seq_start IS ? AND source_seq_end IS ? "
                    "AND wing IS ? AND sensitivity IS ?"
                    + "".join(f" AND {key} IS ?" for key in fact_provenance.COLUMNS),
                    (
                        sub_id,
                        pred,
                        obj_id,
                        boundary,
                        row["confidence"] if row["confidence"] is not None else 1.0,
                        row["source_closet"],
                        row["source_file"] or source_file,
                        row["source_drawer_id"],
                        row["adapter_name"],
                        row["source_thread"],
                        row["source_seq_start"],
                        row["source_seq_end"],
                        row["wing"],
                        row["sensitivity"],
                        *(row[key] for key in fact_provenance.COLUMNS),
                    ),
                ).fetchone()
                if existing:
                    self._mark_superseded(conn, [triple_id], existing["id"])
                    return existing["id"]
                new_id = make_triple_id(sub_id, pred, obj_id, boundary, utc_now_iso())
                conn.execute(
                    """INSERT INTO triples (
                        id, subject, predicate, object, valid_from, valid_to,
                        confidence, source_closet, source_file,
                        source_drawer_id, adapter_name, hub_stamp,
                        source_thread, source_seq_start, source_seq_end,
                        wing, sensitivity
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        new_id,
                        sub_id,
                        pred,
                        obj_id,
                        boundary,
                        None,
                        row["confidence"] if row["confidence"] is not None else 1.0,
                        row["source_closet"],
                        row["source_file"] or source_file,
                        row["source_drawer_id"],
                        row["adapter_name"],
                        row["hub_stamp"],
                        row["source_thread"],
                        row["source_seq_start"],
                        row["source_seq_end"],
                        row["wing"],
                        row["sensitivity"],
                    ),
                )
                conn.execute(
                    "UPDATE triples SET "
                    + ", ".join(f"{key}=?" for key in fact_provenance.COLUMNS)
                    + " WHERE id=?",
                    (*(row[key] for key in fact_provenance.COLUMNS), new_id),
                )
                self._mark_superseded(conn, [triple_id], new_id)
                return new_id

    # ── Query operations ──────────────────────────────────────────────────

    def query_entity(
        self,
        name: str,
        as_of: str = None,
        direction: str = "outgoing",
        wings=None,
        sensitivities=None,
    ):
        """
        Get all relationships for an entity.

        direction: "outgoing" (entity → ?), "incoming" (? → entity), "both"
        as_of: ISO date or canonical UTC datetime — only return facts valid then
        wings / sensitivities: only facts in these wings and of these
        sensitivities (see :func:`visibility`); ``None`` means any
        """
        as_of = sanitize_iso_temporal(as_of, "as_of")
        wings, sensitivities = visibility(wings, sensitivities)
        results = []

        temporal_sql = ""
        temporal_params = []
        if as_of:
            temporal_sql, temporal_params = _temporal_filter_sql(as_of)
        visible_sql, visible_params = _visibility_sql(wings, sensitivities)
        temporal_sql += visible_sql
        temporal_params = list(temporal_params) + visible_params

        with self._lock:
            conn = self._conn()
            eid = self._resolve_entity_id(conn, name)

            if direction in ("outgoing", "both"):
                query = (
                    "SELECT t.*, e.name as obj_name FROM triples t "
                    "JOIN entities e ON t.object = e.id WHERE t.subject = ?" + temporal_sql
                )
                params = [eid] + temporal_params

                for row in conn.execute(query, params).fetchall():
                    results.append(
                        {
                            "direction": "outgoing",
                            "subject": name,
                            "predicate": row["predicate"],
                            "object": row["obj_name"],
                            "valid_from": row["valid_from"],
                            "valid_to": row["valid_to"],
                            "confidence": row["confidence"],
                            "source_closet": row["source_closet"],
                            "current": row["valid_to"] is None,
                            **_fact_fields(row),
                            **provenance.stamp_from_json(row["hub_stamp"]),
                        }
                    )

            if direction in ("incoming", "both"):
                query = (
                    "SELECT t.*, e.name as sub_name FROM triples t "
                    "JOIN entities e ON t.subject = e.id WHERE t.object = ?" + temporal_sql
                )
                params = [eid] + temporal_params

                for row in conn.execute(query, params).fetchall():
                    results.append(
                        {
                            "direction": "incoming",
                            "subject": row["sub_name"],
                            "predicate": row["predicate"],
                            "object": name,
                            "valid_from": row["valid_from"],
                            "valid_to": row["valid_to"],
                            "confidence": row["confidence"],
                            "source_closet": row["source_closet"],
                            "current": row["valid_to"] is None,
                            **_fact_fields(row),
                            **provenance.stamp_from_json(row["hub_stamp"]),
                        }
                    )

            if not results:
                exact_exists = (
                    conn.execute("SELECT 1 FROM entities WHERE id = ?", (eid,)).fetchone()
                    is not None
                )
                if not exact_exists:
                    candidates = self._lookup_entity_candidates(
                        conn, name, eid, wings, sensitivities
                    )
                    if len(candidates) == 1:
                        cand_id = candidates[0]["id"]
                        cand_name = candidates[0]["name"]
                        results.extend(
                            self._triples_for_entity(
                                conn,
                                cand_id,
                                cand_name,
                                direction,
                                temporal_sql,
                                temporal_params,
                            )
                        )

        return results

    def find_entity_candidates(self, name: str, wings=None, sensitivities=None) -> list:
        """Return token/prefix entity matches for disambiguation (never substring).

        With a read filter, only entities that appear in a visible fact.
        """
        if not name or len(name.strip()) < _MIN_ENTITY_CANDIDATE_LEN:
            return []
        wings, sensitivities = visibility(wings, sensitivities)
        with self._lock:
            conn = self._conn()
            eid = self._resolve_entity_id(conn, name)
            return self._lookup_entity_candidates(conn, name, eid, wings, sensitivities)

    def _lookup_entity_candidates(
        self, conn, name: str, eid: str, wings=None, sensitivities=None
    ) -> list:
        if not name or len(name.strip()) < _MIN_ENTITY_CANDIDATE_LEN:
            return []
        name_esc = _escape_like(name.strip())
        id_esc = _escape_like(eid)
        visible_sql, visible_params = _visibility_sql(wings, sensitivities)
        seen_in = ""
        if wings is not None or sensitivities is not None:
            seen_in = (
                f" AND id IN (SELECT subject FROM triples t WHERE 1{visible_sql}"
                f" UNION SELECT object FROM triples t WHERE 1{visible_sql})"
            )
        rows = conn.execute(
            "SELECT id, name FROM entities WHERE ("
            "id = ? OR name = ? OR "
            "name LIKE ? ESCAPE '\\' OR name LIKE ? ESCAPE '\\' OR name LIKE ? ESCAPE '\\' OR "
            "id LIKE ? ESCAPE '\\' OR id LIKE ? ESCAPE '\\' OR id LIKE ? ESCAPE '\\')"
            f"{seen_in} LIMIT 5",
            (
                eid,
                name,
                f"{name_esc} %",
                f"% {name_esc}",
                f"% {name_esc} %",
                f"{id_esc}\\_%",
                f"%\\_{id_esc}",
                f"%\\_{id_esc}\\_%",
                *visible_params,
                *visible_params,
            ),
        ).fetchall()
        out = []
        for row in rows:
            if row["id"] == eid:
                continue
            out.append({"id": row["id"], "name": row["name"]})
        return out

    def _triples_for_entity(
        self, conn, eid: str, name: str, direction: str, temporal_sql: str, temporal_params: list
    ) -> list:
        results = []
        if direction in ("outgoing", "both"):
            query = (
                "SELECT t.*, e.name as obj_name FROM triples t "
                "JOIN entities e ON t.object = e.id WHERE t.subject = ?" + temporal_sql
            )
            for row in conn.execute(query, [eid] + temporal_params).fetchall():
                results.append(
                    {
                        "direction": "outgoing",
                        "subject": name,
                        "predicate": row["predicate"],
                        "object": row["obj_name"],
                        "valid_from": row["valid_from"],
                        "valid_to": row["valid_to"],
                        "confidence": row["confidence"],
                        "source_closet": row["source_closet"],
                        "current": row["valid_to"] is None,
                        **_fact_fields(row),
                    }
                )
        if direction in ("incoming", "both"):
            query = (
                "SELECT t.*, e.name as sub_name FROM triples t "
                "JOIN entities e ON t.subject = e.id WHERE t.object = ?" + temporal_sql
            )
            for row in conn.execute(query, [eid] + temporal_params).fetchall():
                results.append(
                    {
                        "direction": "incoming",
                        "subject": row["sub_name"],
                        "predicate": row["predicate"],
                        "object": name,
                        "valid_from": row["valid_from"],
                        "valid_to": row["valid_to"],
                        "confidence": row["confidence"],
                        "source_closet": row["source_closet"],
                        "current": row["valid_to"] is None,
                        **_fact_fields(row),
                    }
                )
        return results

    def query_relationship(self, predicate: str, as_of: str = None, wings=None, sensitivities=None):
        """Get all triples with a given relationship type."""
        as_of = sanitize_iso_temporal(as_of, "as_of")
        wings, sensitivities = visibility(wings, sensitivities)
        pred = predicate.lower().replace(" ", "_")

        query = """
            SELECT t.*, s.name as sub_name, o.name as obj_name
            FROM triples t
            JOIN entities s ON t.subject = s.id
            JOIN entities o ON t.object = o.id
            WHERE t.predicate = ?
        """
        params = [pred]

        if as_of:
            temporal_sql, temporal_params = _temporal_filter_sql(as_of)
            query += temporal_sql
            params.extend(temporal_params)
        visible_sql, visible_params = _visibility_sql(wings, sensitivities)
        query += visible_sql
        params.extend(visible_params)

        results = []
        with self._lock:
            conn = self._conn()
            for row in conn.execute(query, params).fetchall():
                results.append(
                    {
                        "subject": row["sub_name"],
                        "predicate": pred,
                        "object": row["obj_name"],
                        "valid_from": row["valid_from"],
                        "valid_to": row["valid_to"],
                        "current": row["valid_to"] is None,
                        **_fact_fields(row),
                    }
                )
        return results

    def timeline(
        self,
        entity_name: str = None,
        limit: int = 100,
        offset: int = 0,
        wings=None,
        sensitivities=None,
    ):
        """Get facts in chronological order, optionally filtered by entity.

        Paginated with ``limit``/``offset`` (same convention as drawer
        listing); defaults preserve the historical behavior of returning
        the first 100 facts. Use :meth:`timeline_total` for the full
        matching count.
        """
        limit = max(1, int(limit))
        offset = max(0, int(offset))
        wings, sensitivities = visibility(wings, sensitivities)
        visible_sql, visible_params = _visibility_sql(wings, sensitivities)
        with self._lock:
            conn = self._conn()
            if entity_name:
                eid = self._resolve_entity_id(conn, entity_name)
                where, params = "(t.subject = ? OR t.object = ?)", [eid, eid]
            else:
                where, params = "1", []
            rows = conn.execute(
                f"""
                SELECT t.*, s.name as sub_name, o.name as obj_name
                FROM triples t
                JOIN entities s ON t.subject = s.id
                JOIN entities o ON t.object = o.id
                WHERE {where}{visible_sql}
                ORDER BY t.valid_from ASC NULLS LAST, t.id ASC
                LIMIT ? OFFSET ?
            """,
                (*params, *visible_params, limit, offset),
            ).fetchall()

        return [
            {
                "subject": r["sub_name"],
                "predicate": r["predicate"],
                "object": r["obj_name"],
                "valid_from": r["valid_from"],
                "valid_to": r["valid_to"],
                "current": r["valid_to"] is None,
                **_fact_fields(r),
            }
            for r in rows
        ]

    def timeline_total(self, entity_name: str = None, wings=None, sensitivities=None) -> int:
        """Total number of facts a :meth:`timeline` query matches (all pages)."""
        wings, sensitivities = visibility(wings, sensitivities)
        visible_sql, visible_params = _visibility_sql(wings, sensitivities)
        with self._lock:
            conn = self._conn()
            if entity_name:
                eid = self._resolve_entity_id(conn, entity_name)
                where, params = "(t.subject = ? OR t.object = ?)", [eid, eid]
            else:
                where, params = "1", []
            row = conn.execute(
                f"SELECT COUNT(*) FROM triples t WHERE {where}{visible_sql}",
                (*params, *visible_params),
            ).fetchone()
        return row[0]

    # ── Stats ─────────────────────────────────────────────────────────────

    # -- Replication (RFC 004 step 1: read-replica snapshot) ---------------

    _REPLICATION_TABLES = {
        "entities": ("id", "name", "type", "properties", "created_at"),
        "triples": (
            "id",
            "subject",
            "predicate",
            "object",
            "valid_from",
            "valid_to",
            "confidence",
            "source_closet",
            "source_file",
            "source_drawer_id",
            "adapter_name",
            "extracted_at",
            "source_thread",
            "source_seq_start",
            "source_seq_end",
            "status",
            "superseded_by",
            "wing",
            "sensitivity",
            *fact_provenance.COLUMNS,
        ),
        "fact_sources": (
            "id",
            "triple_id",
            "source_thread",
            "source_seq_start",
            "source_seq_end",
            "source_drawer_id",
            "source_file",
            "added_at",
            "hub_stamp",
            *fact_provenance.COLUMNS,
        ),
    }

    def dump_rows(self, table: str, after_rowid: int = 0, limit: int = 500) -> list:
        """Page KG rows in rowid order for snapshot replication.

        Rows are returned verbatim with a ``_rowid`` pagination cursor.
        rowid order is deterministic, so pages never skip under concurrent
        appends (updates in earlier pages are caught by the next full pass).
        """
        columns = self._REPLICATION_TABLES.get(table)
        if columns is None:
            raise ValueError(f"table must be one of {sorted(self._REPLICATION_TABLES)}")
        with self._lock:
            conn = self._conn()
            extra, join = "", ""
            if table == "fact_sources":
                # A receiver can have the same fact under a different id, or
                # an unrelated observation under this row's integer id.
                extra = ", " + ", ".join(f"t.{c} AS _triple_{c}" for c in _TRIPLE_IDENTITY)
                join = " JOIN triples t ON t.id=s.triple_id"
            rows = conn.execute(
                "SELECT s.rowid AS _rowid, "
                + ", ".join(f"s.{c}" for c in columns)
                + extra
                + f" FROM {table} s"
                + join
                + " WHERE s.rowid > ? ORDER BY s.rowid ASC LIMIT ?",
                (int(after_rowid), max(1, min(int(limit), 1000))),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            if table == "fact_sources":
                item["_triple"] = {
                    "id": item["triple_id"],
                    **{c: item.pop(f"_triple_{c}") for c in _TRIPLE_IDENTITY},
                }
            result.append(item)
        return result

    def apply_row(self, table: str, row: dict) -> dict:
        """Apply a peer row and return an ``applied`` flag and any skip reason.

        Existing fact identity, authorship, first source and approval proof are
        retained. Set end dates and successor links are never replaced or
        cleared, and non-active statuses are not rewritten. Only person-approved
        peer evidence may advance an approved
        fact's lifecycle. Missing successor links are omitted and reported;
        an open target stays open until its successor resolves. Retry those
        rows after applying their dependencies.

        Observations dedupe by source key after resolving exact triple identity
        and checking the source fact id for divergent local history; their
        integer ids are graph-local. An orphan returns a skip report so
        replication can continue. Missing row ids or source identity still
        raise ValueError.
        """
        columns = self._REPLICATION_TABLES.get(table)
        if columns is None:
            raise ValueError(f"table must be one of {sorted(self._REPLICATION_TABLES)}")
        if not row.get("id"):
            raise ValueError("replicated row is missing 'id'")
        if table == "fact_sources":
            identity = row.get("_triple")
            if not isinstance(identity, dict) or any(c not in identity for c in _TRIPLE_IDENTITY):
                raise ValueError("replicated fact source is missing triple identity")
            # Older snapshots already carry the source fact id in triple_id.
            # It is not the graph-local integer observation id.
            identity = {**identity, "id": row.get("triple_id")}
            with self._lock:
                conn = self._conn()
                with self._write(conn):
                    same = _identical_triples(conn, identity)
                    if not same:
                        return {
                            "id": row["id"],
                            "triple_id": row.get("triple_id"),
                            "applied": False,
                            "reason": "no_matching_triple",
                        }
                    _merge_imported_source(conn, row, same[0]["id"])
                    return {"id": row["id"], "triple_id": same[0]["id"], "applied": True}
        # A peer that predates a column sends no key for it. Leave the local
        # value alone then, so an older replica cannot erase a supersede link.
        with self._lock:
            conn = self._conn()
            with self._write(conn):
                report = {"id": row["id"], "applied": True}
                if table == "triples":
                    row, report = _prepare_replicated_triple(conn, row)
                    if not report["applied"]:
                        return report
                _upsert_replicated_row(conn, table, row, columns)
                return report

    def list_wings(self):
        """Fact wings in one covering-index query, without graph-wide statistics."""
        with self._lock:
            return [
                r["wing"]
                for r in self._conn().execute(
                    "SELECT DISTINCT wing FROM triples WHERE wing IS NOT NULL ORDER BY wing"
                )
            ]

    def stats(self):
        with self._lock:
            conn = self._conn()
            entities = conn.execute("SELECT COUNT(*) as cnt FROM entities").fetchone()["cnt"]
            triples = conn.execute("SELECT COUNT(*) as cnt FROM triples").fetchone()["cnt"]
            current = conn.execute(
                "SELECT COUNT(*) as cnt FROM triples WHERE valid_to IS NULL"
            ).fetchone()["cnt"]
            expired = triples - current
            predicates = [
                r["predicate"]
                for r in conn.execute(
                    "SELECT DISTINCT predicate FROM triples ORDER BY predicate"
                ).fetchall()
            ]
            wings = [
                r["wing"]
                for r in conn.execute(
                    "SELECT DISTINCT wing FROM triples WHERE wing IS NOT NULL ORDER BY wing"
                ).fetchall()
            ]
        return {
            "entities": entities,
            "triples": triples,
            "current_facts": current,
            "expired_facts": expired,
            "relationship_types": predicates,
            "wings": wings,
        }

    # ── Seed from known facts ─────────────────────────────────────────────

    def report_entity_id_collisions(self) -> list:
        """List rows still keyed by a legacy id their name no longer derives.

        Each was written before entity ids kept punctuation, and may hold facts
        for more than one spelling. Stored ids stay as they are.
        """
        with self._lock:
            conn = self._conn()
            rows = conn.execute("SELECT id, name FROM entities").fetchall()
        reports = []
        for row in rows:
            current = entity_id_for_name(row["name"])
            legacy = legacy_entity_id(row["name"])
            if row["id"] == legacy and row["id"] != current:
                reports.append(
                    {
                        "id": row["id"],
                        "name": row["name"],
                        "current_id": current,
                        "legacy_id": legacy,
                        "detail": "stored id left unchanged",
                    }
                )
        return reports

    def seed_from_entity_facts(self, entity_facts: dict):
        """
        Seed the knowledge graph from fact_checker.py ENTITY_FACTS.
        This bootstraps the graph with known ground truth.
        """
        for key, facts in entity_facts.items():
            name = facts.get("full_name", key.capitalize())
            etype = facts.get("type", "person")
            self.add_entity(
                name,
                etype,
                {
                    "gender": facts.get("gender", ""),
                    "birthday": facts.get("birthday", ""),
                },
            )

            # Relationships
            parent = facts.get("parent")
            if parent:
                self.add_triple(
                    name, "child_of", parent.capitalize(), valid_from=facts.get("birthday")
                )

            partner = facts.get("partner")
            if partner:
                self.add_triple(name, "married_to", partner.capitalize())

            relationship = facts.get("relationship", "")
            if relationship == "daughter":
                self.add_triple(
                    name,
                    "is_child_of",
                    facts.get("parent", "").capitalize() or name,
                    valid_from=facts.get("birthday"),
                )
            elif relationship == "husband":
                self.add_triple(name, "is_partner_of", facts.get("partner", name).capitalize())
            elif relationship == "brother":
                self.add_triple(name, "is_sibling_of", facts.get("sibling", name).capitalize())
            elif relationship == "dog":
                self.add_triple(name, "is_pet_of", facts.get("owner", name).capitalize())
                self.add_entity(name, "animal")

            # Interests
            for interest in facts.get("interests", []):
                self.add_triple(name, "loves", interest.capitalize(), valid_from="2025-01-01")
