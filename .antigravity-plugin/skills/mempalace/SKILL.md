---
name: mempalace
description: MemPalace — mine projects and conversations into a searchable memory palace. Use when the user asks about MemPalace, memory palace, mining memories, searching memories, palace setup, wings, rooms, or drawers; or when they want to recall past work that may already be filed in their palace.
---

# MemPalace

A searchable memory palace for AI — mine projects and conversations, then search them semantically. Verbatim storage, local-first, zero external API by default.

## Prerequisites

Ensure `mempalace` is installed:

```bash
mempalace --version
```

If not installed (uv recommended):

```bash
uv tool install mempalace   # or: pip install mempalace
```

## Dynamic, version-correct instructions

MemPalace exposes operation-specific instructions through the CLI so this skill stays accurate as MemPalace evolves. To get instructions for any operation:

```bash
mempalace instructions <command>
```

Always prefer the CLI output over what is written here when the two disagree — the CLI is the single source of truth for the installed version.

## Common operations

These are the five operations users ask for most often. Each one wraps a single MemPalace CLI subcommand. The `mempalace instructions <name>` form returns the full, version-correct guidance.

### `help` — discover what MemPalace can do

```bash
mempalace instructions help
```

Use when the user is new, unsure what's possible, or asks "what can you do".

### `init` — first-run setup of the palace

```bash
mempalace instructions init
```

Use when the user has just installed MemPalace, no palace exists yet, or the user explicitly asks to set up / configure / re-initialize their palace.

### `mine` — ingest a project or conversation directory

```bash
mempalace instructions mine
```

Use when the user wants to fold a project's files into their palace, or to ingest exported conversation transcripts into the palace as searchable memory.

### `search` — find verbatim memories by semantic query

```bash
mempalace instructions search
```

Use when the user wants to recall something from the past, find a previous decision, or rediscover code/notes/conversations they already wrote.

### `status` — what's in the palace right now

```bash
mempalace instructions status
```

Use when the user asks "what's in my palace", "how big is my palace", or wants a summary of wings, rooms, and drawer counts.

## MCP tools (preferred over CLI)

Inside Antigravity, the MemPalace MCP server registers a rich set of tools. Use these instead of shelling out to the CLI for live operations (search, diary writes, drawer adds, knowledge graph queries, palace status). The MCP tools always reflect the current palace state without spawning a subprocess.

The MCP server is auto-registered when this plugin is installed at `~/.gemini/config/plugins/mempalace/`. If the server does not appear in Antigravity's MCP store, run `mempalace-mcp --version` to verify the binary is on PATH, then restart Antigravity.

## Design principles (verbatim from the project)

- **Verbatim always** — never summarize, paraphrase, or lossy-compress user data.
- **Local-first, zero external API by default** — extraction, embedding, and LLM-assisted refinement happen on the user's machine.
- **Privacy by architecture** — the system never calls out to external services for core operations.
- **Performance budgets** — hooks under 500ms; startup injection under 100ms.
- **Background everything** — filing, indexing, and timestamps happen via hooks in the background; zero tokens spent on bookkeeping in the chat window.

If a request would violate any of these principles, refuse and explain — even if it would be technically convenient.
