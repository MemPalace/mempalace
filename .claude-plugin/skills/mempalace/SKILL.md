---
name: mempalace
description: Install, configure, and operate MemPalace, including a private local palace, a shared-brain hub, or a client joining an existing hub. Use for first-time setup, MCP wiring, mining, status, wings, rooms, drawers, shared-brain identity, or logstream readiness.
---

# MemPalace Setup

A guided, skill-first setup for a searchable memory palace. The user may have
installed this skill with `npx skills add` before the MemPalace Python package
or MCP server exists; that is the normal bootstrap path.

## Setup protocol

### 1. Inspect before changing anything

- Detect the OS and current agent harness.
- Run `mempalace --version`, `uv --version`, and an appropriate Python version
  check. Do not assume that an installed Python package is reachable on PATH.
- Check for an existing palace and MCP registration. Never reinitialize or
  rebuild an existing palace just to make setup simpler.

### 2. Install the CLI when necessary

Prefer an isolated `uv` tool installation:

```bash
uv tool install mempalace
```

If `uv` is unavailable, use the PATH-visible Python installation:

```bash
python -m pip install mempalace
```

After installation, run `mempalace --version`. If it still is not reachable,
fix PATH or use the matching `uv tool run` invocation before continuing.

### 3. Choose the topology with the user

Ask which outcome they want unless it is already clear:

1. **private local palace** — one machine, local stdio MCP;
2. **shared-brain hub** — this machine owns the palace and serves the fleet;
3. **client joining an existing hub** — this machine connects to a hub owned
   elsewhere.

Also ask which project or conversation corpus should be initialized, offering
the current working directory as the default. A shared-brain client does not
initialize a second copy of the owner's palace.

### 4. Run version-correct initialization

MemPalace provides dynamic, version-correct instructions via the CLI. To get instructions for any operation:

```bash
mempalace instructions <command>
```

Where `<command>` is one of: `help`, `init`, `mine`, `search`, `status`.

Run the appropriate instructions command, then follow the returned instructions step by step.

For a new local palace or hub, follow `mempalace instructions init`, configure
the selected corpus, then verify with `mempalace status`. For a client, skip
local initialization and obtain the hub URL and bearer token from the user.

### 5. Configure MCP

For local stdio integrations, use the command printed by `mempalace mcp`.
Typical registrations are:

```bash
claude mcp add mempalace -- mempalace-mcp
codex mcp add mempalace -- mempalace-mcp
```

For a shared-brain hub, guide the user through `mempalace serve` and the
[official shared-brain guide](https://mempalaceofficial.com/guide/shared-brain.html). Do not expose a non-loopback server without
authentication. For a client joining an existing hub, configure the harness's
HTTP MCP transport with the supplied bearer token; never print or store that
token in project instructions, drawers, or logstream events.

Restart or reconnect the harness when required, then verify that the live MCP
tool list includes MemPalace tools. Package installation alone is not proof
that MCP is connected.

### 6. Configure shared-brain identity and coordination

When shared-brain mode is selected:

- Agree on one stable `<machine>-<harness>` identity for this agent.
- Render the canonical rules with:

  ```bash
  mempalace rules --agent <machine-harness>
  ```

- Install the rendered marker-delimited block in the harness's durable agent
  instructions. Replace an existing marked block instead of appending a
  duplicate.
- Check coordination access with a read-only `mempalace logstream list` or the
  equivalent MCP event-list call.
- Ask whether the harness can maintain a background watcher. If it can, prepare
  the documented `mempalace logstream watch --agent ... --state-file ...`
  command for a local palace owner or synchronized replica. A remote-only MCP
  client must instead use repeated `mempalace_event_wait` calls, preserving the
  last event id as `since_event_id`; never point it at a local SQLite watcher.
  Explain any permission allowlisting needed. If it cannot maintain either
  loop, record that the agent is turn-based and must sweep its MCP inbox with
  `mempalace_event_list` on wake-up.

Do not post a test event without telling the user: logstream events are
immutable. If the user approves a smoke event, address it narrowly and close
the loop with an acknowledgement.

### 7. Report readiness

Summarize the installed version, palace location or hub URL (without secrets),
MCP connection, stable agent identity, watcher mode, and the first safe next
action. For active delegation, hand off to the `mempalace-task` skill.

## Recalling past work

This skill covers setup, mining, and status. For questions about past
work, prior decisions, or people that may already be filed in the
palace, prefer the **`mempalace-recall`** skill — it enforces
search-before-answer so the agent reads the palace instead of guessing.

## Cursor-specific notes

- The Cursor plugin auto-registers `mempalace-mcp`; a standalone `npx skills
  add` installation does not. Always verify the live tool list.
- For automatic background saving every N agent turns plus session-start memory recall, also install the Cursor hooks separately by running `hooks/cursor/install.sh --scope user` from a cloned MemPalace repo. See the [Cursor hooks guide](https://mempalaceofficial.com/guide/cursor-hooks.html) for the full walkthrough.
- The recommended `agent_name` when calling `mempalace_diary_write` from a Cursor session is `cursor-ide` (matches the precedent of `claude-code` and `codex`).

## Canonical references

- [Shared brain](https://mempalaceofficial.com/guide/shared-brain.html)
- [Coordination protocol](https://github.com/MemPalace/mempalace/blob/main/integrations/shared/coordination-protocol.md)
- [Recall protocol](https://github.com/MemPalace/mempalace/blob/main/integrations/shared/recall-protocol.md)
