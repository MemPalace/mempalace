# Design: `mine --remote-url`

**Date:** 2026-06-08
**Status:** Approved
**Branch:** feat/http-transport

## Problem

`mempalace mine` writes drawers directly to a local ChromaDB palace. When the canonical palace lives in k8s (served over HTTP), the workstation cannot write to it — the palace PVC is RWO and held by the running server pod.

## Goal

Allow `mine` to send drawers to a remote mempalace HTTP server instead of writing to local ChromaDB. This makes the k8s instance the single source of truth with no Syncthing, no CronJob, and no PVC sharing.

## Non-Goals

- Streaming or batched bulk-ingest endpoint (drawers are sent one at a time via existing `mempalace_add_drawer`)
- Remote `search`, `query`, or `status` via the mine path
- Any k8s or Syncthing changes

## CLI

```bash
mempalace mine ~/.claude/projects --mode convos \
  --remote-url https://mempalace.example.com \
  --remote-token $MEMPALACE_TOKEN

# Token can also come from env var (fallback if --remote-token not passed)
export MEMPALACE_TOKEN=secret
mempalace mine ~/.claude/projects --mode convos \
  --remote-url https://mempalace.example.com
```

`--palace` is silently ignored when `--remote-url` is set (a warning is printed).

## Architecture

```
cli.py (cmd_mine)
  │
  ├─ remote_url set?
  │     yes → get_remote_collection(url, token)  → RemoteCollection
  │     no  → get_collection(palace_path)         → ChromaCollection (unchanged)
  │
  └─ pass collection to mine() / mine_convos()
           │
           └─ add_drawer(collection, ...) → collection.upsert()
                                                │
                                    RemoteCollection.upsert()
                                                │
                                    POST /mcp  mempalace_add_drawer
                                                │
                                    k8s mempalace server
```

Existing `mine()` and `mine_convos()` are unchanged except for one new parameter each (`remote_url`/`remote_token`) that controls which collection factory is used.

## Files Changed

| File | Change |
|------|--------|
| `mempalace/backends/remote.py` | **New.** `RemoteCollection` class. |
| `mempalace/backends/__init__.py` | Export `RemoteCollection`. |
| `mempalace/palace.py` | Add `get_remote_collection(url, token)` factory. |
| `mempalace/convo_miner.py` | Accept `remote_url`, `remote_token`; swap collection factory. |
| `mempalace/miner.py` | Same. |
| `mempalace/cli.py` | Add `--remote-url`, `--remote-token` to `mine` subparser. |

## `RemoteCollection` (`backends/remote.py`)

Implements `BaseCollection`. Stateless except for the local skip-tracking file.

### Method behaviour

| Method | Behaviour |
|--------|-----------|
| `upsert(documents, ids, metadatas)` | POST `mempalace_add_drawer` per document. Logs `already_exists` at DEBUG. Raises `RuntimeError` on HTTP error. |
| `get(where, ...)` | Reads local state file. Returns `{"ids": [path]}` if `source_file` in state, else `{"ids": []}`. Only the `where={"source_file": ...}` pattern is supported (what `file_already_mined` uses). |
| `delete(where, ...)` | No-op. Server-side idempotency handles re-mines via content-hash drawer IDs. |
| `add(...)` | Delegates to `upsert`. |
| `query(...)` | Raises `NotImplementedError`. |
| `count()` | Raises `NotImplementedError`. |

### HTTP call format

```
POST {url}/mcp
Authorization: Bearer {token}
Content-Type: application/json

{
  "jsonrpc": "2.0",
  "method": "tools/call",
  "id": 1,
  "params": {
    "name": "mempalace_add_drawer",
    "arguments": {
      "wing": "projects",
      "room": "general",
      "content": "...",
      "source_file": "/path/to/file.jsonl",
      "added_by": "mine"
    }
  }
}
```

The server's `tool_add_drawer` deduplicates by content hash. Sending a drawer that already exists returns `{"success": true, "reason": "already_exists"}` — not an error.

### Local state file (skip tracking)

Path: `~/.mempalace/remote_state/{sha256_of_url[:16]}.json`

```json
{
  "url": "https://mempalace.example.com",
  "source_files": [
    "/home/gavin/.claude/projects/foo/bar.jsonl"
  ]
}
```

- Loaded at mine start. Written atomically after each file completes.
- `file_already_mined` for remote: checks `source_file in state["source_files"]`.
- `_register_file` for remote (0-chunk sentinel): adds path to state without sending any drawers.
- If the state file is missing or corrupt, it is recreated empty (re-mines all files; server deduplicates).

## Error Handling

- **401 Unauthorized:** Raise immediately with a clear message ("check --remote-token or MEMPALACE_TOKEN").
- **Connection error / timeout:** Retry once after 2 s, then raise. Do not mark the file as done in state.
- **Server 500:** Log the error and skip the file (do not add to state). Mine continues with remaining files.
- **`already_exists` response:** Treat as success. Add to state. Log at DEBUG.

## Token Resolution Order

1. `--remote-token` CLI flag
2. `MEMPALACE_TOKEN` environment variable
3. Error: "remote-url requires a token"

## Testing

- Unit tests in `tests/test_remote_collection.py` using `responses` or `httpx` mock.
- Test: successful upsert → HTTP call made, file added to state.
- Test: file already in state → no HTTP call made.
- Test: server returns `already_exists` → treated as success.
- Test: 401 → raises with clear message.
- Test: connection error → retries once, then raises.
- Test: `--remote-url` + `--palace` together → warning printed, palace ignored.

## Out of Scope (Future)

- Bulk/batched ingest endpoint (send N drawers in one HTTP call)
- Remote `search` fallback
- Progress bar / ETA for large backlogs
