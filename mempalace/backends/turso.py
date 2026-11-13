"""Turso (pyturso) storage backend for MemPalace.

An optional, fully local backend powered by the Turso database engine (a
ground-up rewrite of SQLite) via the ``pyturso`` package. One database file
per palace, no cloud account, no network. Search work is handed to the
database engine instead of Python:

* semantic search — ``vector_distance_cos()`` ranks inside a single SQL
  statement, so wing/room/file filters and distance ranking combine without
  loading the palace's embeddings into memory;
* lexical search — Tantivy-powered FTS via ``fts_match()`` / ``fts_score()``
  over a ``USING fts`` index that the engine maintains on every write;
* metadata filters — equality / ``$eq`` / ``$in`` / ``$and`` push into SQL
  through ``json_extract()`` expression indexes.

Concurrency is the other reason this backend exists: the Turso
engine's MVCC lets background hooks write while the MCP server searches,
without the WAL-sidecar protocol ``sqlite_exact`` needs to stay consistent.

Differences that shape this module (verified against pyturso 0.8.3):

* rows are plain tuples — no ``sqlite3.Row``; all access is positional;
* ``PRAGMA query_only`` / ``PRAGMA data_version`` do not exist, so there are
  no engine-level read-only connections and no snapshot-detection dance —
  MVCC readers keep a stable snapshot per statement and never see dirty
  writes. Read-only opens are therefore app-level: a separate handle cache,
  no schema initialization, and a mutation guard on every write path;
* ``VACUUM`` is incompatible with the experimental ``multiprocess_wal``
  feature, so ``compact`` maintenance is only advertised when the enabled
  experimental features allow it. The Tantivy segment merge is exposed as
  the backend-specific ``fts_optimize`` maintenance kind;
* the engine's exact-distance scan is always used — the DiskANN vector index
  (``libsql_vector_idx`` / ``vector_top_k``) is not compiled into pyturso
  builds today, and ``ORDER BY vector_distance_cos`` delivers the same
  recall (exact, not approximate) in a single query.

The package is optional: it is imported lazily inside :meth:`TursoBackend._connect`,
so the backend registers and detects without ``pyturso`` installed and only
fails with a clear message when actually opened.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import threading
from typing import Any, Optional

import numpy as np

from ..lexical import bm25_scores, tokenize
from ..stored_text import (
    decode_record,
    decode_value,
    encode_filter,
    prepare_stored_write,
)
from ..timeutil import recency_floor, recency_key, utc_now_iso as _utcnow
from ._filters import matches_where, matches_where_document, validate_where
from ._magic import has_sqlite_magic
from .base import (
    BackendClosedError,
    BackendError,
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
    UnsupportedMaintenanceKindError,
    _IncludeSpec,
    validate_write_batch,
)

logger = logging.getLogger(__name__)

_DB_FILENAME = "turso.db"
_FTS_INDEX = "idx_turso_documents_fts"
_SOURCE_FILE_INDEX = "idx_turso_source_file"
_FILED_AT_INDEX = "idx_turso_filed_at"
_WING_FILED_AT_INDEX = "idx_turso_wing_filed_at"
_LOCUS_INDEX = "idx_turso_locus"
# The FTS index needs ``index_method``; hooks and the MCP server are separate
# OS processes, which needs ``multiprocess_wal``. Override with the
# MEMPALACE_TURSO_EXPERIMENTAL_FEATURES environment variable (e.g. drop
# multiprocess_wal and add vacuum to enable compact maintenance).
_DEFAULT_EXPERIMENTAL_FEATURES = "index_method,multiprocess_wal"
_EXPERIMENTAL_FEATURES_ENV = "MEMPALACE_TURSO_EXPERIMENTAL_FEATURES"

_FACET_FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MAX_PUSHED_IN = 500
_MAX_IN_CHUNK = 900


def _experimental_features() -> str:
    """The experimental features pyturso connects with."""
    override = os.environ.get(_EXPERIMENTAL_FEATURES_ENV)
    return override if override else _DEFAULT_EXPERIMENTAL_FEATURES


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


def _decode_vector(blob: bytes | None) -> list[float]:
    if not blob:
        return []
    return np.frombuffer(blob, dtype=np.float32).astype(float).tolist()


def _json_field_sql(key: str) -> str:
    """SQL expression for a metadata key stored in ``metadata_json``."""
    return f"json_extract(metadata_json, '$.{key}')"


def _pushable_scalar(value) -> bool:
    return isinstance(value, (str, int, float)) and not (
        isinstance(value, float) and value != value
    )


def _equality_where_sql(where: Optional[dict]) -> Optional[tuple[str, list]]:
    """Push equality, ``$eq`` / ``$in`` and ``$and`` of them into SQL, else ``None``.

    ``None`` means the filter needs the Python ``matches_where`` path
    (``$or``, comparisons, ``$contains``, ``None`` operands, non-identifier keys).
    Same translation as ``sqlite_exact``, minus its generated locus columns.
    """
    if not where:
        return "1=1", []
    if not isinstance(where, dict):
        return None
    if list(where.keys()) == ["$and"]:
        clauses = []
        params: list = []
        for child in where["$and"] or []:
            part = _equality_where_sql(child)
            if part is None:
                return None
            sql, child_params = part
            clauses.append(f"({sql})")
            params.extend(child_params)
        return (" AND ".join(clauses) if clauses else "1=1"), params
    clauses = []
    params = []
    for key, expected in where.items():
        if key.startswith("$") or not _FACET_FIELD_RE.match(key):
            return None
        expr = _json_field_sql(key)
        if not isinstance(expected, dict):
            clauses.append(f"{expr} = ?")
            params.append(encode_filter(expected))
            continue
        # ``$eq`` / ``$in`` of scalars push down; ``None`` stays in Python,
        # where it also matches a missing key.
        if len(expected) != 1:
            return None
        ((op, operand),) = expected.items()
        if op == "$eq" and _pushable_scalar(operand):
            clauses.append(f"{expr} = ?")
            params.append(encode_filter(expected)["$eq"])
        elif (
            op == "$in"
            and isinstance(operand, (list, tuple))
            and len(operand) <= _MAX_PUSHED_IN
            and all(_pushable_scalar(value) for value in operand)
        ):
            if not operand:
                clauses.append("0=1")
                continue
            clauses.append(f"{expr} IN ({','.join('?' for _ in operand)})")
            params.extend(encode_filter(expected)["$in"])
        else:
            return None
    return (" AND ".join(clauses) if clauses else "1=1"), params


def _fts_query_string(query: str) -> str:
    """A Tantivy query that OR-joins the query's tokens, each as a literal.

    Tokens are quoted so characters the Tantivy query parser reserves
    (``-``, ``^``, ``:"`` ...) cannot escape the literal or flip the boolean
    structure of the search. Tokens that contain anything outside
    ``\\w`` (Unicode word characters) are dropped whole rather than
    mutated: stripping characters would corrupt accented terms
    (``łukasz`` must not become ``ukasz``). ``tokenize`` already yields
    ``\\w``-only tokens, so the filter is a guard, and a word-character
    token can never contain the quote or backslash that would let it break
    out of the SQL literal the result is embedded into.
    """
    tokens = [token for token in tokenize(query) if len(token) >= 2 and re.fullmatch(r"\w+", token)]
    if not tokens:
        return ""
    return " OR ".join(f'"{token}"' for token in tokens)


class _TursoHandle:
    """One live pyturso connection to a palace, shared by its collections.

    ``read_only`` handles never initialize schema and reject every mutation
    at the app level (the engine has no ``PRAGMA query_only``); MVCC still
    gives each statement a fresh view of committed writes from the writable
    connection and other processes.
    """

    def __init__(
        self,
        conn,
        lock: threading.RLock,
        palace_path: str,
        *,
        read_only: bool = False,
    ):
        self.conn = conn
        self.lock = lock
        self.palace_path = palace_path
        self.read_only = read_only
        self.closed = False


class TursoCollection(BaseCollection):
    def __init__(
        self,
        handle: _TursoHandle,
        collection_name: str,
        backend: Optional[TursoBackend] = None,
    ):
        self._handle = handle
        self._collection_name = collection_name
        self._backend = backend
        self._closed = False

    def _ensure_open(self) -> None:
        if self._closed or self._handle.closed:
            raise BackendClosedError("TursoCollection has been closed")

    @contextlib.contextmanager
    def _write_lock(self):
        """Serialize this handle before taking process-wide writer ownership.

        The handle mutex must be the outer context for the same reason as
        ``sqlite_exact``: a waiting thread must not consume the re-entrant
        credit of :func:`mempalace.palace.mine_palace_lock` and outlive the
        thread that owns the OS lease.
        """
        if self._handle.read_only:
            raise BackendError("read-only turso collection cannot be written")
        # Late import avoids a palace.py -> backend -> palace.py cycle.
        from ..palace import mine_palace_lock

        with self._handle.lock:
            self._ensure_open()
            with mine_palace_lock(self._handle.palace_path):
                yield

    @contextlib.contextmanager
    def _cursor(self, *, write: bool = False):
        self._ensure_open()
        serialization = self._write_lock() if write else self._handle.lock
        with serialization:
            self._ensure_open()
            cur = self._handle.conn.cursor()
            try:
                yield cur
            except Exception:
                with contextlib.suppress(Exception):
                    self._handle.conn.rollback()
                raise
            else:
                if write:
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
                f"turso collection {self._collection_name!r} cannot mix "
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
                f"turso collection {self._collection_name!r} expects "
                f"embedding dimension {stored}, got {dim}"
            )

    def _fts_available(self, cur) -> bool:
        row = cur.execute("SELECT value FROM meta WHERE key = 'fts_available'").fetchone()
        return bool(row and row[0] == "1")

    def _embedder_meta_key(self) -> str:
        return f"embedder_model:{self._collection_name}"

    # ------------------------------------------------------------------
    # Embedder identity (RFC 001)
    # ------------------------------------------------------------------

    def get_stored_embedder_identity(self) -> Optional[EmbedderIdentity]:
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

    def set_embedder_identity(self, identity: EmbedderIdentity) -> None:
        if not identity or not identity.model_name:
            return
        with self._cursor(write=True) as cur:
            cur.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (self._embedder_meta_key(), str(identity.model_name)),
            )

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def add(self, *, documents, ids, metadatas=None, embeddings=None):
        validate_write_batch(ids, documents=documents, metadatas=metadatas, embeddings=embeddings)
        if embeddings is None:
            raise ValueError("turso requires explicit embeddings")
        documents, ids, metadatas = prepare_stored_write(
            documents, ids, metadatas or [{} for _ in ids]
        )
        now = _utcnow()
        with self._cursor(write=True) as cur:
            collection_id = self._collection_id(cur)
            prepared = []
            for doc_id, doc, meta, emb in zip(ids, documents, metadatas, embeddings):
                arr = _as_vector_array(emb)
                prepared.append((doc_id, doc, meta, arr.tobytes(), int(arr.size)))
            self._ensure_collection_dimension(cur, collection_id, [item[4] for item in prepared])
            cur.executemany(
                """
                INSERT INTO documents
                    (collection_id, id, document, metadata_json, embedding, dim, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (collection_id, doc_id, doc, _json_dumps(meta), blob, dim, now, now)
                    for doc_id, doc, meta, blob, dim in prepared
                ],
            )

    def upsert(self, *, documents, ids, metadatas=None, embeddings=None):
        validate_write_batch(ids, documents=documents, metadatas=metadatas, embeddings=embeddings)
        if embeddings is None:
            raise ValueError("turso requires explicit embeddings")
        documents, ids, metadatas = prepare_stored_write(
            documents, ids, metadatas or [{} for _ in ids]
        )
        now = _utcnow()
        with self._cursor(write=True) as cur:
            collection_id = self._collection_id(cur)
            prepared = []
            for doc_id, doc, meta, emb in zip(ids, documents, metadatas, embeddings):
                arr = _as_vector_array(emb)
                prepared.append((doc_id, doc, meta, arr.tobytes(), int(arr.size)))
            self._ensure_collection_dimension(cur, collection_id, [item[4] for item in prepared])
            cur.executemany(
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
                [
                    (collection_id, doc_id, doc, _json_dumps(meta), blob, dim, now, now)
                    for doc_id, doc, meta, blob, dim in prepared
                ],
            )

    def update(self, *, ids, documents=None, metadatas=None, embeddings=None):
        if documents is None and metadatas is None and embeddings is None:
            raise ValueError("update requires at least one of documents, metadatas, embeddings")
        validate_write_batch(ids, documents=documents, metadatas=metadatas, embeddings=embeddings)
        documents, ids, metadatas = prepare_stored_write(documents, ids, metadatas)
        with self._cursor(write=True) as cur:
            collection_id = self._collection_id(cur)
            if documents is None and embeddings is None:
                # Metadata-only update (a wing or room move): merge the JSON
                # and leave document and embedding alone. The FTS index keys
                # on ``document`` only, so it is not touched either.
                now = _utcnow()
                params = []
                for idx, doc_id in enumerate(ids):
                    row = cur.execute(
                        "SELECT metadata_json FROM documents WHERE collection_id = ? AND id = ?",
                        (collection_id, doc_id),
                    ).fetchone()
                    if row is None:
                        continue
                    meta = _json_loads(row[0])
                    meta.update(metadatas[idx] or {})
                    params.append((_json_dumps(meta), now, collection_id, doc_id))
                if params:
                    cur.executemany(
                        "UPDATE documents SET metadata_json = ?, updated_at = ? "
                        "WHERE collection_id = ? AND id = ?",
                        params,
                    )
                return
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
                updates.append((doc, meta, emb_blob, dim, collection_id, doc_id))
            if embeddings is not None:
                self._ensure_collection_dimension(cur, collection_id, [item[3] for item in updates])
            if updates:
                cur.executemany(
                    """
                    UPDATE documents
                    SET document = ?, metadata_json = ?, embedding = ?, dim = ?, updated_at = ?
                    WHERE collection_id = ? AND id = ?
                    """,
                    [
                        (doc, _json_dumps(meta), blob, dim, _utcnow(), cid, doc_id)
                        for doc, meta, blob, dim, cid, doc_id in updates
                    ],
                )

    def delete(self, *, ids=None, where=None):
        with self._cursor(write=True) as cur:
            collection_id = self._collection_id(cur)
            if ids is None:
                rows = self._rows(cur, where=where)
                ids = [row["id"] for row in rows]
            target_ids = list(ids or [])
            if not target_ids:
                return
            for start in range(0, len(target_ids), _MAX_IN_CHUNK):
                chunk = target_ids[start : start + _MAX_IN_CHUNK]
                placeholders = ",".join("?" for _ in chunk)
                cur.execute(
                    f"DELETE FROM documents WHERE collection_id = ? AND id IN ({placeholders})",
                    (collection_id, *chunk),
                )

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def _rows(
        self,
        cur,
        *,
        where=None,
        where_document=None,
        limit=None,
        offset=None,
        spec: Optional[_IncludeSpec] = None,
    ) -> list[dict]:
        validate_where(where)
        validate_where(where_document)
        spec = spec or _IncludeSpec.resolve(None, default_distances=False)
        collection_id = self._collection_id(cur)
        push = None if where_document else _equality_where_sql(where)
        python_where = where is not None and push is None
        need_doc = spec.documents or bool(where_document)
        need_meta = spec.metadatas or python_where or need_doc
        need_emb = spec.embeddings
        cols = ["id"]
        if need_doc:
            cols.append("document")
        if need_meta:
            cols.append("metadata_json")
        if need_emb:
            cols.append("embedding")
        sql = f"SELECT {', '.join(cols)}\nFROM documents\nWHERE collection_id = ?"
        params: list = [collection_id]
        if push is not None and where:
            extra_sql, extra_params = push
            sql += " AND " + extra_sql
            params.extend(extra_params)
        sql += "\nORDER BY rowid"
        # Equality where is applied in SQL, so LIMIT/OFFSET are safe there too.
        # Python-side filters still have to scan, then slice.
        can_limit = (not python_where) and not where_document
        if can_limit and (limit is not None or offset):
            if limit is not None:
                sql += "\nLIMIT ?"
                params.append(int(limit))
            elif offset:
                sql += "\nLIMIT -1"
            if offset:
                sql += "\nOFFSET ?"
                params.append(int(offset))
        out = []
        for row in cur.execute(sql, params).fetchall():
            idx = 1
            doc = ""
            meta: dict = {}
            emb_blob = None
            if need_doc:
                doc = row[idx] or ""
                idx += 1
            if need_meta:
                doc, meta = decode_record(doc, _json_loads(row[idx]))
                idx += 1
            if need_emb:
                emb_blob = row[idx]
            if python_where and not matches_where(meta, where):
                continue
            if where_document and not matches_where_document(doc, where_document):
                continue
            out.append(
                {
                    "id": row[0],
                    "document": doc,
                    "metadata": meta,
                    "embedding": emb_blob,
                }
            )
        return out

    def _rows_by_ids(self, cur, collection_id: int, ids: list[str], spec: _IncludeSpec) -> dict:
        """Fetch requested columns for ``ids`` via ``IN``, keyed by id."""
        unique = []
        seen = set()
        for doc_id in ids:
            if doc_id not in seen:
                seen.add(doc_id)
                unique.append(doc_id)
        if not unique:
            return {}
        need_meta = spec.documents or spec.metadatas
        cols = ["id"]
        if spec.documents:
            cols.append("document")
        if need_meta:
            cols.append("metadata_json")
        if spec.embeddings:
            cols.append("embedding")
        by_id: dict = {}
        for start in range(0, len(unique), _MAX_IN_CHUNK):
            chunk = unique[start : start + _MAX_IN_CHUNK]
            placeholders = ",".join("?" for _ in chunk)
            for row in cur.execute(
                f"SELECT {', '.join(cols)} FROM documents "
                f"WHERE collection_id = ? AND id IN ({placeholders})",
                (collection_id, *chunk),
            ).fetchall():
                idx = 1
                doc = ""
                meta: dict = {}
                emb_blob = None
                if spec.documents:
                    doc = row[idx] or ""
                    idx += 1
                if need_meta:
                    doc, meta = decode_record(doc, _json_loads(row[idx]))
                    idx += 1
                if spec.embeddings:
                    emb_blob = row[idx]
                by_id[row[0]] = {
                    "id": row[0],
                    "document": doc,
                    "metadata": meta,
                    "embedding": emb_blob,
                }
        return by_id

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
        # get(ids=...) must not scan the collection: look up by primary key.
        if ids is not None:
            validate_where(where)
            validate_where(where_document)
            lookup_spec = _IncludeSpec(
                documents=spec.documents or bool(where_document),
                metadatas=spec.metadatas or bool(where),
                distances=False,
                embeddings=spec.embeddings,
            )
            with self._cursor() as cur:
                collection_id = self._collection_id(cur)
                by_id = self._rows_by_ids(cur, collection_id, list(ids), lookup_spec)
            rows = [
                by_id[doc_id]
                for doc_id in ids
                if doc_id in by_id
                and matches_where(by_id[doc_id]["metadata"], where)
                and matches_where_document(by_id[doc_id]["document"], where_document)
            ]
            if offset:
                rows = rows[offset:]
            if limit is not None:
                rows = rows[:limit]
            return self._get_result(rows, spec)

        # Unfiltered (or SQL-pushable equality) pages: LIMIT/OFFSET in SQL.
        python_where = bool(where_document) or (
            where is not None and _equality_where_sql(where) is None
        )
        push_page = (
            not python_where
            and (limit is None or limit >= 0)
            and (offset is None or offset >= 0)
            and (limit is not None or offset)
        )
        with self._cursor() as cur:
            if push_page:
                rows = self._rows(
                    cur,
                    where=where,
                    where_document=where_document,
                    limit=limit,
                    offset=offset,
                    spec=spec,
                )
            else:
                rows = self._rows(cur, where=where, where_document=where_document, spec=spec)
        if not push_page:
            if offset:
                rows = rows[offset:]
            if limit is not None:
                rows = rows[:limit]
        return self._get_result(rows, spec)

    def get_recent(self, *, limit, where=None, order_field="filed_at", include=None):
        """Newest ``limit`` records by the instant in ``order_field``, read in SQL.

        Two timestamp forms (UTC-suffixed and the rest) are read separately,
        bounded below by a text floor computed from the textually newest
        values, and merged by :func:`mempalace.timeutil.recency_key`: valid
        timestamps order by instant (so naive local and UTC forms interleave
        and mixed offsets/fractional precision compare by instant, not text),
        unparseable strings follow in descending text order, and missing /
        non-string values come last in storage order. Equal instants
        tie-break by id, deterministically.

        The merge reads are NOT truncated per form: a per-form ``LIMIT``
        ordered by *text* would drop a textually-smaller row whose *instant*
        is newer (e.g. a ``-02:00`` offset beating a ``+02:00`` one), so the
        reads stop at the floor only and the Python merge does the top-k.
        The read is bounded by the rows at or above the floor (within a day
        of the newest candidates), plus up to ``limit`` records without the
        field. Filters that SQL cannot evaluate fall back to the base
        implementation.
        """
        if limit is None or limit <= 0:
            return super().get_recent(
                limit=limit, where=where, order_field=order_field, include=include
            )
        validate_where(where)
        push = _equality_where_sql(where)
        if push is None or not _FACET_FIELD_RE.match(order_field):
            return super().get_recent(
                limit=limit, where=where, order_field=order_field, include=include
            )
        spec = _IncludeSpec.resolve(include, default_distances=False)
        filter_sql, filter_params = push
        ts = _json_field_sql(order_field)
        dated = f"typeof({ts}) = 'text' AND {ts} > ''"
        utc = f"(substr({ts}, -6) = '+00:00' OR substr({ts}, -1) IN ('Z', 'z'))"
        with self._cursor() as cur:
            collection_id = self._collection_id(cur)

            def read(condition, condition_params, order, cap=None):
                sql = (
                    f"SELECT id, {ts} FROM documents"
                    f" WHERE collection_id = ? AND ({filter_sql}) AND {condition}"
                    f" ORDER BY {order}"
                )
                params = [collection_id, *filter_params, *condition_params]
                if cap is not None:
                    sql += " LIMIT ?"
                    params.append(cap)
                return cur.execute(sql, params).fetchall()

            # The floor candidates are read with a text-ordered LIMIT purely
            # to bound the floor computation; they are re-read below.
            newest = read(dated, [], f"{ts} DESC", cap=limit)
            floor = recency_floor(r[1] for r in newest) if len(newest) == limit else ""
            # No per-form LIMIT here: see the docstring. The floor bound caps
            # the scan; recency_key decides the top-k in the merge below.
            rows = read(f"{dated} AND {ts} >= ? AND {utc}", [floor], f"{ts} DESC, id") + read(
                f"{dated} AND {ts} >= ? AND NOT {utc}", [floor], f"{ts} DESC, id"
            )
            rows.sort(key=lambda r: r[0])
            rows.sort(key=lambda r: recency_key(r[1]), reverse=True)
            ids = [r[0] for r in rows[:limit]]
            if len(ids) < limit:
                ids += [r[0] for r in read(f"NOT ({dated})", [], "rowid", cap=limit)][
                    : limit - len(ids)
                ]
            by_id = self._rows_by_ids(cur, collection_id, ids, spec)
        return self._get_result([by_id[i] for i in ids if i in by_id], spec)

    def get_all_metadata(self, where: Optional[dict] = None) -> list[dict]:
        """Every matching drawer's metadata in one streaming pass."""
        if where is None:
            with self._cursor() as cur:
                collection_id = self._collection_id(cur)
                return [
                    decode_record("", _json_loads(raw))[1]
                    for (raw,) in cur.execute(
                        "SELECT metadata_json FROM documents WHERE collection_id = ? ORDER BY rowid",
                        (collection_id,),
                    ).fetchall()
                ]
        return super().get_all_metadata(where=where)

    def get_all_rows(
        self, where: Optional[dict] = None, include: Optional[list[str]] = None
    ) -> GetResult:
        """Every matching record -- ids plus ``include``d fields -- in one pass."""
        include = list(include) if include else ["metadatas"]
        want_docs = "documents" in include
        want_meta = "metadatas" in include
        if where is not None:
            push = _equality_where_sql(where)
            if push is None:
                # Non-pushable filter: let the paged base implementation do
                # the Python-side matching.
                return super().get_all_rows(where=where, include=include)
        else:
            push = ("1=1", [])
        with self._cursor() as cur:
            collection_id = self._collection_id(cur)
            sql = "SELECT id"
            params: list = [collection_id]
            if want_docs:
                sql += ", document"
            if want_meta or want_docs:
                sql += ", metadata_json"
            sql += f" FROM documents WHERE collection_id = ? AND ({push[0]}) ORDER BY rowid"
            params.extend(push[1])
            ids: list[str] = []
            documents: list = []
            metadatas: list = []
            for row in cur.execute(sql, params).fetchall():
                ids.append(row[0])
                idx = 1
                doc = ""
                meta: dict = {}
                if want_docs:
                    doc = row[idx] or ""
                    idx += 1
                if want_meta or want_docs:
                    # decode_record returns verbatim document and metadata and
                    # never leaks the internal escape flag; it needs both the
                    # stored document and the raw (possibly flagged) metadata.
                    doc, meta = decode_record(doc, _json_loads(row[idx]))
                if want_docs:
                    documents.append(doc)
                if want_meta:
                    metadatas.append(meta)
        if not want_meta:
            metadatas = []
        if not want_docs:
            documents = []
        return GetResult(ids=ids, documents=documents, metadatas=metadatas, embeddings=None)

    def count(self) -> int:
        with self._cursor() as cur:
            collection_id = self._collection_id(cur)
            row = cur.execute(
                "SELECT COUNT(*) FROM documents WHERE collection_id = ?",
                (collection_id,),
            ).fetchone()
            return int(row[0]) if row else 0

    def facet_counts(
        self,
        field: str,
        where: Optional[dict] = None,
        limit: int = 1000,
    ) -> dict[str, int]:
        validate_where(where)
        if not _FACET_FIELD_RE.match(field):
            raise BackendError(f"facet field {field!r} is not a JSON key")
        push = _equality_where_sql(where)
        if push is None:
            raise BackendError("facet_counts does not support local-only filters")
        extra_sql, params = push
        with self._cursor() as cur:
            collection_id = self._collection_id(cur)
            expr = _json_field_sql(field)
            rows = cur.execute(
                f"""
                SELECT {expr} AS k, COUNT(*)
                FROM documents
                WHERE collection_id = ?
                  AND {expr} IS NOT NULL
                  AND ({extra_sql})
                GROUP BY 1
                ORDER BY 2 DESC, 1
                LIMIT ?
                """,
                (collection_id, *params, int(limit)),
            ).fetchall()
        return {str(decode_value(key)): int(count) for key, count in rows if key is not None}

    def iter_metadata(
        self,
        keys: Optional[Any] = None,
        *,
        require_key: Optional[str] = None,
        equals: Optional[dict] = None,
    ):
        """Stream every drawer's metadata from turso in one linear pass.

        Matches :meth:`SQLiteExactCollection.iter_metadata`: with ``keys``,
        only those keys are returned (a drawer with none of them is skipped);
        ``require_key`` limits the scan to drawers with a string value under
        that key; ``equals`` limits it to drawers whose string value under
        each key equals the given one, pushed into SQL when possible.
        """
        key_tuple = tuple(keys) if keys is not None else None
        equals_dict = dict(equals) if equals else None

        def rows():
            with self._cursor() as cur:
                collection_id = self._collection_id(cur)
                clauses = ["collection_id = ?"]
                params: list[Any] = [collection_id]
                multi_key: Optional[str] = None
                multi_vals: Optional[list[str]] = None
                # True only when every ``equals`` entry is enforced in SQL.
                fully_pushed = True
                if equals_dict:
                    for k, v in equals_dict.items():
                        if isinstance(v, (list, tuple, set, frozenset)) and not v:
                            return  # empty membership list matches nothing
                        if _FACET_FIELD_RE.match(k) and isinstance(v, str):
                            clauses.append(f"{_json_field_sql(k)} = ?")
                            params.append(encode_filter(v))
                        elif (
                            _FACET_FIELD_RE.match(k)
                            and multi_key is None
                            and isinstance(v, (list, tuple, set, frozenset))
                            and all(isinstance(x, str) for x in v)
                        ):
                            multi_key = k
                            multi_vals = list(dict.fromkeys(encode_filter(value) for value in v))
                        else:
                            fully_pushed = False
                if (
                    fully_pushed
                    and key_tuple == ("source_file",)
                    and require_key == "source_file"
                    and multi_key is None
                ):
                    sf_expr = _json_field_sql("source_file")
                    clauses.append(f"{sf_expr} IS NOT NULL")
                    where_sql = " AND ".join(clauses)
                    for (sf,) in cur.execute(
                        f"SELECT {sf_expr} FROM documents WHERE {where_sql}", params
                    ).fetchall():
                        if isinstance(sf, str):
                            yield {"source_file": decode_value(sf)}
                    return
                where_sql = " AND ".join(clauses)
                if multi_key is not None and multi_vals is not None:
                    m_expr = _json_field_sql(multi_key)
                    chunks = (
                        multi_vals[start : start + _MAX_IN_CHUNK]
                        for start in range(0, len(multi_vals), _MAX_IN_CHUNK)
                    )
                else:
                    chunks = (None,)
                for chunk in chunks:
                    sql = f"SELECT metadata_json FROM documents WHERE {where_sql}"
                    query_params = params
                    if chunk is not None:
                        ph = ",".join("?" for _ in chunk)
                        sql += f" AND {m_expr} IN ({ph})"
                        query_params = (*params, *chunk)
                    for (raw,) in cur.execute(sql, query_params).fetchall():
                        # decode_record returns the caller-form metadata and
                        # strips the internal escape flag.
                        meta = decode_record("", _json_loads(raw))[1]
                        if require_key is not None and not isinstance(meta.get(require_key), str):
                            continue
                        if equals_dict and not all(
                            isinstance(meta.get(k), str)
                            and (
                                meta.get(k) == v
                                if isinstance(v, str)
                                else isinstance(v, (list, tuple, set, frozenset))
                                and meta.get(k) in v
                            )
                            for k, v in equals_dict.items()
                        ):
                            continue
                        if key_tuple is not None:
                            projected = {k: meta[k] for k in key_tuple if k in meta}
                            if any(v is not None for v in projected.values()):
                                yield projected
                        else:
                            yield meta if meta else None

        return rows()

    # ------------------------------------------------------------------
    # Semantic search
    # ------------------------------------------------------------------

    def query(self, **kwargs) -> QueryResult:
        return self._query_once(**kwargs)

    def _query_once(
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
            raise ValueError("turso requires query_embeddings; use palace.get_collection wrapper")
        if query_embeddings is None:
            raise ValueError("query requires query_embeddings")
        if not query_embeddings:
            raise ValueError("query input must be a non-empty list")

        validate_where(where)
        validate_where(where_document)
        spec = _IncludeSpec.resolve(include, default_distances=True)
        outer_ids: list[list[str]] = []
        outer_docs: list[list[str]] = []
        outer_metas: list[list[dict]] = []
        outer_dists: list[list[float]] = []
        outer_embeds: list[list[list[float]]] = []
        n_results = max(0, int(n_results))

        with self._cursor() as cur:
            collection_id = self._collection_id(cur)
            expected_dim = self._collection_dimension(cur, collection_id)
            push = _equality_where_sql(where)
            # Fully-SQL path: filter + rank + LIMIT in one statement, executed
            # by the engine (its core property: no Python-side vector math,
            # no full embedding load). Fallback path for filters SQL cannot
            # evaluate ($or, $contains, ...): one scan computes distances for
            # every row and Python filters and ranks the matches.
            in_sql = push is not None and not where_document

            for query_vector in query_embeddings:
                q = _as_vector_array(query_vector)
                if expected_dim is not None and int(q.size) != expected_dim:
                    raise DimensionMismatchError(
                        f"turso collection {self._collection_name!r} expects "
                        f"embedding dimension {expected_dim}, got {int(q.size)}"
                    )
                if n_results == 0:
                    outer_ids.append([])
                    outer_docs.append([])
                    outer_metas.append([])
                    outer_dists.append([])
                    if spec.embeddings:
                        outer_embeds.append([])
                    continue

                sql = (
                    "SELECT id, document, metadata_json, embedding,"
                    " vector_distance_cos(embedding, ?) AS distance"
                    "\nFROM documents\nWHERE collection_id = ?"
                )
                params: list = [q.tobytes(), collection_id]
                if in_sql and where:
                    extra_sql, extra_params = push
                    sql += " AND " + extra_sql
                    params.extend(extra_params)
                if in_sql:
                    sql += "\nORDER BY distance ASC, rowid ASC LIMIT ?"
                    params.append(n_results)
                else:
                    # Distance for every row; Python filters below. The scan
                    # cost matches sqlite_exact's full-matrix load, without
                    # the numpy round-trip.
                    sql += "\nORDER BY rowid"
                raw = cur.execute(sql, params).fetchall()
                matched = []
                for row in raw:
                    doc, meta = decode_record(row[1] or "", _json_loads(row[2]))
                    if not in_sql:
                        if where and not matches_where(meta, where):
                            continue
                        if where_document and not matches_where_document(doc, where_document):
                            continue
                    matched.append((row[0], doc, meta, row[3], float(row[4])))
                if not in_sql:
                    matched.sort(key=lambda item: item[4])
                    matched = matched[:n_results]

                outer_ids.append([item[0] for item in matched])
                outer_docs.append([item[1] for item in matched] if spec.documents else [])
                outer_metas.append([item[2] for item in matched] if spec.metadatas else [])
                outer_dists.append([item[4] for item in matched] if spec.distances else [])
                if spec.embeddings:
                    outer_embeds.append([_decode_vector(item[3]) for item in matched])

        return QueryResult(
            ids=outer_ids,
            documents=outer_docs,
            metadatas=outer_metas,
            distances=outer_dists,
            embeddings=outer_embeds if spec.embeddings else None,
        )

    # ------------------------------------------------------------------
    # Lexical search
    # ------------------------------------------------------------------

    def lexical_search(self, *, query: str, n_results: int = 10, where: Optional[dict] = None):
        validate_where(where)
        with self._cursor() as cur:
            hits = self._lexical_search_fts(cur, query=query, n_results=n_results, where=where)
            if hits is not None:
                return LexicalResult(hits=hits)
            rows = self._rows(cur, where=where)
        scores = bm25_scores(query, [row["document"] for row in rows])
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
        fts_query = _fts_query_string(query)
        if not fts_query:
            return None
        collection_id = self._collection_id(cur)
        try:
            # Tantivy BM25 via fts_score: higher = more relevant (unlike
            # SQLite FTS5's bm25(), which is lower = more relevant). The
            # query string is embedded as an inline literal, not a bound
            # parameter: the planner only routes fts_match through the FTS
            # index (and only the index yields nonzero fts_score) when the
            # query is constant at plan time. _fts_query_string reduces the
            # query to quoted [a-z0-9_] tokens, which cannot break out of
            # the SQL literal. The ORDER BY sits OUTSIDE a subquery: when
            # fts_match combines with another predicate (collection_id),
            # the planner's scan path silently drops an inline ORDER BY on
            # fts_score, but sorts the subquery's rows correctly.
            limit_sql = "" if where else "LIMIT ?"
            params: list = [collection_id]
            if not where:
                params.append(max(n_results * 5, n_results))
            rows = cur.execute(
                f"""
                SELECT id, score FROM (
                    SELECT id, fts_score(document, '{fts_query}') AS score
                    FROM documents
                    WHERE fts_match(document, '{fts_query}') AND collection_id = ?
                )
                ORDER BY score DESC
                {limit_sql}
                """,
                params,
            ).fetchall()
        except Exception:
            logger.debug("turso FTS query failed; using Python lexical scan", exc_info=True)
            return None
        if not rows:
            return []
        ids = [row[0] for row in rows]
        by_id = {}
        for start in range(0, len(ids), _MAX_IN_CHUNK):
            chunk_ids = ids[start : start + _MAX_IN_CHUNK]
            placeholders = ",".join("?" for _ in chunk_ids)
            for doc_id, doc, meta_json in cur.execute(
                f"""
                SELECT id, document, metadata_json
                FROM documents
                WHERE collection_id = ? AND id IN ({placeholders})
                """,
                (collection_id, *chunk_ids),
            ).fetchall():
                by_id[doc_id] = decode_record(doc or "", _json_loads(meta_json))
        hits = []
        for doc_id, score in rows:
            doc_meta = by_id.get(doc_id)
            if doc_meta is None:
                continue
            doc, meta = doc_meta
            if not matches_where(meta, where):
                continue
            hits.append(LexicalHit(id=doc_id, document=doc, metadata=meta, score=float(score)))
            if len(hits) >= n_results:
                break
        return hits

    # ------------------------------------------------------------------
    # Maintenance / lifecycle
    # ------------------------------------------------------------------

    def maintenance_state(self) -> dict:
        state: dict = {"row_count": self.count(), "vector_index": None}
        try:
            with self._cursor() as cur:
                page_count = cur.execute("PRAGMA page_count").fetchone()
                freelist = cur.execute("PRAGMA freelist_count").fetchone()
                fts_present = cur.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?",
                    (_FTS_INDEX,),
                ).fetchone()
            state["page_count"] = int(page_count[0]) if page_count else 0
            state["freelist_pages"] = int(freelist[0]) if freelist else 0
            state["fts_index"] = bool(fts_present)
            db_path = TursoBackend._db_path(self._handle.palace_path)
            file_token = []
            for suffix in ("", "-wal"):
                path = f"{db_path}{suffix}"
                try:
                    stat = os.stat(path)
                    file_token.append([suffix or "db", stat.st_size, stat.st_mtime_ns])
                except FileNotFoundError:
                    file_token.append([suffix or "db", None, None])
            state["consistency_token"] = {"files": file_token}
        except Exception:
            pass
        return state

    def run_maintenance(self, kind: str) -> MaintenanceResult:
        if kind not in self._backend.maintenance_kinds:
            raise UnsupportedMaintenanceKindError(
                f"turso does not support maintenance kind {kind!r}"
            )
        with self._cursor(write=True) as cur:
            if kind == "analyze":
                # Refresh planner stats. Concurrent runs serialize on the
                # handle lock.
                cur.execute("ANALYZE")
                return MaintenanceResult(kind="analyze", status="ran")
            if kind == "fts_optimize":
                # Merge the Tantivy FTS segments and flush tombstones left by
                # DELETE/UPDATE -- the documented post-bulk-import step.
                cur.execute(f"OPTIMIZE INDEX {_FTS_INDEX}")
                return MaintenanceResult(kind="fts_optimize", status="ran")
        assert kind == "compact"
        # VACUUM cannot run inside a transaction. Serialize writers first
        # (compact rewrites the database file), then flip the connection to
        # autocommit for the duration.
        before = self.maintenance_state()
        with self._write_lock():
            conn = self._handle.conn
            prev_isolation = conn.isolation_level
            try:
                conn.commit()
                conn.isolation_level = None
                conn.execute("VACUUM")
            finally:
                with contextlib.suppress(Exception):
                    conn.isolation_level = prev_isolation
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

    def close(self) -> None:
        self._closed = True

    def health(self) -> HealthStatus:
        if self._closed or self._handle.closed:
            return HealthStatus.unhealthy("collection closed")
        return HealthStatus.healthy()

    @staticmethod
    def _get_result(rows: list[dict], spec: _IncludeSpec) -> GetResult:
        return GetResult(
            ids=[row["id"] for row in rows],
            documents=[row["document"] for row in rows] if spec.documents else [],
            metadatas=[row["metadata"] for row in rows] if spec.metadatas else [],
            embeddings=(
                [_decode_vector(row["embedding"]) for row in rows] if spec.embeddings else None
            ),
        )


class TursoBackend(BaseBackend):
    name = "turso"
    capabilities = frozenset(
        {
            "requires_explicit_embeddings",
            "supports_embeddings_in",
            "supports_embeddings_passthrough",
            "supports_embeddings_out",
            "supports_metadata_filters",
            "supports_metadata_facets",
            "supports_lexical_search",
            "supports_recency_order",
            "local_mode",
        }
    )
    # ``compact`` (VACUUM) is only available when the enabled experimental
    # features allow it; see :meth:`__init__`. ``fts_optimize`` maps to
    # ``OPTIMIZE INDEX``, merging the engine's Tantivy FTS segments — a
    # backend-specific kind, not a full ``reindex`` analogue.
    maintenance_kinds = frozenset({"analyze", "compact", "fts_optimize"})

    def __init__(self):
        self._clients: dict[str, _TursoHandle] = {}
        self._read_only_clients: dict[str, _TursoHandle] = {}
        self._clients_lock = threading.RLock()
        self._closed = False
        # VACUUM is incompatible with the multiprocess WAL that concurrent
        # hook + MCP server access needs, so compact is only advertised when
        # the enabled features permit it.
        features = [f.strip() for f in _experimental_features().split(",") if f.strip()]
        if "vacuum" in features and "multiprocess_wal" not in features:
            self.maintenance_kinds = frozenset({"analyze", "compact", "fts_optimize"})
        else:
            self.maintenance_kinds = frozenset({"analyze", "fts_optimize"})

    @staticmethod
    def _db_path(palace_path: str) -> str:
        return os.path.join(palace_path, _DB_FILENAME)

    @staticmethod
    def _features() -> str:
        return _experimental_features()

    def _connect(self, palace_path: str, create: bool, *, read_only: bool = False):
        if self._closed:
            raise BackendClosedError("TursoBackend has been closed")
        if create and read_only:
            raise ValueError("turso read-only opens cannot create a palace")
        db_path = self._db_path(palace_path)
        # A read-only open must not create anything: refuse before connecting.
        if not create and not os.path.isfile(db_path):
            raise PalaceNotFoundError(db_path)
        if create:
            os.makedirs(palace_path, exist_ok=True)
            try:
                os.chmod(palace_path, 0o700)
            except (OSError, NotImplementedError):
                pass
        # Hold the registry lock across cache-check + connect (+ schema init
        # on a writable open): two threads first-opening the same palace must
        # not each create a connection nor run _init_schema concurrently on
        # a fresh file.
        with self._clients_lock:
            if self._closed:
                raise BackendClosedError("TursoBackend has been closed")
            clients = self._read_only_clients if read_only else self._clients
            cached = clients.get(palace_path)
            if cached is not None and not cached.closed:
                return cached
            try:
                import turso  # optional dependency; lazy by design
            except ImportError as exc:
                raise BackendError(
                    "the turso backend requires the optional pyturso package; "
                    "install it with: uv sync --extra turso "
                    '(or pip install "mempalace[turso]")'
                ) from exc
            conn = turso.connect(db_path, experimental_features=self._features())
            try:
                lock = threading.RLock()
                handle = _TursoHandle(conn, lock, palace_path, read_only=read_only)
                with handle.lock:
                    if read_only:
                        # A read-only open never initializes or upgrades
                        # schema, creates collections, or bumps any index:
                        # it must not mutate the palace in any way.
                        pass
                    else:
                        from ..palace import mine_palace_lock

                        with mine_palace_lock(palace_path):
                            self._init_schema(conn)
            except BaseException:
                with contextlib.suppress(Exception):
                    conn.close()
                raise
            clients[palace_path] = handle
            return handle

    def _init_schema(self, conn) -> None:
        conn.executescript(
            f"""
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
                embedding F32_BLOB,
                dim INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (collection_id, id)
            );
            CREATE INDEX IF NOT EXISTS idx_turso_documents_collection
                ON documents(collection_id);
            CREATE INDEX IF NOT EXISTS {_SOURCE_FILE_INDEX}
                ON documents(collection_id, (json_extract(metadata_json, '$.source_file')));
            CREATE INDEX IF NOT EXISTS {_FILED_AT_INDEX}
                ON documents(collection_id, (json_extract(metadata_json, '$.filed_at')), id);
            CREATE INDEX IF NOT EXISTS {_WING_FILED_AT_INDEX}
                ON documents(collection_id, (json_extract(metadata_json, '$.wing')),
                             (json_extract(metadata_json, '$.filed_at')), id);
            CREATE INDEX IF NOT EXISTS {_LOCUS_INDEX}
                ON documents(collection_id, (json_extract(metadata_json, '$.wing')),
                             (json_extract(metadata_json, '$.room')));
            """
        )
        # The FTS index needs the experimental ``index_method`` feature; when
        # the engine refuses it, lexical_search falls back to Python BM25.
        try:
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS {_FTS_INDEX} ON documents USING fts (document)"
            )
            conn.execute(
                "INSERT INTO meta(key, value) VALUES ('fts_available', '1') "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
            )
        except Exception:
            logger.warning(
                "turso FTS index unavailable; lexical search falls back to Python BM25",
                exc_info=True,
            )
            conn.execute(
                "INSERT INTO meta(key, value) VALUES ('fts_available', '0') "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
            )
        conn.commit()

    def get_collection(
        self,
        *args,
        **kwargs,
    ) -> TursoCollection:
        palace, collection_name, create, read_only = self._normalize_args(args, kwargs)
        self.require_namespace_support(palace)
        palace_path = palace.local_path
        if palace_path is None:
            raise PalaceNotFoundError("TursoBackend requires PalaceRef.local_path")
        if not create and not os.path.isdir(palace_path):
            raise PalaceNotFoundError(palace_path)
        handle = self._connect(palace_path, create=create, read_only=read_only)
        with handle.lock:
            row = handle.conn.execute(
                "SELECT id FROM collections WHERE name = ?",
                (collection_name,),
            ).fetchone()
            if row is None:
                if not create:
                    raise CollectionNotInitializedError(collection_name)
                # The read-only guard in _connect makes create+read_only
                # unreachable; a read-only open never reaches this insert.
                from ..palace import mine_palace_lock

                with mine_palace_lock(palace_path):
                    handle.conn.execute(
                        "INSERT INTO collections(name, created_at) VALUES (?, ?)",
                        (collection_name, _utcnow()),
                    )
                    handle.conn.commit()
        return TursoCollection(handle, collection_name, backend=self)

    @staticmethod
    def _normalize_args(args, kwargs):
        read_only = False
        options = kwargs.pop("options", None) or {}
        read_only = bool(options.get("read_only", False))
        if "palace" in kwargs:
            palace = kwargs.pop("palace")
            if not isinstance(palace, PalaceRef):
                raise TypeError("palace= must be a PalaceRef instance")
            collection_name = kwargs.pop("collection_name")
            create = bool(kwargs.pop("create", False))
            if args or kwargs:
                raise TypeError("unexpected arguments to get_collection")
            return palace, collection_name, create, read_only
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
            return (
                PalaceRef(id=palace_path, local_path=palace_path),
                collection_name,
                bool(create),
                read_only,
            )
        if "palace_path" in kwargs:
            palace_path = kwargs.pop("palace_path")
            collection_name = kwargs.pop("collection_name")
            create = bool(kwargs.pop("create", False))
            if kwargs:
                raise TypeError("unexpected arguments to get_collection")
            return (
                PalaceRef(id=palace_path, local_path=palace_path),
                collection_name,
                create,
                read_only,
            )
        raise TypeError("get_collection requires palace= or a positional palace_path")

    def close_palace(self, palace: PalaceRef | str) -> None:
        path = palace.local_path if isinstance(palace, PalaceRef) else palace
        if path is None:
            return
        with self._clients_lock:
            cached_handles = [
                self._clients.pop(path, None),
                self._read_only_clients.pop(path, None),
            ]
        for cached in cached_handles:
            if cached is not None:
                with cached.lock:
                    cached.closed = True
                    with contextlib.suppress(Exception):
                        cached.conn.close()

    def close(self) -> None:
        with self._clients_lock:
            handles = list(self._clients.values()) + list(self._read_only_clients.values())
            self._clients.clear()
            self._read_only_clients.clear()
            self._closed = True
        for handle in handles:
            with handle.lock:
                handle.closed = True
                with contextlib.suppress(Exception):
                    handle.conn.close()

    def health(self, palace: Optional[PalaceRef] = None) -> HealthStatus:
        if self._closed:
            return HealthStatus.unhealthy("backend closed")
        if palace and palace.local_path and not os.path.isfile(self._db_path(palace.local_path)):
            return HealthStatus.unhealthy("turso database not found")
        return HealthStatus.healthy()

    @classmethod
    def detect(cls, path: str) -> bool:
        """True when ``path`` holds a turso palace.

        Checks the ``turso.db`` file header the engine actually wrote — the
        same magic check ``sqlite_exact`` uses, on a distinct filename so the
        two backends never claim each other's palaces.
        """
        return has_sqlite_magic(os.path.join(path, _DB_FILENAME))

    def create_collection(self, palace_path: str, collection_name: str) -> TursoCollection:
        return self.get_collection(palace_path, collection_name, create=True)

    def get_or_create_collection(self, palace_path: str, collection_name: str):
        return self.get_collection(palace_path, collection_name, create=True)

    def delete_collection(self, palace_path: str, collection_name: str) -> None:
        handle = self._connect(palace_path, create=False)
        with handle.lock:
            from ..palace import mine_palace_lock

            with mine_palace_lock(palace_path):
                row = handle.conn.execute(
                    "SELECT id FROM collections WHERE name = ?",
                    (collection_name,),
                ).fetchone()
                if row is None:
                    raise CollectionNotInitializedError(collection_name)
                collection_id = int(row[0])
                handle.conn.execute(
                    "DELETE FROM documents WHERE collection_id = ?",
                    (collection_id,),
                )
                handle.conn.execute("DELETE FROM collections WHERE id = ?", (collection_id,))
                handle.conn.commit()


__all__ = ["TursoBackend", "TursoCollection"]
