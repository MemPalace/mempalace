# Turso storage backend (optional)

This optional backend stores a palace in the [Turso database engine](https://turso.tech) — a ground-up rewrite of SQLite — embedded in the MemPalace process via the `pyturso` package. Despite the name, it is fully local: one single-file database per palace, no cloud account, no network, no server. What distinguishes it from the other local backends is that search work is handed to the database engine instead of Python: semantic ranking, full-text search, and metadata filtering all run inside SQL statements executed by the engine.

The backend is opt-in. Nothing about the default (Chroma) install changes when the `turso` extra is not installed.

## Requirements and installation

The backend needs Python 3.10 or newer and the optional `pyturso` dependency (version 0.8 or newer; tested against pyturso 0.8.3):

```bash
uv sync --extra turso           # uv-based install
pip install "mempalace[turso]"  # pip-based install
```

The package is imported lazily: the backend registers and detects turso palaces even without `pyturso` installed, and only fails when a turso palace is actually opened. The error names the fix:

```
the turso backend requires the optional pyturso package; install it with:
uv sync --extra turso (or pip install "mempalace[turso]")
```

## Platform support

pyturso 0.8.3 publishes prebuilt wheels only for:

| Platform | Wheels |
| --- | --- |
| Linux x86_64 | manylinux_2_17 |
| Linux aarch64 | manylinux_2_17 |
| macOS x86_64 | 10.12+ |
| macOS arm64 (Apple Silicon) | 11.0+ |

There are no Windows wheels for this version — only a source distribution. The turso backend is therefore tested and supported on Linux and macOS; on Windows it is not usable without building the driver from source. Windows users should stay on the default `chroma` backend or one of the other bundled backends.

## Selecting the backend

Select turso the same way as every other backend:

```bash
mempalace mine ~/projects/myapp --backend turso
MEMPALACE_BACKEND=turso mempalace search "why did we switch to GraphQL"
```

or persist it in `config.json`:

```json
{ "backend": "turso" }
```

`mempalace init` records the palace path and the backend in `config.json`, so a one-time setup keeps later commands flag-free:

```bash
# Fresh palace in a new directory (the palace must be empty of other
# backends' artifacts; see "Migration is out of scope" below):
mempalace init ~/projects/myapp --palace ~/palaces/my-turso-palace --backend turso
mempalace mine  ~/projects/myapp
mempalace search "why did we switch to GraphQL"
```

To point a single command at a specific palace, pass the global `--palace` flag before the subcommand:

```bash
mempalace --palace ~/palaces/my-turso-palace search "why did we switch to GraphQL" --backend turso
```

An environment choice of `MEMPALACE_BACKEND=turso` present at `init` time is recorded in `config.json` too, so the palace does not silently fall back to the default backend on a later run without the variable.

## File layout and local-only operation

A turso palace is a single file:

```
<palace>/turso.db          # all collections, embeddings, FTS index
<palace>/turso.db-wal      # WAL sidecar, while writable connections are open
```

There is no cloud component, no account, and no network traffic at any point: the engine runs embedded in the MemPalace process, on your machine. Read-only opens (for example `get_collection(..., read_only=True)`) never create files or schema and reject every mutation, so inspection commands cannot alter a palace.

## How search runs in the engine

Semantic search executes as one SQL statement: the engine computes `vector_distance_cos()` per row, applies the metadata filters, sorts by distance, and returns the top rows — the palace's embeddings are never loaded into a Python-side matrix. Metadata filters (equality, `$eq`, `$in`, `$and`) are pushed down into SQL through `json_extract()` expression indexes on `source_file`, `filed_at`, `wing`/`room`, and friends. Filters SQL cannot push down (`$or`, `$contains`, ...) fall back to a single scan with Python-side filtering.

Lexical search uses the engine's Tantivy-powered full-text index (`fts_match()` / `fts_score()` over a `USING fts` index the engine maintains on every write). If the FTS index could not be created (see [experimental features](#experimental-engine-features)), lexical search falls back to Python BM25 and a warning is logged.

### Semantic search is exact, and scales linearly

The turso backend uses an **exact** cosine-distance scan — there is no approximate-nearest-neighbor (ANN) index. The engine's DiskANN vector index is not compiled into pyturso builds today, and the exact `ORDER BY vector_distance_cos` query delivers the same recall in a single statement. The tradeoff:

- Recall is always exact — no ANN approximation.
- Query cost grows linearly with the number of rows matching the search scope.

Scoping a search is the supported way to bound that cost: wing, room, and source-file filters are pushed down before the distance scan, so `--wing`/`--room`-scoped searches only scan the drawers in that scope. Unscoped searches across a very large palace scan every drawer in the collection.

## Experimental engine features

The Turso engine gates some functionality behind experimental features, enabled at connect time. MemPalace connects with `index_method,multiprocess_wal` by default:

| Feature | What it enables |
| --- | --- |
| `index_method` | Creating the `USING fts` full-text index (lexical search). Without it, lexical search falls back to Python BM25. |
| `multiprocess_wal` | Multi-process access to one palace — background hooks and the MCP server are separate OS processes writing and reading the same database file. |
| `vacuum` | `VACUUM` support, which the `compact` maintenance kind needs. **Incompatible with `multiprocess_wal`.** |

Override the set with `MEMPALACE_TURSO_EXPERIMENTAL_FEATURES`, a comma-separated list:

```bash
export MEMPALACE_TURSO_EXPERIMENTAL_FEATURES="index_method,vacuum"
```

The tradeoff is deliberate and must be understood before changing this: **enabling `vacuum` means giving up `multiprocess_wal`**. With `index_method,vacuum`, the `compact` maintenance kind becomes available — but concurrent multi-process access is no longer enabled, so the hooks-plus-MCP-server setup that shares one palace between processes is not supported in that mode. The default (`index_method,multiprocess_wal`) is the right choice for a normal MemPalace deployment; enable `vacuum` only for single-process use of a palace you intend to compact.

Which maintenance kinds the backend advertises follows from the enabled features (see below).

## Concurrency and write routing

The Turso engine uses MVCC: each statement reads a stable snapshot, and committed writes from other connections or processes become visible to new statements. A reader never sees a dirty write, and a background hook can write while the MCP server searches.

MemPalace's existing single-writer write routing is unchanged: writes take the process-wide writer lease for the palace (`mine_palace_lock`) after serializing on the connection, exactly like the `sqlite_exact` backend. Read-only opens use a separate connection cache and a mutation guard on every write path.

Embedder identity and dimension checking follow RFC 001 like every other backend: the model name is recorded per collection, the embedding dimension is fixed per collection, and `mempalace palace set-embedder` can record or override the identity.

## Maintenance

The backend supports these maintenance kinds:

| Kind | What it does | Available |
| --- | --- | --- |
| `fts_optimize` | `OPTIMIZE INDEX` — merges the Tantivy FTS segments and flushes tombstones left by DELETE/UPDATE. The documented post-bulk-import step. | Always |
| `analyze` | `ANALYZE` — refreshes planner statistics. | Always |
| `compact` | `VACUUM` — rewrites the database file, reclaiming free pages. | Only with `vacuum` in the experimental features **and** `multiprocess_wal` disabled |

Maintenance is a library API — there is no CLI subcommand for it. Run it through the public collection wrapper:

```python
from mempalace.palace import get_collection

col = get_collection("~/palaces/my-turso-palace", create=False, backend="turso")
col.run_maintenance("fts_optimize")  # after a bulk import
col.run_maintenance("analyze")
```

Calling a kind the backend does not advertise raises `UnsupportedMaintenanceKindError`; with the default experimental features, that is `compact`. `run_maintenance("compact")` reports `pages_before` / `pages_after` / `pages_reclaimed` in its result.

## Errors and troubleshooting

| Problem | What you see | Fix |
| --- | --- | --- |
| `pyturso` not installed | `the turso backend requires the optional pyturso package; install it with: uv sync --extra turso (or pip install "mempalace[turso]")` | Install the extra. |
| Backend mismatch | `Backend mismatch at <path>: palace at '<path>' contains '<other>' backend artifacts, but 'turso' was selected` — plus: `Select the matching backend or use a fresh palace directory.` | Point `--backend`/`MEMPALACE_BACKEND` at the backend that wrote the artifacts, or start a fresh palace directory. |
| Dimension mismatch | `turso collection '...' expects embedding dimension <N>, got <M>` (or `cannot mix embedding dimensions ...` for a mixed write) | Use the embedding model the palace was mined with, or rebuild the palace with the new model. |
| Unknown backend name | CLI state message listing the registered backends | Set `--backend` or `MEMPALACE_BACKEND` to a registered backend. |

Backend detection is exact: turso palaces are identified by the `turso.db` file header on a distinct filename, so turso never claims a `chroma` or `sqlite_exact` palace and vice versa — a mismatch fails clearly instead of corrupting either format.

## Migration is out of scope

There is no migration path to or from the turso backend. Existing Chroma or SQLite palaces stay on their backend; selecting turso against a directory holding another backend's artifacts fails with the backend-mismatch error above. To move content, start a fresh palace directory with turso selected and mine the sources again — memory ingest is incremental and append-only, and re-mining the same sources into a new palace is the supported workflow.
