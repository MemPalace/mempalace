# Spec: `mempalace/backends/__init__.py`

## Purpose

This is the public package facade for MemPalace storage backends (RFC 001). It defines no behavior of its own; it aggregates and re-exports the public surface from sibling modules so that consumers import everything from one stable namespace (`mempalace/backends/__init__.py:L1-L15`). An implementation in any language should expose a single package/namespace that surfaces the symbols listed below, sourced from the corresponding submodules.

## Re-exported Contract Symbols (from the `base` submodule)

The package re-exports the following abstract contract and value types from `base` (`mempalace/backends/__init__.py:L17-L37`):

- Abstract contracts: `BaseBackend` (per-palace factory contract) and `BaseCollection` (per-collection read/write contract) (`mempalace/backends/__init__.py:L5-L6`, `mempalace/backends/__init__.py:L21-L22`).
- Value/identity object: `PalaceRef` — identifies a palace for a backend (`mempalace/backends/__init__.py:L7`, `mempalace/backends/__init__.py:L32`).
- Typed read returns: `QueryResult`, `GetResult` (`mempalace/backends/__init__.py:L8`, `mempalace/backends/__init__.py:L26`, `mempalace/backends/__init__.py:L33`).
- Lexical/health/maintenance result types: `HealthStatus`, `LexicalHit`, `LexicalResult`, `MaintenanceResult` (`mempalace/backends/__init__.py:L27-L30`).
- Error classes: `BackendError`, `BackendClosedError`, `BackendMismatchError`, `CollectionNotInitializedError`, `DimensionMismatchError`, `EmbedderIdentityMismatchError`, `PalaceNotFoundError`, `UnsupportedCapabilityError`, `UnsupportedFilterError`, `UnsupportedMaintenanceKindError` (`mempalace/backends/__init__.py:L18-L36`).

## Re-exported Concrete Backends (from backend submodules)

The package re-exports concrete backend implementations and their collection classes, one pair per storage engine (`mempalace/backends/__init__.py:L38-L41`):

- `ChromaBackend` / `ChromaCollection` — the in-tree default backend (`mempalace/backends/__init__.py:L14`, `mempalace/backends/__init__.py:L38`).
- `PgVectorBackend` / `PgVectorCollection` (`mempalace/backends/__init__.py:L39`).
- `QdrantBackend` / `QdrantCollection` (`mempalace/backends/__init__.py:L40`).
- `SQLiteExactBackend` / `SQLiteExactCollection` (`mempalace/backends/__init__.py:L41`).

## Re-exported Registry Functions (from the `registry` submodule)

The package re-exports the backend registry API (`mempalace/backends/__init__.py:L42-L52`):

- `get_backend`, `get_backend_class` — resolve a backend instance / class (`mempalace/backends/__init__.py:L46-L47`).
- `register`, `unregister`, `reset_backends` — mutate the registry of available backends (`mempalace/backends/__init__.py:L48-L49`, `mempalace/backends/__init__.py:L51`).
- `available_backends` — enumerate registered backends (`mempalace/backends/__init__.py:L43`).
- `detect_backend_for_path`, `detect_backends_for_path` — infer the backend(s) for a given on-disk palace path (`mempalace/backends/__init__.py:L44-L45`).
- `resolve_backend_for_palace` — resolve the backend for a palace reference (`mempalace/backends/__init__.py:L50`).

## Public Surface Invariant

The exported public namespace is explicitly enumerated and is the authoritative list of symbols this package promises to consumers; it contains exactly the 37 names listed and they must all be importable from the package root (`mempalace/backends/__init__.py:L54-L91`). The enumerated public list is a superset of the docstring summary: every concrete backend pair, every registry function, and every contract/error/result type re-exported above appears in it (`mempalace/backends/__init__.py:L54-L91`). Any symbol not present in this list is not part of the package's public contract.

## Side Effects

Importing the package transitively imports the `base`, `chroma`, `pgvector`, `qdrant`, `sqlite_exact`, and `registry` submodules; any import-time side effects of those modules (e.g. backend registration) occur as a consequence of loading this facade (`mempalace/backends/__init__.py:L17-L52`). This file itself performs no filesystem, network, process, or environment access beyond importing those submodules.
