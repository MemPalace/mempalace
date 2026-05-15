"""sqlite-vec backend for MemPalace (RFC 001).

Per-palace SQLite database stored at ``<palace_path>/sqlite_vec.db`` with:

* ``collections``    — registry of collections (name, dimension, embedder).
* ``items``          — shared items table (collection, id, document, metadata_json).
* ``items_vec_<col>`` — one vec0 virtual table per collection (dimension fixed).

Embedding policy: caller MUST supply ``embeddings=`` in add/upsert and
``query_embeddings=`` in query. ``query_texts`` is rejected — embedder
injection is out of scope for Fase A (per base.py docstring: follow-up PR).

Where translator supports MemPalace's spec operators ($eq, $ne, $in, $nin,
$and, $or, $contains, $gt, $gte, $lt, $lte) via SQLite json_extract.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import struct
from typing import Any, ClassVar, Optional

import sqlite_vec as _sqlite_vec_ext

from .base import (
    BackendClosedError,
    BaseBackend,
    BaseCollection,
    DimensionMismatchError,
    GetResult,
    HealthStatus,
    PalaceNotFoundError,
    PalaceRef,
    QueryResult,
    UnsupportedFilterError,
    _IncludeSpec,
)

logger = logging.getLogger(__name__)

DB_FILENAME = "sqlite_vec.db"

_REQUIRED_OPERATORS = frozenset({"$eq", "$ne", "$in", "$nin", "$and", "$or", "$contains"})
_OPTIONAL_OPERATORS = frozenset({"$gt", "$gte", "$lt", "$lte"})
_SUPPORTED_OPERATORS = _REQUIRED_OPERATORS | _OPTIONAL_OPERATORS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sanitize_table_suffix(name: str) -> str:
    """Sanitize collection name into safe SQL identifier suffix."""
    sanitized = re.sub(r"[^a-zA-Z0-9_]", "_", name)
    if not sanitized:
        sanitized = "_anon"
    elif sanitized[0].isdigit():
        sanitized = f"c_{sanitized}"
    return sanitized[:48]


def _pack_vec(values) -> bytes:
    if not isinstance(values, (list, tuple)):
        values = list(values)
    return struct.pack(f"{len(values)}f", *values)


def _unpack_vec(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


# ---------------------------------------------------------------------------
# Where translator (MemPalace dict → SQL via json_extract)
# ---------------------------------------------------------------------------


def _translate_where(where: dict, *, alias: str = "i") -> tuple[str, list[Any]]:
    """Translate a MemPalace where dict to a parameterized SQL fragment.

    Returns (sql_fragment, params). Empty dict → ('', []).
    Raises UnsupportedFilterError for unknown operators (spec §1.4).
    """
    if not where:
        return "", []
    sql, params = _translate_node(where, alias=alias)
    return sql, params


def _translate_node(node: Any, *, alias: str) -> tuple[str, list[Any]]:
    """Recursive translator. ``node`` is either a dict at filter level or
    a key→value pair we expand. Returns ('', []) when node yields nothing."""
    if not isinstance(node, dict):
        raise UnsupportedFilterError(f"where expects dict, got {type(node).__name__}")

    # Logical combinators at top level: {"$and": [...]} or {"$or": [...]}
    if "$and" in node or "$or" in node:
        if len(node) != 1:
            raise UnsupportedFilterError(
                f"logical operator must be the sole key, got siblings: {list(node)!r}"
            )
        op = "$and" if "$and" in node else "$or"
        sub_nodes = node[op]
        if not isinstance(sub_nodes, list) or not sub_nodes:
            raise UnsupportedFilterError(f"{op} requires non-empty list")
        parts: list[str] = []
        all_params: list[Any] = []
        for sub in sub_nodes:
            s, p = _translate_node(sub, alias=alias)
            if s:
                parts.append(f"({s})")
                all_params.extend(p)
        joiner = " AND " if op == "$and" else " OR "
        return joiner.join(parts), all_params

    # Implicit AND across keys
    parts: list[str] = []
    all_params: list[Any] = []
    for key, val in node.items():
        if key.startswith("$"):
            raise UnsupportedFilterError(
                f"operator {key!r} not allowed at field level (use field: {{operator: value}})"
            )
        s, p = _translate_field(key, val, alias=alias)
        if s:
            parts.append(f"({s})")
            all_params.extend(p)
    return " AND ".join(parts), all_params


def _field_expr(field: str, *, alias: str) -> str:
    """Return SQL expression for accessing field — either column or json_extract."""
    if field == "document":
        return f"{alias}.document"
    if field == "id":
        return f"{alias}.id"
    # Default: read from metadata_json
    return f"json_extract({alias}.metadata_json, '$.{field}')"


def _translate_field(field: str, value: Any, *, alias: str) -> tuple[str, list[Any]]:
    """Translate {field: value-or-operator-dict} pair."""
    expr = _field_expr(field, alias=alias)

    # Plain scalar → $eq
    if not isinstance(value, dict):
        return f"{expr} = ?", [value]

    # Operator dict
    parts: list[str] = []
    params: list[Any] = []
    for op, op_val in value.items():
        if op not in _SUPPORTED_OPERATORS:
            raise UnsupportedFilterError(f"operator {op!r} not supported by sqlite-vec backend")
        if op == "$eq":
            parts.append(f"{expr} = ?")
            params.append(op_val)
        elif op == "$ne":
            parts.append(f"({expr} IS NULL OR {expr} != ?)")
            params.append(op_val)
        elif op == "$in":
            if not isinstance(op_val, (list, tuple)) or not op_val:
                raise UnsupportedFilterError("$in requires non-empty list")
            placeholders = ",".join("?" for _ in op_val)
            parts.append(f"{expr} IN ({placeholders})")
            params.extend(op_val)
        elif op == "$nin":
            if not isinstance(op_val, (list, tuple)) or not op_val:
                raise UnsupportedFilterError("$nin requires non-empty list")
            placeholders = ",".join("?" for _ in op_val)
            parts.append(f"({expr} IS NULL OR {expr} NOT IN ({placeholders}))")
            params.extend(op_val)
        elif op == "$contains":
            parts.append(f"{expr} LIKE ?")
            params.append(f"%{op_val}%")
        elif op == "$gt":
            parts.append(f"{expr} > ?")
            params.append(op_val)
        elif op == "$gte":
            parts.append(f"{expr} >= ?")
            params.append(op_val)
        elif op == "$lt":
            parts.append(f"{expr} < ?")
            params.append(op_val)
        elif op == "$lte":
            parts.append(f"{expr} <= ?")
            params.append(op_val)
    return " AND ".join(parts), params


def _translate_where_document(where_document: dict, *, alias: str = "i") -> tuple[str, list[Any]]:
    """Translate where_document — only $contains supported."""
    if not where_document:
        return "", []
    if "$contains" not in where_document or len(where_document) != 1:
        raise UnsupportedFilterError(
            f"where_document only supports {{'$contains': str}}, got {where_document!r}"
        )
    return f"{alias}.document LIKE ?", [f"%{where_document['$contains']}%"]


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


class SQLiteVecCollection(BaseCollection):
    def __init__(
        self,
        *,
        db: sqlite3.Connection,
        palace_path: str,
        name: str,
        dimension: int,
        embedder=None,
    ):
        self._db = db
        self._palace_path = palace_path
        self._name = name
        self._dimension = dimension
        self._vec_table = f"items_vec_{_sanitize_table_suffix(name)}"
        self._closed = False
        # Optional embedder for query_texts / docs-only add convenience.
        # If None, callers MUST supply embeddings explicitly.
        self._embedder = embedder

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _check(self) -> None:
        if self._closed:
            raise BackendClosedError(f"collection {self._name!r} is closed")

    def _ensure_dim(self, embeddings) -> None:
        for emb in embeddings:
            if len(emb) != self._dimension:
                raise DimensionMismatchError(
                    f"embedding dim {len(emb)} != collection dim {self._dimension}"
                )

    def _require_embeddings(self, embeddings, documents=None):
        """Either embeddings are supplied or we can derive them from documents
        via a configured embedder. Returns the resolved embeddings list."""
        if embeddings is not None:
            return embeddings
        if self._embedder is not None and documents is not None:
            return self._embedder(list(documents))
        raise ValueError(
            "sqlite-vec backend requires explicit embeddings; "
            "supply embeddings= or configure the backend with an embedder"
        )

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def add(self, *, documents, ids, metadatas=None, embeddings=None):
        self._check()
        embeddings = self._require_embeddings(embeddings, documents=documents)
        if len(documents) != len(ids) or len(embeddings) != len(ids):
            raise ValueError("documents, ids, embeddings must have the same length")
        if metadatas is not None and len(metadatas) != len(ids):
            raise ValueError("metadatas length must match ids")
        self._ensure_dim(embeddings)

        cur = self._db.cursor()
        try:
            cur.execute("BEGIN")
            for i, (id_, doc, emb) in enumerate(zip(ids, documents, embeddings)):
                meta_json = json.dumps(metadatas[i]) if metadatas is not None else None
                cur.execute(
                    "INSERT INTO items(collection, id, document, metadata_json) VALUES (?, ?, ?, ?)",
                    (self._name, id_, doc, meta_json),
                )
                rowid = cur.lastrowid
                cur.execute(
                    f"INSERT INTO {self._vec_table}(rowid, embedding) VALUES (?, ?)",
                    (rowid, _pack_vec(emb)),
                )
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise

    def upsert(self, *, documents, ids, metadatas=None, embeddings=None):
        self._check()
        embeddings = self._require_embeddings(embeddings, documents=documents)
        if len(documents) != len(ids) or len(embeddings) != len(ids):
            raise ValueError("documents, ids, embeddings must have the same length")
        if metadatas is not None and len(metadatas) != len(ids):
            raise ValueError("metadatas length must match ids")
        self._ensure_dim(embeddings)

        cur = self._db.cursor()
        try:
            cur.execute("BEGIN")
            for i, (id_, doc, emb) in enumerate(zip(ids, documents, embeddings)):
                meta_json = json.dumps(metadatas[i]) if metadatas is not None else None
                row = cur.execute(
                    "SELECT rowid FROM items WHERE collection = ? AND id = ?",
                    (self._name, id_),
                ).fetchone()
                if row is not None:
                    rowid = row[0]
                    cur.execute(
                        "UPDATE items SET document = ?, metadata_json = ?, updated_at = CURRENT_TIMESTAMP WHERE rowid = ?",
                        (doc, meta_json, rowid),
                    )
                    cur.execute(
                        f"DELETE FROM {self._vec_table} WHERE rowid = ?", (rowid,)
                    )
                    cur.execute(
                        f"INSERT INTO {self._vec_table}(rowid, embedding) VALUES (?, ?)",
                        (rowid, _pack_vec(emb)),
                    )
                else:
                    cur.execute(
                        "INSERT INTO items(collection, id, document, metadata_json) VALUES (?, ?, ?, ?)",
                        (self._name, id_, doc, meta_json),
                    )
                    rowid = cur.lastrowid
                    cur.execute(
                        f"INSERT INTO {self._vec_table}(rowid, embedding) VALUES (?, ?)",
                        (rowid, _pack_vec(emb)),
                    )
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise

    def update(
        self,
        *,
        ids,
        documents=None,
        metadatas=None,
        embeddings=None,
    ):
        """Atomic partial update — preserves embeddings if not supplied.

        Overrides the base default (get+merge+upsert) so callers can update
        document/metadata without re-embedding.
        """
        self._check()
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
        if embeddings is not None:
            self._ensure_dim(embeddings)

        cur = self._db.cursor()
        try:
            cur.execute("BEGIN")
            for i, id_ in enumerate(ids):
                row = cur.execute(
                    "SELECT rowid, document, metadata_json FROM items WHERE collection = ? AND id = ?",
                    (self._name, id_),
                ).fetchone()
                if row is None:
                    # Spec doesn't mandate behaviour for missing ids on update;
                    # match Chroma (silent skip) rather than raising.
                    continue
                rowid, prev_doc, prev_meta_json = row

                new_doc = documents[i] if documents is not None else prev_doc
                if metadatas is not None:
                    prev_meta = json.loads(prev_meta_json) if prev_meta_json else {}
                    prev_meta.update(metadatas[i] or {})
                    new_meta_json = json.dumps(prev_meta)
                else:
                    new_meta_json = prev_meta_json

                cur.execute(
                    "UPDATE items SET document = ?, metadata_json = ?, updated_at = CURRENT_TIMESTAMP WHERE rowid = ?",
                    (new_doc, new_meta_json, rowid),
                )
                if embeddings is not None:
                    cur.execute(f"DELETE FROM {self._vec_table} WHERE rowid = ?", (rowid,))
                    cur.execute(
                        f"INSERT INTO {self._vec_table}(rowid, embedding) VALUES (?, ?)",
                        (rowid, _pack_vec(embeddings[i])),
                    )
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def query(
        self,
        *,
        query_texts=None,
        query_embeddings=None,
        n_results: int = 10,
        where=None,
        where_document=None,
        include=None,
    ) -> QueryResult:
        self._check()
        if query_embeddings is None:
            if query_texts is None:
                raise ValueError("query requires query_embeddings or query_texts")
            if self._embedder is None:
                raise ValueError(
                    "sqlite-vec backend has no embedder configured; supply query_embeddings"
                )
            if not query_texts:
                raise ValueError("query input must be a non-empty list")
            query_embeddings = self._embedder(list(query_texts))
        if not query_embeddings:
            raise ValueError("query input must be a non-empty list")

        spec = _IncludeSpec.resolve(include, default_distances=True)

        where_sql, where_params = _translate_where(where or {}, alias="i")
        doc_sql, doc_params = _translate_where_document(where_document or {}, alias="i")

        # Over-retrieve to compensate for post-filtering. vec0 MATCH+k returns
        # top-k by distance; if we then filter with WHERE, we may end up with
        # < n_results. Strategy: ask vec0 for max(n_results * 5, 50) candidates
        # and LIMIT n_results after the WHERE applies.
        over_k = max(n_results * 5, 50)

        all_ids: list[list[str]] = []
        all_docs: list[list[str]] = []
        all_metas: list[list[dict]] = []
        all_dists: list[list[float]] = []
        all_embs: Optional[list[list[list[float]]]] = [] if spec.embeddings else None

        for qemb in query_embeddings:
            if len(qemb) != self._dimension:
                raise DimensionMismatchError(
                    f"query embedding dim {len(qemb)} != collection dim {self._dimension}"
                )

            clauses = ["v.embedding MATCH ?", "k = ?", "i.collection = ?"]
            params: list[Any] = [_pack_vec(qemb), over_k, self._name]
            if where_sql:
                clauses.append(where_sql)
                params.extend(where_params)
            if doc_sql:
                clauses.append(doc_sql)
                params.extend(doc_params)

            select_emb = ", v.embedding" if spec.embeddings else ", NULL AS embedding"
            sql = f"""
                SELECT i.id, i.document, i.metadata_json, v.distance{select_emb}
                FROM {self._vec_table} v
                JOIN items i ON i.rowid = v.rowid
                WHERE {' AND '.join(clauses)}
                ORDER BY v.distance
                LIMIT ?
            """
            params.append(n_results)
            rows = self._db.execute(sql, params).fetchall()

            qids: list[str] = []
            qdocs: list[str] = []
            qmetas: list[dict] = []
            qdists: list[float] = []
            qembs: list[list[float]] = []
            for id_, doc, meta_json, dist, emb_blob in rows:
                qids.append(id_)
                qdocs.append(doc or "")
                qmetas.append(json.loads(meta_json) if meta_json else {})
                qdists.append(float(dist))
                if spec.embeddings:
                    qembs.append(_unpack_vec(emb_blob) if emb_blob else [])

            all_ids.append(qids)
            all_docs.append(qdocs)
            all_metas.append(qmetas)
            all_dists.append(qdists)
            if spec.embeddings:
                all_embs.append(qembs)

        # Even when caller didn't include a field, RFC 001 §1.3 says the outer
        # shape must be preserved with empty inner lists. We populate inner
        # lists from the rows (cheap), but only return them via the typed
        # result when spec.<field> is True; otherwise we return shape-only
        # placeholders. Distances default to True per QueryResult contract.
        def _shape_only(rows_outer: list[list[str]]) -> list[list]:
            return [[] for _ in rows_outer]

        return QueryResult(
            ids=all_ids,
            documents=all_docs if spec.documents else _shape_only(all_ids),
            metadatas=all_metas if spec.metadatas else _shape_only(all_ids),
            distances=all_dists if spec.distances else _shape_only(all_ids),
            embeddings=all_embs,
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
        self._check()
        spec = _IncludeSpec.resolve(include, default_distances=False)

        clauses = ["i.collection = ?"]
        params: list[Any] = [self._name]

        if ids:
            placeholders = ",".join("?" for _ in ids)
            clauses.append(f"i.id IN ({placeholders})")
            params.extend(ids)
        if where:
            wsql, wparams = _translate_where(where, alias="i")
            if wsql:
                clauses.append(wsql)
                params.extend(wparams)
        if where_document:
            dsql, dparams = _translate_where_document(where_document, alias="i")
            if dsql:
                clauses.append(dsql)
                params.extend(dparams)

        select_fields = ["i.id", "i.rowid"]
        select_fields.append("i.document" if spec.documents else "NULL")
        select_fields.append("i.metadata_json" if spec.metadatas else "NULL")

        sql = f"SELECT {', '.join(select_fields)} FROM items i WHERE {' AND '.join(clauses)} ORDER BY i.rowid"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
            if offset is not None:
                sql += f" OFFSET {int(offset)}"
        elif offset is not None:
            sql += f" LIMIT -1 OFFSET {int(offset)}"

        rows = self._db.execute(sql, params).fetchall()

        out_ids: list[str] = []
        out_docs: list[str] = []
        out_metas: list[dict] = []
        out_embs: Optional[list[list[float]]] = [] if spec.embeddings else None

        for id_, rowid, doc, meta_json in rows:
            out_ids.append(id_)
            if spec.documents:
                out_docs.append(doc or "")
            if spec.metadatas:
                out_metas.append(json.loads(meta_json) if meta_json else {})
            if spec.embeddings:
                emb_row = self._db.execute(
                    f"SELECT embedding FROM {self._vec_table} WHERE rowid = ?", (rowid,)
                ).fetchone()
                out_embs.append(_unpack_vec(emb_row[0]) if emb_row and emb_row[0] else [])

        return GetResult(
            ids=out_ids,
            documents=out_docs if spec.documents else [""] * len(out_ids),
            metadatas=out_metas if spec.metadatas else [{}] * len(out_ids),
            embeddings=out_embs,
        )

    def delete(self, *, ids=None, where=None):
        self._check()
        if ids is None and where is None:
            raise ValueError("delete requires ids or where")

        clauses = ["collection = ?"]
        params: list[Any] = [self._name]

        if ids is not None:
            if not ids:
                return
            placeholders = ",".join("?" for _ in ids)
            clauses.append(f"id IN ({placeholders})")
            params.extend(ids)
        if where:
            # Use alias 'items' here since this query doesn't use 'i'
            wsql, wparams = _translate_where(where, alias="items")
            if wsql:
                clauses.append(wsql)
                params.extend(wparams)

        cur = self._db.cursor()
        rowids = [
            r[0]
            for r in cur.execute(
                f"SELECT rowid FROM items WHERE {' AND '.join(clauses)}", params
            ).fetchall()
        ]
        if not rowids:
            return

        try:
            cur.execute("BEGIN")
            placeholders = ",".join("?" for _ in rowids)
            cur.execute(
                f"DELETE FROM {self._vec_table} WHERE rowid IN ({placeholders})", rowids
            )
            cur.execute(f"DELETE FROM items WHERE rowid IN ({placeholders})", rowids)
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise

    def count(self) -> int:
        self._check()
        row = self._db.execute(
            "SELECT COUNT(*) FROM items WHERE collection = ?", (self._name,)
        ).fetchone()
        return int(row[0]) if row else 0

    def close(self) -> None:
        self._closed = True

    def health(self) -> HealthStatus:
        try:
            self._db.execute("SELECT 1").fetchone()
            self._db.execute(f"SELECT COUNT(*) FROM {self._vec_table} LIMIT 1").fetchone()
            return HealthStatus.healthy(detail=f"sqlite_vec collection {self._name}")
        except Exception as e:
            return HealthStatus.unhealthy(detail=f"sqlite_vec collection {self._name}: {e!r}")


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


class SQLiteVecBackend(BaseBackend):
    name: ClassVar[str] = "sqlite-vec"
    spec_version: ClassVar[str] = "1.0"
    capabilities: ClassVar[frozenset[str]] = frozenset({"supports_update", "supports_filter"})

    def __init__(self, embedder=None):
        """Construct a backend.

        ``embedder`` semantics:

        * ``None`` (default) — lazy-resolve via
          :func:`mempalace.embedding.get_embedding_function` on first
          :meth:`get_collection`. Allows ``query_texts`` / docs-only ``add``.
        * ``False`` — explicitly disable embedder. Callers MUST supply
          ``embeddings`` / ``query_embeddings`` themselves. Used by tests
          and by callers that pre-embed at a different layer.
        * callable — use as-is (typically a Chroma-style ``EmbeddingFunction``).
        """
        # palace_path -> sqlite3.Connection (long-lived per palace)
        self._connections: dict[str, sqlite3.Connection] = {}
        self._closed = False
        self._embedder = embedder
        self._embedder_resolved = embedder is False or callable(embedder)

    def _resolve_embedder(self):
        if self._embedder_resolved:
            # ``False`` means "no embedder by design"; collection sees None
            # so add/query without embeddings raises ValueError.
            return self._embedder if callable(self._embedder) else None
        try:
            from ..embedding import get_embedding_function
            self._embedder = get_embedding_function()
        except Exception:
            logger.exception("Failed to build embedding function; query_texts will fail")
            self._embedder = None
        self._embedder_resolved = True
        return self._embedder

    def _open_db(self, palace_path: str) -> sqlite3.Connection:
        if self._closed:
            raise BackendClosedError("backend closed")
        cached = self._connections.get(palace_path)
        if cached is not None:
            return cached

        os.makedirs(palace_path, exist_ok=True)
        db_path = os.path.join(palace_path, DB_FILENAME)
        db = sqlite3.connect(db_path, check_same_thread=False)
        db.enable_load_extension(True)
        _sqlite_vec_ext.load(db)
        db.enable_load_extension(False)

        # Bootstrap schema
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS collections (
                name TEXT PRIMARY KEY,
                dimension INT NOT NULL,
                embedder_name TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS items (
                rowid INTEGER PRIMARY KEY AUTOINCREMENT,
                collection TEXT NOT NULL,
                id TEXT NOT NULL,
                document TEXT,
                metadata_json TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(collection, id),
                FOREIGN KEY (collection) REFERENCES collections(name) ON DELETE CASCADE
            )
            """
        )
        db.execute("CREATE INDEX IF NOT EXISTS idx_items_collection ON items(collection)")
        db.commit()

        self._connections[palace_path] = db
        return db

    def get_collection(self, *args, **kwargs) -> BaseCollection:
        if self._closed:
            raise BackendClosedError("backend closed")

        # Accept both new-style (palace=PalaceRef, ...) and legacy positional
        # (palace_path_str, collection_name, create) to match ChromaBackend's
        # transition shim.
        if "palace" in kwargs:
            palace: PalaceRef = kwargs.pop("palace")
            if not isinstance(palace, PalaceRef):
                raise TypeError("palace= must be a PalaceRef instance")
            collection_name: str = kwargs.pop("collection_name")
            create: bool = kwargs.pop("create", False)
            options: Optional[dict] = kwargs.pop("options", None)
            if kwargs or args:
                raise TypeError(f"unexpected extra args: args={args!r} kwargs={list(kwargs)!r}")
        else:
            # Legacy: positional palace_path string
            if not args:
                raise TypeError("get_collection requires palace= or positional palace_path")
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
                raise TypeError(f"unexpected extra args: rest={rest!r} kwargs={list(kwargs)!r}")
            palace = PalaceRef(id=palace_path, local_path=palace_path)

        if not palace.local_path:
            raise ValueError("sqlite-vec backend requires PalaceRef.local_path")

        if create:
            os.makedirs(palace.local_path, exist_ok=True)
        elif not os.path.isdir(palace.local_path):
            raise PalaceNotFoundError(palace.local_path)

        db = self._open_db(palace.local_path)

        row = db.execute(
            "SELECT dimension FROM collections WHERE name = ?", (collection_name,)
        ).fetchone()

        if row is None:
            if not create:
                raise PalaceNotFoundError(
                    f"collection {collection_name!r} not found in {palace.local_path}"
                )
            opts = options or {}
            dim = opts.get("dimension")
            if dim is None:
                raise ValueError(
                    "creating a new collection requires options={'dimension': N}"
                )
            embedder = opts.get("embedder_name")
            vec_table = f"items_vec_{_sanitize_table_suffix(collection_name)}"
            with db:
                db.execute(
                    "INSERT INTO collections(name, dimension, embedder_name) VALUES (?, ?, ?)",
                    (collection_name, int(dim), embedder),
                )
                db.execute(
                    f"CREATE VIRTUAL TABLE IF NOT EXISTS {vec_table} USING vec0(embedding float[{int(dim)}])"
                )
            dimension = int(dim)
        else:
            dimension = int(row[0])

        return SQLiteVecCollection(
            db=db,
            palace_path=palace.local_path,
            name=collection_name,
            dimension=dimension,
            embedder=self._resolve_embedder(),
        )

    def close_palace(self, palace: PalaceRef) -> None:
        if not palace.local_path:
            return
        db = self._connections.pop(palace.local_path, None)
        if db is not None:
            try:
                db.close()
            except Exception:
                logger.exception("error closing sqlite-vec db for %s", palace.local_path)

    def close(self) -> None:
        for path, db in list(self._connections.items()):
            try:
                db.close()
            except Exception:
                logger.exception("error closing sqlite-vec db for %s", path)
        self._connections.clear()
        self._closed = True

    def health(self, palace: Optional[PalaceRef] = None) -> HealthStatus:
        if self._closed:
            return HealthStatus.unhealthy("backend closed")
        if palace is None:
            return HealthStatus.healthy(detail=f"{len(self._connections)} open dbs")
        if not palace.local_path:
            return HealthStatus.unhealthy("local_path required")
        try:
            db = self._open_db(palace.local_path)
            db.execute("SELECT 1").fetchone()
            return HealthStatus.healthy(detail=palace.local_path)
        except Exception as e:
            return HealthStatus.unhealthy(detail=f"{palace.local_path}: {e!r}")

    @classmethod
    def detect(cls, path: str) -> bool:
        """Auto-detect sqlite-vec palace by presence of the db file."""
        return os.path.isfile(os.path.join(path, DB_FILENAME))
