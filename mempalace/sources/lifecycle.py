"""Source-owned drawer generations (internal RFC 002 lifecycle foundation).

Content reads, including raw SQLite paths, share this visibility rule. The
registry is independent of the knowledge graph's movable resolver.
"""

from __future__ import annotations

import contextlib
import os
import re
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .base import SourceAdapterError

_REGISTRY_FILENAME = "source_lifecycle.sqlite3"
_SCHEMA_VERSION = 2
_STATE_CACHE_LIMIT = 1024
_STATES = {"staging", "active", "retired", "abandoned"}


class LifecycleStateError(SourceAdapterError):
    """Generation content cannot be safely resolved against its registry."""


class LifecycleConflictError(LifecycleStateError):
    """A transition lost ownership or was based on an obsolete cursor."""


def lifecycle_path(palace_path: str) -> Path:
    """Return the stable per-palace registry location without touching disk."""
    return Path(palace_path).expanduser().resolve() / _REGISTRY_FILENAME


@dataclass(frozen=True)
class SourceGeneration:
    """Immutable owner and version of one staged extraction."""

    adapter_name: str
    source_file: str
    generation: str
    version: str
    previous: Optional[str] = None
    deleted: bool = False

    @property
    def metadata(self) -> dict:
        """Core-owned stamps for drawers in this generation."""
        return {
            "adapter_name": self.adapter_name,
            "source_file": self.source_file,
            "source_generation": self.generation,
            "source_version": self.version,
        }

    @property
    def cursor(self) -> dict:
        """The same generic currentness cursor for populated and empty items.

        Consumers compare ``source_version``; absent adapter-specific metadata
        must never mean current. A deletion is an explicit cursor, not absence.
        """
        return {**self.metadata, "source_deleted": self.deleted}


def _validate_owner(adapter_name: str, source_file: str) -> None:
    if not isinstance(adapter_name, str) or not adapter_name:
        raise ValueError("adapter_name must be a non-empty string")
    if not isinstance(source_file, str) or not source_file:
        raise ValueError("source_file must be a non-empty string")


_SCHEMA = {
    "source_generations": "CREATE TABLE source_generations ("
    "adapter TEXT NOT NULL, source TEXT NOT NULL, generation TEXT NOT NULL, "
    "version TEXT NOT NULL, previous TEXT, deleted INTEGER NOT NULL "
    "CHECK(deleted IN (0,1)), state TEXT NOT NULL "
    "CHECK(state IN ('staging','active','retired','abandoned')), "
    "PRIMARY KEY(adapter, source, generation))",
    "source_lifecycle_state": "CREATE TABLE source_lifecycle_state ("
    "singleton INTEGER PRIMARY KEY CHECK(singleton=1), epoch INTEGER NOT NULL CHECK(epoch>=0))",
    **{
        f"one_{state}_source_generation": f"CREATE UNIQUE INDEX one_{state}_source_generation "
        f"ON source_generations(adapter, source) WHERE state='{state}'"
        for state in ("active", "staging")
    },
}


class StaleLifecycleSnapshot(LifecycleStateError):
    """A backend row is newer than the registry snapshot, or its state was lost."""


@contextlib.contextmanager
def reclamation_lease(palace_path, *, exclusive=False):
    """Cross-platform shared reader / nonblocking exclusive reclamation lease.

    The writer creates the rendezvous file; readers never create it. It is
    never unlinked. The OS releases ownership on process death.
    """
    from ..palace import _lock_mine_lock_file, _unlock_mine_lock_file

    path = lifecycle_path(palace_path).with_suffix(".readers.lock")
    try:
        handle = open(path, "r+b" if exclusive else "rb")
    except OSError as exc:
        raise LifecycleStateError("source lifecycle reader lock missing or unavailable") from exc
    locked = False
    try:
        locked = _lock_mine_lock_file(handle, blocking=not exclusive, shared=not exclusive)
        if not locked:
            raise LifecycleConflictError(
                "source readers still hold a generation; retry reclamation"
            )
        yield
    finally:
        try:
            if locked:
                _unlock_mine_lock_file(handle)
        finally:
            handle.close()


def _check_schema(conn) -> None:
    if conn.execute("PRAGMA user_version").fetchone()[0] != _SCHEMA_VERSION:
        raise LifecycleStateError("unsupported source lifecycle registry schema")
    actual = dict(conn.execute("SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL"))

    def normalize(sql):
        return re.sub(r"\s+", "", sql).lower()

    if any(normalize(actual.get(name, "")) != normalize(sql) for name, sql in _SCHEMA.items()):
        raise LifecycleStateError("invalid source lifecycle registry schema invariants")
    rows = conn.execute("SELECT singleton, epoch FROM source_lifecycle_state").fetchall()
    if len(rows) != 1 or rows[0][0] != 1 or not isinstance(rows[0][1], int) or rows[0][1] < 0:
        raise LifecycleStateError("invalid source lifecycle visibility epoch")


def visibility_epoch(palace_path):
    """Read a durable cache token; missing legacy state does not create files."""
    path = lifecycle_path(palace_path)
    if not path.exists():
        return None
    with SourceLifecycleSnapshot(palace_path) as snapshot:
        return (
            str(path),
            snapshot._identity,
            snapshot._connection()
            .execute("SELECT epoch FROM source_lifecycle_state WHERE singleton=1")
            .fetchone()[0],
        )


def _generation(row) -> Optional[SourceGeneration]:
    if row is None:
        return None
    if (
        not all(isinstance(value, str) and value for value in row[:3])
        or not isinstance(row[3], str)
        or (row[4] is not None and (not isinstance(row[4], str) or not row[4]))
        or row[5] not in (0, 1)
    ):
        raise LifecycleStateError("invalid source generation cursor in registry")
    return SourceGeneration(*row[:5], deleted=bool(row[5]))


class SourceLifecycleSnapshot:
    """Lazy no-create reader with one repeatable activation snapshot.

    Legacy rows do not need a registry. A tagged row without valid state raises
    a diagnostic, never becomes legacy content. Readers keep the SQLite read
    transaction until close; physical retirement needs a separate reader-safe
    reclamation protocol and must not delete rows behind this snapshot.
    """

    def __init__(self, palace_path: str):
        self.path = lifecycle_path(palace_path)
        self._conn = None
        self._closed = False
        self._lease = None
        self._entered_missing = False
        self._states: dict[tuple, bool] = {}
        self._legacy_owners: dict[tuple, bool] = {}
        self._identity = None

    def __enter__(self):
        if self.path.exists():
            self._connection()
        else:
            self._entered_missing = True
        return self

    def __exit__(self, *exc_info):
        self.close()

    def close(self) -> None:
        """Release the snapshot; idempotent even after a failed open."""
        conn, lease = self._conn, self._lease
        self._conn = self._lease = None
        self._closed = True
        try:
            if conn is not None:
                conn.close()
        finally:
            if lease is not None:
                lease.__exit__(None, None, None)

    def _connection(self):
        if self._closed:
            raise LifecycleStateError("source lifecycle snapshot is closed")
        if self._entered_missing:
            raise StaleLifecycleSnapshot(
                "source generation registry missing or invalid at snapshot open; restore the matching registry"
            )
        if self._conn is None:
            try:
                if self.path.exists():
                    self._lease = reclamation_lease(str(self.path.parent))
                    self._lease.__enter__()
                # No exists-check/connect race, no writable fallback, and no
                # immutable=1 (which would ignore a live registry WAL).
                conn = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
                try:
                    conn.execute("PRAGMA query_only=ON")
                    conn.execute("BEGIN")
                    _check_schema(conn)
                except BaseException:
                    conn.close()
                    raise
                stat = self.path.stat()
                self._identity = (stat.st_dev, stat.st_ino)
                self._conn = conn
            except (sqlite3.Error, LifecycleStateError) as exc:
                if self._lease is not None:
                    self._lease.__exit__(None, None, None)
                    self._lease = None
                raise LifecycleStateError(
                    f"source generation registry missing or invalid at {self.path}; "
                    "restore the matching registry and drawer store together"
                ) from exc
        return self._conn

    def _owned(self, adapter_name, source_file, state):
        _validate_owner(adapter_name, source_file)
        try:
            rows = (
                self._connection()
                .execute(
                    "SELECT adapter, source, generation, version, previous, deleted "
                    "FROM source_generations WHERE adapter=? AND source=? AND state=?",
                    (adapter_name, source_file, state),
                )
                .fetchmany(2)
            )
        except sqlite3.Error as exc:
            raise LifecycleStateError("invalid source lifecycle registry tables") from exc
        if len(rows) > 1:
            raise LifecycleStateError(f"ambiguous {state} source generations in registry")
        return _generation(rows[0] if rows else None)

    def current(self, adapter_name: str, source_file: str) -> Optional[SourceGeneration]:
        """Read the active generic cursor; registry must exist and be valid."""
        return self._owned(adapter_name, source_file, "active")

    def pending(self, adapter_name: str, source_file: str) -> Optional[SourceGeneration]:
        """Read a crash-orphan handle for explicit recovery under the writer lease."""
        return self._owned(adapter_name, source_file, "staging")

    def visible(self, metadata: Optional[dict]) -> bool:
        """True only for legacy rows or a registered active owned generation."""
        if self._closed:
            raise LifecycleStateError("source lifecycle snapshot is closed")
        meta = metadata or {}
        if "source_generation" not in meta and "source_version" not in meta:
            # First managed activation replaces this exact producer's legacy
            # rows too. Keep their bytes; old snapshots still see them. Rows
            # without both owner stamps cannot be attributed and stay legacy.
            owner = (meta.get("adapter_name"), meta.get("source_file"))
            registry_present = self._conn is not None or (
                not self._entered_missing and self.path.exists()
            )
            if registry_present and all(isinstance(v, str) and v for v in owner):
                if owner not in self._legacy_owners:
                    if len(self._legacy_owners) >= _STATE_CACHE_LIMIT:
                        self._legacy_owners.pop(next(iter(self._legacy_owners)))
                    self._legacy_owners[owner] = self.current(*owner) is None
                return self._legacy_owners[owner]
            return True
        owner = (meta.get("adapter_name"), meta.get("source_file"))
        generation = meta.get("source_generation")
        if not all(isinstance(v, str) and v for v in (*owner, generation)):
            raise LifecycleStateError("generation drawer has invalid ownership metadata")
        version = meta.get("source_version")
        if not isinstance(version, str):
            raise LifecycleStateError("generation drawer has invalid source_version metadata")
        key = (*owner, generation, version)
        if key not in self._states:
            try:
                row = (
                    self._connection()
                    .execute(
                        "SELECT state, deleted, version FROM source_generations "
                        "WHERE adapter=? AND source=? AND generation=?",
                        key[:3],
                    )
                    .fetchone()
                )
            except sqlite3.Error as exc:
                raise LifecycleStateError("invalid source lifecycle registry tables") from exc
            if row is None:
                raise StaleLifecycleSnapshot("generation has no activation state in this snapshot")
            if row[2] != version:
                raise LifecycleStateError(
                    "generation drawer source_version does not match registry"
                )
            if row[0] not in _STATES or row[1] not in (0, 1):
                raise LifecycleStateError(
                    "generation drawer has no valid activation state; "
                    "restore the matching registry and drawer store together"
                )
            if row[0] == "active":
                # A damaged/restored registry may have lost its unique index.
                # Never expose two competing active generations in that case.
                active = self.current(*owner)
                if active is None or active.generation != generation:
                    raise LifecycleStateError("invalid active source generation in registry")
            if len(self._states) >= _STATE_CACHE_LIMIT:
                self._states.pop(next(iter(self._states)))
            self._states[key] = row[0] == "active" and not row[1]
        return self._states[key]


def _check_initialization_safe(palace_path):
    """A vector-only restore is not a new registry. Inspect without creation."""
    from ..backends._inproc_sqlite import open_reader
    from ..config import get_configured_collection_name

    name = get_configured_collection_name()
    for filename, backend in (("chroma.sqlite3", "chroma"), ("sqlite_exact.sqlite3", "exact")):
        db = palace_path / filename
        if not db.exists():
            continue
        conn = (
            open_reader(db)
            if backend == "chroma"
            else sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)
        )
        try:
            if backend == "chroma":
                tagged = conn.execute(
                    "SELECT 1 FROM embedding_metadata m JOIN embeddings e ON e.id=m.id "
                    "JOIN segments s ON s.id=e.segment_id JOIN collections c ON c.id=s.collection "
                    "WHERE c.name=? AND m.key IN ('source_generation','source_version') LIMIT 1",
                    (name,),
                ).fetchone()
                if (
                    not tagged
                    and conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE name='embedding_metadata_array'"
                    ).fetchone()
                ):
                    tagged = conn.execute(
                        "SELECT 1 FROM embedding_metadata_array m JOIN embeddings e ON e.id=m.id "
                        "JOIN segments s ON s.id=e.segment_id JOIN collections c ON c.id=s.collection "
                        "WHERE c.name=? AND m.key IN ('source_generation','source_version') LIMIT 1",
                        (name,),
                    ).fetchone()
            else:
                tagged = conn.execute(
                    "SELECT 1 FROM documents d JOIN collections c ON c.id=d.collection_id "
                    "WHERE c.name=? AND (json_type(metadata_json, '$.source_generation') IS NOT NULL "
                    "OR json_type(metadata_json, '$.source_version') IS NOT NULL) LIMIT 1",
                    (name,),
                ).fetchone()
            if tagged:
                raise LifecycleStateError(
                    "managed drawers exist without their registry; restore the matching registry"
                )
        finally:
            conn.close()


class SourceLifecycleStore:
    """Writer-owned registry; callers must hold the palace's writer lease.

    SQLite transactions guard transitions too: a second staging attempt for
    the same owner is refused rather than replacing its pending generation.
    Interrupted staging is retained hidden until explicitly abandoned while
    holding that lease. No transition deletes another producer or legacy row.
    """

    def __init__(self, palace_path: str):
        self.palace_path = palace_path
        self.path = lifecycle_path(palace_path)
        if not self.path.exists():
            _check_initialization_safe(self.path.parent)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # This unpublished schema intentionally has no migration from the
        # earlier review-only prototype. Never silently repair unknown state.
        with contextlib.closing(sqlite3.connect(self.path)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                if version == 0:
                    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table'").fetchone():
                        raise LifecycleStateError("refusing to initialize an unrecognized registry")
                    for sql in _SCHEMA.values():
                        conn.execute(sql)
                    conn.execute("INSERT INTO source_lifecycle_state VALUES(1,0)")
                    conn.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
                _check_schema(conn)
        lock_path = self.path.with_suffix(".readers.lock")
        fd = os.open(lock_path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)

    @contextlib.contextmanager
    def _transaction(self):
        # A lost registry must not be recreated by a later transition.
        conn = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True)
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                _check_schema(conn)
                yield conn
                # Every registry mutation changes the snapshot token, including
                # begin: a backend can see newly staged rows before activation.
                conn.execute("UPDATE source_lifecycle_state SET epoch=epoch+1 WHERE singleton=1")
        finally:
            conn.close()

    def snapshot(self) -> SourceLifecycleSnapshot:
        """Return a lazy no-create view of this registry."""
        return SourceLifecycleSnapshot(self.palace_path)

    def begin(
        self, adapter_name: str, source_file: str, version: str, *, deleted: bool = False
    ) -> SourceGeneration:
        """Stage a fresh generation without changing the last good cursor."""
        _validate_owner(adapter_name, source_file)
        if not isinstance(version, str):
            raise ValueError("source version must be a string")
        if not isinstance(deleted, bool):
            raise ValueError("deleted must be a boolean")
        with self._transaction() as conn:
            if conn.execute(
                "SELECT 1 FROM source_generations WHERE adapter=? AND source=? "
                "AND state IN ('staging','abandoned') LIMIT 1",
                (adapter_name, source_file),
            ).fetchone():
                raise LifecycleConflictError(
                    "source item already has a staged or abandoned generation; reclaim before retry"
                )
            previous = conn.execute(
                "SELECT generation FROM source_generations "
                "WHERE adapter=? AND source=? AND state='active'",
                (adapter_name, source_file),
            ).fetchone()
            generation = SourceGeneration(
                adapter_name,
                source_file,
                uuid.uuid4().hex,
                version,
                previous[0] if previous else None,
                deleted,
            )
            try:
                conn.execute(
                    "INSERT INTO source_generations VALUES(?,?,?,?,?,?, 'staging')",
                    (
                        adapter_name,
                        source_file,
                        generation.generation,
                        version,
                        generation.previous,
                        int(deleted),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise LifecycleConflictError(
                    "source item already has a staged generation; recover under the writer lease"
                ) from exc
        return generation

    def activate(self, generation: SourceGeneration) -> None:
        """Atomically replace the active cursor after every drawer write succeeded."""
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT version, previous, deleted, state FROM source_generations "
                "WHERE adapter=? AND source=? AND generation=?",
                (generation.adapter_name, generation.source_file, generation.generation),
            ).fetchone()
            expected = (generation.version, generation.previous, int(generation.deleted), "staging")
            if row != expected:
                raise LifecycleConflictError("generation is not the owned staging record")
            active = conn.execute(
                "SELECT generation FROM source_generations "
                "WHERE adapter=? AND source=? AND state='active'",
                (generation.adapter_name, generation.source_file),
            ).fetchone()
            if (active[0] if active else None) != generation.previous:
                raise LifecycleConflictError("active source generation changed during extraction")
            if conn.execute(
                "SELECT 1 FROM source_generations WHERE adapter=? AND source=? "
                "AND state='retired' LIMIT 1",
                (generation.adapter_name, generation.source_file),
            ).fetchone():
                raise LifecycleConflictError(
                    "retired generation requires reclamation before activation"
                )
            conn.execute(
                "UPDATE source_generations SET state='retired' "
                "WHERE adapter=? AND source=? AND state='active'",
                (generation.adapter_name, generation.source_file),
            )
            self._set_state(conn, generation, "active")

    def abandon(self, generation: SourceGeneration) -> None:
        """Hide failed staging permanently; never abandon an activated generation."""
        with self._transaction() as conn:
            self._set_state(conn, generation, "abandoned")

    @staticmethod
    def _set_state(conn, generation, state):
        updated = conn.execute(
            "UPDATE source_generations SET state=? WHERE adapter=? AND source=? "
            "AND generation=? AND version=? AND previous IS ? AND deleted=? AND state='staging'",
            (
                state,
                generation.adapter_name,
                generation.source_file,
                generation.generation,
                generation.version,
                generation.previous,
                int(generation.deleted),
            ),
        ).rowcount
        if updated != 1:
            raise LifecycleConflictError("generation is not the owned staging record")

    def reclaim(self, collection, adapter_name, source_file):
        """Delete exact inactive generations after readers quiesce.

        Acquires the palace writer lease (reentrant within an ingest) and takes
        a physical drawer collection. A delete failure leaves registry records intact and blocks
        another activation; retry is idempotent, including after process death.
        """
        from ..palace import mine_palace_lock

        with mine_palace_lock(self.palace_path):
            # Avoid delaying a new item when there is nothing to reclaim.
            with self.snapshot() as snapshot:
                rows = (
                    snapshot._connection()
                    .execute(
                        "SELECT 1 FROM source_generations WHERE adapter=? AND source=? "
                        "AND state IN ('retired','abandoned') LIMIT 1",
                        (adapter_name, source_file),
                    )
                    .fetchone()
                )
            if not rows:
                return
            with reclamation_lease(self.palace_path, exclusive=True):
                with self._transaction() as conn:
                    rows = conn.execute(
                        "SELECT generation FROM source_generations WHERE adapter=? AND source=? "
                        "AND state IN ('retired','abandoned')",
                        (adapter_name, source_file),
                    ).fetchall()
                    for (generation,) in rows:
                        collection.delete(
                            where={
                                "$and": [
                                    {"adapter_name": adapter_name},
                                    {"source_file": source_file},
                                    {"source_generation": generation},
                                ]
                            }
                        )
                        conn.execute(
                            "DELETE FROM source_generations WHERE adapter=? AND source=? "
                            "AND generation=? AND state IN ('retired','abandoned')",
                            (adapter_name, source_file, generation),
                        )
