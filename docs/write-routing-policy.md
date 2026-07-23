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

## Follow-up PRs

PR 2 will apply the policy to hook-triggered writes.

PR 3 will apply the policy to routine CLI writes.

Maintenance operations such as repair, migration, and index rebuild are not
ordinary routed writes. They require a separate exclusive-maintenance policy.
