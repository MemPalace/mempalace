---
name: mempalace
description: Install, configure, and operate MemPalace, including a private local palace, a shared-brain hub, or a client joining an existing hub. Use for first-time setup, MCP wiring, mining, status, palace audit and repair, wings, rooms, drawers, shared-brain identity, or logstream readiness.
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

- Agree on a stable `host:harness:project` identity: lowercase host label
  (machine), harness family (`claude`, `codex`, `grok`, `antigravity`, …),
  and the current workspace as project. Two windows in the same project
  are one actor.
- Render the canonical rules with:

  ```bash
  mempalace rules --host <host> --harness <harness> --project <example>
  ```

  Default `--mcp full` matches the 47-tool `mempalace-mcp` server this
  skill registers. If the user opted into `mempalace-light-mcp`, re-render
  with `--mcp light` instead. Replace an existing
  `<!-- mempalace-shared-brain -->` block instead of appending a duplicate.
- Install the rendered marker-delimited block in the harness's durable agent
  instructions (Claude `~/.claude/CLAUDE.md`, Codex `~/.codex/AGENTS.md`,
  Grok `~/.grok/AGENTS.md`, Antigravity `~/.gemini/config/GEMINI.md`).
- Check coordination access with a read-only `mempalace logstream list` or the
  equivalent MCP event-list call.
- Interactive sessions are declared-idle: they sweep the inbox on collab /
  before long tasks and do **not** arm a watcher at session start. Arm
  `mempalace logstream watch --agent <host>:<harness>:<project>` (the CLI
  defaults a sanitized `--state-file`) only when the user asked to listen,
  the agent claimed a task, or it delegated. A remote-only MCP client must
  instead loop on `mempalace_event_wait`, preserving the last event id as
  `since_event_id`; never point it at a local SQLite watcher. Explain any
  permission allowlisting needed. If it cannot maintain either loop, record
  that the agent is turn-based and must sweep its MCP inbox with
  `mempalace_event_list` on wake-up.

Do not post a test event without telling the user: logstream events are
immutable. If the user approves a smoke event, address it narrowly and close
the loop with an acknowledgement.

### 7. Report readiness

Summarize the installed version, palace location or hub URL (without secrets),
MCP connection, stable agent identity, watcher mode, and the first safe next
action. For active delegation, hand off to the `mempalace-task` skill.

Ask whether the user wants weekly stable-release checks. The default is no.
Explain that enabling them contacts PyPI but sends no palace content, identity,
or telemetry. When enabling, record the installer actually used with
`mempalace update configure --enable --installer uv-tool` (or `pipx` / `pip`);
use `--disable` to opt out. Checks never install anything. In
`mempalace_status`, treat `updates.server` as the palace-serving runtime and
`updates.client` (when present) as the local proxy runtime; do not conflate
their versions or installers. For a client update, use the local `mempalace
update plan`. A remote server update is informational on the client: surface it
naturally and ask the hub operator to prepare and authorize the plan on the
palace-serving machine. Never use a client-generated plan to upgrade the
server, and never execute any plan without explicit approval.

## Palace health: audit and repair session

When the user asks how well organized the palace is, whether memory is
"messy", why a scoped search or wake-up misses things, or invokes
`/mempalace:audit`, run the audit and then offer a repair session:

```bash
mempalace instructions audit
```

Follow the returned instructions. In short: run `mempalace audit --json`
(read-only, safe while the MCP server is running), present the five layer
scores and findings, then walk the user through repairs **one structured
question at a time** with a recommended option first: merging wings and
rooms spelled two ways, folding stub wings, deleting tunnels on generic
tokens and self-link hallways, agreeing a knowledge-graph predicate
vocabulary, and giving flat wings a closed room set with
`mempalace rooms propose` / `apply`. Moves over deletions, numbers before actions, verbatim content
always. Re-run the audit at the end and write a diary entry with the
before and after scores and every decision made.

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
