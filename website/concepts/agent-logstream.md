# Agent Logstream

MemPalace is a memory system first — but agents sharing one palace also need to
*coordinate*: delegate work to each other, wait for replies, and hand off
patches without a human relaying messages between machines. The **logstream**
is that coordination layer (RFC 003).

It is a small append-only event log served by the same MemPalace hub that
serves memory, stored next to the palace as `logstream.sqlite3`. It follows
the same promises as everything else in MemPalace:

- **Local-first** — lives inside your palace directory, reachable over
  loopback, LAN, or tailnet exactly like the rest of the hub. No cloud queue,
  no Redis, no SaaS.
- **Exact payloads** — event bodies and artifacts are stored verbatim,
  byte-for-byte, with a SHA-256 for verification.
- **Append-only** — events are immutable. Corrections and acknowledgements
  are *new* events that reference prior ones.
- **Durable before realtime** — everything is recoverable after a reconnect;
  nothing exists only in a socket.

## Events

An event is a structured coordination message with routing metadata and an
optional verbatim body:

```json
{
  "id": "evt_20260702T032443_02ce0c31acdb",
  "type": "patch.ready",
  "stream": "project/mempalace",
  "room": "patches",
  "from_agent": "windows-codex",
  "to_agent": "mac-codex",
  "correlation_id": "task_123",
  "branch": "feat/my-feature",
  "base_commit": "2668053",
  "status": "ready",
  "artifact_ids": ["art_20260702T032443_e5cb86f7aba8"],
  "body": "Search ranking patch is ready. Tests passed on Windows.",
  "created_at": "2026-07-02T03:24:43Z"
}
```

- **`stream`** is the broad channel — `project/<name>` for per-project work,
  or a shared channel like `shared_agent_brain`.
- **`room`** is the sub-channel: `delegation`, `patches`, `reviews`, `status`.
- **`correlation_id`** ties a request to its replies and acks. Generate one
  per task and carry it through the whole exchange.
- **`to_agent`** targets one agent, or `*` to broadcast. Filters on
  `to_agent` also match broadcasts.
- **`status`** is one of `open`, `claimed`, `ready`, `applied`, `blocked`,
  `failed`, `superseded`.
- **`seq`** (returned on every event) is the append-order cursor. Pass the
  last seen event's id as `since_event_id` to resume exactly where you left
  off.

## Artifacts

An artifact is exact content attached to an event: a unified diff, a
generated file, a test log, a JSON report. Artifacts are stored verbatim with
`sha256` and `size_bytes`, so the receiving agent can verify integrity before
applying anything. v1 artifacts are UTF-8 text, up to 4 MiB.

## The delegation loop

The canonical two-agent exchange:

**Requester (agent A):**

1. `mempalace_event_append` — `type=task.request`, `to_agent=agent-b`,
   `correlation_id=task_123`, body describing the work.
2. `mempalace_event_wait` — `correlation_id=task_123`, `type=patch.ready`,
   `timeout_ms=300000`.
3. `mempalace_artifact_get` — fetch the patch, verify the `sha256`.
4. Apply the patch locally, run tests. Patch application is always an
   explicit local decision — the logstream never applies anything for you.
5. `mempalace_event_ack` — `status=applied` (or `failed` with notes).

**Worker (agent B):**

1. `mempalace_event_wait` — `to_agent=agent-b`, `type=task.request`.
2. Do the work.
3. `mempalace_patch_submit` — stores the artifact and appends the
   `patch.ready` event in one call.

If an agent can't produce a patch, it still replies: `task.reply` or a
`blocked`/`failed` status with verbatim notes. Silence is the only failure
mode the logstream can't help with.

`mempalace_event_wait` is a long-poll: it blocks up to 5 minutes and returns
`{ "timed_out": true, "events": [] }` on timeout rather than erroring. Loop
on it for longer waits, passing `since_event_id` to avoid reprocessing.

## Tools and CLI

Seven MCP tools serve the logstream: `mempalace_event_append`,
`mempalace_event_list`, `mempalace_event_wait`, `mempalace_event_ack`,
`mempalace_artifact_put`, `mempalace_artifact_get`, and
`mempalace_patch_submit` — see the [MCP tools reference](/reference/mcp-tools)
for schemas.

The same operations are available from the shell:

```bash
mempalace logstream append --type task.request --stream project/myapp \
  --room delegation --from-agent mac --to-agent windows \
  --correlation-id task_123 --body "Please fix the flaky test."

mempalace logstream wait --correlation-id task_123 --type patch.ready \
  --timeout-ms 300000 --json

mempalace artifact get art_... | git apply --3way
```

`--json` makes every command scriptable; `wait` exits `2` on timeout so
shell loops can retry.

For push-based consumers (dashboards, live viewers), the hub also serves
the stream over Server-Sent Events at `GET /logstream/stream` — same
filters, same JSON envelope, `since_event_id` resume — see
[Shared Brain](/guide/shared-brain#operating-the-shared-brain).

## Coordination vs. memory

The logstream complements the palace; it does not replace it:

| | Palace (drawers) | Logstream (events) |
|---|---|---|
| Purpose | Long-term recall | Active coordination |
| Access | Semantic search | Structured filters + long-poll |
| Lifetime | Forever | Forever (append-only) |
| Content | Anything worth remembering | Work packets, replies, patches |

Durable outcomes still belong in the palace: when a delegated task concludes,
file the *decision and outcome* as a drawer so it is searchable later. The
event trail records *how* the work moved between agents; the drawer records
*what* was learned.

The logstream is deliberately independent of the vector index — it opens no
Chroma handles, so coordination keeps working even while the palace index is
being mined, repaired, or rebuilt.
