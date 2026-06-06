"""SQLite exact-vector backend for MemPalace.

This backend is intentionally simple and local-first. It is a correctness
backend, not a high-throughput ANN backend: vectors are stored as float32
blobs and query uses exact cosine distance over the matching collection.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Optional

import numpy as np

from .base import (
    BackendClosedError,
    BaseBackend,
    BaseCollection,
    CollectionNotInitializedError,
    DimensionMismatchError,
    GetResult,
    HealthStatus,
    LexicalHit,
    LexicalResult,
    PalaceNotFoundError,
    PalaceRef,
    QueryResult,
    UnsupportedFilterError,
    _IncludeSpec,
)

logger = logging.getLogger(__name__)

_DB_FILENAME = "sqlite_exact.sqlite3"
_TOKEN_RE = re.compile(r"\w{2,}", re.UNICODE)
_SUPPORTED_OPERATORS = frozenset(
    {"$eq", "$ne", "$in", "$nin", "$and", "$or", "$contains", "$gt", "$gte", "$lt", "$lte"}
)


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


def _encode_vector(vector: list[float]) -> bytes:
    return _as_vector_array(vector).tobytes()


def _as_vector_array(vector: list[float]) -> np.ndarray:
    arr = np.asarray(vector, dtype=np.float32)
    if arr.ndim != 1 or arr.size == 0:
        raise ValueError("embedding must be a non-empty 1D vector")
    return arr


def _decode_vector(blob: bytes | None) -> list[float]:
    if not blob:
        return []
    return np.frombuffer(blob, dtype=np.float32).astype(float).tolist()


def _decode_array(blob: bytes | None) -> Optional[np.ndarray]:
    if not blob:
        return None
    arr = np.frombuffer(blob, dtype=np.float32)
    if arr.size == 0:
        return None
    return arr


def _tokenize(text: str) -> list[str]:
    if not text:
        return []
    return _TOKEN_RE.findall(text.lower())


def _bm25_scores(query: str, documents: list[str], k1: float = 1.5, b: float = 0.75) -> list[float]:
    query_terms = set(_tokenize(query))
    n_docs = len(documents)
    if not query_terms or n_docs == 0:
        return [0.0] * n_docs

    tokenized = [_tokenize(d) for d in documents]
    doc_lens = [len(toks) for toks in tokenized]
    if not any(doc_lens):
        return [0.0] * n_docs
    avgdl = sum(doc_lens) / n_docs or 1.0

    df = {term: 0 for term in query_terms}
    for toks in tokenized:
        for term in set(toks) & query_terms:
            df[term] += 1

    idf = {term: np.log((n_docs - df[term] + 0.5) / (df[term] + 0.5) + 1.0) for term in query_terms}

    scores = []
    for toks, dl in zip(tokenized, doc_lens):
        if dl == 0:
            scores.append(0.0)
            continue
        tf: dict[str, int] = {}
        for token in toks:
            if token in query_terms:
                tf[token] = tf.get(token, 0) + 1
        score = 0.0
        for term, freq in tf.items():
            num = freq * (k1 + 1)
            den = freq + k1 * (1 - b + b * dl / avgdl)
            score += float(idf[term]) * num / den
        scores.append(score)
    return scores


def _validate_where(where: Optional[dict]) -> None:
    if not where:
        return
    stack = [where]
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        for key, value in node.items():
            if key.startswith("$") and key not in _SUPPORTED_OPERATORS:
                raise UnsupportedFilterError(f"operator {key!r} not supported by sqlite_exact")
            if isinstance(value, dict):
                stack.append(value)
            elif isinstance(value, list):
                stack.extend(item for item in value if isinstance(item, dict))


def _coerce_comparable(value: Any):
    if isinstance(value, bool):
        return int(value)
    return value


def _compare(actual: Any, op: str, expected: Any) -> bool:
    actual = _coerce_comparable(actual)
    expected = _coerce_comparable(expected)
    if op == "$eq":
        return actual == expected
    if op == "$ne":
        return actual != expected
    if op == "$in":
        return actual in (expected or [])
    if op == "$nin":
        return actual not in (expected or [])
    if op == "$contains":
        return str(expected) in str(actual or "")
    try:
        if op == "$gt":
            return actual > expected
        if op == "$gte":
            return actual >= expected
        if op == "$lt":
            return actual < expected
        if op == "$lte":
            return actual <= expected
    except TypeError:
        return False
    raise UnsupportedFilterError(f"operator {op!r} not supported by sqlite_exact")


def _matches_where(meta: dict, where: Optional[dict]) -> bool:
    if not where:
        return True
    if not isinstance(where, dict):
        return False
    for key, expected in where.items():
        if key == "$and":
            if not all(_matches_where(meta, clause) for clause in expected or []):
                return False
            continue
        if key == "$or":
            if not any(_matches_where(meta, clause) for clause in expected or []):
                return False
            continue
        if key.startswith("$"):
            raise UnsupportedFilterError(f"operator {key!r} not supported by sqlite_exact")
        actual = meta.get(key)
        if isinstance(expected, dict):
            for op, operand in expected.items():
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


class _SQLiteExactHandle:
    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock):
        self.conn = conn
        self.lock = lock
        self.closed = False


class SQLiteExactCollection(BaseCollection):
    def __init__(self, handle: _SQLiteExactHandle, collection_name: str):
        self._handle = handle
        self._collection_name = collection_name
        self._closed = False

    def _ensure_open(self) -> None:
        if self._closed or self._handle.closed:
            raise BackendClosedError("SQLiteExactCollection has been closed")

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
                f"sqlite_exact collection {self._collection_name!r} cannot mix "
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
                f"sqlite_exact collection {self._collection_name!r} expects "
                f"embedding dimension {stored}, got {dim}"
            )

    def _fts_available(self, cur) -> bool:
        row = cur.execute("SELECT value FROM meta WHERE key = 'fts5_available'").fetchone()
        return bool(row and row[0] == "1")

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

    def add(self, *, documents, ids, metadatas=None, embeddings=None):
        _validate_write_batch(
            documents=documents,
            ids=ids,
            metadatas=metadatas,
            embeddings=embeddings,
        )
        if embeddings is None:
            raise ValueError("sqlite_exact requires explicit embeddings")
        metadatas = metadatas or [{} for _ in ids]
        now = _utcnow()
        with self._cursor() as cur:
            collection_id = self._collection_id(cur)
            prepared = []
            for doc_id, doc, meta, emb in zip(ids, documents, metadatas, embeddings):
                arr = _as_vector_array(emb)
                prepared.append((doc_id, doc, meta, arr.tobytes(), int(arr.size)))
            self._ensure_collection_dimension(cur, collection_id, [item[4] for item in prepared])
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
                self._replace_fts(cur, collection_id, doc_id, doc)

    def upsert(self, *, documents, ids, metadatas=None, embeddings=None):
        _validate_write_batch(
            documents=documents,
            ids=ids,
            metadatas=metadatas,
            embeddings=embeddings,
        )
        if embeddings is None:
            raise ValueError("sqlite_exact requires explicit embeddings")
        metadatas = metadatas or [{} for _ in ids]
        now = _utcnow()
        with self._cursor() as cur:
            collection_id = self._collection_id(cur)
            prepared = []
            for doc_id, doc, meta, emb in zip(ids, documents, metadatas, embeddings):
                arr = _as_vector_array(emb)
                prepared.append((doc_id, doc, meta, arr.tobytes(), int(arr.size)))
            self._ensure_collection_dimension(cur, collection_id, [item[4] for item in prepared])
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
                self._replace_fts(cur, collection_id, doc_id, doc)

    def update(self, *, ids, documents=None, metadatas=None, embeddings=None):
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
        with self._cursor() as cur:
            collection_id = self._collection_id(cur)
            updates = []
            for idx, doc_id in enumerate(ids):
                row = cur.execute(
                    """
                    SELECT document, metadata_json, embedding, dim
                    FROM documents
                    WHERE collection_id = ? AND id = ?
                    """,
                    (collection_id, doc_id),
                ).fetchone()
                if row is None:
                    continue
                doc = documents[idx] if documents is not None else row[0]
                meta = _json_loads(row[1])
                if metadatas is not None:
                    meta.update(metadatas[idx] or {})
                if embeddings is not None:
                    arr = _as_vector_array(embeddings[idx])
                    emb_blob = arr.tobytes()
                    dim = int(arr.size)
                else:
                    emb_blob = row[2]
                    dim = row[3]
                updates.append((doc_id, doc, meta, emb_blob, dim))
            if embeddings is not None:
                self._ensure_collection_dimension(cur, collection_id, [item[4] for item in updates])
            for doc_id, doc, meta, emb_blob, dim in updates:
                cur.execute(
                    """
                    UPDATE documents
                    SET document = ?, metadata_json = ?, embedding = ?, dim = ?, updated_at = ?
                    WHERE collection_id = ? AND id = ?
                    """,
                    (doc, _json_dumps(meta), emb_blob, dim, _utcnow(), collection_id, doc_id),
                )
                self._replace_fts(cur, collection_id, doc_id, doc)

    def _rows(self, cur, *, where=None, where_document=None) -> list[dict]:
        _validate_where(where)
        _validate_where(where_document)
        collection_id = self._collection_id(cur)
        rows = cur.execute(
            """
            SELECT id, document, metadata_json, embedding
            FROM documents
            WHERE collection_id = ?
            ORDER BY rowid
            """,
            (collection_id,),
        ).fetchall()
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
        if query_texts is not None:
            raise ValueError(
                "sqlite_exact requires query_embeddings; use palace.get_collection wrapper"
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
            rows = self._rows(cur, where=where, where_document=where_document)
            row_vectors = [(row, _decode_array(row["embedding"])) for row in rows]

        for query_vector in query_embeddings:
            q = _as_vector_array(query_vector)
            if expected_dim is not None and int(q.size) != expected_dim:
                raise DimensionMismatchError(
                    f"sqlite_exact collection {self._collection_name!r} expects "
                    f"embedding dimension {expected_dim}, got {int(q.size)}"
                )
            q_norm = float(np.linalg.norm(q))
            scored = []
            for row, vec in row_vectors:
                if vec is None or vec.size != q.size:
                    continue
                denom = q_norm * float(np.linalg.norm(vec))
                cos = 0.0 if denom <= 0 else float(np.dot(q, vec) / denom)
                distance = 1.0 - max(-1.0, min(1.0, cos))
                scored.append((distance, row, vec))
            scored.sort(key=lambda item: item[0])
            top = scored[:n_results]

            outer_ids.append([row["id"] for _, row, _ in top])
            outer_docs.append([row["document"] for _, row, _ in top] if spec.documents else [])
            outer_metas.append([row["metadata"] for _, row, _ in top] if spec.metadatas else [])
            outer_dists.append([float(dist) for dist, _, _ in top] if spec.distances else [])
            if spec.embeddings:
                outer_embeds.append([vec.astype(float).tolist() for _, _, vec in top])

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
        with self._cursor() as cur:
            rows = self._rows(cur, where=where, where_document=where_document)
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
                cur.execute(
                    "DELETE FROM documents WHERE collection_id = ? AND id = ?",
                    (collection_id, doc_id),
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

    def _lexical_search_fts(self, cur, *, query: str, n_results: int, where: Optional[dict]):
        if not self._fts_available(cur):
            return None
        tokens = [t for t in _tokenize(query) if len(t) >= 2]
        if not tokens:
            return None
        fts_query = " OR ".join(tokens)
        collection_id = self._collection_id(cur)
        try:
            limit_sql = "" if where else "LIMIT ?"
            params = (fts_query, collection_id)
            if not where:
                params = (*params, max(n_results * 5, n_results))
            rows = cur.execute(
                f"""
                SELECT doc_id, bm25(docs_fts) AS rank
                FROM docs_fts
                WHERE docs_fts MATCH ? AND collection_id = ?
                ORDER BY rank
                {limit_sql}
                """,
                params,
            ).fetchall()
        except sqlite3.Error:
            logger.debug("sqlite_exact FTS query failed; using Python lexical scan", exc_info=True)
            return None
        if not rows:
            return []
        ids = [row[0] for row in rows]
        docs = []
        for start in range(0, len(ids), 900):
            chunk_ids = ids[start : start + 900]
            placeholders = ",".join("?" for _ in chunk_ids)
            docs.extend(
                cur.execute(
                    f"""
                    SELECT id, document, metadata_json
                    FROM documents
                    WHERE collection_id = ? AND id IN ({placeholders})
                    """,
                    (collection_id, *chunk_ids),
                ).fetchall()
            )
        by_id = {doc_id: (doc or "", _json_loads(meta_json)) for doc_id, doc, meta_json in docs}
        hits = []
        for doc_id, rank in rows:
            doc_meta = by_id.get(doc_id)
            if doc_meta is None:
                continue
            doc, meta = doc_meta
            if not _matches_where(meta, where):
                continue
            hits.append(
                LexicalHit(
                    id=doc_id,
                    document=doc,
                    metadata=meta,
                    score=-float(rank),
                )
            )
            if len(hits) >= n_results:
                break
        return hits

    def close(self) -> None:
        self._closed = True

    def health(self) -> HealthStatus:
        if self._closed or self._handle.closed:
            return HealthStatus.unhealthy("collection closed")
        return HealthStatus.healthy()


class SQLiteExactBackend(BaseBackend):
    name = "sqlite_exact"
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

    def __init__(self):
        self._clients: dict[str, _SQLiteExactHandle] = {}
        self._clients_lock = threading.RLock()
        self._closed = False

    @staticmethod
    def _db_path(palace_path: str) -> str:
        return os.path.join(palace_path, _DB_FILENAME)

    def _connect(self, palace_path: str, create: bool):
        if self._closed:
            raise BackendClosedError("SQLiteExactBackend has been closed")
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
            cached = self._clients.get(palace_path)
            if cached is not None and not cached.closed:
                return cached
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        lock = threading.RLock()
        handle = _SQLiteExactHandle(conn, lock)
        with handle.lock:
            self._init_schema(conn)
        with self._clients_lock:
            self._clients[palace_path] = handle
        return handle

    def _init_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            PRAGMA journal_mode=WAL;
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
            """
        )
        columns = {row[1] for row in conn.execute("PRAGMA table_info(collections)").fetchall()}
        if "dimension" not in columns:
            conn.execute("ALTER TABLE collections ADD COLUMN dimension INTEGER")
        try:
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts
                USING fts5(collection_id UNINDEXED, doc_id UNINDEXED, document)
                """
            )
            conn.execute(
                """
                INSERT INTO meta(key, value)
                VALUES ('fts5_available', '1')
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """
            )
        except sqlite3.OperationalError:
            conn.execute(
                """
                INSERT INTO meta(key, value)
                VALUES ('fts5_available', '0')
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """
            )
        conn.commit()

    def get_collection(
        self,
        *args,
        **kwargs,
    ) -> SQLiteExactCollection:
        palace, collection_name, create = self._normalize_args(args, kwargs)
        palace_path = palace.local_path
        if palace_path is None:
            raise PalaceNotFoundError("SQLiteExactBackend requires PalaceRef.local_path")
        if not create and not os.path.isdir(palace_path):
            raise PalaceNotFoundError(palace_path)
        handle = self._connect(palace_path, create=create)
        with handle.lock:
            row = handle.conn.execute(
                "SELECT id FROM collections WHERE name = ?",
                (collection_name,),
            ).fetchone()
            if row is None:
                if not create:
                    raise CollectionNotInitializedError(collection_name)
                handle.conn.execute(
                    "INSERT INTO collections(name, created_at) VALUES (?, ?)",
                    (collection_name, _utcnow()),
                )
                handle.conn.commit()
        return SQLiteExactCollection(handle, collection_name)

    @staticmethod
    def _normalize_args(args, kwargs):
        if "palace" in kwargs:
            palace = kwargs.pop("palace")
            if not isinstance(palace, PalaceRef):
                raise TypeError("palace= must be a PalaceRef instance")
            collection_name = kwargs.pop("collection_name")
            create = bool(kwargs.pop("create", False))
            kwargs.pop("options", None)
            if args or kwargs:
                raise TypeError("unexpected arguments to get_collection")
            return palace, collection_name, create
        if args:
            palace_path = args[0]
            rest = list(args[1:])
            collection_name = kwargs.pop("collection_name", None) or (rest.pop(0) if rest else None)
            if collection_name is None:
                raise TypeError("collection_name is required")
            create = kwargs.pop("create", False)
            if rest:
                create = rest.pop(0)
            if rest or kwargs:
                raise TypeError("unexpected arguments to get_collection")
            return PalaceRef(id=palace_path, local_path=palace_path), collection_name, bool(create)
        if "palace_path" in kwargs:
            palace_path = kwargs.pop("palace_path")
            collection_name = kwargs.pop("collection_name")
            create = bool(kwargs.pop("create", False))
            if kwargs:
                raise TypeError("unexpected arguments to get_collection")
            return PalaceRef(id=palace_path, local_path=palace_path), collection_name, create
        raise TypeError("get_collection requires palace= or a positional palace_path")

    def close_palace(self, palace: PalaceRef | str) -> None:
        path = palace.local_path if isinstance(palace, PalaceRef) else palace
        if path is None:
            return
        with self._clients_lock:
            cached = self._clients.pop(path, None)
        if cached is not None:
            with cached.lock:
                cached.closed = True
                cached.conn.close()

    def close(self) -> None:
        with self._clients_lock:
            handles = list(self._clients.values())
            self._clients.clear()
        for handle in handles:
            with handle.lock:
                handle.closed = True
                handle.conn.close()
        self._closed = True

    def health(self, palace: Optional[PalaceRef] = None) -> HealthStatus:
        if self._closed:
            return HealthStatus.unhealthy("backend closed")
        if palace and palace.local_path and not os.path.isfile(self._db_path(palace.local_path)):
            return HealthStatus.unhealthy("sqlite_exact database not found")
        return HealthStatus.healthy()

    @classmethod
    def detect(cls, path: str) -> bool:
        return os.path.isfile(os.path.join(path, _DB_FILENAME))

    def create_collection(self, palace_path: str, collection_name: str) -> SQLiteExactCollection:
        return self.get_collection(palace_path, collection_name, create=True)

    def get_or_create_collection(self, palace_path: str, collection_name: str):
        return self.get_collection(palace_path, collection_name, create=True)

    def delete_collection(self, palace_path: str, collection_name: str) -> None:
        handle = self._connect(palace_path, create=False)
        with handle.lock:
            row = handle.conn.execute(
                "SELECT id FROM collections WHERE name = ?",
                (collection_name,),
            ).fetchone()
            if row is None:
                raise CollectionNotInitializedError(collection_name)
            collection_id = int(row[0])
            handle.conn.execute("DELETE FROM documents WHERE collection_id = ?", (collection_id,))
            try:
                handle.conn.execute(
                    "DELETE FROM docs_fts WHERE collection_id = ?",
                    (collection_id,),
                )
            except sqlite3.OperationalError:
                pass
            handle.conn.execute("DELETE FROM collections WHERE id = ?", (collection_id,))
            handle.conn.commit()


__all__ = ["SQLiteExactBackend", "SQLiteExactCollection"]
