# Spec: tests/test_backend_conformance.py

Backend isolation conformance suite enforcing the RFC 001 isolation contract against the built-in local storage backends (`tests/test_backend_conformance.py:L1-L6`). It runs the shared isolation assertions exported from the helper module `_backend_conformance` (`tests/test_backend_conformance.py:L10-L10`).

## Scope and parametrization

The suite covers two local backend implementations, each identified by a stable test id: `chroma` for the ChromaDB backend and `sqlite_exact` for the SQLite-exact backend (`tests/test_backend_conformance.py:L12-L19`). Backends are exercised via the type, not instances; each test constructs its own instance (`tests/test_backend_conformance.py:L25-L25`). Server-mode backends (e.g. qdrant) are explicitly out of scope here and run the same shared assertions in their own module (`tests/test_backend_conformance.py:L4-L5`).

## Test: cross-palace isolation

For each local backend, a single backend instance must isolate two distinct palaces keyed by `PalaceRef.id` (`tests/test_backend_conformance.py:L22-L24`).

Setup constructs two palaces labeled `alpha` and `beta`, each backed by a distinct on-disk path under the test's temporary directory; the `PalaceRef` for each uses the path string as both its `id` and its `local_path` (`tests/test_backend_conformance.py:L27-L33`). For each palace a collection named `mempalace_drawers` is obtained with creation enabled (`tests/test_backend_conformance.py:L31-L33`). The two resulting collections are then passed to the shared isolation assertion `assert_partition_isolation(backend, alpha_collection, beta_collection)` (`tests/test_backend_conformance.py:L34-L34`). Regardless of pass/fail, the backend is closed afterward (`tests/test_backend_conformance.py:L35-L36`).

### Isolation contract enforced (via the shared helper)

The helper treats the first collection as `writer` and the second as `other`, and asserts a record written through `writer` is never visible, modifiable, or deletable through `other`, while `writer` continues to hold it (`tests/_backend_conformance.py:L19-L31`). Behavior:

- A backend that declares the `requires_explicit_embeddings` capability is supplied an explicit probe vector; otherwise the query path uses text (`tests/_backend_conformance.py:L32-L33`, `tests/_backend_conformance.py:L46-L49`). The default probe vector is `[1.0, 0.0, 0.0, 0.0]` (`tests/_backend_conformance.py:L16-L16`).
- A baseline count of `other` is captured before the write (`tests/_backend_conformance.py:L35-L35`).
- One probe record is added to `writer` with id `conformance-isolation-probe`, document text `partition isolation probe document`, and metadata `{"wing": "conformance"}`; the embedding field is included only for explicit-embedding backends (`tests/_backend_conformance.py:L14-L15`, `tests/_backend_conformance.py:L37-L44`).
- Querying `other` (by embedding for explicit backends, by text otherwise) for up to 10 results MUST NOT return the probe id (`tests/_backend_conformance.py:L46-L51`).
- A `get` against `other` for the probe id MUST return no ids (`tests/_backend_conformance.py:L53-L55`).
- The count of `other` MUST remain unchanged from the baseline (`tests/_backend_conformance.py:L56-L56`).
- A `delete` of the probe id issued against `other` MUST NOT remove the record from `writer`; a subsequent `get` against `writer` MUST still return the probe id (`tests/_backend_conformance.py:L58-L61`).

## Test: local backends do not claim namespace isolation

Local backends isolate by on-disk path rather than by namespace, so they must not advertise the namespace-isolation capability (`tests/test_backend_conformance.py:L39-L41`). The test asserts that the string `supports_namespace_isolation` is absent from the `capabilities` set of both `ChromaBackend` and `SQLiteExactBackend` (`tests/test_backend_conformance.py:L42-L43`).

## Observable contracts and invariants

- Backend constructor takes no required arguments; `get_collection` accepts `palace`, `collection_name`, and `create` and returns a collection object exposing `add`, `query`, `get`, `count`, and `delete` (`tests/test_backend_conformance.py:L25-L33`, `tests/_backend_conformance.py:L44-L61`).
- `query` returns an object whose `ids` is a list-of-lists (per-query result groups); the first group is read for hits (`tests/_backend_conformance.py:L50-L51`). `get` returns an object whose `ids` is a flat list (`tests/_backend_conformance.py:L53-L60`).
- `capabilities` is a class-level collection of capability strings, including the optional `requires_explicit_embeddings` and `supports_namespace_isolation` flags (`tests/test_backend_conformance.py:L42-L43`, `tests/_backend_conformance.py:L32-L32`).
- Side effect: each test writes backend data under a per-test temporary directory only (`tests/test_backend_conformance.py:L29-L30`).

<promise>SPEC_WRITTEN path=specs/tests/test_backend_conformance.md citations=21</promise>
