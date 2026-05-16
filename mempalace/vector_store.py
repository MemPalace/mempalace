"""Backend-agnostic vector store access for MemPalace."""

import os

from .backends import VectorCollection
from .config import MempalaceConfig


def get_collection(
    palace_path: str, config=None, create: bool = False, collection_name: str = None
) -> VectorCollection:
    cfg = config or MempalaceConfig()
    backend = getattr(cfg, "vector_backend", "chroma")
    col_name = collection_name or getattr(cfg, "collection_name", "mempalace_drawers")

    if backend == "qdrant":
        from .backends.qdrant_store import get_qdrant_collection

        return get_qdrant_collection(cfg, col_name, create=create)

    from .backends.chroma_store import get_chroma_collection

    return get_chroma_collection(palace_path, col_name, create=create)


def reset_collection(palace_path: str, config=None) -> VectorCollection:
    cfg = config or MempalaceConfig()
    backend = getattr(cfg, "vector_backend", "chroma")
    collection_name = getattr(cfg, "collection_name", "mempalace_drawers")

    if backend == "qdrant":
        from .backends.qdrant_store import reset_qdrant_collection

        return reset_qdrant_collection(cfg, collection_name)

    from .backends.chroma_store import reset_chroma_collection

    os.makedirs(palace_path, exist_ok=True)
    return reset_chroma_collection(palace_path, collection_name)
