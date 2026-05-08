"""``PalaceContext`` facade passed to source adapters (RFC 002 §9).

Bundles the palace-side surface an adapter needs during :meth:`ingest`:
drawer collection, closet collection, knowledge graph, palace config, and
progress hooks. Adapters receive a ``PalaceContext`` instance and MUST NOT
import ``mempalace.palace`` directly — that coupling is what the facade
exists to prevent.

This module publishes the shape third-party adapters target. Core's mine
loop will construct a concrete ``PalaceContext`` and pass it to adapters
when the filesystem/conversations miners are migrated onto ``BaseSourceAdapter``
in a follow-up PR; until then, no in-tree code constructs one, but the
contract is stable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol

from .base import DrawerRecord


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
            meta.setdefault("adapter_name", self.adapter_name)
        if self.adapter_version:
            meta.setdefault("adapter_version", self.adapter_version)
        drawer_id = _build_drawer_id(record)
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

    def emit(self, event: str, **details: Any) -> None:
        """Invoke each registered progress hook with ``(event, **details)``."""
        for hook in self.progress_hooks:
            try:
                hook(event, **details)
            except Exception:  # pragma: no cover - hook errors never fail mine
                import logging

                logging.getLogger(__name__).exception("progress hook failed on %r", event)


def _build_drawer_id(record: DrawerRecord) -> str:
    """Deterministic drawer id: ``<sha256(source_file)[:24]>_<chunk_index>``.

    Matches the shape existing miners rely on (``source_file`` + chunk index
    pair) while keeping the id chroma-safe (no separators that collide with
    existing metadata values). 96-bit SHA-256 prefix keeps collision risk
    negligible across corpora the size of a palace (sha1@64 bits was too
    close to the birthday bound for large ingests). Adapters that need a
    different id scheme can bypass :meth:`PalaceContext.upsert_drawer` and
    write through ``drawer_collection.upsert`` directly.
    """
    import hashlib

    digest = hashlib.sha256(record.source_file.encode("utf-8")).hexdigest()[:24]
    return f"{digest}_{record.chunk_index}"
