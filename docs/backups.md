# Nightly backups and scratch restore

Back up the entire Commonplace data directory. `control.sqlite3` is the hub's
control database; it holds accounts, workspace membership and token hashes.
Workspace directories hold boards, rules, threads, palaces and generation
pointers. Restoring a workspace alone loses the hub records needed to use it.

The supported snapshot path is offline and requires Linux `/proc`. The
gateway and all workers must be stopped. `backup-hook.sh` does this through
the operator's Compose configuration, then runs `commonplace backup` in a
one-off container. An exclusive data-directory lock and orphan-worker check
refuse cooperating writers. Every detected SQLite database, including the
control DB, historical generations and nested workspace files, is copied
through SQLite's backup API. Committed WAL rows are included, standalone
snapshots use DELETE journal mode, and WAL/SHM/journal sidecars are omitted.
Other files are copied verbatim; only the disposable shared model cache is
excluded. Do not run writers outside the managed service during this window.

A manifest records each file's relative path, size and SHA256. Snapshot
publication is atomic. These snapshots support the current plaintext SQLite
stores. They do not establish a SQLCipher backup format: an encrypted store
requires its own checkpoint/byte-copy and key-recovery design before rollout.
Do not treat an opaque copied file as a verified encrypted database restore.

## Configure the nightly job

Install Bash, coreutils, `flock`, Docker Compose and restic on the backup
runner. Provision a restic repository on a separate machine or remote object
store, with a strong password and restricted access. Restic encrypts repository
data; keep its password/recovery material separately from the data host.
The job never initializes repositories or reads password files itself.
Verify the destination really is off-host; a remote URL alone cannot prove
that. See [restic's repository preparation](https://restic.readthedocs.io/en/stable/030_preparing_a_new_repo.html).

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
keep logs private. The nightly wrapper alerts on snapshot, restart, upload,
retention and cleanup failures, returns nonzero, and separately reports alert
delivery failure. A missing alert executable refuses the run before downtime.
Also configure scheduler-level failure handling and monitor the age of the
last successful remote snapshot: a disabled scheduler or killed process cannot
reliably run its own failure callback.

Run with an existing absolute staging directory, outside the source data tree:

```sh
/path/to/commonplace/app/nightly-backup.sh /path/to/commonplace/backups
```

The wrapper locks that staging directory against overlapping nightly runs,
stops writers for the snapshot, restarts the gateway before upload, and invokes
restic only after the hook succeeds. A partial-file restic exit is a failure.
It then applies `forget --keep-within 30d --prune`, scoped to this deployment's
host label and `commonplace` tag. Grouping uses `host,tags`, since snapshot
paths change every run. Unrelated snapshots are not forgotten. The retention
window is measured from the newest successful snapshot; forgetting only runs
after a successful upload. This supersedes the old 30-daily / 12-weekly policy.
See [restic retention semantics](https://restic.readthedocs.io/en/stable/060_forget.html).

Temporary plaintext snapshots are removed after success or failure. A failure
preserves a mode-600 diagnostic log and reports its location. A hard kill or
power loss may leave a hidden `.commonplace-nightly.*` directory; inspect and
remove abandoned staging only when no backup is running. Staging is not a
retained backup, and no second local backup repository is created.

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
TimeoutStartSec=20min
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

Install and enable these only after a manual backup, received failure alert
and scratch restore succeed. Pick a time and deadline appropriate to data size;
expect an outage during the snapshot phase. Schedule periodic `restic check`
and monthly restore drills separately.

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
