"""Rust exact-vector backend for MemPalace.

High-performance native backend powered by crates/mempalace-core and PyO3.
Stores data in the same sqlite_exact.sqlite3 database as SQLiteExactBackend,
but uses a native contiguous vector index in Rust. Complex filters, requests for
returned embeddings, and installs without the native extension use the Python
backend.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from .base import (
    DimensionMismatchError,
    QueryResult,
    _IncludeSpec,
    REGISTRY_SENTINEL_ROOM,
    REGISTRY_SENTINEL_INGEST_MODE,
    where_without_registry_exclusion,
)
from .sqlite_exact import (
    _DB_FILENAME,
    SQLiteExactBackend,
    SQLiteExactCollection,
    _SnapshotChanged,
    _as_vector_array,
    _json_field_sql,
    _validate_where,
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
                room_expr = _json_field_sql("room", locus_columns=self._handle.has_locus_columns)
                registry_ids = frozenset(
                    row[0]
                    for row in cur.execute(
                        f"SELECT id FROM documents WHERE collection_id = ? AND "
                        f"({room_expr} = ? OR json_extract(metadata_json, '$.ingest_mode') = ?)",
                        (
                            self._collection_id(cur),
                            REGISTRY_SENTINEL_ROOM,
                            REGISTRY_SENTINEL_INGEST_MODE,
                        ),
                    )
                )
                self._handle._native_cache[self._collection_name] = (version, index, registry_ids)
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
        _validate_where(where)
        scope = where_without_registry_exclusion(where)
        can_use_native = (
            _NativeVectorIndex is not None
            and not spec.embeddings
            and not where_document
            and (not scope or (len(scope) == 1 and isinstance(scope.get("wing"), str)))
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
            return self._query_native(query_embeddings, n_results, scope, spec)
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
            registry_ids = self._handle._native_cache[self._collection_name][2]

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
                # Installed native extensions accept only a wing filter. At
                # most len(registry_ids) candidates can be bookkeeping rows,
                # so overfetching by that count preserves the real top-k.
                # Cache the IDs with the index rather than scan metadata per query.
                hits = native.query_parallel(q.tolist(), n_results + len(registry_ids), filter_wing)
                hits = [hit for hit in hits if hit[0] not in registry_ids][:n_results]
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
