"""Storage backend contract for MemPalace (RFC 001).

This module defines the surface every storage backend must implement:

* ``BaseCollection`` — the per-collection read/write interface, kwargs-only.
* ``BaseBackend`` — the per-palace factory, addressed by ``PalaceRef``.
* ``QueryResult`` / ``GetResult`` — typed result dataclasses that replace the
  Chroma dict shape as the canonical return type.
* Error classes + ``HealthStatus`` — uniform across backends.

This is the v1 cleanup from RFC 001 §10: full typed results, ``PalaceRef``,
registry-ready ABC. Embedder injection, maintenance hooks, and the full
conformance suite land in follow-up PRs.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import ClassVar, Optional, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class BackendError(Exception):
    """Base class for every storage-backend error raised by core."""


class PalaceNotFoundError(BackendError, FileNotFoundError):
    """Raised when ``get_collection(create=False)`` is called on a missing palace.

    Subclass of ``FileNotFoundError`` so legacy callers that catch the latter
    (pre-#413 seam) keep working unchanged.
    """


class CollectionNotInitializedError(PalaceNotFoundError):
    """Raised when the palace exists on disk but the requested collection has
    never been created (e.g. ``init`` ran but ``mine`` has not).

    Distinct from :class:`PalaceNotFoundError`: the palace dir and DB are
    present and valid, only the collection has not been bootstrapped yet.
    Subclass of :class:`PalaceNotFoundError` (and therefore
    :class:`FileNotFoundError`) so legacy callers catching either parent
    keep working unchanged.
    """


class BackendClosedError(BackendError):
    """Raised when a backend method is called after ``close()``."""


class UnsupportedFilterError(BackendError):
    """Raised when a where-clause uses an operator the backend does not implement.

    Silent dropping of unknown operators is forbidden by spec (RFC 001 §1.4).
    """


class UnsupportedCapabilityError(BackendError):
    """Raised when a backend does not implement an optional capability."""


class UnsupportedMaintenanceKindError(BackendError):
    """Raised when ``run_maintenance(kind)`` is called with an unadvertised kind.

    A backend MUST advertise a kind in ``maintenance_kinds`` before it accepts
    it (RFC 001). Advertising a kind it does not implement is a conformance
    failure; a kind it has no analogue for MUST be omitted, not no-op'd.
    """


class BackendMismatchError(BackendError):
    """Raised when a selected backend does not match existing palace artifacts."""


class DimensionMismatchError(BackendError):
    """Raised when the embedding dimension on write does not match the collection."""


class EmbedderIdentityMismatchError(BackendError):
    """Raised when the stored embedder model name differs from the current one."""


class EmbedderIdentityUnknownWarning(UserWarning):
    """Emitted on first open of a collection with no recorded embedder identity.

    Legacy palaces created before identity tracking carry no model name. Per
    RFC 001 the right behavior is warn-not-fail: the identity is recorded on
    the next write and subsequent opens become strict.
    """


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PalaceRef:
    """A handle to a palace, consumed by backends.

    ``id`` is always present and is the key backends use to cache handles.
    ``local_path`` is populated for filesystem-rooted palaces.
    ``namespace`` is used by server-mode backends for tenant / prefix routing.

    Isolation contract (RFC 001 §2.1, conformance: ``tests/test_backend_conformance.py``)
    -----------------------------------------------------------------------------------
    ``id`` is the *required* isolation key. Within a single backend instance:

        A record written for one ``PalaceRef.id`` MUST NOT be returned,
        modified, or deleted by an operation issued for a different
        ``PalaceRef.id``. Cross-palace access is a spec violation.

    ``namespace`` is *additional* partitioning, honored only by backends that
    advertise the ``supports_namespace_isolation`` capability. For those
    backends the same guarantee extends to namespaces:

        A record written under one ``namespace`` MUST NOT be returned,
        modified, or deleted by an operation issued under a different
        ``namespace`` within the same backend instance. Cross-namespace
        access is a spec violation.

    Backends that do not advertise ``supports_namespace_isolation`` (e.g.
    ``sqlite_exact``, whose isolation is the on-disk path alone) MAY ignore
    ``namespace`` entirely; callers MUST NOT rely on it for tenant isolation
    on such backends. Any conforming backend can self-check both guarantees by
    running the shared assertions in ``tests/_backend_conformance.py``.
    """

    id: str
    local_path: Optional[str] = None
    namespace: Optional[str] = None


@dataclass(frozen=True)
class EmbedderIdentity:
    """Identity of the embedder that produced a collection's vectors (RFC 001).

    ``model_name`` is the stable identity persisted alongside a collection and
    checked on subsequent opens. ``dimension`` is the vector width. A
    ``dimension`` of ``0`` means *unknown / not probed* — comparisons treat it
    as "no dimension signal" rather than a real zero-width vector, so a cheap
    read-path check can compare model names without loading the model.
    """

    model_name: str
    dimension: int = 0


@dataclass(frozen=True)
class MaintenanceResult:
    """Observable outcome of ``run_maintenance(kind)`` (RFC 001).

    Maintenance is *not* fire-and-forget: a backend MUST serialize concurrent
    same-kind runs and report the outcome so a caller can learn it must not
    re-trigger. ``status`` is one of:

    * ``"ran"`` — this call performed the maintenance.
    * ``"already_running"`` — another caller holds the work; this call did
      nothing and the caller MUST NOT re-trigger (the production index-build
      wedge: concurrent writers each issuing the build stacked exclusive locks).
    * ``"noop"`` — nothing needed doing (e.g. the index already exists).

    ``stats`` is free-form per kind (rows analyzed, bytes reclaimed, index
    build time) for benchmark/operator reporting.
    """

    kind: str
    status: str
    stats: dict = field(default_factory=dict)


@runtime_checkable
class Embedder(Protocol):
    """Minimal embedder contract (RFC 001, normative for identity checking).

    The fuller embedder RFC (batching/async/pooling) is additive; identity
    enforcement depends only on these three members.
    """

    model_name: str
    dimension: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


def check_embedder_identity(
    stored: Optional[EmbedderIdentity],
    current: Optional[EmbedderIdentity],
    *,
    force_model_swap: bool = False,
) -> str:
    """Three-state embedder-identity check (RFC 001).

    Returns the resolved state and raises on a hard, unforced conflict:

    * ``"unknown"`` — no identity recorded yet (legacy collection), or the
      current embedder is nameless. The caller warns and records on write.
    * ``"known_match"`` — stored name (and dimension, when both known) equal
      the current embedder. Proceed normally.
    * ``"known_mismatch"`` — names or dimensions differ. Without
      ``force_model_swap`` this raises (:class:`EmbedderIdentityMismatchError`
      for a model swap, :class:`DimensionMismatchError` for a width change,
      which is checked first because mismatched vectors are physically
      unusable). With ``force_model_swap`` it returns the state so the caller
      can re-record the identity and log the swap.

    A ``dimension`` of ``0`` on either side means "unknown" and is skipped, so
    a model-name-only check (cheap read path) still works.
    """
    if current is None or not current.model_name:
        return "unknown"
    if stored is None:
        return "unknown"

    dim_conflict = bool(stored.dimension and current.dimension) and (
        stored.dimension != current.dimension
    )
    name_conflict = stored.model_name != current.model_name

    if not dim_conflict and not name_conflict:
        return "known_match"

    if force_model_swap:
        return "known_mismatch"

    if dim_conflict:
        raise DimensionMismatchError(
            f"collection was built with a {stored.dimension}-dim embedder "
            f"({stored.model_name!r}) but the current embedder is "
            f"{current.dimension}-dim ({current.model_name!r}); the stored "
            "vectors are incompatible. Re-embed the palace to switch models."
        )
    raise EmbedderIdentityMismatchError(
        f"collection was built with embedder {stored.model_name!r} but the "
        f"current embedder is {current.model_name!r}. Searching across a model "
        "swap silently degrades recall. Re-embed the palace, or run "
        "`mempalace palace set-embedder --model <name> --force` to record the "
        "new identity if you know the vectors are compatible."
    )


@dataclass(frozen=True)
class HealthStatus:
    ok: bool
    detail: str = ""

    @classmethod
    def healthy(cls, detail: str = "") -> "HealthStatus":
        return cls(ok=True, detail=detail)

    @classmethod
    def unhealthy(cls, detail: str) -> "HealthStatus":
        return cls(ok=False, detail=detail)


_TYPED_RESULT_FIELDS = ("ids", "documents", "metadatas", "distances", "embeddings")


class _DictCompatMixin:
    """Transitional dict-protocol access for typed results.

    RFC 001 §1.3 spec is attribute access (``result.ids``). The ``result["ids"]``
    and ``result.get("ids")`` forms are retained as a migration shim for callers
    that predate the typed interface and are scheduled for removal in a follow-
    up cleanup. New code MUST use attribute access.
    """

    def __getitem__(self, key: str):
        if key in _TYPED_RESULT_FIELDS:
            return getattr(self, key)
        raise KeyError(key)

    def get(self, key: str, default=None):
        if key in _TYPED_RESULT_FIELDS:
            val = getattr(self, key, default)
            return default if val is None else val
        return default

    def __contains__(self, key: object) -> bool:
        return key in _TYPED_RESULT_FIELDS and getattr(self, key, None) is not None


@dataclass(frozen=True)
class QueryResult(_DictCompatMixin):
    """Typed return from ``BaseCollection.query``.

    Outer list dimension = number of query vectors / texts.
    Inner list dimension = hits per query (may be zero).

    Fields not in ``include=`` at the call site are populated with empty lists
    of the correct outer shape (never ``None``), except ``embeddings`` which
    is ``None`` when not requested.
    """

    ids: list[list[str]]
    documents: list[list[str]]
    metadatas: list[list[dict]]
    distances: list[list[float]]
    embeddings: Optional[list[list[list[float]]]] = None

    @classmethod
    def empty(cls, num_queries: int = 1, embeddings_requested: bool = False) -> "QueryResult":
        """Construct an all-empty result preserving outer dimension.

        When ``embeddings_requested`` is True, ``embeddings`` preserves the outer
        query dimension with empty hit lists (matching the spec's rule that fields
        requested via ``include=`` carry the outer shape even when empty). When
        False, ``embeddings`` stays ``None`` to signal the field was not requested.
        """
        empty_outer = [[] for _ in range(num_queries)]
        return cls(
            ids=[[] for _ in range(num_queries)],
            documents=[[] for _ in range(num_queries)],
            metadatas=[[] for _ in range(num_queries)],
            distances=[[] for _ in range(num_queries)],
            embeddings=empty_outer if embeddings_requested else None,
        )


@dataclass(frozen=True)
class GetResult(_DictCompatMixin):
    """Typed return from ``BaseCollection.get``."""

    ids: list[str]
    documents: list[str]
    metadatas: list[dict]
    embeddings: Optional[list[list[float]]] = None

    @classmethod
    def empty(cls) -> "GetResult":
        return cls(ids=[], documents=[], metadatas=[], embeddings=None)


@dataclass(frozen=True)
class LexicalHit:
    """One hit from backend lexical candidate search."""

    id: str
    document: str
    metadata: dict
    score: float


@dataclass(frozen=True)
class LexicalResult:
    """Typed return from ``BaseCollection.lexical_search``."""

    hits: list[LexicalHit]


# ---------------------------------------------------------------------------
# Collection contract
# ---------------------------------------------------------------------------


class BaseCollection(ABC):
    """Per-collection read/write surface every backend must implement."""

    @abstractmethod
    def add(
        self,
        *,
        documents: list[str],
        ids: list[str],
        metadatas: Optional[list[dict]] = None,
        embeddings: Optional[list[list[float]]] = None,
    ) -> None: ...

    @abstractmethod
    def upsert(
        self,
        *,
        documents: list[str],
        ids: list[str],
        metadatas: Optional[list[dict]] = None,
        embeddings: Optional[list[list[float]]] = None,
    ) -> None: ...

    @abstractmethod
    def query(
        self,
        *,
        query_texts: Optional[list[str]] = None,
        query_embeddings: Optional[list[list[float]]] = None,
        n_results: int = 10,
        where: Optional[dict] = None,
        where_document: Optional[dict] = None,
        include: Optional[list[str]] = None,
    ) -> QueryResult: ...

    @abstractmethod
    def get(
        self,
        *,
        ids: Optional[list[str]] = None,
        where: Optional[dict] = None,
        where_document: Optional[dict] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        include: Optional[list[str]] = None,
    ) -> GetResult: ...

    @abstractmethod
    def delete(
        self,
        *,
        ids: Optional[list[str]] = None,
        where: Optional[dict] = None,
    ) -> None: ...

    @abstractmethod
    def count(self) -> int: ...

    # ------------------------------------------------------------------
    # Optional methods with ABC defaults (spec §1.2)
    # ------------------------------------------------------------------

    def estimated_count(self) -> int:
        return self.count()

    def close(self) -> None:
        return None

    def health(self) -> HealthStatus:
        return HealthStatus.healthy()

    @property
    def distance_metric(self) -> str:
        """The space this collection's ``distances`` are reported in.

        Defaults to the owning backend's declared metric (cosine for all
        in-tree backends). Collections that can vary per-collection — e.g. a
        legacy Chroma palace built without ``hnsw:space=cosine`` — override
        this to report their actual space so core ranking converts correctly.
        """
        return "cosine"

    def get_stored_embedder_identity(self) -> Optional[EmbedderIdentity]:
        """Return the embedder identity recorded for this collection, if any.

        Returns ``None`` when nothing is recorded — a legacy collection, or a
        backend that does not yet persist identity. Core treats ``None`` as the
        ``unknown`` state (warn, do not fail). Backends override this and
        :meth:`set_embedder_identity` against their own metadata store.
        """
        return None

    def set_embedder_identity(self, identity: EmbedderIdentity) -> None:
        """Persist this collection's embedder identity. Default: no-op.

        A backend without an identity slot inherits the no-op default and so
        stays permanently ``unknown`` (safe — it simply never enforces). The
        enforcement choke point calls this when recording on first write or
        on an explicit, forced model swap.
        """
        return None

    def effective_embedder_identity(self) -> Optional[EmbedderIdentity]:
        """The identity of the embedder this collection actually uses.

        For ``server_embedder`` backends that ignore the injected embedder,
        this reports the server-side embedder so the same identity rules apply
        (RFC 001). Defaults to ``None`` — the collection is embedded by the
        injected/core embedder, and the caller supplies the current identity.
        """
        return None

    def maintenance_state(self) -> dict:
        """Return a structured snapshot of this collection's maintenance state.

        Free-form per backend (e.g. row count, whether a vector index exists,
        last-analyze age). Used by benchmark harnesses to record state
        alongside each latency/recall measurement so an un-analyzed store is
        not compared against a settled one (RFC 001). Defaults to empty.
        """
        return {}

    def run_maintenance(self, kind: str) -> "MaintenanceResult":
        """Run a maintenance ``kind`` and return an observable result (RFC 001).

        Backends advertise supported kinds in ``BaseBackend.maintenance_kinds``
        and override this. The default supports nothing, so every kind raises
        :class:`UnsupportedMaintenanceKindError`. Implementations MUST serialize
        concurrent same-kind runs and report ``already_running`` rather than
        stacking the work.
        """
        raise UnsupportedMaintenanceKindError(f"backend does not support maintenance kind {kind!r}")

    def lexical_search(
        self,
        *,
        query: str,
        n_results: int = 10,
        where: Optional[dict] = None,
    ) -> LexicalResult:
        raise UnsupportedCapabilityError("backend does not support lexical_search")

    def update(
        self,
        *,
        ids: list[str],
        documents: Optional[list[str]] = None,
        metadatas: Optional[list[dict]] = None,
        embeddings: Optional[list[list[float]]] = None,
    ) -> None:
        """Default non-atomic update: get + merge + upsert.

        Backends advertising ``supports_update`` MUST override with an atomic
        single-round-trip implementation.
        """
        if documents is None and metadatas is None and embeddings is None:
            raise ValueError("update requires at least one of documents, metadatas, embeddings")

        n = len(ids)
        for label, value in (
            ("documents", documents),
            ("metadatas", metadatas),
            ("embeddings", embeddings),
        ):
            if value is not None and len(value) != n:
                raise ValueError(f"{label} length {len(value)} does not match ids length {n}")

        existing = self.get(ids=ids, include=["documents", "metadatas"])
        by_id = {
            rid: (existing.documents[i], existing.metadatas[i])
            for i, rid in enumerate(existing.ids)
        }
        merged_docs: list[str] = []
        merged_metas: list[dict] = []
        for i, rid in enumerate(ids):
            prev_doc, prev_meta = by_id.get(rid, ("", {}))
            merged_docs.append(documents[i] if documents is not None else prev_doc)
            new_meta = dict(prev_meta or {})
            if metadatas is not None:
                new_meta.update(metadatas[i] or {})
            merged_metas.append(new_meta)
        self.upsert(
            documents=merged_docs,
            ids=list(ids),
            metadatas=merged_metas,
            embeddings=embeddings,
        )


# ---------------------------------------------------------------------------
# Backend contract
# ---------------------------------------------------------------------------


class BaseBackend(ABC):
    """Long-lived factory serving many palaces (RFC 001 §2).

    Instances are lightweight on construction — no I/O, no network. All
    connection work is deferred to ``get_collection``. Instances are thread-
    safe for concurrent ``get_collection`` calls across different palaces.

    Every backend MUST satisfy the per-``PalaceRef.id`` isolation guarantee in
    :class:`PalaceRef`. Backends that additionally isolate by
    ``PalaceRef.namespace`` (multi-tenant / hosted deployments) MUST advertise
    the ``supports_namespace_isolation`` capability token; doing so is a
    promise to satisfy the cross-namespace guarantee and to pass the namespace
    arm of the conformance suite. Backends without the token MAY ignore
    ``namespace``.
    """

    name: ClassVar[str]
    spec_version: ClassVar[str] = "1.0"
    capabilities: ClassVar[frozenset[str]] = frozenset()
    #: The space ``query()`` reports ``distances`` in (RFC 001 §2.1).
    #: One of ``"cosine"`` | ``"l2"`` | ``"ip"``. The contract for the
    #: ``distances`` field is *lower = closer* regardless of metric; core
    #: search converts distance→similarity off this declaration rather than
    #: assuming cosine. All in-tree backends are cosine today.
    distance_metric: ClassVar[str] = "cosine"
    #: Maintenance kinds this backend implements (RFC 001). Reserved names:
    #: ``"analyze"`` (refresh planner/query statistics), ``"compact"`` (reclaim
    #: space, rewrite storage), ``"reindex"`` (build/rebuild secondary indexes).
    #: A backend with no analogue for a kind MUST omit it rather than declare a
    #: no-op, so a benchmark harness can trust the set. Backends MAY add their
    #: own kinds. ``run_maintenance`` raises ``UnsupportedMaintenanceKindError``
    #: for anything not listed here.
    maintenance_kinds: ClassVar[frozenset[str]] = frozenset()

    @abstractmethod
    def get_collection(
        self,
        *,
        palace: PalaceRef,
        collection_name: str,
        create: bool = False,
        options: Optional[dict] = None,
    ) -> BaseCollection: ...

    def close_palace(self, palace: PalaceRef) -> None:
        """Evict cached handles for a single palace. Default: no-op."""
        return None

    def close(self) -> None:
        """Shut down the entire backend. Default: no-op."""
        return None

    def health(self, palace: Optional[PalaceRef] = None) -> HealthStatus:
        return HealthStatus.healthy()

    # Optional detection hint used by selection priority (RFC 001 §3.3 (4)):
    @classmethod
    def detect(cls, path: str) -> bool:  # pragma: no cover - default hook
        return False


# ---------------------------------------------------------------------------
# Adapter utilities
# ---------------------------------------------------------------------------


# Keys the Chroma ``include=`` parameter accepts.
_VALID_INCLUDE_KEYS = frozenset({"documents", "metadatas", "distances", "embeddings"})


@dataclass
class _IncludeSpec:
    """Resolve an ``include=`` parameter with spec-mandated defaults."""

    documents: bool = True
    metadatas: bool = True
    distances: bool = True  # only meaningful for query
    embeddings: bool = False

    @classmethod
    def resolve(
        cls, include: Optional[list[str]], *, default_distances: bool = True
    ) -> "_IncludeSpec":
        if include is None:
            return cls(
                documents=True,
                metadatas=True,
                distances=default_distances,
                embeddings=False,
            )
        keys = {k for k in include if k in _VALID_INCLUDE_KEYS}
        return cls(
            documents="documents" in keys,
            metadatas="metadatas" in keys,
            distances="distances" in keys,
            embeddings="embeddings" in keys,
        )
