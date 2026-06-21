# Behavior Spec: Embedder-Identity Persistence & Three-State Enforcement (RFC 001)

This file is a test suite. It defines the observable contract for recording an
embedding model's identity into a palace's storage backend and refusing a
dangerous model swap on open. The motivation: a same-dimension model swap (two
384-dimension models) silently corrupts retrieval on explicit-embedding backends
that have no native model check, so the contract records the model name and
refuses a swap (tests/test_embedder_identity.py:L1-L9). The checks below avoid
loading any real embedding model; only the configured model *name* and explicit
vectors are needed (tests/test_embedder_identity.py:L5-L9).

## Data types

`EmbedderIdentity(model_name, dimension)` is a value pair of a string model name
and an integer vector dimension (tests/test_embedder_identity.py:L32, L42).
Two fields are observable as `.model_name` and `.dimension`
(tests/test_embedder_identity.py:L103, L118, L134).

`PalaceRef(id, local_path)` identifies a palace by id and local filesystem path;
both are constructed from the same path string in these tests
(tests/test_embedder_identity.py:L85, L93, L129, L327).

Error/warning surface, importable from the backend base module
(tests/test_embedder_identity.py:L16-L23):
- `DimensionMismatchError` — vector width differs.
- `EmbedderIdentityMismatchError` — model name differs at equal width.
- `EmbedderIdentityUnknownWarning` — cannot determine stored identity.

## `check_embedder_identity(stored, current, force_model_swap=False)` — three-state helper

Returns one of the string states `"unknown"`, `"known_match"`, `"known_mismatch"`,
or raises (tests/test_embedder_identity.py:L26-L73).

- Returns `"unknown"` when nothing is stored (stored is null)
  (tests/test_embedder_identity.py:L31-L32).
- Returns `"unknown"` when the current identity is nameless (empty model name)
  or null, even when a stored identity exists
  (tests/test_embedder_identity.py:L35-L38).
- Returns `"known_match"` when stored and current identities are equal
  (tests/test_embedder_identity.py:L41-L43).
- Returns `"known_match"` when names match and the current dimension is 0;
  dimension 0 means "not probed" and must not be treated as a real conflict
  (tests/test_embedder_identity.py:L46-L51).
- Raises `EmbedderIdentityMismatchError` when names differ at equal dimension
  (the silent-corruption case) (tests/test_embedder_identity.py:L54-L56).
- Raises `DimensionMismatchError` when dimensions differ — width change is
  physically unusable and is checked BEFORE the name swap, even when the names
  also differ (tests/test_embedder_identity.py:L59-L62).
- With `force_model_swap=True`, a name mismatch at equal dimension does NOT raise
  and instead returns `"known_mismatch"`
  (tests/test_embedder_identity.py:L65-L73).

## Per-backend identity persistence (collection API)

Every backend collection exposes `get_stored_embedder_identity()` (returns an
`EmbedderIdentity` or null) and `set_embedder_identity(identity)`
(tests/test_embedder_identity.py:L99-L103, L156-L161). Collections are obtained
via `backend.get_collection(palace=ref, collection_name="mempalace_drawers", create=True)`
(tests/test_embedder_identity.py:L86, L94).

### Common roundtrip contract
A freshly created collection reports no stored identity (null)
(tests/test_embedder_identity.py:L99, L115, L354). After
`set_embedder_identity(EmbedderIdentity(name, dim))`, a subsequent
`get_stored_embedder_identity()` returns the same name and dimension
(tests/test_embedder_identity.py:L101-L103, L116-L118, L355-L357).

### Nameless identity is ignored on set
`set_embedder_identity(EmbedderIdentity("", dim))` records nothing; the stored
identity stays null (tests/test_embedder_identity.py:L106-L110).

### SQLite-exact backend
`SQLiteExactBackend` stores identity alongside the collection; the roundtrip
requires at least one added document with explicit embeddings to exist
(tests/test_embedder_identity.py:L81-L86, L97-L103, L108).

### Chroma backend (sidecar file)
`ChromaBackend` persists identity in a sidecar JSON file named
`mempalace_embedder.json` inside the palace local path; after a set, that file
exists on disk (tests/test_embedder_identity.py:L89-L94, L113-L119).
A malformed sidecar (valid JSON but not a dict, e.g. a JSON array) must NOT raise
on read — it degrades to null/unknown — and a subsequent
`set_embedder_identity` overwrites the junk successfully
(tests/test_embedder_identity.py:L302-L311).

### pgvector backend (sidecar marker)
Identity lives in a sidecar that is separate from the mismatch marker. The
backend exposes `_set_embedder_identity(ref, collection_name, identity)` and
`_get_embedder_identity(ref, collection_name)`. Recording identity needs no
marker — the sidecar is unguarded — and a subsequent `_write_marker(ref, cfg)`
rebuild must NOT wipe the recorded identity
(tests/test_embedder_identity.py:L122-L134). Config is `_PgVectorConfig(dsn=...,
namespace=...)` (tests/test_embedder_identity.py:L128, L375).
`PgVectorCollection.set_embedder_identity` must create the marker/sidecar when
none exists yet (brand-new palace whose first write has not created the marker),
rather than silently no-op into permanent "unknown"
(tests/test_embedder_identity.py:L371-L396).

### qdrant backend (local marker)
Identity is persisted in the local marker file only; the qdrant client is never
touched for identity operations (a placeholder object stands in)
(tests/test_embedder_identity.py:L319-L336). `QdrantBackend` exposes
`_write_marker`, `_set_embedder_identity`, `_get_embedder_identity` with the same
contract: a marker rewrite after recording identity must not wipe embedders
(tests/test_embedder_identity.py:L339-L349). The `QdrantCollection` delegates
`get_stored_embedder_identity` / `set_embedder_identity` to the marker
(tests/test_embedder_identity.py:L352-L357), and a set on a palace whose marker
does not yet exist must create it rather than no-op
(tests/test_embedder_identity.py:L360-L368). Config is `_QdrantConfig(url=...,
api_key=..., namespace=...)` (tests/test_embedder_identity.py:L323, L343).

### EmbeddingCollection wrapper
`EmbeddingCollection` wraps an inner `BaseCollection`. Because `BaseCollection`
defines the identity methods as concrete methods, they are NOT auto-delegated;
the wrapper must explicitly forward them. `set_embedder_identity` on the wrapper
must reach the inner collection, and `get_stored_embedder_identity` on the
wrapper must return the inner collection's stored value — otherwise the wrapper
silently reports the no-op default and masks the wrapped backend
(tests/test_embedder_identity.py:L137-L166).

## Enforcement via `palace.get_collection` / `set_palace_embedder_identity`

The palace module keeps a validation cache `palace._VALIDATED_IDENTITY` that must
be cleared between enforcement checks to force re-validation
(tests/test_embedder_identity.py:L174-L180, L197). Behavior is driven by two
environment variables: `MEMPALACE_BACKEND` (e.g. `sqlite_exact`) and
`MEMPALACE_EMBEDDING_MODEL` (the configured model name)
(tests/test_embedder_identity.py:L192-L193).

`palace.get_collection(path, collection_name=..., create=...)` enforces identity
on open (tests/test_embedder_identity.py:L199, L211):
- When the stored identity matches the configured model, opening does not raise
  (tests/test_embedder_identity.py:L191-L199).
- When the configured model differs from the stored one (same data), opening
  raises `EmbedderIdentityMismatchError`
  (tests/test_embedder_identity.py:L202-L211).
- A brand-new palace opened with `create=True` records the currently configured
  model as its stored identity
  (tests/test_embedder_identity.py:L214-L221).
- A legacy palace with data but no recorded identity emits an
  `EmbedderIdentityUnknownWarning` on open (does not raise)
  (tests/test_embedder_identity.py:L224-L232).
- When the current effective embedder is nameless (model name resolves to empty),
  enforcement is a no-op: no raise and no warning, even with stored data and no
  recorded identity (tests/test_embedder_identity.py:L235-L246). The nameless
  state is produced by `mempalace.embedding.current_model_name` returning empty
  (tests/test_embedder_identity.py:L243).

### `palace.set_palace_embedder_identity(path, model=..., force=...)`
- Recording a model that differs from the stored one without force is refused
  with `EmbedderIdentityMismatchError`
  (tests/test_embedder_identity.py:L254-L262).
- With `force=True` the override succeeds, recording the name only (no foreign
  model load), and returns a `(old, new)` pair whose `.model_name` fields are the
  previous and new model names
  (tests/test_embedder_identity.py:L263-L265).
- When no model is given and none is configured (empty `embedding_model`),
  recording would be a no-op in every backend, so the call refuses with
  `ValueError` rather than claim a phantom success
  (tests/test_embedder_identity.py:L268-L275).

### Effective identity preference
`palace._enforce_embedder_identity(collection, path, collection_name, create=...)`
must prefer a collection's `effective_embedder_identity()` over the configured
model when the collection reports one (e.g. a server-side embedder). When the
effective identity disagrees with the stored identity, enforcement raises
`EmbedderIdentityMismatchError` even though config says a matching name, and the
collection's `set_embedder_identity` must NOT be called on that mismatch
(tests/test_embedder_identity.py:L278-L299).

### qdrant enforcement
`palace._enforce_embedder_identity` reads the qdrant local marker (no live
server) and compares to `MEMPALACE_EMBEDDING_MODEL`; a swap raises
`EmbedderIdentityMismatchError` just like the local backends
(tests/test_embedder_identity.py:L399-L408).
