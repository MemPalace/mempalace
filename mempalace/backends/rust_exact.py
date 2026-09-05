"""Rust exact-vector backend for MemPalace.

High-performance native backend powered by crates/mempalace-core and PyO3.
Stores data in the same sqlite_exact.sqlite3 database as SQLiteExactBackend,
but uses a native memory-mapped contiguous SIMD index in Rust.
Memory consumption is 526 MB for 334k documents (vs 2,430 MB in pure Python)
and multi-core parallel queries execute in 7–11 ms.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from .base import (
    CollectionNotInitializedError,
    DimensionMismatchError,
    PalaceNotFoundError,
    QueryResult,
    _IncludeSpec,
)
from .sqlite_exact import (
    _DB_FILENAME,
    SQLiteExactBackend,
    SQLiteExactCollection,
    _SQLiteExactHandle,
    _as_vector_array,
    _utcnow,
)

logger = logging.getLogger(__name__)

try:
    from ..mempalace_core_rs import NativeVectorIndex as _NativeVectorIndex
except (ImportError, ValueError):
    try:
        from mempalace_core_rs import NativeVectorIndex as _NativeVectorIndex
    except (ImportError, ValueError):
        _NativeVectorIndex = None


class RustExactCollection(SQLiteExactCollection):
    """Collection wrapper that delegates vector scanning to mempalace_core_rs."""

    def __init__(self, handle: _SQLiteExactHandle, collection_name: str, backend=None):
        super().__init__(handle, collection_name, backend=backend)
        self._native_index: Optional[Any] = None
        self._native_data_version: Optional[int] = None

    def _ensure_native_index(self, cur, collection_id: int):
        if _NativeVectorIndex is None:
            return None
        db_file = os.path.join(self._handle.palace_path, _DB_FILENAME)
        if not os.path.isfile(db_file):
            return None
        data_version = int(cur.execute("PRAGMA data_version").fetchone()[0])
        if self._native_data_version != data_version:
            self._native_index = None
            self._native_data_version = data_version
        if self._native_index is None:
            try:
                self._native_index = _NativeVectorIndex.load_from_sqlite(
                    db_file, self._collection_name
                )
            except Exception as e:
                logger.warning("Failed to load Rust native vector index: %s", e)
                self._native_index = None
        return self._native_index

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
                "rust_exact requires query_embeddings; use palace.get_collection wrapper"
            )
        if query_embeddings is None:
            raise ValueError("query requires query_embeddings")
        if not query_embeddings:
            raise ValueError("query input must be a non-empty list")

        spec = _IncludeSpec.resolve(include, default_distances=True)
        can_use_native = (
            _NativeVectorIndex is not None
            and not spec.embeddings
            and not where_document
            and (
                not where
                or (
                    isinstance(where, dict)
                    and len(where) == 1
                    and isinstance(where.get("wing"), str)
                )
            )
        )
        if not can_use_native:
            # Fall back to base SQLiteExactCollection implementation
            return super().query(
                query_embeddings=query_embeddings,
                n_results=n_results,
                where=where,
                where_document=where_document,
                include=include,
            )

        outer_ids: list[list[str]] = []
        outer_docs: list[list[str]] = []
        outer_metas: list[list[dict]] = []
        outer_dists: list[list[float]] = []
        n_results = max(0, int(n_results))

        with self._cursor() as cur:
            collection_id = self._collection_id(cur)
            expected_dim = self._collection_dimension(cur, collection_id)
            native = self._ensure_native_index(cur, collection_id)
            if native is None:
                return super().query(
                    query_embeddings=query_embeddings,
                    n_results=n_results,
                    where=where,
                    where_document=where_document,
                    include=include,
                )

            filter_wing = where.get("wing") if where else None
            for query_vector in query_embeddings:
                q = _as_vector_array(query_vector)
                if expected_dim is not None and int(q.size) != expected_dim:
                    raise DimensionMismatchError(
                        f"rust_exact collection {self._collection_name!r} expects "
                        f"embedding dimension {expected_dim}, got {int(q.size)}"
                    )
                if native.is_empty() or n_results == 0:
                    outer_ids.append([])
                    outer_docs.append([])
                    outer_metas.append([])
                    outer_dists.append([])
                    continue
                hits = native.query_parallel(q.tolist(), n_results, filter_wing)
                top_ids = [h[0] for h in hits]
                top_dists = [float(h[1]) for h in hits]
                docs_by_id: dict[str, str] = {}
                metas_by_id: dict[str, dict] = {}
                if spec.documents or spec.metadatas:
                    docs_by_id, metas_by_id = self._hydrate(cur, collection_id, top_ids, spec)
                outer_ids.append(top_ids)
                outer_docs.append(
                    [docs_by_id.get(doc_id, "") for doc_id in top_ids] if spec.documents else []
                )
                outer_metas.append(
                    [metas_by_id.get(doc_id, {}) for doc_id in top_ids] if spec.metadatas else []
                )
                outer_dists.append(top_dists if spec.distances else [])

        return QueryResult(
            ids=outer_ids,
            documents=outer_docs,
            metadatas=outer_metas,
            distances=outer_dists,
            embeddings=None,
        )


class RustExactBackend(SQLiteExactBackend):
    """Backend factory for rust_exact."""

    name = "rust_exact"

    def get_collection(self, *args, **kwargs) -> RustExactCollection:
        palace, collection_name, create, read_only = self._normalize_args(args, kwargs)
        self.require_namespace_support(palace)
        palace_path = palace.local_path
        if palace_path is None:
            raise PalaceNotFoundError("RustExactBackend requires PalaceRef.local_path")
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
                from ..palace import mine_palace_lock

                with mine_palace_lock(palace_path):
                    handle.conn.execute(
                        "INSERT INTO collections(name, created_at) VALUES (?, ?)",
                        (collection_name, _utcnow()),
                    )
        return RustExactCollection(handle, collection_name, backend=self)

    @classmethod
    def detect(cls, path: str) -> bool:
        """Return True when ``path`` has a .rust_exact marker."""
        marker = os.path.join(path, ".rust_exact")
        return os.path.isfile(marker)
