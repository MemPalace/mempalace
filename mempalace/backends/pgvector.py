"""Postgres + pgvector backend for MemPalace.

pgvector is an opt-in external-service backend, the SQL counterpart to the
Qdrant REST backend. Chroma remains the default; this adapter only runs when
the user explicitly selects ``pgvector`` via config, env, or CLI/MCP flag.
Embeddings are still produced locally by MemPalace through the core embedding
wrapper before vectors are written to Postgres.

Why a second external backend: it exercises the storage contract on a
fundamentally different substrate (SQL + JSONB + the pgvector ``<=>`` operator)
than Qdrant's REST/dict model, proving the ``BaseBackend`` / ``BaseCollection``
surface is not accidentally shaped around one vendor.

Isolation model (RFC 001 isolation contract): one table per
``namespace`` + ``palace`` + ``collection``. The namespace contributes to the
table name, so this backend advertises ``supports_namespace_isolation`` and
satisfies the cross-namespace conformance arm.

Dependency posture: the live client needs the optional ``psycopg`` dependency
(``pip install mempalace[pgvector]``), imported lazily so the package imports
fine without it. CI runs against an in-memory fake client; the live Postgres
round-trip is gated behind ``MEMPALACE_PGVECTOR_LIVE_URL``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Optional
from urllib import parse as urlparse

import numpy as np

from ._sidecar import EMBEDDER_SIDECAR_FILENAME, read_embedder_sidecar, write_embedder_sidecar
from .base import (
    BackendClosedError,
    BackendError,
    BackendMismatchError,
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

_DEFAULT_DSN = "postgresql://localhost:5432/mempalace"
_MARKER_FILENAME = "pgvector_backend.json"
_MAX_IDENTIFIER = 63  # Postgres identifier byte limit.
_TOKEN_RE = re.compile(r"\w{2,}", re.UNICODE)
# Operators that translate to a JSONB containment predicate and so can be
# pushed down to SQL. Comparisons, $or and $contains stay on the local exact
# path (Python filtering), mirroring the Qdrant backend's local fallback.
_SUPPORTED_OPERATORS = frozenset(
    {"$eq", "$ne", "$in", "$nin", "$and", "$or", "$contains", "$gt", "$gte", "$lt", "$lte"}
)
_PUSHDOWN_OPERATORS = frozenset({"$eq", "$ne", "$in", "$nin", "$and"})


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_dumps(obj: Any) -> str:
    return json.dumps(obj or {}, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


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
                raise UnsupportedFilterError(f"operator {key!r} not supported by pgvector")
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
    raise UnsupportedFilterError(f"operator {op!r} not supported by pgvector")


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
            raise UnsupportedFilterError(f"operator {key!r} not supported by pgvector")
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


def _requires_local_filter(where: Optional[dict], where_document: Optional[dict] = None) -> bool:
    """True when ``where``/``where_document`` cannot be fully pushed to SQL.

    Equality, ``$in``, ``$nin``, ``$ne`` and ``$and`` become JSONB containment
    predicates; everything else ($or, $contains, comparisons, any
    where_document) is evaluated on the local exact path so correctness never
    depends on a hand-rolled SQL cast.
    """
    if where_document:
        return True
    if not where:
        return False
    stack = [where]
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        for key, value in node.items():
            if key.startswith("$") and key not in _PUSHDOWN_OPERATORS:
                return True
            if isinstance(value, dict):
                # A field mapping to an operator dict: only pushdown operators
                # keep it on the fast path.
                for op in value:
                    if op.startswith("$") and op not in _PUSHDOWN_OPERATORS:
                        return True
                stack.append(value)
            elif isinstance(value, list):
                stack.extend(item for item in value if isinstance(item, dict))
    return False


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


def _as_vector_array(vector: list[float]) -> np.ndarray:
    arr = np.asarray(vector, dtype=np.float32)
    if arr.ndim != 1 or arr.size == 0:
        raise ValueError("embedding must be a non-empty 1D vector")
    return arr


def _normalize_vectors(embeddings: list[list[float]]) -> tuple[list[list[float]], int]:
    vectors = []
    dims = set()
    for embedding in embeddings:
        arr = _as_vector_array(embedding)
        vectors.append(arr.astype(float).tolist())
        dims.add(int(arr.size))
    if len(dims) > 1:
        raise DimensionMismatchError(
            f"pgvector batch cannot mix embedding dimensions {sorted(dims)}"
        )
    return vectors, dims.pop() if dims else 0


def _jsonable_metadata(meta: dict | None) -> dict:
    try:
        value = json.loads(json.dumps(meta or {}, ensure_ascii=False))
    except (TypeError, ValueError):
        value = {}
    return value if isinstance(value, dict) else {}


def _vector_distance(query: np.ndarray, vector: list[float] | None) -> Optional[float]:
    if vector is None:
        return None
    vec = _as_vector_array(vector)
    if vec.size != query.size:
        return None
    denom = float(np.linalg.norm(query)) * float(np.linalg.norm(vec))
    cos = 0.0 if denom <= 0 else float(np.dot(query, vec) / denom)
    return 1.0 - max(-1.0, min(1.0, cos))


def _vector_literal(vector: list[float]) -> str:
    """Render a vector as the pgvector text literal ``[1,2,3]``.

    Using the text form keeps the optional dependency surface to ``psycopg``
    alone — no ``pgvector`` Python adapter is required, only the server-side
    extension.
    """
    return "[" + ",".join(repr(float(v)) for v in vector) + "]"


def _parse_vector(value: Any) -> Optional[list[float]]:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return [float(v) for v in value]
    text = str(value).strip()
    if not text:
        return None
    text = text.strip("[]")
    if not text:
        return []
    return [float(part) for part in text.split(",")]


def _slug(value: str, fallback: str = "palace") -> str:
    safe = re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_")
    safe = safe or fallback
    if len(safe) <= 48:
        return safe
    digest = sha256(value.encode("utf-8", errors="surrogatepass")).hexdigest()[:12]
    return f"{safe[:35]}_{digest}"


def _pg_identifier(name: str) -> str:
    """Clamp an identifier to Postgres' 63-byte limit, hashing the overflow."""
    if len(name.encode("utf-8")) <= _MAX_IDENTIFIER:
        return name
    digest = sha256(name.encode("utf-8", errors="surrogatepass")).hexdigest()[:12]
    return f"{name[:50]}_{digest}"


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


# Session-level advisory-lock namespace for serializing HNSW index builds
# across daemon writers (RFC 001). classid is a fixed mempalace constant
# ("MEMP" in ASCII); objid is a stable per-table key. Both must fit a signed
# int4, which ``pg_advisory_lock(int4, int4)`` requires.
_MAINTENANCE_LOCK_CLASSID = 0x4D454D50  # "MEMP" — a positive, valid int4


def _advisory_objid(table: str) -> int:
    """Stable signed-int4 advisory key derived from the table name."""
    raw = int(sha256(table.encode("utf-8")).hexdigest()[:8], 16)  # 0 .. 2**32-1
    return raw - 2**32 if raw >= 2**31 else raw


def _hnsw_index_name(table: str) -> str:
    """Deterministic, collision-safe index name for ``table``.

    Routes through :func:`_pg_identifier`, which hashes the overflow when the
    name exceeds Postgres' 63-byte limit. A naive ``[:63]`` truncation could
    return the table name verbatim (tables and indexes share the ``pg_class``
    namespace), which would fail with "relation already exists".
    """
    return _pg_identifier(f"{table}_hnsw_idx")


def _field_sql(field: str, expression: Any, params: list) -> str:
    """Translate one field predicate to a JSONB containment expression."""
    if isinstance(expression, dict):
        parts = []
        for op, operand in expression.items():
            if op == "$eq":
                params.append(_json_dumps({field: operand}))
                parts.append("metadata @> %s::jsonb")
            elif op == "$ne":
                params.append(_json_dumps({field: operand}))
                parts.append("(NOT (metadata @> %s::jsonb))")
            elif op == "$in":
                ors = []
                for item in operand or []:
                    params.append(_json_dumps({field: item}))
                    ors.append("metadata @> %s::jsonb")
                parts.append("(" + (" OR ".join(ors) if ors else "FALSE") + ")")
            elif op == "$nin":
                ors = []
                for item in operand or []:
                    params.append(_json_dumps({field: item}))
                    ors.append("metadata @> %s::jsonb")
                parts.append("(NOT (" + (" OR ".join(ors) if ors else "FALSE") + "))")
            else:  # pragma: no cover - guarded by _requires_local_filter
                raise UnsupportedFilterError(f"operator {op!r} not pushed down by pgvector")
        return " AND ".join(parts) if parts else "TRUE"
    params.append(_json_dumps({field: expression}))
    return "metadata @> %s::jsonb"


def _where_to_sql(where: Optional[dict], params: list) -> str:
    """Translate the pushdown filter subset to a JSONB SQL predicate.

    Appends bound parameters to ``params`` and returns a boolean SQL string.
    Only operators allowed past :func:`_requires_local_filter` reach here.
    """
    if not where:
        return "TRUE"
    clauses = []
    for key, expected in where.items():
        if key == "$and":
            for clause in expected or []:
                clauses.append(f"({_where_to_sql(clause, params)})")
            continue
        if key.startswith("$"):  # pragma: no cover - guarded upstream
            raise UnsupportedFilterError(f"operator {key!r} not pushed down by pgvector")
        clauses.append(_field_sql(key, expected, params))
    return " AND ".join(clauses) if clauses else "TRUE"


@dataclass(frozen=True)
class _PgVectorConfig:
    dsn: str = _DEFAULT_DSN
    namespace: Optional[str] = None

    @classmethod
    def from_options(cls, options: Optional[dict] = None) -> "_PgVectorConfig":
        options = options or {}
        try:
            from ..config import MempalaceConfig

            cfg = MempalaceConfig()
        except Exception:  # pragma: no cover - config import should be boring
            cfg = None
        dsn = (
            options.get("dsn")
            or options.get("url")
            or os.environ.get("MEMPALACE_PGVECTOR_DSN")
            or getattr(cfg, "pgvector_dsn", None)
            or _DEFAULT_DSN
        )
        namespace = (
            options.get("namespace")
            or os.environ.get("MEMPALACE_PGVECTOR_NAMESPACE")
            or getattr(cfg, "pgvector_namespace", None)
        )
        return cls(
            dsn=str(dsn).strip() or _DEFAULT_DSN,
            namespace=str(namespace).strip() or None if namespace else None,
        )


class _PgVectorClient:
    """Thin psycopg wrapper. ``psycopg`` is imported lazily on first connect."""

    def __init__(self, config: _PgVectorConfig):
        self._config = config
        self._conn = None
        self._closed = False
        self._lock = threading.RLock()

    def _connect(self):
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - exercised only without the extra
            raise BackendError(
                "pgvector backend requires the optional 'psycopg' dependency; "
                "install mempalace[pgvector]"
            ) from exc
        # One client is shared across threads (PgVectorBackend caches a
        # single instance per config), so the read-create-store on self._conn
        # must hold the same lock _execute serializes on; unlocked, two
        # first-connect threads each opened a connection and the loser leaked
        # unclosed. The RLock makes the _execute -> _connect nesting safe. A
        # stalled connect blocks peers under the lock the same way any
        # in-flight query on this single shared connection already does.
        with self._lock:
            if self._closed:
                raise BackendError("pgvector client has been closed")
            if self._conn is not None and not getattr(self._conn, "closed", False):
                return self._conn
            try:
                self._conn = psycopg.connect(self._config.dsn)
            except Exception as exc:  # noqa: BLE001 - surface any driver failure uniformly
                raise BackendError(f"pgvector connection failed: {exc}") from exc
            return self._conn

    def _execute(self, sql: str, params=None, *, fetch: bool = False, many: bool = False):
        with self._lock:
            conn = self._connect()
            try:
                with conn.cursor() as cur:
                    if many:
                        cur.executemany(sql, params or [])
                        rows = None
                    else:
                        cur.execute(sql, params or [])
                        rows = cur.fetchall() if fetch else None
                conn.commit()
            except Exception as exc:  # noqa: BLE001 - normalize to BackendError
                try:
                    conn.rollback()
                except Exception:  # pragma: no cover - rollback best effort
                    pass
                raise BackendError(f"pgvector query failed: {exc}") from exc
        return rows

    def ping(self) -> None:
        self._execute("SELECT 1", fetch=True)

    def ensure_extension(self) -> None:
        try:
            self._execute("CREATE EXTENSION IF NOT EXISTS vector")
        except BackendError:
            # Extension may already exist or require elevated privilege; the
            # table create will fail loudly later if the vector type is absent.
            logger.debug("pgvector CREATE EXTENSION skipped", exc_info=True)

    def table_exists(self, table: str) -> bool:
        rows = self._execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_name = %s",
            [table],
            fetch=True,
        )
        return bool(rows)

    def table_dimension(self, table: str) -> Optional[int]:
        # Read the declared dimension via ``format_type`` (which invokes the
        # type's own typmod_out and yields canonical ``vector(384)`` text)
        # rather than the raw ``atttypmod``. On the pgvector versions tested
        # (0.8.x) atttypmod already equals the bare dimension, so the direct
        # read also worked — but format_type is the canonical, version-proof
        # source of truth and avoids depending on the internal typmod encoding
        # staying stable across pgvector releases.
        try:
            rows = self._execute(
                "SELECT format_type(a.atttypid, a.atttypmod) FROM pg_attribute a "
                "WHERE a.attrelid = %s::regclass AND a.attname = 'embedding'",
                [_quote_identifier(table)],
                fetch=True,
            )
        except BackendError:
            return None
        if not rows or not rows[0] or not rows[0][0]:
            return None
        match = re.search(r"\((\d+)\)", str(rows[0][0]))
        return int(match.group(1)) if match else None

    def create_table(self, table: str, dimension: int) -> None:
        self.ensure_extension()
        qi = _quote_identifier(table)
        self._execute(
            f"CREATE TABLE IF NOT EXISTS {qi} ("
            "id text PRIMARY KEY, "
            "document text NOT NULL DEFAULT '', "
            "metadata jsonb NOT NULL DEFAULT '{}'::jsonb, "
            f"embedding vector({int(dimension)}), "
            "updated_at timestamptz)"
        )

    def upsert_rows(self, table: str, rows: list[dict]) -> None:
        if not rows:
            return
        qi = _quote_identifier(table)
        sql = (
            f"INSERT INTO {qi} (id, document, metadata, embedding, updated_at) "
            "VALUES (%s, %s, %s::jsonb, %s::vector, %s) "
            "ON CONFLICT (id) DO UPDATE SET "
            "document = EXCLUDED.document, metadata = EXCLUDED.metadata, "
            "embedding = EXCLUDED.embedding, updated_at = EXCLUDED.updated_at"
        )
        params = [
            (
                row["id"],
                row["document"],
                _json_dumps(row.get("metadata")),
                _vector_literal(row["embedding"]),
                row.get("updated_at") or _utcnow(),
            )
            for row in rows
        ]
        self._execute(sql, params, many=True)

    def query_rows(
        self,
        table: str,
        *,
        vector: list[float],
        limit: int,
        where: Optional[dict],
        with_embedding: bool,
    ) -> list[dict]:
        qi = _quote_identifier(table)
        params: list = [_vector_literal(vector)]
        where_sql = _where_to_sql(where, params) if where else "TRUE"
        cols = "id, document, metadata"
        if with_embedding:
            cols += ", embedding"
        params.append(int(limit))
        # SQL text order — distance %s::vector, then WHERE params, then LIMIT %s
        # — already matches positional binding order in ``params``.
        sql = (
            f"SELECT {cols}, embedding <=> %s::vector AS distance "
            f"FROM {qi} WHERE {where_sql} ORDER BY distance ASC LIMIT %s"
        )
        rows = self._execute(sql, params, fetch=True)
        return [
            self._row(record, with_embedding=with_embedding, with_distance=True)
            for record in rows or []
        ]

    def scroll_rows(
        self,
        table: str,
        *,
        where: Optional[dict] = None,
        with_embedding: bool = False,
    ) -> list[dict]:
        qi = _quote_identifier(table)
        params: list = []
        where_sql = _where_to_sql(where, params) if where else "TRUE"
        cols = "id, document, metadata"
        if with_embedding:
            cols += ", embedding"
        sql = f"SELECT {cols} FROM {qi} WHERE {where_sql}"
        rows = self._execute(sql, params, fetch=True)
        return [
            self._row(record, with_embedding=with_embedding, with_distance=False)
            for record in rows or []
        ]

    def delete_rows(
        self,
        table: str,
        *,
        ids: Optional[list[str]] = None,
        where: Optional[dict] = None,
    ) -> None:
        qi = _quote_identifier(table)
        if ids is not None:
            self._execute(f"DELETE FROM {qi} WHERE id = ANY(%s)", [list(ids)])
            return
        params: list = []
        where_sql = _where_to_sql(where, params) if where else "TRUE"
        self._execute(f"DELETE FROM {qi} WHERE {where_sql}", params)

    def count_rows(self, table: str) -> int:
        rows = self._execute(f"SELECT count(*) FROM {_quote_identifier(table)}", fetch=True)
        return int(rows[0][0]) if rows and rows[0] else 0

    def drop_table(self, table: str) -> None:
        self._execute(f"DROP TABLE IF EXISTS {_quote_identifier(table)}")

    # ------------------------------------------------------------------
    # Maintenance (RFC 001)
    # ------------------------------------------------------------------
    def has_vector_index(self, table: str) -> bool:
        rows = self._execute(
            "SELECT 1 FROM pg_indexes WHERE schemaname = current_schema() "
            "AND tablename = %s AND indexdef ILIKE %s",
            [table, "%using hnsw%"],
            fetch=True,
        )
        return bool(rows)

    def try_advisory_lock(self, classid: int, objid: int) -> bool:
        rows = self._execute("SELECT pg_try_advisory_lock(%s, %s)", [classid, objid], fetch=True)
        return bool(rows and rows[0] and rows[0][0])

    def advisory_unlock(self, classid: int, objid: int) -> None:
        self._execute("SELECT pg_advisory_unlock(%s, %s)", [classid, objid], fetch=True)

    def create_hnsw_index(self, table: str) -> None:
        qi = _quote_identifier(table)
        idx = _quote_identifier(_hnsw_index_name(table))
        # Non-concurrent build takes ACCESS EXCLUSIVE for the build duration;
        # the advisory lock in the caller ensures only one session builds, so
        # writes are blocked once rather than by every writer that crossed the
        # threshold (the production wedge this serialization fixes).
        self._execute(
            f"CREATE INDEX IF NOT EXISTS {idx} ON {qi} USING hnsw (embedding vector_cosine_ops)"
        )

    def analyze_table(self, table: str) -> None:
        self._execute(f"ANALYZE {_quote_identifier(table)}")

    def close(self) -> None:
        # Terminal: the only caller is PgVectorBackend.close(), after which
        # the backend refuses to hand the client out again. Without the flag a
        # stale reference would silently reconnect and leak a session nobody
        # can ever close.
        with self._lock:
            self._closed = True
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:  # pragma: no cover - close best effort
                    pass
                self._conn = None

    @staticmethod
    def _row(record, *, with_embedding: bool, with_distance: bool) -> dict:
        record = list(record)
        row = {
            "id": str(record[0]),
            "document": record[1] if record[1] is not None else "",
            "metadata": record[2]
            if isinstance(record[2], dict)
            else (json.loads(record[2]) if record[2] else {}),
            "embedding": None,
            "distance": None,
        }
        idx = 3
        if with_embedding:
            row["embedding"] = _parse_vector(record[idx])
            idx += 1
        if with_distance:
            row["distance"] = float(record[idx]) if record[idx] is not None else None
        return row


class PgVectorCollection(BaseCollection):
    def __init__(
        self,
        *,
        backend: "PgVectorBackend",
        client: _PgVectorClient,
        config: _PgVectorConfig,
        palace: PalaceRef,
        collection_name: str,
        table: str,
    ):
        self._backend = backend
        self._client = client
        self._config = config
        self._palace = palace
        self._collection_name = collection_name
        self._table = table
        self._lock = threading.RLock()
        self._closed = False
        self._known_dimension: Optional[int] = None

    def _ensure_open(self) -> None:
        if self._closed or self._backend._closed:
            raise BackendClosedError("PgVectorCollection has been closed")

    def _table_exists(self) -> bool:
        return self._client.table_exists(self._table)

    def _marker_exists(self) -> bool:
        return self._backend._marker_exists(self._palace)

    def get_stored_embedder_identity(self):
        return self._backend._get_embedder_identity(self._palace, self._collection_name)

    def set_embedder_identity(self, identity) -> None:
        # Sidecar-backed (see PgVectorBackend), so this records even on a
        # brand-new palace whose mismatch marker doesn't exist yet.
        self._backend._set_embedder_identity(self._palace, self._collection_name, identity)

    def _ensure_table(self, dimension: int) -> None:
        if dimension <= 0:
            raise ValueError("embedding dimension must be positive")
        with self._lock:
            self._ensure_open()
            if self._known_dimension is not None:
                if self._known_dimension != dimension:
                    raise DimensionMismatchError(
                        f"pgvector collection {self._collection_name!r} expects "
                        f"embedding dimension {self._known_dimension}, got {dimension}"
                    )
                return
            if not self._table_exists():
                self._client.create_table(self._table, dimension)
                self._known_dimension = dimension
                return
            existing_dim = self._client.table_dimension(self._table)
            if existing_dim is not None and existing_dim != dimension:
                raise DimensionMismatchError(
                    f"pgvector collection {self._collection_name!r} expects "
                    f"embedding dimension {existing_dim}, got {dimension}"
                )
            self._known_dimension = existing_dim or dimension

    def _scroll(self, *, where=None, with_embedding=False) -> list[dict]:
        self._ensure_open()
        if not self._table_exists():
            if self._marker_exists():
                raise CollectionNotInitializedError(self._collection_name)
            return []
        return self._client.scroll_rows(self._table, where=where, with_embedding=with_embedding)

    def _rows(
        self,
        *,
        ids=None,
        where=None,
        where_document=None,
        with_embedding=False,
    ) -> list[dict]:
        _validate_where(where)
        _validate_where(where_document)
        pushdown = None if _requires_local_filter(where, where_document) else where
        rows = self._scroll(where=pushdown, with_embedding=with_embedding)
        id_set = set(ids) if ids is not None else None
        return [
            row
            for row in rows
            if (id_set is None or row["id"] in id_set)
            and _matches_where(row["metadata"], where)
            and _matches_where_document(row["document"], where_document)
        ]

    def add(self, *, documents, ids, metadatas=None, embeddings=None):
        _validate_write_batch(
            documents=documents, ids=ids, metadatas=metadatas, embeddings=embeddings
        )
        if embeddings is None:
            raise ValueError("pgvector requires explicit embeddings")
        if len(set(ids)) != len(ids):
            raise ValueError("add ids must be unique")
        existing = self.get(ids=list(ids), include=[])
        if existing.ids:
            raise ValueError(f"ids already exist in pgvector collection: {existing.ids}")
        self.upsert(documents=documents, ids=ids, metadatas=metadatas, embeddings=embeddings)

    def upsert(self, *, documents, ids, metadatas=None, embeddings=None):
        _validate_write_batch(
            documents=documents, ids=ids, metadatas=metadatas, embeddings=embeddings
        )
        if embeddings is None:
            raise ValueError("pgvector requires explicit embeddings")
        vectors, dimension = _normalize_vectors(embeddings)
        self._ensure_table(dimension)
        metadatas = metadatas or [{} for _ in ids]
        rows = [
            {
                "id": str(doc_id),
                "document": str(doc),
                "metadata": _jsonable_metadata(meta),
                "embedding": vector,
                "updated_at": _utcnow(),
            }
            for doc_id, doc, meta, vector in zip(ids, documents, metadatas, vectors)
        ]
        self._client.upsert_rows(self._table, rows)
        self._backend._write_marker(self._palace, self._config)

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
        existing = self.get(ids=ids, include=["documents", "metadatas", "embeddings"])
        by_id = {
            rid: (existing.documents[i], existing.metadatas[i], existing.embeddings[i])
            for i, rid in enumerate(existing.ids)
            if existing.embeddings is not None
        }
        out_ids, out_docs, out_metas, out_embeddings = [], [], [], []
        for idx, doc_id in enumerate(ids):
            if doc_id not in by_id:
                continue
            prev_doc, prev_meta, prev_embedding = by_id[doc_id]
            out_ids.append(doc_id)
            out_docs.append(documents[idx] if documents is not None else prev_doc)
            meta = dict(prev_meta or {})
            if metadatas is not None:
                meta.update(metadatas[idx] or {})
            out_metas.append(meta)
            out_embeddings.append(embeddings[idx] if embeddings is not None else prev_embedding)
        if out_ids:
            self.upsert(
                documents=out_docs, ids=out_ids, metadatas=out_metas, embeddings=out_embeddings
            )

    def _query_local_exact(
        self, *, query_embeddings, n_results, where, where_document, include
    ) -> QueryResult:
        spec = _IncludeSpec.resolve(include, default_distances=True)
        pushdown = None if _requires_local_filter(where, where_document) else where
        rows = self._scroll(where=pushdown, with_embedding=True)
        rows = [
            row
            for row in rows
            if _matches_where(row["metadata"], where)
            and _matches_where_document(row["document"], where_document)
        ]
        outer_ids: list[list[str]] = []
        outer_docs: list[list[str]] = []
        outer_metas: list[list[dict]] = []
        outer_dists: list[list[float]] = []
        outer_embeds: list[list[list[float]]] = []
        for query_vector in query_embeddings:
            q = _as_vector_array(query_vector)
            scored = []
            for row in rows:
                distance = _vector_distance(q, row["embedding"])
                if distance is not None:
                    scored.append((distance, row))
            scored.sort(key=lambda item: item[0])
            top = scored[:n_results]
            outer_ids.append([row["id"] for _, row in top])
            outer_docs.append([row["document"] for _, row in top] if spec.documents else [])
            outer_metas.append([row["metadata"] for _, row in top] if spec.metadatas else [])
            outer_dists.append([float(dist) for dist, _ in top] if spec.distances else [])
            if spec.embeddings:
                outer_embeds.append([row["embedding"] or [] for _, row in top])
        return QueryResult(
            ids=outer_ids,
            documents=outer_docs,
            metadatas=outer_metas,
            distances=outer_dists,
            embeddings=outer_embeds if spec.embeddings else None,
        )

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
                "pgvector requires query_embeddings; use palace.get_collection wrapper"
            )
        if query_embeddings is None:
            raise ValueError("query requires query_embeddings")
        if not query_embeddings:
            raise ValueError("query input must be a non-empty list")
        _validate_where(where)
        _validate_where(where_document)
        if _requires_local_filter(where, where_document):
            return self._query_local_exact(
                query_embeddings=query_embeddings,
                n_results=n_results,
                where=where,
                where_document=where_document,
                include=include,
            )
        self._ensure_open()
        if not self._table_exists():
            if self._marker_exists():
                raise CollectionNotInitializedError(self._collection_name)
            return QueryResult.empty(
                num_queries=len(query_embeddings),
                embeddings_requested=bool(include and "embeddings" in include),
            )
        spec = _IncludeSpec.resolve(include, default_distances=True)
        outer_ids: list[list[str]] = []
        outer_docs: list[list[str]] = []
        outer_metas: list[list[dict]] = []
        outer_dists: list[list[float]] = []
        outer_embeds: list[list[list[float]]] = []
        for query_vector in query_embeddings:
            q = _as_vector_array(query_vector)
            if self._known_dimension is None:
                self._known_dimension = self._client.table_dimension(self._table)
            if self._known_dimension is not None and int(q.size) != self._known_dimension:
                raise DimensionMismatchError(
                    f"pgvector collection {self._collection_name!r} expects "
                    f"embedding dimension {self._known_dimension}, got {int(q.size)}"
                )
            rows = self._client.query_rows(
                self._table,
                vector=q.astype(float).tolist(),
                limit=n_results,
                where=where,
                with_embedding=spec.embeddings,
            )
            outer_ids.append([row["id"] for row in rows])
            outer_docs.append([row["document"] for row in rows] if spec.documents else [])
            outer_metas.append([row["metadata"] for row in rows] if spec.metadatas else [])
            outer_dists.append(
                [float(row["distance"]) if row["distance"] is not None else 1.0 for row in rows]
                if spec.distances
                else []
            )
            if spec.embeddings:
                outer_embeds.append([row["embedding"] or [] for row in rows])
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
        rows = self._rows(
            ids=ids, where=where, where_document=where_document, with_embedding=spec.embeddings
        )
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
            embeddings=[row["embedding"] or [] for row in rows] if spec.embeddings else None,
        )

    def delete(self, *, ids=None, where=None):
        _validate_where(where)
        if not self._table_exists():
            if self._marker_exists():
                raise CollectionNotInitializedError(self._collection_name)
            return
        if ids is not None and where is None:
            self._client.delete_rows(self._table, ids=list(ids))
            return
        if ids is None and where is not None and not _requires_local_filter(where):
            self._client.delete_rows(self._table, where=where)
            return
        rows = self._rows(ids=ids, where=where)
        if rows:
            self._client.delete_rows(self._table, ids=[row["id"] for row in rows])

    def count(self) -> int:
        self._ensure_open()
        if not self._table_exists():
            if self._marker_exists():
                raise CollectionNotInitializedError(self._collection_name)
            return 0
        return self._client.count_rows(self._table)

    def lexical_search(self, *, query: str, n_results: int = 10, where: Optional[dict] = None):
        _validate_where(where)
        pushdown = None if _requires_local_filter(where) else where
        rows = self._scroll(where=pushdown, with_embedding=False)
        rows = [row for row in rows if _matches_where(row["metadata"], where)]
        scores = _bm25_scores(query, [row["document"] for row in rows])
        hits = [
            LexicalHit(
                id=row["id"],
                document=row["document"],
                metadata=row["metadata"],
                score=score,
            )
            for row, score in zip(rows, scores)
            if score > 0
        ]
        hits.sort(key=lambda hit: hit.score, reverse=True)
        return LexicalResult(hits=hits[:n_results])

    def close(self) -> None:
        self._closed = True

    def health(self) -> HealthStatus:
        if self._closed or self._backend._closed:
            return HealthStatus.unhealthy("collection closed")
        try:
            if not self._table_exists():
                return HealthStatus.unhealthy("pgvector table not found")
        except Exception as exc:  # noqa: BLE001 - backend health should summarize
            return HealthStatus.unhealthy(str(exc))
        return HealthStatus.healthy()

    def maintenance_state(self) -> dict:
        empty = {"row_count": 0, "vector_index": None, "index_build_complete": False}
        self._ensure_open()
        try:
            if not self._table_exists():
                return empty
            rows = self._client.count_rows(self._table)
            has_index = self._client.has_vector_index(self._table)
        except Exception:  # noqa: BLE001 - state report must not raise
            logger.debug("pgvector maintenance state probe failed", exc_info=True)
            return empty
        return {
            "row_count": rows,
            "vector_index": "hnsw" if has_index else None,
            "index_build_complete": has_index,
        }

    def run_maintenance(self, kind: str):
        from .base import MaintenanceResult, UnsupportedMaintenanceKindError

        if kind not in PgVectorBackend.maintenance_kinds:
            raise UnsupportedMaintenanceKindError(
                f"pgvector does not support maintenance kind {kind!r}"
            )
        self._ensure_open()
        # Nothing to maintain on a not-yet-materialized table (collection opened
        # create=True but never written) — return noop rather than letting a
        # raw "relation does not exist" error escape.
        if not self._table_exists():
            return MaintenanceResult(kind=kind, status="noop", stats={"reason": "no table"})
        if kind == "analyze":
            self._client.analyze_table(self._table)
            return MaintenanceResult(kind="analyze", status="ran")

        # reindex → build the optional HNSW index. Opt-in: it makes search
        # approximate, trading the exact-scan 100%-recall default for scale.
        # Serialized with a session advisory lock so concurrent daemon writers
        # learn "already_running" instead of each stacking an ACCESS EXCLUSIVE
        # index build.
        if self._client.has_vector_index(self._table):
            return MaintenanceResult(kind="reindex", status="noop", stats={"vector_index": "hnsw"})
        classid, objid = _MAINTENANCE_LOCK_CLASSID, _advisory_objid(self._table)
        if not self._client.try_advisory_lock(classid, objid):
            return MaintenanceResult(kind="reindex", status="already_running")
        try:
            if self._client.has_vector_index(self._table):  # re-check under lock
                return MaintenanceResult(
                    kind="reindex", status="noop", stats={"vector_index": "hnsw"}
                )
            self._client.create_hnsw_index(self._table)
            return MaintenanceResult(kind="reindex", status="ran", stats={"vector_index": "hnsw"})
        finally:
            self._client.advisory_unlock(classid, objid)


class PgVectorBackend(BaseBackend):
    name = "pgvector"
    capabilities = frozenset(
        {
            "requires_explicit_embeddings",
            "supports_embeddings_in",
            "supports_embeddings_passthrough",
            "supports_embeddings_out",
            "supports_metadata_filters",
            "supports_lexical_search",
            "supports_namespace_isolation",
            "supports_server_side_indexes",
            "server_mode",
        }
    )
    # "compact" is omitted: Postgres autovacuum reclaims space automatically,
    # so a manual VACUUM kind would be redundant. "reindex" builds the optional
    # HNSW index — an opt-in scale lever, NOT on by default, because it makes
    # vector search approximate (the exact ``<=>`` scan is the 100%-recall path).
    maintenance_kinds = frozenset({"analyze", "reindex"})

    def __init__(self):
        self._clients: dict[_PgVectorConfig, _PgVectorClient] = {}
        self._collections_by_palace: dict[str, list[PgVectorCollection]] = {}
        self._lock = threading.RLock()
        self._closed = False

    # ------------------------------------------------------------------
    # Marker / mismatch protection (mirrors the Qdrant local marker).
    # ------------------------------------------------------------------
    @staticmethod
    def _marker_path(palace_path: str) -> str:
        return os.path.join(palace_path, _MARKER_FILENAME)

    @staticmethod
    def _palace_hash(palace: PalaceRef) -> str:
        return sha256(palace.id.encode("utf-8", errors="surrogatepass")).hexdigest()[:16]

    def _table_prefix(self, *, palace: PalaceRef, config: _PgVectorConfig) -> str:
        parts = ["mempalace"]
        if config.namespace:
            parts.append(_slug(config.namespace, "namespace"))
        parts.append(self._palace_hash(palace))
        return "_".join(parts)

    def _table_name(
        self, *, palace: PalaceRef, collection_name: str, config: _PgVectorConfig
    ) -> str:
        config = _PgVectorConfig(
            dsn=config.dsn,
            namespace=palace.namespace or config.namespace,
        )
        prefix = self._table_prefix(palace=palace, config=config)
        return _pg_identifier(f"{prefix}_{_slug(collection_name, 'collection')}")

    def _sanitized_dsn(self, dsn: str) -> dict:
        try:
            parsed = urlparse.urlparse(dsn)
        except Exception:  # pragma: no cover - defensive
            return {"raw": ""}
        return {
            "host": parsed.hostname or "",
            "port": parsed.port or 5432,
            "dbname": (parsed.path or "").lstrip("/"),
        }

    def _marker_target(self, palace: PalaceRef, config: _PgVectorConfig) -> dict:
        target = self._sanitized_dsn(config.dsn)
        target.update(
            {
                "namespace": config.namespace,
                "palace_hash": self._palace_hash(palace),
                "table_prefix": self._table_prefix(palace=palace, config=config),
            }
        )
        return target

    def _marker_exists(self, palace: PalaceRef) -> bool:
        return bool(palace.local_path and os.path.isfile(self._marker_path(palace.local_path)))

    def _read_marker(self, palace: PalaceRef) -> Optional[dict]:
        if not palace.local_path:
            return None
        marker_path = self._marker_path(palace.local_path)
        if not os.path.isfile(marker_path):
            return None
        try:
            with open(marker_path, encoding="utf-8") as f:
                marker = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            raise BackendMismatchError(f"pgvector marker is unreadable: {marker_path}") from exc
        return marker if isinstance(marker, dict) else {}

    def _validate_marker_target(self, palace: PalaceRef, config: _PgVectorConfig) -> None:
        marker = self._read_marker(palace)
        if marker is None:
            return
        if marker.get("backend") != self.name:
            raise BackendMismatchError("pgvector marker does not identify the pgvector backend")
        expected = self._marker_target(palace, config)
        actual = marker.get("pgvector")
        if not isinstance(actual, dict):
            raise BackendMismatchError("pgvector marker is missing target metadata")
        mismatched = [
            key for key, expected_value in expected.items() if actual.get(key) != expected_value
        ]
        if mismatched:
            details = ", ".join(mismatched)
            raise BackendMismatchError(
                "pgvector marker target does not match current configuration "
                f"({details}); keep MEMPALACE_PGVECTOR_DSN and namespace consistent "
                "or use a fresh palace directory"
            )

    def _write_marker(self, palace: PalaceRef, config: _PgVectorConfig) -> None:
        if not palace.local_path:
            return
        os.makedirs(palace.local_path, exist_ok=True)
        try:
            os.chmod(palace.local_path, 0o700)
        except (OSError, NotImplementedError):
            pass
        marker = {
            "backend": self.name,
            "schema_version": 1,
            "created_at": _utcnow(),
            "palace_id": palace.id,
            "pgvector": self._marker_target(palace, config),
        }
        marker_path = self._marker_path(palace.local_path)
        with open(marker_path, "w", encoding="utf-8") as f:
            json.dump(marker, f, indent=2, ensure_ascii=False)
        try:
            os.chmod(marker_path, 0o600)
        except (OSError, NotImplementedError):
            pass

    # Embedder identity lives in a sidecar, NOT the backend marker: the marker's
    # presence signals "palace initialized" (reads raise CollectionNotInitialized
    # when the marker exists but the remote table doesn't), so recording identity
    # at first empty open must not create it. The sidecar is unguarded — like the
    # chroma sidecar — so a brand-new palace can record identity immediately.
    @staticmethod
    def _embedder_sidecar_path(palace: PalaceRef) -> Optional[str]:
        if not palace.local_path:
            return None
        return os.path.join(palace.local_path, EMBEDDER_SIDECAR_FILENAME)

    def _get_embedder_identity(self, palace: PalaceRef, collection_name: str):
        return read_embedder_sidecar(self._embedder_sidecar_path(palace), collection_name)

    def _set_embedder_identity(self, palace: PalaceRef, collection_name: str, identity) -> None:
        write_embedder_sidecar(self._embedder_sidecar_path(palace), collection_name, identity)

    # ------------------------------------------------------------------
    def _client(self, config: _PgVectorConfig) -> _PgVectorClient:
        with self._lock:
            # Checked under the lock so a client cannot be created and stored
            # concurrently with close() clearing the registry (mirrors
            # SQLiteExactBackend._connect).
            if self._closed:
                raise BackendClosedError("PgVectorBackend has been closed")
            client = self._clients.get(config)
            if client is None:
                client = _PgVectorClient(config)
                self._clients[config] = client
            return client

    def get_collection(self, *args, **kwargs) -> PgVectorCollection:
        palace, collection_name, create, options = self._normalize_args(args, kwargs)
        config = _PgVectorConfig.from_options(options)
        if palace.namespace and palace.namespace != config.namespace:
            config = _PgVectorConfig(dsn=config.dsn, namespace=palace.namespace)
        client = self._client(config)
        if palace.local_path:
            marker_path = self._marker_path(palace.local_path)
            if os.path.isfile(marker_path):
                self._validate_marker_target(palace, config)
            elif not create:
                raise PalaceNotFoundError(marker_path)
        else:
            # The local marker is this backend's only mismatch-protection
            # anchor. With no local_path (pure-remote / hosted mode) we can
            # neither write nor validate it, so opening would silently drop
            # protection against DSN/namespace drift. Refuse loudly. A remote
            # marker store for pure-remote palaces is tracked as a follow-up.
            raise BackendError(
                "pgvector backend requires a local palace path to anchor mismatch "
                "protection; pure-remote palaces (local_path=None) are not "
                "supported yet"
            )
        table = self._table_name(palace=palace, collection_name=collection_name, config=config)
        if not create and not client.table_exists(table):
            raise CollectionNotInitializedError(collection_name)
        collection = PgVectorCollection(
            backend=self,
            client=client,
            config=config,
            palace=palace,
            collection_name=collection_name,
            table=table,
        )
        with self._lock:
            self._collections_by_palace.setdefault(palace.id, []).append(collection)
        return collection

    @staticmethod
    def _normalize_args(args, kwargs):
        if "palace" in kwargs:
            palace = kwargs.pop("palace")
            if not isinstance(palace, PalaceRef):
                raise TypeError("palace= must be a PalaceRef instance")
            collection_name = kwargs.pop("collection_name")
            create = bool(kwargs.pop("create", False))
            options = kwargs.pop("options", None)
            if args or kwargs:
                raise TypeError("unexpected arguments to get_collection")
            return palace, collection_name, create, options
        if args:
            palace_path = args[0]
            rest = list(args[1:])
            collection_name = kwargs.pop("collection_name", None) or (rest.pop(0) if rest else None)
            if collection_name is None:
                raise TypeError("collection_name is required")
            create = kwargs.pop("create", False)
            if rest:
                create = rest.pop(0)
            options = kwargs.pop("options", None)
            if rest or kwargs:
                raise TypeError("unexpected arguments to get_collection")
            return (
                PalaceRef(id=palace_path, local_path=palace_path),
                collection_name,
                bool(create),
                options,
            )
        if "palace_path" in kwargs:
            palace_path = kwargs.pop("palace_path")
            collection_name = kwargs.pop("collection_name")
            create = bool(kwargs.pop("create", False))
            options = kwargs.pop("options", None)
            if kwargs:
                raise TypeError("unexpected arguments to get_collection")
            return (
                PalaceRef(id=palace_path, local_path=palace_path),
                collection_name,
                create,
                options,
            )
        raise TypeError("get_collection requires palace= or a positional palace_path")

    def close_palace(self, palace: PalaceRef | str) -> None:
        palace_id = palace.id if isinstance(palace, PalaceRef) else palace
        with self._lock:
            collections = self._collections_by_palace.pop(palace_id, [])
        for collection in collections:
            collection.close()

    def close(self) -> None:
        with self._lock:
            collections = [
                collection
                for palace_collections in self._collections_by_palace.values()
                for collection in palace_collections
            ]
            clients = list(self._clients.values())
            self._collections_by_palace.clear()
            self._clients.clear()
            self._closed = True
        for collection in collections:
            collection.close()
        for client in clients:
            client.close()

    def health(self, palace: Optional[PalaceRef] = None) -> HealthStatus:
        if self._closed:
            return HealthStatus.unhealthy("backend closed")
        try:
            self._client(_PgVectorConfig.from_options()).ping()
        except Exception as exc:  # noqa: BLE001 - user-facing health status
            return HealthStatus.unhealthy(str(exc))
        if (
            palace
            and palace.local_path
            and not os.path.isfile(self._marker_path(palace.local_path))
        ):
            return HealthStatus.unhealthy("pgvector marker not found")
        return HealthStatus.healthy()

    @classmethod
    def detect(cls, path: str) -> bool:
        return os.path.isfile(os.path.join(path, _MARKER_FILENAME))

    def create_collection(self, palace_path: str, collection_name: str) -> PgVectorCollection:
        return self.get_collection(palace_path, collection_name, create=True)

    def get_or_create_collection(self, palace_path: str, collection_name: str):
        return self.get_collection(palace_path, collection_name, create=True)

    def delete_collection(self, palace_path: str, collection_name: str) -> None:
        palace = PalaceRef(id=palace_path, local_path=palace_path)
        config = _PgVectorConfig.from_options()
        table = self._table_name(palace=palace, collection_name=collection_name, config=config)
        self._client(config).drop_table(table)
