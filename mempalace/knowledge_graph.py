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
import os
import sqlite3
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional
from .config import sanitize_iso_temporal
from .ids import make_triple_id


DEFAULT_KG_PATH = os.path.expanduser("~/.mempalace/knowledge_graph.sqlite3")


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
    """

    as_of_key = _temporal_start_key(as_of)
    valid_from_expr = _sql_temporal_start_expr("t.valid_from")
    valid_to_expr = _sql_temporal_end_expr("t.valid_to")

    return (
        f" AND (t.valid_from IS NULL OR {valid_from_expr} <= ?) "
        f"AND (t.valid_to IS NULL OR {valid_to_expr} >= ?)",
        [as_of_key, as_of_key],
    )


class KnowledgeGraph:
    def __init__(self, db_path: str = None, oplog=None):
        """``oplog`` (an :class:`mempalace.oplog.OpLog`) enables the RFC 004
        step-2a dual-write shadow: every KG mutation that actually changes
        state also emits a kg.* op. Emission lives inside this class because
        only the write methods know whether state changed — ``add_triple``'s
        dedup short-circuit and a zero-match ``invalidate`` must emit
        nothing. ``None`` (the default) keeps every existing caller
        op-log-free."""
        self.db_path = db_path or DEFAULT_KG_PATH
        self._oplog = oplog
        db_parent = Path(self.db_path).parent
        db_parent.mkdir(parents=True, exist_ok=True)
        try:
            db_parent.chmod(0o700)
        except (OSError, NotImplementedError):
            pass
        self._connection = None
        self._lock = threading.Lock()
        self._init_db()

    def _shadow_emit(self, kind: str, payload: dict, author_agent: str) -> None:
        if self._oplog is None:
            return
        from .oplog import shadow_append

        shadow_append(self._oplog, kind, payload, author_agent=author_agent)

    def _init_db(self):
        conn = self._conn()
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
                FOREIGN KEY (subject) REFERENCES entities(id),
                FOREIGN KEY (object) REFERENCES entities(id)
            );

            CREATE INDEX IF NOT EXISTS idx_triples_subject ON triples(subject);
            CREATE INDEX IF NOT EXISTS idx_triples_object ON triples(object);
            CREATE INDEX IF NOT EXISTS idx_triples_predicate ON triples(predicate);
            CREATE INDEX IF NOT EXISTS idx_triples_valid ON triples(valid_from, valid_to);
        """)
        self._migrate_schema(conn)
        conn.commit()

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

    def _conn(self):
        if self._connection is None:
            self._connection = sqlite3.connect(self.db_path, timeout=10, check_same_thread=False)
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.row_factory = sqlite3.Row
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

    def _entity_id(self, name: str) -> str:
        return name.lower().replace(" ", "_").replace("'", "")

    # ── Write operations ──────────────────────────────────────────────────

    def add_entity(
        self,
        name: str,
        entity_type: str = "unknown",
        properties: dict = None,
        author_agent: str = "mcp",
    ):
        """Add or update an entity node."""
        eid = self._entity_id(name)
        props = json.dumps(properties or {})
        with self._lock:
            conn = self._conn()
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO entities (id, name, type, properties) VALUES (?, ?, ?, ?)",
                    (eid, name, entity_type, props),
                )
        # LWW-by-HLC per entity key (RFC 004 §6.2) — REPLACE always mutates,
        # so an explicit upsert always emits.
        self._shadow_emit(
            "kg.entity.upsert",
            {
                "entity_id": eid,
                "name": name,
                "type": entity_type,
                "properties": properties or {},
            },
            author_agent,
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
        author_agent: str = "mcp",
    ):
        """
        Add a relationship triple: subject → predicate → object.

        ``source_drawer_id`` and ``adapter_name`` are RFC 002 §5.5 provenance
        fields populated by adapters that advertise ``supports_kg_triples``;
        they default to ``None`` so every existing caller stays
        source-compatible.

        Examples:
            add_triple("Max", "child_of", "Alice", valid_from="2015-04-01")
            add_triple("Max", "does", "swimming", valid_from="2025-01-01")
            add_triple("Alice", "worried_about", "Max injury", valid_from="2026-01-01")
        """

        valid_from = sanitize_iso_temporal(valid_from, "valid_from")
        valid_to = sanitize_iso_temporal(valid_to, "valid_to")

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

        sub_id = self._entity_id(subject)
        obj_id = self._entity_id(obj)
        pred = predicate.lower().replace(" ", "_")

        # Explicit so the emitted op carries the exact stored value; matches
        # the format SQLite's CURRENT_TIMESTAMP default produced before.
        extracted_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        # Auto-create entities if they don't exist
        with self._lock:
            conn = self._conn()
            with conn:
                conn.execute(
                    "INSERT OR IGNORE INTO entities (id, name) VALUES (?, ?)",
                    (sub_id, subject),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO entities (id, name) VALUES (?, ?)",
                    (obj_id, obj),
                )

                # Check for existing identical triple
                existing = conn.execute(
                    "SELECT id FROM triples WHERE subject=? AND predicate=? AND object=? AND valid_to IS NULL",
                    (sub_id, pred, obj_id),
                ).fetchone()
                if existing:
                    created = False
                    triple_id = existing["id"]  # Already exists and still valid
                else:
                    created = True
                    triple_id = make_triple_id(
                        sub_id, pred, obj_id, valid_from, datetime.now().isoformat()
                    )
                    conn.execute(
                        """INSERT INTO triples (
                            id, subject, predicate, object, valid_from, valid_to,
                            confidence, source_closet, source_file,
                            source_drawer_id, adapter_name, extracted_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                            extracted_at,
                        ),
                    )
        if created:
            # G-set assert (RFC 004 §6.2). The payload carries the full row
            # so a fold can reproduce it — including the auto-created
            # entities, which are re-derivable from the names and therefore
            # get no op of their own.
            self._shadow_emit(
                "kg.assert",
                {
                    "triple_id": triple_id,
                    "subject": sub_id,
                    "subject_name": subject,
                    "predicate": pred,
                    "object": obj_id,
                    "object_name": obj,
                    "valid_from": valid_from,
                    "valid_to": valid_to,
                    "confidence": confidence,
                    "source_closet": source_closet,
                    "source_file": source_file,
                    "source_drawer_id": source_drawer_id,
                    "adapter_name": adapter_name,
                    "extracted_at": extracted_at,
                },
                author_agent,
            )
        return triple_id

    def invalidate(
        self,
        subject: str,
        predicate: str,
        obj: str,
        ended: str = None,
        author_agent: str = "mcp",
    ):
        """Mark a relationship as no longer valid (set valid_to date/time)."""
        sub_id = self._entity_id(subject)
        obj_id = self._entity_id(obj)
        pred = predicate.lower().replace(" ", "_")
        ended = sanitize_iso_temporal(ended or date.today().isoformat(), "ended")

        with self._lock:
            conn = self._conn()
            with conn:
                rows = conn.execute(
                    "SELECT id, valid_from FROM triples "
                    "WHERE subject=? AND predicate=? AND object=? AND valid_to IS NULL",
                    (sub_id, pred, obj_id),
                ).fetchall()

                for row in rows:
                    valid_from = row["valid_from"]
                    if valid_from is not None and _temporal_end_key(ended) < _temporal_start_key(
                        valid_from
                    ):
                        raise ValueError(
                            f"valid_to={ended!r} is before valid_from={valid_from!r}; "
                            "an inverted interval would be invisible to every KG query"
                        )

                conn.execute(
                    "UPDATE triples SET valid_to=? "
                    "WHERE subject=? AND predicate=? AND object=? AND valid_to IS NULL",
                    (ended, sub_id, pred, obj_id),
                )
        closed_triple_ids = [row["id"] for row in rows]
        if closed_triple_ids:
            # Interval-close (RFC 004 §6.2): idempotent, min valid_to wins.
            # A zero-match invalidate changed nothing and emits nothing.
            self._shadow_emit(
                "kg.close",
                {
                    "subject": sub_id,
                    "subject_name": subject,
                    "predicate": pred,
                    "object": obj_id,
                    "object_name": obj,
                    "ended": ended,
                    "closed_triple_ids": closed_triple_ids,
                },
                author_agent,
            )

    # ── Query operations ──────────────────────────────────────────────────

    def get_triple(self, triple_id: str) -> Optional[dict]:
        """Fetch one triple row by id, or None. Used by the RFC 004 step-2a
        shadow verifier to compare op-log expectations against stored state."""
        with self._lock:
            conn = self._conn()
            row = conn.execute("SELECT * FROM triples WHERE id = ?", (triple_id,)).fetchone()
        return dict(row) if row is not None else None

    def get_entity(self, entity_id: str) -> Optional[dict]:
        """Fetch one entity row by id, or None."""
        with self._lock:
            conn = self._conn()
            row = conn.execute("SELECT * FROM entities WHERE id = ?", (entity_id,)).fetchone()
        return dict(row) if row is not None else None

    def query_entity(self, name: str, as_of: str = None, direction: str = "outgoing"):
        """
        Get all relationships for an entity.

        direction: "outgoing" (entity → ?), "incoming" (? → entity), "both"
        as_of: ISO date or canonical UTC datetime — only return facts valid then
        """
        as_of = sanitize_iso_temporal(as_of, "as_of")
        eid = self._entity_id(name)
        results = []

        temporal_sql = ""
        temporal_params = []
        if as_of:
            temporal_sql, temporal_params = _temporal_filter_sql(as_of)

        with self._lock:
            conn = self._conn()

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
                        }
                    )

        return results

    def query_relationship(self, predicate: str, as_of: str = None):
        """Get all triples with a given relationship type."""
        as_of = sanitize_iso_temporal(as_of, "as_of")
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
                    }
                )
        return results

    def timeline(self, entity_name: str = None):
        """Get all facts in chronological order, optionally filtered by entity."""
        with self._lock:
            conn = self._conn()
            if entity_name:
                eid = self._entity_id(entity_name)
                rows = conn.execute(
                    """
                    SELECT t.*, s.name as sub_name, o.name as obj_name
                    FROM triples t
                    JOIN entities s ON t.subject = s.id
                    JOIN entities o ON t.object = o.id
                    WHERE (t.subject = ? OR t.object = ?)
                    ORDER BY t.valid_from ASC NULLS LAST
                    LIMIT 100
                """,
                    (eid, eid),
                ).fetchall()
            else:
                rows = conn.execute("""
                    SELECT t.*, s.name as sub_name, o.name as obj_name
                    FROM triples t
                    JOIN entities s ON t.subject = s.id
                    JOIN entities o ON t.object = o.id
                    ORDER BY t.valid_from ASC NULLS LAST
                    LIMIT 100
                """).fetchall()

        return [
            {
                "subject": r["sub_name"],
                "predicate": r["predicate"],
                "object": r["obj_name"],
                "valid_from": r["valid_from"],
                "valid_to": r["valid_to"],
                "current": r["valid_to"] is None,
            }
            for r in rows
        ]

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
        ),
    }

    def apply_assert_op(self, payload: dict) -> bool:
        """Fold one replicated kg.assert op: G-set semantics, keyed by
        triple id (RFC 004 merge table). Entities are re-derivable from the
        payload names, so they are auto-created exactly as add_triple does.
        Never emits ops — this IS the application of one. Returns True if
        the triple row was inserted, False if it already existed."""
        triple_id = payload.get("triple_id")
        if not triple_id:
            raise ValueError("kg.assert payload is missing 'triple_id'")
        with self._lock:
            conn = self._conn()
            with conn:
                for eid, name in (
                    (payload.get("subject"), payload.get("subject_name")),
                    (payload.get("object"), payload.get("object_name")),
                ):
                    if eid:
                        conn.execute(
                            "INSERT OR IGNORE INTO entities (id, name) VALUES (?, ?)",
                            (eid, name or eid),
                        )
                cursor = conn.execute(
                    """INSERT OR IGNORE INTO triples (
                        id, subject, predicate, object, valid_from, valid_to,
                        confidence, source_closet, source_file,
                        source_drawer_id, adapter_name, extracted_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        triple_id,
                        payload.get("subject"),
                        payload.get("predicate"),
                        payload.get("object"),
                        payload.get("valid_from"),
                        payload.get("valid_to"),
                        payload.get("confidence", 1.0),
                        payload.get("source_closet"),
                        payload.get("source_file"),
                        payload.get("source_drawer_id"),
                        payload.get("adapter_name"),
                        payload.get("extracted_at"),
                    ),
                )
                return cursor.rowcount > 0

    def apply_close_op(self, payload: dict) -> int:
        """Fold one replicated kg.close op: interval-close, idempotent,
        min valid_to wins (RFC 004 merge table). Returns rows changed."""
        ended = payload.get("ended")
        if not ended:
            raise ValueError("kg.close payload is missing 'ended'")
        ended_key = _temporal_end_key(ended)
        changed = 0
        with self._lock:
            conn = self._conn()
            with conn:
                for triple_id in payload.get("closed_triple_ids") or []:
                    row = conn.execute(
                        "SELECT valid_to FROM triples WHERE id = ?", (triple_id,)
                    ).fetchone()
                    if row is None:
                        continue  # assert not yet folded; a later round closes it
                    current = row["valid_to"]
                    if current is not None and _temporal_end_key(current) <= ended_key:
                        continue  # already closed at least as early — min wins
                    conn.execute("UPDATE triples SET valid_to = ? WHERE id = ?", (ended, triple_id))
                    changed += 1
        return changed

    def apply_entity_upsert_op(self, payload: dict) -> None:
        """Fold one replicated kg.entity.upsert op: LWW register per entity
        key — callers fold ops in order, so last applied wins."""
        entity_id = payload.get("entity_id")
        if not entity_id:
            raise ValueError("kg.entity.upsert payload is missing 'entity_id'")
        props = json.dumps(payload.get("properties") or {})
        with self._lock:
            conn = self._conn()
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO entities (id, name, type, properties)"
                    " VALUES (?, ?, ?, ?)",
                    (
                        entity_id,
                        payload.get("name") or entity_id,
                        payload.get("type") or "unknown",
                        props,
                    ),
                )

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
            rows = conn.execute(
                f"SELECT rowid, {', '.join(columns)} FROM {table} "
                "WHERE rowid > ? ORDER BY rowid ASC LIMIT ?",
                (int(after_rowid), max(1, min(int(limit), 1000))),
            ).fetchall()
        return [dict(row) | {"_rowid": row["rowid"]} for row in rows]

    def apply_row(self, table: str, row: dict) -> None:
        """Fold one replicated KG row in, keyed by id (INSERT OR REPLACE).

        REPLACE makes invalidations (valid_to updates) and entity edits
        converge on re-pull; rows are never deleted by replication.
        """
        columns = self._REPLICATION_TABLES.get(table)
        if columns is None:
            raise ValueError(f"table must be one of {sorted(self._REPLICATION_TABLES)}")
        if not row.get("id"):
            raise ValueError("replicated row is missing 'id'")
        values = [row.get(col) for col in columns]
        with self._lock:
            conn = self._conn()
            with conn:
                conn.execute(
                    f"INSERT OR REPLACE INTO {table} ({', '.join(columns)}) "
                    f"VALUES ({', '.join('?' for _ in columns)})",
                    values,
                )

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
        return {
            "entities": entities,
            "triples": triples,
            "current_facts": current,
            "expired_facts": expired,
            "relationship_types": predicates,
        }

    # ── Seed from known facts ─────────────────────────────────────────────

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
