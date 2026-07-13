"""Core-side embedding adapter for explicit-vector backends."""

from __future__ import annotations

from typing import Optional

from .base import BaseCollection


def _embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed ``texts`` with the configured local embedding function."""
    if not texts:
        return []
    from ..embedding import get_embedding_function

    ef = get_embedding_function()
    vectors = ef(input=texts)
    return [list(v) for v in vectors]


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
            metadatas = _as_list(metadatas)
        if embeddings is None:
            embeddings = _embed_texts(documents)
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
            metadatas = _as_list(metadatas)
        if embeddings is None:
            embeddings = _embed_texts(documents)
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
            query_embeddings = _embed_texts(_as_list(query_texts))
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

    def update(self, *, ids, documents=None, metadatas=None, embeddings=None):
        ids = _as_list(ids)
        if documents is not None:
            documents = _as_list(documents)
            if embeddings is None:
                embeddings = _embed_texts(documents)
        if metadatas is not None:
            metadatas = _as_list(metadatas)
        return self._inner.update(
            ids=ids,
            documents=documents,
            metadatas=metadatas,
            embeddings=embeddings,
        )
