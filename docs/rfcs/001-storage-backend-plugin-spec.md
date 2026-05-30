# RFC 001 — Storage Backend Plugin Specification

- **Status:** Draft
- **Tracking issue:** [#737](https://github.com/MemPalace/mempalace/issues/737)
- **Supersedes:** The informal seam introduced by [#413](https://github.com/MemPalace/mempalace/pull/413)
- **Related:** [#266](https://github.com/MemPalace/mempalace/issues/266), [#574](https://github.com/MemPalace/mempalace/pull/574), [#643](https://github.com/MemPalace/mempalace/pull/643), [#665](https://github.com/MemPalace/mempalace/pull/665), [#697](https://github.com/MemPalace/mempalace/pull/697), [#700](https://github.com/MemPalace/mempalace/pull/700), [#381](https://github.com/MemPalace/mempalace/pull/381)
- **Spec version:** `1.0`

## Summary

A formal contract for MemPalace storage backends so third parties can ship `pip install mempalace-<name>` packages that drop into the core without patches. The spec defines the collection interface, the backend lifecycle, registration via Python entry points, configuration shape, a required test contract, and a migration path between backends.

It also sets up MemPalace to run as a long-lived daemon that manages many palaces, where different palaces may route to different backends.

## Motivation

Six backend PRs are currently in flight. Each one solves the same problem six different ways — different method signatures, different registration mechanisms, different embedder ownership, incompatible where-clause dialects, no shared test suite. The ad-hoc `BaseCollection` ABC merged in #413 was deliberately minimal and deferred every non-obvious decision. This RFC closes the open decisions so backend authors can build to a stable contract.

## Goals

1. A backend ships as a standalone Python package; installing it is sufficient to use it.
2. All callers in MemPalace core go through the collection interface. No direct `chromadb` imports outside `mempalace/backends/chroma.py`.
3. Backends are interchangeable: every backend passes the same shared test suite, and `mempalace migrate` supports lossless movement between them when source/target capabilities allow, with explicit re-embedding as the fallback (§8.2).
4. The model scales from single-user local (one backend, one palace, no config) to a daemon serving many palaces with heterogeneous backends.
5. Chroma's current dict-shaped return values are not the long-term contract. Typed results are spec v1.

## Non-goals

- Defining the embedder pipeline in detail. The embedder is a separate contract this spec depends on but does not specify.
- Defining the sync subsystem. This spec only declares the capability flag and the minimal hook a sync subsystem will read.
- Specifying wire protocol for a future networked daemon. That is a separate RFC.

---

## 1. Collection contract

### 1.1 Required methods

All backends implement `BaseCollection` with kwargs-only signatures:

```python
class BaseCollection(ABC):
    @abstractmethod
    def add(
        self,
        *,
        documents: list[str],
        ids: list[str],
        metadatas: list[dict] | None = None,
        embeddings: list[list[float]] | None = None,
    ) -> None: ...

    @abstractmethod
    def upsert(
        self,
        *,
        documents: list[str],
        ids: list[str],
        metadatas: list[dict] | None = None,
        embeddings: list[list[float]] | None = None,
    ) -> None: ...

    @abstractmethod
    def query(
        self,
        *,
        query_texts: list[str] | None = None,
        query_embeddings: list[list[float]] | None = None,
        n_results: int = 10,
        where: dict | None = None,
        where_document: dict | None = None,
        include: list[str] | None = None,
    ) -> QueryResult: ...

    @abstractmethod
    def get(
        self,
        *,
        ids: list[str] | None = None,
        where: dict | None = None,
        where_document: dict | None = None,
        limit: int | None = None,
        offset: int | None = None,
        include: list[str] | None = None,
    ) -> GetResult: ...

    @abstractmethod
    def delete(
        self,
        *,
        ids: list[str] | None = None,
        where: dict | None = None,
    ) -> None: ...

    @abstractmethod
    def count(self) -> int: ...
```

### 1.2 Optional methods (default implementations on the ABC)

```python
def estimated_count(self) -> int:
    return self.count()

def close(self) -> None:
    return None

def health(self) -> HealthStatus:
    return HealthStatus.ok()

def update(
    self,
    *,
    ids: list[str],
    documents: list[str] | None = None,
    metadatas: list[dict] | None = None,
    embeddings: list[list[float]] | None = None,
) -> None:
    """Partial update of existing rows. At least one of documents/metadatas/embeddings must be non-None.

    Default implementation: get(ids=...), merge the provided fields, upsert. Non-atomic
    and does two round-trips. Backends advertising `supports_update` MUST override with
    an atomic, single-round-trip implementation.
    """
    ...  # default impl in the ABC
```

Backends with cheap approximate counters override `estimated_count`. Backends that hold connections must override `close`. Backends with native partial-update primitives (Postgres `UPDATE`, Lance `merge_insert`) override `update` and advertise `supports_update`; the token signals "atomic + single round-trip," not "supports partial updates at all" — the default implementation already supports them, just non-atomically.

### 1.3 Typed results (replaces Chroma dict shape)

```python
@dataclass(frozen=True)
class QueryResult:
    ids: list[list[str]]                              # outer = queries, inner = hits
    documents: list[list[str]]
    metadatas: list[list[dict]]
    distances: list[list[float]]
    embeddings: list[list[list[float]]] | None = None

@dataclass(frozen=True)
class GetResult:
    ids: list[str]
    documents: list[str]
    metadatas: list[dict]
    embeddings: list[list[float]] | None = None
```

On empty results: return a result object with empty inner lists, never raise. Specifically, an empty query returns `QueryResult(ids=[[]], documents=[[]], metadatas=[[]], distances=[[]])` — the outer dimension is the number of query vectors issued; the inner dimension is hits per query and may be zero.

`include` controls which fields are populated. Fields not in `include` are populated with empty lists of the correct outer shape; they are never `None` (except `embeddings`, which is `None` when not requested).

### 1.4 Where-clause dialect

**Required operators:** `$eq`, `$ne`, `$in`, `$nin`, `$and`, `$or`, `$contains`.

Backends that do not support full-text natively MUST still implement `$contains` via payload string match — correctness is required; performance is not. `supports_contains_fast` (§2.1) is the only performance floor the spec promises. Without it, callers and benchmarks MUST assume `$contains` is O(n). This is an intentional split: `$contains` is a correctness requirement, `contains_fast` is the performance boundary, and the gap between scan and indexed FTS is too large for the spec to paper over.

**Unknown operators:** backends MUST raise `UnsupportedFilterError`. Silent dropping is forbidden — it produces incorrect results.

**Optional operators:** `$gt`, `$gte`, `$lt`, `$lte`. Backends either implement them or reject with `UnsupportedFilterError`. Advertised via capabilities.

### 1.5 Embeddings

#### Signature compliance (all backends)

All backends MUST accept a pre-computed `embeddings=` argument on `add` / `upsert` without raising. This is signature compliance only — it does not guarantee the vectors are persisted (see passthrough below). Capability token: `supports_embeddings_in`.

Backends MUST NOT hardcode embedding models or dimensions. Model selection is the embedder's responsibility (§4).

#### Passthrough vs re-embed (separate guarantee)

Accepting the argument is not the same as honoring it. Two distinct semantics, distinguished by capability:

- **`supports_embeddings_passthrough`** — when `embeddings=` is provided, the backend MUST persist those vectors as-is and MUST NOT re-embed from text. This is the stronger guarantee lossless migration depends on.
- **No `supports_embeddings_passthrough`** — the backend always re-embeds from text at write time. Provided `embeddings=` is accepted (signature compliance) but discarded. Migration *to* such a backend is re-embedding, not lossless transfer.

`supports_migration_export` (source-side bulk read) MUST be paired with `supports_embeddings_passthrough` (target-side lossless write) for a migration to be labeled lossless. The `mempalace migrate` CLI refuses to run between backends where the target lacks `supports_embeddings_passthrough` unless `--accept-re-embed` is passed, which records re-embedding in the target palace's migration log.

#### Dimension check (all backends, required)

Backends MUST validate embedding dimension on first write to a new collection and on open of an existing collection, and MUST raise `DimensionMismatchError` on mismatch. Silent acceptance of mismatched dimensions produces unrecoverable corruption.

#### Model identity check (all backends, three-state)

Dimension matching is necessary but not sufficient. Swapping to a different model that happens to share a dimension (e.g., both 384-d) silently degrades retrieval without tripping `DimensionMismatchError`. Backends MUST persist `embedder.model_name` alongside the collection on first write and MUST check it on subsequent open. Three outcomes:

| State | Condition | Required behavior |
|---|---|---|
| `known_match` | Stored name equals current `embedder.model_name` | Proceed normally. |
| `known_mismatch` | Stored name exists and differs from current | Raise `EmbedderIdentityMismatchError`. Override only via explicit CLI `--force-model-swap`, which writes the swap to the palace's migration log and updates the stored identity. |
| `unknown` | No model name recorded (legacy collection, pre-v1 palace) | Do not hard-fail — emit a `EmbedderIdentityUnknownWarning` on first open. The resolved identity is recorded on the next successful write, reindex, or migration, transitioning the palace to `known_match` going forward. CLI exposes `mempalace palace set-embedder --model NAME` for explicit resolution. |

The `unknown` state exists because existing palaces from #413 and earlier have no recorded identity; hard-failing them on upgrade would be hostile. Once recorded, subsequent opens are strict.

#### `server_embedder` backends are not exempt

A backend advertising `server_embedder` (§2.1) provides its own embedder and MAY ignore the `embedder=` kwarg passed to `get_collection`. That does **not** exempt it from the dimension and identity rules above. Such backends MUST:

- Expose an effective `model_name: str` and `dimension: int` describing the embedder actually in use (via `BaseCollection.effective_embedder_identity() -> EmbedderIdentity`).
- Persist that effective identity on first write and validate it on open, per the three-state rules above.
- Raise `DimensionMismatchError` and `EmbedderIdentityMismatchError` on conflicts between the effective identity and any injected `embedder` (if one was passed) or between the stored identity and the current effective identity.

`server_embedder` documents where the embedding happens; it never suspends the safety contract. A backend that cannot report its effective embedder identity does not qualify for the `server_embedder` capability.

---

## 2. Backend contract

### 2.1 Identity and capabilities

```python
class BaseBackend(ABC):
    name: ClassVar[str]                    # "chroma", "postgres", "qdrant", ...
    spec_version: ClassVar[str] = "1.0"    # which spec version this backend targets
    capabilities: ClassVar[frozenset[str]]
```

Defined capability tokens (v1):

| Token | Meaning |
|---|---|
| `supports_embeddings_in` | Accepts pre-computed `embeddings=` without raising (signature compliance; MUST be true for all backends) |
| `supports_embeddings_passthrough` | Persists provided `embeddings=` as-is without re-embedding (required for lossless migration target) |
| `supports_embeddings_out` | Returns embeddings when `include=["embeddings"]` is requested |
| `supports_estimated_count` | `estimated_count()` is meaningfully cheaper than `count()` |
| `supports_update` | `update()` is atomic and single-round-trip (vs the ABC default of get+merge+upsert) |
| `supports_metadata_filters` | Implements the required where-clause subset (§1.4) |
| `supports_range_filters` | Implements `$gt` / `$gte` / `$lt` / `$lte` |
| `supports_contains_fast` | `$contains` is indexed (vs scan-based) |
| `supports_server_side_indexes` | Exposes index creation / maintenance to operators |
| `supports_migration_export` | Implements a bulk read path suitable for `mempalace migrate` |
| `supports_change_feed` | Exposes `changes_since(cursor)` for the sync subsystem |
| `supports_sync` | Implies `supports_change_feed` plus idempotent upserts under conflicts |
| `requires_external_service` | Needs a running server (e.g., Postgres, hosted Qdrant) |
| `local_mode` | Persists to `palace.local_path` |
| `server_mode` | Connects to an external server; `palace.namespace` is used |
| `server_embedder` | Backend provides its own embedder (may ignore injected one) |

A backend may advertise both `local_mode` and `server_mode` (e.g., Chroma with either `PersistentClient` or `HttpClient`).

Capability tokens are free-form strings, not an enum — third-party backends may declare novel capabilities for their ecosystem. Core MemPalace only inspects the tokens listed above.

### 2.2 Palace references

A backend serves palaces, not raw filesystem paths. This is the central change from #413.

```python
@dataclass(frozen=True)
class PalaceRef:
    id: str                          # stable identity, used as cache key
    local_path: str | None = None    # filesystem root, if this palace is local
    namespace: str | None = None     # server-side namespace/prefix, if applicable
```

Rules:
- `id` is always present. It is the key the backend uses to cache open handles.
- Local-only backends read `local_path`. If `local_path is None` they raise `PalaceNotFoundError`.
- Server-only backends read `namespace`. If `namespace is None` they derive one deterministically from `id`.
- Mixed-mode backends may use both (e.g., a local cache alongside a server store).

### 2.3 Methods

```python
class BaseBackend(ABC):
    @abstractmethod
    def get_collection(
        self,
        *,
        palace: PalaceRef,
        collection_name: str,
        create: bool,
        embedder: Embedder | None = None,
        options: dict | None = None,
    ) -> BaseCollection: ...

    def close_palace(self, palace: PalaceRef) -> None:
        """Evict a single palace's cached handles. Default: no-op."""
        return None

    def close(self) -> None:
        """Shut down the entire backend instance. Default: no-op."""
        return None

    def health(self, palace: PalaceRef | None = None) -> HealthStatus:
        """Return health. With palace=None, probe the backend itself."""
        return HealthStatus.ok()
```

### 2.4 Semantics of `create`

- `create=False` on a nonexistent palace MUST raise `PalaceNotFoundError` (subclass of `FileNotFoundError` for backwards compatibility with the #413 seam).
- `create=True` MUST be idempotent — calling it repeatedly with the same arguments produces the same state and does not corrupt existing data.
- `create=True` on local backends creates the directory with `0700` permissions (matches the existing Chroma behavior).

### 2.5 Concurrency

A backend instance is long-lived and serves many palaces. Backends MUST be thread-safe for concurrent `get_collection` calls across different `PalaceRef.id` values. Collection handles for the same `(palace.id, collection_name)` MAY be cached internally and returned on subsequent calls.

Backends MAY assume a single thread accesses a given `BaseCollection` instance at a time. MemPalace core serializes access per palace; backend authors are not required to make individual collections thread-safe.

### 2.6 Lifecycle

1. `__init__`: lightweight. No I/O, no network connections. A backend instance may be constructed and never used.
2. First call to `get_collection`: may open connections, create schemas, etc. All I/O is lazy.
3. `close_palace(palace)`: releases cached handles for one palace. Safe to call on a palace that was never opened.
4. `close()`: releases all resources. After `close()`, further calls MUST raise `BackendClosedError`.

There is no explicit `connect()` — it is always implicit and lazy, matching current Chroma behavior.

---

## 3. Registration and discovery

### 3.1 Entry points (primary mechanism)

Third-party backends ship as installable packages:

```toml
# pyproject.toml of mempalace-postgres
[project.entry-points."mempalace.backends"]
postgres = "mempalace_postgres:PostgresBackend"
```

MemPalace discovers backends at process start via `importlib.metadata.entry_points(group="mempalace.backends")`. No patches to the core are required.

### 3.2 In-tree registry (secondary)

For tests and local development:

```python
from mempalace.backends.registry import register

register("my-experimental-backend", MyBackend)
```

Entry-point discovery and explicit `register()` populate the same registry. Explicit registration wins on name conflict.

### 3.3 Selection priority

When resolving a palace's backend, priority (highest first):

1. Explicit `backend=` kwarg to `Palace(...)` or CLI `--backend`
2. Per-palace `backend` key in config (see §4)
3. `MEMPALACE_BACKEND` environment variable
4. Auto-detect from on-disk artifacts: `chroma.sqlite3` → `chroma`, `*.lance` → `lance`, etc. Backends declare detection hints via an optional `BaseBackend.detect(path: str) -> bool` classmethod.
5. Default: `chroma`.

**Auto-detection is strictly a migration/upgrade compatibility path, not a general selection mechanism.** It exists so existing palaces from v3.x keep opening without forced config migration. For *new* palaces, explicit configuration or CLI flag always wins — creating a palace without a resolved backend from (1)–(3) falls through to default (5), never to detection (4). Auto-detection fires only when a local path is presented AND no earlier rule has chosen a backend AND the path already contains backend-identifiable artifacts.

---

## 4. Configuration

### 4.1 Shape

```json
{
  "backends": {
    "chroma": { "type": "chroma" },
    "pg_prod": {
      "type": "postgres",
      "dsn": "postgresql://...",
      "pool_size": 10
    }
  },
  "palaces": {
    "work": {
      "backend": "pg_prod",
      "namespace": "work"
    },
    "personal": {
      "backend": "chroma",
      "local_path": "~/.mempalace/personal"
    }
  },
  "embedder": {
    "type": "onnx",
    "model": "all-MiniLM-L6-v2"
  }
}
```

Single-user local mode: all of this is optional. The absence of a config file yields one Chroma backend, one palace at the default path, with the default embedder.

### 4.2 Environment variables

- `MEMPALACE_BACKEND` — shortcut for the default backend type when there is no config.
- `MEMPALACE_<NAME>_*` — per-backend secrets and connection info (e.g., `MEMPALACE_POSTGRES_DSN`, `MEMPALACE_QDRANT_URL`, `MEMPALACE_QDRANT_API_KEY`).
- Secrets MUST be readable from env vars; config files are for structure, env vars for credentials.

### 4.3 Backend-specific options

The `options` kwarg to `get_collection` is a free-form dict. Each backend documents its accepted keys. Unknown keys MUST be ignored (forward compatibility), but the backend MAY log a warning.

### 4.4 Multi-tenancy (absorbs #697)

Per-tenant collection-name prefixing is not a backend concern. It is handled by the resolver layer above backends: `PalaceRef.namespace` carries the tenant identifier, and the backend uses it as given. The `collection_prefix` concept from #697 dissolves into this model.

---

## 5. Embedder contract (minimal, external to this spec)

This spec assumes an `Embedder` protocol, defined fully in a separate RFC:

```python
class Embedder(Protocol):
    model_name: str
    dimension: int
    def embed(self, texts: list[str]) -> list[list[float]]: ...
```

Backends receive an `Embedder` via `get_collection(embedder=...)`. Backends with `server_embedder` capability MAY ignore the injected embedder.

---

## 6. Sync (capability declaration only)

The sync subsystem is out of scope for this spec. What this spec defines:

- `supports_sync` capability flag (§2.1) — a backend advertising it agrees to implement idempotent upserts under conflict and to expose change data.
- Optional method on `BaseCollection`:
  ```python
  def changes_since(self, cursor: SyncCursor) -> Iterator[Change]: ...
  ```
- Backends without `supports_change_feed` / `supports_sync` are rejected by the sync subsystem at bind time.

Local single-user deployments never load the sync subsystem; non-sync-capable backends cost them nothing.

---

## 7. Testing contract

### 7.1 The abstract suite

MemPalace ships `mempalace.backends.testing.AbstractBackendContractSuite` — a pytest mixin. Every backend package ships a concrete subclass:

```python
from mempalace.backends.testing import AbstractBackendContractSuite

class TestPostgresBackend(AbstractBackendContractSuite):
    @pytest.fixture
    def backend(self, tmp_path):
        return PostgresBackend(dsn=os.environ["TEST_PG_DSN"])
```

The suite covers:
- Round-trip for every required method
- Empty-result shape (outer dimension preserved, inner lists empty)
- `create=False` on missing palace raises `PalaceNotFoundError`
- `create=True` is idempotent
- Full required where-clause subset including `$contains`
- Unknown operator raises `UnsupportedFilterError`
- Dimension-mismatch detection
- Unicode text and unicode IDs
- Large batch writes (10k+ items)
- Delete-then-query consistency
- `close()` releases handles and further calls raise `BackendClosedError`
- Concurrent `get_collection` across different palaces is safe

### 7.2 Parametrized core suite

The existing MemPalace test suite is parametrized over all registered backends when `MEMPALACE_TEST_ALL_BACKENDS=1` is set in the environment. This is the "strongest parity claim" — if a backend passes the full core suite, it is drop-in compatible. This is expensive; local development defaults to Chroma only, CI runs all backends on a scheduled job.

### 7.3 Benchmark methodology hooks

Backend-to-backend comparisons are meaningless without accounting for per-backend maintenance state. Postgres with stale planner stats behaves very differently from Postgres post-`VACUUM ANALYZE`; HNSW-based stores behave differently before and after index compaction.

Backends MAY implement `maintenance_state()` returning a structured dict describing the current state (e.g., `{"autovacuum_age_seconds": 42, "last_analyze": "...", "index_build_complete": true}`), and `run_maintenance(kind: str)` to trigger supported kinds. Both are optional.

Supported maintenance kinds MUST be advertised via a class-level frozenset:

```python
class BaseBackend(ABC):
    maintenance_kinds: ClassVar[frozenset[str]] = frozenset()
```

The spec reserves the kind names `"analyze"` (update planner/query statistics), `"compact"` (reclaim space, rewrite storage), and `"reindex"` (rebuild secondary indexes). Backends MAY add their own kinds; the reserved names MUST mean what the spec says if advertised.

`run_maintenance(kind)` MUST raise `UnsupportedMaintenanceKindError` when called with a kind not in `maintenance_kinds`. Advertising a kind without implementing it is a conformance failure.

The benchmark harness under [benchmarks/](../../benchmarks/) records `maintenance_state()` alongside every latency/recall measurement it publishes. Published numbers MUST include three phases: immediately after bulk load, after the backend's native background maintenance has caught up, and after `run_maintenance(kind)` has been called for each kind in `maintenance_kinds`. Harnesses rely on this advertisement to decide what to call — they MUST NOT assume kind names. This prevents comparing an un-`ANALYZE`d Postgres to a settled Chroma and calling the former slow.

### 7.4 ID stability for non-string-ID backends

Backends requiring UUID IDs (Qdrant) use a canonical namespace:

```python
NAMESPACE_MEMPALACE = uuid.UUID("TO-BE-ASSIGNED-ONCE-FOR-ALL-TIME")
backend_id = uuid.uuid5(NAMESPACE_MEMPALACE, original_id)
```

The namespace UUID is fixed at spec v1 adoption and recorded here. This resolves the #700 vs #381 divergence.

---

## 8. Migration

### 8.1 The CLI

```
mempalace migrate --palace PATH --from chroma --to postgres
mempalace migrate --all --to lance
```

Implementation is backend-agnostic: reads from source via `BaseCollection.get(include=["documents", "metadatas", "embeddings"])`, writes to target via `BaseCollection.upsert(...)` with the original embeddings. No backend-specific migration code.

### 8.2 Lossless vs re-embed

Migration is labeled **lossless** only when:

- The source advertises `supports_migration_export` (bulk read includes embeddings), AND
- The target advertises `supports_embeddings_passthrough` (persists provided embeddings as-is), AND
- Source and target agree on `embedder.model_name` (or `--force-model-swap` is explicit).

If the target lacks `supports_embeddings_passthrough`, `mempalace migrate` refuses to run. Passing `--accept-re-embed` overrides — the migration proceeds but re-embeds from document text at write time, and the migration record labels the result as re-embedded rather than lossless. Retrieval quality may shift.

### 8.3 Safety

- Source is never modified. Migration is read-only against the source backend.
- Target palace must not already exist unless `--overwrite` is passed.
- A successful migration writes a `.mempalace-migration.json` record into the target palace containing: source backend name, source path/ref, timestamp, row count, `lossless: true|false`, source and target `embedder.model_name`, and whether `--force-model-swap` or `--accept-re-embed` was used.

### 8.4 Verification

After migration, run `mempalace verify --palace PATH --against SOURCE_PATH --source-backend chroma`. This samples N rows and confirms round-trip parity (ids match, documents match, embedding cosine similarity ≥ 0.999 when the migration was lossless; a looser document-overlap check when re-embedded).

---

## 9. Versioning and compatibility

- `BaseBackend.spec_version` declares which spec version a backend implements.
- MemPalace refuses to load a backend declaring a different major version.
- Minor versions are additive (new optional methods, new capability tokens). Backends declaring an older minor continue to work.
- This is spec v1.0.

---

## 10. Cleanup prerequisite (not in this spec, but gating)

The #413 seam is incomplete. Seven files in `mempalace/` still import `chromadb` directly:

- [mempalace/repair.py:35](https://github.com/MemPalace/mempalace/blob/develop/mempalace/repair.py#L35)
- [mempalace/dedup.py:30](https://github.com/MemPalace/mempalace/blob/develop/mempalace/dedup.py#L30)
- [mempalace/cli.py:171](https://github.com/MemPalace/mempalace/blob/develop/mempalace/cli.py#L171)
- [mempalace/cli.py:278](https://github.com/MemPalace/mempalace/blob/develop/mempalace/cli.py#L278)
- [mempalace/mcp_server.py:32](https://github.com/MemPalace/mempalace/blob/develop/mempalace/mcp_server.py#L32)
- [mempalace/migrate.py:109](https://github.com/MemPalace/mempalace/blob/develop/mempalace/migrate.py#L109)
- Plus the instruction docs.

These must be routed through `BaseCollection` before the spec can be enforced. Combined with the dict-to-typed-result migration from §1.3, this is substantial enough to be its own PR, landing before any new backend implementation merges.

One implementation detail worth flagging for the cleanup PR: `mcp_server._get_client()` caches a `PersistentClient` at module scope and invalidates it on `chroma.sqlite3` inode or mtime changes (merged via [#757](https://github.com/MemPalace/mempalace/pull/757)). Both the cache and the stat-based freshness check are Chroma-specific. They should migrate into `ChromaBackend.get_collection()` (§2.5, handle caching) and `ChromaBackend.close_palace()` (§2.6, explicit flush) during cleanup — other backends do not have a single on-disk SQLite file to stat. The `mempalace_reconnect` MCP tool then becomes a thin wrapper around `backend.close_palace(palace_ref)`.

---

## 11. Impact on in-flight PRs

| PR | Effort to align |
|---|---|
| [#574](https://github.com/MemPalace/mempalace/pull/574) LanceDB | Closest to final shape. Needs `PalaceRef` and typed results. |
| [#665](https://github.com/MemPalace/mempalace/pull/665) Postgres | Decouple embedder; adopt `PalaceRef`. Schema-per-palace already fits the daemon model. |
| [#700](https://github.com/MemPalace/mempalace/pull/700) Qdrant | Decouple embedder; adopt `PalaceRef`; align UUID namespace. |
| [#381](https://github.com/MemPalace/mempalace/pull/381) Qdrant (older) | Same as #700; also subclass `BaseCollection` rather than using a bare `Protocol`. |
| [#643](https://github.com/MemPalace/mempalace/pull/643) PalaceStore | Parametrized-test approach becomes the standard. |
| [#697](https://github.com/MemPalace/mempalace/pull/697) Chroma HttpClient + prefix | `collection_prefix` dissolves into `PalaceRef.namespace`. |

---

## 12. Open questions

None blocking. The following are nice-to-have for a future minor revision:

- Should `changes_since` accept a filter (e.g., "changes to this collection only")?
- Do we want a `BaseBackend.capabilities(palace=...)` variant, so that capabilities can be per-palace (e.g., `supports_contains_fast` depends on whether an index exists)?
- Should `run_maintenance(kind)` return a structured result (rows analyzed, bytes reclaimed) or stay fire-and-forget?

---

## 13. Rollout

1. Land the cleanup PR (§10): route all callers through `BaseCollection`, migrate to typed results. Chroma remains the only backend.
2. Land this spec as-is. Add `AbstractBackendContractSuite`, entry-point discovery, `PalaceRouter`, `PalaceRef`.
3. Update ChromaBackend to spec v1.0 (add capabilities declaration, `detect()` classmethod, `PalaceRef` support).
4. Rebase in-flight backend PRs against the spec. Each must pass the abstract suite.
5. Ship `mempalace migrate` CLI.
6. Update [ROADMAP.md](../../ROADMAP.md) with spec v1.0 adoption under v4.0.0-alpha.
