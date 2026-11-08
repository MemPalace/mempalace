# Commonplace deployment

Keep host configuration outside the source checkout. The tracked Compose and
Caddy files are examples; addresses under `192.0.2.0/24` and `2001:db8::/32`
are documentation placeholders. Replace them before deploying.

## Files

Choose an operator directory and an app checkout beneath it:

```text
/path/to/commonplace/
  .env                 # private environment and credentials; mode 600
  compose.yml          # operator-owned configuration
  deploy.env           # private backup gate configuration; mode 600
  data/                # persistent data, owned by UID/GID 10001
  backups/             # temporary private staging for nightly upload
  app/                 # release source, replaceable independently of configuration
```

Copy `deploy/compose.host.yml.example` to the operator's `compose.yml` and
`deploy/commonplace.env.example` to `.env`. Set `COMMONPLACE_SOURCE_DIR`,
`COMMONPLACE_ENV_FILE`, and `COMMONPLACE_DATA_DIR` to absolute paths on that
host. Set `COMMONPLACE_BASE_URL` and `COMMONPLACE_ALLOWED_HOSTS` for the URL
clients will use. Do not commit the filled-in files. The corresponding
`deploy/compose.host.yml`, `deploy/commonplace.env`, `deploy/deploy.local.sh`,
and local Caddy copies are ignored when used for local testing.

Install the reviewed `app/deploy/commonplace/deploy.sh` as `/srv/commonplace/deploy.sh`
for updates as described below.

A deployment wrapper must pass the same external env and Compose paths on
both validation and deployment. `COMPOSE_FILE` must identify the
operator-owned file, not an example in the checkout. Configure the backup
job with the same paths. Preserve the top-level `name` in the Compose file
when relocating it so Compose manages the existing container and network. Leave
`COMPOSE_PROJECT_NAME` unset in the environment and environment file.

The service publishes only `127.0.0.1:8765`. Caddy terminates TLS and proxies
requests to that listener. The container runs as `10001:10001`, mounts its
persistent data at `/data`, and has a 3 GiB memory limit. Use a local
filesystem with working SQLite/Chroma locks, encrypted storage where
required, and an off-host backup. Never rebuild or initialize an existing
data directory during an application update.

The image preloads the local MiniLM embedding model as the runtime user.
Its cache is under `$HOME/.cache/chroma`, outside `/data`;
workspace workers link to that cache. A failed preload fails the build.

## Releases and reverse proxy

Stage a reviewed release into the app directory with git or rsync. An rsync
update must leave `.env`, `compose.yml`, and other operator files outside
its destination. Do not use `--delete` against the operator directory.
Keep the previous source release and image tag for rollback.

Copy `deploy/commonplace.caddy.example` or
`deploy/commonplace-public.caddy.example` to your private Caddy site
configuration. Replace bind addresses, hostnames and the static root with
your deployment values. Keep installed site files outside the checkout;
application updates must not replace host-wide Caddy settings. Validate the
complete Caddy configuration before reloading the host service.

The private template routes `/app*`, `/api/*`, `/mcp`, `/health`, `/healthz`,
`/s/*`, `/oauth/*` and `/.well-known/*` to the gateway without stripping paths.
Its fallback serves `landing/public/`. The public template separates the
website, app and agent endpoints; see [PUBLIC.md](../PUBLIC.md).

## Bootstrap and verification

Use the reviewed release's CLI to create the workspace and issue a scoped
token. Never put token values in tracked configuration, shell history,
screenshots or command output. Clients connect to the chosen `/mcp` URL
with `Authorization: Bearer $COMMONPLACE_TOKEN`; stdio-only clients can use
`commonplace connect --url https://hub.example.com/mcp` with that variable.
See `commonplace/agent-skill/SKILL.md` for client integration.

Verify `/healthz`, sign-in, authenticated `tools/list`, memory save/search,
and board access after deploying. Unauthenticated MCP requests must be
refused. A static test does not prove TLS, container startup or recovery.

## Backups

Use [nightly backups and scratch restore](../../docs/backups.md) to configure
`app/nightly-backup.sh`: consistent online snapshots of the running service,
encrypted off-host restic storage, 30-day retention and failure notification. Keep credentials
and scheduler configuration outside the checkout. The older 30-daily / 12-weekly
policy is superseded.

For integration into another host backup job, executable `app/backup-hook.sh`
still accepts an existing absolute staging directory and only creates a
snapshot. The staging directory must be on the same filesystem as the data
directory. That hook does not configure or invoke restic; its caller must
upload, apply retention, remove local staging (`commonplace-*` and
`.commonplace-staging`, also after a failure) and alert on failure.

By default the hook reads `.env` and `compose.yml` from the parent of its
source checkout. Override that directory with `COMMONPLACE_DEPLOY_DIR`,
the environment file with `COMMONPLACE_ENV_FILE`, or the single Compose
file with `COMPOSE_FILE`. Nonempty overrides must be absolute paths;
relative paths are rejected before the gateway is touched. The deploy
wrapper and backup scheduler must agree on these files and the Compose
project name.

The hook never stops, starts, restarts, recreates, removes or kills the
serve container and never starts a one-off container; it uses only Compose `ps`
and `exec` plus `docker inspect`. It locks the backup directory, records the
serve container's ID and start time, finds the host path mounted at `/data`,
creates `.backup-staging/` there (owned by `10001:10001`, mode 0700), removes
earlier unlocked leftovers and checks free space. It then runs
`commonplace backup --online` inside the running container with stdin closed,
bounded by `COMMONPLACE_BACKUP_DEADLINE` (default 4500 seconds) plus a grace
period, while it refreshes a heartbeat file the in-container snapshot watches.
If the container's ID or start time changed, the run fails as "serve changed".
Only a committed snapshot with `control.sqlite3` and a valid `manifest.json`
is moved to the staging directory and published by atomic rename; both parent
directories are synced. The service stays up. Memory pauses for about a minute
or two and one-shot CLI commands are refused during the run; see
[backups](../../docs/backups.md) for what users see and when a run is refused.

Failure returns nonzero with a one-line stage, keeps a mode-600 diagnostic log
(stages, counts and seconds only), removes its staging, leaves the service
untouched and must abort the parent backup job. Staging that a still-running
snapshot holds locked stays until the next run removes it. Verify failure
propagation and an off-host restore before relying on the scheduler.

Never put worktrees or other project paths inside the data directory. Install
this release's `commonplace` CLI on the data host before enabling the hook, or
install none; an older host CLI does not take the backup fence.

### Live proof

`deploy/commonplace/backup-live-probe.sh EVIDENCE_DIR` runs one backup through
the scheduler unit and samples the service while it runs: `/healthz` locally
and publicly every second, a board read and a heartbeat on a dedicated probe
card every 5 seconds, and an MCP memory search every 5 seconds. It passes only
if the serve container keeps its ID and start time, no one-off container
appears, every health check and board read and write succeeds, memory fails
only with `-32603` inside one bounded pause window, no sampler stalls, and a
new restic snapshot exists and verifies. An OOM kill in the window makes the
result inconclusive. Run it as root, with the owner's approval, and see the
script header for its settings; the token is never printed or put on a command
line.

## Rollback and recovery

For a code-only rollback, retain live data, restore the previous source
release and image tag, and use the same external Compose/env files:

```bash
docker compose --env-file "$COMMONPLACE_ENV_FILE" -f "$COMPOSE_FILE" \
  up -d --no-build --pull never commonplace
```

Restore the previous installed Caddy site files if routes changed, validate
the full configuration and reload Caddy. Check health, sign-in and MCP.
A wrapper that always rebuilds is unsuitable for image rollback. An image
without online backup makes the hook fail closed with an alert; it never
stops the service. Do not reinstall an older hook that stops the service.

For data recovery, retrieve a selected snapshot into a separate directory, then
run `commonplace restore <snapshot_dir> <new_data_dir>` to verify its manifest,
hashes and SQLite integrity before publishing a complete tree. Follow the
scratch restore procedure linked above first. Stop Commonplace, preserve
IDs and verbatim data, and set ownership to `10001:10001`.
Retain the previous data directory, switch to the restored sibling and
start its matching pinned image. Never run `init` against recovered data.
Verify health, search, cards and a new backup.

## Local checks

```bash
uv run pytest -q -p no:cacheprovider tests/test_commonplace_deploy.py
uv run ruff check .
uv run ruff format --check .
bash -n backup-hook.sh nightly-backup.sh deploy/commonplace/deploy.sh deploy/commonplace/backup-live-probe.sh
shellcheck backup-hook.sh nightly-backup.sh deploy/commonplace/deploy.sh deploy/commonplace/backup-live-probe.sh
```

## Deploy

Before running, stage the reviewed release and pin its build context and image tag
in the operator-owned `compose.yml`. The script does not pull source or remove
placeholders; prepare all deployment values yourself.

Copy the complete [deploy.env.example](deploy.env.example) and edit the installed
file for your deployment (example operator directory `/srv/commonplace`):

```bash
sudo install -o root -g root -m 600 app/deploy/commonplace/deploy.env.example /srv/commonplace/deploy.env
sudoedit /srv/commonplace/deploy.env
sudo /srv/commonplace/deploy.sh
```

Run the installed script as root after staging and pinning the release.
There is no skip option. Install jq on the host (`apt install jq`); bash,
GNU date, timeout, sleep, flock, curl, systemctl, systemd-run and docker compose
are also required. systemd must support transient services with `Type=exec`
(version 240 or newer).

Set `COMMONPLACE_ROOT` to the operator directory (default `/srv/commonplace`).
`COMMONPLACE_DEPLOY_ENV` defaults to `$COMMONPLACE_ROOT/deploy.env`.
The file is plain `KEY=VALUE` data, never shell code: use no quotes, shell
expansions or command substitutions. Environment values override file values.
Only these keys are accepted from deploy.env; other keys are ignored:
`DEPLOY_BACKUP_UNIT`, `DEPLOY_BACKUP_TIMEOUT`, `DEPLOY_BACKUP_CHECK_CMD`,
`DEPLOY_BACKUP_LATEST_CMD`, `DEPLOY_BACKUP_MAX_AGE`, `APP_PORT`, `COMPOSE_FILE`,
`COMPOSE_ENV_FILE`, `COMMONPLACE_SOURCE_DIR`, `COMMONPLACE_ENV_FILE`,
and `COMMONPLACE_DATA_DIR`.
Command values split on whitespace into an executable and arguments; shell
quoting, pipelines and glob patterns are not interpreted.

The three backup settings and the three `COMMONPLACE_*` path settings are required.
The paths may also come from the Compose environment file. The latest command must filter to
this deployment's host/tag and return one JSON object with a 64-character
lowercase hexadecimal `id` and an RFC3339 `time`, or `null` if none exists.
Both commands must return nonzero on failure. Keep the configuration root-owned
and mode 600. `DEPLOY_BACKUP_MAX_AGE` accepts 60–86400 seconds (default 7200).
`COMPOSE_FILE` defaults to `$COMMONPLACE_ROOT/compose.yml`, `COMPOSE_ENV_FILE`
to `$COMMONPLACE_ROOT/.env`, and `APP_PORT` to 8765 (integer 1–65535).
This script uses the operator-owned `$COMMONPLACE_ROOT/compose.yml` and
`$COMMONPLACE_ROOT/.env` by default, not the tracked example. It reads the
three path settings below from that env file as literal KEY=VALUE data
(environment overrides file values), before starting the backup. File path values
must have no whitespace, quotes, expansions, backslashes or inline comments.
The deployment configuration must define the absolute `COMMONPLACE_SOURCE_DIR`,
`COMMONPLACE_ENV_FILE` and `COMMONPLACE_DATA_DIR` paths required by the example.
Keep these aligned with the backup unit's configuration.

The script performs these steps in order:

1. Validate root, configuration, dependencies and files, then acquire a nonblocking deploy lock.
2. Hold a reference to the backup unit, then start it with `systemctl start --wait`, require a changed,
   nonempty InvocationID and a successful Result. The unit must be loaded, Type=oneshot and RemainAfterExit=no. The start is
   bounded by DEPLOY_BACKUP_TIMEOUT (positive integer seconds, default 3600);
   the unit's own `TimeoutStartSec` must be finite and below it, or the deploy
   refuses before starting the unit. For the online backup use
   `TimeoutStartSec=3h` and `DEPLOY_BACKUP_TIMEOUT=11400`.
   ExecMainStartTimestamp must independently be at or after deploy start.
   All clock comparisons use epoch seconds, matching systemd timestamp precision.
   Missing InvocationID/Result state fails closed. The script creates a unique
   transient `commonplace-deploy-proof-*.service` running `sleep infinity` with
   `After=<backup>.service`. An active unit's dependency keeps the backup loaded
   under [systemd's unit garbage collection rules](https://www.freedesktop.org/software/systemd/man/251/systemd.unit.html#Unit%20Garbage%20Collection).
   `After=` only orders jobs; it does not start the backup. `Type=exec` makes
   reference creation wait for successful execution of the holder, before reading
   the previous InvocationID or starting the backup. This preserves execution
   proof even when the timer is masked and the oneshot exits before start returns;
   no polling or journal retention is needed. No enabled timer or persistent
   reference is an operator precondition, and `RemainAfterExit=yes` is refused.
3. Validate the latest snapshot: it must start at or after deploy start,
   be no more than 60 seconds ahead of the current clock, and be within MAX_AGE.
4. Run the backup verification command and require success.
   Require the backup to be inactive, then stop the reference holder before any
   Compose call. An EXIT trap also stops it on failure, timeout, HUP, INT or TERM;
   `--collect` lets systemd unload the stopped transient service. A failure to
   create or release the holder fails the deploy. SIGKILL or a shell crash cannot
   run a trap: inspect and stop any leftover `commonplace-deploy-proof-*.service`
   before retrying.
5. Validate Compose JSON so every published port binds to `127.0.0.1`,
   require the published port equals APP_PORT, run `up -d --build`, show `ps`,
   and print the health HTTP status. The health probe is informational and has
   a 10-second timeout; verify readiness separately as described above.

The deploy script makes no docker/compose call until steps 2–4 pass. The backup unit never stops, restarts or replaces the gateway; it snapshots the running service. MAX_AGE must exceed your measured backup duration (restic stamps `time` at the start of the backup); the default 7200 seconds covers the hook's 75-minute snapshot deadline plus upload for typical data sizes.

On failure, fix the backup or configuration and rerun the script. Do not work
around the gate. The script does not fetch source or delete operator files.

## Running build identity

When staging a clean reviewed source tree, set `COMMONPLACE_BUILD_COMMIT` to its
full reviewed commit (40 or 64 hexadecimal characters) before building with the
tracked host compose example. The actual image recipe validates the build argument
and writes `commonplace/web/_build_commit.txt` before package installation. This
resource travels with the installed wheel. An empty build argument is permitted
and reports **Running commit unknown**; an invalid nonempty argument fails the
build. No runtime environment or checkout HEAD overrides image identity. Admin
shows both installed package versions separately from the commit.

## Optional backup status

Only the hosting administrator provisions live configuration. The optional tracked
`deploy/compose.backup-status.yml.example` overlays the host compose example. Set
`COMMONPLACE_BACKUP_STATUS_HOST_DIR` to an existing directory outside `/data` and
all backup snapshot/staging trees. Provision it with UID/GID **10001:10001**, mode
**0700**; receipts are **0600**. Compose uses `bind.create_host_path:false` on both
binds and must never create the source as root. If using another UID, provide
explicit read access rather than making files public.

The same host directory has two distinct container targets:

| Target | Mount | Consumer |
| --- | --- | --- |
| `/run/commonplace-backup-status-ro` | read-only | Gateway, `COMMONPLACE_BACKUP_STATUS_DIR` |
| `/run/commonplace-backup-status-rw` | read-write | Snapshot CLI override only |

Keep the gateway environment on the RO path. Set
`COMMONPLACE_BACKUP_STATUS_WRITE_DIR=/run/commonplace-backup-status-rw` on the
**host hook invocation**, never in the gateway environment. Only the real snapshot
exec inserts `-e COMMONPLACE_BACKUP_STATUS_DIR=/run/commonplace-backup-status-rw`
before the service name. Preflight/check and cleanup do not report and receive no
writer override. The hook still uses only the existing ps/exec topology and no new
container. For a manual offline CLI in that container use the same explicit
per-command `compose exec -T -e ...` override; a host CLI uses the provisioned host
directory as its status path. Without opt-in, reporting is disabled and Admin shows
unknown. Never mount a container engine socket for status reads.

Both aliases are visible in the shared container mount namespace. The configured
gateway path is enforced RO and gateway code only reads it; this is **not OS
isolation** preventing that process from reaching the RW alias. No per-process
mount table or UID separation is claimed. This residual is accepted for this topology.

Receipts record the last completed online/offline instance snapshot attempt, not
running state or verified remote upload/retention. No historical backfill is possible;
SIGKILL before reporting leaves the previous receipt. Receipt errors never change
backup success/failure or fencing. Admin authorization is signed-in current-workspace
owner plus a per-request server admin flag; ordinary owners see exactly
“Instance administrator access required” and send no operational requests.
See [Admin API](../../docs/admin.md) and [backup receipts](../../docs/backups.md#optional-status-receipts).
