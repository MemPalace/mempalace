"""Core-side embedding adapter for explicit-vector backends."""

from __future__ import annotations

import inspect
from pathlib import PurePath
from typing import Optional

from .base import BaseCollection, GetResult, initialize_last_modified_metadata


def _supports_metadata_aware_documents(embedder) -> bool:
    """Whether ``embed_documents`` explicitly accepts per-document metadata."""
    method = getattr(embedder, "embed_documents", None)
    if not callable(method):
        return False
    try:
        parameters = inspect.signature(method).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(parameter.name == "metadatas" for parameter in parameters)


def _get_document_embedder():
    from ..embedding import get_embedding_function

    return get_embedding_function()


def _embed_texts(
    texts: list[str],
    *,
    query: bool = False,
    metadatas: Optional[list[dict]] = None,
    embedder=None,
) -> list[list[float]]:
    """Embed ``texts`` with the configured local embedding function.

    Embedding functions return ``list[np.ndarray]`` (float32). ``list(arr)``
    would unpack that into ``np.float32`` *scalars*, which ChromaDB's
    ``normalize_embeddings`` rejects outright ("Expected embeddings to be a
    list of floats or ints..."), so every write through this wrapper must
    convert to real Python floats. ``.tolist()`` does that in C; the
    ``float(x)`` branch covers embedders that already hand back plain
    sequences.
    """
    if not texts:
        return []
    if embedder is None:
        from ..embedding import get_embedding_function

        ef = get_embedding_function()
    else:
        ef = embedder
    if query and callable(getattr(ef, "embed_query", None)):
        vectors = ef.embed_query(input=texts)
    elif not query and callable(getattr(ef, "embed_documents", None)):
        method = ef.embed_documents
        if _supports_metadata_aware_documents(ef):
            vectors = method(input=texts, metadatas=metadatas)
        else:
            vectors = method(input=texts)
    else:
        vectors = ef(input=texts)
    return [
        v.tolist() if hasattr(v, "tolist") else [float(x) for x in v]  # numpy | plain sequence
        for v in vectors
    ]


def _as_list(value):
    """Normalize ChromaDB's ``OneOrMany`` shape (``str`` | ``dict`` | sequence) to a list.

    A bare ``str`` (a document/id) or ``dict`` (a single metadata) must be
    *wrapped*, not iterated: ``list("abc")`` yields ``['a', 'b', 'c']`` and
    ``list({"k": 1})`` yields ``['k']`` — either desyncs embeddings/metadatas
    from ``ids`` on explicit-vector backends (pgvector, sqlite_exact). A list is
    returned unchanged (no copy); any other iterable is materialized once.
    See PR #1706/#1707 review.
    """
    if isinstance(value, (str, dict)):
        return [value]
    if isinstance(value, list):
        return value
    return list(value)


_EMBEDDING_TITLE_FIELDS = frozenset({"title", "source_file", "source_path", "media_type"})


def _metadata_can_change_embedding(metadata) -> bool:
    return bool(isinstance(metadata, dict) and _EMBEDDING_TITLE_FIELDS.intersection(metadata))


def _metadata_title(metadata: dict) -> Optional[str]:
    title = metadata.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    source_file = metadata.get("source_file")
    if isinstance(source_file, str) and source_file.strip():
        return PurePath(source_file.replace("\\", "/")).name or None
    source_path = metadata.get("source_path")
    if isinstance(source_path, str) and source_path.strip():
        return PurePath(source_path.replace("\\", "/")).name or None
    return None


def _is_native_media(metadata: dict) -> bool:
    return metadata.get("media_type") in {"image", "audio", "video"}


class EmbeddingCollection(BaseCollection):
    """Wrap a collection that requires explicit vectors.

    Backends opt in with the ``requires_explicit_embeddings`` capability.
    Core callers can keep using ``documents=`` and ``query_texts=``; this
    wrapper computes vectors locally before delegating to the backend.
    """

    def __init__(self, inner: BaseCollection):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    @property
    def distance_metric(self) -> str:
        # Explicit delegation: ``BaseCollection`` defines ``distance_metric``
        # as a property, so it resolves on this subclass and ``__getattr__``
        # never fires — without this override the wrapper would report the
        # base "cosine" default and mask a wrapped non-cosine backend.
        return self._inner.distance_metric

    # Same shadowing reason as ``distance_metric``: these are concrete methods
    # on ``BaseCollection``, so ``__getattr__`` never delegates them. Forward
    # explicitly to the wrapped backend collection's identity store.
    def get_stored_embedder_identity(self):
        return self._inner.get_stored_embedder_identity()

    def set_embedder_identity(self, identity) -> None:
        return self._inner.set_embedder_identity(identity)

    def effective_embedder_identity(self):
        return self._inner.effective_embedder_identity()

    def maintenance_state(self) -> dict:
        return self._inner.maintenance_state()

    def run_maintenance(self, kind: str):
        return self._inner.run_maintenance(kind)

    def add(self, *, documents, ids, metadatas=None, embeddings=None):
        documents = _as_list(documents)
        ids = _as_list(ids)
        if metadatas is not None:
            metadatas = initialize_last_modified_metadata(_as_list(metadatas))
        if embeddings is None:
            embeddings = _embed_texts(documents, metadatas=metadatas)
        return self._inner.add(
            documents=documents,
            ids=ids,
            metadatas=metadatas,
            embeddings=embeddings,
        )

    def upsert(self, *, documents, ids, metadatas=None, embeddings=None):
        documents = _as_list(documents)
        ids = _as_list(ids)
        if metadatas is not None:
            metadatas = initialize_last_modified_metadata(_as_list(metadatas))
        if embeddings is None:
            embeddings = _embed_texts(documents, metadatas=metadatas)
        return self._inner.upsert(
            documents=documents,
            ids=ids,
            metadatas=metadatas,
            embeddings=embeddings,
        )

    def query(
        self,
        *,
        query_texts: Optional[list[str] | str] = None,
        query_embeddings: Optional[list[list[float]]] = None,
        n_results: int = 10,
        where: Optional[dict] = None,
        where_document: Optional[dict] = None,
        include: Optional[list[str]] = None,
    ):
        if query_texts is not None and query_embeddings is None:
            query_embeddings = _embed_texts(_as_list(query_texts), query=True)
            query_texts = None
        return self._inner.query(
            query_texts=query_texts,
            query_embeddings=query_embeddings,
            n_results=n_results,
            where=where,
            where_document=where_document,
            include=include,
        )

    def get(
        self, *, ids=None, where=None, where_document=None, limit=None, offset=None, include=None
    ):
        return self._inner.get(
            ids=ids,
            where=where,
            where_document=where_document,
            limit=limit,
            offset=offset,
            include=include,
        )

    def delete(self, *, ids=None, where=None):
        return self._inner.delete(ids=ids, where=where)

    def count(self) -> int:
        return self._inner.count()

    def estimated_count(self) -> int:
        return self._inner.estimated_count()

    def close(self) -> None:
        return self._inner.close()

    def health(self):
        return self._inner.health()

    def lexical_search(self, *, query: str, n_results: int = 10, where: Optional[dict] = None):
        return self._inner.lexical_search(query=query, n_results=n_results, where=where)

    def get_recent(
        self,
        *,
        limit: int,
        where: Optional[dict] = None,
        order_field: str = "filed_at",
        include: Optional[list[str]] = None,
    ):
        # Concrete on ``BaseCollection`` (the scan-and-sort default), so MRO
        # would resolve it here and shadow a backend that pushes the ordering
        # into storage. Forward explicitly.
        return self._inner.get_recent(
            limit=limit, where=where, order_field=order_field, include=include
        )

    def facet_counts(
        self, field: str, where: Optional[dict] = None, limit: int = 1000
    ) -> dict[str, int]:
        # ``BaseCollection.facet_counts`` is a concrete method that raises
        # ``UnsupportedCapabilityError`` as its default. MRO resolves it on
        # this subclass before ``__getattr__`` ever fires, so without an
        # explicit forwarder every facet call against a wrapped backend
        # (qdrant, pgvector, sqlite_exact) raises and silently degrades to
        # client-side counting in mcp_server's try/except.
        return self._inner.facet_counts(field, where=where, limit=limit)

    def get_all_metadata(self, where: Optional[dict] = None) -> list[dict]:
        # ``BaseCollection.get_all_metadata`` ships a concrete default that
        # pages through ``self.get(include=["metadatas"])``. Without this
        # forwarder, MRO resolves the call here on the subclass and runs the
        # base default — which routes back through ``self.get()`` (the
        # wrapper's get, then ``__getattr__`` to the inner's get). Result:
        # the inner's overridden ``get_all_metadata`` (e.g. pgvector's
        # ``with_document=False`` fast path from #1892) is never reached,
        # and every metadata-only fetch transfers the full document column
        # over the wire. Same MRO-shadow pattern as ``facet_counts`` /
        # ``lexical_search`` above.
        return self._inner.get_all_metadata(where=where)

    def get_all_rows(
        self, where: Optional[dict] = None, include: Optional[list[str]] = None
    ) -> GetResult:
        # Same MRO shadow as ``get_all_metadata`` right above: the concrete
        # default on ``BaseCollection`` pages through ``self.get()``, so without
        # this forwarder a backend's single-pass implementation (qdrant scrolls
        # its own cursor) is never reached and the O(n^2) offset walk comes back.
        return self._inner.get_all_rows(where=where, include=include)

    def update(self, *, ids, documents=None, metadatas=None, embeddings=None):
        ids = _as_list(ids)
        if metadatas is not None:
            metadatas = _as_list(metadatas)
        if documents is not None:
            documents = _as_list(documents)
        for label, value in (
            ("documents", documents),
            ("metadatas", metadatas),
            ("embeddings", embeddings),
        ):
            if value is not None and len(value) != len(ids):
                raise ValueError(
                    f"{label} length {len(value)} does not match ids length {len(ids)}"
                )
        if documents is not None and embeddings is None:
            embedder = _get_document_embedder()
            if _supports_metadata_aware_documents(embedder):
                existing = self._inner.get(ids=ids, include=["documents", "metadatas"])
                old_by_id = {
                    record_id: (
                        existing.documents[index],
                        existing.metadatas[index] or {},
                    )
                    for index, record_id in enumerate(existing.ids)
                }
                if any(_is_native_media(meta) for _, meta in old_by_id.values()):
                    raise ValueError(
                        "document updates for native media assets require explicit embeddings; "
                        "re-ingest the source to preserve native media vectors"
                    )
                effective_metadatas = []
                for index, record_id in enumerate(ids):
                    _old_document, old_metadata = old_by_id.get(record_id, ("", {}))
                    metadata = dict(old_metadata)
                    update_metadata = (metadatas[index] or {}) if metadatas is not None else {}
                    metadata.update(update_metadata)
                    effective_metadatas.append(metadata)
                embeddings = _embed_texts(
                    documents,
                    metadatas=effective_metadatas,
                    embedder=embedder,
                )
            else:
                # Older embedders do not consume metadata. Preserve their
                # no-read behavior and call their ordinary document method.
                embeddings = _embed_texts(documents, metadatas=metadatas, embedder=embedder)
        elif (
            embeddings is None
            and metadatas is not None
            and any(_metadata_can_change_embedding(metadata) for metadata in metadatas)
        ):
            embedder = _get_document_embedder()
            if _supports_metadata_aware_documents(embedder):
                existing = self._inner.get(ids=ids, include=["documents", "metadatas"])
                old_by_id = {
                    record_id: (
                        existing.documents[index],
                        existing.metadatas[index] or {},
                    )
                    for index, record_id in enumerate(existing.ids)
                }
                effective_documents = []
                effective_metadatas = []
                title_changed = False
                media_found = False
                for index, record_id in enumerate(ids):
                    old_document, old_metadata = old_by_id.get(record_id, ("", {}))
                    metadata = dict(old_metadata)
                    update_metadata = metadatas[index] or {}
                    metadata.update(update_metadata)
                    media_found = media_found or _is_native_media(old_metadata)
                    if _is_native_media(old_metadata) and {
                        "source_path",
                        "source_file",
                        "media_type",
                    }.intersection(update_metadata):
                        raise ValueError(
                            "changing a native media source path or media_type requires "
                            "re-ingesting the source; explicit vectors are also accepted"
                        )
                    title_changed = title_changed or (
                        not _is_native_media(old_metadata)
                        and _metadata_title(old_metadata) != _metadata_title(metadata)
                    )
                    effective_documents.append(old_document)
                    effective_metadatas.append(metadata)
                if title_changed:
                    if media_found:
                        raise ValueError(
                            "mixed native media and text metadata updates cannot re-embed "
                            "safely in one batch; update the records separately"
                        )
                    embeddings = _embed_texts(
                        effective_documents,
                        metadatas=effective_metadatas,
                        embedder=embedder,
                    )
        return self._inner.update(
            ids=ids,
            documents=documents,
            metadatas=metadatas,
            embeddings=embeddings,
        )
