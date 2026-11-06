# Nightly backups and scratch restore

Back up the entire Commonplace data directory. `control.sqlite3` is the hub's
control database; it holds accounts, workspace membership and token hashes.
Workspace directories hold boards, rules, threads, palaces and generation
pointers. Restoring a workspace alone loses the hub records needed to use it.

The nightly snapshot is online and requires Linux `/proc`. The service keeps
running: `backup-hook.sh` never stops, restarts, recreates or replaces the
serve container and never starts a one-off container. It runs
`commonplace backup --online` inside the running container with
`docker compose exec`. That command asks the gateway, over a private local
socket, to pause memory workers and briefly freeze file creation, takes one
consistent cut of every database and file, resumes the workers and copies the
rest while the service stays up. SQLite databases are copied through SQLite's
backup API from read transactions opened at the cut, so committed WAL rows are
included and WAL/SHM/journal sidecars are omitted. Worker files are copied while
their workers are paused. Other files are copied verbatim up to their size at
the cut; the disposable shared model cache, core dumps and the backup's own
staging and lock files are excluded. The snapshot format, `commonplace restore`
and manifest verification are the same as for an offline snapshot.

What users see during a run: the site, sign-in, boards and chat stay up.
Memory calls (MCP `tools/call`, `initialize`, `ping` and `/api/memory`) wait up
to 20 seconds while workers are paused, then fail with JSON-RPC `-32603` and
`data.retry_after_s` (or a REST error) until the workers are back, usually a
minute or two. Creating a workspace, thread or skill can wait up to a few
seconds. One-shot `commonplace` CLI commands against the data directory are
refused for the whole run.

The run fails closed (nonzero exit, one-line stage, no snapshot published,
service untouched) when the service is down or its image has no online backup,
a workspace database is encrypted, the backup directory is on another
filesystem, free space is short, a previous run still holds its lock, workers
do not drain or a lease lapses, or a successor agent launched by the service is
still alive (`successor active`) or starts during the run (`successor
launched`). A successor that outlives its launch fails each night's backup until
it exits.

Encrypted workspace databases are refused by the online snapshot. Back up such
a data directory offline in an announced maintenance window: stop the service
yourself, run `commonplace backup DEST` from the service's image against the
stopped data directory, then start the service again. The nightly hook never
does this for you.

Only these writers are coordinated: processes in the serve container (gateway,
workers, launched successors and their sessions) and host `commonplace` CLIs on
the data directory, which hold a shared fence lock for their lifetime. Install
this release's CLI on the data host before enabling the hook, or install none:
older host CLIs do not take the fence. Never put worktrees or other project
paths (including handoff successor projects) inside the data directory, and do
not run a gateway outside the container against it: an outside agent could
write the data tree during the cut and nothing in the backup can detect that.

Run exactly one gateway per data directory. The online backup pauses and
fences only the gateway that answers on the admin socket, so a second gateway
would write unseen during the copy. `commonplace serve` binds that socket
before it starts workers or serves requests, and exits nonzero ("run one gateway
per data directory") while another gateway answers on it, or when the socket
cannot be bound at all. Opening the control database, including its idempotent
schema setup, runs before that check. A second gateway configured with a different
`COMMONPLACE_ADMIN_SOCKET` is not detected; never start one.

Files other than SQLite databases and worker files are copied up to their size
at the cut. That is consistent because the gateway never rewrites such a file
in place: it appends (logs), creates new files, or writes a temporary file and
renames it over the old one. Worker files are copied while their workers are
paused.

A manifest records each file's relative path, size and SHA256. Snapshot
publication is atomic. Offline SQLCipher snapshots preserve encrypted bytes and
can be restored without keys; hashes verify those bytes, not decrypted
integrity. Keyslots and wrapping credentials are separate recovery inputs,
outside this backup tree. See [experimental workspace
encryption](workspace-encryption.md) for its limits and remaining rollout
requirements.

## Configure the nightly job

Install Bash, coreutils, `flock`, Python 3, Docker Compose and restic on the
backup runner, which is the data host. Provision a restic repository on a
separate machine or remote object store, with a strong password and restricted
access. Restic encrypts repository data; keep its password/recovery material
separately from the data host. The job never initializes repositories or reads
password files itself. Verify the destination really is off-host; a remote URL
alone cannot prove that. See [restic's repository
preparation](https://restic.readthedocs.io/en/stable/030_preparing_a_new_repo.html).

Create a private, mode-600 scheduler environment file outside the checkout:

```ini
COMMONPLACE_DEPLOY_DIR=/path/to/commonplace
COMMONPLACE_ENV_FILE=/path/to/commonplace/.env
COMPOSE_FILE=/path/to/commonplace/compose.yml
RESTIC_REPOSITORY=sftp:backup@backup.example.net:/srv/backups/commonplace
RESTIC_PASSWORD_FILE=/etc/commonplace/backup-password
COMMONPLACE_BACKUP_ID=commonplace-primary
COMMONPLACE_BACKUP_ALERT=/usr/local/libexec/commonplace-backup-alert
```

Use a unique, stable `COMMONPLACE_BACKUP_ID` for this deployment. It is the
restic snapshot host label, defaults to `commonplace`, and accepts letters,
digits, dots, underscores and hyphens. Do not reuse it for another data tree
in the same repository. Only explicit remote repository schemes are accepted;
local directories are refused. Authentication and transport configuration
remain operator-owned. Never print credentials or put them in command arguments.

The required alert executable receives two arguments: the failed stage and
the private diagnostic-log path (possibly empty). It must send a notification
to a monitored channel and return nonzero when delivery fails; it has a
30-second deadline. For example, on a host with configured mail delivery:

```sh
#!/bin/sh
set -eu
printf 'Commonplace backup failed at stage: %s\nInspect the private backup logs.\n' "$1" |
    mail -s 'Commonplace backup failure' backups@example.com
```

Install that script as the configured executable, test actual delivery, and
keep logs private. The nightly wrapper alerts on snapshot, upload, retention
and cleanup failures, returns nonzero, and separately reports alert delivery
failure. A missing alert executable refuses the run before any snapshot work.
Also configure scheduler-level failure handling and monitor the age of the
last successful remote snapshot: a disabled scheduler or killed process cannot
reliably run its own failure callback.

Run with an existing absolute staging directory outside the source data tree
but on the same filesystem as the data directory (the snapshot is published by
rename; another filesystem is refused):

```sh
/path/to/commonplace/app/nightly-backup.sh /path/to/commonplace/backups
```

The wrapper locks that staging directory against overlapping nightly runs,
takes the online snapshot through the hook, and invokes restic only after the
hook succeeds. A partial-file restic exit is a failure.
It then applies `forget --keep-within 30d --prune`, scoped to this deployment's
host label and `commonplace` tag. Grouping uses `host,tags`, since snapshot
paths change every run. Unrelated snapshots are not forgotten. The retention
window is measured from the newest successful snapshot; forgetting only runs
after a successful upload. This supersedes the old 30-daily / 12-weekly policy.
See [restic retention semantics](https://restic.readthedocs.io/en/stable/060_forget.html).

Temporary plaintext snapshots are removed after success or failure. A failure
preserves a mode-600 diagnostic log (stages, counts and seconds only) and
reports its location; the hook removes its staging. The snapshot is assembled in
`.backup-staging/` inside the data directory (owned by the service user, mode
0700, never part of a snapshot), then moved to the staging directory and
published there. A snapshot process that is still running keeps its locked
staging until it exits; each run first removes every earlier unlocked leftover
and fails with "previous backup still running" while one is locked. A hard kill
or power loss may also leave a hidden `.commonplace-nightly.*` directory;
inspect and remove abandoned staging only when no backup is running. Staging is
not a retained backup, and no second local backup repository is created.

The hook bounds the snapshot by `COMMONPLACE_BACKUP_DEADLINE` (default 4500
seconds, enforced inside the container as well). If the hook is killed, its
heartbeat file goes stale and the in-container snapshot stops itself within
about a minute; nothing is published. Other `COMMONPLACE_BACKUP_*` knobs (lease,
pause limit, bandwidth cap, disk reserve) are read from the service's own
environment; the hook reads only `COMMONPLACE_BACKUP_DEADLINE`,
`COMMONPLACE_BACKUP_RESERVE_BYTES` (free space kept on the data filesystem,
default the larger of 10 GB and 5 percent), `COMMONPLACE_BACKUP_MARGIN_BYTES`
(extra free space on top of the size estimate and the reserve, default 2 GB;
tests on small filesystems lower it) and `COMMONPLACE_BACKUP_HEARTBEAT_STALE_S`
(default 60; the hook touches the heartbeat every 10 s, or every third of a
shorter limit; set the same value in the service's environment). These host
values bound the hook's own checks only: the in-container snapshot reads its
knobs, including the reserve and the heartbeat limit, from the service's
environment. It never reads the env file itself.

The hook creates the heartbeat file `<stamp>.hb` next to the snapshot's
staging before it starts the snapshot, and keeps touching it. An online
backup refuses to start without that file, and a heartbeat that disappears
during the run stops it, so a manual `commonplace backup --online` (or a plain
`commonplace backup DEST` that goes online beside a serving gateway) needs the
same file kept fresh by its caller.

If you wrap the hook in your own host job instead of `nightly-backup.sh`, make
that job remove `commonplace-*` and `.commonplace-staging` from its staging
directory on every failure (keep its log), so a failed run leaves no plaintext.

An operator-owned systemd service and timer can run this nightly:

```ini
# commonplace-backup.service
[Unit]
Description=Commonplace nightly backup
OnFailure=commonplace-backup-failure.service

[Service]
Type=oneshot
EnvironmentFile=/etc/commonplace/backup.env
ExecStart=/path/to/commonplace/app/nightly-backup.sh /path/to/commonplace/backups
TimeoutStartSec=3h
UMask=0077
```

```ini
# commonplace-backup-failure.service
[Unit]
Description=Notify Commonplace backup scheduler failure

[Service]
Type=oneshot
EnvironmentFile=/etc/commonplace/backup.env
ExecStart=/usr/local/libexec/commonplace-backup-alert scheduler ""
TimeoutStartSec=30s
```

```ini
# commonplace-backup.timer
[Unit]
Description=Run Commonplace backup nightly

[Timer]
OnCalendar=*-*-* 03:00:00
Persistent=true
RandomizedDelaySec=10min

[Install]
WantedBy=timers.target
```

Install and enable these only after a manual backup, received failure alert and
scratch restore succeed. Pick a time and deadline appropriate to data size.
Keep the bounds nested: the hook's 75-minute snapshot deadline, below the
unit's finite `TimeoutStartSec` (3 hours here), below the deploy gate's
`DEPLOY_BACKUP_TIMEOUT` (for example 11400 seconds). The deploy script refuses
an infinite unit timeout or one not below its own. The service stays up during
the run; expect the memory pause described above. Schedule periodic `restic
check` and monthly restore drills separately.

## Restore drill on a scratch host

Use a separate scratch host and the application release matching the snapshot.
Give it a separate data directory and isolated network configuration so it
cannot send webhooks/mail or connect production clients. Preserve the original
data and repository. Never run `init` against recovered data.

1. Retrieve one explicitly selected snapshot. Load repository credentials
   through private operator configuration; do not copy them into the report.

   ```sh
   restic snapshots --host commonplace-primary --tag commonplace
   /usr/bin/time -p restic restore <snapshot-id> --target /scratch/retrieved
   ```

   Restic preserves the backed-up absolute path beneath the target. Locate the
   `commonplace-<timestamp>/manifest.json` belonging to that selected snapshot.
   Record its repository snapshot ID and retrieval time in private evidence.

2. Verify and publish a fresh offline tree:

   ```sh
   commonplace restore /scratch/retrieved/path/to/commonplace-snapshot /scratch/restored-data
   ```

   The destination must not exist. The command rejects missing, extra,
   duplicate, unsafe or damaged files and checks SQLite integrity, then
   atomically publishes the tree with private permissions. Its JSON result
   reports file count, bytes and `elapsed_seconds` for verification and local
   restore, excluding retrieval. A manifest checks accidental damage, not
   authenticity; trust repository encryption and access controls.

3. Inspect the restored control DB's workspace IDs and memberships. Check at
   least one workspace's exact memory text, board cards, rules and threads,
   including a fresh user's workspace. Start the pinned application against
   the scratch tree with the proper ownership. Verify sign-in, scoped access,
   search and cards; verify another backup can be made after stopping it.

4. Record retrieval, local restore and time-to-usable separately, plus snapshot
   ID, release, file/byte counts, checked workspace IDs and results. Keep
   deployment details and memory content out of public reports. An actual
   off-host retrieval and delivered failure alert are deployment evidence;
   offline unit tests do not establish either.

For production recovery only after the drill: stop all writers, retain the old
data directory, switch the persistent mount to the verified restored sibling,
set ownership for the service user and start the matching image. Verify health,
sign-in, isolation, search, boards and a subsequent nightly backup.
