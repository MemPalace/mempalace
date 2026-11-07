# Live sync

Live sync uploads selected Claude Code and Codex transcripts
verbatim to your Commonplace gateway. Choose a remote gateway only if you want
those files uploaded there. T3 sessions use their underlying native transcript
roots. Cursor supports structure-only inspection with
`commonplace sync probe FILE`; its native database is not a supported sync source.

## Set up

Enable capture as the workspace owner with
`POST /api/settings/capture` and `{"enabled":true}`. Use a matching Claude Code
or Codex connection with `memory:write`. See [capture](transcript-capture.md).
Normal consented server imports remain available separately through
[one-time import](one-time-import.md).

Create a private environment file (mode `0600`) containing only the credentials
you need, as shell-compatible `NAME='value'` lines:

```text
COMMONPLACE_CAPTURE_TOKEN_CLAUDE_CODE='your Claude Code connection token'
COMMONPLACE_CAPTURE_TOKEN_CODEX='your Codex connection token'
```

Use an absolute environment-file path. Preview the installation first:

```bash
commonplace sync install --source claude-code --source codex \
  --hub https://YOUR_GATEWAY --env-file /ABSOLUTE/PATH/sync.env
```

This writes nothing. Add `--enable` to save configuration and install and start
a user service: systemd on Linux, launchd on macOS. `--no-activate` writes the
service without starting it; `--no-service` saves configuration only.
`--service systemd` or `--service launchd` selects the format explicitly.
Installation never edits client hooks. Keep the Python environment installed
at the same location so the service can restart.

`--source claude-code:ROOT` and `--source codex:ROOT` narrow selection; defaults
are `~/.claude/projects` and `~/.codex/sessions`.
Native scans skip symlinks and wait for complete records.
Legacy `--memory` synchronization is disabled; memory-once is not available yet.
Memory roots are not inspected, even when retained in old configuration.


For a foreground pass, load the environment file into your shell and run
`commonplace sync run --once`. `commonplace sync run` runs continuously;
`commonplace sync status` reports local progress. Keep environment values out
of command arguments and shared logs. Configuration and the durable local
outbox live under `~/.commonplace/sync/`, with private permissions; the outbox
stores references and hashes, not transcript text or credentials.

## Destination consent and upgrading

Choose a destination explicitly with `--hub` or `COMMONPLACE_URL` in your shell
or private environment file. There is no hosted default. A supplied environment
file must be private even when `--hub` is present. Preview needs neither an
environment file nor `--no-service`; it reports counts and source types only.

An enabled transcript-only installation saves `consent` in the existing local
`config.json`: version 1 and the validated exact hub string. Tokens are excluded.
Unmarked configurations, configured memory selections and changed destinations
exit 2 before token loading or state access. Reinstall explicitly to consent to
an endpoint change, including spelling changes; an identical run override works.
This binds only the endpoint: a different workspace token at the same hub is still
possible. Workspace/principal and token-rotation binding remain future work.

All existing stores and SQLite schema remain unchanged. Disabled legacy outbox
rows remain intact, without reads, retries or acknowledgements. Status excludes
them from pending and parked counts, reports `Disabled legacy operations: N`,
and labels native-memory sources `disabled; coverage unverified`, even if a stored
status says ready. Status inspection does not mark backfill complete.

Stop old processes before updating, then explicitly reinstall transcript-only or
uninstall. Updating files alone does not protect a running old process. Existing
systemd and launchd restart policies retry rejected old configs until reinstall
or uninstall; service definitions are unchanged. This safety stop does not prove
memory coverage or activate replacement import, runner, Mac or Grok routes.

## Timing and recovery

Free opens one account-wide upload window every 900 seconds; captures on other
streams can join it for 60 seconds, and each stream still waits 900 seconds
between appends. After a one-time import, live uploads may wait up to 900
seconds. A refused import still consumes that slot. Paid and unrestricted self-hosted instances send changes immediately,
within the server's write budget. Continuous sync checks files every two seconds;
it does not stream individual tokens.

Continuous `sync run` gets one backfill phase per hub and local state, after an explicit transcript-only reinstall for older configurations. It uses the normal 120 attempts per hour first,
then adds at most one backfill attempt every 2 seconds (with an immediate
legacy-format retry if required), up to 5,000 additional attempts in total.
It ends once at least one selected session has a known cursor and every selected
session is caught up or refused, or the cap is reached. Workspace capture being
off does not end backfill. Conflict, paused, and revoked queued rows do not keep
backfill alive; eligible revoked transcript rows are retried once after each daemon restart.
Retained legacy memory operations never keep backfill active or consume its allowance.
Transient failures retain the normal retry backoff.
After catch-up or the cap, only the normal hourly budget applies; unused hourly
slots remain available immediately. Backfill never restarts. `run --once` uses
the normal budget. After the first backfill attempt, `commonplace sync status`
shows `Backfill: active, X of 5000 attempts` or
`Backfill: done, X of 5000 attempts`; it hides this line at zero attempts.
Free's server cadence still applies. Additional backfill exposure is bounded by
about 5,000 × (512 KiB + one record); retries also consume attempts.

The sync event feed contains settings and timing signals, without transcript or
memory content. Paid and unrestricted instances long-poll for up to 25 seconds;
Free polls at its next window. Workspace members can see sync toggles and request
timing. `POST /api/settings/capture` with `{"sync_now":true}` requests a cycle
without bypassing cadence. Settings and feed `interval_seconds` show effective
cadence; `/api/plan` may show zero for unknown or shared accounts even though
Free's 900-second limit applies.

Offline changes remain pending and retry after reconnect. Lost acknowledgements
can be replayed without duplicate writes. Keep the selected source files intact:
a changed queued slice or prefix becomes a conflict, rather than resetting the
cursor. `run --once` exits `0` on completion, pacing or capture being off, `1`
on conflicts, revoked credentials or transport failures, and `2` for invalid
configuration or permissions. Fix the reported cause and retry.

## Imports and shared sessions

Matching native sessions from the same user share stored bytes across connections,
machines and private imports. Different users remain separate; private transcript
reads also require permission and must belong to the filing user. Divergent live
captures retain a fork. Diverging imports are stored whole and ungrouped.
Existing duplicate histories remain intact and are listed as legacy copies.
`GET /api/sync/sessions` reports merged session `bytes`, streams, forks and legacy
copies to signed-in people; an import's `stored_messages` keeps its own-submission
count as described in [one-time import](one-time-import.md).

Three exceptions keep separate imports: snapshots ending in an incomplete live
tail; older T3 snapshots imported with an explicit native format that automatic
format detection cannot recognize; and identities containing `#sha256:`.
An incomplete final multipart JSON object is refused; a complete final record
without a newline is accepted. Exact bytes remain available in every case.

## Find synced history in Memory

Uploads and Memory indexing have separate progress. After an accepted capture
append or exact retry, background indexing catches up the whole stored session.
Normal server imports are also indexed in the background; legacy client memory sync is disabled.
Searchable text keeps the original words; a hit's `source.thread` can point to
the shared session rather than an individual import.

Structured mirror copies of private stored threads, facts derived from them, and their
tunnel previews are returned only to callers who can read their source thread. This
covers structured Memory search, `memory_get` and session start context. CLI-format (`cli_compatible`) search excludes mirror drawer copies for everyone, including owners; its `facts` field is still returned only to callers who can read each fact's source thread. A
`superseded_by` id is returned only when its target is another kept fact in the same
response.

Adding, updating, deleting or linking drawers in a stored-thread room requires
permission to read that thread. Known exceptions: `delete_by_source`, `delete_tunnel`
and `delete_hallway` take no room and are not checked yet.

Fact mutations (`kg_invalidate`, `kg_supersede`, `kg_save` merges, and single-valued supersedes through `kg_save` or `fact_save`) on thread-derived
facts are not room-checked. A member with fact-write permissions but no source-thread
read access can mutate those hidden facts. Invalidate's `closed` count is a yes/no
oracle for hidden values. This pre-existing write-side exposure is accepted for follow-up alongside `delete_by_source`, `delete_tunnel` and `delete_hallway`. Presence is not hidden either: taxonomy and aggregate counts can show stored-thread ids and per-thread counts to callers who can read the private section, fan-out ranks and counts can reflect hidden hits, and gating runs after a bounded fetch of at most 100 candidates, so hidden hits can take candidate slots.

Mirror copies do not count as duplicates for anyone. Similarity dedup exclusion (D5) is
verified on compact, sqlite_exact and chroma.
Default self-service PAT and OAuth connections still cannot retrieve this
history through Memory search, `memory_get` or session start context, even when allowed to upload it.

Existing imports are indexed automatically. Older captures that never sync again
need `commonplace search-index run --workspace WORKSPACE_ID --captures` from an
operator. This is separate from the upload backfill described above. Index bytes
count toward workspace storage, and indexing pauses 16 MiB before the plan limit.
See [indexing progress and controls](one-time-import.md#indexing-progress-and-storage)
for the owner status endpoint, operator commands and the indexing off switch.

## Stop or remove

`commonplace sync uninstall` previews removal. Add `--enable` to stop the service
and remove each installed systemd unit and launchd plist; `--no-activate` removes
the files without running a service manager. Local configuration and pending
state remain. Disable workspace capture with `{"enabled":false}` to reject
future transcript uploads immediately; existing history stays intact.
