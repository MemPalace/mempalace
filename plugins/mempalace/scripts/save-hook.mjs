#!/usr/bin/env node
/**
 * MEMPALACE ANTIGRAVITY SAVE HOOK — Stop event handler (cross-platform Node.js)
 *
 * Fires when Antigravity's agent execution loop stops. Counts turns per
 * conversation and spawns a detached background `mempalace mine --mode convos`
 * every Nth turn (`MEMPAL_SAVE_INTERVAL`, default 15).
 *
 * Always writes `{}` to stdout and exits 0 (never `{"decision":"continue"}`,
 * which would force an infinite loop).
 */

import { spawn, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  emitStopPass,
  ensureStateDir,
  gcStaleState,
  hookLog,
  inferWing,
  isKillSwitchTripped,
  isValidTranscriptPath,
  parseHookStdin,
  readStdinSync,
  resolveHome,
  resolvePythonBin,
  resolveSaveInterval,
  resolveStateDir,
  writeCounterAtomic,
} from './hook-common.mjs';

const SELF_PATH = fileURLToPath(import.meta.url);

function runBackgroundWorker(args) {
  const [pythonBin, transcriptDir, wing, pendingFile, stateDir, convId] = args;
  const logFile = path.join(stateDir, 'antigravity_hook.log');
  try {
    const probe = spawnSync(pythonBin, ['-m', 'mempalace', '--version'], {
      stdio: 'ignore',
      timeout: 15_000,
      windowsHide: true,
    });
    if (probe.status === 0) {
      let logFd = null;
      try {
        logFd = fs.openSync(logFile, 'a');
      } catch {
        // Fall back to ignore stdio if log cannot be opened
      }
      spawnSync(
        pythonBin,
        ['-m', 'mempalace', 'mine', transcriptDir, '--mode', 'convos', '--wing', wing],
        {
          stdio: ['ignore', logFd ?? 'ignore', logFd ?? 'ignore'],
          timeout: 300_000,
          windowsHide: true,
        }
      );
      if (logFd !== null) {
        try {
          fs.closeSync(logFd);
        } catch {
          // Ignore close error
        }
      }
      hookLog(stateDir, 'stop', convId, `background mine finished wing=${wing}`);
    } else {
      hookLog(
        stateDir,
        'stop',
        convId,
        `ERROR: mempalace is not runnable via ${pythonBin} -m mempalace; install mempalace or set MEMPAL_PYTHON`
      );
    }
  } catch (err) {
    hookLog(stateDir, 'stop', convId, `ERROR: background mine failed: ${err?.message || err}`);
  } finally {
    try {
      fs.rmSync(pendingFile, { force: true });
    } catch {
      // Ignore cleanup error
    }
  }
}

function main() {
  if (process.argv[2] === '--background-mine') {
    runBackgroundWorker(process.argv.slice(3));
    process.exit(0);
  }

  const rawInput = readStdinSync();
  const home = resolveHome();

  if (isKillSwitchTripped(home)) {
    emitStopPass();
    process.exit(0);
  }

  const stateDir = resolveStateDir(home);
  ensureStateDir(stateDir);
  gcStaleState(stateDir);

  let parsed;
  try {
    if (!rawInput.trim()) {
      throw new Error('Empty stdin');
    }
    parsed = parseHookStdin(rawInput, home);
  } catch (err) {
    if (rawInput) {
      hookLog(
        stateDir,
        'stop',
        'unknown',
        'input parse failed; see antigravity_last_input.log'
      );
      try {
        const lastInputPath = path.join(stateDir, 'antigravity_last_input.log');
        fs.writeFileSync(lastInputPath, rawInput.slice(0, 4096), { encoding: 'utf-8', mode: 0o600 });
      } catch {
        // Fail-open
      }
    }
    emitStopPass();
    process.exit(0);
  }

  const {
    conversationId,
    transcriptPath,
    workspacePath,
    executionNum,
    terminationReason,
    fullyIdle,
  } = parsed;

  if (!fullyIdle) {
    hookLog(
      stateDir,
      'stop',
      conversationId,
      `deferring save: fullyIdle=False (executionNum=${executionNum}, terminationReason=${terminationReason})`
    );
    emitStopPass();
    process.exit(0);
  }

  if (terminationReason === 'error') {
    hookLog(stateDir, 'stop', conversationId, 'skipping save: terminationReason=error');
    emitStopPass();
    process.exit(0);
  }

  const counterFile = path.join(stateDir, `antigravity_save_count_${conversationId}`);
  let count = 0;
  try {
    if (fs.existsSync(counterFile)) {
      const rawCount = fs.readFileSync(counterFile, 'utf-8').trim();
      if (/^\d+$/.test(rawCount)) {
        count = Number.parseInt(rawCount, 10);
      }
    }
  } catch {
    count = 0;
  }
  count += 1;
  writeCounterAtomic(counterFile, count);

  const interval = resolveSaveInterval();
  hookLog(
    stateDir,
    'stop',
    conversationId,
    `count=${count} interval=${interval} executionNum=${executionNum} workspace=${workspacePath}`
  );

  if (count % interval !== 0) {
    emitStopPass();
    process.exit(0);
  }

  const pendingFile = path.join(stateDir, `antigravity_pending_${conversationId}`);
  try {
    if (fs.existsSync(pendingFile)) {
      const st = fs.statSync(pendingFile);
      if (Date.now() - st.mtimeMs < 3_600_000) {
        hookLog(stateDir, 'stop', conversationId, 'pending save still in flight; skipping');
        emitStopPass();
        process.exit(0);
      }
      hookLog(stateDir, 'stop', conversationId, 'stale pending marker reclaimed');
      fs.rmSync(pendingFile, { force: true });
    }
  } catch {
    // Fail-open
  }

  if (!isValidTranscriptPath(transcriptPath)) {
    hookLog(
      stateDir,
      'stop',
      conversationId,
      `invalid transcriptPath rejected: ${transcriptPath}`
    );
    emitStopPass();
    process.exit(0);
  }

  let isTranscriptFile = false;
  try {
    isTranscriptFile = fs.statSync(transcriptPath).isFile();
  } catch {
    isTranscriptFile = false;
  }
  if (!isTranscriptFile) {
    hookLog(
      stateDir,
      'stop',
      conversationId,
      `transcriptPath does not exist: ${transcriptPath}`
    );
    emitStopPass();
    process.exit(0);
  }

  const wing = inferWing(workspacePath, 'sessions');
  const transcriptDir = path.dirname(transcriptPath);
  const pythonBin = resolvePythonBin(home);

  hookLog(
    stateDir,
    'stop',
    conversationId,
    `TRIGGERING SAVE wing=${wing} transcript_dir=${transcriptDir}`
  );

  try {
    fs.writeFileSync(pendingFile, '', 'utf-8');
  } catch {
    // Continue best-effort
  }

  hookLog(
    stateDir,
    'stop',
    conversationId,
    `spawning background mine wing=${wing} transcript_dir=${transcriptDir}`
  );

  try {
    const child = spawn(
      process.execPath,
      [
        SELF_PATH,
        '--background-mine',
        pythonBin,
        transcriptDir,
        wing,
        pendingFile,
        stateDir,
        conversationId,
      ],
      {
        detached: true,
        stdio: 'ignore',
        windowsHide: true,
      }
    );
    child.unref();
  } catch (err) {
    hookLog(stateDir, 'stop', conversationId, `ERROR: failed to spawn background worker: ${err}`);
    try {
      fs.rmSync(pendingFile, { force: true });
    } catch {
      // Ignore
    }
  }

  emitStopPass();
  process.exit(0);
}

main();
