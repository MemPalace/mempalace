"""Qdrant REST backend for MemPalace.

Qdrant is an opt-in external-service backend. Chroma remains the default; this
adapter only runs when the user explicitly selects ``qdrant`` via config, env,
or CLI/MCP flag. Embeddings are still produced locally by MemPalace through the
core embedding wrapper before vectors are sent to Qdrant.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Optional
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

import numpy as np

from .base import (
    BackendClosedError,
    BackendMismatchError,
    BackendError,
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

_DEFAULT_URL = "http://localhost:6333"
_MARKER_FILENAME = "qdrant_backend.json"
_PAYLOAD_ID = "mempalace_id"
_PAYLOAD_DOCUMENT = "document"
_PAYLOAD_METADATA = "metadata"
_POINT_NAMESPACE = uuid.UUID("c06c3fc7-5c14-4dc4-84c2-24a5f72d8dc1")
_TOKEN_RE = re.compile(r"\w{2,}", re.UNICODE)
_SUPPORTED_OPERATORS = frozenset(
    {"$eq", "$ne", "$in", "$nin", "$and", "$or", "$contains", "$gt", "$gte", "$lt", "$lte"}
)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


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
                raise UnsupportedFilterError(f"operator {key!r} not supported by qdrant")
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
    raise UnsupportedFilterError(f"operator {op!r} not supported by qdrant")


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
            raise UnsupportedFilterError(f"operator {key!r} not supported by qdrant")
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
        raise DimensionMismatchError(f"qdrant batch cannot mix embedding dimensions {sorted(dims)}")
    return vectors, dims.pop() if dims else 0


def _jsonable_metadata(meta: dict | None) -> dict:
    try:
        value = json.loads(json.dumps(meta or {}, ensure_ascii=False))
    except (TypeError, ValueError):
        value = {}
    return value if isinstance(value, dict) else {}


def _point_id(doc_id: str) -> str:
    return str(uuid.uuid5(_POINT_NAMESPACE, str(doc_id)))


def _slug(value: str, fallback: str = "palace") -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", value).strip("_")
    safe = safe or fallback
    if len(safe) <= 64:
        return safe
    digest = sha256(value.encode("utf-8", errors="surrogatepass")).hexdigest()[:12]
    return f"{safe[:51]}_{digest}"


def _payload_row(point: dict) -> dict:
    payload = point.get("payload") or {}
    meta = payload.get(_PAYLOAD_METADATA) or {}
    if not isinstance(meta, dict):
        meta = {}
    vector = point.get("vector")
    if isinstance(vector, dict):
        vector = vector.get("") or vector.get("default") or next(iter(vector.values()), None)
    return {
        "id": str(payload.get(_PAYLOAD_ID) or point.get("id") or ""),
        "document": str(payload.get(_PAYLOAD_DOCUMENT) or ""),
        "metadata": meta,
        "embedding": vector if isinstance(vector, list) else None,
        "score": point.get("score"),
    }


def _vector_distance(query: np.ndarray, vector: list[float] | None) -> Optional[float]:
    if vector is None:
        return None
    vec = _as_vector_array(vector)
    if vec.size != query.size:
        return None
    denom = float(np.linalg.norm(query)) * float(np.linalg.norm(vec))
    cos = 0.0 if denom <= 0 else float(np.dot(query, vec) / denom)
    return 1.0 - max(-1.0, min(1.0, cos))


def _qdrant_score_to_distance(score: Any) -> float:
    try:
        return 1.0 - max(-1.0, min(1.0, float(score)))
    except (TypeError, ValueError):
        return 1.0


class _QdrantHTTPError(BackendError):
    def __init__(self, status: int, detail: str):
        super().__init__(f"Qdrant HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class _QdrantConfig:
    url: str = _DEFAULT_URL
    api_key: Optional[str] = None
    timeout: float = 10.0
    namespace: Optional[str] = None

    @classmethod
    def from_options(cls, options: Optional[dict] = None) -> "_QdrantConfig":
        options = options or {}
        try:
            from ..config import MempalaceConfig

            cfg = MempalaceConfig()
        except Exception:  # pragma: no cover - config import should be boring
            cfg = None
        url = (
            options.get("url")
            or os.environ.get("MEMPALACE_QDRANT_URL")
            or getattr(cfg, "qdrant_url", None)
            or _DEFAULT_URL
        )
        api_key = (
            options.get("api_key")
            or os.environ.get("MEMPALACE_QDRANT_API_KEY")
            or getattr(cfg, "qdrant_api_key", None)
        )
        namespace = (
            options.get("namespace")
            or os.environ.get("MEMPALACE_QDRANT_NAMESPACE")
            or getattr(cfg, "qdrant_namespace", None)
        )
        raw_timeout = (
            options.get("timeout")
            or os.environ.get("MEMPALACE_QDRANT_TIMEOUT")
            or getattr(cfg, "qdrant_timeout", None)
            or 10.0
        )
        try:
            timeout = float(raw_timeout)
        except (TypeError, ValueError):
            timeout = 10.0
        if timeout <= 0:
            timeout = 10.0
        return cls(
            url=str(url).rstrip("/") or _DEFAULT_URL,
            api_key=str(api_key) if api_key else None,
            timeout=timeout,
            namespace=str(namespace).strip() or None if namespace else None,
        )


class _QdrantRESTClient:
    def __init__(self, config: _QdrantConfig):
        self._config = config

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Optional[dict] = None,
        query: Optional[dict] = None,
    ) -> dict:
        url = f"{self._config.url}{path}"
        if query:
            url = f"{url}?{urlparse.urlencode(query)}"
        data = None
        headers = {"Content-Type": "application/json"}
        if self._config.api_key:
            headers["api-key"] = self._config.api_key
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urlrequest.Request(url, data=data, method=method, headers=headers)
        try:
            with urlrequest.urlopen(req, timeout=self._config.timeout) as resp:
                raw = resp.read()
        except urlerror.HTTPError as exc:
            raw = exc.read()
            detail = raw.decode("utf-8", errors="replace") if raw else str(exc)
            raise _QdrantHTTPError(exc.code, detail) from exc
        except urlerror.URLError as exc:
            raise BackendError(f"Qdrant request failed: {exc.reason}") from exc
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise BackendError("Qdrant returned invalid JSON") from exc

    def collection_exists(self, collection: str) -> bool:
        try:
            self.request("GET", f"/collections/{urlparse.quote(collection, safe='')}")
        except _QdrantHTTPError as exc:
            if exc.status == 404:
                return False
            raise
        return True

    def get_collection_info(self, collection: str) -> dict:
        return self.request("GET", f"/collections/{urlparse.quote(collection, safe='')}")

    def create_collection(self, collection: str, dimension: int) -> None:
        self.request(
            "PUT",
            f"/collections/{urlparse.quote(collection, safe='')}",
            body={"vectors": {"size": int(dimension), "distance": "Cosine"}},
        )

    def create_payload_index(self, collection: str, field_name: str, field_schema: str) -> None:
        try:
            self.request(
                "PUT",
                f"/collections/{urlparse.quote(collection, safe='')}/index",
                query={"wait": "true"},
                body={"field_name": field_name, "field_schema": field_schema},
            )
        except _QdrantHTTPError as exc:
            if exc.status in (400, 409):
                logger.debug("Qdrant payload index creation skipped: %s", exc)
                return
            raise

    def upsert_points(self, collection: str, points: list[dict]) -> None:
        self.request(
            "PUT",
            f"/collections/{urlparse.quote(collection, safe='')}/points",
            query={"wait": "true"},
            body={"points": points},
        )

    def query_points(
        self,
        collection: str,
        *,
        vector: list[float],
        limit: int,
        qdrant_filter: Optional[dict],
        with_vector: bool,
    ) -> list[dict]:
        body = {
            "query": vector,
            "limit": int(limit),
            "with_payload": True,
            "with_vector": bool(with_vector),
        }
        if qdrant_filter:
            body["filter"] = qdrant_filter
        try:
            response = self.request(
                "POST",
                f"/collections/{urlparse.quote(collection, safe='')}/points/query",
                body=body,
            )
        except _QdrantHTTPError as exc:
            if exc.status not in (404, 405):
                raise
            body = {
                "vector": vector,
                "limit": int(limit),
                "with_payload": True,
                "with_vector": bool(with_vector),
            }
            if qdrant_filter:
                body["filter"] = qdrant_filter
            response = self.request(
                "POST",
                f"/collections/{urlparse.quote(collection, safe='')}/points/search",
                body=body,
            )
        result = response.get("result") or {}
        if isinstance(result, list):
            return result
        return list(result.get("points") or [])

    def scroll_points(
        self,
        collection: str,
        *,
        qdrant_filter: Optional[dict] = None,
        limit: int = 256,
        offset: Any = None,
        with_vector: bool = False,
    ) -> tuple[list[dict], Any]:
        body: dict[str, Any] = {
            "limit": int(limit),
            "with_payload": True,
            "with_vector": bool(with_vector),
        }
        if qdrant_filter:
            body["filter"] = qdrant_filter
        if offset is not None:
            body["offset"] = offset
        response = self.request(
            "POST",
            f"/collections/{urlparse.quote(collection, safe='')}/points/scroll",
            body=body,
        )
        result = response.get("result") or {}
        return list(result.get("points") or []), result.get("next_page_offset")

    def delete_points(
        self,
        collection: str,
        *,
        point_ids: Optional[list[str]] = None,
        qdrant_filter: Optional[dict] = None,
    ) -> None:
        selector = (
            {"points": point_ids or []}
            if point_ids is not None
            else {"filter": qdrant_filter or {}}
        )
        self.request(
            "POST",
            f"/collections/{urlparse.quote(collection, safe='')}/points/delete",
            query={"wait": "true"},
            body=selector,
        )

    def count_points(self, collection: str) -> int:
        response = self.request(
            "POST",
            f"/collections/{urlparse.quote(collection, safe='')}/points/count",
            body={"exact": True},
        )
        result = response.get("result") or {}
        return int(result.get("count") or 0)

    def delete_collection(self, collection: str) -> None:
        self.request("DELETE", f"/collections/{urlparse.quote(collection, safe='')}")


def _condition(field: str, expression: Any) -> tuple[Optional[dict], list[dict]]:
    key = f"{_PAYLOAD_METADATA}.{field}"
    if isinstance(expression, dict):
        conditions = []
        must_not = []
        for op, operand in expression.items():
            if op == "$eq":
                conditions.append({"key": key, "match": {"value": operand}})
            elif op == "$ne":
                must_not.append({"key": key, "match": {"value": operand}})
            elif op == "$in":
                conditions.append({"key": key, "match": {"any": operand or []}})
            elif op == "$nin":
                must_not.append({"key": key, "match": {"any": operand or []}})
            elif op in ("$gt", "$gte", "$lt", "$lte"):
                range_key = {"$gt": "gt", "$gte": "gte", "$lt": "lt", "$lte": "lte"}[op]
                conditions.append({"key": key, "range": {range_key: operand}})
            else:
                return None, []
        if len(conditions) == 1 and not must_not:
            return conditions[0], []
        body: dict[str, Any] = {}
        if conditions:
            body["must"] = conditions
        if must_not:
            body["must_not"] = must_not
        return body, []
    return {"key": key, "match": {"value": expression}}, []


def _requires_local_filter(where: Optional[dict], where_document: Optional[dict] = None) -> bool:
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
            if key in ("$or", "$contains"):
                return True
            if isinstance(value, dict):
                if "$contains" in value:
                    return True
                stack.append(value)
            elif isinstance(value, list):
                stack.extend(item for item in value if isinstance(item, dict))
    return False


def _qdrant_filter(where: Optional[dict]) -> Optional[dict]:
    if not where:
        return None
    _validate_where(where)
    must = []
    must_not = []
    for key, expected in where.items():
        if key == "$and":
            for clause in expected or []:
                child = _qdrant_filter(clause)
                if child:
                    must.append(child)
            continue
        if key == "$or":
            return None
        if key.startswith("$"):
            return None
        condition, not_conditions = _condition(key, expected)
        if condition is None:
            return None
        must.append(condition)
        must_not.extend(not_conditions)
    out: dict[str, Any] = {}
    if must:
        out["must"] = must
    if must_not:
        out["must_not"] = must_not
    return out or None


def _combine_filters(*filters: Optional[dict]) -> Optional[dict]:
    present = [flt for flt in filters if flt]
    if not present:
        return None
    if len(present) == 1:
        return present[0]
    return {"must": present}


def _text_any_filter(query: str) -> Optional[dict]:
    tokens = _tokenize(query)
    if not tokens:
        return None
    return {"must": [{"key": _PAYLOAD_DOCUMENT, "match": {"text_any": " ".join(tokens)}}]}


class QdrantCollection(BaseCollection):
    def __init__(
        self,
        *,
        backend: "QdrantBackend",
        client: _QdrantRESTClient,
        config: _QdrantConfig,
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

    def _ensure_open(self) -> None:
        if self._closed or self._backend._closed:
            raise BackendClosedError("QdrantCollection has been closed")

    def _remote_exists(self) -> bool:
        return self._client.collection_exists(self._remote_collection)

    def _marker_exists(self) -> bool:
        return self._backend._marker_exists(self._palace)

    def _remote_dimension(self) -> Optional[int]:
        try:
            info = self._client.get_collection_info(self._remote_collection)
        except _QdrantHTTPError as exc:
            if exc.status == 404:
                return None
            raise
        result = info.get("result") or info
        params = (result.get("config") or {}).get("params") or {}
        vectors = params.get("vectors") or params.get("vectors_config") or {}
        if isinstance(vectors, dict) and "size" in vectors:
            return int(vectors["size"])
        if isinstance(vectors, dict):
            for value in vectors.values():
                if isinstance(value, dict) and "size" in value:
                    return int(value["size"])
        return None

    def _ensure_remote_collection(self, dimension: int) -> None:
        if dimension <= 0:
            raise ValueError("embedding dimension must be positive")
        with self._lock:
            self._ensure_open()
            if self._known_dimension is not None:
                if self._known_dimension != dimension:
                    raise DimensionMismatchError(
                        f"qdrant collection {self._collection_name!r} expects "
                        f"embedding dimension {self._known_dimension}, got {dimension}"
                    )
                return
            if not self._remote_exists():
                self._client.create_collection(self._remote_collection, dimension)
                self._client.create_payload_index(
                    self._remote_collection, _PAYLOAD_DOCUMENT, "text"
                )
                self._known_dimension = dimension
                return
            remote_dim = self._remote_dimension()
            if remote_dim is not None and remote_dim != dimension:
                raise DimensionMismatchError(
                    f"qdrant collection {self._collection_name!r} expects "
                    f"embedding dimension {remote_dim}, got {dimension}"
                )
            self._known_dimension = remote_dim or dimension

    def _scroll_all(
        self,
        *,
        qdrant_filter: Optional[dict] = None,
        with_vector: bool = False,
    ) -> list[dict]:
        self._ensure_open()
        if not self._remote_exists():
            if self._marker_exists():
                raise CollectionNotInitializedError(self._collection_name)
            return []
        rows = []
        offset = None
        while True:
            points, offset = self._client.scroll_points(
                self._remote_collection,
                qdrant_filter=qdrant_filter,
                limit=256,
                offset=offset,
                with_vector=with_vector,
            )
            rows.extend(_payload_row(point) for point in points)
            if offset is None:
                return rows

    def _rows(
        self,
        *,
        ids: Optional[list[str]] = None,
        where: Optional[dict] = None,
        where_document: Optional[dict] = None,
        with_vector: bool = False,
    ) -> list[dict]:
        _validate_where(where)
        _validate_where(where_document)
        q_filter = None if _requires_local_filter(where, where_document) else _qdrant_filter(where)
        if ids is not None:
            id_filter = {"must": [{"has_id": [_point_id(doc_id) for doc_id in ids]}]}
            q_filter = _combine_filters(q_filter, id_filter)
        rows = self._scroll_all(qdrant_filter=q_filter, with_vector=with_vector)
        rows = [
            row
            for row in rows
            if (ids is None or row["id"] in set(ids))
            and _matches_where(row["metadata"], where)
            and _matches_where_document(row["document"], where_document)
        ]
        return rows

    def add(self, *, documents, ids, metadatas=None, embeddings=None):
        _validate_write_batch(
            documents=documents,
            ids=ids,
            metadatas=metadatas,
            embeddings=embeddings,
        )
        if embeddings is None:
            raise ValueError("qdrant requires explicit embeddings")
        if len(set(ids)) != len(ids):
            raise ValueError("add ids must be unique")
        existing = self.get(ids=list(ids), include=[])
        if existing.ids:
            raise ValueError(f"ids already exist in qdrant collection: {existing.ids}")
        self.upsert(documents=documents, ids=ids, metadatas=metadatas, embeddings=embeddings)

    def upsert(self, *, documents, ids, metadatas=None, embeddings=None):
        _validate_write_batch(
            documents=documents,
            ids=ids,
            metadatas=metadatas,
            embeddings=embeddings,
        )
        if embeddings is None:
            raise ValueError("qdrant requires explicit embeddings")
        vectors, dimension = _normalize_vectors(embeddings)
        self._ensure_remote_collection(dimension)
        metadatas = metadatas or [{} for _ in ids]
        points = []
        for doc_id, doc, meta, vector in zip(ids, documents, metadatas, vectors):
            points.append(
                {
                    "id": _point_id(doc_id),
                    "vector": vector,
                    "payload": {
                        _PAYLOAD_ID: str(doc_id),
                        _PAYLOAD_DOCUMENT: str(doc),
                        _PAYLOAD_METADATA: _jsonable_metadata(meta),
                        "updated_at": _utcnow(),
                    },
                }
            )
        self._client.upsert_points(self._remote_collection, points)
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

    def _query_local_exact(
        self,
        *,
        query_embeddings: list[list[float]],
        n_results: int,
        where: Optional[dict],
        where_document: Optional[dict],
        include: Optional[list[str]],
    ) -> QueryResult:
        spec = _IncludeSpec.resolve(include, default_distances=True)
        q_filter = None if _requires_local_filter(where, where_document) else _qdrant_filter(where)
        rows = self._scroll_all(qdrant_filter=q_filter, with_vector=True)
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
            raise ValueError("qdrant requires query_embeddings; use palace.get_collection wrapper")
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
        if not self._remote_exists():
            if self._marker_exists():
                raise CollectionNotInitializedError(self._collection_name)
            return QueryResult.empty(
                num_queries=len(query_embeddings),
                embeddings_requested=bool(include and "embeddings" in include),
            )

        spec = _IncludeSpec.resolve(include, default_distances=True)
        q_filter = _qdrant_filter(where)
        outer_ids: list[list[str]] = []
        outer_docs: list[list[str]] = []
        outer_metas: list[list[dict]] = []
        outer_dists: list[list[float]] = []
        outer_embeds: list[list[list[float]]] = []
        for query_vector in query_embeddings:
            q = _as_vector_array(query_vector)
            if self._known_dimension is None:
                self._known_dimension = self._remote_dimension()
            if self._known_dimension is not None and int(q.size) != self._known_dimension:
                raise DimensionMismatchError(
                    f"qdrant collection {self._collection_name!r} expects "
                    f"embedding dimension {self._known_dimension}, got {int(q.size)}"
                )
            points = self._client.query_points(
                self._remote_collection,
                vector=q.astype(float).tolist(),
                limit=n_results,
                qdrant_filter=q_filter,
                with_vector=spec.embeddings,
            )
            rows = [_payload_row(point) for point in points]
            outer_ids.append([row["id"] for row in rows])
            outer_docs.append([row["document"] for row in rows] if spec.documents else [])
            outer_metas.append([row["metadata"] for row in rows] if spec.metadatas else [])
            outer_dists.append(
                [_qdrant_score_to_distance(row["score"]) for row in rows] if spec.distances else []
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
            ids=ids,
            where=where,
            where_document=where_document,
            with_vector=spec.embeddings,
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
        if not self._remote_exists():
            if self._marker_exists():
                raise CollectionNotInitializedError(self._collection_name)
            return
        if ids is not None and where is None:
            self._client.delete_points(
                self._remote_collection,
                point_ids=[_point_id(doc_id) for doc_id in ids],
            )
            return
        if ids is None and where is not None and not _requires_local_filter(where):
            q_filter = _qdrant_filter(where)
            self._client.delete_points(self._remote_collection, qdrant_filter=q_filter)
            return
        rows = self._rows(ids=ids, where=where)
        if rows:
            self._client.delete_points(
                self._remote_collection,
                point_ids=[_point_id(row["id"]) for row in rows],
            )

    def count(self) -> int:
        self._ensure_open()
        if not self._remote_exists():
            if self._marker_exists():
                raise CollectionNotInitializedError(self._collection_name)
            return 0
        return self._client.count_points(self._remote_collection)

    def lexical_search(self, *, query: str, n_results: int = 10, where: Optional[dict] = None):
        _validate_where(where)
        q_filter = None if _requires_local_filter(where) else _qdrant_filter(where)
        rows = []
        text_filter = _text_any_filter(query)
        text_filter_success = False
        if text_filter:
            try:
                rows = self._scroll_all(
                    qdrant_filter=_combine_filters(q_filter, text_filter),
                    with_vector=False,
                )
                text_filter_success = True
            except BackendError:
                logger.debug(
                    "Qdrant text filter failed; falling back to lexical scan", exc_info=True
                )
                rows = []
        if not text_filter_success:
            rows = self._scroll_all(qdrant_filter=q_filter, with_vector=False)
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
            if not self._client.collection_exists(self._remote_collection):
                return HealthStatus.unhealthy("qdrant collection not found")
        except Exception as exc:  # noqa: BLE001 - backend health should summarize
            return HealthStatus.unhealthy(str(exc))
        return HealthStatus.healthy()


class QdrantBackend(BaseBackend):
    name = "qdrant"
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
        self._clients: dict[_QdrantConfig, _QdrantRESTClient] = {}
        self._collections_by_palace: dict[str, list[QdrantCollection]] = {}
        self._lock = threading.RLock()
        self._closed = False

    @staticmethod
    def _marker_path(palace_path: str) -> str:
        return os.path.join(palace_path, _MARKER_FILENAME)

    @staticmethod
    def _palace_hash(palace: PalaceRef) -> str:
        return sha256(palace.id.encode("utf-8", errors="surrogatepass")).hexdigest()[:16]

    def _remote_collection_prefix(self, *, palace: PalaceRef, config: _QdrantConfig) -> str:
        parts = ["mempalace"]
        if config.namespace:
            parts.append(_slug(config.namespace, "namespace"))
        parts.append(self._palace_hash(palace))
        return "_".join(parts)

    def _marker_target(self, palace: PalaceRef, config: _QdrantConfig) -> dict:
        return {
            "url": config.url,
            "namespace": config.namespace,
            "palace_hash": self._palace_hash(palace),
            "remote_prefix": self._remote_collection_prefix(palace=palace, config=config),
        }

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
            raise BackendMismatchError(f"qdrant marker is unreadable: {marker_path}") from exc
        return marker if isinstance(marker, dict) else {}

    def _validate_marker_target(self, palace: PalaceRef, config: _QdrantConfig) -> None:
        marker = self._read_marker(palace)
        if marker is None:
            return
        if marker.get("backend") != self.name:
            raise BackendMismatchError("qdrant marker does not identify the qdrant backend")
        expected = self._marker_target(palace, config)
        actual = marker.get("qdrant")
        if not isinstance(actual, dict):
            raise BackendMismatchError("qdrant marker is missing remote target metadata")
        mismatched = [
            key for key, expected_value in expected.items() if actual.get(key) != expected_value
        ]
        if mismatched:
            details = ", ".join(mismatched)
            raise BackendMismatchError(
                "qdrant marker remote target does not match current configuration "
                f"({details}); keep MEMPALACE_QDRANT_URL and namespace consistent "
                "or use a fresh palace directory"
            )

    def _write_marker(self, palace: PalaceRef, config: _QdrantConfig) -> None:
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
            "qdrant": self._marker_target(palace, config),
        }
        marker_path = self._marker_path(palace.local_path)
        with open(marker_path, "w", encoding="utf-8") as f:
            json.dump(marker, f, indent=2, ensure_ascii=False)
        try:
            os.chmod(marker_path, 0o600)
        except (OSError, NotImplementedError):
            pass

    def _client(self, config: _QdrantConfig) -> _QdrantRESTClient:
        if self._closed:
            raise BackendClosedError("QdrantBackend has been closed")
        with self._lock:
            client = self._clients.get(config)
            if client is None:
                client = _QdrantRESTClient(config)
                self._clients[config] = client
            return client

    def _remote_collection_name(
        self,
        *,
        palace: PalaceRef,
        collection_name: str,
        config: _QdrantConfig,
    ) -> str:
        config = _QdrantConfig(
            url=config.url,
            api_key=config.api_key,
            timeout=config.timeout,
            namespace=palace.namespace or config.namespace,
        )
        prefix = self._remote_collection_prefix(palace=palace, config=config)
        return f"{prefix}_{_slug(collection_name, 'collection')}"

    def get_collection(
        self,
        *args,
        **kwargs,
    ) -> QdrantCollection:
        palace, collection_name, create, options = self._normalize_args(args, kwargs)
        config = _QdrantConfig.from_options(options)
        if palace.namespace and palace.namespace != config.namespace:
            config = _QdrantConfig(
                url=config.url,
                api_key=config.api_key,
                timeout=config.timeout,
                namespace=palace.namespace,
            )
        client = self._client(config)
        if palace.local_path:
            marker_path = self._marker_path(palace.local_path)
            if os.path.isfile(marker_path):
                self._validate_marker_target(palace, config)
            elif not create:
                raise PalaceNotFoundError(marker_path)
        else:
            # The qdrant marker is this backend's only mismatch-protection
            # anchor, and it lives next to the palace on local disk. With no
            # local_path (the pure-remote / hosted mode) we can neither write
            # nor validate it, so opening would silently drop protection
            # against URL/namespace drift. Refuse loudly instead. A remote
            # marker store for pure-remote palaces is tracked as a follow-up.
            raise BackendError(
                "qdrant backend requires a local palace path to anchor mismatch "
                "protection; pure-remote palaces (local_path=None) are not "
                "supported yet"
            )
        remote_collection = self._remote_collection_name(
            palace=palace,
            collection_name=collection_name,
            config=config,
        )
        if not create and not client.collection_exists(remote_collection):
            raise CollectionNotInitializedError(collection_name)
        collection = QdrantCollection(
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
            self._collections_by_palace.clear()
            self._clients.clear()
            self._closed = True
        for collection in collections:
            collection.close()

    def health(self, palace: Optional[PalaceRef] = None) -> HealthStatus:
        if self._closed:
            return HealthStatus.unhealthy("backend closed")
        try:
            client = self._client(_QdrantConfig.from_options())
            client.request("GET", "/collections")
        except Exception as exc:  # noqa: BLE001 - user-facing health status
            return HealthStatus.unhealthy(str(exc))
        if (
            palace
            and palace.local_path
            and not os.path.isfile(self._marker_path(palace.local_path))
        ):
            return HealthStatus.unhealthy("qdrant marker not found")
        return HealthStatus.healthy()

    @classmethod
    def detect(cls, path: str) -> bool:
        return os.path.isfile(os.path.join(path, _MARKER_FILENAME))

    def create_collection(self, palace_path: str, collection_name: str) -> QdrantCollection:
        return self.get_collection(palace_path, collection_name, create=True)

    def get_or_create_collection(self, palace_path: str, collection_name: str):
        return self.get_collection(palace_path, collection_name, create=True)

    def delete_collection(self, palace_path: str, collection_name: str) -> None:
        palace = PalaceRef(id=palace_path, local_path=palace_path)
        config = _QdrantConfig.from_options()
        remote_collection = self._remote_collection_name(
            palace=palace,
            collection_name=collection_name,
            config=config,
        )
        client = self._client(config)
        if client.collection_exists(remote_collection):
            client.delete_collection(remote_collection)


__all__ = ["QdrantBackend", "QdrantCollection"]
