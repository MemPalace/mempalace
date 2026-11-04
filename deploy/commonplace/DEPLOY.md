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

Use the tracked `app/deploy/commonplace/deploy.sh` for updates as described below.

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
`app/nightly-backup.sh`: consistent offline snapshots, encrypted off-host
restic storage, 30-day retention and failure notification. Keep credentials
and scheduler configuration outside the checkout. The older 30-daily / 12-weekly
policy is superseded.

For integration into another host backup job, executable `app/backup-hook.sh`
still accepts an existing absolute staging directory and only creates a
snapshot. That hook does not configure or invoke restic; its caller must
upload, apply retention, remove local staging and alert on failure.

By default the hook reads `.env` and `compose.yml` from the parent of its
source checkout. Override that directory with `COMMONPLACE_DEPLOY_DIR`,
the environment file with `COMMONPLACE_ENV_FILE`, or the single Compose
file with `COMPOSE_FILE`. Nonempty overrides must be absolute paths;
relative paths are rejected before the gateway is touched. The deploy
wrapper and backup scheduler must agree on these files and the Compose
project name.

The hook locks the backup directory, checks the backup CLI, stops the
container and its workers, takes a consistent snapshot in a one-off
container, and restarts the gateway even if snapshot creation fails.
Expect a short outage. Staging is owned by `10001:10001` and is mounted at
`/backup` separately from the source `/data`. Only a complete snapshot with
`control.sqlite3` and a valid `manifest.json` is published by atomic rename.
Failure returns nonzero, preserves staging and a mode-600 diagnostic log,
and must abort the parent backup job. Verify failure propagation and an
off-host restore before relying on the scheduler.

## Rollback and recovery

For a code-only rollback, retain live data, restore the previous source
release and image tag, and use the same external Compose/env files:

```bash
docker compose --env-file "$COMMONPLACE_ENV_FILE" -f "$COMPOSE_FILE" \
  up -d --no-build --pull never commonplace
```

Restore the previous installed Caddy site files if routes changed, validate
the full configuration and reload Caddy. Check health, sign-in and MCP.
A wrapper that always rebuilds is unsuitable for image rollback. Keep the
backup hook aligned with the selected image before the next scheduled run.

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
bash -n backup-hook.sh deploy/commonplace/deploy.sh
```

## Deploy

Run `app/deploy/commonplace/deploy.sh` as root after staging the release.
There is no skip option. Install jq on the host (`apt install jq`); bash,
GNU date, timeout, flock, curl, systemctl and docker compose are also required.

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

Example private `deploy.env` (replace placeholders):

```text
DEPLOY_BACKUP_UNIT=<host>-backup.service
DEPLOY_BACKUP_CHECK_CMD=/usr/local/sbin/<host>-backup --check
DEPLOY_BACKUP_LATEST_CMD=/usr/local/sbin/<host>-backup --latest-json
DEPLOY_BACKUP_MAX_AGE=7200
DEPLOY_BACKUP_TIMEOUT=3600
```

The three backup settings are required. The latest command must filter to
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
The Compose environment must still define the absolute `COMMONPLACE_SOURCE_DIR`,
`COMMONPLACE_ENV_FILE` and `COMMONPLACE_DATA_DIR` paths required by the example.
Keep these aligned with the backup unit's configuration.

The script performs these steps in order:

1. Validate root, configuration, dependencies and files, then acquire a nonblocking deploy lock.
2. Start the backup unit with `systemctl start --wait`, require a changed,
   nonempty InvocationID and a successful Result. The unit must be loaded, Type=oneshot and RemainAfterExit=no. The start is
   bounded by DEPLOY_BACKUP_TIMEOUT (positive integer seconds, default 3600);
   ExecMainStartTimestamp must independently be at or after deploy start.
   All clock comparisons use epoch seconds, matching systemd timestamp precision.
   Missing InvocationID/Result state fails closed. A prerequisite is that the
   inactive unit remains referenced, for example by an enabled timer, so systemd
   does not unload it and discard InvocationID. Before using this script, inspect
   `systemctl show -p Triggers <backup>.timer` to confirm the service reference,
   and verify a completed backup leaves a nonempty InvocationID different from
   its previous value (`systemctl show -p InvocationID <backup>.service`).
   An empty post-run InvocationID reports that the unit was unloaded.
3. Validate the latest snapshot: it must start at or after deploy start,
   be no more than 60 seconds ahead of the current clock, and be within MAX_AGE.
4. Run the backup verification command and require success.
5. Validate Compose JSON so every published port binds to `127.0.0.1`,
   require the published port equals APP_PORT, run `up -d --build`, show `ps`,
   and print the health HTTP status. The health probe is informational and has
   a 10-second timeout; verify readiness separately as described above.

The deploy script makes no docker/compose call until steps 2–4 pass. The backup unit itself may briefly stop and start the gateway (that is the backup's design, not a deploy step). The default MAX_AGE of 7200 seconds exceeds the default 3600-second backup timeout. MAX_AGE must exceed your measured backup duration (restic stamps `time` at the start of the backup).

On failure, fix the backup or configuration and rerun the script. Do not work
around the gate. The script does not fetch source or delete operator files.
