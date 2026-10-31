/**
 * mempalace-auto.js — Opencode plugin that auto-initializes MemPalace in every project.
 *
 * Behavior (once per project per opencode process):
 *   1. On first `event` for a project in an opencode process, detect the git repo root of ctx.directory.
 *   2. Spawn `mempalace init --yes <root>` in the background (detached, unref).
 *   3. After init succeeds, spawn `mempalace mine --limit 200 <root>` in the background.
 *   4. Inject the MemPalace Memory Protocol section into the system prompt on every chat
 *      via `experimental.chat.system.transform` — so every agent knows when/how to use it.
 *
 * Why this approach:
 *   - No AGENTS.md patching — the protocol lives in memory, not on disk.
 *   - No shell wrappers — uses native opencode plugin hooks.
 *   - Idempotent — `mempalace init --yes` is safe to run multiple times.
 *   - Skipped for non-git directories (~, /tmp, Downloads, etc.).
 *   - Per-opencode-process dedup via in-memory Set.
 *
 * Install:
 *   Drop this file into ~/.config/opencode/plugins/ and restart opencode.
 *   No opencode.json change needed — local plugins are auto-loaded.
 *
 * Logs:
 *   /tmp/mempalace-auto.log — stdout/stderr of spawned init/mine processes.
 */

import { spawn, execSync } from 'node:child_process';
import { openSync, writeSync, closeSync } from 'node:fs';

const MEMPALACE_PROTOCOL = `## MemPalace Memory Protocol

This project uses MemPalace for persistent AI memory across sessions. The MCP server is configured globally — all agents have access to 19 memory tools automatically.

### When to Save Memories
- **After completing a significant task**: Save what was done, decisions made, and why
- **After debugging sessions**: Save the root cause, fix, and patterns observed
- **When making architectural decisions**: Save the decision, alternatives considered, and rationale
- **Before ending a long session**: Save key context that the next session will need

### How to Save
Use \`mempalace_add_drawer\` with:
- \`wing\`: "{your-project-name}" (this project's wing)
- \`room\`: Appropriate topic slug (e.g., "auth-migration", "deploy-config", "bug-fixes")
- \`content\`: Verbatim content — exact words, decisions, code snippets. Never summarize.

### How to Recall
- **Before starting work**: Call \`mempalace_search\` with the topic you're working on
- **When unsure about past decisions**: Search for the decision topic
- **When context seems missing**: Check \`mempalace_kg_query\` for entity relationships

### Agent Diary
Each agent can maintain a personal diary via \`mempalace_diary_write\` / \`mempalace_diary_read\`. Use this for session-level notes, observations, and learnings.

### Available Tools (19 total)
- Palace read: \`mempalace_status\`, \`mempalace_search\`, \`mempalace_list_wings\`, \`mempalace_list_rooms\`, \`mempalace_get_taxonomy\`, \`mempalace_check_duplicate\`, \`mempalace_get_aaak_spec\`
- Palace write: \`mempalace_add_drawer\`, \`mempalace_delete_drawer\`
- Knowledge Graph: \`mempalace_kg_query\`, \`mempalace_kg_add\`, \`mempalace_kg_invalidate\`, \`mempalace_kg_timeline\`, \`mempalace_kg_stats\`
- Navigation: \`mempalace_traverse\`, \`mempalace_find_tunnels\`, \`mempalace_graph_stats\`
- Diary: \`mempalace_diary_write\`, \`mempalace_diary_read\`
`;

const LOG_FILE = '/tmp/mempalace-auto.log';

// Dedup across an entire opencode process lifetime.
const initializedProjects = new Set();
const initializingProjects = new Set();
const miningProjects = new Set();

function log(msg) {
  try {
    // Append a timestamped line to the log file without blocking.
    const line = `[${new Date().toISOString()}] ${msg}\n`;
    // We intentionally use sync append here — it's ~1 syscall per line and only fires
    // on init/mine transitions, not on every event.
    // writeSync/closeSync imported at module top.
    const fd = openSync(LOG_FILE, 'a');
    writeSync(fd, line);
    closeSync(fd);
  } catch {
    // Logging must never crash the plugin.
  }
}

function findGitRoot(startDir) {
  if (!startDir) return null;
  try {
    const root = execSync('git rev-parse --show-toplevel', {
      cwd: startDir,
      stdio: ['ignore', 'pipe', 'ignore'],
      timeout: 2000,
    })
      .toString()
      .trim();
    return root || null;
  } catch {
    return null;
  }
}

function spawnDetached(cmd, args, tag) {
  try {
    const out = openSync(LOG_FILE, 'a');
    const err = openSync(LOG_FILE, 'a');
    const child = spawn(cmd, args, {
      detached: true,
      stdio: ['ignore', out, err],
    });
    closeSync(out);
    closeSync(err);
    child.on('error', (e) => log(`[${tag}] spawn error: ${e.message}`));
    child.unref();
    return child;
  } catch (e) {
    log(`[${tag}] spawn threw: ${e?.message ?? e}`);
    return null;
  }
}

function ensureInitialized(projectRoot) {
  if (
    initializedProjects.has(projectRoot) ||
    initializingProjects.has(projectRoot) ||
    miningProjects.has(projectRoot)
  ) {
    return;
  }
  initializingProjects.add(projectRoot);

  log(`init: ${projectRoot}`);
  const init = spawnDetached('mempalace', ['init', '--yes', projectRoot], 'init');
  if (!init) {
    initializingProjects.delete(projectRoot);
    return;
  }

  let settled = false;
  const finalize = (code, reason) => {
    if (settled) return;
    settled = true;
    initializingProjects.delete(projectRoot);
    if (code === 0) {
      log(`init ok → mine: ${projectRoot}`);
      miningProjects.add(projectRoot);
      const mine = spawnDetached('mempalace', ['mine', '--limit', '200', projectRoot], 'mine');
      if (!mine) {
        miningProjects.delete(projectRoot);
        log(`mine failed error=spawn-error: ${projectRoot}`);
        return;
      }

      let mineSettled = false;
      const finalizeMine = (mineCode, mineReason) => {
        if (mineSettled) return;
        mineSettled = true;
        miningProjects.delete(projectRoot);
        if (mineCode === 0) {
          initializedProjects.add(projectRoot);
          log(`mine ok: ${projectRoot}`);
        } else {
          initializedProjects.delete(projectRoot);
          log(`mine failed ${mineReason}=${mineCode}: ${projectRoot}`);
        }
      };

      mine.on('error', (error) => {
        finalizeMine(error?.code ?? error?.message ?? 'spawn-error', 'error');
      });

      mine.on('exit', (mineCode) => {
        finalizeMine(mineCode, 'code');
      });

      mine.on('close', (mineCode) => {
        finalizeMine(mineCode, 'close');
      });
    } else {
      log(`init failed ${reason}=${code}: ${projectRoot}`);
    }
  };

  init.on('error', (error) => {
    finalize(error?.code ?? error?.message ?? 'spawn-error', 'error');
  });

  init.on('exit', (code) => {
    finalize(code, 'code');
  });

  init.on('close', (code) => {
    finalize(code, 'close');
  });
}

export const MempalaceAutoPlugin = async (ctx) => {
  const projectRoot = findGitRoot(ctx?.directory);
  if (projectRoot) {
    log(`plugin loaded, project=${projectRoot}`);
  } else {
    log(`plugin loaded, no git repo at ${ctx?.directory ?? '?'} — passive mode`);
  }

  return {
    event: async () => {
      if (projectRoot) ensureInitialized(projectRoot);
    },

    'experimental.chat.system.transform': async (_input, output) => {
      if (!projectRoot) return;
      // Avoid double-injection if already present.
      if (output.system.some((entry) => entry.includes('MemPalace Memory Protocol'))) return;
      output.system.push(MEMPALACE_PROTOCOL);
    },
  };
};

export default MempalaceAutoPlugin;
