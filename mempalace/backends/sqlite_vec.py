"""sqlite-vec backend for mempalace.

Drop-in replacement for the chroma backend on platforms where
``chromadb_rust_bindings`` is unsafe (notably macOS 26 / ARM64, where the
rust bindings have an intra-process UAF in the recursive segment walker —
chroma#6852, mempalace#1355). All vector storage and ANN search go through
``sqlite-vec``'s ``vec0`` virtual table; metadata lives in regular SQLite
columns. No tokio, no rust-side concurrency; one Python process, one
sqlite connection per palace.

The on-disk layout is a single ``sqlite_vec.db`` file at
``<palace_path>/sqlite_vec.db``. The chromadb dir on the same path is left
untouched so the old data can be re-migrated or swapped back if needed.
"""

from __future__ import annotations

import json
import sqlite3
import struct
import threading
from typing import Any, Iterable, Optional

import sqlite_vec

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


# ---------------------------------------------------------------------------
# Where-clause compiler
# ---------------------------------------------------------------------------

def _normalize_get_collection_args(args, kwargs):
    """Coerce the legacy positional form into the kwarg form ChromaBackend
    accepts. Returns ``(PalaceRef, collection_name, create, options)``.
    """
    if args:
        first = args[0]
        if isinstance(first, PalaceRef):
            palace = first
            rest_args = args[1:]
        elif isinstance(first, str):
            palace = PalaceRef(id=first, local_path=first)
            rest_args = args[1:]
        else:
            raise TypeError(
                f"get_collection: first positional arg must be PalaceRef or palace path, got {type(first).__name__}"
            )
        collection_name = (
            rest_args[0]
            if rest_args
            else kwargs.pop("collection_name", "mempalace_drawers")
        )
        create = (
            rest_args[1] if len(rest_args) >= 2 else kwargs.pop("create", False)
        )
    else:
        palace = kwargs.pop("palace", None)
        if palace is None:
            raise TypeError("get_collection requires palace= (PalaceRef) or a positional palace path")
        if not isinstance(palace, PalaceRef):
            palace = PalaceRef(id=str(palace), local_path=str(palace))
        collection_name = kwargs.pop("collection_name", "mempalace_drawers")
        create = kwargs.pop("create", False)
    options = kwargs.pop("options", None)
    return palace, collection_name, create, options


_OP_MAP = {
    "$eq": "=",
    "$ne": "!=",
    "$gt": ">",
    "$gte": ">=",
    "$lt": "<",
    "$lte": "<=",
}
_LIST_OPS = {"$in", "$nin"}
_LOGICAL_OPS = {"$and", "$or"}
_DOC_OPS = {"$contains", "$not_contains"}
_SUPPORTED_OPS = set(_OP_MAP) | _LIST_OPS | _LOGICAL_OPS


def _coerce_value(v: Any) -> Any:
    """Convert Python values to types SQLite handles natively."""
    if isinstance(v, bool):
        return 1 if v else 0
    return v


def _compile_where(where: Optional[dict], params: list) -> str:
    """Compile a chroma-style ``where`` dict to a SQL clause over the
    ``meta`` JSON column. Returns the clause (without leading WHERE) or
    ``"1=1"`` for empty input. Mutates ``params`` in place.
    """
    if not where:
        return "1=1"
    return _compile_node(where, params)


def _compile_node(node: Any, params: list) -> str:
    if not isinstance(node, dict):
        raise UnsupportedFilterError(f"where node must be a dict, got {type(node).__name__}")

    if len(node) == 1:
        (key, value), = node.items()
        if key in _LOGICAL_OPS:
            if not isinstance(value, list) or not value:
                raise UnsupportedFilterError(f"{key} requires a non-empty list")
            joiner = " AND " if key == "$and" else " OR "
            return "(" + joiner.join(_compile_node(v, params) for v in value) + ")"
        return _compile_field(key, value, params)

    # Implicit AND across multiple keys.
    parts = [_compile_field(k, v, params) for k, v in node.items()]
    return "(" + " AND ".join(parts) + ")"


def _compile_field(key: str, value: Any, params: list) -> str:
    if key.startswith("$"):
        raise UnsupportedFilterError(f"operator {key!r} not valid at field position")
    extractor = "json_extract(meta, ?)"
    json_path = "$." + key
    if isinstance(value, dict):
        if len(value) != 1:
            raise UnsupportedFilterError(
                f"field {key!r}: each operator dict must have exactly one entry"
            )
        ((op, opv),) = value.items()
        if op in _OP_MAP:
            params.extend([json_path, _coerce_value(opv)])
            return f"{extractor} {_OP_MAP[op]} ?"
        if op in _LIST_OPS:
            if not isinstance(opv, (list, tuple)) or not opv:
                raise UnsupportedFilterError(f"{op} requires a non-empty list")
            placeholders = ",".join(["?"] * len(opv))
            params.append(json_path)
            params.extend(_coerce_value(x) for x in opv)
            cmp = "IN" if op == "$in" else "NOT IN"
            return f"{extractor} {cmp} ({placeholders})"
        raise UnsupportedFilterError(f"operator {op!r} not supported")
    # Bare scalar = equality.
    params.extend([json_path, _coerce_value(value)])
    return f"{extractor} = ?"


def _compile_where_document(wd: Optional[dict], params: list) -> str:
    if not wd:
        return "1=1"
    if len(wd) != 1:
        raise UnsupportedFilterError("where_document must have exactly one operator")
    ((op, value),) = wd.items()
    if op in _LOGICAL_OPS:
        if not isinstance(value, list) or not value:
            raise UnsupportedFilterError(f"{op} requires a non-empty list")
        joiner = " AND " if op == "$and" else " OR "
        return "(" + joiner.join(_compile_where_document(v, params) for v in value) + ")"
    if op == "$contains":
        params.append(f"%{value}%")
        return "doc LIKE ?"
    if op == "$not_contains":
        params.append(f"%{value}%")
        return "doc NOT LIKE ?"
    raise UnsupportedFilterError(f"where_document operator {op!r} not supported")


# ---------------------------------------------------------------------------
# Vector encoding
# ---------------------------------------------------------------------------


def _encode_vec(vec: list[float]) -> bytes:
    """Pack a Python float list into the little-endian f32 blob sqlite-vec expects."""
    return struct.pack(f"{len(vec)}f", *vec)


def _decode_vec(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


class SqliteVecCollection(BaseCollection):
    def __init__(self, backend: "SqliteVecBackend", palace_path: str, name: str, dimension: int):
        self._backend = backend
        self._palace_path = palace_path
        self._name = name
        self._dim = dimension
        self._closed = False

    # ---- internals ----

    def _conn(self) -> sqlite3.Connection:
        if self._closed:
            raise BackendClosedError("collection is closed")
        return self._backend._conn(self._palace_path)

    def _vec_table(self) -> str:
        return f"vec_{self._name}"

    def _check(self, embeddings: Optional[list[list[float]]]) -> None:
        if embeddings is None:
            return
        for e in embeddings:
            if len(e) != self._dim:
                raise DimensionMismatchError(
                    f"embedding dim {len(e)} != collection dim {self._dim}"
                )

    def _embed(self, texts: list[str]) -> list[list[float]]:
        ef = self._backend._embedder()
        result = ef(texts)
        return [list(v) for v in result]

    # ---- writes ----

    def add(self, *, documents, ids, metadatas=None, embeddings=None):
        self._upsert_impl(documents, ids, metadatas, embeddings, replace=False)

    def upsert(self, *, documents, ids, metadatas=None, embeddings=None):
        self._upsert_impl(documents, ids, metadatas, embeddings, replace=True)

    def _upsert_impl(self, documents, ids, metadatas, embeddings, *, replace: bool):
        if not ids:
            return
        if embeddings is None:
            embeddings = self._embed(list(documents))
        self._check(embeddings)
        if metadatas is None:
            metadatas = [{} for _ in ids]
        conn = self._conn()
        vec_tbl = self._vec_table()
        with conn:
            cur = conn.cursor()
            for did, doc, meta, emb in zip(ids, documents, metadatas, embeddings):
                meta_json = json.dumps(meta or {}, sort_keys=True, ensure_ascii=False)
                if replace:
                    cur.execute(
                        f"""
                        INSERT INTO drawers (collection, drawer_id, doc, meta)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(collection, drawer_id) DO UPDATE SET
                            doc=excluded.doc, meta=excluded.meta
                        RETURNING rowid
                        """,
                        (self._name, did, doc, meta_json),
                    )
                    row_pk = cur.fetchone()[0]
                    cur.execute(f"DELETE FROM {vec_tbl} WHERE rowid = ?", (row_pk,))
                    cur.execute(
                        f"INSERT INTO {vec_tbl} (rowid, embedding) VALUES (?, ?)",
                        (row_pk, _encode_vec(emb)),
                    )
                else:
                    cur.execute(
                        "INSERT INTO drawers (collection, drawer_id, doc, meta) VALUES (?, ?, ?, ?)",
                        (self._name, did, doc, meta_json),
                    )
                    row_pk = cur.lastrowid
                    cur.execute(
                        f"INSERT INTO {vec_tbl} (rowid, embedding) VALUES (?, ?)",
                        (row_pk, _encode_vec(emb)),
                    )

    def update(self, *, ids, documents=None, metadatas=None, embeddings=None):
        # Atomic per-row update overriding the BaseCollection default (which
        # round-trips through get + upsert).
        if documents is None and metadatas is None and embeddings is None:
            raise ValueError("update requires at least one of documents, metadatas, embeddings")
        self._check(embeddings)
        n = len(ids)
        for label, value in (
            ("documents", documents),
            ("metadatas", metadatas),
            ("embeddings", embeddings),
        ):
            if value is not None and len(value) != n:
                raise ValueError(f"{label} length {len(value)} does not match ids length {n}")
        conn = self._conn()
        vec_tbl = self._vec_table()
        with conn:
            cur = conn.cursor()
            for i, did in enumerate(ids):
                cur.execute(
                    "SELECT rowid, doc, meta FROM drawers WHERE collection=? AND drawer_id=?",
                    (self._name, did),
                )
                row = cur.fetchone()
                if row is None:
                    continue  # silently skip missing ids (matches chroma)
                row_pk, prev_doc, prev_meta = row
                new_doc = documents[i] if documents is not None else prev_doc
                new_meta = json.loads(prev_meta or "{}")
                if metadatas is not None:
                    new_meta.update(metadatas[i] or {})
                cur.execute(
                    "UPDATE drawers SET doc=?, meta=? WHERE rowid=?",
                    (new_doc, json.dumps(new_meta, sort_keys=True, ensure_ascii=False), row_pk),
                )
                if embeddings is not None:
                    cur.execute(f"DELETE FROM {vec_tbl} WHERE rowid=?", (row_pk,))
                    cur.execute(
                        f"INSERT INTO {vec_tbl} (rowid, embedding) VALUES (?, ?)",
                        (row_pk, _encode_vec(embeddings[i])),
                    )

    def delete(self, *, ids=None, where=None):
        conn = self._conn()
        vec_tbl = self._vec_table()
        with conn:
            cur = conn.cursor()
            if ids:
                placeholders = ",".join(["?"] * len(ids))
                cur.execute(
                    f"SELECT rowid FROM drawers WHERE collection=? AND drawer_id IN ({placeholders})",
                    [self._name, *ids],
                )
                rowids = [r[0] for r in cur.fetchall()]
                if rowids:
                    rp = ",".join(["?"] * len(rowids))
                    cur.execute(f"DELETE FROM {vec_tbl} WHERE rowid IN ({rp})", rowids)
                    cur.execute(f"DELETE FROM drawers WHERE rowid IN ({rp})", rowids)
                return
            if where:
                params: list = [self._name]
                clause = _compile_where(where, params)
                cur.execute(
                    f"SELECT rowid FROM drawers WHERE collection=? AND {clause}",
                    params,
                )
                rowids = [r[0] for r in cur.fetchall()]
                if rowids:
                    rp = ",".join(["?"] * len(rowids))
                    cur.execute(f"DELETE FROM {vec_tbl} WHERE rowid IN ({rp})", rowids)
                    cur.execute(f"DELETE FROM drawers WHERE rowid IN ({rp})", rowids)

    # ---- reads ----

    def count(self) -> int:
        cur = self._conn().cursor()
        cur.execute("SELECT COUNT(*) FROM drawers WHERE collection=?", (self._name,))
        return cur.fetchone()[0]

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
        conn = self._conn()
        cur = conn.cursor()
        params: list = [self._name]
        clauses = ["collection=?"]
        if ids:
            placeholders = ",".join(["?"] * len(ids))
            clauses.append(f"drawer_id IN ({placeholders})")
            params.extend(ids)
        if where:
            clauses.append(_compile_where(where, params))
        if where_document:
            clauses.append(_compile_where_document(where_document, params))
        sql = f"SELECT rowid, drawer_id, doc, meta FROM drawers WHERE {' AND '.join(clauses)} ORDER BY rowid"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
            if offset is not None:
                sql += f" OFFSET {int(offset)}"
        cur.execute(sql, params)
        rows = cur.fetchall()
        if not rows:
            return GetResult.empty()
        out_ids: list[str] = []
        out_docs: list[str] = []
        out_metas: list[dict] = []
        out_embs: Optional[list[list[float]]] = [] if spec.embeddings else None
        for rowid, drawer_id, doc, meta in rows:
            out_ids.append(drawer_id)
            out_docs.append(doc or "")
            out_metas.append(json.loads(meta) if meta else {})
            if spec.embeddings:
                cur.execute(f"SELECT embedding FROM {self._vec_table()} WHERE rowid=?", (rowid,))
                vrow = cur.fetchone()
                out_embs.append(_decode_vec(vrow[0]) if vrow else [])
        return GetResult(ids=out_ids, documents=out_docs, metadatas=out_metas, embeddings=out_embs)

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
        if (query_texts is None) == (query_embeddings is None):
            raise ValueError("query requires exactly one of query_texts or query_embeddings")
        if query_embeddings is None:
            query_embeddings = self._embed(list(query_texts))
        self._check(query_embeddings)

        spec = _IncludeSpec.resolve(include, default_distances=True)
        conn = self._conn()
        cur = conn.cursor()
        vec_tbl = self._vec_table()

        # Build metadata pre-filter — we narrow rowids first, then KNN over them.
        narrow_clauses = ["collection=?"]
        narrow_params: list = [self._name]
        if where:
            narrow_clauses.append(_compile_where(where, narrow_params))
        if where_document:
            narrow_clauses.append(_compile_where_document(where_document, narrow_params))
        cur.execute(
            f"SELECT rowid FROM drawers WHERE {' AND '.join(narrow_clauses)}",
            narrow_params,
        )
        candidate_rowids = [r[0] for r in cur.fetchall()]
        if not candidate_rowids:
            return QueryResult.empty(num_queries=len(query_embeddings), embeddings_requested=spec.embeddings)

        out_ids: list[list[str]] = []
        out_docs: list[list[str]] = []
        out_metas: list[list[dict]] = []
        out_dists: list[list[float]] = []
        out_embs: Optional[list[list[list[float]]]] = [] if spec.embeddings else None

        # sqlite-vec's KNN syntax: WHERE embedding MATCH ? AND rowid IN (...) ORDER BY distance LIMIT k
        chunk = 500
        for q in query_embeddings:
            qblob = _encode_vec(list(q))
            # In-clause has a SQLite limit; if huge candidate set, rely on metadata filter only via full table.
            if len(candidate_rowids) > 5000:
                sql = f"""
                    SELECT v.rowid, v.distance, d.drawer_id, d.doc, d.meta
                    FROM {vec_tbl} v
                    JOIN drawers d ON d.rowid = v.rowid
                    WHERE v.embedding MATCH ? AND k = ? AND d.collection = ?
                    ORDER BY v.distance
                """
                cur.execute(sql, (qblob, n_results, self._name))
            else:
                placeholders = ",".join(["?"] * len(candidate_rowids))
                sql = f"""
                    SELECT v.rowid, v.distance, d.drawer_id, d.doc, d.meta
                    FROM {vec_tbl} v
                    JOIN drawers d ON d.rowid = v.rowid
                    WHERE v.embedding MATCH ? AND k = ? AND v.rowid IN ({placeholders})
                    ORDER BY v.distance
                """
                cur.execute(sql, (qblob, n_results, *candidate_rowids))
            hits = cur.fetchall()
            ids_i, docs_i, metas_i, dists_i = [], [], [], []
            embs_i: Optional[list[list[float]]] = [] if spec.embeddings else None
            for rowid, dist, did, doc, meta in hits:
                ids_i.append(did)
                docs_i.append(doc or "")
                metas_i.append(json.loads(meta) if meta else {})
                dists_i.append(float(dist))
                if spec.embeddings:
                    cur.execute(f"SELECT embedding FROM {vec_tbl} WHERE rowid=?", (rowid,))
                    vrow = cur.fetchone()
                    embs_i.append(_decode_vec(vrow[0]) if vrow else [])
            out_ids.append(ids_i)
            out_docs.append(docs_i)
            out_metas.append(metas_i)
            out_dists.append(dists_i)
            if out_embs is not None:
                out_embs.append(embs_i or [])

        return QueryResult(
            ids=out_ids,
            documents=out_docs,
            metadatas=out_metas,
            distances=out_dists,
            embeddings=out_embs,
        )

    def close(self) -> None:
        self._closed = True

    def health(self) -> HealthStatus:
        try:
            self.count()
            return HealthStatus.healthy()
        except Exception as e:
            return HealthStatus.unhealthy(f"sqlite-vec: {e!r}")


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


class SqliteVecBackend(BaseBackend):
    name = "sqlite_vec"
    capabilities = frozenset({"supports_update"})

    def __init__(self):
        self._conns: dict[str, sqlite3.Connection] = {}
        self._lock = threading.Lock()
        self._ef = None

    def _embedder(self):
        if self._ef is None:
            from ..embedding import get_embedding_function
            self._ef = get_embedding_function()
        return self._ef

    def _conn(self, palace_path: str) -> sqlite3.Connection:
        with self._lock:
            conn = self._conns.get(palace_path)
            if conn is not None:
                return conn
            import os
            os.makedirs(palace_path, exist_ok=True)
            db_path = os.path.join(palace_path, "sqlite_vec.db")
            c = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
            c.enable_load_extension(True)
            sqlite_vec.load(c)
            c.enable_load_extension(False)
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA foreign_keys=ON")
            self._init_schema(c)
            self._conns[palace_path] = c
            return c

    def _init_schema(self, conn: sqlite3.Connection) -> None:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS collections (
                name TEXT PRIMARY KEY,
                dimension INTEGER NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS drawers (
                rowid INTEGER PRIMARY KEY AUTOINCREMENT,
                collection TEXT NOT NULL,
                drawer_id TEXT NOT NULL,
                doc TEXT,
                meta TEXT,
                UNIQUE(collection, drawer_id)
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_drawers_collection ON drawers(collection)")

    def get_collection(self, *args, **kwargs) -> BaseCollection:
        """Accept the new kwarg form **and** the legacy positional form
        ``get_collection(palace_path, collection_name, create=False)`` that
        ``mempalace.palace.get_collection`` still passes through.
        """
        palace, collection_name, create, options = _normalize_get_collection_args(
            args, kwargs
        )
        if not palace.local_path:
            raise PalaceNotFoundError("sqlite_vec backend requires a filesystem palace path")
        conn = self._conn(palace.local_path)
        cur = conn.cursor()
        cur.execute("SELECT dimension FROM collections WHERE name=?", (collection_name,))
        row = cur.fetchone()
        if row is None:
            if not create:
                raise PalaceNotFoundError(
                    f"collection {collection_name!r} not found in palace at {palace.local_path}"
                )
            dim = (options or {}).get("dimension", 384)
            with conn:
                cur.execute("INSERT INTO collections (name, dimension) VALUES (?, ?)", (collection_name, dim))
                # vec0 virtual table per-collection (different collections may have different dims)
                cur.execute(
                    f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_{collection_name} "
                    f"USING vec0(rowid INTEGER PRIMARY KEY, embedding FLOAT[{dim}])"
                )
        else:
            dim = row[0]
            cur.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_{collection_name} "
                f"USING vec0(rowid INTEGER PRIMARY KEY, embedding FLOAT[{dim}])"
            )
        return SqliteVecCollection(self, palace.local_path, collection_name, dim)

    def close_palace(self, palace: PalaceRef) -> None:
        if not palace.local_path:
            return
        with self._lock:
            conn = self._conns.pop(palace.local_path, None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def close(self) -> None:
        with self._lock:
            for conn in self._conns.values():
                try:
                    conn.close()
                except Exception:
                    pass
            self._conns.clear()

    def health(self, palace: Optional[PalaceRef] = None) -> HealthStatus:
        if palace is None or not palace.local_path:
            return HealthStatus.healthy("backend ready (no palace bound)")
        try:
            self._conn(palace.local_path).execute("SELECT 1").fetchone()
            return HealthStatus.healthy()
        except Exception as e:
            return HealthStatus.unhealthy(f"sqlite_vec: {e!r}")

    @classmethod
    def detect(cls, path: str) -> bool:
        import os
        return os.path.isfile(os.path.join(path, "sqlite_vec.db"))
