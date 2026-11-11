"""Topology facts in the workspace file ``topology.sqlite3``: upsert, read, conflicts, history.

One row in ``topology_facts`` is one structured fact, ``(subject, predicate, object)``
in the workspace's ``topology`` wing: ``node:web1 kind "server"``,
``link:laptop>web1:tailscale status "up"``, ``route:example.com hops [...]``. Every
observation of a fact is kept in ``topology_observations`` with its observer, time,
verbatim evidence and expiry. Two observers that disagree leave the current value in
place and file the other value as a contender in ``topology_conflicts``.

The file sits next to the other workspace stores, so it is encrypted with the workspace
and never mixes workspaces. The schema is additive and every write is one transaction:
a crash leaves the previous facts untouched. A read never creates the file. Callers
authorize first (``TOPOLOGY_WING`` through the token's wing grants); these functions
only store and read. The contract is in ``docs/topology.md``.
"""

from __future__ import annotations

import secrets
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from commonplace.admission import GATE
from mempalace.sqlite_crypto import connect, row_factory

from . import model
from .model import Observer, TopologyError

FILENAME = "topology.sqlite3"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS topology_facts (
    id TEXT PRIMARY KEY,
    wing TEXT NOT NULL DEFAULT 'topology',
    subject TEXT NOT NULL, predicate TEXT NOT NULL, object TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN
        ('current','contender','superseded','rejected','withdrawn','retracted')),
    superseded_by TEXT,
    observed_at TEXT NOT NULL, expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS topology_facts_current
    ON topology_facts(subject, predicate) WHERE state='current';
CREATE INDEX IF NOT EXISTS topology_facts_subject
    ON topology_facts(subject, predicate, state);
CREATE TABLE IF NOT EXISTS topology_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fact_id TEXT NOT NULL REFERENCES topology_facts(id),
    observer_kind TEXT NOT NULL CHECK(observer_kind IN ('agent','runner','owner')),
    observer_id TEXT NOT NULL, evidence TEXT NOT NULL,
    first_observed_at TEXT NOT NULL, observed_at TEXT NOT NULL,
    expires_at TEXT NOT NULL, seen_count INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS topology_observations_fact ON topology_observations(fact_id);
CREATE TABLE IF NOT EXISTS topology_conflicts (
    id TEXT PRIMARY KEY,
    subject TEXT NOT NULL, predicate TEXT NOT NULL,
    current_fact_id TEXT NOT NULL REFERENCES topology_facts(id),
    contender_fact_id TEXT NOT NULL UNIQUE REFERENCES topology_facts(id),
    state TEXT NOT NULL CHECK(state IN ('open','accepted','rejected','withdrawn','replaced')),
    resolved_by TEXT, resolved_at TEXT, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS topology_conflicts_open ON topology_conflicts(state);
"""


class NotFound(TopologyError):
    """No such conflict in this workspace."""


def open_topology(gateway, workspace_id: str) -> "TopologyStore":
    """The gateway's one store for a workspace; it follows the workspace's directory."""
    stores = gateway.__dict__.setdefault("_topology_stores", {})
    if workspace_id not in stores:
        supervisor = gateway.supervisor
        stores[workspace_id] = TopologyStore(lambda: supervisor.workspace_dir(workspace_id))
    return stores[workspace_id]


class TopologyStore:
    """One workspace's topology file. ``directory`` is a path or a callable returning it."""

    def __init__(self, directory):
        self._directory = directory if callable(directory) else lambda: directory
        self._lock = threading.RLock()
        self._conn = None
        self._path = None

    @property
    def path(self) -> Path:
        return Path(self._directory()).resolve() / FILENAME

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
            self._conn = self._path = None

    def _connection(self, create):
        path = self.path
        if path != self._path and self._conn is not None:
            self._conn.close()
            self._conn = None
        if self._conn is None:
            if not create and not path.exists():
                return None
            with GATE.creating(path):
                path.parent.mkdir(parents=True, exist_ok=True)
                conn = connect(str(path), timeout=5, check_same_thread=False, isolation_level=None)
            try:
                conn.row_factory = row_factory
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.executescript(_SCHEMA)
            except Exception:
                conn.close()
                raise
            self._conn, self._path = conn, path
        return self._conn

    @contextmanager
    def _write(self):
        with self._lock:
            conn = self._connection(create=True)
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    @contextmanager
    def _read(self):
        """Yields ``None`` for a workspace that never stored a topology fact."""
        with self._lock:
            yield self._connection(create=False)


def _store(topo) -> TopologyStore:
    if not isinstance(topo, TopologyStore):
        raise TopologyError("no: a TopologyStore is required")
    return topo


def _observer(observer) -> Observer:
    if not isinstance(observer, Observer):
        raise TopologyError("no: observer must be a topology Observer")
    return observer


# ── Writes ────────────────────────────────────────────────────────────────
def upsert(
    topo: TopologyStore,
    items: list,
    *,
    observer: Observer,
    evidence: str,
    ttl_seconds: int | None = None,
    replace: bool = False,
    now: datetime | None = None,
) -> list[dict]:
    """Store node, link and route items as facts, all or nothing.

    Per fact the result holds ``subject``, ``predicate``, ``fact_id`` and an ``action``:
    ``added`` (no current value), ``refreshed`` (same value seen again), ``superseded``
    (the value changed and nobody else vouches for the old one) or ``conflict`` (another
    observer's fresh value differs; the current value stays and ``conflict_id`` names
    the record). ``replace=True`` is the authoritative write for an owner-approved
    change: it supersedes whatever is current and closes open conflicts on the fact.
    """
    topo, observer = _store(topo), _observer(observer)
    facts = model.facts_for(items)
    evidence = model.evidence_text(evidence)
    now = model.clock(now)
    if not isinstance(replace, bool):
        raise TopologyError("no: replace must be true or false")
    deadlines = {
        predicate: model.stamp(model.expiry(predicate, now, ttl_seconds))
        for _, predicate, _ in facts
    }
    with topo._write() as conn:
        _require_identity(conn, facts)
        seen = (observer, evidence, model.stamp(now))
        return [
            _upsert_fact(conn, subject, predicate, value, seen, deadlines[predicate], replace)
            for subject, predicate, value in facts
        ]


def upsert_node(topo: TopologyStore, key: str, fields: dict, **write) -> list[dict]:
    """Upsert one node's fields; see :func:`upsert` for ``write`` and the result."""
    return upsert(topo, [{"node": key, "fields": fields}], **write)


def upsert_link(
    topo: TopologyStore, source: str, target: str, method: str, fields=None, **write
) -> list[dict]:
    """Upsert one link (``source`` reaches ``target`` by ``method``) and its fields."""
    return upsert(topo, [{"link": [source, target, method], "fields": fields or {}}], **write)


def upsert_route(topo: TopologyStore, site: str, hops: list, **write) -> list[dict]:
    """Upsert a site's route: the ordered link ids from the site to where it is served."""
    return upsert(topo, [{"route": site, "hops": hops}], **write)


def _require_identity(conn, facts):
    """A node's first write names its kind, so the map never holds a kindless node."""
    written = {(subject, predicate) for subject, predicate, _ in facts}
    for subject in {subject for subject, _, _ in facts if subject.startswith("node:")}:
        if (subject, "kind") in written or _current(conn, subject, "kind"):
            continue
        raise TopologyError("no: the first write for a node needs its kind")


def _current(conn, subject, predicate):
    return conn.execute(
        "SELECT * FROM topology_facts WHERE subject=? AND predicate=? AND state='current'",
        (subject, predicate),
    ).fetchone()


def _insert_fact(conn, subject, predicate, obj, state, stamp, expires):
    fact_id = "tf_" + secrets.token_hex(8)
    conn.execute(
        "INSERT INTO topology_facts (id,wing,subject,predicate,object,state,"
        "observed_at,expires_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            fact_id,
            model.TOPOLOGY_WING,
            subject,
            predicate,
            obj,
            state,
            stamp,
            expires,
            stamp,
            stamp,
        ),
    )
    return fact_id


def _observe(conn, fact_id, seen, expires):
    """Record one observation; an identical repeat bumps its row instead of adding one."""
    observer, evidence, stamp = seen
    row = conn.execute(
        "SELECT id FROM topology_observations WHERE fact_id=? AND observer_kind=? "
        "AND observer_id=? AND evidence=?",
        (fact_id, observer.kind, observer.id, evidence),
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE topology_observations SET observed_at=max(observed_at,?), "
            "expires_at=max(expires_at,?), seen_count=seen_count+1 WHERE id=?",
            (stamp, expires, row["id"]),
        )
    else:
        conn.execute(
            "INSERT INTO topology_observations (fact_id,observer_kind,observer_id,evidence,"
            "first_observed_at,observed_at,expires_at) VALUES (?,?,?,?,?,?,?)",
            (fact_id, observer.kind, observer.id, evidence, stamp, stamp, expires),
        )
    conn.execute(
        "UPDATE topology_facts SET observed_at=max(observed_at,?), "
        "expires_at=max(expires_at,?), updated_at=? WHERE id=?",
        (stamp, expires, stamp, fact_id),
    )


def _set_state(conn, fact_id, state, stamp, superseded_by=None):
    conn.execute(
        "UPDATE topology_facts SET state=?, superseded_by=COALESCE(?, superseded_by), "
        "updated_at=? WHERE id=?",
        (state, superseded_by, stamp, fact_id),
    )


def _close_conflict(conn, conflict_id, state, actor, stamp):
    conn.execute(
        "UPDATE topology_conflicts SET state=?, resolved_by=?, resolved_at=? WHERE id=?",
        (state, actor, stamp, conflict_id),
    )


def _open_conflicts(conn, subject, predicate):
    return conn.execute(
        "SELECT c.*, f.object AS contender_object FROM topology_conflicts c "
        "JOIN topology_facts f ON f.id=c.contender_fact_id "
        "WHERE c.subject=? AND c.predicate=? AND c.state='open' "
        "ORDER BY c.created_at, c.id",
        (subject, predicate),
    ).fetchall()


def _others_vouch(conn, fact_id, observer, stamp):
    """Does another observer hold a still-fresh observation of this fact?"""
    return (
        conn.execute(
            "SELECT 1 FROM topology_observations WHERE fact_id=? AND expires_at>? "
            "AND NOT (observer_kind=? AND observer_id=?) LIMIT 1",
            (fact_id, stamp, observer.kind, observer.id),
        ).fetchone()
        is not None
    )


def _withdraw_own(conn, subject, predicate, observer, stamp):
    """An observer that now agrees with the current value drops its own contenders."""
    for conflict in _open_conflicts(conn, subject, predicate):
        fact_id = conflict["contender_fact_id"]
        # The empty stamp counts every observation, fresh or not: sole observers only.
        if (
            _others_vouch(conn, fact_id, observer, "")
            or not conn.execute(
                "SELECT 1 FROM topology_observations WHERE fact_id=? AND observer_kind=? "
                "AND observer_id=?",
                (fact_id, observer.kind, observer.id),
            ).fetchone()
        ):
            continue
        _set_state(conn, fact_id, "withdrawn", stamp)
        _close_conflict(conn, conflict["id"], "withdrawn", f"{observer.kind}:{observer.id}", stamp)


def _promote(conn, current, new_object, seen, expires, *, replace, actor):
    """Make ``new_object`` the current value; the old value becomes history."""
    stamp = seen[2]
    subject, predicate = current["subject"], current["predicate"]
    conflicts = _open_conflicts(conn, subject, predicate)
    match = next((c for c in conflicts if c["contender_object"] == new_object), None)
    _set_state(conn, current["id"], "superseded", stamp)
    if match:
        fact_id = match["contender_fact_id"]
        _set_state(conn, fact_id, "current", stamp)
        _close_conflict(conn, match["id"], "accepted", actor, stamp)
    else:
        fact_id = _insert_fact(conn, subject, predicate, new_object, "current", stamp, expires)
    _set_state(conn, current["id"], "superseded", stamp, superseded_by=fact_id)
    for conflict in conflicts:
        if conflict is match:
            continue
        if replace:
            _set_state(conn, conflict["contender_fact_id"], "rejected", stamp)
            _close_conflict(conn, conflict["id"], "replaced", actor, stamp)
        else:
            conn.execute(
                "UPDATE topology_conflicts SET current_fact_id=? WHERE id=?",
                (fact_id, conflict["id"]),
            )
    return fact_id


def _upsert_fact(conn, subject, predicate, value, seen, expires, replace):
    observer, _, stamp = seen
    actor = f"{observer.kind}:{observer.id}"
    obj = model.encode(value)
    result = {"subject": subject, "predicate": predicate}
    current = _current(conn, subject, predicate)
    if current is None:
        fact_id = _insert_fact(conn, subject, predicate, obj, "current", stamp, expires)
        action = "added"
    elif current["object"] == obj:
        fact_id, action = current["id"], "refreshed"
        if replace:
            for conflict in _open_conflicts(conn, subject, predicate):
                _set_state(conn, conflict["contender_fact_id"], "rejected", stamp)
                _close_conflict(conn, conflict["id"], "replaced", actor, stamp)
        else:
            _withdraw_own(conn, subject, predicate, observer, stamp)
    elif (
        replace
        or current["expires_at"] <= stamp
        or not _others_vouch(conn, current["id"], observer, stamp)
    ):
        fact_id = _promote(conn, current, obj, seen, expires, replace=replace, actor=actor)
        action = "superseded"
        result["previous"] = model.decode(current["object"])
    else:
        action = "conflict"
        match = next(
            (c for c in _open_conflicts(conn, subject, predicate) if c["contender_object"] == obj),
            None,
        )
        if match:
            fact_id, result["conflict_id"] = match["contender_fact_id"], match["id"]
        else:
            fact_id = _insert_fact(conn, subject, predicate, obj, "contender", stamp, expires)
            result["conflict_id"] = "tc_" + secrets.token_hex(8)
            conn.execute(
                "INSERT INTO topology_conflicts (id,subject,predicate,"
                "current_fact_id,contender_fact_id,state,created_at) VALUES (?,?,?,?,?,'open',?)",
                (
                    result["conflict_id"],
                    subject,
                    predicate,
                    current["id"],
                    fact_id,
                    stamp,
                ),
            )
        result["current"] = model.decode(current["object"])
    _observe(conn, fact_id, seen, expires)
    return {**result, "fact_id": fact_id, "action": action}


def resolve_conflict(
    topo: TopologyStore, conflict_id: str, *, accept: bool, actor: str, now=None
) -> dict:
    """Settle an open conflict: ``accept`` makes the contender current, else it is rejected.

    The losing value stays in history. ``actor`` is recorded as ``resolved_by``; the
    caller checks that the actor may decide (the workspace owner).
    """
    topo = _store(topo)
    if not isinstance(accept, bool):
        raise TopologyError("no: accept must be true or false")
    if not isinstance(actor, str) or not model.OBSERVER_ID.fullmatch(actor):
        raise TopologyError("no: actor must be a user or connection id")
    stamp = model.stamp(model.clock(now))
    with topo._write() as conn:
        conflict = conn.execute(
            "SELECT * FROM topology_conflicts WHERE id=?", (conflict_id,)
        ).fetchone()
        if conflict is None:
            raise NotFound("no: conflict not found")
        if conflict["state"] != "open":
            raise TopologyError(f"no: conflict is already {conflict['state']}")
        contender = conflict["contender_fact_id"]
        if accept:
            current = _current(conn, conflict["subject"], conflict["predicate"])
            if current is not None:
                _set_state(conn, current["id"], "superseded", stamp, superseded_by=contender)
            _set_state(conn, contender, "current", stamp)
            conn.execute(
                "UPDATE topology_conflicts SET current_fact_id=? WHERE subject=? "
                "AND predicate=? AND state='open' AND id!=?",
                (contender, conflict["subject"], conflict["predicate"], conflict_id),
            )
        else:
            _set_state(conn, contender, "rejected", stamp)
        _close_conflict(conn, conflict_id, "accepted" if accept else "rejected", actor, stamp)
        return _conflict_view(
            conn,
            conn.execute("SELECT * FROM topology_conflicts WHERE id=?", (conflict_id,)).fetchone(),
            stamp,
        )


def retract(
    topo: TopologyStore,
    subject: str,
    *,
    observer: Observer,
    evidence: str,
    predicate: str | None = None,
    now: datetime | None = None,
) -> int:
    """Take a subject (or one of its fields) off the map; the facts stay as history.

    Returns how many facts were retracted. The retraction itself is recorded as an
    observation with its evidence. Links and routes that mention a retracted node are
    separate subjects and stay until they are retracted or expire.
    """
    topo, observer = _store(topo), _observer(observer)
    subject = _subject(subject)
    evidence = model.evidence_text(evidence)
    if predicate == _identity(subject):
        raise TopologyError("no: retract the whole subject, not its identity field")
    stamp = model.stamp(model.clock(now))
    with topo._write() as conn:
        rows = conn.execute(
            "SELECT * FROM topology_facts WHERE subject=? "
            "AND state IN ('current','contender')" + (" AND predicate=?" if predicate else ""),
            (subject,) + ((predicate,) if predicate else ()),
        ).fetchall()
        for row in rows:
            _observe(conn, row["id"], (observer, evidence, stamp), row["expires_at"])
            _set_state(conn, row["id"], "retracted", stamp)
        conn.execute(
            "UPDATE topology_conflicts SET state='replaced', resolved_by=?, resolved_at=? "
            "WHERE subject=? AND state='open'" + (" AND predicate=?" if predicate else ""),
            (f"{observer.kind}:{observer.id}", stamp, subject)
            + ((predicate,) if predicate else ()),
        )
        return sum(row["state"] == "current" for row in rows)


def _subject(subject) -> str:
    if isinstance(subject, str):
        kind, _, rest = subject.partition(":")
        if kind == "node":
            return model.node_subject(rest)
        if kind == "route":
            return model.route_subject(rest)
        if kind == "link":
            return model.link_subject(*model.parse_link_id(rest))
    raise TopologyError("no: subject must be node:<key>, link:<id> or route:<site>")


def _identity(subject) -> str:
    return {"node": "kind", "link": "method", "route": "hops"}[subject.partition(":")[0]]


# ── Reads ─────────────────────────────────────────────────────────────────
def _observations(conn, fact_ids, stamp):
    found = {fact_id: [] for fact_id in fact_ids}
    ids = list(fact_ids)
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        for row in conn.execute(
            "SELECT * FROM topology_observations WHERE fact_id IN (%s) ORDER BY observed_at DESC, id DESC"
            % ",".join("?" * len(chunk)),
            chunk,
        ):
            found[row["fact_id"]].append(
                {
                    "observer": {"kind": row["observer_kind"], "id": row["observer_id"]},
                    "evidence": row["evidence"],
                    "first_observed_at": row["first_observed_at"],
                    "observed_at": row["observed_at"],
                    "expires_at": row["expires_at"],
                    "stale": row["expires_at"] <= stamp,
                    "seen_count": row["seen_count"],
                }
            )
    return found


def _fact_view(row, observations, stamp) -> dict:
    return {
        "fact_id": row["id"],
        "value": model.decode(row["object"]),
        "state": row["state"],
        "observed_at": row["observed_at"],
        "expires_at": row["expires_at"],
        "stale": row["expires_at"] <= stamp,
        "provenance": observations.get(row["id"], []),
    }


def _conflict_view(conn, conflict, stamp) -> dict:
    facts = {
        row["id"]: row
        for row in conn.execute(
            "SELECT * FROM topology_facts WHERE id IN (?,?)",
            (conflict["current_fact_id"], conflict["contender_fact_id"]),
        )
    }
    observations = _observations(conn, facts, stamp)
    return {
        "conflict_id": conflict["id"],
        "subject": conflict["subject"],
        "predicate": conflict["predicate"],
        "state": conflict["state"],
        "created_at": conflict["created_at"],
        "resolved_by": conflict["resolved_by"],
        "resolved_at": conflict["resolved_at"],
        "current": _fact_view(facts[conflict["current_fact_id"]], observations, stamp),
        "contender": _fact_view(facts[conflict["contender_fact_id"]], observations, stamp),
    }


def _subjects(conn, stamp, subject=None) -> dict:
    """Current facts grouped by subject, each with provenance and open conflicts."""
    rows = conn.execute(
        "SELECT * FROM topology_facts WHERE state IN ('current','contender')"
        + (" AND subject=?" if subject else "")
        + " ORDER BY subject, predicate, created_at, id",
        ((subject,) if subject else ()),
    ).fetchall()
    observations = _observations(conn, [row["id"] for row in rows], stamp)
    conflict_ids = {
        row["contender_fact_id"]: row["id"]
        for row in conn.execute(
            "SELECT id, contender_fact_id FROM topology_conflicts WHERE state='open'"
        )
    }
    grouped = {}
    for row in rows:
        if row["state"] == "current":
            field = _fact_view(row, observations, stamp)
            field["conflicts"] = []
            grouped.setdefault(row["subject"], {})[row["predicate"]] = field
    for row in rows:
        field = grouped.get(row["subject"], {}).get(row["predicate"])
        if row["state"] == "contender" and field is not None and row["id"] in conflict_ids:
            view = _fact_view(row, observations, stamp)
            view["conflict_id"] = conflict_ids[row["id"]]
            field["conflicts"].append(view)
    return grouped


def _record(subject, fields) -> dict:
    """``values`` holds only fresh values; ``fields`` has every value with its detail."""
    kind, _, rest = subject.partition(":")
    identity = fields.get(_identity(subject))
    record = {
        "subject": subject,
        "wing": model.TOPOLOGY_WING,
        "values": {name: field["value"] for name, field in fields.items() if not field["stale"]},
        "fields": fields,
        "stale": identity is None or identity["stale"],
        "conflicted": any(field["conflicts"] for field in fields.values()),
    }
    if kind == "node":
        record["key"] = rest
    elif kind == "link":
        record["id"] = rest
        record["from"], record["to"], record["method"] = model.parse_link_id(rest)
    else:
        record["site"] = rest
    return record


def _hops(record, links) -> list[dict]:
    """Each hop of a route with what is known about its link right now."""
    hops = []
    for hop in record["fields"]["hops"]["value"]:
        source, target, method = model.parse_link_id(hop)
        link = links.get("link:" + hop)
        hops.append(
            {
                "link": hop,
                "from": source,
                "to": target,
                "method": method,
                "known": link is not None,
                "stale": link is None or link["stale"],
                "status": (link or {"values": {}})["values"].get("status", "unknown"),
            }
        )
    return hops


def _read(topo, now, subject=None):
    topo = _store(topo)
    stamp = model.stamp(model.clock(now))
    with topo._read() as conn:
        return {} if conn is None else _subjects(conn, stamp, subject)


def get_node(topo: TopologyStore, key: str, *, now: datetime | None = None) -> dict | None:
    """One node with its fields, provenance, freshness and open conflicts, or ``None``."""
    subject = model.node_subject(key)
    grouped = _read(topo, now, subject)
    return _record(subject, grouped[subject]) if subject in grouped else None


def get_link(
    topo: TopologyStore, source: str, target: str, method: str, *, now: datetime | None = None
) -> dict | None:
    """One link (``source`` reaches ``target`` by ``method``), or ``None``."""
    subject = model.link_subject(source, target, method)
    grouped = _read(topo, now, subject)
    return _record(subject, grouped[subject]) if subject in grouped else None


def get_route(topo: TopologyStore, site: str, *, now: datetime | None = None) -> dict | None:
    """A site's route with every hop resolved against the link facts, or ``None``."""
    site = model.node_key(site, "site")
    routes = read_graph(topo, now=now)["routes"]
    return next((route for route in routes if route["site"] == site), None)


def read_graph(topo: TopologyStore, *, now: datetime | None = None) -> dict:
    """The whole map: ``nodes``, ``links``, ``routes`` and the open ``conflicts``.

    A workspace that never stored a topology fact returns four empty lists.
    """
    grouped = _read(topo, now)
    records = {subject: _record(subject, fields) for subject, fields in grouped.items()}
    graph = {"wing": model.TOPOLOGY_WING, "nodes": [], "links": [], "routes": []}
    for subject, record in records.items():
        kind = subject.partition(":")[0]
        if kind == "route" and "hops" in record["fields"]:
            record["hops"] = _hops(record, records)
        graph[kind + "s"].append(record)
    graph["conflicts"] = list_conflicts(topo, now=now)
    return graph


def list_conflicts(
    topo: TopologyStore, *, open_only: bool = True, now: datetime | None = None
) -> list[dict]:
    """Conflict records, oldest first, each with both values and their provenance."""
    topo = _store(topo)
    stamp = model.stamp(model.clock(now))
    with topo._read() as conn:
        if conn is None:
            return []
        rows = conn.execute(
            "SELECT * FROM topology_conflicts"
            + (" WHERE state='open'" if open_only else "")
            + " ORDER BY created_at, id"
        ).fetchall()
        return [_conflict_view(conn, row, stamp) for row in rows]


def history(
    topo: TopologyStore, subject: str, *, predicate: str | None = None, now=None
) -> list[dict]:
    """Every value a subject ever held, newest first, including superseded ones."""
    topo, subject = _store(topo), _subject(subject)
    stamp = model.stamp(model.clock(now))
    with topo._read() as conn:
        if conn is None:
            return []
        rows = conn.execute(
            "SELECT * FROM topology_facts WHERE subject=?"
            + (" AND predicate=?" if predicate else "")
            + " ORDER BY created_at DESC, rowid DESC",
            (subject,) + ((predicate,) if predicate else ()),
        ).fetchall()
        observations = _observations(conn, [row["id"] for row in rows], stamp)
        return [
            {
                **_fact_view(row, observations, stamp),
                "predicate": row["predicate"],
                "superseded_by": row["superseded_by"],
            }
            for row in rows
        ]
