# Live sync

Live sync uploads selected Claude Code and Codex transcripts and memory files
verbatim to your Commonplace gateway. Choose a remote gateway only if you want
those files uploaded there. T3 sessions use their underlying native transcript
roots. Cursor supports structure-only inspection with
`commonplace sync probe FILE`; its native database is not a supported sync source.

## Set up

Enable capture as the workspace owner with
`POST /api/settings/capture` and `{"enabled":true}`. Use a matching Claude Code
or Codex connection with `memory:write`. Memory files need an import-enabled
connection in your personal workspace. See [capture](transcript-capture.md) and
[import permissions](one-time-import.md#import-only-connection).

Create a private environment file (mode `0600`) containing only the credentials
you need, as shell-compatible `NAME='value'` lines:

```text
COMMONPLACE_CAPTURE_TOKEN_CLAUDE_CODE='your Claude Code connection token'
COMMONPLACE_CAPTURE_TOKEN_CODEX='your Codex connection token'
COMMONPLACE_IMPORT_TOKEN='your import-enabled connection token'
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
are `~/.claude/projects` and `~/.codex/sessions`. Add
`--memory notes=ROOT` to sync Markdown files, or `--memory 'notes=ROOT:**/*.txt'`
for another pattern. Memory selection skips recently modified files (60 seconds),
files over 400,000 bytes, Commonplace's own data and onboarding pointer files.
Native scans skip symlinks and wait for complete records.

For a foreground pass, load the environment file into your shell and run
`commonplace sync run --once`. `commonplace sync run` runs continuously;
`commonplace sync status` reports local progress. Keep environment values out
of command arguments and shared logs. Configuration and the durable local
outbox live under `~/.commonplace/sync/`, with private permissions; the outbox
stores references and hashes, not transcript text or credentials.

## Timing and recovery

Free opens one account-wide upload window every 900 seconds; captures on other
streams can join it for 60 seconds, and each stream still waits 900 seconds
between appends. After a one-time import, live uploads may wait up to 900
seconds. A refused import still consumes that slot, and memory-only uploads
use it too. Paid and unrestricted self-hosted instances send changes immediately,
within the server's write budget. Continuous sync checks files every two seconds;
it does not stream individual tokens.

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

## Stop or remove

`commonplace sync uninstall` previews removal. Add `--enable` to stop the service
and remove each installed systemd unit and launchd plist; `--no-activate` removes
the files without running a service manager. Local configuration and pending
state remain. Disable workspace capture with `{"enabled":false}` to reject
future transcript uploads immediately; existing history stays intact.
