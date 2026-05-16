"""Optional PostgreSQL-backed MemPalace storage backend.

The backend prefers ``pg_sorted_heap`` when available and falls back to
``pgvector``. Optional dependencies are imported lazily so the default Chroma
install remains zero-config.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

from .base import (
    BackendClosedError,
    BaseBackend,
    BaseCollection,
    GetResult,
    HealthStatus,
    PalaceNotFoundError,
    PalaceRef,
    QueryResult,
    UnsupportedFilterError,
    _IncludeSpec,
)

logger = logging.getLogger("mempalace.postgres")

EMBEDDING_DIM = 384
EMBEDDING_MODEL = "chroma-default-all-MiniLM-L6-v2"
VECTOR_INDEX_MIN_ROWS = 5_000
VECTOR_INDEX_CHECK_INTERVAL_ROWS = 1_000

_embedder = None


def _load_psycopg2():
    try:
        import psycopg2
        from psycopg2 import sql
    except ImportError as exc:  # pragma: no cover - exercised without the extra installed.
        raise RuntimeError(
            "PostgreSQL backend requires optional dependencies. "
            'Install with: pip install "mempalace[postgres]"'
        ) from exc
    return psycopg2, sql


def _embed(texts: list[str]) -> list[list[float]]:
    """Embed texts for PostgreSQL vector search.

    Reuse Chroma's default local embedding function so the PostgreSQL backend
    matches the zero-API embedding model already used by the default backend
    without adding a second ML dependency stack.
    """
    global _embedder
    if _embedder is None:
        try:
            from chromadb.utils import embedding_functions
        except ImportError as exc:  # pragma: no cover - chromadb is a core dependency.
            raise RuntimeError(
                "PostgreSQL backend text queries require ChromaDB's local embedding function."
            ) from exc

        _embedder = embedding_functions.DefaultEmbeddingFunction()
        logger.info("Loaded embedding model: %s", EMBEDDING_MODEL)

    vectors = _embedder(texts)
    return [[float(value) for value in vector] for vector in vectors]


def _vec_literal(vector: list[float]) -> str:
    """Convert a vector to a PostgreSQL vector/svec literal."""
    return "[" + ",".join(f"{float(value):.8f}" for value in vector) + "]"


def _parse_vector_literal(value: Any) -> list[float]:
    """Parse pgvector/svec text output into a Python list."""
    if value is None:
        return []
    if isinstance(value, list):
        return [float(v) for v in value]
    text = str(value).strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    if not text:
        return []
    return [float(part) for part in text.split(",")]


def _metadata_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _validate_write_lengths(
    *,
    documents: list[str],
    ids: list[str],
    metadatas: Optional[list[dict[str, Any]]],
    embeddings: Optional[list[list[float]]],
) -> None:
    if len(documents) != len(ids):
        raise ValueError("documents and ids must have the same length")
    if metadatas is not None and len(metadatas) != len(documents):
        raise ValueError("metadatas and documents must have the same length")
    if embeddings is not None and len(embeddings) != len(documents):
        raise ValueError("embeddings and documents must have the same length")


class PostgresCollection(BaseCollection):
    """PostgreSQL collection adapter implementing the RFC 001 collection contract."""

    def __init__(self, dsn: str, table_name: str = "mempalace_drawers"):
        self.dsn = dsn
        self.table_name = table_name
        self._conn = None
        self._vec_type: Optional[str] = None
        self._table_am: Optional[str] = None
        self._index_am: Optional[str] = None
        self._setup_done = False
        self._vector_index_ready = False
        self._rows_since_index_check = VECTOR_INDEX_CHECK_INTERVAL_ROWS
        self._local_row_estimate = 0

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def add(
        self,
        *,
        documents: list[str],
        ids: list[str],
        metadatas: Optional[list[dict[str, Any]]] = None,
        embeddings: Optional[list[list[float]]] = None,
    ) -> None:
        embeddings = self._prepare_write_inputs(
            documents=documents,
            ids=ids,
            metadatas=metadatas,
            embeddings=embeddings,
        )
        self._insert_rows(
            documents=documents,
            ids=ids,
            metadatas=metadatas,
            embeddings=embeddings,
            update_on_conflict=False,
        )

    def upsert(
        self,
        *,
        documents: list[str],
        ids: list[str],
        metadatas: Optional[list[dict[str, Any]]] = None,
        embeddings: Optional[list[list[float]]] = None,
    ) -> None:
        embeddings = self._prepare_write_inputs(
            documents=documents,
            ids=ids,
            metadatas=metadatas,
            embeddings=embeddings,
        )
        self._insert_rows(
            documents=documents,
            ids=ids,
            metadatas=metadatas,
            embeddings=embeddings,
            update_on_conflict=True,
        )

    def _prepare_write_inputs(
        self,
        *,
        documents: list[str],
        ids: list[str],
        metadatas: Optional[list[dict[str, Any]]],
        embeddings: Optional[list[list[float]]],
    ) -> list[list[float]]:
        _validate_write_lengths(
            documents=documents,
            ids=ids,
            metadatas=metadatas,
            embeddings=embeddings,
        )
        self._ensure_setup(create=True)
        if embeddings is None:
            embeddings = _embed(documents)
        if len(embeddings) != len(documents):
            raise ValueError("embeddings and documents must have the same length")
        return embeddings

    def _insert_rows(
        self,
        *,
        documents: list[str],
        ids: list[str],
        metadatas: Optional[list[dict[str, Any]]],
        embeddings: list[list[float]],
        update_on_conflict: bool,
    ) -> None:
        if update_on_conflict:
            conflict_clause = self._sql.SQL(
                "ON CONFLICT (id) DO UPDATE SET "
                "wing = EXCLUDED.wing, "
                "room = EXCLUDED.room, "
                "document = EXCLUDED.document, "
                "embedding = EXCLUDED.embedding, "
                "metadata = EXCLUDED.metadata"
            )
        else:
            conflict_clause = self._sql.SQL("ON CONFLICT (id) DO NOTHING")

        rows_by_id: dict[str, tuple[str, str, str, str, str, str]] = {}
        ordered_ids: list[str] = []
        for index, (doc_id, document) in enumerate(zip(ids, documents)):
            metadata = dict(metadatas[index]) if metadatas else {}
            wing = _metadata_value(metadata.pop("wing", ""))
            room = _metadata_value(metadata.pop("room", ""))
            embedding = _vec_literal(embeddings[index])
            if doc_id not in rows_by_id:
                ordered_ids.append(doc_id)
                rows_by_id[doc_id] = (
                    wing,
                    room,
                    doc_id,
                    document,
                    embedding,
                    json.dumps(metadata),
                )
            elif update_on_conflict:
                rows_by_id[doc_id] = (
                    wing,
                    room,
                    doc_id,
                    document,
                    embedding,
                    json.dumps(metadata),
                )

        rows = [rows_by_id[doc_id] for doc_id in ordered_ids]
        if not rows:
            return
        self._local_row_estimate += len(rows)

        wings = [row[0] for row in rows]
        rooms = [row[1] for row in rows]
        doc_ids = [row[2] for row in rows]
        row_documents = [row[3] for row in rows]
        row_embeddings = [row[4] for row in rows]
        row_metadatas = [row[5] for row in rows]

        cur = self._get_conn().cursor()
        if self._table_am == "sorted_heap":
            cur.execute(
                self._sql.SQL(
                    "INSERT INTO {} (wing, room, id, document, embedding, metadata) "
                    "SELECT wing, room, id, document, embedding_text::{}, metadata_text::jsonb "
                    "FROM unnest("
                    "%s::text[], %s::text[], %s::text[], %s::text[], %s::text[], %s::text[]"
                    ") AS rows(wing, room, id, document, embedding_text, metadata_text) "
                    "{}"
                ).format(self._table_id, self._vec_type_sql, conflict_clause),
                (wings, rooms, doc_ids, row_documents, row_embeddings, row_metadatas),
            )
        else:
            cur.execute(
                self._sql.SQL(
                    "INSERT INTO {} (id, wing, room, document, embedding, metadata) "
                    "SELECT id, wing, room, document, embedding_text::{}, metadata_text::jsonb "
                    "FROM unnest("
                    "%s::text[], %s::text[], %s::text[], %s::text[], %s::text[], %s::text[]"
                    ") AS rows(id, wing, room, document, embedding_text, metadata_text) "
                    "{}"
                ).format(self._table_id, self._vec_type_sql, conflict_clause),
                (doc_ids, wings, rooms, row_documents, row_embeddings, row_metadatas),
            )

        self._maybe_create_vector_index(inserted_rows=len(rows))

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

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
        if where_document is not None:
            raise UnsupportedFilterError("PostgreSQL backend does not support where_document")
        if (query_texts is None) == (query_embeddings is None):
            raise ValueError("query requires exactly one of query_texts or query_embeddings")
        chosen = query_texts if query_texts is not None else query_embeddings
        if not chosen:
            raise ValueError("query input must be a non-empty list")
        if n_results <= 0:
            raise ValueError("n_results must be positive")

        vectors = query_embeddings if query_embeddings is not None else _embed(query_texts or [])
        spec = _IncludeSpec.resolve(include, default_distances=True)
        self._ensure_setup(create=True)

        all_ids: list[list[str]] = []
        all_documents: list[list[str]] = []
        all_metadatas: list[list[dict]] = []
        all_distances: list[list[float]] = []
        all_embeddings: Optional[list[list[list[float]]]] = [] if spec.embeddings else None

        for query_embedding in vectors:
            ids, documents, metadatas, distances, embeddings = self._query_one(
                query_embedding=query_embedding,
                n_results=n_results,
                where=where,
                include_embeddings=spec.embeddings,
            )
            all_ids.append(ids)
            all_documents.append(documents if spec.documents else [])
            all_metadatas.append(metadatas if spec.metadatas else [])
            all_distances.append(distances if spec.distances else [])
            if all_embeddings is not None:
                all_embeddings.append(embeddings)

        return QueryResult(
            ids=all_ids,
            documents=all_documents,
            metadatas=all_metadatas,
            distances=all_distances,
            embeddings=all_embeddings,
        )

    def _query_one(
        self,
        *,
        query_embedding: list[float],
        n_results: int,
        where: Optional[dict],
        include_embeddings: bool,
    ) -> tuple[list[str], list[str], list[dict], list[float], list[list[float]]]:
        where_sql, where_params = self._where_to_sql(where)
        where_clause = (
            self._sql.SQL("WHERE {}").format(where_sql) if where_sql else self._sql.SQL("")
        )
        embedding_select = (
            self._sql.SQL(", embedding::text") if include_embeddings else self._sql.SQL("")
        )
        embedding = _vec_literal(query_embedding)

        cur = self._get_conn().cursor()
        cur.execute(
            self._sql.SQL(
                "SELECT id, document, wing, room, metadata, "
                "embedding <=> %s::{} AS distance{} "
                "FROM {} {} "
                "ORDER BY embedding <=> %s::{} "
                "LIMIT %s"
            ).format(
                self._vec_type_sql,
                embedding_select,
                self._table_id,
                where_clause,
                self._vec_type_sql,
            ),
            [embedding, *where_params, embedding, int(n_results)],
        )
        rows = cur.fetchall()

        result_ids: list[str] = []
        result_documents: list[str] = []
        result_metadatas: list[dict] = []
        result_distances: list[float] = []
        result_embeddings: list[list[float]] = []
        for row in rows:
            doc_id, document, wing, room, metadata, distance, *rest = row
            result_ids.append(doc_id)
            result_documents.append(document)
            result_metadatas.append(self._metadata_dict(wing, room, metadata))
            result_distances.append(float(distance))
            if include_embeddings:
                result_embeddings.append(_parse_vector_literal(rest[0] if rest else None))

        return (
            result_ids,
            result_documents,
            result_metadatas,
            result_distances,
            result_embeddings,
        )

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
        if where_document is not None:
            raise UnsupportedFilterError("PostgreSQL backend does not support where_document")
        if ids is not None and not ids:
            raise ValueError("Expected ids to be a non-empty list in get")
        self._ensure_setup(create=True)

        spec = _IncludeSpec.resolve(include, default_distances=False)
        clauses = []
        params: list[Any] = []
        if ids is not None:
            placeholders = self._sql.SQL(", ").join(self._sql.Placeholder() for _ in ids)
            clauses.append(self._sql.SQL("id IN ({})").format(placeholders))
            params.extend(ids)
        if where:
            where_sql, where_params = self._where_to_sql(where)
            if where_sql:
                clauses.append(where_sql)
                params.extend(where_params)

        where_clause = (
            self._sql.SQL("WHERE {}").format(self._sql.SQL(" AND ").join(clauses))
            if clauses
            else self._sql.SQL("")
        )
        limit_clause = self._sql.SQL("LIMIT %s") if limit else self._sql.SQL("")
        offset_clause = self._sql.SQL("OFFSET %s") if offset else self._sql.SQL("")
        if limit:
            params.append(int(limit))
        if offset:
            params.append(int(offset))
        embedding_select = (
            self._sql.SQL(", embedding::text") if spec.embeddings else self._sql.SQL("")
        )

        cur = self._get_conn().cursor()
        cur.execute(
            self._sql.SQL("SELECT id, document, wing, room, metadata{} FROM {} {} {} {}").format(
                embedding_select,
                self._table_id,
                where_clause,
                limit_clause,
                offset_clause,
            ),
            params,
        )
        rows = cur.fetchall()

        result_ids = [row[0] for row in rows]
        documents = [row[1] for row in rows] if spec.documents else []
        metadatas = (
            [self._metadata_dict(row[2], row[3], row[4]) for row in rows] if spec.metadatas else []
        )
        embeddings = [_parse_vector_literal(row[5]) for row in rows] if spec.embeddings else None
        return GetResult(
            ids=result_ids, documents=documents, metadatas=metadatas, embeddings=embeddings
        )

    def delete(
        self,
        *,
        ids: Optional[list[str]] = None,
        where: Optional[dict] = None,
    ) -> None:
        if ids is not None and not ids:
            raise ValueError("Expected ids to be a non-empty list in delete")
        if not ids and not where:
            return
        self._ensure_setup(create=True)

        clauses = []
        params: list[Any] = []
        if ids:
            placeholders = self._sql.SQL(", ").join(self._sql.Placeholder() for _ in ids)
            clauses.append(self._sql.SQL("id IN ({})").format(placeholders))
            params.extend(ids)
        if where:
            where_sql, where_params = self._where_to_sql(where)
            if where_sql:
                clauses.append(where_sql)
                params.extend(where_params)

        if not clauses:
            return
        cur = self._get_conn().cursor()
        cur.execute(
            self._sql.SQL("DELETE FROM {} WHERE {}").format(
                self._table_id, self._sql.SQL(" AND ").join(clauses)
            ),
            params,
        )

    def count(self) -> int:
        self._ensure_setup(create=True)
        cur = self._get_conn().cursor()
        # Public collection API: keep this exact. Use estimated_count() for
        # status/heuristic paths where stale PostgreSQL catalog stats are acceptable.
        cur.execute(self._sql.SQL("SELECT COUNT(*) FROM {}").format(self._table_id))
        return cur.fetchone()[0]

    def estimated_count(self) -> int:
        self._ensure_setup(create=True)
        return self._estimated_count()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------
    # Connection / DDL helpers
    # ------------------------------------------------------------------

    @property
    def _sql(self):
        _psycopg2, sql = _load_psycopg2()
        return sql

    @property
    def _table_id(self):
        return self._sql.Identifier(self.table_name)

    @property
    def _vec_type_sql(self):
        if not self._vec_type:
            raise RuntimeError("PostgreSQL vector type was not detected")
        return self._sql.SQL(self._vec_type)

    def _get_conn(self):
        psycopg2, _sql = _load_psycopg2()
        if self._conn is None or self._conn.closed:
            self._conn = psycopg2.connect(self.dsn)
            self._conn.autocommit = True
        return self._conn

    def _detect_extensions(self, *, create: bool = False) -> None:
        if self._vec_type:
            return

        cur = self._get_conn().cursor()
        cur.execute(
            "SELECT extname FROM pg_extension WHERE extname IN ('pg_sorted_heap', 'vector')"
        )
        installed = {row[0] for row in cur.fetchall()}

        if "pg_sorted_heap" in installed:
            self._vec_type = "svec"
            self._table_am = "sorted_heap"
            self._index_am = "sorted_hnsw"
        elif "vector" in installed:
            self._vec_type = "vector"
            self._table_am = "heap"
            self._index_am = "hnsw"
        elif create:
            for extension, vec_type, table_am, index_am in (
                ("pg_sorted_heap", "svec", "sorted_heap", "sorted_hnsw"),
                ("vector", "vector", "heap", "hnsw"),
            ):
                try:
                    cur.execute(
                        self._sql.SQL("CREATE EXTENSION IF NOT EXISTS {}").format(
                            self._sql.Identifier(extension)
                        )
                    )
                    self._vec_type = vec_type
                    self._table_am = table_am
                    self._index_am = index_am
                    break
                except Exception:
                    logger.debug(
                        "Could not create PostgreSQL extension %s", extension, exc_info=True
                    )
                    continue

        if not self._vec_type:
            raise RuntimeError(
                "PostgreSQL backend requires pgvector or pg_sorted_heap. "
                "Install one of them with CREATE EXTENSION before opening read-only collections."
            )

    def _open(self, *, create: bool) -> None:
        self._detect_extensions(create=create)
        if create:
            self._ensure_setup(create=True)
            return
        if not self._table_exists():
            raise PalaceNotFoundError(f"PostgreSQL collection does not exist: {self.table_name}")
        self._setup_done = True

    def _table_exists(self) -> bool:
        cur = self._get_conn().cursor()
        cur.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = %s",
            (self.table_name,),
        )
        return cur.fetchone() is not None

    def _ensure_setup(self, *, create: bool = True) -> None:
        if self._setup_done:
            return

        self._detect_extensions(create=create)
        if not create and not self._table_exists():
            raise PalaceNotFoundError(f"PostgreSQL collection does not exist: {self.table_name}")

        if create:
            cur = self._get_conn().cursor()
            self._create_table(cur)
        self._setup_done = True

    def _create_table(self, cur) -> None:
        if self._table_exists():
            return

        vec_type = self._sql.SQL("{}({})").format(
            self._vec_type_sql, self._sql.SQL(str(EMBEDDING_DIM))
        )
        if self._table_am == "sorted_heap":
            cur.execute(
                self._sql.SQL(
                    "CREATE TABLE {} ("
                    "wing text COLLATE \"C\" NOT NULL DEFAULT '', "
                    "room text COLLATE \"C\" NOT NULL DEFAULT '', "
                    "id text NOT NULL, "
                    "document text NOT NULL, "
                    "embedding {}, "
                    "metadata jsonb DEFAULT '{{}}', "
                    "PRIMARY KEY (wing, room, id)"
                    ") USING sorted_heap"
                ).format(self._table_id, vec_type)
            )
            cur.execute(
                self._sql.SQL("CREATE UNIQUE INDEX {} ON {} USING btree (id)").format(
                    self._sql.Identifier(f"{self.table_name}_id_idx"), self._table_id
                )
            )
        else:
            cur.execute(
                self._sql.SQL(
                    "CREATE TABLE {} ("
                    "id text PRIMARY KEY, "
                    "wing text NOT NULL DEFAULT '', "
                    "room text NOT NULL DEFAULT '', "
                    "document text NOT NULL, "
                    "embedding {}, "
                    "metadata jsonb DEFAULT '{{}}'"
                    ")"
                ).format(self._table_id, vec_type)
            )
            for column in ("wing", "room"):
                cur.execute(
                    self._sql.SQL("CREATE INDEX {} ON {} ({})").format(
                        self._sql.Identifier(f"{self.table_name}_{column}_idx"),
                        self._table_id,
                        self._sql.Identifier(column),
                    )
                )

        logger.info(
            "Created PostgreSQL collection %s (%s, %s)",
            self.table_name,
            self._table_am,
            self._vec_type,
        )

    def _maybe_create_vector_index(self, *, inserted_rows: int = 0) -> None:
        if self._vector_index_ready:
            return
        self._rows_since_index_check += inserted_rows
        if self._rows_since_index_check < VECTOR_INDEX_CHECK_INTERVAL_ROWS:
            return
        self._rows_since_index_check = 0

        cur = self._get_conn().cursor()
        index_name = f"{self.table_name}_vec_idx"
        ops = "svec_cosine_ops" if self._vec_type == "svec" else "vector_cosine_ops"
        if self._vector_index_exists(cur, index_name=index_name, ops=ops):
            self._vector_index_ready = True
            return

        if self._estimated_count() < VECTOR_INDEX_MIN_ROWS:
            return

        lock_key = f"vec_idx:{self.table_name}:{self._index_am}:{ops}"
        cur.execute(
            "SELECT pg_advisory_lock(hashtext('mempalace.postgres.vec_idx'), hashtext(%s))",
            (lock_key,),
        )
        try:
            if self._vector_index_exists(cur, index_name=index_name, ops=ops):
                self._vector_index_ready = True
                return
            cur.execute(
                self._sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} USING {} (embedding {})").format(
                    self._sql.Identifier(index_name),
                    self._table_id,
                    self._sql.SQL(self._index_am),
                    self._sql.SQL(ops),
                )
            )
            self._vector_index_ready = True
        finally:
            cur.execute(
                "SELECT pg_advisory_unlock(hashtext('mempalace.postgres.vec_idx'), hashtext(%s))",
                (lock_key,),
            )

    def _vector_index_exists(self, cur, *, index_name: str, ops: str) -> bool:
        cur.execute(
            """
            SELECT 1
            FROM pg_indexes
            WHERE schemaname = 'public'
              AND tablename = %s
              AND indexname = %s
            UNION ALL
            SELECT 1
            FROM pg_index i
            JOIN pg_class idx ON idx.oid = i.indexrelid
            JOIN pg_class tbl ON tbl.oid = i.indrelid
            JOIN pg_namespace ns ON ns.oid = tbl.relnamespace
            JOIN pg_am am ON am.oid = idx.relam
            JOIN pg_attribute att ON att.attrelid = tbl.oid AND att.attnum = i.indkey[0]
            JOIN pg_opclass opc ON opc.oid = i.indclass[0]
            WHERE ns.nspname = 'public'
              AND tbl.relname = %s
              AND am.amname = %s
              AND att.attname = 'embedding'
              AND opc.opcname = %s
              AND i.indisready
              AND i.indisvalid
            LIMIT 1
            """,
            (self.table_name, index_name, self.table_name, self._index_am, ops),
        )
        return cur.fetchone() is not None

    def _estimated_count(self) -> int:
        cur = self._get_conn().cursor()
        cur.execute(
            """
            SELECT GREATEST(
                COALESCE(c.reltuples, 0),
                COALESCE(s.n_live_tup, 0)
            )::bigint
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            LEFT JOIN pg_stat_all_tables s ON s.relid = c.oid
            WHERE n.nspname = 'public' AND c.relname = %s
            """,
            (self.table_name,),
        )
        row = cur.fetchone()
        if not row:
            return self._local_row_estimate
        return max(int(row[0]), self._local_row_estimate)

    def _where_to_sql(self, where: Optional[dict[str, Any]]):
        if not where:
            return None, []
        if not isinstance(where, dict):
            raise ValueError("PostgreSQL where filter must be a dictionary")

        if len(where) == 1 and next(iter(where)) in ("$and", "$or"):
            operator = next(iter(where))
            conditions = where[operator]
            if not isinstance(conditions, list) or not conditions:
                raise ValueError(f"PostgreSQL where operator {operator} requires a non-empty list")
            parts = []
            params = []
            for condition in conditions:
                clause, clause_params = self._where_to_sql(condition)
                if clause is None:
                    raise ValueError(
                        f"PostgreSQL where operator {operator} contains an empty filter"
                    )
                parts.append(self._sql.SQL("({})").format(clause))
                params.extend(clause_params)
            joiner = self._sql.SQL(" AND " if operator == "$and" else " OR ")
            return joiner.join(parts), params

        clauses = []
        params = []
        for key, value in where.items():
            if key.startswith("$"):
                raise UnsupportedFilterError(f"unsupported PostgreSQL where operator: {key}")
            clause, clause_params = self._field_filter_to_sql(key, value)
            clauses.append(clause)
            params.extend(clause_params)
        if not clauses:
            return None, []
        return self._sql.SQL(" AND ").join(clauses), params

    def _field_filter_to_sql(self, key: str, value: Any):
        if key in ("wing", "room"):
            lhs = self._sql.SQL("{}").format(self._sql.Identifier(key))
            lhs_params = []
        else:
            lhs = self._sql.SQL("metadata->>%s")
            lhs_params = [key]

        if not isinstance(value, dict):
            return self._sql.SQL("{} = %s").format(lhs), [*lhs_params, _metadata_value(value)]

        if len(value) != 1:
            raise ValueError(f"PostgreSQL where field {key!r} must contain exactly one operator")

        operator, operand = next(iter(value.items()))
        if operator == "$eq":
            return self._sql.SQL("{} = %s").format(lhs), [*lhs_params, _metadata_value(operand)]
        if operator == "$ne":
            return self._sql.SQL("{} <> %s").format(lhs), [*lhs_params, _metadata_value(operand)]
        if operator in ("$in", "$nin"):
            if not isinstance(operand, list) or not operand:
                raise ValueError(f"PostgreSQL where operator {operator} requires a non-empty list")
            placeholders = self._sql.SQL(", ").join(self._sql.Placeholder() for _ in operand)
            sql_operator = self._sql.SQL("IN" if operator == "$in" else "NOT IN")
            return (
                self._sql.SQL("{} {} ({})").format(lhs, sql_operator, placeholders),
                [*lhs_params, *(_metadata_value(item) for item in operand)],
            )
        raise UnsupportedFilterError(f"unsupported PostgreSQL where field operator: {operator}")

    @staticmethod
    def _metadata_dict(wing: str, room: str, metadata: Any) -> dict[str, Any]:
        result = dict(metadata) if isinstance(metadata, dict) else {}
        result["wing"] = wing
        result["room"] = room
        return result


class PostgresBackend(BaseBackend):
    """Factory for optional PostgreSQL collections."""

    name = "postgres"
    capabilities = frozenset(
        {
            "supports_embeddings_in",
            "supports_embeddings_passthrough",
            "supports_embeddings_out",
            "supports_metadata_filters",
            "server_mode",
        }
    )

    def __init__(self, dsn: Optional[str] = None):
        self.dsn = dsn or _dsn_from_env()
        self._collections: dict[tuple[str, str, str], PostgresCollection] = {}
        self._closed = False

    def get_collection(
        self,
        *,
        palace: PalaceRef,
        collection_name: str,
        create: bool = False,
        options: Optional[dict] = None,
    ) -> PostgresCollection:
        if self._closed:
            raise BackendClosedError("PostgresBackend has been closed")

        options = options or {}
        dsn = options.get("dsn") or self.dsn or _dsn_from_env()
        if not dsn:
            raise RuntimeError(
                "PostgreSQL backend selected but no DSN is configured. "
                "Set MEMPALACE_POSTGRES_DSN or MEMPALACE_PG_DSN."
            )
        table_name = options.get("table_name") or collection_name
        cache_key = (str(dsn), palace.id, table_name)

        collection = self._collections.get(cache_key)
        if collection is None:
            collection = PostgresCollection(str(dsn), table_name=table_name)
            collection._open(create=create)
            self._collections[cache_key] = collection
        elif create:
            collection._ensure_setup(create=True)
        return collection

    def close_palace(self, palace: PalaceRef) -> None:
        for key, collection in list(self._collections.items()):
            if key[1] == palace.id:
                collection.close()
                self._collections.pop(key, None)

    def close(self) -> None:
        for collection in self._collections.values():
            collection.close()
        self._collections.clear()
        self._closed = True

    def health(self, palace: Optional[PalaceRef] = None) -> HealthStatus:
        del palace
        if self._closed:
            return HealthStatus.unhealthy("backend closed")
        dsn = self.dsn or _dsn_from_env()
        if not dsn:
            return HealthStatus.unhealthy("missing PostgreSQL DSN")
        try:
            psycopg2, _sql = _load_psycopg2()
            conn = psycopg2.connect(dsn)
            try:
                cur = conn.cursor()
                cur.execute("SELECT 1")
            finally:
                conn.close()
        except Exception as exc:
            return HealthStatus.unhealthy(str(exc))
        return HealthStatus.healthy("PostgreSQL reachable")

    @classmethod
    def detect(cls, path: str) -> bool:
        del path
        return False


def _dsn_from_env() -> Optional[str]:
    return os.environ.get("MEMPALACE_POSTGRES_DSN") or os.environ.get("MEMPALACE_PG_DSN")
