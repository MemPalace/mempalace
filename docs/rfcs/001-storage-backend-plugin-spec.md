# RFC 001 — Storage Backend Plugin Specification

- **Status:** Accepted (2026-06-07)
- **Tracking issue:** [#737](https://github.com/MemPalace/mempalace/issues/737)
- **Supersedes:** The informal seam introduced by [#413](https://github.com/MemPalace/mempalace/pull/413)
- **Related:** [#266](https://github.com/MemPalace/mempalace/issues/266), [#574](https://github.com/MemPalace/mempalace/pull/574), [#643](https://github.com/MemPalace/mempalace/pull/643), [#665](https://github.com/MemPalace/mempalace/pull/665), [#697](https://github.com/MemPalace/mempalace/pull/697), [#700](https://github.com/MemPalace/mempalace/pull/700), [#381](https://github.com/MemPalace/mempalace/pull/381), [#1679](https://github.com/MemPalace/mempalace/pull/1679)
- **Spec version:** `1.0`

> **Implementation status (2026-08-11).** The §1–2 contract surface (`PalaceRef`,
> typed results, `BaseBackend` / `BaseCollection`, capability tokens, and most of
> the §10 seam cleanup) landed ahead of this merge via [#1679](https://github.com/MemPalace/mempalace/pull/1679),
> which ships in-tree `pgvector`, `qdrant`, and `sqlite_exact` backends. The three
> areas originally deferred as follow-ups have since landed: embedder-identity
> (§1.5 / §5, [#1731](https://github.com/MemPalace/mempalace/pull/1731) /
> [#1734](https://github.com/MemPalace/mempalace/pull/1734)), maintenance hooks
> (§7.3, [#1732](https://github.com/MemPalace/mempalace/pull/1732)), and searcher
> metric-awareness (§10, [#1727](https://github.com/MemPalace/mempalace/pull/1727)).
> Accepting this RFC pins the contract; remaining gaps are evolutionary, not
> blocking.

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

An injected embedder that exposes no usable `model_name` (empty or `None`) resolves to `unknown` rather than being a hard error — the backend persists no identity, emits `EmbedderIdentityUnknownWarning`, and records identity on the first open against an embedder that *does* report a name. A nameless embedder is therefore a degraded-but-valid mode, not a rejection; operators promote it with `mempalace palace set-embedder --model NAME`.

> **Follow-up dependency.** The `MUST persist embedder.model_name` rule above is only satisfiable once the `Embedder` protocol (§5) is a normative contract — a backend cannot persist an identity it is never handed. §5 now pins the minimal protocol (`model_name`, `dimension`, `embed`) as sufficient for this section; the embedder-identity enforcement is tracked as follow-up implementation work (the current in-tree backends defer it). See §5.

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
    name: ClassVar[str]                       # "chroma", "postgres", "qdrant", ...
    spec_version: ClassVar[str] = "1.0"       # which spec version this backend targets
    capabilities: ClassVar[frozenset[str]]
    distance_metric: ClassVar[str] = "cosine" # "cosine" | "l2" | "ip" (inner product)
```

`distance_metric` declares the space the backend's `distances` are reported in.
It is **not** a capability token (it is a single value, not a boolean), so it is a
class attribute. Core search code MUST convert a backend's reported distance to a
similarity using this declaration rather than assuming cosine — see §10, which
adds `searcher.py` to the cleanup precisely because `_hybrid_rank` currently
hard-codes `max(0, 1 - distance)` (cosine-only). All in-tree backends are `cosine`
today, so the assumption is latent, not yet wrong; the declaration makes a
non-cosine backend (e.g. a dot-product store) correct rather than silently
mis-ranked.

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
| `supports_namespace_isolation` | Enforces `PalaceRef.namespace` as a hard isolation boundary (§4.4). Multi-tenant deployments MAY rely on it for tenant isolation; backends without it MUST NOT be relied on for that. |

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

**Multiple collections per palace.** `get_collection` is keyed by `collection_name`, and a palace MAY hold more than one collection. Backends MUST support N collections per palace, addressed by distinct `collection_name` values, with the §2.5 isolation guarantee applying per `(palace.id, collection_name)`. The "palace" is not 1:1 with a collection: production already splits verbatim drawers from short, query-term-saturated session-recovery checkpoints into sibling collections (`mempalace_drawers` vs `mempalace_session_recovery`) so the latter can't dominate vector top-N. Backends like Postgres (schema/table naming) and Qdrant (collection naming) handle this trivially; a backend author MUST NOT assume one collection per palace and design themselves into a corner. No signature changes — this is already implicit in `collection_name`; it is stated here so it is a contract fact, not a convention.

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

Note the interaction with a globally-set `MEMPALACE_BACKEND`: rule (3) sits above detection (4), so a user who exports `MEMPALACE_BACKEND=postgres` and then opens a palace containing on-disk Chroma artifacts gets postgres — the env var wins and detection is skipped. That is intended (explicit configuration overrides detection), but it means **setting `MEMPALACE_BACKEND` globally overrides existing-palace auto-detection; users opening pre-existing palaces of mixed backends should leave it unset** and rely on per-palace config or detection.

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
- `<NAME>` is the backend's **type** name (the `type` field in §4.1 — `postgres`, `qdrant`), uppercased, not the per-instance config key. So the `pg_prod` instance in §4.1 reads `MEMPALACE_POSTGRES_*`, not `MEMPALACE_PG_PROD_*` — instances of the same type share one env namespace, and connection-specific values that differ per instance (distinct DSNs) belong in the config file's per-backend block, not in env. Hyphens in a type name are normalized to underscores for the env prefix (`my-backend` → `MEMPALACE_MY_BACKEND_*`).
- Secrets MUST be readable from env vars; config files are for structure, env vars for credentials.

### 4.3 Backend-specific options

The `options` kwarg to `get_collection` is a free-form dict. Each backend documents its accepted keys. Unknown keys MUST be ignored (forward compatibility), but the backend MAY log a warning.

### 4.4 Multi-tenancy (absorbs #697)

Per-tenant collection-name prefixing is not a backend concern. It is handled by the resolver layer above backends: `PalaceRef.namespace` carries the tenant identifier. The `collection_prefix` concept from #697 dissolves into this model.

**Isolation contract.** `PalaceRef.id` is the *required* isolation key for every backend: within a single backend instance, a record written for one `id` MUST NOT be returned, modified, or deleted by an operation issued for a different `id`. Cross-palace access is a spec violation. This is the non-negotiable blanket MUST; multi-tenant deployments may cite it as their primary partition boundary even when they do not use `namespace`.

`namespace` is *additional* partitioning. A backend that advertises `supports_namespace_isolation` (§2.1) MUST extend the same guarantee to namespaces:

> A record written under one `namespace` MUST NOT be returned, modified, or deleted by an operation issued under a different `namespace` within the same backend instance. Cross-namespace access is a spec violation, not a caller misconfiguration.

This is what hosted multi-tenant deployments cite as the basis for *namespace*-level tenant isolation. Authorization (which namespaces a given request may touch) stays on the deployment side; the backend's job is to guarantee no bleed *within* the instance once the namespace is fixed.

Making namespace isolation a declared capability rather than a blanket `MUST` is deliberate: path-rooted local backends (e.g. `chroma`, `sqlite_exact`) already isolate by `local_path` / `id`, and forcing them to re-implement a second axis would be ceremony with no security gain.

**Conformance arms.** Self-attestation is not enough. The isolation suite (`tests/_backend_conformance.py` / `assert_partition_isolation`) has two distinct arms:

1. **Cross-`id` isolation** — required of every backend. Two `PalaceRef`s that differ only in `id` MUST NOT see each other's records.
2. **Same-`id` / different-`namespace` isolation** — required of every backend that advertises `supports_namespace_isolation`. Two refs that share `id` (and, when applicable, `local_path`) but differ in `namespace` MUST NOT see each other's records. Distinct from arm 1; a backend that only passes arm 1 does not get to claim the capability.

**No silent drop.** A backend that does **not** advertise `supports_namespace_isolation` MUST NOT silently accept and ignore a populated `namespace` — that is a latent cross-namespace leak, same spirit as `UnsupportedFilterError` for unknown operators. Such backends MUST either raise (e.g. `UnsupportedCapabilityError`) when `PalaceRef.namespace` is non-`None`, or honor the namespace and advertise the capability. Callers targeting path-rooted backends MUST leave `namespace` as `None`.

---

## 5. Embedder contract (minimal, normative here)

§1.5 makes persisting and checking `embedder.model_name` a hard `MUST`. A backend cannot satisfy that against an embedder that has no identity to read, so the minimal protocol below is **normative for this spec** — not deferred. A fuller Embedder RFC (batching, async, pooling, multi-vector) is tracked separately, but it is *additive*: §1.5 conformance depends only on the three members here.

```python
class Embedder(Protocol):
    model_name: str          # stable identity persisted and checked per §1.5
    dimension: int           # validated per §1.5 dimension check
    def embed(self, texts: list[str]) -> list[list[float]]: ...
```

Backends receive an `Embedder` via `get_collection(embedder=...)`. Backends with the `server_embedder` capability MAY ignore the injected embedder but MUST still expose an effective `model_name` / `dimension` (§1.5). An embedder whose `model_name` is empty or `None` is handled as the §1.5 `unknown` state, not a hard error.

> **Follow-up.** The full Embedder RFC is the only external contract §1.5 leans on; it is tracked as a hard, blocking dependency of the §1.5 *implementation* (not of this spec's acceptance — the minimal protocol above closes the contract gap). Tracking issue: see §13. The current in-tree backends defer embedder-identity enforcement until that work lands.

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
- Isolation arm 1: cross-`PalaceRef.id` (every backend)
- Isolation arm 2: same-`id` / different-`namespace` (only when `supports_namespace_isolation` is advertised; self-attestation without this arm is a conformance failure)
- Non-advertising backends raise on a populated `namespace` rather than silently accepting it (§4.4)

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

The spec reserves the kind names `"analyze"` (update planner/query statistics), `"compact"` (reclaim space, rewrite storage), and `"reindex"` (rebuild secondary indexes). Backends MAY add their own kinds; the reserved names MUST mean what the spec says if advertised. A backend that has no analogue for a reserved kind MUST omit it from `maintenance_kinds` rather than declaring it as a no-op — otherwise a benchmark harness sees `"analyze"` advertised and assumes it did what the spec says when the implementation did nothing.

`run_maintenance(kind)` MUST raise `UnsupportedMaintenanceKindError` when called with a kind not in `maintenance_kinds`. Advertising a kind without implementing it is a conformance failure.

**`run_maintenance` is observable, not fire-and-forget.** This resolves the §12 open question, and it is driven by a production failure: on a lazy-index path, multiple daemon writers crossing the index-build threshold in the same window each issued the build, stacked an `ACCESS EXCLUSIVE` lock, and blocked writes for the whole build (fixed in-backend with a session-level advisory lock). A pure fire-and-forget call reproduces that race — if the call cannot report "already running," concurrent callers re-trigger the build. Therefore:

- `run_maintenance(kind)` MUST be safe to call concurrently. A backend MUST serialize same-kind maintenance internally (advisory lock, build flag, or equivalent) so a second caller does not start a duplicate operation.
- It returns a structured `MaintenanceResult` (e.g. `{"kind": str, "status": "ran" | "already_running" | "noop", "stats": {...}}`) rather than `None`. `stats` is free-form per kind (rows analyzed, bytes reclaimed, fragments merged). `already_running` is how a concurrent caller learns it must not re-trigger.

This makes the maintenance hook the operator-safe path the lazy-index concurrency wedge requires, and gives the benchmark harness (above) machine-readable phase data.

The benchmark harness under [benchmarks/](../../benchmarks/) records `maintenance_state()` alongside every latency/recall measurement it publishes. Published numbers MUST include three phases: immediately after bulk load, after the backend's native background maintenance has caught up, and after `run_maintenance(kind)` has been called for each kind in `maintenance_kinds`. Harnesses rely on this advertisement to decide what to call — they MUST NOT assume kind names. This prevents comparing an un-`ANALYZE`d Postgres to a settled Chroma and calling the former slow.

### 7.4 ID stability for non-string-ID backends

Backends requiring UUID IDs (Qdrant) use a canonical namespace:

```python
NAMESPACE_MEMPALACE = uuid.UUID("c06c3fc7-5c14-4dc4-84c2-24a5f72d8dc1")
backend_id = uuid.uuid5(NAMESPACE_MEMPALACE, original_id)
```

The namespace UUID is fixed at spec v1 adoption and recorded here — once, for all time. This value is the one the in-tree `qdrant` backend already shipped with in #1679, so existing Qdrant palaces' point IDs already derive from it; promoting the shipped constant to canonical (rather than minting a fresh one) avoids re-deriving any deployed palace's IDs. New UUID-ID backends MUST use this exact namespace. This resolves the #700 vs #381 divergence.

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

A backend that persists the exact float32 vector in its own durable store (rather than re-embedding from text on read) satisfies **both** sides of the lossless pairing: it qualifies for `supports_migration_export` as a source (it can hand back the verbatim vector it stored) and for `supports_embeddings_passthrough` as a target (it persists provided vectors as-is). Such "rank-from-stored-vectors" backends are first-class migration endpoints in both directions — the pairing in this section is about the two *capabilities* being present, not about any particular index structure. An exact-vector store therefore makes `chroma → <store>` and `<store> → chroma` lossless under §8 in both directions, given model-identity agreement.

### 8.3 Safety

- Source is never modified. Migration is read-only against the source backend.
- Target palace must not already exist unless `--overwrite` is passed.
- A successful migration writes a `.mempalace-migration.json` record into the target palace containing: source backend name, source path/ref, timestamp, row count, `lossless: true|false`, source and target `embedder.model_name`, and whether `--force-model-swap` or `--accept-re-embed` was used.

### 8.4 Verification

After migration, run `mempalace verify --palace PATH --against SOURCE_PATH --source-backend chroma`. This samples N rows and confirms round-trip parity (ids match, documents match, embedding cosine similarity ≥ 0.999 when the migration was lossless; a looser document-overlap check when re-embedded).

---

## 9. Versioning and compatibility

- `BaseBackend.spec_version` declares which spec version a backend implements.
- MemPalace refuses to load a backend declaring a different major version. The failure is loud and names the mismatch, e.g.:
  ```python
  raise BackendVersionMismatchError(
      f"backend {name!r} targets spec {backend.spec_version!r}; "
      f"this MemPalace implements major version {CORE_MAJOR!r}. "
      f"Install a build of {name!r} that targets spec {CORE_MAJOR}.x."
  )
  ```
- Minor versions are additive (new optional methods, new capability tokens). Backends declaring an older minor continue to work.
- This is spec v1.0.

---

## 10. Cleanup prerequisite (mostly landed via #1679)

**Status update (2026-06-07).** The bulk of this cleanup landed in [#1679](https://github.com/MemPalace/mempalace/pull/1679): the direct `chromadb` *client* imports across `repair.py`, `dedup.py`, `cli.py`, `mcp_server.py`, and `migrate.py` were routed through `BaseCollection`, and the dict-to-typed-result migration (§1.3) shipped. What remains is narrow:

- **Residual exception-class imports.** `mcp_server.py` and `repair.py` still `from chromadb.errors import NotFoundError` to catch Chroma's not-found error. These are catch-site couplings, not client construction; they should resolve to a backend-neutral `PalaceNotFoundError` / collection-not-found exception from the contract (§2.4) so non-Chroma palaces raise the same type.
- **`mcp_server._get_client()` caching.** Caches a `PersistentClient` at module scope and invalidates it on `chroma.sqlite3` inode or mtime changes (merged via [#757](https://github.com/MemPalace/mempalace/pull/757)). Both the cache and the stat-based freshness check are Chroma-specific. They should migrate into `ChromaBackend.get_collection()` (§2.5, handle caching) and `ChromaBackend.close_palace()` (§2.6, explicit flush) — other backends do not have a single on-disk SQLite file to stat. The `mempalace_reconnect` MCP tool then becomes a thin wrapper around `backend.close_palace(palace_ref)`.

**`searcher.py` is the highest-leakage module still coupled, and #1679 did not cover it** (flagged by two backend authors — kostadis-ntnx on this RFC, jphein on #1679). Two concrete couplings:

- `_hybrid_rank()` hard-codes `vec_sim = max(0, 1 - distance)`, i.e. it assumes cosine distance ∈ [0, 2]. For a backend whose metric isn't cosine (dot-product, L2) the ranking is silently wrong. Fix: make the conversion metric-aware off the backend-declared `distance_metric` (§2.1) rather than hard-coding cosine. All in-tree backends are cosine today, so this is latent — but it is a contract gap, not a coincidence to keep relying on.
- The BM25 survival fallback opens `chroma.sqlite3` directly and queries Chroma's FTS5 `embedding_fulltext_search` shadow table. On any non-Chroma palace this path is dead. Fix: route lexical fallback through `BaseCollection` (§1.4 `$contains` / a lexical capability), or gate it on a backend-declared capability so backends whose index self-heals never request it.

This `searcher.py` work is tracked as follow-up implementation (see §13) — it is the remaining piece that promotes a non-Chroma backend from "stores and retrieves" to a first-class search peer.

---

## 11. Impact on in-flight PRs

**Update (2026-06-07).** The first wave of in-tree backends — `pgvector`, `qdrant`, `sqlite_exact` — landed via [#1679](https://github.com/MemPalace/mempalace/pull/1679) implementing the §1–2 contract surface directly, ahead of this RFC's merge. That reshapes the table below: the community PRs now rebase against both the spec *and* the shipped in-tree backends, and the prediction that `collection_prefix` would dissolve into `PalaceRef.namespace` (#697) held.

| PR | Status | Effort to align |
|---|---|---|
| [#574](https://github.com/MemPalace/mempalace/pull/574) LanceDB | Open | Closest to final shape. Needs `PalaceRef` and typed results (now both shipped in-tree — rebase against the merged ABC). |
| [#665](https://github.com/MemPalace/mempalace/pull/665) Postgres (`pg_sorted_heap`) | Open | The in-tree `pgvector` backend (#1679) now occupies the basic Postgres slot; #665 rebases as the optional `pg_sorted_heap` performance variant. Decouple embedder; adopt the merged `PalaceRef`. Its session-level advisory-lock fix for the lazy-index concurrency wedge is the production basis for §7.3's observable `run_maintenance`. |
| [#700](https://github.com/MemPalace/mempalace/pull/700) Qdrant | Open | Largely superseded by the in-tree `qdrant` backend (#1679), which already uses the §7.4 canonical namespace. Reconcile or close in favor of the merged backend. |
| [#381](https://github.com/MemPalace/mempalace/pull/381) Qdrant (older) | Open | Same as #700; subclass the merged `BaseCollection` rather than a bare `Protocol`, or close as superseded. |
| [#643](https://github.com/MemPalace/mempalace/pull/643) PalaceStore | Closed | POC; the parametrized-test approach it explored became the standard (§7.2). |
| [#697](https://github.com/MemPalace/mempalace/pull/697) Chroma HttpClient + prefix | Closed | `collection_prefix` dissolved into `PalaceRef.namespace` + `supports_namespace_isolation` (§4.4), as predicted. |

---

## 12. Resolved decisions

The three questions that were open at draft are resolved for v1, informed by the backend-author reviews:

- **`changes_since` accepts a collection filter — yes.** It takes an optional `collection_name` so a sync caller can request changes for one collection (LanceDB and others already track changes per table; filtering by collection is natural). Additive on the §6 optional hook.
- **Per-palace capabilities — no; capabilities stay static/class-level.** Making `supports_*` palace-dependent turns every capability check into "maybe — depends," pushing state tracking onto every caller. A backend that builds indexes lazily either guarantees the index exists when the capability is needed (and advertises it) or doesn't advertise it. The one isolation-by-deployment case (`supports_contains_fast` where an FTS index may or may not exist) is handled by the backend either guaranteeing the floor or omitting the token — not by per-palace variance. (Note: `supports_namespace_isolation` is still a static, class-level token — it declares what the backend *enforces*, not a per-palace fact.)
- **`run_maintenance(kind)` returns a structured, observable result — resolved in §7.3.** It is not fire-and-forget: it returns `MaintenanceResult` and must serialize concurrent same-kind calls (the production lazy-index concurrency wedge is why). See §7.3.

No remaining blockers. Genuinely additive future items (new optional methods, new tokens) land under the minor-version rule (§9).

---

## 13. Rollout

The original draft sequenced cleanup → spec → ChromaBackend → in-flight rebase → migrate CLI. In practice the contract surface and the first backends shipped together in [#1679](https://github.com/MemPalace/mempalace/pull/1679) ahead of this merge, so the remaining sequence is what's left, not the whole list.

**Done (via #1679):**
1. ✅ §10 seam cleanup (client imports routed through `BaseCollection`; typed-result migration §1.3).
2. ✅ `BaseBackend` / `BaseCollection`, `PalaceRef`, capability tokens, entry-point/registry discovery.
3. ✅ In-tree `pgvector`, `qdrant`, `sqlite_exact` backends + `tests/test_backend_conformance.py`.
4. ✅ Canonical §7.4 namespace pinned to the shipped value.

**Remaining (tracked as follow-up issues):**
5. **Embedder-identity contract (§1.5 / §5)** — [#1724](https://github.com/MemPalace/mempalace/issues/1724). Land the minimal `Embedder` protocol as a real injected dependency; persist + check `model_name` per the three-state model. *Hard dependency for §1.5 conformance.*
6. **Maintenance hooks (§7.3)** — [#1725](https://github.com/MemPalace/mempalace/issues/1725). Implement `maintenance_kinds` / `maintenance_state()` / observable `run_maintenance()` in the in-tree backends; wire the §7.3 advisory-lock serialization (`pg_sorted_heap` / #665 has the reference fix).
7. **`searcher.py` metric-awareness + lexical fallback (§10)** — [#1726](https://github.com/MemPalace/mempalace/issues/1726). Make `_hybrid_rank` read `distance_metric`; route the BM25 fallback through `BaseCollection` instead of `chroma.sqlite3`.
8. Residual §10 items: backend-neutral not-found exception; migrate `mcp_server._get_client()` caching into `ChromaBackend`.
9. Finish the `mempalace migrate` CLI (§8) and the parametrized core suite gate (§7.2, `MEMPALACE_TEST_ALL_BACKENDS=1` in CI).
10. Rebase / reconcile the remaining community backend PRs (§11) against the merged contract.
11. Update [ROADMAP.md](../../ROADMAP.md) with spec v1.0 adoption under v4.0.0-alpha.
