"""Storage backend implementations for MemPalace (RFC 001).

Public surface:

* :class:`BaseCollection` — per-collection read/write contract.
* :class:`BaseBackend` — per-palace factory contract.
* :class:`PalaceRef` — value object identifying a palace for a backend.
* :class:`QueryResult` / :class:`GetResult` — typed read returns.
* Error classes: :class:`PalaceNotFoundError`, :class:`BackendClosedError`,
  :class:`UnsupportedFilterError`, :class:`DimensionMismatchError`,
  :class:`EmbedderIdentityMismatchError`.
* Registry: :func:`get_backend`, :func:`register`, :func:`available_backends`,
  :func:`resolve_backend_for_palace`.
* In-tree Chroma default: :class:`ChromaBackend`, :class:`ChromaCollection`.
"""

from .base import (
    BackendClosedError,
    BackendError,
    BaseBackend,
    BaseCollection,
    DimensionMismatchError,
    EmbedderIdentityMismatchError,
    GetResult,
    HealthStatus,
    PalaceNotFoundError,
    PalaceRef,
    QueryResult,
    UnsupportedFilterError,
)
from .chroma import ChromaBackend, ChromaCollection
from .registry import (
    available_backends,
    get_backend,
    get_backend_class,
    register,
    reset_backends,
    resolve_backend_for_palace,
    unregister,
)

__all__ = [
    "BackendClosedError",
    "BackendError",
    "BaseBackend",
    "BaseCollection",
    "ChromaBackend",
    "ChromaCollection",
    "DimensionMismatchError",
    "EmbedderIdentityMismatchError",
    "GetResult",
    "HealthStatus",
    "PalaceNotFoundError",
    "PalaceRef",
    "QueryResult",
    "UnsupportedFilterError",
    "available_backends",
    "get_backend",
    "get_backend_class",
    "register",
    "reset_backends",
    "resolve_backend_for_palace",
    "unregister",
]
