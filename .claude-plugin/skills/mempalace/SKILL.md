---
name: mempalace
description: MemPalace — mine projects and conversations into a searchable memory palace. Use when asked about mempalace, memory palace, mining memories, searching memories, or palace setup.
allowed-tools: Bash, Read, Write, Edit, Glob, Grep
---

# MemPalace

A searchable memory palace for AI — mine projects and conversations, then search them semantically.

## Prerequisites

Ensure `mempalace` is installed:

```bash
mempalace --version
```

If not installed (uv recommended):

```bash
uv tool install mempalace   # or: pip install mempalace
```

## Usage

MemPalace provides dynamic instructions via the CLI. To get instructions for any operation:

```bash
mempalace instructions <command>
```

Where `<command>` is one of: `help`, `init`, `mine`, `search`, `status`.

Run the appropriate instructions command, then follow the returned instructions step by step.
