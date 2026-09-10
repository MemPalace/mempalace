"""Rust exact-vector backend for MemPalace.

High-performance native backend powered by crates/mempalace-core and PyO3.
Stores data in the same sqlite_exact.sqlite3 database as SQLiteExactBackend,
but uses a native contiguous vector index in Rust.
Memory consumption is 526 MB for 334k documents (vs 2,430 MB in pure Python)
and multi-core parallel queries execute in 7–11 ms.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from .base import (
    DimensionMismatchError,
    QueryResult,
    _IncludeSpec,
)
from .sqlite_exact import (
    _DB_FILENAME,
    SQLiteExactBackend,
    SQLiteExactCollection,
    _SnapshotChanged,
    _as_vector_array,
)

logger = logging.getLogger(__name__)

try:
    from ..mempalace_core_rs import NativeVectorIndex as _NativeVectorIndex
except (ImportError, ValueError):
    try:
        from mempalace_core_rs import NativeVectorIndex as _NativeVectorIndex
    except (ImportError, ValueError):
        _NativeVectorIndex = None


class _NativeUnavailable(Exception):
    """Leave the native cursor before entering the Python fallback."""


class RustExactCollection(SQLiteExactCollection):
    """Collection wrapper that delegates vector scanning to mempalace_core_rs."""

    @property
    def _native_index(self) -> Optional[Any]:
        cached = self._handle._native_cache.get(self._collection_name)
        return cached[1] if cached is not None else None

    def _ensure_native_index(self, cur):
        if _NativeVectorIndex is None:
            return None
        db_file = os.path.join(self._handle.palace_path, _DB_FILENAME)
        if not os.path.isfile(db_file):
            return None
        version = self._index_version(cur)
        cached = self._handle._native_cache.get(self._collection_name)
        if cached is None or cached[0] != version:
            self._handle._native_cache.pop(self._collection_name, None)
            try:
                index = _NativeVectorIndex.load_from_sqlite(db_file, self._collection_name)
                self._handle._native_cache[self._collection_name] = (version, index)
            except Exception as e:
                logger.warning("Failed to load Rust native vector index: %s", e)
        return self._native_index

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
            return super()._query_once(
                query_embeddings=query_embeddings,
                n_results=n_results,
                where=where,
                where_document=where_document,
                include=include,
            )

        try:
            return self._query_native(query_embeddings, n_results, where, spec)
        except _NativeUnavailable:
            return super()._query_once(
                query_embeddings=query_embeddings,
                n_results=n_results,
                where=where,
                where_document=where_document,
                include=include,
            )

    def _query_native(self, query_embeddings, n_results, where, spec) -> QueryResult:
        outer_ids: list[list[str]] = []
        outer_docs: list[list[str]] = []
        outer_metas: list[list[dict]] = []
        outer_dists: list[list[float]] = []
        n_results = max(0, int(n_results))

        with self._cursor() as cur:
            snapshot = self._index_version(cur)
            collection_id = self._collection_id(cur)
            expected_dim = self._collection_dimension(cur, collection_id)
            native = self._ensure_native_index(cur)
            if native is None:
                raise _NativeUnavailable()

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

            # Both the loader and hydration use separate SQLite statements.
            # A commit anywhere between version capture and hydration requires
            # discarding all batch results, not merely refreshing the next call.
            if snapshot != self._index_version(cur):
                raise _SnapshotChanged()

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
        collection = super().get_collection(*args, **kwargs)
        return RustExactCollection(collection._handle, collection._collection_name, backend=self)

    @classmethod
    def detect(cls, path: str) -> bool:
        """Native acceleration is selected explicitly, not a separate disk format."""
        return False
