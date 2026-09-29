# CLI write routing

Routine CLI writes consume the shared routing policy introduced for the Tier 3
rollout tracked in #1963.

## Routed commands

The policy applies to:

- `mempalace mine`;
- `mempalace sweep`;
- `mempalace sync`;
- the optional post-setup mine run by `mempalace init`.

## Policies

### `direct`

Use the existing direct in-process execution path.

### `prefer`

Submit through the local daemon. Interactive CLI commands are allowed to start
the daemon when it is not already running.

### `require`

Submit through the local daemon. Interactive CLI commands are allowed to start
the daemon when it is not already running.

For CLI commands, `prefer` and `require` both select the daemon because daemon
startup is permitted. Their difference remains meaningful to hook callers,
which cannot cold-start the daemon.

## Explicit flags

Force daemon execution:

    mempalace mine ./project --daemon

Force direct execution:

    mempalace mine ./project --direct

The flags are mutually exclusive and override environment/config policy.

Background execution:

    mempalace mine ./project --background

`--background` is valid when the selected route is the daemon. A direct route
with `--background` exits with a configuration error.

## Configuration

Use the daemon for routine CLI writes:

    MEMPALACE_CLI_WRITE_ROUTING=prefer

Prohibit an accidental direct route:

    MEMPALACE_CLI_WRITE_ROUTING=require

Retain direct behavior:

    MEMPALACE_CLI_WRITE_ROUTING=direct

Config file:

    {
      "write_routing": {
        "cli": "require"
      }
    }

## Defaults and rollout

The default remains `direct` in this PR.

This means existing users receive no silent execution-topology change. A
supervised Tier 3 deployment enables `prefer` or `require` explicitly. The
production default can be changed later after the stacked rollout is reviewed.

## Safety properties

- Daemon submission errors never trigger direct fallback.
- A failed or ambiguous submission may already have created a durable job;
  retrying directly could duplicate content.
- Post-init mining forwards the already-scanned file list into the daemon, so
  the project is not scanned twice.
- `sweep` is now a first-class daemon job.
- `--direct` remains an explicit emergency/debug escape hatch.
- No low-level lock behavior is changed.

## Maintenance exclusions

These remain outside ordinary routing:

- repair;
- migration and wing migration;
- index rebuild;
- closet compression;
- embedder identity changes.

They can replace indexes, rewrite broad metadata sets, or require all cached
handles to close. They need an exclusive-maintenance protocol rather than an
ordinary queued-write job.
