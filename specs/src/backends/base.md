# Spec: Storage Backend Contract (`mempalace/backends/base.py`)

Defines the abstract surface every storage backend implements: a per-collection
read/write interface, a per-palace factory, typed result objects, error classes,
and an embedder-identity check (mempalace/backends/base.py:L1-L14). The spec is
RFC 001. Implementable in any language; the observable contract is the set of
operations, their inputs/outputs, the typed result shapes, the error taxonomy,
and the isolation/identity invariants.

## Error taxonomy

A single root error type is the base of every backend error raised by core
(mempalace/backends/base.py:L26-L27). The following derive from it:

- `PalaceNotFoundError` — raised when a collection is requested with
  `create=False` on a missing palace. It MUST also be a subtype of the
  host language's file-not-found error so legacy callers catching that keep
  working (mempalace/backends/base.py:L30-L35).
- `CollectionNotInitializedError` — raised when the palace exists on disk and
  its database is valid but the requested collection was never created (e.g.
  init ran but mine did not). It is a subtype of `PalaceNotFoundError` (and thus
  of file-not-found) (mempalace/backends/base.py:L38-L47).
- `BackendClosedError` — raised when any backend method is called after
  `close()` (mempalace/backends/base.py:L50-L51).
- `UnsupportedFilterError` — raised when a where-clause uses an operator the
  backend does not implement. Silently dropping unknown operators is forbidden
  (mempalace/backends/base.py:L54-L58).
- `UnsupportedCapabilityError` — raised when an optional capability is not
  implemented (mempalace/backends/base.py:L61-L62).
- `UnsupportedMaintenanceKindError` — raised when `run_maintenance(kind)` is
  called with a kind the backend did not advertise
  (mempalace/backends/base.py:L65-L71).
- `BackendMismatchError` — raised when the selected backend does not match
  existing palace artifacts (mempalace/backends/base.py:L74-L75).
- `DimensionMismatchError` — raised when an embedding's dimension on write does
  not match the collection's (mempalace/backends/base.py:L78-L79).
- `EmbedderIdentityMismatchError` — raised when the stored embedder model name
  differs from the current one (mempalace/backends/base.py:L82-L83).
- `EmbedderIdentityUnknownWarning` — a warning (not an error) emitted on the
  first open of a collection that has no recorded embedder identity; the
  identity is recorded on the next write and subsequent opens become strict
  (mempalace/backends/base.py:L86-L92).

## Value objects

### PalaceRef

An immutable handle to a palace with three fields: `id` (required string),
`local_path` (optional string, populated for filesystem-rooted palaces), and
`namespace` (optional string, used for tenant/prefix routing in server-mode
backends) (mempalace/backends/base.py:L100-L134).

Isolation contract: `id` is the required isolation key. Within one backend
instance, a record written for one `PalaceRef.id` MUST NOT be returned,
modified, or deleted by an operation issued for a different `id`
(mempalace/backends/base.py:L108-L114). `namespace` is additional partitioning
honored only by backends advertising the `supports_namespace_isolation`
capability; for those, the same cross-isolation guarantee extends to namespaces
(mempalace/backends/base.py:L116-L123). Backends not advertising that capability
MAY ignore `namespace`, and callers MUST NOT rely on it for tenant isolation on
those backends (mempalace/backends/base.py:L125-L129).

### EmbedderIdentity

Immutable pair: `model_name` (string, the stable persisted identity checked on
opens) and `dimension` (integer vector width, default `0`). A `dimension` of `0`
means unknown/not-probed and is treated as "no dimension signal" rather than a
real zero-width vector (mempalace/backends/base.py:L137-L149).

### MaintenanceResult

Immutable record with `kind` (string), `status` (string), and `stats` (free-form
map, default empty) (mempalace/backends/base.py:L152-L172). `status` is exactly
one of: `"ran"` (this call performed the maintenance), `"already_running"`
(another caller holds the work, this call did nothing and the caller MUST NOT
re-trigger), or `"noop"` (nothing needed doing)
(mempalace/backends/base.py:L158-L168).

### HealthStatus

Immutable record with `ok` (boolean) and `detail` (string, default empty)
(mempalace/backends/base.py:L245-L248). Two named constructors: `healthy(detail)`
produces `ok=True`, and `unhealthy(detail)` produces `ok=False`
(mempalace/backends/base.py:L250-L256).

### LexicalHit / LexicalResult

`LexicalHit` is an immutable record with `id` (string), `document` (string),
`metadata` (map), and `score` (float) (mempalace/backends/base.py:L337-L344).
`LexicalResult` wraps an ordered list of hits in field `hits`
(mempalace/backends/base.py:L347-L351).

## Embedder protocol and identity check

An embedder exposes `model_name` (string), `dimension` (integer), and an
`embed(texts)` operation mapping a list of strings to a list of float vectors
(mempalace/backends/base.py:L175-L186).

`check_embedder_identity(stored, current, force_model_swap=False)` returns one of
three state strings and may raise (mempalace/backends/base.py:L189-L212):

- Returns `"unknown"` when `current` is absent or has an empty `model_name`, or
  when `stored` is absent (mempalace/backends/base.py:L213-L216).
- Computes a dimension conflict only when both sides have a nonzero dimension and
  those dimensions differ; a name conflict is any difference in `model_name`
  (mempalace/backends/base.py:L218-L221).
- Returns `"known_match"` when there is neither a dimension nor a name conflict
  (mempalace/backends/base.py:L223-L224).
- When there is a conflict and `force_model_swap` is true, returns
  `"known_mismatch"` without raising (mempalace/backends/base.py:L226-L227).
- When there is a conflict and `force_model_swap` is false, a dimension conflict
  is checked first and raises `DimensionMismatchError` (because mismatched-width
  vectors are physically unusable); otherwise raises
  `EmbedderIdentityMismatchError` (mempalace/backends/base.py:L229-L242). The
  error messages name the stored vs current dimensions/models and instruct the
  user to re-embed or run the set-embedder force command
  (mempalace/backends/base.py:L230-L242).

## Typed query/get results

Result objects also support transitional map-style access: `result["ids"]`,
`result.get("ids", default)`, and membership tests are valid only for the five
field names `ids`, `documents`, `metadatas`, `distances`, `embeddings`; any other
key raises a key error on indexed access or returns the default on `get`
(mempalace/backends/base.py:L259-L283). Attribute access (`result.ids`) is the
canonical interface; the map forms are a deprecated migration shim
(mempalace/backends/base.py:L262-L269). Membership for a field is true only when
that field is present and non-null (mempalace/backends/base.py:L282-L283).

### QueryResult

Immutable result of `query`, with fields `ids`, `documents`, `metadatas`,
`distances` (each a list-of-lists) and `embeddings` (a list-of-lists-of-vectors
or null) (mempalace/backends/base.py:L286-L302). The outer list dimension equals
the number of query vectors/texts; the inner list dimension is the hits per query
(may be zero) (mempalace/backends/base.py:L288-L296). Fields not requested via
`include=` are populated with empty lists of the correct outer shape (never
null), except `embeddings`, which is null when not requested
(mempalace/backends/base.py:L293-L296). The `empty(num_queries=1,
embeddings_requested=False)` constructor builds an all-empty result preserving
the outer dimension: each of the four mandatory fields becomes `num_queries`
empty inner lists; `embeddings` becomes that same shape when requested, else null
(mempalace/backends/base.py:L304-L320).

### GetResult

Immutable result of `get`, with flat (single-level) lists `ids`, `documents`,
`metadatas`, plus `embeddings` (a list of vectors or null)
(mempalace/backends/base.py:L323-L330). The `empty()` constructor returns empty
lists for the three mandatory fields and null embeddings
(mempalace/backends/base.py:L332-L334).

## Collection contract (`BaseCollection`)

Per-collection read/write surface. The following operations are required (must be
implemented by every backend) (mempalace/backends/base.py:L359-L415):

- `add(documents, ids, metadatas=None, embeddings=None)` → no return; inserts
  records (mempalace/backends/base.py:L362-L370).
- `upsert(documents, ids, metadatas=None, embeddings=None)` → no return; inserts
  or replaces by id (mempalace/backends/base.py:L372-L380).
- `query(query_texts=None, query_embeddings=None, n_results=10, where=None,
  where_document=None, include=None)` → `QueryResult`
  (mempalace/backends/base.py:L382-L392).
- `get(ids=None, where=None, where_document=None, limit=None, offset=None,
  include=None)` → `GetResult` (mempalace/backends/base.py:L394-L404).
- `delete(ids=None, where=None)` → no return
  (mempalace/backends/base.py:L406-L412).
- `count()` → integer record count (mempalace/backends/base.py:L414-L415).

All operations take keyword arguments only (mempalace/backends/base.py:L363-L412).

Optional operations have default behaviors a backend MAY override:

- `estimated_count()` defaults to returning `count()`
  (mempalace/backends/base.py:L421-L422).
- `close()` defaults to no-op (mempalace/backends/base.py:L424-L425).
- `health()` defaults to a healthy status (mempalace/backends/base.py:L427-L428).
- `distance_metric` (a property) defaults to `"cosine"`; collections whose actual
  space differs (e.g. a legacy palace not built with cosine) override it so core
  ranking converts correctly (mempalace/backends/base.py:L430-L439).
- `get_stored_embedder_identity()` defaults to null, meaning unknown (legacy or
  non-persisting backend); core treats null as the warn-not-fail unknown state
  (mempalace/backends/base.py:L441-L449).
- `set_embedder_identity(identity)` defaults to no-op; a backend without an
  identity slot stays permanently unknown and never enforces
  (mempalace/backends/base.py:L451-L459).
- `effective_embedder_identity()` defaults to null; server-side-embedder backends
  override it to report the server's embedder
  (mempalace/backends/base.py:L461-L469).
- `maintenance_state()` defaults to an empty map; free-form per backend
  (mempalace/backends/base.py:L471-L479).
- `run_maintenance(kind)` defaults to raising `UnsupportedMaintenanceKindError`
  for every kind; overriders MUST serialize concurrent same-kind runs and report
  `already_running` rather than stacking work
  (mempalace/backends/base.py:L481-L490).
- `lexical_search(query, n_results=10, where=None)` defaults to raising
  `UnsupportedCapabilityError` (mempalace/backends/base.py:L492-L499).

### Default `update`

`update(ids, documents=None, metadatas=None, embeddings=None)` provides a default
non-atomic implementation (get + merge + upsert); backends advertising
`supports_update` MUST override it with an atomic single-round-trip version
(mempalace/backends/base.py:L501-L513). Behavior of the default:

- Raises a value error if all of `documents`, `metadatas`, `embeddings` are null
  (at least one is required) (mempalace/backends/base.py:L514-L515).
- For each of `documents`, `metadatas`, `embeddings` that is provided, its length
  MUST equal the length of `ids`, else a value error naming the field
  (mempalace/backends/base.py:L517-L524).
- Fetches existing documents and metadatas for the given ids
  (mempalace/backends/base.py:L526-L530). For each id: the document becomes the
  supplied one if provided, else the previous document (empty string if the id
  did not exist) (mempalace/backends/base.py:L531-L535). Metadata is a shallow
  merge — start from previous metadata, then overlay supplied metadata keys (an
  update, not a replace); missing ids start from empty metadata
  (mempalace/backends/base.py:L534-L539). The merged documents and metadatas are
  written via `upsert` along with the supplied embeddings
  (mempalace/backends/base.py:L540-L545).

## Backend contract (`BaseBackend`)

A long-lived factory serving many palaces. Construction MUST be lightweight: no
I/O and no network; all connection work is deferred to `get_collection`.
Instances MUST be thread-safe for concurrent `get_collection` calls across
different palaces (mempalace/backends/base.py:L553-L567).

Class-level declarations forming the backend's advertised contract
(mempalace/backends/base.py:L569-L585):

- `name` — the backend's identifier (mempalace/backends/base.py:L569).
- `spec_version` — defaults to `"1.0"` (mempalace/backends/base.py:L570).
- `capabilities` — a set of capability tokens, default empty
  (mempalace/backends/base.py:L571).
- `distance_metric` — the space `query()` reports distances in, one of
  `"cosine"`, `"l2"`, `"ip"`, default `"cosine"`. The contract for the
  `distances` field is lower = closer regardless of metric; core converts
  distance to similarity off this declaration
  (mempalace/backends/base.py:L572-L577).
- `maintenance_kinds` — set of kinds this backend implements, default empty.
  Reserved kind names: `"analyze"` (refresh planner/query statistics),
  `"compact"` (reclaim space, rewrite storage), `"reindex"` (build/rebuild
  secondary indexes). A backend with no analogue for a kind MUST omit it rather
  than declare a no-op; `run_maintenance` raises
  `UnsupportedMaintenanceKindError` for any kind not listed here
  (mempalace/backends/base.py:L578-L585).

Operations:

- `get_collection(palace, collection_name, create=False, options=None)` →
  `BaseCollection`. Required; keyword-only arguments
  (mempalace/backends/base.py:L587-L595).
- `close_palace(palace)` — evicts cached handles for a single palace; default
  no-op (mempalace/backends/base.py:L597-L599).
- `close()` — shuts down the entire backend; default no-op
  (mempalace/backends/base.py:L601-L603).
- `health(palace=None)` — defaults to a healthy status
  (mempalace/backends/base.py:L605-L606).
- `detect(path)` — a class-level detection hint used by selection priority;
  default returns false (mempalace/backends/base.py:L608-L611).

Every backend MUST satisfy the per-`PalaceRef.id` isolation guarantee. Backends
isolating additionally by `namespace` MUST advertise
`supports_namespace_isolation`, which is a promise to satisfy the cross-namespace
guarantee; backends without the token MAY ignore `namespace`
(mempalace/backends/base.py:L560-L566).

## Include-resolution helper

`include=` parameters accept exactly the four keys `documents`, `metadatas`,
`distances`, `embeddings`; any other key is ignored
(mempalace/backends/base.py:L619-L620, L643-L643). The resolver produces four
booleans. When `include` is null, the defaults are `documents=True`,
`metadatas=True`, `distances=default_distances` (a parameter defaulting to true),
and `embeddings=False` (mempalace/backends/base.py:L623-L642). When `include` is
a list, each of the four booleans is true exactly when its key appears among the
recognized keys in the list (mempalace/backends/base.py:L643-L649).

<promise>SPEC_WRITTEN path=specs/src/backends/base.md citations=63</promise>
