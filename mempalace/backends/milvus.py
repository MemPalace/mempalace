"""Milvus backend for MemPalace.

The backend uses the modern ``pymilvus.MilvusClient`` API only. By default each
local palace gets its own Milvus Lite database at ``<palace>/milvus.db``. Users
can opt into Milvus server or Zilliz Cloud by configuring a URI and token.

Embeddings are supplied by MemPalace's core embedding wrapper. This backend
declares ``requires_explicit_embeddings`` and stores/query vectors directly.
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

import numpy as np

from ..config import (
    DEFAULT_MILVUS_CONSISTENCY_LEVEL,
    normalize_milvus_consistency_level,
    strip_lone_surrogates,
)
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

DEFAULT_DB_FILENAME = "milvus.db"
_MARKER_FILENAME = "milvus_backend.json"
_MAX_QUERY_WINDOW = 16384
FIELD_ID = "id"
FIELD_DOCUMENT = "document"
FIELD_METADATA = "metadata"
FIELD_VECTOR = "vector"
FIELD_SPARSE = "sparse"
DOCUMENT_MAX_LENGTH = 65535
DRAWER_ID_MAX_LENGTH = 512
RESERVED_FIELDS = {
    FIELD_ID,
    FIELD_DOCUMENT,
    FIELD_METADATA,
    FIELD_VECTOR,
    FIELD_SPARSE,
    "distance",
    "score",
}

_FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_server_uri(uri: str) -> bool:
    normalized = uri.lower()
    return normalized.startswith(("http://", "https://", "tcp://", "grpc://"))


def _quote_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if value is None:
        raise UnsupportedFilterError("Milvus filters do not support null comparisons")
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def _like_value(value: Any) -> str:
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"%{text}%"'


def _field_name(name: str) -> str:
    if not isinstance(name, str) or not _FIELD_RE.match(name):
        raise UnsupportedFilterError(f"Milvus filter field {name!r} is not a safe identifier")
    return name


def _translate_field(field: str, expected: Any) -> str:
    field = _field_name(field)
    if isinstance(expected, dict):
        parts = []
        for op, operand in expected.items():
            if op == "$eq":
                parts.append(f"{field} == {_quote_value(operand)}")
            elif op == "$ne":
                parts.append(f"{field} != {_quote_value(operand)}")
            elif op == "$in":
                if not isinstance(operand, list) or not operand:
                    raise UnsupportedFilterError(f"$in requires a non-empty list for {field!r}")
                items = ", ".join(_quote_value(item) for item in operand)
                parts.append(f"{field} in [{items}]")
            elif op == "$nin":
                if not isinstance(operand, list) or not operand:
                    raise UnsupportedFilterError(f"$nin requires a non-empty list for {field!r}")
                items = ", ".join(_quote_value(item) for item in operand)
                parts.append(f"{field} not in [{items}]")
            elif op == "$gt":
                parts.append(f"{field} > {_quote_value(operand)}")
            elif op == "$gte":
                parts.append(f"{field} >= {_quote_value(operand)}")
            elif op == "$lt":
                parts.append(f"{field} < {_quote_value(operand)}")
            elif op == "$lte":
                parts.append(f"{field} <= {_quote_value(operand)}")
            elif op == "$contains":
                parts.append(f"{field} like {_like_value(operand)}")
            else:
                raise UnsupportedFilterError(f"operator {op!r} not supported by milvus")
        return " and ".join(parts)
    return f"{field} == {_quote_value(expected)}"


def _translate_clause(clause: dict) -> str:
    if not isinstance(clause, dict):
        raise UnsupportedFilterError(f"where clause must be a dict, got {type(clause).__name__}")
    if not clause:
        return ""
    parts = []
    for key, value in clause.items():
        if key == "$and":
            if not isinstance(value, list) or not value:
                raise UnsupportedFilterError("$and requires a non-empty list of clauses")
            nested = [_translate_clause(item) for item in value]
            parts.append("(" + " and ".join(part for part in nested if part) + ")")
        elif key == "$or":
            if not isinstance(value, list) or not value:
                raise UnsupportedFilterError("$or requires a non-empty list of clauses")
            nested = [_translate_clause(item) for item in value]
            parts.append("(" + " or ".join(part for part in nested if part) + ")")
        elif key.startswith("$"):
            raise UnsupportedFilterError(f"operator {key!r} not supported by milvus")
        else:
            parts.append(_translate_field(key, value))
    return " and ".join(part for part in parts if part)


def translate_where(where: Optional[dict]) -> str:
    """Translate the portable metadata where DSL into a Milvus filter string."""
    if not where:
        return ""
    return _translate_clause(where)


def translate_where_document(where_document: Optional[dict]) -> str:
    """Translate the portable document filter subset into a Milvus filter."""
    if not where_document:
        return ""
    if not isinstance(where_document, dict):
        raise UnsupportedFilterError("where_document must be a dict")
    parts = []
    for key, value in where_document.items():
        if key == "$contains":
            parts.append(f"{FIELD_DOCUMENT} like {_like_value(value)}")
        elif key == "$and":
            if not isinstance(value, list) or not value:
                raise UnsupportedFilterError("$and requires a non-empty list of clauses")
            nested = [translate_where_document(item) for item in value]
            parts.append("(" + " and ".join(part for part in nested if part) + ")")
        elif key == "$or":
            if not isinstance(value, list) or not value:
                raise UnsupportedFilterError("$or requires a non-empty list of clauses")
            nested = [translate_where_document(item) for item in value]
            parts.append("(" + " or ".join(part for part in nested if part) + ")")
        else:
            raise UnsupportedFilterError(f"where_document operator {key!r} not supported")
    return " and ".join(part for part in parts if part)


def _combine_filter(*filters: str) -> str:
    present = [flt for flt in filters if flt]
    if not present:
        return ""
    if len(present) == 1:
        return present[0]
    return "(" + ") and (".join(present) + ")"


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
        raise DimensionMismatchError(f"milvus batch cannot mix embedding dimensions {sorted(dims)}")
    return vectors, dims.pop() if dims else 0


def _clean_text(value: Any) -> str:
    text = "" if value is None else str(value)
    return strip_lone_surrogates(text).replace("\x00", "")


def _utf8_len(value: str) -> int:
    return len(value.encode("utf-8"))


def _jsonable_metadata(meta: dict | None) -> dict:
    cleaned = {}
    for key, value in (meta or {}).items():
        if key in RESERVED_FIELDS:
            raise ValueError(f"metadata key {key!r} clashes with a reserved Milvus field")
        try:
            json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            value = str(value)
        cleaned[str(key)] = value
    return cleaned


def _slug(value: str, fallback: str = "collection") -> str:
    safe = re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_")
    if not safe or not re.match(r"^[A-Za-z_]", safe):
        safe = f"{fallback}_{safe}" if safe else fallback
    if len(safe) <= 120:
        return safe
    digest = sha256(value.encode("utf-8", errors="surrogatepass")).hexdigest()[:12]
    return f"{safe[:107]}_{digest}"


@dataclass(frozen=True)
class _MilvusConfig:
    uri: Optional[str] = None
    token: Optional[str] = None
    db_name: Optional[str] = None
    namespace: Optional[str] = None
    db_filename: str = DEFAULT_DB_FILENAME
    consistency_level: str = DEFAULT_MILVUS_CONSISTENCY_LEVEL

    @classmethod
    def from_options(cls, options: Optional[dict] = None) -> "_MilvusConfig":
        options = options or {}
        try:
            from ..config import MempalaceConfig

            cfg = MempalaceConfig()
        except Exception:  # pragma: no cover - config import should be boring
            cfg = None
        uri = (
            options.get("uri")
            or os.environ.get("MEMPALACE_MILVUS_URI")
            or getattr(cfg, "milvus_uri", None)
        )
        token = (
            options.get("token")
            or os.environ.get("MEMPALACE_MILVUS_TOKEN")
            or getattr(cfg, "milvus_token", None)
        )
        db_name = (
            options.get("db_name")
            or os.environ.get("MEMPALACE_MILVUS_DB_NAME")
            or getattr(cfg, "milvus_db_name", None)
        )
        namespace = (
            options.get("namespace")
            or os.environ.get("MEMPALACE_MILVUS_NAMESPACE")
            or getattr(cfg, "milvus_namespace", None)
        )
        try:
            consistency_level = (
                options.get("consistency_level")
                or os.environ.get("MEMPALACE_MILVUS_CONSISTENCY_LEVEL")
                or getattr(cfg, "milvus_consistency_level", DEFAULT_MILVUS_CONSISTENCY_LEVEL)
            )
            consistency_level = normalize_milvus_consistency_level(consistency_level)
        except ValueError as exc:
            raise BackendError(str(exc)) from exc
        db_filename = options.get("db_filename") or DEFAULT_DB_FILENAME
        return cls(
            uri=str(uri).strip() if uri else None,
            token=str(token) if token else None,
            db_name=str(db_name).strip() if db_name else None,
            namespace=str(namespace).strip() if namespace else None,
            db_filename=str(db_filename).strip() or DEFAULT_DB_FILENAME,
            consistency_level=consistency_level,
        )


class MilvusCollection(BaseCollection):
    def __init__(
        self,
        *,
        backend: "MilvusBackend",
        client: Any,
        config: _MilvusConfig,
        palace: PalaceRef,
        collection_name: str,
        remote_collection: str,
    ):
        self._backend = backend
        self._client = client
        self._config = config
        self._palace = palace
        self._collection_name = collection_name
        self._remote_collection = remote_collection
        self._lock = threading.RLock()
        self._closed = False
        self._known_dimension: Optional[int] = None
        self._known_native_lexical: Optional[bool] = None

    def _ensure_open(self) -> None:
        if self._closed or self._backend._closed:
            raise BackendClosedError("MilvusCollection has been closed")

    def _remote_exists(self) -> bool:
        return bool(self._client.has_collection(self._remote_collection))

    def _marker_exists(self) -> bool:
        return self._backend._marker_exists(self._palace)

    def get_stored_embedder_identity(self):
        return self._backend._get_embedder_identity(self._palace, self._collection_name)

    def set_embedder_identity(self, identity) -> None:
        self._backend._set_embedder_identity(self._palace, self._collection_name, identity)

    @property
    def distance_metric(self) -> str:
        return "cosine"

    def _vector_score_to_distance(self, score: Any) -> float:
        if score is None:
            return 1.0
        value = float(score)
        if self._config.uri and _is_server_uri(self._config.uri):
            return 1.0 - max(-1.0, min(1.0, value))
        return value

    def _remote_dimension(self) -> Optional[int]:
        if not self._remote_exists():
            return None
        try:
            info = self._client.describe_collection(self._remote_collection)
        except Exception:
            logger.debug("Milvus describe_collection failed", exc_info=True)
            return None
        fields = []
        if isinstance(info, dict):
            fields = info.get("fields") or (info.get("schema") or {}).get("fields") or []
        for field in fields:
            name = field.get("name") or field.get("field_name")
            if name != FIELD_VECTOR:
                continue
            params = field.get("params") or field.get("type_params") or {}
            dim = field.get("dim") or params.get("dim")
            try:
                return int(dim)
            except (TypeError, ValueError):
                return None
        return None

    def _ensure_remote_collection(self, dimension: int) -> None:
        if dimension <= 0:
            raise ValueError("embedding dimension must be positive")
        with self._lock:
            self._ensure_open()
            if self._known_dimension is not None:
                if self._known_dimension != dimension:
                    raise DimensionMismatchError(
                        f"milvus collection {self._collection_name!r} expects "
                        f"embedding dimension {self._known_dimension}, got {dimension}"
                    )
                return
            if not self._remote_exists():
                self._backend._create_remote_collection(
                    self._client,
                    self._remote_collection,
                    dimension,
                    consistency_level=self._config.consistency_level,
                )
                self._backend._load_remote_collection(self._client, self._remote_collection)
                self._known_dimension = dimension
                self._known_native_lexical = True
                self._backend._write_marker(self._palace, self._config)
                return
            remote_dim = self._remote_dimension()
            if remote_dim is not None and remote_dim != dimension:
                raise DimensionMismatchError(
                    f"milvus collection {self._collection_name!r} expects "
                    f"embedding dimension {remote_dim}, got {dimension}"
                )
            self._known_dimension = remote_dim or dimension

    def _has_native_lexical(self) -> bool:
        if self._known_native_lexical is not None:
            return self._known_native_lexical
        if not self._remote_exists():
            self._known_native_lexical = False
            return False
        try:
            info = self._client.describe_collection(self._remote_collection)
        except Exception:
            logger.debug("Milvus describe_collection failed", exc_info=True)
            self._known_native_lexical = False
            return False
        fields = []
        if isinstance(info, dict):
            fields = info.get("fields") or (info.get("schema") or {}).get("fields") or []
        self._known_native_lexical = any(
            (field.get("name") or field.get("field_name")) == FIELD_SPARSE for field in fields
        )
        return self._known_native_lexical

    def _output_fields(self, spec: _IncludeSpec) -> list[str]:
        fields = [FIELD_ID]
        if spec.documents:
            fields.append(FIELD_DOCUMENT)
        if spec.embeddings:
            fields.append(FIELD_VECTOR)
        if spec.metadatas:
            fields.append(FIELD_METADATA)
        return list(dict.fromkeys(fields))

    def _extract_metadata(self, row: dict) -> dict:
        metadata = row.get(FIELD_METADATA)
        if isinstance(metadata, dict):
            return metadata
        if isinstance(metadata, str):
            try:
                parsed = json.loads(metadata)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                return parsed
        return {key: value for key, value in row.items() if key not in RESERVED_FIELDS}

    def _row_from_search_hit(self, hit: Any) -> dict:
        if isinstance(hit, dict):
            entity = hit.get("entity") or hit
            doc_id = hit.get(FIELD_ID) or hit.get("id") or entity.get(FIELD_ID)
            score = hit.get("distance", hit.get("score", 0.0))
        else:
            entity = getattr(hit, "entity", None) or {}
            doc_id = getattr(hit, "id", None) or entity.get(FIELD_ID)
            score = getattr(hit, "distance", getattr(hit, "score", 0.0))
        row = dict(entity or {})
        row[FIELD_ID] = str(doc_id)
        row["distance"] = float(score or 0.0)
        return row

    def _prepare_rows(
        self,
        *,
        documents: list[str],
        ids: list[str],
        metadatas: Optional[list[dict]],
        embeddings: list[list[float]],
    ) -> tuple[list[dict], int]:
        if len(documents) != len(ids):
            raise ValueError(
                f"documents length {len(documents)} does not match ids length {len(ids)}"
            )
        if metadatas is not None and len(metadatas) != len(ids):
            raise ValueError(
                f"metadatas length {len(metadatas)} does not match ids length {len(ids)}"
            )
        if len(embeddings) != len(ids):
            raise ValueError(
                f"embeddings length {len(embeddings)} does not match ids length {len(ids)}"
            )
        vectors, dimension = _normalize_vectors(embeddings)
        metadatas = metadatas or [{} for _ in ids]
        rows = []
        for idx, (doc_id, document, metadata, vector) in enumerate(
            zip(ids, documents, metadatas, vectors)
        ):
            if not isinstance(doc_id, str) or not doc_id:
                raise ValueError(f"row {idx}: id must be a non-empty string")
            doc_id_bytes = _utf8_len(doc_id)
            if doc_id_bytes > DRAWER_ID_MAX_LENGTH:
                raise ValueError(
                    f"row {idx}: id byte length {doc_id_bytes} exceeds {DRAWER_ID_MAX_LENGTH}"
                )
            document = _clean_text(document)
            document_bytes = _utf8_len(document)
            if document_bytes > DOCUMENT_MAX_LENGTH:
                raise ValueError(
                    f"row {idx}: document byte length {document_bytes} exceeds "
                    f"Milvus VARCHAR limit {DOCUMENT_MAX_LENGTH}; chunk before storing"
                )
            row = {
                FIELD_ID: doc_id,
                FIELD_DOCUMENT: document,
                FIELD_METADATA: _jsonable_metadata(metadata),
                FIELD_VECTOR: vector,
            }
            row.update(row[FIELD_METADATA])
            rows.append(row)
        return rows, dimension

    def add(self, *, documents, ids, metadatas=None, embeddings=None):
        if embeddings is None:
            raise ValueError("milvus requires explicit embeddings")
        if len(set(ids)) != len(ids):
            raise ValueError("add ids must be unique")
        rows, dimension = self._prepare_rows(
            documents=documents,
            ids=ids,
            metadatas=metadatas,
            embeddings=embeddings,
        )
        if not rows:
            return
        if self._remote_exists():
            existing = self.get(ids=list(ids), include=[])
            if existing.ids:
                raise ValueError(f"ids already exist in milvus collection: {existing.ids}")
        self._ensure_remote_collection(dimension)
        self._client.insert(collection_name=self._remote_collection, data=rows)
        self._backend._write_marker(self._palace, self._config)

    def upsert(self, *, documents, ids, metadatas=None, embeddings=None):
        if embeddings is None:
            raise ValueError("milvus requires explicit embeddings")
        rows, dimension = self._prepare_rows(
            documents=documents,
            ids=ids,
            metadatas=metadatas,
            embeddings=embeddings,
        )
        if not rows:
            return
        self._ensure_remote_collection(dimension)
        self._client.upsert(collection_name=self._remote_collection, data=rows)
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
        existing = self.get(ids=list(ids), include=["documents", "metadatas", "embeddings"])
        by_id = {
            rid: (existing.documents[i], existing.metadatas[i], existing.embeddings[i])
            for i, rid in enumerate(existing.ids)
            if existing.embeddings is not None
        }
        out_ids = []
        out_docs = []
        out_metas = []
        out_embeddings = []
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
                documents=out_docs,
                ids=out_ids,
                metadatas=out_metas,
                embeddings=out_embeddings,
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
            raise ValueError("milvus requires query_embeddings; use palace.get_collection wrapper")
        if query_embeddings is None:
            raise ValueError("query requires query_embeddings")
        if not query_embeddings:
            return QueryResult.empty(
                num_queries=0,
                embeddings_requested=bool(include and "embeddings" in include),
            )
        if not self._remote_exists():
            if self._marker_exists():
                raise CollectionNotInitializedError(self._collection_name)
            return QueryResult.empty(
                num_queries=len(query_embeddings),
                embeddings_requested=bool(include and "embeddings" in include),
            )
        spec = _IncludeSpec.resolve(include, default_distances=True)
        output_fields = self._output_fields(spec)
        filter_expr = _combine_filter(
            translate_where(where), translate_where_document(where_document)
        )
        outer_ids: list[list[str]] = []
        outer_docs: list[list[str]] = []
        outer_metas: list[list[dict]] = []
        outer_dists: list[list[float]] = []
        outer_embeddings: list[list[list[float]]] = []
        for query_vector in query_embeddings:
            q = _as_vector_array(query_vector)
            if self._known_dimension is None:
                self._known_dimension = self._remote_dimension()
            if self._known_dimension is not None and int(q.size) != self._known_dimension:
                raise DimensionMismatchError(
                    f"milvus collection {self._collection_name!r} expects "
                    f"embedding dimension {self._known_dimension}, got {int(q.size)}"
                )
            kwargs = {
                "collection_name": self._remote_collection,
                "data": [q.astype(float).tolist()],
                "limit": int(n_results),
                "output_fields": output_fields,
                "anns_field": FIELD_VECTOR,
                "search_params": {"metric_type": "COSINE"},
                "consistency_level": self._config.consistency_level,
            }
            if filter_expr:
                kwargs["filter"] = filter_expr
            raw = self._client.search(**kwargs)
            hits = raw[0] if raw else []
            rows = [self._row_from_search_hit(hit) for hit in hits]
            outer_ids.append([row[FIELD_ID] for row in rows])
            outer_docs.append(
                [row.get(FIELD_DOCUMENT, "") for row in rows] if spec.documents else []
            )
            outer_metas.append(
                [self._extract_metadata(row) for row in rows] if spec.metadatas else []
            )
            outer_dists.append(
                [self._vector_score_to_distance(row.get("distance")) for row in rows]
                if spec.distances
                else []
            )
            if spec.embeddings:
                outer_embeddings.append([row.get(FIELD_VECTOR) or [] for row in rows])
        return QueryResult(
            ids=outer_ids,
            documents=outer_docs,
            metadatas=outer_metas,
            distances=outer_dists,
            embeddings=outer_embeddings if spec.embeddings else None,
        )

    def _collect_by_filter(
        self,
        *,
        filter_expr: str = "",
        output_fields: Optional[list[str]] = None,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> list[dict]:
        if not self._remote_exists():
            if self._marker_exists():
                raise CollectionNotInitializedError(self._collection_name)
            return []
        fields = output_fields or [FIELD_ID]
        if limit is not None and offset + limit <= _MAX_QUERY_WINDOW:
            kwargs = {
                "collection_name": self._remote_collection,
                "output_fields": fields,
                "limit": int(limit),
                "consistency_level": self._config.consistency_level,
            }
            if filter_expr:
                kwargs["filter"] = filter_expr
            if offset:
                kwargs["offset"] = int(offset)
            return list(self._client.query(**kwargs) or [])
        batch_size = min(_MAX_QUERY_WINDOW, limit or 1000)
        kwargs = {
            "collection_name": self._remote_collection,
            "output_fields": fields,
            "batch_size": int(batch_size),
            "consistency_level": self._config.consistency_level,
        }
        if filter_expr:
            kwargs["filter"] = filter_expr
        iterator = self._client.query_iterator(**kwargs)
        skipped = 0
        rows = []
        try:
            while True:
                batch = iterator.next()
                if not batch:
                    break
                for row in batch:
                    if skipped < offset:
                        skipped += 1
                        continue
                    rows.append(row)
                    if limit is not None and len(rows) >= limit:
                        return rows
        finally:
            try:
                iterator.close()
            except Exception:
                pass
        return rows

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
        output_fields = self._output_fields(spec)
        if ids is not None:
            if not ids:
                return GetResult.empty()
            if not self._remote_exists():
                if self._marker_exists():
                    raise CollectionNotInitializedError(self._collection_name)
                return GetResult.empty()
            records = self._client.get(
                collection_name=self._remote_collection,
                ids=list(ids),
                output_fields=output_fields,
                consistency_level=self._config.consistency_level,
            )
            by_id = {str(row.get(FIELD_ID)): row for row in records or []}
            rows = [by_id[str(doc_id)] for doc_id in ids if str(doc_id) in by_id]
        else:
            filter_expr = _combine_filter(
                translate_where(where),
                translate_where_document(where_document),
            )
            rows = self._collect_by_filter(
                filter_expr=filter_expr,
                output_fields=output_fields,
                limit=limit,
                offset=offset or 0,
            )
        return GetResult(
            ids=[str(row.get(FIELD_ID, "")) for row in rows],
            documents=[row.get(FIELD_DOCUMENT, "") for row in rows] if spec.documents else [],
            metadatas=[self._extract_metadata(row) for row in rows] if spec.metadatas else [],
            embeddings=[row.get(FIELD_VECTOR) or [] for row in rows] if spec.embeddings else None,
        )

    def get_all_metadata(self, where: Optional[dict] = None) -> list[dict]:
        rows = self._collect_by_filter(
            filter_expr=translate_where(where),
            output_fields=[FIELD_ID, FIELD_METADATA],
        )
        return [self._extract_metadata(row) for row in rows]

    def delete(self, *, ids=None, where=None):
        filter_expr = translate_where(where)
        if ids is None and where is None:
            raise ValueError("delete requires either ids= or where=")
        if not self._remote_exists():
            if self._marker_exists():
                raise CollectionNotInitializedError(self._collection_name)
            return
        if ids is not None and where is not None:
            rows = self._collect_by_filter(
                filter_expr=filter_expr,
                output_fields=[FIELD_ID],
            )
            allowed = {row[FIELD_ID] for row in rows}
            ids = [doc_id for doc_id in ids if doc_id in allowed]
        if ids is not None:
            if not ids:
                return
            self._client.delete(collection_name=self._remote_collection, ids=list(ids))
        else:
            self._client.delete(collection_name=self._remote_collection, filter=filter_expr)

    def count(self) -> int:
        if not self._remote_exists():
            if self._marker_exists():
                raise CollectionNotInitializedError(self._collection_name)
            return 0
        try:
            rows = self._client.query(
                collection_name=self._remote_collection,
                filter="",
                output_fields=["count(*)"],
                consistency_level=self._config.consistency_level,
            )
            if rows:
                first = rows[0]
                return int(first.get("count(*)", first.get("count", 0)))
        except Exception:
            logger.debug("Milvus count(*) query failed; falling back to stats", exc_info=True)
        try:
            stats = self._client.get_collection_stats(self._remote_collection)
            return int(stats.get("row_count", stats.get("num_entities", 0)))
        except Exception as exc:
            raise BackendError(f"Milvus count failed: {exc}") from exc

    def lexical_search(self, *, query: str, n_results: int = 10, where: Optional[dict] = None):
        filter_expr = translate_where(where)
        if not self._remote_exists():
            if self._marker_exists():
                raise CollectionNotInitializedError(self._collection_name)
            return LexicalResult(hits=[])
        if not self._has_native_lexical():
            raise BackendError(
                "milvus lexical search requires a collection created with native BM25 support"
            )
        return self._native_lexical_search(
            query=query,
            n_results=n_results,
            filter_expr=filter_expr,
        )

    def _native_lexical_search(
        self,
        *,
        query: str,
        n_results: int,
        filter_expr: str,
    ) -> LexicalResult:
        try:
            kwargs = {
                "collection_name": self._remote_collection,
                "data": [query],
                "anns_field": FIELD_SPARSE,
                "output_fields": [FIELD_ID, FIELD_DOCUMENT, FIELD_METADATA],
                "limit": int(n_results),
                "search_params": {"params": {}},
                "consistency_level": self._config.consistency_level,
            }
            if filter_expr:
                kwargs["filter"] = filter_expr
            raw = self._client.search(**kwargs)
        except Exception as exc:
            raise BackendError("Milvus native BM25 search failed") from exc
        rows = [self._row_from_search_hit(hit) for hit in (raw[0] if raw else [])]
        return LexicalResult(
            hits=[
                LexicalHit(
                    id=str(row.get(FIELD_ID, "")),
                    document=row.get(FIELD_DOCUMENT, ""),
                    metadata=self._extract_metadata(row),
                    score=float(row.get("distance", row.get("score", 0.0)) or 0.0),
                )
                for row in rows
            ]
        )

    def close(self) -> None:
        self._closed = True

    def health(self) -> HealthStatus:
        if self._closed or self._backend._closed:
            return HealthStatus.unhealthy("collection closed")
        try:
            if not self._remote_exists():
                return HealthStatus.unhealthy("milvus collection not found")
        except Exception as exc:
            return HealthStatus.unhealthy(str(exc))
        return HealthStatus.healthy()


class MilvusBackend(BaseBackend):
    name = "milvus"
    capabilities = frozenset(
        {
            "requires_explicit_embeddings",
            "supports_embeddings_in",
            "supports_embeddings_passthrough",
            "supports_embeddings_out",
            "supports_metadata_filters",
            "supports_lexical_search",
            "supports_namespace_isolation",
            "server_mode",
        }
    )

    def __init__(self):
        self._clients: dict[_MilvusConfig, Any] = {}
        self._collections_by_palace: dict[str, list[MilvusCollection]] = {}
        self._lock = threading.RLock()
        self._closed = False

    @staticmethod
    def _marker_path(palace_path: str) -> str:
        return os.path.join(palace_path, _MARKER_FILENAME)

    @staticmethod
    def _palace_hash(palace: PalaceRef) -> str:
        return sha256(palace.id.encode("utf-8", errors="surrogatepass")).hexdigest()[:16]

    def _effective_config(self, palace: PalaceRef, options: Optional[dict]) -> _MilvusConfig:
        config = _MilvusConfig.from_options(options)
        namespace = palace.namespace or config.namespace
        if config.uri:
            return _MilvusConfig(
                uri=config.uri,
                token=config.token,
                db_name=config.db_name,
                namespace=namespace,
                db_filename=config.db_filename,
                consistency_level=config.consistency_level,
            )
        if not palace.local_path:
            raise BackendError(
                "milvus backend requires a local palace path when no URI is configured"
            )
        return _MilvusConfig(
            uri=os.path.join(palace.local_path, config.db_filename),
            token=config.token,
            db_name=config.db_name,
            namespace=namespace,
            db_filename=config.db_filename,
            consistency_level=config.consistency_level,
        )

    def _marker_target(self, palace: PalaceRef, config: _MilvusConfig) -> dict:
        return {
            "uri": config.uri,
            "db_name": config.db_name,
            "namespace": config.namespace,
            "palace_hash": self._palace_hash(palace),
            "remote_prefix": self._remote_collection_prefix(palace=palace, config=config),
        }

    def _remote_collection_prefix(self, *, palace: PalaceRef, config: _MilvusConfig) -> str:
        parts = ["mempalace"]
        if config.namespace:
            parts.append(_slug(config.namespace, "namespace"))
        parts.append(self._palace_hash(palace))
        return "_".join(parts)

    def _remote_collection_name(
        self,
        *,
        palace: PalaceRef,
        collection_name: str,
        config: _MilvusConfig,
    ) -> str:
        if config.uri and not _is_server_uri(config.uri) and not config.namespace:
            return _slug(collection_name)
        prefix = self._remote_collection_prefix(palace=palace, config=config)
        return f"{prefix}_{_slug(collection_name)}"

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
            raise BackendMismatchError(f"milvus marker is unreadable: {marker_path}") from exc
        return marker if isinstance(marker, dict) else {}

    def _validate_marker_target(self, palace: PalaceRef, config: _MilvusConfig) -> None:
        marker = self._read_marker(palace)
        if marker is None:
            return
        if marker.get("backend") != self.name:
            raise BackendMismatchError("milvus marker does not identify the milvus backend")
        expected = self._marker_target(palace, config)
        actual = marker.get("milvus")
        if not isinstance(actual, dict):
            raise BackendMismatchError("milvus marker is missing target metadata")
        mismatched = [
            key for key, expected_value in expected.items() if actual.get(key) != expected_value
        ]
        if mismatched:
            raise BackendMismatchError(
                "milvus marker target does not match current configuration "
                f"({', '.join(mismatched)}); keep MEMPALACE_MILVUS_URI and "
                "Milvus database/namespace settings consistent or use a fresh palace directory"
            )

    def _write_marker(self, palace: PalaceRef, config: _MilvusConfig) -> None:
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
            "milvus": self._marker_target(palace, config),
        }
        marker_path = self._marker_path(palace.local_path)
        with open(marker_path, "w", encoding="utf-8") as f:
            json.dump(marker, f, indent=2, ensure_ascii=False)
        try:
            os.chmod(marker_path, 0o600)
        except (OSError, NotImplementedError):
            pass

    @staticmethod
    def _embedder_sidecar_path(palace: PalaceRef) -> Optional[str]:
        if not palace.local_path:
            return None
        return os.path.join(palace.local_path, EMBEDDER_SIDECAR_FILENAME)

    def _get_embedder_identity(self, palace: PalaceRef, collection_name: str):
        return read_embedder_sidecar(self._embedder_sidecar_path(palace), collection_name)

    def _set_embedder_identity(self, palace: PalaceRef, collection_name: str, identity) -> None:
        write_embedder_sidecar(self._embedder_sidecar_path(palace), collection_name, identity)

    def _client(self, config: _MilvusConfig):
        if self._closed:
            raise BackendClosedError("MilvusBackend has been closed")
        with self._lock:
            client = self._clients.get(config)
            if client is not None:
                return client
            if config.uri and not _is_server_uri(config.uri):
                parent = os.path.dirname(os.path.abspath(config.uri)) or "."
                os.makedirs(parent, exist_ok=True)
                try:
                    os.chmod(parent, 0o700)
                except (OSError, NotImplementedError):
                    pass
            from pymilvus import MilvusClient

            kwargs = {"uri": config.uri or DEFAULT_DB_FILENAME}
            if config.token:
                kwargs["token"] = config.token
            if config.db_name:
                kwargs["db_name"] = config.db_name
            client = MilvusClient(**kwargs)
            self._clients[config] = client
            return client

    def get_collection(self, *args, **kwargs) -> MilvusCollection:
        palace, collection_name, create, options = self._normalize_args(args, kwargs)
        config = self._effective_config(palace, options)
        if palace.local_path:
            if create:
                os.makedirs(palace.local_path, exist_ok=True)
                try:
                    os.chmod(palace.local_path, 0o700)
                except (OSError, NotImplementedError):
                    pass
            marker_path = self._marker_path(palace.local_path)
            local_db_exists = bool(
                config.uri and not _is_server_uri(config.uri) and os.path.exists(config.uri)
            )
            if os.path.isfile(marker_path):
                self._validate_marker_target(palace, config)
            elif not create and not local_db_exists:
                raise PalaceNotFoundError(marker_path)
        else:
            raise BackendError(
                "milvus backend requires a local palace path to anchor mismatch protection"
            )
        client = self._client(config)
        remote_collection = self._remote_collection_name(
            palace=palace,
            collection_name=collection_name,
            config=config,
        )
        if not create and not client.has_collection(remote_collection):
            raise CollectionNotInitializedError(collection_name)
        if client.has_collection(remote_collection):
            self._load_remote_collection(client, remote_collection)
        collection = MilvusCollection(
            backend=self,
            client=client,
            config=config,
            palace=palace,
            collection_name=collection_name,
            remote_collection=remote_collection,
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

    def _create_remote_collection(
        self,
        client,
        collection_name: str,
        dimension: int,
        *,
        consistency_level: str,
    ) -> None:
        self._create_remote_collection_schema(
            client,
            collection_name,
            dimension,
            consistency_level=consistency_level,
        )

    def _create_remote_collection_schema(
        self,
        client,
        collection_name: str,
        dimension: int,
        *,
        consistency_level: str,
    ) -> None:
        from pymilvus import DataType

        schema = client.create_schema(auto_id=False, enable_dynamic_field=True)
        schema.add_field(
            field_name=FIELD_ID,
            datatype=DataType.VARCHAR,
            is_primary=True,
            max_length=DRAWER_ID_MAX_LENGTH,
        )
        schema.add_field(
            field_name=FIELD_DOCUMENT,
            datatype=DataType.VARCHAR,
            max_length=DOCUMENT_MAX_LENGTH,
            enable_analyzer=True,
            enable_match=True,
        )
        schema.add_field(field_name=FIELD_METADATA, datatype=DataType.JSON)
        schema.add_field(
            field_name=FIELD_VECTOR,
            datatype=DataType.FLOAT_VECTOR,
            dim=int(dimension),
        )
        from pymilvus import Function, FunctionType

        schema.add_field(field_name=FIELD_SPARSE, datatype=DataType.SPARSE_FLOAT_VECTOR)
        schema.add_function(
            Function(
                name="document_bm25",
                input_field_names=[FIELD_DOCUMENT],
                output_field_names=[FIELD_SPARSE],
                function_type=FunctionType.BM25,
            )
        )
        index_params = client.prepare_index_params()
        index_params.add_index(
            field_name=FIELD_VECTOR,
            index_type="AUTOINDEX",
            metric_type="COSINE",
        )
        index_params.add_index(
            field_name=FIELD_SPARSE,
            index_type="SPARSE_INVERTED_INDEX",
            metric_type="BM25",
            params={"inverted_index_algo": "DAAT_MAXSCORE"},
        )
        client.create_collection(
            collection_name=collection_name,
            schema=schema,
            index_params=index_params,
            consistency_level=consistency_level,
        )

    def _load_remote_collection(self, client, collection_name: str) -> None:
        try:
            client.load_collection(collection_name)
        except Exception:
            logger.debug("Milvus load_collection skipped", exc_info=True)

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
            try:
                client.close()
            except Exception:
                logger.debug("Milvus client close failed", exc_info=True)

    def health(self, palace: Optional[PalaceRef] = None) -> HealthStatus:
        if self._closed:
            return HealthStatus.unhealthy("backend closed")
        if palace and palace.local_path and not self._marker_exists(palace):
            return HealthStatus.unhealthy("milvus marker not found")
        try:
            if palace is None:
                config = _MilvusConfig.from_options()
                if not config.uri:
                    return HealthStatus.healthy("milvus lite uses per-palace local files")
            else:
                config = self._effective_config(palace, None)
            client = self._client(config)
            client.list_collections()
        except Exception as exc:
            return HealthStatus.unhealthy(str(exc))
        return HealthStatus.healthy()

    @classmethod
    def detect(cls, path: str) -> bool:
        return os.path.isfile(os.path.join(path, _MARKER_FILENAME)) or os.path.exists(
            os.path.join(path, DEFAULT_DB_FILENAME)
        )

    def create_collection(self, palace_path: str, collection_name: str) -> MilvusCollection:
        collection = self.get_collection(palace_path, collection_name, create=True)
        if collection._remote_exists():
            raise ValueError(f"collection {collection_name!r} already exists")
        return collection

    def get_or_create_collection(self, palace_path: str, collection_name: str):
        return self.get_collection(palace_path, collection_name, create=True)

    def delete_collection(self, palace_path: str, collection_name: str) -> None:
        palace = PalaceRef(id=palace_path, local_path=palace_path)
        config = self._effective_config(palace, None)
        client = self._client(config)
        remote_collection = self._remote_collection_name(
            palace=palace,
            collection_name=collection_name,
            config=config,
        )
        if client.has_collection(remote_collection):
            client.drop_collection(remote_collection)


__all__ = [
    "DEFAULT_DB_FILENAME",
    "DOCUMENT_MAX_LENGTH",
    "DRAWER_ID_MAX_LENGTH",
    "MilvusBackend",
    "MilvusCollection",
    "translate_where",
    "translate_where_document",
]
