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
from datetime import date
from pathlib import Path
from typing import Optional

from . import paths
from .config import sanitize_iso_temporal
from .ids import make_triple_id
from .timeutil import utc_now_iso, utc_now_z

DEFAULT_KG_PATH = paths.legacy_knowledge_graph_path()
_MIN_ENTITY_CANDIDATE_LEN = 3


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
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def import_graph_rows(source_path: str, target_path: str, apply: bool = False) -> dict:
    """Copy one graph file's entities and triples into another, keeping what is there.

    The source is only read. An entity the target already has (same id) is left
    as it is; so is a fact it already records with the same subject, predicate,
    object and validity window, whenever it was recorded. A current fact whose
    subject and predicate already hold a different current object in the target
    is not copied but listed under ``conflicts``, so the two never both read as
    current. A reused fact id with different content or validity is kept in
    the target and listed under ``id_conflicts``. With ``apply`` false the
    import runs against an in-memory snapshot: no stored facts change and a
    missing target is not created. The counts say what would be copied.
    """
    if paths.same_path(source_path, target_path):
        raise ValueError("the source and the target are the same knowledge graph")
    source = _read_only(source_path)
    target = None
    kg = None

    def found(sql, args):
        return target.execute(sql, args).fetchone() is not None

    counts = {}
    try:
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
        for table, columns in KnowledgeGraph._REPLICATION_TABLES.items():
            available = {r["name"] for r in source.execute(f"PRAGMA table_info({table})")}
            cols = [c for c in columns if c in available]
            numbers = {"added": 0, "present": 0}
            if table == "triples":
                numbers["conflicts"] = []
                numbers["id_conflicts"] = []
            counts[table] = numbers
            if "id" not in cols:
                continue
            insert = (
                f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})"
            )
            for row in source.execute(f"SELECT {', '.join(cols)} FROM {table}"):
                if table == "triples":
                    fact = (row["subject"], row["predicate"], row["object"])
                    window = (row["valid_from"], row["valid_to"])
                    if found(
                        "SELECT 1 FROM triples WHERE subject = ? AND predicate = ? "
                        "AND object = ? AND valid_from IS ? AND valid_to IS ?",
                        fact + window,
                    ):
                        numbers["present"] += 1
                        continue
                    if found("SELECT 1 FROM triples WHERE id = ?", (row["id"],)):
                        numbers["id_conflicts"].append(row["id"])
                        continue
                    clash = (
                        target.execute(
                            "SELECT object FROM triples WHERE subject = ? AND predicate = ? "
                            "AND object != ? AND valid_to IS NULL",
                            fact,
                        ).fetchone()
                        if row["valid_to"] is None
                        else None
                    )
                    if clash is not None:
                        numbers["conflicts"].append(
                            {
                                "subject": row["subject"],
                                "predicate": row["predicate"],
                                "object": row["object"],
                                "current_object": clash[0],
                            }
                        )
                        continue
                elif found(f"SELECT 1 FROM {table} WHERE id = ?", (row["id"],)):
                    numbers["present"] += 1
                    continue
                inserted = target.execute(insert, tuple(row)).rowcount
                if inserted != 1:
                    raise sqlite3.IntegrityError(
                        f"knowledge graph import did not insert {table} row {row['id']!r}"
                    )
                numbers["added"] += inserted
        if apply:
            target.commit()
    finally:
        source.close()
        if kg is not None:
            kg.close()
    return counts


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
            with conn:
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

        pred = predicate.lower().replace(" ", "_")

        # Auto-create entities if they don't exist
        with self._lock:
            conn = self._conn()
            with conn:
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

                # Check for existing identical triple
                existing = conn.execute(
                    "SELECT id FROM triples WHERE subject=? AND predicate=? AND object=? AND valid_to IS NULL",
                    (sub_id, pred, obj_id),
                ).fetchone()
                if existing:
                    return existing["id"]  # Already exists and still valid

                # An identical closed assertion is a replay, not a new fact.
                # The open-row check above only deduplicates open triples, so
                # retrying a historical (closed) assertion after an
                # interruption used to insert a second row with a new ID.
                # Different intervals or provenance stay distinguishable.
                if valid_to is not None:
                    replayed = conn.execute(
                        """SELECT id FROM triples
                        WHERE subject=? AND predicate=? AND object=?
                          AND valid_from IS ? AND valid_to IS ?
                          AND confidence IS ? AND source_closet IS ?
                          AND source_file IS ? AND source_drawer_id IS ?
                          AND adapter_name IS ?""",
                        (
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
                        ),
                    ).fetchone()
                    if replayed:
                        return replayed["id"]  # Identical closed assertion already stored

                triple_id = make_triple_id(sub_id, pred, obj_id, valid_from, utc_now_iso())
                conn.execute(
                    """INSERT INTO triples (
                        id, subject, predicate, object, valid_from, valid_to,
                        confidence, source_closet, source_file,
                        source_drawer_id, adapter_name
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                    ),
                )
                return triple_id

    def invalidate(self, subject: str, predicate: str, obj: str, ended: str = None):
        """Mark a relationship as no longer valid (set valid_to date/time)."""
        pred = predicate.lower().replace(" ", "_")
        ended = sanitize_iso_temporal(ended or date.today().isoformat(), "ended")

        with self._lock:
            conn = self._conn()
            with conn:
                sub_id = self._resolve_entity_id(conn, subject)
                obj_id = self._resolve_entity_id(conn, obj)
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

        pred = predicate.lower().replace(" ", "_")

        with self._lock:
            conn = self._conn()
            with conn:
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
                    "SELECT valid_from FROM triples "
                    "WHERE subject=? AND predicate=? AND object=? AND valid_to IS NULL",
                    (sub_id, pred, old_id),
                ).fetchall()
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
                    "WHERE subject=? AND predicate=? AND object=? AND valid_to IS NULL",
                    (boundary, sub_id, pred, old_id),
                )

                # Open the successor at the same instant (idempotent if already open).
                existing = conn.execute(
                    "SELECT id FROM triples "
                    "WHERE subject=? AND predicate=? AND object=? AND valid_to IS NULL",
                    (sub_id, pred, new_id),
                ).fetchone()
                if existing:
                    return existing["id"]

                triple_id = make_triple_id(sub_id, pred, new_id, boundary, utc_now_iso())
                conn.execute(
                    """INSERT INTO triples (
                        id, subject, predicate, object, valid_from, valid_to,
                        confidence, source_closet, source_file,
                        source_drawer_id, adapter_name
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                    ),
                )
                return triple_id

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
        ``adapter_name``): rewording a predicate is not new evidence, and a
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
            with conn:
                obj_id = self._resolve_entity_id(conn, obj)
                row = conn.execute(
                    "SELECT subject, valid_from, confidence, source_closet, source_file, "
                    "source_drawer_id, adapter_name FROM triples "
                    "WHERE id=? AND valid_to IS NULL",
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
                    "AND source_drawer_id IS ? AND adapter_name IS ?",
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
                    ),
                ).fetchone()
                if existing:
                    return existing["id"]
                new_id = make_triple_id(sub_id, pred, obj_id, boundary, utc_now_iso())
                conn.execute(
                    """INSERT INTO triples (
                        id, subject, predicate, object, valid_from, valid_to,
                        confidence, source_closet, source_file,
                        source_drawer_id, adapter_name
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                    ),
                )
                return new_id

    # ── Query operations ──────────────────────────────────────────────────

    def query_entity(self, name: str, as_of: str = None, direction: str = "outgoing"):
        """
        Get all relationships for an entity.

        direction: "outgoing" (entity → ?), "incoming" (? → entity), "both"
        as_of: ISO date or canonical UTC datetime — only return facts valid then
        """
        as_of = sanitize_iso_temporal(as_of, "as_of")
        results = []

        temporal_sql = ""
        temporal_params = []
        if as_of:
            temporal_sql, temporal_params = _temporal_filter_sql(as_of)

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

            if not results:
                exact_exists = (
                    conn.execute("SELECT 1 FROM entities WHERE id = ?", (eid,)).fetchone()
                    is not None
                )
                if not exact_exists:
                    candidates = self._lookup_entity_candidates(conn, name, eid)
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

    def find_entity_candidates(self, name: str) -> list:
        """Return token/prefix entity matches for disambiguation (never substring)."""
        if not name or len(name.strip()) < _MIN_ENTITY_CANDIDATE_LEN:
            return []
        with self._lock:
            conn = self._conn()
            eid = self._resolve_entity_id(conn, name)
            return self._lookup_entity_candidates(conn, name, eid)

    def _lookup_entity_candidates(self, conn, name: str, eid: str) -> list:
        if not name or len(name.strip()) < _MIN_ENTITY_CANDIDATE_LEN:
            return []
        name_esc = _escape_like(name.strip())
        id_esc = _escape_like(eid)
        rows = conn.execute(
            "SELECT id, name FROM entities WHERE "
            "id = ? OR name = ? OR "
            "name LIKE ? ESCAPE '\\' OR name LIKE ? ESCAPE '\\' OR name LIKE ? ESCAPE '\\' OR "
            "id LIKE ? ESCAPE '\\' OR id LIKE ? ESCAPE '\\' OR id LIKE ? ESCAPE '\\' "
            "LIMIT 5",
            (
                eid,
                name,
                f"{name_esc} %",
                f"% {name_esc}",
                f"% {name_esc} %",
                f"{id_esc}\\_%",
                f"%\\_{id_esc}",
                f"%\\_{id_esc}\\_%",
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

    def timeline(self, entity_name: str = None, limit: int = 100, offset: int = 0):
        """Get facts in chronological order, optionally filtered by entity.

        Paginated with ``limit``/``offset`` (same convention as drawer
        listing); defaults preserve the historical behavior of returning
        the first 100 facts. Use :meth:`timeline_total` for the full
        matching count.
        """
        limit = max(1, int(limit))
        offset = max(0, int(offset))
        with self._lock:
            conn = self._conn()
            if entity_name:
                eid = self._resolve_entity_id(conn, entity_name)
                rows = conn.execute(
                    """
                    SELECT t.*, s.name as sub_name, o.name as obj_name
                    FROM triples t
                    JOIN entities s ON t.subject = s.id
                    JOIN entities o ON t.object = o.id
                    WHERE (t.subject = ? OR t.object = ?)
                    ORDER BY t.valid_from ASC NULLS LAST, t.id ASC
                    LIMIT ? OFFSET ?
                """,
                    (eid, eid, limit, offset),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT t.*, s.name as sub_name, o.name as obj_name
                    FROM triples t
                    JOIN entities s ON t.subject = s.id
                    JOIN entities o ON t.object = o.id
                    ORDER BY t.valid_from ASC NULLS LAST, t.id ASC
                    LIMIT ? OFFSET ?
                """,
                    (limit, offset),
                ).fetchall()

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

    def timeline_total(self, entity_name: str = None) -> int:
        """Total number of facts a :meth:`timeline` query matches (all pages)."""
        with self._lock:
            conn = self._conn()
            if entity_name:
                eid = self._resolve_entity_id(conn, entity_name)
                row = conn.execute(
                    "SELECT COUNT(*) FROM triples WHERE subject = ? OR object = ?",
                    (eid, eid),
                ).fetchone()
            else:
                row = conn.execute("SELECT COUNT(*) FROM triples").fetchone()
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
