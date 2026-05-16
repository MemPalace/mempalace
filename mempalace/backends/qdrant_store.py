import uuid
from typing import Optional

from . import VectorCollection

_KEY_DOCUMENT = "document"
_KEY_DRAWER_ID = "_drawer_id"
_KEY_IDS = "ids"
_KEY_DOCUMENTS = "documents"
_KEY_METADATAS = "metadatas"
_KEY_DISTANCES = "distances"

_DEFAULT_EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"

_embedders: dict = {}


def _get_embedder(model: str):
    if model not in _embedders:
        from fastembed import TextEmbedding

        _embedders[model] = TextEmbedding(model)
    return _embedders[model]


def _embed(texts: list[str], model: str) -> list[list[float]]:
    return [list(v) for v in _get_embedder(model).embed(texts)]


def _vector_size_for_model(model: str) -> int:
    return _get_embedder(model).embedding_size


def _str_to_uuid(s: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, s))


def _unpack_payload(point_or_record) -> tuple[str, str, dict]:
    payload = dict(point_or_record.payload or {})
    doc = payload.pop(_KEY_DOCUMENT, "")
    orig_id = payload.pop(_KEY_DRAWER_ID, str(point_or_record.id))
    return orig_id, doc, payload


def _where_to_filter(where: Optional[dict]):
    if not where:
        return None

    from qdrant_client.models import FieldCondition, Filter, MatchValue

    pairs = []
    if "$and" in where:
        for clause in where["$and"]:
            pairs.extend(clause.items())
    else:
        pairs = list(where.items())

    conditions = [FieldCondition(key=k, match=MatchValue(value=v)) for k, v in pairs]
    return Filter(must=conditions) if conditions else None  # type: ignore[arg-type]


def _ensure_collection(client, collection_name: str, vector_size: int) -> None:
    from qdrant_client.models import Distance, VectorParams

    if not client.collection_exists(collection_name):
        client.create_collection(
            collection_name=collection_name,
            vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
        )


class QdrantCollection(VectorCollection):
    _SCROLL_END = object()

    def __init__(self, client, collection_name: str, model: str, vector_size: int):
        self._client = client
        self._name = collection_name
        self._model = model
        self._vector_size = vector_size
        self._scroll_cursor = None

    def add(self, documents, ids, metadatas):
        from qdrant_client.models import PointStruct

        docs = list(documents)
        _ensure_collection(self._client, self._name, self._vector_size)

        vectors = _embed(docs, self._model)
        points = []
        for doc, meta, orig_id, vec in zip(docs, metadatas, ids, vectors):
            payload = dict(meta)
            payload[_KEY_DRAWER_ID] = orig_id
            payload[_KEY_DOCUMENT] = doc
            points.append(PointStruct(id=_str_to_uuid(orig_id), vector=vec, payload=payload))

        self._client.upsert(collection_name=self._name, points=points)

    def upsert(self, ids, documents, metadatas):
        self.add(documents=documents, ids=ids, metadatas=metadatas)

    def delete(self, ids):
        qdrant_ids = [_str_to_uuid(i) for i in ids]
        self._client.delete(
            collection_name=self._name,
            points_selector=qdrant_ids,
        )

    def query(self, query_texts, n_results, where=None, include=None):
        query_filter = _where_to_filter(where)
        query_vec = _embed([query_texts[0]], self._model)[0]

        results = self._client.query_points(
            collection_name=self._name,
            query=query_vec,
            query_filter=query_filter,
            limit=n_results,
            with_payload=True,
        ).points

        docs, metas, dists, ids = [], [], [], []
        for point in results:
            orig_id, doc, meta = _unpack_payload(point)
            docs.append(doc)
            metas.append(meta)
            dists.append(min(1.0, max(0.0, 1.0 - point.score)))
            ids.append(orig_id)

        return {
            _KEY_DOCUMENTS: [docs],
            _KEY_METADATAS: [metas],
            _KEY_DISTANCES: [dists],
            _KEY_IDS: [ids],
        }

    def get(self, where=None, limit=None, offset=None, include=None, ids=None):
        if ids is not None:
            qdrant_ids = [_str_to_uuid(i) for i in ids]
            records = self._client.retrieve(
                collection_name=self._name, ids=qdrant_ids, with_payload=True
            )
            return self._records_to_result(records, include)

        scroll_filter = _where_to_filter(where)

        if not offset:
            self._scroll_cursor = None

        if self._scroll_cursor is self._SCROLL_END:
            return self._records_to_result([], include)

        records, next_cursor = self._client.scroll(
            collection_name=self._name,
            scroll_filter=scroll_filter,
            limit=limit or 10,
            offset=self._scroll_cursor,
            with_payload=True,
            with_vectors=False,
        )
        self._scroll_cursor = next_cursor if next_cursor is not None else self._SCROLL_END

        return self._records_to_result(records, include)

    def count(self):
        return self._client.count(collection_name=self._name).count

    def _records_to_result(self, records, include=None):
        result_ids, result_docs, result_metas = [], [], []

        for record in records:
            orig_id, doc, meta = _unpack_payload(record)
            result_ids.append(orig_id)
            if not include or _KEY_DOCUMENTS in include:
                result_docs.append(doc)
            if not include or _KEY_METADATAS in include:
                result_metas.append(meta)

        result: dict = {_KEY_IDS: result_ids}
        if not include or _KEY_DOCUMENTS in include:
            result[_KEY_DOCUMENTS] = result_docs
        if not include or _KEY_METADATAS in include:
            result[_KEY_METADATAS] = result_metas
        return result


def get_qdrant_client(config):
    try:
        from qdrant_client import QdrantClient
    except ImportError as exc:
        raise ImportError(
            "qdrant-client is not installed. "
            "Install with: pip install 'mempalace[qdrant]' "
            "or: pip install 'qdrant-client[fastembed]'"
        ) from exc

    qdrant_url = getattr(config, "qdrant_url", None)
    qdrant_api_key = getattr(config, "qdrant_api_key", None)

    return QdrantClient(url=qdrant_url, api_key=qdrant_api_key or None)


def get_qdrant_collection(config, collection_name: str, create: bool = False) -> QdrantCollection:
    client = get_qdrant_client(config)
    model = getattr(config, "qdrant_embedding_model", _DEFAULT_EMBEDDING_MODEL)
    vector_size = _vector_size_for_model(model)

    if not create and not client.collection_exists(collection_name):
        raise ValueError(
            f"Qdrant collection '{collection_name}' not found. Run: mempalace mine <dir>"
        )

    if create:
        _ensure_collection(client, collection_name, vector_size)

    return QdrantCollection(client, collection_name, model=model, vector_size=vector_size)


def reset_qdrant_collection(config, collection_name: str) -> QdrantCollection:
    client = get_qdrant_client(config)
    model = getattr(config, "qdrant_embedding_model", _DEFAULT_EMBEDDING_MODEL)
    vector_size = _vector_size_for_model(model)

    client.delete_collection(collection_name)

    _ensure_collection(client, collection_name, vector_size)
    return QdrantCollection(client, collection_name, model=model, vector_size=vector_size)
