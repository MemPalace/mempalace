"""Core-side embedding adapter for explicit-vector backends."""

from __future__ import annotations

import inspect
import logging
import math
from pathlib import PurePath
from typing import Optional

from .base import (
    BaseCollection,
    GetResult,
    initialize_last_modified_metadata,
    validate_write_batch,
)

logger = logging.getLogger(__name__)


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
    from ..embedding import configured_embedding_function

    return configured_embedding_function()


def _resolve_document_embedder():
    """Release a transport request lease during configuration and construction."""
    from ..embedding import embedding_section

    with embedding_section():
        return _get_document_embedder()


def _merged_metadata(old, patch):
    metadata = {**(old or {}), **(patch or {})}
    return {key: value for key, value in metadata.items() if value is not None}


def _embeddable(texts: list[str]) -> list[str]:
    """``texts`` with lone surrogates as U+FFFD, for the embedder only.

    A stray source byte is stored as a lone surrogate, which tokenizers and
    JSON embedding APIs reject; the stored text keeps the byte.
    """
    from ..config import strip_lone_surrogates

    return [strip_lone_surrogates(text) if isinstance(text, str) else text for text in texts]


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

    Runs under :func:`mempalace.embedding.embedding_section` so a transport
    can release its request lock for this call only. Callers must invoke this
    before taking a backend write lock: dropping that lock from inside the
    embedding function Chroma runs during ``add`` deadlocks with the next writer.
    """
    if not texts:
        return []
    from ..embedding import configured_embedding_function, embedding_section

    texts = _embeddable(texts)
    with embedding_section():
        ef = configured_embedding_function() if embedder is None else embedder
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


def _usable_vector(vector, dimension: int) -> bool:
    if vector is None or dimension <= 0:
        return False
    try:
        return len(vector) == dimension and all(math.isfinite(float(x)) for x in vector)
    except (TypeError, ValueError):
        return False


def _with_embed_keys(metadatas: list, keys: list, merges: bool) -> list:
    """``metadatas`` with each row's ``embed_key`` set to the key its vector has.

    A row without a key drops any key it carried; on a backend that merges
    upsert metadata that takes an explicit ``None``.
    """
    from ..embed_key import EMBED_KEY_FIELD

    rows = []
    for metadata, key in zip(metadatas, keys):
        row = dict(metadata or {})
        if key:
            row[EMBED_KEY_FIELD] = key
        elif merges:
            row[EMBED_KEY_FIELD] = None
        else:
            row.pop(EMBED_KEY_FIELD, None)
        rows.append(row)
    return rows


class EmbeddingCollection(BaseCollection):
    """Wrap a collection that requires explicit vectors.

    Backends opt in with the ``requires_explicit_embeddings`` capability.
    Core callers can keep using ``documents=`` and ``query_texts=``; this
    wrapper computes vectors locally before delegating to the backend.

    With ``content_reuse`` (backends with ``supports_embedding_reuse``) every
    vector this wrapper computes is stored with its ``embed_key``
    (:mod:`mempalace.embed_key`), and a write first looks those keys up: a
    stored row with the same key already holds the vector, so it is copied
    instead of embedded. Only the vector is shared; every drawer keeps its
    own text and metadata.
    """

    def __init__(self, inner: BaseCollection, *, content_reuse: bool = False):
        self._inner = inner
        self._content_reuse = content_reuse

    def _embed_documents(self, documents: list, metadatas, embedder) -> tuple:
        """``(vectors, keys)`` for ``documents``; ``keys`` is ``None`` when reuse is off.

        Rows whose key is already stored copy that vector; rows of the batch
        that share a key are embedded once.
        """
        if not self._content_reuse:
            return _embed_texts(documents, metadatas=metadatas, embedder=embedder), None
        from ..embed_key import embed_key, embedder_inputs, embedder_profile, profile_identity

        profile = embedder_profile(embedder)
        if profile is None:
            return _embed_texts(documents, metadatas=metadatas, embedder=embedder), None
        identity = profile_identity(profile)
        try:
            inputs = embedder_inputs(embedder, _embeddable(documents), metadatas)
            keys = [
                embed_key(identity, *model_input)
                if model_input is not None
                and not (metadatas is not None and _is_native_media(metadatas[index] or {}))
                else None
                for index, model_input in enumerate(inputs)
            ]
        except Exception:
            logger.debug("could not key the batch; embedding all of it", exc_info=True)
            return _embed_texts(documents, metadatas=metadatas, embedder=embedder), None
        found = self._stored_vectors_by_key(keys, embedder, identity, profile)
        vectors: list = [found.get(key) if key else None for key in keys]
        first_of: dict = {}
        to_embed = []
        for index, key in enumerate(keys):
            if vectors[index] is not None:
                continue
            if key is None or key not in first_of:
                if key is not None:
                    first_of[key] = index
                to_embed.append(index)
        fresh = _embed_texts(
            [documents[i] for i in to_embed],
            metadatas=None if metadatas is None else [metadatas[i] for i in to_embed],
            embedder=embedder,
        )
        for index, vector in zip(to_embed, fresh):
            vectors[index] = vector
        for index, key in enumerate(keys):
            if vectors[index] is None:
                vectors[index] = vectors[first_of[key]]
        return vectors, keys

    def _stored_vectors_by_key(self, keys: list, embedder, identity: str, profile: dict) -> dict:
        """``{embed_key: vector}`` for the keys a stored row already holds.

        A hit is used only when the key recomputed from its own text and
        metadata is the key it carries, and its vector has the embedder's
        dimension. Lookups go ``LOOKUP_BATCH`` keys at a time; a failed one
        leaves its keys to be embedded.
        """
        from ..embed_key import EMBED_KEY_FIELD, LOOKUP_BATCH, embed_key, embedder_inputs

        wanted = list(dict.fromkeys(key for key in keys if key))
        found: dict = {}
        dimension = None
        for start in range(0, len(wanted), LOOKUP_BATCH):
            chunk = wanted[start : start + LOOKUP_BATCH]
            asked = set(chunk)
            try:
                got = self._inner.get(
                    where={EMBED_KEY_FIELD: {"$in": chunk}},
                    include=["embeddings", "documents", "metadatas"],
                )
                ids = list(got.ids or [])
                documents = list(got.documents or [None] * len(ids))
                metadatas = [meta or {} for meta in (got.metadatas or [None] * len(ids))]
                stored = got.embeddings
                stored = [None] * len(ids) if stored is None else list(stored)
                hits = [
                    (doc, meta, vector)
                    for doc, meta, vector in zip(documents, metadatas, stored)
                    if meta.get(EMBED_KEY_FIELD) in asked
                    and meta.get(EMBED_KEY_FIELD) not in found
                    and isinstance(doc, str)
                    and not _is_native_media(meta)
                ]
                if not hits:
                    continue
                inputs = embedder_inputs(
                    embedder, _embeddable([doc for doc, _, _ in hits]), [m for _, m, _ in hits]
                )
                for (_, meta, vector), model_input in zip(hits, inputs):
                    key = meta[EMBED_KEY_FIELD]
                    if key in found or model_input is None:
                        continue
                    if embed_key(identity, *model_input) != key:
                        continue
                    if dimension is None:
                        dimension = self._reference_dimension(profile)
                    if _usable_vector(vector, dimension):
                        found[key] = [float(x) for x in vector]
            except Exception:
                logger.debug("embed_key lookup failed; embedding those rows", exc_info=True)
        return found

    def _keyed_update_metadata(self, ids: list, metadatas, keys):
        """An update's metadata patch carrying the keys of the vectors it re-embedded."""
        if keys is None or not any(keys):
            return metadatas
        rows = [{} for _ in ids] if metadatas is None else metadatas
        merges = getattr(self._inner, "upsert_merges_metadata", False) is True
        return _with_embed_keys(rows, keys, merges=merges)

    def _reference_dimension(self, profile: dict) -> int:
        """The current embedder's vector width: declared, recorded, or probed."""
        declared = profile.get("dimension")
        if isinstance(declared, int) and not isinstance(declared, bool) and declared > 0:
            return declared
        recorded = self._inner.get_stored_embedder_identity()
        if recorded is not None and recorded.dimension > 0:
            return recorded.dimension
        from ..embedding import probe_dimension

        return probe_dimension()

    def __getattr__(self, name):
        return getattr(self._inner, name)

    @property
    def upsert_merges_metadata(self):
        return self._inner.upsert_merges_metadata

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
            embedder = _resolve_document_embedder() if self._content_reuse else None
            embeddings, keys = self._embed_documents(documents, metadatas, embedder)
            if keys is not None and metadatas is not None:
                metadatas = _with_embed_keys(metadatas, keys, merges=False)
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
        validate_write_batch(ids, documents=documents, metadatas=metadatas)
        expected = None
        if embeddings is None:
            embedder = _resolve_document_embedder()
            effective = metadatas
            if getattr(
                self._inner, "upsert_merges_metadata", False
            ) is True and _supports_metadata_aware_documents(embedder):
                existing = self._inner.get(ids=ids, include=["metadatas"])
                old = dict(zip(existing.ids, existing.metadatas))
                expected = old
                effective = []
                for index, record_id in enumerate(ids):
                    metadata = dict(old.get(record_id) or {})
                    if _is_native_media(metadata):
                        raise ValueError(
                            "native media upsert requires explicit embeddings or re-ingest"
                        )
                    effective.append(
                        _merged_metadata(
                            metadata, metadatas[index] if metadatas is not None else None
                        )
                    )
            embeddings, keys = self._embed_documents(documents, effective, embedder)
            if keys is not None and metadatas is not None:
                merges = getattr(self._inner, "upsert_merges_metadata", False) is True
                metadatas = _with_embed_keys(metadatas, keys, merges=merges)
        guarded = getattr(self._inner, "_upsert_with_metadata_guard", None)
        if expected is not None and callable(guarded):
            return guarded(
                expected=expected,
                documents=documents,
                ids=ids,
                metadatas=metadatas,
                embeddings=embeddings,
            )
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
        # ``with_document=False`` fast path) is never reached,
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
        validate_write_batch(ids, documents=documents, metadatas=metadatas, embeddings=embeddings)
        expected = None
        if documents is not None and embeddings is None:
            embedder = _resolve_document_embedder()
            if _supports_metadata_aware_documents(embedder):
                existing = self._inner.get(ids=ids, include=["documents", "metadatas"])
                old_by_id = {
                    record_id: (
                        existing.documents[index],
                        existing.metadatas[index] or {},
                    )
                    for index, record_id in enumerate(existing.ids)
                }
                expected = old_by_id
                if any(_is_native_media(meta) for _, meta in old_by_id.values()):
                    raise ValueError(
                        "document updates for native media assets require explicit embeddings; "
                        "re-ingest the source to preserve native media vectors"
                    )
                effective_metadatas = []
                for index, record_id in enumerate(ids):
                    _old_document, old_metadata = old_by_id.get(record_id, ("", {}))
                    update_metadata = (metadatas[index] or {}) if metadatas is not None else {}
                    effective_metadatas.append(_merged_metadata(old_metadata, update_metadata))
                embeddings, keys = self._embed_documents(documents, effective_metadatas, embedder)
            else:
                # Older embedders do not consume metadata. Preserve their
                # no-read behavior and call their ordinary document method.
                embeddings, keys = self._embed_documents(documents, metadatas, embedder)
            metadatas = self._keyed_update_metadata(ids, metadatas, keys)
        elif (
            embeddings is None
            and metadatas is not None
            and any(_metadata_can_change_embedding(metadata) for metadata in metadatas)
        ):
            embedder = _resolve_document_embedder()
            if _supports_metadata_aware_documents(embedder):
                existing = self._inner.get(ids=ids, include=["documents", "metadatas"])
                old_by_id = {
                    record_id: (
                        existing.documents[index],
                        existing.metadatas[index] or {},
                    )
                    for index, record_id in enumerate(existing.ids)
                }
                expected = old_by_id
                effective_documents = []
                effective_metadatas = []
                title_changed = False
                media_found = False
                for index, record_id in enumerate(ids):
                    old_document, old_metadata = old_by_id.get(record_id, ("", {}))
                    update_metadata = metadatas[index] or {}
                    metadata = _merged_metadata(old_metadata, update_metadata)
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
                    embeddings, keys = self._embed_documents(
                        effective_documents, effective_metadatas, embedder
                    )
                    metadatas = self._keyed_update_metadata(ids, metadatas, keys)
        guarded = getattr(self._inner, "_update_with_metadata_guard", None)
        if expected is not None and callable(guarded):
            return guarded(
                expected=expected,
                ids=ids,
                documents=documents,
                metadatas=metadatas,
                embeddings=embeddings,
            )
        return self._inner.update(
            ids=ids,
            documents=documents,
            metadatas=metadatas,
            embeddings=embeddings,
        )
