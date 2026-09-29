# How to Use MemPalace Hooks (Auto-Save)

MemPalace hooks act as an "Auto-Save" feature. They help your AI keep a permanent memory without you needing to run manual commands.

### 1. What are these hooks?
* **Save Hook** (`mempal_save_hook.sh`): Saves new facts and decisions every 15 messages.
* **PreCompact Hook** (`mempal_precompact_hook.sh`): Saves your context right before the AI's memory window fills up.

### 2. Setup for Claude Code
Add this to `~/.claude/settings.local.json` (global) or `.claude/settings.local.json` (project-scoped) to enable automatic background saving:

```json
{
  "hooks": {
    "Stop": [
      {
        "matcher": "*", 
        "hooks": [{
          "type": "command",
          "command": "/absolute/path/to/hooks/mempal_save_hook.sh",
          "timeout": 30
        }]
      }
    ],
    "PreCompact": [
      {
        "hooks": [{
          "type": "command",
          "command": "/absolute/path/to/hooks/mempal_precompact_hook.sh",
          "timeout": 30
        }]
      }
    ]
  }
}
```

Make the hooks executable:
```bash
chmod +x /absolute/path/to/hooks/mempal_save_hook.sh
chmod +x /absolute/path/to/hooks/mempal_precompact_hook.sh
```

**Note:** Replace `/absolute/path/to/hooks/` with the actual path where you cloned the MemPalace repository (e.g., `~/projects/mempalace/hooks/`).

### 3. What changed (v3.1.0+)

Both hooks now have **two-layer capture**:

1. **Auto-mine**: Before blocking the AI, the hook runs the normalizer on the JSONL transcript and upserts chunks directly into the palace. This captures raw tool output (Bash results, search findings, build errors) that the AI would otherwise summarize away.

2. **Updated reason messages**: The block reason now explicitly tells the AI to save tool output verbatim — not just topics and decisions.

### 4. Backfill past conversations (one-time)

The hooks capture conversations going forward, but you probably have months of past sessions. Run this once to mine them all:

```bash
mempalace mine ~/.claude/projects/ --mode convos
```

### 5. Configuration

- **`SAVE_INTERVAL=15`** — How many human messages between saves
- **`MEMPALACE_PYTHON`** — Python interpreter with mempalace + chromadb. Auto-detects: env var → repo venv → system python3
- **`MEMPAL_DIR`** — Optional directory for auto-ingest via `mempalace mine`
