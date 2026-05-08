"""Source adapter contract for MemPalace (RFC 002).

Mirrors what ``mempalace/backends/base.py`` does for the write side: it defines
the read-side surface every source adapter must implement. A source adapter
extracts content from a specific origin (filesystem, git, Slack, Cursor …) and
yields typed records (``SourceItemMetadata`` / ``DrawerRecord``) that core
routes into the palace.

This module is spec scaffolding. The first-party miners (``mempalace/miner.py``
and ``mempalace/convo_miner.py``) are migrated onto it in a follow-up PR;
in this PR we publish the contract so third-party adapters can begin building
against a stable surface.

See ``docs/rfcs/002-source-adapter-plugin-spec.md`` for the authoritative
spec text.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Iterator, Literal, Optional

if TYPE_CHECKING:
    from .context import PalaceContext  # noqa: F401  (used in string annotation)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SourceAdapterError(Exception):
    """Base class for every source-adapter error raised by core."""


class SourceNotFoundError(SourceAdapterError):
    """Raised when a ``SourceRef`` does not resolve to a readable source."""


class AuthRequiredError(SourceAdapterError):
    """Raised when an adapter needs credentials that were not provided.

    The message MUST name the env vars (or other supported mechanism) the
    operator needs to set.
    """


class AdapterClosedError(SourceAdapterError):
    """Raised when an adapter method is called after ``close()``."""


class TransformationViolationError(SourceAdapterError):
    """Raised by the conformance suite when round-tripping a drawer requires
    an undeclared transformation (RFC 002 §7.2–7.3)."""


class SchemaConformanceError(SourceAdapterError):
    """Raised when a ``DrawerRecord.metadata`` violates the adapter schema
    returned by :meth:`BaseSourceAdapter.describe_schema`."""


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceRef:
    """A handle to the source a user wants to ingest.

    ``local_path`` is for filesystem-rooted sources (project dir, mbox file).
    ``uri`` is for URL-like references (``github.com/org/repo``,
    ``slack://workspace/channel``).
    ``options`` carries adapter-specific non-secret config. Secrets MUST NOT
    be placed here; see §4.2.
    """

    local_path: Optional[str] = None
    uri: Optional[str] = None
    options: dict = field(default_factory=dict)


@dataclass(frozen=True)
class RouteHint:
    """Adapter-supplied routing hint (RFC 002 §2.5)."""

    wing: Optional[str] = None
    room: Optional[str] = None
    hall: Optional[str] = None


@dataclass(frozen=True)
class SourceItemMetadata:
    """Lightweight pointer yielded before drawers for lazy-fetch adapters.

    Core inspects ``version`` via :meth:`BaseSourceAdapter.is_current` to
    decide whether to skip extraction; an adapter that responds positively
    stops yielding drawers for this item and moves to the next.
    """

    source_file: str
    version: str
    size_hint: Optional[int] = None
    route_hint: Optional[RouteHint] = None


@dataclass(frozen=True)
class DrawerRecord:
    """One drawer's worth of extracted content plus flat metadata.

    ``metadata`` values MUST be flat scalars (``str``/``int``/``float``/``bool``)
    per RFC 001 §1.4 — the chroma constraint. Nested data belongs on the
    knowledge graph (§5.5) or in a declared ``json_string`` field (§5.4).
    """

    content: str
    source_file: str
    chunk_index: int = 0
    metadata: dict = field(default_factory=dict)
    route_hint: Optional[RouteHint] = None


@dataclass(frozen=True)
class SourceSummary:
    """High-level description of a source returned by :meth:`source_summary`."""

    description: str
    item_count: Optional[int] = None


IngestMode = Literal["chunked_content", "whole_record", "metadata_only"]


@dataclass(frozen=True)
class FieldSpec:
    """Declared shape of a single per-adapter metadata field (§5.2)."""

    type: Literal["string", "int", "float", "bool", "delimiter_joined_string", "json_string"]
    required: bool
    description: str
    indexed: bool = False
    delimiter: str = ";"
    json_schema: Optional[dict] = None


@dataclass(frozen=True)
class AdapterSchema:
    """The per-adapter metadata schema returned by :meth:`describe_schema`."""

    fields: dict[str, FieldSpec]
    version: str


# The union type adapters yield from ``ingest``.
IngestResult = object  # intentionally broad; runtime checks in core


# ---------------------------------------------------------------------------
# Adapter contract
# ---------------------------------------------------------------------------


class BaseSourceAdapter(ABC):
    """Long-lived adapter serving many ``SourceRef`` invocations (RFC 002 §2).

    Instances are lightweight on construction — no I/O, no network, no
    credential fetch. All work is deferred to :meth:`ingest`. Instances are
    thread-safe for concurrent ``ingest`` calls across different ``SourceRef``
    values (v1 serializes within a single ``SourceRef``).

    Class attributes form the adapter's identity contract:

    * ``name`` — stable adapter name used for registration and drawer metadata.
    * ``adapter_version`` — adapter's own version, independent of
      ``spec_version``. Recorded on every drawer so re-extract workflows can
      target drawers from a known-buggy adapter version.
    * ``capabilities`` — free-form tokens; core inspects a documented subset.
    * ``supported_modes`` — subset of ``chunked_content``, ``whole_record``,
      ``metadata_only``.
    * ``declared_transformations`` — set of transformation names the adapter
      applies to source bytes. The empty set marks a byte-preserving adapter.
    * ``default_privacy_class`` — privacy class level (§6) applied unless the
      palace config overrides it.
    """

    name: ClassVar[str]
    spec_version: ClassVar[str] = "1.0"
    adapter_version: ClassVar[str] = "0.0.0"
    capabilities: ClassVar[frozenset[str]] = frozenset()
    supported_modes: ClassVar[frozenset[str]] = frozenset({"chunked_content"})
    declared_transformations: ClassVar[frozenset[str]] = frozenset()
    default_privacy_class: ClassVar[str] = "pii_potential"

    # ------------------------------------------------------------------
    # Required methods
    # ------------------------------------------------------------------

    @abstractmethod
    def ingest(
        self,
        *,
        source: SourceRef,
        palace: "PalaceContext",
    ) -> Iterator[IngestResult]:
        """Enumerate and extract content from a source.

        Yields a stream of ``SourceItemMetadata`` and ``DrawerRecord`` values.
        Lazy adapters yield ``SourceItemMetadata`` ahead of the drawers for
        that item so core can check :meth:`is_current` before committing to
        the fetch. Eager adapters MAY interleave freely.
        """

    @abstractmethod
    def describe_schema(self) -> AdapterSchema:
        """Declare the structured metadata this adapter attaches.

        The returned schema MUST be stable for a given ``adapter_version``.
        Enterprises index on it; core uses it to validate adapter output.
        """

    # ------------------------------------------------------------------
    # Optional methods with default implementations
    # ------------------------------------------------------------------

    def is_current(
        self,
        *,
        item: SourceItemMetadata,
        existing_metadata: Optional[dict],
    ) -> bool:
        """Return True if the palace already has an up-to-date copy of ``item``.

        Default: always returns False (re-extract every time). Adapters
        advertising ``supports_incremental`` MUST override.
        """
        return False

    def source_summary(self, *, source: SourceRef) -> SourceSummary:
        """Describe a source without extracting."""
        return SourceSummary(description=self.name)

    def close(self) -> None:
        """Release any resources the adapter holds. Default: no-op."""
        return None
