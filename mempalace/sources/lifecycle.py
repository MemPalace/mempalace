"""Durable currentness state for RFC 002 source items.

The registry lives in the palace knowledge-graph SQLite database rather than a
separate cursor file: a palace remains the authoritative cursor for adapters.
It records which fully written generation is visible for one
``(adapter_name, source_file)`` pair. The source-adapter runner stages drawer
writes against it and ordinary drawer reads resolve visibility through it.
"""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3
from typing import Optional
import uuid


_STAGING = "staging"
_ACTIVE = "active"
_RETIRED = "retired"


@dataclass(frozen=True)
class SourceGeneration:
    adapter_name: str
    source_file: str
    generation: str
    version: str
    state: str


class SourceLifecycleStore:
    """Small transactional registry for incremental source-item generations."""

    def __init__(self, db_path: str, *, initialize: bool = True):
        self.db_path = db_path
        self._initialized = initialize
        if initialize:
            self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS source_item_generations (
                    adapter_name TEXT NOT NULL,
                    source_file TEXT NOT NULL,
                    generation TEXT NOT NULL,
                    version TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('staging', 'active', 'retired')),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    activated_at TEXT,
                    PRIMARY KEY (adapter_name, source_file, generation)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_source_item_one_active
                    ON source_item_generations(adapter_name, source_file)
                    WHERE state = 'active';
                CREATE INDEX IF NOT EXISTS idx_source_item_generations_lookup
                    ON source_item_generations(adapter_name, source_file, state);
                """
            )

    def active(self, *, adapter_name: str, source_file: str) -> Optional[SourceGeneration]:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT adapter_name, source_file, generation, version, state
                    FROM source_item_generations
                    WHERE adapter_name = ? AND source_file = ? AND state = ?
                    """,
                    (adapter_name, source_file, _ACTIVE),
                ).fetchone()
        except sqlite3.OperationalError as exc:
            if not self._initialized and "no such table" in str(exc).lower():
                return None
            raise
        return SourceGeneration(**dict(row)) if row else None

    def begin(self, *, adapter_name: str, source_file: str, version: str) -> SourceGeneration:
        """Create a fresh, non-visible generation for a changed source item."""
        generation = uuid.uuid4().hex
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO source_item_generations
                    (adapter_name, source_file, generation, version, state)
                VALUES (?, ?, ?, ?, ?)
                """,
                (adapter_name, source_file, generation, version, _STAGING),
            )
        return SourceGeneration(adapter_name, source_file, generation, version, _STAGING)

    def activate(self, generation: SourceGeneration) -> Optional[SourceGeneration]:
        """Atomically switch the visible generation, returning the previous one."""
        if generation.state != _STAGING:
            raise ValueError("only a staging generation can be activated")
        with self._connect() as conn:
            previous_row = conn.execute(
                """
                SELECT adapter_name, source_file, generation, version, state
                FROM source_item_generations
                WHERE adapter_name = ? AND source_file = ? AND state = ?
                """,
                (generation.adapter_name, generation.source_file, _ACTIVE),
            ).fetchone()
            conn.execute(
                """
                UPDATE source_item_generations
                SET state = ?
                WHERE adapter_name = ? AND source_file = ? AND state = ?
                """,
                (_RETIRED, generation.adapter_name, generation.source_file, _ACTIVE),
            )
            changed = conn.execute(
                """
                UPDATE source_item_generations
                SET state = ?, activated_at = CURRENT_TIMESTAMP
                WHERE adapter_name = ? AND source_file = ? AND generation = ? AND state = ?
                """,
                (
                    _ACTIVE,
                    generation.adapter_name,
                    generation.source_file,
                    generation.generation,
                    _STAGING,
                ),
            ).rowcount
            if changed != 1:
                raise ValueError("staging generation does not exist")
        return SourceGeneration(**dict(previous_row)) if previous_row else None

    def abandon(self, generation: SourceGeneration) -> None:
        """Remove a non-visible generation after a handled ingest failure."""
        with self._connect() as conn:
            conn.execute(
                """
                DELETE FROM source_item_generations
                WHERE adapter_name = ? AND source_file = ? AND generation = ? AND state = ?
                """,
                (generation.adapter_name, generation.source_file, generation.generation, _STAGING),
            )

    def tombstone(self, *, adapter_name: str, source_file: str) -> SourceGeneration:
        """Make an item logically deleted before its physical purge completes.

        The active tombstone keeps old generation and legacy drawers hidden if
        deletion is interrupted. A later re-ingest simply activates a normal
        generation and supersedes the tombstone.
        """
        generation = self.begin(
            adapter_name=adapter_name, source_file=source_file, version="__deleted__"
        )
        self.activate(generation)
        return generation

    def prune_retired(self, *, adapter_name: str, source_file: str) -> None:
        """Remove registry history once its physical drawers are purged."""
        with self._connect() as conn:
            conn.execute(
                """
                DELETE FROM source_item_generations
                WHERE adapter_name = ? AND source_file = ? AND state = ?
                """,
                (adapter_name, source_file, _RETIRED),
            )
