"""``PalaceContext`` facade passed to source adapters (RFC 002 §9).

Bundles the palace-side surface an adapter needs during :meth:`ingest`:
drawer collection, closet collection, knowledge graph, palace config, and
progress hooks. Adapters receive a ``PalaceContext`` instance and MUST NOT
import ``mempalace.palace`` directly — that coupling is what the facade
exists to prevent.

This module publishes the shape third-party adapters target. Core constructs a
concrete ``PalaceContext`` for explicit source-adapter mining; legacy
filesystem and conversation miners retain their established paths until they
are migrated separately.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol

from .base import DrawerRecord, SourceItemMetadata


class _CollectionLike(Protocol):
    """Minimum of :class:`mempalace.backends.BaseCollection` adapters rely on.

    Declared as a Protocol so tests and third-party adapters can substitute
    any object with compatible method signatures without importing the
    concrete backend. See ``mempalace/backends/base.py`` for the full surface.
    """

    def add(self, **kwargs: Any) -> None: ...
    def upsert(self, **kwargs: Any) -> None: ...
    def query(self, **kwargs: Any) -> Any: ...
    def get(self, **kwargs: Any) -> Any: ...
    def delete(self, **kwargs: Any) -> None: ...
    def count(self) -> int: ...


class _KnowledgeGraphLike(Protocol):
    def add_triple(self, subject: str, predicate: str, obj: str, **kwargs: Any) -> Any: ...


# Progress hook signature: ``fn(event_name, **details) -> None``.
ProgressHook = Callable[..., None]


class _IncrementalCollectionFacade:
    """A write-safe compatible collection view for one incremental item.

    Reads retain the backend's native result shape.  Writes are constrained to
    the current source item and receive core-owned IDs and provenance, so an
    adapter cannot overwrite the previously active generation by calling
    ``palace.drawer_collection.upsert`` directly.
    """

    def __init__(
        self,
        collection: _CollectionLike,
        item: SourceItemMetadata,
        generation: str,
        adapter_name: str,
        adapter_version: str,
    ):
        self._collection = collection
        self._item = item
        self._generation = generation
        self._adapter_name = adapter_name
        self._adapter_version = adapter_version
        self._next_chunk_index = 0
        self._seen_chunk_indexes = set()

    def upsert(self, *, documents, ids=None, metadatas=None, **kwargs: Any) -> None:
        documents = list(documents)
        metadatas = list(metadatas or [{} for _ in documents])
        if len(documents) != len(metadatas):
            raise ValueError("documents and metadatas must have the same length")
        normalized = []
        generated_ids = []
        for index, metadata in enumerate(metadatas):
            meta = dict(metadata or {})
            source_file = meta.get("source_file", self._item.source_file)
            if source_file != self._item.source_file:
                raise ValueError(
                    "incremental collection writes must target the current source item"
                )
            chunk_index = meta.get("chunk_index")
            if chunk_index is None:
                chunk_index = self._next_chunk_index
                self._next_chunk_index += 1
            else:
                self._next_chunk_index = max(self._next_chunk_index, chunk_index + 1)
            if chunk_index in self._seen_chunk_indexes:
                raise ValueError(
                    f"incremental source item yielded duplicate chunk_index {chunk_index!r}"
                )
            self._seen_chunk_indexes.add(chunk_index)
            record = DrawerRecord(
                content=documents[index], source_file=source_file, chunk_index=chunk_index
            )
            meta.update(
                source_file=source_file,
                chunk_index=chunk_index,
                source_version=self._item.version,
                source_generation=self._generation,
                source_generation_state="staging",
            )
            if self._adapter_name:
                meta["adapter_name"] = self._adapter_name
            if self._adapter_version:
                meta["adapter_version"] = self._adapter_version
            normalized.append(meta)
            generated_ids.append(_build_drawer_id(record, generation=self._generation))
        self._collection.upsert(
            documents=documents, ids=generated_ids, metadatas=normalized, **kwargs
        )

    def add(self, **kwargs: Any) -> None:
        # Treat add as upsert: deterministic generation IDs make retries safe.
        self.upsert(**kwargs)

    def delete(self, **kwargs: Any) -> None:
        raise RuntimeError("incremental adapters cannot delete drawers directly")

    def query(self, **kwargs: Any) -> Any:
        return self._collection.query(**kwargs)

    def get(self, **kwargs: Any) -> Any:
        return self._collection.get(**kwargs)

    def count(self) -> int:
        return self._collection.count()


@dataclass
class PalaceContext:
    """Per-mine-invocation facade passed to :meth:`BaseSourceAdapter.ingest`.

    Fields:
        drawer_collection: The palace's drawer collection (via RFC 001 backend).
        closet_collection: The palace's closet collection, or ``None`` if the
            palace has no closets yet. Adapters should not write to this
            directly; core builds closets post-step (RFC 002 §1.7).
        knowledge_graph: The palace's SQLite knowledge graph. Adapters
            advertising ``supports_kg_triples`` call ``add_triple`` on it.
        palace_path: Filesystem root of the palace (convenience; same as
            ``backend.PalaceRef.local_path``).
        config: Palace config object (hall keywords, rooms list, privacy
            floor, etc.). Shape is the existing :class:`MempalaceConfig`.
        adapter_name: Name of the adapter currently ingesting; populated by
            core so drawers can carry ``metadata["adapter_name"]``.
        adapter_version: Version of the adapter currently ingesting.
        progress_hooks: Optional callables core invokes on progress events.

    Methods are intentionally thin wrappers so the concrete mine loop in
    core can swap implementations without changing adapter code.
    """

    drawer_collection: _CollectionLike
    knowledge_graph: _KnowledgeGraphLike
    palace_path: str
    closet_collection: Optional[_CollectionLike] = None
    config: Optional[Any] = None
    adapter_name: str = ""
    adapter_version: str = ""
    progress_hooks: list[ProgressHook] = field(default_factory=list)

    # Set by the incremental runner after it receives SourceItemMetadata.  A
    # generation-specific ID ensures a staged replacement never overwrites a
    # drawer from the last complete generation.
    _current_item: Optional[SourceItemMetadata] = field(default=None, init=False, repr=False)
    _current_generation: Optional[str] = field(default=None, init=False, repr=False)
    _unscoped_drawer_collection: Optional[_CollectionLike] = field(
        default=None, init=False, repr=False
    )

    # Internal: flag set by :meth:`skip_current_item` and checked by the core
    # mine loop between yields. Not part of the adapter-facing contract; the
    # adapter only needs to know that calling :meth:`skip_current_item` stops
    # drawer emission for the current ``SourceItemMetadata``.
    _skip_requested: bool = False

    # ------------------------------------------------------------------
    # Adapter-facing surface
    # ------------------------------------------------------------------

    def upsert_drawer(self, record: DrawerRecord) -> None:
        """Persist a ``DrawerRecord`` to the drawer collection.

        Applies the spec-mandated ``adapter_name`` and ``adapter_version``
        metadata stamps (§5.1) so adapters never need to populate them.
        """
        meta = dict(record.metadata)
        meta.setdefault("source_file", record.source_file)
        meta.setdefault("chunk_index", record.chunk_index)
        if self.adapter_name:
            meta["adapter_name"] = self.adapter_name
        if self.adapter_version:
            meta["adapter_version"] = self.adapter_version
        if self._current_item is not None:
            if record.source_file != self._current_item.source_file:
                raise ValueError(
                    "incremental adapter yielded a drawer for a different source item "
                    "than its current SourceItemMetadata"
                )
            meta.setdefault("source_version", self._current_item.version)
        if self._current_generation is not None:
            meta.setdefault("source_generation", self._current_generation)
            meta.setdefault("source_generation_state", "staging")
        drawer_id = _build_drawer_id(record, generation=self._current_generation)
        self.drawer_collection.upsert(
            documents=[record.content],
            ids=[drawer_id],
            metadatas=[meta],
        )

    def skip_current_item(self) -> None:
        """Signal to core that the current ``SourceItemMetadata`` is up-to-date
        and no drawers should be emitted for it. Core resets the flag after
        advancing past the item."""
        self._skip_requested = True

    def begin_source_item(
        self,
        item: SourceItemMetadata,
        *,
        generation: Optional[str] = None,
    ) -> None:
        """Bind subsequent drawer writes to one source item and generation.

        This is runner-owned state.  Adapters keep the existing public
        ``PalaceContext`` surface and only observe it indirectly through the
        documented ``skip_current_item`` signal.
        """
        self._current_item = item
        self._current_generation = generation
        self._skip_requested = False
        if generation is not None:
            self._unscoped_drawer_collection = self.drawer_collection
            self.drawer_collection = _IncrementalCollectionFacade(
                self.drawer_collection,
                item,
                generation,
                self.adapter_name,
                self.adapter_version,
            )

    def finish_source_item(self) -> None:
        """Clear runner-owned item state after commit or abandonment."""
        self._current_item = None
        self._current_generation = None
        self._skip_requested = False
        if self._unscoped_drawer_collection is not None:
            self.drawer_collection = self._unscoped_drawer_collection
            self._unscoped_drawer_collection = None

    def emit(self, event: str, **details: Any) -> None:
        """Invoke each registered progress hook with ``(event, **details)``."""
        for hook in self.progress_hooks:
            try:
                hook(event, **details)
            except Exception:  # pragma: no cover - hook errors never fail mine
                import logging

                logging.getLogger(__name__).exception("progress hook failed on %r", event)


def _build_drawer_id(record: DrawerRecord, *, generation: Optional[str] = None) -> str:
    """Deterministic drawer id, optionally namespaced by a source generation.

    Matches the shape existing miners rely on (``source_file`` + chunk index
    pair) while keeping the id chroma-safe (no separators that collide with
    existing metadata values). 96-bit SHA-256 prefix keeps collision risk
    negligible across corpora the size of a palace (sha1@64 bits was too
    close to the birthday bound for large ingests). Adapters that need a
    different id scheme can bypass :meth:`PalaceContext.upsert_drawer` and
    write through ``drawer_collection.upsert`` directly.
    """
    import hashlib

    identity = record.source_file
    if generation is not None:
        identity = f"{identity}\0{generation}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return f"{digest}_{record.chunk_index}"
