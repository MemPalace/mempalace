# Spec: `mempalace/service.py` — Daemon job execution surface

## Purpose

This module is the transport-neutral execution surface used by daemon-backed entry points. It classifies known MCP tools and executes durable background jobs without printing directly to the caller's terminal (instead capturing output for later replay) (mempalace/service.py:L1-L7).

## Environment variables (observable contract)

Three environment variables form the per-job mutable backend/palace state and are snapshotted and restored around every job (mempalace/service.py:L19-L26):

- `MEMPALACE_PALACE_PATH` — absolute palace directory path injected before running a job (mempalace/service.py:L20).
- `MEMPALACE_BACKEND` — selected storage backend name (mempalace/service.py:L21).
- `MEMPALACE_BACKEND_EXPLICIT` — flag/name indicating the backend was explicitly chosen (mempalace/service.py:L19).

These three vars are the per-job isolation set: a job that switches backends (e.g. qdrant) must not poison later jobs in the same long-lived daemon process (mempalace/service.py:L22-L26).

## Tool classification

### Tool sets

Three disjoint named sets of MCP tool names exist:

- READ tools: `mempalace_status`, `mempalace_list_wings`, `mempalace_list_rooms`, `mempalace_get_taxonomy`, `mempalace_get_aaak_spec`, `mempalace_traverse`, `mempalace_find_tunnels`, `mempalace_graph_stats`, `mempalace_list_tunnels`, `mempalace_list_hallways`, `mempalace_follow_tunnels`, `mempalace_search`, `mempalace_check_duplicate`, `mempalace_get_drawer`, `mempalace_list_drawers`, `mempalace_diary_read`, `mempalace_memories_filed_away`, `mempalace_kg_query`, `mempalace_kg_stats`, `mempalace_kg_timeline` (mempalace/service.py:L29-L52).
- WRITE tools: `mempalace_add_drawer`, `mempalace_delete_drawer`, `mempalace_update_drawer`, `mempalace_diary_write`, `mempalace_kg_add`, `mempalace_kg_invalidate`, `mempalace_create_tunnel`, `mempalace_delete_tunnel`, `mempalace_delete_hallway`, `mempalace_hook_settings` (mempalace/service.py:L54-L67).
- MAINTENANCE tools: `mempalace_mine`, `mempalace_sync`, `mempalace_reconnect` (mempalace/service.py:L69-L69).

### `classify_tool(name) -> str`

Input: a tool name string. Output: one of `"read"`, `"write"`, `"maintenance"`, or `"unknown"`, by membership in the respective set; a name in none of the sets returns `"unknown"` (mempalace/service.py:L72-L80).

## Job execution

### `execute_job(kind, payload) -> dict`

Executes exactly one daemon job and returns a JSON-serializable result dictionary (mempalace/service.py:L102-L103).

Dispatch by `kind`: `"mine"` → run_mine, `"sync"` → run_sync, `"diary_write"` → run_diary_write, `"mcp_tool"` → run_mcp_tool. Any other kind returns `{"success": False, "error": "unknown daemon job kind: <kind>", "exit_code": 2}` (mempalace/service.py:L105-L114).

Per-job environment isolation: before running, the current values of the three per-job env vars are snapshotted; after the job completes (success or failure) each is restored — removed if it was previously unset, otherwise reset to its prior value (mempalace/service.py:L116-L127).

Output capture: all stdout and stderr produced during the job are captured rather than printed to the terminal (mempalace/service.py:L94-L99, L121).

Result normalization (mempalace/service.py:L128-L138):
- A `None` job result becomes an empty dictionary.
- A non-dict result is wrapped as `{"success": True, "value": <result>}`.
- `success` defaults to `True` if absent.
- `exit_code` defaults to `0` when `success` is truthy, else `1`.
- If captured stdout is non-empty, it is attached under the `stdout` key; likewise non-empty stderr under `stderr`.

### `run_mine(payload) -> dict`

Resolves the palace path from `payload["palace_path"]` (falling back to the configured default), expands `~`, and makes it absolute; sets `MEMPALACE_PALACE_PATH` to it (mempalace/service.py:L141-L146). Applies the backend from `payload["backend"]` if present (mempalace/service.py:L147).

Payload fields and defaults (mempalace/service.py:L149-L154):
- `source` (or `dir`) — directory to mine.
- `mode` — default `"projects"`.
- `wing` — optional wing override.
- `agent` — default `"mempalace"`.
- `limit` — integer, default `0`.
- `dry_run` — boolean, default false.

If `payload["redetect_origin"]` is truthy, a "pass zero" origin re-detection runs against the source directory and palace, with no LLM provider (mempalace/service.py:L156-L159).

Mode dispatch (mempalace/service.py:L163-L203):
- `"convos"` — mines conversation transcripts; uses `extract` (default `"exchange"`) as the extract mode (mempalace/service.py:L164-L175).
- `"extract"` — mines structured format files (mempalace/service.py:L176-L186).
- `"projects"` — mines project files; respects gitignore unless `payload["no_gitignore"]` is truthy, honors `include_ignored` (default empty list) and `max_chunks_per_file` (mempalace/service.py:L187-L201).
- Any other mode returns `{"success": False, "error": "invalid mine mode: <mode>", "exit_code": 2}` (mempalace/service.py:L202-L203).

Error mapping (mempalace/service.py:L204-L227):
- A "mine already running" lock condition → `{"success": False, "error": <msg>, "error_class": "LockHeldByOtherProcess", "exit_code": 1}`.
- A mine validation error → `{"success": False, "error": <msg>, "error_class": "MineValidationError", "exit_code": 1}`.
- A process-exit signal → `success` is `True` only when its code is `0`; the code is used as `exit_code` (defaulting to `1` if non-integer), `error_class` is `"SystemExit"`.
- Any other exception → `{"success": False, "error": "mine failed: <exc>", "exit_code": 1}`.

On success: `{"success": True, "kind": "mine", "mode": <mode>, "dry_run": <bool>, "exit_code": 0}` (mempalace/service.py:L229-L229).

### `run_sync(payload) -> dict`

Resolves and injects `MEMPALACE_PALACE_PATH` (expanded, absolute) and applies the backend, identically to run_mine (mempalace/service.py:L234-L238).

If the palace path is not an existing directory: prints `"  No palace found at <path>"` and returns `{"success": True, "exit_code": 0}` (mempalace/service.py:L243-L245).

Resolving the backend name for the palace: on failure returns `{"success": False, "error": "Could not resolve palace backend: <exc>", "exit_code": 1}` (mempalace/service.py:L247-L254).

If the palace directory exists but has no backend artifact yet: prints a notice naming the missing artifact and instructs `"  Run: mempalace mine <dir>"`, then returns `{"success": True, "exit_code": 0}` (mempalace/service.py:L256-L262).

Project directory selection (mempalace/service.py:L264-L268): starts with `payload["dir"]` (expanded) if present, then appends each entry of `payload["root"]` (expanded); empty list becomes "no restriction" (None).

`dry_run` defaults to `True` (mempalace/service.py:L269-L269).

Operator-facing banner printed before sync: a 55-character `=` rule, the title `"  MemPalace Sync — Gitignore-aware drawer prune"`, the palace path, optionally the wing, each project directory, and a mode line of `"  Mode:     DRY RUN (no deletions)"` or `"  Mode:     APPLY (deleting drawers)"`, then a 55-character `-` rule (mempalace/service.py:L271-L283).

Sync error mapping (mempalace/service.py:L285-L306):
- Lock-held condition → `{"success": False, "error": <msg>, "error_class": "LockHeldByOtherProcess", "exit_code": 1}`.
- A value/validation error → `{"success": False, "error": <msg>, "exit_code": 2}`.
- Any other exception → `{"success": False, "error": "sync failed: <exc>", "exit_code": 1}`.

Report summary printed (mempalace/service.py:L308-L332): counts for Scanned, Kept, Gitignored, Missing, No source, Out of scope. Gitignored/Missing carry suffix `(would remove)` in dry-run or `(removed)` otherwise. If `by_source` is non-empty, prints up to the top 5 sources sorted by descending count, labeled `"Top sources to remove"` (dry-run) or `"Top sources removed"`. In dry-run, if gitignored+missing > 0, advises re-running with `--apply`. In apply mode, prints the count of removed drawers and closets. Ends with a 55-character `=` rule.

Success return: `{"success": True, "report": <report>, "exit_code": 0}` (mempalace/service.py:L333-L333).

### `run_diary_write(payload) -> dict`

If `palace_path` is present, sets `MEMPALACE_PALACE_PATH` to its expanded absolute form (mempalace/service.py:L337-L339). Applies backend (mempalace/service.py:L340).

Invokes the diary-write MCP tool with `agent_name` (default `"mempalace"`), `entry` (default empty string), `topic` (default `"general"`), and `wing` (default empty string) (mempalace/service.py:L342-L349). The returned dict gets `exit_code` defaulted to `0` when `success` truthy else `1` (mempalace/service.py:L350-L351).

### `run_mcp_tool(payload) -> dict`

Executes a single MCP tool by name over the daemon queue. Only WRITE-classified tools are permitted; read tools are rejected (to avoid exfiltrating verbatim palace content into the queue/result), and maintenance tools have dedicated kinds (mempalace/service.py:L354-L364).

Validation order and errors (mempalace/service.py:L365-L379):
- `arguments` defaults to an empty object; if present but not an object/dict → `{"success": False, "error": "arguments must be an object", "exit_code": 2}`.
- The tool name is classified; if the classification is not `"write"` (including unknown/read/maintenance, or missing name) → `{"success": False, "error": "daemon mcp_tool only accepts write tools; <name> is <classification>", "exit_code": 2}`.
- If the name is not a registered tool → `{"success": False, "error": "unknown MCP tool: <name>", "exit_code": 2}`.

The tool handler is invoked with the arguments as keyword fields (mempalace/service.py:L380-L380). Result handling (mempalace/service.py:L381-L390):
- If the handler returns a dict and it lacks a `success` key, `success` is inferred as `True` only when the dict has no `"error"` key (so a bare `{"error": ...}` failure is recorded as failed). `exit_code` defaults to `0`/`1` from `success`.
- If the handler returns a non-dict, it is wrapped as `{"success": True, "value": <result>, "exit_code": 0}`.

### `_apply_backend(backend)` (internal contract)

If `backend` is falsy, does nothing (mempalace/service.py:L83-L85). Otherwise lowercases and trims the name, validates it resolves to a known backend class, and sets both `MEMPALACE_BACKEND_EXPLICIT` and `MEMPALACE_BACKEND` to the normalized name (mempalace/service.py:L86-L91).

## Output replay

### `print_job_result(result) -> int`

Replays a captured job result and returns the intended process exit code (mempalace/service.py:L393-L394).

Behavior (mempalace/service.py:L395-L403):
- Captured `stdout` (if present) is written to standard output with no added trailing newline.
- Captured `stderr` (if present) is written to standard error with no added trailing newline.
- If the job did not succeed and has an `error` message but produced no captured stderr, prints `"mempalace: <error>"` to standard error.
- Returns `exit_code` from the result; if absent, returns `0` when `success` is truthy (default truthy), else `1`. A falsy/zero exit code yields `0`.

## Invariants

- Every per-job env mutation is reverted after the job regardless of outcome, preventing cross-job leakage in a long-lived process (mempalace/service.py:L116-L127).
- No daemon job prints directly to the caller's terminal during execution; all output is captured and only replayed via `print_job_result` (mempalace/service.py:L94-L99, L393-L403).
- The `mcp_tool` kind is strictly limited to write tools as a security boundary against exfiltrating verbatim content into the queue DB / job result (mempalace/service.py:L355-L375).
- Exit-code convention across all jobs: `0` success, `1` general/runtime failure, `2` validation/bad-input failure (mempalace/service.py:L114, L203, L304, L368, L375, L379).
