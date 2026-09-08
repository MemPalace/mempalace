# Write routing policy

This document defines the shared policy used by the staged Tier 3 daemon
rollout tracked in #1963.

This foundation PR does not change existing hook or CLI routing. It provides
one tested policy model that later hook and CLI PRs can consume without
inventing different fallback rules.

## Policies

`direct`

    Always execute through the existing direct local path.

`prefer`

    Use an available daemon. A caller that is allowed to start the daemon may
    do so. Otherwise, fall back to the direct path.

`require`

    Use an available daemon. A caller that is allowed to start the daemon may
    do so. If neither is possible, block the operation. Never fall back to a
    direct ChromaDB writer.

## Concrete routing outcomes

The shared decision function returns one of:

- `direct`
- `daemon`
- `blocked`

It also reports whether the caller should auto-start the daemon and why the
route was selected.

Hooks generally pass `daemon_can_start=False` because hook execution has a
tight latency budget.

Interactive CLI commands can pass `daemon_can_start=True`.

## Configuration

Global environment policy:

    MEMPALACE_WRITE_ROUTING=direct|prefer|require

Hook-specific environment policy:

    MEMPALACE_HOOK_WRITE_ROUTING=direct|prefer|require

CLI-specific environment policy:

    MEMPALACE_CLI_WRITE_ROUTING=direct|prefer|require

Configuration-file shape:

    {
      "write_routing": {
        "default": "direct",
        "hooks": "prefer",
        "cli": "require"
      }
    }

## Precedence

For hooks:

1. `MEMPALACE_HOOK_WRITE_ROUTING`
2. `MEMPALACE_WRITE_ROUTING`
3. legacy `MEMPALACE_HOOKS_DAEMON`
4. `write_routing.hooks`
5. `write_routing.default`
6. legacy `hooks.daemon`
7. `direct`

For CLI writes:

1. `MEMPALACE_CLI_WRITE_ROUTING`
2. `MEMPALACE_WRITE_ROUTING`
3. `write_routing.cli`
4. `write_routing.default`
5. `direct`

## Backward compatibility

The existing `MEMPALACE_HOOKS_DAEMON` environment variable and
`hooks.daemon` config value remain supported.

Legacy true values map to `prefer`.

Legacy false values map to `direct`.

The existing `MempalaceConfig.hook_use_daemon` property is intentionally
unchanged in this PR. Hook and CLI behavior remains unchanged until their
policy-aware rollout PRs land.

## Invalid policy values

New policy settings accept only:

- `direct`
- `prefer`
- `require`

Invalid values fail with a source-specific error rather than silently falling
back. This is important because silently turning a misspelled `require` into a
direct write would violate the safety purpose of the policy.

## Local backend single-writer safety

File-backed backends such as `chroma`, `sqlite_exact`, and Milvus Lite support
exactly one writable process per palace. Serializing individual calls is not
enough because each long-lived process can retain SQLite/WAL, FTS, or vector
index state between calls.

- A writable daemon owns the palace writer lease for its full lifetime.
- Writable MCP HTTP acquires that lease before binding. It releases the lease
  after a configurable write-idle period and reacquires it on the next
  mutating request.
- MCP stdio opens `sqlite_exact` read-only until it acquires the writer lease.
  It may therefore coexist for reads; mutating tools refuse while another
  process owns the lease. A server that owns the lease releases it after the
  same write-idle period and reacquires it when it next needs to write.
- Read-only MCP HTTP may coexist with the writer.
- Read-only `sqlite_exact` clients use an immutable connection for a clean
  checkpointed database, or `mode=ro` when an active writer's complete WAL
  sidecar pair must remain visible. Both paths enable `query_only` and skip
  schema, WAL, FTS, migration, and metadata initialization.
- Direct CLI and hook writes must not run beside a writable daemon or MCP HTTP
  owner. Route them through the daemon with `require` when the daemon owns the
  palace.
- Direct `sqlite_exact` collection mutations contend for the same palace lease,
  and full LLM closet regeneration owns it before opening collections or
  calling the configured model.

`MEMPALACE_MCP_ALLOW_PEER_WRITER` cannot bypass this protection for local
file-backed or unknown plugin backends. It is retained only for explicitly
remote service backends (`qdrant`, `pgvector`, and Milvus server/Zilliz Cloud)
that coordinate concurrent clients themselves. Milvus Lite remains protected
as local file-backed storage.

Do not delete or unlink a live palace lock to recover ownership. Stop the
owning process cleanly; the operating system releases its lock automatically.
If corruption is suspected, back up the palace and run integrity/repair
operations offline, with no writable service running.

### Idle MCP writer lease release

The stdio and HTTP MCP transports start a writer-idle watchdog. When no
mutating tool has completed for 10 minutes, and no storage-touching request is
in flight, the watchdog closes this process's storage handles and releases its
writer lease. The next mutating request tries to acquire the lease again. It
continues normally if the lease is free, or returns the peer-writer refusal if
another process acquired it during the idle gap.

Configure the threshold with `MEMPALACE_MCP_WRITER_IDLE_MINUTES`. The default
is `10`; `0` disables idle release and restores hold-until-exit behavior. An
unparseable value uses the 10-minute default, and a negative value is treated
as `0`.

This watchdog is independent of `MEMPALACE_MCP_IDLE_HOURS`. Setting the
process idle timeout to `0` keeps an MCP server running, but does not disable
writer-lease release. This separation lets an always-on server remain
available without keeping local peers read-only while it is not writing.

### Prefer one server where you control the deployment

Where every client can use HTTP, prefer one `mempalace serve` process as the
palace's writer. Concurrent client requests then serialize inside one process
instead of moving the writer lease between processes. The systemd template is
[`deploy/mempalace-server.service`](../deploy/mempalace-server.service).

Local stdio clients and compatible CLI operations can discover that server
through the per-palace registry in `~/.mempalace/server/<key>/`. The registry
and its bearer token live under the current operating-system user's home. A
server running under a dedicated service account therefore does not publish a
registry that a human user's local clients can see. Discovery fails quietly in
that topology and the client uses its local path, where the writer lease still
applies. Configure those clients to use the HTTP server explicitly, or run the
server and local forwarders under the same account.

Hook diary checkpoints still write directly on a server machine. `mempalace
mine` forwards a hook-spawned transcript ingest to a live server, but the
session-end checkpoint writes ChromaDB in process through
`_save_diary_direct`, and the hook entry points read no server registry. Every
session end therefore opens the server's palace from a second process. That is
the direct hook write the routing rules above tell you not to run beside a
writable owner. The palace lock does not observe it, because the checkpoint
takes no writer lease. The contention appears at the SQLite layer, where a
checkpoint that wedges mid-write holds the write lock that the server's next
request needs. Until the checkpoint forwards the way the mine already does,
treat a session end on a server machine as a second writer.

For a filesystem-level backup of a palace owned by an always-on service, stop
the service, capture the complete palace, then restart the service and verify
it. Do not make the backup conditional on `pgrep` finding no process under the
service account: an always-on server makes that condition permanently false,
so the backup would be skipped every time. Networked backends should use their
backend-native snapshot procedure.

## Follow-up PRs

Hook-triggered writes now consume this policy; see
`docs/hook-write-routing.md`.

The remaining rollout PR will apply the policy to routine CLI writes.

Maintenance operations such as repair, migration, and index rebuild are not
ordinary routed writes. They require a separate exclusive-maintenance policy.
