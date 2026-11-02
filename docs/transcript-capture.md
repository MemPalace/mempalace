# Opt-in native transcript capture

Capture appends Claude Code session JSONL and Codex rollout JSONL to the active
workspace thread store, using either v1 or compact storage. It needs no model,
handoff, administrator role, or additional service. Capture starts **off** in
each workspace. It does not install hooks or discover files automatically.

A signed-in workspace owner enables it through the ordinary gateway:

```http
POST /api/settings/capture
Content-Type: application/json

{"enabled":true}
```

The same request with `false` stops future capture, including when storage is
full. Existing captured text stays intact. Agent tokens and workspace members
cannot change this setting. Session writes retain the gateway's origin checks.
`GET /api/settings/capture` reports `enabled`, `private`, and `interval_seconds`.

Use a connection belonging to the receiving workspace with client `claude-code`
or `codex` and `memory:write`. Owner opt-in authorizes a narrow, write-only
capture channel, including for the default untrusted inbox connection; it does
not grant general private writes or transcript reads. An authenticated connection
can append only under its own connection ID, source and native session ID.
Rotation of a connection token preserves that identity; replacing a connection
creates a separate capture namespace. Signed-in people can also submit capture
requests, under their own user namespace.

## Local selection and scheduling

Install the package in the environment running the native client. Explicitly set
`COMMONPLACE_URL` to the destination gateway and `COMMONPLACE_TOKEN` to that
client's connection token. HTTPS is required except on loopback. Selecting a
remote gateway deliberately uploads the selected transcripts there. No external
model is involved, and no other server receives the source.

Run one pass over an explicitly selected transcript directory:

```bash
python -m commonplace.capture_client --source claude-code --root TRANSCRIPT_DIRECTORY
python -m commonplace.capture_client --source codex --root ROLLOUT_DIRECTORY
```

Use the complete native transcript roots to include every project and subagent,
or narrower directories to limit what is uploaded. Configure each native client
with its matching connection. The recursive scan includes `.jsonl` files and
skips symlinks. It never reads source files while the workspace opt-in is off.
There is no default directory and no local cursor or credential file.

Use an existing scheduler to repeat the scan. Free accepts one new suffix per
native thread every 15 minutes; an immediate retry of a saved chunk is still a
no-op. Each pass sends at most one bounded suffix per file (normally 512 KiB,
extended to complete the last record, up to another 1 MiB). Large initial
backlogs therefore need repeated passes. Paid, or a self-hosted installation
without plan restrictions, accepts a suffix immediately when the runner fires.
Paid remains subject to the existing server-load throttle.

For updates at native hook events, the existing hook entry point also accepts:

```bash
python -m commonplace.client_hooks --event capture --client claude
python -m commonplace.client_hooks --event capture --client codex
```

This event additionally requires local `COMMONPLACE_CAPTURE=on`. Configure it
on an existing Stop hook, or PostToolUse where supported. Hook JSON must supply
an absolute `transcript_path` or `rollout_path`. The hook starts a detached,
one-pass child; network and transcript reads occur in that child, and hook
errors never interrupt the conversation. It does not change handoff settings.
Periodic scans are still needed to catch sessions without a final hook, late
flushes and retryable failures. Paid “live” means capture at supplied hook
events, not a token-by-token stream or an installed filesystem watcher.
Codex notifications without a native rollout path need the directory scan or
the existing adapter that supplies that path. No hook is installed by these
commands. Remove the local hook or unset the local switch to stop scheduling;
disable the workspace setting to reject uploads immediately.

## Append protocol, privacy and recovery

`GET /api/capture?source=codex&thread_id=NATIVE_ID` returns the caller's accepted
UTF-8 byte `offset`, `prefix_sha256`, `last_sync`, `stored_thread_id`, and plan
interval. A new stream starts at zero with SHA-256 of the empty byte string.

`POST /api/capture` accepts one suffix:

```json
{"source":"codex","thread_id":"session-1","offset":0,"prefix_sha256":"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855","transcript":"{\"type\":\"session_meta\",\"payload\":{\"id\":\"session-1\"}}\n"}
```

The response reports the new cursor and `status: "appended"` or `"duplicate"`.
Requests are limited to 64 MiB of JSON, including escaping. The server validates
the expected prefix and atomically commits the raw segment, readable messages,
search index, counts and storage-budget checks. Retries after a lost response,
even an older saved chunk after later growth, do not duplicate text. There is
no separate cursor write that can get ahead of stored content.

The local runner hashes the already accepted bytes before reading a suffix.
It refuses a changed or shortened prefix, defers files changed during the read,
and holds back partial final records and partial UTF-8 until flushed. A rename
with unchanged native identity resumes the same stream. Truncation or a changed
prefix under the same identity is refused; preserve the original rather than
resetting the cursor. A genuinely new native session is a separate thread.
Claude subagents with `agentId` use a distinct identity under their session.

Every accepted segment has an archive row named `capture.raw`; concatenating
those rows' `raw` values in sequence reproduces the accepted source exactly,
including blank lines, unknown records, JSON spelling and CRLF. Readable rows
retain their source timestamps. Archive timestamps record acceptance time.
Provenance includes the authenticated filing actor and
`capture:<actor-id>:<source>:<native-id>`. Transcript roles remain unverified;
captured text is historical proposal data, never approved rules.

Captured threads use the `private_chats` wing and private file permissions.
The normal thread API and tools still require `threads:read`, the same workspace,
and explicit private read permission and wing scope. Capturing alone grants none
of those reads. Other workspaces cannot access the content or its cursor.
One-time imports and capture use distinct source namespaces; importing the same
file separately is not deduplicated against capture.

Free's normal storage limit includes the thread database, compressed/raw frames,
indexes, WAL and all other workspace files. Capture recomputes its available
budget under the thread writer transaction before and after appending. A quota
refusal rolls back the entire suffix and returns 507. No-op retries and opt-out
remain available when full. This does not replace the existing storage admission
gate for other memory tools. 409 reports opt-out or prefix conflict; 429 means
the next plan interval is due; malformed/incomplete source returns 400. Failures
do not return source text or credentials. The runner continues other files after
a file fails, prints fixed diagnostics, and exits nonzero on failures. Deferred
or unchanged files are successful passes and can be retried by the scheduler.

Unsupported: other providers' native databases, web chat views, non-UTF-8 files,
files without a stable native session identity, symlinked sources, attachments
referenced outside JSONL, and individual records exceeding the runner's bound.
Use the explicit one-time import workflow for supported large snapshots.
Prefix verification rereads saved source bytes, and the current store append
path reads prior messages; this implementation makes no constant-time sync or
unbounded-transcript performance claim. No live owner files or real private
exports are required to test it.
