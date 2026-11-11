## memory-and-search

- **Paths:** `commonplace/memory_tools.py`, `commonplace/search_index_api.py`, `commonplace/store_embed.py`, `commonplace/store_switch.py`, `commonplace/workspace_store.py`, `commonplace/capture.py`, `commonplace/capture_client.py`, `commonplace/agent_log.py`
- **Owns:**

  - Memory tools and routes under the caller's policy, search index progress, workspace store generations and cutovers, and opt-in chat capture.

  - The capture runner classifies hub and transcript failures that a retry can fix (`CaptureUnavailable`: network, 5xx, 408, 409, transcript changed during read) and refused credentials (`CaptureAuth`, 401/403, which pause the spool without giving items up) apart from permanent ones, and `sync_file` accepts an idempotency key sent as `client_op_id`.

  - The generic `agent-log` capture source: any agent appends JSONL records carrying `agent` and `session_id`, and the capture identity is `<agent>:<session_id>`.

- **Promises:**

  - Saved-but-unlinked private notes retain their unreadable-author notice in the error response.

  - Health-flagged notes remain private. HTTP and MCP saves explicitly tell an unreadable author that the note was saved privately and cannot be read for now.

  - Owner memory maps retain readable person relationships when a citation cannot be proved.

  - Owner HTTP progress always forwards the authenticated principal, returns only readable progress and disables response caching.

  - Reads and writes respect the caller's wings and scopes; store cutovers are backup-gated and restartable; capture is explicit and append-only.

  - An agent-log record is stored verbatim in the filing actor's private capture namespace; every record must name the agent and session of its stream, so a stream cannot carry foreign provenance, and native agent names are reserved.

- **How it fails:**

  - Missing, deleted and unreadable citation targets omit memory links without failing the map.

  - Incomplete progress proof returns the fixed 503 error without partial totals or private failure details.

  - An interrupted cutover keeps the previous store live; an index rebuild degrades search speed, not stored text.

- **Tests:**

  - Memory-map regressions compare hidden and absent citation targets and retain readable fact text.

  - `tests/test_card253_s12.py` covers authenticated HTTP isolation and failures; `tests/test_search_index_progress.py` covers the route contract.

  - When only the steady untrusted write limit refuses a capture or import write, a bounded first-backfill allowance is spent instead: per connection once (closing after its window, never renewed) and per workspace (renewing only after its period), never keyed by stream, so restarts and new streams cannot re-arm it. The per-minute budgets still apply and are refunded on refusal; other routes and trusted connections keep their limits. `tests/test_commonplace_public_surface.py::test_first_backfill_of_realistic_size_is_not_throttled_by_the_steady_limit` is red on `origin/develop`.

  - `tests/test_commonplace_memory.py`, `tests/test_commonplace_capture.py`, `tests/test_commonplace_agent_log.py`, `tests/test_workspace_store_switch.py`.
