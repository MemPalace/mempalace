"""Lossless thread store with a full-text index. Owner: part T02.

One SQLite file per workspace (``threads.sqlite3`` in the resolved active
workspace generation, mode 0600). Messages are stored whole, verbatim, one row per
``ThreadMessage`` (``raw`` included). Chunks are views over consecutive
messages (``chunk_messages``) indexed in FTS5 for ``search``.

Idempotent: storing the same ``source_thread_id`` again returns the existing
``StoredThread`` and writes nothing; storing different content under the
same source id raises ``ThreadConflict``. A crash mid-store leaves nothing
behind (one transaction). ``append`` accepts canonical prefix extensions
without rewriting any existing message or chunk. Handoff documents and
their verbatim quotes stay in this workspace file as well. Never deletes.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .models import CHUNK_MAX_CHARS, THREADS_WING, RecallHit, StoredThread, ThreadMessage

MIRROR_PROGRESS_SQL = """
CREATE TABLE IF NOT EXISTS mirror_progress (
    thread_id TEXT PRIMARY KEY REFERENCES threads(id),
    completed_seq INTEGER NOT NULL CHECK(completed_seq >= -1),
    pending_seq INTEGER NOT NULL CHECK(pending_seq >= completed_seq)
)
"""


class ThreadConflict(ValueError):
    """Same source thread id, different content."""


class ThreadStorageLimit(ValueError):
    """The thread cannot fit within its allocated storage budget."""


@dataclass(frozen=True)
class Chunk:
    index: int
    seq_start: int  # inclusive
    seq_end: int  # inclusive
    text: str  # "[<seq>] <role> <kind>[ <name>]: <text>" lines, verbatim text


def chunk_messages(messages: list, max_chars: int = CHUNK_MAX_CHARS) -> list[Chunk]:
    """Group whole messages up to ``max_chars``. One message longer than
    ``max_chars`` is split into consecutive pieces of that message only
    (same seq_start == seq_end), so every character is in exactly one chunk.

    The limit includes the line prefix and separators. A split piece repeats
    its message's prefix; a limit too small to hold that prefix and any text
    raises ``ValueError``. Whitespace and line breaks in text stay untouched.
    """
    if max_chars <= 0:
        raise ValueError("Chunk size must be positive")

    chunks = []
    lines = []
    length = 0
    seq_start = seq_end = 0

    def flush() -> None:
        nonlocal length
        if lines:
            chunks.append(Chunk(len(chunks), seq_start, seq_end, "\n".join(lines)))
            lines.clear()
            length = 0

    for message in messages:
        prefix = f"[{message.seq}] {message.role} {message.kind}"
        if message.name:
            prefix += f" {message.name}"
        prefix += ": "
        capacity = max_chars - len(prefix)
        if capacity < 0 or (capacity == 0 and message.text):
            raise ValueError("Chunk size is too small for the message prefix")

        line = prefix + message.text
        if len(line) > max_chars:
            flush()
            for offset in range(0, len(message.text), capacity):
                chunks.append(
                    Chunk(
                        len(chunks),
                        message.seq,
                        message.seq,
                        prefix + message.text[offset : offset + capacity],
                    )
                )
            continue

        if lines and length + 1 + len(line) > max_chars:
            flush()
        if not lines:
            seq_start = message.seq
        seq_end = message.seq
        length += len(line) + bool(lines)
        lines.append(line)

    flush()
    return chunks


def canonical_sha256(messages: list) -> str:
    """SHA-256 over the messages' (seq, role, kind, name, call_id, text, raw)."""
    canonical = [(m.seq, m.role, m.kind, m.name, m.call_id, m.text, m.raw) for m in messages]
    encoded = json.dumps(canonical, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8", "surrogatepass"
    )
    return hashlib.sha256(encoded).hexdigest()


def _stored_thread(row: Optional[sqlite3.Row]) -> Optional[StoredThread]:
    if row is None:
        return None
    return StoredThread(
        id=row["id"],
        workspace_id=row["workspace_id"],
        source_thread_id=row["source_thread_id"],
        message_count=row["message_count"],
        chunk_count=row["chunk_count"],
        sha256=row["sha256"],
        wing=row["wing"],
        created_at=row["created_at"],
        created_by=row["created_by"],
    )


class ThreadStore:
    def __init__(self, path: str):
        self._lock = threading.RLock()
        if path != ":memory:":
            file_path = Path(path)
            file_path.parent.mkdir(parents=True, exist_ok=True)
            # Create privately before SQLite can write any transcript data.
            descriptor = os.open(file_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                os.chmod(file_path, 0o600)
            finally:
                os.close(descriptor)

        self._conn = sqlite3.connect(
            path, check_same_thread=False, isolation_level=None, timeout=30
        )
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS threads (
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    source_thread_id TEXT NOT NULL,
                    parent_stored_id TEXT NOT NULL DEFAULT '',
                    message_count INTEGER NOT NULL,
                    chunk_count INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    wing TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    UNIQUE(workspace_id, source_thread_id)
                );
                CREATE TABLE IF NOT EXISTS messages (
                    thread_id TEXT NOT NULL REFERENCES threads(id),
                    seq INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    name TEXT NOT NULL,
                    call_id TEXT NOT NULL,
                    at TEXT NOT NULL,
                    text TEXT NOT NULL,
                    raw TEXT NOT NULL,
                    PRIMARY KEY(thread_id, seq)
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    thread_id TEXT NOT NULL REFERENCES threads(id),
                    idx INTEGER NOT NULL,
                    seq_start INTEGER NOT NULL,
                    seq_end INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    PRIMARY KEY(thread_id, idx)
                );
                CREATE TABLE IF NOT EXISTS handoff_docs (
                    handoff_id TEXT PRIMARY KEY,
                    markdown TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS fact_extraction (
                    thread_id TEXT PRIMARY KEY REFERENCES threads(id),
                    version INTEGER NOT NULL,
                    next_seq INTEGER NOT NULL CHECK(next_seq >= 0)
                );
                """
            )
            self._conn.execute(MIRROR_PROGRESS_SQL)
            self._fts_enabled = self._init_fts()
        except BaseException:
            self._conn.close()
            raise

    def mirror_progress(self, stored_id: str):
        with self._lock:
            row = self._conn.execute(
                "SELECT completed_seq, pending_seq FROM mirror_progress WHERE thread_id=?",
                (stored_id,),
            ).fetchone()
            return tuple(row) if row is not None else None

    def begin_mirror(self, stored_id: str, through: int, *, bootstrap: int = -1):
        """Pin an append boundary until its idempotent mirror writes finish."""
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            self._conn.execute(
                "INSERT OR IGNORE INTO mirror_progress VALUES (?,?,?)",
                (stored_id, bootstrap, max(through, bootstrap)),
            )
            self._conn.execute(
                "UPDATE mirror_progress SET pending_seq=max(pending_seq,?) "
                "WHERE thread_id=? AND completed_seq=pending_seq",
                (through, stored_id),
            )
            return self.mirror_progress(stored_id)

    def finish_mirror(self, stored_id: str, through: int):
        """Advance only the pending batch whose units have all committed."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE mirror_progress SET completed_seq=? WHERE thread_id=? AND pending_seq=?",
                (through, stored_id, through),
            )

    def _init_fts(self) -> bool:
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts "
                    "USING fts5(thread_id UNINDEXED, idx UNINDEXED, text)"
                )
            except sqlite3.OperationalError as error:
                if "no such module: fts5" not in str(error).lower():
                    raise
                return False
            # Old writers ignore user_version and can misalign FTS rowids after
            # fallback writes. Repair identities on every open, retaining all
            # aligned entries so migration does not rewrite a healthy index.
            self._conn.execute(
                """DELETE FROM chunks_fts WHERE rowid IN (
                       SELECT f.rowid FROM chunks_fts f
                       LEFT JOIN chunks c ON c.rowid=f.rowid
                       WHERE c.thread_id IS NOT f.thread_id OR c.idx IS NOT f.idx
                   )"""
            )
            if self._conn.execute("PRAGMA user_version").fetchone()[0] < 1:
                self._conn.execute("PRAGMA user_version=1")
            # Also handles reopening with FTS5 after stores made without it.
            self._conn.execute(
                """INSERT INTO chunks_fts(rowid, thread_id, idx, text)
                   SELECT rowid, thread_id, idx, text FROM chunks
                   WHERE rowid NOT IN (SELECT rowid FROM chunks_fts)"""
            )
        return True

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def fact_cursor(self, stored_id: str, version: int) -> int:
        """First unprocessed message for this extractor version; chats stay untouched."""
        with self._lock:
            row = self._conn.execute(
                "SELECT version, next_seq FROM fact_extraction WHERE thread_id=?", (stored_id,)
            ).fetchone()
        if row is not None and row["version"] > version:
            raise ValueError("a newer fact extractor already processed this thread")
        return row["next_seq"] if row is not None and row["version"] == version else 0

    def advance_fact_cursor(self, stored_id: str, version: int, next_seq: int) -> None:
        """Commit progress monotonically, only within the durable message range."""
        if type(version) is not int or version < 1 or type(next_seq) is not int or next_seq < 0:
            raise ValueError("invalid fact extraction progress")
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            stored = self.get(stored_id)
            if stored is None or next_seq > stored.message_count:
                raise ValueError("fact extraction progress is outside the thread")
            self._conn.execute(
                "INSERT INTO fact_extraction(thread_id, version, next_seq) VALUES (?, ?, ?) "
                "ON CONFLICT(thread_id) DO UPDATE SET "
                "next_seq=CASE WHEN version=excluded.version THEN MAX(next_seq, excluded.next_seq) "
                "ELSE excluded.next_seq END, version=excluded.version "
                "WHERE version <= excluded.version",
                (stored_id, version, next_seq),
            )

    def store(
        self,
        workspace_id: str,
        source_thread_id: str,
        messages: list[ThreadMessage],
        *,
        actor: str,
        parent_stored_id: str = "",
        max_storage_bytes: Optional[int] = None,
    ) -> StoredThread:
        messages = list(messages)
        if not messages:
            raise ValueError("Thread must contain at least one message")
        if any(message.seq != seq for seq, message in enumerate(messages)):
            raise ValueError("Message sequence must start at zero and be contiguous")
        digest = canonical_sha256(messages)

        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            existing = _stored_thread(
                self._conn.execute(
                    "SELECT * FROM threads WHERE workspace_id=? AND source_thread_id=?",
                    (workspace_id, source_thread_id),
                ).fetchone()
            )
            if existing is not None:
                if existing.sha256 != digest:
                    raise ThreadConflict("Source thread already has different stored content")
                return existing

            self._check_storage_budget(max_storage_bytes, messages)
            chunks = chunk_messages(messages)
            stored = StoredThread(
                id="thr_" + secrets.token_hex(8),
                workspace_id=workspace_id,
                source_thread_id=source_thread_id,
                message_count=len(messages),
                chunk_count=len(chunks),
                sha256=digest,
                wing=THREADS_WING,
                created_at=datetime.now(timezone.utc).isoformat(),
                created_by=actor,
            )
            self._conn.execute(
                """INSERT INTO threads
                   (id, workspace_id, source_thread_id, parent_stored_id,
                    message_count, chunk_count, sha256, wing, created_at, created_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    stored.id,
                    workspace_id,
                    source_thread_id,
                    parent_stored_id,
                    stored.message_count,
                    stored.chunk_count,
                    digest,
                    stored.wing,
                    stored.created_at,
                    actor,
                ),
            )
            self._conn.executemany(
                """INSERT INTO messages
                   (thread_id, seq, role, kind, name, call_id, at, text, raw)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        stored.id,
                        m.seq,
                        m.role,
                        m.kind,
                        m.name,
                        m.call_id,
                        m.at,
                        m.text,
                        m.raw,
                    )
                    for m in messages
                ],
            )
            self._conn.executemany(
                "INSERT INTO chunks(thread_id, idx, seq_start, seq_end, text) "
                "VALUES (?, ?, ?, ?, ?)",
                [(stored.id, c.index, c.seq_start, c.seq_end, c.text) for c in chunks],
            )
            if self._fts_enabled:
                self._conn.executemany(
                    "INSERT INTO chunks_fts(rowid, thread_id, idx, text) "
                    "SELECT rowid, thread_id, idx, text FROM chunks WHERE thread_id=? AND idx=?",
                    [(stored.id, c.index) for c in chunks],
                )
            self._check_storage_budget(max_storage_bytes)
            return stored

    def _check_storage_budget(self, maximum, messages=()):
        if maximum is None:
            return
        path = self._conn.execute("PRAGMA database_list").fetchone()[2]
        page_size = self._conn.execute("PRAGMA page_size").fetchone()[0]
        pages = self._conn.execute("PRAGMA page_count").fetchone()[0]

        def size(suffix):
            if not path or path == ":memory:":
                return 0
            candidate = Path(path + suffix)
            return candidate.stat().st_size if candidate.exists() else 0

        # Allow for a checkpoint growing the main file and a commit writing
        # every page to WAL. Already spilled frames are included in WAL size.
        bound = (
            max(size(""), pages * page_size)
            + size("-wal")
            + max(size("-shm"), 32768)
            + 32
            + pages * (page_size + 24)
        )
        if messages:
            lengths = {}

            def utf8_size(value):
                # Content blocks share raw objects; avoid encoding the same
                # large line once per block while still budgeting every row.
                key = id(value)
                if key not in lengths:
                    lengths[key] = len(value.encode("utf-8"))
                return lengths[key]

            # Reject before a large transaction can spill. Include raw source,
            # readable text, chunks, FTS and SQLite allocation overhead.
            bound += 256 * 1024 + 8 * sum(
                utf8_size(value)
                for m in messages
                for value in (m.raw, m.text, m.role, m.kind, m.name, m.call_id, m.at)
            )
        if bound > maximum:
            raise ThreadStorageLimit("Insufficient storage for this thread")

    def append(self, stored_id: str, messages: list[ThreadMessage]) -> StoredThread:
        """Append the suffix of a full transcript, retaining every existing row.

        Canonical fields must match the saved prefix. An identical transcript,
        or an older prefix after a concurrent append, is a no-op. Changed
        content raises ``ThreadConflict``. New messages, chunks, FTS entries
        and counts commit together; original chunk boundaries never change.
        """
        messages = list(messages)
        if any(message.seq != seq for seq, message in enumerate(messages)):
            raise ValueError("Message sequence must start at zero and be contiguous")
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            stored = self.get(stored_id)
            if stored is None:
                raise ValueError("Thread not found")
            previous = self.messages(stored_id)
            common = min(len(previous), len(messages))
            if canonical_sha256(previous[:common]) != canonical_sha256(messages[:common]):
                raise ThreadConflict("Source thread already has different stored content")
            if len(messages) <= len(previous):
                return stored
            added = messages[len(previous) :]
            chunks = chunk_messages(added)
            self._conn.executemany(
                "INSERT INTO messages(thread_id, seq, role, kind, name, call_id, at, text, raw) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (stored_id, m.seq, m.role, m.kind, m.name, m.call_id, m.at, m.text, m.raw)
                    for m in added
                ],
            )
            self._conn.executemany(
                "INSERT INTO chunks(thread_id, idx, seq_start, seq_end, text) VALUES (?, ?, ?, ?, ?)",
                [
                    (stored_id, stored.chunk_count + c.index, c.seq_start, c.seq_end, c.text)
                    for c in chunks
                ],
            )
            if self._fts_enabled:
                self._conn.executemany(
                    "INSERT INTO chunks_fts(rowid, thread_id, idx, text) "
                    "SELECT rowid, thread_id, idx, text FROM chunks WHERE thread_id=? AND idx=?",
                    [(stored_id, stored.chunk_count + c.index) for c in chunks],
                )
            self._conn.execute(
                "UPDATE threads SET message_count = ?, chunk_count = ?, sha256 = ? WHERE id = ?",
                (
                    len(messages),
                    stored.chunk_count + len(chunks),
                    canonical_sha256(previous + added),
                    stored_id,
                ),
            )
            return self.get(stored_id)

    def put_doc(self, handoff_id: str, markdown: str) -> None:
        """Save a handoff's verbatim quotes beside its workspace transcript."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO handoff_docs(handoff_id, markdown) VALUES (?, ?) "
                "ON CONFLICT(handoff_id) DO UPDATE SET markdown = excluded.markdown",
                (handoff_id, markdown),
            )

    def doc(self, handoff_id: str) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT markdown FROM handoff_docs WHERE handoff_id = ?", (handoff_id,)
            ).fetchone()
            return row["markdown"] if row is not None else ""

    def get(self, stored_id: str) -> Optional[StoredThread]:
        with self._lock:
            return _stored_thread(
                self._conn.execute("SELECT * FROM threads WHERE id=?", (stored_id,)).fetchone()
            )

    def by_source(self, workspace_id: str, source_thread_id: str) -> Optional[StoredThread]:
        with self._lock:
            return _stored_thread(
                self._conn.execute(
                    "SELECT * FROM threads WHERE workspace_id=? AND source_thread_id=?",
                    (workspace_id, source_thread_id),
                ).fetchone()
            )

    def parent_of(self, stored_id: str) -> Optional[str]:
        """The stored thread this one's agent was a successor of, if any."""
        with self._lock:
            row = self._conn.execute(
                "SELECT parent_stored_id FROM threads WHERE id=?", (stored_id,)
            ).fetchone()
            return row["parent_stored_id"] or None if row is not None else None

    def messages(
        self, stored_id: str, start: int = 0, end: Optional[int] = None
    ) -> list[ThreadMessage]:
        """Messages with ``start <= seq <= end`` (end None: to the last)."""
        if start < 0 or (end is not None and (end < 0 or end < start)):
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, role, kind, name, call_id, at, text, raw FROM messages "
                "WHERE thread_id=? AND seq>=? AND (? IS NULL OR seq<=?) ORDER BY seq",
                (stored_id, start, end, end),
            ).fetchall()
            return [ThreadMessage(**dict(row)) for row in rows]

    def chunks(self, stored_id: str) -> list[Chunk]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT idx, seq_start, seq_end, text FROM chunks WHERE thread_id=? ORDER BY idx",
                (stored_id,),
            ).fetchall()
            return [
                Chunk(row["idx"], row["seq_start"], row["seq_end"], row["text"]) for row in rows
            ]

    def search(self, stored_ids: list[str], query: str, limit: int = 8) -> list[RecallHit]:
        """BM25 over the chunks of ``stored_ids``. Any query text is safe
        (quoted for FTS5, never a syntax error). Best first. Scores are
        negated BM25 (larger is better); the LIKE fallback has tied scores.
        """
        tokens = query.split()
        if not tokens or not stored_ids or limit <= 0:
            return []
        stored_ids = list(dict.fromkeys(stored_ids))
        with self._lock:
            # FTS5's query parser treats NUL as an end marker even in a
            # quoted string, so such queries use the literal scan as well.
            if self._fts_enabled and "\x00" not in query:
                rows = self._search_fts(stored_ids, tokens, limit)
            else:
                rows = self._search_like(stored_ids, tokens, limit)
            rows.sort(key=lambda row: (-row["score"], row["thread_id"], row["idx"]))
            return [
                RecallHit(
                    stored_thread_id=row["thread_id"],
                    chunk_index=row["idx"],
                    seq_start=row["seq_start"],
                    seq_end=row["seq_end"],
                    score=row["score"],
                    text=row["text"],
                )
                for row in rows[:limit]
            ]

    def _sqlite_limit(self, category: str, default: int) -> int:
        # getlimit was added in Python 3.11; retain conservative defaults
        # for Python 3.10, which this project still supports.
        read_limit = getattr(self._conn, "getlimit", None)
        return read_limit(getattr(sqlite3, category)) if read_limit is not None else default

    def _search_fts(self, stored_ids: list[str], tokens: list[str], limit: int) -> list:
        match = " OR ".join('"' + token.replace('"', '""') + '"' for token in tokens)
        batch_size = max(1, min(500, self._sqlite_limit("SQLITE_LIMIT_VARIABLE_NUMBER", 999) - 2))
        rows = []
        for offset in range(0, len(stored_ids), batch_size):
            ids = stored_ids[offset : offset + batch_size]
            placeholders = ",".join("?" for _ in ids)
            chunk_count = self._conn.execute(
                f"SELECT coalesce(sum(chunk_count), 0) FROM threads WHERE id IN ({placeholders})",
                ids,
            ).fetchone()[0]
            if not chunk_count:
                continue
            # Stop counting at the scoped chunk count. A rare global match set
            # is cheaper to join directly than materializing the entire scope.
            match_count = self._conn.execute(
                "SELECT count(*) FROM (SELECT 1 FROM chunks_fts WHERE chunks_fts MATCH ? LIMIT ?)",
                (match, chunk_count),
            ).fetchone()[0]
            if match_count < chunk_count:
                scope_sql = ""
                rank_sql = (
                    "FROM chunks_fts CROSS JOIN chunks AS s ON s.rowid=chunks_fts.rowid "
                    f"WHERE chunks_fts MATCH ? AND s.thread_id IN ({placeholders}) "
                    "ORDER BY bm25(chunks_fts), s.thread_id, s.idx LIMIT ?"
                )
                parameters = (match, *ids, limit)
            else:
                # Covering primary-key metadata avoids payload reads for common
                # terms. AS MATERIALIZED requires SQLite 3.35+ and prevents the
                # planner from moving chunk payload lookups ahead of LIMIT.
                scope_sql = (
                    "scope AS MATERIALIZED ("
                    "SELECT rowid AS chunk_rowid, thread_id, idx FROM chunks "
                    f"WHERE thread_id IN ({placeholders})), "
                )
                rank_sql = (
                    "FROM chunks_fts JOIN scope ON scope.chunk_rowid=chunks_fts.rowid "
                    "WHERE chunks_fts MATCH ? "
                    "ORDER BY bm25(chunks_fts), scope.thread_id, scope.idx LIMIT ?"
                )
                parameters = (*ids, match, limit)
            rows.extend(
                self._conn.execute(
                    "WITH " + scope_sql + "ranked AS MATERIALIZED ("
                    "SELECT chunks_fts.rowid AS chunk_rowid, -bm25(chunks_fts) AS score "
                    + rank_sql
                    + ") "
                    "SELECT c.thread_id, c.idx, c.seq_start, c.seq_end, c.text, ranked.score "
                    "FROM ranked JOIN chunks AS c ON c.rowid=ranked.chunk_rowid",
                    parameters,
                ).fetchall()
            )
        # BM25 scores use the same corpus for every id batch, so their
        # best results can be merged without changing relevance scores.
        return rows

    def _search_like(self, stored_ids: list[str], tokens: list[str], limit: int) -> list:
        variables = self._sqlite_limit("SQLITE_LIMIT_VARIABLE_NUMBER", 999)
        depth = self._sqlite_limit("SQLITE_LIMIT_EXPR_DEPTH", 1_000)
        pattern_limit = self._sqlite_limit("SQLITE_LIMIT_LIKE_PATTERN_LENGTH", 50_000)
        term_batch = max(1, min(100, (variables - 1) // 2, (depth or 1_000) - 8))
        id_batch = max(1, min(500, variables - term_batch - 1))
        predicates = []
        values = []
        for token in tokens:
            pattern = (
                "%" + token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            )
            if "\x00" in token or len(pattern.encode("utf-8")) > pattern_limit:
                # LIKE truncates at NUL and limits pattern length. instr
                # compares the whole string; SQLite lower preserves LIKE's
                # default case-insensitivity for ASCII characters.
                predicates.append("instr(lower(text), lower(?)) > 0")
                values.append(token)
            else:
                predicates.append("text LIKE ? ESCAPE '\\'")
                values.append(pattern)

        found = {}
        for id_offset in range(0, len(stored_ids), id_batch):
            ids = stored_ids[id_offset : id_offset + id_batch]
            placeholders = ",".join("?" for _ in ids)
            for offset in range(0, len(tokens), term_batch):
                matches = " OR ".join(predicates[offset : offset + term_batch])
                rows = self._conn.execute(
                    "SELECT thread_id, idx, seq_start, seq_end, text, 0.0 AS score FROM chunks "
                    f"WHERE thread_id IN ({placeholders}) AND ({matches}) "
                    "ORDER BY thread_id, idx LIMIT ?",
                    (*ids, *values[offset : offset + term_batch], limit),
                ).fetchall()
                for row in rows:
                    found[row["thread_id"], row["idx"]] = row
        # Equal scores have the same stable ordering in every batch; each
        # batch's first `limit` rows suffice for the final first `limit`.
        return list(found.values())


def __getattr__(name):
    if name == "ThreadStoreV2":
        from .thread_store_v2 import ThreadStoreV2

        return ThreadStoreV2
    raise AttributeError(name)
