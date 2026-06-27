"""Source adapter subsystem (RFC 002).

Public surface:

* :class:`BaseSourceAdapter` — per-source read-side contract.
* Typed records: :class:`SourceRef`, :class:`SourceItemMetadata`,
  :class:`DrawerRecord`, :class:`RouteHint`, :class:`SourceSummary`,
  :class:`AdapterSchema`, :class:`FieldSpec`.
* Error classes: :class:`SourceNotFoundError`, :class:`AuthRequiredError`,
  :class:`AdapterClosedError`, :class:`TransformationViolationError`,
  :class:`SchemaConformanceError`.
* Registry: :func:`register`, :func:`get_adapter`, :func:`available_adapters`,
  :func:`resolve_adapter_for_source`.
* :class:`PalaceContext` — facade core passes to adapters during ``ingest``.
* :mod:`transforms` — reference implementations of the reserved §1.4
  transformations + :func:`get_transformation` resolver.
"""

from .base import (
    AdapterClosedError,
    AdapterSchema,
    AuthRequiredError,
    BaseSourceAdapter,
    DrawerRecord,
    FieldSpec,
    IngestMode,
    IngestResult,
    RouteHint,
    SchemaConformanceError,
    SourceAdapterError,
    SourceItemMetadata,
    SourceNotFoundError,
    SourceRef,
    SourceSummary,
    TransformationViolationError,
)
from .context import PalaceContext, ProgressHook
from .ndjson import NdjsonSourceAdapter
from .registry import (
    available_adapters,
    get_adapter,
    get_adapter_class,
    register,
    reset_adapters,
    resolve_adapter_for_source,
    unregister,
)

# First-party in-tree adapters register on import (RFC 002 §3.2 — explicit
# registration, not the third-party entry-point group). This makes
# ``mine --source ndjson`` resolve without an install step.
register(NdjsonSourceAdapter.name, NdjsonSourceAdapter)

__all__ = [
    "AdapterClosedError",
    "AdapterSchema",
    "AuthRequiredError",
    "BaseSourceAdapter",
    "DrawerRecord",
    "FieldSpec",
    "IngestMode",
    "IngestResult",
    "NdjsonSourceAdapter",
    "PalaceContext",
    "ProgressHook",
    "RouteHint",
    "SchemaConformanceError",
    "SourceAdapterError",
    "SourceItemMetadata",
    "SourceNotFoundError",
    "SourceRef",
    "SourceSummary",
    "TransformationViolationError",
    "available_adapters",
    "get_adapter",
    "get_adapter_class",
    "register",
    "reset_adapters",
    "resolve_adapter_for_source",
    "unregister",
]
