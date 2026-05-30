"""SQLite-vec storage backend for MemPalace (RFC 001 reference implementation)."""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import struct
import threading
from typing import Optional

from .base import (
    BackendClosedError,
    BackendError,
    BaseBackend,
    BaseCollection,
    DimensionMismatchError,
    GetResult,
    HealthStatus,
    PalaceNotFoundError,
    PalaceRef,
    QueryResult,
)

logger = logging.getLogger(__name__)

_DB_FILENAME = "palace.db"
_VEC_WARNED = False  # warn once if sqlite-vec is unavailable

_SAFE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# How many extra ANN candidates to fetch before applying Python-side filters.
# Over-fetching compensates for rows that are filtered out by where/where_document.
_ANN_OVERFETCH = 10


def _safe_table_name(name: str) -> str:
    """Validate that ``name`` is a safe SQL identifier (no injection risk).

    Only ``[A-Za-z_][A-Za-z0-9_]*`` is accepted — the same rules SQLite
    itself uses for unquoted identifiers.  Raises ``ValueError`` on violation
    so the error surfaces immediately at construction time rather than at the
    first SQL execution.
    """
    if not _SAFE_NAME_RE.match(name):
        raise ValueError(
            f"collection_name {name!r} is not a valid SQL identifier. "
            "Use only letters, digits, and underscores, starting with a letter or underscore."
        )
    return name


# ---------------------------------------------------------------------------
# sqlite-vec availability probe
# ---------------------------------------------------------------------------


def _try_load_sqlite_vec(conn: sqlite3.Connection) -> bool:
    """Attempt to load the sqlite-vec extension. Returns True on success."""
    global _VEC_WARNED
    try:
        conn.enable_load_extension(True)
        import sqlite_vec  # noqa: F401 — just check importability

        conn.load_extension(sqlite_vec.loadable_path())
        conn.enable_load_extension(False)
        return True
    except Exception:
        if not _VEC_WARNED:
            logger.warning(
                "sqlite-vec extension not available — vector search will use "
                "brute-force cosine scan. Install it with: pip install sqlite-vec"
            )
            _VEC_WARNED = True
        return False


# ---------------------------------------------------------------------------
# Pure-Python brute-force cosine fallback
# ---------------------------------------------------------------------------


def _unpack_f32(blob: Optional[bytes]) -> Optional[list[float]]:
    if not blob:
        return None
    n = len(blob) // 4
    return list(struct.unpack_from(f"{n}f", blob))


def _pack_f32(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _cosine_distance(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = sum(x * x for x in a) ** 0.5
    mag_b = sum(x * x for x in b) ** 0.5
    denom = mag_a * mag_b
    if denom == 0.0:
        return 1.0
    return 1.0 - dot / denom


def _cosine_brute(
    query_vec: list[float],
    rows: list[tuple],  # (id, document, metadata_dict_or_json, embedding_blob)
    n_results: int,
) -> list[tuple[str, str, dict, float]]:
    """Pure-Python cosine scan. Returns list of (id, doc, meta, distance).

    The third element of each row may be a pre-parsed ``dict`` (from the
    in-memory query path) or a JSON string (from the sqlite-vec fallback
    path). Both forms are accepted to avoid a pointless serialisation
    round-trip in the common case.
    """
    scored: list[tuple[float, str, str, dict]] = []
    for row_id, doc, meta_or_json, emb_blob in rows:
        emb = _unpack_f32(emb_blob)
        if emb is None:
            continue
        dist = _cosine_distance(query_vec, emb)
        if isinstance(meta_or_json, dict):
            meta = meta_or_json
        else:
            meta = json.loads(meta_or_json) if meta_or_json else {}
        scored.append((dist, row_id, doc, meta))
    scored.sort(key=lambda t: t[0])
    return [(rid, doc, meta, dist) for dist, rid, doc, meta in scored[:n_results]]


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


class SqliteVecCollection(BaseCollection):
    """A single palace collection backed by a SQLite database file."""

    def __init__(self, db_path: str, collection_name: str) -> None:
        self._db_path = db_path
        self._table = _safe_table_name(collection_name)
        self._vec_table = f"vec_{self._table}"
        self._idx = f"idx_{self._table}_id"
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        self._has_vec = False
        self._vec_dim: Optional[int] = None  # detected from first write; None until then
        self._closed = False

    # ------------------------------------------------------------------
    # Internal connection management
    # ------------------------------------------------------------------

    def _connection(self) -> sqlite3.Connection:
        if self._closed:
            raise BackendClosedError("SqliteVecCollection has been closed")
        if self._conn is None:
            self._conn = sqlite3.connect(
                self._db_path,
                check_same_thread=False,
                timeout=30,
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._has_vec = _try_load_sqlite_vec(self._conn)
            self._init_schema()
        return self._conn

    def _init_schema(self) -> None:
        conn = self._conn
        # Main table — stores verbatim content + metadata
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {self._table} (
                id       TEXT PRIMARY KEY,
                document TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{{}}',
                embedding BLOB
            )
        """)
        conn.execute(f"CREATE INDEX IF NOT EXISTS {self._idx} ON {self._table}(id)")
        conn.commit()
        # Detect dimension from an existing vec table (reconnect after first write).
        if self._has_vec:
            self._vec_dim = self._read_vec_dim(conn)

    def _read_vec_dim(self, conn: sqlite3.Connection) -> Optional[int]:
        """Read the embedding dimension from an existing sqlite-vec virtual table.

        Returns the dimension as an int, or None when the table doesn't exist yet.
        The dimension is encoded in the CREATE VIRTUAL TABLE SQL stored in
        sqlite_master, e.g. ``vec0(embedding float[384])``.
        """
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (self._vec_table,),
        ).fetchone()
        if row and row[0]:
            import re as _re

            m = _re.search(r"float\[(\d+)\]", row[0])
            if m:
                return int(m.group(1))
        return None

    def _ensure_vec_table(self, conn: sqlite3.Connection, dim: int) -> None:
        """Create the sqlite-vec virtual table for ``dim``-dimensional embeddings.

        Called lazily on the first write that includes an embedding.  If the
        table already exists its dimension is validated against ``dim`` and a
        :class:`DimensionMismatchError` is raised on mismatch.
        """
        if self._vec_dim is not None:
            if self._vec_dim != dim:
                raise DimensionMismatchError(
                    f"Embedding dimension {dim} does not match the existing "
                    f"sqlite-vec table dimension {self._vec_dim} for {self._vec_table!r}. "
                    "All embeddings in a collection must have the same dimension."
                )
            return  # table exists and dim matches

        # First write: create the table and record the dimension.
        try:
            conn.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS {self._vec_table} "
                f"USING vec0(embedding float[{dim}])"
            )
            conn.commit()
            self._vec_dim = dim
        except Exception as exc:
            logger.warning("Could not create %s virtual table: %s", self._vec_table, exc)
            self._has_vec = False

    # ------------------------------------------------------------------
    # Metadata filtering (pure-Python, applied post-query)
    # ------------------------------------------------------------------

    @staticmethod
    def _meta_matches(meta: dict, where: Optional[dict]) -> bool:
        """Evaluate a ChromaDB-style where-clause against a metadata dict."""
        if not where:
            return True

        for key, condition in where.items():
            if key == "$and":
                if not all(SqliteVecCollection._meta_matches(meta, sub) for sub in condition):
                    return False
                continue
            if key == "$or":
                if not any(SqliteVecCollection._meta_matches(meta, sub) for sub in condition):
                    return False
                continue

            val = meta.get(key)
            if isinstance(condition, dict):
                for op, operand in condition.items():
                    if op == "$eq" and val != operand:
                        return False
                    elif op == "$ne" and val == operand:
                        return False
                    elif op == "$in" and val not in operand:
                        return False
                    elif op == "$nin" and val in operand:
                        return False
                    elif op == "$gt" and not (val is not None and val > operand):
                        return False
                    elif op == "$gte" and not (val is not None and val >= operand):
                        return False
                    elif op == "$lt" and not (val is not None and val < operand):
                        return False
                    elif op == "$lte" and not (val is not None and val <= operand):
                        return False
                    elif op == "$contains":
                        if not (isinstance(val, str) and operand in val):
                            return False
            else:
                # Implicit $eq
                if val != condition:
                    return False
        return True

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def _upsert_rows(
        self,
        ids: list[str],
        documents: list[str],
        metadatas: Optional[list[dict]],
        embeddings: Optional[list[list[float]]],
    ) -> None:
        with self._lock:
            conn = self._connection()
            metas = metadatas or [{} for _ in ids]
            embs = embeddings or [None] * len(ids)
            # Lazy vec-table creation: detect dim from the first non-None embedding.
            if self._has_vec and embeddings:
                first_emb = next((e for e in embeddings if e is not None), None)
                if first_emb is not None:
                    self._ensure_vec_table(conn, len(first_emb))
            with conn:
                for row_id, doc, meta, emb in zip(ids, documents, metas, embs):
                    meta_json = json.dumps(meta or {}, ensure_ascii=False)
                    emb_blob = _pack_f32(emb) if emb else None
                    conn.execute(
                        f"INSERT OR REPLACE INTO {self._table}(id, document, metadata, embedding) "
                        "VALUES (?, ?, ?, ?)",
                        (row_id, doc, meta_json, emb_blob),
                    )
                    if self._has_vec and emb and self._vec_dim is not None:
                        # sqlite-vec uses rowid; resolve it after upsert
                        row = conn.execute(
                            f"SELECT rowid FROM {self._table} WHERE id = ?", (row_id,)
                        ).fetchone()
                        if row:
                            rowid = row[0]
                            conn.execute(
                                f"INSERT OR REPLACE INTO {self._vec_table}(rowid, embedding) "
                                "VALUES (?, ?)",
                                (rowid, emb_blob),
                            )

    def add(
        self,
        *,
        documents: list[str],
        ids: list[str],
        metadatas: Optional[list[dict]] = None,
        embeddings: Optional[list[list[float]]] = None,
    ) -> None:
        # add() must not silently overwrite — check for pre-existing IDs first.
        with self._lock:
            conn = self._connection()
            if ids:
                placeholders = ",".join("?" * len(ids))
                existing = conn.execute(
                    f"SELECT id FROM {self._table} WHERE id IN ({placeholders})", ids
                ).fetchall()
                if existing:
                    dupes = [row[0] for row in existing]
                    raise BackendError(
                        f"add() called with IDs that already exist "
                        f"(use upsert() to overwrite): {dupes}"
                    )
        self._upsert_rows(ids, documents, metadatas, embeddings)

    def upsert(
        self,
        *,
        documents: list[str],
        ids: list[str],
        metadatas: Optional[list[dict]] = None,
        embeddings: Optional[list[list[float]]] = None,
    ) -> None:
        self._upsert_rows(ids, documents, metadatas, embeddings)

    def update(
        self,
        *,
        ids: list[str],
        documents: Optional[list[str]] = None,
        metadatas: Optional[list[dict]] = None,
        embeddings: Optional[list[list[float]]] = None,
    ) -> None:
        if documents is None and metadatas is None and embeddings is None:
            raise ValueError("update requires at least one of documents, metadatas, embeddings")
        n = len(ids)
        for label, value in (
            ("documents", documents),
            ("metadatas", metadatas),
            ("embeddings", embeddings),
        ):
            if value is not None and len(value) != n:
                raise ValueError(f"{label} length {len(value)} does not match ids length {n}")
        existing = self.get(ids=ids, include=["documents", "metadatas", "embeddings"])
        by_id = {
            eid: (
                existing.documents[i],
                existing.metadatas[i],
                existing.embeddings[i] if existing.embeddings else None,
            )
            for i, eid in enumerate(existing.ids)
        }
        merged_docs, merged_metas, merged_embs = [], [], []
        for i, row_id in enumerate(ids):
            prev_doc, prev_meta, prev_emb = by_id.get(row_id, ("", {}, None))
            merged_docs.append(documents[i] if documents else prev_doc)
            new_meta = dict(prev_meta or {})
            if metadatas:
                new_meta.update(metadatas[i] or {})
            merged_metas.append(new_meta)
            merged_embs.append(embeddings[i] if embeddings else prev_emb)
        self.upsert(
            documents=merged_docs,
            ids=list(ids),
            metadatas=merged_metas,
            embeddings=merged_embs if any(e is not None for e in merged_embs) else None,
        )

    def delete(
        self,
        *,
        ids: Optional[list[str]] = None,
        where: Optional[dict] = None,
    ) -> None:
        with self._lock:
            conn = self._connection()
            with conn:
                if ids is not None:
                    for row_id in ids:
                        row = conn.execute(
                            f"SELECT rowid FROM {self._table} WHERE id = ?", (row_id,)
                        ).fetchone()
                        if row and self._has_vec:
                            conn.execute(
                                f"DELETE FROM {self._vec_table} WHERE rowid = ?", (row[0],)
                            )
                        conn.execute(f"DELETE FROM {self._table} WHERE id = ?", (row_id,))
                elif where is not None:
                    # Must filter in Python — SQLite can't index JSON fields
                    rows = conn.execute(f"SELECT id, rowid, metadata FROM {self._table}").fetchall()
                    to_delete_ids = []
                    to_delete_rowids = []
                    for row in rows:
                        meta = json.loads(row["metadata"]) if row["metadata"] else {}
                        if self._meta_matches(meta, where):
                            to_delete_ids.append(row["id"])
                            to_delete_rowids.append(row["rowid"])
                    for row_id in to_delete_ids:
                        conn.execute(f"DELETE FROM {self._table} WHERE id = ?", (row_id,))
                    if self._has_vec:
                        for rowid in to_delete_rowids:
                            conn.execute(f"DELETE FROM {self._vec_table} WHERE rowid = ?", (rowid,))

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def count(self) -> int:
        with self._lock:
            conn = self._connection()
            row = conn.execute(f"SELECT COUNT(*) FROM {self._table}").fetchone()
            return row[0] if row else 0

    def get(
        self,
        *,
        ids: Optional[list[str]] = None,
        where: Optional[dict] = None,
        where_document: Optional[dict] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        include: Optional[list[str]] = None,
    ) -> GetResult:
        inc = set(include) if include else {"documents", "metadatas"}
        with self._lock:
            conn = self._connection()
            if ids is not None:
                placeholders = ",".join("?" * len(ids))
                rows = conn.execute(
                    f"SELECT id, document, metadata, embedding "
                    f"FROM {self._table} WHERE id IN ({placeholders})",
                    ids,
                ).fetchall()
            else:
                sql = f"SELECT id, document, metadata, embedding FROM {self._table}"
                if limit is not None:
                    sql += f" LIMIT {int(limit)}"
                    if offset is not None:
                        sql += f" OFFSET {int(offset)}"
                elif offset is not None:
                    # SQLite requires LIMIT when OFFSET is used; -1 means no limit.
                    sql += f" LIMIT -1 OFFSET {int(offset)}"
                rows = conn.execute(sql).fetchall()

        # Apply Python-side filters
        filtered = []
        for row in rows:
            meta = json.loads(row["metadata"]) if row["metadata"] else {}
            if where and not self._meta_matches(meta, where):
                continue
            if where_document:
                doc = row["document"] or ""
                cont = where_document.get("$contains")
                if cont and cont not in doc:
                    continue
            filtered.append((row["id"], row["document"], meta, row["embedding"]))

        # Apply offset/limit after Python filtering when IDs were supplied
        if ids is None and (limit is not None or offset is not None):
            pass  # already applied in SQL
        elif ids is not None:
            start = offset or 0
            end = start + limit if limit else None
            filtered = filtered[start:end]

        out_ids = [r[0] for r in filtered]
        out_docs = [r[1] for r in filtered] if "documents" in inc else []
        out_metas = [r[2] for r in filtered] if "metadatas" in inc else []
        out_embs: Optional[list[list[float]]] = None
        if "embeddings" in inc:
            out_embs = []
            for r in filtered:
                vec = _unpack_f32(r[3])
                out_embs.append(vec or [])

        return GetResult(
            ids=out_ids,
            documents=out_docs,
            metadatas=out_metas,
            embeddings=out_embs,
        )

    def query(
        self,
        *,
        query_texts: Optional[list[str]] = None,
        query_embeddings: Optional[list[list[float]]] = None,
        n_results: int = 10,
        where: Optional[dict] = None,
        where_document: Optional[dict] = None,
        include: Optional[list[str]] = None,
    ) -> QueryResult:
        if (query_texts is None) == (query_embeddings is None):
            raise ValueError("query requires exactly one of query_texts or query_embeddings")

        vecs = query_embeddings if query_embeddings is not None else self._embed_texts(query_texts)
        inc = set(include) if include else {"documents", "metadatas", "distances"}

        all_ids: list[list[str]] = []
        all_docs: list[list[str]] = []
        all_metas: list[list[dict]] = []
        all_dists: list[list[float]] = []

        with self._lock:
            conn = self._connection()

            # Pre-load all rows once (used by brute-force path and ANN fallback).
            all_rows = conn.execute(
                f"SELECT id, document, metadata, embedding FROM {self._table}"
            ).fetchall()

        for qvec in vecs:
            hits = self._query_single(qvec, n_results, where, where_document, conn, all_rows)

            q_ids, q_docs, q_metas, q_dists = [], [], [], []
            for row_id, doc, meta, dist in hits:
                q_ids.append(row_id)
                q_docs.append(doc)
                q_metas.append(meta)
                q_dists.append(dist)

            all_ids.append(q_ids)
            all_docs.append(q_docs if "documents" in inc else [])
            all_metas.append(q_metas if "metadatas" in inc else [])
            all_dists.append(q_dists if "distances" in inc else [])

        return QueryResult(
            ids=all_ids,
            documents=all_docs,
            metadatas=all_metas,
            distances=all_dists,
            embeddings=None,
        )

    def _query_single(
        self,
        qvec: list[float],
        n_results: int,
        where: Optional[dict],
        where_document: Optional[dict],
        conn: sqlite3.Connection,
        all_rows: list,
    ) -> list[tuple[str, str, dict, float]]:
        """Execute one query vector, returning (id, doc, meta, distance) tuples.

        Strategy:
        1. When sqlite-vec is available, over-fetch ``n_results * _ANN_OVERFETCH``
           ANN candidates, apply Python-side where/where_document filters, and
           return the top ``n_results`` survivors.
        2. If survivors < n_results (too many filtered out), fall through to the
           brute-force scan of ``all_rows``.
        3. When sqlite-vec is unavailable, go straight to brute-force.
        """
        has_filter = bool(where or where_document)

        if self._has_vec and self._vec_dim is not None:
            # Step 1: ANN over-fetch.
            overfetch_k = n_results * _ANN_OVERFETCH
            ann_hits = self._vec_query(qvec, overfetch_k, conn)

            if has_filter:
                ann_hits = [
                    (rid, doc, meta, dist)
                    for rid, doc, meta, dist in ann_hits
                    if self._meta_matches(meta, where) and self._doc_matches(doc, where_document)
                ]

            if len(ann_hits) >= n_results:
                return ann_hits[:n_results]

            # Step 2: ANN didn't yield enough results — fall back to brute-force
            # on the full table so we don't silently return fewer than requested.
            logger.debug(
                "ANN over-fetch returned %d/%d results with filters; "
                "falling back to brute-force scan.",
                len(ann_hits),
                n_results,
            )

        # Step 3: Brute-force scan.
        candidate_rows = []
        for row in all_rows:
            meta = json.loads(row["metadata"]) if row["metadata"] else {}
            if where and not self._meta_matches(meta, where):
                continue
            if not self._doc_matches(row["document"] or "", where_document):
                continue
            candidate_rows.append((row["id"], row["document"], meta, row["embedding"]))
        return _cosine_brute(qvec, candidate_rows, n_results)

    @staticmethod
    def _doc_matches(doc: str, where_document: Optional[dict]) -> bool:
        """Return True when ``doc`` satisfies ``where_document`` (or no filter given)."""
        if not where_document:
            return True
        cont = where_document.get("$contains")
        return cont is None or cont in doc

    def _vec_query(
        self,
        qvec: list[float],
        n_results: int,
        conn: sqlite3.Connection,
    ) -> list[tuple[str, str, dict, float]]:
        """Use sqlite-vec KNN search, then join back to the main table."""
        qblob = _pack_f32(qvec)
        try:
            vec_rows = conn.execute(
                f"SELECT rowid, distance FROM {self._vec_table} "
                "WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
                (qblob, n_results),
            ).fetchall()
        except Exception as exc:
            logger.warning("sqlite-vec query failed, falling back to brute-force: %s", exc)
            all_rows = conn.execute(
                f"SELECT id, document, metadata, embedding FROM {self._table}"
            ).fetchall()
            raw = [(r["id"], r["document"], r["metadata"], r["embedding"]) for r in all_rows]
            return _cosine_brute(qvec, raw, n_results)

        if not vec_rows:
            return []

        rowids = [r[0] for r in vec_rows]
        dist_by_rowid = {r[0]: r[1] for r in vec_rows}

        placeholders = ",".join("?" * len(rowids))
        drawer_rows = conn.execute(
            f"SELECT rowid, id, document, metadata FROM {self._table} "
            f"WHERE rowid IN ({placeholders})",
            rowids,
        ).fetchall()

        result = []
        for row in drawer_rows:
            meta = json.loads(row["metadata"]) if row["metadata"] else {}
            dist = dist_by_rowid.get(row["rowid"], 1.0)
            result.append((row["id"], row["document"], meta, dist))
        result.sort(key=lambda t: t[3])
        return result

    @staticmethod
    def _embed_texts(texts: list[str]) -> list[list[float]]:
        """Embed a list of texts using the configured MemPalace embedding function."""
        try:
            from ..embedding import get_embedding_function

            ef = get_embedding_function()
            return ef(texts)
        except Exception as exc:
            raise BackendError(
                f"SqliteVecCollection requires an embedding function but got: {exc}. "
                "Install the default embedder with: pip install mempalace or "
                "pass query_embeddings= directly."
            ) from exc

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None
            self._closed = True

    def health(self) -> HealthStatus:
        try:
            self.count()
            return HealthStatus.healthy()
        except Exception as exc:
            return HealthStatus.unhealthy(str(exc))


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


class SqliteVecBackend(BaseBackend):
    """MemPalace storage backend backed by SQLite + optional sqlite-vec ANN.

    Registered under the name ``"sqlite_vec"``.

    Capabilities:
      - ``local_mode`` — palace is a single file, no service required.
      - ``supports_metadata_filters`` — where-clauses are evaluated in Python.
      - ``supports_embeddings_in`` — embeddings can be stored alongside text.
      - ``supports_embeddings_out`` — stored embeddings can be read back.

    Optional:
      - ``supports_ann`` — present only when sqlite-vec extension loads
        successfully on first get_collection() call.
    """

    name = "sqlite_vec"
    capabilities = frozenset(
        {
            "local_mode",
            "supports_metadata_filters",
            "supports_embeddings_in",
            "supports_embeddings_out",
            "supports_contains_fast",
        }
    )

    def __init__(self) -> None:
        self._collections: dict[str, SqliteVecCollection] = {}
        self._lock = threading.Lock()
        self._closed = False

    def get_collection(
        self,
        *,
        palace: PalaceRef,
        collection_name: str,
        create: bool = False,
        options: Optional[dict] = None,
    ) -> SqliteVecCollection:
        if self._closed:
            raise BackendClosedError("SqliteVecBackend has been closed")

        palace_path = palace.local_path
        if palace_path is None:
            raise PalaceNotFoundError("SqliteVecBackend requires PalaceRef.local_path")

        if not create and not os.path.isdir(palace_path):
            raise PalaceNotFoundError(palace_path)

        if create:
            os.makedirs(palace_path, exist_ok=True)
            try:
                os.chmod(palace_path, 0o700)
            except (OSError, NotImplementedError):
                pass

        db_path = os.path.join(palace_path, _DB_FILENAME)
        cache_key = f"{db_path}::{collection_name}"

        with self._lock:
            col = self._collections.get(cache_key)
            if col is None or col._closed:
                col = SqliteVecCollection(db_path, collection_name)
                self._collections[cache_key] = col
        return col

    def close_palace(self, palace: PalaceRef) -> None:
        path = palace.local_path
        if not path:
            return
        db_path = os.path.join(path, _DB_FILENAME)
        with self._lock:
            for key in list(self._collections.keys()):
                if key.startswith(db_path + "::"):
                    try:
                        self._collections[key].close()
                    except Exception:
                        pass
                    del self._collections[key]

    def close(self) -> None:
        with self._lock:
            for col in self._collections.values():
                try:
                    col.close()
                except Exception:
                    pass
            self._collections.clear()
            self._closed = True

    def health(self, palace: Optional[PalaceRef] = None) -> HealthStatus:
        if self._closed:
            return HealthStatus.unhealthy("backend closed")
        return HealthStatus.healthy()

    @classmethod
    def detect(cls, path: str) -> bool:
        """Return True when path contains palace.db but no chroma.sqlite3.

        The presence of chroma.sqlite3 means the Chroma backend should win;
        palace.db alone signals a sqlite_vec palace.
        """
        has_palace_db = os.path.isfile(os.path.join(path, _DB_FILENAME))
        has_chroma_db = os.path.isfile(os.path.join(path, "chroma.sqlite3"))
        return has_palace_db and not has_chroma_db
