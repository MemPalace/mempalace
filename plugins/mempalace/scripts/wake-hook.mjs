#!/usr/bin/env node
/**
 * MEMPALACE ANTIGRAVITY WAKE HOOK — PreInvocation event handler (cross-platform Node.js)
 *
 * Fires before each model invocation, gated strictly to `invocationNum === 1`
 * (first turn of a conversation) plus an atomic directory loop guard.
 * Runs `mempalace wake-up --wing <wing>` with a 500ms hard timeout and returns
 * verbatim palace memory via `{"injectSteps":[{"ephemeralMessage":"..."}]}`.
 */

import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import {
  emitStopPass,
  ensureStateDir,
  hookLog,
  inferWing,
  isKillSwitchTripped,
  parseHookStdin,
  readStdinSync,
  resolveHome,
  resolvePythonBin,
  resolveStateDir,
} from './hook-common.mjs';

function main() {
  const rawInput = readStdinSync();
  const home = resolveHome();

  if (isKillSwitchTripped(home)) {
    emitStopPass();
    process.exit(0);
  }

  const stateDir = resolveStateDir(home);
  ensureStateDir(stateDir);

  let parsed;
  try {
    if (!rawInput.trim()) {
      emitStopPass();
      process.exit(0);
    }
    parsed = parseHookStdin(rawInput, home);
  } catch {
    if (rawInput) {
      hookLog(stateDir, 'preInvocation', 'unknown', 'input parse failed');
    }
    emitStopPass();
    process.exit(0);
  }

  const { conversationId, workspacePath, invocationNum } = parsed;

  // Gate strictly to the first model invocation of the conversation
  if (invocationNum !== 1) {
    emitStopPass();
    process.exit(0);
  }

  // Atomic loop guard: mkdir without `recursive` fails with EEXIST if already created
  const wokeMarker = path.join(stateDir, `antigravity_woke_${conversationId}`);
  try {
    fs.mkdirSync(wokeMarker);
  } catch {
    hookLog(stateDir, 'preInvocation', conversationId, 'already woke this conversation; skipping');
    emitStopPass();
    process.exit(0);
  }

  const wing = inferWing(workspacePath, 'sessions');
  const pythonBin = resolvePythonBin(home);
  hookLog(
    stateDir,
    'preInvocation',
    conversationId,
    `WAKE injection wing=${wing} invocationNum=${invocationNum}`
  );

  try {
    const res = spawnSync(pythonBin, ['-m', 'mempalace', 'wake-up', '--wing', wing], {
      encoding: 'utf-8',
      timeout: 500,
      windowsHide: true,
      env: { ...process.env, PYTHONUTF8: '1' },
    });

    if (res.error || res.status !== 0) {
      emitStopPass();
      process.exit(0);
    }

    const body = (res.stdout || '').replace(/\r\n/g, '\n').trim();
    if (!body) {
      emitStopPass();
      process.exit(0);
    }

    const payload = JSON.stringify({ injectSteps: [{ ephemeralMessage: body }] });
    if (payload.includes('"decision"')) {
      hookLog(
        stateDir,
        'preInvocation',
        conversationId,
        'ERROR: refused to emit decision field from wake hook'
      );
      emitStopPass();
      process.exit(0);
    }

    process.stdout.write(`${payload}\n`);
    process.exit(0);
  } catch {
    emitStopPass();
    process.exit(0);
  }
}

main();
