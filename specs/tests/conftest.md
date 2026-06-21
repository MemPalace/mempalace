# Behavior Spec: tests/conftest.py

Shared test fixtures and load-time environment isolation for the MemPalace test suite. This module establishes a throwaway HOME directory and provides reusable, auto-cleaning fixtures for temporary directories, configuration, storage collections, and knowledge graphs.

## Load-Time Side Effects (HOME Isolation)

At module load time — before any application modules are imported — a session-scoped temporary directory is created with a name prefixed `mempalace_session_` (tests/conftest.py:L19). The original values of the environment variables `HOME`, `USERPROFILE`, `HOMEDRIVE`, and `HOMEPATH` are captured (saving `null`/absent when unset) (tests/conftest.py:L18-L22).

The home-related environment variables are then redirected to point at this temporary directory: `HOME` and `USERPROFILE` are set to the temp dir path; `HOMEDRIVE` is set to the drive component of the temp path (or `"C:"` if there is none); `HOMEPATH` is set to the path component of the temp path (or the full temp path if there is no drive component) (tests/conftest.py:L24-L27). This redirection occurs strictly before application module imports so that any module-level initialization (e.g. a knowledge graph created at import) writes to the throwaway location rather than the real user profile (tests/conftest.py:L7-L11, L29-L34).

## ONNX Model Cache Redirect

After application imports, the storage backend's ONNX embedding model download path is redirected back to the real user's cache, so the HOME redirect does not force a model re-download. The real home is taken from the originally captured `USERPROFILE` (preferred) or `HOME` (tests/conftest.py:L45). If a real home exists, the candidate cache path is `<real_home>/.cache/chroma/onnx_models/all-MiniLM-L6-v2`; if that path exists on disk, the model download path is pointed at it (tests/conftest.py:L46-L49). If the backend embedding module cannot be imported, this step is silently skipped (tests/conftest.py:L39-L51).

## Fixtures

### `_reset_mcp_cache` (autouse, per-test)

An automatically-applied fixture that runs its cleanup both before and after every test (tests/conftest.py:L54-L55, L100-L102). Cleanup resets cached MCP server state only if the MCP server module is already loaded; if it has not been imported, it is left unloaded so subprocess-based tests do not inherit extra storage/database state (tests/conftest.py:L56-L61).

When the MCP server module is loaded, cleanup: closes every cached knowledge-graph instance (calling each one's `close` method if present, swallowing any errors), clears the knowledge-graph-by-path cache, and nulls out the client cache, collection cache, collection-cache backend, collection-cache palace, and collection-open-error fields where they exist (tests/conftest.py:L63-L89). Missing attributes do not cause failure (tests/conftest.py:L88-L89).

Cleanup additionally clears the storage backend's per-process quarantined-paths set so quarantine state does not leak between tests; if the backend cannot be imported or the attribute is absent, this is skipped silently (tests/conftest.py:L91-L98).

### `_isolate_home` (session-scoped, autouse)

A session-wide automatic fixture. The actual HOME redirection happens at module load (above); this fixture's only behavior is teardown after the entire session (tests/conftest.py:L105-L113). On teardown it restores each captured environment variable: if the original value was absent, the variable is removed; otherwise it is restored to its original value (tests/conftest.py:L114-L118). It then recursively deletes the session temp directory, ignoring errors (tests/conftest.py:L119).

### `tmp_dir`

Creates a temporary directory with name prefix `mempalace_test_` and yields its path; on teardown the directory is recursively removed, ignoring errors (tests/conftest.py:L122-L127).

### `palace_path`

Depends on `tmp_dir`. Creates and returns a subdirectory named `palace` inside the temp dir (the directory is created empty) (tests/conftest.py:L130-L135).

### `config`

Depends on `tmp_dir` and `palace_path`. Creates a subdirectory named `config` inside the temp dir, writes a file `config.json` inside it containing a JSON object `{"palace_path": <palace_path>}`, and returns a configuration object constructed against that config directory (tests/conftest.py:L138-L147). The on-disk config file shape is a single JSON object whose `palace_path` key holds the palace directory path (tests/conftest.py:L145-L146).

### `collection`

Depends on `palace_path`. Opens a persistent storage client rooted at the palace path and gets-or-creates a collection named `mempalace_drawers` configured with metadata `{"hnsw:space": "cosine"}` (cosine distance) (tests/conftest.py:L150-L154). Yields the collection; on teardown deletes the `mempalace_drawers` collection and releases the client (tests/conftest.py:L155-L157).

### `seeded_collection`

Depends on `collection`. Adds four representative drawer records, then returns the collection (tests/conftest.py:L160-L215). The records use the IDs `drawer_proj_backend_aaa`, `drawer_proj_backend_bbb`, `drawer_proj_frontend_ccc`, and `drawer_notes_planning_ddd` (tests/conftest.py:L164-L169), each with a verbatim document text (tests/conftest.py:L170-L179).

Each record carries metadata with fields: `wing`, `room`, `source_file`, `chunk_index` (integer 0), `added_by` (`"miner"`), and `filed_at` (ISO-8601 timestamp) (tests/conftest.py:L180-L213). The seeded metadata values are, in order:
- `wing=project room=backend source_file=auth.py filed_at=2026-01-01T00:00:00` (tests/conftest.py:L181-L188)
- `wing=project room=backend source_file=db.py filed_at=2026-01-02T00:00:00` (tests/conftest.py:L189-L196)
- `wing=project room=frontend source_file=App.tsx filed_at=2026-01-03T00:00:00` (tests/conftest.py:L197-L204)
- `wing=notes room=planning source_file=sprint.md filed_at=2026-01-04T00:00:00` (tests/conftest.py:L205-L212)

### `kg`

Depends on `tmp_dir`. Constructs an isolated knowledge graph backed by a SQLite file named `test_kg.sqlite3` inside the temp dir, yields it, and closes it on teardown (tests/conftest.py:L218-L224).

### `seeded_kg`

Depends on `kg`. Pre-loads the graph and returns it (tests/conftest.py:L227-L241). It adds four entities: `Alice` (type `person`), `Max` (type `person`), `swimming` (type `activity`), and `chess` (type `activity`) (tests/conftest.py:L230-L233).

It then adds five temporal triples (subject, predicate, object, with validity dates) (tests/conftest.py:L235-L239):
- `Alice parent_of Max`, valid from `2015-04-01` (open-ended) (tests/conftest.py:L235)
- `Max does swimming`, valid from `2025-01-01` (tests/conftest.py:L236)
- `Max does chess`, valid from `2024-06-01` (tests/conftest.py:L237)
- `Alice works_at Acme Corp`, valid from `2020-01-01` to `2024-12-31` (closed interval) (tests/conftest.py:L238)
- `Alice works_at NewCo`, valid from `2025-01-01` (open-ended, supersedes the prior employer) (tests/conftest.py:L239)

## Invariants and Ordering

- HOME isolation must occur before application module imports; this is enforced by ordering the env-var assignment above the imports in the module body (tests/conftest.py:L17-L34).
- The per-test cache reset runs both before and after each test, guaranteeing a clean cache state on entry and exit (tests/conftest.py:L100-L102).
- All temporary directories created by fixtures are removed on teardown using error-ignoring recursive deletion, so cleanup never fails the test (tests/conftest.py:L119, L127).
- Environment restoration distinguishes "was unset" (remove) from "had a value" (restore), preserving the original environment exactly (tests/conftest.py:L114-L118).
