"""
vector_cache.py — Portable embedding cache (RFC 004 distributed derivation)

A vector is a pure function of (content, embedder identity). When a fleet
pins one identity, vectors become portable facts: computed on whichever
machine has the horsepower, shipped with content, folded insert-only on the
receiver. This sidecar cache (``vector_cache.sqlite3`` in the palace dir)
holds vectors keyed ``(drawer_id, embedder)`` as packed float32 blobs.

The cache is strictly derived state about content the palace already holds:
building it READS the content store and writes only the sidecar — safe to
run against a quiescent origin without breaking quiescence guarantees.

Wire format: vectors travel base64(float32 little-endian). ~1.5 KB raw per
384-dim vector; a 156k-drawer fleet merge is a few hundred MB — LAN/tailnet
territory, never WAN-cloud.
"""

import base64
import os
import sqlite3
import struct
import threading
from pathlib import Path

VECTOR_CACHE_FILENAME = "vector_cache.sqlite3"


def pack_vector(vector: list) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def unpack_vector(blob: bytes) -> list:
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


def vector_to_b64(vector: list) -> str:
    return base64.b64encode(pack_vector(vector)).decode("ascii")


def b64_to_vector(encoded: str) -> list:
    return unpack_vector(base64.b64decode(encoded))


class VectorCache:
    """SQLite-backed vector store, WAL, thread-safe like the other sidecars."""

    def __init__(self, palace_path: str):
        self.db_path = str(Path(os.path.expanduser(palace_path)) / VECTOR_CACHE_FILENAME)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = None
        self._lock = threading.Lock()
        conn = self._conn()
        conn.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS vectors (
                drawer_id TEXT NOT NULL,
                embedder TEXT NOT NULL,
                dim INTEGER NOT NULL,
                vec BLOB NOT NULL,
                PRIMARY KEY (drawer_id, embedder)
            );
        """)
        conn.commit()

    def _conn(self):
        if self._connection is None:
            self._connection = sqlite3.connect(self.db_path, timeout=10, check_same_thread=False)
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.row_factory = sqlite3.Row
        return self._connection

    def close(self):
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def put_many(self, embedder: str, items: list) -> int:
        """items: [(drawer_id, vector: list[float])]. Idempotent by key."""
        with self._lock:
            conn = self._conn()
            with conn:
                conn.executemany(
                    "INSERT OR REPLACE INTO vectors (drawer_id, embedder, dim, vec)"
                    " VALUES (?, ?, ?, ?)",
                    [(d, embedder, len(v), pack_vector(v)) for d, v in items],
                )
        return len(items)

    def get_many(self, embedder: str, drawer_ids: list) -> dict:
        """{drawer_id: vector} for the ids present under this embedder."""
        if not drawer_ids:
            return {}
        out = {}
        with self._lock:
            conn = self._conn()
            for start in range(0, len(drawer_ids), 500):
                chunk = drawer_ids[start : start + 500]
                placeholders = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    f"SELECT drawer_id, vec FROM vectors WHERE embedder = ? "
                    f"AND drawer_id IN ({placeholders})",
                    [embedder, *chunk],
                ).fetchall()
                for row in rows:
                    out[row["drawer_id"]] = unpack_vector(row["vec"])
        return out

    def missing_ids(self, embedder: str, drawer_ids: list) -> list:
        cached = self.get_many(embedder, drawer_ids)
        return [d for d in drawer_ids if d not in cached]

    def count(self, embedder: str) -> int:
        with self._lock:
            conn = self._conn()
            row = conn.execute(
                "SELECT count(*) AS n FROM vectors WHERE embedder = ?", (embedder,)
            ).fetchone()
        return row["n"]


def build_cache(
    palace_path: str,
    model: str,
    batch_size: int = 256,
    authored_only: bool = True,
    progress=None,
) -> dict:
    """Bulk-embed the local palace's documents into the vector cache.

    Reads the content store paged, embeds missing documents with the named
    model in ``batch_size`` batches, writes the sidecar. Idempotent and
    resumable: already-cached (drawer_id, embedder) pairs are skipped, so a
    crash resumes where it left off. ``authored_only`` limits the job to
    drawers this replica authors (no ``replica_origin`` stamp) — the set a
    peer would pull from us.
    """
    from .embedding import get_embedding_function
    from .palace import get_collection
    from .replica_sync import REPLICA_ORIGIN_KEY

    # Resolve the embedding function once, for the requested identity.
    ef = get_embedding_function(model=model)

    def embed(texts):
        vectors = ef(input=texts)
        return [list(v) for v in vectors]

    col = get_collection(os.path.expanduser(palace_path))
    if col is None:
        raise ValueError(f"no collection in {palace_path!r}")
    cache = VectorCache(palace_path)
    try:
        scanned = 0
        embedded = 0
        skipped_cached = 0
        offset = 0
        while True:
            batch = col.get(limit=1000, offset=offset, include=["documents", "metadatas"])
            ids = batch.get("ids") or []
            if not ids:
                break
            docs = batch.get("documents") or []
            metas = batch.get("metadatas") or []
            candidates = [
                (i, d)
                for i, d, m in zip(ids, docs, metas)
                if not (authored_only and (m or {}).get(REPLICA_ORIGIN_KEY))
            ]
            scanned += len(candidates)
            todo_ids = cache.missing_ids(model, [i for i, _ in candidates])
            skipped_cached += len(candidates) - len(todo_ids)
            todo = {i: d for i, d in candidates if i in set(todo_ids)}
            pending = list(todo.items())
            for start in range(0, len(pending), batch_size):
                chunk = pending[start : start + batch_size]
                vectors = embed([d for _, d in chunk])
                cache.put_many(model, [(i, v) for (i, _), v in zip(chunk, vectors)])
                embedded += len(chunk)
                if progress:
                    progress(embedded, scanned)
            offset += len(ids)
        return {
            "model": model,
            "scanned": scanned,
            "embedded": embedded,
            "skipped_cached": skipped_cached,
            "cache_total": cache.count(model),
        }
    finally:
        cache.close()
