# Antigravity IDE — Integration Surface Investigation

**Investigated**: 2026-05-27
**Author**: undeadindustries
**Scope**: What MemPalace can integrate with in Google's Antigravity IDE,
what we shipped, and what we deliberately did not ship.

This document is the source of truth for design decisions in the
`feat/antigravity-support` branch. It exists so a future maintainer can
re-derive every choice without re-reading the docs cold.

---

## 1. Surfaces verified against Google's official Antigravity docs

All quotes are pulled verbatim from Google's official Antigravity
documentation on 2026-05-27. URLs are the authoritative source; the
mirrored excerpts here are for reviewer convenience.

### 1.1. MCP — `https://antigravity.google/docs/mcp`

> The configuration file is located at `~/.gemini/antigravity/mcp_config.json`.
>
> The configuration file has a single `mcpServers` object where you
> define each server you want to connect to.

Schema (verified):

```json
{
  "mcpServers": {
    "<name>": {
      "command": "...",        // stdio
      "args": [...],
      "env": {...},
      "cwd": "...",
      "serverUrl": "...",      // remote
      "headers": {...},
      "authProviderType": "google_credentials",
      "oauth": {"clientId": "...", "clientSecret": "..."},
      "disabled": false,
      "disabledTools": [...]
    }
  }
}
```

Per-plugin form: `mcp_config.json` at the plugin root, same shape.
Antigravity merges plugin entries with the user's
`~/.gemini/antigravity/mcp_config.json` rather than clobbering.

**Cross-checked locally**: the user's existing
`~/.gemini/antigravity/mcp_config.json` already contains a working
`"mempalace": {"command": "/Users/robs/.local/bin/mempalace-mcp"}`
entry, proving the shape matches and the binary is on PATH.

### 1.2. Plugins — `https://antigravity.google/docs/plugins`

> A plugin is a directory containing a `plugin.json` file and optional
> subdirectories for different customization types:
>
> ```
> plugins/<plugin-name>/
> ├── plugin.json       # Required marker file
> ├── mcp_config.json   # Optional MCP server definitions
> ├── hooks.json        # Optional hooks definition
> ├── skills/           # Optional skills
> │   └── <skill-name>/
> │       └── SKILL.md
> └── rules/            # Optional rules
>     └── <rule-name>.md
> ```

Manifest schema (verified):

```json
{
  "name": "my-custom-plugin"
}
```

> The name field is optional and defaults to the directory name if omitted.

Install locations (verified):

> - Workspace Level: Place your plugin folder inside a
>   `.agents/plugins/` or `_agents/plugins/` directory at the root of
>   your opened workspace.
> - Global Level: Place your plugin folder inside
>   `~/.gemini/config/plugins/` in your user home directory.

We ship the global location as the canonical install path.

### 1.3. Skills — `https://antigravity.google/docs/skills`

> A skill is a folder containing a `SKILL.md` file with instructions
> that the agent can follow when working on specific tasks.

Frontmatter (verified):

| Field         | Required | Description                                                                                       |
|---------------|----------|---------------------------------------------------------------------------------------------------|
| `name`        | No       | A unique identifier (lowercase, hyphens). Defaults to the folder name.                            |
| `description` | Yes      | A clear description of what the skill does and when to use it.                                    |

Standalone discovery paths (verified):

| Location                            | Scope                  |
|-------------------------------------|------------------------|
| `<workspace>/.agents/skills/`       | Workspace-specific     |
| `~/.gemini/antigravity/skills/`     | Global, all workspaces |

In-plugin discovery: `<plugin>/skills/<skill-name>/SKILL.md`.

We ship the in-plugin form so a single install registers MCP, skill, and
hooks together.

### 1.4. Hooks — `https://antigravity.google/docs/hooks?app=antigravity`

> Hooks allow you to run custom scripts or shell commands at specific
> points during Antigravity's execution loop.

`hooks.json` lives at one of:
- `~/.gemini/config/hooks.json` (global)
- `<workspace>/.agents/hooks.json` (workspace)
- `<plugin-root>/hooks.json` (per-plugin) — what we ship

Top-level schema (verified):

```json
{
  "<hook-name>": {
    "enabled": true,
    "PreToolUse":     [{ "matcher": "...", "hooks": [{...}] }],
    "PostToolUse":    [{ "matcher": "...", "hooks": [{...}] }],
    "PreInvocation":  [{ ... }],
    "PostInvocation": [{ ... }],
    "Stop":           [{ ... }]
  }
}
```

For `PreInvocation` / `PostInvocation` / `Stop`, items are flat handler
objects; the `matcher` wrapper is only used for `PreToolUse` /
`PostToolUse`.

Handler object (verified):

| Field     | Required | Description                                       |
|-----------|----------|---------------------------------------------------|
| `type`    | No       | Currently only `"command"` is supported. Default. |
| `command` | Yes      | The shell command to execute.                     |
| `timeout` | No       | Timeout in seconds. Defaults to 30.               |

#### STDIN/STDOUT contract (verified)

> Hooks receive input via stdin as JSON and should return output via
> stdout as JSON. Field names use camelCase.

Common stdin fields (every event):

| Field                   | Type            | Description                                           |
|-------------------------|-----------------|-------------------------------------------------------|
| `conversationId`        | string          | The unique UUID of the active agent conversation.     |
| `workspacePaths`        | array<string>   | Absolute directory paths of the user's workspaces.    |
| `transcriptPath`        | string          | Absolute path to the persistent `transcript.jsonl`.   |
| `artifactDirectoryPath` | string          | Path to conversation artifacts and screenshots.       |

`Stop` event additional fields:

| Field               | Type    | Description                                                                |
|---------------------|---------|----------------------------------------------------------------------------|
| `executionNum`      | integer | Sequence number of the execution attempt.                                  |
| `terminationReason` | string  | `"model_stop"`, `"max_steps_exceeded"`, `"error"`, etc.                    |
| `error`             | string  | Optional error message.                                                    |
| `fullyIdle`         | boolean | **Required.** True iff all background commands and async tasks are done.   |

`Stop` event stdout (verified):

| Field      | Type   | Description                                                                                           |
|------------|--------|-------------------------------------------------------------------------------------------------------|
| `decision` | string | **Required.** Set to `"continue"` to FORCE the agent to keep running. Anything else allows the stop. |
| `reason`   | string | Optional. If `decision == "continue"`, injected as a system message.                                  |

**CRITICAL**: emitting `{"decision": "continue"}` from a save hook would
turn it into an infinite agent-loop trigger. The MemPalace save hook
MUST emit `{}` on every code path. There is an explicit refusal in
`mempal_save_hook_antigravity.sh` to ever print the literal word
`"continue"` from a decision field.

`PreInvocation` event additional fields:

| Field             | Type    | Description                                              |
|-------------------|---------|----------------------------------------------------------|
| `invocationNum`   | integer | Sequence number of the current model invocation.         |
| `initialNumSteps` | integer | Number of steps currently in the trajectory.             |

`PreInvocation` event stdout (verified):

| Field         | Type           | Description                                                                                   |
|---------------|----------------|-----------------------------------------------------------------------------------------------|
| `injectSteps` | array<object>  | Optional. Steps injected before the model is called. Each step has one of:                    |
|               |                | `{ "toolCall": {...} }` / `{ "userMessage": "..." }` / `{ "ephemeralMessage": "..." }`        |

We use `ephemeralMessage` for the wake-up injection: the message is
visible to the model on this turn but does not persist to the
transcript, so we do not pollute future model calls with the same
injection.

`PreInvocation` fires before EVERY model invocation, not only at session
start. We gate to `invocationNum == 1` to mimic Cursor's `sessionStart`
semantics — exactly one wake injection per conversation.

### 1.5. Permissions — `https://antigravity.google/docs/permissions`

Permissions are user-side, configured via Allow / Deny / Ask lists.
Plugins do **not** declare permissions in `plugin.json`. The
third-party "antigravity-plugins" community skill at
`~/.gemini/skills/antigravity-plugins/SKILL.md` documents a
`"permissions": [...]` field in `plugin.json`; that field is fabricated
and does not appear in any real Google-shipped plugin (`firebase`,
`google-antigravity-sdk`, `chrome-devtools-plugin`,
`modern-web-guidance-plugin`) inspected at
`~/.gemini/config/plugins/`.

We ship a minimal `plugin.json` of `{"name": "mempalace"}`.

---

## 2. Surfaces shipped

| Surface                   | What we ship                                                              |
|---------------------------|---------------------------------------------------------------------------|
| Plugin manifest           | `.antigravity-plugin/plugin.json` — minimal, verified shape               |
| MCP auto-registration     | `.antigravity-plugin/mcp_config.json` — registers `mempalace-mcp` stdio    |
| Skill                     | `.antigravity-plugin/skills/mempalace/SKILL.md` — real file, frontmatter   |
| `Stop` hook               | `hooks/antigravity/mempal_save_hook_antigravity.sh` — counter + auto-mine |
| `PreInvocation` hook      | `hooks/antigravity/mempal_wake_hook_antigravity.sh` — wake injection       |
| Installer                 | `hooks/antigravity/install.sh` — idempotent, basename-match uninstall      |
| User-facing docs          | `website/guide/antigravity.md` + sidebar wiring                            |
| Examples                  | `examples/antigravity/{hooks.json,mcp_config.json,README.md}`              |
| Tests                     | 3 test files mirroring the cursor blueprint                                |

---

## 3. Surfaces deliberately not shipped

### 3.1. `PreCompact` equivalent — NOT SHIPPED

Antigravity's external `hooks.json` does **not** expose a context-compaction event. The Python SDK has an in-process `@hooks.on_compaction`
decorator (see `~/.gemini/config/plugins/google-antigravity-sdk/examples/getting_started/hooks.md`),
but that fires inside a Python `LocalAgentConfig`-built agent, not the
IDE itself. There is no way to subscribe to compaction from a
plugin's `hooks.json`.

UX consequence: long conversations can auto-compact without a save
checkpoint. The `Stop` hook still catches the conversation when the
user actually ends the turn, so the worst case is that some mid-turn
state is lost on auto-compaction. Verbatim transcript ingestion via
the `Stop` path covers the long-term recall use case.

### 3.2. Slash-commands / `commands/` — NOT SHIPPED

Antigravity has no `commands/` plugin component. The Cursor and Codex
integrations both ship five quick-reference commands
(`mempalace-help`, `-init`, `-mine`, `-search`, `-status`) that point
at `mempalace instructions <cmd>`. Those have been folded into the
`SKILL.md` `## Common operations` section so the agent gets the same
quick-reference content via Antigravity's progressive-disclosure skill
loading. No new files, no rule-noise, same discoverability.

### 3.3. `rules/` — NOT SHIPPED

`rules/<name>.md` files are evaluated as constraints on the agent's
behavior. Shipping rules from MemPalace risks colliding with the
user's existing project rules (e.g. `.agents/rules/*.md` files the
user has already authored). Users who want strict MemPalace-related
rules can drop them into their own `<workspace>/.agents/rules/`
directory; we do not impose them.

### 3.4. Workspace-level `.agents/plugins/mempalace/` install — NOT SHIPPED BY DEFAULT

The installer writes to the global location at
`~/.gemini/config/plugins/mempalace/`. Workspace-scoped installs are
documented in `hooks/antigravity/README.md` for users who want to
limit MemPalace to one workspace; they can `cp -r .antigravity-plugin
<workspace>/.agents/plugins/mempalace`. We do not install there
automatically because the canonical UX is global.

### 3.5. `permissions` field in `plugin.json` — NOT SHIPPED

Antigravity permissions are user-side (`Allow` / `Deny` / `Ask`
lists). Plugin manifests do not declare permissions. The
`"permissions": [...]` field documented in the third-party
"antigravity-plugins" community skill is fabricated; no
Google-shipped plugin uses it.

### 3.6. `PreToolUse` / `PostToolUse` hooks — NOT SHIPPED

These would let MemPalace observe every tool call (e.g.
auto-extract entities after each `write_to_file`). Out of scope
for v1; the hook surface is real and could be added in a future
PR if there is demand. Documenting the omission here so a future
contributor doesn't conclude the hooks weren't supported.

---

## 4. Cross-checks against the user's running Antigravity

The user (`robs@`) has Antigravity 2.0 installed. The following live
artifacts on this machine corroborate the published docs:

| Path                                                                   | Confirms                                                       |
|------------------------------------------------------------------------|----------------------------------------------------------------|
| `~/.gemini/antigravity/mcp_config.json`                                | MCP config path + standard `mcpServers` shape                  |
| `~/.gemini/antigravity/skills/<skill-name>/SKILL.md` (multiple)        | Global skill discovery path                                     |
| `~/.gemini/config/plugins/firebase/plugin.json`                        | Real `plugin.json` shape (no `permissions` field)              |
| `~/.gemini/config/plugins/chrome-devtools-plugin/skills/.../SKILL.md`  | In-plugin skill discovery `<plugin>/skills/<name>/SKILL.md`    |
| `~/.gemini/config/plugins/google-antigravity-sdk/examples/.../hooks.md` | SDK-side compaction hook is in-process Python only            |
| Existing `mempalace` entry in `mcp_config.json`                        | `mempalace-mcp` already running and discoverable               |

---

## 5. Reference URLs (all 2026-05-27)

- `https://antigravity.google/docs/plugins`
- `https://antigravity.google/docs/hooks?app=antigravity`
- `https://antigravity.google/docs/skills`
- `https://antigravity.google/docs/mcp`
- `https://antigravity.google/docs/permissions`
- `https://antigravity.google/docs/subagents`
- `https://antigravity.google/blog/introducing-google-antigravity-sdk`
