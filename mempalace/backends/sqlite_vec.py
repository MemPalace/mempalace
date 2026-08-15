"""SQLite-vec ANN backend for MemPalace.

Fast approximate nearest-neighbour search via sqlite-vec (Alex Garcia).
Uses vec0 virtual table with cosine distance — 0.003s search vs 2-5s
for brute-force exact cosine. Ideal for WSL where ChromaDB HNSW
regularly corrupts across restarts.

Database file: sqlite_vec.sqlite3
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import sqlite3
import struct
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar, Optional, Union

import numpy as np

from .base import (
    BackendClosedError,
    BaseBackend,
    BaseCollection,
    CollectionNotInitializedError,
    DimensionMismatchError,
    EmbedderIdentity,
    GetResult,
    HealthStatus,
    LexicalHit,
    LexicalResult,
    MaintenanceResult,
    PalaceNotFoundError,
    PalaceRef,
    QueryResult,
    UnsupportedFilterError,
    _IncludeSpec,
)

logger = logging.getLogger(__name__)

_DB_FILENAME = "sqlite_vec.sqlite3"
_TOKEN_RE = re.compile(r"\w{2,}", re.UNICODE)
_SUPPORTED_OPERATORS = frozenset(
    {"$eq", "$ne", "$in", "$nin", "$and", "$or", "$contains", "$gt", "$gte", "$lt", "$lte"}
)
_EMBEDDING_DIM = 384  # EmbeddingGemma ONNX


# ── sqlite_vec loader ────────────────────────────────────────────────────


def _load_sqlite_vec():
    """Load sqlite_vec extension, trying multiple paths."""
    try:
        import sqlite_vec

        return sqlite_vec
    except ImportError:
        pass
    # Fallback: auto-detect mempalace site-packages
    tools_base = Path.home() / ".local/share/uv/tools/mempalace/lib"
    if tools_base.exists():
        for py_dir in sorted(tools_base.iterdir(), reverse=True):
            if py_dir.name.startswith("python"):
                sp = str(py_dir / "site-packages")
                if sp not in sys.path:
                    sys.path.insert(0, sp)
                try:
                    import sqlite_vec

                    return sqlite_vec
                except ImportError:
                    continue
    raise ImportError(
        "sqlite_vec is required for sqlite_vec backend. Install it with: uv pip install sqlite-vec"
    )


_sqlite_vec_module = None
_has_vec = False


def _ensure_sqlite_vec():
    """Load sqlite_vec on first use, not at import time."""
    global _sqlite_vec_module, _has_vec
    if _sqlite_vec_module is not None:
        return _sqlite_vec_module
    _sqlite_vec_module = _load_sqlite_vec()
    _has_vec = True
    return _sqlite_vec_module


# ── Helpers ──────────────────────────────────────────────────────────────


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_dumps(obj: Any) -> str:
    return json.dumps(obj or {}, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _json_loads(text: str | None) -> dict:
    if not text:
        return {}
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _as_vector_array(vector: list[float]) -> np.ndarray:
    arr = np.asarray(vector, dtype=np.float32)
    if arr.ndim != 1 or arr.size == 0:
        raise ValueError("embedding must be a non-empty 1D vector")
    return arr


def _encode_vector(vector: list[float]) -> bytes:
    arr = _as_vector_array(vector)
    return struct.pack(f"{len(arr)}f", *arr)


def _decode_vector(blob: bytes | None) -> list[float]:
    if not blob:
        return []
    return list(struct.unpack(f"{len(blob) // 4}f", blob))


def _tokenize(text: str) -> list[str]:
    if not text:
        return []
    return _TOKEN_RE.findall(text.lower())


def _bm25_scores(query: str, documents: list[str], k1: float = 1.5, b: float = 0.75) -> list[float]:
    """Simple BM25 scoring for lexical search fallback."""
    query_terms = set(_tokenize(query))
    if not query_terms:
        return [0.0] * len(documents)
    n_docs = len(documents)
    avgdl = sum(len(_tokenize(d)) for d in documents) / max(1, n_docs)
    scores = []
    for doc in documents:
        doc_terms = _tokenize(doc)
        doc_len = len(doc_terms)
        score = 0.0
        for term in query_terms:
            tf = doc_terms.count(term)
            if tf == 0:
                continue
            df = sum(1 for d in documents if term in _tokenize(d))
            idf = max(0, np.log((n_docs - df + 0.5) / (df + 0.5) + 1))
            numerator = tf * (k1 + 1)
            denominator = tf + k1 * (1 - b + b * doc_len / max(1, avgdl))
            score += idf * numerator / denominator
        scores.append(score)
    return scores


# ── Where-clause matching ────────────────────────────────────────────────


def _compare(value: Any, op: str, expected: Any) -> bool:
    if op == "$eq":
        return value == expected
    if op == "$ne":
        return value != expected
    if op == "$in":
        return value in (expected if isinstance(expected, list) else [])
    if op == "$nin":
        return value not in (expected if isinstance(expected, list) else [])
    if op == "$contains":
        return str(expected) in str(value) if value is not None else False
    if op == "$gt":
        try:
            return float(value) > float(expected)
        except (TypeError, ValueError):
            return False
    if op == "$gte":
        try:
            return float(value) >= float(expected)
        except (TypeError, ValueError):
            return False
    if op == "$lt":
        try:
            return float(value) < float(expected)
        except (TypeError, ValueError):
            return False
    if op == "$lte":
        try:
            return float(value) <= float(expected)
        except (TypeError, ValueError):
            return False
    raise UnsupportedFilterError(f"operator {op!r} not supported by sqlite_vec")


def _validate_where(where: Optional[dict]) -> None:
    if where is None:
        return
    for key, expected in where.items():
        if key in ("$and", "$or"):
            if not isinstance(expected, list):
                raise UnsupportedFilterError(f"{key} value must be a list")
            for clause in expected:
                _validate_where(clause)
            continue
        if key not in _SUPPORTED_OPERATORS and not isinstance(expected, dict):
            continue
        if isinstance(expected, dict):
            for op in expected:
                if op not in _SUPPORTED_OPERATORS:
                    raise UnsupportedFilterError(f"operator {op!r} not supported by sqlite_vec")


def _matches_where(meta: dict, where: Optional[dict]) -> bool:
    if not where:
        return True
    for key, expected in where.items():
        if key == "$and":
            if not all(_matches_where(meta, clause) for clause in (expected or [])):
                return False
            continue
        if key == "$or":
            if not any(_matches_where(meta, clause) for clause in (expected or [])):
                return False
            continue
        if key not in meta and key not in _SUPPORTED_OPERATORS:
            return False
        if key in _SUPPORTED_OPERATORS:
            continue
        actual = meta.get(key)
        if isinstance(expected, dict):
            for op, operand in expected.items():
                if op not in _SUPPORTED_OPERATORS:
                    raise UnsupportedFilterError(f"operator {op!r} not supported")
                if not _compare(actual, op, operand):
                    return False
        elif actual != expected:
            return False
    return True


def _matches_where_document(document: str, where_document: Optional[dict]) -> bool:
    if not where_document:
        return True
    if not isinstance(where_document, dict):
        return False
    for key, value in where_document.items():
        if key == "$contains":
            if str(value) not in document:
                return False
            continue
        if key == "$and":
            if not all(_matches_where_document(document, clause) for clause in value or []):
                return False
            continue
        if key == "$or":
            if not any(_matches_where_document(document, clause) for clause in value or []):
                return False
            continue
        raise UnsupportedFilterError(f"where_document operator {key!r} not supported")
    return True


def _validate_write_batch(
    *,
    documents: list[str],
    ids: list[str],
    metadatas: Optional[list[dict]],
    embeddings: Optional[list[list[float]]],
) -> None:
    n = len(ids)
    if len(documents) != n:
        raise ValueError(f"documents length {len(documents)} does not match ids length {n}")
    if metadatas is not None and len(metadatas) != n:
        raise ValueError(f"metadatas length {len(metadatas)} does not match ids length {n}")
    if embeddings is not None and len(embeddings) != n:
        raise ValueError(f"embeddings length {len(embeddings)} does not match ids length {n}")


# ── Handle ────────────────────────────────────────────────────────────────


class _SQLiteVecHandle:
    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock):
        self.conn = conn
        self.lock = lock
        self.closed = False


# ── Collection ─────────────────────────────────────────────────────────────


class SQLiteVecCollection(BaseCollection):
    """Per-collection operations backed by sqlite-vec."""

    def __init__(self, handle: _SQLiteVecHandle, collection_name: str):
        if not re.match(r"^[a-zA-Z0-9_-]+$", collection_name):
            raise ValueError(f"Invalid collection name: {collection_name!r}")
        self._handle = handle
        self._collection_name = collection_name
        self._closed = False

    def _ensure_open(self) -> None:
        if self._closed or self._handle.closed:
            raise BackendClosedError("SQLiteVecCollection has been closed")

    @contextlib.contextmanager
    def _cursor(self):
        with self._handle.lock:
            self._ensure_open()
            cur = self._handle.conn.cursor()
            try:
                yield cur
            except Exception:
                self._handle.conn.rollback()
                raise
            else:
                self._handle.conn.commit()
            finally:
                cur.close()

    def _ensure_vec_table(self, cur, dim: int) -> None:
        """Create vec0 virtual table on first write with correct dimension.

        Each collection gets its own vec0 table to support different dimensions
        and prevent cross-collection vector corruption.
        """
        table_name = f"doc_vec_{self._collection_name}"
        meta_key = f"vec0_table_created:{self._collection_name}"
        row = cur.execute("SELECT value FROM meta WHERE key = ?", (meta_key,)).fetchone()
        if row and row[0] == "1":
            return
        cur.execute(f"DROP TABLE IF EXISTS {table_name}")
        cur.execute(
            f"CREATE VIRTUAL TABLE {table_name} USING vec0("
            f"  embedding float[{dim}] distance_metric=cosine"
            f")"
        )
        cur.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, '1')", (meta_key,))

    def _vec_table(self) -> str:
        """Collection-specific vec0 table name."""
        return f"doc_vec_{self._collection_name}"

    def _collection_id(self, cur) -> int:
        row = cur.execute(
            "SELECT id FROM collections WHERE name = ?",
            (self._collection_name,),
        ).fetchone()
        if row is None:
            raise CollectionNotInitializedError(self._collection_name)
        return int(row[0])

    def _collection_dimension(self, cur, collection_id: int) -> Optional[int]:
        row = cur.execute(
            "SELECT dimension FROM collections WHERE id = ?",
            (collection_id,),
        ).fetchone()
        if row is None or row[0] is None:
            return None
        return int(row[0])

    def _ensure_collection_dimension(self, cur, collection_id: int, dims: list[int]) -> None:
        distinct = {int(dim) for dim in dims}
        if not distinct:
            return
        if len(distinct) > 1:
            raise DimensionMismatchError(
                f"sqlite_vec collection {self._collection_name!r} cannot mix "
                f"embedding dimensions {sorted(distinct)}"
            )
        dim = distinct.pop()
        stored = self._collection_dimension(cur, collection_id)
        if stored is None:
            cur.execute(
                "UPDATE collections SET dimension = ? WHERE id = ?",
                (dim, collection_id),
            )
        elif stored != dim:
            raise DimensionMismatchError(
                f"sqlite_vec collection {self._collection_name!r} expects "
                f"embedding dimension {stored}, got {dim}"
            )

    def _fts_available(self, cur) -> bool:
        row = cur.execute("SELECT value FROM meta WHERE key = 'fts5_available'").fetchone()
        return bool(row and row[0] == "1")

    def _embedder_meta_key(self) -> str:
        return f"embedder_model:{self._collection_name}"

    def get_stored_embedder_identity(self):
        with self._cursor() as cur:
            try:
                cid = self._collection_id(cur)
            except CollectionNotInitializedError:
                return None
            row = cur.execute(
                "SELECT value FROM meta WHERE key = ?",
                (self._embedder_meta_key(),),
            ).fetchone()
            if not row or not row[0]:
                return None
            dim = self._collection_dimension(cur, cid) or 0
            return EmbedderIdentity(model_name=str(row[0]), dimension=int(dim))

    def set_embedder_identity(self, identity) -> None:
        if not identity or not identity.model_name:
            return
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (self._embedder_meta_key(), str(identity.model_name)),
            )

    def _replace_fts(self, cur, collection_id: int, doc_id: str, document: str) -> None:
        if not self._fts_available(cur):
            return
        cur.execute(
            "DELETE FROM docs_fts WHERE collection_id = ? AND doc_id = ?",
            (collection_id, doc_id),
        )
        cur.execute(
            "INSERT INTO docs_fts(collection_id, doc_id, document) VALUES (?, ?, ?)",
            (collection_id, doc_id, document),
        )

    # ── CRUD ──────────────────────────────────────────────────────────────

    def add(self, *, documents, ids, metadatas=None, embeddings=None):
        _validate_write_batch(
            documents=documents, ids=ids, metadatas=metadatas, embeddings=embeddings
        )
        if not ids:
            return
        if embeddings is None:
            raise ValueError("sqlite_vec requires explicit embeddings")
        metadatas = metadatas or [{} for _ in ids]
        now = _utcnow()
        with self._cursor() as cur:
            collection_id = self._collection_id(cur)
            prepared = []
            for doc_id, doc, meta, emb in zip(ids, documents, metadatas, embeddings):
                arr = _as_vector_array(emb)
                blob = _encode_vector(emb)
                prepared.append((doc_id, doc, meta, blob, int(arr.size)))
            self._ensure_collection_dimension(cur, collection_id, [item[4] for item in prepared])
            self._ensure_vec_table(cur, prepared[0][4])
            for doc_id, doc, meta, emb_blob, dim in prepared:
                cur.execute(
                    """
                    INSERT INTO documents
                        (collection_id, id, document, metadata_json, embedding, dim, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        collection_id,
                        doc_id,
                        doc,
                        _json_dumps(meta),
                        emb_blob,
                        dim,
                        now,
                        now,
                    ),
                )
                # Use the implicit rowid from documents as vec0 rowid
                rowid = cur.lastrowid
                cur.execute(
                    "INSERT OR REPLACE INTO "
                    + self._vec_table()
                    + " (rowid, embedding) VALUES (?, ?)",
                    (rowid, emb_blob),
                )
                self._replace_fts(cur, collection_id, doc_id, doc)

    def upsert(self, *, documents, ids, metadatas=None, embeddings=None):
        _validate_write_batch(
            documents=documents, ids=ids, metadatas=metadatas, embeddings=embeddings
        )
        if not ids:
            return
        if embeddings is None:
            raise ValueError("sqlite_vec requires explicit embeddings")
        metadatas = metadatas or [{} for _ in ids]
        now = _utcnow()
        with self._cursor() as cur:
            collection_id = self._collection_id(cur)
            prepared = []
            for doc_id, doc, meta, emb in zip(ids, documents, metadatas, embeddings):
                arr = _as_vector_array(emb)
                blob = _encode_vector(emb)
                prepared.append((doc_id, doc, meta, blob, int(arr.size)))
            self._ensure_collection_dimension(cur, collection_id, [item[4] for item in prepared])
            self._ensure_vec_table(cur, prepared[0][4])
            for doc_id, doc, meta, emb_blob, dim in prepared:
                cur.execute(
                    """
                    INSERT INTO documents
                        (collection_id, id, document, metadata_json, embedding, dim, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(collection_id, id) DO UPDATE SET
                        document = excluded.document,
                        metadata_json = excluded.metadata_json,
                        embedding = excluded.embedding,
                        dim = excluded.dim,
                        updated_at = excluded.updated_at
                    """,
                    (
                        collection_id,
                        doc_id,
                        doc,
                        _json_dumps(meta),
                        emb_blob,
                        dim,
                        now,
                        now,
                    ),
                )
                # Use documents.rowid as vec0 rowid
                row = cur.execute(
                    "SELECT rowid FROM documents WHERE collection_id = ? AND id = ?",
                    (collection_id, doc_id),
                ).fetchone()
                rowid = row[0] if row else None
                if rowid is not None:
                    cur.execute(
                        "DELETE FROM " + self._vec_table() + " WHERE rowid = ?",
                        (rowid,),
                    )
                    cur.execute(
                        "INSERT INTO " + self._vec_table() + " (rowid, embedding) VALUES (?, ?)",
                        (rowid, emb_blob),
                    )
                self._replace_fts(cur, collection_id, doc_id, doc)

    def _lexical_search_fts(self, cur, *, query: str, n_results: int, where: Optional[dict]):
        """Try FTS5 lexical search. Returns None if FTS unavailable."""
        if not self._fts_available(cur):
            return None
        collection_id = self._collection_id(cur)
        fts_query = " OR ".join(_tokenize(query))
        if not fts_query:
            return None
        try:
            rows = cur.execute(
                """
                SELECT d.id, d.document, d.metadata_json,
                       rank AS score
                FROM docs_fts f
                JOIN documents d ON d.collection_id = f.collection_id AND d.id = f.doc_id
                WHERE f.collection_id = ? AND docs_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (collection_id, fts_query, n_results * 3),
            ).fetchall()
        except sqlite3.OperationalError:
            return None
        hits = []
        for doc_id, doc, meta_json, score in rows:
            meta = _json_loads(meta_json)
            if not _matches_where(meta, where):
                continue
            hits.append(
                LexicalHit(
                    id=doc_id,
                    document=doc or "",
                    metadata=meta,
                    score=float(score) if score else 0.0,
                )
            )
            if len(hits) >= n_results:
                break
        return hits if hits else []

    def _rows(self, cur, *, where=None, where_document=None, limit=None, offset=None):
        """Retrieve all matching rows. Used by get() and lexical fallback."""
        _validate_where(where)
        _validate_where(where_document)
        collection_id = self._collection_id(cur)
        sql = (
            "SELECT id, document, metadata_json, embedding\n"
            "FROM documents\n"
            "WHERE collection_id = ?\n"
            "ORDER BY rowid"
        )
        params = [collection_id]
        if where is None and where_document is None and (limit is not None or offset):
            if limit is not None:
                sql += "\nLIMIT ?"
                params.append(int(limit))
            elif offset:
                sql += "\nLIMIT -1"
            if offset:
                sql += "\nOFFSET ?"
                params.append(int(offset))
        rows = cur.execute(sql, params).fetchall()
        out = []
        for doc_id, doc, meta_json, emb_blob in rows:
            meta = _json_loads(meta_json)
            if not _matches_where(meta, where):
                continue
            if not _matches_where_document(doc or "", where_document):
                continue
            out.append(
                {
                    "id": doc_id,
                    "document": doc or "",
                    "metadata": meta,
                    "embedding": emb_blob,
                }
            )
        return out

    def query(
        self,
        *,
        query_texts=None,
        query_embeddings=None,
        n_results=10,
        where=None,
        where_document=None,
        include=None,
    ) -> QueryResult:
        """ANN search via sqlite-vec vec0 virtual table."""
        if query_texts is not None:
            raise ValueError(
                "sqlite_vec requires query_embeddings; use palace.get_collection wrapper"
            )
        if query_embeddings is None:
            raise ValueError("query requires query_embeddings")
        if not query_embeddings:
            raise ValueError("query input must be a non-empty list")

        spec = _IncludeSpec.resolve(include, default_distances=True)
        outer_ids: list[list[str]] = []
        outer_docs: list[list[str]] = []
        outer_metas: list[list[dict]] = []
        outer_dists: list[list[float]] = []
        outer_embeds: list[list[list[float]]] = []

        with self._cursor() as cur:
            collection_id = self._collection_id(cur)
            expected_dim = self._collection_dimension(cur, collection_id)

            for query_vector in query_embeddings:
                q = _as_vector_array(query_vector)
                if expected_dim is not None and int(q.size) != expected_dim:
                    raise DimensionMismatchError(
                        f"sqlite_vec collection {self._collection_name!r} expects "
                        f"embedding dimension {expected_dim}, got {int(q.size)}"
                    )
                q_blob = _encode_vector(query_vector)

                # Get total document count for window expansion upper bound
                total_count = self.count()

                # Window expansion loop: grow k until we have n_results matches
                # or exhaust the collection (fixes filtered ANN misses)
                k = max(n_results * 3, 10)
                ids_out: list[str] = []
                docs_out: list[str] = []
                metas_out: list[dict] = []
                dists_out: list[float] = []
                embs_out: list[list[float]] = []
                max_attempts = 20  # safety limit against infinite loop

                for _attempt in range(max_attempts):
                    # Fetch candidates with JOIN (one query per attempt)
                    try:
                        vec_rows = cur.execute(
                            f"SELECT v.rowid, v.distance, d.id, d.document, "
                            f"       d.metadata_json, d.embedding "
                            f"FROM {self._vec_table()} v "
                            f"JOIN documents d ON d.rowid = v.rowid AND d.collection_id = ? "
                            f"WHERE v.embedding MATCH ? AND k = ? ORDER BY v.distance",
                            (collection_id, q_blob, k),
                        ).fetchall()
                    except sqlite3.OperationalError as e:
                        if "no such table" in str(e) or "no such module" in str(e):
                            # vec0 table not created yet (no documents written)
                            ids_out.clear()
                            docs_out.clear()
                            metas_out.clear()
                            dists_out.clear()
                            embs_out.clear()
                            break
                        raise

                    ids_out.clear()
                    docs_out.clear()
                    metas_out.clear()
                    dists_out.clear()
                    embs_out.clear()

                    for rowid, distance, doc_id, doc_text, meta_json, emb_blob in vec_rows:
                        meta = _json_loads(meta_json)
                        if not _matches_where(meta, where):
                            continue
                        if not _matches_where_document(doc_text or "", where_document):
                            continue

                        ids_out.append(doc_id)
                        if spec.documents:
                            docs_out.append(doc_text or "")
                        if spec.metadatas:
                            metas_out.append(meta)
                        if spec.distances:
                            dists_out.append(float(distance))
                        if spec.embeddings:
                            embs_out.append(_decode_vector(emb_blob))

                        if len(ids_out) >= n_results:
                            break

                    if len(ids_out) >= n_results:
                        break

                    # Expand window: add n_results * 2 more candidates
                    k = k + n_results * 2
                    # Upper bound: don't expand beyond collection size
                    if total_count > 0 and k >= total_count:
                        if k >= total_count + n_results * 2:
                            break
                        k = total_count

                outer_ids.append(ids_out)
                outer_docs.append(docs_out)
                outer_metas.append(metas_out)
                outer_dists.append(dists_out)
                if spec.embeddings:
                    outer_embeds.append(embs_out)

        return QueryResult(
            ids=outer_ids,
            documents=outer_docs,
            metadatas=outer_metas,
            distances=outer_dists,
            embeddings=outer_embeds if spec.embeddings else None,
        )

    def get(
        self,
        *,
        ids=None,
        where=None,
        where_document=None,
        limit=None,
        offset=None,
        include=None,
    ) -> GetResult:
        spec = _IncludeSpec.resolve(include, default_distances=False)
        push_page = (
            ids is None
            and where is None
            and where_document is None
            and (limit is None or limit >= 0)
            and (offset is None or offset >= 0)
            and (limit is not None or offset)
        )
        with self._cursor() as cur:
            if push_page:
                rows = self._rows(cur, limit=limit, offset=offset)
            else:
                rows = self._rows(cur, where=where, where_document=where_document)
        if not push_page:
            if ids is not None:
                by_id = {row["id"]: row for row in rows}
                rows = [by_id[doc_id] for doc_id in ids if doc_id in by_id]
            if offset:
                rows = rows[offset:]
            if limit is not None:
                rows = rows[:limit]
        return GetResult(
            ids=[row["id"] for row in rows],
            documents=[row["document"] for row in rows] if spec.documents else [],
            metadatas=[row["metadata"] for row in rows] if spec.metadatas else [],
            embeddings=(
                [_decode_vector(row["embedding"]) for row in rows] if spec.embeddings else None
            ),
        )

    def delete(self, *, ids=None, where=None):
        with self._cursor() as cur:
            collection_id = self._collection_id(cur)
            if ids is None:
                rows = self._rows(cur, where=where)
                ids = [row["id"] for row in rows]
            for doc_id in ids or []:
                # Find the rowid for doc_vec cleanup
                row = cur.execute(
                    "SELECT rowid FROM documents WHERE collection_id = ? AND id = ?",
                    (collection_id, doc_id),
                ).fetchone()
                cur.execute(
                    "DELETE FROM documents WHERE collection_id = ? AND id = ?",
                    (collection_id, doc_id),
                )
                if row:
                    cur.execute(
                        "DELETE FROM " + self._vec_table() + " WHERE rowid = ?",
                        (row[0],),
                    )
                if self._fts_available(cur):
                    cur.execute(
                        "DELETE FROM docs_fts WHERE collection_id = ? AND doc_id = ?",
                        (collection_id, doc_id),
                    )

    def count(self) -> int:
        with self._cursor() as cur:
            collection_id = self._collection_id(cur)
            row = cur.execute(
                "SELECT COUNT(*) FROM documents WHERE collection_id = ?",
                (collection_id,),
            ).fetchone()
            return int(row[0]) if row else 0

    def lexical_search(self, *, query: str, n_results: int = 10, where: Optional[dict] = None):
        _validate_where(where)
        with self._cursor() as cur:
            hits = self._lexical_search_fts(cur, query=query, n_results=n_results, where=where)
            if hits is not None:
                return LexicalResult(hits=hits)
            rows = self._rows(cur, where=where)
        scores = _bm25_scores(query, [row["document"] for row in rows])
        scored = [
            LexicalHit(
                id=row["id"],
                document=row["document"],
                metadata=row["metadata"],
                score=score,
            )
            for row, score in zip(rows, scores)
            if score > 0
        ]
        scored.sort(key=lambda hit: hit.score, reverse=True)
        return LexicalResult(hits=scored[:n_results])

    def close(self) -> None:
        self._closed = True

    def health(self) -> HealthStatus:
        try:
            with self._cursor() as cur:
                cur.execute("SELECT 1 FROM documents LIMIT 1")
                return HealthStatus.healthy()
        except Exception as exc:
            return HealthStatus.unhealthy(str(exc))

    def maintenance_state(self) -> dict:
        try:
            with self._cursor() as cur:
                rows = cur.execute(
                    "SELECT page_count, freelist_count FROM pragma_page_count, pragma_freelist_count"
                ).fetchone()
                if rows:
                    return {"page_count": rows[0], "freelist_count": rows[1]}
        except Exception:
            pass
        return {}

    def run_maintenance(self, kind: str) -> MaintenanceResult:
        if kind == "analyze":
            with self._cursor() as cur:
                cur.execute("ANALYZE")
            return MaintenanceResult(kind="analyze", status="ran")
        if kind == "compact":
            before = self.maintenance_state()
            with self._handle.lock:
                self._ensure_open()
                conn = self._handle.conn
                prev = conn.isolation_level
                try:
                    conn.commit()
                    conn.isolation_level = None
                    conn.execute("VACUUM")
                finally:
                    conn.isolation_level = prev
            after = self.maintenance_state()
            reclaimed = max(0, before.get("page_count", 0) - after.get("page_count", 0))
            return MaintenanceResult(
                kind="compact",
                status="ran",
                stats={
                    "pages_before": before.get("page_count", 0),
                    "pages_after": after.get("page_count", 0),
                    "pages_reclaimed": reclaimed,
                },
            )
        raise ValueError(f"Unknown maintenance kind: {kind}")


# ── Backend ────────────────────────────────────────────────────────────────


class SQLiteVecBackend(BaseBackend):
    """Fast ANN backend via sqlite-vec.

    Uses vec0 virtual table for O(log n) approximate nearest-neighbour search.
    WAL mode, mmap_size=0 (safe on WSL). EmbeddingGemma 384-dim cosine.
    """

    name = "sqlite_vec"
    capabilities = frozenset(
        {
            "requires_explicit_embeddings",
            "supports_embeddings_in",
            "supports_embeddings_passthrough",
            "supports_embeddings_out",
            "supports_metadata_filters",
            "supports_lexical_search",
            "local_mode",
        }
    )
    maintenance_kinds = frozenset({"analyze", "compact"})

    def __init__(self):
        self._clients: dict[str, _SQLiteVecHandle] = {}
        self._clients_lock = threading.RLock()
        self._closed = False

    @classmethod
    def detect(cls, path: str) -> bool:
        filepath = os.path.join(path, _DB_FILENAME)
        if not os.path.isfile(filepath):
            return False
        try:
            with open(filepath, "rb") as f:
                return f.read(16) == b"SQLite format 3\x00"
        except (OSError, IOError):
            return False

    @staticmethod
    def _db_path(palace_path: str) -> str:
        return os.path.join(palace_path, _DB_FILENAME)

    def _connect(self, palace_path: str, create: bool):
        if self._closed:
            raise BackendClosedError("SQLiteVecBackend has been closed")
        db_path = self._db_path(palace_path)
        if not create and not os.path.isfile(db_path):
            raise PalaceNotFoundError(db_path)
        if create:
            os.makedirs(palace_path, exist_ok=True)
            try:
                os.chmod(palace_path, 0o700)
            except (OSError, NotImplementedError):
                pass

        with self._clients_lock:
            if self._closed:
                raise BackendClosedError("SQLiteVecBackend has been closed")
            cached = self._clients.get(palace_path)
            if cached is not None and not cached.closed:
                return cached
            conn = sqlite3.connect(db_path, check_same_thread=False)
            try:
                conn.row_factory = sqlite3.Row
                lock = threading.RLock()
                handle = _SQLiteVecHandle(conn, lock)
                with handle.lock:
                    self._init_schema(conn)
            except BaseException:
                conn.close()
                raise
            self._clients[palace_path] = handle
            return handle

    def _init_schema(self, conn: sqlite3.Connection) -> None:
        # WSL-safe pragmas
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA mmap_size=0")

        conn.executescript("""
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS collections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                dimension INTEGER,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS documents (
                collection_id INTEGER NOT NULL,
                id TEXT NOT NULL,
                document TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                embedding BLOB NOT NULL,
                dim INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (collection_id, id),
                FOREIGN KEY(collection_id) REFERENCES collections(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_documents_collection
                ON documents(collection_id);
        """)

        # Add dimension column to legacy collections that lack it
        columns = {row[1] for row in conn.execute("PRAGMA table_info(collections)").fetchall()}
        if "dimension" not in columns:
            conn.execute("ALTER TABLE collections ADD COLUMN dimension INTEGER")

        # sqlite-vec extension — load the module but defer vec0 table creation
        # to first write (dimension is unknown until then)
        vec_mod = _ensure_sqlite_vec()
        if vec_mod:
            conn.enable_load_extension(True)
            try:
                vec_mod.load(conn)
            finally:
                conn.enable_load_extension(False)
            # Store in meta that vec extension is available
            conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('sqlite_vec_available', '1')"
            )

        # FTS5 for lexical search
        try:
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts "
                "USING fts5(collection_id UNINDEXED, doc_id UNINDEXED, document)"
            )
            conn.execute(
                "INSERT INTO meta(key, value) VALUES ('fts5_available', '1') "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
            )
        except sqlite3.OperationalError:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES ('fts5_available', '0') "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
            )

    def get_collection(
        self,
        *,
        palace: PalaceRef,
        collection_name: str,
        create: bool = False,
        options: Optional[dict] = None,
    ):
        self.require_namespace_support(palace)
        if not palace.local_path:
            raise ValueError("sqlite_vec backend requires a local palace")
        handle = self._connect(palace.local_path, create=create)
        collection = SQLiteVecCollection(handle, collection_name)
        if create:
            with collection._cursor() as cur:
                cur.execute(
                    "INSERT OR IGNORE INTO collections(name, created_at) VALUES (?, ?)",
                    (collection_name, _utcnow()),
                )
        return collection

    def list_collection_names(self, palace_path: str) -> list[str]:
        if not os.path.exists(self._db_path(palace_path)):
            return []
        try:
            handle = self._connect(palace_path, create=False)
        except PalaceNotFoundError:
            return []
        with handle.lock:
            rows = handle.conn.execute("SELECT name FROM collections ORDER BY name").fetchall()
        return [row[0] for row in rows]

    def create_collection(self, palace_path: str, collection_name: str) -> SQLiteVecCollection:
        """Create a new collection and return it."""
        return self.get_collection(
            palace=PalaceRef(id=palace_path, local_path=palace_path),
            collection_name=collection_name,
            create=True,
        )

    def get_or_create_collection(
        self, palace_path: str, collection_name: str
    ) -> SQLiteVecCollection:
        """Get an existing collection or create a new one."""
        return self.get_collection(
            palace=PalaceRef(id=palace_path, local_path=palace_path),
            collection_name=collection_name,
            create=True,
        )

    def delete_collection(self, palace: Union[str, PalaceRef], collection_name: str) -> None:
        """Delete a collection and all its data.

        Accepts ``Union[str, PalaceRef]`` for ``palace``, normalizing to
        ``str(path)`` before use — same pattern as ``__init__``.
        """
        if isinstance(palace, PalaceRef):
            palace_path = palace.local_path
        else:
            palace_path = palace
        if not palace_path:
            raise ValueError("sqlite_vec backend requires a local palace")
        handle = self._connect(palace_path, create=False)
        with handle.lock:
            cur = handle.conn.cursor()
            row = cur.execute(
                "SELECT id FROM collections WHERE name = ?", (collection_name,)
            ).fetchone()
            if row is None:
                return
            cid = int(row[0])
            table_name = f"doc_vec_{collection_name}"

            # Problem A: Check vec0 table exists before DELETE
            vec_exists = (
                cur.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (table_name,),
                ).fetchone()
                is not None
            )
            if vec_exists:
                # Remove vectors first, then drop table entirely (Problem C)
                cur.execute(
                    "DELETE FROM " + table_name + " WHERE rowid IN "
                    "(SELECT rowid FROM documents WHERE collection_id = ?)",
                    (cid,),
                )
                cur.execute("DROP TABLE IF EXISTS " + table_name)

            cur.execute("DELETE FROM documents WHERE collection_id = ?", (cid,))
            if handle.conn.execute(
                "SELECT value FROM meta WHERE key = 'fts5_available'"
            ).fetchone() == ("1",):
                cur.execute("DELETE FROM docs_fts WHERE collection_id = ?", (cid,))
            cur.execute("DELETE FROM collections WHERE id = ?", (cid,))

            # Problem B: Clean up collection-scoped metadata
            cur.execute(
                "DELETE FROM meta WHERE key = ?",
                (f"vec0_table_created:{collection_name}",),
            )
            cur.execute(
                "DELETE FROM meta WHERE key = ?",
                (f"embedder_model:{collection_name}",),
            )
            handle.conn.commit()

    def close(self) -> None:
        with self._clients_lock:
            self._closed = True
            for handle in self._clients.values():
                handle.closed = True
                try:
                    handle.conn.close()
                except Exception:
                    pass
            self._clients.clear()

    def health(self, palace: Optional[PalaceRef] = None) -> HealthStatus:
        if palace is None or not palace.local_path:
            return HealthStatus.unhealthy("No local path for sqlite_vec backend")
        try:
            handle = self._connect(palace.local_path, create=False)
            handle.conn.execute("SELECT 1")
            return HealthStatus.healthy()
        except Exception as exc:
            return HealthStatus.unhealthy(str(exc))

    distance_metric: ClassVar[str] = "cosine"
