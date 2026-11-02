"""RFC 008 B1a thread storage, explicitly selected and never wired by default.

Open ``threads.v2.sqlite3`` through this class. The original ThreadStore and
its production factory continue to use v1. Transactions read back every changed
block before commit, including the extensible final block on append.
"""

from __future__ import annotations

import os
import re
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

import zstandard as zstd

from . import thread_frames as frames
from .models import THREADS_WING, RecallHit, ThreadMessage
from .thread_store import (
    MIRROR_PROGRESS_SQL,
    Chunk,
    ThreadConflict,
    ThreadStore as V1ThreadStore,
    canonical_sha256,
    chunk_messages,
)
from .thread_store_v2_schema import APPLICATION_ID, BLOCK_TRIGGERS, FILENAME, SCHEMA, USER_VERSION

_BLOCK_COLUMNS = (
    "thread_id, idx, first_line, n_lines, first_seq, last_seq, text_dict, ztext, raw_dict, zraw"
)
_SURROGATES = re.compile(r"[\ud800-\udfff]")


def _prefix(message):
    return (
        f"[{message.seq}] {message.role} {message.kind}"
        + (f" {message.name}" if message.name else "")
        + ": "
    )


def _index_text(text: str, version: int) -> str:
    if version != 0:
        raise ValueError("Unsupported thread index version")
    # SQLite cannot encode lone surrogates. NUL retains v1 FTS tokenization;
    # queries containing NUL use a literal scan instead.
    return _SURROGATES.sub(" ", text)


class ThreadStoreV2(V1ThreadStore):
    filename = FILENAME

    def __init__(self, path: str):
        self._lock = threading.RLock()
        if path != ":memory:":
            file_path = Path(path)
            if file_path.name != FILENAME:
                raise ValueError(f"Thread store v2 requires filename {FILENAME}")
            file_path.parent.mkdir(parents=True, exist_ok=True)
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
            self._initialize()
        except BaseException:
            self._conn.close()
            raise

    def _initialize(self):
        # Version 3 adds mirror progress in place. Older version-2-only
        # readers reject this store rather than silently losing its progress.
        app = self._conn.execute("PRAGMA application_id").fetchone()[0]
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        tables = self._conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        if tables and (app != APPLICATION_ID or version not in (2, USER_VERSION)):
            raise ValueError("Not a supported v2 thread store")
        if not tables and (app not in (0, APPLICATION_ID) or version not in (0, USER_VERSION)):
            raise ValueError("Not a supported v2 thread store")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA recursive_triggers=ON")
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            # Recheck after the writer lock: another opener may have created
            # the schema since the initial identity check.
            tables = self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            if not tables:
                self._conn.execute(f"PRAGMA application_id={APPLICATION_ID}")
                self._conn.execute(f"PRAGMA user_version={USER_VERSION}")
                statement = ""
                for line in SCHEMA.splitlines(keepends=True):
                    statement += line
                    if sqlite3.complete_statement(statement):
                        self._conn.execute(statement)
                        statement = ""
            self._conn.execute(MIRROR_PROGRESS_SQL)
            self._conn.execute(f"PRAGMA user_version={USER_VERSION}")
            for name, sql in BLOCK_TRIGGERS.items():
                self._conn.execute(f"DROP TRIGGER IF EXISTS {name}")
                self._conn.execute(sql)
            self._fts_enabled = self._create_fts()
            if self._fts_enabled:
                indexed = self._conn.execute("SELECT indexed_chunks FROM index_meta").fetchone()[0]
                count = self._conn.execute("SELECT count(*) FROM chunk").fetchone()[0]
                actual = self._conn.execute("SELECT count(*) FROM chunk_fts").fetchone()[0]
                if indexed != count or actual != count:
                    self._reindex(self.index_version)

    def _create_fts(self):
        try:
            self._conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(text, content='')"
            )
        except sqlite3.OperationalError as error:
            if "no such module: fts5" not in str(error).lower():
                raise
            return False
        return True

    @property
    def index_version(self):
        with self._lock:
            return self._conn.execute("SELECT index_version FROM index_meta").fetchone()[0]

    def reindex(self, index_version: int = 0):
        """Rebuild only derived search data, atomically, from verbatim frames."""
        _index_text("", index_version)
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            if not self._fts_enabled:
                raise ValueError("FTS5 is unavailable")
            self._reindex(index_version)

    def _reindex(self, version):
        self._conn.execute("INSERT INTO chunk_fts(chunk_fts) VALUES ('delete-all')")
        rows = self._conn.execute("SELECT rowid, * FROM chunk ORDER BY thread_id, idx").fetchall()
        cache = {}
        thread_id = None
        for row in rows:
            if row["thread_id"] != thread_id:
                cache.clear()
                thread_id = row["thread_id"]
            self._conn.execute(
                "INSERT INTO chunk_fts(rowid, text) VALUES (?, ?)",
                (row["rowid"], _index_text(self._chunk_text(row, cache), version)),
            )
            # Chunks advance through contiguous message ranges. Only the last
            # block can overlap the next chunk; release all earlier frames.
            if cache:
                key = max(cache)
                cache = {key: cache[key]}
        self._conn.execute(
            "UPDATE index_meta SET index_version=?, indexed_chunks=?", (version, len(rows))
        )

    def _dict(self, dictionary_id):
        if dictionary_id is None:
            return None
        row = self._conn.execute("SELECT data FROM dict WHERE id=?", (dictionary_id,)).fetchone()
        if row is None:
            raise ValueError("Missing thread dictionary")
        return row[0]

    def _get_blob(self, key):
        row = self._conn.execute("SELECT kind, data FROM blob WHERE key=?", (key,)).fetchone()
        if row is None:
            raise ValueError("Missing thread blob")
        return row["kind"], row["data"]

    def _put_blob(self, key, kind, data):
        self._conn.execute("INSERT OR IGNORE INTO blob VALUES (?, ?, ?)", (key, kind, data))
        if self._get_blob(key) != (kind, data):
            raise ValueError("Thread blob key collision")

    def _frame(self, block, frame):
        compressed = block["zraw" if frame == "raw" else "ztext"]
        dictionary = block["raw_dict" if frame == "raw" else "text_dict"]
        return frames.decode_frame(
            frames.decompress(compressed, self._dict(dictionary)), self._get_blob
        )

    def train_dictionaries(self, dictionary_size: int = 8192) -> list[int]:
        """Train from this file's own frames only; existing frames keep their ids.

        No bundled or cross-workspace sample corpus is accepted. Mixed-workspace
        files retain the v1 API but cannot train or use dictionaries on new
        frames. Until enough samples exist a single-workspace file uses no dict.
        """
        if dictionary_size < 256 or dictionary_size > frames.MIN_TRAIN_BYTES // 4:
            raise ValueError("Dictionary size must be between 256 and 32768 bytes")
        # The read snapshot permits concurrent writers in WAL mode. Sampling
        # stops at a fixed budget and omits the random blob-key span header.
        budget = max(frames.MIN_TRAIN_BYTES, dictionary_size * 100)
        samples = {"raw": [], "text": []}
        sizes = {"raw": 0, "text": 0}
        with self._lock, self._conn:
            self._conn.execute("BEGIN")
            if not self._single_workspace():
                raise ValueError("Dictionary training requires a single-workspace file")
            rows = self._conn.execute(
                "SELECT text_dict, ztext, raw_dict, zraw FROM block ORDER BY rowid DESC"
            )
            for block in rows:
                for frame in ("raw", "text"):
                    if sizes[frame] >= budget:
                        continue
                    data = frames.training_sample(
                        block["zraw" if frame == "raw" else "ztext"],
                        self._dict(block[frame + "_dict"]),
                        budget - sizes[frame],
                    )
                    samples[frame].extend(
                        data[offset : offset + 8192] for offset in range(0, len(data), 8192)
                    )
                    sizes[frame] += len(data)
                if all(size >= budget for size in sizes.values()):
                    break
            rows.close()
        # Training holds neither the connection lock nor a SQLite transaction.
        trained = [
            (frame, zstd.train_dictionary(dictionary_size, values).as_bytes())
            for frame, values in samples.items()
            if sizes[frame] >= frames.MIN_TRAIN_BYTES
        ]
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            if not self._single_workspace():
                raise ValueError("Dictionary training requires a single-workspace file")
            return [
                self._conn.execute("INSERT INTO dict(frame, data) VALUES (?, ?)", value).lastrowid
                for value in trained
            ]

    def store(
        self,
        workspace_id,
        source_thread_id,
        messages,
        *,
        actor,
        parent_stored_id="",
        max_storage_bytes=None,
    ):
        messages = list(messages)
        self._validate(messages)
        if not messages:
            raise ValueError("Thread must contain at least one message")
        digest = canonical_sha256(messages)
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            existing = self.by_source(workspace_id, source_thread_id)
            if existing is not None:
                if existing.sha256 != digest:
                    raise ThreadConflict("Source thread already has different stored content")
                return existing
            self._check_storage_budget(max_storage_bytes, messages)
            stored_id = "thr_" + secrets.token_hex(8)
            chunks = chunk_messages(messages)
            self._conn.execute(
                "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    stored_id,
                    workspace_id,
                    source_thread_id,
                    parent_stored_id,
                    len(messages),
                    len(chunks),
                    digest,
                    THREADS_WING,
                    datetime.now(timezone.utc).isoformat(),
                    actor,
                ),
            )
            cache = self._write_messages(stored_id, messages)
            self._write_chunks(stored_id, chunks, messages, cache=cache)
            self._check_storage_budget(max_storage_bytes)
            return self.get(stored_id)

    @staticmethod
    def _validate(messages):
        if any(message.seq != seq for seq, message in enumerate(messages)):
            raise ValueError("Message sequence must start at zero and be contiguous")

    def append(self, stored_id, messages):
        messages = list(messages)
        self._validate(messages)
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            stored = self.get(stored_id)
            if stored is None:
                raise ValueError("Thread not found")
            if len(messages) < stored.message_count:
                # A stale prefix has no full-thread digest to compare. Only
                # read that prefix; the ordinary full/longer path reads none.
                previous = self.messages(stored_id, 0, len(messages) - 1) if messages else []
                matches = canonical_sha256(previous) == canonical_sha256(messages)
            else:
                matches = canonical_sha256(messages[: stored.message_count]) == stored.sha256
            if not matches:
                raise ThreadConflict("Source thread already has different stored content")
            if len(messages) <= stored.message_count:
                return stored
            added = messages[stored.message_count :]
            cache = self._write_messages(stored_id, added)
            self._write_chunks(
                stored_id, chunk_messages(added), added, stored.chunk_count, cache=cache
            )
            self._conn.execute(
                "UPDATE threads SET message_count=?, chunk_count=?, sha256=? WHERE id=?",
                (
                    len(messages),
                    self._conn.execute(
                        "SELECT count(*) FROM chunk WHERE thread_id=?", (stored_id,)
                    ).fetchone()[0],
                    canonical_sha256(messages),
                    stored_id,
                ),
            )
            return self.get(stored_id)

    def _write_messages(self, stored_id, added):
        last = self._conn.execute(
            f"SELECT {_BLOCK_COLUMNS} FROM block WHERE thread_id=? ORDER BY idx DESC LIMIT 1",
            (stored_id,),
        ).fetchone()
        cache = {}
        if last is not None:
            cache[stored_id, last["idx"]] = (
                last,
                self._frame(last, "text"),
                self._frame(last, "raw"),
            )
        existing = (
            self._messages(stored_id, last["first_seq"], last["last_seq"], raw=True, cache=cache)
            if last is not None
            else []
        )
        block_idx = last["idx"] if last is not None else 0
        first_line = last["first_line"] if last is not None else 0
        pending = []
        for message in existing:
            if pending and pending[-1][0].raw == message.raw:
                pending[-1].append(message)
            else:
                pending.append([message])
        raw_size = sum(len(frames.utf8(group[0].raw)) for group in pending)
        text_size = sum(len(frames.utf8(m.text)) + 8 for m in existing)
        for message in added:
            # A run crossing an append boundary remains one line. It may
            # exceed the target, just as an oversized initial line may.
            if pending and pending[-1][0].raw == message.raw:
                pending[-1].append(message)
                text_size += len(frames.utf8(message.text)) + 8
                continue
            raw_length = len(frames.utf8(message.raw))
            text_length = len(frames.utf8(message.text)) + 8
            if pending and max(raw_size + raw_length, text_size + text_length) > frames.BLOCK_BYTES:
                if pending[-1][-1].seq >= added[0].seq:
                    self._write_block(
                        stored_id, block_idx, first_line, pending, added[0].seq, cache
                    )
                first_line += len(pending)
                block_idx += 1
                pending = []
                raw_size = text_size = 0
            pending.append([message])
            raw_size += raw_length
            text_size += text_length
        if pending:
            self._write_block(stored_id, block_idx, first_line, pending, added[0].seq, cache)
        return cache

    def _single_workspace(self):
        return (
            self._conn.execute(
                "SELECT count(*) FROM (SELECT DISTINCT workspace_id FROM threads LIMIT 2)"
            ).fetchone()[0]
            < 2
        )

    def _write_block(self, stored_id, idx, first_line, groups, new_start, cache):
        items = [message for group in groups for message in group]
        raw = [group[0].raw for group in groups]
        text = [message.text for message in items]
        encoded = []
        use_dictionary = self._single_workspace()
        for frame, values in (("raw", raw), ("text", text)):
            dictionary = (
                self._conn.execute(
                    "SELECT id, data FROM dict WHERE frame=? ORDER BY id DESC LIMIT 1", (frame,)
                ).fetchone()
                if use_dictionary
                else None
            )
            encoded.extend(
                (
                    dictionary["id"] if dictionary else None,
                    frames.compress(
                        frames.encode_frame(values, self._put_blob),
                        dictionary["data"] if dictionary else None,
                    ),
                )
            )
        self._conn.execute(
            "INSERT INTO block (thread_id, idx, first_line, n_lines, first_seq, last_seq, raw_dict, zraw, text_dict, ztext) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(thread_id, idx) DO UPDATE SET n_lines=excluded.n_lines, "
            "last_seq=excluded.last_seq, raw_dict=excluded.raw_dict, zraw=excluded.zraw, "
            "text_dict=excluded.text_dict, ztext=excluded.ztext",
            (stored_id, idx, first_line, len(groups), items[0].seq, items[-1].seq, *encoded),
        )
        for offset, group in enumerate(groups):
            line_idx = first_line + offset
            self._conn.execute(
                "INSERT OR IGNORE INTO line VALUES (?, ?, ?)", (stored_id, line_idx, idx)
            )
            self._conn.executemany(
                "INSERT INTO message VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        stored_id,
                        m.seq,
                        line_idx,
                        *(
                            frames.sql_string(value)
                            for value in (m.role, m.kind, m.name, m.call_id, m.at)
                        ),
                    )
                    for m in group
                    if m.seq >= new_start
                ],
            )
        block = self._conn.execute(
            f"SELECT {_BLOCK_COLUMNS} FROM block WHERE thread_id=? AND idx=?", (stored_id, idx)
        ).fetchone()
        actual_raw, actual_text = self._frame(block, "raw"), self._frame(block, "text")
        if actual_raw != raw or actual_text != text:
            raise ValueError("Thread block read-back mismatch")
        cache[stored_id, idx] = (block, actual_text, actual_raw)
        if self._messages(stored_id, items[0].seq, items[-1].seq, raw=True, cache=cache) != items:
            raise ValueError("Thread message read-back mismatch")

    def _write_chunks(self, stored_id, chunks, messages, start=0, *, cache=None):
        # A split piece repeats its message prefix: off/len address its
        # message text, in Python characters. Multi-message chunks address
        # the rendered lines (off=0), retaining NUL and surrogate positions.
        offsets = {}
        version = self.index_version
        originals = {message.seq: message for message in messages}
        verified = {
            message.seq: message
            for message in self._messages(
                stored_id, messages[0].seq, messages[-1].seq, raw=False, cache=cache
            )
        }
        for chunk in chunks:
            if chunk.seq_start == chunk.seq_end:
                message = originals[chunk.seq_start]
                off = offsets.get(chunk.seq_start, 0)
                length = len(chunk.text) - len(_prefix(message))
                offsets[chunk.seq_start] = off + length
            else:
                off, length = 0, len(chunk.text)
            cursor = self._conn.execute(
                "INSERT INTO chunk VALUES (?, ?, ?, ?, ?, ?)",
                (stored_id, start + chunk.index, chunk.seq_start, chunk.seq_end, off, length),
            )
            row = self._conn.execute(
                "SELECT * FROM chunk WHERE rowid=?", (cursor.lastrowid,)
            ).fetchone()
            items = [verified[seq] for seq in range(row["seq_start"], row["seq_end"] + 1)]
            if self._render_chunk(row, items) != chunk.text:
                raise ValueError("Thread chunk read-back mismatch")
            if self._fts_enabled:
                self._conn.execute(
                    "INSERT INTO chunk_fts(rowid, text) VALUES (?, ?)",
                    (cursor.lastrowid, _index_text(chunk.text, version)),
                )
                self._conn.execute("UPDATE index_meta SET indexed_chunks=indexed_chunks+1")

    def messages(self, stored_id, start=0, end=None):
        with self._lock:
            return self._messages(stored_id, start, end, raw=True)

    def _messages(self, stored_id, start, end, *, raw, cache=None):
        if start < 0 or (end is not None and (end < 0 or end < start)):
            return []
        query = (
            "SELECT m.*, l.block_idx FROM message m JOIN line l "
            "ON l.thread_id=m.thread_id AND l.idx=m.line_idx "
            "WHERE m.thread_id=? AND seq>=?"
        )
        params = (stored_id, start)
        if end is not None:
            query += " AND seq<=?"
            params += (end,)
        rows = self._conn.execute(query + " ORDER BY seq", params).fetchall()
        result = []
        if cache is None:
            cache = {}
        for row in rows:
            idx = row["block_idx"]
            key = (stored_id, idx)
            if key not in cache:
                columns = _BLOCK_COLUMNS if raw else "first_seq, first_line, text_dict, ztext"
                block = self._conn.execute(
                    f"SELECT {columns} FROM block WHERE thread_id=? AND idx=?", (stored_id, idx)
                ).fetchone()
                cache[key] = (
                    block,
                    self._frame(block, "text"),
                    self._frame(block, "raw") if raw else None,
                )
            block, texts, raws = cache[key]
            if raw and raws is None:
                # A text-only cache entry deliberately omitted raw columns.
                raw_block = self._conn.execute(
                    "SELECT raw_dict, zraw FROM block WHERE thread_id=? AND idx=?",
                    (stored_id, idx),
                ).fetchone()
                raws = self._frame(raw_block, "raw")
                cache[key] = (block, texts, raws)
            result.append(
                ThreadMessage(
                    seq=row["seq"],
                    text=texts[row["seq"] - block["first_seq"]],
                    raw=raws[row["line_idx"] - block["first_line"]] if raw else "",
                    **{
                        field: frames.string(row[field])
                        for field in ("role", "kind", "name", "call_id", "at")
                    },
                )
            )
        return result

    def _chunk_text(self, row, cache):
        items = self._messages(
            row["thread_id"], row["seq_start"], row["seq_end"], raw=False, cache=cache
        )
        return self._render_chunk(row, items)

    @staticmethod
    def _render_chunk(row, items):
        if len(items) == 1:
            return _prefix(items[0]) + items[0].text[row["off"] : row["off"] + row["len"]]
        rendered = "\n".join(_prefix(item) + item.text for item in items)
        return rendered[row["off"] : row["off"] + row["len"]]

    def chunks(self, stored_id):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM chunk WHERE thread_id=? ORDER BY idx", (stored_id,)
            ).fetchall()
            cache = {}
            return [
                Chunk(row["idx"], row["seq_start"], row["seq_end"], self._chunk_text(row, cache))
                for row in rows
            ]

    def search(self, stored_ids, query, limit=8):
        tokens = query.split()
        if not tokens or not stored_ids or limit <= 0:
            return []
        with self._lock:
            ids = list(dict.fromkeys(stored_ids))
            rows = (
                self._search_fts(ids, tokens, limit)
                if self._fts_enabled and "\0" not in query and not _SURROGATES.search(query)
                else self._search_literal(ids, tokens)
            )
            rows.sort(key=lambda row: (-row["score"], row["thread_id"], row["idx"]))
            cache = {}
            return [
                RecallHit(
                    row["thread_id"],
                    row["idx"],
                    row["seq_start"],
                    row["seq_end"],
                    row["score"],
                    row["text"] if "text" in row else self._chunk_text(row, cache),
                )
                for row in rows[:limit]
            ]

    def _search_fts(self, stored_ids, tokens, limit):
        match = " OR ".join('"' + token.replace('"', '""') + '"' for token in tokens)
        batch_size = max(1, min(500, self._sqlite_limit("SQLITE_LIMIT_VARIABLE_NUMBER", 999) - 2))
        rows = []
        for offset in range(0, len(stored_ids), batch_size):
            ids = stored_ids[offset : offset + batch_size]
            placeholders = ",".join("?" for _ in ids)
            rows.extend(
                dict(row)
                for row in self._conn.execute(
                    "SELECT c.*, -bm25(chunk_fts) AS score FROM chunk_fts "
                    "JOIN chunk c ON c.rowid=chunk_fts.rowid "
                    f"WHERE chunk_fts MATCH ? AND c.thread_id IN ({placeholders}) "
                    "ORDER BY bm25(chunk_fts), c.thread_id, c.idx LIMIT ?",
                    (match, *ids, limit),
                ).fetchall()
            )
        return rows

    def _search_literal(self, stored_ids, tokens):
        # Match SQLite LIKE's ASCII-only case folding and whole NUL tokens.
        lower = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
        tokens = [token.translate(lower) for token in tokens]
        rows = []
        for stored_id in stored_ids:
            for chunk in self.chunks(stored_id):
                if any(token in chunk.text.translate(lower) for token in tokens):
                    rows.append(
                        dict(
                            thread_id=stored_id,
                            idx=chunk.index,
                            seq_start=chunk.seq_start,
                            seq_end=chunk.seq_end,
                            score=0.0,
                            text=chunk.text,
                        )
                    )
        return rows

    def put_doc(self, handoff_id, markdown):
        super().put_doc(handoff_id, frames.sql_string(markdown))

    def doc(self, handoff_id):
        return frames.string(super().doc(handoff_id))
