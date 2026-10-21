# Claude Code Retention Setup

Time-sensitive checklist for Claude Code users who want their sessions in
MemPalace before local transcripts age out.

## Why This Matters

Claude Code conversations are stored as JSONL transcripts under:

```bash
~/.claude/projects/
```

Do not assume those transcripts are permanent. If you have important Claude
Code work, wire auto-save hooks and backfill existing transcripts now.

Codex CLI users can still backfill their local session files, but the urgent
Claude Code transcript-retention window does not apply the same way to Codex
local state.

## Fast Path

From a local clone of this repository:

```bash
pip install mempalace
chmod +x hooks/mempal_save_hook.sh hooks/mempal_precompact_hook.sh
```

Add the hooks to `.claude/settings.local.json`:

```json
{
  "hooks": {
    "Stop": [{
      "matcher": "*",
      "hooks": [{
        "type": "command",
        "command": "/absolute/path/to/mempalace/hooks/mempal_save_hook.sh",
        "timeout": 30
      }]
    }],
    "PreCompact": [{
      "hooks": [{
        "type": "command",
        "command": "/absolute/path/to/mempalace/hooks/mempal_precompact_hook.sh",
        "timeout": 30
      }]
    }]
  }
}
```

Restart Claude Code after editing the settings file. Claude Code loads hooks at
session start.

## Back Up Existing JSONL Files

Run the read-only backup script:

```bash
tools/backup_claude_jsonls.sh
```

This copies transcripts from `~/.claude/projects/` to
`~/Documents/Claude_JSONL_Backup/` and verifies the JSONL count. It does not
modify or delete files inside `~/.claude/`.

## Backfill Existing Sessions Into MemPalace

After backing up, mine the current Claude Code transcript directory:

```bash
mempalace mine ~/.claude/projects/ --mode convos
```

If you also backed up old transcripts:

```bash
mempalace mine ~/Documents/Claude_JSONL_Backup/ --mode convos
```

## Manual Save Command

If hooks are not wired yet, use the slash-command template in:

```bash
tools/save.md
```

It describes a manual `/save` flow that mines the current Claude Code JSONL
transcript into MemPalace. This is a stopgap, not a replacement for hooks.

## Find Older Copies

If you used cloud sync or manual backups, orphan transcripts may still exist
outside `~/.claude/projects/`:

```bash
tools/find_orphan_claude_jsonls.sh
```

The script is read-only. It scans common backup locations and prints candidate
JSONL files with a short topic preview.

## Verify

Search for something you know appears in an old session:

```bash
mempalace search "phrase from an old Claude Code session"
```

Check hook logs after a new session:

```bash
cat ~/.mempalace/hook_state/hook.log
```

## Notes

- The hooks only protect future sessions after Claude Code is restarted.
- `mempalace mine ... --mode convos` is idempotent; re-running it is safe.
- Keep private transcripts private. Do not upload JSONL files to public issues,
  discussions, or gists.
