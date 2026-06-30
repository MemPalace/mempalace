from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from importlib import metadata
from threading import Lock

logger = logging.getLogger(__name__)


class BaseKGStore(ABC):
    """Storage contract for MemPalace's knowledge graph (entities + bitemporal triples).

    The default implementation is the local-SQLite ``KnowledgeGraph``; alternate
    backends (e.g. a shared networked DB) register under ``mempalace.kg_backends``
    and are selected via ``MEMPALACE_KG_BACKEND``.
    """

    name: str = "base"

    def __init__(self, db_path: str | None = None, **kwargs) -> None:
        self.db_path = db_path

    def __enter__(self) -> BaseKGStore:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    @abstractmethod
    def add_entity(
        self, name: str, entity_type: str = "unknown", properties: dict | None = None
    ) -> str: ...
    @abstractmethod
    def add_triple(
        self,
        subject: str,
        predicate: str,
        obj: str,
        valid_from: str | None = None,
        valid_to: str | None = None,
        confidence: float = 1.0,
        source_closet: str | None = None,
        source_file: str | None = None,
        source_drawer_id: str | None = None,
        adapter_name: str | None = None,
    ) -> str: ...
    @abstractmethod
    def query_entity(
        self, name: str, as_of: str | None = None, direction: str = "outgoing"
    ) -> list: ...
    @abstractmethod
    def query_relationship(self, predicate: str, as_of: str | None = None) -> list: ...
    @abstractmethod
    def invalidate(
        self, subject: str, predicate: str, obj: str, ended: str | None = None
    ) -> None: ...
    @abstractmethod
    def seed_from_entity_facts(self, entity_facts: dict) -> None: ...
    @abstractmethod
    def timeline(self, entity_name: str | None = None) -> list: ...
    @abstractmethod
    def stats(self) -> dict: ...
    @abstractmethod
    def close(self) -> None: ...


_KG_REGISTRY = {}
_KG_LOCK = Lock()
_KG_DISCOVERED = False


def register_kg_backend(name, cls):
    if not (isinstance(cls, type) and issubclass(cls, BaseKGStore)):
        raise TypeError(f"kg backend {name!r} must be a BaseKGStore subclass (got {cls!r})")
    with _KG_LOCK:
        _KG_REGISTRY[name] = cls


def _discover():
    global _KG_DISCOVERED
    if _KG_DISCOVERED:
        return
    with _KG_LOCK:
        if _KG_DISCOVERED:
            return
        try:
            eps = metadata.entry_points()
            group = (
                eps.select(group="mempalace.kg_backends")
                if hasattr(eps, "select")
                else eps.get("mempalace.kg_backends", [])
            )
        except Exception:
            logger.exception("entry-point discovery for mempalace.kg_backends failed")
            group = []
        for ep in group:
            if ep.name in _KG_REGISTRY:
                continue
            try:
                cls = ep.load()
            except Exception:
                logger.exception("failed to load kg backend entry point %r", ep.name)
                continue
            if not isinstance(cls, type) or not issubclass(cls, BaseKGStore):
                logger.warning(
                    "entry point %r did not resolve to a BaseKGStore subclass (got %r)",
                    ep.name,
                    cls,
                )
                continue
            _KG_REGISTRY[ep.name] = cls
        _KG_DISCOVERED = True


def get_kg_store(db_path=None, *, explicit=None, backend=None) -> BaseKGStore:
    from .knowledge_graph import KnowledgeGraph

    name = explicit or backend or os.environ.get("MEMPALACE_KG_BACKEND") or "sqlite"
    if name == "sqlite":
        return KnowledgeGraph(db_path=db_path)
    _discover()
    cls = _KG_REGISTRY.get(name)
    if cls is None:
        raise KeyError(
            f"unknown kg backend {name!r}; available: {sorted(_KG_REGISTRY) + ['sqlite']}"
        )
    return cls(db_path=db_path)
